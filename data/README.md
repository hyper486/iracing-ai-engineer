# Frozen local telemetry

`raw/` contains read-only iRacing `.ibt` inputs from the configured Windows
sidecar and explicitly reviewed public sources. Raw telemetry is intentionally
gitignored because it is large and may contain account or driver metadata.

[`manifest.json`](manifest.json) is the reviewable provenance record. It stores
only car/track filenames, source timestamps, byte sizes, checksums, structural
metadata, and a provisional replay receipt. `DriverInfo` is never exported.

The lap/event result in the manifest is a **candidate regression fixture**, not
a human-approved golden. Pit, reset, and lap boundaries should be checked
against replay video before freezing them as domain truth.

[`public_sources.json`](public_sources.json) separately records the public Audi
R8 LMS EVO II GT3 × Spa sample because it has a different IBT schema from the
four sidecar recordings. The pinned Git LFS object passes replay, lap, fuel, and
driving smoke-test gates: 151,892 continuous frames, 17 complete laps, 15
fuel-eligible laps, and 7 clean driving laps. Personalized coaching remains
closed: `condition-cohort-v1` deterministically finds zero matched laps because
approved track-state labels and opponent arrays are missing, some cross-stint
tire context is unobserved, and the source has fewer than the default eight
matched laps. See the
[public sample receipt](../docs/AUDI_SPA_PUBLIC_IBT_RECEIPT.md).
The same manifest also freezes the compact two-process receipt for the
[one-command offline engineer demo](../docs/OFFLINE_DEMO.md); it remains a
candidate smoke receipt, not a human-approved coaching label or event-rules
profile.

To fetch the commit-pinned Audi/Spa asset and then verify it without network
access when it already exists:

```bash
uv run python scripts/fetch_public_ibt.py
uv run python scripts/fetch_public_ibt.py --verify-only
```

The downloader accepts only the manifest's direct `data/raw/*.ibt` target and
approved HTTPS host, never overwrites an existing path, verifies exactly
`162,304,117` bytes and SHA-256
`754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36`,
and marks a successful download read-only. The raw file may contain
`DriverInfo`; keep it local and do not redistribute it.

## Remote replay sources

[`replay_sources.json`](replay_sources.json) records large `.rpy` assets that remain on the
Windows sidecar. An iRacing replay is not admitted to the IBT reader: it is an unsupported,
version-dependent container that may expose only partial or incorrect SDK variables during native
playback. Its current disposition is therefore `REFERENCE_ONLY / REPLAY_SDK_PROBE_REQUIRED`.

The replay manifest contains integrity and storage metadata only. It deliberately omits driver and
team records that may be embedded in the container.
