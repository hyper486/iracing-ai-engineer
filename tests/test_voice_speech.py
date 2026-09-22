"""Fake models/pipes only: no microphone, speaker, model weights or network."""

import json
import sys
import threading
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest

from iracing_ai_engineer import voice_speech as module
from iracing_ai_engineer.voice_speech import LocalSpeech, LocalSpeechError


@pytest.fixture
def directory(tmp_path):
    for name in module._FILES:
        (tmp_path / name).write_bytes(b"synthetic-model-placeholder")
    return tmp_path


@pytest.fixture
def pcm():
    return array("h", [1000, -1000] * 8000).tobytes()


def segment(text="还剩多少油？", **overrides):
    values = dict(text=text, no_speech_prob=0.01, avg_logprob=-0.2,
                  compression_ratio=1.0, start=0.0, end=1.0)
    values.update(overrides)
    return SimpleNamespace(**values)


class Connection:
    def __init__(self, response, blocked=False):
        self.response, self.blocked = response, blocked
        self.sent, self.limits = [], []
        self.closed = False
        self.stop = threading.Event()
        self.requested = threading.Event()

    def send_bytes(self, value):
        self.sent.append(value)
        self.requested.set()

    def recv_bytes(self, limit):
        self.limits.append(limit)
        if self.blocked:
            assert self.stop.wait(3.0), "fake pipe was not reaped"
            raise EOFError("private native diagnostic")
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self):
        self.closed = True
        self.stop.set()


class Process:
    def __init__(self, connection, start_callback=None, resist_terminate=False):
        self.connection, self.start_callback = connection, start_callback
        self.alive = self.closed = False
        self.terminated = self.killed = 0
        self.joined = []
        self.resist_terminate = resist_terminate

    def start(self):
        self.alive = True
        if self.start_callback:
            self.start_callback()

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated += 1
        if not self.resist_terminate:
            self.alive = False
            self.connection.stop.set()

    def kill(self):
        self.killed += 1
        self.alive = False
        self.connection.stop.set()

    def join(self, timeout):
        self.joined.append(timeout)

    def close(self):
        self.closed = True


class Context:
    def __init__(self, response, **options):
        self.response, self.options = response, options
        self.processes, self.children, self.calls = [], [], []

    def Pipe(self, *, duplex):
        assert duplex is True
        self.connection = Connection(self.response, self.options.get("blocked", False))
        child = Connection(b"")
        self.children.append(child)
        return self.connection, child

    def Process(self, **kwargs):
        self.calls.append(kwargs)
        process = Process(self.connection, self.options.get("start_callback"),
                          self.options.get("resist_terminate", False))
        self.processes.append(process)
        return process


def context(monkeypatch, response=None, **options):
    if response is None:
        response = json.dumps({"text": "还剩多少油？", "confidence": 0.75}).encode("ascii")
    fake = Context(response, **options)

    def get_context(method):
        assert method == "spawn"
        return fake

    monkeypatch.setattr(module.multiprocessing, "get_context", get_context)
    return fake


def start_recognition(speech, pcm):
    result = []

    def run():
        try:
            result.append(speech.recognize(pcm))
        except LocalSpeechError as exc:
            result.append(exc.code)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_paths_source_and_frozen(monkeypatch):
    monkeypatch.delattr(module.sys, "frozen", raising=False)
    assert module._model_directory() == (
        Path(module.__file__).resolve().parents[2] / "models" / "faster-whisper-medium"
    )
    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(module.sys, "_MEIPASS", "frozen-placeholder", raising=False)
    assert module._model_directory() == Path("frozen-placeholder/voice-model")
    monkeypatch.delattr(module.sys, "_MEIPASS")
    with pytest.raises(LocalSpeechError, match="^LOCAL_STT_MODEL_MISSING$"):
        module._model_directory()


@pytest.mark.parametrize("missing", module._FILES)
def test_every_local_file_required_including_tokenizer(directory, missing):
    (directory / missing).unlink()
    with pytest.raises(LocalSpeechError, match="^LOCAL_STT_MODEL_MISSING$"):
        LocalSpeech(directory).probe()


def test_model_constructor_local_cpu_only(monkeypatch, directory):
    calls = []
    model = object()
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda *args, **kwargs: (calls.append((args, kwargs)), model)[1],
    ))
    assert module._load_model(directory) is model
    assert calls == [((str(directory),), dict(device="cpu", compute_type="int8", cpu_threads=4,
                                            num_workers=1, local_files_only=True))]


