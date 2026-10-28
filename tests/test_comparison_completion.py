import argparse
import ast
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from harness_client import HarnessError
from scripts import compare_models as study
from scripts import complete_comparison as completion


class CompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        roster = self.root / "models.json"
        roster.write_text(json.dumps({"schema_version": 1, "models": [{"id": "test", "deployment": "deployment"}]}))
        config = study.configuration(roster)
        self.state = {
            "study": config, "source_sha256": study.sources(), "gate_nonce": "TEST",
            "registered_full_schedule": study.full_schedule(config), "run_kind": "full",
            "project_endpoint": "https://example.services.ai.azure.com/api/projects/p",
            "subscription": "sub", "agent_name": "agent", "resource_group": "rg", "account": "account",
            "launch_cutoff_utc": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        }
        self.original = self.root / "original"
        self.gate_folder = self.original / "full/001-test-gate"
        self.gate_folder.mkdir(parents=True)
        study.save(self.original / "state.json", self.state)
        study.save(self.original / "full/result.json", {"status": "schedule_finished"})
        study.save(self.gate_folder / "job.json", {"model_id": "test", "case_id": "gate"})
        study.save(self.gate_folder / "worker-failure.json", {"error": completion.GATE_ERROR})

    def row(self, job):
        return dict(job, readiness_only=job["case"] == "gate", correct=True, status="completed_correct")

    def reports(self):
        original = {"source_sha256": self.state["source_sha256"], "limitations": [], "records": [
            {"model": "test", "case": "gate", "repeat": None, "readiness_only": True,
             "correct": None, "status": "service_or_contract_failure"}]}
        continuation = {"source_sha256": study.sources(),
                        "records": [self.row(job) for job in self.state["registered_full_schedule"]]}
        return original, continuation

    def test_only_never_submitted_jobs_are_planned_even_without_a_response(self):
        jobs, gate = completion.continuation_plan(self.original, self.state, "test")
        self.assertEqual(len(jobs), 18)
        self.assertEqual(gate, self.gate_folder)
        folder = self.original / "full/002-test-compute-r1"
        folder.mkdir()
        study.save(folder / "job.json", {"model_id": "test", "case_id": "compute"})
        study.save(folder / "request.json", {"accepted_remote_state": "unknown"})
        jobs, _ = completion.continuation_plan(self.original, self.state, "test")
        self.assertEqual(len(jobs), 17)
        self.assertNotIn({"model": "test", "case": "compute", "repeat": 1}, jobs)

    def test_unfinished_schedule_or_other_failure_is_not_eligible(self):
        (self.original / "full/result.json").write_text('{"status":"launch_cutoff_reached"}')
        with self.assertRaisesRegex(HarnessError, "finish"):
            completion.continuation_plan(self.original, self.state, "test")
        (self.original / "full/result.json").write_text('{"status":"schedule_finished"}')
        (self.gate_folder / "worker-failure.json").write_text('{"error":"server_error"}')
        with self.assertRaisesRegex(HarnessError, "false negative"):
            completion.continuation_plan(self.original, self.state, "test")

    def preflight(self, directory):
        directory.mkdir()
        study.save(directory / "job.json", {"model_id": "test", "case_id": "compute"})
        study.save(directory / "worker-failure.json", {
            "error": "Foundry transport failed for /conversations: Temporary failure in name resolution"})
        events = [{"event": "request_start", "method": "POST", "path": "/conversations"}]
        (directory / "http.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n")

    def test_dns_preflight_is_not_a_model_invocation_and_any_request_blocks_replay(self):
        directory = self.original / "full/002-test-compute-r1"
        self.preflight(directory)
        self.assertTrue(completion.preflight_only(directory))
        jobs, _ = completion.continuation_plan(self.original, self.state, "test", all_unsubmitted=True)
        self.assertEqual(len(jobs), 18)
        jobs, _ = completion.continuation_plan(self.original, self.state, "test")
        self.assertEqual(len(jobs), 17)
        study.save(directory / "request.json", {"remote_outcome": "unknown"})
        self.assertFalse(completion.preflight_only(directory))
        jobs, _ = completion.continuation_plan(self.original, self.state, "test", all_unsubmitted=True)
        self.assertEqual(len(jobs), 17)

    def test_post_start_evidence_prevents_preflight_classification_without_a_request_file(self):
        directory = self.original / "full/002-test-compute-r1"
        self.preflight(directory)
        with (directory / "http.jsonl").open("a") as stream:
            stream.write(json.dumps({"event": "request_start", "method": "POST", "path": "/responses"}) + "\n")
        self.assertFalse(completion.preflight_only(directory))

    def test_previous_continuation_request_is_excluded_even_if_its_response_body_was_lost(self):
        previous = self.root / "previous"
        folder = previous / "full/001-test-compute-r1"
        folder.mkdir(parents=True)
        study.save(folder / "job.json", {"model_id": "test", "case_id": "compute"})
        study.save(folder / "request.json", {"remote_outcome": "unknown"})
        jobs, _ = completion.continuation_plan(
            self.original, self.state, "test", all_unsubmitted=True, previous=[previous])
        self.assertEqual(len(jobs), 17)
        self.assertNotIn({"model": "test", "case": "compute", "repeat": 1}, jobs)

    def test_transport_exception_normalization_does_not_allow_request_behavior_changes(self):
        snapshot = self.root / "transport-source"
        state = dict(self.state, source_sha256=dict(self.state["source_sha256"]))
        for name in state["source_sha256"]:
            destination = snapshot / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(study.ROOT / name, destination)
        completion.unchanged_protocol(state, snapshot)
        client = snapshot / "harness_client.py"
        client.write_text(client.read_text().replace('"Bearer " + token', '"Basic " + token', 1))
        state["source_sha256"]["harness_client.py"] = study.sha(client)
        with self.assertRaisesRegex(HarnessError, "Shared client changed"):
            completion.unchanged_protocol(state, snapshot)

    def test_quality_keeps_recovered_correctness_but_excludes_interrupted_timing(self):
        original, continuation = self.reports()
        job = continuation["records"].pop(0)
        original["records"].append(dict(
            job, correct=None, status="service_or_contract_failure",
            worker_error="Foundry transport failed: The read operation timed out."))
        preflight = dict(continuation["records"][0], correct=None, directory="dns",
                         status="service_or_contract_failure")
        original["records"].append(preflight)
        state = dict(self.state, continuation={"models": ["test"], "preflight_only_directories": ["dns"]})
        late = dict(job, grading={"correct": True}, usage={"total_tokens": 10})
        combined = completion.combine_reports(original, continuation, state, {}, late)
        self.assertEqual(combined["request_attempts"], 19)
        self.assertEqual(len(combined["preflight_records"]), 1)
        self.assertEqual(combined["models"]["test"]["quality_evaluated"], 18)
        self.assertEqual(combined["models"]["test"]["quality_correct"], 18)
        recovered = next(row for row in combined["records"] if row["verified_after_client_timeout"])
        self.assertFalse(recovered["timing_eligible"])
        self.assertEqual(recovered["status"], "service_or_contract_failure")
        self.assertIsNone(recovered["correct"])
        self.assertEqual(recovered["late_usage"], {"total_tokens": 10})

    def test_unreconciled_worker_failure_has_no_quality_score_or_valid_timing(self):
        original, continuation = self.reports()
        row = continuation["records"][0]
        row.update(correct=None, status="service_or_contract_failure",
                   record_error="Worker exited before saving an incomplete HTTP body")
        state = dict(self.state, continuation={"model": "test"})
        combined = completion.combine_reports(original, continuation, state, {})
        failure = next(row for row in combined["records"] if row["case"] == "compute" and row["repeat"] == 1)
        self.assertIsNone(failure["quality_correct"])
        self.assertFalse(failure["quality_evaluated"])
        self.assertFalse(failure["timing_eligible"])
        self.assertFalse(combined["complete_quality_coverage"])

    def test_protocol_check_allows_only_the_gate_function_to_change(self):
        snapshot = self.root / "source"
        state = dict(self.state, source_sha256=dict(self.state["source_sha256"]))
        for name in state["source_sha256"]:
            destination = snapshot / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(study.ROOT / name, destination)
        controller = snapshot / "scripts/compare_models.py"
        tree = ast.parse(controller.read_text())
        gate = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "verify_gate_execution")
        gate.body = ast.parse("raise RuntimeError('old checker')").body
        controller.write_text(ast.unparse(tree))
        state["source_sha256"]["scripts/compare_models.py"] = study.sha(controller)
        completion.unchanged_protocol(state, snapshot)
        controller.write_text(controller.read_text().replace('INPUT_ROOT = ', 'OTHER_ROOT = ', 1))
        state["source_sha256"]["scripts/compare_models.py"] = study.sha(controller)
        with self.assertRaisesRegex(HarnessError, "beyond"):
            completion.unchanged_protocol(state, snapshot)
        with self.assertRaisesRegex(HarnessError, "fingerprints"):
            completion.unchanged_protocol(self.state, snapshot)

    def test_combined_report_keeps_original_gate_and_labels_the_continuation(self):
        original, continuation = self.reports()
        state = dict(self.state, continuation={"model": "test"})
        combined = completion.combine_reports(original, continuation, state, {"paid_invocations": 0})
        self.assertTrue(combined["complete_scored_coverage"])
        self.assertEqual(combined["scored_attempted"], 18)
        self.assertEqual(combined["request_attempts"], 19)
        self.assertEqual(combined["records"][0]["status"], "service_or_contract_failure")
        self.assertEqual(combined["records"][1]["phase"], "continuation")
        self.assertEqual(combined["models"]["test"]["scored_correct"], 18)
        continuation["records"].pop()
        self.assertFalse(completion.combine_reports(original, continuation, state, {})["complete_scored_coverage"])
        continuation["records"].append(continuation["records"][0])
        with self.assertRaisesRegex(HarnessError, "overlap"):
            completion.combine_reports(original, continuation, state, {})

    def test_paid_flag_and_exact_unsubmitted_count_are_required_before_cloud_calls(self):
        with patch.object(completion, "unchanged_protocol"), patch.object(completion, "HarnessClient") as client:
            with self.assertRaisesRegex(HarnessError, "approval"):
                completion.complete(argparse.Namespace(allow_paid_invocations=False))
            args = argparse.Namespace(allow_paid_invocations=True, study_dir=self.original,
                                      original_source=self.root, model="test", max_invocations=19)
            with self.assertRaisesRegex(HarnessError, "18"):
                completion.complete(args)
            client.assert_not_called()

    def test_read_only_gate_recheck_accepts_relative_execution_and_empty_logprobs_omission(self):
        state = self.state
        gate = {"nonce": "TEST", "value": 5050, "python": "3.13"}
        agent = {"type": "agent_reference", "name": "agent", "version": "1"}
        response = {
            "id": "response", "model": "deployment", "agent_reference": agent,
            "status": "completed", "conversation": {"id": "conversation"},
            "output": [
                {"type": "shell_call", "call_id": "call", "action": {
                    "commands": ["cd /home/session/workspace/data && python3 gate_input.py"]}},
                {"type": "shell_call_output", "call_id": "call", "output": [{
                    "stdout": json.dumps(gate, sort_keys=True) + "\nGATE_SCRIPT_COMPLETED=TEST\n",
                    "outcome": {"exit_code": 0}}]},
                {"type": "message", "content": [{"type": "output_text", "text": "Done", "logprobs": []}]},
            ],
        }
        study.save(self.gate_folder / "response.json", response)
        prompt, files = study.task_payload(state, "gate")
        study.save(self.gate_folder / "request.json", {
            "input": study.file_input(prompt, files), "agent_reference": agent, "conversation": "conversation"})
        (self.gate_folder / "downloads").mkdir()
        (self.gate_folder / "downloads/gate-result.json").write_text(json.dumps(gate))
        live = json.loads(json.dumps(response))
        del live["output"][-1]["content"][0]["logprobs"]

        def download(response, directory):
            directory.mkdir()
            path = directory / "gate-result.json"
            shutil.copyfile(self.gate_folder / "downloads/gate-result.json", path)
            return [path]

        with patch.object(completion, "HarnessClient") as factory, patch.object(study, "verify_consumption"):
            client = factory.return_value
            client.get_response.return_value = live
            client.download_outputs.side_effect = download
            review = completion.recheck_gate(
                client, state, self.gate_folder, {"version": "1", "model": "deployment"}, self.root / "review")
            self.assertTrue(review["script_completed"])
            self.assertEqual(review["paid_invocations"], 0)
            client.invoke.assert_not_called()
            live["output"][1]["output"][0]["stdout"] = "changed"
            with self.assertRaisesRegex(HarnessError, "differs"):
                completion.recheck_gate(
                    client, state, self.gate_folder, {"version": "1", "model": "deployment"}, self.root / "bad-review")

    def test_completion_runs_exactly_18_scored_jobs_and_refuses_a_second_claim(self):
        original_report, continuation_report = self.reports()
        report_dir = self.root / "report"
        report_dir.mkdir()
        study.save(report_dir / "results.json", original_report)
        audit = self.root / "audit.json"
        study.save(audit, {"status": "reconciled", "paid_invocations": 0, "records": [{"response_id": "response"}]})
        study.save(self.gate_folder / "response.json", {"id": "response"})
        version = {"version": "1", "model": "deployment", "record": {"definition": {"model": "deployment"}}}
        study.save(self.original / "versions.json", {"test": version})
        models = [{"name": "deployment", "properties": {"model": {"version": "1"},
                   "provisioningState": "Succeeded"}, "sku": {"name": "Test", "capacity": 50}}]
        study.save(self.original / "models-before.json", models)
        args = argparse.Namespace(
            allow_paid_invocations=True, study_dir=self.original, original_source=self.root, model="test",
            max_invocations=18, original_report=report_dir, original_audit=audit, output_dir=self.root / "completion")
        args.all_unsubmitted = True
        args.model = None

        def fake_report(arguments):
            arguments.output_dir.mkdir()
            study.save(arguments.output_dir / "results.json", continuation_report)

        def fake_job(state, directory, folder, model, case):
            self.assertNotEqual(case, "gate")
            return {"model": model, "case": case, "status": "completed_correct",
                    "grading": {"correct": True}, "client_seconds": 1}

        with patch.object(completion, "unchanged_protocol"), patch.object(completion, "HarnessClient"), \
             patch.object(completion, "recheck_gate", return_value={"paid_invocations": 0}), \
             patch.object(study, "pinned", return_value={"status": "active", "definition": {"model": "deployment"}}), \
             patch.object(study, "cli", return_value=models), patch.object(study, "run_job", side_effect=fake_job) as job, \
             patch.object(study, "audit_study") as auditor, patch.object(study, "report", side_effect=fake_report), \
             redirect_stdout(io.StringIO()):
            completion.complete(args)
            self.assertEqual(job.call_count, 18)
            auditor.assert_called_once()
            args.output_dir = self.root / "different-output"
            with self.assertRaisesRegex(HarnessError, "already claimed"):
                completion.complete(args)
            self.assertEqual(job.call_count, 18)
        combined = json.loads((self.root / "completion/combined-results.json").read_text())
        self.assertTrue(combined["complete_scored_coverage"])
        self.assertEqual(combined["request_attempts"], 19)

    def test_timeout_record_cannot_add_an_unsubmitted_or_foreign_task(self):
        path = self.root / "reconciliation.json"
        study.save(path, {"original_directory": str(self.root / "different-study/job"),
                         "paid_invocations": 0, "resubmitted": False})
        with patch.object(completion, "HarnessClient") as client:
            with self.assertRaisesRegex(HarnessError, "original submitted"):
                completion.reconcile_timeout(path, self.original, self.state, {}, self.root / "inspection")
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
