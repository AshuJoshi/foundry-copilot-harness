"""Publish rebuilt scenario Skills once and verify immutable package roundtrips."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from harness_client import HarnessClient, HarnessError
from retail_support import save
from scripts.prepare_incident import SKILLS as INCIDENT_SKILLS, ROOT

SKILLS = {
    "incident": INCIDENT_SKILLS,
}

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_hashes(packages: Path) -> dict[str, str]:
    files = {}
    for path in packages.rglob("*"):
        if path.is_symlink():
            raise HarnessError("Symlink in package tree.")
        if path.is_file():
            files[path.relative_to(packages).as_posix()] = digest(path.read_bytes())
    return files


def verify_prepared(scenario: str, prepared: Path) -> dict:
    if scenario not in SKILLS:
        raise HarnessError("Unknown scenario.")
    report = json.loads((prepared / "preparation.json").read_text())
    if report.get("status") != "verified":
        raise HarnessError("Prepared input status is not verified.")
    from scripts.prepare_incident import CATALOG
    expected_provenance = {
        "catalog_sha256": digest(CATALOG.read_bytes()),
        "preparer_sha256": digest((ROOT / "scripts/prepare_incident.py").read_bytes()),
        "dependency_manifest_sha256": digest((ROOT / "requirements-incident.txt").read_bytes()),
    }
    if any(report.get(key) != wanted for key, wanted in expected_provenance.items()):
        raise HarnessError("Preparation source/catalog provenance changed; rebuild into a new directory.")
    packages = prepared / "packages"
    if {p.name for p in packages.iterdir()} != set(SKILLS[scenario]):
        raise HarnessError("Unexpected Skill package directories.")
    if package_hashes(packages) != report["package_sha256"]:
        raise HarnessError("Prepared package fingerprints changed.")
    main = packages / SKILLS[scenario][0]
    verifier = ROOT / "skills" / main.name / "verify_inputs.py"
    if verifier.read_bytes() != (main / "verify_inputs.py").read_bytes():
        raise HarnessError("Packaged input verifier differs from tracked source.")
    for skill in SKILLS[scenario]:
        expected = (ROOT / "skills" / skill / "SKILL.md").read_text()
        if scenario == "incident" and skill == SKILLS[scenario][0]:
            expected = expected.replace('"case_id": "case-001"', f'"case_id": "{report["case_id"]}"')
        if expected != (packages / skill / "SKILL.md").read_text():
            raise HarnessError(f"Packaged instructions differ from source: {skill}")
    if scenario == "incident":
        from scripts.prepare_incident import case_config, check_packages
        catalog, case = case_config(report["case_id"])
        if (report.get("source_revision") != catalog["revision"] or report.get("source_files") != case["files"]
                or report.get("pyarrow_version") != "21.0.0"):
            raise HarnessError("Preparation does not match the pinned incident source.")
        if {entry["name"] for entry in report["original_csvs"]} != {"logs.csv", "metrics.csv", "traces.csv"}:
            raise HarnessError("Original CSV reference inventory is incomplete.")
        check_packages(packages, case["upstream_case"])
        for entry in report["original_csvs"]:
            if digest((prepared / "original" / entry["name"]).read_bytes()) != entry["sha256"]:
                raise HarnessError("Original evidence CSV changed.")
            pinned = next(f for f in case["files"]
                          if f["name"] == entry["name"].replace(".csv", ".parquet"))
            if entry["sha256"] != pinned["csv_sha256"]:
                raise HarnessError("Original CSV is not the pinned source conversion.")
        import zipfile
        with zipfile.ZipFile(prepared / "original-input.zip") as archive:
            names = ["logs.csv", "metrics.csv", "traces.csv"]
            if len(archive.namelist()) != 3 or set(archive.namelist()) != set(names):
                raise HarnessError("Unexpected original evidence archive members.")
            for name in names:
                if archive.read(name) != (prepared / "original" / name).read_bytes():
                    raise HarnessError("Evidence archive differs from pinned CSV.")
    result = subprocess.run(
        [sys.executable, str((main / "verify_inputs.py").resolve())],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise HarnessError(f"Prepared input verifier failed: {result.stderr.strip()}")
    verified = json.loads(result.stdout)
    if verified != json.loads((prepared / "runtime-verification.json").read_text()):
        raise HarnessError("Runtime verifier differs from saved preparation verification.")
    return verified


def command(arguments: list[str], log: Path) -> str:
    environment = dict(os.environ, AZURE_DEV_USER_AGENT="microsoft_foundry_skill")
    save(log.with_suffix(".command.json"), {"arguments": arguments})
    result = subprocess.run(
        ["azd", *arguments], capture_output=True, text=True, timeout=600, env=environment,
    )
    log.write_text(result.stdout + "\n" + result.stderr)
    if result.returncode:
        raise HarnessError(f"azd failed; inspect {log}. No automatic write retry.")
    return result.stdout


def publish(scenario: str, prepared: Path, endpoint: str, output: Path, operation: str) -> None:
    if operation not in {"create", "update"}:
        raise HarnessError("Publication operation must be explicit create or update.")
    HarnessClient(endpoint, "endpoint-validation-only", "publication")
    verified = verify_prepared(scenario, prepared)
    output.mkdir(parents=True, exist_ok=False)
    try:
        hashes = package_hashes(prepared / "packages")
        save(output / "conditions.json", {
            "scenario": scenario, "project_endpoint": endpoint, "operation": operation,
            "package_sha256": hashes, "input_verification": verified,
            "publisher_sha256": digest(Path(__file__).read_bytes()), "write_retries": 0,
        })
        versions = {}
        for skill in SKILLS[scenario]:
            if package_hashes(prepared / "packages") != hashes:
                raise HarnessError("Packages changed during publication.")
            raw = command([
                "ai", "skill", operation, skill, "--file",
                str((prepared / "packages" / skill).resolve()),
                "--project-endpoint", endpoint, "--no-prompt", "--output", "json",
            ], output / f"publish-{skill}.log")
            published = json.loads(raw)
            if published.get("name") != skill or not re.fullmatch(r"[1-9][0-9]*", str(published.get("version"))):
                raise HarnessError("Unexpected Skill publication name/version.")
            save(output / f"published-{skill}.json", published)
            versions[skill] = str(published["version"])
            command([
                "ai", "skill", "download", skill, "--version", versions[skill],
                "--output-dir", str((output / "downloaded" / skill).resolve()),
                "--project-endpoint", endpoint, "--no-prompt",
            ], output / f"download-{skill}.log")
            expected = {name.removeprefix(skill + "/"): value for name, value in hashes.items()
                        if name.startswith(skill + "/")}
            if package_hashes(output / "downloaded" / skill) != expected:
                raise HarnessError(f"Published Skill bytes differ: {skill}")
        save(output / "publication.json", {
            "status": "verified", "scenario": scenario, "project_endpoint": endpoint,
            "versions": versions, "package_sha256": hashes, "input_verification": verified,
        })
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        save(output / "failure.json", {
            "status": "stopped", "error": str(error), "retried": False,
            "action": "Inspect immutable version records before considering another publication.",
        })
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SKILLS, required=True)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--operation", choices=["create", "update"], required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-azure-writes", action="store_true")
    args = parser.parse_args()
    try:
        if not args.allow_azure_writes:
            raise HarnessError("Explicit --allow-azure-writes required.")
        publish(args.scenario, args.prepared_dir, args.project_endpoint, args.output_dir, args.operation)
        return 0
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
