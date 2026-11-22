"""Minimal OpenAI-compatible streaming chat client.

Works with OpenRouter, OpenAI itself and Ollama's ``/v1`` shim. Keeping it small and dependency-free
means the payload that leaves the network is assembled in code we can read on one screen, which is
the whole point of this project.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping

import httpx
from polanon import leaked_tokens

logger = logging.getLogger(__name__)

_DONE = "[DONE]"


class LLMError(RuntimeError):
    """Raised when the provider returns an error or an unreadable stream, or a payload is refused.

    The message is for the log and may quote the provider's answer. The audit trail takes
    :attr:`audit_detail` instead, built only from the status code or the token, so a provider that
    echoes a number such as "Limit 30000" cannot make the audit refuse its own row.
    """

    def __init__(
        self, message: str, *, status: int | None = None, token: str | None = None
    ) -> None:
        """Keep the provider's status code, or the token a refused payload held."""
        super().__init__(message)
        self.status = status
        self.token = token

    @property
    def audit_detail(self) -> str:
        """Describe the failure without the provider's words: a token, a status or neither."""
        if self.token is not None:
            return f"token={self.token}"
        if self.status is not None:
            return f"status={self.status}"
        return "transport"


def assert_no_raw_values(messages: Iterable[Mapping[str, str]], mapping: Mapping[str, str]) -> None:
    """Fail loudly if any original value would be sent to the provider.

    This is the core guarantee of the hub, so it is checked in code rather than trusted to reviews.
    polanon's :func:`~polanon.leaked_tokens` searches every message for every value of the mapping
    by the rules the masking follows, independently of how the text was masked: a value that is
    part of a token's label, as the denylisted word "KARTA" is of "<KARTA_1>", a PIN inside a date
    and a short surname inside a longer word ("Lis" in "Lista") are no leak, since they would not be
    masked either. Token-shaped text that can carry a value is read too: a word after a token's
    colon that names no case, as in ``<OSOBA_1:Kowalski>``, and a placeholder that stands for itself
    in the mapping, ``<ADMIN_2024>`` where ``ADMIN_2024`` is a password.

    Raises:
        LLMError: when a mapping value appears in any message content. The error names the token,
            never the value, so it may be logged and audited.
    """
    contents = [str(message.get("content", "")) for message in messages]
    leaked = leaked_tokens(contents, mapping)
    if leaked:
        token = leaked[0]
        raise LLMError(
            f"refusing to send: original value for {token} present in the payload", token=token
        )


async def stream_chat(
    messages: list[dict[str, str]],
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float = 120.0,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[str]:
    """Stream assistant text from an OpenAI-compatible endpoint.

    Args:
        messages: chat messages; contents must already be anonymized.
        model: provider model identifier.
        base_url: API root, e.g. ``https://openrouter.ai/api/v1``.
        api_key: bearer token for the provider. Empty sends no ``Authorization`` header, for an
            endpoint that needs no key, such as Ollama's: ``Bearer`` followed by nothing is not a
            legal header value, so the request would fail before it was sent.
        timeout: total request timeout in seconds.
        client: optional injected client, used by tests.

    Yields:
        Text deltas in arrival order.

    Raises:
        LLMError: on a non-2xx response or a malformed stream.
    """
    payload = {"model": model, "messages": messages, "stream": True}
    headers = _headers(api_key)
    owns_client = client is None
    active = client or httpx.AsyncClient(timeout=timeout)
    try:
        async with active.stream(
            "POST", f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode(errors="replace")
                raise LLMError(
                    f"provider returned {response.status_code}: {body[:300]}",
                    status=response.status_code,
                )
            async for line in response.aiter_lines():
                delta = _parse_sse_line(line)
                if delta is _DONE:
                    return
                if delta:
                    yield delta
    except httpx.HTTPError as exc:
        raise LLMError(f"provider request failed: {exc}") from exc
    finally:
        if owns_client:
            await active.aclose()


async def embed(
    texts: list[str],
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float = 60.0,
    client: httpx.AsyncClient | None = None,
) -> list[list[float]]:
    """Return one embedding per text from an OpenAI-compatible ``/embeddings`` endpoint.

    Args:
        texts: the inputs; like chat messages, they must already be anonymized and checked with
            :func:`assert_no_raw_values`.
        model: embedding model identifier at that endpoint.
        base_url: API root, e.g. ``http://localhost:1234/v1`` for LM Studio.
        api_key: bearer token; empty sends no ``Authorization`` header.
        timeout: total request timeout in seconds.
        client: optional injected client, used by tests.

    Returns:
        The vectors, in the order of ``texts``.

    Raises:
        LLMError: on a non-2xx response, a transport failure or an answer that does not hold one
            vector per input.
    """
    owns_client = client is None
    active = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await active.post(
            f"{base_url.rstrip('/')}/embeddings",
            json={"model": model, "input": texts},
            headers=_headers(api_key),
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise LLMError(f"embeddings request failed: {exc}") from exc
    finally:
        if owns_client:
            await active.aclose()
    if response.status_code >= 400:
        raise LLMError(
            f"embeddings endpoint returned {response.status_code}: {response.text[:300]}"
        )
    return _vectors(response, len(texts))


def _vectors(response: httpx.Response, expected: int) -> list[list[float]]:
    """Read the vectors of an embeddings answer, ordered by their ``index``."""
    try:
        rows = sorted(response.json()["data"], key=lambda row: row.get("index", 0))
        vectors = [[float(x) for x in row["embedding"]] for row in rows]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LLMError("embeddings endpoint returned an unreadable answer") from exc
    if len(vectors) != expected or any(not vector for vector in vectors):
        raise LLMError(f"embeddings endpoint returned {len(vectors)} vectors for {expected} inputs")
    return vectors


def _headers(api_key: str) -> dict[str, str]:
    """Return the request headers; an empty key sends no ``Authorization`` header at all."""
    headers = {"HTTP-Referer": "https://github.com/boromat/polgate", "X-Title": "polgate"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _parse_sse_line(line: str) -> str | None:
    """Return the text delta of one SSE line, ``_DONE`` at end of stream, or None to skip."""
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data:
        return None
    if data == _DONE:
        return _DONE
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        logger.debug("skipping unreadable stream line")
        return None
    if not isinstance(chunk, dict):
        return None
    if "error" in chunk:
        # OpenRouter reports a failure that happens after the answer began inside the stream. Read
        # as the end of the answer, it stored half an answer as a whole one.
        raise LLMError("provider reported an error inside the stream", status=_error_code(chunk))
    choices = chunk.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    delta = first.get("delta") if isinstance(first, dict) else None
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) and content else None


def _error_code(chunk: dict[str, object]) -> int | None:
    """Return the numeric code of an error reported inside the stream, when it gives one."""
    error = chunk.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) else None
