import queue
import subprocess
import threading
import time
import wave
import ctypes
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import mss
import numpy as np
import requests
import soundcard as sc
from PIL import Image, ImageTk

APP_DIR = Path(__file__).resolve().parent
RECORDINGS_DIR = APP_DIR / "recordings"
SAMPLE_RATE = 16000
AUDIO_BLOCK = 0.25
VIDEO_FPS = 8
TRANSCRIBE_SECONDS = 5


def list_windows():
    """Return visible top-level windows as (display label, HWND)."""
    user32 = ctypes.windll.user32
    results = []
    own_title = "会議録画・リアルタイム文字起こし"
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title and own_title not in title:
            results.append((title, int(hwnd)))
        return True

    user32.EnumWindows(callback_type(collect), 0)
    results.sort(key=lambda item: item[0].casefold())
    return results


class CaptureSession:
    def __init__(self, event_queue, mic_id, capture_kind, capture_value, server_url, api_key, backend, gpu_model, cpu_model):
        self.events = event_queue
        self.mic_id = mic_id
        self.capture_kind = capture_kind
        self.capture_value = capture_value
        self.monitor_index = capture_value if capture_kind == "monitor" else None
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.backend = backend
        self.gpu_model = gpu_model
        self.cpu_model = cpu_model
        self.local_model = None
        self.stop_event = threading.Event()
        self.audio_queue = queue.Queue()
        self.threads = []
        self.base = RECORDINGS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.wav_path = self.base.with_suffix(".wav")
        self.video_path = self.base.with_name(self.base.name + "_silent.mp4")
        self.mp4_path = self.base.with_suffix(".mp4")

    def start(self):
        self.stop_event.clear()
        for target in (self._capture_screen, self._capture_audio, self._transcribe_audio):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()

    def _capture_screen(self):
        try:
            if self.capture_kind == "window":
                self._capture_window()
                return
            with mss.MSS() as sct:
                monitor = sct.monitors[self.monitor_index]
                width, height = monitor["width"], monitor["height"]
                writer = cv2.VideoWriter(str(self.video_path), cv2.VideoWriter_fourcc(*"mp4v"), VIDEO_FPS, (width, height))
                if not writer.isOpened():
                    raise RuntimeError("画面動画を作成できません。")
                frame_interval = 1 / VIDEO_FPS
                last_preview = 0
                try:
                    while not self.stop_event.is_set():
                        started = time.monotonic()
                        shot = np.array(sct.grab(monitor))
                        frame = cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)
                        writer.write(frame)
                        if started - last_preview >= 0.5:
                            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                            self.events.put(("preview", rgb))
                            last_preview = started
                        delay = frame_interval - (time.monotonic() - started)
                        if delay > 0:
                            self.stop_event.wait(delay)
                finally:
                    writer.release()
            self.events.put(("screen_done", str(self.video_path)))
        except Exception as exc:
            self.events.put(("capture_error", f"画面録画エラー: {exc}"))
            self.stop_event.set()

    def _capture_window(self):
        from windows_capture import WindowsCapture

        latest = {"frame": None}
        frame_lock = threading.Lock()
        closed = threading.Event()
        capture = WindowsCapture(
            window_hwnd=int(self.capture_value),
            cursor_capture=True,
            draw_border=None,
            minimum_update_interval=50,
        )

        @capture.event
        def on_frame_arrived(frame, _capture_control):
            # Copy the BGRA frame because the capture library owns its backing buffer.
            image = frame.frame_buffer[:, :, :3].copy()
            with frame_lock:
                latest["frame"] = image

        @capture.event
        def on_closed():
            closed.set()

        capture_control = capture.start_free_threaded()
        writer = None
        frame_interval = 1 / VIDEO_FPS
        next_frame_at = time.monotonic()
        last_preview = 0
        try:
            while not self.stop_event.is_set() and not closed.is_set():
                now = time.monotonic()
                if now < next_frame_at:
                    self.stop_event.wait(min(next_frame_at - now, 0.05))
                    continue
                with frame_lock:
                    image = latest["frame"]
                if image is None:
                    self.stop_event.wait(0.05)
                    next_frame_at = time.monotonic()
                    continue
                height, width = image.shape[:2]
                if writer is None:
                    out_width = width - (width % 2)
                    out_height = height - (height % 2)
                    writer = cv2.VideoWriter(
                        str(self.video_path), cv2.VideoWriter_fourcc(*"mp4v"),
                        VIDEO_FPS, (out_width, out_height)
                    )
                    if not writer.isOpened():
                        raise RuntimeError("アプリ画面の動画を作成できません。")
                if image.shape[1] != out_width or image.shape[0] != out_height:
                    image = cv2.resize(image, (out_width, out_height))
                writer.write(image)
                if now - last_preview >= 0.5:
                    self.events.put(("preview", cv2.cvtColor(image, cv2.COLOR_BGR2RGB)))
                    last_preview = now
                next_frame_at = max(next_frame_at + frame_interval, now + 0.001)
        finally:
            capture_control.stop()
            capture_control.wait()
            if writer:
                writer.release()
        if writer is None:
            raise RuntimeError("選択したアプリから画面を取得できませんでした。ウィンドウが開いているか確認してください。")
        self.events.put(("screen_done", str(self.video_path)))

    def _capture_audio(self):
        try:
            speaker = sc.default_speaker()
            loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            mic = sc.get_microphone(id=self.mic_id) if self.mic_id else None
            frames = int(SAMPLE_RATE * AUDIO_BLOCK)
            with ExitStack() as stack:
                loop_recorder = stack.enter_context(loopback.recorder(samplerate=SAMPLE_RATE))
                mic_recorder = stack.enter_context(mic.recorder(samplerate=SAMPLE_RATE)) if mic else None
                with wave.open(str(self.wav_path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(SAMPLE_RATE)
                    while not self.stop_event.is_set():
                        pc_audio = loop_recorder.record(numframes=frames)
                        if mic_recorder:
                            mic_audio = mic_recorder.record(numframes=frames)
                            count = min(len(pc_audio), len(mic_audio))
                            audio = (pc_audio[:count].mean(axis=1) + mic_audio[:count].mean(axis=1)) / 2
                        else:
                            audio = pc_audio.mean(axis=1)
                        audio = np.clip(audio, -1, 1).astype(np.float32)
                        pcm = (audio * 32767).astype("<i2")
                        wav.writeframes(pcm.tobytes())
                        self.audio_queue.put(audio)
            self.audio_queue.put(None)
            self.events.put(("audio_done", str(self.wav_path)))
        except Exception as exc:
            self.events.put(("capture_error", f"音声録音エラー: {exc}"))
            self.stop_event.set()
            self.audio_queue.put(None)

    def _transcribe_audio(self):
        try:
            if self.backend == "gpu":
                self.events.put(("status", "GPUサーバーで文字起こし中"))
            else:
                self.events.put(("status", f"このPCのCPU（{self.cpu_model}モデル）で文字起こし中"))
            batch = []
            sample_count = 0
            total_samples = 0
            batch_target = SAMPLE_RATE * TRANSCRIBE_SECONDS
            while True:
                chunk = self.audio_queue.get()
                if chunk is None:
                    break
                batch.append(chunk)
                sample_count += len(chunk)
                total_samples += len(chunk)
                if sample_count >= batch_target:
                    self._transcribe_batch(np.concatenate(batch), total_samples - sample_count)
                    batch, sample_count = [], 0
            if sample_count:
                self._transcribe_batch(np.concatenate(batch), total_samples - sample_count)
            self.events.put(("transcription_done", None))
        except Exception as exc:
            self.events.put(("transcribe_error", str(exc)))
            self.stop_event.set()
            self.events.put(("transcription_done", None))

    def _transcribe_batch(self, audio, offset_samples):
        if self.backend == "gpu":
            try:
                response = requests.post(
                    self.server_url + "/transcribe/chunk",
                    params={"offset": offset_samples / SAMPLE_RATE, "model": self.gpu_model},
                    headers={"X-API-Key": self.api_key, "Content-Type": "application/octet-stream"},
                    data=audio.astype("<f4", copy=False).tobytes(),
                    timeout=(4, 180),
                )
                response.raise_for_status()
                segments = response.json().get("segments", [])
            except Exception as exc:
                self.backend = "local"
                self.events.put(("status", f"GPUサーバーに接続できないため、このPCで処理します: {exc}"))
                segments = self._local_segments(audio, offset_samples / SAMPLE_RATE)
        else:
            segments = self._local_segments(audio, offset_samples / SAMPLE_RATE)
        for segment in segments:
            text = segment["text"].strip()
            if text:
                self.events.put(("transcript", (segment["start"], text)))

    def _local_segments(self, audio, offset_seconds):
        if self.local_model is None:
            self.events.put(("status", "このPC用の文字起こしモデルを準備しています（初回はモデルを取得します）…"))
            from faster_whisper import WhisperModel
            self.local_model = WhisperModel(self.cpu_model, device="cpu", compute_type="int8")
        segments, _ = self.local_model.transcribe(audio, language="ja", vad_filter=True, beam_size=5)
        return [
            {"start": offset_seconds + segment.start, "text": segment.text.strip()}
            for segment in segments if segment.text.strip()
        ]

    def mux(self):
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            command = [ffmpeg, "-y", "-i", str(self.video_path), "-i", str(self.wav_path), "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-ac", "1", "-shortest", str(self.mp4_path)]
            result = subprocess.run(command, capture_output=True)
            if result.returncode:
                stderr = result.stderr.decode("utf-8", errors="replace")
                raise RuntimeError(stderr[-1500:])
            self.video_path.unlink(missing_ok=True)
            self.events.put(("mux_done", str(self.mp4_path)))
        except Exception as exc:
            self.events.put(("mux_error", f"音声付き動画の作成に失敗しました: {exc}\n録画動画: {self.video_path}\n音声: {self.wav_path}"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("会議録画・リアルタイム文字起こし")
        self.geometry("1100x760")
        self.minsize(820, 580)
        self.events = queue.Queue()
        self.session = None
        self.preview_photo = None
        self.transcript_lines = []
        self.transcript_entries = []
        self.audio_finished = False
        self.screen_finished = False
        self.transcription_finished = False
        self.mux_started = False
        self.monitor_ids = {}
        self.capture_targets = {}
        try:
            with mss.MSS() as sct:
                for index, monitor in enumerate(sct.monitors[1:], start=1):
                    label = f"画面{index}（{monitor['width']}×{monitor['height']}、位置 {monitor['left']},{monitor['top']}）"
                    self.monitor_ids[label] = index
                    self.capture_targets[label] = ("monitor", index)
        except Exception as exc:
            self.capture_targets["画面を検出できません"] = ("monitor", 1)
        self._refresh_window_targets()
        self._build()
        self.after(100, self._poll_events)

    def _refresh_window_targets(self):
        for label in [key for key, value in self.capture_targets.items() if value[0] == "window"]:
            del self.capture_targets[label]
        for title, hwnd in list_windows():
            label = f"アプリ: {title}（{hwnd}）"
            self.capture_targets[label] = ("window", hwnd)
        if hasattr(self, "target_box"):
            current = self.target_var.get()
            values = list(self.capture_targets)
            self.target_box["values"] = values
            if current in self.capture_targets:
                self.target_var.set(current)
            elif values:
                self.target_var.set(values[0])

    def _build(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="会議録画・リアルタイム文字起こし", font=("Yu Gothic UI", 18, "bold")).pack(anchor="w")
        ttk.Label(root, text="録画中の画面を表示しながら、PC音声とマイク音声を文字起こしします。", wraplength=1000).pack(anchor="w", pady=(4, 12))
        capture_controls = ttk.Frame(root)
        capture_controls.pack(fill="x", pady=(0, 6))
        ttk.Label(capture_controls, text="録画対象").pack(side="left")
        self.target_var = tk.StringVar(value=next(iter(self.capture_targets), "画面を検出できません"))
        self.target_box = ttk.Combobox(capture_controls, textvariable=self.target_var, values=list(self.capture_targets), state="readonly", width=75)
        self.target_box.pack(side="left", padx=6)
        ttk.Button(capture_controls, text="アプリ一覧更新", command=self._refresh_window_targets).pack(side="left", padx=(0, 8))
        controls = ttk.Frame(root)
        controls.pack(fill="x", pady=(0, 10))
        ttk.Label(controls, text="マイク").pack(side="left")
        self.mic_var = tk.StringVar(value="マイクを録音しない")
        self.mic_box = ttk.Combobox(controls, textvariable=self.mic_var, state="readonly", width=35)
        self.mic_box.pack(side="left", padx=6)
        self.mic_ids = {"マイクを録音しない": None}
        try:
            for mic in sc.all_microphones(include_loopback=False):
                label = mic.name
                if label in self.mic_ids:
                    label += f" ({mic.id})"
                self.mic_ids[label] = mic.id
        except Exception:
            pass
        self.mic_box["values"] = list(self.mic_ids)
        model_controls = ttk.Frame(root)
        model_controls.pack(fill="x", pady=(0, 8))
        ttk.Label(model_controls, text="GPU使用時").pack(side="left")
        self.gpu_model_var = tk.StringVar(value="medium")
        self.gpu_model_box = ttk.Combobox(model_controls, textvariable=self.gpu_model_var, values=("medium", "small"), state="readonly", width=10)
        self.gpu_model_box.pack(side="left", padx=6)
        ttk.Label(model_controls, text="CPU使用時").pack(side="left", padx=(10, 0))
        self.cpu_model_var = tk.StringVar(value="small")
        self.cpu_model_box = ttk.Combobox(model_controls, textvariable=self.cpu_model_var, values=("small", "base", "tiny"), state="readonly", width=10)
        self.cpu_model_box.pack(side="left", padx=6)
        ttk.Label(model_controls, text="GPU PCが使えない場合はCPUモデルに自動切替").pack(side="left", padx=(8, 0))
        self.start_btn = ttk.Button(controls, text="録画開始", command=self._start)
        self.start_btn.pack(side="left", padx=(12, 4))
        self.stop_btn = ttk.Button(controls, text="録画停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left")
        server_controls = ttk.Frame(root)
        server_controls.pack(fill="x", pady=(0, 8))
        ttk.Label(server_controls, text="GPU PCのURL").pack(side="left")
        self.server_url_var = tk.StringVar(value="")
        ttk.Entry(server_controls, textvariable=self.server_url_var, width=34).pack(side="left", padx=6)
        ttk.Label(server_controls, text="APIキー").pack(side="left", padx=(8, 0))
        self.api_key_var = tk.StringVar(value="")
        ttk.Entry(server_controls, textvariable=self.api_key_var, show="•", width=28).pack(side="left", padx=6)
        self.test_server_btn = ttk.Button(server_controls, text="接続確認", command=self._test_server)
        self.test_server_btn.pack(side="left", padx=4)
        self.status = tk.StringVar(value="準備完了")
        ttk.Label(root, textvariable=self.status).pack(anchor="w", pady=(0, 8))

        panels = ttk.Panedwindow(root, orient="horizontal")
        panels.pack(fill="both", expand=True)
        screen_panel = ttk.Frame(panels, padding=(0, 0, 8, 0))
        text_panel = ttk.Frame(panels, padding=(8, 0, 0, 0))
        panels.add(screen_panel, weight=3)
        panels.add(text_panel, weight=2)
        ttk.Label(screen_panel, text="録画プレビュー（選択した画面またはアプリ）", font=("Yu Gothic UI", 11, "bold")).pack(anchor="w", pady=(0, 5))
        self.preview = ttk.Label(screen_panel, text="録画を開始すると画面が表示されます", anchor="center", relief="solid")
        self.preview.pack(fill="both", expand=True)
        ttk.Label(text_panel, text="文字起こし", font=("Yu Gothic UI", 11, "bold")).pack(anchor="w", pady=(0, 5))
        self.text = tk.Text(text_panel, wrap="word", font=("Yu Gothic UI", 10), height=20)
        self.text.pack(fill="both", expand=True)
        bottom = ttk.Frame(root)
        bottom.pack(fill="x", pady=(9, 0))
        ttk.Button(bottom, text="文字起こしを保存", command=self._save_text).pack(side="left")
        self.import_btn = ttk.Button(bottom, text="録音ファイルを読み込んで文字起こし", command=self._load_media)
        self.import_btn.pack(side="left", padx=8)
        ttk.Button(bottom, text="録画フォルダーを開く", command=self._open_folder).pack(side="left", padx=8)
        ttk.Label(bottom, text="録画には数秒の遅れがあります。初回はモデルのダウンロードが必要です。", wraplength=650).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _start(self):
        server_url = self.server_url_var.get().strip().rstrip("/")
        api_key = self.api_key_var.get().strip()
        if self.target_var.get() not in self.capture_targets:
            messagebox.showwarning("録画対象", "録画対象を選択してください。アプリが一覧にない場合は一覧を更新してください。")
            return
        backend = "local"
        if server_url and api_key:
            try:
                health = requests.get(server_url + "/health", headers={"X-API-Key": api_key}, timeout=4)
                health.raise_for_status()
                health_data = health.json()
                if health_data.get("ready") and health_data.get("gpu_available") and health_data.get("device") == "cuda":
                    backend = "gpu"
                else:
                    self.status.set(f"GPUを利用できないため、このPCのCPU（{self.cpu_model_var.get()}モデル）で処理します")
            except Exception as exc:
                self.status.set(f"GPU PCに接続できないため、このPCで処理します: {exc}")
        else:
            self.status.set(f"GPU PCの接続情報がないため、このPCのCPU（{self.cpu_model_var.get()}モデル）で処理します")
        RECORDINGS_DIR.mkdir(exist_ok=True)
        self.transcript_lines = []
        self.transcript_entries = []
        self.audio_finished = False
        self.screen_finished = False
        self.transcription_finished = False
        self.mux_started = False
        self.text.delete("1.0", "end")
        capture_kind, capture_value = self.capture_targets[self.target_var.get()]
        gpu_model = self.gpu_model_var.get()
        cpu_model = self.cpu_model_var.get()
        self.session = CaptureSession(self.events, self.mic_ids.get(self.mic_var.get()), capture_kind, capture_value, server_url, api_key, backend, gpu_model, cpu_model)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.mic_box.configure(state="disabled")
        self.target_box.configure(state="disabled")
        self.session.start()
        if backend == "gpu":
            self.status.set(f"録画を開始しています（GPU PC: {gpu_model}、CPU fallback: {cpu_model}）…")
        else:
            self.status.set(f"録画を開始しています（ローカルCPU・{cpu_model}モデル）…")

    def _stop(self):
        if self.session:
            self.session.stop()
            self.stop_btn.configure(state="disabled")
            self.status.set("録画を停止しています…")

    def _poll_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "preview":
                    h, w = value.shape[:2]
                    area_w = max(self.preview.winfo_width(), 200)
                    area_h = max(self.preview.winfo_height(), 120)
                    scale = min(area_w / w, area_h / h)
                    image = Image.fromarray(value).resize((int(w * scale), int(h * scale)))
                    self.preview_photo = ImageTk.PhotoImage(image)
                    self.preview.configure(image=self.preview_photo, text="")
                elif kind == "status":
                    self.status.set(value)
                elif kind == "server_ok":
                    self.test_server_btn.configure(state="normal")
                    model = value.get("model", "不明")
                    device = value.get("device", "不明")
                    self.status.set(f"GPU利用可能: {model} / {device}")
                    messagebox.showinfo(
                        "GPU利用可能",
                        f"GPU PCに接続しました。\n\nGPU: 利用可能（{device}）\n文字起こしモデル: {model}",
                    )
                elif kind == "server_unavailable":
                    self.test_server_btn.configure(state="normal")
                    self.status.set("GPUを利用できません。文字起こし時はこのPCのCPUを使用します")
                    messagebox.showwarning(
                        "GPUを利用できません",
                        "GPU PCには接続しましたが、GPU文字起こしの準備ができていません。\n"
                        "GPUサーバーの状態を確認してください。\n\n"
                        "文字起こしを開始すると、このPCのCPUへ切り替わります。",
                    )
                elif kind == "server_error":
                    self.test_server_btn.configure(state="normal")
                    self.status.set("GPU PCに接続できません。文字起こし時はこのPCのCPUを使用します")
                    messagebox.showerror(
                        "GPU PCに接続できません",
                        f"GPU PCに接続できませんでした。\n文字起こし時はこのPCのCPUへ切り替わります。\n\n詳細: {value}",
                    )
                elif kind == "transcript":
                    seconds, line = value
                    self.transcript_entries.append((seconds, line))
                    formatted = f"[{self._format_time(seconds)}] {line}"
                    self.transcript_lines.append(formatted)
                    self.text.insert("end", formatted + "\n")
                    self.text.see("end")
                elif kind == "file_transcript":
                    seconds, line = value
                    self.transcript_entries.append((seconds, line))
                    formatted = f"[{self._format_time(seconds)}] {line}"
                    self.transcript_lines.append(formatted)
                    self.text.insert("end", formatted + "\n")
                    self.text.see("end")
                elif kind == "file_done":
                    source, subtitle_path = value
                    self.status.set(f"文字起こしを保存しました: {Path(source).stem}_文字起こし.txt")
                    if self.transcript_entries:
                        self._open_review(source, self.transcript_entries.copy())
                elif kind == "file_error":
                    source, error = value
                    self.status.set(f"文字起こしに失敗: {Path(source).name}")
                elif kind == "file_started":
                    source, index, total = value
                    self.transcript_entries = []
                    self.transcript_lines = []
                    self.text.delete("1.0", "end")
                    self.status.set(f"文字起こし中（{index}/{total}）: {Path(source).name}")
                elif kind == "file_batch_done":
                    count, failures = value
                    self.import_btn.configure(state="normal")
                    self.status.set(f"処理完了: 成功 {count} / 失敗 {len(failures)} ファイル")
                    if failures:
                        details = "\n\n".join(f"{Path(source).name}: {error}" for source, error in failures)
                        messagebox.showerror("一部のファイルを処理できませんでした", details)
                elif kind == "audio_done":
                    self.audio_finished = True
                    self.status.set("音声を保存しました。録画と文字起こしを仕上げています…")
                    self._maybe_mux()
                elif kind == "screen_done":
                    self.screen_finished = True
                    self._maybe_mux()
                elif kind == "transcription_done":
                    self.transcription_finished = True
                    self._maybe_mux()
                elif kind == "mux_done":
                    self.status.set(f"完了: {value}")
                    self.start_btn.configure(state="normal")
                    self.mic_box.configure(state="readonly")
                    self.target_box.configure(state="readonly")
                elif kind == "capture_error":
                    self.status.set(value)
                    messagebox.showerror("録画エラー", value)
                elif kind == "transcribe_error":
                    self.status.set("文字起こしエラー（録画ファイルは保存されます）")
                    self.text.insert("end", "\n文字起こしエラー: " + value + "\n")
                elif kind == "mux_error":
                    self.status.set("動画の結合に失敗しました")
                    messagebox.showerror("動画作成エラー", value)
                    self.start_btn.configure(state="normal")
                    self.mic_box.configure(state="readonly")
                    self.target_box.configure(state="readonly")
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _maybe_mux(self):
        if (self.audio_finished and self.screen_finished and self.transcription_finished
                and not self.mux_started and self.session):
            self.mux_started = True
            self.status.set("文字起こし完了。音声付き動画を作成しています…")
            threading.Thread(target=self.session.mux, daemon=True).start()

    def _save_text(self):
        if not self.transcript_lines:
            messagebox.showinfo("保存", "保存できる文字起こしがありません。")
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("テキスト", "*.txt")], initialfile="meeting_transcript.txt")
        if path:
            Path(path).write_text("\n".join(self.transcript_lines) + "\n", encoding="utf-8")
            self.status.set(f"文字起こしを保存しました: {path}")

    @staticmethod
    def _format_time(seconds):
        whole = int(seconds)
        return f"{whole // 3600:02d}:{(whole % 3600) // 60:02d}:{whole % 60:02d}"

    @staticmethod
    def _srt_time(seconds):
        millis = int(seconds * 1000)
        hours, millis = divmod(millis, 3_600_000)
        minutes, millis = divmod(millis, 60_000)
        secs, millis = divmod(millis, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def _load_media(self):
        server_url = self.server_url_var.get().strip().rstrip("/")
        api_key = self.api_key_var.get().strip()
        paths = filedialog.askopenfilenames(
            title="文字起こしする音声・動画ファイルを選択（複数選択可）",
            filetypes=[("音声・動画", "*.wav *.mp3 *.m4a *.flac *.ogg *.mp4 *.mkv *.mov *.avi"), ("すべて", "*.*")],
        )
        if not paths:
            return
        self.transcript_entries = []
        self.transcript_lines = []
        self.text.delete("1.0", "end")
        self.import_btn.configure(state="disabled")
        self.status.set(f"{len(paths)}ファイルを順番に文字起こしします…")
        gpu_model = self.gpu_model_var.get()
        cpu_model = self.cpu_model_var.get()
        threading.Thread(target=self._transcribe_files, args=(paths, server_url, api_key, gpu_model, cpu_model), daemon=True).start()

    def _transcribe_files(self, paths, server_url, api_key, gpu_model, cpu_model):
        failures = []
        for index, file_path in enumerate(paths, 1):
            path = Path(file_path)
            self.events.put(("file_started", (str(path), index, len(paths))))
            try:
                self._transcribe_file(path, server_url, api_key, gpu_model, cpu_model)
            except Exception as exc:
                failures.append((str(path), str(exc)))
                self.events.put(("file_error", (str(path), str(exc))))
        self.events.put(("file_batch_done", (len(paths) - len(failures), failures)))

    def _transcribe_file(self, path, server_url, api_key, gpu_model, cpu_model):
        temp_dir = None
        try:
            import tempfile
            audio_path = path
            if path.suffix.lower() in {".mp4", ".mkv", ".mov", ".avi"}:
                import imageio_ffmpeg
                temp_dir = tempfile.TemporaryDirectory(prefix="meeting_audio_")
                audio_path = Path(temp_dir.name) / "audio.wav"
                command = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(audio_path)]
                result = subprocess.run(command, capture_output=True)
                if result.returncode:
                    stderr = result.stderr.decode("utf-8", errors="replace")
                    raise RuntimeError("動画から音声を取り出せませんでした。\n" + stderr[-1200:])
            segments = None
            if server_url and api_key:
                try:
                    self.events.put(("status", "音声をGPU PCへ送信して文字起こし中…"))
                    with audio_path.open("rb") as audio_file:
                        response = requests.post(
                            server_url + "/transcribe/file",
                            headers={"X-API-Key": api_key},
                            params={"model": gpu_model},
                            files={"file": (audio_path.name, audio_file, "application/octet-stream")},
                            timeout=(20, 14400),
                        )
                    response.raise_for_status()
                    segments = response.json().get("segments", [])
                except Exception as exc:
                    self.events.put(("status", f"GPU PCを使えないため、このPCで文字起こしします: {exc}"))
            if segments is None:
                self.events.put(("status", f"このPCのCPUで文字起こし中です（{cpu_model}モデル）…"))
                from faster_whisper import WhisperModel
                model = WhisperModel(cpu_model, device="cpu", compute_type="int8")
                local_segments, _ = model.transcribe(str(audio_path), language="ja", vad_filter=True, beam_size=5)
                segments = [
                    {"start": segment.start, "end": segment.end, "text": segment.text}
                    for segment in local_segments
                ]
            entries = [(float(item["start"]), float(item["end"]), item["text"].strip()) for item in segments if item.get("text", "").strip()]
            for start, _, line in entries:
                self.events.put(("file_transcript", (start, line)))
            txt_path = path.with_name(path.stem + "_文字起こし.txt")
            srt_path = path.with_name(path.stem + "_字幕.srt")
            txt_path.write_text("\n".join(f"[{self._format_time(start)}] {line}" for start, _, line in entries) + "\n", encoding="utf-8")
            srt = "\n\n".join(f"{i}\n{self._srt_time(start)} --> {self._srt_time(end)}\n{line}" for i, (start, end, line) in enumerate(entries, 1))
            srt_path.write_text(srt + ("\n" if srt else ""), encoding="utf-8")
            self.events.put(("file_done", (str(path), str(srt_path))))
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        finally:
            if temp_dir:
                temp_dir.cleanup()

    def _test_server(self):
        server_url = self.server_url_var.get().strip().rstrip("/")
        api_key = self.api_key_var.get().strip()
        if not server_url or not api_key:
            messagebox.showwarning("GPUサーバー", "GPU PCのURLとAPIキーを入力してください。")
            return
        self.test_server_btn.configure(state="disabled")

        def check():
            try:
                response = requests.get(server_url + "/health", headers={"X-API-Key": api_key}, timeout=8)
                response.raise_for_status()
                data = response.json()
                if data.get("ready") and data.get("gpu_available") and data.get("device") == "cuda":
                    self.events.put(("server_ok", data))
                else:
                    self.events.put(("server_unavailable", data))
            except Exception as exc:
                self.events.put(("server_error", str(exc)))

        threading.Thread(target=check, daemon=True).start()

    def _open_review(self, media_path, entries):
        if Path(media_path).suffix.lower() not in {".mp4", ".mkv", ".mov", ".avi"}:
            return
        cap = cv2.VideoCapture(media_path)
        if not cap.isOpened():
            cap.release()
            return
        window = tk.Toplevel(self)
        window.title("録画の確認")
        window.geometry("1000x680")
        panes = ttk.Panedwindow(window, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=10, pady=10)
        video_panel = ttk.Frame(panes)
        transcript_panel = ttk.Frame(panes)
        panes.add(video_panel, weight=3)
        panes.add(transcript_panel, weight=2)
        video_label = ttk.Label(video_panel, text="動画プレビュー", anchor="center", relief="solid")
        video_label.pack(fill="both", expand=True)
        controls = ttk.Frame(video_panel)
        controls.pack(fill="x", pady=6)
        state = {"playing": False, "photo": None, "closed": False}

        def show_frame():
            ok, frame = cap.read()
            if not ok:
                state["playing"] = False
                return
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w = rgb.shape[:2]
            scale = min(max(video_label.winfo_width(), 240) / w, max(video_label.winfo_height(), 160) / h)
            image = Image.fromarray(rgb).resize((int(w * scale), int(h * scale)))
            state["photo"] = ImageTk.PhotoImage(image)
            video_label.configure(image=state["photo"], text="")
            if state["playing"] and not state["closed"]:
                window.after(35, show_frame)

        def toggle_play():
            state["playing"] = not state["playing"]
            if state["playing"]:
                show_frame()

        ttk.Button(controls, text="再生 / 一時停止", command=toggle_play).pack(side="left")
        ttk.Label(transcript_panel, text="発言時刻を選ぶと、その位置へ移動します").pack(anchor="w", pady=(0, 5))
        listing = tk.Listbox(transcript_panel, font=("Yu Gothic UI", 10))
        listing.pack(fill="both", expand=True)
        for start, line in entries:
            listing.insert("end", f"[{self._format_time(start)}] {line}")

        def seek(_event=None):
            selected = listing.curselection()
            if selected:
                cap.set(cv2.CAP_PROP_POS_MSEC, entries[selected[0]][0] * 1000)
                state["playing"] = False
                show_frame()

        listing.bind("<<ListboxSelect>>", seek)

        def close_review():
            state["closed"] = True
            cap.release()
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", close_review)

    def _open_folder(self):
        RECORDINGS_DIR.mkdir(exist_ok=True)
        import os
        os.startfile(RECORDINGS_DIR)

    def _close(self):
        if self.session and self.session.threads and any(thread.is_alive() for thread in self.session.threads):
            if not messagebox.askyesno("終了", "録画中です。録画を停止して終了しますか？"):
                return
            self.session.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()

