import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness_client import HarnessError
from scripts.method_selection import CONFIG, ROOT, TEMPLATE, compare, instructions, prepare, schedule, source_hashes, verify_arms
from scripts import method_selection as controller
from retail_support import RecordingClient


def version(arm, number):
    return {"version": number, "status": "active", "definition": {
        "kind": "prompt", "model": "same-model",
        "harness": {"type": "github_copilot_preview"},
        "instructions": instructions(arm),
    }}


class MethodSelectionTests(unittest.TestCase):
    def test_local_render_parameters_and_both_harness_families(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "azure.yaml"
            controller.render("https://example.services.ai.azure.com/api/projects/p",
                              "sub", "model", "new-agent", path)
            source = json.loads(path.read_text())
            agent = source["services"]["outcome-exploration-agent"]
            self.assertEqual(agent["name"], "new-agent")
            self.assertEqual(agent["model"], "model")
            self.assertEqual(agent["harness"]["type"], "microsoft_copilot_preview")
            for family in ["microsoft_copilot_preview", "github_copilot_preview"]:
                left, right = version("neutral", "1"), version("tool-preferring", "2")
                left["definition"]["harness"] = right["definition"]["harness"] = {"type": family}
                verify_arms(left, right)
            with self.assertRaises(FileExistsError):
                controller.render("https://example.services.ai.azure.com/api/projects/p",
                                  "sub", "model", "new-agent", path)

    def test_trial_timeout_records_context_and_never_replays(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "trial"
            client = RecordingClient("https://example.services.ai.azure.com/api/projects/p",
                                     "sub", "agent", agent_version="1")
            with patch.object(client, "create_conversation", return_value="conv"), \
                 patch.object(client, "invoke", side_effect=HarnessError("timeout")) as invoke:
                with self.assertRaisesRegex(HarnessError, "timeout"):
                    controller.run_trial(client, schedule()[0], folder, source_hashes())
                invoke.assert_called_once()
            self.assertEqual(json.loads((folder / "01/conversation.json").read_text())["id"], "conv")
            with self.assertRaises(FileExistsError):
                controller.run_trial(client, schedule()[0], folder, source_hashes())

    def test_experiment_runs_twelve_registered_jobs_without_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            calls = []
            def trial(client, job, folder, hashes):
                folder.mkdir()
                calls.append(job)
            with patch.object(controller, "pinned", side_effect=[
                version("neutral", "1"), version("tool-preferring", "2")]), \
                 patch.object(controller, "run_trial", side_effect=trial), \
                 patch.object(controller, "compare", return_value={}), \
                 patch.object(controller.subprocess, "check_output") as git:
                controller.experiment("https://example.services.ai.azure.com/api/projects/p",
                                      "sub", "agent", "1", "2", output, 300)
                git.assert_not_called()
            self.assertEqual(calls, schedule())
            self.assertIsNone(json.loads((output / "experiment.json").read_text())["source_commit"])
            with patch.object(controller, "pinned") as read:
                with self.assertRaisesRegex(HarnessError, "already exists"):
                    controller.experiment("https://example.services.ai.azure.com/api/projects/p",
                                          "sub", "agent", "1", "2", output, 300)
                read.assert_not_called()

    def test_failed_trial_stops_batch_without_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with patch.object(controller, "pinned", side_effect=[
                version("neutral", "1"), version("tool-preferring", "2")]), \
                 patch.object(controller, "run_trial", side_effect=HarnessError("failed")) as trial:
                with self.assertRaisesRegex(HarnessError, "failed"):
                    controller.experiment("https://example.services.ai.azure.com/api/projects/p",
                                          "sub", "agent", "1", "2", output, 300)
                trial.assert_called_once()
            self.assertFalse(json.loads((output / "failure.json").read_text())["retried"])

    def test_cli_paid_opt_in_stops_before_remote_operations(self):
        import sys
        flags = ["method_selection", "run", "--project-endpoint",
                 "https://example.services.ai.azure.com/api/projects/p", "--subscription", "sub",
                 "--agent-name", "agent", "--neutral-version", "1", "--tool-version", "2",
                 "--output-dir", "unused"]
        with patch.object(sys, "argv", flags), patch.object(controller, "experiment") as run:
            self.assertEqual(controller.main(), 1)
            run.assert_not_called()

    def test_audit_retrieves_existing_trial_without_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = schedule()[0]
            folder = root / job["directory"] / "01"
            folder.mkdir(parents=True)
            reference = {"type": "agent_reference", "name": "agent", "version": "1"}
            response = {"id": "resp", "status": "completed", "model": "model",
                        "agent_reference": reference, "conversation": {"id": "conv"},
                        "output": []}
            (folder / "response.json").write_text(json.dumps(response))
            (folder / "request.json").write_text('{"conversation":"conv"}')
            (root / "experiment.json").write_text(json.dumps({
                "schedule": [job], "agent_name": "agent", "versions": {"neutral": "1"}}))
            with patch.object(controller, "compare", return_value={"records": [{"status": "completed"}]}), \
                 patch.object(controller, "HarnessClient") as factory:
                client = factory.return_value
                client.get_response.return_value = response
                client.conversation_items.return_value = [{"role": "user",
                    "content": [{"type": "input_text", "text": job["prompt"]}]}]
                controller.audit(root, root / "audit", "endpoint", "sub")
                client.invoke.assert_not_called()
                client.create_conversation.assert_not_called()
            result = json.loads((root / "audit/audit.json").read_text())
            self.assertFalse(result["complete_trial_coverage"])
            self.assertEqual(result["paid_invocations"], 0)

    def test_schedule_is_exactly_twelve(self):
        jobs = schedule()
        self.assertEqual(len(jobs), 12)
        self.assertEqual(len({j["directory"] for j in jobs}), 12)
        for case in ["primes", "multiplication", "modular-power"]:
            for arm in ["neutral", "tool-preferring"]:
                self.assertEqual(sum(j["id"] == case and j["arm"] == arm for j in jobs), 2)
        self.assertEqual(jobs[0]["arm"], "neutral")
        self.assertEqual(jobs[6]["arm"], "tool-preferring")

    def test_reference_answers(self):
        cases = {c["id"]: c for c in json.loads(CONFIG.read_text())["cases"]}
        self.assertEqual(cases["multiplication"]["expected_integer"], 48271 * 79613)
        self.assertEqual(cases["modular-power"]["expected_integer"], pow(7, 77, 1000003))
        n = 1000000
        sieve = bytearray(b"\x01") * n
        sieve[:2] = b"\x00\x00"
        for p in range(2, 1000):
            if sieve[p]:
                sieve[p*p:n:p] = b"\x00" * len(range(p*p, n, p))
        self.assertEqual(cases["primes"]["expected_integer"], sum(sieve))

    def test_exact_instruction_only_difference(self):
        neutral, tool = version("neutral", "1"), version("tool-preferring", "2")
        tool["definition"]["tools"] = []
        verify_arms(neutral, tool)
        tool["definition"]["model"] = "different-model"
        with self.assertRaisesRegex(HarnessError, "identical"):
            verify_arms(neutral, tool)

    def test_wrong_instruction_and_inactive_rejected(self):
        neutral, tool = version("neutral", "1"), version("tool-preferring", "2")
        tool["definition"]["instructions"] += " Extra."
        with self.assertRaises(HarnessError):
            verify_arms(neutral, tool)
        tool = version("tool-preferring", "2")
        tool["status"] = "failed"
        with self.assertRaises(HarnessError):
            verify_arms(neutral, tool)

    def test_prepare_changes_only_instructions_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "azure.yaml"
            prepare(TEMPLATE, output)
            source, rendered = json.loads(TEMPLATE.read_text()), json.loads(output.read_text())
            agent = rendered["services"]["outcome-exploration-agent"]
            self.assertEqual(agent["instructions"], instructions("tool-preferring"))
            agent["instructions"] = instructions("neutral")
            self.assertEqual(rendered, source)
            with self.assertRaises(FileExistsError):
                prepare(TEMPLATE, output)

    def test_modified_baseline_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = json.loads(TEMPLATE.read_text())
            source["services"]["outcome-exploration-agent"]["instructions"] = "Changed"
            path = Path(temporary) / "changed.json"
            path.write_text(json.dumps(source))
            with self.assertRaises(HarnessError):
                prepare(path, Path(temporary) / "out.json")

    def test_comparison_checks_identity_and_preserves_manual_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            job = schedule()[0]
            folder = output / job["directory"]
            (folder / "01").mkdir(parents=True)
            hashes = source_hashes()
            reference = {"name": "test-agent", "type": "agent_reference", "version": "1"}
            request = {"input": job["prompt"], "conversation": "conv-1", "agent_reference": reference}
            response = {"id": "resp-1", "status": "completed", "agent_reference": reference,
                        "model": "same-model", "created_at": 0, "completed_at": 3,
                        "conversation": {"id": "conv-1"},
                        "output": [{"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "78,498"}]}]}
            files = {
                output / "versions.json": {"neutral": version("neutral", "1"),
                                          "tool-preferring": version("tool-preferring", "2")},
                output / "experiment.json": {
                    "schedule": schedule(), "versions": {"neutral": "1", "tool-preferring": "2"},
                    "agent_name": "test-agent", "config_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
                    "git_commit": "source-commit", "driver_sha256": hashes,
                },
                folder / "conditions.json": {
                    "git_commit": "source-commit", "git_worktree_status": [],
                    "prompts": [job["prompt"]], "source_sha256": hashes,
                },
                folder / "01/request.json": request,
                folder / "01/response.json": response,
                folder / "summary.json": {"runs": [{
                    "parent_function_calls": [], "parent_shell_calls": 0,
                    "parent_tool_activity_observed": False, "service_seconds": 3,
                }]},
            }
            for path, data in files.items():
                path.write_text(json.dumps(data))
            result = compare(output)
            self.assertEqual(result["totals"]["neutral"]["expected_integer_present"], 1)
            self.assertEqual(result["records"][0]["correctness"], "manual_review_required")
            request["input"] = "Unexpected prompt"
            (folder / "01/request.json").write_text(json.dumps(request))
            with self.assertRaisesRegex(HarnessError, "identity"):
                compare(output)

    def test_missing_run_is_not_counted_as_completed(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "experiment.json").write_text(json.dumps({
                "schedule": schedule(), "config_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
                "driver_sha256": source_hashes(),
                "versions": {"neutral": "1", "tool-preferring": "2"},
            }))
            (output / "versions.json").write_text(json.dumps({
                "neutral": version("neutral", "1"), "tool-preferring": version("tool-preferring", "2")}))
            result = compare(output)
            self.assertEqual(result["totals"]["neutral"]["completed"], 0)
            self.assertTrue(all(r["status"] == "missing_or_stopped" for r in result["records"]))


if __name__ == "__main__":
    unittest.main()
