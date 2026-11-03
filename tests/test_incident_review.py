import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from harness_client import HarnessError
from incident_review import (
    freeze, pointer, validate_critique, validate_diagnosis,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "agents/incident-review/faulty-diagnosis.json"


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = json.loads(FIXTURE.read_text())
        self.diagnosis = copy.deepcopy(self.fixture["diagnosis"])
        self.archive = self.root / "original-input.zip"
        with zipfile.ZipFile(self.archive, "x") as bundle:
            for evidence in self.diagnosis["evidence"]:
                values = {"evidence_row_id": str(evidence["evidence_row_id"]),
                          **evidence["observed_values"]}
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, fieldnames=list(values), lineterminator="\n")
                writer.writeheader()
                writer.writerow(values)
                bundle.writestr(evidence["file"], stream.getvalue())
        self.manifest = self.root / "input-manifest.json"
        self.manifest.write_text(json.dumps({"case_id": "case-001", "files": []}))
        self.peer = self.root / "diagnosis.json"
        self.peer.write_text(json.dumps(self.diagnosis))
        self.independent = self.root / "independent.json"
        own = copy.deepcopy(self.diagnosis)
        own.update(originating_service="cartservice",
                   mechanism="Fixture independent hypothesis; causality not established.")
        self.independent.write_text(json.dumps(own))
        self.binding = self.root / "binding.json"
        freeze(self.independent, self.peer, self.manifest, self.archive, self.binding)
        bound = json.loads(self.binding.read_text())
        self.critique = {
            "schema_version": 1, "case_id": "case-001",
            "independent_sha256": bound["independent_sha256"],
            "diagnosis_sha256": bound["diagnosis_sha256"],
            "verdict": "revise",
            "evidence": [copy.deepcopy(self.diagnosis["evidence"][0])],
            "findings": [{
                "id": "F1", "category": "overstated", "claim_pointer": "/mechanism",
                "claim_text": "conclusively the original source",
                "assessment": "A client span alone does not establish input provenance.",
                "evidence_ids": ["E1"],
            }],
            "accepted_claims": [],
            "next_test": {"action": "Inspect input provenance",
                          "would_change_diagnosis_if": "Upstream evidence contradicts attribution"},
            "limitations": ["Semantic causality requires review."],
        }
        self.path = self.root / "critique.json"

    def validate(self):
        self.path.write_text(json.dumps(self.critique))
        return validate_critique(self.path, self.binding, self.independent, self.peer,
                                 self.manifest, self.archive)

    def test_mechanical_checks_do_not_claim_causal_correctness(self):
        result = self.validate()
        self.assertEqual(result["status"], "mechanically_valid")
        self.assertEqual(result["semantic_correctness"], "manual_review_required")
        self.assertIn("not_proven", result["independence"])

    def test_flawed_diagnosis_can_have_exact_citations(self):
        result = validate_diagnosis(self.diagnosis, self.archive, "case-001")
        self.assertEqual(result["status"], "passed")
        self.assertTrue(self.fixture["evaluator_only"])
        self.assertEqual(len(self.fixture["expected_objections"]), 3)
        # Schema/evidence checks deliberately do not certify the flawed conclusions.
        self.assertEqual(result["originating_service"], "frontend")

    def test_artifact_mutation_after_freeze_rejected(self):
        for path in [self.independent, self.peer, self.manifest, self.archive]:
            original = path.read_bytes()
            with self.subTest(path=path.name):
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(HarnessError, "hash mismatch"):
                    self.validate()
                path.write_bytes(original)

    def test_freeze_refuses_overwrite(self):
        with self.assertRaises(FileExistsError):
            freeze(self.independent, self.peer, self.manifest, self.archive, self.binding)

    def test_case_mismatch_rejected(self):
        self.critique["case_id"] = "case-002"
        with self.assertRaisesRegex(HarnessError, "identity"):
            self.validate()

    def test_exact_cell_mismatch_rejected(self):
        self.critique["evidence"][0]["observed_values"]["statusCode"] = "500"
        with self.assertRaisesRegex(HarnessError, "cell mismatch"):
            self.validate()

    def test_unresolvable_pointer_and_wrong_quote_rejected(self):
        self.critique["findings"][0]["claim_pointer"] = "/missing"
        with self.assertRaisesRegex(HarnessError, "resolve"):
            self.validate()
        self.critique["findings"][0]["claim_pointer"] = "/mechanism"
        self.critique["findings"][0]["claim_text"] = "Not in the actual diagnosis"
        with self.assertRaisesRegex(HarnessError, "Quoted claim"):
            self.validate()

    def test_unknown_evidence_duplicate_ids_and_uncited_records_rejected(self):
        self.critique["findings"][0]["evidence_ids"] = ["unknown"]
        with self.assertRaisesRegex(HarnessError, "references"):
            self.validate()
        self.critique["findings"][0]["evidence_ids"] = ["E1"]
        self.critique["findings"].append(copy.deepcopy(self.critique["findings"][0]))
        with self.assertRaisesRegex(HarnessError, "finding ID"):
            self.validate()
        self.critique["findings"].pop()
        self.critique["evidence"].append(copy.deepcopy(self.diagnosis["evidence"][1]))
        with self.assertRaisesRegex(HarnessError, "Unused"):
            self.validate()

    def test_honest_agreement_allowed_but_not_contradictory_verdict(self):
        self.critique["verdict"] = "agree"
        with self.assertRaisesRegex(HarnessError, "Agreement"):
            self.validate()
        self.critique["findings"] = []
        self.critique["accepted_claims"] = [{
            "claim_pointer": "/evidence/0/observation",
            "claim_text": self.diagnosis["evidence"][0]["observation"],
            "assessment": "The stated nonzero client span is observed.",
            "evidence_ids": ["E1"],
        }]
        self.assertEqual(self.validate()["status"], "mechanically_valid")
        self.critique["verdict"] = "revise"
        with self.assertRaisesRegex(HarnessError, "concrete finding"):
            self.validate()

    def test_bool_record_id_and_unexpected_fields_rejected(self):
        self.critique["evidence"][0]["evidence_row_id"] = True
        with self.assertRaisesRegex(HarnessError, "type mismatch"):
            self.validate()
        self.critique["evidence"][0]["evidence_row_id"] = 83590
        self.critique["expected_objections"] = ["leaked rubric"]
        with self.assertRaisesRegex(HarnessError, "Unexpected"):
            self.validate()

    def test_fixture_expected_claim_pointers_resolve(self):
        for expected in self.fixture["expected_objections"]:
            self.assertIsInstance(pointer(self.diagnosis, expected["claim_pointer"]), str)
        self.assertEqual(pointer({"a/b": {"~key": ["value"]}}, "/a~1b/~0key/0"), "value")
        for path in ["/a~2b", "/a~1b/~0key/-1", "/a~1b/~0key/00"]:
            with self.assertRaises(HarnessError):
                pointer({"a/b": {"~key": ["value"]}}, path)

    def test_manifests_share_exact_data_references_and_no_a2a(self):
        agents = []
        for name in ["investigator", "challenger"]:
            manifest = json.loads((ROOT / f"agents/incident-review/{name}.azure.yaml").read_text())
            agent = manifest["services"][f"incident-review-{name}"]
            agents.append(agent)
            self.assertEqual(agent["kind"], "prompt")
            self.assertEqual(agent["harness"], {"type": "microsoft_copilot_preview"})
            self.assertEqual(agent["tools"], [])
            self.assertNotIn("protocols", agent)
            self.assertTrue(agent["name"].startswith("__"))
        self.assertEqual(agents[0]["skills"], agents[1]["skills"])
        self.assertEqual(agents[0]["model"], agents[1]["model"])


if __name__ == "__main__":
    unittest.main()
