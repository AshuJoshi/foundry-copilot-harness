import base64
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

from harness_client import HarnessClient, HarnessError
from scripts import prepare_retail
from scripts import run_retail as controller
from retail_evidence import references


class DeadlineTests(unittest.TestCase):
    def slow_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "20")
                self.end_headers()
                for _ in range(20):
                    try:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    time.sleep(0.05)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        return f"http://127.0.0.1:{server.server_port}/"

    def test_socket_timeout_is_not_a_total_response_deadline(self):
        local = self.slow_server()
        client = HarnessClient("https://example.services.ai.azure.com/api/projects/demo",
                               "sub", "agent", timeout=0.25, token_provider=lambda: "fake-test-token")
        with patch.object(client._opener, "open",
                          side_effect=lambda request, timeout: urllib.request.urlopen(local, timeout=timeout)):
            started = time.monotonic()
            self.assertEqual(client._request("/local-only"), b" " * 20)
            self.assertGreater(time.monotonic() - started, 0.75)

    def test_slow_active_response_cannot_extend_outer_deadline(self):
        local = self.slow_server()
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            script = (
                "import sys,urllib.request; "
                "print('READING',flush=True); "
                "response=urllib.request.urlopen(sys.argv[1],timeout=0.25); "
                "response.read(); print('MUST_NOT_COMPLETE',flush=True)"
            )
            with self.assertRaisesRegex(HarnessError, "deadline"):
                controller.wait_worker([sys.executable, "-u", "-c", script, local], folder, 0.6)
            self.assertIn("READING", (folder / "worker.log").read_text())
            self.assertNotIn("MUST_NOT_COMPLETE", (folder / "worker.log").read_text())

    def test_real_worker_is_killed_at_deadline_and_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            script = (
                "from pathlib import Path; import sys,time; "
                "Path(sys.argv[1]).write_text('started'); "
                "time.sleep(30); Path(sys.argv[2]).write_text('must not happen')"
            )
            started = time.monotonic()
            with self.assertRaisesRegex(HarnessError, "deadline"):
                controller.wait_worker([sys.executable, "-c", script, str(folder / "started"),
                                        str(folder / "late")], folder, 0.5)
            self.assertLess(time.monotonic() - started, 3)
            self.assertFalse((folder / "late").exists())
            record = json.loads((folder / "deadline.json").read_text())
            self.assertFalse(record["retried"])
            self.assertFalse(record["service_cancellation_confirmed"])
            self.assertIsNotNone(record["worker_returncode"])

    def test_completed_or_failed_worker_is_recorded_without_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            success = root / "success"
            success.mkdir()
            controller.wait_worker([sys.executable, "-c", "print('complete')"], success, 3)
            self.assertEqual((success / "worker.log").read_text().strip(), "complete")
            failed = root / "failed"
            failed.mkdir()
            with self.assertRaisesRegex(HarnessError, "exit code 7"):
                controller.wait_worker([sys.executable, "-c", "raise SystemExit(7)"], failed, 3)
            self.assertEqual(json.loads((failed / "worker-result.json").read_text())["returncode"], 7)

    def test_invalid_deadline_cannot_start_a_process(self):
        for value in [0, -1, float("nan"), float("inf")]:
            with patch.object(controller.subprocess, "Popen") as process:
                with self.assertRaisesRegex(HarnessError, "finite and positive"):
                    controller.wait_worker(["unused"], Path("unused"), value)
                process.assert_not_called()


class RetailRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prepared = self.root / "prepared"
        self.prepared.mkdir()
        self.plan = {}
        self.refs = {}
        self.rows = []
        settings = prepare_retail.config()
        for index, stage in enumerate(settings["stages"][:3]):
            periods = stage["visible_periods"]
            for period in periods:
                if any(r["period"] == period for r in self.rows):
                    continue
                date = next(p["start"] for p in settings["periods"] if p["id"] == period)
                values = ["10001", "ITEM", "actual", 2,
                          controller.datetime.fromisoformat(date), 0.1, None, "United Kingdom"]
                self.rows.append(prepare_retail.normalize(values, "sheet", len(self.rows) + 2, settings))
            folder = self.prepared / stage["id"]
            folder.mkdir()
            prepare_retail.write_csv(folder / "evidence.csv", self.rows)
            (folder / "policy.json").write_text(json.dumps(settings["metric_policy"]))
            (folder / "DATA-NOTICE.txt").write_bytes(
                (controller.ROOT / "agents/retail-analysis/DATA-NOTICE.txt").read_bytes())
            manifest = {"stage_id": stage["id"], "visible_periods": periods,
                        "available_before": settings["periods"][index + 1]["end"],
                        "rows": len(self.rows), "evidence_sha256": prepare_retail.sha(folder / "evidence.csv"),
                        "policy_sha256": prepare_retail.sha(folder / "policy.json")}
            (folder / "manifest.json").write_text(json.dumps(manifest))
            self.plan[stage["id"]] = manifest
            self.refs[stage["id"]] = references(folder / "evidence.csv")
        self.plan["stable-control"] = {
            "stage_id": "stable-control", "evidence_from": "stage-3/evidence.csv",
            "evidence_sha256": self.plan["stage-3"]["evidence_sha256"], "rows": len(self.rows)}
        self.refs["stable-control"] = self.refs["stage-3"]
        evaluator = self.prepared / "evaluator"
        evaluator.mkdir()
        (evaluator / "references.json").write_text(json.dumps(self.refs))
        (evaluator / "stage-plan.json").write_text(json.dumps(self.plan))
        record = {"status": "verified", "source_sha256": settings["archive_sha256"],
                  "workbook_sha256": settings["workbook_sha256"],
                  "config_sha256": prepare_retail.sha(prepare_retail.CONFIG),
                  "preparer_sha256": prepare_retail.sha(controller.ROOT / "scripts/prepare_retail.py"),
                  "reference_helper_sha256": prepare_retail.sha(controller.ROOT / "retail_evidence.py"),
                  "openpyxl_version": "3.1.5", "sheet_rows_scanned": settings["source_sheet_rows"],
                  "stages": self.plan, "selected_rows": len(self.rows)}
        (self.prepared / "preparation.json").write_text(json.dumps(record))


    def test_actual_csv_bytes_are_preserved_and_unsafe_inputs_rejected(self):
        raw = 'header\n"£,quoted"\n'.encode()
        part = controller.content("read", {"stage.csv": raw})[0]["content"][1]
        self.assertEqual(base64.b64decode(part["file_data"].split(",", 1)[1]), raw)
        for files in [{}, {"../bad.csv": raw}, {"bad\\name.csv": raw}, {"a.zip": raw},
                      {"a.csv": b""}, {"a.csv": b"x" * (controller.MAX_FILE_BYTES + 1)}]:
            with self.assertRaises(HarnessError):
                controller.content("read", files)


    def test_request_is_saved_before_uncertain_transport_failure(self):
        client = controller.RetailClient("https://example.services.ai.azure.com/api/projects/demo",
                                         "sub", "agent", agent_version="1")
        client.evidence_dir = self.root
        client.attachments = {"retail-stage-1.csv": b"actual\n"}
        with patch("harness_client.HarnessClient._request", side_effect=HarnessError("transport")):
            with self.assertRaises(HarnessError):
                client._request(client._protocol_path + "/responses", {"input": "Read"})
        self.assertEqual(json.loads((self.root / "request.json").read_text())["input"][0]["content"][1]
                         ["filename"], "retail-stage-1.csv")


    def test_preparation_fingerprints_visibility_and_stability_are_checked(self):
        self.assertEqual(controller.verify_prepared(self.prepared), self.plan)
        path = self.prepared / "evaluator/stage-plan.json"
        changed = copy.deepcopy(self.plan)
        changed["stable-control"]["evidence_sha256"] = "bad"
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(HarnessError, "Stable"):
            controller.verify_prepared(self.prepared)
        path.write_text(json.dumps(self.plan))
        (self.prepared / "stage-1/evidence.csv").write_text("changed")
        with self.assertRaisesRegex(HarnessError, "identity"):
            controller.verify_prepared(self.prepared)


    def test_inputs_exclude_future_data_or_oracles_and_stable_names_are_identical(self):
        files, binding = controller.inputs(self.prepared, "stage-1", None)
        self.assertEqual(set(files), {"retail-stage-1.csv", "retail-policy.json",
                                     "DATA-NOTICE.txt", "binding-stage-1.json"})
        self.assertNotIn(b"week-2", b"".join(files.values()))
        self.assertNotIn(b"week-3", b"".join(files.values()))
        self.assertNotIn(b"net_transaction_value\": \"0.", b"".join(files.values()))
        self.assertEqual(binding["previous_assessment"], None)
        first, _ = controller.inputs(self.prepared, "stage-3", None)
        stable, _ = controller.inputs(self.prepared, "stable-control", None)
        self.assertEqual(first["retail-stage-3.csv"], stable["retail-stage-3.csv"])
        previous = self.root / "assessment-stage-1.json"
        previous.write_text('{"actual":"prior output"}')
        update, _ = controller.inputs(self.prepared, "stage-2", previous)
        self.assertEqual(update[previous.name], previous.read_bytes())


    def test_render_is_parameterized_and_refuses_overwrite(self):
        out = self.root / "deploy"
        controller.render("https://example.services.ai.azure.com/api/projects/demo", "model", "agent", out)
        manifest = json.loads((out / "azure.yaml").read_text())
        self.assertEqual(manifest["services"]["agent"]["name"], "agent")
        with self.assertRaises(FileExistsError):
            controller.render("https://example.services.ai.azure.com/api/projects/demo", "model", "agent", out)

    def test_scored_prompt_matches_historical_retail_contract(self):
        binding = {
            "stage_id": "stage-2", "evidence_filename": "retail-stage-2.csv",
            "visible_periods": ["baseline", "week-1", "week-2"],
            "evidence_sha256": "0123456789abcdef" * 4,
            "assessment_filename": "assessment-stage-2.json", "report_filename": "report-stage-2.md",
        }
        digest = controller.hashlib.sha256(controller.task(binding, "FIXED_HASH_COMMAND").encode()).hexdigest()
        self.assertEqual(digest, "520b781d6a2d6b35762c59c75b2be39e46a9aaffad715366b76c32b926fee679")

    def test_worker_refuses_to_resubmit_a_recorded_stage(self):
        for marker in ["request.json", "response.json"]:
            folder = self.root / marker.replace(".", "-")
            folder.mkdir()
            (folder / marker).write_text("{}")
            with patch.object(controller, "RetailClient") as client:
                with self.assertRaisesRegex(HarnessError, "recorded submission"):
                    controller.stage_worker(folder / "worker-job.json")
                client.assert_not_called()

    def test_cli_commands_resolve_and_reject_bad_local_configuration(self):
        for module, command in [("scripts.prepare_retail", "build"), ("scripts.run_retail", "render"),
                            ("scripts.run_retail", "run"), ("scripts.run_retail", "audit")]:
            result = subprocess.run([sys.executable, "-B", "-m", module, command, "--help"],
                                    cwd=controller.ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        with patch.object(controller.HarnessClient, "_request", side_effect=AssertionError("No Azure calls")):
            for name in ["../escape", "__AGENT__"]:
                with self.assertRaises(HarnessError):
                    controller.render("https://example.services.ai.azure.com/api/projects/demo",
                                      "model", name, self.root / "not-created")
        self.assertFalse((self.root / "not-created").exists())

    def transport_workflow(self, fail_stage=None):
        """Scripted service boundary; actual request, file and metric checks stay enabled."""
        client = controller.RetailClient("https://example.services.ai.azure.com/api/projects/demo",
                                     "sub", "agent", agent_version="1",
                                     token_provider=lambda: "unused-test-token")
        manifest_dir = self.root / "transport-manifest"
        controller.render(client.project_endpoint, "model", "agent", manifest_dir)
        manifest = manifest_dir / "azure.yaml"
        service = json.loads(manifest.read_text())["services"]["agent"]
        definition = {k: service[k] for k in ["kind", "model", "harness", "instructions"]}
        requests, responses, files = [], {}, {}

        def transport(path, body=None, **kwargs):
            if path == client._agent_path:
                value = {"versions": {"latest": {
                    "version": "1", "status": "active", "definition": definition}}}
            elif path.endswith("/conversations"):
                self.assertEqual(body, {})
                value = {"id": "test-conversation"}
            elif path.endswith("/responses") and body is not None:
                stage = controller.STAGES[len(requests)]
                requests.append(copy.deepcopy(body))
                if stage == fail_stage:
                    raise HarnessError("Uncertain transport failure; do not retry")
                saved = json.loads((client.evidence_dir / "request.json").read_text())
                self.assertEqual(saved, body)
                self.assertEqual(body["conversation"], "test-conversation")
                self.assertEqual(body["agent_reference"]["version"], "1")
                parts = body["input"][0]["content"]
                attached = {p["filename"]: base64.b64decode(p["file_data"].split(",", 1)[1])
                            for p in parts if p["type"] == "input_file"}
                self.assertFalse(any("evaluator" in name or "references" in name for name in attached))
                if len(requests) > 1:
                    prior = controller.STAGES[len(requests) - 2]
                    self.assertEqual(attached[f"assessment-{prior}.json"], files[f"{prior}-assessment"])
                if stage == "stable-control":
                    self.assertEqual(attached["retail-stage-3.csv"],
                                     (self.prepared / "stage-3/evidence.csv").read_bytes())
                hashes = {name: controller.hashlib.sha256(raw).hexdigest() for name, raw in attached.items()}
                citation = [{"source_row_id": self.rows[0]["source_row_id"],
                             "observed_values": {"quantity": "2", "unit_price": "0.1"}}]
                report = {
                    "schema_version": 1, "stage_id": stage,
                    "evidence_sha256": self.plan[stage]["evidence_sha256"],
                    "measure": "derived_net_transaction_value_not_profit",
                    "metrics": self.refs[stage],
                    "claims": [{"id": "C1", "kind": "observation", "text": "Positive lines.",
                                "evidence_refs": citation}],
                    "claim_updates": [] if stage == "stage-1" else [{
                        "prior_claim_id": "C1", "disposition": "retain",
                        "reason": "Same earlier cells.", "evidence_refs": citation}],
                    "limitations": ["Fixture only; no live model run."],
                }
                files[f"{stage}-assessment"] = json.dumps(report).encode()
                files[f"{stage}-report"] = b"Scripted offline fixture; not a model result."
                output = [
                    {"type": "shell_call", "call_id": stage,
                     "action": {"commands": ["cd /home/session/workspace && " + controller.hash_command(hashes)]}},
                    {"type": "shell_call_output", "call_id": stage, "output": [{
                        "outcome": {"exit_code": 0},
                        "stdout": "HANDOFF_VERIFIED=" + json.dumps(hashes, sort_keys=True)}]},
                    {"type": "message", "role": "assistant", "content": [{
                        "type": "output_text", "text": "Fixture complete.", "annotations": [
                            {"type": "file_path", "file_id": f"{stage}-assessment",
                             "filename": f"assessment-{stage}.json"},
                            {"type": "file_path", "file_id": f"{stage}-report",
                             "filename": f"report-{stage}.md"}]}]},
                ]
                value = {"id": "resp-" + stage, "status": "completed", "model": "model",
                         "agent_reference": body["agent_reference"],
                         "conversation": {"id": "test-conversation"}, "output": output}
                responses[value["id"]] = value
            elif "/responses/" in path:
                value = responses[path.rsplit("/", 1)[-1]]
            elif path.startswith("/files/") and path.endswith("/content"):
                return files[path.split("/")[2]]
            elif path.endswith("/items"):
                value = {"data": [{"role": "user"} for _ in requests], "has_more": False}
            else:
                raise AssertionError((path, body))
            return json.dumps(value).encode()

        output, audit = self.root / "transport-run", self.root / "transport-audit"
        with patch.object(HarnessClient, "_request", side_effect=transport), \
         patch.object(controller, "bounded_stage", side_effect=controller.execute_stage), \
         patch("sys.stdout", new_callable=io.StringIO):
            if fail_stage:
                with self.assertRaises(HarnessError):
                    controller.run(client, self.prepared, manifest, output)
            else:
                controller.run(client, self.prepared, manifest, output)
                before_audit = len(requests)
                controller.audit(client, self.prepared, manifest, output, audit)
                self.assertEqual(len(requests), before_audit)
        return requests, output, audit

    def test_transport_level_four_stage_workflow_checks_real_hash_and_download_contracts(self):
        requests, output, audit = self.transport_workflow()
        self.assertEqual(len(requests), 4)
        self.assertEqual(json.loads((audit / "audit.json").read_text())["paid_invocations"], 0)
        self.assertTrue(json.loads((audit / "audit.json").read_text())["complete_four_stage_run"])
        self.assertEqual(len(list(audit.glob("*/downloads/*"))), 8)
        with self.assertRaises(FileExistsError):
            controller.run(controller.RetailClient(
                "https://example.services.ai.azure.com/api/projects/demo", "sub", "agent",
                agent_version="1"), self.prepared, self.root / "unused", output)

    def test_transport_failure_on_second_stage_preserves_first_and_stops_before_third(self):
        requests, output, _ = self.transport_workflow(fail_stage="stage-2")
        self.assertEqual(len(requests), 2)
        self.assertTrue((output / "stage-1/validation.json").exists())
        self.assertTrue((output / "stage-2/request.json").exists())
        self.assertFalse((output / "stage-3").exists())
        self.assertTrue((output / "failure.json").exists())


    def test_paid_flag_is_required_before_any_client_or_network_call(self):
        with patch.object(controller, "RetailClient") as client:
            result = controller.main([
                "run", "--project-endpoint", "endpoint", "--subscription", "sub",
                "--agent-name", "agent", "--agent-version", "1", "--prepared", str(self.prepared),
                "--manifest", "manifest", "--output-dir", str(self.root / "run")])
            self.assertEqual(result, 1)
            client.assert_not_called()
            self.assertEqual(controller.main(["_stage", "--job", "unused"]), 1)
            client.assert_not_called()


    def test_job_uses_pinned_agent_and_supplies_no_extra_model_request(self):
        folder = self.root / "stage-1"
        folder.mkdir()
        client = controller.RetailClient("https://example.services.ai.azure.com/api/projects/demo",
                                         "sub", "agent", agent_version="1", timeout=5)
        with patch.object(controller, "wait_worker") as wait:
            controller.bounded_stage(client, self.prepared, folder, "stage-1", "same-conversation", None)
        job = json.loads((folder / "worker-job.json").read_text())
        self.assertEqual(job["conversation"], "same-conversation")
        self.assertEqual(job["agent_version"], "1")
        self.assertEqual(job["previous"], None)
        self.assertEqual(wait.call_args.args[2], 5)
        self.assertIn("--allow-paid-invocations", wait.call_args.args[0])


    def test_deadline_stops_before_next_stage_and_preserves_failure(self):
        client = controller.RetailClient("https://example.services.ai.azure.com/api/projects/demo",
                                         "sub", "agent", agent_version="1", timeout=1)
        manifest = self.root / "manifest"
        manifest.write_text("{}")
        output = self.root / "stopped"
        with patch.object(controller, "selected", return_value={}), \
             patch.object(client, "create_conversation", return_value="same"), \
             patch.object(controller, "bounded_stage", side_effect=HarnessError("deadline")) as stage, \
             patch.object(client, "invoke") as invoke:
            with self.assertRaisesRegex(HarnessError, "deadline"):
                controller.run(client, self.prepared, manifest, output)
            self.assertEqual(stage.call_count, 1)
            invoke.assert_not_called()
        self.assertFalse((output / "stage-2").exists())
        self.assertFalse(json.loads((output / "failure.json").read_text())["retried"])


    def exercise(self, fail=False):
        calls = []
        current = {}
        client = controller.RetailClient("https://example.services.ai.azure.com/api/projects/demo",
                                         "sub", "agent", agent_version="1")

        def invoke(prompt, *, conversation_id):
            stage_id = controller.STAGES[len(calls)]
            calls.append((stage_id, conversation_id, dict(client.attachments)))
            current["stage"] = stage_id
            response = {"id": "resp-" + stage_id, "status": "completed",
                        "conversation": {"id": conversation_id},
                        "output": [{"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "Done"}]}]}
            controller.save(client.evidence_dir / "request.json", {"input": prompt})
            return response

        def download(response, destination):
            if fail:
                raise HarnessError("download failed")
            destination.mkdir()
            stage_id = current["stage"]
            citation = [{"source_row_id": self.rows[0]["source_row_id"],
                         "observed_values": {"quantity": "2", "unit_price": "0.1"}}]
            report = {"schema_version": 1, "stage_id": stage_id,
                      "evidence_sha256": self.plan[stage_id]["evidence_sha256"],
                      "measure": "derived_net_transaction_value_not_profit",
                      "metrics": self.refs[stage_id],
                      "claims": [{"id": "C1", "kind": "observation", "text": "Positive lines.",
                                  "evidence_refs": citation}],
                      "claim_updates": [] if stage_id == "stage-1" else [{
                          "prior_claim_id": "C1", "disposition": "retain",
                          "reason": "Visible earlier rows unchanged.", "evidence_refs": citation}],
                      "limitations": ["No profit evidence."]}
            paths = [destination / f"assessment-{stage_id}.json", destination / f"report-{stage_id}.md"]
            paths[0].write_text(json.dumps(report))
            paths[1].write_text("Bounded test output, not a model result.")
            return paths

        output = self.root / "run"
        with patch.object(controller, "selected", return_value={"version": "1"}), \
             patch.object(client, "create_conversation", return_value="one-conversation") as create, \
             patch.object(client, "invoke", side_effect=invoke), \
             patch.object(controller, "verify_consumption") as consumption, \
             patch.object(controller, "bounded_stage", side_effect=controller.execute_stage), \
             patch.object(client, "download_outputs", side_effect=download):
            if fail:
                with self.assertRaisesRegex(HarnessError, "download"):
                    controller.run(client, self.prepared, self.root / "manifest", output)
            else:
                (self.root / "manifest").write_text("{}")
                controller.run(client, self.prepared, self.root / "manifest", output)
            self.assertEqual(create.call_count, 1)
            self.assertEqual(consumption.call_count, len(calls))
        return calls, output


    def test_four_stages_use_one_conversation_and_actual_prior_outputs(self):
        calls, output = self.exercise()
        self.assertEqual([c[0] for c in calls], list(controller.STAGES))
        self.assertEqual({c[1] for c in calls}, {"one-conversation"})
        for index in range(1, 4):
            previous = controller.STAGES[index - 1]
            actual = (output / previous / "downloads" / f"assessment-{previous}.json").read_bytes()
            self.assertEqual(calls[index][2][f"assessment-{previous}.json"], actual)
        self.assertEqual(json.loads((output / "result.json").read_text())["invocations"], 4)


    def test_failed_stage_is_preserved_without_retry_or_later_calls(self):
        (self.root / "manifest").write_text("{}")
        calls, output = self.exercise(fail=True)
        self.assertEqual(len(calls), 1)
        self.assertTrue((output / "stage-1/response.json").exists())
        self.assertFalse((output / "result.json").exists())
        self.assertFalse(json.loads((output / "failure.json").read_text())["retried"])


    def test_audit_rechecks_actual_service_files_and_never_invokes(self):
        _, run_dir = self.exercise()
        client = controller.RetailClient("https://example.services.ai.azure.com/api/projects/demo",
                                         "sub", "agent", agent_version="1")
        for stage_id in controller.STAGES:
            files, binding = controller.inputs(
                self.prepared, stage_id,
                None if stage_id == "stage-1" else run_dir / controller.STAGES[
                    controller.STAGES.index(stage_id) - 1] / "downloads" / (
                        "assessment-" + controller.STAGES[controller.STAGES.index(stage_id) - 1] + ".json"))
            expected = {name: controller.hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
            request = {"input": controller.content(controller.task(binding, controller.hash_command(expected)), files),
                       "conversation": "one-conversation",
                       "agent_reference": {"name": "agent", "version": "1", "type": "agent_reference"}}
            (run_dir / stage_id / "request.json").write_text(json.dumps(request))

        def get_response(identifier):
            stage_id = identifier.removeprefix("resp-")
            return json.loads((run_dir / stage_id / "response.json").read_text())

        def download(response, destination):
            source = run_dir / response["id"].removeprefix("resp-") / "downloads"
            destination.mkdir()
            result = []
            for path in source.iterdir():
                target = destination / path.name
                target.write_bytes(path.read_bytes())
                result.append(target)
            return result

        with patch.object(controller, "selected"), \
             patch.object(controller, "verify_consumption"), \
             patch.object(client, "get_response", side_effect=get_response), \
             patch.object(client, "download_outputs", side_effect=download), \
             patch.object(client, "conversation_items", return_value=[]), \
             patch.object(client, "invoke") as invoke:
            controller.audit(client, self.prepared, self.root / "manifest", run_dir, self.root / "audit")
            invoke.assert_not_called()
            self.assertTrue(json.loads((self.root / "audit/audit.json").read_text())["complete_four_stage_run"])
        self.assertEqual(controller.normalized_output({"logprobs": []}), {})
        self.assertEqual(controller.normalized_output({"logprobs": None}), {})
        self.assertEqual(controller.normalized_output({"logprobs": [1]}), {"logprobs": [1]})




if __name__ == "__main__":
    unittest.main()
