# -*- coding: utf-8 -*-
"""通过已配置的 AI 中转站生成并净化可播放的 SVG 动画。"""

import json
import http.client
import io
import logging
import re
import time
import traceback
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from request_options import (
    DEFAULT_MAX_INPUT_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS, token_budget,
    DEFAULT_API_TIMEOUT_MINUTES, DEFAULT_REASONING_EFFORT, api_timeout_seconds,
    anthropic_message_params, degrade_request_body, endpoint_url,
    openai_chat_params, responses_params, resolve_reasoning_effort,
)


SVG_NAMESPACE = "http://www.w3.org/2000/svg"
# 为长输出保留足够的接收空间；字符额度按 Token 预算伸缩，JSON 转义单独预留。
SVG_CHARS_PER_TOKEN = 128
MAX_SVG_CHARS = DEFAULT_MAX_OUTPUT_TOKENS * SVG_CHARS_PER_TOKEN
logger = logging.getLogger(__name__)
ANIMATION_TAGS = {"animate", "animateTransform", "set"}
ALLOWED_TAGS = {
    "svg", "g", "path", "circle", "ellipse", "line", "polyline", "polygon",
    "rect", "text", "tspan", "defs", "use", "symbol", "linearGradient", "radialGradient",
    "stop", "animate", "animateTransform", "set", "title", "desc",
}
ALLOWED_ATTRIBUTES = {
    "id", "viewBox", "preserveAspectRatio", "width", "height", "x", "y", "x1", "x2", "y1", "y2",
    "cx", "cy", "r", "rx", "ry", "d", "points", "fill", "fill-opacity",
    "fill-rule", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-dasharray", "stroke-dashoffset", "stroke-opacity", "opacity", "transform",
    "offset", "stop-color", "stop-opacity", "gradientUnits", "gradientTransform",
    "font-size", "font-family", "font-weight", "text-anchor", "dominant-baseline",
    "dx", "dy", "href", "attributeName", "attributeType", "from", "to", "values",
    "type",
    "keyTimes", "keySplines", "dur", "repeatCount", "repeatDur", "calcMode",
    "additive", "accumulate", "begin", "end", "restart", "by", "role", "aria-label",
}

USER_PROMPT = """生成纯 SVG 2D 静态画面代码，内容是：角色设计描述词
一只悠闲、温和、略带憨态的拟人化鹈鹕，采用二维矢量插画风格，完整侧身朝向画面右侧，骑着一辆复古城市自行车。整体轮廓圆润、简洁，具有自然随性的绘本气质，像一位独自在海边享受好天气的旅行者。
鹈鹕拥有饱满的椭圆形躯干，腹部柔软圆鼓，背部与臀部形成连续流畅的弧线。身体覆盖温暖的奶油白、象牙白羽毛，腹部和身体下缘带有极轻的浅灰绿色阴影。尾部由几片短小的楔形羽毛组成，向后轻轻展开。羽毛以大面积色块表现，只在翅膀和尾部保留少量细线，轮廓清楚，细节克制。
颈部修长，从躯干前上方自然伸出，形成柔和的 S 形弯曲。颈根略宽，向头部逐渐收细，头部小巧圆润。头颈保持略微前探的姿态，呈现轻松、专注地向前骑行的状态。
面部最醒目的特征是向右伸出的长嘴：嘴部长度明显超过头部，形状狭长、扁平，上喙近似一条微微向下倾斜的长楔形，使用柔和的浅金黄色；嘴尖带少量焦糖橙色。下喙连接宽大、柔软的喉囊，形成向下垂落的弧形轮廓，喉囊从金黄过渡到暖橙色，具有轻微的兜袋感。长嘴与喉囊共同构成清晰的鹈鹕识别特征。
眼睛很小，呈深墨绿色或近黑色，外围有一圈淡金黄色眼周，眼内保留一个细小的白色高光。眼睛上方用一条轻微上挑的短弧线表现神态，整体表情平静、友善，带一点认真兜风的可爱感。
头上戴一顶橄榄绿色的软布骑行帽，帽冠低矮圆润，帽顶有一条简化的拼接线，帽檐向前伸出，轻轻覆盖额头。帽身为柔和的灰橄榄绿，帽檐和外轮廓使用更深的森林绿，局部点缀浅绿色短线。
脖子上系着一条暖橘色围巾，围巾贴合颈部，在侧后方形成一个小结，两条长短不同的围巾尾巴向画面左侧飘扬。前层围巾偏明亮的杏橘色，后层偏深赭橘色，用简单的弯曲色块表现翻折与厚度。围巾是角色最鲜明的暖色点缀。
双翼保留鸟类羽翼的形态：靠近观众的一侧翅膀从身体中部向前伸出，略微弯曲，翼尖自然搭在车把上；翅膀主体为奶油白，用少量浅灰绿色线条表现羽毛层次。身体后侧的羽翼使用柔和的灰橄榄色，呈叠放的叶片状羽毛，增加层次。
两条腿细长，呈暖金黄色，关节弯曲自然。脚掌是宽扁的蹼足，脚趾由几条短线概括，稳稳放在自行车踏板上。整体姿态保留鸟类的身体结构，同时具备协调、可信的骑行动作。
配套骑行与动画描述词
鹈鹕坐在一辆深森林绿色的复古自行车上，车架简洁纤细，带有清晰的三角结构、弧形挡泥板、棕色车座和棕色握把。车轮较大，轮胎为深墨绿色，内侧有一圈奶油色轮边，细密的金属辐条围绕轮轴均匀排列。后座行李架上绑着一个小巧的焦糖棕色旅行包，带浅米色绑带与金属搭扣。
骑行动作匀速、轻快，两只蹼足交替画圆踩踏，前后踏板保持半圈的相位差。大腿、小腿围绕关节连续弯曲，脚掌始终贴合踏板。车轮向前滚动，牙盘、曲柄和链条同步运转。身体随着踩踏产生幅度很小的上下起伏，头部与长嘴保持相对稳定，围巾尾端形成柔软、连续的波浪摆动。整体动作舒展、循环流畅，传递“慢慢骑，也很快乐”的感觉。
画风与配色描述词
采用清爽的 SVG 二维矢量绘本风格，以平涂色块为主，辅以少量柔和渐变。线条细而稳定，端点圆润，轮廓带有轻微手绘感。使用低饱和、偏暖的自然配色：奶油白羽毛、橄榄绿帽子、森林绿自行车、杏橘色围巾、金黄色长嘴与蹼足。角色轮廓清晰，色彩温柔，兼具复古旅行海报和生活方式插画的气质。
背景是一条安静的海岸公路，远处有鼠尾草绿色的起伏山丘、淡青绿色海面、小帆船与条纹灯塔，天空悬着一轮柔和的杏黄色太阳。几朵奶油白云缓慢漂移，近处草丛与道路标线向左移动，形成轻柔的景深与行进感。画面留白充足，氛围晴朗、安静、自由，像一个没有明确目的地的海边下午。

只返回完整 SVG 源码"""


