"""Reusable Responses/Files client for Foundry-managed Copilot harness agents."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

JsonObject = dict[str, Any]
API_VERSION = "v1"
COPILOT_HARNESSES = {"microsoft_copilot_preview", "github_copilot_preview"}
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
UPLOAD_TOOL = "upload_file_from_workspace_to_foundry_project"


class HarnessError(RuntimeError):
    """An explicit authentication, transport, contract or validation failure."""


class ResponseError(HarnessError):
    def __init__(self, message: str, response: JsonObject) -> None:
        super().__init__(message)
        self.response = response


def json_object(value: object, context: str) -> JsonObject:
    if not isinstance(value, dict):
        raise HarnessError(f"{context}: expected a JSON object.")
    return value


def required_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(f"{context}: expected a nonempty string.")
    return value


def parse_response(raw: str) -> JsonObject:
    """Accept JSON or terminal SSE; progress alone never establishes completion."""
    try:
        if raw.lstrip().startswith("{"):
            return json_object(json.loads(raw), "Response")
        terminal: JsonObject | None = None
        for block in re.split(r"\r?\n\r?\n", raw):
            data = "\n".join(
                line[5:].lstrip() for line in block.splitlines()
                if line.startswith("data:")
            )
            if not data or data == "[DONE]":
                continue
            event = json_object(json.loads(data), "SSE event")
            if event.get("type") in {
                "response.completed", "response.failed", "response.incomplete",
            }:
                terminal = json_object(event.get("response"), "Terminal SSE response")
        if terminal is None:
            raise HarnessError("No terminal SSE response. Inspect conversation state before retrying.")
        return terminal
    except json.JSONDecodeError as error:
        raise HarnessError(f"Invalid response JSON: {error.msg}.") from error


def final_text(response: JsonObject) -> str:
    if response.get("status") != "completed":
        raise ResponseError(
            f"Response {response.get('id')} ended with status "
            f"{response.get('status')}: {response.get('error') or response.get('incomplete_details')}",
            response,
        )
    messages = [
        item for item in response.get("output", [])
        if isinstance(item, dict) and item.get("type") == "message"
        and item.get("role") == "assistant" and item.get("phase") != "commentary"
    ]
    finals = [item for item in messages if item.get("phase") == "final_answer"]
    if not messages:
        raise ResponseError("Completed response has no final assistant message.", response)
    selected = (finals or messages)[-1]
    text = "\n".join(
        part["text"] for part in selected.get("content", [])
        if isinstance(part, dict) and part.get("type") == "output_text"
        and isinstance(part.get("text"), str)
    )
    if not text.strip():
        raise ResponseError("Completed response has no final answer text.", response)
    return text


@dataclass(frozen=True)
class FileReference:
    file_id: str
    filename: str | None = None


def file_references(response: JsonObject) -> list[FileReference]:
    """Extract annotations and correlated upload outputs, never prose links."""
    output = response.get("output", [])
    uploads: dict[str, str] = {}
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call" and item.get("name") == UPLOAD_TOOL:
            call_id = required_string(item.get("call_id"), "Upload call ID")
            if call_id in uploads:
                raise HarnessError("Duplicate upload call ID; file provenance is ambiguous.")
            uploads[call_id] = item["name"]

    refs: dict[str, FileReference] = {}

    def add(value: JsonObject) -> None:
        file_id = required_string(value.get("file_id"), "File reference ID")
        filename = value.get("filename")
        if filename is not None:
            filename = required_string(filename, "File reference filename")
        previous = refs.get(file_id)
        if previous and previous.filename and filename and previous.filename != filename:
            raise HarnessError(f"Conflicting filenames for file {file_id}.")
        refs[file_id] = FileReference(file_id, filename or (previous.filename if previous else None))

    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content", []):
                if not isinstance(part, dict):
                    continue
                for annotation in part.get("annotations", []):
                    if not isinstance(annotation, dict):
                        continue
                    if annotation.get("type") == "file_path":
                        add(json_object(annotation.get("file_path", annotation), "File annotation"))
        if item.get("type") == "function_call_output" and item.get("call_id") in uploads:
            if item.get("status") != "completed":
                raise HarnessError("Upload tool output did not complete.")
            value = item.get("output")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as error:
                    raise HarnessError("Upload output is not valid JSON.") from error
            upload = json_object(value, "Upload output")
            if upload.get("error") or not upload.get("file_id"):
                raise HarnessError(f"Upload failed: {upload}")
            add(upload)
    return list(refs.values())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str,
        headers: Any, newurl: str,
    ) -> None:
        # Never forward a project bearer token to a redirected destination.
        return None


class HarnessClient:
    def __init__(
        self, project_endpoint: str, subscription_id: str, agent_name: str,
        *, agent_version: str | None = None, timeout: float = 600,
        token_provider: Callable[[], str] | None = None,
        http_observer: Callable[[JsonObject], None] | None = None,
    ) -> None:
        endpoint = project_endpoint.rstrip("/")
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not re.fullmatch(r"[a-zA-Z0-9-]+\.services\.ai\.azure\.com", parsed.hostname or "")
            or not re.fullmatch(r"/api/projects/[^/]+", parsed.path)
            or parsed.query or parsed.fragment or parsed.username or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise HarnessError("Expected a public-cloud HTTPS Foundry project endpoint.")
        if timeout <= 0:
            raise HarnessError("Timeout must be positive.")
        self.project_endpoint = endpoint
        self.subscription_id = required_string(subscription_id, "Subscription ID")
        self.agent_name = required_string(agent_name, "Agent name")
        self.agent_version = agent_version
        self.timeout = timeout
        self.http_observer = http_observer
        self._token_provider = token_provider or self._cli_token
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _cli_token(self) -> str:
        try:
            result = subprocess.run(
                ["az", "account", "get-access-token", "--subscription", self.subscription_id,
                 "--resource", "https://ai.azure.com", "--query", "accessToken",
                 "--output", "tsv", "--only-show-errors"],
                capture_output=True, text=True, check=True, timeout=60,
            )
        except FileNotFoundError as error:
            raise HarnessError("Azure CLI is missing. Install az and sign in first.") from error
        except subprocess.CalledProcessError as error:
            raise HarnessError(f"Azure CLI authentication failed: {error.stderr.strip()}") from error
        except subprocess.TimeoutExpired as error:
            raise HarnessError("Azure CLI token acquisition timed out.") from error
        return required_string(result.stdout.strip(), "Azure access token")

    def _request(
        self, path: str, body: JsonObject | None = None, *, timeout: float | None = None,
        binary: bool = False, query: dict[str, str] | None = None,
    ) -> bytes:
        started = time.monotonic()
        token = required_string(self._token_provider(), "Access token")
        token_seconds = time.monotonic() - started
        parameters = dict(query or {})
        parameters["api-version"] = API_VERSION
        url = self.project_endpoint + path + "?" + urllib.parse.urlencode(parameters)
        req = urllib.request.Request(
            url, data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                     "Foundry-Features": "MicrosoftCopilot=V1Preview"},
        )
        def observe(event: str, *, status: int | None = None, headers=None, size: int | None = None) -> None:
            if self.http_observer is None:
                return
            safe_names = (
                "x-ms-request-id", "apim-request-id", "x-request-id", "x-ms-client-request-id",
                "retry-after", "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
                "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
            )
            self.http_observer({
                "event": event, "utc": datetime.now(timezone.utc).isoformat(),
                "method": req.get_method(), "path": path, "status": status,
                "elapsed_seconds": time.monotonic() - started, "token_seconds": token_seconds,
                "request_bytes": len(req.data) if req.data else 0, "response_bytes": size,
                "headers": {name: headers.get(name) for name in safe_names
                            if headers is not None and headers.get(name) is not None},
            })
        observe("request_start")
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as result:
                if self.http_observer is not None:
                    observe("response_headers", status=result.status, headers=result.headers)
                data = result.read(MAX_DOWNLOAD_BYTES + 1) if binary else result.read()
                if binary and len(data) > MAX_DOWNLOAD_BYTES:
                    raise HarnessError("Download exceeds the client's 50 MiB limit.")
                if self.http_observer is not None:
                    observe("response_complete", status=result.status, headers=result.headers, size=len(data))
                return data
        except urllib.error.HTTPError as error:
            observe("http_error", status=error.code, headers=error.headers)
            detail = error.read(4096).decode("utf-8", errors="replace").replace(token, "[REDACTED]")
            correlation = error.headers.get("x-ms-request-id") or error.headers.get("apim-request-id")
            raise HarnessError(
                f"Foundry HTTP {error.code} for {path}; request ID={correlation}: {detail}. "
                "Do not blindly retry a POST; inspect conversation/response state."
            ) from None
        except (urllib.error.URLError, TimeoutError, HTTPException) as error:
            observe("transport_error")
            raise HarnessError(
                f"Foundry transport failed for {path}: {str(error).replace(token, '[REDACTED]')}. "
                "The request may have been accepted; inspect state before retrying."
            ) from None

    @property
    def _agent_path(self) -> str:
        return "/agents/" + urllib.parse.quote(self.agent_name, safe="")

    @property
    def _protocol_path(self) -> str:
        return self._agent_path + "/endpoint/protocols/openai"

    def get_agent(self) -> JsonObject:
        return json_object(json.loads(self._request(self._agent_path)), "Agent")

    def _verified_version(self) -> str:
        agent = self.get_agent()
        latest = json_object(json_object(agent.get("versions"), "Agent versions").get("latest"), "Latest version")
        version = self.agent_version or required_string(latest.get("version"), "Agent version")
        selected = latest
        if version != latest.get("version"):
            selected = json_object(
                json.loads(self._request(self._agent_path + "/versions/" + urllib.parse.quote(version, safe=""))),
                "Selected version",
            )
        definition = json_object(selected.get("definition"), "Agent definition")
        harness = json_object(definition.get("harness"), "Agent harness")
        if definition.get("kind") != "prompt" or harness.get("type") not in COPILOT_HARNESSES:
            raise HarnessError("Selected agent is not a Copilot-harness Prompt Agent.")
        if selected.get("status") != "active":
            raise HarnessError(f"Selected agent version is not active: {selected.get('status')}.")
        return version

    def create_conversation(self) -> str:
        value = json_object(
            json.loads(self._request(self._protocol_path + "/conversations", {})), "Conversation",
        )
        return required_string(value.get("id"), "Conversation ID")

    def get_response(self, response_id: str) -> JsonObject:
        return parse_response(self._request(
            self._protocol_path + "/responses/" + urllib.parse.quote(response_id, safe=""),
        ).decode("utf-8"))

    def conversation_items(self, conversation_id: str) -> list[JsonObject]:
        path = self._protocol_path + "/conversations/" + urllib.parse.quote(
            required_string(conversation_id, "Conversation ID"), safe="",
        ) + "/items"
        query = {"order": "asc", "limit": "100"}
        items: list[JsonObject] = []
        cursors: set[str] = set()
        for _ in range(100):
            page = json_object(json.loads(self._request(path, query=query)), "Conversation items")
            data = page.get("data")
            if not isinstance(data, list):
                raise HarnessError("Conversation page has no item list.")
            items.extend(json_object(item, "Conversation item") for item in data)
            if page.get("has_more") is False:
                return items
            if page.get("has_more") is not True:
                raise HarnessError("Conversation page has no boolean has_more.")
            cursor = required_string(page.get("last_id"), "Conversation cursor")
            if cursor in cursors or not data:
                raise HarnessError("Conversation pagination did not advance.")
            cursors.add(cursor)
            query["after"] = cursor
        raise HarnessError("Conversation exceeded the client's 100-page inspection limit.")

    def invoke(self, prompt: str, *, conversation_id: str | None = None) -> JsonObject:
        prompt = required_string(prompt, "Prompt")
        version = self._verified_version()
        conversation = conversation_id or self.create_conversation()
        # Print the conversation before POST so an interrupted CLI run can be inspected.
        print(f"Conversation: {conversation}", file=sys.stderr, flush=True)
        body = {
            "conversation": conversation,
            "agent_reference": {"name": self.agent_name, "version": version, "type": "agent_reference"},
            "input": prompt,
        }
        deadline = time.monotonic() + self.timeout
        response = parse_response(self._request(
            self._protocol_path + "/responses", body, timeout=self.timeout,
        ).decode("utf-8"))
        while response.get("status") in ("queued", "in_progress"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResponseError(
                    f"Response {response.get('id')} still running in conversation {conversation}; "
                    "inspect it before retrying.", response,
                )
            time.sleep(min(2, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            response_id = required_string(response.get("id"), "Response ID")
            response = parse_response(self._request(
                self._protocol_path + "/responses/" + urllib.parse.quote(response_id, safe=""),
                timeout=remaining,
            ).decode("utf-8"))
        final_text(response)
        reference = json_object(response.get("agent_reference"), "Response agent reference")
        if reference.get("name") != self.agent_name or reference.get("version") != version:
            raise ResponseError("Response agent/version did not match the requested agent/version.", response)
        return response

    def download_file(self, file_id: str, destination: Path, *, expected: bytes | None = None) -> Path:
        file_id = required_string(file_id, "File ID")
        data = self._request("/files/" + urllib.parse.quote(file_id, safe="") + "/content", binary=True)
        if expected is not None and data != expected:
            raise HarnessError(
                f"File {file_id} failed exact-byte validation: expected {len(expected)} bytes, "
                f"received {len(data)} bytes."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as target:
            target.write(data)
        return destination

    def download_outputs(
        self, response: JsonObject, directory: Path, *, expected: bytes | None = None,
    ) -> list[Path]:
        final_text(response)
        refs = file_references(response)
        if not refs:
            raise HarnessError("No structured file references; a prose link is not upload evidence.")
        if expected is not None and len(refs) != 1:
            raise HarnessError("Exact-byte validation requires exactly one output file.")
        destinations: list[tuple[FileReference, Path]] = []
        for ref in refs:
            name = ref.filename or ref.file_id
            if name in (".", "..") or "/" in name or "\\" in name or ":" in name or "\0" in name:
                raise HarnessError(f"Unsafe output filename: {name!r}.")
            path = directory / name
            if path in [destination for _, destination in destinations] or path.exists():
                raise HarnessError(f"Refusing duplicate/existing output path: {path}.")
            destinations.append((ref, path))
        return [
            self.download_file(ref.file_id, path, expected=expected)
            for ref, path in destinations
        ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--subscription", required=True, help="Explicit Azure subscription ID")
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--agent-version", help="Defaults to the verified latest active version")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--output-dir", type=Path, default=Path(".local/harness-client"))
    commands = parser.add_subparsers(dest="command", required=True)
    invoke = commands.add_parser("invoke", help="Invoke and save the complete response")
    prompt = invoke.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path)
    invoke.add_argument("--conversation", help="Reuse an existing conversation for follow-up")
    invoke.add_argument("--download-files", action="store_true")
    commands.add_parser("file-test", help="Generate, upload, download and verify FILE_DOWNLOAD_OK")
    download = commands.add_parser("download", help="Download structured files from a saved completed response")
    download.add_argument("--response-file", type=Path, required=True)
    download.add_argument("--expect-text", help="Require exactly these UTF-8 bytes in one file")
    args = parser.parse_args(argv)
    run_dir: Path | None = None
    try:
        client = HarnessClient(
            args.project_endpoint, args.subscription, args.agent_name,
            agent_version=args.agent_version, timeout=args.timeout,
        )
        run_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        run_dir = args.output_dir / run_name
        run_dir.mkdir(parents=True, exist_ok=False)
        print(f"Evidence directory: {run_dir}", file=sys.stderr, flush=True)
        expected: bytes | None = None
        if args.command == "download":
            response = json_object(json.loads(args.response_file.read_text(encoding="utf-8")), "Saved response")
            if args.expect_text is not None:
                expected = args.expect_text.encode("utf-8")
        else:
            if args.command == "file-test":
                prompt_text = (
                    "Create a file named harness-file-test.txt containing exactly FILE_DOWNLOAD_OK "
                    "(16 UTF-8 bytes, no newline). Upload it so I can download it, then return the file."
                )
                expected = b"FILE_DOWNLOAD_OK"
            else:
                prompt_text = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8")
            response = client.invoke(
                prompt_text, conversation_id=args.conversation if args.command == "invoke" else None,
            )
        (run_dir / "response.json").write_text(json.dumps(response, indent=2), encoding="utf-8")
        text = final_text(response)
        (run_dir / "answer.txt").write_text(text, encoding="utf-8")
        print(text)
        if args.command in ("file-test", "download") or args.download_files:
            files = client.download_outputs(response, run_dir / "downloads", expected=expected)
            for path in files:
                print(f"Downloaded: {path}")
            if expected is not None:
                print(f"PASS: downloaded bytes match exactly ({len(expected)} bytes).")
        print(f"Response: {response.get('id')}")
        return 0
    except (HarnessError, OSError, ValueError) as error:
        if isinstance(error, ResponseError) and run_dir is not None:
            (run_dir / "response.json").write_text(json.dumps(error.response, indent=2), encoding="utf-8")
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
