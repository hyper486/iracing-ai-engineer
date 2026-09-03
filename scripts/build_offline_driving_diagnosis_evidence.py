"""Compatibility CLI wrapper for :mod:`iracing_ai_engineer.driving_diagnosis`."""

from __future__ import annotations

import importlib as importlib  # explicit legacy re-export

from iracing_ai_engineer import driving_diagnosis as _implementation

# Keep the historical script-as-module surface intact, including private
# verifier helpers used by frozen tests and sibling script loaders.
for _symbol_name, _symbol_value in vars(_implementation).items():
    if not _symbol_name.startswith("__"):
        globals()[_symbol_name] = _symbol_value

del _symbol_name, _symbol_value


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
