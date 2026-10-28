"""Generate a local sample manifest; never create Azure resources or read secrets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from harness_client import HarnessClient, HarnessError, required_string


ROOT = Path(__file__).resolve().parents[1]


def configure(
    endpoint: str, subscription: str, model: str, connection: str, output: Path,
    *, agent_name: str, skill_version: str,
) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", agent_name):
        raise HarnessError("Agent name must be 1-63 letters, digits, underscores or hyphens, starting with a letter or digit.")
    if not re.fullmatch(r"[1-9][0-9]*", skill_version):
        raise HarnessError("Skill version must be an explicit positive integer.")
    client = HarnessClient(endpoint, subscription, agent_name)
    manifest = json.loads(
        (ROOT / "agents/company-research/azure.yaml").read_text(encoding="utf-8")
    )
    manifest["services"]["ai-project"]["endpoint"] = client.project_endpoint
    agent = manifest["services"]["company-research-agent"]
    agent["name"] = agent_name
    agent["skills"][0]["version"] = skill_version
    agent["model"] = required_string(model, "Model deployment")
    agent["tools"][0]["project_connection_id"] = required_string(connection, "Web IQ connection name")
    if "__" in json.dumps(manifest):
        raise HarnessError("Unresolved template placeholder in the configured definition.")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "azure.yaml"
    with path.open("x", encoding="utf-8") as target:
        json.dump(manifest, target, indent=2)
        target.write("\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--model-deployment", required=True)
    parser.add_argument("--connection-name", required=True)
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--skill-version", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path(".local/company-research-deploy"))
    args = parser.parse_args(argv)
    try:
        print(configure(
            args.project_endpoint, args.subscription, args.model_deployment,
            args.connection_name, args.output_dir,
            agent_name=args.agent_name, skill_version=args.skill_version,
        ))
        return 0
    except (HarnessError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
