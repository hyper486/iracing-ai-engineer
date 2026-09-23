"""No microphone/speaker tests; opt-in native check also uses only memory."""

import base64
import io
import json
import os
import subprocess
import threading
import warnings
import wave

import pytest

from iracing_ai_engineer import voice_windows as module
from iracing_ai_engineer.voice_windows import SpeechError, WindowsSpeech


def _wav() -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x00" * 320)
    return target.getvalue()


class _Input(io.BytesIO):
    def close(self):
        self.payload = self.getvalue()
        super().close()


class _Process:
    def __init__(self, output: bytes, *, timeout=False, returncode=0):
        self.stdin = _Input()
        self.stdout = io.BytesIO(output)
        self.returncode = None
        self.desired_returncode = returncode
        self.timeout = timeout
        self.killed = False
        self.wait_count = 0

    def wait(self, timeout=None):
        self.wait_count += 1
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("private diagnostic must not escape", timeout)
        if self.returncode is None:
            self.returncode = self.desired_returncode
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _process(monkeypatch, output: bytes, **kwargs):
    process = _Process(output, **kwargs)
    calls = []

    def popen(command, **options):
        calls.append((command, options))
        return process

    monkeypatch.setattr(module, "_powershell_path", lambda: "C:/Windows/System32/powershell.exe")
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    return process, calls


