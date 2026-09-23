"""All audio devices/streams are fake. No microphone or speaker is opened."""

import io
import struct
import threading
import wave
from types import SimpleNamespace

import pytest

from iracing_ai_engineer import voice_audio as module
from iracing_ai_engineer.voice_audio import AudioError, AudioIO


def _wav(samples=(1000, -1000), *, rate=16_000, channels=1, width=2):
    target = io.BytesIO()
    with wave.open(target, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(
            struct.pack("<" + "h" * len(samples), *samples)
            if width == 2
            else bytes(len(samples) * width)
        )
    return target.getvalue()


class _Stop(Exception):
    pass


class _Abort(Exception):
    pass


class _Stream:
    def __init__(self, owner, kind, options):
        self.owner, self.kind, self.options = owner, kind, options
        self.closed, self.aborted = False, False
        self.output = bytearray()

    def start(self):
        if self.owner.on_start:
            self.owner.on_start(self)
            return
        self.feed()

    def feed(self):
        for _ in range(20_000):
            count = self.options["blocksize"]
            buffer = bytearray(b"\x10\x00" * count * self.options["channels"])
            try:
                self.options["callback"](buffer, count, None, self.owner.status)
            except (_Stop, _Abort):
                if self.kind == "output":
                    self.output.extend(buffer)
                if self.owner.finish_immediately:
                    self.options["finished_callback"]()
                return
            if self.kind == "output":
                self.output.extend(buffer)
        pytest.fail("unbounded callback loop")

    def abort(self):
        self.aborted = True
        if self.owner.abort_error:
            raise OSError("private driver diagnostic")

    def close(self):
        self.closed = True
        if self.owner.close_error:
            raise OSError("private driver diagnostic")


class _Backend:
    CallbackStop, CallbackAbort = _Stop, _Abort

    def __init__(self):
        self._initialized = 1
        self.refreshes = 0
        self.default = SimpleNamespace(device=(0, 1))
        self.devices = [
            {
                "name": "Desktop microphone",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 0,
            },
            {"name": "Headset", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        ]
        self.hosts = [{"name": "Windows WASAPI"}]
        self.streams = []
        self.on_start = None
        self.on_refresh = None
        self.status = False
        self.finish_immediately = True
        self.abort_error = self.close_error = False
        self.wasapi_settings = []

    def WasapiSettings(self, **options):
        value = SimpleNamespace(**options)
        self.wasapi_settings.append(value)
        return value

    def _terminate(self):
        assert all(stream.closed for stream in self.streams)
        self._initialized -= 1

    def _initialize(self):
        self._initialized += 1
        self.refreshes += 1
        if self.on_refresh:
            self.on_refresh()

    def query_devices(self):
        return self.devices

    def query_hostapis(self):
        return self.hosts

    def _stream(self, kind, options):
        stream = _Stream(self, kind, options)
        self.streams.append(stream)
        return stream

    def RawInputStream(self, **options):
        return self._stream("input", options)

    def RawOutputStream(self, **options):
        return self._stream("output", options)


def test_constructing_audio_does_not_import_backend_or_open_devices():
    audio = AudioIO()
    assert audio._backend is None


@pytest.mark.parametrize("after_open", [False, True])
def test_race_call_is_revalidated_after_device_resolution_and_open(after_open):
    backend, permitted = _Backend(), [True]
    if after_open:
        original = backend.RawOutputStream

        def open_then_withdraw(**kwargs):
            stream = original(**kwargs)
            permitted[0] = False
            return stream

        backend.RawOutputStream = open_then_withdraw
    else:
        backend.on_refresh = lambda: permitted.__setitem__(0, False)
    started = []
    stop = threading.Event()
    AudioIO(backend=backend).play(_wav(), stop, start_guard=lambda: permitted[0],
                                  on_started=lambda: started.append(True))
    assert stop.is_set() and started == []
    assert all(stream.closed and not stream.output for stream in backend.streams)


def test_start_receipt_only_follows_real_backend_start_call():
    backend, started = _Backend(), []
    AudioIO(backend=backend).play(
        _wav(), threading.Event(),
        on_started=lambda: started.append(bool(backend.streams[0].output)),
    )
    assert started == [True]


def test_withdrawn_after_open_still_reports_cleanup_failure():
    backend, started = _Backend(), []
    backend.close_error = True
    audio = AudioIO(backend=backend)
    with pytest.raises(AudioError, match="^AUDIO_PLAY_FAILED$") as failure:
        audio.play(_wav(), threading.Event(), start_guard=lambda: not backend.streams,
                   on_started=lambda: started.append(True))
    assert failure.value.__context__ is None and started == []
    assert backend.streams[0].closed and not backend.streams[0].output
    backend.close_error = False
    audio.play(_wav(), threading.Event())  # Ownership was released despite the error.
    assert backend.streams[-1].closed and backend.streams[-1].output


def test_catalog_is_stable_across_device_and_host_reordering():
    backend = _Backend()
    audio = AudioIO(backend=backend)
    before = audio.devices()
    assert set(before) == {"inputs", "outputs"}
    assert all(set(row) == {"id", "name"} for rows in before.values() for row in rows)
    backend.devices.reverse()
    backend.hosts.insert(0, {"name": "Other API"})
    for row in backend.devices:
        row["hostapi"] = 1
    assert audio.devices() == before
    assert backend.refreshes == 2
    assert not backend.streams


def test_duplicate_stable_device_name_fails_closed():
    backend = _Backend()
    audio = AudioIO(backend=backend)
    selected = audio.devices()["inputs"][0]["id"]
    backend.devices.append(dict(backend.devices[0]))
    assert audio.devices()["inputs"] == []
    assert len(audio.devices()["outputs"]) == 1
    with pytest.raises(AudioError, match="^AUDIO_DEVICE_AMBIGUOUS$"):
        audio.record(threading.Event(), selected)
    assert not backend.streams


def test_explicit_system_default_uses_index_not_ambiguous_name():
    backend = _Backend()
    backend.devices.append(dict(backend.devices[0]))
    backend.default.device = (2, 1)
    AudioIO(backend=backend).record(threading.Event(), max_seconds=0.02)
    assert backend.streams[0].options["device"] == 2


def test_duplicate_native_indexes_fail_closed():
    backend = _Backend()
    for row in backend.devices:
        row["index"] = 0
    with pytest.raises(AudioError, match="^AUDIO_UNAVAILABLE$"):
        AudioIO(backend=backend).devices()


def test_driver_control_characters_omitted_without_disabling_valid_devices():
    backend = _Backend()
    backend.devices.append({**backend.devices[0], "name": "broken\x00name"})
    audio = AudioIO(backend=backend)
    assert len(audio.devices()["inputs"]) == 1
    backend.default.device = (2, 1)
    with pytest.raises(AudioError, match="^AUDIO_DEVICE_MISSING$"):
        audio.record(threading.Event())
    assert not backend.streams


def test_same_name_different_host_api_is_not_ambiguous():
    backend = _Backend()
    backend.hosts.append({"name": "Windows MME"})
    backend.devices.append({**backend.devices[0], "hostapi": 1})
    inputs = AudioIO(backend=backend).devices()["inputs"]
    assert len(inputs) == 2 and inputs[0]["id"] != inputs[1]["id"]


@pytest.mark.parametrize("kind", ["record", "play"])
def test_explicit_removed_device_never_falls_back_to_default(kind):
    backend = _Backend()
    audio = AudioIO(backend=backend)
    selected = audio.devices()["inputs" if kind == "record" else "outputs"][0]["id"]
    backend.devices[0 if kind == "record" else 1]["name"] = "Replacement device"
    with pytest.raises(AudioError, match="^AUDIO_DEVICE_MISSING$"):
        if kind == "record":
            audio.record(threading.Event(), selected)
        else:
            audio.play(_wav(), threading.Event(), selected)
    assert not backend.streams


def test_explicit_device_identity_survives_native_index_change():
    backend = _Backend()
    audio = AudioIO(backend=backend)
    selected = audio.devices()["outputs"][0]["id"]
    backend.devices.reverse()
    audio.play(_wav(), threading.Event(), selected)
    assert backend.streams[0].options["device"] == 0


def test_system_default_reinitialized_for_each_operation():
    backend = _Backend()
    backend.devices.append({**backend.devices[1], "name": "New headset"})
    audio = AudioIO(backend=backend)
    audio.play(_wav(), threading.Event())
    assert backend.streams[-1].options["device"] == 1
    backend.on_refresh = lambda: setattr(backend.default, "device", (0, 2))
    audio.play(_wav(), threading.Event())
    assert backend.streams[-1].options["device"] == 2
    assert backend.refreshes == 2


def test_external_portaudio_reference_not_terminated():
    backend = _Backend()
    backend._initialized = 2
    with pytest.raises(AudioError, match="^AUDIO_UNAVAILABLE$"):
        AudioIO(backend=backend).devices()
    assert backend._initialized == 2 and backend.refreshes == 0


def test_initialization_failure_safe_and_retry_possible():
    backend = _Backend()
    original = backend._initialize
    backend._initialize = lambda: (_ for _ in ()).throw(OSError("private hardware details"))
    audio = AudioIO(backend=backend)
    with pytest.raises(AudioError, match="^AUDIO_UNAVAILABLE$") as failure:
        audio.devices()
    assert failure.value.__context__ is None and backend._initialized == 0
    backend._initialize = original
    assert audio.devices()["inputs"]


@pytest.mark.parametrize("default", [(-1, -1), (False, True), (98, 99)])
def test_invalid_system_default_never_uses_first_available(default):
    backend = _Backend()
    backend.default.device = default
    with pytest.raises(AudioError, match="^AUDIO_DEVICE_MISSING$"):
        AudioIO(backend=backend).record(threading.Event())
    assert not backend.streams


def test_record_exact_pcm_contract_and_bounded_duration():
    backend = _Backend()
    raw = AudioIO(backend=backend).record(threading.Event(), max_seconds=0.04)
    assert raw == b"\x10\x00" * 640
    stream = backend.streams[0]
    assert stream.options["samplerate"] == 16_000
    assert stream.options["dtype"] == "int16" and stream.options["channels"] == 1
    assert stream.options["device"] == 0
    assert stream.closed and stream.aborted


@pytest.mark.parametrize("kind", ["record", "play"])
@pytest.mark.parametrize("explicit", [False, True])
def test_wasapi_shared_conversion_keeps_selected_device_and_requested_rate(kind, explicit):
    backend = _Backend()
    backend.devices[0]["default_samplerate"] = 48_000
    backend.devices[1]["default_samplerate"] = 48_000
    audio = AudioIO(backend=backend)
    group = "inputs" if kind == "record" else "outputs"
    selector = audio.devices()[group][0]["id"] if explicit else "default"
    original_default = backend.default.device
    if kind == "record":
        assert audio.record(threading.Event(), selector, max_seconds=0.02) == b"\x10\x00" * 320
    else:
        audio.play(_wav(rate=22_050), threading.Event(), selector)
    stream = backend.streams[0]
    settings = stream.options["extra_settings"]
    assert settings is backend.wasapi_settings[0] and len(backend.wasapi_settings) == 1
    assert vars(settings) == {"exclusive": False, "auto_convert": True}
    assert stream.options["samplerate"] == (16_000 if kind == "record" else 22_050)
    assert stream.options["device"] == (0 if kind == "record" else 1)
    assert stream.options["dtype"] == "int16"
    assert backend.default.device == original_default
    assert backend.refreshes == (2 if explicit else 1)


@pytest.mark.parametrize("kind", ["record", "play"])
@pytest.mark.parametrize("host", ["Windows MME", "Windows DirectSound", "ASIO", "Core Audio"])
def test_non_wasapi_streams_do_not_receive_wasapi_options(kind, host):
    backend = _Backend()
    backend.hosts[0]["name"] = host
    audio = AudioIO(backend=backend)
    if kind == "record":
        audio.record(threading.Event(), max_seconds=0.02)
    else:
        audio.play(_wav(rate=22_050), threading.Event())
    assert "extra_settings" not in backend.streams[0].options
    assert backend.wasapi_settings == []


@pytest.mark.parametrize("kind", ["record", "play"])
def test_wasapi_options_follow_actual_selected_host_not_first_host(kind):
    backend = _Backend()
    backend.hosts.insert(0, {"name": "Windows MME"})
    for item in backend.devices:
        item["hostapi"] = 1
    audio = AudioIO(backend=backend)
    if kind == "record":
        audio.record(threading.Event(), max_seconds=0.02)
    else:
        audio.play(_wav(), threading.Event())
    assert vars(backend.streams[0].options["extra_settings"]) == {
        "exclusive": False, "auto_convert": True,
    }


@pytest.mark.parametrize("kind", ["record", "play"])
def test_wasapi_conversion_configuration_failure_never_falls_back(kind):
    backend = _Backend()
    audio = AudioIO(backend=backend)
    selector = audio.devices()["inputs" if kind == "record" else "outputs"][0]["id"]

    def fail(**_options):
        raise RuntimeError("private native configuration detail")

    backend.WasapiSettings = fail
    with pytest.raises(AudioError, match=f"^AUDIO_{kind.upper()}_FAILED$") as failure:
        if kind == "record":
            audio.record(threading.Event(), selector, max_seconds=0.02)
        else:
            audio.play(_wav(), threading.Event(), selector)
    assert failure.value.__context__ is None and backend.streams == []


def test_record_full_limit_has_bounded_memory():
    backend = _Backend()
    assert len(AudioIO(backend=backend).record(threading.Event())) == 12 * 16_000 * 2


def test_already_released_ptt_opens_no_backend():
    stop = threading.Event()
    stop.set()
    audio = AudioIO()
    assert audio.record(stop) == b""
    audio.play(_wav(), stop)
    assert audio._backend is None


def test_release_during_record_preserves_only_captured_prefix():
    backend = _Backend()
    stop = threading.Event()

    def start(stream):
        stream.options["callback"](b"\x01\x00" * 320, 320, None, False)
        stop.set()

    backend.on_start = start
    assert AudioIO(backend=backend).record(stop) == b"\x01\x00" * 320
    assert backend.streams[0].closed


def test_release_inside_callback_discards_later_samples():
    backend = _Backend()
    stop = threading.Event()

    def start(stream):
        stop.set()
        with pytest.raises(_Abort):
            stream.options["callback"](b"\x01\x00" * 320, 320, None, False)

    backend.on_start = start
    assert AudioIO(backend=backend).record(stop) == b""


def test_record_deadline_without_callbacks_does_not_hang(monkeypatch):
    backend = _Backend()
    backend.on_start = lambda _: None
    ticks = iter((0.0, 13.0))
    monkeypatch.setattr(module, "monotonic_now", lambda: next(ticks))
    assert AudioIO(backend=backend).record(threading.Event()) == b""
    assert backend.streams[0].closed


@pytest.mark.parametrize("maximum", [True, -1, 0, 12.1, float("nan"), float("inf"), "12"])
def test_record_invalid_maximum_opens_no_stream(maximum):
    backend = _Backend()
    with pytest.raises(AudioError, match="^AUDIO_INPUT_INVALID$"):
        AudioIO(backend=backend).record(threading.Event(), max_seconds=maximum)
    assert not backend.streams and backend.refreshes == 0


@pytest.mark.parametrize(
    "kind,code",
    [
        ("record", "AUDIO_INPUT_OVERFLOW"),
        ("play", "AUDIO_OUTPUT_UNDERRUN"),
    ],
)
def test_dropped_audio_is_not_silently_accepted(kind, code):
    backend = _Backend()
    backend.status = True
    with pytest.raises(AudioError, match=f"^{code}$"):
        if kind == "record":
            AudioIO(backend=backend).record(threading.Event())
        else:
            AudioIO(backend=backend).play(_wav(), threading.Event())
    assert backend.streams[0].closed


@pytest.mark.parametrize("kind", ["record", "play"])
def test_driver_start_failure_is_safe_and_closes_stream(kind):
    backend = _Backend()
    backend.on_start = lambda _: (_ for _ in ()).throw(OSError("private device path"))
    with pytest.raises(AudioError, match=f"^AUDIO_{kind.upper()}_FAILED$") as failure:
        if kind == "record":
            AudioIO(backend=backend).record(threading.Event())
        else:
            AudioIO(backend=backend).play(_wav(), threading.Event())
    assert failure.value.__context__ is None
    assert backend.streams[0].closed and backend.streams[0].aborted


def test_close_attempted_even_when_abort_fails():
    backend = _Backend()
    backend.abort_error = True
    AudioIO(backend=backend).play(_wav(), threading.Event())
    assert backend.streams[0].closed


def test_close_failure_is_not_reported_as_success():
    backend = _Backend()
    backend.close_error = True
    with pytest.raises(AudioError, match="^AUDIO_PLAY_FAILED$"):
        AudioIO(backend=backend).play(_wav(), threading.Event())


def test_play_volume_scaling_pcm_and_padding():
    backend = _Backend()
    AudioIO(backend=backend).play(_wav(), threading.Event(), volume=0.5)
    stream = backend.streams[0]
    assert stream.output[:4] == struct.pack("<hh", 500, -500)
    assert not any(stream.output[4:])
    assert stream.options["device"] == 1
    assert stream.options["samplerate"] == 16_000 and stream.options["channels"] == 1
    assert stream.closed and stream.aborted


def test_play_waits_for_final_buffer_drain_and_blocks_other_instances():
    backend = _Backend()
    backend.finish_immediately = False
    entered = threading.Event()

    def start(stream):
        stream.feed()
        entered.set()

    backend.on_start = start
    worker = threading.Thread(
        target=lambda: AudioIO(backend=backend).play(_wav(), threading.Event())
    )
    worker.start()
    try:
        assert entered.wait(2)
        assert worker.is_alive() and not backend.streams[0].closed
        with pytest.raises(AudioError, match="^AUDIO_BUSY$"):
            AudioIO(backend=backend).devices()
        with pytest.raises(AudioError, match="^AUDIO_BUSY$"):
            AudioIO(backend=backend).record(threading.Event())
        backend.streams[0].options["finished_callback"]()
    finally:
        if backend.streams:
            backend.streams[0].options["finished_callback"]()
        worker.join(2)
    assert not worker.is_alive() and backend.streams[0].closed


def test_play_cancel_aborts_queued_audio_without_waiting_for_finished():
    backend = _Backend()
    backend.finish_immediately = False
    stop = threading.Event()
    backend.on_start = lambda _: stop.set()
    AudioIO(backend=backend).play(_wav(), stop)
    assert backend.streams[0].closed and backend.streams[0].aborted


def test_play_timeout_stops_and_closes(monkeypatch):
    backend = _Backend()
    backend.on_start = lambda _: None
    ticks = iter((0.0, 200.0))
    monkeypatch.setattr(module, "monotonic_now", lambda: next(ticks))
    with pytest.raises(AudioError, match="^AUDIO_TIMEOUT$"):
        AudioIO(backend=backend).play(_wav(), threading.Event())
    assert backend.streams[0].closed


@pytest.mark.parametrize("volume", [True, -1, 1.1, float("nan"), float("inf"), "0.5"])
def test_invalid_volume_opens_no_stream(volume):
    backend = _Backend()
    with pytest.raises(AudioError, match="^AUDIO_INPUT_INVALID$"):
        AudioIO(backend=backend).play(_wav(), threading.Event(), volume=volume)
    assert not backend.streams and backend.refreshes == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        _wav()[:-1],
        _wav() + b"trailing",
        _wav(rate=96_000),
        _wav(width=1),
        _wav(channels=3, samples=(1, 2, 3)),
        _wav(samples=()),
        b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 32,
        b"x" * (module.MAX_WAV_BYTES + 1),
        bytearray(_wav()),
    ],
    ids=[
        "empty",
        "truncated",
        "trailing",
        "rate",
        "width",
        "channels",
        "no-frames",
        "header",
        "limit",
        "mutable",
    ],
)
def test_invalid_wav_opens_no_stream(raw):
    backend = _Backend()
    with pytest.raises(AudioError, match="^AUDIO_INPUT_INVALID$"):
        AudioIO(backend=backend).play(raw, threading.Event())
    assert not backend.streams and backend.refreshes == 0


def test_unsupported_selected_channels_do_not_fall_back():
    backend = _Backend()
    backend.devices[1]["max_output_channels"] = 1
    with pytest.raises(AudioError, match="^AUDIO_DEVICE_MISSING$"):
        AudioIO(backend=backend).play(_wav(channels=2), threading.Event())
    assert not backend.streams
