"""Acquire pinned RCAEval inputs or build verified Skill packages offline."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

from harness_client import HarnessError

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "agents/incident-investigation/cases.json"
NOTICE = ROOT / "agents/incident-investigation/DATA-NOTICE.txt"
SKILLS = [
    "incident-investigation-telemetry",
    "incident-trace-data-part-one",
    "incident-trace-data-part-two",
]
LIMIT = 16_000_000
SOURCE_LIMIT = 20_000_000
SOURCE_NAMES = {"metrics.parquet", "logs.parquet", "traces.parquet"}


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as target:
        json.dump(value, target, indent=2)
        target.write("\n")


def case_config(case_id: str) -> tuple[dict, dict]:
    catalog = json.loads(CATALOG.read_text())
    if catalog["schema_version"] != 1:
        raise HarnessError("Unsupported source catalog schema.")
    if not re.fullmatch(r"[0-9a-f]{40}", catalog["revision"]):
        raise HarnessError("Source revision must be an immutable commit.")
    if catalog["repository"] != "phamquiluan/RCAEval":
        raise HarnessError("Unexpected dataset repository.")
    cases = [item for item in catalog["cases"] if item["id"] == case_id]
    if len(cases) != 1 or not re.fullmatch(r"case-\d{3}", case_id):
        raise HarnessError(f"Unknown or duplicate case: {case_id}")
    case = cases[0]
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", case["upstream_case"]):
        raise HarnessError("Unsafe upstream case path.")
    entries = case["files"]
    if len(entries) != 3 or {f["name"] for f in entries} != SOURCE_NAMES:
        raise HarnessError("Source catalog must specify exactly three Parquet files.")
    for entry in entries:
        if (not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or not re.fullmatch(r"[0-9a-f]{64}", entry["csv_sha256"])
                or not 0 < entry["bytes"] <= SOURCE_LIMIT
                or entry["rows"] <= 0 or entry["columns"] <= 0):
            raise HarnessError(f"Invalid pinned source metadata: {entry['name']}")
    traces = next(entry for entry in entries if entry["name"] == "traces.parquet")
    if not 0 < case["trace_split_after"] < traces["rows"]:
        raise HarnessError("Pinned trace split must leave two nonempty shards.")
    return catalog, case


def verify_sources(source: Path, case: dict) -> None:
    for entry in case["files"]:
        path = source / entry["name"]
        if path.is_symlink() or not path.is_file():
            raise HarnessError(f"Missing or nonregular source: {path}")
        if path.stat().st_size != entry["bytes"] or fingerprint(path) != entry["sha256"]:
            raise HarnessError(f"Pinned source fingerprint mismatch: {path}")


def download(case_id: str, output: Path) -> None:
    catalog, case = case_config(case_id)
    output.mkdir(parents=True, exist_ok=False)
    try:
        for entry in case["files"]:
            url = (
                f"https://huggingface.co/datasets/{catalog['repository']}/resolve/"
                f"{catalog['revision']}/{case['upstream_case']}/{entry['name']}"
            )
            with urllib.request.urlopen(url, timeout=120) as response:
                body = response.read(entry["bytes"] + 1)
            if len(body) != entry["bytes"] or hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise HarnessError(f"Download fingerprint mismatch: {entry['name']}")
            with (output / entry["name"]).open("xb") as target:
                target.write(body)
        verify_sources(output, case)
        write_json(output / "download.json", {
            "status": "verified", "case_id": case_id,
            "revision": catalog["revision"], "files": case["files"],
        })
    except (OSError, ValueError, HarnessError) as error:
        write_json(output / "failure.json", {"status": "failed", "error": str(error)})
        raise


def convert(source: Path, destination: Path, entry: dict) -> dict:
    try:
        import pyarrow as pa
        import pyarrow.csv as arrow_csv
        import pyarrow.parquet as parquet
    except ImportError as error:
        raise HarnessError("Preparation requires the pinned incident-prep optional dependency.") from error
    if pa.__version__ != "21.0.0":
        raise HarnessError(f"Require pyarrow 21.0.0, found {pa.__version__}.")
    table = parquet.read_table(source)
    if (table.num_rows != entry["rows"] or table.num_columns != entry["columns"]
            or "evidence_row_id" in table.column_names):
        raise HarnessError(f"Pinned source shape mismatch: {source.name}")
    table = table.add_column(0, "evidence_row_id", pa.array(range(1, len(table) + 1)))
    arrow_csv.write_csv(table, destination)
    restored = arrow_csv.read_csv(
        destination,
        convert_options=arrow_csv.ConvertOptions(
            column_types=table.schema, strings_can_be_null=True,
            quoted_strings_can_be_null=False, null_values=[""],
        ),
    )
    if not table.equals(restored):
        raise HarnessError(f"CSV conversion changed typed values or nulls: {source.name}")
    digest = fingerprint(destination)
    if digest != entry["csv_sha256"]:
        raise HarnessError(f"CSV differs from historical pinned bytes: {source.name}")
    return {
        "name": destination.name, "rows": len(table), "columns": table.column_names,
        "sha256": digest, "first_row_id": 1,
    }


def shard_traces(
    source: Path, destinations: list[Path], rows: int, first_count: int | None = None,
) -> list[dict]:
    if len(destinations) != 2 or rows < 2:
        raise HarnessError("The current Skill layout requires two nonempty trace shards.")
    with source.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames
        if not columns or columns[0] != "evidence_row_id":
            raise HarnessError("Trace CSV is missing its leading record identifier.")
        first_count = (rows + 1) // 2 if first_count is None else first_count
        if not 0 < first_count < rows:
            raise HarnessError("Invalid pinned trace split.")
        sizes = [first_count, rows - first_count]
        entries = []
        expected_id = 1
        for destination, count in zip(destinations, sizes):
            first = expected_id
            with destination.open("x", newline="", encoding="utf-8") as target:
                writer = csv.DictWriter(target, fieldnames=columns, lineterminator="\n")
                writer.writeheader()
                for _ in range(count):
                    row = next(reader, None)
                    if (row is None or None in row or None in row.values()
                            or row["evidence_row_id"] != str(expected_id)):
                        raise HarnessError("Missing, malformed or nonsequential trace record.")
                    writer.writerow(row)
                    expected_id += 1
            entries.append({
                "name": destination.name, "rows": count, "columns": columns,
                "sha256": fingerprint(destination), "first_row_id": first,
                "source_skill": destination.parent.name,
            })
        if next(reader, None) is not None:
            raise HarnessError("Unexpected extra trace records.")
    with source.open(newline="", encoding="utf-8") as original:
        expected = csv.DictReader(original)
        for destination in destinations:
            with destination.open(newline="", encoding="utf-8") as shard:
                reader = csv.DictReader(shard)
                if reader.fieldnames != columns:
                    raise HarnessError("Trace shard schema changed.")
                for row in reader:
                    if row != next(expected, None):
                        raise HarnessError("Trace sharding changed a complete source record.")
        if next(expected, None) is not None:
            raise HarnessError("Trace sharding omitted records.")
    return entries


def check_packages(packages: Path, upstream_case: str, limit: int = LIMIT) -> dict:
    expected = {
        SKILLS[0]: {"SKILL.md", "verify_inputs.py", "DATA-NOTICE.txt",
                    "metrics.csv", "logs.csv", "input-manifest.json"},
        SKILLS[1]: {"SKILL.md", "DATA-NOTICE.txt", "traces-001.csv"},
        SKILLS[2]: {"SKILL.md", "DATA-NOTICE.txt", "traces-002.csv"},
    }
    sizes = {}
    for skill, names in expected.items():
        directory = packages / skill
        files = list(directory.iterdir())
        if ({f.name for f in files} != names
                or any(f.is_symlink() or not f.is_file() for f in files)):
            raise HarnessError(f"Unexpected package members: {skill}")
        size = sum(f.stat().st_size for f in files)
        if size >= limit or any(f.stat().st_size >= limit for f in files):
            raise HarnessError(f"Package exceeds conservative byte limit: {skill}")
        for file in files:
            if upstream_case.encode() in file.read_bytes():
                raise HarnessError(f"Upstream answer-bearing case identity leaked: {file.name}")
        sizes[skill] = size
    return sizes


def build(case_id: str, source: Path, output: Path) -> None:
    catalog, case = case_config(case_id)
    verify_sources(source, case)
    if source.resolve() == output.resolve() or source.resolve().is_relative_to(output.resolve()):
        raise HarnessError("Build output must not contain or replace the source directory.")
    output.mkdir(parents=True, exist_ok=False)
    try:
        original = output / "original"
        original.mkdir()
        files = []
        for entry in case["files"]:
            files.append(convert(
                source / entry["name"], original / entry["name"].replace(".parquet", ".csv"), entry,
            ))
        packages = output / "packages"
        for skill in SKILLS:
            directory = packages / skill
            directory.mkdir(parents=True)
            text = (ROOT / "skills" / skill / "SKILL.md").read_text()
            if skill == SKILLS[0]:
                text = text.replace('"case_id": "case-001"', f'"case_id": "{case_id}"')
            (directory / "SKILL.md").write_text(text, encoding="utf-8")
            shutil.copyfile(NOTICE, directory / "DATA-NOTICE.txt")
        main = packages / SKILLS[0]
        shutil.copyfile(ROOT / "skills" / SKILLS[0] / "verify_inputs.py", main / "verify_inputs.py")
        manifest_files = []
        for entry in files:
            if entry["name"] in {"logs.csv", "metrics.csv"}:
                shutil.copyfile(original / entry["name"], main / entry["name"])
                manifest_files.append(entry)
        traces = next(entry for entry in files if entry["name"] == "traces.csv")
        manifest_files.extend(shard_traces(
            original / "traces.csv",
            [packages / SKILLS[1] / "traces-001.csv", packages / SKILLS[2] / "traces-002.csv"],
            traces["rows"], case["trace_split_after"],
        ))
        manifest = {"case_id": case_id, "files": manifest_files}
        write_json(main / "input-manifest.json", manifest)
        sizes = check_packages(packages, case["upstream_case"])
        verification = subprocess.run(
            [sys.executable, str((main / "verify_inputs.py").resolve())],
            capture_output=True, text=True, timeout=120,
        )
        if verification.returncode:
            raise HarnessError(f"Runtime input verifier failed: {verification.stderr.strip()}")
        actual = json.loads(verification.stdout)
        if actual != dict(manifest, status="verified"):
            raise HarnessError("Runtime verifier returned unexpected output.")
        write_json(output / "runtime-verification.json", actual)
        with zipfile.ZipFile(output / "original-input.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry in files:
                archive.write(original / entry["name"], entry["name"])
        import pyarrow
        write_json(output / "preparation.json", {
            "status": "verified", "case_id": case_id, "source_revision": catalog["revision"],
            "catalog_sha256": fingerprint(CATALOG), "pyarrow_version": pyarrow.__version__,
            "preparer_sha256": fingerprint(Path(__file__)),
            "dependency_manifest_sha256": fingerprint(ROOT / "requirements-incident.txt"),
            "source_files": case["files"], "original_csvs": files, "package_bytes": sizes,
            "package_sha256": {
                str(file.relative_to(packages)): fingerprint(file)
                for file in sorted(packages.rglob("*")) if file.is_file()
            },
            "checks": ["pinned source size/hash", "typed CSV roundtrip including nulls",
                       "historical CSV hashes", "every trace record preserved",
                       "package members and sizes", "standalone runtime verifier"],
            "scope": "Offline preparation only; no publication, deployment or model invocation.",
        })
    except (OSError, ValueError, HarnessError, subprocess.SubprocessError) as error:
        write_json(output / "failure.json", {"status": "failed", "error": str(error)})
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("download", help="Download only pinned public telemetry, never benchmark answers.")
    fetch.add_argument("--case", required=True)
    fetch.add_argument("--output-dir", type=Path, required=True)
    prepare = commands.add_parser("build", help="Offline conversion, packaging and verification.")
    prepare.add_argument("--case", required=True)
    prepare.add_argument("--source-dir", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "download":
            download(args.case, args.output_dir)
        else:
            build(args.case, args.source_dir, args.output_dir)
        print(f"{args.command} verified: {args.case} -> {args.output_dir}")
        return 0
    except (HarnessError, OSError, ValueError, subprocess.SubprocessError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
