# -*- coding: utf-8 -*-
"""svg_review 评分请求与错误解析的回归测试。"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import base64
import io
import json
import socket
import ssl
import struct
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import httpcore

from relay_monitor import svg_artwork, svg_review

RELAY = {
    "name": "score", "label": "评分渠道", "protocol": "openai",
    "baseUrl": "https://example.com/v1", "apiKey": "secret-key",
}
CUSTOM_PROMPT = "按画面构图打分，提供 score、problems 和 comment。"
PREVIEW_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/n1QAAAAASUVORK5CYII="
)


class FakeResponse:
    """模拟 urllib 响应，返回固定 JSON 正文。"""

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def ok_payload(text):
    """构造 OpenAI Chat Completions 的成功响应。"""
    return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}


class ReviewReasoningTests(unittest.TestCase):
    """覆盖思考等级下发、none 省略参数和上游错误解析。"""

    def capture(self, effort, payload=None):
        """执行一次评分并返回实际请求体与解析结果。"""
        captured = {}

        def opener(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(payload or ok_payload(
                '{"score": 88, "problems": [], "comment": "完整"}'
            ))

        result = svg_review.review_svg(
            RELAY, preview_image=PREVIEW_IMAGE, prompt=CUSTOM_PROMPT, model="gpt-6.1-terra",
            reasoning_effort=effort, urlopen=opener,
        )
        return captured["body"], result

    def test_default_low_is_sent_not_minimal(self):
        """默认 low 必须原样下发，不能回落成模型不支持的 minimal。"""
        body, result = self.capture("low")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["max_completion_tokens"], svg_review.REVIEW_MAX_OUTPUT_TOKENS)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(result["score"], 88.0)

    def test_none_omits_reasoning_effort(self):
        """选择关闭思考时不发送 reasoning_effort。"""
        body, _ = self.capture("none")
        self.assertNotIn("reasoning_effort", body)

    def test_http_error_extracts_message_and_hint(self):
        """上游 400 只保留错误消息，并提示调整评分思考等级。"""
        detail = b'{"error":{"message":"unsupported reasoning effort; key: secret\\u002dkey"}}'
        body = io.BytesIO(detail)
        error = urllib.error.HTTPError(RELAY["baseUrl"], 400, "Bad Request", {}, body)
        with patch.object(svg_review.stream_transport, "urlopen", side_effect=error) as opener, \
                self.assertRaises(svg_review.SVGReviewError) as ctx:
            svg_review.review_svg(
                RELAY, preview_image=PREVIEW_IMAGE, model="gpt-6.1-terra",
                reasoning_effort="minimal",
            )
        self.assertIn("HTTP 400：unsupported reasoning effort", str(ctx.exception))
        self.assertIn("评分思考等级", str(ctx.exception))
        self.assertNotIn(RELAY["apiKey"], str(ctx.exception))
        self.assertTrue(body.closed)
        opener.assert_called_once()

    def test_code_fence_json_is_parsed(self):
        """模型用 Markdown 代码块包裹 JSON 时仍能解析。"""
        _, result = self.capture(
            "low",
            ok_payload('```json\n{"score": 95, "problems": ["背景空"], "comment": "好"}\n```'),
        )
        self.assertEqual(result["score"], 95.0)
        self.assertEqual(result["problems"], ["背景空"])
        self.assertEqual(result["comment"], "好")


class ReviewTransportTests(unittest.TestCase):
    def test_openai_review_transports_use_the_shared_client(self):
        text = '{"score": 91}'
        for protocol, endpoint, payload in (
            ("openai", "/chat/completions", ok_payload(text)),
            ("openai-responses", "/responses", {"output_text": text}),
        ):
            with self.subTest(protocol=protocol), patch.object(
                svg_review.stream_transport, "urlopen", return_value=FakeResponse(payload),
            ) as opener:
                result = svg_review.review_svg(
                    {**RELAY, "protocol": protocol}, preview_image=PREVIEW_IMAGE,
                    prompt=CUSTOM_PROMPT, model="review-model",
                )
                self.assertEqual(result["score"], 91)
                request = opener.call_args.args[0]
                self.assertTrue(request.full_url.endswith(endpoint))
                self.assertEqual(json.loads(request.data)["model"], "review-model")

    def test_image_review_uses_visual_inputs_for_all_protocols(self):
        score_text = '{"score": 91}'
        for protocol, payload in (
            ("openai", ok_payload(score_text)),
            ("openai-responses", {"output_text": score_text}),
            ("anthropic", {"content": [{"type": "text", "text": score_text}]}),
        ):
            with self.subTest(protocol=protocol):
                captured = {}

                def opener(request, timeout):
                    captured.update(json.loads(request.data))
                    return FakeResponse(payload)

                result = svg_review.review_svg(
                    {**RELAY, "protocol": protocol}, preview_image=PREVIEW_IMAGE,
                    prompt=CUSTOM_PROMPT, model="review-model", urlopen=opener,
                )
                self.assertEqual(result["review_basis"], "rendered-image")
                if protocol == "anthropic":
                    content = captured["messages"][0]["content"]
                    self.assertEqual(content[1]["type"], "image")
                    encoded = content[1]["source"]["data"]
                elif protocol == "openai-responses":
                    content = captured["input"][0]["content"]
                    self.assertEqual(content[1]["type"], "input_image")
                    encoded = content[1]["image_url"].removeprefix("data:image/png;base64,")
                else:
                    content = captured["messages"][0]["content"]
                    self.assertEqual(content[1]["type"], "image_url")
                    encoded = content[1]["image_url"]["url"].removeprefix("data:image/png;base64,")
                self.assertEqual(len(content), 2)
                self.assertEqual(content[0]["text"], f"{CUSTOM_PROMPT}\n\n{svg_review.VISUAL_ONLY_GUARD}")
                self.assertEqual(base64.b64decode(encoded), PREVIEW_IMAGE)

    def test_missing_or_non_png_preview_is_rejected_before_network_call(self):
        with patch.object(svg_review.stream_transport, "urlopen") as opener:
            for image in (None, b"", b"<svg/>"):
                with self.subTest(image=image), self.assertRaisesRegex(svg_review.SVGReviewError, "已渲染的成品图"):
                    svg_review.review_svg(RELAY, preview_image=image, model="review-model")
            opener.assert_not_called()

    def test_svg_preview_renderer_returns_a_png(self):
        svg = (Path(__file__).parent / "fixtures" / "svg" / "pelican_ride_refined.svg").read_text(encoding="utf-8")
        # 模拟没有 Cairo 的环境，覆盖原服务因加载系统库而无法渲染的回归。
        with patch.dict(sys.modules, {"cairosvg": None, "cairocffi": None}):
            image = svg_review.render_svg_preview(svg, output_width=321, output_height=197)
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", image[16:24]), (321, 197))

    def test_svg_preview_reports_missing_renderer(self):
        with patch.dict(sys.modules, {"resvg_py": None}):
            with self.assertRaisesRegex(svg_review.SVGReviewError, "请重新安装项目的 Python 依赖"):
                svg_review.render_svg_preview("<svg/>")

    def test_svg_preview_renderer_strips_external_resources(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><image href="https://example.com/a.png" width="10" height="10"/></svg>'
        safe_svg = svg_artwork.sanitize_svg(svg)
        self.assertNotIn("https://example.com", safe_svg)
        self.assertTrue(svg_review.render_svg_preview(svg).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_score_recovers_after_real_connection_drops(self):
        requests = []
        body = FakeResponse(ok_payload('{"score": 91}')).payload

        class RelayHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                # 第一次响应只写一部分，模拟 HTTP 200 之后的真实断流。
                self.wfile.write(body[:10] if len(requests) == 1 else body)

            def log_message(self, *_args):
                pass

        with HTTPServer(("127.0.0.1", 0), RelayHandler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                relay = {**RELAY, "baseUrl": f"http://127.0.0.1:{server.server_port}"}
                with patch.object(svg_review.stream_transport, "_proxy_for", return_value=None):
                    result = svg_review.review_svg(
                        relay, preview_image=PREVIEW_IMAGE, prompt=CUSTOM_PROMPT, model="review-model",
                    )
            finally:
                server.shutdown()
                worker.join()
        self.assertEqual(result["score"], 91)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0][0], "/v1/chat/completions")

    def test_default_retries_twice_after_disconnect_and_timeout(self):
        disconnect = urllib.error.URLError(ConnectionResetError(10054, "模拟连接重置"))
        response = FakeResponse(ok_payload('{"score": 91}'))
        with patch.object(svg_review.stream_transport, "urlopen",
                          side_effect=[disconnect, TimeoutError(), response]) as opener:
            result = svg_review.review_svg(
                RELAY, preview_image=PREVIEW_IMAGE, prompt=CUSTOM_PROMPT,
                model="review-model", api_timeout_minutes=1,
            )
        self.assertEqual(result["score"], 91)
        self.assertEqual(result["review_attempts"], 3)
        request_bodies = [call.args[0].data for call in opener.call_args_list]
        self.assertEqual(request_bodies, [request_bodies[0]] * 3)
        self.assertEqual([call.kwargs["timeout"] for call in opener.call_args_list], [60] * 3)

    def test_stops_at_configured_retry_count_including_zero(self):
        for retry_count in (0, 1, 4):
            with self.subTest(retry_count=retry_count), patch.object(
                svg_review.stream_transport, "urlopen", side_effect=TimeoutError(),
            ) as opener, self.assertRaisesRegex(svg_review.SVGReviewError, "连接或读取超时"):
                svg_review.review_svg(RELAY, preview_image=PREVIEW_IMAGE, model="review-model",
                                      api_timeout_minutes=1, retry_count=retry_count)
            self.assertEqual(opener.call_count, retry_count + 1)

    def test_retryable_http_and_incomplete_custom_score_use_same_retry_count(self):
        http_error = urllib.error.HTTPError(RELAY["baseUrl"], 503, "Unavailable", {}, io.BytesIO())
        invalid = FakeResponse(ok_payload('{"problems": []}'))
        valid = FakeResponse(ok_payload('{"score": 84}'))
        with patch.object(svg_review.stream_transport, "urlopen", side_effect=[http_error, invalid, valid]):
            result = svg_review.review_svg(
                RELAY, preview_image=PREVIEW_IMAGE, model="review-model", prompt=CUSTOM_PROMPT,
            )
        self.assertEqual(result["score"], 84)
        self.assertEqual(result["review_attempts"], 3)

    def test_gateway_error_uses_backup_within_retry_budget(self):
        backup = {**RELAY, "name": "backup", "baseUrl": "https://backup.example/v1",
                  "protocol": "openai-responses", "apiKey": "backup-key", "model": "another-model"}
        failure = urllib.error.HTTPError(
            RELAY["baseUrl"], 502, "Bad Gateway", {"x-request-id": "trace-1"},
            io.BytesIO(b'{"error":{"message":"upstream unavailable"}}'),
        )
        response = FakeResponse({"output_text": '{"score": 84}'})
        with patch.object(svg_review.stream_transport, "urlopen", side_effect=[failure, response]) as opener:
            result = svg_review.review_svg(
                RELAY, preview_image=PREVIEW_IMAGE, model="gpt-6.1-terra", reasoning_effort="high",
                prompt=CUSTOM_PROMPT, fallback_relay=backup, retry_count=1,
            )

        self.assertEqual(result["review_attempts"], 2)
        self.assertEqual(result["score"], 84)
        self.assertEqual(result["relay"], "backup")
        self.assertEqual(result["primary_relay"], "score")
        self.assertEqual(result["protocol"], "openai-responses")
        self.assertTrue(failure.fp.closed)
        first, second = (call.args[0] for call in opener.call_args_list)
        self.assertEqual(first.full_url, "https://example.com/v1/chat/completions")
        self.assertEqual(second.full_url, "https://backup.example/v1/responses")
        self.assertEqual(second.get_header("Authorization"), "Bearer backup-key")
        chat, responses = json.loads(first.data), json.loads(second.data)
        self.assertEqual(chat["model"], responses["model"])
        self.assertEqual(chat["reasoning_effort"], responses["reasoning"]["effort"])
        chat_content, responses_content = chat["messages"][0]["content"], responses["input"][0]["content"]
        self.assertEqual(chat_content[0]["text"], responses_content[0]["text"])
        self.assertEqual(chat_content[1]["image_url"]["url"], responses_content[1]["image_url"])

    def test_gateway_error_with_zero_retries_records_diagnostics_without_using_backup(self):
        failure = urllib.error.HTTPError(
            RELAY["baseUrl"], 503, "Unavailable", {"x-request-id": "trace-2"}, io.BytesIO(),
        )
        backup = {**RELAY, "name": "backup", "baseUrl": "https://backup.example/v1"}
        with patch.object(svg_review.stream_transport, "urlopen", side_effect=failure) as opener, \
                self.assertRaises(svg_review.SVGReviewError) as ctx:
            svg_review.review_svg(RELAY, preview_image=PREVIEW_IMAGE, model="review-model",
                                  fallback_relay=backup, retry_count=0)
        self.assertEqual(ctx.exception.attempts, 1)
        self.assertEqual(ctx.exception.relay, "score")
        self.assertEqual(ctx.exception.http_status, 503)
        self.assertEqual(ctx.exception.request_id, "trace-2")
        opener.assert_called_once()

    def test_authentication_error_stops_immediately(self):
        http_error = urllib.error.HTTPError(RELAY["baseUrl"], 401, "Unauthorized", {}, io.BytesIO())
        backup = {**RELAY, "name": "backup", "baseUrl": "https://backup.example/v1"}
        with patch.object(svg_review.stream_transport, "urlopen", side_effect=http_error) as opener, \
                self.assertRaisesRegex(svg_review.SVGReviewError, "HTTP 401"):
            svg_review.review_svg(RELAY, preview_image=PREVIEW_IMAGE,
                                  model="review-model", api_timeout_minutes=1, fallback_relay=backup)
        opener.assert_called_once()

    def test_invalid_response_structure_can_be_retried(self):
        invalid = FakeResponse({"choices": [None]})
        valid = FakeResponse(ok_payload('{"score": 84}'))
        with patch.object(svg_review.stream_transport, "urlopen", side_effect=[invalid, valid]):
            result = svg_review.review_svg(
                RELAY, preview_image=PREVIEW_IMAGE, model="review-model", prompt=CUSTOM_PROMPT,
            )
        self.assertEqual(result["score"], 84)
        self.assertEqual(result["review_attempts"], 2)

    def test_persistent_dns_tls_and_proxy_failures_are_diagnosable(self):
        secret = RELAY["apiKey"]
        tls_error = ConnectionError()
        tls_error.__cause__ = httpcore.ConnectError()
        tls_error.__cause__.__cause__ = ssl.SSLCertVerificationError(1, secret)
        for error, detail in (
            (urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, secret)), "域名解析失败"),
            (tls_error, "TLS 证书校验失败"),
            (httpcore.ProxyError(secret), "代理连接失败"),
        ):
            with self.subTest(detail=detail), patch.object(
                svg_review.stream_transport, "urlopen", side_effect=error,
            ) as opener, self.assertRaisesRegex(svg_review.SVGReviewError, detail) as ctx:
                svg_review.review_svg(RELAY, preview_image=PREVIEW_IMAGE, model="review-model")
            self.assertNotIn(secret, str(ctx.exception))
            opener.assert_called_once()


class ReviewRubricTests(unittest.TestCase):
    def review(self, scores, *, missing=None, blank_evidence=None, first_response=None):
        payload = {
            "score": 99,
            "dimensions": {
                key: {"score": score, "evidence": f"可定位的画面细节 {key}"}
                for key, score in zip(svg_review.DIMENSION_MAX, scores)
            },
            "problems": ["车架和踏板局部拥挤"],
            "comment": "画面评审",
        }
        if missing:
            payload["dimensions"].pop(missing)
        if blank_evidence:
            payload["dimensions"][blank_evidence]["evidence"] = " "

        def opener(request, timeout):
            nonlocal first_response
            content = json.loads(request.data)["messages"][0]["content"]
            self.assertEqual(content[0]["text"], svg_review.DEFAULT_REVIEW_PROMPT)
            self.assertEqual(content[1]["type"], "image_url")
            text = first_response if first_response is not None else json.dumps(payload)
            first_response = None
            return FakeResponse(ok_payload(text))

        return svg_review.review_svg(
            RELAY, preview_image=PREVIEW_IMAGE, model="review-model", urlopen=opener,
        )

    def test_total_uses_all_seven_parts_and_preserves_their_evidence(self):
        result = self.review((19, 20, 11, 9, 6, 6, 8))
        self.assertEqual(result["score"], 79)
        self.assertEqual(result["rubric_version"], svg_review.REVIEW_RUBRIC_VERSION)
        self.assertEqual(result["dimensions"]["road_and_grass"], 11)
        self.assertEqual(result["visual_evidence"]["lighthouse"], "可定位的画面细节 lighthouse")
        self.assertEqual(sum(svg_review.DIMENSION_MAX.values()), 100)

    def test_local_bicycle_and_pelican_issues_change_only_their_own_scores(self):
        polished = self.review((22, 22, 12, 10, 7, 7, 9))
        with_local_issues = self.review((16, 18, 12, 10, 7, 7, 9))
        self.assertEqual(polished["score"], 89)
        self.assertEqual(with_local_issues["score"], 79)
        self.assertEqual(with_local_issues["dimensions"]["water"], polished["dimensions"]["water"])
        self.assertEqual(with_local_issues["dimensions"]["overall"], polished["dimensions"]["overall"])

    def test_broadly_rough_render_stays_below_seventy(self):
        result = self.review((11, 14, 7, 6, 3, 4, 6), first_response='{"score":99}')
        self.assertEqual(result["score"], 51)

    def test_rejects_missing_evidence_and_invalid_scores(self):
        with self.assertRaisesRegex(svg_review.SVGReviewError, "缺少画面分项：water"):
            self.review((19, 20, 11, 9, 6, 6, 8), missing="water")
        with self.assertRaisesRegex(svg_review.SVGReviewError, "画面证据：sky_and_sun"):
            self.review((19, 20, 11, 9, 6, 6, 8), blank_evidence="sky_and_sun")
        for value in (25, -1, 20.5, True):
            with self.subTest(value=value), self.assertRaisesRegex(svg_review.SVGReviewError, "分项分数：bicycle"):
                self.review((value, 20, 11, 9, 6, 6, 8))


if __name__ == "__main__":
    unittest.main()
