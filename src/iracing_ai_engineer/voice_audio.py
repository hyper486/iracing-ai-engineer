"""Explicit, bounded local audio I/O; importing this module opens no devices.

Call record only for a user-initiated push-to-talk action, from a worker thread.
Audio remains in memory. A named device never falls back to another device.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import sys
import threading
import wave
from array import array
from collections import Counter
from contextlib import suppress

from .runtime_clock import monotonic_now

MAX_RECORD_SECONDS = 12.0
MAX_PLAY_SECONDS = 120.0
MAX_WAV_BYTES = 8 * 1024**2
_RATE = 16_000
_BLOCK = 320
_PORTAUDIO_LOCK = threading.Lock()
_CODES = frozenset(
    {
        "AUDIO_UNAVAILABLE",
        "AUDIO_BUSY",
        "AUDIO_INPUT_INVALID",
        "AUDIO_DEVICE_MISSING",
        "AUDIO_DEVICE_AMBIGUOUS",
        "AUDIO_RECORD_FAILED",
        "AUDIO_PLAY_FAILED",
        "AUDIO_INPUT_OVERFLOW",
        "AUDIO_OUTPUT_UNDERRUN",
        "AUDIO_TIMEOUT",
    }
)


class AudioError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _CODES else "AUDIO_UNAVAILABLE"
        super().__init__(self.code)


def _name(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 256
        and not any(ord(character) < 32 for character in value)
    )


def _number(value: object, maximum: float, *, zero: bool = False) -> bool:
    return (
        type(value) in (int, float)
        and (0 <= value <= maximum if zero else 0 < value <= maximum)
        and math.isfinite(value)
    )


def _decode_wav(raw: bytes) -> tuple[bytes, int, int]:
    """Accept only bounded, complete uncompressed PCM16 WAV, not arbitrary audio."""
    invalid = False
    try:
        if type(raw) is not bytes or not 44 <= len(raw) <= MAX_WAV_BYTES:
            raise ValueError("invalid")
        if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            raise ValueError("invalid")
        if int.from_bytes(raw[4:8], "little") + 8 != len(raw):
            raise ValueError("invalid")
        with wave.open(io.BytesIO(raw), "rb") as source:
            rate, channels, count = (
                source.getframerate(),
                source.getnchannels(),
                source.getnframes(),
            )
            if not (
                8_000 <= rate <= 48_000
                and channels in (1, 2)
                and source.getsampwidth() == 2
                and source.getcomptype() == "NONE"
                and 0 < count <= rate * MAX_PLAY_SECONDS
            ):
                raise ValueError("invalid")
            pcm = source.readframes(count + 1)
            if len(pcm) != count * channels * 2:
                raise ValueError("invalid")
    except Exception:
        invalid = True
    if invalid:
        raise AudioError("AUDIO_INPUT_INVALID")
    return pcm, rate, channels


class AudioIO:
    def __init__(self, *, backend=None) -> None:
        # Injection is for synthetic tests; production loads sounddevice lazily.
        self._backend = backend
        self._lock = _PORTAUDIO_LOCK

    def _sounddevice(self):
        if self._backend is None:
            import sounddevice

            self._backend = sounddevice
        return self._backend

    def _catalog(self) -> dict[str, list[dict]]:
        backend = self._sounddevice()
        # PortAudio caches device/default indexes at initialization. Refresh only
        # while our process-wide I/O lock excludes every stream owned here. This
        # is the explicitly pinned sounddevice 0.5.5 API; an unexpected external
        # initialization reference must not be terminated behind its owner's back.
        count = getattr(backend, "_initialized", None)
        if type(count) is not int or count not in (0, 1):
            raise AudioError("AUDIO_UNAVAILABLE")
        if count == 1:
            backend._terminate()
        backend._initialize()
        hosts = backend.query_hostapis()
        devices = backend.query_devices()
        if len(hosts) > 256 or len(devices) > 1024:
            raise AudioError("AUDIO_UNAVAILABLE")
        result: dict[str, list[dict]] = {"inputs": [], "outputs": []}
        seen_indexes: set[int] = set()
        for position, device in enumerate(devices):
            host_index = device["hostapi"]
            index = device.get("index", position)
            if type(host_index) is not int or not 0 <= host_index < len(hosts):
                raise AudioError("AUDIO_UNAVAILABLE")
            host_name, name = hosts[host_index]["name"], device["name"]
            if type(index) is not int or index < 0:
                raise AudioError("AUDIO_UNAVAILABLE")
            if index in seen_indexes:
                raise AudioError("AUDIO_UNAVAILABLE")
            seen_indexes.add(index)
            if not _name(host_name) or not _name(name):
                # Some Windows drivers expose embedded NULs. Never render or
                # persist those identifiers, without disabling unrelated devices.
                continue
            identity = json.dumps([host_name, name], ensure_ascii=True, separators=(",", ":"))
            stable_id = "sd-" + hashlib.sha256(identity.encode("ascii")).hexdigest()
            for kind, field in (
                ("inputs", "max_input_channels"),
                ("outputs", "max_output_channels"),
            ):
                channels = device[field]
                if type(channels) is not int or channels < 0:
                    raise AudioError("AUDIO_UNAVAILABLE")
                if channels:
                    result[kind].append(
                        {
                            "id": stable_id,
                            "name": f"{name} — {host_name}",
                            "index": index,
                            "channels": channels,
                            "hostapi": host_name,
                        }
                    )
        return result

    def _resolve(self, selector: str, kind: str, channels: int) -> tuple[int, dict]:
        if type(selector) is not str or (
            selector != "default" and re.fullmatch(r"sd-[0-9a-f]{64}", selector) is None
        ):
            raise AudioError("AUDIO_INPUT_INVALID")
        catalog = self._catalog()[kind]
        if selector == "default":
            index = self._sounddevice().default.device[0 if kind == "inputs" else 1]
            if type(index) is not int or index < 0:
                raise AudioError("AUDIO_DEVICE_MISSING")
            matches = [item for item in catalog if item["index"] == index]
        else:
            matches = [item for item in catalog if item["id"] == selector]
        if not matches:
            raise AudioError("AUDIO_DEVICE_MISSING")
        if len(matches) != 1:
            raise AudioError("AUDIO_DEVICE_AMBIGUOUS")
        if matches[0]["channels"] < channels:
            raise AudioError("AUDIO_DEVICE_MISSING")
        selected = matches[0]
        options = {}
        if selected["hostapi"] == "Windows WASAPI":
            # Shared-mode conversion supports capture as well as playback:
            # PortAudio v19.7.0 pa_win_wasapi.c input flags at lines 3521-3525.
            # Keep our 16 kHz capture / WAV output contract without changing
            # the endpoint's mix format, global defaults or selected device.
            options["extra_settings"] = self._sounddevice().WasapiSettings(
                exclusive=False, auto_convert=True,
            )
        return selected["index"], options

    def devices(self) -> dict:
        if not self._lock.acquire(blocking=False):
            raise AudioError("AUDIO_BUSY")
        code = "AUDIO_UNAVAILABLE"
        try:
            result = {}
            for kind, items in self._catalog().items():
                counts = Counter(item["id"] for item in items)
                # Never offer a persisted name-derived ID that cannot uniquely
                # identify a device. Other unique devices remain usable. Explicit
                # system-default selection uses its refreshed native index instead.
                result[kind] = [
                    {"id": item["id"], "name": item["name"]}
                    for item in items
                    if counts[item["id"]] == 1
                ]
            return result
        except AudioError as error:
            code = error.code
        except Exception:
            pass
        finally:
            self._lock.release()
        raise AudioError(code)

    @staticmethod
    def _dispose(stream) -> bool:
        if stream is not None:
            # abort never drains queued output; cancellation must not keep talking.
            with suppress(Exception):
                stream.abort()
            try:
                stream.close()
            except Exception:
                return False
        return True

    def record(
        self,
        stop: threading.Event,
        device: str = "default",
        max_seconds: float = MAX_RECORD_SECONDS,
    ) -> bytes:
        if not isinstance(stop, threading.Event) or not _number(max_seconds, MAX_RECORD_SECONDS):
            raise AudioError("AUDIO_INPUT_INVALID")
        if stop.is_set():
            return b""
        if not self._lock.acquire(blocking=False):
            raise AudioError("AUDIO_BUSY")
        code, stream = None, None
        cap = max(1, int(max_seconds * _RATE)) * 2
        storage = bytearray(cap)
        cursor = 0
        done = threading.Event()
        failures: list[str] = []
        try:
            backend = self._sounddevice()
            index, options = self._resolve(device, "inputs", 1)

            def callback(indata, frames, _timing, status):
                nonlocal cursor
                abort, complete = False, False
                try:
                    if stop.is_set():
                        abort = True
                    elif status:
                        failures.append("AUDIO_INPUT_OVERFLOW")
                        abort = True
                    elif type(frames) is not int or not 0 < frames <= 8192:
                        failures.append("AUDIO_RECORD_FAILED")
                        abort = True
                    else:
                        view = memoryview(indata).cast("B")
                        if len(view) != frames * 2:
                            raise ValueError("invalid buffer")
                        count = min(len(view), cap - cursor)
                        storage[cursor : cursor + count] = view[:count]
                        cursor += count
                        complete = cursor == cap
                except Exception:
                    failures.append("AUDIO_RECORD_FAILED")
                    abort = True
                if abort:
                    done.set()
                    raise backend.CallbackAbort
                if complete:
                    raise backend.CallbackStop

            if not stop.is_set():
                stream = backend.RawInputStream(
                    samplerate=_RATE,
                    channels=1,
                    dtype="int16",
                    device=index,
                    blocksize=_BLOCK,
                    callback=callback,
                    finished_callback=done.set,
                    **options,
                )
                stream.start()
                deadline = monotonic_now() + max_seconds
                while not done.is_set() and not stop.is_set():
                    remaining = deadline - monotonic_now()
                    if remaining <= 0:
                        break
                    done.wait(min(0.02, remaining))
            if failures:
                code = failures[0]
        except AudioError as error:
            code = error.code
        except Exception:
            code = "AUDIO_RECORD_FAILED"
        finally:
            if not self._dispose(stream) and code is None:
                code = "AUDIO_RECORD_FAILED"
            self._lock.release()
        if failures and code is None:
            code = failures[0]
        if code is not None:
            raise AudioError(code)
        return bytes(storage[:cursor])

    def play(
        self, wav: bytes, stop: threading.Event, device: str = "default", volume: float = 0.7
    ) -> None:
        if not isinstance(stop, threading.Event) or not _number(volume, 1.0, zero=True):
            raise AudioError("AUDIO_INPUT_INVALID")
        pcm, rate, channels = _decode_wav(wav)
        if stop.is_set():
            return
        if not self._lock.acquire(blocking=False):
            raise AudioError("AUDIO_BUSY")
        code, stream = None, None
        done = threading.Event()
        failures: list[str] = []
        cursor = 0
        try:
            samples = array("h")
            samples.frombytes(pcm)
            if sys.byteorder != "little":
                samples.byteswap()
            if volume != 1.0:
                for index, sample in enumerate(samples):
                    samples[index] = round(sample * volume)
            if sys.byteorder != "little":
                samples.byteswap()
            pcm = samples.tobytes()
            backend = self._sounddevice()
            index, options = self._resolve(device, "outputs", channels)

            def callback(outdata, frames, _timing, status):
                nonlocal cursor
                abort, complete = False, False
                try:
                    view = memoryview(outdata).cast("B")
                    if (
                        type(frames) is not int
                        or not 0 < frames <= 8192
                        or (len(view) != frames * channels * 2)
                    ):
                        raise ValueError("invalid buffer")
                    view[:] = b"\x00" * len(view)
                    if stop.is_set():
                        abort = True
                    elif status:
                        failures.append("AUDIO_OUTPUT_UNDERRUN")
                        abort = True
                    else:
                        count = min(len(view), len(pcm) - cursor)
                        view[:count] = pcm[cursor : cursor + count]
                        cursor += count
                        complete = cursor == len(pcm)
                except Exception:
                    failures.append("AUDIO_PLAY_FAILED")
                    abort = True
                if abort:
                    done.set()
                    raise backend.CallbackAbort
                if complete:
                    # Wait for finished_callback: final samples are still queued.
                    raise backend.CallbackStop

            if not stop.is_set():
                stream = backend.RawOutputStream(
                    samplerate=rate,
                    channels=channels,
                    dtype="int16",
                    device=index,
                    blocksize=_BLOCK,
                    callback=callback,
                    finished_callback=done.set,
                    **options,
                )
                stream.start()
                deadline = monotonic_now() + len(pcm) / (rate * channels * 2) + 2.0
                while not done.is_set() and not stop.is_set():
                    remaining = deadline - monotonic_now()
                    if remaining <= 0:
                        failures.append("AUDIO_TIMEOUT")
                        break
                    done.wait(min(0.02, remaining))
            if failures:
                code = failures[0]
        except AudioError as error:
            code = error.code
        except Exception:
            code = "AUDIO_PLAY_FAILED"
        finally:
            if not self._dispose(stream) and code is None:
                code = "AUDIO_PLAY_FAILED"
            self._lock.release()
        if failures and code is None:
            code = failures[0]
        if code is not None:
            raise AudioError(code)


__all__ = ["AudioIO", "AudioError", "MAX_RECORD_SECONDS", "MAX_PLAY_SECONDS"]
