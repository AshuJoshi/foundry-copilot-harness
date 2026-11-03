import base64
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harness_client import HarnessError
from scripts import run_incident_review as controller
from scripts.run_incident_review import (
    FileClient, MARKER, content, fixture_payload, hash_command, render_role, verify_consumption,
)


class HandoffTests(unittest.TestCase):
    def test_audit_refuses_partial_workflow_without_any_model_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source-snapshot.json").write_text('{"hashes":{},"mode":"natural"}')
            args = SimpleNamespace(audit_run_dir=root)
            with patch.object(controller, "FileClient") as client:
                with self.assertRaisesRegex(HarnessError, "complete"):
                    controller.audit(args)
                client.assert_not_called()

    def test_natural_workflow_freezes_independent_before_peer_critique_and_revises_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = root / "prepared"
            (prepared / "packages/incident-investigation-telemetry").mkdir(parents=True)
            (prepared / "packages/incident-investigation-telemetry/input-manifest.json").write_text("{}")
            publication = root / "publication.json"
            versions = {name: "5" for name in [
                "incident-investigation-telemetry", "incident-trace-data-part-one", "incident-trace-data-part-two"]}
            publication.write_text(json.dumps({
                "status": "verified", "project_endpoint": "https://example.services.ai.azure.com/api/projects/demo",
                "package_sha256": {}, "input_verification": {"case_id": "case-001"}, "versions": versions}))
            args = SimpleNamespace(
                output_dir=root / "run", prepared_dir=prepared, publication=publication, mode="natural",
                project_endpoint="https://example.services.ai.azure.com/api/projects/demo", subscription="sub",
                challenger="challenger", challenger_version="1", challenger_manifest=root / "c",
                investigator="investigator", investigator_version="1", investigator_manifest=root / "i")
            calls = []

            def stage(client, folder, prompt, attachments, verified):
                calls.append((folder.name, attachments))
                downloads = folder / "downloads"
                downloads.mkdir(parents=True)
                name = "critique.json" if folder.name == "C1" else "diagnosis.json"
                artifact = downloads / name
                artifact.write_text(json.dumps({"stage": folder.name}))
                report = downloads / "report.md"
                report.write_text("Report")
                return [artifact, report]

            def freeze(own, peer, manifest, archive, output):
                self.assertEqual([name for name, _ in calls], ["I0", "C0"])
                output.write_text("{}")

            record = {"definition": {"model": "same", "skills": [
                {"name": name, "version": version} for name, version in versions.items()]}}
            with patch.object(controller, "verify_prepared", return_value={"case_id": "case-001"}), \
                 patch.object(controller, "package_hashes", return_value={}), \
                 patch.object(controller, "selected", return_value=record), \
                 patch.object(controller, "stage", side_effect=stage), \
                 patch.object(controller, "freeze", side_effect=freeze), \
                 patch.object(controller, "validate_artifacts", return_value={"status": "passed"}), \
                 patch.object(controller, "validate_critique", return_value={"status": "mechanically_valid"}), \
                 patch.object(controller.subprocess, "check_output") as git:
                controller.run(args)
                git.assert_not_called()
            self.assertEqual([name for name, _ in calls], ["I0", "C0", "C1", "I1"])
            self.assertEqual(calls[0][1], {})
            self.assertEqual(calls[1][1], {})
            self.assertEqual(json.loads(calls[2][1]["independent-assessment.json"]), {"stage": "C0"})
            self.assertEqual(json.loads(calls[2][1]["peer-diagnosis.json"]), {"stage": "I0"})
            self.assertEqual(json.loads(calls[3][1]["peer-critique.json"]), {"stage": "C1"})
            self.assertEqual(json.loads((args.output_dir / "result.json").read_text())["invocations"], 4)
            with patch.object(controller, "verify_prepared") as verify:
                with self.assertRaisesRegex(HarnessError, "exists"):
                    controller.run(args)
                verify.assert_not_called()

    def test_attachment_is_actual_byte_preserving_input_file_not_prompt_text(self):
        raw = b'{"case_id":"case-001"}\n'
        message = content("Read attachment", {"peer.json": raw})[0]
        self.assertEqual(message["content"][0]["text"], "Read attachment")
        file = message["content"][1]
        self.assertEqual(file["type"], "input_file")
        self.assertEqual(base64.b64decode(file["file_data"].split(",", 1)[1]), raw)
        with self.assertRaises(HarnessError):
            content("text", {"../escape.json": raw})

    def test_receiver_must_execute_exact_hash_command_with_actual_output(self):
        expected = {"peer.json": hashlib.sha256(b"bytes").hexdigest()}
        command = hash_command(expected)
        import json
        response = {"output": [
            {"type": "shell_call", "call_id": "call-1", "action": {"commands": [command]}},
            {"type": "shell_call_output", "call_id": "call-1", "output": [{
                "outcome": {"exit_code": 0}, "stdout": MARKER + json.dumps(expected),
            }]},
        ]}
        verify_consumption(response, command, expected)
        response["output"][1]["output"][0]["outcome"]["exit_code"] = 1
        with self.assertRaises(HarnessError):
            verify_consumption(response, command, expected)

    def test_file_request_is_recorded_before_transport_failure(self):
        client = FileClient("https://example.services.ai.azure.com/api/projects/demo",
                            "sub", "agent", agent_version="1")
        with tempfile.TemporaryDirectory() as temporary:
            client.evidence_dir = Path(temporary)
            client.attachments = {"peer.json": b"{}"}
            with patch("harness_client.HarnessClient._request", side_effect=HarnessError("transport")):
                with self.assertRaises(HarnessError):
                    client._request(client._protocol_path + "/responses", {"input": "Read file"})
            import json
            body = json.loads((Path(temporary) / "request.json").read_text())
            self.assertEqual(body["input"][0]["content"][1]["type"], "input_file")

    def test_render_role_preserves_instructions_and_refuses_overwrite(self):
        import json
        versions = {"incident-investigation-telemetry": "4",
                    "incident-trace-data-part-one": "4", "incident-trace-data-part-two": "4"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "azure.yaml"
            render_role("challenger", "https://example.services.ai.azure.com/api/projects/demo",
                        "existing-model", "incident-review-challenger", versions, path)
            value = json.loads(path.read_text())
            agent = value["services"]["incident-review-challenger"]
            self.assertEqual(agent["model"], "existing-model")
            self.assertTrue(all(s["version"] == "4" for s in agent["skills"]))
            with self.assertRaises(FileExistsError):
                render_role("challenger", "endpoint", "model", "name", versions, path)

    def test_fixture_payload_excludes_oracle_and_self_disclosure(self):
        fixture = json.loads((controller.ROOT / "agents/incident-review/faulty-diagnosis.json").read_text())
        fixture["expected_objections"] = [{"criterion": "ORACLE_SENTINEL"}]
        raw = fixture_payload(fixture)
        self.assertEqual(json.loads(raw), fixture["diagnosis"])
        for forbidden in ["ORACLE_SENTINEL", "expected_objections", "fixture_kind", "evaluator_only"]:
            self.assertNotIn(forbidden, raw.decode())
        fixture["diagnosis"]["limitations"] = ["Fixture intentionally bad"]
        with self.assertRaisesRegex(HarnessError, "discloses"):
            fixture_payload(fixture)

    def test_control_has_exactly_independent_then_critique_and_no_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(output_dir=root)
            calls = []
            def stage(client, folder, prompt, attachments, verified):
                calls.append({"folder": folder.name, "prompt": prompt, "attachments": attachments})
                target = folder / "downloads"
                target.mkdir(parents=True)
                artifact = target / ("diagnosis.json" if folder.name == "C0" else "critique.json")
                artifact.write_text('{"independent":"actual output"}')
                report = target / "report.md"
                report.write_text("Output")
                return [artifact, report]
            def freeze(independent, diagnosis, manifest, archive, output):
                output.write_text('{"binding":"frozen"}')
            with patch.object(controller, "stage", side_effect=stage), \
                 patch.object(controller, "validate_diagnosis"), \
                 patch.object(controller, "validate_artifacts"), \
                 patch.object(controller, "freeze", side_effect=freeze), \
                 patch.object(controller, "validate_critique", return_value={"status": "mechanically_valid"}):
                controller.fixture_control(args, object(), {"case_id": "case-001"},
                                           root / "archive.zip", root / "manifest.json")
            self.assertEqual([call["folder"] for call in calls], ["C0", "C1"])
            self.assertEqual(calls[0]["attachments"], {})
            peer = json.loads(calls[1]["attachments"]["peer-diagnosis.json"])
            self.assertEqual(set(calls[1]["attachments"]), {
                "peer-diagnosis.json", "independent-assessment.json",
                "review-binding.json", "review-contracts.json",
            })
            self.assertEqual(peer["originating_service"], "frontend")
            self.assertNotIn(b"expected_objections", b"".join(calls[1]["attachments"].values()))
            self.assertEqual(calls[1]["prompt"], controller.CRITIQUE_TASK)
            result = json.loads((root / "result.json").read_text())
            self.assertEqual(result["invocations"], 2)
            self.assertIn("human-authored", result["proposal_origin"])
            self.assertFalse((root / "I0").exists())
            self.assertFalse((root / "I1").exists())

    def test_control_failure_stops_before_second_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(controller, "validate_diagnosis"), \
                 patch.object(controller, "stage", side_effect=HarnessError("timeout")) as stage:
                with self.assertRaisesRegex(HarnessError, "timeout"):
                    controller.fixture_control(SimpleNamespace(output_dir=root), object(),
                                               {"case_id": "case-001"}, root / "archive", root / "manifest")
                self.assertEqual(stage.call_count, 1)
            self.assertFalse((root / "result.json").exists())

    def test_same_conversation_stage_does_not_create_new_history(self):
        client = FileClient("https://example.services.ai.azure.com/api/projects/demo",
                            "sub", "agent", agent_version="1")
        response = {
            "id": "resp-1", "status": "completed", "conversation": {"id": "same-conversation"},
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done"}]}],
        }
        loads = [{"type": "function_call", "name": "skill", "call_id": "initial-skill"}]
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(client, "create_conversation") as create, \
             patch.object(client, "invoke", return_value=response) as invoke, \
             patch.object(client, "download_outputs", return_value=[]), \
             patch.object(controller, "validate_execution") as validate:
            controller.stage(client, Path(temporary) / "S0", "Self-check", {}, {},
                             "same-conversation", loads)
        create.assert_not_called()
        self.assertEqual(invoke.call_args.kwargs["conversation_id"], "same-conversation")
        validated = validate.call_args.args[0]["output"]
        self.assertEqual(validated[0], loads[0])
        self.assertEqual(validated[-1]["type"], "message")

    def test_reused_proof_contains_only_initial_skill_loads_not_old_verifier(self):
        value = {"output": [
            {"type": "function_call", "call_id": "skill-1", "name": "skill"},
            {"type": "function_call_output", "call_id": "skill-1"},
            {"type": "shell_call", "call_id": "old-verifier"},
            {"type": "shell_call_output", "call_id": "old-verifier"},
        ]}
        self.assertEqual(controller.initial_skill_loads(value), value["output"][:2])

    def test_self_review_has_four_stages_one_agent_one_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []
            client = object()
            def stage(actual_client, folder, prompt, attachments, verified,
                      conversation_id=None, skill_load_evidence=None):
                self.assertIs(actual_client, client)
                calls.append((folder.name, conversation_id, skill_load_evidence, attachments))
                downloads = folder / "downloads"
                downloads.mkdir(parents=True)
                artifact = downloads / ("critique.json" if folder.name == "S1" else "diagnosis.json")
                artifact.write_text("{}")
                report = downloads / "report.md"
                report.write_text("Fixture result")
                if folder.name == "I0":
                    (folder / "conversation.json").write_text('{"id":"same-conversation"}')
                    (folder / "response.json").write_text(json.dumps({"output": [
                        {"type": "function_call", "call_id": "skill-1", "name": "skill"},
                        {"type": "function_call_output", "call_id": "skill-1"},
                    ]}))
                return [artifact, report]
            def freeze(independent, diagnosis, manifest, archive, output):
                output.write_text("{}")
            with patch.object(controller, "stage", side_effect=stage), \
                 patch.object(controller, "validate_artifacts", return_value={"status": "passed"}), \
                 patch.object(controller, "freeze", side_effect=freeze), \
                 patch.object(controller, "validate_critique", return_value={"status": "mechanically_valid"}):
                controller.self_review_control(SimpleNamespace(output_dir=root), client,
                                               {"case_id": "case-001"}, root / "zip", root / "manifest")
            self.assertEqual([item[0] for item in calls], ["I0", "S0", "S1", "I1"])
            self.assertIsNone(calls[0][1])
            self.assertTrue(all(item[1] == "same-conversation" for item in calls[1:]))
            self.assertTrue(all(item[2] for item in calls[1:]))
            self.assertIn("self-check-assessment.json", calls[2][3])
            for item in calls[1:]:
                self.assertIn("self-initial-diagnosis.json", item[3])
                self.assertNotIn("peer-diagnosis.json", item[3])
                self.assertEqual(item[3]["self-initial-diagnosis.json"], calls[1][3]["self-initial-diagnosis.json"])
            result = json.loads((root / "result.json").read_text())
            self.assertEqual(result["invocations"], 4)
            self.assertFalse(result["independent_review"])
            self.assertEqual(result["conversation_count"], 1)

    def test_self_review_failure_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(controller, "stage", side_effect=HarnessError("timeout")) as stage:
                with self.assertRaisesRegex(HarnessError, "timeout"):
                    controller.self_review_control(SimpleNamespace(output_dir=Path(temporary)),
                                                   object(), {"case_id": "case-001"},
                                                   Path(temporary) / "archive",
                                                   Path(temporary) / "manifest")
            self.assertEqual(stage.call_count, 1)



if __name__ == "__main__":
    unittest.main()
