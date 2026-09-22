"""Bounded offline System.Speech helpers. Never open a microphone or speaker.

Only fixed code enters PowerShell's command line. Audio/text use JSON on stdin;
helper output is bounded, errors are fixed codes, and no credential environment
is inherited. Calls are serialized per instance and helpers are always reaped.
"""

from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import re
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

MAX_PCM_BYTES = 16_000 * 2 * 12
MAX_TEXT_CHARS = 500
MAX_WAV_BYTES = 8 * 1024**2
_MAX_RESPONSE = 12 * 1024**2
_TIMEOUT_S = 20.0
_ERROR_CODES = frozenset(
    {
        "SPEECH_UNAVAILABLE",
        "SPEECH_BUSY",
        "SPEECH_INPUT_INVALID",
        "SPEECH_TIMEOUT",
        "SPEECH_HELPER_FAILED",
        "SPEECH_RESPONSE_INVALID",
        "SPEECH_RESPONSE_TOO_LARGE",
    }
)

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$engine = $null; $synth = $null; $stream = $null
try {
    Add-Type -AssemblyName System.Speech
    $raw = [Console]::In.ReadToEnd()
    if ($raw.Length -gt 600000) { throw 'INPUT_LIMIT' }
    $request = $raw | ConvertFrom-Json
    switch ($request.op) {
        'probe' {
            $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
            $installed = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
            $recognizers = @($installed |
                ForEach-Object { @{culture=$_.Culture.Name; name=$_.Name} })
            $voices = @($synth.GetInstalledVoices() | Where-Object {$_.Enabled} |
                ForEach-Object { @{culture=$_.VoiceInfo.Culture.Name; name=$_.VoiceInfo.Name} })
            $answer = @{recognizers=$recognizers; voices=$voices}
        }
        'recognize' {
            $pcm = [Convert]::FromBase64String([string]$request.pcm)
            if ($pcm.Length -lt 2 -or $pcm.Length -gt 384000 -or $pcm.Length % 2) {
                throw 'PCM_LIMIT'
            }
            $culture = [Globalization.CultureInfo]::GetCultureInfo([string]$request.culture)
            $engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($culture)
            $engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
            $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
                16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
                [System.Speech.AudioFormat.AudioChannel]::Mono)
            $stream = New-Object IO.MemoryStream(,$pcm)
            $engine.SetInputToAudioStream($stream, $format)
            $engine.InitialSilenceTimeout = [TimeSpan]::FromSeconds(2)
            $engine.EndSilenceTimeout = [TimeSpan]::FromMilliseconds(700)
            $texts = New-Object 'System.Collections.Generic.List[string]'
            $confidence = 1.0
            for ($part = 0; $part -lt 8; $part++) {
                try { $result = $engine.Recognize([TimeSpan]::FromSeconds(12)) }
                catch [InvalidOperationException] {
                    # End of an explicit stream disconnects recognizer input. Do not
                    # inspect AudioPosition after EOF: that property also throws.
                    if ($texts.Count -gt 0 -and $stream.Position -eq $stream.Length) { break }
                    throw
                }
                if ($null -eq $result) { break }
                if ($result.Text) {
                    $texts.Add($result.Text)
                    $confidence = [Math]::Min($confidence, [double]$result.Confidence)
                }
                if ($part -eq 7) { throw 'PHRASE_LIMIT' }
            }
            $text = $texts -join ' '
            if ($text.Length -gt 500) { throw 'TEXT_LIMIT' }
            if ($texts.Count -eq 0) { $confidence = 0.0 }
            $answer = @{text=$text; confidence=$confidence}
        }
        'synthesize' {
            $text = [string]$request.text
            if ($text.Length -lt 1 -or $text.Length -gt 500) { throw 'TEXT_LIMIT' }
            $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
            if ($request.voice) { $synth.SelectVoice([string]$request.voice) }
            elseif ($text -match '[\u4e00-\u9fff]') {
                $chinese = @($synth.GetInstalledVoices() | Where-Object {
                    $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'zh-CN'
                })
                if ($chinese.Count -eq 0) { throw 'VOICE_UNAVAILABLE' }
                $synth.SelectVoice($chinese[0].VoiceInfo.Name)
            }
            $stream = New-Object IO.MemoryStream
            $synth.SetOutputToWaveStream($stream)
            $synth.Speak($text)
            $synth.SetOutputToNull()
            if ($stream.Length -gt 8388608) { throw 'AUDIO_LIMIT' }
            $answer = @{wav=[Convert]::ToBase64String($stream.ToArray())}
        }
        default { throw 'OP_INVALID' }
    }
    [Console]::Out.Write(($answer | ConvertTo-Json -Compress -Depth 5))
}
catch {
    [Console]::Out.Write('{"error":"SPEECH_HELPER_FAILED"}')
    exit 1
}
finally {
    if ($null -ne $engine) { $engine.Dispose() }
    if ($null -ne $synth) { $synth.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
}
"""
_ENCODED_SCRIPT = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")


class SpeechError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _ERROR_CODES else "SPEECH_HELPER_FAILED"
        super().__init__(self.code)


def _powershell_path() -> str:
    if os.name != "nt":
        raise SpeechError("SPEECH_UNAVAILABLE")
    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True, winmode=0x00000800)
    function = kernel.GetWindowsDirectoryW
    function.argtypes = [ctypes.POINTER(ctypes.c_wchar), ctypes.c_uint]
    function.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    count = function(buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise SpeechError("SPEECH_UNAVAILABLE")
    path = Path(buffer.value) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not path.is_absolute() or not path.is_file():
        raise SpeechError("SPEECH_UNAVAILABLE")
    return str(path)


def _environment() -> dict[str, str]:
    allowed = {
        "systemroot",
        "windir",
        "temp",
        "tmp",
        "userprofile",
        "localappdata",
        "appdata",
        "programdata",
        "programfiles",
        "programfiles(x86)",
        "commonprogramfiles",
    }
    return {key: value for key, value in os.environ.items() if key.lower() in allowed}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON")
        result[key] = value
    return result


def _text(value: object, maximum: int, *, empty: bool = False) -> bool:
    return (
        type(value) is str
        and (empty or bool(value.strip()))
        and len(value) <= maximum
        and not any(ord(character) < 32 for character in value)
    )


class WindowsSpeech:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _exchange(self, request: dict) -> dict:
        command = [
            _powershell_path(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            _ENCODED_SCRIPT,
        ]
        payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            env=_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = bytearray()
        failures: list[str] = []

        def read():
            try:
                while True:
                    chunk = process.stdout.read(min(65536, _MAX_RESPONSE + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > _MAX_RESPONSE:
                        failures.append("SPEECH_RESPONSE_TOO_LARGE")
                        process.kill()
                        break
            except Exception:
                failures.append("SPEECH_HELPER_FAILED")

        def write():
            try:
                if process.stdin.write(payload) != len(payload):
                    failures.append("SPEECH_HELPER_FAILED")
                process.stdin.close()
            except Exception:
                failures.append("SPEECH_HELPER_FAILED")

        threads = [
            threading.Thread(target=read, name="speech-output", daemon=True),
            threading.Thread(target=write, name="speech-input", daemon=True),
        ]
        try:
            for thread in threads:
                thread.start()
            try:
                process.wait(timeout=_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                raise SpeechError("SPEECH_TIMEOUT") from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=3)
            for pipe in (process.stdin, process.stdout):
                with suppress(Exception):
                    pipe.close()
        if any(thread.is_alive() for thread in threads):
            raise SpeechError("SPEECH_HELPER_FAILED")
        if failures:
            raise SpeechError(failures[0])
        if process.returncode != 0:
            raise SpeechError("SPEECH_HELPER_FAILED")
        result = json.loads(output.decode("utf-8-sig"), object_pairs_hook=_unique)
        if type(result) is not dict:
            raise SpeechError("SPEECH_RESPONSE_INVALID")
        return result

    def _call(self, request: dict) -> dict:
        if not self._lock.acquire(blocking=False):
            raise SpeechError("SPEECH_BUSY")
        code = "SPEECH_HELPER_FAILED"
        try:
            try:
                return self._exchange(request)
            except SpeechError as error:
                code = error.code
            except Exception:
                pass
        finally:
            self._lock.release()
        raise SpeechError(code)

    def probe(self) -> dict:
        result = self._call({"op": "probe"})
        if set(result) != {"recognizers", "voices"}:
            raise SpeechError("SPEECH_RESPONSE_INVALID")
        for kind in ("recognizers", "voices"):
            values = result[kind]
            if type(values) is not list or len(values) > 64:
                raise SpeechError("SPEECH_RESPONSE_INVALID")
            for item in values:
                if (
                    type(item) is not dict
                    or set(item) != {"culture", "name"}
                    or not (_text(item["culture"], 32) and _text(item["name"], 256))
                ):
                    raise SpeechError("SPEECH_RESPONSE_INVALID")
        return result

    def recognize(self, pcm: bytes, culture: str = "zh-CN") -> dict:
        if (
            type(pcm) is not bytes
            or not 2 <= len(pcm) <= MAX_PCM_BYTES
            or len(pcm) % 2
            or (type(culture) is not str or re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}", culture) is None)
        ):
            raise SpeechError("SPEECH_INPUT_INVALID")
        result = self._call(
            {"op": "recognize", "pcm": base64.b64encode(pcm).decode("ascii"), "culture": culture}
        )
        confidence = result.get("confidence")
        if set(result) != {"text", "confidence"} or not (
            _text(result.get("text"), MAX_TEXT_CHARS, empty=True)
            and type(confidence) in (int, float)
            and 0 <= confidence <= 1
            and math.isfinite(confidence)
        ):
            raise SpeechError("SPEECH_RESPONSE_INVALID")
        return {"text": result["text"], "confidence": float(confidence)}

    def synthesize(self, text: str, voice: str = "") -> bytes:
        if not _text(text, MAX_TEXT_CHARS) or not _text(voice, 256, empty=True):
            raise SpeechError("SPEECH_INPUT_INVALID")
        result = self._call({"op": "synthesize", "text": text, "voice": voice})
        code = None
        try:
            if set(result) != {"wav"} or type(result["wav"]) is not str:
                raise ValueError("invalid response")
            audio = base64.b64decode(result["wav"], validate=True)
            from .voice_audio import _decode_wav

            _decode_wav(audio)
        except Exception:
            code = "SPEECH_RESPONSE_INVALID"
        if code is not None:
            raise SpeechError(code)
        return audio


__all__ = ["WindowsSpeech", "SpeechError", "MAX_PCM_BYTES", "MAX_TEXT_CHARS", "MAX_WAV_BYTES"]
