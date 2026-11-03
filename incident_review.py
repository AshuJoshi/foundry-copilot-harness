"""Offline contracts and evidence bindings for the designed two-agent incident review."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import zipfile

from harness_client import HarnessError
from incident_investigation import FILES, validate_artifacts

ROOT = Path(__file__).parent
SCHEMAS = ROOT / "agents/incident-review/schemas.json"
VOCABULARY = {
    "ref", "type", "enum", "minLength", "pattern", "minimum", "required",
    "properties", "additionalProperties", "minProperties", "values", "items", "minItems",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contract(value: object, name: str) -> None:
    definitions = json.loads(SCHEMAS.read_text())["definitions"]

    def check(actual: object, rule: dict, location: str) -> None:
        if set(rule) - VOCABULARY:
            raise HarnessError(f"Unsupported schema vocabulary: {location}")
        if "ref" in rule:
            if set(rule) != {"ref"} or rule["ref"] not in definitions:
                raise HarnessError("Invalid contract reference.")
            check(actual, definitions[rule["ref"]], location)
            return
        types = rule.get("type")
        types = [types] if isinstance(types, str) else types
        valid = {
            "object": isinstance(actual, dict), "array": isinstance(actual, list),
            "string": isinstance(actual, str), "integer": type(actual) is int,
            "null": actual is None,
        }
        if not types or any(t not in valid for t in types) or not any(valid[t] for t in types):
            raise HarnessError(f"Contract type mismatch: {location}")
        if "enum" in rule and actual not in rule["enum"]:
            raise HarnessError(f"Contract enum mismatch: {location}")
        if isinstance(actual, str):
            if len(actual.strip()) < rule.get("minLength", 0):
                raise HarnessError(f"Empty contract text: {location}")
            if "pattern" in rule and not re.search(rule["pattern"], actual):
                raise HarnessError(f"Contract pattern mismatch: {location}")
        elif type(actual) is int:
            if actual < rule.get("minimum", actual):
                raise HarnessError(f"Contract minimum mismatch: {location}")
        elif isinstance(actual, dict):
            if len(actual) < rule.get("minProperties", 0) or set(rule.get("required", [])) - actual.keys():
                raise HarnessError(f"Missing contract fields: {location}")
            properties = rule.get("properties", {})
            if rule.get("additionalProperties") is False and actual.keys() - properties.keys():
                raise HarnessError(f"Unexpected contract fields: {location}")
            for key, item in actual.items():
                child = properties.get(key, rule.get("values"))
                if child:
                    check(item, child, location + "/" + key)
        elif isinstance(actual, list):
            if len(actual) < rule.get("minItems", 0):
                raise HarnessError(f"Too few contract items: {location}")
            for index, item in enumerate(actual):
                check(item, rule["items"], location + "/" + str(index))

    if name not in definitions:
        raise HarnessError(f"Unknown contract: {name}")
    check(value, definitions[name], name)


def validate_diagnosis(value: dict, archive: Path, case_id: str) -> dict:
    contract(value, "diagnosis")
    # Reuse the established diagnosis/evidence checker without changing peer artifacts.
    with tempfile.TemporaryDirectory(prefix="incident-review-diagnosis-") as temporary:
        root = Path(temporary)
        diagnosis, report = root / "diagnosis.json", root / "report.md"
        diagnosis.write_text(json.dumps(value))
        report.write_text(" ".join(e["id"] for e in value["evidence"]))
        return validate_artifacts([diagnosis, report], archive, case_id)


def pointer(document: object, path: str) -> object:
    if not path.startswith("/"):
        raise HarnessError("Claim pointer must address a specific field.")
    value = document
    try:
        for raw in path[1:].split("/"):
            if re.search(r"~(?![01])", raw):
                raise HarnessError("Malformed JSON pointer escape.")
            key = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", key):
                    raise HarnessError("Invalid JSON pointer index.")
                value = value[int(key)]
            elif isinstance(value, dict):
                value = value[key]
            else:
                raise HarnessError("JSON pointer crosses a scalar.")
    except (KeyError, IndexError) as error:
        raise HarnessError(f"Claim pointer does not resolve: {path}") from error
    return value


def verify_evidence(evidence: list[dict], archive: Path) -> set[str]:
    identifiers = set()
    wanted = {}
    for item in evidence:
        contract(item, "evidence")
        key = (item["file"], item["evidence_row_id"])
        if item["id"] in identifiers or key in wanted or "evidence_row_id" in item["observed_values"]:
            raise HarnessError("Duplicate evidence or ID-only citation.")
        identifiers.add(item["id"])
        wanted[key] = item
    found = set()
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.namelist()) != 3 or set(bundle.namelist()) != FILES:
            raise HarnessError("Unexpected source archive members.")
        for name in FILES:
            with bundle.open(name) as binary, io.TextIOWrapper(binary, encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    key = (name, int(row["evidence_row_id"]))
                    if key not in wanted:
                        continue
                    if key in found:
                        raise HarnessError("Ambiguous source record ID.")
                    item = wanted[key]
                    if any(column not in row or row[column] != observed
                           for column, observed in item["observed_values"].items()):
                        raise HarnessError(f"Exact evidence cell mismatch: {item['id']}")
                    found.add(key)
    if found != wanted.keys():
        raise HarnessError("Cited source record missing.")
    return identifiers


def freeze(
    independent: Path, diagnosis: Path, manifest: Path, archive: Path, output: Path,
) -> dict:
    inputs = json.loads(manifest.read_text())
    case_id = inputs["case_id"]
    for artifact in [independent, diagnosis]:
        validate_diagnosis(json.loads(artifact.read_text()), archive, case_id)
    value = {
        "schema_version": 1, "case_id": case_id, "input_manifest_sha256": sha(manifest),
        "archive_sha256": sha(archive),
        "independent_sha256": sha(independent), "diagnosis_sha256": sha(diagnosis),
    }
    contract(value, "binding")
    with output.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    return value


def validate_critique(
    critique: Path, binding: Path, independent: Path, diagnosis: Path, manifest: Path, archive: Path,
) -> dict:
    value = json.loads(critique.read_text())
    bound = json.loads(binding.read_text())
    contract(value, "critique")
    contract(bound, "binding")
    if (bound["independent_sha256"] != sha(independent)
            or bound["diagnosis_sha256"] != sha(diagnosis)
            or bound["input_manifest_sha256"] != sha(manifest)
            or bound["archive_sha256"] != sha(archive)
            or bound["case_id"] != json.loads(manifest.read_text())["case_id"]
            or any(value[key] != bound[key] for key in
                   ["case_id", "independent_sha256", "diagnosis_sha256"])):
        raise HarnessError("Review handoff identity/hash mismatch.")
    for artifact in [independent, diagnosis]:
        validate_diagnosis(json.loads(artifact.read_text()), archive, bound["case_id"])
    identifiers = verify_evidence(value["evidence"], archive)
    original = json.loads(diagnosis.read_text())
    finding_ids = set()
    cited = set()
    for item in [*value["findings"], *value["accepted_claims"]]:
        if "id" in item:
            if item["id"] in finding_ids:
                raise HarnessError("Duplicate finding ID.")
            finding_ids.add(item["id"])
        claim = pointer(original, item["claim_pointer"])
        if not isinstance(claim, str) or item["claim_text"] not in claim:
            raise HarnessError("Quoted claim does not match its diagnosis field.")
        refs = item["evidence_ids"]
        if len(refs) != len(set(refs)) or not set(refs) <= identifiers:
            raise HarnessError("Unknown or duplicate critique evidence references.")
        cited.update(refs)
    if cited != identifiers:
        raise HarnessError("Unused critique evidence or no addressed claims.")
    if value["verdict"] == "agree" and value["findings"]:
        raise HarnessError("Agreement verdict contradicts objection findings.")
    if value["verdict"] == "revise" and not value["findings"]:
        raise HarnessError("Revision verdict requires a concrete finding.")
    return {
        "status": "mechanically_valid", "findings": len(value["findings"]),
        "accepted_claims": len(value["accepted_claims"]),
        "semantic_correctness": "manual_review_required",
        "independence": "not_proven_by_hashes; requires future controller/event provenance",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["freeze", "check"])
    for name in ["independent", "diagnosis", "manifest", "archive"]:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--critique", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            result = freeze(args.independent, args.diagnosis, args.manifest, args.archive, args.binding)
        else:
            if args.critique is None:
                raise HarnessError("check requires --critique.")
            result = validate_critique(args.critique, args.binding, args.independent,
                                       args.diagnosis, args.manifest, args.archive)
        print(json.dumps(result, indent=2))
        return 0
    except (HarnessError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
