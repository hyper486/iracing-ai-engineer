"""Anonymous invented SDK bytes and diagnostic invariants, never speed gates."""

import runpy
from pathlib import Path

import pytest

from iracing_ai_engineer.sdk_probe import WindowsPyirsdkTransport
from iracing_ai_engineer.synthetic_sdk import synthetic_sdk

BENCHMARK = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "benchmark_sdk_reader.py")
)


def test_fixture_uses_only_anonymous_owned_memory_and_real_getters(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Synthetic fixture must never construct/start a live transport")

    monkeypatch.setattr(WindowsPyirsdkTransport, "__init__", forbidden)
    monkeypatch.setattr(WindowsPyirsdkTransport, "startup", forbidden)
    with synthetic_sdk(fields=12, arrays=6) as (transport, descriptors):
        memory = transport._client._shared_mem
        assert transport._client._data_valid_event is None
        assert not transport.connected
        frame = transport.read_frozen(tuple(row.name for row in descriptors))
        assert not frame.read_errors and frame.buffer_tick == 100
        assert frame.values["SyntheticField0000"] == [bytes([128 + i]) for i in range(64)]
        assert frame.values["SyntheticField0001"] == [i % 2 == 0 for i in range(64)]
        assert frame.values["SyntheticField0002"] == [-2 - i for i in range(64)]
        assert frame.values["SyntheticField0003"] == [2**31 + 3 + i for i in range(64)]
        assert frame.values["SyntheticField0004"] == [(4 - i) / 16 for i in range(64)]
        assert frame.values["SyntheticField0005"] == [(5 - i) / 16 for i in range(64)]
        assert [frame.values[f"SyntheticField{i:04d}"] for i in range(6, 12)] == [
            b"\x80", True, -8, 2**31 + 9, 10 / 16, 11 / 16,
        ]
    assert memory.closed


def test_fixture_releases_its_mapping_on_error():
    with pytest.raises(RuntimeError, match="invented failure"):
        with synthetic_sdk() as (transport, _):
            memory = transport._client._shared_mem
            raise RuntimeError("invented failure")
    assert memory.closed


@pytest.mark.parametrize("kwargs", [
    {"fields": 5}, {"fields": 4097}, {"fields": True}, {"arrays": -1},
    {"arrays": 336}, {"arrays": False}, {"include_chars": 1},
])
def test_fixture_arguments_are_bounded(kwargs):
    with pytest.raises(ValueError, match="SYNTHETIC_SDK_ARGUMENT"):
        with synthetic_sdk(**kwargs):
            pytest.fail("invalid fixture admitted")


def test_cpu_benchmark_is_deterministic_and_discloses_exclusions():
    result = BENCHMARK["benchmark"](3)
    assert result["evidence_kind"] == "SYNTHETIC_CPU_AND_TIMER_ONLY"
    for key in ("sdk_accessed", "live_acceptance", "event_wait_measured",
                "durable_recording_measured", "char_fields_included"):
        assert result[key] is False
    assert (result["fields"], result["arrays"], result["array_size"]) == (335, 20, 64)
    assert result["values_sha256"] == (
        "6ba84fbea36daa75b5de744729d1a4cccc4b490712e1fbbb132be7209675939e"
    )
    assert set(result["timing"]) == {"read_frozen", "metadata", "queue_accounting"}
    assert result["poll_timer"] == {
        "samples_each": 0, "target_period_ms": 10, "measured": False, "timing": {},
    }


def test_optional_timer_report_has_no_platform_dependent_speed_assertion():
    result = BENCHMARK["benchmark"](1, wait_samples=1)
    assert result["poll_timer"]["measured"] is True
    assert result["poll_timer"]["samples_each"] == 1
    assert set(result["poll_timer"]["timing"]) == {"event_wait", "reader_sleep"}
    assert result["event_wait_measured"] is False  # The SDK event was never opened.


@pytest.mark.parametrize("iterations,wait_samples", [
    (0, 0), (10001, 0), (True, 0), (1.5, 0), (1, -1), (1, 301), (1, True), (1, .5),
])
def test_benchmark_rejects_invalid_arguments(iterations, wait_samples):
    with pytest.raises(ValueError, match="SYNTHETIC_SDK_ARGUMENT"):
        BENCHMARK["benchmark"](iterations, wait_samples=wait_samples)
