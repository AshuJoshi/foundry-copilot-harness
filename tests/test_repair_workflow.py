from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from harness_client import HarnessError
from retail_support import RecordingClient
from scripts import configure_repair, publish_incident_skills, publish_repair_skills, run_software_repair
from scripts.prepare_software_repair import ROOT, SKILLS, digest

ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"


class RepairWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prepared = self.root / "prepared"
        for skill in SKILLS:
            (self.prepared / "packages" / skill / "repository").mkdir(parents=True)
        source = self.prepared / "packages/software-repair/repository/src"
        source.mkdir()
        (source / "example.py").write_text("value = 1\n")
        self.task = {"case_id": "repair-001", "base_commit": "a" * 40}
        (self.prepared / "packages/software-repair/task.json").write_text(json.dumps(self.task))
        self.verified = dict(self.task, status="verified")
        self.versions = {skill: "4" for skill in SKILLS}
        self.manifest = configure_repair.configure(
            ENDPOINT, "sub", "model", "repair-test", self.versions, self.root / "deployment")
        self.publication = self.root / "publication.json"
        self.publication.write_text(json.dumps({
            "status": "verified", "scenario": "repair", "project_endpoint": ENDPOINT,
            "input_verification": self.verified, "versions": self.versions,
            "package_sha256": publish_incident_skills.package_hashes(self.prepared / "packages")}))
        service = json.loads(self.manifest.read_text())["services"]["software-repair-agent"]
        self.definition = {k: service[k] for k in ["kind", "model", "harness", "instructions", "skills", "tools"]}
        self.agent = {"version": "1", "status": "active", "definition": self.definition}
        self.client = RecordingClient(ENDPOINT, "sub", "repair-test", agent_version="1",
                                      token_provider=lambda: "fake")
        self.reference = {"type": "agent_reference", "name": "repair-test", "version": "1"}
        self.response = {"id": "resp", "status": "completed", "model": "model",
                         "agent_reference": self.reference, "conversation": {"id": "conv"},
                         "output": [{"type": "message", "role": "assistant",
                                     "content": [{"type": "output_text", "text": "Candidate delivered."}]}]}
        self.repair = {
            **self.task, "schema_version": 1, "status": "candidate", "summary": "Correct value.",
            "changed_files": ["src/example.py"], "tests": [], "limitations": ["Official tests not run."],
            "patch": "diff --git a/src/example.py b/src/example.py\n"
                     "--- a/src/example.py\n+++ b/src/example.py\n"
                     "@@ -1 +1 @@\n-value = 1\n+value = 2\n",
        }

    def download(self, response, folder):
        folder.mkdir(parents=True)
        (folder / "repair.json").write_text(json.dumps(self.repair))
        (folder / "report.md").write_text("Candidate only; no official benchmark claim.")
        return [folder / "repair.json", folder / "report.md"]

    def test_configuration_rejects_missing_versions_and_overwrite_without_azure(self):
        self.assertEqual(self.definition["harness"]["type"], "microsoft_copilot_preview")
        self.assertEqual({s["name"]: s["version"] for s in self.definition["skills"]}, self.versions)
        with patch.object(RecordingClient, "_request") as network:
            with self.assertRaises(FileExistsError):
                configure_repair.configure(ENDPOINT, "sub", "model", "repair-test",
                                           self.versions, self.root / "deployment")
            with self.assertRaisesRegex(HarnessError, "two"):
                configure_repair.configure(ENDPOINT, "sub", "model", "repair-test",
                                           {}, self.root / "invalid")
            network.assert_not_called()

    def test_configuration_cli_explains_local_output_and_frozen_settings(self):
        output = self.root / "configuration-cli"
        stdout = io.StringIO()
        with redirect_stdout(stdout), patch.object(RecordingClient, "_request") as network:
            code = configure_repair.main([
                "--project-endpoint", ENDPOINT, "--subscription", "sub",
                "--model-deployment", "model", "--agent-name", "repair-test",
                "--publication", str(self.publication), "--output-dir", str(output)])
            network.assert_not_called()
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("Configuration written:", text)
        self.assertIn(str(output / "azure.yaml"), text)
        self.assertIn("Agent: repair-test", text)
        self.assertIn("Model: model", text)
        for skill in SKILLS:
            self.assertIn(f"{skill}: version 4", text)
        self.assertIn("No Azure resources changed", text)
        self.assertIn("No agent deployed or invoked", text)

    def test_preflight_exact_definition_pins_and_publication(self):
        with patch.object(run_software_repair, "verify_prepared", return_value=self.verified), \
             patch.object(self.client, "get_agent", return_value={"versions": {"latest": self.agent}}):
            self.assertEqual(run_software_repair.preflight(
                self.client, self.prepared, self.publication, self.manifest)[1], self.agent)
            self.agent["definition"] = dict(self.definition, model="other")
            with self.assertRaisesRegex(HarnessError, "definition differs"):
                run_software_repair.preflight(self.client, self.prepared, self.publication, self.manifest)
            self.agent["definition"] = self.definition
            published = json.loads(self.publication.read_text())
            published["versions"][SKILLS[0]] = "5"
            self.publication.write_text(json.dumps(published))
            with self.assertRaisesRegex(HarnessError, "pin"):
                run_software_repair.preflight(self.client, self.prepared, self.publication, self.manifest)

    def test_preflight_checks_nonlatest_immutable_version(self):
        latest = dict(self.agent, version="2")
        with patch.object(run_software_repair, "verify_prepared", return_value=self.verified), \
             patch.object(self.client, "get_agent", return_value={"versions": {"latest": latest}}), \
             patch.object(self.client, "_request", return_value=json.dumps(self.agent).encode()) as read:
            self.assertEqual(run_software_repair.preflight(
                self.client, self.prepared, self.publication, self.manifest)[1]["version"], "1")
            self.assertTrue(read.call_args.args[0].endswith("/versions/1"))

    def run_fixture(self, output):
        def invoke(text, *, conversation_id):
            self.assertEqual(conversation_id, "conv")
            self.assertEqual(text, run_software_repair.prompt(self.task["case_id"]))
            (output / "request.json").write_text(json.dumps({
                "input": text, "agent_reference": self.reference, "conversation": conversation_id}))
            return self.response
        with patch.object(run_software_repair, "preflight", return_value=(self.verified, self.agent)), \
             patch.object(self.client, "create_conversation", return_value="conv"), \
             patch.object(self.client, "invoke", side_effect=invoke) as submit, \
             patch.object(run_software_repair, "validate_execution"), \
             patch.object(self.client, "download_outputs", side_effect=self.download):
            result = run_software_repair.run(
                self.client, self.prepared, self.publication, self.manifest, output)
            submit.assert_called_once()
            return result

    def test_runner_and_readonly_audit_check_real_patch_and_original_downloads(self):
        output = self.root / "run"
        result = self.run_fixture(output)
        self.assertEqual(result["artifact_validation"]["status"], "passed")
        self.assertEqual(result["downloaded_sha256"]["repair.json"],
                         digest((output / "downloads/repair.json").read_bytes()))
        audit_dir = self.root / "audit"
        with patch.object(run_software_repair, "preflight", return_value=(self.verified, self.agent)), \
             patch.object(run_software_repair, "validate_execution"), \
             patch.object(self.client, "get_response", return_value=self.response), \
             patch.object(self.client, "download_outputs", side_effect=self.download), \
             patch.object(self.client, "invoke") as invoke:
            checked = run_software_repair.audit(
                self.client, self.prepared, self.publication, self.manifest, output, audit_dir)
            self.assertEqual(checked["status"], "reconciled")
            self.assertEqual(checked["paid_invocations"], 0)
            invoke.assert_not_called()
        self.assertEqual((self.prepared / "packages/software-repair/repository/src/example.py").read_text(),
                         "value = 1\n")

    def test_run_failure_stops_once_and_records_original_error(self):
        output = self.root / "failed"
        with patch.object(run_software_repair, "preflight", return_value=(self.verified, self.agent)), \
             patch.object(self.client, "create_conversation", return_value="conv"), \
             patch.object(self.client, "invoke", side_effect=HarnessError("upload denied")) as submit:
            with self.assertRaisesRegex(HarnessError, "upload denied"):
                run_software_repair.run(self.client, self.prepared, self.publication, self.manifest, output)
            submit.assert_called_once()
        self.assertFalse((output / "result.json").exists())
        self.assertFalse(json.loads((output / "failure.json").read_text())["retried"])
        with patch.object(run_software_repair, "preflight") as preflight:
            with self.assertRaisesRegex(HarnessError, "exists"):
                run_software_repair.run(self.client, self.prepared, self.publication, self.manifest, output)
            preflight.assert_not_called()

    def test_audit_rejects_changed_download_and_preserves_failure(self):
        output = self.root / "run"
        self.run_fixture(output)
        (output / "downloads/report.md").write_text("Changed original.")
        target = self.root / "audit"
        with patch.object(run_software_repair, "preflight", return_value=(self.verified, self.agent)), \
             patch.object(run_software_repair, "validate_execution"), \
             patch.object(self.client, "get_response", return_value=self.response), \
             patch.object(self.client, "download_outputs", side_effect=self.download), \
             patch.object(self.client, "invoke") as invoke:
            with self.assertRaisesRegex(HarnessError, "download provenance"):
                run_software_repair.audit(
                    self.client, self.prepared, self.publication, self.manifest, output, target)
            invoke.assert_not_called()
        self.assertTrue((target / "failure.json").exists())
        self.assertFalse((target / "audit.json").exists())

    def test_publication_compares_every_byte_of_both_packages(self):
        for skill in SKILLS:
            (self.prepared / "packages" / skill / "SKILL.md").write_text(skill)
        def command(args, log):
            if args[2] == "create":
                return json.dumps({"name": args[3], "version": "4"})
            skill = args[3]
            shutil.copytree(self.prepared / "packages" / skill,
                            Path(args[args.index("--output-dir") + 1]))
            return "downloaded"
        output = self.root / "published"
        stdout = io.StringIO()
        with patch.object(publish_repair_skills, "verify_prepared", return_value=self.verified), \
             patch.object(publish_incident_skills, "command", side_effect=command) as write, \
             redirect_stdout(stdout):
            code = publish_repair_skills.main([
                "--prepared-dir", str(self.prepared), "--project-endpoint", ENDPOINT,
                "--operation", "create", "--output-dir", str(output), "--allow-azure-writes"])
            self.assertEqual(code, 0)
            self.assertEqual(write.call_count, 4)
        result = json.loads((output / "publication.json").read_text())
        self.assertEqual(result["scenario"], "repair")
        self.assertEqual(result["versions"], self.versions)
        for skill in SKILLS:
            self.assertIn(f"{skill}: version 4", stdout.getvalue())
        self.assertIn("Published and verified 2 Skills", stdout.getvalue())
        self.assertIn(str(output / "publication.json"), stdout.getvalue())
        self.assertIn("No agent deployed or invoked", stdout.getvalue())

    def test_publication_stops_on_first_write_failure_without_retry(self):
        output = self.root / "failed-publication"
        with patch.object(publish_repair_skills, "verify_prepared", return_value=self.verified), \
             patch.object(publish_incident_skills, "command", side_effect=HarnessError("ambiguous write")) as write, \
             redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaisesRegex(HarnessError, "ambiguous"):
                publish_repair_skills.publish(self.prepared, ENDPOINT, output, "create")
            write.assert_called_once()
            self.assertNotIn("Published and verified", stdout.getvalue())
        self.assertFalse(json.loads((output / "failure.json").read_text())["retried"])
        self.assertFalse((output / "publication.json").exists())

    def test_verifier_rejects_changed_source_before_publication(self):
        (self.prepared / "preparation.json").write_text('{"status":"verified"}')
        with patch.object(publish_incident_skills, "command") as write:
            with self.assertRaisesRegex(HarnessError, "source/config"):
                publish_repair_skills.publish(self.prepared, ENDPOINT, self.root / "bad", "create")
            write.assert_not_called()

    def test_paid_and_write_gates_run_before_network(self):
        with patch.object(publish_repair_skills, "publish") as publish:
            code = publish_repair_skills.main([
                "--prepared-dir", str(self.prepared), "--project-endpoint", ENDPOINT,
                "--operation", "create", "--output-dir", str(self.root / "publication")])
            self.assertEqual(code, 1)
            publish.assert_not_called()
        with patch.object(sys, "argv", ["run", "--project-endpoint", ENDPOINT,
                                       "--subscription", "sub", "--agent-name", "repair-test",
                                       "--agent-version", "1", "--prepared-dir", str(self.prepared),
                                       "--publication", str(self.publication), "--manifest", str(self.manifest),
                                       "--output-dir", str(self.root / "run")]), \
             patch.object(run_software_repair, "RecordingClient") as client:
            self.assertEqual(run_software_repair.main(), 1)
            client.assert_not_called()

    def test_documented_module_help_works_outside_repository_directory(self):
        import os
        for module in ["prepare_software_repair", "evaluate_software_repair", "configure_repair",
                       "publish_repair_skills", "run_software_repair"]:
            result = subprocess.run(
                [sys.executable, "-B", "-m", "scripts." + module, "--help"],
                cwd=self.root, env=dict(os.environ, PYTHONPATH=str(ROOT)),
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
