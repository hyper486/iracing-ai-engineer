"""Synthetic keys and SDL devices only; never polls real keyboard/joystick input."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest

from iracing_ai_engineer import voice_inputs as inputs

GUID_A, GUID_B = "1" * 32, "2" * 32
WHEEL = {"kind": "joystick", "guid": GUID_A, "name": "Synthetic Wheel", "button": 1}


class Joy:
    def __init__(self, instance=10, guid=GUID_A, name="Synthetic Wheel", buttons=None):
        self.instance, self.guid, self.name = instance, guid, name
        self.buttons = [False, False, False] if buttons is None else buttons
        self.reads, self.quit_calls = [], 0

    def get_instance_id(self):
        return self.instance

    def get_guid(self):
        return self.guid

    def get_name(self):
        return self.name

    def get_numbuttons(self):
        return len(self.buttons)

    def get_button(self, button):
        self.reads.append(button)
        return self.buttons[button]

    def quit(self):
        self.quit_calls += 1


class Pygame:
    HIDDEN, JOYDEVICEADDED, JOYDEVICEREMOVED, JOYBUTTONDOWN, JOYBUTTONUP = range(1, 6)

    def __init__(self):
        self.devices, self.events = [], []
        self.display_ready = self.joystick_ready = False
        self.threads, self.modes, self.blocked, self.allowed = [], [], [], []
        self.error = None
        self.display = SimpleNamespace(
            get_init=lambda: self.display_ready,
            init=lambda: self._init("display"),
            quit=lambda: self._quit("display"), set_mode=self._mode,
        )
        self.joystick = SimpleNamespace(
            get_init=lambda: self.joystick_ready,
            init=lambda: self._init("joystick"), quit=lambda: self._quit("joystick"),
            get_count=lambda: len(self.devices), Joystick=lambda index: self.devices[index],
        )
        self.event = SimpleNamespace(
            get=self._get, set_blocked=self.blocked.append, set_allowed=self.allowed.append,
        )

    def _init(self, kind):
        self.threads.append(threading.get_ident())
        setattr(self, kind + "_ready", True)

    def _quit(self, kind):
        self.threads.append(threading.get_ident())
        setattr(self, kind + "_ready", False)

    def _mode(self, size, flags):
        self.threads.append(threading.get_ident())
        self.modes.append((size, flags))

    def _get(self):
        self.threads.append(threading.get_ident())
        if self.error:
            raise self.error
        events, self.events = self.events, []
        return events


@pytest.fixture
def machine(monkeypatch):
    pg, log, keys = Pygame(), [], {"down": False, "queries": []}
    monkeypatch.setattr(inputs, "_make_pygame", lambda: pg)

    def read_key(vk):
        keys["queries"].append(vk)
        return keys["down"]

    monkeypatch.setattr(inputs, "_make_key_reader", lambda: read_key)
    poller = inputs.InputPoller(
        lambda: log.append(("press",)), lambda: log.append(("release",)),
        lambda binding: log.append(("binding", binding)), lambda code: log.append(("error", code)),
    )
    # Deterministically exercise worker ticks; this stub never starts any OS input.
    poller._thread = SimpleNamespace(is_alive=lambda: False, join=lambda timeout: None)
    now = [1.0]

    def tick(gap=0.03):
        now[0] += gap
        poller._poll_once(now[0])

    yield SimpleNamespace(poller=poller, pg=pg, log=log, keys=keys, tick=tick)
    poller.close()
    poller._run()  # Already stopped: execute worker-finally cleanup only.
    assert not inputs._SDL_OWNER.locked()


def test_keyboard_start_held_requires_observed_release_then_fresh_press(machine):
    m = machine
    m.keys["down"] = True
    m.tick()
    m.tick()
    assert m.log == []
    m.keys["down"] = False
    m.tick()
    m.keys["down"] = True
    m.tick()
    m.tick()
    m.keys["down"] = False
    m.tick()
    assert m.log == [("press",), ("release",)]
    assert set(m.keys["queries"]) == {0x78}  # F9 only; no scan of other keys.
    assert m.pg.modes == []


def test_binding_change_cancels_before_release_and_new_held_key_is_not_capture(machine):
    m = machine
    m.tick()
    m.keys["down"] = True
    m.tick()
    m.poller.set_binding({"kind": "keyboard", "key": "F12"})
    m.tick()
    assert m.log == [("press",), ("error", "INPUT_BINDING_CHANGED"), ("release",)]
    assert m.keys["queries"][-1] == 0x7B
    m.tick()
    assert len(m.log) == 3


def test_control_change_during_a_key_read_drops_the_old_result(machine, monkeypatch):
    m = machine
    m.tick()

    def changing(_vk):
        m.poller.set_binding({"kind": "keyboard", "key": "F10"})
        return True

    monkeypatch.setattr(m.poller, "_key_reader", changing)
    m.tick()
    assert m.log == []


def test_windowless_keyboard_reader_uses_only_high_bit_and_selected_vk(monkeypatch):
    calls, values = [], iter((-32768, 1, 0))

    class Function:
        def __call__(self, vk):
            calls.append(vk)
            return next(values)

    function = Function()
    monkeypatch.setattr(inputs.os, "name", "nt")
    monkeypatch.setattr(inputs.ctypes, "WinDLL", lambda *args, **kwargs: SimpleNamespace(
        GetAsyncKeyState=function,
    ), raising=False)
    reader = inputs._make_key_reader()
    assert [reader(0x78) for _ in range(3)] == [True, False, False]
    assert calls == [0x78] * 3
    assert function.restype is inputs.ctypes.c_short


def test_joystick_reads_only_selected_button_and_recovers_missing_up_event(machine):
    m = machine
    chosen, other = Joy(), Joy(20, GUID_B, "Other Synthetic Device")
    m.pg.devices = [chosen, other]
    m.poller.set_binding(WHEEL)
    m.tick()
    chosen.buttons[1] = True
    m.tick()
    m.pg.devices.reverse()  # SDL device indices change, instance identity does not.
    m.tick()
    chosen.buttons[1] = False  # No JOYBUTTONUP event: current state still releases.
    m.tick()
    assert m.log == [("press",), ("release",)]
    assert set(chosen.reads) == {1} and other.reads == []
    assert m.keys["queries"] == []
    assert m.pg.modes == [((1, 1), m.pg.HIDDEN)]
    assert m.pg.blocked == [None]
    assert set(m.pg.allowed[0]) == {
        m.pg.JOYDEVICEADDED, m.pg.JOYDEVICEREMOVED, m.pg.JOYBUTTONDOWN, m.pg.JOYBUTTONUP,
    }


def test_disconnect_cancels_recording_without_keyboard_fallback(machine):
    m = machine
    joy = Joy()
    m.pg.devices = [joy]
    m.poller.set_binding(WHEEL)
    m.tick()
    joy.buttons[1] = True
    m.tick()
    m.pg.devices = []
    m.tick()
    m.tick()
    assert m.log == [("press",), ("error", "INPUT_DEVICE_MISSING"), ("release",)]
    assert m.poller.snapshot()["status"] == "WAIT_DEVICE"
    assert m.keys["queries"] == []
    assert joy.quit_calls == 1


def test_reconnect_same_fingerprint_new_instance_cancels_and_does_not_capture_held(machine):
    m = machine
    joy = Joy()
    m.pg.devices = [joy]
    m.poller.set_binding(WHEEL)
    m.tick()
    joy.buttons[1] = True
    m.tick()
    replacement = Joy(11, buttons=[False, True, False])
    m.pg.devices = [replacement]
    m.tick()
    m.tick()
    assert m.log == [("press",), ("error", "INPUT_DEVICE_CHANGED"), ("release",)]
    replacement.buttons[1] = False
    m.tick()
    replacement.buttons[1] = True
    m.tick()
    assert m.log[-1] == ("press",)


def test_duplicate_guid_and_name_never_picks_an_arbitrary_device(machine):
    m = machine
    first, duplicate = Joy(), Joy(11)
    m.pg.devices = [first]
    m.poller.set_binding(WHEEL)
    m.tick()
    first.buttons[1] = True
    m.tick()
    m.pg.devices.append(duplicate)
    m.tick()
    assert m.log == [("press",), ("error", "INPUT_DEVICE_AMBIGUOUS"), ("release",)]
    m.pg.devices.remove(duplicate)
    m.tick()
    assert len(m.log) == 3


def test_bind_requires_new_edge_and_binding_press_is_never_a_recording(machine):
    m = machine
    joy = Joy(buttons=[True, False, False])
    m.pg.devices = [joy]
    m.poller.begin_bind()
    assert m.poller.snapshot()["binding_active"] is True
    m.tick()
    m.tick()
    assert not m.log
    joy.buttons[0] = False
    m.tick()
    joy.buttons[0] = True
    m.tick()
    expected = {**WHEEL, "button": 0}
    assert m.log == [("binding", expected)]
    assert m.poller.snapshot()["binding"] == expected
    assert m.poller.snapshot()["binding_active"] is False
    m.tick()
    m.tick()
    assert len(m.log) == 1
    joy.buttons[0] = False
    m.tick()
    joy.buttons[0] = True
    m.tick()
    assert m.log[-1] == ("press",)


def test_bind_ignores_a_newly_connected_device_already_held(machine):
    m = machine
    m.poller.begin_bind()
    m.tick()
    joy = Joy(buttons=[False, True, False])
    m.pg.devices = [joy]
    m.tick()
    m.tick()
    assert not m.log
    joy.buttons[1] = False
    m.tick()
    joy.buttons[1] = True
    m.tick()
    assert m.log == [("binding", WHEEL)]


@pytest.mark.parametrize("duplicate", [False, True])
def test_bind_rejects_multiple_edges_or_indistinguishable_devices(machine, duplicate):
    m = machine
    first, second = Joy(), Joy(11, GUID_A if duplicate else GUID_B)
    m.pg.devices = [first, second]
    m.poller.begin_bind()
    m.tick()
    first.buttons[1] = True
    if not duplicate:
        second.buttons[1] = True
    m.tick()
    assert m.log == [("error", "INPUT_BIND_AMBIGUOUS")]
    assert m.poller.snapshot()["binding_active"] is True
    m.tick()
    assert len(m.log) == 1


def test_cancel_bind_restores_explicit_keyboard_and_suppresses_held_key(machine):
    m = machine
    m.poller.begin_bind()
    m.tick()
    m.keys["down"] = True
    m.poller.cancel_bind()
    m.tick()
    assert m.poller.snapshot()["binding_active"] is False
    assert m.poller.snapshot()["binding"] == inputs.DEFAULT_BINDING
    assert not m.log and m.pg.display_ready is False


@pytest.mark.parametrize("gap", [0.50001, -0.01])
def test_long_or_backward_poll_gap_cancels_and_requires_new_edge(machine, gap):
    m = machine
    m.tick()
    m.keys["down"] = True
    m.tick()
    m.tick(gap)
    assert m.log == [("press",), ("error", "INPUT_POLL_GAP"), ("release",)]
    m.tick()
    assert len(m.log) == 3


def test_snapshot_and_binding_callback_cannot_mutate_internal_binding(machine):
    m = machine
    chosen = WHEEL.copy()
    m.poller.set_binding(chosen)
    chosen["button"] = 99
    value = m.poller.snapshot()
    value["binding"]["button"] = 98
    assert m.poller.snapshot()["binding"] == WHEEL


@pytest.mark.parametrize("binding", [
    None, [], {}, {"kind": "keyboard", "key": "A"}, {"kind": "keyboard", "key": True},
    {"kind": "keyboard", "key": "F7"}, {"kind": "keyboard", "key": "F13"},
    {"kind": "keyboard", "key": "F9", "all_keys": True},
    {**WHEEL, "guid": "not-an-sdl-guid"}, {**WHEEL, "name": ""},
    {**WHEEL, "name": "bad\nname"}, {**WHEEL, "button": True},
    {**WHEEL, "button": 1.0}, {**WHEEL, "button": -1}, {**WHEEL, "button": 256},
    {**WHEEL, "device_index": 0},
])
def test_invalid_bindings_are_rejected_before_hardware_access(machine, binding):
    with pytest.raises(ValueError, match="^INPUT_BINDING_INVALID$"):
        machine.poller.set_binding(binding)
    assert machine.pg.modes == [] and machine.keys["queries"] == []


def test_sdl_resources_restore_environment_and_cannot_have_two_owners(machine, monkeypatch):
    m = machine
    monkeypatch.setenv("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "0")
    monkeypatch.delenv("PYGAME_HIDE_SUPPORT_PROMPT", raising=False)
    m.poller.begin_bind()
    m.tick()
    assert os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] == "1"
    with pytest.raises(inputs._InputError, match="INPUT_SDL_IN_USE"):
        inputs._JoystickReader()
    m.poller.cancel_bind()
    m.tick()
    assert os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] == "0"
    assert "PYGAME_HIDE_SUPPORT_PROMPT" not in os.environ
    assert not m.pg.display_ready and not m.pg.joystick_ready


def _wait(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("SYNTHETIC_WORKER_TIMEOUT")
        time.sleep(0.005)


def test_real_worker_can_close_restart_and_cancel_active_key_without_touching_hardware(monkeypatch):
    log, state = [], {"down": True, "reads": 0}

    def reader(_vk):
        state["reads"] += 1
        return state["down"]

    monkeypatch.setattr(inputs, "_make_key_reader", lambda: reader)
    p = inputs.InputPoller(lambda: log.append("press"), lambda: log.append("release"),
                           lambda binding: None, lambda code: log.append(code))
    try:
        p.start()
        _wait(lambda: state["reads"] >= 2)
        assert not log
        state["down"] = False
        previous = state["reads"]
        _wait(lambda: state["reads"] > previous)
        state["down"] = True
        _wait(lambda: "press" in log)
        p.close()
        assert log == ["press", "INPUT_CLOSED", "release"]
        assert p.snapshot()["status"] == "CLOSED" and not p._thread.is_alive()
        p.set_binding({"kind": "keyboard", "key": "F10"})
        previous = state["reads"]
        p.start({"kind": "keyboard", "key": "F10"})
        _wait(lambda: state["reads"] >= previous + 2)
        assert len(log) == 3  # Still held across restart: no capture.
        with pytest.raises(ValueError, match="INPUT_ALREADY_STARTED_OR_STOPPING"):
            p.start()
    finally:
        p.close()
    assert not p._thread.is_alive()


def test_worker_owns_sdl_thread_and_sanitizes_failure_before_release(monkeypatch):
    pg, log = Pygame(), []
    joy = Joy()
    pg.devices = [joy]
    monkeypatch.setattr(inputs, "_make_pygame", lambda: pg)
    p = inputs.InputPoller(lambda: log.append("press"), lambda: log.append("release"),
                           lambda binding: None, lambda code: log.append(code))
    try:
        p.start(WHEEL)
        _wait(lambda: p.snapshot()["status"] == "READY")
        joy.buttons[1] = True
        _wait(lambda: "press" in log)
        pg.error = RuntimeError("SYNTHETIC_SECRET_NOT_PUBLIC")
        _wait(lambda: "release" in log)
        assert log == ["press", "INPUT_POLL_FAILED", "release"]
        assert p.snapshot()["status"] == "ERROR"
    finally:
        p.close()
    assert set(pg.threads) == {p._thread.ident}
    assert threading.get_ident() not in pg.threads
    assert not inputs._SDL_OWNER.locked()


def test_callback_error_stops_worker_and_releases_without_exception_text(monkeypatch):
    log, down = [], [False]
    monkeypatch.setattr(inputs, "_make_key_reader", lambda: lambda vk: down[0])

    def broken():
        raise RuntimeError("SYNTHETIC_CALLBACK_SECRET")

    p = inputs.InputPoller(broken, lambda: log.append("release"),
                           lambda binding: None, lambda code: log.append(code))
    try:
        p.start()
        _wait(lambda: p.snapshot()["status"] == "READY")
        down[0] = True
        _wait(lambda: not p._thread.is_alive())
        assert log == ["INPUT_CALLBACK_FAILED", "INPUT_CLOSED", "release"]
        assert p.snapshot()["status"] == "ERROR"
    finally:
        p.close()


def test_close_is_bounded_and_restart_cannot_overlap_a_blocked_callback(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    down = [False]
    monkeypatch.setattr(inputs, "_make_key_reader", lambda: lambda vk: down[0])

    def delayed():
        entered.set()
        release.wait(5)

    p = inputs.InputPoller(delayed, lambda: None, lambda binding: None, lambda code: None)
    try:
        p.start()
        _wait(lambda: p.snapshot()["status"] == "READY")
        down[0] = True
        assert entered.wait(1)
        started = time.monotonic()
        p.close()
        assert time.monotonic() - started < 2.5
        assert p.snapshot()["status"] == "STOPPING"
        with pytest.raises(ValueError, match="INPUT_ALREADY_STARTED_OR_STOPPING"):
            p.start()
    finally:
        release.set()
        p.close()
    assert not p._thread.is_alive() and p.snapshot()["status"] == "CLOSED"


def test_preexisting_sdl_session_is_not_adopted_or_shut_down(machine):
    m = machine
    m.pg.display_ready = True
    m.poller.begin_bind()
    with pytest.raises(inputs._InputError, match="INPUT_SDL_IN_USE"):
        m.tick()
    assert m.pg.display_ready is True and not m.pg.threads
    assert not inputs._SDL_OWNER.locked()


@pytest.mark.parametrize("count", [-1, 33, True, 1.0])
def test_invalid_or_excess_device_count_is_bounded_before_enumeration(machine, count):
    m = machine
    m.pg.joystick.get_count = lambda: count
    m.poller.begin_bind()
    with pytest.raises(inputs._InputError, match="INPUT_DEVICE_LIMIT"):
        m.tick()
    assert not m.log


def test_event_overflow_fails_closed_instead_of_guessing_a_binding(machine):
    m = machine
    m.pg.events = [object()] * 2049
    m.poller.begin_bind()
    with pytest.raises(inputs._InputError, match="INPUT_EVENT_OVERFLOW"):
        m.tick()
    assert not m.log and m.poller.snapshot()["binding_active"] is True


def test_missing_button_is_not_silently_remapped(machine):
    m = machine
    m.pg.devices = [Joy(buttons=[False])]
    m.poller.set_binding(WHEEL)
    m.tick()
    assert m.log == [("error", "INPUT_BUTTON_UNAVAILABLE")]
    assert m.poller.snapshot()["status"] == "WAIT_DEVICE"


def test_binding_callback_receives_a_defensive_copy(machine):
    m = machine
    joy = Joy()
    m.pg.devices = [joy]

    def mutate(binding):
        binding["button"] = 99

    m.poller._on_binding = mutate
    m.poller.begin_bind()
    m.tick()
    joy.buttons[1] = True
    m.tick()
    assert m.poller.snapshot()["binding"] == WHEEL


def test_begin_bind_cancels_an_active_ptt_before_collecting_wheel_edges(machine):
    m = machine
    m.tick()
    m.keys["down"] = True
    m.tick()
    m.poller.begin_bind()
    m.tick()
    assert m.log == [("press",), ("error", "INPUT_BINDING_CHANGED"), ("release",)]
    assert m.poller.snapshot()["binding_active"] is True


def test_not_started_bind_refused_and_close_before_start_is_safe():
    p = inputs.InputPoller(lambda: None, lambda: None, lambda binding: None, lambda code: None)
    with pytest.raises(ValueError, match="INPUT_NOT_RUNNING"):
        p.begin_bind()
    p.close()
    p.close()
    assert p.snapshot()["status"] == "CLOSED"


def test_sdl_initialization_failure_restores_only_owned_resources(machine):
    m = machine

    def broken(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_INIT_FAILURE")

    m.pg.display.set_mode = broken
    m.poller.begin_bind()
    with pytest.raises(RuntimeError, match="SYNTHETIC_INIT_FAILURE"):
        m.tick()
    assert not m.pg.display_ready and not m.pg.joystick_ready
    assert not inputs._SDL_OWNER.locked()