def test_probe_and_synthesis_lazy_no_inference(monkeypatch, directory):
    calls = []

    class Windows:
        def __init__(self):
            calls.append("construct")

        def probe(self):
            return {"recognizers": [], "voices": [{"culture": "zh-CN", "name": "Chinese"}]}

        def synthesize(self, text, voice):
            calls.append((text, voice))
            return b"fake-wave"

    monkeypatch.setattr(module, "WindowsSpeech", Windows)
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(module.multiprocessing, "get_context", lambda _: pytest.fail("spawn"))
    speech = LocalSpeech(directory)
    assert calls == []
    result = speech.probe()
    assert [row["culture"] for row in result["recognizers"]] == ["zh-CN", "en-US"]
    assert result["voices"] == [{"culture": "zh-CN", "name": "Chinese"}]
    assert speech.synthesize("测试", "Chinese") == b"fake-wave"
    assert calls == ["construct", ("测试", "Chinese")]
    speech.close()
    with pytest.raises(LocalSpeechError, match="LOCAL_STT_CLOSED"):
        speech.probe()


def test_missing_runtime_does_not_spawn(monkeypatch, directory):
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda _: None)
    with pytest.raises(LocalSpeechError, match="LOCAL_STT_UNAVAILABLE"):
        LocalSpeech(directory).probe()


@pytest.mark.parametrize("pcm", [b"", b"1", bytearray(b"00"), b"00" * 192001, None],
                         ids=["empty", "odd", "mutable", "oversized", "none"])
def test_invalid_audio(directory, pcm):
    with pytest.raises(LocalSpeechError, match="LOCAL_STT_INPUT_INVALID"):
        LocalSpeech(directory).recognize(pcm)


@pytest.mark.parametrize("culture", ["zh", "fr-FR", None, [], True])
def test_invalid_culture(directory, pcm, culture):
    with pytest.raises(LocalSpeechError, match="LOCAL_STT_INPUT_INVALID"):
        LocalSpeech(directory).recognize(pcm, culture)


@pytest.mark.parametrize("samples", [b"\0\0" * 16000, b"\1\0" * 16000, b"\0\0"],
                         ids=["silence", "constant-dc", "short"])
def test_silence_and_dc_rejected_without_worker(directory, monkeypatch, samples):
    monkeypatch.setattr(module.multiprocessing, "get_context", lambda _: pytest.fail("spawn"))
    assert LocalSpeech(directory).recognize(samples) == {"text": "", "confidence": 0.0}


def test_transcribe_fixed_options_lazy_segments(pcm):
    calls = []

    class Model:
        def transcribe(self, samples, **options):
            calls.append(options)
            assert samples.dtype.name == "float32" and len(samples) == 16000
            return iter([segment("油？")]), None

    assert module._transcribe(Model(), pcm, "zh") == {"text": "油？", "confidence": 0.75}
    assert calls == [dict(language="zh", task="transcribe", beam_size=3, best_of=1,
                          temperature=0.0, condition_on_previous_text=False,
                          word_timestamps=False, vad_filter=False, no_speech_threshold=0.6,
                          log_prob_threshold=-1.0, compression_ratio_threshold=2.4,
                          max_new_tokens=128, initial_prompt=module._CHINESE_VOCABULARY)]


@pytest.mark.parametrize("overrides", [
    {"no_speech_prob": 0.65}, {"no_speech_prob": -0.1}, {"avg_logprob": -1.01},
    {"avg_logprob": float("nan")}, {"avg_logprob": True}, {"avg_logprob": 10**1000},
    {"compression_ratio": 2.41}, {"start": -1}, {"end": 12.51}, {"end": "1"},
    {"text": "x" * 501}, {"text": ""}, {"text": "!!"}, {"text": "hello\x00"},
    {"text": "\ud800"}, {"text": "谢谢谢谢谢谢"}, {"text": "hello hello hello"},
    {"text": "啊啊啊啊"},
])
def test_reject_unsafe_or_low_evidence_segments(overrides):
    assert module._score_segments([segment(**overrides)]) == {"text": "", "confidence": 0.0}


def test_segment_bounds_and_good_short_chinese():
    assert module._score_segments([segment("几圈")])["confidence"] == 0.75
    assert module._score_segments([segment("油门"), segment("时间", start=0.1)]) == {
        "text": "", "confidence": 0.0,
    }
    assert module._score_segments([segment("油", start=0, end=0)] * 33)["confidence"] == 0
    assert module._score_segments([])["confidence"] == 0


