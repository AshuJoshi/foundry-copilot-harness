import csv
import ast
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest

import model_comparison_eval as grading


class JsonGradingTests(unittest.TestCase):
    def test_scoring_and_sandbox_logic_match_original_implementation(self):
        tree = ast.parse(Path(grading.__file__).read_text())
        nodes = [
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
            or isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "WORKER" for target in node.targets)
        ]
        digest = hashlib.sha256("\n".join(ast.dump(n, include_attributes=False) for n in nodes).encode()).hexdigest()
        self.assertEqual(digest, "6f3edef1ba539d2e4a7baf4ff99f3d964e26504d72d1400302e1aeda7caef83b")

    def test_exact_computation_requires_values_types_and_output_shape(self):
        good = {"prime_count": 78498, "product": 3842999123, "modular_power": 51477}
        self.assertTrue(grading.grade_json("compute", json.dumps(good))["correct"])
        good["prime_count"] = "78498"
        self.assertFalse(grading.grade_json("compute", json.dumps(good))["correct"])
        self.assertFalse(grading.grade_json("compute", "The answer is 78498")["contract_ok"])

    def test_constraints_verify_optimal_assignment_not_integer_presence(self):
        good = {"assignment": {"A": "Y", "B": "Z", "C": "X", "D": "W"}, "total_cost": 14}
        self.assertTrue(grading.grade_json("constraints", json.dumps(good))["correct"])
        good["assignment"]["B"] = "X"
        self.assertFalse(grading.grade_json("constraints", json.dumps(good))["correct"])

    def test_csv_reference_preserves_duplicates_and_exclusions(self):
        expected = grading.transform_reference()
        self.assertEqual(expected["valid_rows"], 6)
        self.assertEqual(len(expected["excluded_rows"]), 6)
        serial = json.loads(json.dumps(expected, default=str))
        self.assertTrue(grading.grade_json("transform", json.dumps(serial))["correct"])
        serial["categories"]["A"]["rows"] -= 1
        self.assertFalse(grading.grade_json("transform", json.dumps(serial))["correct"])

    def test_evidence_checks_causal_qualification_and_actual_cells(self):
        with (grading.FIXTURES / "evidence.csv").open(newline="") as stream:
            refs = [{"row_id": r["row_id"], "quantity": r["quantity"], "unit_price": r["unit_price"]}
                    for r in csv.DictReader(stream)]
        good = {"net_values": {"P1": "900.00", "P2": "780"},
                "verdicts": {"A": "contradicted", "B": "contradicted", "C": "not_established",
                             "D": "not_established", "E": "supported"},
                "evidence": list(reversed(refs)), "limitations": ["No costs or credit reasons."]}
        self.assertTrue(grading.grade_json("evidence", json.dumps(good))["correct"])
        good["verdicts"]["C"] = "supported"
        self.assertFalse(grading.grade_json("evidence", json.dumps(good))["correct"])
        good["evidence"][0]["quantity"] = "fake"
        self.assertFalse(grading.grade_json("evidence", json.dumps(good))["checks"]["exact_cells"])

    def test_reference_code_cases_cover_tuples_invalid_inputs_and_signed_dedup(self):
        self.assertEqual(grading.interval_reference(((3, 4), (1, 2))), [[1, 2], [3, 4]])
        self.assertEqual(grading.interval_reference([[1, 3], [3, 7]]), [[1, 7]])
        with self.assertRaises(ValueError):
            grading.interval_reference([[True, 2]])
        one = {"id": "a", "account": "X", "amount_cents": -10}
        self.assertEqual(grading.reconcile_reference([one, dict(one)]), {"X": -10})
        with self.assertRaises(ValueError):
            grading.reconcile_reference([one, dict(one, amount_cents=5)])
        self.assertTrue(any("$tuple" in json.dumps(v) for v in grading.code_tests("debug")[1]))


@unittest.skipUnless(os.environ.get("MODEL_STUDY_TEST_IMAGE"), "Set pinned grading image for actual Docker controls.")
class DockerGradingTests(unittest.TestCase):
    def test_correct_controls_pass_and_actual_buggy_module_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for case, reference, name in [
                ("implement", grading.interval_reference, "normalize_intervals"),
                ("debug", grading.reconcile_reference, "reconcile"),
            ]:
                path = root / (case + ".py")
                path.write_text(inspect.getsource(reference).replace(reference.__name__, name))
                score = grading.grade_code(case, path, os.environ["MODEL_STUDY_TEST_IMAGE"], root / case)
                self.assertTrue(score["correct"], (case, score, (root / case / "container.log").read_text()))
            bad = grading.grade_code("debug", grading.FIXTURES / "debugging_starter.py",
                                     os.environ["MODEL_STUDY_TEST_IMAGE"], root / "bad")
            self.assertFalse(bad["correct"])
            self.assertLess(bad["checks_passed"], bad["checks_total"])
