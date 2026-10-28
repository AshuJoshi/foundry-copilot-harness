"""Recording and input-integrity helpers shared by retail execution and audit."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import urllib.parse

from harness_client import HarnessClient, HarnessError, JsonObject

MARKER = "HANDOFF_VERIFIED="


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as target:
        json.dump(value, target, indent=2)
        target.write("\n")


class RecordingClient(HarnessClient):
    evidence_dir: Path | None = None

    def _request(
        self, path: str, body: JsonObject | None = None, *,
        timeout: float | None = None, binary: bool = False,
        query: dict[str, str] | None = None,
    ) -> bytes:
        if body is not None and path == self._protocol_path + "/responses":
            if self.evidence_dir is None:
                raise HarnessError("No evidence directory before response submission.")
            save(self.evidence_dir / "request.json", body)
        return super()._request(path, body, timeout=timeout, binary=binary, query=query)


def summarize(response: JsonObject) -> JsonObject:
    output = response.get("output", [])
    functions = [
        item.get("name") for item in output if item.get("type") == "function_call"
    ]
    shell = [item for item in output if item.get("type") == "shell_call"]
    return {
        "response_id": response.get("id"),
        "service_status": response.get("status"),
        "agent_reference": response.get("agent_reference"),
        "service_seconds": response["completed_at"] - response["created_at"]
        if isinstance(response.get("completed_at"), (int, float))
        and isinstance(response.get("created_at"), (int, float)) else None,
        "usage": response.get("usage"),
        "parent_function_calls": functions,
        "parent_shell_calls": len(shell),
        "parent_mcp_calls": sum(i.get("type") == "mcp_call" for i in output),
        "parent_tool_activity_observed": bool(functions or shell or any(
            i.get("type") == "mcp_call" for i in output
        )),
        "correctness": "not_assessed",
        "scope": "Visible parent response only; nested activity may not be exposed.",
    }


def hash_command(expected: dict[str, str]) -> str:
    return (
        "python3 - <<'VERIFY_HANDOFF'\n"
        "import hashlib,json\n"
        "from pathlib import Path\n"
        f"expected={expected!r}\n"
        "roots=[Path('/home/session/workspace'),Path('/mnt/data'),Path.cwd()]\n"
        "verified={}\n"
        "for name,wanted in expected.items():\n"
        "    found=set()\n"
        "    for root in roots:\n"
        "        if root.exists():\n"
        "            found.update(p.resolve() for p in root.rglob(name) if p.is_file())\n"
        "    matching=[p for p in found if hashlib.sha256(p.read_bytes()).hexdigest()==wanted]\n"
        "    if not matching:\n"
        "        raise RuntimeError('Attachment missing or changed: '+name)\n"
        "    verified[name]=hashlib.sha256(matching[0].read_bytes()).hexdigest()\n"
        f"print({MARKER!r}+json.dumps(verified,sort_keys=True))\n"
        "VERIFY_HANDOFF"
    )


def verify_consumption(response: dict, command: str, expected: dict, *, allow_shell_wrappers: bool = False) -> None:
    calls = {item["call_id"]: item for item in response["output"] if item["type"] == "shell_call"}
    for item in response["output"]:
        call = calls.get(item.get("call_id"))
        if item["type"] != "shell_call_output" or call is None:
            continue
        for actual, result in zip(call.get("action", {}).get("commands", []), item.get("output", [])):
            matches = actual.strip() == command.strip() or (
                allow_shell_wrappers and command.strip() in actual)
            if not matches or result.get("outcome", {}).get("exit_code") != 0:
                continue
            for line in result.get("stdout", "").splitlines():
                if line.startswith(MARKER) and json.loads(line[len(MARKER):]) == expected:
                    return
    raise HarnessError("No successful receiver-side attachment hash script and matching hashes found.")


def definition_for_rest(definition: JsonObject) -> JsonObject:
    value = copy.deepcopy(definition)
    for tool in value.get("tools", []):
        if tool.get("type") in {"microsoft_copilot_toolset_preview", "github_copilot_toolset_preview"}:
            if "defaultConfig" in tool:
                if "default_config" in tool:
                    raise HarnessError("Copilot toolset declares both defaultConfig and default_config.")
                tool["default_config"] = tool.pop("defaultConfig")
    return value


def selected(client: HarnessClient, manifest: Path) -> dict:
    config = json.loads(manifest.read_text())
    agents = [s for s in config["services"].values() if s.get("host") == "azure.ai.agent"]
    if len(agents) != 1 or agents[0]["name"] != client.agent_name:
        raise HarnessError("Manifest agent mismatch.")
    agent = agents[0]
    if config["services"][agent["uses"][0]]["endpoint"] != client.project_endpoint:
        raise HarnessError("Manifest project mismatch.")
    version = client._verified_version()
    result = client.get_agent()["versions"]["latest"]
    if result["version"] != version:
        result = json.loads(client._request(client._agent_path + "/versions/" +
                                           urllib.parse.quote(version, safe="")))
    expected = definition_for_rest({
        k: agent[k] for k in ["kind", "model", "harness", "instructions", "skills", "tools"] if k in agent})
    actual = dict(result["definition"])
    for definition in [expected, actual]:
        if definition.get("tools") == []:
            definition.pop("tools")
    if actual != expected:
        raise HarnessError("Pinned role definition differs from source snapshot manifest.")
    return result
