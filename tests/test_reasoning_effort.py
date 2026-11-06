# -*- coding: utf-8 -*-
"""推理等级的持久化、协议透传和重试回归测试，不访问真实渠道。"""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import get_args
from unittest.mock import ANY, patch

from fastapi.testclient import TestClient

import monitor
import server
from request_options import (
    AnthropicReasoningEffort, OpenAIReasoningEffort, degrade_request_body,
    endpoint_url, normalize_base_url, openai_chat_params, resolve_reasoning_effort,
)


RELAY = {
    "name": "relay-a", "label": "测试渠道", "protocol": "openai",
    "baseUrl": "https://relay.invalid/v1", "apiKey": "test-key", "model": "gpt-5.4",
}
QUESTIONS = [{"q": "计算 1+1", "answer": "2"}]


class ReasoningSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "settings.db"
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("CREATE TABLE svg_generations(id INTEGER PRIMARY KEY, trigger TEXT, requested_at TEXT)")
            con.execute("INSERT INTO svg_generations VALUES(1,'manual','2026-10-23T10:00:00')")
            con.commit()
        self.config = self.root / "config.json"
        self.config.write_text('{"settings": {"reasoning_effort": "high"}}', encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch.object(monitor, "CONFIG_PATH", self.config).start()
        patch.object(monitor, "DATA_DIR", self.root).start()
        patch.object(server, "get_con", lambda: sqlite3.connect(self.db)).start()
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def test_old_database_gets_xhigh_and_keeps_existing_settings(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO settings VALUES('max_tokens', '2048')")
            con.commit()
        settings = self.client.get("/api/settings").json()
        self.assertEqual(settings["reasoning_effort"], "xhigh")
        self.assertEqual(settings["max_tokens"], 2048)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute(
                "SELECT value FROM settings WHERE key='reasoning_effort'"
            ).fetchone()[0], "xhigh")

    def test_new_database_imports_configured_effort(self):
        self.assertEqual(self.client.get("/api/settings").json()["reasoning_effort"], "high")

    def test_all_levels_roundtrip_and_old_clients_preserve_saved_value(self):
        self.client.get("/api/settings")
        for effort in get_args(OpenAIReasoningEffort):
            with self.subTest(effort=effort):
                response = self.client.put("/api/settings", json={"reasoning_effort": effort})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.client.get("/api/settings").json()["reasoning_effort"], effort)
        self.client.put("/api/settings", json={"reasoning_effort": "low"})
        self.client.put("/api/settings", json={"max_tokens": 1024})
        self.assertEqual(self.client.get("/api/settings").json()["reasoning_effort"], "low")

    def test_invalid_levels_are_rejected_without_overwriting_setting(self):
        self.client.get("/api/settings")
        for effort in ("ultra", "", "XHIGH", 1):
            with self.subTest(effort=effort):
                self.assertEqual(self.client.put(
                    "/api/settings", json={"reasoning_effort": effort}
                ).status_code, 422)
        self.assertEqual(self.client.get("/api/settings").json()["reasoning_effort"], "high")

    def test_svg_worker_uses_persisted_level(self):
        self.client.get("/api/settings")
        self.client.put("/api/settings", json={"reasoning_effort": "medium"})
        with patch.object(server, "_svg_row_status"), patch.object(server, "_svg_state_update"), \
                patch.object(server.svg_artwork, "generate_svg", return_value="<svg/>") as generate:
            self.assertTrue(server._run_one_svg_generation(1, RELAY))
        generate.assert_called_once_with(RELAY, reasoning_effort="medium", api_timeout_minutes=15,
                                         max_tokens=32768, max_input_tokens=262144,
                                         failure_log_path=server._svg_failure_log_path(1),
                                         failure_log_metadata=ANY)
        self.assertEqual(generate.call_args.kwargs["failure_log_metadata"]["generation_id"], 1)

    def test_svg_worker_uses_channel_override_and_protocol_default(self):
        self.client.get("/api/settings")
        self.client.put("/api/settings", json={"reasoning_effort": "medium", "anthropic_reasoning_effort": "low"})
        for relay, expected in (({**RELAY, "reasoning_effort": "high"}, "high"),
                                ({**RELAY, "protocol": "anthropic"}, "low")):
            with self.subTest(protocol=relay["protocol"]), \
                    patch.object(server, "_svg_row_status"), patch.object(server, "_svg_state_update"), \
                    patch.object(server.svg_artwork, "generate_svg", return_value="<svg/>") as generate:
                self.assertTrue(server._run_one_svg_generation(1, relay))
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(generate.call_args.args, (relay,))
                self.assertEqual(generate.call_args.kwargs["reasoning_effort"], expected)


class ChannelReasoningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "channels.db"
        self.config = self.root / "config.json"
        self.config.write_text('{"relays": [], "settings": {}}', encoding="utf-8")

        def connect():
            con = sqlite3.connect(self.db)
            monitor.init_relay_schema(con)
            con.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
            return con

        self.addCleanup(patch.stopall)
        patch.object(monitor, "CONFIG_PATH", self.config).start()
        patch.object(server, "get_con", connect).start()
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def test_channel_override_and_protocol_defaults_are_independent(self):
        for name, protocol, effort in (("inherit-openai", "openai", None),
                                       ("custom-openai", "openai", "high"),
                                       ("inherit-responses", "openai-responses", None),
                                       ("custom-responses", "openai-responses", "minimal"),
                                       ("inherit-anthropic", "anthropic", None),
                                       ("custom-anthropic", "anthropic", "max"),
                                       ("model-default", "anthropic", "default")):
            response = self.client.post("/api/relays", json={
                **RELAY, "name": name, "protocol": protocol, "reasoning_effort": effort,
            })
            self.assertEqual(response.status_code, 200)
        self.client.put("/api/settings", json={"reasoning_effort": "low", "anthropic_reasoning_effort": "medium"})
        relays = {item["name"]: item for item in self.client.get("/api/relays").json()}
        expected = {"inherit-openai": "low", "custom-openai": "high",
                    "inherit-responses": "low", "custom-responses": "minimal", "inherit-anthropic": "medium",
                    "custom-anthropic": "max", "model-default": "default"}
        self.assertEqual({name: item["effective_reasoning_effort"] for name, item in relays.items()}, expected)
        self.assertIsNone(relays["inherit-openai"]["reasoning_effort"])
        self.assertEqual(relays["custom-anthropic"]["reasoning_effort"], "max")

    def test_edit_preserves_missing_value_and_can_restore_inheritance(self):
        self.client.post("/api/relays", json={**RELAY, "reasoning_effort": "high"})
        self.client.put("/api/relays/relay-a", json={**RELAY, "apiKey": ""})
        self.assertEqual(self.client.get("/api/relays").json()[0]["reasoning_effort"], "high")
        self.client.put("/api/relays/relay-a", json={**RELAY, "apiKey": "", "reasoning_effort": None})
        self.assertIsNone(self.client.get("/api/relays").json()[0]["reasoning_effort"])
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT apiKey FROM relays").fetchone()[0], RELAY["apiKey"])

    def test_rejects_protocol_mismatch_before_writing(self):
        for protocol, effort in (("anthropic", "minimal"),):
            response = self.client.post("/api/relays", json={**RELAY, "protocol": protocol, "reasoning_effort": effort})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/relays").json(), [])

    def test_openai_accepts_max_effort(self):
        response = self.client.post("/api/relays", json={**RELAY, "protocol": "openai", "reasoning_effort": "max"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/relays").json()[0]["reasoning_effort"], "max")

    def test_protocol_change_from_old_client_falls_back_to_inheritance(self):
        self.client.post("/api/relays", json={**RELAY, "reasoning_effort": "minimal"})
        response = self.client.put("/api/relays/relay-a", json={**RELAY, "protocol": "anthropic"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.client.get("/api/relays").json()[0]["reasoning_effort"])

    def test_migration_keeps_old_channels_and_inherits_defaults(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("""CREATE TABLE relays(name TEXT PRIMARY KEY, label TEXT, protocol TEXT,
                           baseUrl TEXT, apiKey TEXT, model TEXT, enabled INTEGER)""")
            con.execute("INSERT INTO relays VALUES('old', '旧渠道', 'anthropic', 'url', 'key', 'model', 0)")
            con.commit()
            relays = monitor.load_relays(con)
        self.assertEqual(relays[0]["name"], "old")
        self.assertEqual(relays[0]["enabled"], 0)
        self.assertEqual(relays[0]["apiKey"], "key")
        self.assertIsNone(relays[0]["reasoning_effort"])
        self.assertEqual(resolve_reasoning_effort(relays[0]), "xhigh")


class ReasoningProviderTests(unittest.TestCase):
    def test_openai_responses_yaml_uses_streaming_adapter_and_native_fields(self):
        config = monitor.gen_provider_yaml(
            {**RELAY, "protocol": "openai-responses", "reasoning_effort": "high"},
            QUESTIONS, 2048, "low", 9,
        )
        self.assertIn("openai_responses_provider.cjs", config)
        self.assertIn("max_output_tokens: 2048", config)
        self.assertIn("requestTimeoutMs: 540000", config)
        self.assertIn("stream: true", config)
        self.assertIn('reasoning_effort: "high"', config)
        self.assertNotIn("max_completion_tokens", config)
        self.assertNotIn("passthrough", config)
        self.assertNotIn("temperature:", config)

    @unittest.skipUnless(shutil.which("node"), "需要 Node 验证 Responses SSE 适配器")
    def test_openai_responses_provider_consumes_sse_and_sends_native_body(self):
        script = r"""
const Provider = require('./openai_responses_provider.cjs');
const captured = {};
global.fetch = async (url, options) => {
  captured.url = url;
  captured.body = JSON.parse(options.body);
  const events = [
    'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"答"}\n\n',
    'data: {"type":"response.output_text.delta","delta":"案"}\n\n',
    'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}}\n\n',
    'data: [DONE]\n\n',
  ].join('');
  const bytes = new TextEncoder().encode(events);
  return new Response(new ReadableStream({start(controller) {
    controller.enqueue(bytes.slice(0, 31));
    controller.enqueue(bytes.slice(31));
    controller.close();
  }}), {status: 200, headers: {'content-type': 'text/event-stream; charset=utf-8'}});
};
(async () => {
  const provider = new Provider({config: {
    apiBaseUrl: 'https://relay.invalid/v1', apiKey: 'test-key', model: 'test-model',
    max_output_tokens: 2048, requestTimeoutMs: 5000, reasoning_effort: 'high', stream: true,
  }});
  const result = await provider.callApi('测试');
  process.stdout.write(JSON.stringify({captured, result}));
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script], capture_output=True, text=True,
            encoding="utf-8", cwd=monitor.BASE_DIR, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["captured"]["url"], "https://relay.invalid/v1/responses")
        self.assertEqual(data["captured"]["body"], {
            "model": "test-model", "input": "测试", "max_output_tokens": 2048,
            "stream": True, "reasoning": {"effort": "high"},
        })
        self.assertEqual(data["result"]["output"], "答案")
        self.assertEqual(data["result"]["tokenUsage"], {"prompt": 3, "completion": 2, "total": 5})

    def test_anthropic_yaml_omits_openai_parameters(self):
        for effort in get_args(AnthropicReasoningEffort):
            config = monitor.gen_provider_yaml(
                {**RELAY, "protocol": "anthropic", "model": "claude-sonnet-5-5"},
                QUESTIONS, 512, effort,
            )
            self.assertNotIn("reasoning_effort", config)
            self.assertNotIn("passthrough", config)
            if effort not in ("none", "default"):
                self.assertIn('"type": "adaptive"', config)
                self.assertIn('"effort": "' + effort + '"', config)
                self.assertNotIn("temperature:", config)

    def test_anthropic_yaml_omits_thinking_for_legacy_models(self):
        config = monitor.gen_provider_yaml(
            {**RELAY, "protocol": "anthropic", "model": "claude-sonnet-4-20250514"},
            QUESTIONS, 512, "xhigh",
        )
        self.assertNotIn('"type": "adaptive"', config)
        self.assertNotIn("output_config", config)
        self.assertNotIn("reasoning_effort", config)

    def test_openai_yaml_matches_model_capabilities(self):
        cases = {
            "gpt-4o": ('max_tokens_field: "max_tokens"', "temperature: 0"),
            "o3": ('max_tokens_field: "max_completion_tokens"', "reasoning_effort"),
            "gpt-5.4": ('max_tokens_field: "max_completion_tokens"', "reasoning_effort"),
        }
        for model, (field, extra) in cases.items():
            with self.subTest(model=model):
                config = monitor.gen_provider_yaml({**RELAY, "model": model}, QUESTIONS, 512, "xhigh")
                self.assertIn(field, config)
                self.assertIn(extra, config)
                self.assertNotIn("passthrough", config)
                if model == "gpt-4o":
                    self.assertNotIn("reasoning_effort", config)
                else:
                    self.assertNotIn("temperature:", config)

    @unittest.skipUnless(shutil.which("node") and (monitor.BASE_DIR / "node_modules/promptfoo").exists(),
                         "需要本地 Node 和 promptfoo 才能验证最终请求体")
    def test_actual_provider_body_follows_model_capabilities(self):
        # 验证适配器实际发出的请求体，防止 YAML 与请求格式再次脱节。
        cases = []
        for model in ("gpt-5.4", "o3", "gpt-4o"):
            for effort in get_args(OpenAIReasoningEffort):
                cases.append({
                    "model": model,
                    "effort": effort,
                    "yaml": monitor.gen_provider_yaml({**RELAY, "model": model}, QUESTIONS, 512, effort),
                })
        script = r"""
const fs = require('node:fs');
const yaml = require('js-yaml');
const Provider = require('./openai_chat_provider.cjs');
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const results = [];
  for (const item of cases) {
    const provider = yaml.load(item.yaml).providers[0];
    let captured = null;
    global.fetch = async (url, options) => {
      captured = {url, body: JSON.parse(options.body)};
      return new Response(JSON.stringify({
        choices: [{message: {content: '2'}}],
        usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2},
      }), {status: 200, headers: {'content-type': 'application/json'}});
    };
    const result = await new Provider({config: provider.config}).callApi('测试');
    if (result.error) throw new Error(result.error);
    results.push(captured.body);
  }
  process.stdout.write(JSON.stringify(results));
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(
                [shutil.which("node"), "-e", script], input=json.dumps(cases),
                capture_output=True, text=True, encoding="utf-8", cwd=monitor.BASE_DIR, timeout=60,
                env={**os.environ, "PROMPTFOO_CONFIG_DIR": temp, "PROMPTFOO_DISABLE_TELEMETRY": "1"},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {
            "o3": {"minimal": "low", "low": "low", "medium": "medium",
                   "high": "high", "xhigh": "high", "max": "high"},
            "gpt-5.4": {"minimal": "minimal", "low": "low", "medium": "medium",
                        "high": "high", "xhigh": "xhigh", "max": "xhigh"},
        }
        for case, body in zip(cases, json.loads(result.stdout)):
            model, effort = case["model"], case["effort"]
            with self.subTest(model=model, effort=effort):
                self.assertEqual(body["model"], model)
                self.assertNotIn("max_output_tokens", body)
                if model == "gpt-4o":
                    self.assertEqual(body["max_tokens"], 512)
                    self.assertEqual(body["temperature"], 0)
                    self.assertNotIn("reasoning_effort", body)
                    continue
                self.assertEqual(body["max_completion_tokens"], 512)
                self.assertNotIn("max_tokens", body)
                self.assertNotIn("temperature", body)
                if effort in ("default", "none"):
                    self.assertNotIn("reasoning_effort", body)
                else:
                    self.assertEqual(body["reasoning_effort"], expected[model][effort])
        default_config = monitor.gen_provider_yaml(RELAY, QUESTIONS, 512)
        self.assertIn('reasoning_effort: "xhigh"', default_config)

    @unittest.skipUnless(shutil.which("node") and (monitor.BASE_DIR / "node_modules/promptfoo").exists(),
                         "需要本地 Node 和 promptfoo 才能验证最终请求体")
    def test_actual_anthropic_provider_sends_native_effort_without_temperature(self):
        cases = [{"effort": effort, "yaml": monitor.gen_provider_yaml(
            {**RELAY, "protocol": "anthropic", "model": "claude-sonnet-5-5", "reasoning_effort": effort},
            QUESTIONS, 512, "medium",
        )} for effort in get_args(AnthropicReasoningEffort)]
        script = r"""
console.warn = () => {};
console.log = () => {};
const fs = require('node:fs');
const yaml = require('js-yaml');
const { disableCache } = require('./node_modules/promptfoo/dist/src/cache.js');
const { AnthropicMessagesProvider } = require('./node_modules/promptfoo/dist/src/providers/anthropic/messages.js');
disableCache();
(async () => {
  const results = [];
  for (const item of JSON.parse(fs.readFileSync(0, 'utf8'))) {
    const entry = yaml.load(item.yaml).providers[0];
    const wrapper = entry.id.startsWith('anthropic:') ? null : new (require(entry.id))({config: entry.config});
    const provider = wrapper ? await wrapper.provider : new AnthropicMessagesProvider(
      entry.id.slice('anthropic:messages:'.length), {config: entry.config});
    provider.anthropic.messages.create = async params => {
      results.push(params);
      return {content: [{type:'text', text:'2'}], usage:{input_tokens:1, output_tokens:1}};
    };
    const response = await (wrapper || provider).callApi('测试');
    await (wrapper || provider).cleanup();
    if (response.error) throw new Error(response.error);
  }
  fs.writeFileSync(process.env.RESULT_PATH, JSON.stringify(results));
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
        with tempfile.TemporaryDirectory() as temp:
            result_path = Path(temp) / "result.json"
            result = subprocess.run(
                [shutil.which("node"), "-e", script], input=json.dumps(cases),
                capture_output=True, text=True, encoding="utf-8", cwd=monitor.BASE_DIR, timeout=60,
                env={**os.environ, "PROMPTFOO_CONFIG_DIR": temp, "PROMPTFOO_DISABLE_TELEMETRY": "1",
                     "RESULT_PATH": str(result_path)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            bodies = json.loads(result_path.read_text("utf-8"))
        for case, body in zip(cases, bodies):
            self.assertNotIn("reasoning_effort", body)
            if case["effort"] == "default":
                self.assertNotIn("thinking", body)
                self.assertNotIn("output_config", body)
            elif case["effort"] == "none":
                self.assertEqual(body["thinking"], {"type": "disabled"})
                self.assertNotIn("output_config", body)
            else:
                self.assertEqual(body["thinking"], {"type": "adaptive"})
                self.assertEqual(body["output_config"], {"effort": case["effort"]})
                self.assertNotIn("temperature", body)

    def test_rate_limit_retry_keeps_selected_effort(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_stream(command, **_kwargs):
                output_path = Path(command[command.index("-o") + 1])
                retry = "retry_429" in output_path.name
                row = {"vars": {"question": QUESTIONS[0]["q"]}, "success": True, "score": 1,
                       "response": {"output": "2"}} if retry else {
                    "vars": {"question": QUESTIONS[0]["q"]}, "error": "HTTP 429", "response": {}}
                output_path.write_text(json.dumps({"results": [row]}), encoding="utf-8")
                return 0, ""

            with patch.object(monitor, "GEN_DIR", root), \
                    patch.object(monitor.shutil, "which", return_value="promptfoo"), \
                    patch.object(monitor, "_run_streaming", side_effect=fake_stream):
                result = monitor.run_promptfoo(
                    root / "initial.yaml", tag="test", relay={**RELAY, "reasoning_effort": "high"},
                    questions=QUESTIONS, reasoning_effort="low", log=lambda _message: None,
                )
            self.assertEqual(result[:3], (100.0, 1, 1))
            self.assertIn('reasoning_effort: "high"', (root / "retry_429_test.yaml").read_text("utf-8"))

    def test_cli_uses_saved_level_in_initial_and_retry_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.json"
            config.write_text('{"settings": {"reasoning_effort": "high"}}', encoding="utf-8")
            con = sqlite3.connect(":memory:")
            con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO settings VALUES('reasoning_effort', 'minimal')")
            control = monitor.new_control()

            def stop_after_request(*_args, **_kwargs):
                control["stop"] = True
                raise SystemExit("已停止")

            with patch.object(monitor, "CONFIG_PATH", config), \
                    patch.object(monitor, "GEN_DIR", root / "generated"), \
                    patch.object(monitor, "REPORT_DIR", root / "reports"), \
                    patch.object(monitor, "init_db", return_value=con), \
                    patch.object(monitor, "run_promptfoo", side_effect=stop_after_request) as run:
                monitor.run_all(relays=[RELAY], questions=QUESTIONS, control=control,
                                log=lambda _message: None)
            self.assertEqual(run.call_args.kwargs["reasoning_effort"], "minimal")
            self.assertIn('reasoning_effort: "minimal"',
                          (root / "generated/config_relay-a.yaml").read_text("utf-8"))


class RequestFormatTests(unittest.TestCase):
    def test_anthropic_sdk_base_url_does_not_keep_version(self):
        # SDK 自行拼接 /v1/messages，基础地址带 /v1 会请求成 /v1/v1/messages。
        self.assertEqual(
            normalize_base_url("anthropic", "https://relay.invalid/v1/messages"),
            "https://relay.invalid",
        )
        self.assertEqual(
            normalize_base_url("anthropic", "https://relay.invalid/v1/"),
            "https://relay.invalid",
        )

    def test_endpoint_url_normalizes_repeated_paths(self):
        self.assertEqual(
            normalize_base_url("openai", "https://relay.invalid/v1/chat/completions"),
            "https://relay.invalid/v1",
        )
        self.assertEqual(
            normalize_base_url("openai", "https://relay.invalid/v1/v1"),
            "https://relay.invalid/v1",
        )
        cases = (
            ("openai", "https://relay.invalid", "https://relay.invalid/v1/chat/completions"),
            ("openai", "https://relay.invalid/v1", "https://relay.invalid/v1/chat/completions"),
            ("openai-responses", "https://relay.invalid/v1/responses",
             "https://relay.invalid/v1/responses"),
            ("anthropic", "https://relay.invalid/v1", "https://relay.invalid/v1/messages"),
        )
        for protocol, base, expected in cases:
            with self.subTest(protocol=protocol, base=base):
                self.assertEqual(endpoint_url(protocol, base), expected)

    def test_unknown_models_do_not_send_reasoning(self):
        params = openai_chat_params("relay-alias", "xhigh")
        self.assertEqual(params["max_tokens_field"], "max_tokens")
        self.assertIsNone(params["reasoning_effort"])
        self.assertEqual(params["temperature"], 0)

    def test_degrade_openai_body_fields(self):
        body = {
            "model": "gpt-5.4", "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0, "reasoning_effort": "high", "max_completion_tokens": 512,
        }
        degraded = degrade_request_body(
            "openai", body, "Unsupported parameter: 'temperature' is not supported.",
        )
        self.assertNotIn("temperature", degraded)
        degraded = degrade_request_body(
            "openai", body, "max_completion_tokens is not supported with this model",
        )
        self.assertNotIn("max_completion_tokens", degraded)
        self.assertEqual(degraded["max_tokens"], 512)
        self.assertEqual(degrade_request_body("openai", body, "rate limit exceeded"), None)

    def test_degrade_anthropic_keeps_required_fields(self):
        body = {
            "model": "claude-sonnet-5-5", "max_tokens": 512,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
        }
        degraded = degrade_request_body(
            "anthropic", body, "output_config: Extra inputs are not permitted",
        )
        self.assertNotIn("output_config", degraded)
        self.assertIn("thinking", degraded)
        self.assertIn("max_tokens", degraded)
        self.assertIn("messages", degraded)

    def test_degrade_responses_drops_reasoning(self):
        body = {"model": "gpt-4o", "input": "hi", "max_output_tokens": 512,
                "reasoning": {"effort": "high"}, "stream": True}
        degraded = degrade_request_body(
            "openai-responses", body, "Unknown parameter: 'reasoning'",
        )
        self.assertNotIn("reasoning", degraded)
        self.assertIn("input", degraded)


if __name__ == "__main__":
    unittest.main()
