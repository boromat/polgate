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


async def _embed(handler, texts, api_key: str = "k"):
    from polgate.llm.openai_compat import embed

    async with _client(handler) as client:
        return await embed(
            texts, model="bge-m3", base_url="http://embed.test/v1/", api_key=api_key, client=client
        )


async def test_embeddings_come_back_in_the_order_of_the_inputs():
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content), request.headers))
        rows = [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]
        return httpx.Response(200, json={"data": rows})

    vectors = await _embed(handler, ["<OSOBA_1> zarabia", "umowa najmu"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    url, payload, headers = seen[0]
    assert url == "http://embed.test/v1/embeddings"
    assert payload == {"model": "bge-m3", "input": ["<OSOBA_1> zarabia", "umowa najmu"]}
    assert headers["authorization"] == "Bearer k"


async def test_embeddings_without_a_key_send_no_authorization_header():
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    assert await _embed(handler, ["x"], api_key="") == [[1.0]]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="model not loaded"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
        httpx.Response(200, json={"data": [{"index": 0}, {"index": 1}]}),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": []}, {"embedding": [1]}]}),
    ],
)
async def test_an_unusable_embeddings_answer_becomes_an_llm_error(response):
    with pytest.raises(LLMError, match="embeddings"):
        await _embed(lambda request: response, ["a", "b"])


async def test_an_embeddings_transport_failure_becomes_an_llm_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMError, match="embeddings request failed"):
        await _embed(handler, ["a"])


def test_the_chat_key_never_travels_to_another_embeddings_host():
    from polgate.config import Settings

    shared = Settings(llm_base_url="https://chat.test/v1", llm_api_key="chat-key")
    assert shared.embedding_endpoint() == ("https://chat.test/v1", "chat-key")

    local = Settings(
        llm_base_url="https://chat.test/v1",
        llm_api_key="chat-key",
        embedding_base_url="http://localhost:1234/v1",
    )
    assert local.embedding_endpoint() == ("http://localhost:1234/v1", "")


async def test_a_malformed_embeddings_url_becomes_an_llm_error():
    """httpx.InvalidURL is no httpx.HTTPError: it went past the fallback and lost the turn."""
    from polgate.llm.openai_compat import embed

    with pytest.raises(LLMError, match="embeddings request failed"):
        # A control character in the host: a setting pasted with one, such as a stray NUL.
        await embed(["a"], model="m", base_url="http://loc\x00al/v1", api_key="")


@pytest.mark.parametrize(
    "line",
    [
        'data: {"choices": [{"delta": null}]}',
        'data: {"choices": []}',
        'data: {"choices": [{"delta": {"content": 5}}]}',
        "data: [1, 2, 3]",
        'data: "tekst"',
    ],
)
def test_stream_lines_of_another_shape_are_skipped(line):
    """A null delta raised AttributeError out of the stream, and the turn died without a word."""
    from polgate.llm.openai_compat import _parse_sse_line

    assert _parse_sse_line(line) is None


async def test_an_error_inside_the_stream_ends_the_answer_as_an_error():
    """Read as the end of the answer, it stored half an answer as a whole one."""
    body = (
        'data: {"choices": [{"delta": {"content": "Połowa"}}]}\n\n'
        'data: {"error": {"code": 502, "message": "upstream failed"}}\n\n'
    )

    async with _client(lambda request: httpx.Response(200, content=body.encode())) as client:
        with pytest.raises(LLMError, match="inside the stream") as raised:
            await _collect(client)
    assert raised.value.audit_detail == "status=502"
