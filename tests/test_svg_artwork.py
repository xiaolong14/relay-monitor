# -*- coding: utf-8 -*-
"""SVG 动画协议适配与净化测试。"""

import io
import json
import unittest
import xml.etree.ElementTree as ET
from typing import get_args

import svg_artwork
from request_options import AnthropicReasoningEffort


SAFE_ANIMATED_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">
<circle cx="5" cy="5" r="4" fill="#ff8800">
<animateTransform attributeName="transform" type="rotate" from="0 5 5" to="360 5 5" dur="2s" repeatCount="indefinite"/>
</circle></svg>"""


class FakeResponse:
    """模拟模型服务的 JSON 响应，测试过程不访问真实中转站。"""

    def __init__(self, payload):
        self.body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.body.read(size)


class FakeStreamResponse:
    """模拟 Responses API 的 SSE 或同步 JSON 响应。"""

    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self, size=-1):
        if self.offset >= len(self.payload):
            return b""
        end = self.payload.find(b"\n", self.offset)
        end = len(self.payload) if end < 0 else end + 1
        if size >= 0:
            end = min(end, self.offset + size)
        line = self.payload[self.offset:end]
        self.offset = end
        return line


class SVGArtworkTests(unittest.TestCase):
    def test_reused_wheels_and_animation_are_preserved(self):
        source = '''<svg xmlns="http://www.w3.org/2000/svg"
            xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 320 180">
          <defs><g id="wheel"><circle r="40" fill="none" stroke="#183b2f"/>
            <g><animateTransform attributeName="transform" type="rotate"
              from="0" to="360" dur="2s" repeatCount="indefinite"/>
              <line y1="-40" y2="40" stroke="#183b2f"/>
            </g></g></defs>
          <use href="#wheel" x="80" y="100"/>
          <use xlink:href="#wheel" transform="translate(240 100)"/>
        </svg>'''
        root = ET.fromstring(svg_artwork.sanitize_svg(source))
        ns = {"s": svg_artwork.SVG_NAMESPACE}
        wheels = root.findall("s:use", ns)
        self.assertEqual(len(wheels), 2)
        self.assertTrue(all(wheel.get("href") == "#wheel" for wheel in wheels))
        self.assertEqual(wheels[0].get("x"), "80")
        self.assertEqual(wheels[1].get("transform"), "translate(240 100)")
        self.assertIsNotNone(root.find("s:defs/s:g/s:g/s:animateTransform", ns))

    def test_symbol_reuse_keeps_viewport_and_removes_active_content(self):
        source = '''<svg xmlns="http://www.w3.org/2000/svg">
          <defs><symbol id="wheel" viewBox="0 0 80 80" preserveAspectRatio="xMidYMid meet">
            <script>alert(1)</script><circle cx="40" cy="40" r="35" onclick="alert(2)">
              <animate attributeName="opacity" values="0.8;1;0.8" dur="2s"/>
            </circle></symbol></defs>
          <use href="#wheel" width="80" height="80"/>
          <use href="https://example.invalid/wheel.svg#wheel"/>
          <use href="data:image/svg+xml,unsafe"/>
          <use href="javascript:alert(3)"/>
        </svg>'''
        cleaned = svg_artwork.sanitize_svg(source)
        root = ET.fromstring(cleaned)
        ns = {"s": svg_artwork.SVG_NAMESPACE}
        symbol = root.find("s:defs/s:symbol", ns)
        self.assertEqual(symbol.get("viewBox"), "0 0 80 80")
        self.assertEqual(symbol.get("preserveAspectRatio"), "xMidYMid meet")
        self.assertEqual([n.get("href") for n in root.findall("s:use", ns)],
                         ["#wheel", None, None, None])
        for unsafe in ("<script", "onclick", "https://", "data:", "javascript:"):
            self.assertNotIn(unsafe, cleaned)

    def test_svg_keeps_animation_and_removes_active_content(self):
        source = """<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">
        <script>alert(2)</script><g onclick="alert(3)" style="color:red">
        <image href="https://example.invalid/pixel.svg"/>
        <circle cx="1" cy="1" r="1" fill="url(https://example.invalid/x)"/>
        <animate attributeName="opacity" from="0" to="1" dur="2s" repeatCount="indefinite"/>
        </g></svg>"""

        cleaned = svg_artwork.sanitize_svg(source)

        self.assertIn("<animate", cleaned)
        self.assertNotIn("<script", cleaned)
        self.assertNotIn("<image", cleaned)
        self.assertNotIn("onload", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertNotIn("style=", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertIn('viewBox="0 0 800 420"', cleaned)

    def test_static_svg_is_rejected(self):
        with self.assertRaisesRegex(svg_artwork.SVGGenerationError, "没有可播放动画"):
            svg_artwork.sanitize_svg('<svg><circle r="2"/></svg>')

    def test_openai_compatible_request(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeResponse({"choices": [{"message": {"content": SAFE_ANIMATED_SVG}}]})

        relay = {
            "protocol": "openai", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "gpt-5.4",
        }
        result = svg_artwork.generate_svg(relay, urlopen=fake_urlopen)

        self.assertEqual(captured["url"], "https://relay.invalid/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        self.assertEqual(captured["timeout"], 900)
        self.assertEqual(captured["body"]["reasoning_effort"], "xhigh")
        self.assertEqual(captured["body"]["max_completion_tokens"], 32768)
        self.assertNotIn("max_tokens", captured["body"])
        self.assertNotIn("temperature", captured["body"])
        self.assertIn("鹈鹕", result)
        self.assertIn("<animateTransform", result)

    def test_anthropic_request(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["key"] = request.get_header("X-api-key")
            captured["body"] = json.loads(request.data)
            return FakeResponse({"content": [{"type": "text", "text": SAFE_ANIMATED_SVG}]})

        relay = {
            "protocol": "anthropic", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "claude-sonnet-5-5",
        }
        result = svg_artwork.generate_svg(relay, urlopen=fake_urlopen)

        self.assertEqual(captured["url"], "https://relay.invalid/v1/messages")
        self.assertEqual(captured["key"], "test-key")
        self.assertNotIn("reasoning_effort", captured["body"])
        self.assertEqual(captured["body"]["thinking"], {"type": "adaptive"})
        self.assertEqual(captured["body"]["output_config"], {"effort": "xhigh"})
        self.assertNotIn("temperature", captured["body"])
        self.assertIn("<animateTransform", result)

    def test_openai_responses_uses_native_streaming_format(self):
        captured = {}
        midpoint = len(SAFE_ANIMATED_SVG) // 2
        events = [
            {"type": "response.output_text.delta", "delta": SAFE_ANIMATED_SVG[:midpoint]},
            {"type": "response.output_text.delta", "delta": SAFE_ANIMATED_SVG[midpoint:]},
            {"type": "response.completed", "response": {
                "status": "completed", "usage": {"input_tokens": 12, "output_tokens": 34},
            }},
        ]
        stream = "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        ) + "data: [DONE]\n\n"

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["accept"] = request.get_header("Accept")
            captured["body"] = json.loads(request.data)
            return FakeStreamResponse(stream)

        result = svg_artwork.generate_svg({
            "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "gpt-5.4",
        }, urlopen=fake_urlopen, reasoning_effort="high")

        self.assertEqual(captured["url"], "https://relay.invalid/v1/responses")
        self.assertEqual(captured["accept"], "text/event-stream, application/json")
        self.assertEqual(captured["body"]["input"], svg_artwork.USER_PROMPT)
        self.assertEqual(captured["body"]["reasoning"], {"effort": "high"})
        self.assertEqual(captured["body"]["max_output_tokens"], 32768)
        self.assertTrue(captured["body"]["stream"])
        self.assertNotIn("messages", captured["body"])
        self.assertNotIn("reasoning_effort", captured["body"])
        self.assertIn("<animateTransform", result)

    def test_openai_responses_accepts_non_streaming_json_fallback(self):
        payload = json.dumps({
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": SAFE_ANIMATED_SVG},
            ]}],
        })

        result = svg_artwork.generate_svg({
            "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "test-model",
        }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(payload))

        self.assertIn("<animateTransform", result)

    def test_openai_responses_accepts_complete_svg_from_incomplete_json_fallback(self):
        payload = json.dumps({
            "status": "incomplete",
            "incomplete_details": {"reason": "upstream_interrupted"},
            "output_text": SAFE_ANIMATED_SVG,
        })

        result = svg_artwork.generate_svg({
            "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "test-model",
        }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(payload))

        self.assertIn("<animateTransform", result)

    def test_openai_responses_accepts_complete_svg_from_incomplete_stream_delta(self):
        events = [
            {"type": "response.output_text.delta", "delta": SAFE_ANIMATED_SVG},
            {"type": "response.incomplete", "response": {
                "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            }},
        ]
        stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events)

        result = svg_artwork.generate_svg({
            "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "test-model",
        }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(stream))

        self.assertIn("<animateTransform", result)

    def test_openai_responses_uses_final_response_when_delta_is_truncated(self):
        events = [
            {"type": "response.output_text.delta", "delta": SAFE_ANIMATED_SVG[:-8]},
            {"type": "response.incomplete", "response": {
                "status": "incomplete", "incomplete_details": {"reason": "length"},
                "output_text": SAFE_ANIMATED_SVG,
            }},
        ]
        stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events)

        result = svg_artwork.generate_svg({
            "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "test-model",
        }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(stream))

        self.assertIn("<animateTransform", result)

    def test_openai_responses_uses_done_text_when_delta_is_truncated(self):
        events = [
            {"type": "response.output_text.delta", "delta": SAFE_ANIMATED_SVG[:-8]},
            {"type": "response.output_text.done", "text": SAFE_ANIMATED_SVG},
            {"type": "response.incomplete", "response": {
                "status": "incomplete", "incomplete_details": {"reason": "length"},
            }},
        ]
        stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events)

        result = svg_artwork.generate_svg({
            "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "test-model",
        }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(stream))

        self.assertIn("<animateTransform", result)

    def test_openai_responses_accepts_complete_svg_from_incomplete_final_output(self):
        event = {"type": "response.incomplete", "response": {
            "status": "incomplete", "incomplete_details": {"reason": "upstream_interrupted"},
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": SAFE_ANIMATED_SVG},
            ]}],
        }}
        stream = f"data: {json.dumps(event)}\n\n"

        result = svg_artwork.generate_svg({
            "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "test-model",
        }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(stream))

        self.assertIn("<animateTransform", result)

    def test_openai_responses_rejects_truncated_incomplete_stream(self):
        delta = {"type": "response.output_text.delta", "delta": SAFE_ANIMATED_SVG[:-6]}
        event = {"type": "response.incomplete", "response": {
            "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
        }}
        stream = f"data: {json.dumps(delta)}\n\ndata: {json.dumps(event)}\n\ndata: [DONE]\n\n"

        with self.assertRaisesRegex(svg_artwork.SVGGenerationError, "长度上限"):
            svg_artwork.generate_svg({
                "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
                "apiKey": "test-key", "model": "test-model",
            }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(stream))

    def test_openai_responses_reports_incomplete_reason_for_invalid_svg(self):
        event = {"type": "response.incomplete", "response": {
            "status": "incomplete", "incomplete_details": {"reason": "upstream_interrupted"},
            "output_text": "<svg><g>",
        }}
        stream = f"data: {json.dumps(event)}\n\n"

        with self.assertRaisesRegex(svg_artwork.SVGGenerationError, "upstream_interrupted"):
            svg_artwork.generate_svg({
                "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
                "apiKey": "test-key", "model": "test-model",
            }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(stream))

    def test_openai_responses_maps_length_reason_and_reports_svg_validation_failure(self):
        event = {"type": "response.incomplete", "response": {
            "status": "incomplete", "incomplete_details": {"reason": "length"},
            "output_text": "<svg><g>",
        }}
        stream = f"data: {json.dumps(event)}\n\n"

        with self.assertRaisesRegex(svg_artwork.SVGGenerationError, "达到长度上限.*SVG 根标签未闭合"):
            svg_artwork.generate_svg({
                "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
                "apiKey": "test-key", "model": "test-model",
            }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(stream))

    def test_reasoning_only_is_not_mislabeled_as_rejected_svg_format(self):
        summary = "我们需要生成 SVG。草拟示意：<svg ...> ... </svg>。接下来画帆船。"
        events = [
            {"type": "response.reasoning_summary_text.delta", "delta": summary[:20]},
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
        stream = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)

        with self.assertRaises(svg_artwork.SVGGenerationError) as caught:
            svg_artwork.generate_svg({
                "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
                "apiKey": "test-key", "model": "test-model",
            }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(stream), reasoning_effort="max")
        self.assertIn("输出 Token 全部消耗在推理中", str(caught.exception))
        self.assertIn("推理摘要=", str(caught.exception))
        self.assertNotIn("输出校验失败", str(caught.exception))
        self.assertNotIn("长度上限", str(caught.exception))

    def test_complete_animated_svg_embedded_in_reasoning_summary_is_salvaged(self):
        summary = f"模型草拟的完整内容：\n```svg\n{SAFE_ANIMATED_SVG}\n```"
        events = [
            {"type": "response.reasoning_summary_text.delta", "delta": summary},
            {"type": "response.reasoning_summary_text.done", "text": summary},
            {"type": "response.incomplete", "response": {
                "status": "incomplete", "incomplete_details": {"reason": "length"},
                "usage": {"output_tokens": 32768, "output_tokens_details": {
                    "reasoning_tokens": 32768,
                }},
            }},
        ]
        stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events)

        result = svg_artwork.generate_svg({
            "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "test-model",
        }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(stream), reasoning_effort="max")

        self.assertIn("<animateTransform", result)

    def test_summary_fallback_accepts_snapshots_and_preserves_multiple_parts(self):
        summary = "示例根标签：<svg ...>\n完整动画：" + SAFE_ANIMATED_SVG
        item = {"type": "reasoning", "summary": [{"type": "summary_text", "text": summary}]}
        final = {"status": "incomplete", "incomplete_details": {"reason": "length"},
                 "output": [item]}
        event_groups = {
            "最终对象": [{"type": "response.incomplete", "response": final}],
            "输出项快照": [{"type": "response.output_item.done", "item": item}],
            "摘要段快照": [{"type": "response.reasoning_summary_part.done",
                            "part": item["summary"][0]}],
            "多输出项摘要": [
                {"type": "response.reasoning_summary_text.delta", "output_index": 0,
                 "summary_index": 0, "delta": SAFE_ANIMATED_SVG},
                {"type": "response.reasoning_summary_text.done", "output_index": 1,
                 "summary_index": 0, "text": "另一个输出项，不能覆盖前面的完整动画"},
            ],
            "多摘要段": [
                {"type": "response.reasoning_summary_text.done", "output_index": 0,
                 "summary_index": 0, "text": SAFE_ANIMATED_SVG},
                {"type": "response.reasoning_summary_text.done", "output_index": 0,
                 "summary_index": 1, "text": "后续说明，不能覆盖前面的完整动画"},
            ],
        }
        bodies = {name: "".join(f"data: {json.dumps(event)}\n\n" for event in events)
                  for name, events in event_groups.items()}
        bodies["同步 JSON"] = json.dumps(final)
        for name, body in bodies.items():
            with self.subTest(source=name):
                result = svg_artwork.generate_svg({
                    "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
                    "apiKey": "test-key", "model": "test-model",
                }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(body))
                self.assertIn("<animateTransform", result)

    def test_output_item_snapshot_is_accepted_without_text_delta(self):
        event = {"type": "response.output_item.done", "item": {
            "type": "message", "content": [{"type": "output_text", "text": SAFE_ANIMATED_SVG}],
        }}
        result = svg_artwork.generate_svg({
            "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "test-model",
        }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(f"data: {json.dumps(event)}\n\n"))
        self.assertIn("<animateTransform", result)

    def test_standard_output_has_priority_over_summary_svg(self):
        alternate = SAFE_ANIMATED_SVG.replace("#ff8800", "#00ff00")
        payload = {"status": "completed", "output_text": SAFE_ANIMATED_SVG, "output": [{
            "type": "reasoning", "summary": [{"type": "summary_text", "text": alternate}],
        }]}
        result = svg_artwork.generate_svg({
            "protocol": "openai-responses", "baseUrl": "https://relay.invalid/v1",
            "apiKey": "test-key", "model": "test-model",
        }, urlopen=lambda *_args, **_kwargs: FakeStreamResponse(json.dumps(payload)))
        self.assertIn("#ff8800", result)
        self.assertNotIn("#00ff00", result)

    def test_openai_reasoning_override_and_model_default(self):
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "default"):
            with self.subTest(effort=effort):
                captured = {}

                def fake_urlopen(request, timeout):
                    captured.update(json.loads(request.data))
                    return FakeResponse({"choices": [{"message": {"content": SAFE_ANIMATED_SVG}}]})

                svg_artwork.generate_svg({
                    "baseUrl": "https://relay.invalid/v1", "apiKey": "test-key", "model": "gpt-5.4",
                }, urlopen=fake_urlopen, reasoning_effort=effort)
                if effort in ("default", "none"):
                    # OpenAI 没有 none 等级，省略 reasoning_effort 才能被上游接受。
                    self.assertNotIn("reasoning_effort", captured)
                else:
                    self.assertEqual(captured["reasoning_effort"], effort)
                self.assertEqual(captured["max_completion_tokens"], 32768)
                self.assertNotIn("temperature", captured)

    def test_anthropic_channel_override_uses_native_parameters(self):
        for effort in get_args(AnthropicReasoningEffort):
            with self.subTest(effort=effort):
                captured = {}

                def fake_urlopen(request, timeout):
                    captured.update(json.loads(request.data))
                    return FakeResponse({"content": [{"type": "text", "text": SAFE_ANIMATED_SVG}]})

                svg_artwork.generate_svg({
                    "protocol": "anthropic", "baseUrl": "https://relay.invalid/v1", "apiKey": "test-key",
                    "model": "claude-sonnet-5-5", "reasoning_effort": effort,
                }, urlopen=fake_urlopen, reasoning_effort="medium")
                self.assertNotIn("reasoning_effort", captured)
                if effort == "default":
                    self.assertNotIn("thinking", captured)
                    self.assertNotIn("output_config", captured)
                elif effort == "none":
                    self.assertEqual(captured["thinking"], {"type": "disabled"})
                    self.assertNotIn("output_config", captured)
                else:
                    self.assertEqual(captured["thinking"], {"type": "adaptive"})
                    self.assertEqual(captured["output_config"], {"effort": effort})


if __name__ == "__main__":
    unittest.main()
