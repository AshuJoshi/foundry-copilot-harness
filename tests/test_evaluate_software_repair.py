import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness_client import HarnessError
from scripts.evaluate_software_repair import check_report, evaluate, candidate_patch
from scripts.prepare_software_repair import digest


CASE = {"instance_id": "task-1", "FAIL_TO_PASS": '["new"]', "PASS_TO_PASS": '["old"]'}


def report(resolved=True):
    return {"task-1": {
        "patch_successfully_applied": True, "resolved": resolved,
        "tests_status": {
            "FAIL_TO_PASS": {"success": ["new"] if resolved else [],
                             "failure": [] if resolved else ["new"]},
            "PASS_TO_PASS": {"success": ["old"], "failure": []},
        },
    }}


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_controls_have_distinct_expectations(self):
        self.assertFalse(check_report(report(False), CASE, "baseline")["resolved"])
        self.assertTrue(check_report(report(), CASE, "reference")["resolved"])
        self.assertFalse(check_report(report(False), CASE, "candidate")["resolved"])
        with self.assertRaisesRegex(HarnessError, "Negative"):
            check_report(report(), CASE, "baseline")
        with self.assertRaisesRegex(HarnessError, "Reference"):
            check_report(report(False), CASE, "reference")

    def test_missing_extra_duplicate_and_overlapping_tests_rejected(self):
        for success, failure in [([], []), (["new", "extra"], []),
                                 (["new", "new"], []), (["new"], ["new"])]:
            value = report()
            value["task-1"]["tests_status"]["FAIL_TO_PASS"] = {"success": success, "failure": failure}
            with self.assertRaisesRegex(HarnessError, "accounting"):
                check_report(value, CASE, "candidate")

    def test_resolution_and_patch_failure_rejected(self):
        value = report()
        value["task-1"]["resolved"] = False
        with self.assertRaisesRegex(HarnessError, "resolution"):
            check_report(value, CASE, "candidate")
        value["task-1"]["patch_successfully_applied"] = False
        with self.assertRaisesRegex(HarnessError, "apply"):
            check_report(value, CASE, "candidate")

    def test_candidate_fingerprint_checked_before_copying_or_execution(self):
        candidate = self.root / "repair.json"
        candidate.write_text('{"patch":"untrusted"}')
        with self.assertRaisesRegex(HarnessError, "fingerprint"):
            candidate_patch(self.root, candidate, "0" * 64, self.root / "repo", {})
        self.assertFalse((self.root / "repo").exists())

    def test_evaluator_failure_preserved_not_unresolved(self):
        config = {"case_sha256": "case", "evaluator_version": "4.1.0", "evaluator_image": "digest"}
        output = self.root / "failure"
        with patch("scripts.evaluate_software_repair.settings", return_value=config), \
             patch("scripts.evaluate_software_repair.verify_prepared", return_value=(dict(CASE, patch="fix"), {})), \
             patch("scripts.evaluate_software_repair.run_official", side_effect=HarnessError("daemon unavailable")) as run:
            with self.assertRaisesRegex(HarnessError, "daemon"):
                evaluate(self.root, output, "reference", "test-reference")
            self.assertEqual(run.call_count, 1)
            with self.assertRaises(FileExistsError):
                evaluate(self.root, output, "reference", "test-reference")
        self.assertTrue((output / "failure.json").exists())
        self.assertFalse((output / "summary.json").exists())

    def test_summary_is_derived_from_actual_official_report(self):
        config = {"case_sha256": "case", "evaluator_version": "4.1.0", "evaluator_image": "digest"}
        output = self.root / "success"
        with patch("scripts.evaluate_software_repair.settings", return_value=config), \
             patch("scripts.evaluate_software_repair.verify_prepared", return_value=(dict(CASE, patch="fix"), {})), \
             patch("scripts.evaluate_software_repair.run_official", return_value=report()):
            evaluate(self.root, output, "reference", "test-reference")
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["tests"]["FAIL_TO_PASS"]["passed"], 1)
        self.assertEqual(summary["patch_sha256"], digest(b"fix"))
        self.assertTrue(summary["resolved"])

    def test_candidate_applies_outside_parent_checkout(self):
        prepared = self.root / "prepared"
        for skill in ["software-repair", "software-repair-repository-data"]:
            (prepared / "packages" / skill / "repository").mkdir(parents=True)
        source = prepared / "packages/software-repair/repository/src"
        source.mkdir()
        (source / "example.py").write_text("value = 1\n")
        task = {"case_id": "repair-001", "base_commit": "a" * 40}
        patch_text = ("diff --git a/src/example.py b/src/example.py\n"
                      "--- a/src/example.py\n+++ b/src/example.py\n"
                      "@@ -1 +1 @@\n-value = 1\n+value = 2\n")
        repair = dict(task, schema_version=1, status="candidate", summary="Change value",
                      changed_files=["src/example.py"], patch=patch_text, tests=[], limitations=[])
        candidate = self.root / "repair.json"
        candidate.write_text(json.dumps(repair))
        candidate.with_name("report.md").write_text("Fixture candidate")
        config = {"case_sha256": "case", "evaluator_version": "4.1.0", "evaluator_image": "digest"}
        output = self.root / "candidate-evaluation"
        with patch("scripts.evaluate_software_repair.settings", return_value=config), \
             patch("scripts.evaluate_software_repair.verify_prepared", return_value=(CASE, task)), \
             patch("scripts.evaluate_software_repair.run_official", return_value=report()) as official:
            evaluate(prepared, output, "candidate", "new-run", candidate,
                     digest(candidate.read_bytes()))
        self.assertEqual(official.call_args.args[1], patch_text)
        self.assertFalse((output / "candidate-base").exists())
        self.assertTrue(json.loads((output / "summary.json").read_text())["resolved"])


if __name__ == "__main__":
    unittest.main()
