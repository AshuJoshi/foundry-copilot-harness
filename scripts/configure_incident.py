"""Render one incident agent using explicit published Skill versions; no Azure writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from harness_client import HarnessClient, HarnessError, required_string
from retail_support import save
from scripts.prepare_incident import ROOT, SKILLS


def configure(endpoint: str, subscription: str, model: str, name: str,
              versions: dict[str, str], output: Path) -> Path:
    client = HarnessClient(endpoint, subscription, name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", name):
        raise HarnessError("Use a distinct agent name with 1-63 safe characters.")
    if set(versions) != set(SKILLS) or any(
        not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]*", version)
        for version in versions.values()
    ):
        raise HarnessError("Pin exactly the three incident Skill names to positive immutable versions.")
    value = json.loads((ROOT / "agents/incident-investigation/azure.yaml").read_text())
    value["services"]["ai-project"]["endpoint"] = client.project_endpoint
    agent = value["services"]["incident-investigation-agent"]
    agent["name"] = name
    agent["model"] = required_string(model, "Model deployment")
    agent["skills"] = [{"name": skill, "version": versions[skill]} for skill in SKILLS]
    if "__" in json.dumps(value):
        raise HarnessError("Unresolved incident manifest placeholder.")
    output.mkdir(parents=True, exist_ok=False)
    path = output / "azure.yaml"
    save(path, value)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["project-endpoint", "subscription", "model-deployment", "agent-name"]:
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        publication = json.loads(args.publication.read_text())
        if publication.get("status") != "verified" or publication.get("project_endpoint") != args.project_endpoint:
            raise HarnessError("Require verified Skill publication in the selected project.")
        print(configure(args.project_endpoint, args.subscription, args.model_deployment,
                        args.agent_name, publication["versions"], args.output_dir))
        return 0
    except (HarnessError, OSError, ValueError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
