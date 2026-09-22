"""Lightweight source/frozen entry point for the native Windows desktop app."""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    if not getattr(sys, "frozen", False):
        source_root = Path(__file__).resolve().parents[1] / "src"
        if not (source_root / "iracing_ai_engineer" / "desktop_app.py").is_file():
            raise RuntimeError("DESKTOP_SOURCE_UNAVAILABLE")
        sys.path.insert(0, str(source_root))

    # Keep the broad CLI (including report/export commands) out of this entry.
    from iracing_ai_engineer.desktop_app import main as desktop_main

    return desktop_main(argv)


if __name__ == "__main__":
    # PyInstaller diverts speech worker children before normal CLI/UI startup.
    from multiprocessing import freeze_support

    freeze_support()
    raise SystemExit(main())
