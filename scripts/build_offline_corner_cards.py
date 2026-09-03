"""Compatibility CLI wrapper for :mod:`iracing_ai_engineer.corner_cards`."""

from __future__ import annotations

from iracing_ai_engineer import corner_cards as _implementation

# Keep the historical script-as-module surface intact, including private
# verifier helpers used by the frozen report/test loaders.
for _symbol_name, _symbol_value in vars(_implementation).items():
    if not _symbol_name.startswith("__"):
        globals()[_symbol_name] = _symbol_value

del _symbol_name, _symbol_value


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
