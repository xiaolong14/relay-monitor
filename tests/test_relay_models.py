import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return json.dumps(self.payload).encode("utf-8")


def test_fetch_openai_models_uses_bearer_and_models_endpoint(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return _Response({"data": [{"id": "gpt-a"}, {"id": "gpt-a"}, {"id": "gpt-b"}]})

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    models = server.fetch_relay_models({
        "name": "relay-a", "label": "A站", "protocol": "openai",
        "baseUrl": "https://example.test/v1/", "apiKey": "secret",
    })

    assert models == ["gpt-a", "gpt-b"]
    assert captured == {
        "url": "https://example.test/v1/models",
        "authorization": "Bearer secret",
        "timeout": 30,
    }


def test_fetch_anthropic_models_accepts_models_payload(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["api_key"] = request.headers["X-api-key"]
        captured["version"] = request.headers["Anthropic-version"]
        return _Response({"models": [{"id": "claude-test"}]})

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    assert server.fetch_relay_models({
        "name": "relay-b", "label": "B站", "protocol": "anthropic",
        "baseUrl": "https://example.test/v1", "apiKey": "secret",
    }) == ["claude-test"]
    assert captured == {"api_key": "secret", "version": "2023-06-01"}