class SVGGenerationError(Exception):
    """可以安全展示给用户的 SVG 生成错误。"""

    def __init__(self, message, raw_response=None, response_metadata=None):
        super().__init__(message)
        self.raw_response = raw_response
        self.response_metadata = response_metadata or {}


class _ResponseCapture:
    """在解析之前保留接收字节，断流或读取异常也不会丢掉已接收内容。"""

    def __init__(self, response):
        self.response = response
        self.parts = []
        self.line_buffer = bytearray()
        self.received = 0
        self.complete = False
        self.stop_reason = "读取尚未结束"

    def _read(self, method, size):
        try:
            chunk = getattr(self.response, method)(size)
        except Exception as exc:
            # HTTP 库可能把最后一段正文放在异常上，而不是作为读取结果返回。
            if isinstance(exc, http.client.IncompleteRead) and exc.partial:
                self.parts.append(exc.partial)
                self.received += len(exc.partial)
            self.stop_reason = f"读取异常：{type(exc).__name__}"
            raise
        if chunk:
            self.parts.append(chunk)
            self.received += len(chunk)
        else:
            self.complete = True
            self.stop_reason = "响应正文已读至 EOF"
        return chunk

    def readline(self, size=-1):
        if not hasattr(self.response, "read1"):
            return self._read("readline", size)
        # 按网络可用字节先取证再分行；直接调用 HTTPResponse.readline 会在
        # 一行中途超时时丢掉内部缓冲中尚未返回的半行内容。
        while True:
            end = self.line_buffer.find(b"\n") + 1
            if end or (size >= 0 and len(self.line_buffer) >= size) or self.complete:
                end = end or len(self.line_buffer)
                if size >= 0:
                    end = min(end, size)
                line = bytes(self.line_buffer[:end])
                del self.line_buffer[:end]
                return line
            chunk_size = 64 * 1024 if size < 0 else min(64 * 1024, size - len(self.line_buffer))
            chunk = self._read("read1", chunk_size)
            if chunk:
                self.line_buffer.extend(chunk)


    def read_body(self, limit):
        """分段接收同步 JSON 与 HTTP 错误正文，保留超时前的每一段。"""
        received = 0
        while received <= limit:
            method = "read1" if hasattr(self.response, "read1") else "read"
            chunk = self._read(method, min(64 * 1024, limit + 1 - received))
            if not chunk:
                break
            received += len(chunk)
        if received > limit:
            self.stop_reason = "达到本地响应大小保护上限"
        return self.raw_response

    @property
    def raw_response(self):
        return b"".join(self.parts)

    @property
    def metadata(self):
        response_metadata = _capture_response_metadata(self.response)
        complete = self.complete
        stop_reason = self.stop_reason
        length = response_metadata.get("response_headers", {}).get("content-length")
        if complete and length and length.isdigit() and self.received != int(length):
            complete = False
            stop_reason = f"响应提前结束：声明 {length} 字节，实际收到 {self.received} 字节"
        return {
            **response_metadata,
            "response_body_complete": complete,
            "response_capture_end": stop_reason,
        }


def _redact_log_bytes(content, secrets):
    """上游错误可能回显凭证；仅替换已知密钥，保留其他完整排障内容。"""
    redacted = False
    for secret in secrets:
        if not secret:
            continue
        for value in {str(secret), json.dumps(str(secret))[1:-1], urllib.parse.quote(str(secret), safe="")}:
            encoded = value.encode("utf-8")
            if encoded in content:
                content = content.replace(encoded, b"[REDACTED]")
                redacted = True
    return content, redacted


