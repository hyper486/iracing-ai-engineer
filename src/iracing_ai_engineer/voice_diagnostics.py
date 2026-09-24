"""Explicit synthetic voice check: memory only, no microphone, speaker or cloud."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import wave
from pathlib import Path


def wav_to_pcm(wav: bytes) -> bytes:
    """Small synthetic test conversion, not a general untrusted media importer."""
    import numpy as np

    with wave.open(io.BytesIO(wav), "rb") as handle:
        if handle.getsampwidth() != 2 or not 1 <= handle.getnchannels() <= 2:
            raise ValueError("VOICE_TEST_FORMAT")
        count, rate, channels = handle.getnframes(), handle.getframerate(), handle.getnchannels()
        if not 0 < count <= rate * 12:
            raise ValueError("VOICE_TEST_DURATION")
        samples = np.frombuffer(handle.readframes(count), dtype="<i2").astype(np.float32)
    samples = samples.reshape(-1, channels).mean(axis=1)
    converted = np.interp(np.arange(round(count * 16000 / rate)) * rate / 16000,
                          np.arange(count), samples)
    return np.clip(np.rint(converted), -32768, 32767).astype("<i2").tobytes()


def _verify_model() -> None:
    if getattr(sys, "frozen", False):
        directory = Path(sys._MEIPASS) / "voice-model"
        manifest_path = directory / "voice-model.json"
    else:
        root = Path(__file__).resolve().parents[2]
        directory = root / "models" / "faster-whisper-medium"
        manifest_path = root / "packaging" / "voice-model.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest["files"]) != {"config.json", "model.bin", "tokenizer.json", "vocabulary.txt"}:
        raise ValueError("VOICE_TEST_MODEL_MANIFEST")
    for name, expected in manifest["files"].items():
        with (directory / name).open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != expected:
            raise ValueError("VOICE_TEST_MODEL_HASH")


def run_voice_self_test() -> list[dict]:
    """Check packaged dependencies and real Chinese TTS -> STT without audio I/O."""
    checks = []
    speech = None
    try:
        import pygame
        import sounddevice

        from .voice_speech import LocalSpeech

        # Import only: no SDL input listener, PortAudio stream, SDK or credentials.
        if not hasattr(pygame, "joystick") or not hasattr(sounddevice, "RawInputStream"):
            raise ValueError("VOICE_TEST_IMPORT")
        checks.append({"id": "VOICE_IO_IMPORT_ONLY", "status": "PASS"})
        _verify_model()
        checks.append({"id": "PINNED_LOCAL_STT_MODEL_HASHES", "status": "PASS"})
        speech = LocalSpeech()
        wav = speech.synthesize("现在还剩多少油？")
        result = speech.recognize(wav_to_pcm(wav), "zh-CN")
        if result["confidence"] < 0.5 or "油" not in result["text"]:
            raise ValueError("VOICE_TEST_RECOGNITION")
        checks.append({"id": "CHINESE_SYNTHETIC_TTS_TO_STT", "status": "PASS"})
        from .voice_tire_confirmation import tire_voice_intent
        for phrase, expected in (
            ("记录四胎已换新。", "FULL_NEW_SET"),
            ("记录本次没有换胎。", "NO_TIRE_CHANGE"),
            ("记录部分换胎。", "PARTIAL_OR_UNKNOWN"),
            ("确认记录。", "CONFIRM"), ("取消记录。", "CANCEL"),
        ):
            result = speech.recognize(wav_to_pcm(speech.synthesize(phrase)), "zh-CN")
            if result["confidence"] < .5 or tire_voice_intent(result["text"]) != expected:
                raise ValueError("VOICE_TEST_TIRE_RECOGNITION")
        checks.append({"id": "CHINESE_TIRE_REVIEW_TTS_TO_STT", "status": "PASS"})
    except Exception:
        checks.append({"id": "SYNTHETIC_VOICE_RUNTIME", "status": "FAIL"})
    finally:
        if speech is not None:
            speech.close()
    return checks
