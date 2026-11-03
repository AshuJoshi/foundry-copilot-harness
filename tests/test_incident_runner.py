import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness_client import HarnessError, ResponseError
from retail_support import RecordingClient
from scripts import configure_incident, run_incident
from scripts.prepare_incident import SKILLS

ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"


class IncidentRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = RecordingClient(ENDPOINT, "subscription", "test-agent", agent_version="1")
        self.output = self.root / "run"

    def test_configuration_is_local_and_requires_complete_immutable_versions(self):
        versions = {skill: "4" for skill in SKILLS}
        with patch.object(self.client, "_request") as network:
            path = configure_incident.configure(
                ENDPOINT, "sub", "model", "test-agent", versions, self.root / "deploy")
            network.assert_not_called()
        value = json.loads(path.read_text())["services"]["incident-investigation-agent"]
        self.assertEqual(value["harness"]["type"], "microsoft_copilot_preview")
        self.assertEqual({s["name"]: s["version"] for s in value["skills"]}, versions)
        with self.assertRaises(FileExistsError):
            configure_incident.configure(ENDPOINT, "sub", "model", "test-agent", versions, self.root / "deploy")
        with self.assertRaisesRegex(HarnessError, "three"):
            configure_incident.configure(ENDPOINT, "sub", "model", "test-agent", {}, self.root / "bad")

    def test_preflight_checks_published_inputs_versions_and_both_harness_families(self):
        versions = {skill: "4" for skill in SKILLS}
        path = configure_incident.configure(ENDPOINT, "sub", "model", "test-agent",
                                            versions, self.root / "deploy")
        publication = self.root / "publication.json"
        publication.write_text(json.dumps({
            "status": "verified", "scenario": "incident", "project_endpoint": ENDPOINT,
            "package_sha256": {"file": "hash"}, "input_verification": {"case_id": "case-001"},
            "versions": versions}))
        value = json.loads(path.read_text())
        definition = {key: item for key, item in value["services"]["incident-investigation-agent"].items()
                      if key in {"kind", "model", "harness", "instructions", "skills", "tools"}}
        for family in ["microsoft_copilot_preview", "github_copilot_preview"]:
            definition["harness"] = {"type": family}
            value["services"]["incident-investigation-agent"]["harness"] = {"type": family}
            path.write_text(json.dumps(value))
            record = {"version": "1", "status": "active", "definition": definition}
            with patch.object(run_incident, "verify_prepared", return_value={"case_id": "case-001"}), \
                 patch.object(run_incident, "package_hashes", return_value={"file": "hash"}), \
                 patch.object(self.client, "get_agent", return_value={"versions": {"latest": record}}):
                self.assertEqual(run_incident.preflight(
                    self.client, "incident", self.root, publication, path)[1], record)
                record["definition"] = dict(definition, model="wrong")
                with self.assertRaisesRegex(HarnessError, "differs"):
                    run_incident.preflight(self.client, "incident", self.root, publication, path)

    def test_timeout_records_context_without_git_or_resubmission(self):
        with patch.object(self.client, "create_conversation", return_value="conv-1"), \
             patch.object(self.client, "invoke", side_effect=HarnessError("timeout")) as invoke, \
             patch.object(run_incident.subprocess, "check_output") as git:
            with self.assertRaisesRegex(HarnessError, "timeout"):
                run_incident.invoke_once(self.client, "prompt", self.output, {}, {},
                                         ["scripts/run_incident.py"])
            git.assert_not_called()
        invoke.assert_called_once()
        self.assertTrue((self.output / "conversation.json").exists())
        conditions = json.loads((self.output / "conditions.json").read_text())
        self.assertIsNone(conditions["source_commit"])
        with self.assertRaises(FileExistsError):
            run_incident.invoke_once(self.client, "prompt", self.output, {}, {}, [])

    def test_failed_response_is_preserved(self):
        failed = {"id": "resp-1", "status": "failed", "output": []}
        with patch.object(self.client, "create_conversation", return_value="conv-1"), \
             patch.object(self.client, "invoke", side_effect=ResponseError("failed", failed)):
            with self.assertRaises(ResponseError):
                run_incident.invoke_once(self.client, "prompt", self.output, {}, {}, [])
        self.assertEqual(json.loads((self.output / "response.json").read_text()), failed)

    def test_existing_run_stops_before_preflight(self):
        self.output.mkdir()
        with patch.object(run_incident, "preflight") as preflight:
            with self.assertRaisesRegex(HarnessError, "exists"):
                run_incident.run(self.client, self.root, self.root, self.root, self.output)
            preflight.assert_not_called()

    def test_missing_upload_stops_without_success_record(self):
        def invoke(*_):
            self.output.mkdir()
            return {"status": "completed"}
        with patch.object(run_incident, "preflight", return_value=({"case_id": "case-001"}, {})), \
             patch.object(run_incident, "invoke_once", side_effect=invoke) as call, \
             patch.object(run_incident, "validate_execution"), \
             patch.object(self.client, "download_outputs", side_effect=HarnessError("upload failed")):
            with self.assertRaisesRegex(HarnessError, "upload"):
                run_incident.run(self.client, self.root, self.root, self.root, self.output)
        call.assert_called_once()
        self.assertTrue((self.output / "failure.json").exists())
        self.assertFalse((self.output / "result.json").exists())

    def test_prompt_does_not_embed_withheld_answer(self):
        text = run_incident.prompt("case-001")
        for value in ["cartservice", "re3ob", "inject_time"]:
            self.assertNotIn(value, text)

    def test_audit_rechecks_exact_response_files_without_invocation(self):
        verification = {"case_id": "case-001"}
        text = run_incident.prompt("case-001")
        self.output.mkdir()
        (self.output / "downloads").mkdir()
        for name in ["diagnosis.json", "report.md"]:
            (self.output / "downloads" / name).write_text(name)
        agent = {"type": "agent_reference", "name": "test-agent", "version": "1"}
        response = {"id": "resp", "status": "completed", "model": "model", "output": [],
                    "agent_reference": agent, "conversation": {"id": "conv"}}
        (self.output / "conditions.json").write_text(json.dumps({
            "source_sha256": {}, "input_verification": verification,
            "prompt_sha256": run_incident.digest(text.encode())}))
        (self.output / "request.json").write_text(json.dumps({
            "input": text, "agent_reference": agent, "conversation": "conv"}))
        (self.output / "response.json").write_text(json.dumps(response))
        target = self.root / "audit"

        def download(value, directory):
            directory.mkdir()
            paths = []
            for name in ["diagnosis.json", "report.md"]:
                path = directory / name
                path.write_text(name)
                paths.append(path)
            return paths

        with patch.object(run_incident, "preflight", return_value=(
            verification, {"definition": {"model": "model"}})), \
             patch.object(self.client, "get_response", return_value=response), \
             patch.object(self.client, "download_outputs", side_effect=download), \
             patch.object(self.client, "invoke") as invoke, \
             patch.object(self.client, "create_conversation") as create, \
             patch.object(run_incident, "validate_execution"), \
             patch.object(run_incident, "validate_artifacts", return_value={"status": "passed"}):
            result = run_incident.audit(self.client, self.root, self.root, self.root, self.output, target)
            self.assertEqual(result["paid_invocations"], 0)
            invoke.assert_not_called()
            create.assert_not_called()
            response["model"] = "different"
            with self.assertRaisesRegex(HarnessError, "identity"):
                run_incident.audit(self.client, self.root, self.root, self.root,
                                   self.output, self.root / "wrong-model")

    def test_cli_blocks_paid_run_but_allows_read_only_audit(self):
        import sys
        flags = [
            "run_incident", "--project-endpoint", ENDPOINT, "--subscription", "sub",
            "--agent-name", "test-agent", "--agent-version", "1",
            "--prepared-dir", str(self.root), "--publication", str(self.root),
            "--manifest", str(self.root), "--output-dir", str(self.output)]
        with patch.object(sys, "argv", flags), patch.object(run_incident, "run") as run:
            self.assertEqual(run_incident.main(), 1)
            run.assert_not_called()
        with patch.object(sys, "argv", flags + ["--audit-run-dir", str(self.root)]), \
             patch.object(run_incident, "audit", return_value={"paid_invocations": 0}) as auditor, \
             patch.object(run_incident, "run") as run:
            self.assertEqual(run_incident.main(), 0)
            auditor.assert_called_once()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
