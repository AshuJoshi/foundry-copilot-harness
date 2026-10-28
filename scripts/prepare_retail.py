"""Acquire a pinned real workbook and prepare chronological retail evidence offline."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import sys
import urllib.request
import zipfile

from harness_client import HarnessError
from retail_evidence import amount_fields, references

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "agents/retail-analysis/data-policy.json"
COLUMNS = [
    "source_row_id", "sheet", "source_row", "period", "invoice", "stock_code",
    "description", "quantity", "invoice_date", "unit_price", "customer_id", "country",
    "included", "exclusion_reason", "cancellation_flag",
]
HEADERS = ["Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def config() -> dict:
    value = json.loads(CONFIG.read_text())
    if value["schema_version"] != 1 or len(value["periods"]) != 4:
        raise HarnessError("Unexpected retail experiment configuration.")
    previous = None
    ids = set()
    for period in value["periods"]:
        start, end = datetime.fromisoformat(period["start"]), datetime.fromisoformat(period["end"])
        if period["id"] in ids or end-start != timedelta(days=7) or (previous and start != previous):
            raise HarnessError("Retail periods must be distinct adjacent seven-day intervals.")
        ids.add(period["id"])
        previous = end
    if [s["visible_periods"] for s in value["stages"][:3]] != [
        [p["id"] for p in value["periods"][:n]] for n in [2, 3, 4]
    ] or value["stages"][3] != {"id": "stable-control", "same_evidence_as": "stage-3"}:
        raise HarnessError("Evidence visibility schedule differs from the predeclared design.")
    return value


def download(output: Path) -> None:
    settings = config()
    output.mkdir(parents=True, exist_ok=False)
    try:
        with urllib.request.urlopen(settings["url"], timeout=180) as response:
            raw = response.read(settings["archive_bytes"] + 1)
        if len(raw) != settings["archive_bytes"] or hashlib.sha256(raw).hexdigest() != settings["archive_sha256"]:
            raise HarnessError("Retail archive fingerprint mismatch.")
        with (output / "source.zip").open("xb") as stream:
            stream.write(raw)
        save(output / "download.json", {"status": "verified", "sha256": settings["archive_sha256"]})
    except (HarnessError, OSError, ValueError) as error:
        save(output / "failure.json", {"error": str(error)})
        raise


def cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)


def normalize(values: tuple, sheet: str, number: int, settings: dict) -> dict | None:
    if len(values) != 8:
        raise HarnessError("Workbook record width changed.")
    date = values[4]
    if not isinstance(date, datetime):
        raise HarnessError(f"Missing/non-datetime invoice timestamp at {sheet}:{number}")
    if cell(values[7]) != settings["country"]:
        return None
    period = next((p["id"] for p in settings["periods"]
                   if datetime.fromisoformat(p["start"]) <= date < datetime.fromisoformat(p["end"])), None)
    if period is None:
        return None
    quantity, price = cell(values[3]), cell(values[5])
    _, _, reason = amount_fields(quantity, price)
    invoice = cell(values[0])
    return dict(zip(COLUMNS, [
        f"{sheet}:{number}", sheet, str(number), period, invoice, cell(values[1]),
        cell(values[2]), quantity, date.isoformat(), price, cell(values[6]), cell(values[7]),
        str(not reason).lower(), reason, str(invoice.lower().startswith("c")).lower(),
    ]))


def write_csv(path: Path, rows: list[dict]) -> None:
    import csv
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build(source: Path, output: Path) -> None:
    settings = config()
    if source.stat().st_size != settings["archive_bytes"] or sha(source) != settings["archive_sha256"]:
        raise HarnessError("Pinned retail source archive changed.")
    try:
        import openpyxl
    except ImportError as error:
        raise HarnessError("Install the pinned retail-prep optional dependency.") from error
    if openpyxl.__version__ != "3.1.5":
        raise HarnessError("Require openpyxl 3.1.5.")
    with zipfile.ZipFile(source) as archive:
        if archive.namelist() != [settings["workbook"]]:
            raise HarnessError("Unexpected retail archive members.")
        if archive.getinfo(settings["workbook"]).file_size != settings["workbook_bytes"]:
            raise HarnessError("Workbook size mismatch.")
        raw = archive.read(settings["workbook"])
    if hashlib.sha256(raw).hexdigest() != settings["workbook_sha256"]:
        raise HarnessError("Workbook fingerprint mismatch.")
    output.mkdir(parents=True, exist_ok=False)
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        selected = []
        sheet_counts = {}
        try:
            for sheet in workbook:
                rows = sheet.iter_rows(values_only=True)
                if list(next(rows)) != HEADERS:
                    raise HarnessError(f"Workbook column schema changed: {sheet.title}")
                count = 0
                for number, values in enumerate(rows, 2):
                    count += 1
                    row = normalize(values, sheet.title, number, settings)
                    if row:
                        selected.append(row)
                sheet_counts[sheet.title] = count
        finally:
            workbook.close()
        if sheet_counts != settings["source_sheet_rows"]:
            raise HarnessError("Pinned workbook sheet/record counts changed.")
        selected.sort(key=lambda r: (r["invoice_date"], r["sheet"], int(r["source_row"])))
        if not selected:
            raise HarnessError("No real transactions in predeclared country/windows.")
        evaluator = output / "evaluator"
        evaluator.mkdir()
        reference_records = {}
        stage_records = {}
        prior_rows = set()
        for stage in settings["stages"][:3]:
            folder = output / stage["id"]
            folder.mkdir()
            visible = [r for r in selected if r["period"] in stage["visible_periods"]]
            expected_periods = set(stage["visible_periods"])
            if {r["period"] for r in visible} != expected_periods:
                raise HarnessError("A predeclared observation period is empty.")
            ids = {r["source_row_id"] for r in visible}
            if not prior_rows <= ids:
                raise HarnessError("Chronological update lost earlier evidence.")
            prior_rows = ids
            write_csv(folder / "evidence.csv", visible)
            if (folder / "evidence.csv").stat().st_size >= 16_000_000:
                raise HarnessError("Stage CSV exceeds conservative materialization byte limit.")
            shutil.copyfile(ROOT / "agents/retail-analysis/DATA-NOTICE.txt", folder / "DATA-NOTICE.txt")
            policy = dict(settings["metric_policy"])
            save(folder / "policy.json", policy)
            # The agent receives only its stage/cutoff, never the future schedule or references.
            manifest = {
                "stage_id": stage["id"], "visible_periods": stage["visible_periods"],
                "available_before": next(p["end"] for p in settings["periods"]
                                         if p["id"] == stage["visible_periods"][-1]),
                "rows": len(visible), "evidence_sha256": sha(folder / "evidence.csv"),
                "policy_sha256": sha(folder / "policy.json"),
            }
            save(folder / "manifest.json", manifest)
            reference_records[stage["id"]] = references(folder / "evidence.csv")
            stage_records[stage["id"]] = manifest
        # Stable control reuses the exact file and filename, not a renamed duplicate attachment.
        stage_records["stable-control"] = dict(
            stage_id="stable-control", evidence_from="stage-3/evidence.csv",
            evidence_sha256=stage_records["stage-3"]["evidence_sha256"],
            rows=stage_records["stage-3"]["rows"],
        )
        save(evaluator / "references.json", dict(
            reference_records, **{"stable-control": reference_records["stage-3"]},
        ))
        save(evaluator / "stage-plan.json", stage_records)
        save(output / "preparation.json", {
            "status": "verified", "source_sha256": settings["archive_sha256"],
            "workbook_sha256": settings["workbook_sha256"],
            "config_sha256": sha(CONFIG), "preparer_sha256": sha(Path(__file__)),
            "reference_helper_sha256": sha(ROOT / "retail_evidence.py"),
            "openpyxl_version": openpyxl.__version__, "sheet_rows_scanned": sheet_counts,
            "selected_country": settings["country"], "selected_rows": len(selected),
            "stages": stage_records,
            "scope": "Offline real-data preparation/references; no agent deployment or invocation.",
        })
    except (HarnessError, OSError, ValueError, KeyError) as error:
        save(output / "failure.json", {"error": str(error)})
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("download")
    fetch.add_argument("--output-dir", type=Path, required=True)
    prepare = commands.add_parser("build")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            download(args.output_dir)
        else:
            build(args.source, args.output_dir)
        print(f"{args.command} verified: {args.output_dir}")
        return 0
    except (HarnessError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
