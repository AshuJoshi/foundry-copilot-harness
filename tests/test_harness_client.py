import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from harness_client import (
    FileReference, HarnessClient, HarnessError, ResponseError, UPLOAD_TOOL,
    file_references, final_text, main, parse_response,
)


ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"


def completed() -> dict:
    return {
        "id": "resp-test", "status": "completed",
        "agent_reference": {"name": "agent", "version": "2"},
        "output": [{
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Final answer"}],
        }],
    }


def uploaded() -> dict:
    response = completed()
    response["output"][:0] = [
        {"type": "function_call", "name": UPLOAD_TOOL, "call_id": "upload-1"},
        {"type": "function_call_output", "call_id": "upload-1", "status": "completed",
         "output": json.dumps({"file_id": "file-123", "filename": "result.txt"})},
    ]
    return response


class ResponseTests(unittest.TestCase):
    def test_json_and_terminal_sse(self) -> None:
        response = completed()
        self.assertEqual(parse_response(json.dumps(response)), response)
        event = json.dumps({"type": "response.completed", "response": response})
        self.assertEqual(parse_response("event: response.completed\r\ndata: " + event + "\r\n\r\n"), response)

    def test_progress_is_not_success(self) -> None:
        with self.assertRaisesRegex(HarnessError, "No terminal"):
            parse_response('data: {"type":"response.output_text.delta","delta":"Hello"}\n\n')

    def test_failed_incomplete_empty_and_commentary_only(self) -> None:
        for status in ("failed", "incomplete", "in_progress"):
            response = completed()
            response["status"] = status
            with self.subTest(status=status), self.assertRaises(ResponseError):
                final_text(response)
        for output in ([], [{"type": "message", "role": "assistant", "phase": "commentary"}]):
            response = completed()
            response["output"] = output
            with self.assertRaises(ResponseError):
                final_text(response)

    def test_last_unphased_message_and_explicit_final(self) -> None:
        response = completed()
        response["output"].insert(0, {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Working..."}],
        })
        self.assertEqual(final_text(response), "Final answer")
        response["output"][0]["phase"] = "final_answer"
        self.assertEqual(final_text(response), "Working...")

    def test_upload_correlation_and_no_markdown_scraping(self) -> None:
        self.assertEqual(file_references(uploaded()), [FileReference("file-123", "result.txt")])
        response = uploaded()
        response["output"][1]["call_id"] = "unrelated"
        response["output"][-1]["content"][0]["text"] = "[download](file-123)"
        self.assertEqual(file_references(response), [])

    def test_annotations_and_deduplication(self) -> None:
        for annotation in (
            {"type": "file_path", "file_id": "file-123"},
            {"type": "file_path", "file_path": {"file_id": "file-123"}},
        ):
            response = uploaded()
            response["output"][-1]["content"][0]["annotations"] = [annotation]
            self.assertEqual(file_references(response), [FileReference("file-123", "result.txt")])

    def test_upload_failures_are_explicit(self) -> None:
        for value in ('not json', '{"error":"403"}', '{"filename":"result.txt"}'):
            response = uploaded()
            response["output"][1]["output"] = value
            with self.subTest(value=value), self.assertRaises(HarnessError):
                file_references(response)

    def test_duplicate_upload_ids_rejected(self) -> None:
        response = uploaded()
        response["output"].insert(0, response["output"][0])
        with self.assertRaisesRegex(HarnessError, "Duplicate"):
            file_references(response)


