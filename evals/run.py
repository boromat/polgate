"""Measure how much personal data reaches the provider in realistic conversations.

The tests check one case each, and none of them says how much personal data gets past the hub in a
conversation as people actually write one: several turns, a file, a surname in another case two
messages later. This script plays every conversation in ``conversations.json`` through the running
application, the way a browser does: it signs in, uploads and approves the attachments, sends each
turn and reads the answer stream. The provider is a local HTTP server that records every request it
receives and answers by repeating the masked message back, cut into small chunks, so the restored
answer must equal what the employee typed. It serves ``/embeddings`` too, with a stand-in model
(``bag_of_words.py``), and the attachment budget is set small, so every attachment goes through the
search in attachments and its passages through the embeddings endpoint; what reached that endpoint
counts as received like the chat. ``--no-search`` plays the conversations without the search.

It counts four things:

* values from a conversation's ``must_not_leak`` list that reached the provider, whole or in part;
* phrases from its ``must_survive`` list that did not reach it as written, masked when they
  should not be;
* turns the hub refused to send because its last check found an original value;
* answers that did not come back exactly as the employee wrote the message.

No paid API is involved and nothing leaves the machine. Run it from the repository root::

    python evals/run.py                      # mock judge, the default
    JUDGE_BACKEND=ollama python evals/run.py # the local Ollama judge, when one is running
    python evals/run.py --check              # exit 1 on a finding known_gaps.json lacks (CI)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
import threading
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bag_of_words import vector

DATASET = Path(__file__).resolve().parent / "conversations.json"
#: The leaks and over-masking polanon is known to produce here, which ``--check`` accepts.
KNOWN_GAPS = DATASET.with_name("known_gaps.json")
#: The attachment budget with the search on: smaller than every attachment here, so each one is
#: searched, and its passages go through the embeddings endpoint.
SEARCH_BUDGET = 120
#: Characters per streamed chunk. Small, so tokens arrive split and the restorer has to join them.
CHUNK = 3
_MIME = {".csv": "text/csv", ".txt": "text/plain"}


@dataclass(frozen=True)
class Attachment:
    """A file uploaded into the conversation before the first turn."""

    filename: str
    text: str


@dataclass(frozen=True)
class Conversation:
    """One scripted conversation and what must and must not reach the provider."""

    id: str
    turns: list[str]
    attachments: list[Attachment]
    must_not_leak: list[str]
    must_survive: list[str]


@dataclass
class Result:
    """What happened to one conversation."""

    id: str
    turns: int
    sent: int = 0
    refused: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    leaked: list[str] = field(default_factory=list)
    leaked_in_part: list[str] = field(default_factory=list)
    over_masked: list[str] = field(default_factory=list)
    not_restored: list[int] = field(default_factory=list)
    masked_off_list: list[str] = field(default_factory=list)
    judge_failed: int = 0


class FakeProvider:
    """An OpenAI-compatible endpoint on localhost that records requests and echoes the last turn."""

    def __init__(self) -> None:
        """Bind to a free local port; :meth:`start` begins serving."""
        self.requests: list[dict[str, Any]] = []
        self.embedded: list[list[str]] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                if self.path.endswith("/embeddings"):
                    provider.embedded.append(payload["input"])
                    self._answer("application/json", json.dumps(_embeddings(payload["input"])))
                    return
                provider.requests.append(payload)
                self._answer("text/event-stream", _sse(_last_user_message(payload)))

            def _answer(self, content_type: str, text: str) -> None:
                body = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                """Keep the request log off the report."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        """The base URL the hub is configured with."""
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def start(self) -> None:
        """Serve in a background thread for the life of the process."""
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        """Stop serving."""
        self._server.shutdown()


def _embeddings(texts: list[str]) -> dict[str, Any]:
    """Answer an embeddings request with the stand-in model."""
    return {"data": [{"index": i, "embedding": vector(t)} for i, t in enumerate(texts)]}


def _last_user_message(payload: dict[str, Any]) -> str:
    """Return the masked text of the turn being answered."""
    users = [m["content"] for m in payload.get("messages", []) if m.get("role") == "user"]
    return users[-1] if users else ""


def _sse(text: str) -> str:
    """Encode ``text`` as an OpenAI-style event stream of ``CHUNK``-sized deltas."""
    chunks = [text[i : i + CHUNK] for i in range(0, len(text), CHUNK)]
    events = [f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n" for c in chunks]
    return "".join(events) + "data: [DONE]\n\n"


def load_dataset(path: Path = DATASET) -> list[Conversation]:
    """Read the conversations and check that every listed value is really in them.

    Raises:
        ValueError: when a value or phrase on a list does not occur in its conversation, which
            would make it count as protected without ever having been tested.
    """
    conversations = []
    for raw in json.loads(path.read_text(encoding="utf-8")):
        conversation = Conversation(
            id=raw["id"],
            turns=raw["turns"],
            attachments=[Attachment(**a) for a in raw.get("attachments", [])],
            must_not_leak=raw.get("must_not_leak", []),
            must_survive=raw.get("must_survive", []),
        )
        source = "\n".join(
            [*conversation.turns, *(f"{a.filename}\n{a.text}" for a in conversation.attachments)]
        )
        for value in conversation.must_not_leak:
            if not _occurs(value, source):
                raise ValueError(f"{conversation.id}: {value!r} is not in the conversation")
        for phrase in conversation.must_survive:
            if phrase not in source:
                raise ValueError(f"{conversation.id}: {phrase!r} is not in the conversation")
        conversations.append(conversation)
    ids = [c.id for c in conversations]
    if len(ids) != len(set(ids)):
        raise ValueError("conversation ids must be unique")
    return conversations


def _occurs(value: str, text: str) -> bool:
    """Return whether ``value`` is in ``text`` as a word of its own, not inside a longer one."""
    return re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text) is not None


def _parts(value: str) -> list[str]:
    """Return the pieces of a value that identify on their own: names, the login of an e-mail."""
    login = re.split(r"\s*(?:@|\[at\])\s*", value, maxsplit=1)
    if len(login) == 2:
        return [login[0]]
    words = [w.strip(".,;()") for w in value.split()]
    if len(words) < 2:
        return []
    return [w for w in words if (w[:1].isupper() and w.isalpha() and len(w) >= 3) or len(w) >= 5]


def _sse_events(body: str) -> Iterator[tuple[str, dict[str, str]]]:
    """Parse the hub's answer stream into ``(event, data)`` pairs."""
    for block in re.split(r"\r?\n\r?\n", body):
        event, data = "message", ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data += line[len("data:") :].strip()
        if data:
            yield event, json.loads(data)


