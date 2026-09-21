# Crew Chief read-only extraction

This is an independently built, advisor-only Windows reader derived from the
Crew Chief iRacing SDK wrapper. It does not require Crew Chief to run and does
not load its UI, voices, settings, MQTT publisher, spotter or strategy engine.
It reads iRacing directly; it is not Crew Chief's subtitle shared-memory API.

## Pinned provenance

Upstream repository: <https://gitlab.com/mr_belowski/CrewChiefV4>

Commit: `150c8107ad03af621afec83712e96109cf2a3a93`

Extracted/adapted sources:

- [iRacingSDK.cs](https://gitlab.com/mr_belowski/CrewChiefV4/-/blob/150c8107ad03af621afec83712e96109cf2a3a93/CrewChiefV4/iRacing/iRSDKSharp/iRacingSDK.cs):
  descriptor offsets, `GetVarHeaders` table extraction, and `GetData` primitive
  scalar/array dispatch, now in `CrewChiefSdkReader.cs`.
- [CVarHeader.cs](https://gitlab.com/mr_belowski/CrewChiefV4/-/blob/150c8107ad03af621afec83712e96109cf2a3a93/CrewChiefV4/iRacing/iRSDKSharp/CVarHeader.cs):
  descriptor constructor, type ordering, properties and type-size logic, now in
  `CVarHeader.cs` with internal visibility and `count_as_time` added.
- [LICENSE](https://gitlab.com/mr_belowski/CrewChiefV4/-/blob/150c8107ad03af621afec83712e96109cf2a3a93/LICENSE):
  the MIT notice is preserved in `LICENSE.CrewChief` and must accompany copies.

`SnapshotReader.cs`, the NDJSON `Program.cs`, and synthetic tests are the local
integration/safety wrapper, not unmodified upstream files. Header/layout parsing
is independently implemented to match this repository's validated SDK v2 layout.
No upstream `CiRSDKHeader`, `CVarBuf`, `YamlParser`, `iRacingDiskSDK`, full
`iRacingData`, game-state mapper or application dependency is required. In
particular the upstream YAML parser's error logging and application control are
not incorporated; original SessionInfo bytes travel only through the private pipe.

## Deliberate changes

- Only `Local\IRSDKMemMapFileName` is accepted. Both the mapping and view are
  explicitly read-only. There is no arbitrary map/file argument, replay input,
  production test mode, simulator launch or control endpoint.
- Upstream broadcast, pit/chat/replay/recording controls, native message APIs,
  data-valid event handle management and code-generation/file writes are omitted.
- Decode from a copied complete frame, never repeated reads of a moving buffer.
  Preserve numeric SDK types instead of Crew Chief-specific enums. Bitfields
  decode as unsigned 32-bit values; scalar/array shape is preserved. SDK character
  values/descriptors use byte-preserving Latin-1 rather than the host code page.
  Float32 values are promoted exactly to double before JSON serialization to
  prevent `JavaScriptSerializer` from shortening a Single's significant digits.
- Unknown types, duplicate/empty names, invalid counts/ranges and overlapping
  SDK sections fail closed. Missing values are never replaced with zero. Nonfinite
  floating-point values or non-Boolean bytes produce a null field and its name in
  `read_errors`, including when an array contains one invalid element.
- Header version 2, 1-360 Hz declared rate, 1-4 buffers, 1-4096 descriptors,
  1-4096 elements per descriptor and a valid current-buffer index are required.
  Resource bounds: 64 MiB mapping; 4 MiB frame; 8 MiB SessionInfo;
  1,048,576 total descriptor elements; 16 MiB UTF-8 output line including CRLF.
  Output exceeding this bound becomes a fixed `inconsistent/output_limit` failure. These are
  defensive implementation caps, not claims about the maximum iRacing SDK size.
- Up to three snapshot attempts compare metadata, complete descriptor bytes,
  selected-buffer offset and begin/end tick markers around copying. SessionInfo
  is copied twice and accepted only with identical bytes and an unchanged update
  counter/layout. The newest complete buffer is selected, not an in-progress one.
  These checks detect observed races; they cannot defeat an ABA writer that
  violates the SDK's tick/update protocol. A later caller must still reject stale
  ticks and distinguish live driving from replay/menus.
- Per-buffer begin-tick markers at offset +8 follow this repository's
  pyirsdk 1.3.6/SDK-v2 consistency contract. Historical writers that leave this
  field as padding are not silently accepted by weakening the check.
- The mapped connection is reopened for every request, so disconnect/reconnect
  does not keep a stale mapping handle indefinitely. No output is retained here.

## Process protocol and privacy

Build using `scripts/build_crewchief_reader.ps1`; no NuGet package or Crew Chief
installation is needed. Source is C# 5/.NET Framework compatible. The production
entry point is `Aeis.CrewChiefReader.Program`; the only additional assembly
reference is `System.Web.Extensions.dll` for `JavaScriptSerializer`.

Send exactly `snapshot` followed by a newline to stdin. Receive one UTF-8 NDJSON
object on stdout. EOF exits and closes resources. No diagnostic log is emitted.
Malformed commands produce fixed `invalid_request`; command-line arguments are
rejected with `invalid_arguments` and exit status 2. Request lines are bounded.

Every response includes `protocol: "crewchief-readonly-v1"`. A success contains
`status: "ok"`, connection/layout metadata, `buffer_tick`, `session_info_update`,
the original complete `session_info_b64`, all variable `descriptors`, all decoded
`values`, and `read_errors`. Failures contain only `status` (`unavailable` or
`inconsistent`) and a fixed `error_code`, never exception text or private data.

**Successful responses contain private raw telemetry and full SessionInfo.**
Base64 is transport encoding, not anonymization. Only a trusted local consumer
may read this pipe; filtering and source attribution must happen before
persistence or publication. Never commit raw responses or treat synthetic test
results as authentic SDK live evidence. Extracting the reader does not unlock
data that iRacing withholds or refreshes only in the pits.

Synthetic tests compile a separate entry point,
`Aeis.CrewChiefReader.Tests.ReaderTests`, with the production source files plus
`tests/ReaderTests.cs`. They inject byte arrays through an internal interface;
the production executable has no switch that exposes this test path.
