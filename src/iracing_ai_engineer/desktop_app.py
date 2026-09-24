"""Native Tk entry point and frozen-binary self-test. No browser/web server."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import time
from contextlib import suppress
from pathlib import Path

_MUTEX_NAME = "Local\\AEIS-Engineer-Native-v1"


class _SingleInstance:
    """Per-login-session mutex; no system service, admin rights or autostart."""

    def __init__(self):
        self._handle = None
        self._kernel = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            raise OSError("DESKTOP_INSTANCE_LOCK_FAILED")
        self._kernel, self._handle = kernel, handle
        if ctypes.get_last_error() == 183:
            self.close()
            return False
        return True

    def close(self) -> None:
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


class _SelfTestController:
    """No SettingsStore, filesystem credentials, SDK startup or cloud client."""

    def __init__(self):
        from .live_app import AppState
        from .llm_engineer import EngineerService

        self.state = AppState()
        self.service = EngineerService(self.state.snapshot, environ={})
        self.closed = False

    def snapshot(self):
        telemetry = self.state.snapshot()
        telemetry["source_mode"] = "SYNTHETIC_DEMO"
        return {
            "telemetry": telemetry, "engineer": self.service.snapshot(),
            "lifecycle": "RUNNING", "notice": "合成桌面自检；未访问游戏或云端。",
            "settings": {"provider": "off", "model": "deepseek-flash",
                         "recording_enabled": False, "remember_key": False,
                         "key_configured": False},
        }

    def submit(self, question, scope="live"):
        return self.service.submit(question, scope)

    def configure(self, **kwargs):
        raise ValueError("SELF_TEST_ONLY")

    def load_session(self, path):
        raise ValueError("SELF_TEST_ONLY")

    def set_recording(self, enabled):
        raise ValueError("SELF_TEST_ONLY")

    def close(self):
        self.service.close()
        self.closed = True

    def is_closed(self):
        return self.closed


def run_self_test() -> dict:
    """Exercise packaged Tk and the numerical pipeline with invented inputs."""
    import tkinter as tk

    from .desktop_window import DesktopWindow
    from .synthetic_runtime import (
        run_synthetic_capture_replay,
        run_synthetic_pit_observation,
        run_synthetic_runtime,
        run_synthetic_tire_capture_replay,
        run_synthetic_tire_confirmation,
    )

    checks = []
    callback_errors = []
    root = controller = None
    try:
        numerical = run_synthetic_runtime()
        checks.extend(numerical["checks"])
        if numerical["status"] != "PASS":
            raise ValueError("SYNTHETIC_NUMERICAL_FAILED")
        checks.append(run_synthetic_pit_observation())
        if checks[-1]["status"] != "PASS":
            raise ValueError("SYNTHETIC_PIT_OBSERVATION_FAILED")
        checks.append(run_synthetic_tire_confirmation())
        if checks[-1]["status"] != "PASS":
            raise ValueError("SYNTHETIC_TIRE_CONFIRMATION_FAILED")
        checks.append(run_synthetic_tire_capture_replay())
        if checks[-1]["status"] != "PASS":
            raise ValueError("SYNTHETIC_TIRE_CAPTURE_REPLAY_FAILED")
        checks.append(run_synthetic_capture_replay())
        if checks[-1]["status"] != "PASS":
            raise ValueError("SYNTHETIC_CAPTURE_REPLAY_FAILED")
        import irsdk

        if not hasattr(irsdk, "IRSDK"):
            raise ValueError("SDK_MODULE_MISSING")
        checks.append({"id": "SDK_MODULE_IMPORT_ONLY", "status": "PASS"})
        controller = _SelfTestController()
        root = tk.Tk()
        root.report_callback_exception = lambda *_args: callback_errors.append("TK_CALLBACK_FAILED")
        root.withdraw()
        DesktopWindow(root, controller)
        root.update_idletasks()
        root.update()
        checks.append({"id": "NATIVE_TK_WINDOW", "status": "PASS"})
        if controller.submit("当前油量证据够吗？")[0] != 202:
            raise ValueError("LOCAL_SUBMIT_FAILED")
        deadline = time.monotonic() + 3
        while controller.snapshot()["engineer"]["answer"] is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("SELF_TEST_ANSWER_TIMEOUT")
            root.update()
            time.sleep(0.01)
        value = controller.snapshot()["engineer"]
        if value["requests_used"] != 0 or value["answer"]["origin"] != "local_fallback":
            raise ValueError("LOCAL_ANSWER_FAILED")
        render_deadline = time.monotonic() + 0.3
        while time.monotonic() < render_deadline:
            root.update()
            time.sleep(0.01)
        if callback_errors:
            raise ValueError("NATIVE_RENDER_FAILED")
        checks.append({"id": "LOCAL_ANSWER_NO_KEY", "status": "PASS"})
        # Exercise the actual window-X protocol, not a shortcut root.destroy().
        root.tk.call(root.protocol("WM_DELETE_WINDOW"))
        close_deadline = time.monotonic() + 3
        while root.winfo_exists():
            if time.monotonic() >= close_deadline:
                raise TimeoutError("WINDOW_CLOSE_TIMEOUT")
            root.update()
            if controller.is_closed():
                # The GUI polls close completion on the next scheduled tick.
                time.sleep(0.01)
            try:
                if not root.winfo_exists():
                    break
            except tk.TclError:
                break
        if not controller.is_closed():
            raise ValueError("WORKER_CLOSE_FAILED")
        if callback_errors:
            raise ValueError("NATIVE_CLOSE_CALLBACK_FAILED")
        checks.append({"id": "WORKER_CLEAN_CLOSE", "status": "PASS"})
        checks.append({"id": "WINDOW_CLOSE_PROTOCOL", "status": "PASS"})
    except Exception:
        checks.append({"id": "NATIVE_RUNTIME", "status": "FAIL"})
    finally:
        if controller is not None:
            controller.close()
        if root is not None:
            with suppress(tk.TclError):
                root.destroy()
    return {
        "contract_version": "engineer-desktop-self-test-v1",
        "status": "PASS" if checks and all(item["status"] == "PASS" for item in checks) else "FAIL",
        "source_kind": "SYNTHETIC", "native_gui": True,
        "sdk_accessed": False, "provider_called": False, "live_acceptance": False,
        "checks": checks,
    }


def _self_test_output(path: Path, result: dict) -> None:
    # The caller chooses a new explicit receipt, never a settings/capture file.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=True, allow_nan=False)
        handle.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Native advisor-only iRacing engineer")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--voice-self-test", action="store_true")
    parser.add_argument("--self-test-output", type=Path)
    parser.add_argument("--ui-smoke-seconds", type=float)
    args = parser.parse_args(argv)
    if args.self_test:
        result = run_self_test()
        if args.voice_self_test:
            from .voice_diagnostics import run_voice_self_test

            result["checks"].extend(run_voice_self_test())
            if any(item["status"] != "PASS" for item in result["checks"]):
                result["status"] = "FAIL"
        if args.self_test_output is not None:
            try:
                _self_test_output(args.self_test_output, result)
            except OSError:
                return 2
        return 0 if result["status"] == "PASS" else 1
    if args.self_test_output is not None or args.voice_self_test:
        return 2
    if args.ui_smoke_seconds is not None and not 0.1 <= args.ui_smoke_seconds <= 120:
        return 2

    import tkinter as tk
    from tkinter import messagebox

    from .desktop_controller import DesktopController
    from .desktop_window import DesktopWindow

    mutex, root, controller = _SingleInstance(), None, None
    try:
        if not mutex.acquire():
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(
                "AEIS Engineer", "工程师窗口已经运行，请切换到已有窗口。", parent=root,
            )
            return 0
        root = tk.Tk()

        def callback_error(*_args):
            messagebox.showerror(
                "AEIS Engineer", "界面操作未完成；请重试或重新打开应用。", parent=root,
            )

        root.report_callback_exception = callback_error
        controller = (
            _SelfTestController() if args.ui_smoke_seconds
            else DesktopController(voice_runtime=True)
        )
        DesktopWindow(root, controller)
        if args.ui_smoke_seconds:
            # Explicit QA mode is synthetic, no SDK/credentials/provider access.
            def finish_smoke():
                root.tk.call(root.protocol("WM_DELETE_WINDOW"))

            root.after(round(args.ui_smoke_seconds * 1000), finish_smoke)
        else:
            controller.start()
        root.mainloop()
        return 0
    except Exception:
        if root is not None:
            with suppress(tk.TclError):
                messagebox.showerror(
                    "AEIS Engineer",
                    "应用未能启动。请检查运行环境或本机设置；不会显示私密错误内容。",
                    parent=root,
                )
        return 1
    finally:
        if controller is not None:
            controller.close()
            # Keep the instance lock until recorder/settings finalization really ends.
            # The normal window remains responsive in STOPPING while this happens.
            while not controller.is_closed():
                time.sleep(0.02)
        if root is not None:
            with suppress(tk.TclError):
                root.destroy()
        mutex.close()


if __name__ == "__main__":
    raise SystemExit(main())
