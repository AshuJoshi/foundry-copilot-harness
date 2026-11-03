import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import runpy

from harness_client import HarnessError
from incident_investigation import SKILL_NAME, SKILLS, validate_artifacts, validate_execution


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "case.zip"
        self.evidence = []
        with zipfile.ZipFile(self.archive, "w") as bundle:
            for index, (name, column, value) in enumerate([
                ("metrics.csv", "frontend_cpu", "0.025"),
                ("logs.csv", "container_name", "cartservice"),
                ("traces.csv", "serviceName", "frontend"),
            ], 1):
                text = io.StringIO(newline="")
                writer = csv.DictWriter(text, fieldnames=["evidence_row_id", column, "message"])
                writer.writeheader()
                writer.writerow({"evidence_row_id": "1", column: value, "message": 'a,\n"quoted" observation'})
                bundle.writestr(name, text.getvalue())
                self.evidence.append({
                    "id": f"E{index}", "file": name, "evidence_row_id": 1,
                    "observed_values": {column: value, "message": 'a,\n"quoted" observation'},
                    "observation": "Recorded data", "relevance": "A hypothesis test",
                })
        self.diagnosis = {
            "schema_version": 1, "case_id": "case-001", "status": "supported",
            "originating_service": "cartservice", "mechanism": "A tentative mechanism",
            "evidence": self.evidence,
            "alternatives": [
                {"hypothesis": "A", "evidence_ids": ["E1"], "assessment": "Not conclusive"},
                {"hypothesis": "B", "evidence_ids": ["E2"], "assessment": "Not conclusive"},
            ],
            "next_test": {"action": "Check", "would_change_diagnosis_if": "Contradiction"},
            "mitigation": {"action": "Advisory", "risks": "Risk", "verification_and_rollback": "Rollback"},
            "limitations": ["No recovery test"],
        }
        self.paths = [self.root / "diagnosis.json", self.root / "report.md"]
        self.paths[1].write_text("Observations E1 E2 E3; inferred cause; advisory action.")

    def validate(self):
        self.paths[0].write_text(json.dumps(self.diagnosis))
        return validate_artifacts(self.paths, self.archive, "case-001")

    def test_exact_cells_multiline_and_service(self):
        self.assertEqual(self.validate()["verified_evidence_records"], 3)

    def test_tampered_or_excerpt_cells_fail(self):
        for column, value in [("message", "a,"), ("invented", "value")]:
            with self.subTest(column=column):
                original = copy.deepcopy(self.diagnosis)
                self.diagnosis["evidence"][0]["observed_values"][column] = value
                with self.assertRaisesRegex(HarnessError, "cell mismatch"):
                    self.validate()
                self.diagnosis = original

    def test_invalid_source_ids_fail(self):
        for value in [True, 0, -1, 1.0, "1", 2]:
            with self.subTest(value=value):
                self.diagnosis["evidence"][0]["evidence_row_id"] = value
                with self.assertRaises(HarnessError):
                    self.validate()

    def test_duplicate_evidence_fails(self):
        self.diagnosis["evidence"].append(copy.deepcopy(self.evidence[0]))
        with self.assertRaises(HarnessError):
            self.validate()

    def test_missing_modality_fails(self):
        self.diagnosis["evidence"][2]["file"] = "logs.csv"
        self.diagnosis["evidence"][2]["evidence_row_id"] = 2
        with self.assertRaisesRegex(HarnessError, "cover"):
            self.validate()

    def test_unknown_service_and_bad_status_fail(self):
        self.diagnosis["originating_service"] = "invented"
        with self.assertRaisesRegex(HarnessError, "not present"):
            self.validate()
        self.diagnosis["status"] = "insufficient_evidence"
        with self.assertRaisesRegex(HarnessError, "null"):
            self.validate()
        self.diagnosis["originating_service"] = None
        self.assertEqual(self.validate()["diagnosis_status"], "insufficient_evidence")

    def test_invalid_alternative_reference_fails(self):
        self.diagnosis["alternatives"][0]["evidence_ids"] = ["E99"]
        with self.assertRaisesRegex(HarnessError, "missing evidence"):
            self.validate()

    def test_boolean_schema_version_fails(self):
        self.diagnosis["schema_version"] = True
        with self.assertRaisesRegex(HarnessError, "schema"):
            self.validate()

    def test_report_missing_evidence_reference_fails(self):
        self.paths[1].write_text("E1 E2 E30")
        with self.assertRaisesRegex(HarnessError, "reference"):
            self.validate()

    def test_execution_proof_not_prose(self):
        expected = {"case_id": "case-001", "archive_sha256": "abc", "files": []}
        response = {"output": [
            {"type": "function_call", "call_id": "s", "name": "skill",
             "arguments": json.dumps({"skill": SKILL_NAME})},
            {"type": "function_call_output", "call_id": "s", "status": "completed",
             "output": "Skill loaded successfully"},
            {"type": "shell_call", "call_id": "v",
             "action": {"commands": ["python3 /skill/verify_inputs.py"]}},
            {"type": "shell_call_output", "call_id": "v",
             "output": [{"outcome": {"exit_code": 0},
                         "stdout": json.dumps(dict(expected, status="verified")) + "\n<shellId: 1>"}]},
        ]}
        for name in sorted(SKILLS - {SKILL_NAME}):
            response["output"].extend([
                {"type": "function_call", "call_id": name, "name": "skill",
                 "arguments": json.dumps({"skill": name})},
                {"type": "function_call_output", "call_id": name, "status": "completed",
                 "output": "Skill loaded successfully"},
            ])
        validate_execution(response, expected)
        response["output"][2]["action"]["commands"][0] += " || echo passed"
        with self.assertRaises(HarnessError):
            validate_execution(response, expected)
        response["output"].append({"type": "mcp_call"})
        with self.assertRaisesRegex(HarnessError, "external"):
            validate_execution(response, expected)

    def test_malformed_shapes_fail_explicitly(self):
        self.diagnosis["status"] = []
        with self.assertRaises(HarnessError):
            self.validate()
        self.diagnosis["status"] = "supported"
        self.diagnosis["evidence"][0]["file"] = []
        with self.assertRaises(HarnessError):
            self.validate()

    def test_runtime_verifier_checks_all_data_skills_and_hashes(self):
        verify = runpy.run_path(
            str(Path("skills/incident-investigation-telemetry/verify_inputs.py"))
        )["verify"]
        main = self.root / SKILL_NAME
        main.mkdir()
        entries = []
        for name, folder, row_id in [
            ("metrics.csv", main, 1), ("logs.csv", main, 1),
            ("traces-001.csv", self.root / "incident-trace-data-part-one", 1),
            ("traces-002.csv", self.root / "incident-trace-data-part-two", 2),
        ]:
            folder.mkdir(exist_ok=True)
            data = f"evidence_row_id,value\n{row_id},observed\n".encode()
            (folder / name).write_bytes(data)
            item = {"name": name, "sha256": hashlib.sha256(data).hexdigest(),
                    "rows": 1, "columns": ["evidence_row_id", "value"], "first_row_id": row_id}
            if folder != main:
                item["source_skill"] = folder.name
            entries.append(item)
        manifest = {"case_id": "case-001", "files": entries}
        (main / "input-manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(verify(main), dict(manifest, status="verified"))
        (main / "logs.csv").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            verify(main)


if __name__ == "__main__":
    unittest.main()
