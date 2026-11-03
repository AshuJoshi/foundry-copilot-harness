"""Prepare and record a bounded, instruction-only exploration comparison."""
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

from harness_client import COPILOT_HARNESSES, HarnessClient, HarnessError, JsonObject, ResponseError, final_text, json_object
from retail_support import RecordingClient, save, summarize
from scripts.run_retail import normalized_output

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "agents/instruction-selection/experiment.json"
TEMPLATE = ROOT / "agents/instruction-selection/azure.yaml"


def source_hashes() -> dict[str, str]:
    names = ["scripts/method_selection.py", "harness_client.py", "retail_support.py",
             "scripts/run_retail.py", "agents/instruction-selection/azure.yaml",
             "agents/instruction-selection/experiment.json"]
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def render(endpoint: str, subscription: str, model: str, agent: str, output: Path) -> None:
    client = HarnessClient(endpoint, subscription, agent)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", agent) or not model.strip():
        raise HarnessError("Require a distinct safe agent name and explicit existing model deployment.")
    manifest = json.loads(TEMPLATE.read_text())
    manifest["services"]["ai-project"]["endpoint"] = client.project_endpoint
    definition = manifest["services"]["outcome-exploration-agent"]
    definition.update(name=agent, model=model)
    if "__" in json.dumps(manifest):
        raise HarnessError("Unresolved instruction-selection placeholder.")
    output.parent.mkdir(parents=True, exist_ok=True)
    save(output, manifest)


def instructions(arm: str) -> str:
    baseline = json.loads(TEMPLATE.read_text())["services"]["outcome-exploration-agent"]["instructions"]
    if arm == "neutral":
        return baseline
    if arm == "tool-preferring":
        return baseline + json.loads(CONFIG.read_text())["tool_preference_suffix"]
    raise HarnessError(f"Unknown experiment arm: {arm}")


def prepare(manifest: Path, output: Path) -> None:
    source = json.loads(manifest.read_text())
    agents = [service for service in source["services"].values()
              if service.get("host") == "azure.ai.agent"]
    if len(agents) != 1:
        raise HarnessError("Expected exactly one agent in the rendered manifest.")
    agent = agents[0]
    if (agent.get("instructions") != instructions("neutral")
            or agent.get("kind") != "prompt"
            or agent.get("harness", {}).get("type") not in COPILOT_HARNESSES
            or agent.get("skills") or agent.get("tools")):
        raise HarnessError("Input must be the unmodified, rendered exploration definition.")
    agent["instructions"] = instructions("tool-preferring")
    output.parent.mkdir(parents=True, exist_ok=True)
    save(output, source)


def pinned(client: RecordingClient, version: str) -> JsonObject:
    if not re.fullmatch(r"[1-9][0-9]*", version):
        raise HarnessError("Require explicit positive integer versions.")
    latest = client.get_agent()["versions"]["latest"]
    if latest["version"] == version:
        return latest
    return json_object(json.loads(client._request(
        client._agent_path + "/versions/" + urllib.parse.quote(version, safe=""),
    )), "Pinned agent version")


def verify_arms(neutral: JsonObject, preferred: JsonObject) -> None:
    for arm, selected in [("neutral", neutral), ("tool-preferring", preferred)]:
        definition = selected["definition"]
        if (selected.get("status") != "active"
                or definition.get("kind") != "prompt"
                or definition.get("harness", {}).get("type") not in COPILOT_HARNESSES
                or definition.get("skills") or definition.get("tools")
                or definition.get("instructions") != instructions(arm)):
            raise HarnessError(f"{arm} definition does not match the source experiment.")
    left, right = dict(neutral["definition"]), dict(preferred["definition"])
    for definition in [left, right]:
        definition.pop("instructions")
        for key in ["tools", "skills"]:
            if definition.get(key) == []:
                definition.pop(key)
    if left != right or neutral["version"] == preferred["version"]:
        raise HarnessError("Arms must have different versions and identical non-instruction definitions.")


def schedule() -> list[JsonObject]:
    config = json.loads(CONFIG.read_text())
    jobs = []
    for repeat in range(1, config["repetitions"] + 1):
        arms = ["neutral", "tool-preferring"] if repeat % 2 else ["tool-preferring", "neutral"]
        for case in config["cases"]:
            for arm in arms:
                jobs.append(dict(case, arm=arm, repeat=repeat,
                                 directory=f"{len(jobs) + 1:02d}-{case['id']}-{arm}"))
    return jobs