@pytest.mark.parametrize("raw", [
    b"{}", b"null", b"[]", b'{"text":"x","confidence":true}',
    b'{"text":"x","confidence":0}', b'{"text":"","confidence":0.75}',
    b'{"text":"x","confidence":NaN}', b'{"text":"x","confidence":0.8}',
    b'{"text":"x","text":"y","confidence":0.75}', b"not-json",
    b'{"error":"private detail"}', b'{"text":"x","confidence":0.75,"extra":1}',
])
def test_invalid_child_response(raw):
    with pytest.raises(LocalSpeechError, match="^LOCAL_STT_RESPONSE_INVALID$"):
        module._response(raw)


def test_warm_worker_reused_and_close_reaps(monkeypatch, directory, pcm):
    fake = context(monkeypatch)
    speech = LocalSpeech(directory)
    assert speech.recognize(pcm)["confidence"] == 0.75
    assert speech.recognize(pcm, "en-US")["confidence"] == 0.75
    assert len(fake.processes) == 1
    assert fake.calls[0]["target"] is module._model_worker
    assert fake.calls[0]["daemon"] is True
    assert fake.calls[0]["args"][1] == str(directory)
    assert fake.connection.sent == [b"z" + pcm, b"e" + pcm]
    assert fake.connection.limits == [4096, 4096]
    assert fake.children[0].closed
    speech.close()
    assert fake.processes[0].terminated == 1 and fake.processes[0].closed
    assert fake.connection.closed
    speech.close()
    with pytest.raises(LocalSpeechError, match="LOCAL_STT_CLOSED"):
        speech.recognize(pcm)


def test_idle_worker_exit_replaced(monkeypatch, directory, pcm):
    fake = context(monkeypatch)
    speech = LocalSpeech(directory)
    speech.recognize(pcm)
    fake.processes[0].alive = False
    speech.recognize(pcm)
    assert len(fake.processes) == 2 and fake.processes[0].closed
    speech.close()


@pytest.mark.parametrize("response,code", [
    (b'{"error":"LOCAL_STT_FAILED"}', "LOCAL_STT_FAILED"),
    (b"not-json", "LOCAL_STT_RESPONSE_INVALID"),
    (EOFError("secret-native-error"), "LOCAL_STT_FAILED"),
])
def test_worker_error_reaped_sanitized(monkeypatch, directory, pcm, response, code):
    fake = context(monkeypatch, response)
    speech = LocalSpeech(directory)
    with pytest.raises(LocalSpeechError) as error:
        speech.recognize(pcm)
    assert str(error.value) == code
    assert "secret" not in repr(error.value)
    assert fake.processes[0].closed and fake.connection.closed


def test_timeout_hard_kill_and_subsequent_call_can_recover(monkeypatch, directory, pcm):
    fake = context(monkeypatch, blocked=True, resist_terminate=True)
    monkeypatch.setattr(module, "_TIMEOUT_S", 0.06)
    speech = LocalSpeech(directory)
    with pytest.raises(LocalSpeechError, match="^LOCAL_STT_TIMEOUT$"):
        speech.recognize(pcm)
    assert fake.processes[0].terminated == 1 and fake.processes[0].killed == 1
    assert fake.processes[0].closed and not speech._io_thread.is_alive()
    fake.options["blocked"] = False
    assert speech.recognize(pcm)["confidence"] == 0.75
    speech.close()


def test_cancel_immediate_single_flight_and_reap(monkeypatch, directory, pcm):
    fake = context(monkeypatch, blocked=True)
    speech = LocalSpeech(directory)
    thread, result = start_recognition(speech, pcm)
    # The request event is only available after the owned IO thread starts.
    for _ in range(100):
        if hasattr(fake, "connection"):
            break
        threading.Event().wait(0.005)
    assert fake.connection.requested.wait(1)
    with pytest.raises(LocalSpeechError, match="^LOCAL_STT_BUSY$"):
        speech.recognize(pcm)
    speech.cancel()
    thread.join(2)
    assert not thread.is_alive() and result == ["LOCAL_STT_CANCELLED"]
    assert fake.processes[0].closed and fake.connection.closed


def test_cancel_during_spawn_never_starts_inference(monkeypatch, directory, pcm):
    spawned, release = threading.Event(), threading.Event()

    def slow_start():
        spawned.set()
        assert release.wait(2)

    fake = context(monkeypatch, start_callback=slow_start)
    speech = LocalSpeech(directory)
    thread, result = start_recognition(speech, pcm)
    assert spawned.wait(1)
    speech.cancel()  # Cannot wait for slow_start, which needs release below.
    release.set()
    thread.join(2)
    assert result == ["LOCAL_STT_CANCELLED"] and not thread.is_alive()
    assert fake.connection.sent == [] and fake.processes[0].closed


