"""Run one pinned incident agent with verified published inputs and exact evidence checks."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse

from harness_client import COPILOT_HARNESSES, HarnessError, JsonObject, ResponseError, final_text
from incident_investigation import validate_artifacts, validate_execution
from retail_support import RecordingClient, save, summarize, definition_for_rest
from scripts.publish_incident_skills import package_hashes, verify_prepared
from scripts.run_retail import normalized_output

ROOT = Path(__file__).resolve().parents[1]

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prompt(case_id: str) -> str:
    return (
        "Use the attached incident-investigation-telemetry Skill and its two "
        f"attached data-only Skills for {case_id}. Verify the complete supplied "
        "telemetry with the standalone verifier before analysis. Investigate the "
        "originating failure, distinguish downstream symptoms, assess alternatives "
        "and provide a falsifiable next test and advisory mitigation with risks and "
        "rollback criteria. Use only local telemetry and Python standard library. "
        "Do not browse, install packages, access benchmark answers or change inputs "
        "or external systems. Upload diagnosis.json using the Skill's exact schema "
        "and report.md. Cite complete exact original CSV cell strings and stable "
        "record IDs across metrics, logs and traces. Explain limitations and "
        "uncertainty honestly; do not invent observed recovery or customer impact."
    )


def preflight(
    client: RecordingClient, scenario: str, prepared: Path, publication: Path, manifest: Path,
) -> tuple[dict, dict]:
    if not client.agent_version or not re.fullmatch(r"[1-9][0-9]*", client.agent_version):
        raise HarnessError("Require an explicit positive integer agent version.")
    verified = verify_prepared(scenario, prepared)
    published = json.loads(publication.read_text())
    if (published.get("status") != "verified" or published["scenario"] != scenario
            or published["project_endpoint"] != client.project_endpoint
            or published["package_sha256"] != package_hashes(prepared / "packages")
            or published["input_verification"] != verified):
        raise HarnessError("Publication does not match these verified local inputs/project.")
    services = json.loads(manifest.read_text())["services"]
    agents = [s for s in services.values() if s.get("host") == "azure.ai.agent"]
    if len(agents) != 1 or agents[0]["name"] != client.agent_name:
        raise HarnessError("Manifest agent identity mismatch.")
    configured = agents[0]
    if services[configured["uses"][0]]["endpoint"] != client.project_endpoint:
        raise HarnessError("Manifest project endpoint mismatch.")
    expected = definition_for_rest({key: configured[key] for key in [
        "kind", "model", "harness", "instructions", "skills", "tools",
    ] if key in configured})
    if (expected.get("kind") != "prompt"
            or expected.get("harness", {}).get("type") not in COPILOT_HARNESSES
            or expected.get("tools")):
        raise HarnessError("Unexpected scenario agent type or configured external tools.")
    skills = expected.get("skills", [])
    if (len(skills) != len(published["versions"])
            or {s["name"]: s["version"] for s in skills} != published["versions"]):
        raise HarnessError("Manifest does not pin exact roundtrip-verified Skill versions.")
    latest = client.get_agent()["versions"]["latest"]
    selected = latest if latest["version"] == client.agent_version else json.loads(client._request(
        client._agent_path + "/versions/" + urllib.parse.quote(client.agent_version, safe=""),
    ))
    actual = dict(selected["definition"])
    for definition in [actual, expected]:
        for key in ["tools", "skills"]:
            if definition.get(key) == []:
                definition.pop(key)
    if selected["status"] != "active" or actual != expected:
        raise HarnessError("Pinned deployed definition differs from the rendered manifest.")
    return verified, selected


def invoke_once(
    client: RecordingClient, text: str, output: Path, selected: dict, verification: dict,
    source_files: list[str],
) -> JsonObject:
    output.mkdir(parents=True, exist_ok=False)
    save(output / "agent.json", selected)
    save(output / "conditions.json", {
        "source_commit": None,
        "source_provenance": "Source hashes recorded; committed-export verification is a separate owner-controlled step.",
        "source_sha256": {name: digest((ROOT / name).read_bytes()) for name in source_files},
        "input_verification": verification, "prompt_sha256": digest(text.encode()),
        "invocation_retries": 0,
    })
    (output / "prompt.txt").write_text(text, encoding="utf-8")
    conversation = client.create_conversation()
    save(output / "conversation.json", {"id": conversation})
    client.evidence_dir = output
    started = time.monotonic()
    try:
        response = client.invoke(text, conversation_id=conversation)
    except ResponseError as error:
        save(output / "response.json", error.response)
        raise
    save(output / "response.json", response)
    returned = response.get("conversation")
    returned_id = returned.get("id") if isinstance(returned, dict) else returned
    if returned_id != conversation or response.get("model") != selected["definition"]["model"]:
        raise HarnessError("Response conversation or selected model mismatch.")
    summary = summarize(response)
    summary["client_invocation_seconds"] = round(time.monotonic() - started, 3)
    save(output / "metrics.json", summary)
    (output / "answer.txt").write_text(final_text(response), encoding="utf-8")
    return response


def arguments(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    for name in ["project-endpoint", "subscription", "agent-name", "agent-version"]:
        parser.add_argument("--" + name, required=True)
    for name in ["prepared-dir", "publication", "manifest", "output-dir"]:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--allow-paid-invocation", action="store_true")
    parser.add_argument("--audit-run-dir", type=Path,
                        help="Read-only audit of an existing run; no model invocation.")
    return parser


def run(
    client: RecordingClient, prepared: Path, publication: Path, manifest: Path, output: Path,
) -> dict:
    if output.exists():
        raise HarnessError("Run output exists; refusing another invocation.")
    verification, selected = preflight(client, "incident", prepared, publication, manifest)
    try:
        response = invoke_once(client, prompt(verification["case_id"]), output, selected, verification, [
            "scripts/run_incident.py", "scripts/publish_incident_skills.py", "retail_support.py",
            "harness_client.py", "incident_investigation.py", "scripts/prepare_incident.py",
            "agents/incident-investigation/azure.yaml", "agents/incident-investigation/cases.json",
        ])
        validate_execution(response, verification)
        paths = client.download_outputs(response, output / "downloads")
        artifacts = validate_artifacts(paths, prepared / "original-input.zip", verification["case_id"])
        result = {
            "service_status": response["status"], "execution_validation": "passed",
            "artifact_validation": artifacts,
            "downloaded_sha256": {p.name: digest(p.read_bytes()) for p in paths},
            "scope": "Exact evidence cells/schema checked; causal relevance and labels need separate review.",
        }
        save(output / "result.json", result)
        return result
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if output.exists():
            save(output / "failure.json", {"status": "stopped", "error": str(error), "retried": False})
        raise


def audit(client: RecordingClient, prepared: Path, publication: Path, manifest: Path,
          run_dir: Path, output: Path) -> dict:
    verification, selected = preflight(client, "incident", prepared, publication, manifest)
    conditions = json.loads((run_dir / "conditions.json").read_text())
    for name, wanted in conditions["source_sha256"].items():
        if digest((ROOT / name).read_bytes()) != wanted:
            raise HarnessError("Audited source differs from original run.")
    text = prompt(verification["case_id"])
    if conditions["input_verification"] != verification or conditions["prompt_sha256"] != digest(text.encode()):
        raise HarnessError("Audited input verification or task changed.")
    request = json.loads((run_dir / "request.json").read_text())
    saved = json.loads((run_dir / "response.json").read_text())
    output.mkdir(parents=True, exist_ok=False)
    live = client.get_response(saved["id"])
    save(output / "response.json", live)
    agent = {"type": "agent_reference", "name": client.agent_name, "version": client.agent_version}
    conversation = live.get("conversation")
    conversation = conversation.get("id") if isinstance(conversation, dict) else conversation
    if (request["input"] != text or request["agent_reference"] != agent
            or live.get("agent_reference") != agent or request["conversation"] != conversation
            or live.get("model") != selected["definition"]["model"]
            or normalized_output(saved["output"]) != normalized_output(live["output"])):
        raise HarnessError("Audited response/request identity or content differs.")
    validate_execution(live, verification)
    paths = client.download_outputs(live, output / "downloads")
    for path in paths:
        if path.read_bytes() != (run_dir / "downloads" / path.name).read_bytes():
            raise HarnessError("Audited output file bytes changed.")
    artifacts = validate_artifacts(paths, prepared / "original-input.zip", verification["case_id"])
    result = {"status": "reconciled", "paid_invocations": 0, "response_id": live["id"],
              "artifact_validation": artifacts,
              "scope": "Exact execution/request/file checks; causal quality requires human review."}
    save(output / "audit.json", result)
    return result


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
