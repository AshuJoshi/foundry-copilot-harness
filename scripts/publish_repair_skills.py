"""Verify complete repair inputs, publish two Skills once and check their exact bytes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from harness_client import HarnessError
from scripts.evaluate_software_repair import verify_prepared as verify_evaluator_inputs
from scripts.prepare_software_repair import CONFIG, ROOT, SKILLS, digest, settings
from scripts.publish_incident_skills import package_hashes, publish_verified


def verify_prepared(prepared: Path) -> dict:
    report = json.loads((prepared / "preparation.json").read_text())
    if report.get("status") != "verified":
        raise HarnessError("Prepared repair inputs are not verified.")
    if (report.get("config_sha256") != digest(CONFIG.read_bytes())
            or report.get("preparer_sha256") != digest((ROOT / "scripts/prepare_software_repair.py").read_bytes())):
        raise HarnessError("Preparation source/config changed; rebuild into a new directory.")
    packages = prepared / "packages"
    if {p.name for p in packages.iterdir()} != set(SKILLS):
        raise HarnessError("Unexpected repair Skill package directories.")
    if package_hashes(packages) != report["package_sha256"]:
        raise HarnessError("Prepared repair package fingerprints changed.")
    for skill in SKILLS:
        if (ROOT / "skills" / skill / "SKILL.md").read_bytes() != (packages / skill / "SKILL.md").read_bytes():
            raise HarnessError("Packaged repair instructions differ from source.")
        if (ROOT / "agents/software-repair/DATA-NOTICE.txt").read_bytes() != (packages / skill / "DATA-NOTICE.txt").read_bytes():
            raise HarnessError("Packaged repair data notice differs from source.")
    main = packages / SKILLS[0]
    if (ROOT / "skills/software-repair/verify_inputs.py").read_bytes() != (main / "verify_inputs.py").read_bytes():
        raise HarnessError("Packaged repair verifier differs from source.")
    verify_evaluator_inputs(prepared, settings())
    result = subprocess.run(
        [sys.executable, str((main / "verify_inputs.py").resolve())],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise HarnessError(f"Prepared repair verifier failed: {result.stderr.strip()}")
    verified = json.loads(result.stdout)
    if verified != json.loads((prepared / "runtime-verification.json").read_text()):
        raise HarnessError("Runtime repair verification differs from preparation.")
    return verified


def publish(prepared: Path, endpoint: str, output: Path, operation: str) -> None:
    if operation not in {"create", "update"}:
        raise HarnessError("Select create or update explicitly.")
    verified = verify_prepared(prepared)
    publish_verified("repair", SKILLS, prepared, endpoint, output, operation, verified)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--operation", choices=["create", "update"], required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-azure-writes", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.allow_azure_writes:
            raise HarnessError("Explicit --allow-azure-writes required.")
        publish(args.prepared_dir, args.project_endpoint, args.output_dir, args.operation)
        return 0
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