def compare(output: Path) -> JsonObject:
    experiment = json.loads((output / "experiment.json").read_text())
    if experiment["config_sha256"] != hashlib.sha256(CONFIG.read_bytes()).hexdigest():
        raise HarnessError("Experiment configuration changed since the run.")
    if experiment["schedule"] != schedule():
        raise HarnessError("Recorded schedule differs from the registered twelve-request experiment.")
    if experiment["driver_sha256"] != source_hashes():
        raise HarnessError("Experiment source changed since the run.")
    versions = json.loads((output / "versions.json").read_text())
    verify_arms(versions["neutral"], versions["tool-preferring"])
    if experiment["versions"] != {arm: value["version"] for arm, value in versions.items()}:
        raise HarnessError("Experiment version pins differ from the recorded definitions.")
    records = []
    conversations = set()
    for job in experiment["schedule"]:
        folder = output / job["directory"]
        if not (folder / "01/response.json").is_file():
            records.append(dict(job, status="missing_or_stopped"))
            continue
        response = json.loads((folder / "01/response.json").read_text())
        if response.get("status") != "completed" or not (folder / "summary.json").is_file():
            records.append(dict(job, status="not_completed"))
            continue
        summary = json.loads((folder / "summary.json").read_text())
        request = json.loads((folder / "01/request.json").read_text())
        conditions = json.loads((folder / "conditions.json").read_text())
        expected_version = experiment["versions"][job["arm"]]
        expected_agent = {"name": experiment["agent_name"], "type": "agent_reference",
                          "version": expected_version}
        if (request["input"] != job["prompt"]
                or request["conversation"] != response["conversation"]["id"]
                or request["agent_reference"] != expected_agent
                or response["agent_reference"] != expected_agent
                or response.get("model") != versions[job["arm"]]["definition"]["model"]
                or conditions["prompts"] != [job["prompt"]]):
            raise HarnessError(f"Recorded request/response identity mismatch: {folder}")
        conversation = request["conversation"]
        if conversation in conversations:
            raise HarnessError("Trials must have distinct fresh conversations.")
        conversations.add(conversation)
        expected_hashes = experiment["driver_sha256"]
        for name, digest in expected_hashes.items():
            if digest != hashlib.sha256((ROOT / name).read_bytes()).hexdigest():
                raise HarnessError(f"Driver changed since experiment: {name}")
        for name, digest in conditions["source_sha256"].items():
            if expected_hashes.get(name) != digest:
                raise HarnessError(f"Recorded source fingerprint differs: {name}")
        if conditions["source_sha256"] != experiment["driver_sha256"]:
            raise HarnessError(f"Recorded source fingerprints differ: {folder}")
        detail = summarize(response)
        for key in ["parent_function_calls", "parent_shell_calls", "parent_tool_activity_observed", "service_seconds"]:
            if summary["runs"][0][key] != detail[key]:
                raise HarnessError("Recorded summary differs from the actual response.")
        text = final_text(response)
        numbers = [int(value.replace(",", "")) for value in
                   re.findall(r"(?<![\w.])-?\d+(?:,\d{3})*(?![\w.])", text)]
        records.append(dict(
            job, status="completed",
            expected_integer_present=job["expected_integer"] in numbers,
            answer=text,
            parent_function_calls=detail["parent_function_calls"],
            parent_shell_calls=detail["parent_shell_calls"],
            parent_tool_activity_observed=detail["parent_tool_activity_observed"],
            service_seconds=detail["service_seconds"],
            correctness="manual_review_required",
        ))
    totals = {}
    for arm in ["neutral", "tool-preferring"]:
        completed = [r for r in records if r["arm"] == arm and r["status"] == "completed"]
        totals[arm] = {
            "completed": len(completed),
            "parent_shell_observed": sum(r["parent_shell_calls"] > 0 for r in completed),
            "no_parent_tool_calls_observed": sum(
                not r["parent_tool_activity_observed"] for r in completed
            ),
            "expected_integer_present": sum(r["expected_integer_present"] for r in completed),
        }
    result = {
        "records": records, "totals": totals,
        "scope": "Parent-visible activity; integer presence is not proof of correctness or recall.",
    }
    return result


def audit(output: Path, destination: Path, endpoint: str, subscription: str) -> None:
    comparison = compare(output)
    experiment = json.loads((output / "experiment.json").read_text())
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    for job, record in zip(experiment["schedule"], comparison["records"]):
        if record["status"] != "completed":
            continue
        client = HarnessClient(endpoint, subscription, experiment["agent_name"],
                               agent_version=experiment["versions"][job["arm"]], timeout=60)
        folder = output / job["directory"] / "01"
        saved = json.loads((folder / "response.json").read_text())
        response = client.get_response(saved["id"])
        target = destination / job["directory"]
        target.mkdir()
        save(target / "response.json", response)
        if (response.get("status") != "completed" or response.get("model") != saved.get("model")
                or response.get("agent_reference") != saved.get("agent_reference")
                or response.get("conversation") != saved.get("conversation")
                or normalized_output(response["output"]) != normalized_output(saved["output"])):
            raise HarnessError("Audited service response differs from the recorded trial.")
        items = client.conversation_items(json.loads((folder / "request.json").read_text())["conversation"])
        users = [item for item in items if item.get("role") == "user"]
        if len(users) != 1 or "".join(part.get("text", "") for part in users[0]["content"]) != job["prompt"]:
            raise HarnessError("Audited conversation is not the registered single-request trial.")
        records.append({"directory": job["directory"], "response_id": response["id"]})
    save(destination / "audit.json", {
        "status": "reconciled" if len(records) == 12 else "partial_retrieval",
        "records": records, "complete_trial_coverage": len(records) == 12,
        "paid_invocations": 0, "semantic_quality": "manual_review_required",
    })


