"""Live bounded two-agent review with explicit base64 input_file handoffs."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
import urllib.parse

from harness_client import HarnessClient, HarnessError, ResponseError, final_text
from incident_investigation import validate_artifacts, validate_execution
from incident_review import contract, freeze, sha, validate_critique, validate_diagnosis
from retail_support import RecordingClient, save, summarize, hash_command, verify_consumption, selected, MARKER
from scripts.publish_incident_skills import package_hashes, verify_prepared
from scripts.run_retail import normalized_output

ROOT = Path(__file__).resolve().parents[1]
MARKER = "HANDOFF_VERIFIED="
CRITIQUE_TASK = (
    "CRITIQUE stage. Read the four actual attached JSON files after verifying their hashes. "
    "Use the definitions.critique contract in review-contracts.json (its ref keys refer to "
    "definitions there). review-binding.json supplies the case and artifact hashes. "
    "Critique peer-diagnosis.json, preserving independent-assessment.json. Exact quoted "
    "claims must resolve at JSON pointers; evidence must cite complete original cells. "
    "Retain supported claims; do not invent objections. Upload exactly critique.json and "
    "report.md. Use all telemetry Skills and standalone verifier. Do not output diagnosis.json."
)
INDEPENDENT_TASK = (
    "INDEPENDENT stage. You have no peer diagnosis. Use the verified case-001 telemetry "
    "and all three Skills. Run the standalone input verifier. Upload exactly diagnosis.json "
    "and report.md. Follow the investigation Skill schema, exact cells, alternatives, "
    "uncertainty, falsifiable test and advisory-only mitigation. No browsing or installs."
)


def content(text: str, attachments: dict[str, bytes]) -> list[dict]:
    parts = [{"type": "input_text", "text": text}]
    for name, raw in attachments.items():
        if Path(name).name != name or not name.endswith(".json") or len(raw) > 1_000_000:
            raise HarnessError("Only bounded, basename JSON attachments are allowed.")
        parts.append({
            "type": "input_file", "filename": name,
            "file_data": "data:application/json;base64," + base64.b64encode(raw).decode("ascii"),
        })
    return [{"role": "user", "content": parts}]


class FileClient(RecordingClient):
    attachments: dict[str, bytes]

    def _request(self, path, body=None, **kwargs):
        if body is not None and path == self._protocol_path + "/responses" and self.attachments:
            body = dict(body, input=content(body["input"], self.attachments))
        return super()._request(path, body, **kwargs)








def render_role(role: str, endpoint: str, model: str, name: str, versions: dict, output: Path) -> None:
    if output.exists():
        raise FileExistsError(output)
    if role not in {"investigator", "challenger", "self-review"}:
        raise HarnessError("Unknown incident-review role.")
    HarnessClient(endpoint, "local-configuration", name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", name) or not model.strip():
        raise HarnessError("Require explicit agent name and existing model deployment.")
    source = json.loads((ROOT / f"agents/incident-review/{role}.azure.yaml").read_text())
    agent = source["services"][f"incident-review-{role}"]
    source["services"]["ai-project"]["endpoint"] = endpoint
    agent["name"] = name
    agent["model"] = model
    if set(versions) != {s["name"] for s in agent["skills"]}:
        raise HarnessError("Render requires exactly the three incident Skill versions.")
    if any(not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]*", version)
           for version in versions.values()):
        raise HarnessError("Require positive immutable Skill versions.")
    for skill in agent["skills"]:
        skill["version"] = versions[skill["name"]]
    if "__" in json.dumps(source):
        raise HarnessError("Unresolved role manifest placeholders.")
    output.parent.mkdir(parents=True, exist_ok=True)
    save(output, source)


def fixture_payload(fixture: dict) -> bytes:
    if (fixture.get("fixture_kind") != "deliberately_flawed_proposal_not_model_output"
            or fixture.get("evaluator_only") is not True):
        raise HarnessError("Require the explicitly classified evaluator fixture.")
    diagnosis = fixture["diagnosis"]
    contract(diagnosis, "diagnosis")
    raw = (json.dumps(diagnosis, indent=2) + "\n").encode("utf-8")
    for marker in ["expected_objections", "fixture", "deliberately", "scoring_boundary"]:
        if marker in raw.decode().lower():
            raise HarnessError("Fixture diagnosis discloses evaluator/control identity.")
    return raw


def fixture_control(args, challenger: FileClient, verified: dict, archive: Path, manifest: Path) -> None:
    fixture_path = ROOT / "agents/incident-review/faulty-diagnosis.json"
    fixture = json.loads(fixture_path.read_text())
    raw = fixture_payload(fixture)
    validate_diagnosis(json.loads(raw), archive, verified["case_id"])
    proposal = args.output_dir / "proposal"
    proposal.mkdir()
    diagnosis = proposal / "diagnosis.json"
    diagnosis.write_bytes(raw)
    save(args.output_dir / "fixture-provenance.json", {
        "origin": "human-authored deliberately flawed proposal; NOT a model result",
        "fixture_sha256": sha(fixture_path), "agent_facing_diagnosis_sha256": sha(diagnosis),
        "evaluator_only_fields_excluded": True,
        "expected_objections_sent": False,
    })
    independent = stage(challenger, args.output_dir / "C0", INDEPENDENT_TASK, {}, verified)
    validate_artifacts(independent, archive, verified["case_id"])
    own = args.output_dir / "C0/downloads/diagnosis.json"
    binding = args.output_dir / "binding.json"
    freeze(own, diagnosis, manifest, archive, binding)
    attachments = {
        "peer-diagnosis.json": raw,
        "independent-assessment.json": own.read_bytes(),
        "review-binding.json": binding.read_bytes(),
        "review-contracts.json": (ROOT / "agents/incident-review/schemas.json").read_bytes(),
    }
    paths = stage(challenger, args.output_dir / "C1", CRITIQUE_TASK, attachments, verified)
    if len(paths) != 2 or {p.name for p in paths} != {"critique.json", "report.md"}:
        raise HarnessError("Expected exactly critique.json and report.md.")
    critique = args.output_dir / "C1/downloads/critique.json"
    checked = validate_critique(critique, binding, own, diagnosis, manifest, archive)
    save(args.output_dir / "critique-validation.json", checked)
    save(args.output_dir / "result.json", {
        "status": "completed_mechanical_checks", "mode": "faulty-proposal-control",
        "invocations": 2, "file_handoffs_verified": ["C1"], "critique": checked,
        "proposal_origin": "human-authored fixture, not investigator model output",
        "semantic_outcome": "manual_expected-objection_scoring_required",
    })


def stage(
    client: FileClient, folder: Path, prompt: str, attachments: dict[str, bytes],
    verification: dict, conversation_id: str | None = None,
    skill_load_evidence: list[dict] | None = None,
) -> list[Path]:
    folder.mkdir()
    client.evidence_dir = folder
    client.attachments = attachments
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in attachments.items()}
    save(folder / "handoff.json", {"attachments": hashes, "transport": "Responses input_file/file_data"})
    command = hash_command(hashes) if hashes else None
    if command:
        prompt += (
            "\nAttached files must be present as actual local files. Before critique/revision, "
            "execute this exact standalone command unchanged. If materialization failed, stop; "
            "do not reconstruct attachment text from the prompt or report success. Then read "
            "the verified JSON files for your task. This command checks file bytes, not your conclusions.\n"
            + command
        )
    (folder / "prompt.txt").write_text(prompt)
    conversation = conversation_id or client.create_conversation()
    save(folder / "conversation.json", {"id": conversation})
    try:
        response = client.invoke(prompt, conversation_id=conversation)
    except ResponseError as error:
        save(folder / "response.json", error.response)
        raise
    save(folder / "response.json", response)
    save(folder / "agent-binding.json", {"name": client.agent_name, "version": client.agent_version})
    returned = response.get("conversation")
    if (returned.get("id") if isinstance(returned, dict) else returned) != conversation:
        raise HarnessError("Wrong response conversation.")
    if skill_load_evidence:
        # Reuse only initial Skill loads; this stage must supply its own verifier execution.
        validate_execution(dict(response, output=skill_load_evidence + response["output"]), verification)
        save(folder / "initial-skill-load-evidence.json", skill_load_evidence)
    else:
        validate_execution(response, verification)
    if command:
        verify_consumption(response, command, hashes)
        save(folder / "receiver-hashes.json", hashes)
    save(folder / "metrics.json", summarize(response))
    (folder / "answer.txt").write_text(final_text(response))
    return client.download_outputs(response, folder / "downloads")


def initial_skill_loads(response: dict) -> list[dict]:
    calls = {i["call_id"] for i in response["output"]
             if i["type"] == "function_call" and i.get("name") == "skill"}
    return [i for i in response["output"]
            if i["type"] in {"function_call", "function_call_output"} and i.get("call_id") in calls]


def self_review_control(
    args, client: FileClient, verified: dict, archive: Path, manifest: Path,
) -> None:
    common = (
        "Use verified case-001 telemetry. Load all three telemetry Skills initially and run "
        "the exact standalone input verifier in every stage. Use no network or installations. "
        "Return diagnosis.json/report.md under the investigation Skill contract with exact "
        "evidence cells, alternatives, uncertainty and advisory-only mitigation."
    )
    initial = stage(client, args.output_dir / "I0", "INITIAL stage. " + common, {}, verified)
    validate_artifacts(initial, archive, verified["case_id"])
    original = args.output_dir / "I0/downloads/diagnosis.json"
    response = json.loads((args.output_dir / "I0/response.json").read_text())
    conversation = json.loads((args.output_dir / "I0/conversation.json").read_text())["id"]
    loads = initial_skill_loads(response)
    self_checked = stage(
        client, args.output_dir / "S0",
        "SELF-CHECK stage in the same conversation: critically re-examine your INITIAL diagnosis "
        "for missed evidence and counterexamples. You are not an independent reviewer. Read "
        "the attached exact initial artifact. " + common,
        {"self-initial-diagnosis.json": original.read_bytes()}, verified, conversation, loads,
    )
    validate_artifacts(self_checked, archive, verified["case_id"])
    own = args.output_dir / "S0/downloads/diagnosis.json"
    binding = args.output_dir / "binding.json"
    freeze(own, original, manifest, archive, binding)
    paths = stage(
        client, args.output_dir / "S1",
        "SELF-CRITIQUE: both assessments are your own outputs in this conversation. "
        "The legacy independent_sha256 field binds your self-check, not an independent peer. "
        + CRITIQUE_TASK.replace("independent-assessment.json", "self-check-assessment.json")
                       .replace("peer-diagnosis.json", "self-initial-diagnosis.json"),
        {
            "self-initial-diagnosis.json": original.read_bytes(),
            "self-check-assessment.json": own.read_bytes(),
            "review-binding.json": binding.read_bytes(),
            "review-contracts.json": (ROOT / "agents/incident-review/schemas.json").read_bytes(),
        }, verified, conversation, loads,
    )
    if len(paths) != 2 or {p.name for p in paths} != {"critique.json", "report.md"}:
        raise HarnessError("Self-critique must deliver exactly critique.json/report.md.")
    critique = args.output_dir / "S1/downloads/critique.json"
    checked = validate_critique(critique, binding, own, original, manifest, archive)
    save(args.output_dir / "critique-validation.json", dict(
        checked, independence="not_independent; same agent and continuous conversation",
    ))
    final = stage(
        client, args.output_dir / "I1",
        "FINAL REVISION, exactly once. Use your initial diagnosis, self-check and self-critique. "
        "Recheck every objection against telemetry; retain valid claims and explicitly explain "
        "which findings you accept or reject. Do not treat your own agreement as proof. " + common,
        {"self-initial-diagnosis.json": original.read_bytes(), "peer-critique.json": critique.read_bytes(),
         "review-contracts.json": (ROOT / "agents/incident-review/schemas.json").read_bytes()},
        verified, conversation, loads,
    )
    result = validate_artifacts(final, archive, verified["case_id"])
    save(args.output_dir / "result.json", {
        "status": "completed_mechanical_checks", "mode": "self-review-control", "invocations": 4,
        "conversation_count": 1, "independent_review": False,
        "file_handoffs_verified": ["S0", "S1", "I1"],
        "critique": dict(checked, independence="not_independent; self-review with history"),
        "revision": result, "semantic_outcome": "manual_comparison_required",
    })






def run(args) -> None:
    if args.output_dir.exists():
        raise HarnessError("Review batch exists; refuse duplicate paid workflow.")
    if args.mode not in {"natural", "faulty-proposal-control", "self-review-control"}:
        raise HarnessError("Unknown review mode.")
    for version in [args.challenger_version, args.investigator_version if args.mode == "natural" else "1"]:
        if not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]*", version):
            raise HarnessError("Require explicitly pinned positive agent versions.")
    verified = verify_prepared("incident", args.prepared_dir)
    challenger = FileClient(args.project_endpoint, args.subscription, args.challenger,
                            agent_version=args.challenger_version, timeout=900)
    agents = {"challenger": selected(challenger, args.challenger_manifest)}
    investigator = None
    if args.mode == "natural":
        investigator = FileClient(args.project_endpoint, args.subscription, args.investigator,
                                  agent_version=args.investigator_version, timeout=900)
        agents["investigator"] = selected(investigator, args.investigator_manifest)
    if args.mode == "natural" and args.investigator == args.challenger:
        raise HarnessError("Require two distinct deployed agents.")
    if verified["case_id"] != "case-001":
        raise HarnessError("This bounded live pilot is approved for case-001 only.")
    if not args.publication:
        raise HarnessError("Require verified publication record.")
    published = json.loads(args.publication.read_text())
    if (published.get("status") != "verified"
            or published["project_endpoint"] != args.project_endpoint
            or published["package_sha256"] != package_hashes(args.prepared_dir / "packages")
            or published["input_verification"] != verified):
        raise HarnessError("Published telemetry differs from local verified inputs.")
    for version in agents.values():
        skills = version["definition"]["skills"]
        if len(skills) != 3 or {s["name"]: s["version"] for s in skills} != published["versions"]:
            raise HarnessError("Role does not pin the verified telemetry publication.")
    if (args.mode == "natural" and agents["investigator"]["definition"]["skills"]
            != agents["challenger"]["definition"]["skills"]):
        raise HarnessError("Roles must pin identical telemetry Skills.")
    if (args.mode == "natural" and agents["investigator"]["definition"]["model"]
            != agents["challenger"]["definition"]["model"]):
        raise HarnessError("Roles must use the same model for this controlled comparison.")
    args.output_dir.mkdir(parents=True)
    snapshot = {}
    sources = [
        ROOT / "scripts/run_incident_review.py", ROOT / "incident_review.py", ROOT / "harness_client.py",
        ROOT / "retail_support.py", ROOT / "incident_investigation.py",
        ROOT / "scripts/publish_incident_skills.py",
        *sorted((ROOT / "agents/incident-review").glob("*")),
    ]
    for file in sources:
        if file.is_file():
            snapshot[file.relative_to(ROOT).as_posix()] = sha(file)
    save(args.output_dir / "source-snapshot.json", {
        "hashes": snapshot,
        "source_commit": None,
        "source_provenance": "Source hashes recorded; owner-controlled Git export verified separately.",
        "agents": agents, "mode": args.mode,
        "invocation_budget": 2 if args.mode == "faulty-proposal-control" else 4,
        "retries": 0,
    })
    archive = args.prepared_dir / "original-input.zip"
    manifest = args.prepared_dir / "packages/incident-investigation-telemetry/input-manifest.json"
    try:
        if args.mode == "faulty-proposal-control":
            fixture_control(args, challenger, verified, archive, manifest)
            return
        if args.mode == "self-review-control":
            self_review_control(args, challenger, verified, archive, manifest)
            return
        if investigator is None:
            raise HarnessError("Natural workflow requires investigator.")
        common = ("Use the verified case-001 telemetry and all three Skills. Run the standalone "
                  "input verifier. Upload exactly diagnosis.json and report.md. Follow the "
                  "investigation Skill schema, exact cells, alternatives, uncertainty, "
                  "falsifiable test and advisory-only mitigation. No browsing or installs. ")
        first = stage(investigator, args.output_dir / "I0", "INITIAL stage. " + common, {}, verified)
        validate_artifacts(first, archive, verified["case_id"])
        independent = stage(
            challenger, args.output_dir / "C0",
            "INDEPENDENT stage. You have no peer diagnosis. " + common, {}, verified,
        )
        validate_artifacts(independent, archive, verified["case_id"])
        diagnosis = args.output_dir / "I0/downloads/diagnosis.json"
        own = args.output_dir / "C0/downloads/diagnosis.json"
        binding = args.output_dir / "binding.json"
        freeze(own, diagnosis, manifest, archive, binding)
        files = {
            "peer-diagnosis.json": diagnosis.read_bytes(),
            "independent-assessment.json": own.read_bytes(),
            "review-binding.json": binding.read_bytes(),
            "review-contracts.json": (ROOT / "agents/incident-review/schemas.json").read_bytes(),
        }
        critique_paths = stage(
            challenger, args.output_dir / "C1",
            CRITIQUE_TASK,
            files, verified,
        )
        if {p.name for p in critique_paths} != {"critique.json", "report.md"} or len(critique_paths) != 2:
            raise HarnessError("Expected exactly critique.json and report.md.")
        critique = args.output_dir / "C1/downloads/critique.json"
        check = validate_critique(critique, binding, own, diagnosis, manifest, archive)
        save(args.output_dir / "critique-validation.json", check)
        revision_files = {"peer-diagnosis.json": diagnosis.read_bytes(),
                          "peer-critique.json": critique.read_bytes(),
                          "review-contracts.json": files["review-contracts.json"]}
        revised = stage(
            investigator, args.output_dir / "I1",
            "REVISION stage, exactly one permitted revision. Read and verify the actual attached "
            "original diagnosis and critique. Recheck cited telemetry yourself. Correct supported "
            "errors, reject unsupported criticism, preserve justified uncertainty. Explain accepted "
            "and rejected objections in report.md. " + common,
            revision_files, verified,
        )
        final = validate_artifacts(revised, archive, verified["case_id"])
        save(args.output_dir / "result.json", {
            "status": "completed_mechanical_checks", "invocations": 4,
            "file_handoffs_verified": ["C1", "I1"], "critique": check, "revision": final,
            "semantic_outcome": "manual_review_required",
        })
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        save(args.output_dir / "failure.json", {"error": str(error), "retried": False})
        raise


def audit(args) -> None:
    run_dir = args.audit_run_dir
    snapshot = json.loads((run_dir / "source-snapshot.json").read_text())
    if not (run_dir / "result.json").exists():
        raise HarnessError("A complete workflow result is required for this audit.")
    for name, wanted in snapshot["hashes"].items():
        if sha(ROOT / name) != wanted:
            raise HarnessError("Source changed since the original workflow.")
    verified = verify_prepared("incident", args.prepared_dir)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    checked = []
    for folder in sorted(run_dir.iterdir()):
        if not folder.is_dir() or not (folder / "response.json").exists():
            continue
        binding = json.loads((folder / "agent-binding.json").read_text())
        client = FileClient(args.project_endpoint, args.subscription, binding["name"],
                            agent_version=binding["version"], timeout=60)
        saved = json.loads((folder / "response.json").read_text())
        request = json.loads((folder / "request.json").read_text())
        original_agent = next((agent for agent in snapshot["agents"].values()
                               if agent["version"] == binding["version"]
                               and agent.get("name") == binding["name"]), None)
        if original_agent is None:
            raise HarnessError("Audited agent is not bound to the original role definition.")
        response = client.get_response(saved["id"])
        target = args.output_dir / folder.name
        target.mkdir()
        save(target / "response.json", response)
        expected = {"type": "agent_reference", **binding}
        conversation = response.get("conversation")
        conversation = conversation.get("id") if isinstance(conversation, dict) else conversation
        if (response.get("agent_reference") != expected or request["agent_reference"] != expected
                or response.get("model") != original_agent["definition"]["model"]
                or request["conversation"] != conversation
                or normalized_output(saved["output"]) != normalized_output(response["output"])):
            raise HarnessError("Audited response identity/content differs.")
        hashes = json.loads((folder / "handoff.json").read_text())["attachments"]
        if hashes:
            verify_consumption(response, hash_command(hashes), hashes)
            parts = request["input"][0]["content"]
            attached = {part["filename"]: hashlib.sha256(base64.b64decode(
                part["file_data"].split(",", 1)[1], validate=True)).hexdigest()
                        for part in parts if part["type"] == "input_file"}
            if attached != hashes or parts[0]["text"] != (folder / "prompt.txt").read_text():
                raise HarnessError("Audited handoff request differs.")
        elif request["input"] != (folder / "prompt.txt").read_text():
            raise HarnessError("Audited task differs.")
        loads = json.loads((folder / "initial-skill-load-evidence.json").read_text()) if (
            folder / "initial-skill-load-evidence.json").exists() else []
        validate_execution(dict(response, output=loads + response["output"]), verified)
        paths = client.download_outputs(response, target / "downloads")
        original_names = {path.name for path in (folder / "downloads").iterdir() if path.is_file()}
        if {path.name for path in paths} != original_names:
            raise HarnessError("Audited artifact set differs.")
        for path in paths:
            if path.read_bytes() != (folder / "downloads" / path.name).read_bytes():
                raise HarnessError("Audited artifact bytes differ.")
        checked.append({"stage": folder.name, "response_id": response["id"]})
    expected_count = 2 if snapshot["mode"] == "faulty-proposal-control" else 4
    if len(checked) != expected_count:
        raise HarnessError("Incomplete audited stage coverage.")
    save(args.output_dir / "audit.json", {"status": "reconciled", "stages": checked, "paid_invocations": 0,
                                        "semantic_quality": "manual_review_required"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["project-endpoint", "subscription", "challenger", "challenger-version"]:
        parser.add_argument("--" + name, required=True)
    for name in ["investigator", "investigator-version"]:
        parser.add_argument("--" + name)
    for name in ["prepared-dir", "output-dir", "challenger-manifest"]:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--investigator-manifest", type=Path)
    parser.add_argument("--mode", choices=["natural", "faulty-proposal-control", "self-review-control"],
                        default="natural")
    parser.add_argument("--allow-four-paid-invocations", action="store_true")
    parser.add_argument("--allow-two-paid-invocations", action="store_true")
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--model-deployment")
    parser.add_argument("--audit-run-dir", type=Path)
    args = parser.parse_args()
    try:
        if args.audit_run_dir:
            audit(args)
            return 0
        if args.render_only:
            if args.mode == "self-review-control":
                if not args.model_deployment:
                    raise HarnessError("Rendering self-review requires a model deployment.")
                versions = json.loads(args.publication.read_text())["versions"]
                render_role("self-review", args.project_endpoint, args.model_deployment,
                            args.challenger, versions, args.challenger_manifest)
                return 0
            if not args.model_deployment or not args.investigator or not args.investigator_manifest:
                raise HarnessError("Manifest rendering requires model name and both role names/paths.")
            versions = json.loads(args.publication.read_text())["versions"]
            for role, name, path in [
                ("investigator", args.investigator, args.investigator_manifest),
                ("challenger", args.challenger, args.challenger_manifest),
            ]:
                render_role(role, args.project_endpoint, args.model_deployment, name, versions, path)
            return 0
        if args.mode == "natural":
            if (not args.allow_four_paid_invocations or not args.investigator
                    or not args.investigator_version or not args.investigator_manifest):
                raise HarnessError("Require both roles and explicit four-call opt-in.")
        elif args.mode == "self-review-control":
            if not args.allow_four_paid_invocations:
                raise HarnessError("Explicit four-call self-review budget opt-in required.")
        elif not args.allow_two_paid_invocations:
            raise HarnessError("Explicit two-call control budget opt-in required.")
        run(args)
        return 0
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
