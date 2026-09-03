from __future__ import annotations

import pytest

from iracing_ai_engineer.capabilities import unavailable_inference_capability


def test_unavailable_inference_is_explicitly_unknown_and_blocks_named_claims():
    capability = unavailable_inference_capability(
        reasons=("MODEL_NOT_IMPLEMENTED",),
        blocked_claims=("UNSUPPORTED_CLAIM",),
    )

    assert capability == {
        "blocked_claims": ["UNSUPPORTED_CLAIM"],
        "confidence": "NONE",
        "contract_version": "inference-capability-v1",
        "estimate_available": False,
        "provenance": "UNKNOWN",
        "reasons": ["MODEL_NOT_IMPLEMENTED"],
        "status": "SKIP",
    }


@pytest.mark.parametrize("field", ["reasons", "blocked_claims"])
def test_unavailable_inference_rejects_empty_evidence(field: str):
    arguments = {
        "reasons": ("MODEL_NOT_IMPLEMENTED",),
        "blocked_claims": ("UNSUPPORTED_CLAIM",),
    }
    arguments[field] = ()

    with pytest.raises(ValueError, match="must name"):
        unavailable_inference_capability(**arguments)
