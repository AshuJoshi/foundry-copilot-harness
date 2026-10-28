import copy
import json
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.configure_company_research import configure
from scripts.run_company_research import (
    build_prompt, mcp_payloads, public_domain, source_urls,
    validate_artifacts, validate_execution, calculate, SKILL_ROOT, main,
)
from harness_client import HarnessClient, HarnessError, ResponseError

ROOT = Path(__file__).resolve().parents[1]

class DiligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assumptions = json.loads((SKILL_ROOT / "assumptions.json").read_text())

    def test_exact_illustrative_thresholds(self) -> None:
        result = calculate(self.assumptions)
        values = {s["name"]: s for s in result["scenarios"]}
        self.assertEqual(values["base"]["revenue"], 510)
        self.assertEqual(values["base"]["variable_cost"], 180)
        self.assertEqual(values["base"]["gross_margin_percent"], 64.71)
        self.assertEqual(values["base"]["contribution_after_fixed"], 130)
        self.assertEqual(values["base"]["break_even_success_rate"], 0.6333)
        self.assertEqual(values["downside"]["contribution_after_fixed"], -30)
        self.assertEqual(values["upside"]["contribution_after_fixed"], 250)

    def test_invalid_inputs_do_not_become_successful_models(self) -> None:
        for key, value in (
            ("success_rate", 1.1), ("monthly_attempts", 0),
            ("monthly_attempts", True), ("monthly_attempts", 1.5),
            ("price_per_success", 0), ("cost_per_attempt", -1),
            ("success_rate", float("nan")),
        ):
            assumptions = copy.deepcopy(self.assumptions)
            assumptions["scenarios"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                calculate(assumptions)

    def test_zero_success_has_no_fake_margin(self) -> None:
        self.assumptions["scenarios"][0]["success_rate"] = 0
        self.assertIsNone(calculate(self.assumptions)["scenarios"][0]["gross_margin_percent"])

    def test_company_is_runtime_data(self) -> None:
        one = build_prompt("TinyFish", "https://www.tinyfish.ai/", "Technical moat?")
        two = build_prompt("Other startup", "example.com", "Distribution?")
        self.assertIn('"company": "TinyFish"', one)
        self.assertIn('"company": "Other startup"', two)
        self.assertNotIn("TinyFish", two)
        self.assertEqual(public_domain("https://www.tinyfish.ai/"), "www.tinyfish.ai")

    def test_private_or_malformed_domains_rejected(self) -> None:
        for domain in ("localhost", "127.0.0.1", "https://user@example.com", "http://example.com", "a.internal", "example.com/private"):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                public_domain(domain)

    def test_plausible_answer_is_not_tool_proof(self) -> None:
        with self.assertRaisesRegex(HarnessError, "Skill load"):
            validate_execution({"output": [{"type": "message", "text": "I searched Web IQ."}]})

    def test_actual_namespaced_mcp_json_text_contract(self) -> None:
        payload = {"webResults": [{"url": "https://example.com", "title": "Source"}]}
        self.assertEqual(mcp_payloads(json.dumps([{"text": json.dumps(payload)}])), [payload])
        self.assertEqual(mcp_payloads({"structuredContent": payload}), [payload])
        with self.assertRaisesRegex(HarnessError, "error-shaped"):
            mcp_payloads({"isError": True, "content": []})

    def test_malformed_nested_webiq_json_is_not_silently_repaired(self):
        payload = {"webResults": [{
            "url": "https://example.com", "content": 'Example: {"Authorization": "PLACEHOLDER"}',
        }]}
        text = json.dumps(payload)
        self.assertEqual(mcp_payloads(json.dumps([{"text": text}])), [payload])
        broken = text.replace('\\"PLACEHOLDER\\"', '\\"MASKED"')
        outer = json.dumps([{"text": broken}])
        self.assertEqual(json.loads(outer)[0]["text"], broken)
        with self.assertRaisesRegex(HarnessError, "structured JSON"):
            mcp_payloads(outer)

    def test_namespaced_calls_and_standalone_calculator_proof(self) -> None:
        computed = calculate(self.assumptions)
        response = {"output": [
            {"type": "function_call", "name": "skill", "call_id": "s",
             "arguments": '{"skill":"company-research"}'},
            {"type": "function_call_output", "call_id": "s", "status": "completed",
             "output": "Skill loaded successfully"},
            {"type": "mcp_call", "name": "webiq___web", "server_label": "managed_tools",
             "status": "completed", "output": json.dumps([{"text": '{"webResults":[{"url":"https://example.com"}]}'}])},
            {"type": "shell_call", "call_id": "p",
             "action": {"commands": ["python3 /skill/calculate_scenarios.py"]}},
            {"type": "shell_call_output", "call_id": "p",
             "output": [{"outcome": {"exit_code": 0}, "stdout": json.dumps(computed) + "\n<shellId: 0>"}]},
        ]}
        self.assertIn("https://example.com", validate_execution(response))
        response["output"][3]["action"]["commands"] = [
            "python3 /skill/calculate_scenarios.py || echo fake"
        ]
        with self.assertRaisesRegex(HarnessError, "standalone"):
            validate_execution(response)

    def test_source_urls_are_fields_not_substrings_of_page_text(self) -> None:
        self.assertEqual(
            source_urls({"webResults": [{"url": "https://example.com", "content": "https://invented.com"}]}),
            {"https://example.com"},
        )

    def test_initializer_writes_concrete_values_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            path = configure(
                "https://example.services.ai.azure.com/api/projects/demo",
                "sub", "existing-model", "approved-connection", target,
                agent_name="my-research-agent", skill_version="3",
            )
            data = json.loads(path.read_text())
            agent = data["services"]["company-research-agent"]
            self.assertEqual(agent["name"], "my-research-agent")
            self.assertEqual(agent["skills"], [{"name": "company-research", "version": "3"}])
            self.assertEqual(agent["model"], "existing-model")
            self.assertEqual(agent["tools"][0]["project_connection_id"], "approved-connection")
            self.assertNotIn("__", path.read_text())
            with self.assertRaises(FileExistsError):
                configure("https://example.services.ai.azure.com/api/projects/demo", "sub", "model", "conn", target,
                          agent_name="my-research-agent", skill_version="3")

    def test_artifact_source_provenance_and_calculation_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [
                {"id": f"S{i}", "url": f"https://example.com/{i}", "title": f"Source {i}",
                 "source_type": "primary" if i == 0 else "independent",
                 "claim": "Public claim", "retrieved_at": "2026-10-14T00:00:00Z"}
                for i in range(4)
            ]
            (root / "evidence.json").write_text(json.dumps({"company": "Example", "sources": sources}))
            (root / "scenarios.json").write_text(json.dumps(calculate(self.assumptions)))
            (root / "report.md").write_text(
                "Example example.com. Illustrative inputs, not company actuals.\n"
                + "\n".join(f"{s['id']} {s['url']}" for s in sources)
            )
            paths = [root / name for name in ("report.md", "evidence.json", "scenarios.json")]
            urls = {s["url"] for s in sources}
            validate_artifacts(paths, company="Example", domain="example.com", mcp_evidence=urls)
            with self.assertRaisesRegex(HarnessError, "retrieval evidence"):
                validate_artifacts(paths, company="Example", domain="example.com", mcp_evidence=set())
            (root / "scenarios.json").write_text('{"fake":"model-generated numbers"}')
            with self.assertRaisesRegex(HarnessError, "independently calculated"):
                validate_artifacts(paths, company="Example", domain="example.com", mcp_evidence=urls)

    def test_configuration_rejects_bad_names_versions_and_unresolved_values_without_azure(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(HarnessClient, "_request", side_effect=AssertionError("No Azure calls")):
            root = Path(temporary)
            for name, version, model in [
                ("../escape", "1", "model"), ("agent", "latest", "model"),
                ("agent", "0", "model"), ("agent", "1", "__MODEL__"),
            ]:
                target = root / "unused"
                with self.subTest(name=name, version=version, model=model), self.assertRaises(HarnessError):
                    configure("https://example.services.ai.azure.com/api/projects/demo", "sub",
                              model, "connection", target, agent_name=name, skill_version=version)
                self.assertFalse(target.exists())
            output = configure("https://example.services.ai.azure.com/api/projects/demo", "sub",
                               "model", "connection", root / "valid", agent_name="research", skill_version="2")
            value = json.loads(output.read_text())
            self.assertEqual(value["services"]["company-research-agent"]["harness"],
                             {"type": "microsoft_copilot_preview"})
            self.assertEqual(value["services"]["company-research-agent"]["tools"][0]["allowed_tools"],
                             ["web", "news", "browse"])

    def arguments(self, directory):
        return [
            "--company", "Example", "--domain", "https://example.com/",
            "--project-endpoint", "https://example.services.ai.azure.com/api/projects/demo",
            "--subscription", "sub", "--agent-name", "research", "--agent-version", "2",
            "--output-dir", str(directory),
        ]

    def execution_response(self):
        return {
            "id": "response-test", "status": "completed",
            "agent_reference": {"name": "research", "version": "2", "type": "agent_reference"},
            "output": [
                {"type": "function_call", "name": "skill", "call_id": "s",
                 "arguments": '{"skill":"company-research"}'},
                {"type": "function_call_output", "call_id": "s", "status": "completed",
                 "output": "Skill loaded successfully"},
                {"type": "mcp_call", "name": "web", "server_label": "webiq", "status": "completed",
                 "output": json.dumps({"webResults": [{"url": f"https://example.com/{i}"} for i in range(4)]})},
                {"type": "shell_call", "call_id": "p",
                 "action": {"commands": ["python3 /skill/calculate_scenarios.py"]}},
                {"type": "shell_call_output", "call_id": "p",
                 "output": [{"outcome": {"exit_code": 0},
                             "stdout": json.dumps(calculate(self.assumptions))}]},
                {"type": "message", "role": "assistant", "phase": "final_answer",
                 "content": [{"type": "output_text", "text": "Research complete."}]},
            ],
        }

    def write_outputs(self, response, directory):
        directory.mkdir()
        sources = [{
            "id": f"S{i}", "url": f"https://example.com/{i}", "title": f"Source {i}",
            "source_type": "primary" if i == 0 else "independent",
            "claim": "Public statement", "retrieved_at": "2026-10-26T00:00:00Z",
        } for i in range(4)]
        (directory / "report.md").write_text(
            "Example example.com. Illustrative assumptions, not company actuals.\n"
            + "\n".join(f"{s['id']} {s['url']}" for s in sources))
        (directory / "evidence.json").write_text(json.dumps({"company": "Example", "sources": sources}))
        (directory / "scenarios.json").write_text(json.dumps(calculate(self.assumptions)))
        return [directory / name for name in ["report.md", "evidence.json", "scenarios.json"]]

    def test_runner_requires_paid_opt_in_and_explicit_numeric_version_before_azure(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("scripts.run_company_research.HarnessClient") as client, \
             patch("sys.stderr", new_callable=io.StringIO):
            root = Path(temporary) / "not-created"
            args = self.arguments(root)
            self.assertEqual(main(args), 1)
            bad = [v if v != "2" else "latest" for v in args]
            self.assertEqual(main(bad + ["--allow-paid-invocation"]), 1)
            client.assert_not_called()
            self.assertFalse(root.exists())

    def test_runner_mocked_end_to_end_preserves_request_and_verifies_actual_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("scripts.run_company_research.HarnessClient") as factory, \
             patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            root = Path(temporary)
            client = factory.return_value
            client.create_conversation.return_value = "conversation-test"

            def invoke(prompt, *, conversation_id):
                request = next(root.glob("*/request.json"))
                self.assertEqual(json.loads(request.read_text())["conversation"], conversation_id)
                self.assertEqual(json.loads(request.read_text())["input"], prompt)
                return self.execution_response()

            client.invoke.side_effect = invoke
            client.download_outputs.side_effect = self.write_outputs
            self.assertEqual(main(self.arguments(root) + ["--allow-paid-invocation"]), 0)
            record = json.loads(next(root.glob("*/verification.json")).read_text())
            self.assertEqual(record["status"], "passed")
            client.invoke.assert_called_once()
            client.download_outputs.assert_called_once()
            self.assertFalse(list(root.glob("*/failure.json")))

    def test_rate_limited_browse_is_reported_and_not_accepted_as_source_evidence(self):
        response = self.execution_response()
        response["output"].insert(3, {
            "type": "mcp_call", "name": "webiq___browse", "server_label": "managed_tools",
            "status": "completed", "output": json.dumps({
                "url": "https://rate-limited.example/", "retryAfter": "2026-10-27T00:00:00Z",
            }),
        })
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            urls = validate_execution(response)
        self.assertNotIn("https://rate-limited.example/", urls)
        self.assertEqual(len(urls), 4)
        self.assertIn("excluded from retrieval evidence", stderr.getvalue())

    def test_rate_limited_web_search_does_not_count_as_successful_research(self):
        response = self.execution_response()
        response["output"][2]["output"] = json.dumps({
            "url": "https://example.com/", "retryAfter": "2026-10-27T00:00:00Z",
        })
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaisesRegex(
                HarnessError, "No successful actual Web IQ web-search results"):
            validate_execution(response)

    def test_runner_preserves_response_and_failure_without_retry(self):
        for failure in ["response", "transport", "malformed-mcp", "wrong-artifact"]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary, \
                 patch("scripts.run_company_research.HarnessClient") as factory, \
                 patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                root = Path(temporary)
                client = factory.return_value
                client.create_conversation.return_value = "conversation-test"
                response = self.execution_response()
                if failure == "response":
                    response["status"] = "failed"
                    client.invoke.side_effect = ResponseError("Service response failed", response)
                elif failure == "transport":
                    client.invoke.side_effect = HarnessError("Request may have been accepted")
                elif failure == "malformed-mcp":
                    response["output"][2]["output"] = '[{"text":"broken JSON"}]'
                    client.invoke.return_value = response
                else:
                    client.invoke.return_value = response

                    def wrong_output(response, directory):
                        paths = self.write_outputs(response, directory)
                        (directory / "scenarios.json").write_text('{"wrong":1}')
                        return paths

                    client.download_outputs.side_effect = wrong_output
                self.assertEqual(main(self.arguments(root) + ["--allow-paid-invocation"]), 1)
                client.invoke.assert_called_once()
                self.assertEqual(json.loads(next(root.glob("*/failure.json")).read_text())["client_retries"], 0)
                self.assertTrue(list(root.glob("*/request.json")))
                self.assertFalse(list(root.glob("*/verification.json")))
                if failure != "transport":
                    self.assertTrue(list(root.glob("*/response.json")))
                if failure != "wrong-artifact":
                    client.download_outputs.assert_not_called()

    def test_validation_uses_repository_paths_not_current_working_directory(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            try:
                os.chdir(temporary)
                urls = validate_execution(self.execution_response())
                paths = self.write_outputs({}, Path(temporary) / "outputs")
                validate_artifacts(paths, company="Example", domain="example.com", mcp_evidence=urls)
            finally:
                os.chdir(original)

    def test_cli_help_works_for_documented_module_commands(self):
        for module in ["scripts.configure_company_research", "scripts.run_company_research"]:
            result = subprocess.run(
                [sys.executable, "-B", "-m", module, "--help"], cwd=ROOT, capture_output=True,
                text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--agent-name", result.stdout)
        self.assertIn("--allow-paid-invocation", result.stdout)

    def test_documentation_relative_links_resolve(self):
        path = ROOT / "agents/company-research/README.md"
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if "://" not in link:
                self.assertTrue((path.parent / link.split("#")[0]).exists(), link)


if __name__ == "__main__":
    unittest.main()
