import copy
import csv
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from harness_client import HarnessError
from scripts import prepare_retail as preparation
from retail_evidence import amount_fields, check_report, metrics, references

try:
    import openpyxl
except ImportError:
    openpyxl = None


def row(number, quantity="2", price="0.10", invoice="10001", customer=""):
    _, _, reason = amount_fields(quantity, price)
    return {
        "source_row_id": f"sheet:{number}", "sheet": "sheet", "source_row": str(number),
        "period": "baseline", "invoice": invoice, "stock_code": "ITEM",
        "description": 'item,\n"quoted"', "quantity": quantity, "invoice_date": "2010-09-28T12:00:00",
        "unit_price": price, "customer_id": customer, "country": "United Kingdom",
        "included": str(not reason).lower(), "exclusion_reason": reason,
        "cancellation_flag": str(invoice.lower().startswith("c")).lower(),
    }


class RetailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_decimal_policy_and_signs_do_not_assume_cancellations_are_all_negatives(self):
        rows = [row(1), row(2, "-1", "0.10", "C100"), row(3, "-2", "0.05", "A200"),
                row(4, "1", "0.10", "C300"), row(5, "1", "0", "400"),
                row(6, "0", "1"), row(7, "1", "-3"), row(8, "1", "NaN")]
        result = metrics(rows)
        from decimal import Decimal
        self.assertEqual(Decimal(result["gross_positive_value"]), Decimal("0.30"))
        self.assertEqual(Decimal(result["negative_value_magnitude"]), Decimal("0.20"))
        self.assertEqual(Decimal(result["net_transaction_value"]), Decimal("0.10"))
        self.assertEqual(result["included_rows"], 4)
        self.assertEqual(result["excluded_rows"], 4)
        self.assertEqual(result["negative_non_cancellation_rows"], 1)
        self.assertEqual(result["positive_cancellation_rows"], 1)
        self.assertEqual(result["missing_customer_rows"], 8)

    def test_false_flags_and_duplicate_source_ids_rejected(self):
        bad = row(1)
        bad["included"] = "false"
        with self.assertRaisesRegex(HarnessError, "flags"):
            metrics([bad])
        source = self.root / "evidence.csv"
        preparation.write_csv(source, [row(1), row(1)])
        with self.assertRaisesRegex(HarnessError, "Duplicate source"):
            references(source)

    def report(self):
        source = self.root / "evidence.csv"
        preparation.write_csv(source, [row(1), row(2)])
        report = {
            "schema_version": 1, "stage_id": "stage-1", "evidence_sha256": preparation.sha(source),
            "measure": "derived_net_transaction_value_not_profit", "metrics": references(source),
            "claims": [{"id": "A", "kind": "observation", "text": "There are positive transaction lines.",
                        "evidence_refs": [{"source_row_id": "sheet:1",
                                           "observed_values": {"quantity": "2", "unit_price": "0.10"}}]}],
            "claim_updates": [], "limitations": ["No cost or shipment evidence."],
        }
        return source, report

    def test_report_mechanical_validity_is_not_semantic_truth(self):
        source, report = self.report()
        result = check_report(report, source, {"id": "stage-1"})
        self.assertEqual(result["status"], "mechanically_valid")
        self.assertIn("manual_review", result["semantic_grounding_and_revision_quality"])
        report["measure"] = "profit"
        with self.assertRaisesRegex(HarnessError, "measure"):
            check_report(report, source, {"id": "stage-1"})

    def test_wrong_arithmetic_or_excerpt_rejected(self):
        source, report = self.report()
        original = copy.deepcopy(report)
        report["metrics"]["cumulative"]["net_transaction_value"] = "999"
        with self.assertRaisesRegex(HarnessError, "decimal"):
            check_report(report, source, {"id": "stage-1"})
        report = original
        report["claims"][0]["evidence_refs"][0]["observed_values"] = {"description": "item"}
        with self.assertRaisesRegex(HarnessError, "reference"):
            check_report(report, source, {"id": "stage-1"})

    def test_all_prior_claims_require_disposition_even_on_stable_data(self):
        source, report = self.report()
        prior = copy.deepcopy(report)
        report["stage_id"] = "stable-control"
        with self.assertRaisesRegex(HarnessError, "prior claim"):
            check_report(report, source, {"id": "stable-control"}, prior)
        report["claim_updates"] = [{
            "prior_claim_id": "A", "disposition": "retain", "reason": "Same recorded evidence.",
            "evidence_refs": report["claims"][0]["evidence_refs"],
        }]
        self.assertEqual(check_report(report, source, {"id": "stable-control"}, prior)["status"],
                         "mechanically_valid")

    def test_visibility_schedule_and_boundaries(self):
        settings = preparation.config()
        values = ("10001", "ITEM", "Description", 1, datetime(2010, 10, 4), 0.1, None, "United Kingdom")
        self.assertEqual(preparation.normalize(values, "sheet", 2, settings)["period"], "week-1")
        values = (*values[:4], datetime(2010, 10, 25), *values[5:])
        self.assertIsNone(preparation.normalize(values, "sheet", 2, settings))
        with self.assertRaisesRegex(HarnessError, "timestamp"):
            preparation.normalize((*values[:4], None, *values[5:]), "sheet", 2, settings)

    def test_malformed_claims_and_boolean_counts_fail_explicitly(self):
        source, report = self.report()
        original = copy.deepcopy(report)
        report["claims"][0]["kind"] = ["observation"]
        with self.assertRaises(HarnessError):
            check_report(report, source, {"id": "stage-1"})
        report = copy.deepcopy(original)
        report["claims"][0]["evidence_refs"][0]["source_row_id"] = ["sheet:1"]
        with self.assertRaises(HarnessError):
            check_report(report, source, {"id": "stage-1"})
        report = copy.deepcopy(original)
        report["metrics"]["cumulative"]["included_rows"] = True
        with self.assertRaisesRegex(HarnessError, "count"):
            check_report(report, source, {"id": "stage-1"})

    def test_download_hash_failure_and_existing_output(self):
        settings = dict(preparation.config(), archive_bytes=3, archive_sha256="0" * 64)
        with patch.object(preparation, "config", return_value=settings), \
             patch.object(preparation.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")):
            with self.assertRaisesRegex(HarnessError, "fingerprint"):
                preparation.download(self.root / "download")
        self.assertTrue((self.root / "download/failure.json").exists())
        with self.assertRaises(FileExistsError):
            preparation.download(self.root / "download")


@unittest.skipIf(openpyxl is None, "Install retail-prep extra for workbook preparation fixture tests.")
class WorkbookTests(unittest.TestCase):
    def test_complete_offline_stage_build_excludes_future_and_preserves_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = copy.deepcopy(preparation.config())
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "fixture"
            sheet.append(preparation.HEADERS)
            for date in [datetime(2010, 9, 28), datetime(2010, 10, 5),
                         datetime(2010, 10, 12), datetime(2010, 10, 19),
                         datetime(2010, 10, 26)]:
                sheet.append(["10001", "ITEM", 'real,\n"item"', 2, date, 0.1, None, "United Kingdom"])
            sheet.append(["10001", "ITEM", 'real,\n"item"', 2, datetime(2010, 10, 5), 0.1, None, "United Kingdom"])
            binary = io.BytesIO()
            workbook.save(binary)
            raw = binary.getvalue()
            source = root / "source.zip"
            with zipfile.ZipFile(source, "x") as bundle:
                bundle.writestr(settings["workbook"], raw)
            settings.update(
                archive_bytes=source.stat().st_size, archive_sha256=preparation.sha(source),
                workbook_bytes=len(raw), workbook_sha256=hashlib.sha256(raw).hexdigest(),
                source_sheet_rows={"fixture": 6},
            )
            with patch.object(preparation, "config", return_value=settings), \
                 patch.object(preparation.urllib.request, "urlopen", side_effect=AssertionError("Network forbidden")):
                preparation.build(source, root / "prepared")
                with self.assertRaises(FileExistsError):
                    preparation.build(source, root / "prepared")
            result = root / "prepared"
            plan = json.loads((result / "evaluator/stage-plan.json").read_text())
            self.assertEqual([plan[f"stage-{i}"]["rows"] for i in [1, 2, 3]], [3, 4, 5])
            self.assertEqual(plan["stable-control"]["evidence_sha256"], plan["stage-3"]["evidence_sha256"])
            for i in [1, 2, 3]:
                with (result / f"stage-{i}/evidence.csv").open(newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len({r["source_row_id"] for r in rows}), len(rows))
                self.assertTrue(all(r["invoice_date"] < plan[f"stage-{i}"]["available_before"] for r in rows))
                self.assertNotIn("references", {p.name for p in (result / f"stage-{i}").iterdir()})


if __name__ == "__main__":
    unittest.main()
