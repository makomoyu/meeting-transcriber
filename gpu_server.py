import hmac
import gc
import os
import tempfile
import threading
from pathlib import Path
# 手順1：WindowsでCUDA関連DLLを検索できるようにする。
# Python 3.8以降では、DLLの検索フォルダを明示的に登録できる。
import sysconfig

# DLL検索用のハンドルを保持し、登録が解除されないようにする。
_CUDA_DLL_HANDLES = []


def configure_cuda_dll_paths():
    """CUDA 12・cuDNN 9関連のDLL検索パスを登録する。"""
    site_packages = Path(sysconfig.get_paths()["purelib"])
    nvidia_dir = site_packages / "nvidia"

    # 手順2：必要なDLLが配置される可能性のあるフォルダを登録する。
    dll_directories = [
        nvidia_dir / "cublas" / "bin",
        nvidia_dir / "cudnn" / "bin",
        nvidia_dir / "cuda_nvrtc" / "bin",
        site_packages / "ctranslate2",
    ]

    for directory in dll_directories:
        if directory.is_dir():
            try:
                _CUDA_DLL_HANDLES.append(
                    os.add_dll_directory(str(directory))
                )
                print(f"CUDA DLL directory registered: {directory}")
            except OSError as exc:
                print(f"CUDA DLL directory registration failed: {exc}")


# 手順3：faster-whisperを読み込む前に実行する。
configure_cuda_dll_paths()

import numpy as np
import numpy as np
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from faster_whisper import WhisperModel

MODEL_NAME = os.environ.get("WHISPER_MODEL", "medium")
API_KEY = os.environ.get("TRANSCRIBE_API_KEY", "")
SAMPLE_RATE = 16000
MODEL = None
MODEL_LOCK = threading.Lock()
ALLOWED_MODELS = {"tiny", "base", "small", "medium"}
app = FastAPI(title="LAN GPU Transcription Service")


def verify_api_key(x_api_key: str = Header(default="")):
    if not API_KEY:
        raise HTTPException(status_code=503, detail="TRANSCRIBE_API_KEY is not configured")
    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.on_event("startup")
def load_gpu_model():
    global MODEL
    # Load once and keep the model resident on the GPU for live and file jobs.
    MODEL = WhisperModel(MODEL_NAME, device="cuda", compute_type="float16")


@app.get("/health", dependencies=[Depends(verify_api_key)])
def health():
    with MODEL_LOCK:
        gpu_available = MODEL is not None
        return {
            "ready": gpu_available,
            "gpu_available": gpu_available,
            "model": MODEL_NAME if gpu_available else None,
            "device": "cuda" if gpu_available else None,
        }


def transcribe_audio(audio, model_name=MODEL_NAME):
    global MODEL, MODEL_NAME
    if model_name not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="Unsupported model")
    output = []
    with MODEL_LOCK:
        if MODEL is None or MODEL_NAME != model_name:
            MODEL = None
            gc.collect()
            MODEL = WhisperModel(model_name, device="cuda", compute_type="float16")
            MODEL_NAME = model_name
        segments, _ = MODEL.transcribe(audio, language="ja", vad_filter=True, beam_size=5)
        for segment in segments:
            text = segment.text.strip()
            if text:
                output.append({"start": segment.start, "end": segment.end, "text": text})
    return output


@app.post("/transcribe/chunk", dependencies=[Depends(verify_api_key)])
async def transcribe_chunk(request: Request, offset: float = 0.0, model: str = Query(default=MODEL_NAME)):
    # Client sends raw little-endian float32 mono samples at 16 kHz.
    body = await request.body()
    if len(body) % 4:
        raise HTTPException(status_code=400, detail="Audio payload must contain float32 samples")
    audio = np.frombuffer(body, dtype="<f4").copy()
    if not len(audio):
        return {"segments": []}
    segments = transcribe_audio(audio, model)
    for segment in segments:
        segment["start"] += offset
        segment["end"] += offset
    return {"segments": segments}


@app.post("/transcribe/file", dependencies=[Depends(verify_api_key)])
def transcribe_file(file: UploadFile = File(...), model: str = Query(default=MODEL_NAME)):
    suffix = Path(file.filename or "audio.wav").suffix.lower()
    if suffix not in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma"}:
        raise HTTPException(status_code=415, detail="Only audio files are accepted; extract video audio on the client first")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="meeting_audio_", suffix=suffix, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            while data := file.file.read(1024 * 1024):
                temp_file.write(data)
        segments = transcribe_audio(str(temp_path), model)
        return {"model": model, "segments": segments}
    finally:
        file.file.close()
        if temp_path:
            temp_path.unlink(missing_ok=True)
