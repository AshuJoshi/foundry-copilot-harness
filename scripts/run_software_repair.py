"""Invoke one pinned repair agent and validate authentic downloaded candidate artifacts."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

from harness_client import COPILOT_HARNESSES, HarnessError
from retail_support import RecordingClient, definition_for_rest, save
from scripts.prepare_software_repair import ROOT, digest, SKILLS
from scripts.publish_incident_skills import package_hashes
from scripts.publish_repair_skills import verify_prepared
from scripts.run_incident import arguments, invoke_once
from scripts.run_retail import normalized_output
from software_repair import validate_artifacts, validate_execution

SOURCE_FILES = [
    "scripts/run_software_repair.py", "scripts/run_incident.py",
    "scripts/publish_repair_skills.py", "scripts/publish_incident_skills.py",
    "scripts/prepare_software_repair.py", "scripts/evaluate_software_repair.py",
    "scripts/configure_repair.py", "software_repair.py", "retail_support.py", "harness_client.py",
    "agents/software-repair/azure.yaml", "agents/software-repair/case.json",
    "agents/software-repair/DATA-NOTICE.txt",
    "skills/software-repair/SKILL.md", "skills/software-repair/verify_inputs.py",
    "skills/software-repair-repository-data/SKILL.md",
    "requirements-repair-prep.txt", "requirements-repair-eval.txt",
]


def preflight(client: RecordingClient, prepared: Path, publication: Path, manifest: Path) -> tuple[dict, dict]:
    if not client.agent_version or not re.fullmatch(r"[1-9][0-9]*", client.agent_version):
        raise HarnessError("Require an explicit positive integer agent version.")
    verified = verify_prepared(prepared)
    published = json.loads(publication.read_text())
    if (published.get("status") != "verified" or published.get("scenario") != "repair"
            or published["project_endpoint"] != client.project_endpoint
            or published["package_sha256"] != package_hashes(prepared / "packages")
            or published["input_verification"] != verified):
        raise HarnessError("Publication differs from these verified repair inputs/project.")
    services = json.loads(manifest.read_text())["services"]
    agents = [s for s in services.values() if s.get("host") == "azure.ai.agent"]
    if len(agents) != 1 or agents[0]["name"] != client.agent_name:
        raise HarnessError("Repair manifest agent identity mismatch.")
    configured = agents[0]
    if services[configured["uses"][0]]["endpoint"] != client.project_endpoint:
        raise HarnessError("Repair manifest project mismatch.")
    expected = definition_for_rest({k: configured[k] for k in [
        "kind", "model", "harness", "instructions", "skills", "tools",
    ] if k in configured})
    if (expected.get("kind") != "prompt"
            or expected.get("harness", {}).get("type") not in COPILOT_HARNESSES
            or expected.get("tools")):
        raise HarnessError("Unexpected repair agent type or configured external tools.")
    pins = expected.get("skills", [])
    if (len(pins) != len(SKILLS) or set(published["versions"]) != set(SKILLS)
            or {s["name"]: s["version"] for s in pins} != published["versions"]):
        raise HarnessError("Repair manifest must pin both exact published Skills.")
    latest = client.get_agent()["versions"]["latest"]
    selected = latest if latest["version"] == client.agent_version else json.loads(client._request(
        client._agent_path + "/versions/" + urllib.parse.quote(client.agent_version, safe="")))
    actual = dict(selected["definition"])
    for definition in [expected, actual]:
        for key in ["tools", "skills"]:
            if definition.get(key) == []:
                definition.pop(key)
    if selected["status"] != "active" or actual != expected:
        raise HarnessError("Pinned repair definition differs from the rendered manifest.")
    return verified, selected


def check_downloads(paths: list[Path], prepared: Path) -> dict:
    task = json.loads((prepared / "packages/software-repair/task.json").read_text())
    with tempfile.TemporaryDirectory(prefix="repair-artifact-check-") as temporary:
        repository = Path(temporary) / "repository"
        for skill in SKILLS:
            shutil.copytree(prepared / "packages" / skill / "repository",
                            repository, dirs_exist_ok=True)
        return validate_artifacts(paths, task, repository)


def prompt(case_id: str) -> str:
    return (
        "Use the attached software-repair and software-repair-repository-data Skills "
        f"for {case_id}. Load both Skills and run the standalone input verifier before "
        "analysis. Investigate the actual issue in task.json using only the complete "
        "pinned local repository, merging both repository trees into a fresh writable "
        "copy. Reproduce and test when possible with available dependencies, without "
        "browsing, network access, installations, benchmark answers or input changes. "
        "Produce a minimal text patch to existing Python source files under src/ only; "
        "do not change tests, configuration or dependencies. Git may be absent: use "
        "Python difflib with standard diff --git headers and preserve a pristine copy. "
        "Do not write a custom patch applier; independent Git application checking "
        "will follow download. Upload exactly repair.json containing the complete "
        "patch string and report.md with the Skill schema, not a .diff file. "
        "Report actual attempted test commands and outcomes, including failures "
        "and missing dependencies. Do not claim independent benchmark success, "
        "which will be evaluated after download."
    )


def run(
    client: RecordingClient, prepared: Path, publication: Path, manifest: Path, output: Path,
) -> dict:
    if output.exists():
        raise HarnessError("Run output exists; refusing another invocation.")
    verification, selected = preflight(client, prepared, publication, manifest)
    task = json.loads((prepared / "packages/software-repair/task.json").read_text())
    try:
        response = invoke_once(client, prompt(task["case_id"]), output, selected, verification, SOURCE_FILES)
        validate_execution(response, verification)
        paths = client.download_outputs(response, output / "downloads")
        artifacts = check_downloads(paths, prepared)
        result = {
            "service_status": response["status"], "execution_validation": "passed",
            "artifact_validation": artifacts,
            "downloaded_sha256": {p.name: digest(p.read_bytes()) for p in paths},
            "scope": "Candidate delivery/application only; official Docker grading is separate.",
        }
        save(output / "result.json", result)
        return result
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if output.exists():
            save(output / "failure.json", {"status": "stopped", "error": str(error), "retried": False})
        raise


def audit(client: RecordingClient, prepared: Path, publication: Path, manifest: Path,
          run_dir: Path, output: Path) -> dict:
    verification, selected = preflight(client, prepared, publication, manifest)
    conditions = json.loads((run_dir / "conditions.json").read_text())
    if set(conditions["source_sha256"]) != set(SOURCE_FILES):
        raise HarnessError("Audited repair source inventory differs from the original run.")
    for name, wanted in conditions["source_sha256"].items():
        if digest((ROOT / name).read_bytes()) != wanted:
            raise HarnessError("Audited repair source changed.")
    text = prompt(verification["case_id"])
    if conditions["input_verification"] != verification or conditions["prompt_sha256"] != digest(text.encode()):
        raise HarnessError("Audited repair task or verification changed.")
    submitted = json.loads((run_dir / "request.json").read_text())
    saved = json.loads((run_dir / "response.json").read_text())
    original_result = json.loads((run_dir / "result.json").read_text())
    output.mkdir(parents=True, exist_ok=False)
    try:
        live = client.get_response(saved["id"])
        save(output / "response.json", live)
        reference = {"type": "agent_reference", "name": client.agent_name, "version": client.agent_version}
        conversation = live.get("conversation")
        conversation = conversation.get("id") if isinstance(conversation, dict) else conversation
        if (live.get("id") != saved["id"] or live.get("status") != "completed"
                or submitted["input"] != text or submitted["agent_reference"] != reference
                or live.get("agent_reference") != reference or submitted["conversation"] != conversation
                or live.get("model") != selected["definition"]["model"]
                or normalized_output(saved["output"]) != normalized_output(live["output"])):
            raise HarnessError("Audited repair response/request differs from original.")
        validate_execution(live, verification)
        paths = client.download_outputs(live, output / "downloads")
        for path in paths:
            if (path.read_bytes() != (run_dir / "downloads" / path.name).read_bytes()
                    or digest(path.read_bytes()) != original_result["downloaded_sha256"][path.name]):
                raise HarnessError("Audited repair file differs from original download provenance.")
        artifacts = check_downloads(paths, prepared)
        if artifacts != original_result["artifact_validation"]:
            raise HarnessError("Audited repair patch/application result changed.")
        result = {"status": "reconciled", "paid_invocations": 0, "response_id": live["id"],
                  "artifact_validation": artifacts,
                  "scope": "Execution, original files and patch application only; official Docker grading is separate."}
        save(output / "audit.json", result)
        return result
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        save(output / "failure.json", {"status": "stopped", "error": str(error), "retried": False})
        raise


def main() -> int:
    args = arguments(__doc__ or "").parse_args()
    try:
        if not args.audit_run_dir and not args.allow_paid_invocation:
            raise HarnessError("Explicit --allow-paid-invocation required.")
        client = RecordingClient(args.project_endpoint, args.subscription, args.agent_name,
                                 agent_version=args.agent_version, timeout=args.timeout)
        if args.audit_run_dir:
            result = audit(client, args.prepared_dir, args.publication, args.manifest,
                           args.audit_run_dir, args.output_dir)
        else:
            result = run(client, args.prepared_dir, args.publication, args.manifest, args.output_dir)
        print(json.dumps(result, indent=2))
        return 0
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
