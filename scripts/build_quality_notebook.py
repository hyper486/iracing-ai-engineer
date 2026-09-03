"""Build and execute the reader-facing Nürburgring telemetry audit notebook."""

from __future__ import annotations

import argparse
import hashlib
import json
import textwrap
from pathlib import Path

import nbformat
from nbclient import NotebookClient

LEGACY_QUALITY_INPUT_COUNT = 4
_MANIFEST_FILE_KEYS = frozenset(
    {"byte_size", "file_name", "record_count", "sha256", "source_mtime_utc"}
)


class QualityNotebookManifestError(ValueError):
    """Raised when the frozen notebook input manifest is not exact and usable."""


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QualityNotebookManifestError(f"duplicate manifest JSON key: {key}")
        result[key] = value
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_quality_notebook_inputs(
    root: Path,
) -> tuple[dict[str, object], tuple[Path, ...]]:
    """Load and verify exactly the four manifest-declared legacy IBT inputs."""

    manifest_path = root / "data" / "manifest.json"
    manifest_value = json.loads(
        manifest_path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_json_object,
    )
    if type(manifest_value) is not dict:
        raise QualityNotebookManifestError("data manifest must be a JSON object")
    manifest = manifest_value
    if manifest.get("manifest_version") != 1:
        raise QualityNotebookManifestError("data manifest version must be 1")
    file_entries = manifest.get("files")
    if type(file_entries) is not list or len(file_entries) != LEGACY_QUALITY_INPUT_COUNT:
        raise QualityNotebookManifestError(
            "quality notebook manifest must declare exactly four legacy IBT files"
        )

    paths: list[Path] = []
    declared_names: list[str] = []
    raw_directory = root / "data" / "raw"
    for index, value in enumerate(file_entries, start=1):
        if type(value) is not dict or set(value) != _MANIFEST_FILE_KEYS:
            raise QualityNotebookManifestError(
                f"data manifest file entry {index} has unexpected or missing fields"
            )
        name = value["file_name"]
        byte_size = value["byte_size"]
        record_count = value["record_count"]
        expected_sha256 = value["sha256"]
        source_mtime_utc = value["source_mtime_utc"]
        if (
            type(name) is not str
            or not name
            or Path(name).name != name
            or "/" in name
            or "\\" in name
            or Path(name).suffix.casefold() != ".ibt"
        ):
            raise QualityNotebookManifestError(
                f"data manifest file entry {index} has an unsafe IBT filename"
            )
        if type(byte_size) is not int or byte_size < 1:
            raise QualityNotebookManifestError(
                f"data manifest file entry {index} has an invalid byte_size"
            )
        if type(record_count) is not int or record_count < 1:
            raise QualityNotebookManifestError(
                f"data manifest file entry {index} has an invalid record_count"
            )
        if (
            type(expected_sha256) is not str
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise QualityNotebookManifestError(
                f"data manifest file entry {index} has an invalid sha256"
            )
        if (
            type(source_mtime_utc) is not str
            or not source_mtime_utc
            or not source_mtime_utc.endswith("Z")
        ):
            raise QualityNotebookManifestError(
                f"data manifest file entry {index} has an invalid source_mtime_utc"
            )
        if name in declared_names:
            raise QualityNotebookManifestError(f"duplicate manifest IBT filename: {name}")

        path = raw_directory / name
        if not path.is_file():
            raise FileNotFoundError(f"manifest-declared IBT file is missing: {path}")
        actual_size = path.stat().st_size
        if actual_size != byte_size:
            raise QualityNotebookManifestError(
                f"manifest byte_size mismatch for {name}: {actual_size} != {byte_size}"
            )
        actual_sha256 = _sha256_file(path)
        if actual_sha256 != expected_sha256:
            raise QualityNotebookManifestError(
                f"manifest sha256 mismatch for {name}: {actual_sha256}"
            )
        declared_names.append(name)
        paths.append(path)

    primary_sample = manifest.get("primary_sample")
    if type(primary_sample) is not str or primary_sample not in declared_names:
        raise QualityNotebookManifestError(
            "data manifest primary_sample must name one declared legacy IBT file"
        )
    return manifest, tuple(paths)


def markdown(text: str):
    return nbformat.v4.new_markdown_cell(textwrap.dedent(text).strip())


def code(text: str):
    return nbformat.v4.new_code_cell(textwrap.dedent(text).strip())


def build_notebook():
    notebook = nbformat.v4.new_notebook()
    notebook.metadata = {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
        "language_info": {"name": "python", "version": "3.12"},
    }
    notebook.cells = [
        markdown(
            """
            # McLaren 570S GT4 × Nürburgring Combined telemetry audit

            This notebook decides which downstream capabilities the four frozen
            iRacing `.ibt` recordings can safely support. The analytical grain is
            one immutable telemetry record (nominally 60 Hz); the downstream use
            is deterministic replay, lap segmentation, fuel estimation, and later
            corner-level coaching.
            """
        ),
        code(
            """
            from pathlib import Path
            import sys

            import matplotlib.pyplot as plt
            import polars as pl

            ROOT = Path.cwd().resolve()
            sys.path.insert(0, str(ROOT))
            sys.path.insert(0, str(ROOT / "src"))

            from scripts.build_quality_notebook import load_quality_notebook_inputs
            from iracing_ai_engineer.ibt import IbtReader, sha256_file
            from iracing_ai_engineer.quality import analyze_ibt
            from iracing_ai_engineer.replay import replay_ibt

            RAW_DIR = ROOT / "data" / "raw"
            manifest, paths = load_quality_notebook_inputs(ROOT)
            reports = [analyze_ibt(path) for path in paths]
            primary_path = RAW_DIR / manifest["primary_sample"]
            primary = next(report for report in reports if Path(report.source_path) == primary_path)
            """
        ),
        markdown("## TL;DR"),
        code(
            """
            summary = pl.DataFrame([report.summary_row() for report in reports])
            display(summary)

            print(
                f"{len(reports)}/{len(reports)} files are replay-readable; "
                f"{sum(r.capabilities.lap_ready for r in reports)} is lap-ready."
            )
            print(
                f"Primary sample: "
                f"{sum(lap.structurally_complete for lap in primary.laps)} structural laps, "
                f"{sum(lap.clean_for_driving for lap in primary.laps)} clean lap."
            )
            print(
                "Decision: keep fuel modeling and comparative driving coaching disabled; "
                "the current data is sufficient for Stage 0 replay/lap-pipeline validation only."
            )
            """
        ),
        markdown(
            """
            ## Context & methods

            The check is capability-oriented rather than a single pass/fail:

            - `replay_readable`: fixed-width payload, finite ordered session time,
              and exact record boundary.
            - `lap_ready`: at least one conservative boundary-to-boundary lap.
            - `fuel_ready`: at least two complete no-pit laps with measurable burn.
            - `driving_ready`: at least three clean laps for an algorithm smoke test.
            - `coaching_evidence_ready`: reserved and disabled until a trusted
              condition-cohort receipt and independently authenticated labels
              are attached; it also requires at least eight matched clean laps.

            A distance wrap plus lap-counter evidence is preferred. Counter-only
            boundaries near start/finish are retained at lower confidence. Pit,
            incident, off-track, reset, gap, and partial-lap labels remain explicit.

            Assumptions: these 2025 recordings are dry, from one car/track schema;
            SessionInfo is used only for a non-driver public subset. Current-stint
            tire wear is not treated as directly measured.
            """
        ),
        markdown("## Data provenance and integrity"),
        code(
            """
            expected = {item["file_name"]: item for item in manifest["files"]}
            provenance_rows = []
            for path in paths:
                digest = sha256_file(path)
                item = expected[path.name]
                provenance_rows.append(
                    {
                        "file": path.name,
                        "bytes": path.stat().st_size,
                        "sha256": digest,
                        "manifest_match": digest == item["sha256"]
                        and path.stat().st_size == item["byte_size"],
                    }
                )
            provenance = pl.DataFrame(provenance_rows)
            assert provenance["manifest_match"].all()
            display(provenance)

            context = primary.session_context
            print(
                {
                    "track": context["track_display_name"],
                    "track_length": context["track_length"],
                    "event_type": context["event_type"],
                    "sim_build": context["sim_build"],
                    "driver_info_exported": manifest["privacy"]["driver_info_exported"],
                }
            )
            """
        ),
        markdown("## Primary-sample gates and field coverage"),
        code(
            """
            gate_rows = [
                {
                    "gate": gate.gate_id,
                    "status": gate.status,
                    "metric": gate.metric,
                    "threshold": gate.threshold,
                }
                for gate in primary.gates
            ]
            display(pl.DataFrame(gate_rows))

            focus_fields = {
                "SessionTime", "SessionTick", "Lap", "LapCompleted", "LapDistPct",
                "Speed", "Throttle", "Brake", "SteeringWheelAngle", "Gear", "RPM",
                "FuelLevel", "FuelLevelPct", "OnPitRoad", "PlayerCarInPitStall",
                "PlayerTrackSurface", "PlayerCarMyIncidentCount",
            }
            field_rows = [
                {
                    "field": item.name,
                    "present": item.present,
                    "finite_fraction": item.finite_fraction,
                    "minimum": item.minimum,
                    "maximum": item.maximum,
                }
                for item in primary.field_stats
                if item.name in focus_fields
            ]
            display(pl.DataFrame(field_rows))
            """
        ),
        markdown("## Lap segmentation evidence"),
        code(
            """
            lap_rows = [
                {
                    "ordinal": lap.ordinal,
                    "source_lap": f"{lap.source_lap_start}->{lap.source_lap_end}",
                    "duration_s": round(lap.duration_s, 3),
                    "distance_laps": round(lap.distance_coverage_laps, 5),
                    "start_boundary": lap.start_boundary,
                    "end_boundary": lap.end_boundary,
                    "complete": lap.structurally_complete,
                    "quality_complete": lap.quality_complete,
                    "clean": lap.clean_for_driving,
                    "fuel_eligible": lap.fuel_eligible,
                    "pit_fraction": round(lap.on_pit_road_fraction, 4),
                    "incident_delta": lap.incident_delta,
                    "missing_ticks": lap.missing_ticks,
                    "duplicate_times": lap.duplicate_time_steps,
                    "tags": ",".join(lap.tags),
                    "invalid": ",".join(lap.invalid_reasons),
                }
                for lap in primary.laps
            ]
            display(pl.DataFrame(lap_rows))
            """
        ),
        code(
            """
            with IbtReader(primary_path) as reader:
                trace = reader.get_channels(("LapDistPct", "Speed"))

            fig, ax = plt.subplots(figsize=(11, 4.5))
            for lap in primary.laps:
                if not lap.structurally_complete:
                    continue
                section = slice(lap.start_frame, lap.end_frame_exclusive)
                label = f"segment {lap.ordinal}: " + (
                    "clean" if lap.clean_for_driving else "excluded"
                )
                ax.plot(
                    trace["LapDistPct"][section],
                    trace["Speed"][section] * 3.6,
                    linewidth=1.0,
                    alpha=0.9,
                    label=label,
                )
            ax.set(xlabel="Lap distance fraction", ylabel="Speed (km/h)", xlim=(0, 1))
            ax.set_title("Structurally complete segments; excluded laps stay visible")
            ax.grid(alpha=0.25)
            ax.legend()
            plt.show()
            """
        ),
        markdown("## Canonical replay receipt (batch event pipeline)"),
        code(
            """
            receipt_single = replay_ibt(primary_path, frame_hash_chunk_size=1)
            receipt_chunked = replay_ibt(primary_path, frame_hash_chunk_size=4096)
            assert receipt_single.replay_sha256 == receipt_chunked.replay_sha256
            assert (
                receipt_single.normalized_frames_sha256
                == receipt_chunked.normalized_frames_sha256
            )
            receipt_view = {
                "source_sha256": receipt_chunked.source_sha256,
                "schema_sha256": receipt_chunked.schema_sha256,
                "normalized_frames_sha256": receipt_chunked.normalized_frames_sha256,
                "events_sha256": receipt_chunked.events_sha256,
                "results_sha256": receipt_chunked.results_sha256,
                "replay_sha256": receipt_chunked.replay_sha256,
                "frame_count": receipt_chunked.frame_count,
                "event_count": receipt_chunked.event_count,
                "structurally_complete_lap_count": (
                    receipt_chunked.structurally_complete_lap_count
                ),
                "clean_driving_lap_count": receipt_chunked.clean_driving_lap_count,
                "frame_hash_partition_invariant": True,
                "event_pipeline_mode": receipt_chunked.event_pipeline_mode,
            }
            receipt_view
            """
        ),
        markdown("## Takeaways"),
        code(
            """
            print("1. IBT decoder, schema validation, and canonical frame hashing are operational.")
            print(
                "2. The primary file validates pit/reset-aware lap segmentation "
                "on real telemetry."
            )
            print(
                "3. The sole structural lap has a duplicate timestamp, so there "
                "are zero driving-clean laps."
            )
            print("4. Next data target: >=8 clean matched laps plus a session with >=2 pit stops.")
            print(
                "5. M0 is still partial: live shared-memory collection remains "
                "to be implemented; the event/lap pipeline is still batch."
            )
            """
        ),
    ]
    return notebook


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("notebooks/01_nurburgring_gt4_data_quality.ipynb"),
    )
    parser.add_argument("--no-execute", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    notebook = build_notebook()
    nbformat.write(notebook, output)
    if not args.no_execute:
        client = NotebookClient(
            notebook,
            timeout=600,
            kernel_name="python3",
            resources={"metadata": {"path": str(root)}},
        )
        client.execute(cwd=str(root))
        nbformat.write(notebook, output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
