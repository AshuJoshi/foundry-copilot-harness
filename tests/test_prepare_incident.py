import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness_client import HarnessError
from scripts import prepare_incident as preparation

try:
    import pyarrow as pa
    import pyarrow.csv as arrow_csv
    import pyarrow.parquet as parquet
except ImportError:
    pa = None


def sha(data):
    return hashlib.sha256(data).hexdigest()


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_catalog_is_pinned_and_complete(self):
        for number in range(1, 4):
            catalog, case = preparation.case_config(f"case-{number:03d}")
            self.assertEqual(len(catalog["revision"]), 40)
            self.assertEqual(len(case["files"]), 3)
            self.assertNotIn("inject_time", json.dumps(case))
        with self.assertRaises(HarnessError):
            preparation.case_config("../case-001")

    def test_download_checks_bytes_and_refuses_overwrite(self):
        case = {"files": [{"name": "logs.parquet", "bytes": 3, "sha256": sha(b"abc")}],
                "upstream_case": "upstream"}
        catalog = {"repository": "phamquiluan/RCAEval", "revision": "a" * 40}
        with patch.object(preparation, "case_config", return_value=(catalog, case)), \
             patch.object(preparation.urllib.request, "urlopen", return_value=io.BytesIO(b"abc")) as fetch:
            target = self.root / "download"
            preparation.download("case-001", target)
            self.assertEqual((target / "logs.parquet").read_bytes(), b"abc")
            self.assertIn("/resolve/" + "a" * 40, fetch.call_args.args[0])
            with self.assertRaises(FileExistsError):
                preparation.download("case-001", target)
            self.assertEqual(fetch.call_count, 1)

    def test_bad_download_keeps_failure_record(self):
        case = {"files": [{"name": "logs.parquet", "bytes": 3, "sha256": sha(b"abc")}],
                "upstream_case": "upstream"}
        catalog = {"repository": "phamquiluan/RCAEval", "revision": "a" * 40}
        target = self.root / "bad-download"
        with patch.object(preparation, "case_config", return_value=(catalog, case)), \
             patch.object(preparation.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")):
            with self.assertRaisesRegex(HarnessError, "fingerprint"):
                preparation.download("case-001", target)
        self.assertTrue((target / "failure.json").is_file())
        self.assertFalse((target / "logs.parquet").exists())

    def test_bad_source_rejected(self):
        (self.root / "logs.parquet").write_bytes(b"bad")
        case = {"files": [{"name": "logs.parquet", "bytes": 3, "sha256": sha(b"abc")}]}
        with self.assertRaisesRegex(HarnessError, "fingerprint"):
            preparation.verify_sources(self.root, case)

    def write_traces(self, row_ids=(1, 2, 3, 4)):
        source = self.root / "traces.csv"
        with source.open("w", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["evidence_row_id", "message", "missing"])
            for number in row_ids:
                writer.writerow([number, 'line one\nline "two", \u00e9', ""])
        destinations = []
        for index in [1, 2]:
            folder = self.root / preparation.SKILLS[index]
            folder.mkdir()
            destinations.append(folder / f"traces-{index:03d}.csv")
        return source, destinations

    def test_shards_preserve_multiline_empty_unicode_and_ids(self):
        source, destinations = self.write_traces()
        entries = preparation.shard_traces(source, destinations, 4)
        self.assertEqual([e["first_row_id"] for e in entries], [1, 3])
        self.assertEqual([e["rows"] for e in entries], [2, 2])
        with source.open(newline="") as stream:
            original = list(csv.DictReader(stream))
        restored = []
        for path in destinations:
            with path.open(newline="") as stream:
                restored.extend(csv.DictReader(stream))
        self.assertEqual(original, restored)

    def test_shards_reject_nonsequential_ids(self):
        source, destinations = self.write_traces((1, 2, 4, 5))
        with self.assertRaisesRegex(HarnessError, "nonsequential"):
            preparation.shard_traces(source, destinations, 4)

    def test_shards_reject_wrong_count(self):
        source, destinations = self.write_traces()
        with self.assertRaisesRegex(HarnessError, "extra trace"):
            preparation.shard_traces(source, destinations, 3)

    def test_shards_use_pinned_boundary(self):
        source, destinations = self.write_traces()
        entries = preparation.shard_traces(source, destinations, 4, first_count=3)
        self.assertEqual([entry["rows"] for entry in entries], [3, 1])
        self.assertEqual([entry["first_row_id"] for entry in entries], [1, 4])

    def make_packages(self):
        members = [
            ["SKILL.md", "verify_inputs.py", "DATA-NOTICE.txt",
             "metrics.csv", "logs.csv", "input-manifest.json"],
            ["SKILL.md", "DATA-NOTICE.txt", "traces-001.csv"],
            ["SKILL.md", "DATA-NOTICE.txt", "traces-002.csv"],
        ]
        for skill, names in zip(preparation.SKILLS, members):
            folder = self.root / skill
            folder.mkdir()
            for name in names:
                (folder / name).write_text("data\n")
        return self.root

    def test_package_size_and_member_set_checked(self):
        packages = self.make_packages()
        self.assertEqual(len(preparation.check_packages(packages, "answer-bearing-upstream")), 3)
        with self.assertRaisesRegex(HarnessError, "byte limit"):
            preparation.check_packages(packages, "answer-bearing-upstream", limit=10)
        (packages / preparation.SKILLS[0] / "inject_time.txt").write_text("123")
        with self.assertRaisesRegex(HarnessError, "members"):
            preparation.check_packages(packages, "answer-bearing-upstream")

    def test_package_rejects_upstream_identity(self):
        packages = self.make_packages()
        (packages / preparation.SKILLS[0] / "input-manifest.json").write_text("answer-bearing-upstream")
        with self.assertRaisesRegex(HarnessError, "leaked"):
            preparation.check_packages(packages, "answer-bearing-upstream")


@unittest.skipIf(pa is None, "Install the incident-prep extra for Parquet preparation tests.")
class ArrowPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.table = pa.table({
            "time": pa.array([1, 2, 3, 4], type=pa.int64()),
            "value": pa.array([0.1, None, 1.2345678901234567, -0.0], type=pa.float64()),
            "text": pa.array(["", None, 'multi\nline,"quoted"', "\u00e9"], type=pa.large_string()),
        })
        self.entries = []
        for name in ["logs.parquet", "metrics.parquet", "traces.parquet"]:
            path = self.source / name
            parquet.write_table(self.table, path)
            with_id = self.table.add_column(0, "evidence_row_id", pa.array(range(1, 5)))
            buffer = pa.BufferOutputStream()
            arrow_csv.write_csv(with_id, buffer)
            self.entries.append({
                "name": name, "bytes": path.stat().st_size, "sha256": preparation.fingerprint(path),
                "rows": 4, "columns": 3, "csv_sha256": sha(buffer.getvalue().to_pybytes()),
            })
        self.catalog = self.root / "cases.json"
        self.catalog.write_text(json.dumps({
            "schema_version": 1, "repository": "phamquiluan/RCAEval", "revision": "a" * 40,
            "cases": [{"id": "case-991", "upstream_case": "answer-bearing-upstream",
                       "files": self.entries, "trace_split_after": 2}],
        }))

    def test_typed_csv_roundtrip_and_historical_hash(self):
        entry = self.entries[0]
        result = preparation.convert(self.source / entry["name"], self.root / "output.csv", entry)
        self.assertEqual(result["rows"], 4)
        self.assertEqual(result["sha256"], entry["csv_sha256"])
        entry["csv_sha256"] = "0" * 64
        with self.assertRaisesRegex(HarnessError, "historical"):
            preparation.convert(self.source / entry["name"], self.root / "changed.csv", entry)

    def test_source_shape_mismatch_rejected(self):
        entry = dict(self.entries[0], rows=5)
        with self.assertRaisesRegex(HarnessError, "shape"):
            preparation.convert(self.source / entry["name"], self.root / "output.csv", entry)

    def test_build_is_offline_verified_and_non_overwriting(self):
        output = self.root / "built"
        with patch.object(preparation, "CATALOG", self.catalog), \
             patch.object(preparation.urllib.request, "urlopen", side_effect=AssertionError("Network forbidden")):
            preparation.build("case-991", self.source, output)
            with self.assertRaises(FileExistsError):
                preparation.build("case-991", self.source, output)
        verification = json.loads((output / "runtime-verification.json").read_text())
        self.assertEqual(verification["status"], "verified")
        report = json.loads((output / "preparation.json").read_text())
        self.assertEqual(len(report["package_bytes"]), 3)
        for path in (output / "packages").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"answer-bearing-upstream", path.read_bytes())

    def test_verifier_failure_is_not_success(self):
        output = self.root / "failed-build"
        failed = subprocess.CompletedProcess(["python"], 1, "", "verifier failed")
        with patch.object(preparation, "CATALOG", self.catalog), \
             patch.object(preparation.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(HarnessError, "verifier"):
                preparation.build("case-991", self.source, output)
        self.assertTrue((output / "failure.json").exists())
        self.assertFalse((output / "preparation.json").exists())


if __name__ == "__main__":
    unittest.main()
