#!/usr/bin/env python3
"""Compatibility wrapper for the packaged M1 pit/service/stint builder."""

from iracing_ai_engineer.pit_stint import (
    PIT_STINT_CONTRACT_VERSION,
    PitStintReceiptError,
    _build_receipt_from_samples,
    build_pit_stint_receipt,
    canonical_sha256,
    main,
)

__all__ = [
    "PIT_STINT_CONTRACT_VERSION",
    "PitStintReceiptError",
    "_build_receipt_from_samples",
    "build_pit_stint_receipt",
    "canonical_sha256",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
