# -*- coding: utf-8 -*-
"""评测与动画生成共用的请求参数约定。

协议字段的取舍参考 sub2api 的网关兼容做法：
1. OpenAI Chat Completions 只发 max_tokens / max_completion_tokens 中的一个；
2. 推理模型不接受 temperature，非推理模型不接受 reasoning_effort；
3. Anthropic 只有特定模型系列接受 thinking.adaptive 与 output_config.effort；
4. OpenAI Responses 只接受 max_output_tokens 与 reasoning.effort。
"""

import re
from typing import Literal, get_args
from urllib.parse import urlsplit


DEFAULT_REASONING_EFFORT = "xhigh"
DEFAULT_MAX_OUTPUT_TOKENS = 32 * 1024
DEFAULT_MAX_INPUT_TOKENS = 256 * 1024
DEFAULT_API_TIMEOUT_MINUTES = 15
MAX_API_TIMEOUT_MINUTES = 1440
OpenAIReasoningEffort = Literal["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"]
AnthropicReasoningEffort = Literal["default", "none", "low", "medium", "high", "xhigh", "max"]
ReasoningEffort = Literal["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"]

# 推理等级从轻到重；渠道不支持某个等级时，收敛到不超过它的最高可用等级。
REASONING_LEVEL_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")

# 与 sub2api pkg/claude 的 effort 目录保持一致：只有这些系列接受 output_config.effort，
# 旧模型或未知模型发送 thinking.adaptive / output_config 会被上游直接以 400 拒绝。
ANTHROPIC_EFFORT_FAMILIES = (
    ("claude-mythos-preview", ("low", "medium", "high", "max")),
    ("claude-mythos-5", ("low", "medium", "high", "xhigh", "max")),
    ("claude-fable-5", ("low", "medium", "high", "xhigh", "max")),
    ("claude-sonnet-4-6", ("low", "medium", "high", "max")),
    ("claude-sonnet-5-5", ("low", "medium", "high", "xhigh", "max")),
    ("claude-sonnet-5", ("low", "medium", "high", "xhigh", "max")),
    ("claude-opus-4-8", ("low", "medium", "high", "xhigh", "max")),
    ("claude-opus-4-7", ("low", "medium", "high", "xhigh", "max")),
    ("claude-opus-4-6", ("low", "medium", "high", "max")),
    ("claude-opus-4-5", ("low", "medium", "high")),
    ("claude-opus-5-5", ("low", "medium", "high", "xhigh", "max")),
    ("claude-opus-5", ("low", "medium", "high", "xhigh", "max")),
    ("claude-haiku-5-5", ("low", "medium", "high", "xhigh", "max")),
)

# 请求地址里可以省略的版本段与各协议的接口后缀。
_VERSION_PATTERN = re.compile(r"/v\d+(?:\.\d+)?")
_DUPLICATE_VERSION = re.compile(r"(/v\d+(?:\.\d+)?)\1+")
_ENDPOINT_SUFFIXES = ("/chat/completions", "/responses", "/messages")


def token_budget(value, label):
    """校验用户指定的 Token 预算，不再设置与模型无关的低上限。"""
    message = f"{label}必须为正整数"
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(message)
    try:
        budget = int(value)
    except ValueError as exc:
        raise ValueError(message) from exc
    if budget < 1:
        raise ValueError(message)
    return budget


def api_timeout_seconds(minutes=DEFAULT_API_TIMEOUT_MINUTES):
    """统一校验 API 超时配置，并将持久化的分钟数转换为请求使用的秒数。"""
    message = f"API 请求超时必须为 1–{MAX_API_TIMEOUT_MINUTES} 分钟的整数"
    if isinstance(minutes, bool) or not isinstance(minutes, (int, str)):
        raise ValueError(message)
    try:
        value = int(minutes)
    except ValueError as exc:
        raise ValueError(message) from exc
    if not 1 <= value <= MAX_API_TIMEOUT_MINUTES:
        raise ValueError(message)
    return value * 60


def reasoning_setting_key(protocol):
    """按协议选择系统默认项，避免将某一协议专属等级传给另一协议。"""
    return "anthropic_reasoning_effort" if protocol == "anthropic" else "reasoning_effort"