def test_timeout_during_spawn_cancels_late_child_before_any_pcm(monkeypatch, directory, pcm):
    spawned, release = threading.Event(), threading.Event()

    def slow_start():
        spawned.set()
        assert release.wait(3)

    fake = context(monkeypatch, start_callback=slow_start)
    monkeypatch.setattr(module, "_TIMEOUT_S", 0.01)
    speech = LocalSpeech(directory)
    thread, result = start_recognition(speech, pcm)
    assert spawned.wait(1)
    thread.join(2)
    assert result == ["LOCAL_STT_TIMEOUT"] and not thread.is_alive()
    with pytest.raises(LocalSpeechError, match="LOCAL_STT_BUSY"):
        speech.recognize(pcm)
    release.set()
    speech._io_thread.join(2)
    assert fake.connection.sent == [] and fake.processes[0].closed
    speech.close()


def test_close_inflight_reaps_and_rejects_later_work(monkeypatch, directory, pcm):
    fake = context(monkeypatch, blocked=True)
    speech = LocalSpeech(directory)
    thread, result = start_recognition(speech, pcm)
    for _ in range(100):
        if hasattr(fake, "connection"):
            break
        threading.Event().wait(0.005)
    assert fake.connection.requested.wait(1)
    speech.close()
    thread.join(2)
    assert result == ["LOCAL_STT_CANCELLED"] and not thread.is_alive()
    assert fake.processes[0].closed and not speech._io_thread.is_alive()


def test_error_code_never_includes_untrusted_detail():
    assert str(LocalSpeechError("arbitrary private content")) == "LOCAL_STT_FAILED"


def test_worker_offline_no_credentials_lazy_model_reuse(monkeypatch, directory, pcm):
    environment = {"PATH": "runtime-placeholder", "DEEPSEEK_API_KEY": "sentinel"}
    monkeypatch.setattr(module.os, "environ", environment)
    monkeypatch.setattr(module.logging, "disable", lambda _: None)
    priorities = []
    monkeypatch.setattr(module, "_lower_worker_priority", lambda: priorities.append("self"))
    loads, calls = [], []
    model = object()
    monkeypatch.setattr(module, "_load_model", lambda path: (loads.append(path), model)[1])
    monkeypatch.setattr(module, "_transcribe", lambda *args: (
        calls.append(args), {"text": "油", "confidence": 0.75},
    )[1])

    class Pipe:
        payloads = [b"z" + pcm, b"e" + pcm]
        output = []
        closed = False

        def poll(self, timeout):
            assert timeout == 60
            return bool(self.payloads)

        def recv_bytes(self, limit):
            assert limit == module.MAX_PCM_BYTES + 1
            return self.payloads.pop(0)

        def send_bytes(self, value):
            self.output.append(value)

        def close(self):
            self.closed = True

    pipe = Pipe()
    module._model_worker(pipe, str(directory))
    assert loads == [directory] and [row[2] for row in calls] == ["zh", "en"]
    assert all(module._response(value)["text"] == "油" for value in pipe.output)
    assert pipe.closed
    assert "DEEPSEEK_API_KEY" not in environment
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert priorities == ["self"]


def test_deadline_includes_received_response_validation(monkeypatch, directory, pcm):
    fake = context(monkeypatch)
    readings = iter([0, 26])
    monkeypatch.setattr(module, "monotonic_now", lambda: next(readings))
    with pytest.raises(LocalSpeechError, match="LOCAL_STT_TIMEOUT"):
        LocalSpeech(directory).recognize(pcm)
    assert fake.processes[0].closed


def test_worker_priority_uses_only_current_process(monkeypatch):
    calls = []

    def current():
        calls.append("current")
        return 42

    def set_priority(handle, priority):
        calls.append((handle, priority))
        return 1

    def dll(name, **kwargs):
        assert name == "kernel32" and kwargs == {"use_last_error": True}
        return SimpleNamespace(GetCurrentProcess=current, SetPriorityClass=set_priority)

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.ctypes, "WinDLL", dll, raising=False)
    module._lower_worker_priority()
    assert calls == ["current", (42, 0x4000)]


def test_priority_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "win32")

    def denied(*args, **kwargs):
        raise OSError("native detail not exposed")

    monkeypatch.setattr(module.ctypes, "WinDLL", denied, raising=False)
    module._lower_worker_priority()
