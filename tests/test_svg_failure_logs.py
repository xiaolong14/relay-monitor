# -*- coding: utf-8 -*-
"""失败取证和下载链路回归，使用隔离数据库与模拟上游，不消耗渠道额度。"""

import http.client
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import monitor
import server
import svg_artwork


RELAY = {
    "name": "relay-a", "label": "测试渠道", "protocol": "openai-responses",
    "baseUrl": "https://relay.invalid/v1", "apiKey": "sk-private-test-key", "model": "test-model",
}
BODY_MARKER = b"\n=== Raw upstream response body (known credentials redacted only) ===\n"


class Response(io.BytesIO):
    """模拟可分段读取的原始响应。"""

    status = 200
    headers = {"content-type": "text/event-stream", "x-request-id": "upstream-request-123"}


class FailureLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "svg-failures" / "123.log"

    def read_log(self):
        header, body = self.path.read_bytes().split(BODY_MARKER, 1)
        return json.loads(header.split(b"\n", 1)[1]), body

    def generate_failure(self, response, protocol="openai-responses", **options):
        with self.assertRaises(svg_artwork.SVGGenerationError) as caught:
            svg_artwork.generate_svg(
                {**RELAY, "protocol": protocol}, failure_log_path=self.path,
                failure_log_metadata={"generation_id": 123, "trigger": "manual"},
                urlopen=lambda *_args, **_kwargs: response, **options,
            )
        self.assertIn("失败日志已保存", str(caught.exception))
        return self.read_log()

    def test_incomplete_stream_preserves_every_byte_and_request_diagnostics(self):
        events = [
            {"type": "response.output_text.delta", "delta": "<svg><g>截断内容"},
            {"type": "response.incomplete", "response": {
                "status": "incomplete", "incomplete_details": {"reason": "length"},
                "usage": {"output_tokens": 32768},
            }},
        ]
        raw = ("".join(f"data: {json.dumps(event, ensure_ascii=False)}\r\n\r\n" for event in events)
               + "data: [DONE]\r\n\r\n").encode("utf-8")
        details, body = self.generate_failure(Response(raw))
        self.assertEqual(body, raw)
        self.assertTrue(details["response_body_complete"])
        self.assertEqual(details["captured_response_bytes"], len(raw))
        self.assertEqual(details["request_body"]["input"], svg_artwork.USER_PROMPT)
        self.assertTrue(details["request_body"]["stream"])
        self.assertEqual(details["response_headers"]["x-request-id"], "upstream-request-123")
        self.assertIn("_validate_responses_candidates", details["traceback"])
        self.assertIn("输出校验失败：SVG 根标签未闭合", details["error"])
        self.assertEqual(details["generation_id"], 123)
        self.assertGreaterEqual(details["elapsed_seconds"], 0)
        self.assertNotIn(RELAY["apiKey"].encode(), self.path.read_bytes())

    def test_all_json_protocols_preserve_complete_invalid_output(self):
        payloads = {
            "openai": {"choices": [{"message": {"content": "<svg><g>"}}]},
            "anthropic": {"content": [{"type": "text", "text": "<svg><g>"}]},
            "openai-responses": {"status": "incomplete", "incomplete_details": {"reason": "length"},
                                 "output_text": "<svg><g>"},
        }
        for protocol, payload in payloads.items():
            with self.subTest(protocol=protocol):
                raw = json.dumps(payload).encode()
                details, body = self.generate_failure(Response(raw), protocol=protocol)
                self.assertEqual(body, raw)
                self.assertTrue(details["response_body_complete"])
                self.assertEqual(details["protocol"], protocol)

    def test_parser_failure_still_captures_following_events(self):
        raw = b'data: {invalid-json}\n\ndata: {"type":"error","error":{"message":"last detail"}}\n\n'
        details, body = self.generate_failure(Response(raw))
        self.assertEqual(body, raw)
        self.assertTrue(details["response_body_complete"])
        self.assertIn("JSONDecodeError", details["traceback"])

    def test_reasoning_summary_only_is_reported_without_claiming_svg_rejection(self):
        summary = "模型计划绘图，并提到占位符 <svg ...> ... </svg>，随后继续规划。"
        events = [
            {"type": "response.reasoning_summary_text.delta", "delta": summary},
            {"type": "response.reasoning_summary_text.done", "text": summary},
            {"type": "response.output_item.done", "item": {
                "type": "reasoning", "status": "incomplete",
                "summary": [{"type": "summary_text", "text": summary}],
            }},
            {"type": "response.incomplete", "response": {
                "status": "incomplete", "incomplete_details": {"reason": "length"},
                "usage": {"output_tokens": 32768, "output_tokens_details": {
                    "reasoning_tokens": 32768,
                }},
                "output": [{"type": "reasoning", "status": "incomplete", "summary": [
                    {"type": "summary_text", "text": summary},
                ]}],
            }},
        ]
        raw = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events).encode()
        details, body = self.generate_failure(Response(raw))
        self.assertEqual(body, raw)
        self.assertIn("输出 Token 全部消耗在推理中", details["error"])
        self.assertNotIn("输出校验失败", details["error"])
        self.assertIn(f"推理摘要={len(summary)}字符（未提取到有效 SVG）", details["error"])
        # 忽略 stream 参数的渠道返回相同 JSON 对象时，应得到一致诊断和完整日志。
        raw = json.dumps(events[-1]["response"], ensure_ascii=False).encode()
        details, body = self.generate_failure(Response(raw))
        self.assertEqual(body, raw)
        self.assertIn("输出 Token 全部消耗在推理中", details["error"])
        self.assertNotIn("输出校验失败", details["error"])
        self.assertIn(f"推理摘要={len(summary)}字符（未提取到有效 SVG）", details["error"])

    def test_timeout_and_incomplete_read_keep_received_content(self):
        partial = b'data: {"type":"response.created"}\n\n'
        for protocol in ("openai", "openai-responses"):
            for error in (TimeoutError("upstream stalled"), http.client.IncompleteRead(b"last bytes", 20)):
                with self.subTest(protocol=protocol, error=type(error).__name__):
                    class BrokenResponse(Response):
                        def read(self, size=-1):
                            if self.tell() < len(partial):
                                return super().read(size)
                            raise error

                        read1 = read

                        def readline(self, size=-1):
                            if self.tell() < len(partial):
                                return super().readline(size)
                            raise error

                    details, body = self.generate_failure(BrokenResponse(partial), protocol=protocol)
                    expected = partial + (b"last bytes" if isinstance(error, http.client.IncompleteRead) else b"")
                    self.assertEqual(body, expected)
                    self.assertFalse(details["response_body_complete"])
                    self.assertIn(type(error).__name__, details["response_capture_end"])
                    self.assertIn(str(error), details["traceback"])

    def test_stream_timeout_mid_line_keeps_unterminated_network_bytes(self):
        partial = b'data: {"type":"response.output_text.delta","delta":"<svg'

        class MidLineTimeout(Response):
            def read1(self, size=-1):
                if self.tell() < len(partial):
                    return super().read1(size)
                raise TimeoutError("interrupted in the middle of an SSE line")

            def readline(self, size=-1):
                raise AssertionError("不能用等待换行的读取方式取证")

        details, body = self.generate_failure(MidLineTimeout(partial))
        self.assertEqual(body, partial)
        self.assertFalse(details["response_body_complete"])
        self.assertEqual(details["error_type"], "TimeoutError")

    def test_premature_eof_does_not_claim_complete_body(self):
        response = Response(b"truncated")
        response.headers = {"content-length": "1000"}
        details, body = self.generate_failure(response, protocol="openai")
        self.assertEqual(body, b"truncated")
        self.assertFalse(details["response_body_complete"])
        self.assertIn("实际收到 9 字节", details["response_capture_end"])

    def test_http_524_retains_error_page_and_redacts_echoed_key(self):
        headers = Message()
        headers["cf-ray"] = "cloudflare-trace-id"
        headers["set-cookie"] = "private-cookie"
        raw = f"<html>524 Origin timed out: {RELAY['apiKey']}</html>".encode()

        def fail(*_args, **_kwargs):
            raise urllib.error.HTTPError(RELAY["baseUrl"], 524, "timeout", headers, io.BytesIO(raw))

        with self.assertRaisesRegex(svg_artwork.SVGGenerationError, "HTTP 524"):
            svg_artwork.generate_svg(RELAY, urlopen=fail, failure_log_path=self.path)
        details, body = self.read_log()
        self.assertEqual(details["http_status"], 524)
        self.assertTrue(details["response_body_complete"])
        self.assertTrue(details["credentials_redacted"])
        self.assertEqual(details["response_headers"]["cf-ray"], "cloudflare-trace-id")
        self.assertNotIn("set-cookie", details["response_headers"])
        self.assertEqual(body, raw.replace(RELAY["apiKey"].encode(), b"[REDACTED]"))
        self.assertNotIn(RELAY["apiKey"].encode(), self.path.read_bytes())
        self.assertIn("HTTPError", details["traceback"])

    def test_size_limit_is_explicitly_recorded_as_partial(self):
        limit = svg_artwork.SVG_CHARS_PER_TOKEN * 6 + 64 * 1024
        raw = b"x" * (limit + 100)
        for protocol in ("openai", "openai-responses"):
            with self.subTest(protocol=protocol):
                details, body = self.generate_failure(Response(raw), protocol=protocol, max_tokens=1)
                self.assertEqual(body, raw[:limit + 1])
                self.assertFalse(details["response_body_complete"])
                self.assertIn("保护上限", details["response_capture_end"])

    def test_unexpected_response_shape_keeps_traceback(self):
        details, body = self.generate_failure(Response(b"[]"), protocol="openai")
        self.assertEqual(body, b"[]")
        self.assertEqual(details["error_type"], "AttributeError")
        self.assertIn("AttributeError", details["traceback"])

    def test_log_write_failure_does_not_claim_saved(self):
        with patch.object(Path, "write_bytes", side_effect=PermissionError("no access")):
            with self.assertRaisesRegex(svg_artwork.SVGGenerationError, "日志写入磁盘失败") as caught:
                svg_artwork.generate_svg(
                    RELAY, urlopen=lambda *_a, **_k: Response(b"invalid"), failure_log_path=self.path,
                )
        self.assertNotIn("已保存", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_success_does_not_create_failure_log(self):
        raw = b'{"choices":[{"message":{"content":"<svg><circle r=\\"1\\"><animate attributeName=\\"r\\" from=\\"1\\" to=\\"2\\" dur=\\"1s\\"/></circle></svg>"}}]}'
        result = svg_artwork.generate_svg(
            {**RELAY, "protocol": "openai"}, urlopen=lambda *_a, **_k: Response(raw),
            failure_log_path=self.path,
        )
        self.assertIn("<animate", result)
        self.assertFalse(self.path.exists())


class FailureLogAPITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "history.db"
        for name, value in (("DATA_DIR", self.root), ("DB_PATH", self.db)):
            target = patch.object(monitor, name, value)
            target.start()
            self.addCleanup(target.stop)
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("""CREATE TABLE svg_generations(
                id INTEGER PRIMARY KEY, relay TEXT, relay_label TEXT, model_id TEXT,
                status TEXT, trigger TEXT, error_detail TEXT, requested_at TEXT,
                started_at TEXT, completed_at TEXT, svg_content TEXT)""")
            con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
            con.executemany(
                "INSERT INTO svg_generations(id,relay,status,trigger) VALUES(?,'test',?,'manual')",
                [(1, "failed"), (2, "failed"), (3, "completed")],
            )
            con.commit()
        self.client = TestClient(server.app)  # 不启动后台调度器。
        self.addCleanup(self.client.close)

    def test_worker_failure_is_saved_listed_and_downloadable(self):
        settings = {"reasoning_effort": "high", "api_timeout_minutes": 15,
                    "max_tokens": 32768, "max_input_tokens": 262144}
        raw = b'data: {"type":"response.incomplete","response":{"status":"incomplete","incomplete_details":{"reason":"length"},"output_text":"<svg><g>"}}\n\n'
        with patch.object(server, "get_con", side_effect=lambda: sqlite3.connect(self.db)), \
                patch.object(server, "load_settings", return_value=settings), \
                patch.object(server, "_svg_state_update"), \
                patch.object(svg_artwork.urllib.request, "urlopen", side_effect=lambda *_a, **_k: Response(raw)):
            self.assertFalse(server._run_one_svg_generation(1, RELAY))
        listing = self.client.get("/api/svg-generations").json()
        rows = {row["id"]: row for row in listing["items"]}
        self.assertTrue(rows[1]["failure_log_available"])
        self.assertFalse(rows[2]["failure_log_available"])
        self.assertFalse(rows[3]["failure_log_available"])
        download = self.client.get("/api/svg-generations/1/failure-log")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, server._svg_failure_log_path(1).read_bytes())
        self.assertTrue(download.content.endswith(raw))
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(download.headers["cache-control"], "no-store")
        self.assertNotIn(RELAY["apiKey"].encode(), download.content)

    def test_download_rejects_missing_logs_and_non_failed_records(self):
        for generation_id, status in ((1, 404), (999, 404), (3, 409)):
            with self.subTest(generation_id=generation_id):
                self.assertEqual(self.client.get(
                    f"/api/svg-generations/{generation_id}/failure-log",
                ).status_code, status)
        self.assertEqual(self.client.get("/api/svg-generations/not-an-id/failure-log").status_code, 422)

    def test_worker_fallback_keeps_original_internal_exception(self):
        try:
            raise RuntimeError(f"unexpected worker error {RELAY['apiKey']}")
        except RuntimeError as exc:
            message = server._record_svg_worker_failure(1, RELAY, exc)
        content = server._svg_failure_log_path(1).read_bytes()
        self.assertIn("日志已保存", message)
        self.assertIn(b"RuntimeError", content)
        self.assertNotIn(RELAY["apiKey"].encode(), content)
        server._record_svg_worker_failure(1, RELAY, ValueError("second failure"))
        self.assertEqual(content, server._svg_failure_log_path(1).read_bytes())


if __name__ == "__main__":
    unittest.main()
