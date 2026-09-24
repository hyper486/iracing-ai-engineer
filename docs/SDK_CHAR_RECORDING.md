# Schema-bound SDK char recording

The pinned pyirsdk getter returns `char` values as bytes (a scalar or an array of
one-byte values). Previously, native queue accounting rejected them and the
collector's generic JSON guard rejected them independently. A full-schema capture
could therefore fail on a valid char field even though numerical analysis and
proximity did not need that field. This was reproduced with owned anonymous
synthetic SDK memory, **not** observed as a failure in a new real game session.

## Encoding and compatibility

Only raw values bound to a validated descriptor with `type_code=0` / `dtype=char`
receive this conversion:

| Raw representation | Required shape | Persisted JSON value |
|---|---|---|
| Immutable `bytes` | Exactly descriptor `count` bytes | Latin-1 byte-to-codepoint string |
| List/tuple containing raw bytes | Exactly `count` elements, each one plain one-byte `bytes` | Concatenated Latin-1 string |
| Already supported JSON value | Existing collector JSON rules | Unchanged |

Latin-1 is a reversible octet mapping here, **not** a text-language claim. All 256
values, embedded NULs and trailing NUL padding are preserved. For a newly encoded
raw-byte field, `saved_value.encode("latin-1")` recovers the original bytes.
No replacement decoding, trimming, `repr`, arbitrary object coercion or silent
dropping of fields is used. Original transport frames remain unmodified.

Existing JSON values are unchanged, including previously trimmed Crew Chief
strings; missing historical padding is not invented. The persisted contract stays
`live-collector-v2`: char strings were already representable in that contract.
A fixed invented char/duplicate/conflict capture was compared directly with
unmodified collector code from `5145c5e`; its 3,855 serialized bytes and receipt
digest remain identical. Existing numerical golden captures also remain covered.

Duplicate-frame digests now consume the exact prepared values used by the frame
record. Replaying a sealed file can therefore recompute duplicate/conflict evidence
without the original Python bytes objects. Schema-bound char counts share the
existing one-schema cache, whose key checks descriptor contents, not object identity.

## Retained safety boundaries

- Queue accounting accepts immutable bytes with explicit length/overhead bounds.
  It is a memory-size check, **not** permission to serialize bytes anywhere.
- Bytes in numeric/undeclared fields, nested unsupported values, non-redacted SessionInfo or
  direct generic writer input still fail. Mutable byte arrays and memory views
  remain rejected. Non-finite numerical values keep their existing rejection.
- Malformed raw-byte char shapes fail before any semantic record for that sample. A failed
  native recorder remains terminal and cannot append a false `COMPLETE` receipt.
  Independent proximity and numerical analysis continue when the unrelated
  recording lane fails.
- Raw char fields remain **private capture content**, potentially including
  identity. They are not added to live/LLM/voice allowlists or historical summary
  cards. DriverInfo redaction remains separate and is not a blanket raw-data
  anonymization guarantee.
- No simulator access/control, audio-device access, provider call, migration of
  existing files, threshold relaxation or live acceptance is implied.

## Verification

Tests cover every byte value, scalar/array/packed shapes, NUL retention, malformed
inputs, general bytes guards, changed descriptor cache keys, identical legacy
capture bytes and strict duplicate/conflict replay. A real pinned pyirsdk getter
over invented anonymous memory supplies all six SDK types to the actual private
recorder and historical replay owner. The real native reader is separately tested
with complete char recording and a failed recording lane while proximity continues.

The packaged capture-recomputation check now includes raw char values, verifies
their persisted octets and excludes their marker from the historical report. The
optional CPU diagnostic can exercise them without recording:

```powershell
uv run python scripts/benchmark_sdk_reader.py --iterations 1000 --include-chars
```

That diagnostic still excludes real SDK event waits, durable recording, hardware
hearing and VR load. Actual in-car collection and the full product acceptance
remain open.