def _configure(workdir: Path, provider_url: str, password: str, budget: int | None) -> None:
    """Point the hub at a scratch database and the fake provider, before it reads its settings."""
    from cryptography.fernet import Fernet

    os.environ.update(
        {
            "DATABASE_URL": f"sqlite:///{(workdir / 'eval.db').as_posix()}",
            "UPLOAD_DIR": str(workdir / "uploads"),
            "SECRET_KEY": secrets.token_urlsafe(32),
            "VAULT_KEY": Fernet.generate_key().decode(),
            "LLM_BASE_URL": provider_url,
            "LLM_API_KEY": "",
            "LLM_MODEL": "eval/echo",
            "ADMIN_USERNAME": "eval",
            "ADMIN_PASSWORD": password,
        }
    )
    if budget is not None:
        # Small enough that every attachment here is searched, passage by passage.
        os.environ.update(
            {
                "EMBEDDING_MODEL": "eval/bag-of-words",
                "EMBEDDING_BASE_URL": provider_url,
                "ATTACHMENT_BUDGET_CHARS": str(budget),
                "RETRIEVAL_CHUNK_CHARS": str(max(budget // 2, 40)),
            }
        )
    os.environ.setdefault("JUDGE_BACKEND", "mock")


def run(conversations: Sequence[Conversation], provider: FakeProvider) -> list[Result]:
    """Play every conversation through the hub and score what the provider received."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session, col, select

    from polgate.db import get_engine
    from polgate.main import create_app
    from polgate.models import AuditEvent, FileAsset
    from polgate.vault import store as vault

    results = []
    with TestClient(create_app(), follow_redirects=False) as client:
        signed_in = client.post(
            "/login",
            data={"username": "eval", "password": os.environ["ADMIN_PASSWORD"], "next": "/chat"},
        )
        if signed_in.status_code != 303:
            raise RuntimeError(f"could not sign in: HTTP {signed_in.status_code}")

        for conversation in conversations:
            result = Result(id=conversation.id, turns=len(conversation.turns))
            first_request = len(provider.requests)
            first_embedding = len(provider.embedded)
            created = client.post("/chat/new")
            conversation_id = int(created.headers["location"].rsplit("/", 1)[1])

            for attachment in conversation.attachments:
                mime = _MIME[Path(attachment.filename).suffix.lower()]
                upload = client.post(
                    f"/files/upload?conversation_id={conversation_id}",
                    files={"file": (attachment.filename, attachment.text.encode(), mime)},
                )
                if upload.status_code != 200:
                    result.errors.append(f"upload {attachment.filename}: {upload.status_code}")
                    continue
                with Session(get_engine()) as session:
                    asset = session.exec(
                        select(FileAsset)
                        .where(col(FileAsset.conversation_id) == conversation_id)
                        .order_by(col(FileAsset.id).desc())
                    ).first()
                if asset is not None:
                    client.post(f"/files/{asset.id}/approve")

            sent_text: list[str] = [f"{a.filename}\n{a.text}" for a in conversation.attachments]
            for index, text in enumerate(conversation.turns, start=1):
                client.post(
                    f"/chat/{conversation_id}/send",
                    data={"text": text, "extra_terms": "", "approved": "1"},
                )
                body = client.get(
                    f"/chat/{conversation_id}/stream", headers={"Accept": "text/event-stream"}
                ).text
                answer, failure = "", ""
                for event, data in _sse_events(body):
                    if event == "token":
                        answer += data["text"]
                    elif event == "error":
                        failure = data["detail"]
                if failure == "refused_unmasked":
                    result.refused.append(index)
                    continue
                if failure:
                    result.errors.append(f"turn {index}: {failure}")
                    continue
                result.sent += 1
                sent_text.append(text)
                if answer != text.strip():
                    result.not_restored.append(index)

            chat = [
                str(message.get("content", ""))
                for payload in provider.requests[first_request:]
                for message in payload.get("messages", [])
            ]
            embedded = [text for batch in provider.embedded[first_embedding:] for text in batch]
            received = "\n".join([*chat, *embedded])
            with Session(get_engine()) as session:
                mapping = vault.load_mapping(session, conversation_id)
                result.judge_failed = sum(
                    "judge_failed" in (event.detail or "")
                    for event in session.exec(
                        select(AuditEvent)
                        .where(col(AuditEvent.conversation_id) == conversation_id)
                        .where(col(AuditEvent.event) == "message_sent")
                    )
                )
            _score(conversation, result, received, "\n".join(sent_text), mapping)
            results.append(result)
    return results


def _score(
    conversation: Conversation,
    result: Result,
    received: str,
    sent_text: str,
    mapping: dict[str, str],
) -> None:
    """Fill in the leak, over-masking and off-list counts of one conversation."""
    for value in conversation.must_not_leak:
        if _occurs(value, received):
            result.leaked.append(value)
        elif any(_occurs(part, received) for part in _parts(value)):
            result.leaked_in_part.append(value)
    # A phrase only in a refused turn never went out, so it says nothing about over-masking.
    result.over_masked = [
        phrase
        for phrase in conversation.must_survive
        if phrase in sent_text and phrase not in received
    ]
    listed = [value.casefold() for value in conversation.must_not_leak]
    result.masked_off_list = sorted(
        value
        for token, value in mapping.items()
        if value != token
        and not any(value.casefold() in v or v in value.casefold() for v in listed)
    )


def report(results: Sequence[Result], conversations: Sequence[Conversation], judge: str) -> str:
    """Return the summary table, then one line per conversation that had a finding."""
    from importlib.metadata import version

    values = sum(len(c.must_not_leak) for c in conversations)
    phrases = sum(len(c.must_survive) for c in conversations)
    turns = sum(r.turns for r in results)
    sent = sum(r.sent for r in results)
    leaked = sum(len(r.leaked) for r in results)
    in_part = sum(len(r.leaked_in_part) for r in results)
    lines = [
        f"polanon {version('polanon')}, judge {judge}",
        f"conversations {len(results)}, turns {turns}, "
        f"attachments {sum(len(c.attachments) for c in conversations)}",
        "",
        f"values that must not leak     {values}",
        f"  leaked whole                {leaked}",
        f"  leaked in part              {in_part}",
        f"phrases that must survive     {phrases}",
        f"  masked anyway               {sum(len(r.over_masked) for r in results)}",
        f"turns refused before sending  {sum(len(r.refused) for r in results)}",
        f"answers restored exactly      {sent - sum(len(r.not_restored) for r in results)}/{sent}",
        f"other errors                  {sum(len(r.errors) for r in results)}",
        f"judge failures                {sum(r.judge_failed for r in results)}",
        f"values masked off the list    {sum(len(r.masked_off_list) for r in results)}",
    ]
    for r in results:
        findings = {
            "leaked": r.leaked,
            "in part": r.leaked_in_part,
            "over-masked": r.over_masked,
            "refused turns": r.refused,
            "not restored": r.not_restored,
            "errors": r.errors,
        }
        shown = "; ".join(f"{name}: {items}" for name, items in findings.items() if items)
        if shown:
            lines.append(f"  {r.id}: {shown}")
    return "\n".join(lines)


def compare(results: Sequence[Result], known: dict[str, dict[str, list[str]]]) -> list[str]:
    """Return the findings ``known_gaps.json`` does not list, and the listed ones now gone.

    A leak or an over-masked phrase that polanon is known to miss is listed there, so the check
    passes on the gaps the README describes and fails on a new one. A refusal, an answer restored
    wrong or an error is never expected.
    """
    problems = []
    for r in results:
        found = {
            "leaked": [*r.leaked, *r.leaked_in_part],
            "over_masked": r.over_masked,
        }
        for kind, values in found.items():
            expected = known.get(kind, {}).get(r.id, [])
            problems += [f"{r.id}: new {kind} {v!r}" for v in values if v not in expected]
            problems += [
                f"{r.id}: {kind} {v!r} no longer happens, take it off {KNOWN_GAPS.name}"
                for v in expected
                if v not in values
            ]
        problems += [f"{r.id}: turn {i} refused" for i in r.refused]
        problems += [f"{r.id}: turn {i} not restored exactly" for i in r.not_restored]
        problems += [f"{r.id}: {error}" for error in r.errors]
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Run the evaluation and print the report.

    Returns:
        The process exit code: 1 with ``--check`` when anything was found, otherwise 0.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--check", action="store_true", help=f"exit 1 on any finding {KNOWN_GAPS.name} lacks"
    )
    parser.add_argument("--json", type=Path, help="also write the results to this file")
    parser.add_argument(
        "--off-list", action="store_true", help="list the values masked that no list names"
    )
    parser.add_argument(
        "--no-search", action="store_true", help="send attachments cut, without the search"
    )
    parser.add_argument(
        "--attachment-budget",
        type=int,
        default=SEARCH_BUDGET,
        help=f"attachment budget with the search on (default {SEARCH_BUDGET})",
    )
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    conversations = load_dataset()
    provider = FakeProvider()
    provider.start()
    with tempfile.TemporaryDirectory(prefix="polgate-eval-", ignore_cleanup_errors=True) as tmp:
        budget = None if args.no_search else args.attachment_budget
        _configure(Path(tmp), provider.url, secrets.token_urlsafe(16), budget)
        try:
            results = run(conversations, provider)
        finally:
            provider.stop()
            from polgate.db import get_engine

            get_engine().dispose()

    judge = os.environ["JUDGE_BACKEND"]
    print(report(results, conversations, judge))
    texts = sum(len(batch) for batch in provider.embedded)
    search = "off" if args.no_search else f"on, attachment budget {args.attachment_budget}"
    requests = len(provider.embedded)
    print(f"search in attachments {search}: {requests} embedding requests, {texts} texts")
    if args.off_list:
        for r in results:
            if r.masked_off_list:
                print(f"  {r.id} masked off the list: {r.masked_off_list}")
    if args.json:
        args.json.write_text(
            json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if not args.check:
        return 0
    known = json.loads(KNOWN_GAPS.read_text(encoding="utf-8"))
    problems = compare(results, known)
    print("\n".join(["", *problems]) if problems else f"\nno findings beyond {KNOWN_GAPS.name}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
