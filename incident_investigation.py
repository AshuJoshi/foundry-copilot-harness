"""Validate incident outputs against original records, not benchmark answers."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import re
import zipfile

from harness_client import HarnessError, JsonObject, json_object, required_string

SKILL_NAME = "incident-investigation-telemetry"
SKILLS = {SKILL_NAME, "incident-trace-data-part-one", "incident-trace-data-part-two"}
FILES = {"metrics.csv", "logs.csv", "traces.csv"}


def validate_execution(response: JsonObject, expected_inputs: JsonObject) -> None:
    output = response.get("output", [])
    calls = {
        item["call_id"]: json.loads(item.get("arguments", "{}")).get("skill") for item in output
        if item.get("type") == "function_call" and item.get("name") == "skill"
    }
    loaded = {
        calls[item["call_id"]] for item in output
        if item.get("type") == "function_call_output" and item.get("call_id") in calls
        and item.get("status") == "completed"
        and "loaded successfully" in str(item.get("output", ""))
    }
    if not SKILLS.issubset(loaded):
        raise HarnessError("Missing correlated successful incident/data Skill loads.")
    if any(item.get("type") == "mcp_call" for item in output):
        raise HarnessError("Incident run used an external MCP tool.")
    shell = {item["call_id"]: item for item in output if item.get("type") == "shell_call"}
    expected = dict(expected_inputs, status="verified")
    for item in output:
        call = shell.get(item.get("call_id"))
        if item.get("type") != "shell_call_output" or call is None:
            continue
        commands = call.get("action", {}).get("commands", [])
        chunks = item.get("output", [])
        if len(commands) != len(chunks):
            continue
        for command, chunk in zip(commands, chunks):
            if (re.fullmatch(r"python3\s+/\S*/verify_inputs\.py", command.strip())
                    and chunk.get("outcome", {}).get("exit_code") == 0):
                stdout = chunk.get("stdout", "")
                try:
                    actual, _ = json.JSONDecoder().raw_decode(stdout.lstrip())
                except (json.JSONDecodeError, AttributeError):
                    continue
                if actual == expected:
                    return
    raise HarnessError("No standalone exit-zero input verifier with matching hashes and schemas.")


def _text(value: object, label: str) -> str:
    return required_string(value, label)


def validate_artifacts(paths: list[Path], archive: Path, case_id: str) -> JsonObject:
    if {p.name for p in paths} != {"diagnosis.json", "report.md"} or len(paths) != 2:
        raise HarnessError("Expected exactly diagnosis.json and report.md.")
    by_name = {p.name: p for p in paths}
    diagnosis = json_object(json.loads(by_name["diagnosis.json"].read_text()), "Diagnosis")
    if type(diagnosis.get("schema_version")) is not int or diagnosis["schema_version"] != 1:
        raise HarnessError("Invalid diagnosis schema version.")
    if diagnosis.get("case_id") != case_id:
        raise HarnessError("Diagnosis case ID mismatch.")
    status = diagnosis.get("status")
    if not isinstance(status, str) or status not in {"supported", "tentative", "insufficient_evidence"}:
        raise HarnessError("Invalid diagnosis status.")
    if status == "insufficient_evidence":
        if diagnosis.get("originating_service") is not None:
            raise HarnessError("Insufficient evidence requires null originating service.")
    else:
        _text(diagnosis.get("originating_service"), "Originating service")
    _text(diagnosis.get("mechanism"), "Mechanism")
    evidence = diagnosis.get("evidence")
    if not isinstance(evidence, list) or len(evidence) < 3:
        raise HarnessError("At least three evidence records are required.")
    wanted: dict[str, set[int]] = {name: set() for name in FILES}
    ids = set()
    for entry in evidence:
        item = json_object(entry, "Evidence")
        identifier = _text(item.get("id"), "Evidence ID")
        if identifier in ids:
            raise HarnessError("Duplicate evidence ID.")
        ids.add(identifier)
        name = item.get("file")
        row_id = item.get("evidence_row_id")
        if not isinstance(name, str) or name not in FILES or type(row_id) is not int or row_id < 1:
            raise HarnessError("Invalid evidence file or record ID.")
        if row_id in wanted[name]:
            raise HarnessError("Duplicate evidence record.")
        wanted[name].add(row_id)
        values = json_object(item.get("observed_values"), "Observed values")
        if not values or "evidence_row_id" in values or not all(isinstance(v, str) for v in values.values()):
            raise HarnessError("Observed values must contain non-ID columns with exact string cells.")
        _text(item.get("observation"), "Observation")
        _text(item.get("relevance"), "Relevance")
    if any(not rows for rows in wanted.values()):
        raise HarnessError("Evidence must cover metrics, logs and traces.")
    found = {}
    services = set()
    with zipfile.ZipFile(archive) as bundle:
        if set(bundle.namelist()) != FILES:
            raise HarnessError("Invalid original archive members.")
        for name in sorted(FILES):
            with bundle.open(name) as binary, io.TextIOWrapper(binary, encoding="utf-8", newline="") as text:
                for row in csv.DictReader(text):
                    row_id = int(row["evidence_row_id"])
                    if row_id in wanted[name]:
                        found[name, row_id] = row
                    if name == "logs.csv":
                        services.add(row["container_name"])
                    elif name == "traces.csv":
                        services.add(row["serviceName"])
    service = diagnosis.get("originating_service")
    if service is not None and service not in services:
        raise HarnessError("Originating service is not present in telemetry.")
    for item in evidence:
        row = found.get((item["file"], item["evidence_row_id"]))
        if row is None:
            raise HarnessError("Evidence record does not exist.")
        for column, value in item["observed_values"].items():
            if column not in row or row[column] != value:
                raise HarnessError(
                    f"Evidence cell mismatch: {item['id']}/{item['file']}/"
                    f"{item['evidence_row_id']}/{column}."
                )
    alternatives = diagnosis.get("alternatives")
    if not isinstance(alternatives, list) or len(alternatives) < 2:
        raise HarnessError("At least two alternatives are required.")
    for alternative in alternatives:
        item = json_object(alternative, "Alternative")
        _text(item.get("hypothesis"), "Alternative hypothesis")
        _text(item.get("assessment"), "Alternative assessment")
        refs = item.get("evidence_ids")
        if not isinstance(refs, list) or not refs or not all(isinstance(x, str) and x in ids for x in refs):
            raise HarnessError("Alternative cites missing evidence IDs.")
    for section, fields in [
        ("next_test", ("action", "would_change_diagnosis_if")),
        ("mitigation", ("action", "risks", "verification_and_rollback")),
    ]:
        item = json_object(diagnosis.get(section), section)
        for field in fields:
            _text(item.get(field), f"{section}.{field}")
    limitations = diagnosis.get("limitations")
    if not isinstance(limitations, list) or not limitations:
        raise HarnessError("Limitations must be a nonempty list.")
    for value in limitations:
        _text(value, "Limitation")
    report = _text(by_name["report.md"].read_text(), "Report")
    if not all(re.search(rf"(?<!\w){re.escape(identifier)}(?!\w)", report) for identifier in ids):
        raise HarnessError("Report does not reference every evidence ID.")
    return {
        "status": "passed", "case_id": case_id,
        "diagnosis_status": status, "originating_service": service,
        "verified_evidence_records": len(evidence),
        "scope": "Schema and exact source cells only; causal relevance and mitigation need separate review.",
    }
