"""The outbound client: the last few lines of code before text leaves the network."""

import json

import httpx
import pytest

from polgate.llm.openai_compat import LLMError, assert_no_raw_values, stream_chat

MESSAGES = [{"role": "user", "content": "Faktura dla <OSOBA_1>"}]


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _collect(client, api_key: str = "k") -> list[str]:
    return [
        delta
        async for delta in stream_chat(
            MESSAGES, model="m", base_url="https://provider.test/v1", api_key=api_key, client=client
        )
    ]


def test_an_original_value_in_the_payload_is_refused():
    with pytest.raises(LLMError, match="OSOBA_1"):
        assert_no_raw_values(
            [{"role": "user", "content": "Faktura dla Jan Kowalski"}],
            {"<OSOBA_1>": "Jan Kowalski"},
        )


def test_a_blank_mapping_value_never_blocks_a_send():
    """An empty string appears in every text; treating it as a leak would block everything."""
    assert assert_no_raw_values(MESSAGES, {"<OSOBA_1>": "   "}) is None


async def test_a_provider_error_becomes_an_llm_error():
    def handler(request):
        return httpx.Response(429, text="slow down")

    async with _client(handler) as client:
        with pytest.raises(LLMError, match="429"):
            await _collect(client)


async def test_a_transport_failure_becomes_an_llm_error():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    async with _client(handler) as client:
        with pytest.raises(LLMError, match="provider request failed"):
            await _collect(client)


async def test_the_stream_stops_at_the_done_marker():
    body = (
        'data: {"choices": [{"delta": {"content": "raz"}}]}\n\n'
        "data: [DONE]\n\n"
        'data: {"choices": [{"delta": {"content": "dwa"}}]}\n\n'
    )

    def handler(request):
        return httpx.Response(200, content=body.encode())

    async with _client(handler) as client:
        assert await _collect(client) == ["raz"]


async def test_comments_blank_lines_and_broken_json_are_skipped():
    body = (
        ": ping\n\n"
        "data:\n\n"
        "data: {nie-json}\n\n"
        'data: {"choices": []}\n\n'
        'data: {"choices": [{"delta": {}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "tekst"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(200, content=body.encode())

    async with _client(handler) as client:
        assert await _collect(client) == ["tekst"]


async def test_the_request_carries_the_model_and_the_bearer_token():
    seen: dict = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    async with _client(handler) as client:
        await _collect(client)

    assert seen["auth"] == "Bearer k"
    assert seen["url"] == "https://provider.test/v1/chat/completions"
    assert seen["body"]["model"] == "m"
    assert seen["body"]["stream"] is True


async def test_an_empty_key_sends_no_authorization_header():
    """A local endpoint such as Ollama's needs no key; "Bearer " alone is not a legal header."""
    seen: dict = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, content=b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n')

    async with _client(handler) as client:
        assert await _collect(client, api_key="") == ["ok"]

    assert "authorization" not in seen["headers"]
    assert seen["headers"]["x-title"] == "polgate"