def test_fixed_command_hidden_no_profile_stdin_only_and_environment_allowlist(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-sentinel")
    monkeypatch.setenv("OTHER_PROVIDER_TOKEN", "unit-sentinel")
    monkeypatch.setenv("PSModulePath", "untrusted-module-path")
    monkeypatch.setenv("TEMP", "C:/Users/racer/AppData/Local/Temp")
    raw = _wav()
    process, calls = _process(
        monkeypatch, json.dumps({"wav": base64.b64encode(raw).decode()}).encode()
    )
    spoken = "燃油？'; Write-Output malicious; #"
    assert WindowsSpeech().synthesize(spoken) == raw
    command, options = calls[0]
    assert command[0].startswith("C:/Windows/System32/")
    assert command[1:5] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    assert base64.b64decode(command[5]).decode("utf-16-le") == module._SCRIPT
    assert spoken not in " ".join(command)
    assert options["shell"] is False
    assert options["stderr"] == subprocess.DEVNULL
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert options["env"].get("TEMP") == "C:/Users/racer/AppData/Local/Temp"
    assert not any(
        "TOKEN" in key or "KEY" in key or key == "PSModulePath" for key in options["env"]
    )
    assert json.loads(process.stdin.payload) == {"op": "synthesize", "text": spoken, "voice": ""}
    assert process.stdin.closed and process.stdout.closed and process.wait_count >= 2


def test_script_never_opens_audio_hardware_or_uses_user_code():
    assert "SetInputToDefaultAudioDevice" not in module._SCRIPT
    assert "SetOutputToDefaultAudioDevice" not in module._SCRIPT
    assert "Invoke-Expression" not in module._SCRIPT
    assert "SetInputToAudioStream" in module._SCRIPT
    assert "SetOutputToWaveStream" in module._SCRIPT
    assert "DictationGrammar" in module._SCRIPT
    assert "SelectVoice([string]$request.voice)" in module._SCRIPT
    assert "VoiceInfo.Culture.Name -eq 'zh-CN'" in module._SCRIPT
    assert "$engine.AudioPosition" not in module._SCRIPT
    assert "$stream.Position -eq $stream.Length" in module._SCRIPT


@pytest.mark.parametrize("rate", [True, -11, 11, 3.0, "3"])
def test_synthesis_rate_requires_a_bounded_integer(rate):
    with pytest.raises(SpeechError, match="SPEECH_INPUT_INVALID"):
        WindowsSpeech().synthesize("左侧有车", rate=rate)


def test_nondefault_rate_is_data_on_stdin_not_executable_code(monkeypatch):
    process, _ = _process(monkeypatch, json.dumps({
        "wav": base64.b64encode(_wav()).decode(),
    }).encode())
    WindowsSpeech().synthesize("左侧有车", rate=3)
    assert json.loads(process.stdin.payload)["rate"] == 3


def test_timeout_kills_reaps_closes_and_sanitizes(monkeypatch):
    process, _ = _process(monkeypatch, b"", timeout=True)
    with pytest.raises(SpeechError, match="^SPEECH_TIMEOUT$") as failure:
        WindowsSpeech().probe()
    assert process.killed and process.stdin.closed and process.stdout.closed
    assert failure.value.__context__ is None


def test_excess_output_is_bounded_and_child_killed(monkeypatch):
    monkeypatch.setattr(module, "_MAX_RESPONSE", 32)
    process, _ = _process(monkeypatch, b"x" * 100)
    with pytest.raises(SpeechError, match="^SPEECH_RESPONSE_TOO_LARGE$"):
        WindowsSpeech().probe()
    assert process.killed and process.stdout.closed


@pytest.mark.parametrize("output", [b"not JSON", b"[]", b'{"voices":[],"voices":[]}'])
def test_invalid_helper_output_is_safe(monkeypatch, output):
    _process(monkeypatch, output)
    with pytest.raises(SpeechError) as failure:
        WindowsSpeech().probe()
    assert failure.value.code in module._ERROR_CODES
    assert failure.value.__context__ is None


def test_nonzero_helper_exit_does_not_publish_its_output(monkeypatch):
    _process(monkeypatch, b'{"error":"private helper details"}', returncode=1)
    with pytest.raises(SpeechError, match="^SPEECH_HELPER_FAILED$"):
        WindowsSpeech().probe()


def test_process_creation_error_sanitized_and_lock_reusable(monkeypatch):
    speech = WindowsSpeech()

    def fail(_request):
        raise OSError("private path and diagnostic")

    monkeypatch.setattr(speech, "_exchange", fail)
    with pytest.raises(SpeechError, match="^SPEECH_HELPER_FAILED$") as failure:
        speech.probe()
    assert failure.value.__context__ is None
    monkeypatch.setattr(speech, "_exchange", lambda _: {"recognizers": [], "voices": []})
    assert speech.probe() == {"recognizers": [], "voices": []}


def test_calls_are_serialized_without_spawning_second_helper(monkeypatch):
    speech = WindowsSpeech()
    entered, release = threading.Event(), threading.Event()

    def exchange(_):
        entered.set()
        assert release.wait(2)
        return {"recognizers": [], "voices": []}

    monkeypatch.setattr(speech, "_exchange", exchange)
    worker = threading.Thread(target=speech.probe)
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(SpeechError, match="^SPEECH_BUSY$"):
            speech.probe()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()


@pytest.mark.parametrize(
    "pcm,culture",
    [
        (b"", "zh-CN"),
        (b"x", "zh-CN"),
        (bytearray(b"xx"), "zh-CN"),
        (b"xx", "zh-CN;evil"),
        (b"xx", None),
        (b"xx", True),
        (b"x" * (module.MAX_PCM_BYTES + 2), "zh-CN"),
    ],
    ids=["empty", "odd", "mutable", "culture-code", "culture-none", "culture-bool", "limit"],
)
def test_invalid_pcm_or_culture_never_spawns_helper(monkeypatch, pcm, culture):
    monkeypatch.setattr(WindowsSpeech, "_exchange", lambda *_: pytest.fail("helper called"))
    with pytest.raises(SpeechError, match="^SPEECH_INPUT_INVALID$"):
        WindowsSpeech().recognize(pcm, culture)


@pytest.mark.parametrize(
    "text,voice",
    [
        ("", ""),
        (" ", ""),
        ("x" * 501, ""),
        ("hello\n", ""),
        (True, ""),
        ("hello", "x" * 257),
        ("hello", None),
    ],
    ids=["empty", "blank", "limit", "control", "bool", "voice-limit", "voice-none"],
)
def test_invalid_synthesis_never_spawns_helper(monkeypatch, text, voice):
    monkeypatch.setattr(WindowsSpeech, "_exchange", lambda *_: pytest.fail("helper called"))
    with pytest.raises(SpeechError, match="^SPEECH_INPUT_INVALID$"):
        WindowsSpeech().synthesize(text, voice)


@pytest.mark.parametrize("confidence", [True, -0.1, 1.1, float("nan"), float("inf"), "0.7"])
def test_invalid_confidence_rejected(monkeypatch, confidence):
    monkeypatch.setattr(
        WindowsSpeech, "_exchange", lambda *_: {"text": "燃油", "confidence": confidence}
    )
    with pytest.raises(SpeechError, match="^SPEECH_RESPONSE_INVALID$"):
        WindowsSpeech().recognize(b"\x00\x00")


def test_recognize_zero_confidence_empty_is_valid(monkeypatch):
    requests = []

    def exchange(_self, request):
        requests.append(request)
        return {"text": "", "confidence": 0}

    monkeypatch.setattr(WindowsSpeech, "_exchange", exchange)
    assert WindowsSpeech().recognize(b"\x00\x00") == {"text": "", "confidence": 0.0}
    assert requests == [{"op": "recognize", "culture": "zh-CN", "pcm": "AAA="}]


@pytest.mark.parametrize(
    "result",
    [
        {"recognizers": [], "voices": [], "extra": "private"},
        {"recognizers": [], "voices": ["voice"]},
        {"recognizers": [{"culture": "zh-CN", "name": "x\n"}], "voices": []},
        {"recognizers": [], "voices": [{"culture": "zh-CN", "name": "x", "extra": 1}]},
    ],
)
def test_probe_response_schema_is_bounded(monkeypatch, result):
    monkeypatch.setattr(WindowsSpeech, "_exchange", lambda *_: result)
    with pytest.raises(SpeechError, match="^SPEECH_RESPONSE_INVALID$"):
        WindowsSpeech().probe()


@pytest.mark.parametrize("raw", [b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 32, _wav()[:-2]])
def test_synthesis_requires_complete_pcm_wav(monkeypatch, raw):
    monkeypatch.setattr(
        WindowsSpeech, "_exchange", lambda *_: {"wav": base64.b64encode(raw).decode()}
    )
    with pytest.raises(SpeechError, match="^SPEECH_RESPONSE_INVALID$"):
        WindowsSpeech().synthesize("燃油")


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("IRACING_TEST_OFFLINE_SPEECH") != "1",
    reason="explicit opt-in local speech engine; no microphone or speaker",
)
def test_native_chinese_memory_roundtrip_no_audio_devices():
    # Python 3.12 is pinned by this project. The stdlib resampler is test-only.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import audioop

    speech = WindowsSpeech()
    capabilities = speech.probe()
    if not any(item["culture"] == "zh-CN" for item in capabilities["recognizers"]):
        pytest.skip("Chinese offline recognizer not installed")
    wav = speech.synthesize("现在还剩多少燃油")
    with wave.open(io.BytesIO(wav), "rb") as source:
        assert source.getsampwidth() == 2 and source.getnchannels() == 1
        pcm = audioop.ratecv(
            source.readframes(source.getnframes()), 2, 1, source.getframerate(), 16_000, None
        )[0]
    result = speech.recognize(pcm)
    # Proves native grammar/EOF handling, not real-microphone recognition accuracy.
    assert result["text"] and 0 <= result["confidence"] <= 1