def resolve_reasoning_effort(relay, default_effort=DEFAULT_REASONING_EFFORT):
    """渠道明确配置优先；空值表示继承系统默认，default 表示由模型决定。"""
    effort = relay.get("reasoning_effort")
    if effort is None:
        effort = default_effort
    reasoning_params(relay.get("protocol", "openai"), effort)
    return effort


def reasoning_params(protocol, effort=DEFAULT_REASONING_EFFORT):
    """协议级别的等级校验结果，仅用于校验用户输入，不直接发送给模型。"""
    allowed = AnthropicReasoningEffort if protocol == "anthropic" else OpenAIReasoningEffort
    if effort not in get_args(allowed):
        raise ValueError(f"{protocol} 协议不支持推理等级：{effort}")
    if effort == "default" or protocol == "openai":
        return {} if effort == "default" else {"reasoning_effort": effort}
    if protocol == "anthropic":
        if effort == "none":
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
    return {"reasoning": {"effort": effort}}


def normalize_model_name(model):
    """规范化模型名：去掉渠道前缀、区域前缀、日期后缀与 thinking 标记。"""
    name = str(model or "").strip().lower().removeprefix("models/")
    if "/" in name:
        # OpenRouter / sub2api 习惯使用 provider/model；能力判断只看真实模型名。
        name = name.split("/", 1)[1].strip().removeprefix("models/")
    for prefix in ("us.", "eu.", "apac.", "jp.", "au.", "us-gov.", "global."):
        name = name.removeprefix(prefix)
    name = name.removeprefix("anthropic.")
    name = name.removesuffix("-thinking")
    # 日期后缀（-20250514）不影响能力判断；带点号的 Claude 小版本统一成连字符。
    name = re.sub(r"-\d{8}$", "", name)
    for dotted, dashed in (("claude-opus-5.5", "claude-opus-5-5"),
                           ("claude-sonnet-5.5", "claude-sonnet-5-5"),
                           ("claude-haiku-5.5", "claude-haiku-5-5")):
        if name == dotted:
            name = dashed
    return name


def openai_reasoning_levels(model):
    """返回 OpenAI 模型接受的 reasoning_effort 等级；不支持推理时返回空元组。"""
    name = normalize_model_name(model)
    reasoning = bool(
        re.match(r"(o1|o3|o4|o5)(?:-|$)", name)
        or re.search(r"gpt-(?:5|6|7)(?:\.|-|$)", name)
        or "codex" in name
    )
    if not reasoning:
        return ()
    levels = ["low", "medium", "high"]
    # gpt-5 起才有 minimal；gpt-5.1 以后以及 gpt-6/7 才接受 xhigh / max。
    if re.search(r"gpt-(?:5|6|7)(?:\.|-|$)", name):
        levels.insert(0, "minimal")
    if re.search(r"gpt-5\.([1-9]\d*)", name) or re.search(r"gpt-(?:6|7)", name) or "codex-max" in name:
        levels.append("xhigh")
    if re.search(r"gpt-(?:6|7)", name):
        levels.append("max")
    return tuple(levels)


def anthropic_effort_levels(model):
    """返回 Claude 模型接受的 output_config.effort 等级；不支持时返回空元组。"""
    name = normalize_model_name(model)
    for family, levels in ANTHROPIC_EFFORT_FAMILIES:
        if name == family or name.startswith(family + "-"):
            return levels
    return ()


def clamp_reasoning_effort(effort, levels):
    """把等级收敛到模型支持的范围内；default / none 或模型不支持推理时返回 None。"""
    if not levels or effort in ("default", "none"):
        return None
    if effort in levels:
        return effort
    index = REASONING_LEVEL_ORDER.index(effort)
    candidates = [level for level in levels if REASONING_LEVEL_ORDER.index(level) <= index]
    return candidates[-1] if candidates else levels[0]


def openai_chat_params(model, effort=DEFAULT_REASONING_EFFORT):
    """按模型能力返回 Chat Completions 可安全发送的字段组合。

    返回字段：
    max_tokens_field：推理模型用 max_completion_tokens，其余模型用 max_tokens；
    reasoning_effort：模型支持推理时才有值；
    temperature：推理模型不支持 temperature，返回 None 表示不下发该字段。
    """
    levels = openai_reasoning_levels(model)
    return {
        "max_tokens_field": "max_completion_tokens" if levels else "max_tokens",
        "reasoning_effort": clamp_reasoning_effort(effort, levels),
        "temperature": None if levels else 0,
    }


