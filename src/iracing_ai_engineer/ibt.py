"""Safe, read-only adapter around pyirsdk's offline IBT reader."""

from __future__ import annotations

import hashlib
import json
import math
import mmap
import os
import struct
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

import irsdk
import numpy as np

from .contracts import READER_CONTRACT_VERSION

TYPE_NAMES = {
    0: "char",
    1: "bool",
    2: "int32",
    3: "uint32_or_bitfield",
    4: "float32",
    5: "float64",
}
TYPE_SIZES = {0: 1, 1: 1, 2: 4, 3: 4, 4: 4, 5: 8}
SUPPORTED_IBT_VERSIONS = {2}
SUPPORTED_PYIRSDK_VERSION = "1.3.6"


class IbtFormatError(RuntimeError):
    """Raised when an IBT file cannot satisfy the reader contract."""


@dataclass(frozen=True)
class VariableInfo:
    name: str
    type_code: int
    dtype: str
    offset: int
    count: int
    count_as_time: bool
    description: str
    unit: str


@dataclass(frozen=True)
class IbtMetadata:
    path: str
    file_size_bytes: int
    format_version: int
    status: int
    tick_rate_hz: int
    record_count: int
    declared_lap_count: int
    session_start_time_s: float
    session_end_time_s: float
    declared_duration_s: float
    variable_count: int
    record_size_bytes: int
    data_offset_bytes: int
    trailing_bytes: int
    schema_sha256: str
    field_names_sha256: str
    canonical_schema_sha256: str
    reader_contract_version: str = READER_CONTRACT_VERSION


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_mmap(shared_mem: Any, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    view = memoryview(shared_mem)
    try:
        for offset in range(0, len(view), chunk_size):
            digest.update(view[offset : offset + chunk_size])
    finally:
        view.release()
    return digest.hexdigest()


def _preflight_ibt_header(raw_header: bytes) -> None:
    if raw_header.startswith(b"YLPR"):
        raise IbtFormatError(
            "iRacing replay container (.rpy/YLPR) is not IBT telemetry; "
            "play it in iRacing and probe the live SDK instead"
        )
    if len(raw_header) != 48:
        raise IbtFormatError("truncated IBT header")
    values = struct.unpack("<12i", raw_header)
    pre_version = values[0]
    pre_tick_rate = values[2]
    pre_num_vars = values[6]
    pre_num_buf = values[8]
    pre_buf_len = values[9]
    if pre_version not in SUPPORTED_IBT_VERSIONS:
        raise IbtFormatError(f"unsupported IBT version: {pre_version}")
    if not 1 <= pre_tick_rate <= 360:
        raise IbtFormatError(f"invalid tick rate: {pre_tick_rate}")
    if not 1 <= pre_num_vars <= 4096:
        raise IbtFormatError(f"invalid variable count: {pre_num_vars}")
    if not 1 <= pre_num_buf <= 4:
        raise IbtFormatError(f"invalid buffer count: {pre_num_buf}")
    if pre_buf_len <= 0:
        raise IbtFormatError(f"invalid record size: {pre_buf_len}")


class IbtReader:
    """Validated context-manager for immutable `.ibt` telemetry.

    pyirsdk 1.3.6 exposes the offline payload but does not provide a context
    manager and has a broken bounds check in ``IBT.get``. This adapter owns
    closure and validates every record index before delegating.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self._reader: irsdk.IBT | None = None
        self._source_digest: str | None = None
        self._opened_file_size: int | None = None

    def __enter__(self) -> IbtReader:
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def open(self) -> None:
        if self._reader is not None:
            return
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        opened_file = self.path.open("rb")
        try:
            _preflight_ibt_header(opened_file.read(48))
            if version("pyirsdk") != SUPPORTED_PYIRSDK_VERSION:
                raise IbtFormatError(
                    f"unsupported pyirsdk version; expected {SUPPORTED_PYIRSDK_VERSION}"
                )
        except Exception:
            opened_file.close()
            raise

        reader = irsdk.IBT()
        try:
            opened_stat = os.fstat(opened_file.fileno())
            if opened_stat.st_size < 144:
                raise IbtFormatError("truncated IBT metadata")
            shared_mem = mmap.mmap(opened_file.fileno(), 0, access=mmap.ACCESS_READ)
            reader._ibt_file = opened_file
            reader._shared_mem = shared_mem
            reader._header = irsdk.Header(shared_mem)
            reader._disk_header = irsdk.DiskSubHeader(shared_mem, 112)
            self._validate_open_reader(reader)
            digest = _sha256_mmap(reader._shared_mem)
        except Exception:
            reader.close()
            if not opened_file.closed:
                opened_file.close()
            raise
        self._reader = reader
        self._source_digest = digest
        self._opened_file_size = opened_stat.st_size

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
            self._source_digest = None
            self._opened_file_size = None

    @property
    def _open_reader(self) -> irsdk.IBT:
        if self._reader is None:
            raise RuntimeError("IBT reader is not open; use it as a context manager")
        return self._reader

    @staticmethod
    def _validate_open_reader(reader: irsdk.IBT) -> None:
        header = reader._header
        disk = reader._disk_header
        if header is None or disk is None:
            raise IbtFormatError("missing IBT header")
        if header.version not in SUPPORTED_IBT_VERSIONS:
            raise IbtFormatError(f"unsupported IBT version: {header.version}")
        if not 1 <= header.tick_rate <= 360:
            raise IbtFormatError(f"invalid tick rate: {header.tick_rate}")
        if not 1 <= header.num_vars <= 4096:
            raise IbtFormatError(f"invalid variable count: {header.num_vars}")
        if not 1 <= header.num_buf <= 4:
            raise IbtFormatError(f"invalid buffer count: {header.num_buf}")
        if disk.session_record_count < 2:
            raise IbtFormatError(f"invalid record count: {disk.session_record_count}")
        if (
            not math.isfinite(disk.session_start_time)
            or not math.isfinite(disk.session_end_time)
            or disk.session_end_time < disk.session_start_time
        ):
            raise IbtFormatError("invalid disk session time range")
        if header.buf_len <= 0 or not header.var_buf:
            raise IbtFormatError("invalid telemetry record layout")

        file_size = os.fstat(reader._ibt_file.fileno()).st_size
        variable_header_end = header.var_header_offset + header.num_vars * 144
        if header.var_header_offset < 0 or variable_header_end > file_size:
            raise IbtFormatError("variable header table extends outside the IBT file")
        session_info_end = header.session_info_offset + header.session_info_len
        if (
            header.session_info_offset < 0
            or header.session_info_len < 0
            or session_info_end > file_size
        ):
            raise IbtFormatError("SessionInfo extends outside the IBT file")
        data_offset = header.var_buf[0].buf_offset
        if not (
            variable_header_end
            <= header.session_info_offset
            <= session_info_end
            <= data_offset
        ):
            raise IbtFormatError("IBT sections overlap or are out of canonical order")
        expected_end = (
            data_offset + disk.session_record_count * header.buf_len
        )
        if data_offset <= 0 or expected_end > file_size:
            raise IbtFormatError(
                f"truncated IBT: records end at {expected_end}, file has {file_size} bytes"
            )

        variable_headers = reader._var_headers
        names = [item.name for item in variable_headers]
        if len(names) != len(set(names)):
            raise IbtFormatError("duplicate variable names in IBT schema")
        for item in variable_headers:
            type_code = int(item.type)
            count = int(item.count)
            offset = int(item.offset)
            if type_code not in TYPE_SIZES:
                raise IbtFormatError(f"unsupported type code {type_code} for {item.name}")
            if count < 1 or offset < 0:
                raise IbtFormatError(f"invalid layout for {item.name}")
            if offset + count * TYPE_SIZES[type_code] > header.buf_len:
                raise IbtFormatError(f"channel {item.name} extends outside a record")

    @property
    def variables(self) -> tuple[VariableInfo, ...]:
        reader = self._open_reader
        return tuple(
            VariableInfo(
                name=header.name,
                type_code=int(header.type),
                dtype=TYPE_NAMES.get(int(header.type), f"unknown_{header.type}"),
                offset=int(header.offset),
                count=int(header.count),
                count_as_time=bool(header.count_as_time),
                description=header.desc,
                unit=header.unit,
            )
            for header in reader._var_headers
        )

    @property
    def variable_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.variables)

    @property
    def schema_sha256(self) -> str:
        schema = [
            {
                "name": item.name,
                "type": item.type_code,
                "offset": item.offset,
                "count": item.count,
                "count_as_time": item.count_as_time,
                "unit": item.unit,
            }
            for item in self.variables
        ]
        payload = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def field_names_sha256(self) -> str:
        payload = "\n".join(item.name for item in self.variables).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def source_sha256(self) -> str:
        _ = self._open_reader
        if self._source_digest is None:
            raise RuntimeError("source digest is unavailable")
        return self._source_digest

    def verify_source_unchanged(self) -> None:
        reader = self._open_reader
        if _sha256_mmap(reader._shared_mem) != self.source_sha256:
            raise IbtFormatError("IBT source changed while it was being read")

    @property
    def canonical_schema_sha256(self) -> str:
        canonical = {
            "reader_contract_version": READER_CONTRACT_VERSION,
            "tick_rate_hz": int(self._open_reader._header.tick_rate),
            "variables": [
                {
                    "count": item.count,
                    "count_as_time": item.count_as_time,
                    "dtype": item.dtype,
                    "name": item.name,
                    "type_code": item.type_code,
                    "unit": item.unit,
                }
                for item in sorted(self.variables, key=lambda value: value.name)
            ],
        }
        payload = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def metadata(self) -> IbtMetadata:
        reader = self._open_reader
        header = reader._header
        disk = reader._disk_header
        if self._opened_file_size is None:
            raise RuntimeError("opened file size is unavailable")
        file_size = self._opened_file_size
        expected_end = header.var_buf[0].buf_offset + disk.session_record_count * header.buf_len
        return IbtMetadata(
            path=str(self.path),
            file_size_bytes=file_size,
            format_version=int(header.version),
            status=int(header.status),
            tick_rate_hz=int(header.tick_rate),
            record_count=int(disk.session_record_count),
            declared_lap_count=int(disk.session_lap_count),
            session_start_time_s=float(disk.session_start_time),
            session_end_time_s=float(disk.session_end_time),
            declared_duration_s=float(disk.session_end_time - disk.session_start_time),
            variable_count=int(header.num_vars),
            record_size_bytes=int(header.buf_len),
            data_offset_bytes=int(header.var_buf[0].buf_offset),
            trailing_bytes=int(file_size - expected_end),
            schema_sha256=self.schema_sha256,
            field_names_sha256=self.field_names_sha256,
            canonical_schema_sha256=self.canonical_schema_sha256,
        )

    def get_record(self, index: int, names: list[str] | tuple[str, ...]) -> dict[str, Any]:
        reader = self._open_reader
        record_count = int(reader._disk_header.session_record_count)
        if not 0 <= index < record_count:
            raise IndexError(f"record index {index} outside [0, {record_count})")
        missing = sorted(set(names) - set(self.variable_names))
        if missing:
            raise KeyError(f"missing IBT channels: {', '.join(missing)}")
        return {name: reader.get(index, name) for name in names}

    def get_channel(self, name: str) -> np.ndarray:
        reader = self._open_reader
        if name not in self.variable_names:
            raise KeyError(f"missing IBT channel: {name}")
        return np.asarray(reader.get_all(name))

    def get_channels(self, names: list[str] | tuple[str, ...]) -> dict[str, np.ndarray]:
        missing = sorted(set(names) - set(self.variable_names))
        if missing:
            raise KeyError(f"missing IBT channels: {', '.join(missing)}")
        return {name: self.get_channel(name) for name in names}

    def public_session_context(self) -> dict[str, Any]:
        """Return a deliberately narrow, lazily parsed non-driver subset."""

        reader = self._open_reader
        metadata = irsdk.IRSDK()
        try:
            # Reuse the already validated mmap. Reopening by path could mix two
            # sources if the pathname is replaced while a replay is running.
            metadata._shared_mem = reader._shared_mem
            metadata._header = reader._header
            metadata.is_initialized = True
            weekend = metadata["WeekendInfo"] or {}
            session_info = metadata["SessionInfo"] or {}
            sessions = session_info.get("Sessions", []) or []
            return {
                "track_name": weekend.get("TrackName"),
                "track_display_name": weekend.get("TrackDisplayName"),
                "track_config_name": weekend.get("TrackConfigName"),
                "track_length": weekend.get("TrackLength"),
                "track_type": weekend.get("TrackType"),
                "event_type": weekend.get("EventType"),
                "category": weekend.get("Category"),
                "official": weekend.get("Official"),
                "sim_build": weekend.get("BuildVersion"),
                "track_version": weekend.get("TrackVersion"),
                "sessions": [
                    {
                        "session_num": item.get("SessionNum"),
                        "session_name": item.get("SessionName"),
                        "session_type": item.get("SessionType"),
                    }
                    for item in sessions
                ],
            }
        finally:
            # Do not call shutdown(): this helper does not own the mmap.
            metadata._shared_mem = None
            metadata._header = None
            metadata.is_initialized = False

    def metadata_dict(self) -> dict[str, Any]:
        return asdict(self.metadata)
