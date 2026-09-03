"""Shared fail-closed capability records for unavailable inferred quantities."""

from __future__ import annotations

from collections.abc import Sequence

INFERENCE_CAPABILITY_CONTRACT_VERSION = "inference-capability-v1"


def unavailable_inference_capability(
    *,
    reasons: Sequence[str],
    blocked_claims: Sequence[str],
) -> dict[str, object]:
    """Describe an inference that is unavailable without fabricating an estimate."""

    normalized_reasons = _unique_codes(reasons, "reasons")
    normalized_claims = _unique_codes(blocked_claims, "blocked_claims")
    if not normalized_reasons or not normalized_claims:
        raise ValueError("unavailable inference must name reasons and blocked claims")
    return {
        "blocked_claims": list(normalized_claims),
        "confidence": "NONE",
        "contract_version": INFERENCE_CAPABILITY_CONTRACT_VERSION,
        "estimate_available": False,
        "provenance": "UNKNOWN",
        "reasons": list(normalized_reasons),
        "status": "SKIP",
    }


def _unique_codes(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{label} must be a sequence of codes")
    normalized = tuple(values)
    if (
        any(
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > 128
            or any(ord(character) < 32 for character in value)
            for value in normalized
        )
        or len(normalized) != len(set(normalized))
    ):
        raise ValueError(f"{label} must contain unique non-empty plain strings")
    return normalized
