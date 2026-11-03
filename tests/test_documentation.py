import ast
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

from scripts.run_company_research import calculate, SKILL_ROOT


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "agents/company-research/README.md"
RETAIL_GUIDE = ROOT / "agents/retail-analysis/README.md"
COMPARISON_GUIDE = ROOT / "agents/model-comparison/README.md"
PREVIEW = GUIDE.parent / "output-preview"


class DocumentationTests(unittest.TestCase):
    def test_source_license_does_not_replace_dataset_attribution(self):
        license_text = (ROOT / "LICENSE").read_text()
        self.assertIn("MIT License", license_text)
        self.assertIn("Contributors to this repository", license_text)
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        self.assertIn("[MIT license](LICENSE)", (ROOT / "README.md").read_text())
        notice = (ROOT / "agents/retail-analysis/DATA-NOTICE.txt").read_text()
        self.assertIn("Daqing Chen", notice)
        self.assertIn("Creative Commons Attribution 4.0", notice)

    def test_preview_calculation_is_exactly_reproducible_from_source(self):
        assumptions = json.loads((SKILL_ROOT / "assumptions.json").read_text())
        actual = json.loads((PREVIEW / "scenarios.json").read_text())
        self.assertEqual(actual, calculate(assumptions))
        report = (PREVIEW / "report.md").read_text()
        for value in ["40.48%", "64.71%", "78.95%", "7,000", "8,500", "9,500"]:
            self.assertIn(value, report)
        self.assertIn("Illustrative", report)

    def test_research_preview_is_explicitly_fictional_with_reserved_urls(self):
        ledger = json.loads((PREVIEW / "evidence.json").read_text())
        self.assertIn("Fictional documentation", ledger["_preview"])
        self.assertIn("No Web IQ retrieval occurred", ledger["_preview"])
        self.assertEqual(ledger["company"], "ExampleCo")
        self.assertEqual(len(ledger["sources"]), 4)
        report = (PREVIEW / "report.md").read_text()
        self.assertIn("Fictional documentation preview. No research was performed.", report)
        self.assertIn("not retrieved evidence", report)
        ids = set()
        for source in ledger["sources"]:
            self.assertTrue({"id", "url", "title", "source_type", "claim", "retrieved_at"} <= source.keys())
            self.assertIn("fictional", source["title"])
            self.assertIn(source["source_type"], {"primary", "independent"})
            self.assertEqual(datetime.fromisoformat(source["retrieved_at"].replace("Z", "+00:00")).year, 2000)
            self.assertIn(urllib.parse.urlsplit(source["url"]).hostname,
                          {"example.com", "example.org", "example.net"})
            self.assertIn(source["id"], report)
            self.assertIn(source["url"], report)
            ids.add(source["id"])
        self.assertEqual(len(ids), 4)

    def test_all_public_documentation_links_resolve(self):
        paths = [ROOT / "README.md", GUIDE, RETAIL_GUIDE, COMPARISON_GUIDE,
                 ROOT / "agents/incident-investigation/README.md",
                 ROOT / "agents/incident-review/README.md",
                 COMPARISON_GUIDE.parent / "RESULTS.md",
                 *PREVIEW.glob("*.md"), SKILL_ROOT / "SKILL.md"]
        for path in paths:
            for link in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                if "://" in link or link.startswith("mailto:"):
                    continue
                relative, _, anchor = link.partition("#")
                target = path.parent / relative if relative else path
                self.assertTrue(target.exists(), (path, link))
                if anchor and target.suffix == ".md":
                    headings = [
                        re.sub(r"[^\w -]", "", h.lower().strip()).replace(" ", "-")
                        for h in re.findall(r"^#+\s+(.+)$", target.read_text(), re.M)
                    ]
                    self.assertIn(anchor, headings, (path, link))

    def test_documented_shell_blocks_are_valid_bash_without_executing_them(self):
        for path in [ROOT / "README.md", GUIDE, RETAIL_GUIDE, COMPARISON_GUIDE, PREVIEW / "README.md",
                     ROOT / "agents/incident-investigation/README.md",
                     ROOT / "agents/incident-review/README.md"]:
            for block in re.findall(r"```bash\n(.*?)```", path.read_text(), re.S):
                result = subprocess.run(
                    ["bash", "-n"], input=block, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, (path, result.stderr))

    def test_retail_documentation_uses_existing_modules_and_ignores_run_outputs(self):
        text = RETAIL_GUIDE.read_text()
        for module in ["scripts.prepare_retail", "scripts.run_retail"]:
            self.assertTrue((ROOT / (module.replace(".", "/") + ".py")).is_file())
            self.assertIn(f"-m {module}", text)
        self.assertIn("--allow-paid-invocations", text)
        self.assertIn("--run-dir .local/retail-run", text)
        self.assertIn("openpyxl==3.1.5", (ROOT / "requirements-retail.txt").read_text())
        self.assertIn("Never upload `evaluator/`", text)

    def test_documented_python_flags_match_real_parsers(self):
        text = GUIDE.read_text()
        for module in ["configure_company_research", "run_company_research"]:
            tree = ast.parse((ROOT / "scripts" / f"{module}.py").read_text())
            flags = {
                arg.value for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            }
            blocks = [block for block in re.findall(r"```bash\n(.*?)```", text, re.S)
                      if f"-m scripts.{module} \\\n" in block]
            self.assertEqual(len(blocks), 1)
            self.assertLessEqual(set(re.findall(r"--[a-z][a-z-]*", blocks[0])), flags)
        invocation = next(block for block in re.findall(r"```bash\n(.*?)```", text, re.S)
                          if "-m scripts.run_company_research \\\n" in block)
        self.assertIn("--allow-paid-invocation", invocation)
        self.assertIn("--agent-version", invocation)

    def test_comparison_and_completion_commands_use_real_parser_flags(self):
        all_blocks = re.findall(r"```bash\n(.*?)```", COMPARISON_GUIDE.read_text(), re.S)
        commands = [("compare_models", name) for name in ["prepare", "deploy", "run", "smoke", "audit", "report"]]
        commands.append(("complete_comparison", ""))
        for module, command in commands:
            invocation = f"-m scripts.{module} " + (command + " " if command else "")
            blocks = [block for block in all_blocks if invocation in block]
            self.assertTrue(blocks)
            arguments = [sys.executable, "-B", "-m", "scripts." + module]
            if command:
                arguments.append(command)
            result = subprocess.run(arguments + ["--help"], cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            flags = set(re.findall(r"--[a-z][a-z-]*", result.stdout))
            for block in blocks:
                self.assertLessEqual(set(re.findall(r"--[a-z][a-z-]*", block)), flags)

    def test_documented_local_configuration_command_runs_in_a_standalone_copy(self):
        block = next(block for block in re.findall(r"```bash\n(.*?)```", GUIDE.read_text(), re.S)
                     if "-m scripts.configure_company_research \\\n" in block)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in [
                "harness_client.py", "scripts/configure_company_research.py",
                "agents/company-research/azure.yaml",
            ]:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                       FOUNDRY_PROJECT_ENDPOINT="https://example.services.ai.azure.com/api/projects/demo",
                       AZURE_SUBSCRIPTION_ID="example-subscription",
                       MODEL_DEPLOYMENT="example-deployment", WEBIQ_CONNECTION_NAME="example-webiq",
                       AGENT_NAME="example-research", SKILL_VERSION="3")
            env.pop("PYTHONPATH", None)
            result = subprocess.run(
                ["bash", "-eu", "-c", block], cwd=root, env=env,
                capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((root / ".local/company-research-deploy/azure.yaml").read_text())
            agent = manifest["services"]["company-research-agent"]
            self.assertEqual(agent["name"], "example-research")
            self.assertEqual(agent["model"], "example-deployment")
            self.assertEqual(agent["skills"], [{"name": "company-research", "version": "3"}])
            self.assertNotIn("__", json.dumps(manifest))
            second = subprocess.run(
                ["bash", "-eu", "-c", block], cwd=root, env=env,
                capture_output=True, text=True, timeout=20)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(json.loads((root / ".local/company-research-deploy/azure.yaml").read_text()), manifest)

    def test_run_storage_remains_ignored_and_separate_from_previews(self):
        rules = (ROOT / ".gitignore").read_text().splitlines()
        self.assertIn(".local/", rules)
        self.assertFalse(any(rule.startswith("!.local") for rule in rules))
        script = ast.parse((ROOT / "scripts/run_company_research.py").read_text())
        constants = {node.value for node in ast.walk(script)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertIn(".local/company-research", constants)
        self.assertEqual({p.name for p in PREVIEW.iterdir()},
                         {"README.md", "report.md", "evidence.json", "scenarios.json"})
        for path in PREVIEW.iterdir():
            text = path.read_text()
            self.assertNotRegex(text, r"(?:caresp_|conv_|/subscriptions/|Bearer\s+[A-Za-z0-9])")
        self.assertIn("documentation previews", GUIDE.read_text())


if __name__ == "__main__":
    unittest.main()
