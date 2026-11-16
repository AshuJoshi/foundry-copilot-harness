import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from harness_client import HarnessError
from software_repair import validate_artifacts, validate_execution

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "repair_verifier", ROOT / "skills/software-repair/verify_inputs.py",
)
assert spec is not None and spec.loader is not None
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        (self.repository / "src").mkdir(parents=True)
        (self.repository / "src/example.py").write_text("value = 1\n")
        self.task = {"case_id": "repair-001", "base_commit": "a" * 40}
        (self.root / "task.json").write_text(json.dumps(self.task))
        self.manifest = dict(self.task, files=[
            {"name": p.relative_to(self.root).as_posix(), "bytes": len(p.read_bytes()),
             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in [self.root / "task.json", self.repository / "src/example.py"]
        ])
        (self.root / "input-manifest.json").write_text(json.dumps(self.manifest))
        self.patch = (
            "diff --git a/src/example.py b/src/example.py\n"
            "--- a/src/example.py\n+++ b/src/example.py\n"
            "@@ -1 +1 @@\n-value = 1\n+value = 2\n"
        )
        self.repair = {
            "schema_version": 1, **self.task, "status": "candidate",
            "summary": "Correct the value.", "changed_files": ["src/example.py"],
            "tests": [], "limitations": ["No benchmark evaluation yet."],
        }
        self.paths = [self.root / name for name in ["repair.json", "report.md"]]

    def validate(self):
        self.paths[0].write_text(json.dumps(dict(self.repair, patch=self.patch)))
        self.paths[1].write_text("Candidate patch; official tests not run.")
        return validate_artifacts(self.paths, self.task, self.repository)

    def test_valid_candidate(self):
        self.assertEqual(self.validate()["status"], "passed")
        self.assertEqual((self.repository / "src/example.py").read_text(), "value = 1\n")

    def test_identity_mismatch(self):
        self.repair["base_commit"] = "b" * 40
        with self.assertRaisesRegex(HarnessError, "identity"):
            self.validate()

    def test_non_source_patch(self):
        self.patch = self.patch.replace("src/example.py", "../example.py")
        with self.assertRaises(HarnessError):
            self.validate()

    def test_declared_files_mismatch(self):
        self.repair["changed_files"] = []
        with self.assertRaisesRegex(HarnessError, "changed_files"):
            self.validate()

    def test_patch_context_mismatch(self):
        self.patch = self.patch.replace("-value = 1", "-value = 5")
        with self.assertRaisesRegex(HarnessError, "does not apply"):
            self.validate()

    def test_file_modes_rejected(self):
        self.patch = self.patch.replace("--- a/", "old mode 100644\nnew mode 100755\n--- a/")
        with self.assertRaisesRegex(HarnessError, "file operation"):
            self.validate()

    def test_blocked_requires_empty_patch(self):
        self.repair.update(status="blocked", changed_files=[])
        with self.assertRaises(HarnessError):
            self.validate()
        self.patch = ""
        self.assertEqual(self.validate()["status"], "blocked")

    def test_bool_exit_code_rejected(self):
        self.repair["tests"] = [{"command": "test", "exit_code": True, "observation": "ok"}]
        with self.assertRaisesRegex(HarnessError, "exit_code"):
            self.validate()

    def test_runtime_verification(self):
        result = verifier.verify(self.root)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["files_count"], 2)
        (self.repository / "src/example.py").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            verifier.verify(self.root)

    def test_extra_repository_file_rejected(self):
        (self.repository / "src/extra.py").write_text("extra\n")
        with self.assertRaisesRegex(ValueError, "member set"):
            verifier.verify(self.root)

    def test_companion_skill_verification(self):
        main = self.root / "software-repair"
        data = self.root / "software-repair-repository-data"
        main.mkdir()
        (data / "repository/doc").mkdir(parents=True)
        (data / "repository/doc/example.rst").write_text("Documentation\n")
        (main / "repository/src").mkdir(parents=True)
        (main / "repository/src/example.py").write_text("value = 1\n")
        (main / "task.json").write_text(json.dumps(self.task))
        entries = []
        for source, file in [(main, main / "task.json"),
                             (main, main / "repository/src/example.py"),
                             (data, data / "repository/doc/example.rst")]:
            entries.append({
                "source_skill": source.name, "name": file.relative_to(source).as_posix(),
                "bytes": file.stat().st_size,
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
            })
        (main / "input-manifest.json").write_text(json.dumps(dict(self.task, files=entries)))
        self.assertEqual(verifier.verify(main)["files_count"], 3)
        (data / "repository/doc/example.rst").write_text("Changed\n")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            verifier.verify(main)

    def test_unknown_source_skill_rejected(self):
        self.manifest["files"][0]["source_skill"] = "other-skill"
        (self.root / "input-manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "Unknown source"):
            verifier.verify(self.root)

    def test_unsafe_manifest_path_rejected(self):
        self.manifest["files"][0]["name"] = "../task.json"
        (self.root / "input-manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            verifier.verify(self.root)

    def test_correlated_execution(self):
        expected = verifier.verify(self.root)
        response = {"output": [
            {"type": "function_call", "name": "skill", "call_id": "skill-1",
             "arguments": '{"skill":"software-repair"}'},
            {"type": "function_call_output", "call_id": "skill-1", "status": "completed",
             "output": "loaded successfully"},
            {"type": "function_call", "name": "skill", "call_id": "skill-2",
             "arguments": '{"skill":"software-repair-repository-data"}'},
            {"type": "function_call_output", "call_id": "skill-2", "status": "completed",
             "output": "loaded successfully"},
            {"type": "shell_call", "call_id": "shell-1",
             "action": {"commands": ["python3 /skills/software-repair/verify_inputs.py"]}},
            {"type": "shell_call_output", "call_id": "shell-1",
             "output": [{"outcome": {"exit_code": 0}, "stdout": json.dumps(expected)}]},
        ]}
        validate_execution(response, expected)
        response["output"][1]["call_id"] = "unrelated"
        with self.assertRaisesRegex(HarnessError, "Skill loads"):
            validate_execution(response, expected)

    def test_execution_rejects_mcp(self):
        with self.assertRaises(HarnessError):
            validate_execution({"output": [{"type": "mcp_call"}]}, {})


if __name__ == "__main__":
    unittest.main()
