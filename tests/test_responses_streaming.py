"""Responses 流结束契约回归；使用本地模拟服务，不消耗渠道额度。"""

import io
import json
import shutil
import subprocess
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from relay_monitor import svg_artwork


ROOT = Path(__file__).resolve().parent.parent
SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>鹈鹕</text></svg>'
RELAY = {"protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
         "model": "gpt-6.1-sol", "apiKey": "sk-test"}


def sse(*events):
    return "".join(f"data: {json.dumps(event, ensure_ascii=False)}\r\n\r\n"
                   for event in events).encode("utf-8")


class HeldResponse(io.BytesIO):
    """终止事件之后模拟连接持续挂起，禁止继续等 EOF。"""

    def read1(self, size=-1):
        if self.tell() == len(self.getvalue()):
            raise TimeoutError("终止事件之后仍在等待连接关闭")
        return super().read1(min(size, 7))


class ResponsesStreamingTests(unittest.TestCase):
    def test_svg_stops_at_terminal_event_without_waiting_for_eof(self):
        for event_type in ("response.completed", "response.done", "response.incomplete"):
            with self.subTest(event_type=event_type):
                response = {"status": "incomplete" if event_type.endswith("incomplete") else "completed",
                            "output_text": SVG}
                wire = sse({"type": "response.output_text.delta", "delta": SVG},
                           {"type": event_type, "response": response})
                result = svg_artwork.generate_svg(
                    RELAY, urlopen=lambda *_args, **_kwargs: HeldResponse(wire))
                self.assertIn("鹈鹕", result)

    def test_svg_terminal_failure_keeps_upstream_error_instead_of_timing_out(self):
        for event in (
            {"type": "error", "code": "stream_timeout"},
            {"type": "response.failed", "response": {"error": {"code": "stream_timeout"}}},
            {"type": "response.cancelled", "response": {"status": "cancelled", "output_text": SVG}},
        ):
            with self.subTest(event=event):
                with self.assertRaises(svg_artwork.SVGGenerationError) as caught:
                    svg_artwork.generate_svg(
                        RELAY, urlopen=lambda *_args, **_kwargs: HeldResponse(sse(event)))
                if event["type"] == "response.cancelled":
                    self.assertIn("取消", str(caught.exception))
                else:
                    self.assertEqual(caught.exception.response_metadata["upstream_error_code"], "stream_timeout")

    @unittest.skipUnless(shutil.which("node"), "需要 Node 验证题库适配器")
    def test_node_responses_streaming_contract(self):
        script = Path(__file__).with_name("responses_streaming_check.cjs")
        result = subprocess.run([shutil.which("node"), str(script)], cwd=ROOT,
                                text=True, capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Responses 流式协议验证通过", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "需要 Node 验证两条调用链")
    def test_http_terminal_event_finishes_before_chunked_response_closes(self):
        requests = []
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                data = sse({"type": "response.output_text.delta", "delta": SVG},
                           {"type": "response.completed", "response": {"status": "completed",
                            "usage": {"input_tokens": 3, "output_tokens": 2}}})
                try:
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                    # 故意不发送 HTTP 结束块与 [DONE]，客户端应以 Responses 终止事件结束。
                    release.wait(8)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    self.close_connection = True

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}/v1"
        try:
            result = svg_artwork.generate_svg(
                {**RELAY, "baseUrl": base},
                urlopen=lambda request, **_kwargs: urllib.request.urlopen(request, timeout=1))
            self.assertIn("鹈鹕", result)
            script = r"""
const Provider = require('./providers/openai_responses_provider.cjs');
new Provider({config:{apiBaseUrl:process.argv[1], apiKey:'sk-test', model:'gpt-6.1-sol',
  max_output_tokens:2048, requestTimeoutMs:1000}}).callApi('测试').then(result=>process.stdout.write(JSON.stringify(result)));
"""
            run = subprocess.run([shutil.which("node"), "-e", script, base], cwd=ROOT,
                                 capture_output=True, text=True, encoding="utf-8", timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertIn("鹈鹕", result["output"])
            self.assertEqual(result["tokenUsage"], {"prompt": 3, "completion": 2, "total": 5})
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(path == "/v1/responses" and body["stream"] for path, body in requests))
        finally:
            release.set()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
