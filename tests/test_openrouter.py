"""OpenRouter reader: request shape, parsing, and fallback (network mocked)."""
import io
import json

from app import agent

NOW = "2026-08-04 14:00"


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_openrouter_request_and_parse(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
        seen["body"] = json.loads(req.data)
        reply = {"intent": "report_delay", "delay_minutes": 120, "new_eta": None,
                 "earliest": "2026-08-04 19:00", "latest_end": None, "choice_index": None}
        return FakeResp(json.dumps({"choices": [{"message": {"content": "```json\n" + json.dumps(reply) + "\n```"}}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ex = agent.OpenRouterExtractor(api_key="sk-or-test", model="anthropic/claude-haiku-4.5")("2 ghante late, 7 ke baad", NOW)
    assert (ex.intent, ex.delay_minutes, ex.earliest) == ("report_delay", 120, "2026-08-04 19:00")
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-or-test"
    assert seen["body"]["model"] == "anthropic/claude-haiku-4.5"
    assert seen["body"]["messages"][0]["role"] == "system"


def test_openrouter_selected_and_falls_back(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    def boom(req, timeout):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    extract = agent.get_extractor()
    assert agent.ACTIVE["name"] == "openrouter"
    assert extract("2 hrs late", NOW).delay_minutes == 120      # regex fallback


def test_regex_when_no_keys(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent.get_extractor()
    assert agent.ACTIVE["name"] == "regex"