def _capture_response_metadata(response):
    """仅保留可用于排障的响应状态和安全响应头，不保存认证信息。"""
    if response is None:
        return {}
    metadata = {}
    status = getattr(response, "status", None) or getattr(response, "code", None)
    if status is not None:
        metadata["http_status"] = status
    headers = getattr(response, "headers", None)
    if headers is None and hasattr(response, "info"):
        try:
            headers = response.info()
        except Exception:
            headers = None
    if headers is not None:
        safe_headers = {}
        for name in ("content-type", "content-length", "transfer-encoding", "date", "retry-after", "x-request-id",
                     "request-id", "cf-ray", "server-timing"):
            try:
                value = headers.get(name)
            except Exception:
                value = None
            if value is not None:
                safe_headers[name] = str(value)[:500]
        if safe_headers:
            metadata["response_headers"] = safe_headers
    return metadata


def _write_failure_log(path, raw_response, metadata, secrets=()):
    """以原始响应字节保存失败证据；元信息与响应体之间使用固定分隔符。"""
    if not path:
        return None
    path = Path(path)
    body = raw_response or b""
    body, body_redacted = _redact_log_bytes(body, secrets)
    metadata_redacted = False

    def redact_values(value):
        nonlocal metadata_redacted
        if isinstance(value, dict):
            return {key: redact_values(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact_values(item) for item in value]
        if isinstance(value, str):
            cleaned, changed = _redact_log_bytes(value.encode("utf-8"), secrets)
            metadata_redacted |= changed
            return cleaned.decode("utf-8")
        return value

    safe_metadata = redact_values(metadata)
    details = {
        **safe_metadata,
        "captured_response_bytes": len(raw_response or b""),
        "saved_response_bytes": len(body),
        "response_body_complete": metadata.get("response_body_complete", False),
        "credentials_redacted": body_redacted or metadata_redacted,
    }
    header = (
        "=== Relay Monitor SVG failure log ===\n"
        + json.dumps(details, ensure_ascii=False, indent=2, default=str)
        + "\n=== Raw upstream response body (known credentials redacted only) ===\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(path.name + ".tmp")
    temporary_path.write_bytes(header + body)
    temporary_path.replace(path)
    return path


def record_failure(error, *, log_path, metadata, raw_response=None, cause=None, secrets=()):
    """落盘失败诊断并返回带日志位置的可展示异常。"""
    original = cause if cause is not None else error
    metadata = {
        **metadata,
        "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "error_type": type(original).__name__,
        "error": str(error),
        "traceback": "".join(traceback.format_exception(
            type(original), original, original.__traceback__,
        )),
    }
    metadata.update(getattr(error, "response_metadata", {}) or {})
    response_body = raw_response
    if response_body is None:
        response_body = getattr(error, "raw_response", None)
    safe_message = _redact_log_bytes(str(error).encode("utf-8"), secrets)[0].decode("utf-8")
    try:
        saved_path = _write_failure_log(log_path, response_body, metadata, secrets)
    except OSError as write_error:
        logger.error("SVG 失败日志落盘失败：记录=%s，异常类型=%s", metadata.get("generation_id"), type(write_error).__name__)
        return SVGGenerationError(f"{safe_message}；失败日志写入磁盘失败，请检查数据目录权限", response_body, metadata)
    if saved_path:
        logger.error("SVG 失败日志已保存：记录=%s，路径=%s，响应字节=%s",
                     metadata.get("generation_id", "未知"), saved_path, len(response_body or b""))
        return SVGGenerationError(f"{safe_message}；失败日志已保存，可在生成记录中下载", response_body, metadata)
    return SVGGenerationError(safe_message, response_body, metadata)


def _incomplete_error(reason, stream=False, validation_error=None, diagnostics=None,
                      reasoning_tokens=None, output_tokens=None,
                      reasoning_summary_chars=0):
    """把 Responses 未完成原因和安全校验结果转换成稳定错误。"""
    reasoning_exhausted = (
        output_tokens is not None and reasoning_tokens is not None
        and output_tokens > 0 and reasoning_tokens >= output_tokens
    )
    if reasoning_exhausted:
        message = (
            "未收到可用 SVG；上游报告输出 Token 全部消耗在推理中；"
            "请检查渠道生成结果、输出预算与 Responses 字段映射"
        )
    elif reasoning_summary_chars and not validation_error:
        message = "上游只返回了推理摘要，没有返回 SVG 输出正文；请检查模型输出与渠道 Responses 映射"
    elif not validation_error:
        prefix = "OpenAI Responses 流" if stream else "OpenAI Responses"
        detail = f"（原因：{str(reason)[:100]}）" if reason else ""
        message = f"{prefix}未返回 SVG 输出正文{detail}，请检查上游响应"
    elif reason in {"max_output_tokens", "max_tokens", "length"}:
        message = (
            "模型输出达到长度上限，动画未完整生成；请提高设置中的最大输出 Token，或检查渠道模型上限"
        )
    else:
        prefix = "OpenAI Responses 流" if stream else "OpenAI Responses"
        detail = f"（原因：{str(reason)[:100]}）" if reason else ""
        message = f"{prefix}未完整生成{detail}，请重试"
    if validation_error:
        message += f"；输出校验失败：{validation_error}"
    if diagnostics:
        message += f"；诊断：{diagnostics}"
    return SVGGenerationError(message)


def _responses_output_text(payload):
    """从最终 Responses 对象提取文本，不依赖流式增量是否完整。"""
    if not isinstance(payload, dict):
        return ""
    text = payload.get("output_text")
    if isinstance(text, str) and text:
        return text
    parts = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if (isinstance(content, dict) and content.get("type") == "output_text"
                    and isinstance(content.get("text"), str)):
                parts.append(content["text"])
    return "".join(parts)


def _responses_reasoning_summary(payload):
    """兼容渠道把成品映射到摘要的情况，最终对象与流式事件共用回退。"""
    if not isinstance(payload, dict):
        return ""
    parts = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        for part in item.get("summary") or []:
            if (isinstance(part, dict) and part.get("type") == "summary_text"
                    and isinstance(part.get("text"), str)):
                parts.append(part["text"])
    return "\n".join(parts)


def _text_from_response(payload, protocol, allow_incomplete=False):
    """读取 OpenAI 兼容或 Anthropic 消息文本，屏蔽未知响应结构。"""
    if protocol == "anthropic":
        stop_reason = payload.get("stop_reason")
        blocks = payload.get("content", [])
        text = "\n".join(
            block.get("text", "") for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
    elif protocol == "openai-responses":
        error = payload.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            raise SVGGenerationError(
                f"OpenAI Responses 返回错误{f'（{code}）' if code else ''}"
            )
        incomplete = payload.get("incomplete_details") or {}
        stop_reason = incomplete.get("reason") if payload.get("status") == "incomplete" else None
        text = _responses_output_text(payload)
    else:
        choices = payload.get("choices", [])
        stop_reason = choices[0].get("finish_reason") if choices else None
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "")
        if isinstance(content, list):
            text = "\n".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        else:
            text = content
    # 区分渠道截断与格式不符，不记录响应正文或渠道密钥。
    if stop_reason in {"length", "max_tokens", "max_output_tokens"} and not allow_incomplete:
        logger.warning("SVG 生成被渠道截断：协议=%s，结束原因=%s", protocol, stop_reason)
        raise SVGGenerationError("模型输出达到长度上限，动画未完整生成；请提高设置中的最大输出 Token，或检查渠道模型上限")
    if (protocol == "openai-responses" and payload.get("status") == "incomplete"
            and not allow_incomplete):
        raise _incomplete_error(stop_reason)
    if not isinstance(text, str) or not text.strip():
        raise SVGGenerationError("模型没有返回 SVG 内容")
    return text.strip()


def _read_openai_responses(response, max_response_bytes, max_chars):
    """读取 Responses SSE；渠道忽略 stream 参数并返回 JSON 时自动回退解析。"""
    event_lines = []
    raw_parts = []
    text_parts = []
    done_text_parts = []
    reasoning_summaries = {}
    done_items = {}
    text_chars = 0
    final_response = None
    received = 0
    saw_sse = False
    final_event_type = ""
    parse_error = None

    def process_event(lines):
        nonlocal final_response, final_event_type, text_chars
        if not lines:
            return
        event_name = ""
        data_lines = []
        for line in lines:
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        data = "\n".join(data_lines)
        if not data or data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise SVGGenerationError("OpenAI Responses 流包含无效 JSON 事件") from exc
        event_type = payload.get("type") or event_name
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
                text_chars += len(delta)
                if text_chars > max_chars:
                    raise SVGGenerationError("SVG 内容过大，请重试")
        elif event_type == "response.output_text.done":
            text = payload.get("text")
            if isinstance(text, str):
                done_text_parts.append(text)
                if sum(map(len, done_text_parts)) > max_chars:
                    raise SVGGenerationError("SVG 内容过大，请重试")
        elif event_type == "response.reasoning_summary_text.delta":
            # 按输出项和摘要段分别拼接，done 只覆盖对应段，避免丢掉其他段的成品。
            delta = payload.get("delta")
            if isinstance(delta, str):
                key = (payload.get("output_index", 0), payload.get("summary_index", 0))
                reasoning_summaries.setdefault(key, []).append(delta)
        elif event_type == "response.reasoning_summary_text.done":
            summary = payload.get("text")
            if isinstance(summary, str):
                key = (payload.get("output_index", 0), payload.get("summary_index", 0))
                reasoning_summaries[key] = [summary]
        elif event_type == "response.reasoning_summary_part.done":
            part = payload.get("part") or {}
            if part.get("type") == "summary_text" and isinstance(part.get("text"), str):
                key = (payload.get("output_index", 0), payload.get("summary_index", 0))
                reasoning_summaries[key] = [part["text"]]
        elif event_type == "response.output_item.done":
            item = payload.get("item")
            if isinstance(item, dict):
                done_items[payload.get("output_index", 0)] = item
        elif event_type in {"response.completed", "response.incomplete"}:
            final_response = payload.get("response") or payload
            final_event_type = event_type
        elif event_type in {"response.failed", "error"}:
            error = payload.get("error") or (payload.get("response") or {}).get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            raise SVGGenerationError(
                f"OpenAI Responses 流返回错误{f'（{code}）' if code else ''}"
            )

    while True:
        raw_line = response.readline(max_response_bytes - received + 1)
        if not raw_line:
            break
        received += len(raw_line)
        if received > max_response_bytes:
            response.stop_reason = "达到本地响应大小保护上限"
            raise SVGGenerationError("渠道响应超过大小限制")
        if parse_error is not None:
            continue
        try:
            line = raw_line.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            parse_error = SVGGenerationError("渠道响应格式无效")
            parse_error.__cause__ = exc
            continue
        if line.startswith(("event:", "data:", ":")):
            saw_sse = True
            raw_parts.clear()
        if not saw_sse:
            raw_parts.append(raw_line)
        if not line:
            if saw_sse:
                try:
                    process_event(event_lines)
                except Exception as exc:
                    # 解析已失败仍接收剩余正文，避免丢掉上游随后返回的诊断事件。
                    parse_error = exc
            event_lines = []
        else:
            event_lines.append(line)
    if parse_error is not None:
        raise parse_error
    if event_lines and saw_sse:
        process_event(event_lines)

    if not saw_sse:
        try:
            payload = json.loads(b"".join(raw_parts).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SVGGenerationError("渠道响应格式无效") from exc
        status = payload.get("status")
        incomplete = (payload.get("incomplete_details") or {}).get("reason")
        if payload.get("error"):
            # 沿用同步协议的显式错误处理，不能用摘要掩盖上游错误。
            _text_from_response(payload, "openai-responses", allow_incomplete=True)
        text = _responses_output_text(payload)
        summary = _responses_reasoning_summary(payload)
        logger.log(
            logging.WARNING if status == "incomplete" else logging.INFO,
            "OpenAI Responses 同步响应结束：状态=%s，未完成原因=%s，接收字节=%s，文本字符=%s",
            status or "未知", incomplete or "无", received, len(text),
        )
        return _validate_responses_candidates(
            [("同步响应正文", text)] if text else [], max_chars, status, incomplete,
            stream=False, received=received,
            output_tokens=(payload.get("usage") or {}).get("output_tokens"),
            reasoning_tokens=(((payload.get("usage") or {}).get("output_tokens_details") or {})
                              .get("reasoning_tokens")),
            reasoning_summary_chars=len(summary),
            reasoning_summary_candidate=summary,
        )

    status = final_response.get("status") if final_response else None
    incomplete = ((final_response.get("incomplete_details") or {}).get("reason")
                  if final_response else None)
    logger.log(
        logging.WARNING if status == "incomplete" else logging.INFO,
        "OpenAI Responses 流结束：事件=%s，状态=%s，未完成原因=%s，接收字节=%s，增量文本字符=%s",
        final_event_type or "未收到终止事件", status or "未知", incomplete or "无",
        received, text_chars,
    )
    if final_response:
        if final_response.get("error"):
            raise SVGGenerationError("OpenAI Responses 流返回错误")
    final_text = _responses_output_text(final_response)
    candidates = []
    if final_text:
        candidates.append(("终止事件正文", final_text))
    done_output = {"output": [item for _, item in sorted(done_items.items())]}
    done_item_text = _responses_output_text(done_output)
    if done_item_text:
        candidates.append(("output_item.done", done_item_text))
    done_text = "".join(done_text_parts).strip()
    if done_text:
        candidates.append(("output_text.done", done_text))
    delta_text = "".join(text_parts).strip()
    if delta_text:
        candidates.append(("output_text.delta", delta_text))
    summary_sources = [
        _responses_reasoning_summary(final_response),
        _responses_reasoning_summary(done_output),
        "\n".join("".join(parts) for _, parts in sorted(reasoning_summaries.items())),
    ]
    # 同一摘要会在 delta、done 和最终对象中重复出现，诊断长度不重复累计。
    reasoning_summary_chars = max(map(len, summary_sources), default=0)
    summary_text = "\n".join(dict.fromkeys(text for text in summary_sources if text))
    return _validate_responses_candidates(
        candidates, max_chars, status, incomplete, stream=True,
        received=received, event_type=final_event_type,
        output_tokens=((final_response.get("usage") or {}).get("output_tokens")
                       if final_response else None),
        reasoning_tokens=(((((final_response.get("usage") or {}).get("output_tokens_details") or {})
                            .get("reasoning_tokens")) if final_response else None)),
        reasoning_summary_chars=reasoning_summary_chars,
        reasoning_summary_candidate=summary_text,
    )


def _validate_responses_candidates(candidates, max_chars, status, reason, *,
                                   stream, received, event_type="", output_tokens=None,
                                   reasoning_tokens=None, reasoning_summary_chars=0,
                                   reasoning_summary_candidate=""):
    """优先验证终止正文，再回退到 done 和 delta 文本；只接受安全完整的 SVG。"""
    failures = []
    seen = set()
    for source, text in candidates:
        if not isinstance(text, str) or not text.strip() or text in seen:
            continue
        seen.add(text)
        try:
            svg = sanitize_svg(text, max_chars=max_chars)
        except SVGGenerationError as exc:
            failures.append((source, len(text), str(exc)))
            logger.warning(
                "OpenAI Responses 输出候选校验失败：来源=%s，字符数=%s，原因=%s",
                source, len(text), exc,
            )
            continue
        logger.log(
            logging.WARNING if status == "incomplete" else logging.INFO,
            "OpenAI Responses 输出已通过校验：来源=%s，状态=%s，未完成原因=%s，终止事件=%s，接收字节=%s，文本字符=%s",
            source, status or "未知", reason or "无", event_type or "同步响应",
            received, len(text),
        )
        if status == "incomplete":
            logger.warning("OpenAI Responses 虽标记未完成，但候选 SVG 已通过完整性与安全校验，继续使用")
        return svg

    if reasoning_summary_candidate:
        # 使用重叠候选，前面的未闭合示意标签不能吞掉后面真正的 SVG。
        for match in re.finditer(
                r"(?=(<svg\b[^>]*>.*?</svg\s*>))", reasoning_summary_candidate,
                flags=re.IGNORECASE | re.DOTALL):
            try:
                svg = sanitize_svg(match.group(1), max_chars=max_chars)
            except SVGGenerationError:
                continue
            logger.warning(
                "OpenAI Responses 从 reasoning_summary 提取到完整 SVG：状态=%s，未完成原因=%s，文本字符=%s",
                status or "未知", reason or "无", len(match.group(1)),
            )
            return svg

    if status == "incomplete":
        validation_error = failures[0][2] if failures else None
        diagnostics = [f"接收={received}字节", f"请求上限={max_chars // SVG_CHARS_PER_TOKEN} Token"]
        if output_tokens is not None:
            diagnostics.append(f"上游输出Token={output_tokens}")
        if reasoning_tokens is not None:
            diagnostics.append(f"上游推理Token={reasoning_tokens}")
        if reasoning_summary_chars:
            diagnostics.append(f"推理摘要={reasoning_summary_chars}字符（未提取到有效 SVG）")
        if candidates:
            diagnostics.append(
                "候选文本=" + "/".join(f"{len(text)}字符" for _, text in candidates
                                        if isinstance(text, str))
            )
        raise _incomplete_error(
            reason, stream=stream, validation_error=validation_error,
            diagnostics="，".join(diagnostics), reasoning_tokens=reasoning_tokens,
            output_tokens=output_tokens, reasoning_summary_chars=reasoning_summary_chars,
        )
    if failures:
        raise SVGGenerationError(failures[0][2])
    raise SVGGenerationError("模型没有返回 SVG 内容")


def _extract_svg(text, max_chars=MAX_SVG_CHARS):
    """兼容模型偶尔添加的围栏或前后说明，只取一个 SVG 根元素。"""
    text = re.sub(r"^\s*```(?:svg|xml)?\s*|\s*```\s*$", "", text,
                  flags=re.IGNORECASE)
    match = re.search(r"<svg\b[^>]*>.*?</svg\s*>", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        if re.search(r"<svg\b", text, flags=re.IGNORECASE):
            raise SVGGenerationError("SVG 根标签未闭合，输出可能被上游截断")
        raise SVGGenerationError("模型未返回完整 SVG，请重试")
    candidate = match.group(0)
    if len(candidate) > max_chars:
        raise SVGGenerationError("SVG 内容过大，请重试")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", candidate, flags=re.IGNORECASE):
        raise SVGGenerationError("SVG 包含不支持的 XML 声明")
    return candidate


def sanitize_svg(text, max_chars=MAX_SVG_CHARS):
    """保留安全绘图、文档内图形复用与 SMIL 动画，移除脚本、事件和外部引用。"""
    candidate = _extract_svg(text, max_chars=max_chars)
    try:
        source = ET.fromstring(candidate)
    except ET.ParseError as exc:
        raise SVGGenerationError("模型返回的 SVG 格式无效，请重试") from exc

    def local_name(name):
        return name.rsplit("}", 1)[-1]

    if local_name(source.tag) != "svg":
        raise SVGGenerationError("模型没有返回 SVG 画布，请重试")

    # 限定动画目标，防止通过 SMIL 动态改变链接、事件等非绘图属性。
    animation_attributes = {
        "transform", "opacity", "fill", "stroke", "stroke-width", "fill-opacity",
        "stroke-opacity", "stroke-dashoffset", "x", "y", "cx", "cy", "r",
        "rx", "ry", "d", "points", "width", "height",
    }

    removed_tags = Counter()
    removed_attributes = Counter()

    def clean_element(element):
        tag = local_name(element.tag)
        if tag not in ALLOWED_TAGS:
            removed_tags[tag] += 1
            return None
        if tag in ANIMATION_TAGS and element.get("attributeName") not in animation_attributes:
            return None
        cleaned = ET.Element(f"{{{SVG_NAMESPACE}}}{tag}")
        for raw_name, raw_value in element.attrib.items():
            name = local_name(raw_name)
            value = str(raw_value).strip()
            if name not in ALLOWED_ATTRIBUTES or name.lower().startswith("on"):
                removed_attributes[name] += 1
                continue
            if name == "id" and not re.fullmatch(r"[A-Za-z_][\w.:-]{0,99}", value):
                continue
            if name == "href" and not re.fullmatch(r"#[A-Za-z_][\w.:-]{0,99}", value):
                removed_attributes[name] += 1
                continue
            if "url(" in value.lower() and not re.fullmatch(
                    r"url\(\s*#[A-Za-z_][\w.:-]{0,99}\s*\)", value, re.IGNORECASE):
                continue
            # xlink:href 统一为现代 SVG href；只允许本文件内的 #id 引用。
            # use 原样复用净化后的定义，不展开图形，避免丢轮子或递归膨胀。
            cleaned.set(name, value)
        cleaned.text = element.text if tag in {"text", "tspan", "title", "desc"} else None
        for child in list(element):
            safe_child = clean_element(child)
            if safe_child is not None:
                cleaned.append(safe_child)
        return cleaned

    root = clean_element(source)
    if root is None:
        raise SVGGenerationError("SVG 画布无法读取，请重试")
    if removed_tags or removed_attributes:
        logger.warning("SVG 安全净化移除内容：元素=%s，属性=%s",
                       dict(removed_tags), dict(removed_attributes))
    if not any(local_name(node.tag) in ANIMATION_TAGS for node in root.iter()):
        raise SVGGenerationError("生成结果没有可播放动画，请重试")
    # 保留模型画布坐标，仅让根画布自适应预览；子图形的尺寸必须保留。
    view_box = root.get("viewBox", "0 0 800 420")
    parts = re.split(r"[\s,]+", view_box.strip())
    try:
        valid_view_box = len(parts) == 4 and float(parts[2]) > 0 and float(parts[3]) > 0
    except ValueError:
        valid_view_box = False
    root.set("viewBox", view_box if valid_view_box else "0 0 800 420")
    root.set("width", "100%")
    root.set("height", "100%")
    root.set("role", "img")
    root.set("aria-label", "鹈鹕骑自行车循环动画")
    ET.register_namespace("", SVG_NAMESPACE)
    sanitized = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    if len(sanitized) > max_chars:
        raise SVGGenerationError("SVG 内容过大，请重试")
    return sanitized


def generate_svg(relay, urlopen=None, reasoning_effort=DEFAULT_REASONING_EFFORT,
                 api_timeout_minutes=DEFAULT_API_TIMEOUT_MINUTES,
                 max_tokens=DEFAULT_MAX_OUTPUT_TOKENS, max_input_tokens=DEFAULT_MAX_INPUT_TOKENS,
                 failure_log_path=None, failure_log_metadata=None):
    """统一记录各协议失败证据；网络、解析、校验和意外异常共享同一入口。"""
    started = time.monotonic()
    diagnostic = {
        "metadata": {
            **(failure_log_metadata or {}),
            "request_started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "protocol": relay.get("protocol", "openai"),
            "model": relay.get("model", ""),
            "response_body_complete": False,
            "response_capture_end": "尚未收到上游响应",
        },
        "capture": None,
    }
    try:
        return _generate_svg_request(
            relay, diagnostic, urlopen, reasoning_effort, api_timeout_minutes,
            max_tokens, max_input_tokens,
        )
    except Exception as exc:
        metadata = diagnostic["metadata"]
        timeout_seconds = metadata.get("api_timeout_seconds")
        if isinstance(exc, urllib.error.HTTPError):
            capture = _ResponseCapture(exc)
            diagnostic["capture"] = capture
            try:
                capture.read_body(metadata["max_response_bytes"])
            except Exception as read_error:
                metadata["http_error_body_read_traceback"] = "".join(traceback.format_exception(
                    type(read_error), read_error, read_error.__traceback__,
                ))
            finally:
                exc.close()
            error = SVGGenerationError(f"渠道 API 返回 HTTP {exc.code}")
        elif isinstance(exc, TimeoutError) or (
                isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)):
            error = SVGGenerationError(
                f"渠道 API 请求超时（已配置 {timeout_seconds // 60} 分钟），请稍后重试"
            )
        elif isinstance(exc, urllib.error.URLError):
            error = SVGGenerationError("无法连接渠道 API，请检查地址和网络")
        elif isinstance(exc, SVGGenerationError):
            error = exc
        else:
            error = SVGGenerationError("生成服务内部错误，请查看失败日志")
        capture = diagnostic["capture"]
        if capture is not None:
            metadata.update(capture.metadata)
        metadata["elapsed_seconds"] = round(time.monotonic() - started, 3)
        raise record_failure(
            error, log_path=failure_log_path,
            raw_response=capture.raw_response if capture else None,
            metadata=metadata, cause=exc, secrets=(relay.get("apiKey"),),
        ) from exc


def _safe_endpoint(endpoint):
    """去掉用户信息与查询串，只保留可写入诊断的地址。"""
    parsed = urllib.parse.urlsplit(endpoint)
    return urllib.parse.urlunsplit((
        parsed.scheme, parsed.netloc.rsplit("@", 1)[-1],
        parsed.path, "", "",
    ))


def _alternate_endpoint(protocol, endpoint):
    """返回同一渠道的另一种版本段写法，用于 404/405 时探测路由差异。"""
    parsed = urllib.parse.urlsplit(endpoint)
    suffix = {
        "anthropic": "/messages",
        "openai-responses": "/responses",
    }.get(protocol, "/chat/completions")
    if not parsed.path.endswith(suffix):
        return None
    # 有 /vN 时去掉版本段，没有时补成 /v1，覆盖中转站两种常见挂载方式。
    new_path = re.sub(r"/v\d+(?:\.\d+)?(?=" + re.escape(suffix) + r"$)", "", parsed.path)
    if new_path == parsed.path:
        new_path = parsed.path[: -len(suffix)] + "/v1" + suffix
    return urllib.parse.urlunsplit((
        parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], new_path, "", "",
    ))


def _open_with_retry(urlopen, protocol, endpoint, body, headers, timeout_seconds):
    """发送 JSON 请求并返回 (响应, 最终地址, 最终请求体)。

    400/422 时按上游提示降级可选参数（最多两次），404/405 时尝试另一种版本段写法；
    其余错误原样抛出，交给统一失败记录逻辑处理。
    """
    current_body = dict(body)
    current_endpoint = endpoint
    alternative = _alternate_endpoint(protocol, endpoint)
    param_retries = 0
    endpoint_retried = False
    while True:
        request = urllib.request.Request(
            current_endpoint,
            data=json.dumps(current_body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            return urlopen(request, timeout=timeout_seconds), current_endpoint, current_body
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 422) and param_retries < 2:
                detail = exc.read(8192).decode("utf-8", "replace")
                exc.close()
                degraded = degrade_request_body(protocol, current_body, detail)
                if degraded:
                    current_body = degraded
                    param_retries += 1
                    continue
                # 已经读过错误正文，重新包装以保留失败日志所需的响应内容。
                raise urllib.error.HTTPError(
                    exc.url, exc.code, exc.reason, exc.headers,
                    io.BytesIO(detail.encode("utf-8", "replace")),
                ) from exc
            if exc.code in (404, 405) and alternative and not endpoint_retried:
                exc.close()
                current_endpoint = alternative
                endpoint_retried = True
                continue
            raise


def _generate_svg_request(relay, diagnostic, urlopen, reasoning_effort,
                          api_timeout_minutes, max_tokens, max_input_tokens):
    """通过中转站发送绘图提示词并返回已净化的 SVG。"""
    if urlopen is None:
        urlopen = urllib.request.urlopen
    timeout_seconds = api_timeout_seconds(api_timeout_minutes)
    max_tokens = token_budget(max_tokens, "输出 Token 预算")
    max_input_tokens = token_budget(max_input_tokens, "输入 Token 预算")
    max_svg_chars = max_tokens * SVG_CHARS_PER_TOKEN
    max_response_bytes = max_svg_chars * 6 + 64 * 1024
    protocol = relay.get("protocol", "openai")
    reasoning_effort = resolve_reasoning_effort(relay, reasoning_effort)
    base_url = relay.get("baseUrl", "")
    model = relay.get("model", "")
    api_key = relay.get("apiKey", "")
    if not base_url or not model or not api_key:
        raise SVGGenerationError("渠道地址、模型或 API Key 未配置")

    # 参数按模型能力下发，避免把模型不支持的字段塞进请求而被上游 400 拒绝。
    endpoint = endpoint_url(protocol, base_url)
    if protocol == "anthropic":
        params = anthropic_message_params(model, reasoning_effort)
        body = {
            "model": model,
            "max_tokens": max_tokens,
            **({"temperature": 0} if params["temperature"] is not None else {}),
            **({"thinking": params["thinking"]} if params["thinking"] else {}),
            **({"output_config": params["output_config"]} if params["output_config"] else {}),
            "messages": [{"role": "user", "content": USER_PROMPT}],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    elif protocol == "openai-responses":
        body = {
            "model": model,
            "input": USER_PROMPT,
            "max_output_tokens": max_tokens,
            "stream": True,
            **responses_params(model, reasoning_effort),
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "Authorization": f"Bearer {api_key}",
        }
    else:
        params = openai_chat_params(model, reasoning_effort)
        body = {
            "model": model,
            params["max_tokens_field"]: max_tokens,
            **({"temperature": params["temperature"]} if params["temperature"] is not None else {}),
            **({"reasoning_effort": params["reasoning_effort"]} if params["reasoning_effort"] else {}),
            "messages": [
                {"role": "user", "content": USER_PROMPT},
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    # 输入预算是模型容量约定；这两个协议没有通用的 max_input_tokens 请求字段。
    # 完整发送原始输入，不按字符估算 Token 后截断，也不伪造渠道不支持的参数。
    logger.info("渠道 API Token 预算：协议=%s，输出=%s，输入=%s（完整发送输入）",
                protocol, max_tokens, max_input_tokens)
    # 只保存实际请求正文和不含认证参数的地址，不把请求头写入日志。
    diagnostic["metadata"].update({
        "endpoint": _safe_endpoint(endpoint),
        "request_body": body,
        "reasoning_effort": reasoning_effort,
        "max_output_tokens": max_tokens,
        "max_input_tokens": max_input_tokens,
        "api_timeout_seconds": timeout_seconds,
        "max_response_bytes": max_response_bytes,
    })
    response, endpoint, body = _open_with_retry(
        urlopen, protocol, endpoint, body, headers, timeout_seconds,
    )
    diagnostic["metadata"].update({
        "endpoint": _safe_endpoint(endpoint),
        "request_body": body,
    })
    with response:
        capture = _ResponseCapture(response)
        diagnostic["capture"] = capture
        if protocol == "openai-responses":
            # Responses 读取阶段已经完成 SVG 完整性、安全性和动画校验。
            return _read_openai_responses(capture, max_response_bytes, max_svg_chars)
        raw = capture.read_body(max_response_bytes)
        if len(raw) > max_response_bytes:
            raise SVGGenerationError("渠道响应超过大小限制")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SVGGenerationError("渠道响应格式无效") from exc
        return sanitize_svg(
            _text_from_response(payload, protocol), max_chars=max_svg_chars,
        )
