"""Owned anonymous bytes for SDK-reader diagnostics, never a simulator mapping."""

from __future__ import annotations

import mmap
import struct
from contextlib import contextmanager

from .sdk_probe import WindowsPyirsdkTransport, _stable_sdk_layout


@contextmanager
def synthetic_sdk(*, fields=335, arrays=20, include_chars=True):
    """Real pinned pyirsdk headers/getters over explicitly invented anonymous RAM.

    No transport constructor/startup, named map, event, status HTTP, simulator,
    audio or provider is opened. The fixture owns and closes its anonymous map.
    """
    import irsdk

    if (type(fields) is not int or not 6 <= fields <= 4096 or type(arrays) is not int
            or not 0 <= arrays <= fields or type(include_chars) is not bool):
        raise ValueError("SYNTHETIC_SDK_ARGUMENT")
    variables, size = [], 0
    formats = ("c", "?", "i", "I", "f", "d")
    for index in range(fields):
        code = index % 6 if include_chars else 1 + index % 5
        count = 64 if index < arrays else 1
        values = tuple(
            bytes((128 + position % 128,)) if code == 0 else
            position % 2 == 0 if code == 1 else
            -index - position if code == 2 else
            2**31 + index + position if code == 3 else
            (index - position) / 16
            for position in range(count)
        )
        packed = struct.pack(f"<{count}{formats[code]}", *values)
        variables.append((f"SyntheticField{index:04d}", code, count, size, packed))
        size += len(packed)
    schema_offset = 128
    info = (b'WeekendInfo:\n SimMode: full\n TrackName: Synthetic fixture\n'
            b'SessionInfo:\n Sessions:\n - SessionNum: 0\n   SessionType: Practice\n')
    info_offset = schema_offset + fields * 144
    buffer_start = info_offset + len(info) + 64
    memory = mmap.mmap(-1, buffer_start + size * 3)
    transport = object.__new__(WindowsPyirsdkTransport)
    transport._irsdk = irsdk
    transport._client = irsdk.IRSDK(parse_yaml_async=False)
    try:
        struct.pack_into("<10i", memory, 0, 2, 1, 60, 1, len(info), info_offset,
                         fields, schema_offset, 3, size)
        memory[44] = 0
        memory[info_offset:info_offset + len(info)] = info
        for index, (name, code, count, offset, packed) in enumerate(variables):
            start = schema_offset + index * 144
            struct.pack_into("<3i?", memory, start, code, offset, count, False)
            encoded = name.encode("ascii")
            memory[start + 16:start + 16 + len(encoded)] = encoded
            for buffer in range(3):
                at = buffer_start + buffer * size + offset
                memory[at:at + len(packed)] = packed
        for index in range(3):
            struct.pack_into("<4i", memory, 48 + index * 16,
                             100 - index, buffer_start + index * size, 100 - index, 0)
        transport._client._shared_mem = memory
        transport._client._header = irsdk.Header(memory)
        transport._client.is_initialized = True
        transport._startup_layout = _stable_sdk_layout(memory)
        descriptors = transport.descriptors()
        yield transport, descriptors
    finally:
        transport.close()
        if not memory.closed:
            memory.close()
