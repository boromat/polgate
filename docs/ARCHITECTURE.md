# Architecture

## The engine and the hub
- **[polanon](https://github.com/boromat/polanon)**: the anonymization library, in its own repository
  and installed from [PyPI](https://pypi.org/project/polanon/) as `polanon[ner,judge]`. Pure Python,
  stdlib core, optional extras (spaCy for NER, httpx for LLM judges). Public facade:
  `polanon.Anonymizer`. No knowledge of users, DB, HTTP; `tests/test_layering.py` checks the installed
  version for that boundary.
- **`polgate`**: the hub, this repository. FastAPI + HTMX UI, accounts, encrypted vault, files, audit,
  LLM routing. Depends on polanon.

Single-tenant, self-hosted hub. Employees chat and upload files through the hub; the hub masks PII
locally, sends only tokens to the cloud model, and restores the originals in the response.

```
employee browser ── HTMX/SSE ──> FastAPI hub
                                   │
                    SecurityHeadersMiddleware: CSP without unsafe-inline/eval, origin check
                    SessionMiddleware: signed cookie -> request.state.user
                                   │
                    ┌──────────────┴──────────────┐
                    │ stage 1: guards (deterministic, always on)
                    │   RegexGuard    PESEL/NIP/REGON/KRS/ID/IBAN/card/email/phone/postal/IP/
                    │                 MAC/address/plate/VIN, each with its checksum where one exists
                    │   SecretsGuard  API keys, JWT, passwords, private keys
                    │   CompanyGuard  Polish company names anchored on a legal form (Sp. z o.o., S.A.)
                    │   PersonNameGuard Polish first names in every case, any letter case, with the
                    │                 surname that agrees with them or after "pan"/"pani"/"dr"
                    │   InternationalGuard foreign VAT/company numbers, Bankgiro/sort code/accounts,
                    │                 phones, addresses, postcodes, legal forms, labelled names
                    │   DenylistGuard admin-listed company names
                    │   NerGuard      spaCy pl NER: PERSON / LOCATION / ORG
                    │      → Allowlist drops names the policy never masks (Facebook, Canva …)
                    │      → merge_spans → stable tokens <OSOBA_1> … + mapping
                    ├──────────────────────────────┐
                    │ stage 2: Judge (LLM) reads the MASKED text, flags what still identifies someone
                    │   MockJudge (default, demo stub) | OllamaJudge (local, prod) | RemoteJudge (cloud, testing only)
                    │   failure → Policy.judge_fallback (send stage-1 | block)
                    ├──────────────────────────────┐
                    │ Vault: token→original, Fernet-encrypted, TTL per Policy
                    ├──────────────────────────────┐
                    │ Search in attachments (polgate/retrieval), only with EMBEDDING_MODEL set:
                    │   an approved file over the budget → its masked text in passages → /embeddings
                    │   (local endpoint recommended) → AttachmentChunk, once per file and model
                    │   each turn: masked question → /embeddings → rank by cosine + words, tokens
                    │   exact → best top_k that fit the budget; endpoint down → file cut as before
                    │   every text bound for /embeddings passes assert_no_raw_values first
                    ├──────────────────────────────┐
                    │ LLM router: OpenAI-compatible client (OpenRouter / OpenAI / Ollama)
                    │   provider receives tokens only; SYSTEM_PROMPT says "copy tokens verbatim"
                    ├──────────────────────────────┐
                    │ StreamRestorer: replaces tokens in the SSE stream, buffering partial "<OSOBA_"
                    │ OutputSweep: unknown tokens / new PII in the answer → audit + warning
                    └──────────────────────────────┘
                    Audit log: who, when, provider, entity counts, stage-2 hits, hashes. Never raw values.
                    Retention job (hourly): expired vault rows, history past its TTL with the files
                    on disk, conversations left empty, queued turns nobody streamed, and stored
                    uploads no row refers to.
```

The guards, the judges, `StreamRestorer` and the output sweep come from polanon; the vault, the LLM
router, the audit log and the retention job are the hub's.

polanon's `anonymize()` runs the guards with `asyncio.to_thread`, and the hub does the same for the
output sweep and for reading an uploaded document. All three are CPU-bound work reached from an async
handler, and a hub is shared: inline, a long upload stops everybody else's requests until it
finishes.

polanon's `merge_spans` never unmasks what any guard flagged. Overlapping detections of the same
type become their union, so a name one guard reads to the end and a model cuts short is still one
whole name.
Where types disagree, confidence picks the label — a checksum-validated identifier scores 1.0, a
company name found by its legal form 0.95, a name with its surname or a labelled credential 0.9, a
statistical NER hit 0.85, a judge finding 0.5 — and the losing detection keeps whatever it covered
outside the winner under its own type.

## Storage
SQLite (`data/polgate.db`). `Message.content_original_enc` and `VaultEntry.value_enc` are Fernet-encrypted;
`content_sent` / `text_sent` hold tokenized text and are safe in plaintext.

`AttachmentChunk` holds the passages the search in attachments indexed: a slice of a file's
`text_sent` and a float32 vector computed from it, unique per file, position and embedding model.
Both are in plaintext, like `text_sent` itself; `docs/THREAT_MODEL.md` says why and when that would
have to change. They are deleted before their file, by retention or rejection, and with their
conversation. Ranking runs in memory with numpy: a conversation's attachments hold few enough
passages that a vector database would add a service and no speed.

`polgate/retrieval/select.py` is pure: cutting, ranking and choosing passages take masked text and
vectors and nothing else, so a later source of passages, such as a company knowledge base
(`docs/HANDOFF.md`), can reuse it unchanged. `search.py` binds it to a turn: what to index, the
embeddings calls and the fallback. `store.py` is the table.

Every SQLite connection is opened in WAL mode with a busy timeout and `foreign_keys=ON`: readers keep
working while somebody writes, a moment of overlap waits instead of failing, and the references
between conversations, messages and vault rows are enforced rather than merely documented.

`init_db()` runs `create_all` and then `polgate.schema.reconcile`, which adds columns a newer model
declares and an older database lacks, backfilling each with the field's default. It never drops,
renames or retypes: those are reported, and `python -m polgate.schema` prints the report on demand.
Keep model changes additive and no installation needs a migration step.

## Policies (admin panel, single row)
- `hil_chat` / `hil_files`: `auto` (send, preview after) or `review` (approve mask before sending).
- `judge_enabled`, `judge_fallback`: `send_stage1` or `block`.
- `vault_ttl_days`, `history_ttl_days`: retention, enforced by `polgate/retention.py`. Empty means
  keep forever; a number means the hourly job deletes anything older.
- `enabled_entities`, `denylist`.

## Context budget
The provider sees the conversation rebuilt from `content_sent`, capped at `HISTORY_BUDGET_CHARS`, and
each approved attachment capped at `ATTACHMENT_BUDGET_CHARS`. Attachments are resent on every turn,
so without a cap one long PDF is paid for again with each message and eventually exceeds the model's
context. The newest turn is always sent, even when it alone exceeds the budget.

## Errors
`polgate/web/errors.py` answers an `HTTPException` in the shape the client asked for: one translated
line for an HTMX swap, a full page for a browser, JSON for anything else. The status code is shown;
the English detail meant for a developer never is. `/health` reads a table rather than opening a
connection, because SQLite creates an empty file when the database is missing.

## Token stability
Within a conversation the same original value maps to the same token for as long as the vault keeps
the mapping (30 days by default); after that, a token can stand for a different value in the same
conversation. This lets the model reason consistently ("<OSOBA_1> wrote to <FIRMA_2>").

## Browser side
No inline script, no inline event handler and no `style=` attribute in any template: behaviour lives
in `polgate/static/app.js` behind `data-` hooks, styling in `style.css`. That is what lets the
Content-Security-Policy in `polgate/security.py` refuse `unsafe-inline` and `unsafe-eval` outright,
and `tests/test_security.py` fails if a template breaks the rule.

## Measuring detection
Detection is measured in polanon, not here: its
[benchmark](https://github.com/boromat/polanon/blob/main/benchmarks/README.md) annotates synthetic
sample documents with the values that must be masked, scores coverage per entity type, and holds
the numbers as test floors so they cannot regress.

## What this does NOT protect against (see THREAT_MODEL.md)
Inference from context the judge misses, images without OCR, metadata in files, a compromised hub host.
