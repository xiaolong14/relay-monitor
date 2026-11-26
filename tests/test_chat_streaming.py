"""Chat Completions 流式协议回归，不使用真实渠道或凭证。"""

import http.client
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from relay_monitor import svg_artwork
from relay_monitor.request_options import degrade_request_body


RELAY = {"protocol": "openai", "model": "gpt-6.1-sol", "apiKey": "sk-test",
         "baseUrl": "https://relay.invalid/v1"}
SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>鹈鹕</text></svg>'
MARKER = b"\n=== Raw upstream response body (known credentials redacted only) ===\n"


def sse(*payloads):
    return "".join("data: " + (p if isinstance(p, str) else json.dumps(p, ensure_ascii=False))
                   + "\r\n\r\n" for p in payloads).encode("utf-8")


def chunk(text=None, finish=None, **delta):
    if text is not None:
        delta["content"] = text
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class NetworkResponse(io.BytesIO):
    """按单字节交付，刻意拆开中文编码与 SSE 行；禁止一次读完正文。"""

    status = 200
    headers = {"content-type": "text/event-stream"}

    def read1(self, size=-1):
        return super().read(1 if size < 0 else min(size, 1))

    def read(self, *_args):
        raise AssertionError("流式响应应逐段读取")

    def readline(self, *_args):
        raise AssertionError("应先保存网络字节再分行")


class ChatStreamingTests(unittest.TestCase):
    def test_svg_receives_fragmented_content_and_usage_without_reading_to_eof(self):
        captured = {}
        wire = b": keepalive\r\n\r\n" + sse(
            chunk(reasoning_content="推理不是最终画面"),
            chunk(SVG[:25]),
            {"choices": [{"index": 1, "delta": {"content": "不能混入其他候选"}}]},
            chunk([{"type": "text", "text": SVG[25:]}]),
            chunk(finish="stop"),
            {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 8}},
            "[DONE]",
        )

        class HeldConnection(NetworkResponse):
            def read1(self, size=-1):
                if self.tell() == len(wire):
                    raise AssertionError("已收到 DONE 后不能继续等待上游关闭连接")
                return super().read1(size)

        def open_response(request, timeout):
            captured.update(body=json.loads(request.data), timeout=timeout, accept=request.get_header("Accept"))
            return HeldConnection(wire)

        result = svg_artwork.generate_svg(RELAY, urlopen=open_response)
        self.assertIn("鹈鹕", result)
        self.assertNotIn("推理", result)
        self.assertTrue(captured["body"]["stream"])
        self.assertEqual(captured["body"]["stream_options"], {"include_usage": True})
        self.assertEqual(captured["accept"], "text/event-stream, application/json")
        self.assertEqual(captured["timeout"], 900)

    def test_truncated_stream_and_error_keep_raw_failure_evidence(self):
        cases = (
            (sse(chunk(SVG)), "生成完成前断开"),
            (sse(chunk(SVG[:20]), chunk(finish="length"), "[DONE]"), "长度上限"),
            (sse({"error": {"code": "stream_timeout"}}), "stream_timeout"),
            (b'data: {invalid}\n\n' + sse(chunk(finish="stop")), "无效 JSON"),
        )
        for wire, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "failure.log"
                with self.assertRaisesRegex(svg_artwork.SVGGenerationError, message):
                    svg_artwork.generate_svg(RELAY, urlopen=lambda *_a, **_k: NetworkResponse(wire),
                                             failure_log_path=path)
                header, raw = path.read_bytes().split(MARKER, 1)
                self.assertEqual(raw, wire)
                details = json.loads(header.split(b"\n", 1)[1])
                self.assertTrue(details["request_body"]["stream"])
                if message == "长度上限":
                    self.assertFalse(details["retryable"])

    def test_disconnect_and_mid_line_timeout_keep_partial_bytes(self):
        wire = sse(chunk(SVG[:20])) + b'data: {"choices":'
        for failure in (http.client.IncompleteRead(b"tail", 10), TimeoutError("stall")):
            class BrokenConnection(NetworkResponse):
                def read1(self, size=-1):
                    if self.tell() == len(wire):
                        raise failure
                    return super().read1(size)

            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "failure.log"
                with self.assertRaises(svg_artwork.SVGGenerationError):
                    svg_artwork.generate_svg(RELAY, urlopen=lambda *_a, **_k: BrokenConnection(wire),
                                             failure_log_path=path)
                header, raw = path.read_bytes().split(MARKER, 1)
                self.assertEqual(raw, wire + (b"tail" if isinstance(failure, http.client.IncompleteRead) else b""))
                self.assertFalse(json.loads(header.split(b"\n", 1)[1])["response_body_complete"])

    def test_rejecting_usage_option_keeps_streaming_and_same_model_parameters(self):
        requests = []

        def open_response(request, **_options):
            body = json.loads(request.data)
            requests.append(body)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 400, "bad", {},
                                             io.BytesIO(b'Unsupported parameter: stream_options.include_usage'))
            return NetworkResponse(sse(chunk(SVG), chunk(finish="stop"), "[DONE]"))

        self.assertIn("鹈鹕", svg_artwork.generate_svg(RELAY, urlopen=open_response))
        self.assertNotIn("stream_options", requests[1])
        self.assertEqual({k: v for k, v in requests[0].items() if k != "stream_options"}, requests[1])
        self.assertTrue(requests[1]["stream"])
        self.assertIsNone(degrade_request_body("openai", {"model": "x", "stream": True},
                                             "stream is unsupported"))

    @unittest.skipUnless(shutil.which("node"), "需要 Node 验证题库流式适配器")
    def test_node_provider_streaming_contracts(self):
        script = Path(__file__).with_name("chat_streaming_check.cjs")
        result = subprocess.run([shutil.which("node"), str(script)], cwd=script.parent.parent,
                                text=True, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("流式协议验证通过", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "需要 Node 验证两条链路的真实 HTTP 流")
    def test_http_chunked_stream_finishes_without_waiting_for_connection_close(self):
        """真正的 HTTP chunked 响应发送 DONE 后保持连接，两个客户端都必须及时返回。"""
        requests = []
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for event in (chunk(SVG[:20]), chunk(SVG[20:]), chunk(finish="stop"), "[DONE]"):
                        data = sse(event)
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                    # 不发 HTTP 结束块；若客户端等待同步 JSON 或 EOF，测试会触发超时。
                    release.wait(8)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    self.close_connection = True

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}/v1"
        import urllib.request

        def open_response(request, **_options):
            return urllib.request.urlopen(request, timeout=2)

        try:
            self.assertIn("鹈鹕", svg_artwork.generate_svg({**RELAY, "baseUrl": base}, urlopen=open_response))
            script = r"""
const Provider = require('./providers/openai_chat_provider.cjs');
new Provider({config:{apiBaseUrl:process.argv[1], apiKey:'sk-test', model:'gpt-6.1-sol',
  max_tokens:2048, requestTimeoutMs:2000}}).callApi('测试').then(result=>process.stdout.write(JSON.stringify(result)));
"""
            result = subprocess.run([shutil.which("node"), "-e", script, base],
                                    cwd=Path(__file__).resolve().parent.parent,
                                    capture_output=True, text=True, encoding="utf-8", timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("鹈鹕", json.loads(result.stdout)["output"])
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(body["stream"] for body in requests))
        finally:
            release.set()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
