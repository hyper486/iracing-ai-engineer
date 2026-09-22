"""Opt-in private raw capture for the local app; never an HTTP file service.

Even with DriverInfo redaction, raw telemetry and SessionInfo remain private.
Each instance owns one exclusively created clip. Only an explicit successful
``finish`` writes COMPLETE; disconnect/error cleanup preserves an incomplete
prefix. Its returned receipt contains aggregate counts, never paths or identity.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from .collector import (
    CollectorConsistencyError,
    CollectorSample,
    JsonlHandleWriter,
    LiveCollector,
)
from .telemetry import SourceKind

DEFAULT_MAX_BYTES = 512 * 1024**2
_SAFE_RECEIPT_FIELDS = (
    "completion_status",
    "semantic_record_count",
    "run_record_count",
    "frame_record_count",
    "event_record_count",
    "schema_record_count",
    "session_info_record_count",
    "samples_seen",
    "duplicate_sample_count",
    "duplicate_conflict_count",
    "dropped_tick_count",
    "stale_event_count",
    "session_reset_count",
    "schema_change_count",
    "schema_epoch_count",
    "session_epoch_count",
)


def _public_checkout() -> Path | None:
    """Locate this module's checkout without relying on the caller's cwd."""

    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "AGENTS.md").is_file():
            return parent
    return None


def _private_directory(directory: Path) -> Path:
    resolved = directory.resolve()
    checkout = _public_checkout()
    if checkout is not None and resolved.is_relative_to(checkout):
        raise CollectorConsistencyError("RAW_RECORDING_DIRECTORY_IN_PUBLIC_CHECKOUT")
    return resolved


class AppRecorder:
    """Single-owner, bounded, durable recorder for one SDK_LIVE connection.

    Callers must close in a finally block. After any ingest/finalization error,
    the recorder is terminal: it must not retry, reopen or append COMPLETE.
    ``byte_count`` is the successfully committed byte count, not an estimate of
    any partial write left by an I/O failure. It is safe to show in local status.
    """

    def __init__(
        self,
        directory: Path,
        *,
        source_id: str,
        session_id: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive plain integer")
        directory = Path(directory)
        resolved = _private_directory(directory)
        resolved.mkdir(parents=True, exist_ok=True)
        # Re-resolve after mkdir too, including Windows junctions. Do not use
        # the original alias when creating the file.
        resolved = _private_directory(resolved)
        path = resolved / f"capture-{uuid4().hex}.jsonl"
        self._handle = path.open("x+b", buffering=0)
        self._closed = False
        self._failed = False
        self._finished = False
        self._samples_seen = 0
        self._receipt: dict[str, object] | None = None
        try:
            # Keep the exact builtin writer type so LiveCollector preserves
            # its validated single-encoding fast path and all handle checks.
            self._writer = JsonlHandleWriter(
                self._handle, fsync_each_record=True, max_output_bytes=max_bytes
            )
            self._writer.__enter__()
            self._collector = LiveCollector(
                self._writer,
                source_id=source_id,
                session_id=session_id,
                expected_source_kind=SourceKind.SDK_LIVE,
                include_driver_info=False,
            )
        except BaseException:
            self.close()
            raise

    @property
    def byte_count(self) -> int:
        return self._writer.byte_size

    def _require_active(self) -> None:
        if self._failed:
            raise CollectorConsistencyError("RAW_RECORDING_FAILED")
        if self._closed or self._finished:
            raise RuntimeError("raw recorder is already closed or finished")

    def ingest(self, sample: CollectorSample) -> None:
        self._require_active()
        try:
            self._collector.ingest(sample)
        except BaseException:
            self._failed = True
            raise
        self._samples_seen += 1

    def finish(self) -> dict[str, object] | None:
        if self._finished:
            return None if self._receipt is None else dict(self._receipt)
        self._require_active()
        try:
            if self._samples_seen:
                receipt = self._collector.finish().to_dict()
                self._writer.close()
                self._receipt = {key: receipt[key] for key in _SAFE_RECEIPT_FIELDS}
                self._receipt["record_count"] = int(receipt["semantic_record_count"]) + 1
                self._receipt["byte_count"] = self.byte_count
            self.close()
        except BaseException:
            self._failed = True
            self._receipt = None
            raise
        self._finished = True
        return None if self._receipt is None else dict(self._receipt)

    def close(self) -> None:
        """Close only the owned handle; never finalize an interrupted clip."""

        if not self._closed:
            self._closed = True
            self._handle.close()


__all__ = ["AppRecorder", "DEFAULT_MAX_BYTES"]
