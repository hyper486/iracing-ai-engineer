"""Read-only push-to-talk edges; no hooks, injected input, axes or device output.

Callbacks run on the input worker and must return promptly. Cancellation emits
on_error before on_release; the owner must discard that recording, not submit it.
Keyboard polling reads only the configured function key. SDL is initialized,
pumped and closed by one worker, with a hidden window for its event queue.

References: https://www.pygame.org/docs/ref/joystick.html
https://www.pygame.org/docs/ref/event.html
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getasynckeystate
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from .runtime_clock import monotonic_now

POLL_INTERVAL_S = 1 / 30
MAX_POLL_GAP_S = 0.5
DEFAULT_BINDING = {"kind": "keyboard", "key": "F9"}
_KEYS = {f"F{number}": 0x70 + number - 1 for number in range(8, 13)}
_SDL_OWNER = threading.Lock()
_ERROR_CODES = frozenset({
    "INPUT_KEYBOARD_UNAVAILABLE", "INPUT_SDL_IN_USE", "INPUT_EVENT_OVERFLOW",
    "INPUT_DEVICE_LIMIT", "INPUT_DEVICE_CHANGED", "INPUT_DEVICE_INVALID", "INPUT_POLL_FAILED",
})


class _InputError(ValueError):
    def __init__(self, code: str):
        super().__init__(code if code in _ERROR_CODES else "INPUT_POLL_FAILED")


def _binding(value: object) -> dict:
    if type(value) is not dict:
        raise ValueError("INPUT_BINDING_INVALID")
    if (
        value.get("kind") == "keyboard" and set(value) == {"kind", "key"}
        and type(value["key"]) is str and value["key"] in _KEYS
    ):
        return value.copy()
    if value.get("kind") == "joystick" and set(value) == {"kind", "guid", "name", "button"}:
        guid, name, button = value["guid"], value["name"], value["button"]
        if (
            type(guid) is str and re.fullmatch(r"[0-9a-fA-F]{32}", guid)
            and type(name) is str and 1 <= len(name) <= 256
            and not any(ord(char) < 32 or 0x7F <= ord(char) < 0xA0 for char in name)
            and not any(0xD800 <= ord(char) <= 0xDFFF for char in name)
            and type(button) is int and 0 <= button < 256
        ):
            return {"kind": "joystick", "guid": guid.lower(), "name": name, "button": button}
    raise ValueError("INPUT_BINDING_INVALID")


def _make_key_reader() -> Callable[[int], bool]:
    if os.name != "nt":
        raise _InputError("INPUT_KEYBOARD_UNAVAILABLE")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    read = user32.GetAsyncKeyState
    read.argtypes = (ctypes.c_int,)
    read.restype = ctypes.c_short

    def current(vk: int) -> bool:
        # The low "pressed since last query" bit is explicitly unreliable.
        return bool(read(vk) & 0x8000)

    return current


def _make_pygame():
    import pygame

    return pygame


@dataclass(frozen=True)
class _Device:
    guid: str
    name: str
    buttons: dict[int, bool]


class _JoystickReader:
    """Worker-owned SDL resources; device indices never become stored bindings."""

    def __init__(self):
        self._pygame = None
        self._handles = {}
        self._display_owned = self._joystick_owned = False
        self._environment = {}
        self._owned = _SDL_OWNER.acquire(blocking=False)
        if not self._owned:
            raise _InputError("INPUT_SDL_IN_USE")
        try:
            for key in ("PYGAME_HIDE_SUPPORT_PROMPT", "SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"):
                self._environment[key] = os.environ.get(key)
                os.environ[key] = "1"
            pygame = self._pygame = _make_pygame()
            if pygame.display.get_init() or pygame.joystick.get_init():
                raise _InputError("INPUT_SDL_IN_USE")
            pygame.display.init()
            self._display_owned = True
            pygame.display.set_mode((1, 1), flags=pygame.HIDDEN)
            pygame.joystick.init()
            self._joystick_owned = True
            pygame.event.set_blocked(None)
            pygame.event.set_allowed([
                pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED,
                pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP,
            ])
        except Exception:
            self.close()
            raise

    def read(self, *, all_buttons: bool, binding: dict) -> dict[int, _Device]:
        pygame = self._pygame
        if len(pygame.event.get()) > 2048:
            raise _InputError("INPUT_EVENT_OVERFLOW")
        count = pygame.joystick.get_count()
        if type(count) is not int or not 0 <= count <= 32:
            raise _InputError("INPUT_DEVICE_LIMIT")
        devices, handles = {}, {}
        for index in range(count):
            joy = pygame.joystick.Joystick(index)
            instance = joy.get_instance_id()
            if type(instance) is not int or instance < 0 or instance in devices:
                raise _InputError("INPUT_DEVICE_CHANGED")
            guid, name = joy.get_guid(), joy.get_name()
            try:
                identity = _binding({"kind": "joystick", "guid": guid, "name": name, "button": 0})
            except ValueError:
                raise _InputError("INPUT_DEVICE_INVALID") from None
            button_count = joy.get_numbuttons()
            if type(button_count) is not int or not 0 <= button_count <= 256:
                raise _InputError("INPUT_DEVICE_INVALID")
            selected = range(button_count) if all_buttons else ()
            if not all_buttons and (identity["guid"], name) == (
                binding.get("guid"), binding.get("name"),
            ):
                selected = (binding["button"],) if binding["button"] < button_count else ()
            states = {}
            for button in selected:
                value = joy.get_button(button)
                if type(value) not in (bool, int) or value not in (0, 1):
                    raise _InputError("INPUT_DEVICE_INVALID")
                states[button] = bool(value)
            devices[instance] = _Device(identity["guid"], name, states)
            handles[instance] = joy
        if pygame.joystick.get_count() != count:
            raise _InputError("INPUT_DEVICE_CHANGED")
        for instance, joy in self._handles.items():
            if instance not in handles:
                with suppress(Exception):
                    joy.quit()
        self._handles = handles
        return devices

    def close(self) -> None:
        for joy in self._handles.values():
            with suppress(Exception):
                joy.quit()
        self._handles.clear()
        if self._joystick_owned:
            with suppress(Exception):
                self._pygame.joystick.quit()
            self._joystick_owned = False
        if self._display_owned:
            with suppress(Exception):
                self._pygame.display.quit()
            self._display_owned = False
        for key, previous in self._environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self._environment.clear()
        if self._owned:
            self._owned = False
            _SDL_OWNER.release()


class InputPoller:
    """One low-rate worker, private snapshots and fail-closed device selection.

    A fresh release/press is required after startup, rebind, reconnect or a poll
    gap. Switching to a keyboard is explicit; a missing wheel never falls back.
    ``close`` waits at most one second; status stays STOPPING if a callback blocks.
    """

    def __init__(
        self, on_press: Callable, on_release: Callable,
        on_binding: Callable[[dict], None], on_error: Callable[[str], None],
    ):
        if not all(callable(callback) for callback in (on_press, on_release, on_binding, on_error)):
            raise ValueError("INPUT_CALLBACK_INVALID")
        self._on_press, self._on_release = on_press, on_release
        self._on_binding, self._on_error = on_binding, on_error
        self._lock = threading.Lock()
        self._binding = DEFAULT_BINDING.copy()
        self._binding_active = False
        self._status = "STOPPED"
        self._revision = 0
        self._seen_revision = -1
        self._stop = threading.Event()
        self._closed = False
        self._thread = None
        self._key_reader = self._joystick_reader = None
        self._last_down = self._target_instance = self._last_poll_at = None
        self._buttons = {}
        self._pressed = False
        self._last_error = None

    def start(self, binding: dict | None = None) -> None:
        selected = _binding(DEFAULT_BINDING if binding is None else binding)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("INPUT_ALREADY_STARTED_OR_STOPPING")
            self._closed = False
            self._stop = threading.Event()
            self._binding_active = False
            self._seen_revision = -1
            self._key_reader = self._joystick_reader = None
            self._last_down = self._target_instance = self._last_poll_at = None
            self._buttons = {}
            self._pressed = False
            self._last_error = None
            self._binding = selected
            self._revision += 1
            self._status = "STARTING"
            self._thread = threading.Thread(target=self._run, name="voice-input", daemon=True)
            self._thread.start()

    def set_binding(self, binding: dict) -> None:
        selected = _binding(binding)
        with self._lock:
            self._binding, self._binding_active = selected, False
            self._revision += 1

    def begin_bind(self) -> None:
        with self._lock:
            if self._closed or self._thread is None or self._stop.is_set():
                raise ValueError("INPUT_NOT_RUNNING")
            self._binding_active = True
            self._revision += 1

    def cancel_bind(self) -> None:
        with self._lock:
            if self._binding_active:
                self._binding_active = False
                self._revision += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"binding": self._binding.copy(), "binding_active": self._binding_active,
                    "status": self._status}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._binding_active = False
            self._stop.set()
            thread = self._thread
            self._status = "STOPPING" if thread is not None and thread.is_alive() else "CLOSED"
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _status_is(self, status: str) -> None:
        with self._lock:
            if not self._closed:
                self._status = status

    def _callback(self, callback: Callable, *args) -> None:
        try:
            callback(*args)
        except Exception:
            self._stop.set()
            self._status_is("ERROR")
            if callback is not self._on_error:
                with suppress(Exception):
                    self._on_error("INPUT_CALLBACK_FAILED")

    def _cancel(self, code: str) -> None:
        if self._pressed:
            self._callback(self._on_error, code)
            self._pressed = False
            self._callback(self._on_release)
        self._last_down = self._target_instance = None
        self._buttons.clear()

    def _fault(self, code: str, status: str = "ERROR") -> None:
        if self._last_error != code:
            self._callback(self._on_error, code)
            self._last_error = code
        if self._pressed:
            self._pressed = False
            self._callback(self._on_release)
        self._last_down = self._target_instance = None
        self._buttons.clear()
        self._status_is(status)

    def _edge(self, down: bool) -> None:
        previous, self._last_down = self._last_down, down
        self._last_error = None
        if self._stop.is_set():
            return
        if previous is False and down:
            self._pressed = True
            self._callback(self._on_press)
        elif not down and self._pressed:
            self._pressed = False
            self._callback(self._on_release)
        if not self._stop.is_set():
            self._status_is("PRESSED" if self._pressed else "READY")

    def _current(self, revision: int) -> bool:
        with self._lock:
            return not self._closed and revision == self._revision and not self._stop.is_set()

    def _poll_once(self, now: float) -> None:
        with self._lock:
            binding, binding_active, revision = (
                self._binding.copy(), self._binding_active, self._revision,
            )
        if self._seen_revision != revision:
            self._cancel("INPUT_BINDING_CHANGED")
            self._seen_revision = revision
        if self._last_poll_at is not None and not 0 <= now - self._last_poll_at <= MAX_POLL_GAP_S:
            self._fault("INPUT_POLL_GAP")
        self._last_poll_at = now
        if self._stop.is_set():
            return
        if binding["kind"] == "keyboard" and not binding_active:
            if self._joystick_reader is not None:
                self._joystick_reader.close()
                self._joystick_reader = None
            if self._key_reader is None:
                self._key_reader = _make_key_reader()
            down = self._key_reader(_KEYS[binding["key"]])
            if type(down) is not bool:
                raise _InputError("INPUT_KEYBOARD_UNAVAILABLE")
            if not self._current(revision):
                self._cancel("INPUT_BINDING_CHANGED")
                return
            self._edge(down)
            return
        if self._joystick_reader is None:
            self._joystick_reader = _JoystickReader()
        devices = self._joystick_reader.read(all_buttons=binding_active, binding=binding)
        if not self._current(revision):
            self._cancel("INPUT_BINDING_CHANGED")
            return
        counts = Counter((device.guid, device.name) for device in devices.values())
        if binding_active:
            self._bind_edge(devices, counts, revision)
            return
        candidates = [(instance, device) for instance, device in devices.items()
                      if (device.guid, device.name) == (binding["guid"], binding["name"])]
        if len(candidates) != 1:
            self._fault("INPUT_DEVICE_AMBIGUOUS" if candidates else "INPUT_DEVICE_MISSING",
                        "WAIT_DEVICE")
            return
        instance, device = candidates[0]
        if binding["button"] not in device.buttons:
            self._fault("INPUT_BUTTON_UNAVAILABLE", "WAIT_DEVICE")
            return
        if self._target_instance != instance:
            self._cancel("INPUT_DEVICE_CHANGED")
            self._target_instance = instance
        self._edge(device.buttons[binding["button"]])

    def _bind_edge(self, devices: dict, counts: Counter, revision: int) -> None:
        states = {(instance, button): down for instance, device in devices.items()
                  for button, down in device.buttons.items()}
        candidates = [
            key for key, down in states.items() if down and self._buttons.get(key) is False
        ]
        self._buttons = states
        self._status_is("BINDING")
        if not candidates:
            return
        instance, button = candidates[0]
        device = devices[instance]
        if len(candidates) != 1 or counts[(device.guid, device.name)] != 1:
            # Preserve current baselines, so held ambiguous buttons cannot bind later.
            self._callback(self._on_error, "INPUT_BIND_AMBIGUOUS")
            return
        selected = {"kind": "joystick", "guid": device.guid, "name": device.name, "button": button}
        with self._lock:
            if self._closed or revision != self._revision or not self._binding_active:
                return
            self._binding, self._binding_active = selected, False
            self._revision += 1
        self._callback(self._on_binding, selected.copy())
        # The next normal poll establishes a held baseline; binding is never a PTT press.

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self._poll_once(monotonic_now())
                except _InputError as exc:
                    self._fault(str(exc))
                except Exception:
                    self._fault("INPUT_POLL_FAILED")
                self._stop.wait(POLL_INTERVAL_S)
        finally:
            self._cancel("INPUT_CLOSED")
            if self._joystick_reader is not None:
                self._joystick_reader.close()
                self._joystick_reader = None
            with self._lock:
                self._binding_active = False
                self._status = "CLOSED" if self._closed else "ERROR"


__all__ = ["DEFAULT_BINDING", "InputPoller"]
