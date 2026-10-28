"""Complete explicitly approved, never-submitted tasks without retrying model requests."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from harness_client import HarnessClient, HarnessError
from retail_support import save
from scripts import compare_models as study

GATE_ERROR = "No native gate script completion marker and matching output observed."
DELIVERY_ERRORS = {
    "No structured file references; a prose link is not upload evidence.",
    "Coding task did not deliver exactly its requested file.",
}
SCRIPT = Path(__file__).resolve()


def unchanged_protocol(state: dict, original_source: Path) -> None:
    current = study.sources()
    if current.keys() != state["source_sha256"].keys():
        raise HarnessError("The continuation source inventory differs from the original protocol.")
    for name, digest in state["source_sha256"].items():
        old = original_source / name
        if study.sha(old) != digest:
            raise HarnessError("Original source snapshot no longer matches its frozen fingerprints.")
        if name not in {"scripts/compare_models.py", "harness_client.py"} and current[name] != digest:
            raise HarnessError("Only the readiness-checker and explicit transport-error corrections may differ.")

    def without_gate(path: Path) -> str:
        tree = ast.parse(path.read_text())
        tree.body = [node for node in tree.body
                     if not (isinstance(node, ast.FunctionDef) and node.name == "verify_gate_execution")]
        return ast.dump(tree)

    if without_gate(original_source / "scripts/compare_models.py") != without_gate(study.ROOT / "scripts/compare_models.py"):
        raise HarnessError("Controller behavior changed beyond the readiness-checker correction.")

    def without_transport_catch(path: Path) -> str:
        tree = ast.parse(path.read_text())
        tree.body = [node for node in tree.body if not (
            isinstance(node, ast.ImportFrom) and node.module == "http.client"
            and len(node.names) == 1 and node.names[0].name == "HTTPException")]
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef) or function.name != "_request":
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Tuple):
                    if [ast.unparse(item) for item in node.type.elts] == [
                            "urllib.error.URLError", "TimeoutError", "HTTPException"]:
                        node.type.elts.pop()
        return ast.dump(tree)

    if without_transport_catch(original_source / "harness_client.py") != without_transport_catch(study.ROOT / "harness_client.py"):
        raise HarnessError("Shared client changed beyond non-retrying HTTP transport-error handling.")


def preflight_only(folder: Path) -> bool:
    if any((folder / name).exists() for name in ["request.json", "response.json", "conversation.json"]):
        return False
    failure = folder / "worker-failure.json"
    events = folder / "http.jsonl"
    if not failure.exists() or not events.exists():
        return False
    error = json.loads(failure.read_text()).get("error", "")
    trace = [json.loads(line) for line in events.read_text().splitlines()]
    return ("/conversations:" in error and "Temporary failure in name resolution" in error
            and any(event["event"] == "request_start" and event["method"] == "POST"
                    and event["path"].endswith("/conversations") for event in trace)
            and not any("/responses" in event["path"] for event in trace))


def continuation_plan(directory: Path, state: dict, model_id: str, *,
                      all_unsubmitted: bool = False, previous: list[Path] | None = None) -> tuple[list[dict], Path]:
    result = json.loads((directory / "full/result.json").read_text())
    if result["status"] != "schedule_finished" or (directory / "full/failure.json").exists():
        raise HarnessError("Wait for the original schedule to finish before planning a continuation.")
    if state["registered_full_schedule"] != study.full_schedule(state["study"]):
        raise HarnessError("Original schedule differs from the fixed battery.")
    if model_id not in {m["id"] for m in state["study"]["models"]}:
        raise HarnessError("Model is not in the original roster.")
    attempted, gates = set(), []
    folders = [folder for origin in [directory, *(previous or [])]
               for folder in sorted((origin / "full").iterdir())]
    for folder in folders:
        if not folder.is_dir() or not (folder / "job.json").exists():
            continue
        job = json.loads((folder / "job.json").read_text())
        if job["case_id"] == "gate":
            if job["model_id"] == model_id:
                gates.append(folder)
            continue
        repeat = re.fullmatch(r"\d+-" + re.escape(job["model_id"]) + "-"
                              + re.escape(job["case_id"]) + r"-r([1-3])", folder.name)
        if repeat is None:
            raise HarnessError("Ambiguous original job directory; do not risk resubmitting a request.")
        if all_unsubmitted and preflight_only(folder):
            continue
        attempted.add((job["model_id"], job["case_id"], int(repeat[1])))
    if len(gates) != 1:
        raise HarnessError("Require exactly one original readiness attempt for the selected model.")
    failure = gates[0] / "worker-failure.json"
    gate_error = json.loads(failure.read_text()).get("error") if failure.exists() else None
    passed = any(record["model"] == model_id and record["case"] == "gate"
                 and record["status"] == "completed_correct" for record in result.get("records", []))
    if gate_error != GATE_ERROR and not (all_unsubmitted and passed):
        raise HarnessError("Require successful readiness or an independently verified checker false negative.")
    jobs = [job for job in state["registered_full_schedule"]
            if job["model"] == model_id and (job["model"], job["case"], job["repeat"]) not in attempted]
    if not jobs and not all_unsubmitted:
        raise HarnessError("No unsubmitted scored jobs remain; never retry original attempts.")
    return jobs, gates[0]


def recheck_gate(client: HarnessClient, state: dict, gate_folder: Path, version: dict,
                 output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    saved = json.loads((gate_folder / "response.json").read_text())
    live = client.get_response(saved["id"])
    save(output / "response.json", live)
    expected_agent = {"type": "agent_reference", "name": state["agent_name"], "version": version["version"]}
    prompt, files = study.task_payload(state, "gate")
    request = json.loads((gate_folder / "request.json").read_text())
    conversation = live.get("conversation")
    conversation = conversation.get("id") if isinstance(conversation, dict) else conversation
    if (live["status"] != "completed" or live.get("model") != version["model"]
            or live.get("agent_reference") != expected_agent
            or study.normalized_output(live["output"]) != study.normalized_output(saved["output"])
            or request["input"] != study.file_input(prompt, files)
            or request["agent_reference"] != expected_agent or request["conversation"] != conversation):
        raise HarnessError("Readiness response/request differs from its original model, task or conversation.")
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    study.verify_consumption(live, study.hash_command(hashes), hashes, allow_shell_wrappers=True)
    paths = client.download_outputs(live, output / "downloads")
    if {p.name for p in paths} != {"gate-result.json"}:
        raise HarnessError("Readiness did not deliver exactly its requested file.")
    path = paths[0]
    if path.read_bytes() != (gate_folder / "downloads" / path.name).read_bytes():
        raise HarnessError("Readiness artifact changed since the original download.")
    gate = json.loads(path.read_text())
    if (set(gate) != {"nonce", "value", "python"} or gate["nonce"] != state["gate_nonce"]
            or type(gate["value"]) is not int or gate["value"] != 5050
            or not isinstance(gate["python"], str)):
        raise HarnessError("Readiness artifact contents mismatch.")
    evidence = study.verify_gate_execution(live, gate)
    evidence.update(response_id=live["id"], artifact_sha256=study.sha(path),
                    original_directory=str(gate_folder.resolve()), paid_invocations=0,
                    original_outcome_preserved=True)
    save(output / "review.json", evidence)
    return evidence


def combine_reports(original: dict, continuation: dict, state: dict, review: dict,
                    late: dict | list[dict] | None = None) -> dict:
    rows = [dict(row, phase=row.get("phase", phase)) for phase, report in
            [("original", original), ("continuation", continuation)] for row in report["records"]]
    preflight_names = set(state["continuation"].get("preflight_only_directories", []))
    preflight_rows = [row for row in rows if row["phase"] == "original" and row.get("directory") in preflight_names]
    rows = [row for row in rows if row not in preflight_rows]
    reconciliations = late if isinstance(late, list) else [late] if late else []
    for row in rows:
        interrupted = "transport failed" in (row.get("worker_error") or "").lower()
        recovery = next((item for item in reconciliations
                         if (row["model"], row["case"], row.get("repeat"))
                         == (item["model"], item["case"], item["repeat"])), None)
        recovered = recovery is not None
        delivery_failed = row.get("worker_error") in DELIVERY_ERRORS
        row["verified_after_client_timeout"] = recovered
        row["quality_correct"] = (
            recovery["grading"]["correct"] if recovery is not None else row["correct"]
            if isinstance(row["correct"], bool) else False if delivery_failed else None)
        row["quality_evaluated"] = not row["readiness_only"] and isinstance(row["quality_correct"], bool)
        completed = row["status"] in {"completed_correct", "completed_incorrect"} or delivery_failed
        row["timing_eligible"] = not row["readiness_only"] and completed and not interrupted and not recovered
        row["late_usage"] = recovery.get("usage") if recovery is not None else None
    scored = [row for row in rows if not row["readiness_only"]]
    keys = [(row["model"], row["case"], row["repeat"]) for row in scored]
    expected = {(job["model"], job["case"], job["repeat"]) for job in state["registered_full_schedule"]}
    if len(keys) != len(set(keys)) or not set(keys) <= expected:
        raise HarnessError("Reports overlap or contain unregistered scored jobs; refusing to pool them.")
    models = {}
    for model in state["study"]["models"]:
        selected = [row for row in rows if row["model"] == model["id"]]
        attempts = [row for row in selected if not row["readiness_only"]]
        models[model["id"]] = {
            "gate_outcomes": [row["status"] for row in selected if row["readiness_only"]],
            "readiness_rechecked": model["id"] in state["continuation"].get(
                "models", [state["continuation"].get("model")]),
            "scored_attempted": len(attempts), "scored_correct": sum(row["correct"] is True for row in attempts),
            "quality_evaluated": sum(row["quality_evaluated"] for row in attempts),
            "quality_correct": sum(row["quality_evaluated"] and row["quality_correct"] for row in attempts),
            "outcomes": dict(Counter(row["status"] for row in attempts)),
        }
    return {
        "records": rows, "preflight_records": preflight_rows, "models": models,
        "complete_scored_coverage": set(keys) == expected,
        "complete_quality_coverage": set(keys) == expected and all(row["quality_evaluated"] for row in scored),
        "scored_attempted": len(scored), "registered_scored_jobs": len(expected),
        "request_attempts": len(rows), "client_resubmissions": 0, "prior_results_pooled": False,
        "continuation": state["continuation"], "readiness_review": review,
        "read_only_timeout_reconciliation": late,
        "original_source_sha256": original["source_sha256"],
        "continuation_source_sha256": continuation["source_sha256"],
        "limitations": original["limitations"] + [
            "Original outcomes retained; all selected readiness responses/files rechecked read-only.",
            "Preflight-only DNS failures are retained separately, not counted as model invocations.",
            "Client transport interruptions are not model-quality failures and are excluded from timing.",
            "Late recovered artifacts can establish correctness without rewriting original client failures.",
            "Only never-submitted tasks ran later; continuation timing is a separate phase.",
        ],
    }


def reconcile_timeout(path: Path, original_dir: Path, state: dict, versions: dict, output: Path) -> dict:
    info = json.loads(path.read_text())
    folder = Path(info["original_directory"])
    if (folder.parent.resolve() != (original_dir / "full").resolve()
            or info["paid_invocations"] != 0 or info["resubmitted"] is not False
            or not (folder / "request.json").exists()):
        raise HarnessError("Timeout reconciliation does not identify an original submitted request.")
    job = json.loads((folder / "job.json").read_text())
    model, case_id = job["model_id"], job["case_id"]
    version = versions[model]
    repeat = re.fullmatch(r"\d+-" + re.escape(model) + "-" + re.escape(case_id) + r"-r([1-3])", folder.name)
    if repeat is None or case_id not in {"implement", "debug"}:
        raise HarnessError("Timeout artifact reconciliation requires an original code task.")
    output.mkdir(parents=True, exist_ok=False)
    client = HarnessClient(state["project_endpoint"], state["subscription"], state["agent_name"],
                           agent_version=version["version"], timeout=60)
    response = client.get_response(info["response_id"])
    save(output / "response.json", response)
    request = json.loads((folder / "request.json").read_text())
    prompt, files = study.task_payload(state, case_id)
    agent = {"type": "agent_reference", "name": state["agent_name"], "version": version["version"]}
    conversation = response.get("conversation")
    conversation = conversation.get("id") if isinstance(conversation, dict) else conversation
    if (response["status"] != "completed" or response.get("model") != version["model"]
            or response.get("agent_reference") != agent or request["agent_reference"] != agent
            or request["input"] != study.file_input(prompt, files) or request["conversation"] != conversation):
        raise HarnessError("Recovered response does not match the original submitted task.")
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    study.verify_consumption(response, study.hash_command(hashes), hashes, allow_shell_wrappers=True)
    paths = client.download_outputs(response, output / "downloads")
    case = next(case for case in state["study"]["cases"] if case["id"] == case_id)
    if ({p.name for p in paths} != {case["output"]["filename"]}
            or study.sha(paths[0]) != info["artifact_sha256"]):
        raise HarnessError("Recovered actual artifact differs from its read-only inspection.")
    grading = study.grade_code(case_id, paths[0], state["python_image"], output / "grading")
    result = {"model": model, "case": case_id, "repeat": int(repeat[1]), "grading": grading,
              "response_id": response["id"], "original_failure_preserved": True,
              "paid_invocations": 0, "source_reconciliation_sha256": study.sha(path),
              "usage": response.get("usage")}
    save(output / "review.json", result)
    return result


def complete(args) -> None:
    if not args.allow_paid_invocations:
        raise HarnessError("Explicit paid invocation approval is required.")
    all_unsubmitted = getattr(args, "all_unsubmitted", False)
    previous = getattr(args, "previous_continuation", []) or []
    previous_sources = getattr(args, "previous_source", []) or []
    claims = list(args.study_dir.parent.glob(f"{args.study_dir.name}-continuation-*.json"))
    claimed_outputs = {Path(json.loads(path.read_text())["output_dir"]).resolve() for path in claims}
    if (len(previous) != len(previous_sources) or len(previous) != len(set(previous))
            or claimed_outputs != {path.resolve() for path in previous}):
        raise HarnessError("A continuation was already claimed; supply its complete verified history, not a resubmission.")
    claim = args.study_dir.parent / (
        f"{args.study_dir.name}-continuation-phase-{len(previous) + 1}.json")
    original = json.loads((args.study_dir / "state.json").read_text())
    unchanged_protocol(original, args.original_source)
    previous_reports = []
    previous_evidence = []
    for index, (directory, source) in enumerate(zip(previous, previous_sources), 1):
        prior = json.loads((directory / "state.json").read_text())
        unchanged_protocol(prior, source)
        if (prior["continuation"]["original_state_sha256"] != study.sha(args.study_dir / "state.json")
                or any(prior[key] != value for key, value in original.items()
                       if key not in {"source_sha256", "approved_max_invocations"})
                or not any((directory / "full" / name).exists() for name in ["failure.json", "result.json"])):
            raise HarnessError("Previous continuation is not a finished phase of this exact original study.")
        prior_report = json.loads((directory / "report/results.json").read_text())
        prior_audit = json.loads((directory / "audit/audit.json").read_text())
        if (prior_report["source_sha256"] != prior["source_sha256"]
                or prior_audit["status"] != "reconciled" or prior_audit["paid_invocations"] != 0):
            raise HarnessError("Previous continuation requires its own source-bound report and read-only audit.")
        previous_reports.extend(dict(row, phase=f"continuation-{index}") for row in prior_report["records"])
        previous_evidence.append({"directory": str(directory.resolve()),
                                  "state_sha256": study.sha(directory / "state.json"),
                                  "report_sha256": study.sha(directory / "report/results.json"),
                                  "audit_sha256": study.sha(directory / "audit/audit.json")})
    models = [m["id"] for m in original["study"]["models"]] if all_unsubmitted else [args.model]
    planned, gates = [], {}
    for model in models:
        remaining, gate = continuation_plan(args.study_dir, original, model,
                                            all_unsubmitted=all_unsubmitted, previous=previous)
        if remaining:
            planned.extend(remaining)
            gates[model] = gate
    keys = {(job["model"], job["case"], job["repeat"]) for job in planned}
    jobs = [job for job in original["registered_full_schedule"]
            if (job["model"], job["case"], job["repeat"]) in keys]
    if not jobs:
        raise HarnessError("No unsubmitted tasks remain.")
    if args.max_invocations != len(jobs):
        raise HarnessError(f"Confirm exactly {len(jobs)} never-submitted requests.")
    original_report = json.loads((args.original_report / "results.json").read_text())
    if original_report["source_sha256"] != original["source_sha256"]:
        raise HarnessError("Original report does not match the frozen source fingerprints.")
    audit = json.loads(args.original_audit.read_text())
    if (audit["status"] != "reconciled" or audit["paid_invocations"] != 0
            or any(json.loads((gate / "response.json").read_text())["id"]
                   not in {record["response_id"] for record in audit["records"]} for gate in gates.values())):
        raise HarnessError("Original response/file audit is missing or unresolved.")
    versions = json.loads((args.study_dir / "versions.json").read_text())
    for model in gates:
        version = versions[model]
        client = HarnessClient(original["project_endpoint"], original["subscription"], original["agent_name"],
                               agent_version=version["version"], timeout=60)
        pinned = study.pinned(client, version["version"])
        if pinned["status"] != "active" or pinned["definition"] != version["record"]["definition"]:
            raise HarnessError("Original pinned agent definition is no longer active and unchanged.")
    before = {m["name"]: m for m in json.loads((args.study_dir / "models-before.json").read_text())}
    current = {m["name"]: m for m in study.cli([
        "cognitiveservices", "account", "deployment", "list", "--subscription", original["subscription"],
        "--resource-group", original["resource_group"], "--name", original["account"]])}
    for model in original["study"]["models"]:
        old, new = before[model["deployment"]], current.get(model["deployment"])
        if (new is None or new["properties"]["provisioningState"] != "Succeeded"
                or new["properties"]["model"] != old["properties"]["model"] or new["sku"] != old["sku"]
                or new["properties"].get("versionUpgradeOption") != old["properties"].get("versionUpgradeOption")):
            raise HarnessError("An original model deployment changed; do not combine configurations.")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    review = {}
    for model, gate in gates.items():
        version = versions[model]
        client = HarnessClient(original["project_endpoint"], original["subscription"], original["agent_name"],
                               agent_version=version["version"], timeout=60)
        review[model] = recheck_gate(client, original, gate, version, args.output_dir / "readiness-review" / model)
    late = []
    inspections = getattr(args, "timeout_reconciliation", None) or []
    if isinstance(inspections, Path):
        inspections = [inspections]
    for index, path in enumerate(inspections, 1):
        info = json.loads(path.read_text())
        origin = next((directory for directory in [args.study_dir, *previous]
                       if (directory / "full").resolve() == Path(info["original_directory"]).parent.resolve()), None)
        if origin is None:
            raise HarnessError("Reconciliation is outside the complete declared study history.")
        late.append(reconcile_timeout(path, origin, original, versions,
                                      args.output_dir / "timeout-reconciliation" / str(index)))
    state = dict(original, source_sha256=study.sources(), approved_max_invocations=len(jobs))
    state["continuation"] = {
        "original_study": str(args.study_dir.resolve()), "original_state_sha256": study.sha(args.study_dir / "state.json"),
        "original_report_sha256": study.sha(args.original_report / "results.json"),
        "original_audit_sha256": study.sha(args.original_audit),
        "model": args.model, "models": list(gates), "registered_unsubmitted_jobs": jobs,
        "preflight_only_directories": [p.name for p in (args.study_dir / "full").iterdir()
                                       if p.is_dir() and preflight_only(p)] if all_unsubmitted else [],
        "new_readiness_requests": 0, "runner_sha256": study.sha(SCRIPT),
        "previous_continuations": previous_evidence,
        "reason": "Explicitly approved unsubmitted tasks after infrastructure/checker interruption",
    }
    save(args.output_dir / "state.json", state)
    save(args.output_dir / "versions.json", versions)
    save(claim, {"output_dir": str(args.output_dir.resolve()), "jobs": jobs,
                 "runner_sha256": state["continuation"]["runner_sha256"], "automatic_retry": False})
    full = args.output_dir / "full"
    full.mkdir()
    records = []
    try:
        for number, job in enumerate(jobs, 1):
            study.check_sources(state)
            if study.sha(SCRIPT) != state["continuation"]["runner_sha256"]:
                raise HarnessError("Continuation runner changed during execution.")
            if datetime.now(timezone.utc) >= datetime.fromisoformat(state["launch_cutoff_utc"]):
                raise HarnessError("Original launch cutoff reached; no further requests.")
            folder = full / f"{number:03d}-{job['model']}-{job['case']}-r{job['repeat']}"
            print(f"START continuation {number}/{len(jobs)} {job['model']}/{job['case']} r{job['repeat']}", flush=True)
            record = study.run_job(state, args.output_dir, folder, job["model"], job["case"])
            record["repeat"] = job["repeat"]
            if (record["status"] == "service_or_contract_failure"
                    and (folder / "worker-failure.json").exists() and (folder / "response.json").exists()):
                error = json.loads((folder / "worker-failure.json").read_text()).get("error")
                response = json.loads((folder / "response.json").read_text())
                if error in DELIVERY_ERRORS and response.get("status") == "completed":
                    record["original_worker_outcome"] = record["status"]
                    record.update(status="completed_incorrect",
                                  grading={"correct": False, "contract_ok": False, "detail": error})
                    save(folder / "grading.json", record["grading"])
            save(full / f"record-{number:03d}.json", record)
            records.append(record)
            print(f"RESULT continuation {number}/{len(jobs)}: {record['status']}", flush=True)
            if record["status"] not in {"completed_correct", "completed_incorrect"}:
                raise HarnessError("Continuation operational/evaluation failure; stop without retries.")
        save(full / "result.json", {
            "status": "schedule_finished", "attempted": len(records), "maximum": len(jobs),
            "paused": [], "records": records, "scored_attempted": len(records),
            "complete_scored_coverage": len(records) == len(jobs), "client_retries": 0,
        })
    except (HarnessError, OSError, ValueError, KeyError) as error:
        save(full / "failure.json", {"error": str(error), "records": records, "retried": False})
        raise
    study.audit_study(argparse.Namespace(study_dir=args.output_dir, output_dir=args.output_dir / "audit"))
    study.report(argparse.Namespace(study_dir=args.output_dir, output_dir=args.output_dir / "report"))
    continuation_report = json.loads((args.output_dir / "report/results.json").read_text())
    continuation_report["records"] = previous_reports + [
        dict(row, phase=f"continuation-{len(previous) + 1}") for row in continuation_report["records"]]
    combined = combine_reports(original_report, continuation_report, state, review, late)
    save(args.output_dir / "combined-results.json", combined)
    with (args.output_dir / "combined-runs.csv").open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(
            key for row in combined["records"] for key in row)))
        writer.writeheader()
        writer.writerows({key: json.dumps(value) if isinstance(value, (list, dict)) else value
                          for key, value in row.items()} for row in combined["records"])
    lines = ["# Model comparison results", "",
             "Original run plus an explicitly labeled continuation of never-submitted tasks.",
             "Original failures remain in the raw results. Client-network and checker interruptions",
             "are not model-quality failures. Preflight-only events are retained separately.",
             "Recovered code is checked read-only and independently graded without resubmission.",
             "Interrupted timings are excluded. Continuation timing is a separate phase.", "",
             "| Model | Scored requests | Quality evaluated | Verified output passes |",
             "|-------|-----------------|-------------------|------------------------|"]
    for model, data in combined["models"].items():
        lines.append(f"| {model} | {data['scored_attempted']} | {data['quality_evaluated']} | {data['quality_correct']} |")
    lines += ["", f"Complete scored coverage: {combined['complete_scored_coverage']}.",
              f"Request attempts including original readiness: {combined['request_attempts']}.",
              "See combined-results.json for original/continuation phases, usage and limitations."]
    (args.output_dir / "combined-report.md").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("study-dir", "original-source", "original-report", "original-audit", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--model")
    scope.add_argument("--all-unsubmitted", action="store_true")
    parser.add_argument("--timeout-reconciliation", type=Path, action="append")
    parser.add_argument("--previous-continuation", type=Path, action="append")
    parser.add_argument("--previous-source", type=Path, action="append")
    parser.add_argument("--max-invocations", type=int, required=True)
    parser.add_argument("--allow-paid-invocations", action="store_true")
    args = parser.parse_args(argv)
    try:
        complete(args)
        return 0
    except (HarnessError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
