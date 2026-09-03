"""Compatibility wrapper for :mod:`iracing_ai_engineer.pit_plan`.

The implementation lives in the installable package.  Re-exporting every
non-dunder name keeps existing dynamic script loaders, including private
validator users, source-compatible during the package migration.
"""

from __future__ import annotations

from iracing_ai_engineer import pit_plan as _implementation

globals().update(
    {
        name: value
        for name, value in vars(_implementation).items()
        if not name.startswith("__")
    }
)


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
