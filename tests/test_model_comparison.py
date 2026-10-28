import argparse
import base64
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness_client import HarnessClient, HarnessError, ResponseError
from scripts import compare_models as study


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def config(self, count=10):
        path = self.root / f"models-{count}.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "models": [{"id": f"model-{i}", "deployment": f"deployment-{i}"} for i in range(count)],
        }))
        return study.configuration(path)

    def state(self, count=10):
        config = self.config(count)
        config["region"] = "westus"
        return {
            "study": config, "registered_full_schedule": study.full_schedule(config),
            "subscription": "sub", "resource_group": "group", "account": "account",
            "project_id": "/project", "project_endpoint": "https://example.services.ai.azure.com/api/projects/p",
            "agent_name": "comparison-test", "approved_max_invocations": config["full_max_invocations"],
            "launch_cutoff_utc": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "source_sha256": study.sources(), "gate_nonce": "TEST", "run_kind": "full",
            "server_telemetry": "unknown", "pricing": "unknown", "python_image": "unused",
        }

    def models(self, state):
        return [{"name": m["deployment"], "properties": {
            "model": {"name": m["deployment"], "version": "1"}, "provisioningState": "Succeeded"},
            "sku": {"name": "Test", "capacity": 50}} for m in state["study"]["models"]]

    def test_ten_model_schedule_is_180_unique_jobs_and_190_maximum_calls(self):
        config = self.config()
        jobs = study.full_schedule(config)
        self.assertEqual(len(jobs), 180)
        self.assertEqual(len({(j["model"], j["case"], j["repeat"]) for j in jobs}), 180)
        self.assertEqual(config["full_max_invocations"], 190)
        for model in config["models"]:
            self.assertEqual(len([j for j in jobs if j["model"] == model["id"]]), 18)
        two = self.config(2)
        self.assertEqual(two["full_max_invocations"], 38)

    def test_roster_template_excludes_gpt_oss_and_includes_new_requested_models(self):
        roster = json.loads((study.SAMPLE / "models.example.json").read_text())
        ids = {m["id"] for m in roster["models"]}
        self.assertEqual(len(ids), 9)
        self.assertTrue({"terra61", "terra56", "luna56", "sol61"} <= ids)
        self.assertNotIn("sol56", ids)
        self.assertNotIn("gpt-oss120", ids)
        with self.assertRaisesRegex(HarnessError, "placeholder"):
            study.configuration(study.SAMPLE / "models.example.json")

    def test_nine_model_schedule_has_162_scored_jobs_and_171_total_calls(self):
        config = self.config(9)
        self.assertEqual(config["full_max_invocations"], 171)
        self.assertEqual(len(study.full_schedule(config)), 162)
        model = json.loads((study.SAMPLE / "model-deployments/terra61.json").read_text())
        self.assertEqual(model["properties"]["model"], {
            "format": "OpenAI", "name": "gpt-6.1-terra", "version": "2026-10-26"})
        self.assertEqual(model["sku"], {"name": "DataZoneStandard", "capacity": 100})
        self.assertEqual(model["properties"]["versionUpgradeOption"], "NoAutoUpgrade")

    def test_duplicate_roster_ids_and_deployments_are_rejected(self):
        for duplicate in ["id", "deployment"]:
            path = self.root / "bad.json"
            models = [{"id": "a", "deployment": "deployment-a"}, {"id": "b", "deployment": "deployment-b"}]
            models[1][duplicate] = models[0][duplicate]
            path.write_text(json.dumps({"schema_version": 1, "models": models}))
            with self.assertRaisesRegex(HarnessError, "distinct"):
                study.configuration(path)
        path.write_text("[]")
        with self.assertRaises(HarnessError):
            study.configuration(path)

    def test_changed_battery_or_fixtures_are_rejected(self):
        self.config(2)
        original = Path.read_text

        def changed(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            if path == study.SAMPLE / "battery.json":
                value = json.loads(text)
                value["cases"][0]["prompt"] += " changed"
                return json.dumps(value)
            return text

        with patch.object(Path, "read_text", changed):
            with self.assertRaisesRegex(HarnessError, "fingerprint"):
                study.configuration(self.root / "models-2.json")

    def test_agent_variants_differ_only_in_model_and_keep_neutral_tool_profile(self):
        first = study.definition(study.render("https://example.services.ai.azure.com/api/projects/p",
                                             "one", "comparison", self.root / "one"))
        second = study.definition(study.render("https://example.services.ai.azure.com/api/projects/p",
                                              "two", "comparison", self.root / "two"))
        first.pop("model")
        second.pop("model")
        self.assertEqual(first, second)
        self.assertEqual(first["instructions"], study.instructions())
        tools = first["tools"][0]
        self.assertFalse(tools["default_config"]["enabled"])
        self.assertEqual({x["name"] for x in tools["configs"]},
                         {"filesystem_read", "filesystem_write", "shell"})

    def test_scored_prompt_and_inputs_do_not_leak_oracles_or_model_names(self):
        state = self.state(2)
        prompt, files = study.task_payload(state, "compute")
        self.assertEqual(files, {})
        self.assertNotIn("78498", prompt)
        prompt, files = study.task_payload(state, "debug")
        self.assertEqual(set(files), {"debugging_starter.py"})
        self.assertIn("VERIFY_HANDOFF", prompt)
        self.assertNotIn("model_comparison_eval", prompt)
        self.assertNotIn("deployment-0", prompt)

    def test_gate_accepts_native_completion_with_wrappers_but_not_text_only(self):
        gate = {"nonce": "TEST", "value": 5050, "python": "3.13"}
        for command, exitcode in [
            ("python3 /home/session/workspace/data/gate_input.py", 0),
            ("cd /home/session/workspace/outputs && python3 /home/session/workspace/data/gate_input.py", 0),
            ("mkdir -p /home/session/workspace/outputs && cd /home/session/workspace/data && python3 gate_input.py", 0),
            ("cd /home/session/workspace/data && python3 ./gate_input.py", 0),
            ("python3 /home/session/workspace/data/gate_input.py && find /missing", 1),
        ]:
            response = {"output": [
                {"type": "shell_call", "call_id": "a", "action": {"commands": [command]}},
                {"type": "shell_call_output", "call_id": "a", "output": [{
                    "stdout": json.dumps(gate, sort_keys=True) + "\nGATE_SCRIPT_COMPLETED=TEST\n",
                    "outcome": {"exit_code": exitcode}}]},
            ]}
            self.assertTrue(study.verify_gate_execution(response, gate)["script_completed"])
            response["output"][1]["output"][0]["stdout"] = json.dumps(gate)
            with self.assertRaises(HarnessError):
                study.verify_gate_execution(response, gate)
        with self.assertRaises(HarnessError):
            study.verify_gate_execution({"output": []}, gate)
        response["output"][0]["action"]["commands"] = ["python3 other_gate_input.py"]
        response["output"][1]["output"][0]["stdout"] = (
            json.dumps(gate, sort_keys=True) + "\nGATE_SCRIPT_COMPLETED=TEST\n")
        with self.assertRaises(HarnessError):
            study.verify_gate_execution(response, gate)

    def test_paid_and_write_flags_stop_before_remote_calls(self):
        with patch.object(study, "load_state") as load:
            with self.assertRaises(HarnessError):
                study.run_study(argparse.Namespace(allow_paid_invocations=False))
            with self.assertRaises(HarnessError):
                study.deploy(argparse.Namespace(allow_azure_writes=False))
            load.assert_not_called()
        with patch.object(study, "worker") as worker, redirect_stderr(io.StringIO()):
            self.assertEqual(study.main(["_worker", "--job", "unused"]), 1)
            worker.assert_not_called()

    def exercise(self, *, count=10, failure=None, expired=False):
        state = self.state(count)
        if expired:
            state["launch_cutoff_utc"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        models = self.models(state)
        (self.root / "models-before.json").write_text(json.dumps(models))
        (self.root / "deployment-ready.json").write_text("{}")
        calls = []

        def job(state, directory, folder, model, case):
            folder.mkdir()
            calls.append((model, case))
            status = "completed_correct"
            if model == "model-0" and case == "compute" and len(calls) == count + 1 and failure:
                status = failure
            return {"model": model, "case": case, "status": status, "client_seconds": 0.1,
                    "grading": {"correct": status == "completed_correct"}, "retried": False}

        with patch.object(study, "load_state", return_value=state), \
             patch.object(study, "cli", return_value=models), \
             patch.object(study, "run_job", side_effect=job), redirect_stdout(io.StringIO()):
            args = argparse.Namespace(study_dir=self.root, allow_paid_invocations=True,
                                      max_invocations=state["study"]["full_max_invocations"])
            if failure == "evaluation_infrastructure_failed":
                with self.assertRaises(HarnessError):
                    study.run_study(args)
            else:
                study.run_study(args)
        return state, calls

    def test_run_has_exact_registered_budget_and_refuses_existing_run_directory(self):
        state, calls = self.exercise()
        self.assertEqual(len(calls), 190)
        result = json.loads((self.root / "full/result.json").read_text())
        self.assertEqual(result["scored_attempted"], 180)
        self.assertTrue(result["complete_scored_coverage"])
        self.assertEqual(result["client_retries"], 0)
        with patch.object(study, "load_state", return_value=state), \
             patch.object(study, "cli", return_value=self.models(state)), \
             patch.object(study, "run_job") as job:
            with self.assertRaises(FileExistsError):
                study.run_study(argparse.Namespace(study_dir=self.root, allow_paid_invocations=True,
                                                  max_invocations=190))
            job.assert_not_called()

    def test_operational_failure_pauses_only_that_model_without_retry(self):
        _, calls = self.exercise(failure="startup_failed")
        self.assertEqual(len([x for x in calls if x[0] == "model-0"]), 2)
        self.assertEqual(json.loads((self.root / "full/result.json").read_text())["paused"], ["model-0"])

    def test_wrong_completed_answer_continues_scoring(self):
        _, calls = self.exercise(failure="completed_incorrect")
        self.assertEqual(len(calls), 190)
        self.assertEqual(json.loads((self.root / "full/result.json").read_text())["paused"], [])

    def test_grader_infrastructure_failure_stops_entire_batch(self):
        _, calls = self.exercise(failure="evaluation_infrastructure_failed")
        self.assertEqual(len(calls), 11)
        self.assertTrue((self.root / "full/failure.json").exists())

    def test_expired_cutoff_starts_no_invocations(self):
        _, calls = self.exercise(expired=True)
        self.assertEqual(calls, [])
        result = json.loads((self.root / "full/result.json").read_text())
        self.assertEqual(result["status"], "launch_cutoff_reached")
        self.assertFalse(result["complete_scored_coverage"])

    def test_wrong_budget_is_rejected_before_cloud_preflight(self):
        state = self.state(2)
        with patch.object(study, "load_state", return_value=state), patch.object(study, "cli") as cli:
            with self.assertRaisesRegex(HarnessError, "38"):
                study.run_study(argparse.Namespace(study_dir=self.root, allow_paid_invocations=True,
                                                  max_invocations=190))
            cli.assert_not_called()

    def test_missing_model_stops_preparation_without_deployment_or_inference(self):
        self.config(2)
        project = {"location": "westus", "properties": {"endpoints": {
            "AI Foundry API": "https://example.services.ai.azure.com/api/projects/p"}}}
        args = argparse.Namespace(
            models=self.root / "models-2.json", stop_starting_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            max_invocations=38, region="westus", agent_name="new-test",
            project_endpoint="https://example.services.ai.azure.com/api/projects/p",
            subscription="sub", resource_group="rg", account="account", project="p",
            output_dir=self.root / "unused", python_image="unused")
        with patch.object(study, "cli", side_effect=[project, []]) as cli, \
             patch.object(study.HarnessClient, "invoke") as invoke:
            with self.assertRaisesRegex(HarnessError, "not deployed"):
                study.prepare(args)
            self.assertEqual(cli.call_count, 2)
            invoke.assert_not_called()
        self.assertFalse(args.output_dir.exists())

    def test_preparation_freezes_exact_roster_schedule_and_source_without_git(self):
        state = self.state(2)
        project = {"location": "westus", "properties": {"endpoints": {
            "AI Foundry API": state["project_endpoint"]}}}
        args = argparse.Namespace(
            models=self.root / "models-2.json", stop_starting_at=state["launch_cutoff_utc"],
            max_invocations=38, region="westus", agent_name="new-comparison",
            project_endpoint=state["project_endpoint"], subscription="sub", resource_group="rg",
            account="account", project="p", output_dir=self.root / "prepared",
            python_image="python@sha256:" + "a" * 64)
        with patch.object(study, "cli", side_effect=[project, self.models(state)]), \
             patch.object(study.HarnessClient, "_request", return_value=b'{"data":[],"has_more":false}'), \
             patch.object(study.subprocess, "run") as process:
            process.return_value = argparse.Namespace(stdout="[]")
            study.prepare(args)
            self.assertEqual(process.call_count, 1)
            self.assertEqual(process.call_args.args[0][:3], ["docker", "image", "inspect"])
        prepared = json.loads((args.output_dir / "state.json").read_text())
        self.assertEqual(prepared["approved_max_invocations"], 38)
        self.assertEqual(prepared["study"]["models"], state["study"]["models"])
        self.assertEqual(len(prepared["registered_full_schedule"]), 36)
        self.assertEqual(prepared["source_sha256"], study.sources())
        self.assertEqual(len(list((args.output_dir / "definitions").glob("*/azure.yaml"))), 2)

    def test_known_baseline_readiness_failure_stops_before_other_models(self):
        state = self.state(2)
        state["study"]["models"][0]["id"] = "sol61"
        state["registered_full_schedule"] = study.full_schedule(state["study"])
        models = self.models(state)
        (self.root / "models-before.json").write_text(json.dumps(models))
        (self.root / "deployment-ready.json").write_text("{}")
        with patch.object(study, "load_state", return_value=state), \
             patch.object(study, "cli", return_value=models), \
             patch.object(study, "run_job", return_value={
                 "model": "sol61", "case": "gate", "status": "startup_failed"}) as job, \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(HarnessError, "baseline readiness"):
                study.run_study(argparse.Namespace(study_dir=self.root, allow_paid_invocations=True,
                                                  max_invocations=38))
            job.assert_called_once()
        self.assertTrue((self.root / "full/failure.json").exists())

    def test_deploy_shared_identity_only_receives_one_project_grant(self):
        state = self.state(2)
        records = []
        for index, model in enumerate(state["study"]["models"], 1):
            manifest = study.render(state["project_endpoint"], model["deployment"], state["agent_name"],
                                    self.root / "definitions" / model["id"])
            records.append({"status": "active", "version": str(index),
                            "definition": study.definition(manifest),
                            "instance_identity": {"principal_id": "one-principal"}})
        with patch.object(study, "load_state", return_value=state), patch.object(study, "azd"), \
             patch.object(study, "selected", side_effect=records), \
             patch.object(study, "pinned", side_effect=records), \
             patch.object(study, "cli", side_effect=[[], {"created": True}]) as cli, \
             patch.object(study.time, "sleep") as sleep, redirect_stdout(io.StringIO()):
            study.deploy(argparse.Namespace(study_dir=self.root, allow_azure_writes=True,
                                            allow_project_role_grants=True))
        ready = json.loads((self.root / "deployment-ready.json").read_text())
        self.assertEqual(ready, {"versions": 2, "project_role_grants": 1, "identities_verified": 1})
        self.assertEqual(cli.call_count, 2)
        self.assertEqual(cli.call_args_list[-1].args[0][-2:], ["--scope", "/project"])
        sleep.assert_called_once_with(300)

    def test_worker_replay_guard_and_failed_response_usage_preservation(self):
        state = self.state(1)
        versions = {"model-0": {"version": "1", "model": "deployment-0"}}
        (self.root / "versions.json").write_text(json.dumps(versions))
        folder = self.root / "job"
        folder.mkdir()
        job = folder / "job.json"
        job.write_text(json.dumps({"study_dir": str(self.root), "model_id": "model-0", "case_id": "compute"}))
        response = {"id": "failed-test", "status": "failed", "model": "deployment-0", "usage": None,
                    "output": [], "agent_reference": {
                        "type": "agent_reference", "name": state["agent_name"], "version": "1"}}
        with patch.object(study, "load_state", return_value=state), \
             patch.object(study, "StudyClient") as factory:
            client = factory.return_value
            client.create_conversation.return_value = "test-conversation"
            client.invoke.side_effect = ResponseError("Service failed", response)
            with self.assertRaises(ResponseError):
                study.worker(job)
            self.assertEqual(json.loads((folder / "response.json").read_text())["status"], "failed")
            self.assertIsNone(json.loads((folder / "summary.json").read_text())["usage"])
            with self.assertRaisesRegex(HarnessError, "already submitted"):
                study.worker(job)
            client.invoke.assert_called_once()

    def worker_fixture(self, *, wrong_model=False):
        state = self.state(1)
        (self.root / "versions.json").write_text(json.dumps({
            "model-0": {"version": "1", "model": "deployment-0"}}))
        folder = self.root / "native-gate"
        folder.mkdir()
        job = folder / "job.json"
        job.write_text(json.dumps({"study_dir": str(self.root), "model_id": "model-0", "case_id": "gate"}))
        gate = {"nonce": "TEST", "value": 5050, "python": "3.13"}
        bodies = []

        def transport(client, path, body=None, **kwargs):
            if path == client._agent_path:
                value = {"versions": {"latest": {
                    "version": "1", "status": "active",
                    "definition": {"kind": "prompt", "harness": {"type": "github_copilot_preview"}}}}}
            elif path.endswith("/conversations"):
                value = {"id": "test-conversation"}
            elif path.endswith("/responses"):
                bodies.append(body)
                self.assertEqual(json.loads((folder / "request.json").read_text()), body)
                prompt, inputs = study.task_payload(state, "gate")
                self.assertEqual(body["input"], study.file_input(prompt, inputs))
                attached = next(p for p in body["input"][0]["content"] if p["type"] == "input_file")
                self.assertEqual(base64.b64decode(attached["file_data"].split(",", 1)[1]), inputs["gate_input.py"])
                hashes = {n: study.hashlib.sha256(raw).hexdigest() for n, raw in inputs.items()}
                value = {
                    "id": "response-test", "status": "completed",
                    "model": "unexpected-model" if wrong_model else "deployment-0",
                    "conversation": {"id": "test-conversation"}, "agent_reference": body["agent_reference"],
                    "created_at": 100, "completed_at": 105, "usage": {"input_tokens": 10, "output_tokens": 5},
                    "output": [
                        {"type": "shell_call", "call_id": "hash",
                         "action": {"commands": [study.hash_command(hashes)]}},
                        {"type": "shell_call_output", "call_id": "hash", "output": [{
                            "outcome": {"exit_code": 0},
                            "stdout": "HANDOFF_VERIFIED=" + json.dumps(hashes, sort_keys=True)}]},
                        {"type": "shell_call", "call_id": "script", "action": {
                            "commands": ["python3 /home/session/workspace/data/gate_input.py"]}},
                        {"type": "shell_call_output", "call_id": "script", "output": [{
                            "outcome": {"exit_code": 0},
                            "stdout": json.dumps(gate, sort_keys=True) + "\nGATE_SCRIPT_COMPLETED=TEST\n"}]},
                        {"type": "message", "role": "assistant", "content": [{
                            "type": "output_text", "text": "Test fixture.",
                            "annotations": [{"type": "file_path", "file_id": "gate-file",
                                             "filename": "gate-result.json"}]}]},
                    ],
                }
            elif path == "/files/gate-file/content":
                return json.dumps(gate).encode()
            else:
                raise AssertionError(path)
            return json.dumps(value).encode()

        with patch.object(study, "load_state", return_value=state), \
             patch.object(HarnessClient, "_request", transport), redirect_stderr(io.StringIO()):
            if wrong_model:
                with self.assertRaisesRegex(HarnessError, "deployment alias"):
                    study.worker(job)
            else:
                study.worker(job)
        return folder, bodies, gate

    def test_native_gate_worker_runs_all_contract_checks_against_scripted_transport(self):
        folder, bodies, gate = self.worker_fixture()
        self.assertEqual(len(bodies), 1)
        self.assertEqual(json.loads((folder / "downloads/gate-result.json").read_text()), gate)
        self.assertTrue(json.loads((folder / "readiness-evidence.json").read_text())["script_completed"])
        self.assertTrue((folder / "worker-success.json").exists())

    def test_wrong_returned_model_is_rejected_without_download_or_retry(self):
        folder, bodies, _ = self.worker_fixture(wrong_model=True)
        self.assertEqual(len(bodies), 1)
        self.assertFalse((folder / "worker-success.json").exists())
        self.assertFalse((folder / "downloads").exists())

    def test_audit_rechecks_actual_file_bytes_and_never_submits_a_request(self):
        folder, _, gate = self.worker_fixture()
        run = self.root / "full"
        run.mkdir()
        folder = folder.rename(run / "001-model-0-gate")
        response = json.loads((folder / "response.json").read_text())
        state = self.state(1)
        requests = []

        def transport(client, path, body=None, **kwargs):
            self.assertIsNone(body)
            requests.append(path)
            if "/responses/" in path:
                return json.dumps(response).encode()
            if path == "/files/gate-file/content":
                return json.dumps(gate).encode()
            raise AssertionError(path)

        with patch.object(study, "load_state", return_value=state), \
             patch.object(HarnessClient, "_request", transport):
            study.audit_study(argparse.Namespace(study_dir=self.root, output_dir=self.root / "audit"))
        audit = json.loads((self.root / "audit/audit.json").read_text())
        self.assertEqual(audit["paid_invocations"], 0)
        self.assertEqual(len(audit["records"]), 1)
        self.assertEqual(audit["records"][0]["receiver_verification"]["status"], "verified")
        self.assertEqual((self.root / "audit/001-model-0-gate/downloads/gate-result.json").read_bytes(),
                         (folder / "downloads/gate-result.json").read_bytes())
        self.assertEqual(len(requests), 2)

    def test_report_contains_exact_prompts_and_unknown_metrics_not_zero(self):
        state, _ = self.exercise(count=2)
        with patch.object(study, "load_state", return_value=state):
            study.report(argparse.Namespace(study_dir=self.root, output_dir=self.root / "report"))
        result = json.loads((self.root / "report/results.json").read_text())
        self.assertEqual(len(result["records"]), 38)
        self.assertTrue(all(r["total_tokens"] is None for r in result["records"]))
        self.assertTrue(all(r["actual_billed_usd"] is None for r in result["records"]))
        self.assertTrue(all(r["internal_model_loops"] is None for r in result["records"]))
        prompts = json.loads((self.root / "report/prompts.json").read_text())
        self.assertIn("48271 multiplied by 79613", prompts["cases"]["compute"]["exact_user_prompt"])
        self.assertIn("Attempts / planned", (self.root / "report/report.md").read_text())


class HttpObservationTests(unittest.TestCase):
    def test_observer_logs_only_safe_headers_without_tokens_or_bodies(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"x-ms-request-id": "test", "Authorization": "header-secret"}

        events = []
        client = HarnessClient("https://example.services.ai.azure.com/api/projects/p", "sub", "agent",
                               token_provider=lambda: "token-secret", http_observer=events.append)
        with patch.object(client._opener, "open", return_value=Response(b"{}")):
            self.assertEqual(client._request("/agents"), b"{}")
        self.assertEqual(events[-1]["headers"], {"x-ms-request-id": "test"})
        self.assertNotIn("secret", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
