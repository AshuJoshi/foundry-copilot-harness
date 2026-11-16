"""Validate repair artifacts and execution, independently of benchmark grading."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

from harness_client import HarnessError, JsonObject, json_object, required_string

SKILL_NAME = "software-repair"
SKILLS = {SKILL_NAME, "software-repair-repository-data"}


def validate_execution(response: JsonObject, expected_verification: JsonObject) -> None:
    output = response.get("output", [])
    calls = {
        item["call_id"]: json.loads(item.get("arguments", "{}")).get("skill")
        for item in output
        if item.get("type") == "function_call" and item.get("name") == "skill"
    }
    loaded = {
        calls.get(item.get("call_id")) for item in output
        if item.get("type") == "function_call_output"
        and item.get("call_id") in calls
        and item.get("status") == "completed"
        and "loaded successfully" in str(item.get("output", ""))
    }
    if not SKILLS.issubset(loaded):
        raise HarnessError("Missing correlated successful repair/data Skill loads.")
    if any(item.get("type") == "mcp_call" for item in output):
        raise HarnessError("Repair run used an external MCP tool.")
    shells = {i["call_id"]: i for i in output if i.get("type") == "shell_call"}
    for item in output:
        call = shells.get(item.get("call_id"))
        if item.get("type") != "shell_call_output" or call is None:
            continue
        commands = call.get("action", {}).get("commands", [])
        chunks = item.get("output", [])
        if len(commands) != len(chunks):
            continue
        for command, chunk in zip(commands, chunks):
            if (re.fullmatch(r"python3\s+/\S*/verify_inputs\.py", command.strip())
                    and chunk.get("outcome", {}).get("exit_code") == 0):
                try:
                    value, _ = json.JSONDecoder().raw_decode(chunk.get("stdout", "").lstrip())
                except json.JSONDecodeError:
                    continue
                if value == expected_verification and value.get("status") == "verified":
                    return
    raise HarnessError("No exit-zero standalone repository verifier matching expected inputs.")


def validate_artifacts(paths: list[Path], task: JsonObject, repository: Path) -> JsonObject:
    if len(paths) != 2 or {p.name for p in paths} != {"repair.json", "report.md"}:
        raise HarnessError("Expected exactly repair.json and report.md.")
    files = {p.name: p for p in paths}
    repair = json_object(json.loads(files["repair.json"].read_text()), "Repair")
    if (type(repair.get("schema_version")) is not int or repair["schema_version"] != 1
            or repair.get("case_id") != task["case_id"]
            or repair.get("base_commit") != task["base_commit"]):
        raise HarnessError("Repair schema or pinned task identity mismatch.")
    if repair.get("status") not in {"candidate", "blocked"}:
        raise HarnessError("Repair status must be candidate or blocked.")
    required_string(repair.get("summary"), "Repair summary")
    required_string(files["report.md"].read_text(), "Repair report")
    for key in ["changed_files", "tests", "limitations"]:
        if not isinstance(repair.get(key), list):
            raise HarnessError(f"Repair {key} must be a list.")
    for limitation in repair["limitations"]:
        required_string(limitation, "Repair limitation")
    for test in repair["tests"]:
        record = json_object(test, "Test record")
        required_string(record.get("command"), "Test command")
        required_string(record.get("observation"), "Test observation")
        if record.get("exit_code") is not None and type(record["exit_code"]) is not int:
            raise HarnessError("Test exit_code must be an integer or null.")
    patch = repair.get("patch")
    if not isinstance(patch, str):
        raise HarnessError("Repair patch must be a string transported inside JSON.")
    if repair["status"] == "blocked":
        if patch.strip() or repair["changed_files"] or not repair["limitations"]:
            raise HarnessError("Blocked repair must have no patch/files and explain limitations.")
        return {"status": "blocked", "scope": "No candidate patch to evaluate."}
    headers = re.findall(r"^diff --git a/(\S+) b/(\S+)$", patch, re.MULTILINE)
    if (not headers or len(headers) != len(re.findall(r"^diff --git ", patch, re.MULTILINE))
            or re.search(r"^(?:new file mode|deleted file mode|old mode|new mode|"
                         r"rename |copy |GIT binary patch|Binary files)", patch, re.MULTILINE)):
        raise HarnessError("Unsupported patch structure or file operation.")
    changed = []
    for old, new in headers:
        path = PurePosixPath(new)
        if (old != new or path.is_absolute() or ".." in path.parts or "\\" in new
                or not new.startswith("src/") or path.suffix != ".py"
                or (repository / new).is_symlink() or not (repository / new).is_file()
                or not (repository / new).resolve().is_relative_to(repository.resolve())):
            raise HarnessError(f"Patch path is not an existing allowed source file: {new}")
        changed.append(new)
    if len(set(changed)) != len(changed) or repair["changed_files"] != changed:
        raise HarnessError("Patch paths and declared changed_files differ.")
    with tempfile.TemporaryDirectory(prefix="repair-patch-validation-") as temporary:
        patch_file = Path(temporary) / "patch.diff"
        patch_file.write_text(patch, encoding="utf-8")
        result = subprocess.run(
            ["git", "apply", "--check", str(patch_file)],
            cwd=repository, capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise HarnessError(f"Patch does not apply to the pinned snapshot: {result.stderr.strip()}")
        stats = subprocess.run(
            ["git", "apply", "--numstat", str(patch_file)],
            cwd=repository, capture_output=True, text=True, timeout=30,
        )
    if stats.returncode or [line.split("\t")[-1] for line in stats.stdout.splitlines()] != changed:
        raise HarnessError("Actual Git patch targets differ from declared source paths.")
    return {
        "status": "passed", "changed_files": changed,
        "scope": "Identity, schema, source-only paths and clean application; tests graded separately.",
    }
