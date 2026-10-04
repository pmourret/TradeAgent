"""Le client Anthropic est testé avec le vrai SDK et un transport HTTP simulé : aucun réseau, aucun centime."""
import json

import anthropic
import pytest

try:  # le SDK Anthropic 1.x utilise httpx2, les versions 0.x utilisent httpx
    import httpx2 as httpx
except ImportError:
    import httpx

from tradeagent.llm import AnthropicClient, FakeLLMClient, LLMError
from tradeagent.models import Decision


def sdk(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return anthropic.Anthropic(api_key="sk-test-key", http_client=http, max_retries=0)


def message(text="{}", usage=None, content=None):
    return httpx.Response(200, json={
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-haiku-4-5-20251001",
        "content": content if content is not None else [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": usage or {"input_tokens": 1200, "output_tokens": 80},
    })


def test_request_shape_and_key_placement():
    seen = []

    def handler(request):
        seen.append(request)
        return message('{"action": "hold"}')

    client = AnthropicClient("claude-haiku-4-5-20251001", client=sdk(handler))
    reply = client.complete("SYSTEM TEXT", "USER TEXT", 300)

    request = seen[0]
    body = json.loads(request.content)
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["max_tokens"] == 300
    assert body["system"] == "SYSTEM TEXT"
    assert body["messages"] == [{"role": "user", "content": "USER TEXT"}]
    assert request.headers["x-api-key"] == "sk-test-key"
    assert "sk-test-key" not in request.content.decode()  # la clé ne voyage jamais dans le prompt
    assert reply.text == '{"action": "hold"}'


def test_usage_is_parsed():
    client = AnthropicClient("m", client=sdk(lambda r: message(usage={"input_tokens": 1200, "output_tokens": 80})))
    usage = client.complete("s", "u", 100).usage
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (1200, 80, 0, 0)


def test_cache_usage_is_parsed():
    usage_json = {"input_tokens": 100, "output_tokens": 50,
                  "cache_read_input_tokens": 900, "cache_creation_input_tokens": 300}
    usage = AnthropicClient("m", client=sdk(lambda r: message(usage=usage_json))).complete("s", "u", 100).usage
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (900, 300)


def test_null_cache_fields_count_as_zero():
    usage_json = {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": None, "cache_creation_input_tokens": None}
    usage = AnthropicClient("m", client=sdk(lambda r: message(usage=usage_json))).complete("s", "u", 100).usage
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (0, 0)


def test_only_text_blocks_are_returned():
    blocks = [{"type": "text", "text": '{"action":'}, {"type": "text", "text": ' "hold"}'}]
    reply = AnthropicClient("m", client=sdk(lambda r: message(content=blocks))).complete("s", "u", 100)
    assert reply.text == '{"action": "hold"}'


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 529])
def test_http_errors_become_llm_errors(status):
    def handler(request):
        return httpx.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})

    with pytest.raises(LLMError, match="appel Anthropic échoué"):
        AnthropicClient("m", client=sdk(handler)).complete("s", "u", 100)


def test_network_failure_becomes_llm_error():
    def handler(request):
        raise httpx.ConnectError("network down")

    with pytest.raises(LLMError):
        AnthropicClient("m", client=sdk(handler)).complete("s", "u", 100)


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicClient("m")


def test_api_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    AnthropicClient("m")  # ne doit pas lever


def test_fake_client_is_deterministic_and_emits_valid_decisions():
    user = 'Current state (JSON):\n{"positions":{"BTC/EUR":{"qty":0,"value":0},"ETH/EUR":{"qty":0,"value":0}}}'
    a, b = FakeLLMClient(seed=4), FakeLLMClient(seed=4)
    replies_a = [a.complete("sys", user, 100) for _ in range(40)]
    replies_b = [b.complete("sys", user, 100) for _ in range(40)]
    assert [r.text for r in replies_a] == [r.text for r in replies_b]
    actions = {Decision.from_json(r.text).action for r in replies_a}
    assert "hold" in actions and len(actions) > 1
    assert all(r.usage.input_tokens > 0 and r.usage.output_tokens > 0 for r in replies_a)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_api_key_never_appears_in_error_messages(status):
    def handler(request):
        return httpx.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})

    with pytest.raises(LLMError) as info:
        AnthropicClient("m", client=sdk(handler)).complete("s", "u", 100)
    assert "sk-test-key" not in str(info.value)
