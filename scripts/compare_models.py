"""Run a fixed task battery against model-only Copilot harness agent versions."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
from uuid import uuid4

from harness_client import HarnessClient, HarnessError, JsonObject, ResponseError, final_text
from retail_support import definition_for_rest, hash_command, verify_consumption, RecordingClient, save, summarize, selected
from scripts.run_retail import normalized_output, wait_worker
from model_comparison_eval import EvaluationInfrastructureError, grade_code, grade_json

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "agents/model-comparison"
ROLE = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
INPUT_ROOT = "/home/session/workspace/data/"

def cli(arguments: list[str]) -> object:
    response = subprocess.run(["az", *arguments, "--only-show-errors", "-o", "json"],
                              capture_output=True, text=True, timeout=180)
    if response.returncode:
        raise HarnessError(f"Azure CLI failed: {response.stderr.strip()}")
    return json.loads(response.stdout)


def instructions() -> str:
    return json.loads((SAMPLE / "azure.yaml").read_text())["services"]["comparison-agent"]["instructions"]


def sources() -> dict[str, str]:
    names = [
        "scripts/compare_models.py", "model_comparison_eval.py", "harness_client.py",
        "retail_support.py", "scripts/run_retail.py", "scripts/prepare_retail.py", "retail_evidence.py",
        "agents/model-comparison/azure.yaml", "agents/model-comparison/battery.json",
        "agents/model-comparison/models.example.json",
        "agents/model-comparison/model-deployments/terra61.json",
    ]
    names.extend(str(p.relative_to(ROOT)) for p in sorted((SAMPLE / "fixtures").iterdir()) if p.is_file())
    return {name: sha(ROOT / name) for name in names}


def configuration(models_path: Path) -> dict:
    value = json.loads((SAMPLE / "battery.json").read_text())
    battery = {k: value[k] for k in ["boundary", "cases", "full_repetitions"]}
    digest = hashlib.sha256(json.dumps(battery, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    fixtures = {p.name: sha(p) for p in sorted((SAMPLE / "fixtures").iterdir()) if p.is_file()}
    if (value["schema_version"] != 1 or value["full_repetitions"] != 3 or len(value["cases"]) != 6
            or value["invocation_deadline_seconds"] != 420 or digest != value["battery_sha256"]
            or fixtures != value["fixtures_sha256"]):
        raise HarnessError("The fixed battery or its fixture fingerprints changed.")
    roster = json.loads(models_path.read_text())
    if not isinstance(roster, dict):
        raise HarnessError("Model roster must be a JSON object.")
    models = roster.get("models")
    if (roster.get("schema_version") != 1 or not isinstance(models, list) or not 1 <= len(models) <= 10):
        raise HarnessError("Provide a model roster with one to ten existing deployment bindings.")
    for model in models:
        if (not isinstance(model, dict) or set(model) != {"id", "deployment"}
                or not isinstance(model["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", model["id"])
                or not isinstance(model["deployment"], str) or not model["deployment"].strip()
                or model["deployment"] != model["deployment"].strip() or "__" in model["deployment"]):
            raise HarnessError("Replace every roster placeholder with an explicit existing deployment name.")
    if (len({m["id"] for m in models}) != len(models)
            or len({m["deployment"] for m in models}) != len(models)):
        raise HarnessError("Model IDs and deployment names must be distinct.")
    for case in value["cases"]:
        for name in case["inputs"]:
            if Path(name).name != name or name not in fixtures:
                raise HarnessError("Unregistered battery input.")
    value.update(models=models, study_id="fixed-battery-v1",
                 full_max_invocations=len(models) * (1 + len(value["cases"]) * value["full_repetitions"]))
    return value


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def full_schedule(config: dict) -> list[dict]:
    jobs = []
    models = [m["id"] for m in config["models"]]
    for repeat in range(1, config["full_repetitions"] + 1):
        for index, case in enumerate(config["cases"]):
            shift = (index + 2 * (repeat - 1)) % len(models)
            for model_id in models[shift:] + models[:shift]:
                jobs.append({"model": model_id, "case": case["id"], "repeat": repeat})
    return jobs


def check_sources(state: dict) -> None:
    if sources() != state["source_sha256"]:
        raise HarnessError("Study source/fixtures changed; do not pool protocol versions or continue this batch.")


def render(endpoint: str, model: str, agent: str, directory: Path) -> dict:
    manifest = json.loads((SAMPLE / "azure.yaml").read_text())
    manifest["services"]["ai-project"]["endpoint"] = endpoint
    service = manifest["services"].pop("comparison-agent")
    service.update(name=agent, model=model)
    manifest["services"][agent] = service
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / "azure.yaml", manifest)
    return manifest


def definition(manifest: dict) -> dict:
    agent = next(v for v in manifest["services"].values() if v["host"] == "azure.ai.agent")
    return definition_for_rest({k: agent[k] for k in ["kind", "model", "harness", "instructions", "tools"]})


def prepare(args) -> None:
    config = configuration(args.models)
    parsed = datetime.fromisoformat(args.stop_starting_at)
    if parsed.tzinfo is None or parsed <= datetime.now(timezone.utc):
        raise HarnessError("Require a future timezone-aware launch cutoff.")
    cutoff = parsed.astimezone(timezone.utc).isoformat()
    if not 1 <= args.max_invocations <= 190 or args.max_invocations != config["full_max_invocations"]:
        raise HarnessError(f"Confirm --max-invocations {config['full_max_invocations']} for this roster.")
    config["region"] = args.region.lower()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", args.agent_name):
        raise HarnessError("Use a distinct approved agent name of 1-63 safe characters.")
    client = HarnessClient(args.project_endpoint, args.subscription, args.agent_name)
    resource = (f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}/providers/"
                f"Microsoft.CognitiveServices/accounts/{args.account}")
    project_id = resource + "/projects/" + args.project
    project = cli(["rest", "--method", "get", "--subscription", args.subscription,
                   "--url", f"https://management.azure.com{project_id}?api-version=2026-09-01"])
    if (project["properties"]["endpoints"]["AI Foundry API"].rstrip("/") != client.project_endpoint
            or project["location"].lower() != config["region"]):
        raise HarnessError("Project ARM identity and endpoint differ.")
    models = cli(["cognitiveservices", "account", "deployment", "list",
                  "--subscription", args.subscription, "--resource-group", args.resource_group,
                  "--name", args.account])
    by_name = {m["name"]: m for m in models}
    for model in config["models"]:
        deployed = by_name.get(model["deployment"])
        if not deployed or deployed["properties"]["provisioningState"] != "Succeeded":
            raise HarnessError("A selected model is not deployed/succeeded.")
    image = subprocess.run(["docker", "image", "inspect", args.python_image], capture_output=True,
                           text=True, timeout=30, check=True)
    if not re.fullmatch(r"(?:docker.io/library/)?python@sha256:[0-9a-f]{64}", args.python_image):
        raise HarnessError("Require the approved official Python image pinned by digest.")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    agent_list = json.loads(client._request("/agents", query={"limit": "100"}))
    if agent_list.get("has_more") or any(a["name"] == args.agent_name for a in agent_list["data"]):
        raise HarnessError("Study agent exists or uniqueness listing is incomplete; do not overwrite.")
    save(args.output_dir / "agents-before.json", agent_list)
    save(args.output_dir / "models-before.json", models)
    save(args.output_dir / "python-image.json", json.loads(image.stdout))
    expected = None
    for model in config["models"]:
        manifest = render(client.project_endpoint, model["deployment"], args.agent_name,
                          args.output_dir / "definitions" / model["id"])
        common = dict(definition(manifest))
        common.pop("model")
        if expected is not None and common != expected:
            raise HarnessError("Study variants differ beyond model.")
        expected = common
    save(args.output_dir / "state.json", {
        "study": config, "source_sha256": sources(), "project_endpoint": client.project_endpoint,
        "subscription": args.subscription, "resource_group": args.resource_group, "account": args.account,
        "project": args.project, "project_id": project_id, "agent_name": args.agent_name,
        "python_image": args.python_image, "gate_nonce": "STUDY_" + uuid4().hex,
        "registered_full_schedule": full_schedule(config),
        "server_telemetry": "This runner does not query server telemetry; server-only metrics are unknown.",
        "pricing": "Not verified; dollar costs unavailable, raw token fields retained.",
        "run_kind": "full", "launch_cutoff_utc": cutoff, "models_input_sha256": sha(args.models),
        "approved_max_invocations": args.max_invocations,
    })


def azd(arguments: list[str], cwd: Path, path: Path) -> None:
    env = dict(os.environ, AZURE_DEV_USER_AGENT="microsoft_foundry_skill")
    completed = subprocess.run(["azd", *arguments], cwd=cwd, env=env,
                               capture_output=True, text=True, timeout=240)
    save(path, {"arguments": arguments, "exit_code": completed.returncode,
                "stdout": completed.stdout, "stderr": completed.stderr})
    if completed.returncode:
        raise HarnessError(f"azd operation failed; inspect {path}; no deployment retry.")


def load_state(directory: Path) -> dict:
    state = json.loads((directory / "state.json").read_text())
    check_sources(state)
    return state


def deploy(args) -> None:
    if not args.allow_azure_writes or not args.allow_project_role_grants:
        raise HarnessError("Study deployment and project identity grants require separate explicit flags.")
    state = load_state(args.study_dir)
    if (args.study_dir / "versions.json").exists() or (args.study_dir / "deployment-started.json").exists():
        raise HarnessError("Deployment already attempted; inspect state, do not create extra versions.")
    save(args.study_dir / "deployment-started.json", {"utc": datetime.now(timezone.utc).isoformat()})
    client = RecordingClient(state["project_endpoint"], state["subscription"], state["agent_name"], timeout=60)
    versions, principals = {}, set()
    grants = 0
    try:
        for model in state["study"]["models"]:
            folder = args.study_dir / "definitions" / model["id"]
            azd(["env", "new", "study-" + model["id"], "--subscription", state["subscription"],
                 "--location", state["study"]["region"], "--no-prompt"], folder, folder / "env-new.json")
            values = {
                "AZURE_RESOURCE_GROUP": state["resource_group"],
                "AZURE_AI_PROJECT_ENDPOINT": state["project_endpoint"],
                "FOUNDRY_PROJECT_ENDPOINT": state["project_endpoint"],
                "AZURE_AI_PROJECT_ID": state["project_id"],
                "AZURE_AI_PROJECT_CONNECTIONS_PROJECT_ENDPOINT": state["project_endpoint"],
            }
            for key, value in values.items():
                azd(["env", "set", key, value], folder, folder / (key + ".json"))
            azd(["deploy", state["agent_name"], "--no-prompt"], folder, folder / "deployment.json")
            record = selected(client, folder / "azure.yaml")
            if record["status"] != "active":
                raise HarnessError("Published study version differs from model-only source.")
            principal = record["instance_identity"]["principal_id"]
            versions[model["id"]] = {"version": record["version"], "record": record,
                                     "model": model["deployment"], "principal": principal}
            save(folder / "published.json", record)
            print(f"DEPLOYED {model['id']}: version {record['version']}", flush=True)
            if principal not in principals:
                assignments = cli([
                    "role", "assignment", "list", "--subscription", state["subscription"],
                    "--assignee-object-id", principal, "--scope", state["project_id"],
                    "--fill-principal-name", "false", "--fill-role-definition-name", "false",
                ])
                matching = [r for r in assignments if r["roleDefinitionId"].lower().endswith("/" + ROLE)
                            and r["scope"].lower() == state["project_id"].lower()
                            and r["principalId"].lower() == principal.lower()]
                if matching:
                    assignment = {"existing": matching}
                else:
                    assignment = cli([
                        "role", "assignment", "create", "--subscription", state["subscription"],
                        "--assignee-object-id", principal, "--assignee-principal-type", "ServicePrincipal",
                        "--role", ROLE, "--scope", state["project_id"],
                    ])
                    grants += 1
                save(folder / "project-role.json", assignment)
                principals.add(principal)
        expected_versions = len(state["study"]["models"])
        if len({v["version"] for v in versions.values()}) != expected_versions:
            raise HarnessError(f"Expected {expected_versions} distinct pinned versions.")
        for model in state["study"]["models"]:
            selected_version = pinned(client, versions[model["id"]]["version"])
            if (selected_version["status"] != "active"
                    or selected_version["definition"] != versions[model["id"]]["record"]["definition"]):
                raise HarnessError("Earlier pinned version unavailable after publishing later versions.")
        save(args.study_dir / "versions.json", versions)
        print("Waiting 300s once for project-role propagation.", flush=True)
        time.sleep(300)
        save(args.study_dir / "deployment-ready.json", {
            "versions": expected_versions, "project_role_grants": grants, "identities_verified": len(principals)})
    except (HarnessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        save(args.study_dir / "deployment-failure.json", {"error": str(error), "retried": False})
        raise


def file_input(text: str, attachments: dict[str, bytes]) -> list[dict]:
    parts = [{"type": "input_text", "text": text}]
    if sum(map(len, attachments.values())) > 131072:
        raise HarnessError("Study input exceeds frozen small-fixture bounds.")
    for name, data in attachments.items():
        if Path(name).name != name or Path(name).suffix not in {".py", ".csv", ".json"}:
            raise HarnessError("Unexpected study input filename.")
        parts.append({"type": "input_file", "filename": name,
                      "file_data": "data:text/plain;base64," + base64.b64encode(data).decode()})
    return [{"role": "user", "content": parts}]


class StudyClient(RecordingClient):
    def __init__(self, *args, log_dir: Path, **kwargs):
        self.log_dir = log_dir
        self.attachments: dict[str, bytes] = {}
        super().__init__(*args, http_observer=self.observe, **kwargs)

    def observe(self, value: dict) -> None:
        with (self.log_dir / "http.jsonl").open("a") as stream:
            stream.write(json.dumps(value) + "\n")

    def _request(self, path, body=None, **kwargs):
        if body is not None and path == self._protocol_path + "/responses":
            body = dict(body, input=file_input(body["input"], self.attachments))
        return super()._request(path, body, **kwargs)


def gate_script(nonce: str) -> bytes:
    return (
        "import json,platform\nfrom pathlib import Path\n"
        f"value={{'nonce':{nonce!r},'value':sum(range(1,101)),'python':platform.python_version()}}\n"
        "Path('/home/session/workspace/outputs/gate-result.json').write_text(json.dumps(value,sort_keys=True))\n"
        "print(json.dumps(value,sort_keys=True))\n"
        f"print('GATE_SCRIPT_COMPLETED='+{nonce!r})\n"
    ).encode()


def verify_gate_execution(response: dict, gate: dict) -> dict:
    calls = {i["call_id"]: i for i in response["output"] if i["type"] == "shell_call"}
    observed, literal, nonzero = False, False, 0
    for item in response["output"]:
        if item["type"] != "shell_call_output" or item.get("call_id") not in calls:
            continue
        for command, output in zip(calls[item["call_id"]]["action"]["commands"], item.get("output", [])):
            code = output.get("outcome", {}).get("exit_code")
            nonzero += int(code is not None and code != 0)
            stdout = output.get("stdout", "")
            if (re.search(r"(?<![\w.-])gate_input\.py(?![\w.-])", command)
                    and json.dumps(gate, sort_keys=True) in stdout
                    and "GATE_SCRIPT_COMPLETED=" + gate["nonce"] in stdout):
                observed = True
                literal = literal or command.strip() == "python3 " + INPUT_ROOT + "gate_input.py"
    if not observed:
        raise HarnessError("No native gate script completion marker and matching output observed.")
    return {"script_completed": True, "literal_command_adherence": literal,
            "nonzero_shell_results": nonzero, "policy": "verified-outcome-v3"}


def task_payload(state: dict, case_id: str) -> tuple[str, dict[str, bytes]]:
    if case_id == "gate":
        files = {"gate_input.py": gate_script(state["gate_nonce"])}
        task = ("Readiness only, not a scored task. After hash verification, execute "
                "/home/session/workspace/data/gate_input.py unchanged using Python3. "
                "Shell wrappers or directory changes are permitted. The script writes "
                "/home/session/workspace/outputs/gate-result.json and prints its data plus a completion marker. "
                "Upload exactly that actual file, not a prose claim. "
                "Do not add any other output file.")
    else:
        case = next(c for c in state["study"]["cases"] if c["id"] == case_id)
        files = {name: (SAMPLE / "fixtures" / name).read_bytes() for name in case["inputs"]}
        task = case["prompt"]
    if files:
        command = hash_command({name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()})
        task = "FIRST run this exact receiver-side hash command, then inspect the verified files:\n" + command + "\n\n" + task
    return state["study"]["boundary"] + "\n\n" + task, files


def worker(job_path: Path) -> None:
    job = json.loads(job_path.read_text())
    state = load_state(Path(job["study_dir"]))
    versions = json.loads((Path(job["study_dir"]) / "versions.json").read_text())
    version = versions[job["model_id"]]
    if job["case_id"] not in {"gate", *(c["id"] for c in state["study"]["cases"])}:
        raise HarnessError("Unregistered study case.")
    folder = job_path.parent
    if (folder / "request.json").exists() or (folder / "response.json").exists():
        raise HarnessError("Worker already submitted or recorded this case; inspect instead of retrying.")
    client = StudyClient(state["project_endpoint"], state["subscription"], state["agent_name"],
                         agent_version=version["version"], timeout=400, log_dir=folder)
    prompt, files = task_payload(state, job["case_id"])
    client.attachments, client.evidence_dir = files, folder
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    save(folder / "inputs.json", {"case": job["case_id"], "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                                "file_sha256": hashes})
    (folder / "prompt.txt").write_text(prompt)
    conversation = client.create_conversation()
    save(folder / "conversation.json", {"id": conversation})
    started = time.monotonic()
    try:
        response = client.invoke(prompt, conversation_id=conversation)
    except ResponseError as error:
        save(folder / "response.json", error.response)
        failed_detail = summarize(error.response)
        failed_detail.update(returned_model=error.response.get("model"), expected_model=version["model"],
                             agent_version=version["version"])
        save(folder / "summary.json", failed_detail)
        raise
    save(folder / "response.json", response)
    (folder / "answer.txt").write_text(final_text(response))
    detail = summarize(response)
    detail.update(returned_model=response.get("model"), expected_model=version["model"],
                  agent_version=version["version"],
                  visible_tool_calls=sum(i["type"] in {"shell_call", "function_call"} for i in response["output"]),
                  visible_shell_commands=sum(len(i["action"]["commands"]) for i in response["output"]
                                             if i["type"] == "shell_call"),
                  nonzero_shell_results=sum(
                      o.get("outcome", {}).get("exit_code") not in {None, 0}
                      for i in response["output"] if i["type"] == "shell_call_output"
                      for o in i.get("output", [])))
    save(folder / "summary.json", detail)
    if (response.get("model") != version["model"]
            or response.get("agent_reference") != {
                "type": "agent_reference", "name": state["agent_name"], "version": version["version"]}):
        raise HarnessError("Returned deployment alias or pinned agent version differs from expected model.")
    forbidden = [i.get("name") for i in response["output"] if i["type"] == "function_call"
                 and i.get("name") in {"task", "fetch", "web_search"}]
    if forbidden:
        raise HarnessError(f"Common study boundary violated: forbidden tools {forbidden}.")
    returned = response.get("conversation")
    if (returned.get("id") if isinstance(returned, dict) else returned) != conversation:
        raise HarnessError("Study response changed conversation.")
    if files:
        verify_consumption(response, hash_command(hashes), hashes, allow_shell_wrappers=True)
    invocation_seconds = time.monotonic() - started
    if job["case_id"] == "gate":
        paths = client.download_outputs(response, folder / "downloads")
        if {p.name for p in paths} != {"gate-result.json"}:
            raise HarnessError("Gate did not deliver exactly its requested file.")
        gate = json.loads(paths[0].read_text())
        if (set(gate) != {"nonce", "value", "python"} or gate["nonce"] != state["gate_nonce"]
                or type(gate["value"]) is not int or gate["value"] != 5050
                or not isinstance(gate["python"], str)):
            raise HarnessError("Gate artifact contents mismatch.")
        save(folder / "readiness-evidence.json", verify_gate_execution(response, gate))
    else:
        case = next(c for c in state["study"]["cases"] if c["id"] == job["case_id"])
        if case["output"]["kind"] == "code":
            paths = client.download_outputs(response, folder / "downloads")
            if {p.name for p in paths} != {case["output"]["filename"]}:
                raise HarnessError("Coding task did not deliver exactly its requested file.")
    save(folder / "worker-success.json", {
        "invocation_seconds": invocation_seconds, "including_download_seconds": time.monotonic() - started,
    })


def classify(folder: Path) -> str:
    if (folder / "deadline.json").exists():
        return "timeout_unknown"
    events = [json.loads(line) for line in (folder / "http.jsonl").read_text().splitlines()] if (folder / "http.jsonl").exists() else []
    codes = [e["status"] for e in events if e.get("status")]
    if 429 in codes:
        return "rate_limited"
    if 401 in codes or 403 in codes:
        return "permission_or_policy"
    log = (folder / "worker.log").read_text(errors="replace")
    if "session_creation_failed" in log:
        return "startup_failed"
    if "not supported" in log.lower() or "unsupported" in log.lower():
        return "unsupported_or_protocol"
    return "service_or_contract_failure"


def run_job(state: dict, study_dir: Path, folder: Path, model_id: str, case_id: str) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / "job.json", {"study_dir": str(study_dir.resolve()), "model_id": model_id, "case_id": case_id})
    started = time.monotonic()
    try:
        wait_worker([sys.executable, "-u", "-m", "scripts.compare_models", "_worker",
                     "--job", str((folder / "job.json").resolve()), "--allow-paid-invocations"], folder, 420)
    except HarnessError as error:
        return {"status": classify(folder), "error": str(error),
                "model": model_id, "case": case_id, "client_seconds": time.monotonic() - started,
                "correctness": None, "retried": False}
    try:
        if case_id == "gate":
            grading = {"correct": True, "contract_ok": True, "scope": "Native execution/file readiness only"}
        else:
            case = next(c for c in state["study"]["cases"] if c["id"] == case_id)
            if case["output"]["kind"] == "code":
                grading = grade_code(case_id, folder / "downloads" / case["output"]["filename"],
                                     state["python_image"], folder / "grading")
            else:
                grading = grade_json(case_id, (folder / "answer.txt").read_text())
    except (EvaluationInfrastructureError, OSError, ValueError, KeyError, TypeError) as error:
        save(folder / "evaluation-failure.json", {"error": str(error), "correctness": None})
        return {"status": "evaluation_infrastructure_failed", "model": model_id, "case": case_id,
                "error": str(error), "client_seconds": time.monotonic() - started,
                "correctness": None, "retried": False}
    save(folder / "grading.json", grading)
    return {"status": "completed_correct" if grading["correct"] else "completed_incorrect",
            "model": model_id, "case": case_id, "client_seconds": time.monotonic() - started,
            "grading": grading, "retried": False}


def verify_model_bindings(state: dict, directory: Path) -> None:
    if not (directory / "deployment-ready.json").exists():
        raise HarnessError("Published versions and permission propagation are not verified.")
    previous = {m["name"]: m for m in json.loads((directory / "models-before.json").read_text())}
    current = {m["name"]: m for m in cli([
        "cognitiveservices", "account", "deployment", "list", "--subscription", state["subscription"],
        "--resource-group", state["resource_group"], "--name", state["account"]])}
    for model in state["study"]["models"]:
        old, new = previous[model["deployment"]], current.get(model["deployment"])
        if (new is None or new["properties"]["provisioningState"] != "Succeeded"
                or new["properties"]["model"] != old["properties"]["model"] or new["sku"] != old["sku"]):
            raise HarnessError("A selected deployment's model/version/SKU/capacity changed before the run.")


def smoke_study(args) -> None:
    if not args.allow_paid_invocations:
        raise HarnessError("Explicit --allow-paid-invocations required before any remote requests.")
    state = load_state(args.study_dir)
    if len(state["study"]["models"]) != 1 or args.max_invocations != 2:
        raise HarnessError("Smoke verification requires one model and --max-invocations 2.")
    verify_model_bindings(state, args.study_dir)
    output = args.study_dir / "smoke"
    output.mkdir(exist_ok=False)
    model = state["study"]["models"][0]["id"]
    records = []
    try:
        for number, case_id in enumerate(["gate", "implement"], 1):
            if datetime.now(timezone.utc) >= datetime.fromisoformat(state["launch_cutoff_utc"]):
                raise HarnessError("Launch cutoff reached; no further smoke requests.")
            record = run_job(state, args.study_dir, output / f"{number:03d}-{model}-{case_id}", model, case_id)
            records.append(record)
            save(output / f"record-{number:03d}.json", record)
            print(f"SMOKE {model}/{case_id}: {record['status']}", flush=True)
            if record["status"] != "completed_correct":
                raise HarnessError("Smoke verification failed; stop without retries or additional requests.")
        save(output / "result.json", {
            "status": "verified", "records": records, "request_attempts": len(records),
            "scope": "One readiness and one implementation request; not a full model comparison.",
            "full_battery_rerun": False, "client_retries": 0,
        })
    except (HarnessError, EvaluationInfrastructureError, OSError, ValueError, KeyError) as error:
        save(output / "failure.json", {"error": str(error), "records": records, "retried": False})
        raise


def run_study(args) -> None:
    if not args.allow_paid_invocations:
        raise HarnessError("Explicit --allow-paid-invocations required before any remote requests.")
    state = load_state(args.study_dir)
    maximum = state["study"]["full_max_invocations"]
    if args.max_invocations != maximum or state["approved_max_invocations"] != maximum:
        raise HarnessError(f"Confirm --max-invocations {maximum} for the frozen roster.")
    if state["registered_full_schedule"] != full_schedule(state["study"]):
        raise HarnessError("The frozen schedule differs from the registered battery.")
    verify_model_bindings(state, args.study_dir)
    output = args.study_dir / "full"
    output.mkdir(exist_ok=False)
    eligible, paused, records = [], set(), []
    count = 0
    cutoff_reached = False

    def may_start() -> bool:
        return datetime.now(timezone.utc) < datetime.fromisoformat(state["launch_cutoff_utc"])

    def record_result(record: dict) -> None:
        records.append(record)
        save(output / f"record-{count:03d}.json", record)
        if record["status"] == "evaluation_infrastructure_failed":
            raise HarnessError("Evaluation infrastructure failed; stop before more paid requests.")

    try:
        for model in state["study"]["models"]:
            if not may_start():
                cutoff_reached = True
                break
            count += 1
            print(f"START {count}/{maximum} {model['id']}/readiness", flush=True)
            record = run_job(state, args.study_dir, output / f"{count:03d}-{model['id']}-gate",
                             model["id"], "gate")
            record_result(record)
            print(f"READINESS {model['id']}: {record['status']}", flush=True)
            if record["status"] == "completed_correct":
                eligible.append(model["id"])
            else:
                paused.add(model["id"])
            if model["id"] == "sol61" and model["id"] in paused:
                raise HarnessError("Known baseline readiness failed; inspect before other paid requests.")
        for job in state["registered_full_schedule"]:
            if not may_start():
                cutoff_reached = True
                break
            model_id, case_id = job["model"], job["case"]
            if model_id not in eligible or model_id in paused:
                continue
            if count >= maximum:
                raise HarnessError("Approved invocation budget exhausted.")
            count += 1
            print(f"START {count}/{maximum} {model_id}/{case_id} repeat {job['repeat']}", flush=True)
            record = run_job(state, args.study_dir, output / f"{count:03d}-{model_id}-{case_id}-r{job['repeat']}",
                             model_id, case_id)
            record["repeat"] = job["repeat"]
            record_result(record)
            if record["status"] not in {"completed_correct", "completed_incorrect"}:
                paused.add(model_id)
            print(f"RESULT {count}/{maximum} {model_id}/{case_id} r{job['repeat']}: "
                  f"{record['status']}; wall={record['client_seconds']:.1f}s", flush=True)
        save(output / "result.json", {
            "status": "launch_cutoff_reached" if cutoff_reached else "schedule_finished",
            "attempted": count, "maximum": maximum, "eligible": eligible, "paused": sorted(paused),
            "records": records, "launch_cutoff_reached": cutoff_reached,
            "registered_scored_jobs": len(state["registered_full_schedule"]),
            "scored_attempted": sum(r["case"] != "gate" for r in records),
            "complete_scored_coverage": sum(r["case"] != "gate" for r in records) == len(state["registered_full_schedule"]),
            "client_retries": 0,
        })
    except (HarnessError, EvaluationInfrastructureError, OSError, ValueError, KeyError) as error:
        save(output / "failure.json", {"error": str(error), "attempted": count,
                                      "records": records, "retried": False})
        raise


def audit_study(args) -> None:
    state = load_state(args.study_dir)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    versions = json.loads((args.study_dir / "versions.json").read_text())
    checked = []
    run_directory = getattr(args, "run_kind", None) or state.get("run_kind", "pilot")
    for folder in sorted((args.study_dir / run_directory).iterdir()):
        if not folder.is_dir() or not (folder / "response.json").exists():
            continue
        job = json.loads((folder / "job.json").read_text())
        client = HarnessClient(state["project_endpoint"], state["subscription"], state["agent_name"],
                               agent_version=versions[job["model_id"]]["version"], timeout=30)
        saved = json.loads((folder / "response.json").read_text())
        live = client.get_response(saved["id"])
        target = output / folder.name
        target.mkdir()
        save(target / "response.json", live)
        for key in ["id", "status", "model", "agent_reference", "conversation", "output", "usage",
                    "created_at", "completed_at"]:
            if normalized_output(live.get(key)) != normalized_output(saved.get(key)):
                raise HarnessError(f"Service audit changed {key}.")
        request = json.loads((folder / "request.json").read_text())
        prompt, files = task_payload(state, job["case_id"])
        expected_agent = {"type": "agent_reference", "name": state["agent_name"],
                          "version": versions[job["model_id"]]["version"]}
        if live.get("model") != versions[job["model_id"]]["model"] or live.get("agent_reference") != expected_agent:
            raise HarnessError("Service audit returned an unexpected model/version binding.")
        returned_conversation = live.get("conversation")
        returned_conversation = (returned_conversation.get("id") if isinstance(returned_conversation, dict)
                                 else returned_conversation)
        if (request["input"] != file_input(prompt, files) or request["agent_reference"] != expected_agent
                or request["conversation"] != returned_conversation):
            raise HarnessError("Audit request differs from frozen task or pinned model version.")
        receiver = {"status": "not_required"}
        if files and live["status"] == "completed":
            hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
            try:
                verify_consumption(live, hash_command(hashes), hashes, allow_shell_wrappers=True)
                receiver = {"status": "verified"}
            except HarnessError as error:
                receiver = {"status": "failed", "error": str(error)}
        if (folder / "downloads").exists():
            paths = client.download_outputs(live, target / "downloads")
            for path in paths:
                if path.read_bytes() != (folder / "downloads" / path.name).read_bytes():
                    raise HarnessError("Service audit output bytes differ.")
        checked.append({"directory": folder.name, "response_id": live["id"],
                        "receiver_verification": receiver})
    save(output / "audit.json", {"status": "reconciled", "records": checked, "paid_invocations": 0})


def report(args) -> None:
    state = load_state(args.study_dir)
    pilot_dir = args.study_dir / "full"
    rows = []
    for path in sorted(pilot_dir.glob("record-*.json"), key=lambda p: int(p.stem.split("-")[-1])):
        record = json.loads(path.read_text())
        number = int(path.stem.split("-")[-1])
        folder = next(p for p in pilot_dir.iterdir() if p.is_dir() and p.name.startswith(f"{number:03d}-"))
        detail = json.loads((folder / "summary.json").read_text()) if (folder / "summary.json").exists() else {}
        usage = detail.get("usage") or {}
        worker_detail = json.loads((folder / "worker-success.json").read_text()) if (folder / "worker-success.json").exists() else {}
        failure = json.loads((folder / "worker-failure.json").read_text()) if (folder / "worker-failure.json").exists() else {}
        row = {
            "model": record["model"], "case": record["case"], "readiness_only": record["case"] == "gate",
            "repeat": record.get("repeat"), "expected_model": detail.get("expected_model"),
            "returned_model": detail.get("returned_model"), "agent_version": detail.get("agent_version"),
            "status": record["status"], "correct": record.get("grading", {}).get("correct"),
            "worker_error": failure.get("error"), "record_error": record.get("error"),
            "contract_ok": record.get("grading", {}).get("contract_ok"),
            "grading_detail": record.get("grading", {}).get("detail"),
            "client_seconds": record["client_seconds"], "service_seconds": detail.get("service_seconds"),
            "invocation_seconds": worker_detail.get("invocation_seconds"),
            "including_download_seconds": worker_detail.get("including_download_seconds"),
            "evaluation_seconds": record.get("grading", {}).get("evaluation_seconds"),
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
            "reasoning_output_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
            "total_tokens": usage.get("total_tokens"), "parent_shell_calls": detail.get("parent_shell_calls"),
            "parent_function_calls": detail.get("parent_function_calls"),
            "visible_tool_calls": detail.get("visible_tool_calls"),
            "visible_shell_commands": detail.get("visible_shell_commands"),
            "nonzero_shell_results": detail.get("nonzero_shell_results"), "internal_model_loops": None,
            "checks_passed": record.get("grading", {}).get("checks_passed"),
            "checks_total": record.get("grading", {}).get("checks_total"),
            "estimated_usd": None, "actual_billed_usd": None,
            "backend_model_calls": None, "cold_warm_type": None,
            "response_id": detail.get("response_id"), "directory": folder.name,
        }
        rows.append(row)
    by_model = {}
    for model in state["study"]["models"]:
        selected = [r for r in rows if r["model"] == model["id"]]
        scored = [r for r in selected if not r["readiness_only"]]
        by_model[model["id"]] = {
            "gate_outcomes": [r["status"] for r in selected if r["readiness_only"]],
            "scored_attempted": len(scored), "scored_correct": sum(r["correct"] is True for r in scored),
            "outcomes": dict(Counter(r["status"] for r in scored)),
            "scope": "Six fixed cases, three repetitions. Readiness is excluded from task correctness.",
        }
    args.output_dir.mkdir(parents=True, exist_ok=False)
    save(args.output_dir / "results.json", {
        "records": rows, "models": by_model, "server_telemetry": state["server_telemetry"],
        "pricing": state["pricing"], "study_id": state["study"]["study_id"],
        "source_sha256": state["source_sha256"], "prior_results_pooled": False,
        "limitations": [
            "Gate outcomes excluded from scored-task correctness/latency comparisons.",
            "No private reasoning inferred from tool counts; mandatory input-hash calls are not calculation.",
            "Missing usage/backend/pricing fields are unknown, not zero.",
            "Capacity/SKU/provider defaults differ and are recorded; not a load benchmark.",
            "Internal model loops/backend calls are unavailable; visible tools are not loop counts.",
            "Incomplete or paused coverage is not counted as an incorrect completed answer.",
        ],
    })
    with (args.output_dir / "runs.csv").open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["model", "status"])
        writer.writeheader()
        writer.writerows({k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in row.items()}
                         for row in rows)
    prompts = {}
    for case_id in ["gate", *(c["id"] for c in state["study"]["cases"])]:
        prompt, files = task_payload(state, case_id)
        prompts[case_id] = {"exact_user_prompt": prompt,
                           "input_sha256": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}
    save(args.output_dir / "prompts.json",
         {"common_agent_instructions": instructions(), "cases": prompts})
    lines = ["# Model comparison results", "",
             "Same neutral agent instructions/tools and task inputs; only the model binding changes.",
             "Six task types, three repetitions per eligible model.", "",
             "## What was tested", "",
             "- Readiness: execute the supplied Python sum 1..100 script and return its actual verified file.",
             "- Compute: exact prime count, multiplication and modular exponentiation.",
             "- Constraints: minimum-cost assignment subject to forbidden job/depot pairs.",
             "- Transform: exact CSV/Decimal aggregation with duplicates and invalid-row handling.",
             "- Implement: write interval-merging code, including invalid-input and mutation checks.",
             "- Debug: repair signed account totals/deduplication with hidden edge tests.",
             "- Evidence: distinguish supported, contradicted and unknown business claims from supplied data.",
             "",
             "See prompts.json for every exact user prompt, common instructions and input hashes.", "",
             "| Model | Gate | Scored attempts | Correct |",
             "|-------|------|-----------------|---------|"]
    for model, data in by_model.items():
        gate = ', '.join(data['gate_outcomes']) or "not run"
        lines.append(f"| {model} | {gate} | "
                     f"{data['scored_attempted']} | {data['scored_correct']} |")
    lines += ["", "## Matched task coverage", "",
              "| Model | Case | Attempts / planned | Correct | Mean wall seconds | Mean input tokens | Mean output tokens |",
              "|-------|------|--------------------|---------|-------------------|-------------------|--------------------|"]
    for model in state["study"]["models"]:
        for case in state["study"]["cases"]:
            selected = [r for r in rows if r["model"] == model["id"] and r["case"] == case["id"]]
            planned = state["study"]["full_repetitions"]
            def mean(field: str) -> str:
                values = [r[field] for r in selected if r[field] is not None]
                return f"{sum(values)/len(values):.1f}" if values else "unknown"
            lines.append(f"| {model['id']} | {case['id']} | {len(selected)} / {planned} | "
                         f"{sum(r['correct'] is True for r in selected)} | {mean('client_seconds')} | "
                         f"{mean('input_tokens')} | {mean('output_tokens')} |")
    summary = pilot_dir / "result.json"
    if summary.exists():
        value = json.loads(summary.read_text())
        lines += ["", f"Run status: {value['status']}; {value['attempted']} invocation attempts; "
                  f"paused models: {', '.join(value['paused']) or 'none'}.",
                  "Missing coverage is untested, not a zero-quality score."]
    lines += ["", "Costs and server-only metrics are unavailable, not zero.",
              "See results.json and runs.csv for raw field coverage and individual outcomes.",
              "Wall time includes client overhead and grading. Service seconds are reported separately.",
              "Visible tool calls and nonzero shell results are captured; internal model-loop counts are unknown.",
              "Unresolved or failed gates are not evidence of intrinsic model answer quality."]
    (args.output_dir / "report.md").write_text("\n".join(lines) + "\n")


def pinned(client: RecordingClient, version: str) -> JsonObject:
    if not re.fullmatch(r"[1-9][0-9]*", version):
        raise HarnessError("Require explicit positive integer versions.")
    latest = client.get_agent()["versions"]["latest"]
    if latest["version"] == version:
        return latest
    return json.loads(client._request(
        client._agent_path + "/versions/" + urllib.parse.quote(version, safe=""),
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare", help="Read-only Azure preflight and local schedule/manifest preparation.")
    for name in ("project-endpoint", "subscription", "resource-group", "account", "project", "python-image",
                 "agent-name", "stop-starting-at"):
        setup.add_argument("--" + name, required=True)
    setup.add_argument("--region", default="westus")
    setup.add_argument("--models", type=Path, required=True)
    setup.add_argument("--max-invocations", type=int, required=True)
    setup.add_argument("--output-dir", type=Path, required=True)
    deployment = commands.add_parser("deploy")
    deployment.add_argument("--study-dir", type=Path, required=True)
    deployment.add_argument("--allow-azure-writes", action="store_true")
    deployment.add_argument("--allow-project-role-grants", action="store_true")
    for name in ("run", "smoke"):
        run = commands.add_parser(name)
        run.add_argument("--study-dir", type=Path, required=True)
        run.add_argument("--allow-paid-invocations", action="store_true")
        run.add_argument("--max-invocations", type=int, required=True)
    for command in ("audit", "report"):
        analysis = commands.add_parser(command)
        analysis.add_argument("--study-dir", type=Path, required=True)
        analysis.add_argument("--output-dir", type=Path, required=True)
        if command == "audit":
            analysis.add_argument("--run-kind", choices=["full", "smoke"])
    work = commands.add_parser("_worker", help="Internal bounded worker; not a retry/recovery command.")
    work.add_argument("--job", type=Path, required=True)
    work.add_argument("--allow-paid-invocations", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args)
        elif args.command == "deploy":
            deploy(args)
        elif args.command == "run":
            run_study(args)
        elif args.command == "smoke":
            smoke_study(args)
        elif args.command == "audit":
            audit_study(args)
        elif args.command == "report":
            report(args)
        else:
            if not args.allow_paid_invocations:
                raise HarnessError("Worker requires explicit paid invocation opt-in.")
            worker(args.job)
        return 0
    except (HarnessError, EvaluationInfrastructureError, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError) as error:
        if args.command == "_worker" and args.job.is_file():
            path = args.job.parent / "worker-failure.json"
            if not path.exists():
                save(path, {"error": str(error), "retried": False})
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