def run_trial(client: RecordingClient, job: JsonObject, output: Path, hashes: dict[str, str]) -> None:
    output.mkdir(parents=True, exist_ok=False)
    save(output / "conditions.json", {"prompts": [job["prompt"]], "source_sha256": hashes,
                                      "invocation_retries": 0})
    directory = output / "01"
    directory.mkdir()
    client.evidence_dir = directory
    (directory / "prompt.txt").write_text(job["prompt"])
    conversation = client.create_conversation()
    save(directory / "conversation.json", {"id": conversation})
    started = time.monotonic()
    try:
        response = client.invoke(job["prompt"], conversation_id=conversation)
    except ResponseError as error:
        save(directory / "response.json", error.response)
        raise
    save(directory / "response.json", response)
    returned = response.get("conversation")
    if (returned.get("id") if isinstance(returned, dict) else returned) != conversation:
        raise HarnessError("Response conversation differs from the requested trial.")
    (directory / "answer.txt").write_text(final_text(response))
    detail = summarize(response)
    detail["client_invocation_seconds"] = time.monotonic() - started
    save(directory / "summary.json", detail)
    save(output / "summary.json", {"status": "completed", "runs": [detail],
                                   "correctness": "not_assessed"})


def experiment(
    endpoint: str, subscription: str, agent: str,
    neutral_version: str, preferred_version: str, output: Path, timeout: float,
) -> None:
    if output.exists():
        raise HarnessError("Experiment output already exists; refusing duplicate batch.")
    clients = {
        arm: RecordingClient(endpoint, subscription, agent, agent_version=version, timeout=timeout)
        for arm, version in [("neutral", neutral_version), ("tool-preferring", preferred_version)]
    }
    selected = {arm: pinned(client, client.agent_version or "") for arm, client in clients.items()}
    verify_arms(selected["neutral"], selected["tool-preferring"])
    output.mkdir(parents=True, exist_ok=False)
    save(output / "versions.json", selected)
    jobs = schedule()
    hashes = source_hashes()
    save(output / "experiment.json", {
        "source_commit": None,
        "source_provenance": "Source hashes recorded; owner-created Git export verified separately.",
        "agent_name": agent, "schedule": jobs,
        "versions": {"neutral": neutral_version, "tool-preferring": preferred_version},
        "config_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
        "driver_sha256": hashes,
        "generation_settings": "No explicit reasoning effort or sampling override.",
        "invocation_retries": 0,
    })
    try:
        for job in jobs:
            print(f"{job['directory']}: starting", flush=True)
            if source_hashes() != hashes:
                raise HarnessError("Source changed during the experiment; stop before another request.")
            run_trial(clients[job["arm"]], job, output / job["directory"], hashes)
        save(output / "comparison.json", compare(output))
    except (HarnessError, OSError, ValueError, subprocess.SubprocessError) as error:
        save(output / "failure.json", {"error": str(error), "retried": False})
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    rendering = commands.add_parser("render", help="Render the neutral agent locally.")
    for name in ["project-endpoint", "subscription", "agent-name", "model-deployment"]:
        rendering.add_argument("--" + name, required=True)
    rendering.add_argument("--output", type=Path, required=True)
    prep = commands.add_parser("prepare", help="Render the instruction-only manifest change; no Azure calls.")
    prep.add_argument("--manifest", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    report = commands.add_parser("compare", help="Read-only reconstruction of recorded results; no invocations.")
    report.add_argument("--output-dir", type=Path, required=True)
    auditing = commands.add_parser("audit", help="Re-fetch recorded trials without invoking models.")
    for name in ["project-endpoint", "subscription"]:
        auditing.add_argument("--" + name, required=True)
    auditing.add_argument("--run-dir", type=Path, required=True)
    auditing.add_argument("--output-dir", type=Path, required=True)
    execute = commands.add_parser("run")
    for name in ["project-endpoint", "subscription", "agent-name", "neutral-version", "tool-version"]:
        execute.add_argument("--" + name, required=True)
    execute.add_argument("--output-dir", type=Path, required=True)
    execute.add_argument("--timeout", type=float, default=300)
    execute.add_argument("--allow-paid-invocations", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "render":
            render(args.project_endpoint, args.subscription, args.model_deployment, args.agent_name, args.output)
        elif args.command == "prepare":
            prepare(args.manifest, args.output)
        elif args.command == "compare":
            print(json.dumps(compare(args.output_dir), indent=2))
        elif args.command == "audit":
            audit(args.run_dir, args.output_dir, args.project_endpoint, args.subscription)
        else:
            if not args.allow_paid_invocations or args.timeout <= 0:
                raise HarnessError("Require explicit paid opt-in and positive timeout.")
            experiment(
                args.project_endpoint, args.subscription, args.agent_name,
                args.neutral_version, args.tool_version, args.output_dir, args.timeout,
            )
        return 0
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
