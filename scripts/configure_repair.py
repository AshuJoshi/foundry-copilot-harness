"""Render a repair agent with explicit model and published Skill pins; no Azure writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from harness_client import HarnessClient, HarnessError, required_string
from retail_support import save
from scripts.prepare_software_repair import ROOT, SKILLS


def configure(endpoint: str, subscription: str, model: str, name: str,
              versions: dict[str, str], output: Path) -> Path:
    client = HarnessClient(endpoint, subscription, name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", name):
        raise HarnessError("Use a distinct agent name with 1-63 safe characters.")
    if set(versions) != set(SKILLS) or any(
        not isinstance(v, str) or not re.fullmatch(r"[1-9][0-9]*", v) for v in versions.values()
    ):
        raise HarnessError("Pin exactly the two repair Skills to positive immutable versions.")
    manifest = json.loads((ROOT / "agents/software-repair/azure.yaml").read_text())
    manifest["services"]["ai-project"]["endpoint"] = client.project_endpoint
    agent = manifest["services"]["software-repair-agent"]
    agent.update(name=name, model=required_string(model, "Model deployment"))
    agent["skills"] = [{"name": skill, "version": versions[skill]} for skill in SKILLS]
    if "__" in json.dumps(manifest):
        raise HarnessError("Unresolved repair manifest placeholder.")
    output.mkdir(parents=True, exist_ok=False)
    path = output / "azure.yaml"
    save(path, manifest)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ["project-endpoint", "subscription", "model-deployment", "agent-name"]:
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        publication = json.loads(args.publication.read_text())
        if (publication.get("status") != "verified" or publication.get("scenario") != "repair"
                or publication.get("project_endpoint") != args.project_endpoint):
            raise HarnessError("Require verified repair publication in the selected project.")
        print(configure(args.project_endpoint, args.subscription, args.model_deployment,
                        args.agent_name, publication["versions"], args.output_dir))
        return 0
    except (HarnessError, OSError, ValueError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
