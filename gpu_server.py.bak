import hmac
import os
import tempfile
import threading
from pathlib import Path

import numpy as np
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from faster_whisper import WhisperModel

MODEL_NAME = os.environ.get("WHISPER_MODEL", "medium")
API_KEY = os.environ.get("TRANSCRIBE_API_KEY", "")
SAMPLE_RATE = 16000
MODEL = None
MODEL_LOCK = threading.Lock()
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
    return {"ready": MODEL is not None, "model": MODEL_NAME, "device": "cuda"}


def transcribe_audio(audio):
    if MODEL is None:
        raise HTTPException(status_code=503, detail="GPU model is not ready")
    output = []
    with MODEL_LOCK:
        segments, _ = MODEL.transcribe(audio, language="ja", vad_filter=True, beam_size=5)
        for segment in segments:
            text = segment.text.strip()
            if text:
                output.append({"start": segment.start, "end": segment.end, "text": text})
    return output


@app.post("/transcribe/chunk", dependencies=[Depends(verify_api_key)])
async def transcribe_chunk(request: Request, offset: float = 0.0):
    # Client sends raw little-endian float32 mono samples at 16 kHz.
    body = await request.body()
    if len(body) % 4:
        raise HTTPException(status_code=400, detail="Audio payload must contain float32 samples")
    audio = np.frombuffer(body, dtype="<f4").copy()
    if not len(audio):
        return {"segments": []}
    segments = transcribe_audio(audio)
    for segment in segments:
        segment["start"] += offset
        segment["end"] += offset
    return {"segments": segments}


@app.post("/transcribe/file", dependencies=[Depends(verify_api_key)])
def transcribe_file(file: UploadFile = File(...)):
    suffix = Path(file.filename or "audio.wav").suffix.lower()
    if suffix not in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma"}:
        raise HTTPException(status_code=415, detail="Only audio files are accepted; extract video audio on the client first")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="meeting_audio_", suffix=suffix, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            while data := file.file.read(1024 * 1024):
                temp_file.write(data)
        segments = transcribe_audio(str(temp_path))
        return {"model": MODEL_NAME, "segments": segments}
    finally:
        file.file.close()
        if temp_path:
            temp_path.unlink(missing_ok=True)
