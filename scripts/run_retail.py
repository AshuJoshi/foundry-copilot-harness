"""Bounded live retail evidence updates, with byte-verified input and checked outputs."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

from harness_client import HarnessClient, HarnessError, JsonObject, ResponseError, final_text
from retail_support import hash_command, selected, verify_consumption, RecordingClient, save, summarize
from scripts.prepare_retail import CONFIG, ROOT, config, sha
from retail_evidence import check_report, metrics, read_rows, references

STAGES = ("stage-1", "stage-2", "stage-3", "stable-control")
MAX_FILE_BYTES = 16_000_000


def content(text: str, attachments: dict[str, bytes]) -> list[dict]:
    parts = [{"type": "input_text", "text": text}]
    if not attachments or sum(map(len, attachments.values())) > MAX_FILE_BYTES:
        raise HarnessError("Require nonempty bounded retail attachments.")
    for name, raw in attachments.items():
        suffix = Path(name).suffix
        if (Path(name).name != name or "\\" in name
                or suffix not in {".csv", ".json", ".txt"} or not raw):
            raise HarnessError("Retail attachment must be a bounded basename CSV/JSON/text file.")
        media = {".csv": "text/csv", ".json": "application/json", ".txt": "text/plain"}[suffix]
        parts.append({"type": "input_file", "filename": name,
                      "file_data": f"data:{media};base64," + base64.b64encode(raw).decode("ascii")})
    return [{"role": "user", "content": parts}]


class RetailClient(RecordingClient):
    attachments: dict[str, bytes]

    def _request(self, path: str, body: JsonObject | None = None, **kwargs) -> bytes:
        if body is not None and path == self._protocol_path + "/responses":
            body = dict(body, input=content(body["input"], self.attachments))
        return super()._request(path, body, **kwargs)


def render(endpoint: str, model: str, name: str, output: Path) -> None:
    HarnessClient(endpoint, "local-configuration", name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", name):
        raise HarnessError("Use an agent name of 1-63 letters, digits, underscores or hyphens.")
    value = json.loads((ROOT / "agents/retail-analysis/azure.yaml").read_text())
    value["services"]["ai-project"]["endpoint"] = endpoint
    agent = value["services"].pop("retail-evidence-agent")
    agent["name"], agent["model"] = name, model
    value["services"][name] = agent
    if not all([endpoint, model, name]) or "__" in json.dumps(value):
        raise HarnessError("Unresolved retail deployment values.")
    output.mkdir(parents=True, exist_ok=False)
    save(output / "azure.yaml", value)


def verify_prepared(prepared: Path) -> dict:
    settings = config()
    record = json.loads((prepared / "preparation.json").read_text())
    if (record.get("status") != "verified"
            or record.get("source_sha256") != settings["archive_sha256"]
            or record.get("workbook_sha256") != settings["workbook_sha256"]
            or record.get("config_sha256") != sha(CONFIG)
            or record.get("preparer_sha256") != sha(ROOT / "scripts/prepare_retail.py")
            or record.get("reference_helper_sha256") != sha(ROOT / "retail_evidence.py")
            or record.get("openpyxl_version") != "3.1.5"
            or record.get("sheet_rows_scanned") != settings["source_sheet_rows"]):
        raise HarnessError("Retail preparation/source provenance mismatch.")
    plan = json.loads((prepared / "evaluator/stage-plan.json").read_text())
    expected_references = json.loads((prepared / "evaluator/references.json").read_text())
    if set(plan) != set(STAGES) or set(expected_references) != set(STAGES):
        raise HarnessError("Require the complete predeclared four-stage plan.")
    prior = {}
    for stage in settings["stages"][:3]:
        folder = prepared / stage["id"]
        manifest = json.loads((folder / "manifest.json").read_text())
        if (manifest != plan[stage["id"]] or manifest != record["stages"][stage["id"]]
                or manifest["evidence_sha256"] != sha(folder / "evidence.csv")
                or manifest["policy_sha256"] != sha(folder / "policy.json")
                or json.loads((folder / "policy.json").read_text()) != settings["metric_policy"]
                or (folder / "DATA-NOTICE.txt").read_bytes()
                != (ROOT / "agents/retail-analysis/DATA-NOTICE.txt").read_bytes()
                or manifest["visible_periods"] != stage["visible_periods"]):
            raise HarnessError("Stage input identity/policy mismatch.")
        rows = read_rows(folder / "evidence.csv")
        current = {r["source_row_id"]: r for r in rows}
        periods = {p["id"]: p for p in settings["periods"]}
        if (len(rows) != manifest["rows"] or set(r["period"] for r in rows) != set(stage["visible_periods"])
                or manifest["available_before"] != periods[stage["visible_periods"][-1]]["end"]
                or any(current.get(k) != v for k, v in prior.items())
                or any(r["country"] != settings["country"]
                       or not periods[r["period"]]["start"] <= r["invoice_date"] < periods[r["period"]]["end"]
                       for r in rows)
                or references(folder / "evidence.csv") != expected_references[stage["id"]]):
            raise HarnessError("Retail visibility, references or immutable earlier rows changed.")
        prior = current
    stable = {"stage_id": "stable-control", "evidence_from": "stage-3/evidence.csv",
              "evidence_sha256": plan["stage-3"]["evidence_sha256"], "rows": plan["stage-3"]["rows"]}
    if (plan["stable-control"] != stable or record["stages"]["stable-control"] != stable
            or expected_references["stable-control"] != expected_references["stage-3"]
            or len(prior) != record["selected_rows"]):
        raise HarnessError("Stable control differs from the exact final snapshot.")
    return plan


def inputs(prepared: Path, stage_id: str, previous: Path | None) -> tuple[dict[str, bytes], dict]:
    folder = prepared / ("stage-3" if stage_id == "stable-control" else stage_id)
    binding = json.loads((folder / "manifest.json").read_text())
    binding["stage_id"] = stage_id
    evidence_name = f"retail-{folder.name}.csv"
    files = {evidence_name: (folder / "evidence.csv").read_bytes(),
             "retail-policy.json": (folder / "policy.json").read_bytes(),
             "DATA-NOTICE.txt": (folder / "DATA-NOTICE.txt").read_bytes()}
    if previous is not None:
        files[previous.name] = previous.read_bytes()
    binding.update(evidence_filename=evidence_name, policy_filename="retail-policy.json",
                   previous_assessment=previous.name if previous else None,
                   assessment_filename=f"assessment-{stage_id}.json",
                   report_filename=f"report-{stage_id}.md")
    files[f"binding-{stage_id}.json"] = (json.dumps(binding, indent=2) + "\n").encode()
    content("Validate bounded inputs", files)
    return files, binding


def task(binding: dict, command: str) -> str:
    fields = {key: "integer count" if type(value) is int else "finite exact Decimal string"
              for key, value in metrics([]).items()}
    example = {
        "schema_version": 1, "stage_id": binding["stage_id"],
        "evidence_sha256": binding["evidence_sha256"],
        "measure": "derived_net_transaction_value_not_profit",
        "metrics": {"periods": {p: fields for p in binding["visible_periods"]}, "cumulative": fields},
        "claims": [{"id": "C1", "kind": "observation",
                    "text": "<bounded claim>",
                    "evidence_refs": [{"source_row_id": "<actual ID>",
                                       "observed_values": {"quantity": "<exact cell>",
                                                           "unit_price": "<exact cell>"}}]}],
        "claim_updates": [], "limitations": ["<actual evidence limits>"],
    }
    text = (
        "Assess this historical retail snapshot for a commercial team. Use only attached "
        "evidence/policy and prior assessment; do not browse, install packages or change systems. "
        "The binding lists the current CSV, visible periods and output filenames. "
        "FIRST execute the following exact hash command, without modification, using the shell. "
        "Stop honestly on failure. Then read the verified binding/policy and previous assessment "
        "(if present), and actually calculate the CSV using Python csv and Decimal with precision 60. "
        "Use paths whose bytes matched verification, not a stale file with a similar name.\n\n"
        + command + "\n\n"
        "Upload exactly TWO files using the project's upload tool: "
        + binding["assessment_filename"] + " and " + binding["report_filename"] + ". "
        "Write each stage's outputs to a separate directory and preserve prior artifacts. "
        "The JSON structure below is a contract illustration, NOT values: replace all metric "
        "placeholders with exact calculated values, covering every listed field/period. "
        "Provide 4-7 useful bounded claims. Each requires exact source-row/cell citations; "
        "aggregate claims also require actual full-snapshot calculation, not cherry-picked rows. "
        "Use kind observation, inference or unknown. Missing data can justify unknowns, not "
        "a causal story. Keep prior claim IDs stable where sensible and give EVERY claim in "
        "the supplied previous assessment exactly one claim_updates entry with prior_claim_id, "
        "disposition (retain/revise/withdraw), reason and evidence_refs in the same citation shape. "
        "Initial claim_updates must be []. For unchanged evidence preserve measured facts; "
        "any changed interpretation needs an explicit reason, not fabricated new events. "
        "Do not confuse cumulative totals with equal-window performance. Negative/cancelled "
        "lines do not prove a cancellation reason or cohort return rate. "
        "The markdown should explain equal-week changes, cancellation/sign evidence, justified "
        "claim revisions, limitations and evidence needed next. No decision or business action runs. "
        "Keep it concise; no private chain-of-thought is requested.\n"
        + json.dumps(example, indent=2)
    )
    return text


def execute_stage(
    client: RetailClient, prepared: Path, folder: Path, stage_id: str,
    conversation: str, previous: Path | None,
) -> None:
    files, binding = inputs(prepared, stage_id, previous)
    expected = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    command = hash_command(expected)
    prompt = task(binding, command)
    client.evidence_dir, client.attachments = folder, files
    save(folder / "inputs.json", {"binding": binding, "sha256": expected})
    (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
    try:
        response = client.invoke(prompt, conversation_id=conversation)
    except ResponseError as error:
        save(folder / "response.json", error.response)
        raise
    save(folder / "response.json", response)
    (folder / "answer.txt").write_text(final_text(response), encoding="utf-8")
    save(folder / "summary.json", summarize(response))
    returned = response.get("conversation")
    if (returned.get("id") if isinstance(returned, dict) else returned) != conversation:
        raise HarnessError("Retail response changed the requested conversation.")
    verify_consumption(response, command, expected, allow_shell_wrappers=True)
    downloads = client.download_outputs(response, folder / "downloads")
    if {p.name for p in downloads} != {binding["assessment_filename"], binding["report_filename"]}:
        raise HarnessError("Require exactly the two contracted retail outputs.")
    report = json.loads((folder / "downloads" / binding["assessment_filename"]).read_text())
    previous_report = json.loads(previous.read_text()) if previous else None
    evidence = prepared / ("stage-3" if stage_id == "stable-control" else stage_id) / "evidence.csv"
    checked = check_report(report, evidence, {"id": stage_id}, previous_report)
    if not (folder / "downloads" / binding["report_filename"]).read_text().strip():
        raise HarnessError("Retail markdown report is empty.")
    save(folder / "validation.json", checked)


def wait_worker(command: list[str], folder: Path, timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise HarnessError("Stage deadline must be finite and positive.")
    started = time.monotonic()
    with (folder / "worker.log").open("x", encoding="utf-8") as log:
        with subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT) as worker:
            try:
                save(folder / "worker-start.json", {
                    "pid": worker.pid, "deadline_seconds": timeout,
                    "started_utc": datetime.now(timezone.utc).isoformat(),
                })
                while True:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        worker.kill()
                        worker.wait()
                        save(folder / "deadline.json", {
                            "deadline_seconds": timeout, "elapsed_seconds": time.monotonic() - started,
                            "worker_pid": worker.pid, "worker_returncode": worker.returncode,
                            "request_recorded": (folder / "request.json").exists(),
                            "response_file_present": (folder / "response.json").exists(),
                            "service_cancellation_confirmed": False, "retried": False,
                        })
                        raise HarnessError(
                            f"Retail stage {folder.name} exceeded its {timeout:g}s wall-clock deadline. "
                            "Local worker killed and reaped; remote state is unknown. "
                            "No retry or next stage; inspect service history."
                        )
                    try:
                        code = worker.wait(timeout=min(30, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() - started < timeout:
                            phase = (
                                "response saved; awaiting artifacts/checks"
                                if (folder / "response.json").exists()
                                else "request recorded; awaiting response"
                                if (folder / "request.json").exists() else "preparing request"
                            )
                            print(f"{folder.name}: {time.monotonic() - started:.0f}s/"
                                  f"{timeout:g}s; {phase}", flush=True)
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait()
        save(folder / "worker-result.json", {
            "returncode": code, "elapsed_seconds": time.monotonic() - started, "retried": False,
        })
    if code:
        raise HarnessError(
            f"Retail stage {folder.name} worker failed with exit code {code}; "
            f"inspect {folder / 'worker.log'}. No retry or next stage."
        )


def bounded_stage(
    client: RetailClient, prepared: Path, folder: Path, stage_id: str,
    conversation: str, previous: Path | None,
) -> None:
    job = folder / "worker-job.json"
    save(job, {
        "schema_version": 1, "stage_id": stage_id, "conversation": conversation,
        "prepared": str(prepared.resolve()), "previous": str(previous.resolve()) if previous else None,
        "project_endpoint": client.project_endpoint, "subscription": client.subscription_id,
        "agent_name": client.agent_name, "agent_version": client.agent_version, "timeout": client.timeout,
    })
    wait_worker([sys.executable, "-u", "-m", "scripts.run_retail", "_stage",
                 "--job", str(job.resolve()), "--allow-paid-invocations"], folder, client.timeout)


def stage_worker(job_path: Path) -> None:
    if (job_path.parent / "request.json").exists() or (job_path.parent / "response.json").exists():
        raise HarnessError("This stage already has a recorded submission; inspect it rather than replaying it.")
    job = json.loads(job_path.read_text())
    if (job.get("schema_version") != 1 or job.get("stage_id") not in STAGES
            or not isinstance(job.get("agent_version"), str)
            or not job["agent_version"].isdecimal() or int(job["agent_version"]) < 1):
        raise HarnessError("Require a valid source-generated stage job with a pinned agent version.")
    client = RetailClient(job["project_endpoint"], job["subscription"], job["agent_name"],
                          agent_version=job["agent_version"], timeout=job["timeout"])
    execute_stage(client, Path(job["prepared"]), job_path.parent, job["stage_id"], job["conversation"],
                  Path(job["previous"]) if job["previous"] else None)


def run(client: RetailClient, prepared: Path, manifest: Path, output: Path) -> None:
    if not client.agent_version or not client.agent_version.isdecimal() or int(client.agent_version) < 1:
        raise HarnessError("Require an explicitly pinned positive agent version.")
    plan = verify_prepared(prepared)
    output.mkdir(parents=True, exist_ok=False)
    try:
        save(output / "agent.json", selected(client, manifest))
        save(output / "conditions.json", {
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "stages": list(STAGES), "maximum_paid_invocations": 4,
            "invocation_retries": 0, "source_manifest_sha256": sha(manifest),
            "per_stage_wall_clock_deadline_seconds": client.timeout,
            "deadline_scope": "Isolated stage worker including auth, request, response, downloads and checks.",
            "local_worker_termination_is_not_remote_cancellation": True,
            "preparation_sha256": sha(prepared / "preparation.json"),
            "evaluator_references_supplied": False,
            "source_sha256": {name: sha(ROOT / name) for name in [
                "scripts/run_retail.py", "retail_evidence.py", "scripts/prepare_retail.py",
                "harness_client.py", "retail_support.py",
                "agents/retail-analysis/azure.yaml",
                "agents/retail-analysis/data-policy.json",
            ]},
        })
        conversation = client.create_conversation()
        save(output / "conversation.json", {"id": conversation})
        previous = None
        records = []
        for stage_id in STAGES:
            folder = output / stage_id
            folder.mkdir()
            bounded_stage(client, prepared, folder, stage_id, conversation, previous)
            previous = folder / "downloads" / f"assessment-{stage_id}.json"
            checked = json.loads((folder / "validation.json").read_text())
            records.append(dict(stage_id=stage_id, **checked))
            print(f"{stage_id}: completed; arithmetic/citations/claim accounting checked", flush=True)
        save(output / "result.json", {
            "status": "completed_mechanical_checks", "invocations": len(records),
            "conversation_count": 1, "stages": records,
            "stable_evidence_sha256": plan["stable-control"]["evidence_sha256"],
            "semantic_quality": "manual_review_required",
        })
    except (HarnessError, OSError, ValueError, KeyError, TypeError) as error:
        save(output / "failure.json", {
            "status": "stopped", "error": str(error), "retried": False,
            "action": "Inspect preserved request/response and service history before any new invocation.",
        })
        raise


def normalized_output(value):
    if isinstance(value, list):
        return [normalized_output(item) for item in value]
    if isinstance(value, dict):
        return {k: normalized_output(v) for k, v in value.items()
                if not (k == "logprobs" and v in (None, []))}
    return value


def audit(client: RetailClient, prepared: Path, manifest: Path, run_dir: Path, output: Path) -> None:
    """Re-fetch and check an existing run without submitting or continuing a turn."""
    verify_prepared(prepared)
    if not client.agent_version or not re.fullmatch(r"[1-9][0-9]*", client.agent_version):
        raise HarnessError("Read-only audit requires an explicit positive agent version.")
    output.mkdir(parents=True, exist_ok=False)
    try:
        selected(client, manifest)
        conditions = json.loads((run_dir / "conditions.json").read_text())
        if conditions.get("stages") != list(STAGES) or conditions.get("maximum_paid_invocations") != 4:
            raise HarnessError("Audit requires the recorded four-stage schedule.")
        conversation = json.loads((run_dir / "conversation.json").read_text())["id"]
        previous = None
        prior_report = None
        records = []
        for stage_id in STAGES:
            folder = run_dir / stage_id
            if not (folder / "response.json").exists():
                break
            target = output / stage_id
            target.mkdir()
            saved = json.loads((folder / "response.json").read_text())
            live = client.get_response(saved["id"])
            save(target / "service-response.json", live)
            for key in ["id", "status", "model", "conversation", "agent_reference", "output", "usage",
                        "created_at", "completed_at"]:
                if normalized_output(live.get(key)) != normalized_output(saved.get(key)):
                    raise HarnessError(f"Service response changed field {key} in {stage_id}.")
            final_text(live)
            files, binding = inputs(prepared, stage_id, previous)
            expected = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
            prompt = task(binding, hash_command(expected))
            request = json.loads((folder / "request.json").read_text())
            if (request["input"] != content(prompt, files) or request["conversation"] != conversation
                    or request["agent_reference"] != {
                        "name": client.agent_name, "version": client.agent_version, "type": "agent_reference"}):
                raise HarnessError("Recorded request differs from the source-bound retail inputs.")
            verify_consumption(live, hash_command(expected), expected, allow_shell_wrappers=True)
            downloads = client.download_outputs(live, target / "downloads")
            if {p.name for p in downloads} != {binding["assessment_filename"], binding["report_filename"]}:
                raise HarnessError("Service audit output filenames mismatch.")
            for path in downloads:
                if path.read_bytes() != (folder / "downloads" / path.name).read_bytes():
                    raise HarnessError("Service file bytes differ from the recorded download.")
            previous = target / "downloads" / binding["assessment_filename"]
            report = json.loads(previous.read_text())
            evidence = prepared / ("stage-3" if stage_id == "stable-control" else stage_id) / "evidence.csv"
            checked = check_report(report, evidence, {"id": stage_id}, prior_report)
            records.append({"stage_id": stage_id, "response_id": live["id"], **checked})
            prior_report = report
        save(output / "conversation-items.json", client.conversation_items(conversation))
        save(output / "audit.json", {
            "status": "reconciled", "stages": records, "paid_invocations": 0,
            "complete_four_stage_run": len(records) == 4 and (run_dir / "result.json").exists(),
            "normalization": "Only empty/absent logprobs ([]/null/missing); other compared traces and bytes exact.",
            "semantic_quality": "manual_review_required",
        })
    except (HarnessError, OSError, ValueError, KeyError, TypeError) as error:
        save(output / "failure.json", {"status": "audit_stopped", "error": str(error), "paid_invocations": 0})
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("render", help="Generate a local agent definition; no Azure calls.")
    for flag in ("project-endpoint", "model", "agent-name"):
        configure.add_argument("--" + flag, required=True)
    configure.add_argument("--output-dir", type=Path, required=True)
    worker = commands.add_parser("_stage", help="Internal stage worker, not a recovery command.")
    worker.add_argument("--job", type=Path, required=True)
    worker.add_argument("--allow-paid-invocations", action="store_true")
    for name in ("run", "audit"):
        live = commands.add_parser(name)
        for flag in ("project-endpoint", "subscription", "agent-name", "agent-version"):
            live.add_argument("--" + flag, required=True)
        for flag in ("prepared", "manifest", "output-dir"):
            live.add_argument("--" + flag, type=Path, required=True)
        live.add_argument("--timeout", type=float, default=900,
                          help="Run: total wall-clock deadline per stage. Audit: socket timeout.")
        if name == "run":
            live.add_argument("--allow-paid-invocations", action="store_true")
        else:
            live.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            render(args.project_endpoint, args.model, args.agent_name, args.output_dir)
        elif args.command == "_stage":
            if not args.allow_paid_invocations:
                raise HarnessError("Stage worker requires explicit --allow-paid-invocations.")
            stage_worker(args.job)
        else:
            if args.command == "run" and not args.allow_paid_invocations:
                raise HarnessError("Explicit --allow-paid-invocations required; no implicit model requests.")
            if not math.isfinite(args.timeout) or args.timeout <= 0:
                raise HarnessError("Timeout must be finite and positive.")
            client = RetailClient(args.project_endpoint, args.subscription, args.agent_name,
                                  agent_version=args.agent_version, timeout=args.timeout)
            if args.command == "run":
                run(client, args.prepared, args.manifest, args.output_dir)
            else:
                audit(client, args.prepared, args.manifest, args.run_dir, args.output_dir)
        return 0
    except (HarnessError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
