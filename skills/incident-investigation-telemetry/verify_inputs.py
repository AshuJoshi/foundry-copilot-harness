"""Verify telemetry packaging without diagnosing the incident."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys


def verify(directory: Path) -> dict[str, object]:
    manifest = json.loads((directory / "input-manifest.json").read_text())
    files = []
    names = [item["name"] for item in manifest["files"]]
    if (len(names) != len(set(names)) or not {"logs.csv", "metrics.csv"}.issubset(names)
            or not any(re.fullmatch(r"traces-\d{3}\.csv", name) for name in names)
            or any(name not in {"logs.csv", "metrics.csv"}
                   and not re.fullmatch(r"traces-\d{3}\.csv", name) for name in names)):
        raise ValueError("Unexpected input files.")
    for expected in manifest["files"]:
        name = expected["name"]
        source_skill = expected.get("source_skill")
        if name in {"logs.csv", "metrics.csv"}:
            source = directory / name
            if source_skill is not None:
                raise ValueError(f"Unexpected source Skill: {name}")
        else:
            allowed = {
                "traces-001.csv": "incident-trace-data-part-one",
                "traces-002.csv": "incident-trace-data-part-two",
            }
            if allowed.get(name) != source_skill:
                raise ValueError(f"Unexpected source Skill: {name}")
            source = directory.parent / source_skill / name
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError(f"CSV hash mismatch: {name}")
        reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""))
        count = 0
        for count, row in enumerate(reader, 1):
            if (row["evidence_row_id"] != str(expected["first_row_id"] + count - 1)
                    or None in row or None in row.values()):
                raise ValueError(f"Invalid record: {name}/{count}")
        if count != expected["rows"] or reader.fieldnames != expected["columns"]:
            raise ValueError(f"CSV shape mismatch: {name}")
        files.append(expected)
    return {"case_id": manifest["case_id"], "files": files, "status": "verified"}


if __name__ == "__main__":
    try:
        print(json.dumps(verify(Path(__file__).resolve().parent)))
    except (OSError, ValueError, KeyError) as error:
        print(f"ERROR: input verification failed: {error}", file=sys.stderr)
        raise SystemExit(1)