def anthropic_message_params(model, effort=DEFAULT_REASONING_EFFORT):
    """按 Claude 模型能力返回 thinking / output_config / temperature。

    支持 effort 的系列使用自适应思考并禁用 temperature；
    旧模型或未知模型完全省略 thinking 与 output_config，避免被上游拒绝。
    """
    levels = anthropic_effort_levels(model)
    if not levels or effort == "default":
        return {"thinking": None, "output_config": None, "temperature": 0}
    if effort == "none":
        return {"thinking": {"type": "disabled"}, "output_config": None, "temperature": 0}
    return {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": clamp_reasoning_effort(effort, levels)},
        "temperature": None,
    }


def responses_params(model, effort=DEFAULT_REASONING_EFFORT):
    """OpenAI Responses 只使用 reasoning.effort，不携带 Chat Completions 字段。"""
    level = clamp_reasoning_effort(effort, openai_reasoning_levels(model))
    return {"reasoning": {"effort": level}} if level else {}


def clean_base_url(base_url):
    """去掉末尾斜杠、重复版本段和接口路径，返回渠道基础地址。"""
    url = str(base_url or "").strip().rstrip("/")
    url = _DUPLICATE_VERSION.sub(r"\1", url)
    for suffix in _ENDPOINT_SUFFIXES:
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            url = _DUPLICATE_VERSION.sub(r"\1", url)
    return url


def normalize_base_url(protocol, base_url):
    """返回 provider 使用的基础地址。

    Anthropic SDK 会自行拼接 /v1/messages，因此基础地址必须去掉版本段，
    否则配置了 /v1 的渠道会被请求成 /v1/v1/messages。
    """
    url = clean_base_url(base_url)
    if protocol == "anthropic":
        url = re.sub(r"/v\d+(?:\.\d+)?$", "", url)
    return url


def endpoint_url(protocol, base_url):
    """返回可直接请求的完整地址，兼容带了 /v1、漏了 /v1 或直接给接口路径的配置。"""
    base = clean_base_url(base_url)
    path = urlsplit(base).path
    has_version = bool(_VERSION_PATTERN.search(path))
    if protocol == "anthropic":
        return base + ("/messages" if has_version else "/v1/messages")
    if not has_version and path in ("", "/"):
        base += "/v1"
    suffix = "/responses" if protocol == "openai-responses" else "/chat/completions"
    return base + suffix


def degrade_request_body(protocol, body, error_text):
    """上游提示字段不被支持时，返回去掉或替换问题字段的请求体；无改动返回 None。

    仅处理可选字段，model / messages / input 等协议必需字段永不删除，
    防止自动降级把请求改成另一种非法格式。
    """
    if not isinstance(body, dict) or not error_text:
        return None
    text = str(error_text).lower()
    result = dict(body)
    changed = False

    def drop(field):
        nonlocal changed
        if field in result:
            result.pop(field)
            changed = True

    optional_fields = {
        "anthropic": ("temperature", "thinking", "output_config"),
        "openai-responses": ("temperature", "reasoning", "max_output_tokens"),
        "openai": ("temperature", "reasoning_effort"),
    }.get(protocol, ("temperature",))

    for field in optional_fields:
        if field in result and field in text:
            drop(field)

    if protocol == "openai":
        current = ("max_completion_tokens" if "max_completion_tokens" in result
                   else "max_tokens" if "max_tokens" in result else None)
        alternate = {"max_completion_tokens": "max_tokens",
                     "max_tokens": "max_completion_tokens"}.get(current)
        if current and current in text:
            # 先换用另一字段；该字段已被换过时直接删除，由下一轮重试兜底。
            if alternate and alternate not in result:
                result[alternate] = result.pop(current)
            else:
                result.pop(current)
            changed = True

    if not changed and any(word in text for word in (
            "unsupported parameter", "unknown parameter", "unknown field",
            "unrecognized", "extra inputs", "not allowed")):
        for field in optional_fields:
            if field in result:
                drop(field)
                break
    return result if changed else None
