"""Local-only Whisper STT with a killable, single-flight CPU worker.

``confidence`` preserves the existing speech interface but is NOT a calibrated
probability: 0.75 means the deterministic admission checks passed; 0 means
rejection. A maximum 12-second PCM utterance gets a 25-second total deadline.
Cancellation returns no transcript and terminates the inference process. The
warm worker exits after 60 idle seconds; close() reaps it explicitly.

Model: https://huggingface.co/Systran/faster-whisper-medium
Runtime: https://github.com/SYSTRAN/faster-whisper
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import logging
import math
import multiprocessing
import os
import re
import sys
import threading
from contextlib import suppress
from pathlib import Path

from .runtime_clock import monotonic_now
from .voice_windows import MAX_PCM_BYTES, MAX_TEXT_CHARS, WindowsSpeech

_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
_TIMEOUT_S = 25.0
_IDLE_S = 60.0
_MAX_RESPONSE = 4096
_CULTURES = {"zh-CN": "zh", "en-US": "en"}
_CHINESE_VOCABULARY = "油量，油耗，进站，轮胎，刹车，油门，圈速，弯角，损失，几圈。"
_ERROR_CODES = frozenset({
    "LOCAL_STT_MODEL_MISSING", "LOCAL_STT_UNAVAILABLE", "LOCAL_STT_INPUT_INVALID",
    "LOCAL_STT_BUSY", "LOCAL_STT_CLOSED", "LOCAL_STT_CANCELLED", "LOCAL_STT_TIMEOUT",
    "LOCAL_STT_FAILED", "LOCAL_STT_RESPONSE_INVALID", "LOCAL_STT_STOP_FAILED",
})


class LocalSpeechError(ValueError):
    """Only a fixed code, never native errors, paths, audio or transcript text."""

    def __init__(self, code: str):
        self.code = code if type(code) is str and code in _ERROR_CODES else "LOCAL_STT_FAILED"
        super().__init__(self.code)


def _model_directory() -> Path:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if type(base) is not str:
            raise LocalSpeechError("LOCAL_STT_MODEL_MISSING")
        return Path(base) / "voice-model"
    return Path(__file__).resolve().parents[2] / "models" / "faster-whisper-medium"


def _check_model(path: Path) -> None:
    try:
        if not path.is_dir() or any(
            not (path / name).is_file() or (path / name).stat().st_size <= 0 for name in _FILES
        ):
            raise OSError
    except (OSError, ValueError):
        raise LocalSpeechError("LOCAL_STT_MODEL_MISSING") from None


def _load_model(path: Path):
    # Never accept a hub model ID; tokenizer.json is mandatory to avoid fallback.
    _check_model(path)
    from faster_whisper import WhisperModel

    return WhisperModel(
        str(path), device="cpu", compute_type="int8", cpu_threads=4, num_workers=1,
        local_files_only=True,
    )


def _pcm_array(pcm: bytes):
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    # Remove DC for the silence test only; even a nonzero constant is not speech.
    centered = samples - samples.mean()
    audible = len(samples) >= 1600 and float(np.sqrt(np.mean(centered * centered))) >= 0.0001
    return samples, audible


def _score_segments(segments) -> dict:
    texts, last_end = [], 0.0
    for index, segment in enumerate(segments):
        if index >= 32:
            return {"text": "", "confidence": 0.0}
        text = getattr(segment, "text", None)
        values = [getattr(segment, key, None) for key in (
            "no_speech_prob", "avg_logprob", "compression_ratio", "start", "end",
        )]
        if (
            type(text) is not str or len(text) > MAX_TEXT_CHARS
            or any(type(value) not in (int, float) or not -1e6 <= value <= 1e6
                   or not math.isfinite(value) for value in values)
        ):
            return {"text": "", "confidence": 0.0}
        no_speech, logprob, compression, start, end = values
        if (
            not 0 <= no_speech < 0.65 or not -1.0 <= logprob <= 0
            or not 0 <= compression <= 2.4 or not 0 <= start <= end <= 12.5
            or start < last_end - 0.1
            or any(ord(char) < 32 and char not in "\n\t" for char in text)
            or any(0xD800 <= ord(char) <= 0xDFFF for char in text)
        ):
            return {"text": "", "confidence": 0.0}
        last_end = end
        text = " ".join(text.split())
        if text:
            texts.append(text)
        if sum(map(len, texts)) + len(texts) - 1 > MAX_TEXT_CHARS:
            return {"text": "", "confidence": 0.0}
    text = " ".join(texts).strip()
    compact = "".join(char for char in text.casefold() if char.isalnum())
    if (
        not compact or re.search(r"(.{1,40})\1{3,}", compact)
        or re.search(r"(.{2,40})\1\1", compact)
    ):
        return {"text": "", "confidence": 0.0}
    return {"text": text, "confidence": 0.75}


def _transcribe(model, pcm: bytes, language: str) -> dict:
    samples, audible = _pcm_array(pcm)
    if not audible:
        return {"text": "", "confidence": 0.0}
    segments, _info = model.transcribe(
        samples, language=language, task="transcribe", beam_size=3, best_of=1,
        temperature=0.0, condition_on_previous_text=False, word_timestamps=False,
        vad_filter=False, no_speech_threshold=0.6, log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4, max_new_tokens=128,
        initial_prompt=_CHINESE_VOCABULARY if language == "zh" else None,
    )
    # Inference lives in this iteration, not merely in model.transcribe().
    return _score_segments(segments)


def _lower_worker_priority() -> None:
    """Best-effort CPU scheduling for this worker only, never the game or app."""
    if sys.platform != "win32":
        return
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        current = kernel.GetCurrentProcess
        current.argtypes, current.restype = [], ctypes.c_void_p
        set_priority = kernel.SetPriorityClass
        set_priority.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        set_priority.restype = ctypes.c_int
        set_priority(current(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
    except Exception:
        pass  # No real-time/VR performance guarantee if scheduling is denied.


def _model_worker(connection, directory: str) -> None:
    _lower_worker_priority()
    # The process receives PCM only via its private pipe, never command-line text.
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "USERPROFILE",
               "LOCALAPPDATA", "APPDATA"}
    for key in tuple(os.environ):
        if key.upper() not in allowed:
            os.environ.pop(key, None)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                      TOKENIZERS_PARALLELISM="false", OMP_NUM_THREADS="4")
    logging.disable(logging.CRITICAL)
    model = None
    original_stdout, original_stderr = sys.stdout, sys.stderr
    try:
        with open(os.devnull, "w", encoding="utf-8") as quiet:
            sys.stdout = sys.stderr = quiet
            while connection.poll(_IDLE_S):
                payload = connection.recv_bytes(MAX_PCM_BYTES + 1)
                if not payload or payload[:1] not in (b"z", b"e"):
                    break
                pcm = payload[1:]
                if not 2 <= len(pcm) <= MAX_PCM_BYTES or len(pcm) % 2:
                    break
                try:
                    if model is None:
                        model = _load_model(Path(directory))
                    result = _transcribe(model, pcm, "zh" if payload[:1] == b"z" else "en")
                except Exception:
                    result = {"error": "LOCAL_STT_FAILED"}
                connection.send_bytes(json.dumps(result, ensure_ascii=True).encode("ascii"))
                if "error" in result:
                    break
    except Exception:
        pass  # Native errors and transcripts are never emitted to stderr.
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        connection.close()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _response(raw: bytes) -> dict:
    try:
        result = json.loads(raw.decode("ascii"), object_pairs_hook=_unique)
        if result == {"error": "LOCAL_STT_FAILED"}:
            raise LocalSpeechError("LOCAL_STT_FAILED")
        if type(result) is not dict or set(result) != {"text", "confidence"}:
            raise ValueError
        text, confidence = result["text"], result["confidence"]
        if (
            type(text) is not str or len(text) > MAX_TEXT_CHARS
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in text)
            or type(confidence) not in (int, float) or confidence not in (0.0, 0.75)
            or bool(text.strip()) != (confidence == 0.75)
        ):
            raise ValueError
        return {"text": text, "confidence": float(confidence)}
    except LocalSpeechError:
        raise
    except Exception:
        raise LocalSpeechError("LOCAL_STT_RESPONSE_INVALID") from None


class LocalSpeech:
    """Whisper recognition plus the existing bounded Windows voice synthesizer."""

    def __init__(self, model_dir: Path | None = None):
        self._directory = Path(model_dir) if model_dir is not None else _model_directory()
        self._call_lock, self._state_lock, self._cleanup_lock = (
            threading.Lock(), threading.Lock(), threading.Lock(),
        )
        self._process = self._connection = self._windows_speech = None
        self._active_cancel = self._io_thread = None
        self._closed = False

    def _windows(self):
        with self._state_lock:
            if self._closed:
                raise LocalSpeechError("LOCAL_STT_CLOSED")
            if self._windows_speech is None:
                self._windows_speech = WindowsSpeech()
            return self._windows_speech

    def probe(self) -> dict:
        with self._state_lock:
            if self._closed:
                raise LocalSpeechError("LOCAL_STT_CLOSED")
        _check_model(self._directory)
        if importlib.util.find_spec("faster_whisper") is None:
            raise LocalSpeechError("LOCAL_STT_UNAVAILABLE")
        voices = self._windows().probe()["voices"]
        return {
            "recognizers": [{"culture": culture, "name": "Local Whisper medium (CPU)"}
                            for culture in _CULTURES],
            "voices": voices,
        }

    def synthesize(self, text: str, voice: str = "") -> bytes:
        return self._windows().synthesize(text, voice=voice)

    def _ensure_process(self, cancel):
        with self._state_lock:
            process, connection = self._process, self._connection
        if process is not None and process.is_alive():
            return process, connection
        self._drop_process()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=_model_worker, args=(child, str(self._directory)), daemon=True,
        )
        try:
            with self._state_lock:
                if self._closed or cancel.is_set():
                    raise LocalSpeechError("LOCAL_STT_CANCELLED")
            # Starting a process never holds the lock needed by immediate cancel().
            process.start()
            with self._state_lock:
                self._process, self._connection = process, parent
                cancelled = self._closed or cancel.is_set()
            if cancelled:
                self._drop_process()
                raise LocalSpeechError("LOCAL_STT_CANCELLED")
        except Exception:
            with suppress(Exception):
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1.0)
            with suppress(Exception):
                parent.close()
            raise
        finally:
            child.close()
        return process, parent

    def _drop_process(self) -> None:
        with self._cleanup_lock:
            with self._state_lock:
                process, connection = self._process, self._connection
            if process is None:
                return
            try:
                if process.is_alive():
                    with suppress(Exception):
                        process.terminate()
                process.join(timeout=1.0)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1.0)
                stopped = not process.is_alive()
            except Exception:
                stopped = False
            if not stopped:
                raise LocalSpeechError("LOCAL_STT_STOP_FAILED")
            with suppress(Exception):
                connection.close()
            with suppress(Exception):
                process.close()
            with self._state_lock:
                if self._process is process:
                    self._process = self._connection = None

    def cancel(self) -> None:
        """Signal immediately; the recognition owner terminates/reaps its child."""
        with self._state_lock:
            if self._active_cancel is not None:
                self._active_cancel.set()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            if self._active_cancel is not None:
                self._active_cancel.set()
        self._drop_process()
        # close() is called by the service's shutdown worker, not its UI callback.
        io_thread = self._io_thread
        if io_thread is not None and io_thread is not threading.current_thread():
            io_thread.join(timeout=2.5)
            if io_thread.is_alive():
                raise LocalSpeechError("LOCAL_STT_STOP_FAILED")
        self._drop_process()

    def recognize(self, pcm: bytes, culture: str = "zh-CN") -> dict:
        if (
            type(pcm) is not bytes or not 2 <= len(pcm) <= MAX_PCM_BYTES or len(pcm) % 2
            or type(culture) is not str or culture not in _CULTURES
        ):
            raise LocalSpeechError("LOCAL_STT_INPUT_INVALID")
        with self._state_lock:
            if self._closed:
                raise LocalSpeechError("LOCAL_STT_CLOSED")
        _check_model(self._directory)
        if not self._call_lock.acquire(blocking=False):
            raise LocalSpeechError("LOCAL_STT_BUSY")
        result, done, failure = [], threading.Event(), None
        deadline = monotonic_now() + _TIMEOUT_S
        cancel = threading.Event()
        try:
            if self._io_thread is not None and self._io_thread.is_alive():
                raise LocalSpeechError("LOCAL_STT_BUSY")
            with self._state_lock:
                if self._closed:
                    raise LocalSpeechError("LOCAL_STT_CLOSED")
                self._active_cancel = cancel
            _samples, audible = _pcm_array(pcm)
            if not audible:
                return {"text": "", "confidence": 0.0}
            def exchange():
                try:
                    _process, connection = self._ensure_process(cancel)
                    if cancel.is_set():
                        return
                    connection.send_bytes((b"z" if culture == "zh-CN" else b"e") + pcm)
                    result.append(connection.recv_bytes(_MAX_RESPONSE))
                except Exception:
                    pass
                finally:
                    done.set()

            self._io_thread = threading.Thread(target=exchange, name="local-stt-pipe", daemon=True)
            self._io_thread.start()
            while not done.wait(0.05):
                if cancel.is_set():
                    raise LocalSpeechError("LOCAL_STT_CANCELLED")
                if monotonic_now() >= deadline:
                    raise LocalSpeechError("LOCAL_STT_TIMEOUT")
            if cancel.is_set():
                raise LocalSpeechError("LOCAL_STT_CANCELLED")
            if monotonic_now() >= deadline:
                raise LocalSpeechError("LOCAL_STT_TIMEOUT")
            if len(result) != 1:
                raise LocalSpeechError("LOCAL_STT_FAILED")
            return _response(result[0])
        except LocalSpeechError as exc:
            failure = exc.code
        except Exception:
            failure = "LOCAL_STT_FAILED"
        finally:
            if failure is not None:
                # A delayed process.start() must not begin inference after its
                # owner has already returned a deadline/cancellation failure.
                cancel.set()
                try:
                    self._drop_process()
                except Exception:
                    failure = "LOCAL_STT_STOP_FAILED"
            if self._io_thread is not None:
                self._io_thread.join(timeout=0.5)
            with self._state_lock:
                self._active_cancel = None
            self._call_lock.release()
        raise LocalSpeechError(failure)


__all__ = ["LocalSpeech", "LocalSpeechError"]