class ClientTests(unittest.TestCase):
    def client(self, **kwargs: object) -> HarnessClient:
        return HarnessClient(ENDPOINT, "subscription", "agent", token_provider=lambda: "secret-token", **kwargs)

    def test_endpoint_guards(self) -> None:
        for endpoint in (
            "http://example.services.ai.azure.com/api/projects/demo",
            "https://evil.example/api/projects/demo",
            ENDPOINT + "?override=true",
            "https://user@example.services.ai.azure.com/api/projects/demo",
            "https://example.services.ai.azure.com:8443/api/projects/demo",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(HarnessError):
                HarnessClient(endpoint, "sub", "agent")

    def test_fresh_conversation_pinned_version_and_polling(self) -> None:
        client = self.client()
        agent = {"versions": {"latest": {"version": "2", "status": "active", "definition": {
            "kind": "prompt", "harness": {"type": "github_copilot_preview"},
        }}}}
        results = [agent, {"id": "conv-fresh"}, {"id": "resp-test", "status": "queued"}, completed()]
        with patch.object(client, "_request", side_effect=[json.dumps(v).encode() for v in results]) as request:
            with patch("harness_client.time.sleep"):
                self.assertEqual(client.invoke("Hello")["status"], "completed")
        body = request.call_args_list[2].args[1]
        self.assertEqual(body["conversation"], "conv-fresh")
        self.assertEqual(body["agent_reference"]["version"], "2")
        self.assertTrue(request.call_args_list[3].args[0].endswith("/responses/resp-test"))

    def test_followup_reuses_conversation_and_rejects_wrong_version(self) -> None:
        client = self.client()
        response = completed()
        response["agent_reference"]["version"] = "1"
        with patch.object(client, "_verified_version", return_value="2"):
            with patch.object(client, "create_conversation") as create:
                with patch.object(client, "_request", return_value=json.dumps(response).encode()) as request:
                    with self.assertRaisesRegex(ResponseError, "did not match"):
                        client.invoke("Follow up", conversation_id="conv-existing")
        create.assert_not_called()
        self.assertEqual(request.call_args.args[1]["conversation"], "conv-existing")

    def test_standard_agent_rejected_before_invocation(self) -> None:
        client = self.client()
        agent = {"versions": {"latest": {"version": "1", "status": "active",
                                       "definition": {"kind": "prompt", "harness": {"type": "other"}}}}}
        with patch.object(client, "get_agent", return_value=agent):
            with self.assertRaisesRegex(HarnessError, "not a Copilot"):
                client.invoke("Hello")

    def test_exact_download_and_no_overwrite(self) -> None:
        client = self.client()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.txt"
            with patch.object(client, "_request", return_value=b"FILE_DOWNLOAD_OK"):
                client.download_file("file-123", target, expected=b"FILE_DOWNLOAD_OK")
                self.assertEqual(target.read_bytes(), b"FILE_DOWNLOAD_OK")
                with self.assertRaises(FileExistsError):
                    client.download_file("file-123", target)

    def test_newline_mismatch_creates_no_file(self) -> None:
        client = self.client()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.txt"
            with patch.object(client, "_request", return_value=b"FILE_DOWNLOAD_OK\n"):
                with self.assertRaisesRegex(HarnessError, "exact-byte"):
                    client.download_file("file-123", target, expected=b"FILE_DOWNLOAD_OK")
            self.assertFalse(target.exists())

    def test_unsafe_names_and_missing_files(self) -> None:
        client = self.client()
        for name in ("../escape.txt", r"..\escape.txt", "/absolute", "C:escape", ".."):
            response = uploaded()
            response["output"][1]["output"] = json.dumps({"file_id": "file-123", "filename": name})
            with self.subTest(name=name), self.assertRaisesRegex(HarnessError, "Unsafe"):
                client.download_outputs(response, Path("downloads"))
        with self.assertRaisesRegex(HarnessError, "No structured"):
            client.download_outputs(completed(), Path("downloads"))

    def test_http_error_redacts_token_and_never_retries(self) -> None:
        client = self.client()
        error = urllib.error.HTTPError(ENDPOINT, 403, "Forbidden", {}, io.BytesIO(b"denied secret-token"))
        with patch.object(client._opener, "open", side_effect=error) as request:
            with self.assertRaises(HarnessError) as raised:
                client._request("/files/file-123/content")
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertIn("403", str(raised.exception))
        request.assert_called_once()

    def test_cli_saves_failure_evidence_and_exits_nonzero(self) -> None:
        response = completed()
        response["status"] = "failed"
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(HarnessClient, "invoke", side_effect=ResponseError("failed", response)):
                code = main([
                    "--project-endpoint", ENDPOINT, "--subscription", "sub",
                    "--agent-name", "agent", "--output-dir", directory, "file-test",
                ])
            self.assertEqual(code, 1)
            saved = list(Path(directory).glob("*/response.json"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(json.loads(saved[0].read_text())["status"], "failed")

    def test_incomplete_response_body_is_recorded_as_transport_failure_without_retry(self) -> None:
        from http.client import IncompleteRead

        events = []
        client = self.client(http_observer=events.append)
        with patch.object(client._opener, "open") as request:
            response = request.return_value.__enter__.return_value
            response.status = 200
            response.headers = {"x-request-id": "test-request"}
            response.read.side_effect = IncompleteRead(b"partial body", 4)
            with self.assertRaisesRegex(HarnessError, "transport failed"):
                client._request("/responses", {"input": "test"})
            request.assert_called_once()
        self.assertEqual(events[-1]["event"], "transport_error")
        self.assertNotIn("secret-token", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
