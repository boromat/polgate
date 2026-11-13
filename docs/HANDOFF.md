# HANDOFF: implementation status and next steps

Status: steps 1-12 are done and tested. The masking engine that steps 3-5 and 12 built lived here as
`piianon/` and is now a library of its own, [polanon](https://github.com/boromat/polanon), which the
hub installs from PyPI, now at 0.3.3 or later. Version 0.1.0 was released on 2026-11-03: the
repository is public and the image is on GitHub Container Registry. Version 0.2.0, dated
2026-11-13, adds `start.bat` to run the hub on Windows and a leak evaluation that measures what
reaches the provider, and requires polanon 0.3.3, which fixed every leak that evaluation found
(see *0.2.0*). What is left is under *Known detection gaps* and *After MVP*.

Read `CLAUDE.md` (rules) and `docs/ARCHITECTURE.md` (data flow) first. Run `/env-check` at the start
of a session and `/quality-gate` before every commit, and `/pii-safety-review` as well for anything
touching the outbound path. Use `/add-page` for the repetitive parts of the hub and `/add-guard` for
a new detector, which is made in polanon.

**Definition of done for every change:** `pytest --cov` green with coverage at or above the floor in
`pyproject.toml`, `ruff check .`, `ruff format --check .` and `mypy polgate` clean, new UI strings in
both i18n files, a Conventional Commit.

## Environment
Set up once on Debian or Ubuntu. The first command installs system packages with sudo, which an
agent working here leaves to a person (`CLAUDE.md`, rule 6):

```bash
sudo apt install -y python3.12-venv python3-pip
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m spacy download pl_core_news_md
cp .env.example .env   # then set LLM_API_KEY
```

Optional: Ollama for the real stage-2 judge (`JUDGE_BACKEND=ollama`), tesseract for OCR.

---

## Done

> The guards, the pipeline, stream restore, the output sweep, the judges and the benchmark moved to
> [polanon](https://github.com/boromat/polanon) with the library, together with their tests. Test
> files marked "polanon" below are in that repository; the others are in this one.

### Step 1: skeleton — done
FastAPI app factory, Jinja2 + vendored HTMX (`polgate/static/htmx.min.js`), SQLite via
SQLModel, `/health`, PL/EN language switch, admin seeded on first start, hourly vault purge task.

### Step 2: auth and accounts — done
Signed-cookie sessions (`polgate/auth/session.py`), argon2id passwords, `current_user` redirecting to
`/login`, `require_admin` returning 403, `login` audit event. Tests: `tests/test_web_auth.py`.

### Step 3: stage-1 guards and the pipeline — done
`RegexGuard` (PESEL, NIP, REGON, KRS, ID card, passport, IBAN, card, email, phone, postcode, IP, MAC,
address, plate, VIN, all with checksums where one exists), `SecretsGuard`, `CompanyGuard`,
`DenylistGuard`, `NerGuard`, and `anonymize()` with stable, reading-order token numbering.
Tests (polanon): `tests/test_pipeline.py`, `test_recognizers_pl.py`, `test_company_guard.py`,
`test_ner_guard.py`.

> Note: `NerGuard` uses spaCy directly instead of Presidio. Presidio's `SpacyRecognizer` maps the
> OntoNotes labels, while the Polish models emit the NKJP set (`persName`, `placeName`, `orgName`), so
> Presidio silently returned nothing. Dropping it also removed a heavy dependency we were bypassing.

### Step 4: vault, stream restore, output sweep, audit — done
Fernet-encrypted vault with TTL and purge, `StreamRestorer` that never splits a token across chunks,
`sweep()` for invented tokens and hallucinated data, audit log that refuses to store personal data.
Tests: `tests/test_vault_and_audit.py`; polanon: `test_stream_restore.py`, `test_output_sweep.py`.

### Step 5: stage-2 judges — done
Shared Polish prompt, strict JSON parsing that drops anything not quoted verbatim, `MockJudge`,
`OllamaJudge`, `RemoteJudge`, and `build_judge()`, which is the hub's, in
`polgate/judge_factory.py`. Tests: `tests/test_judge_factory.py`; polanon: `tests/test_judges.py`.

### Step 6: cloud client and turn router — done
OpenAI-compatible streaming client, `assert_no_raw_values` called before every send, `run_turn()`
orchestrating anonymize → verify → stream → restore → sweep → persist → audit.
Tests: `tests/test_router.py`, including the no-leak assertion and a token split across chunks.

### Step 7: chat UI — done
Conversation list, message history, SSE streaming, per-message "what the model saw" preview, optional
approve-before-send flow, per-message extra terms to mask.

### Step 8: files — done
PDF, DOCX, XLSX, CSV, JSON, TXT extraction with a cp1250 fallback, optional OCR, review-and-approve flow,
approved files attached to later turns. Tests: `tests/test_files.py`.

### Step 9: admin panel — done
Users, policy form (review mode, judge, retention, entity types, denylist), audit browser with filter and
paging, statistics. Tests: `tests/test_web_flows.py`.

---

### Step 10: packaging and docs — done
`docs/THREAT_MODEL.md` (what is and is not protected), `docs/GDPR_NOTES.md` (data minimisation and
pseudonymisation, not legal advice), `benchmarks/samples/` with `demo.py` for reading the masked text
and an assertion that restoration is lossless (both now in polanon), `CHANGELOG.md`, and a README
carrying the measured numbers, the configuration reference and the honest limitations.

### Step 11: security and retention — done
`polgate/security.py` (Content-Security-Policy with no `unsafe-inline`/`unsafe-eval`, hardening headers,
origin check on state-changing requests), `safe_next` against the open redirect in `?next=`,
`polgate/auth/throttle.py` (account lockout counted from the audit trail), `/account` for changing one's
own password and language, an admin password reset, and `polgate/retention.py`, which finally enforces
`Policy.history_ttl_days`. Every inline script, handler and style attribute moved into
`polgate/static/app.js` and `style.css`. Tests: `tests/test_security.py`, `test_account.py`,
`test_retention.py`.

### Step 12: measured detection — done
Two more samples (an IT ticket and an employment contract), `benchmarks/gold/*.json` with 82 annotated
occurrences, `benchmarks/run.py` scoring coverage, partial masking, wrong types and over-masking, and
`tests/test_benchmark.py` holding the numbers as floors. At the time, guards alone covered 68%, guards
plus `pl_core_news_md` covered 81 of 82 with nothing partial and nothing over-masked. The benchmark
moved to polanon and has grown since; the current numbers are in its
[benchmark notes](https://github.com/boromat/polanon/blob/main/benchmarks/README.md).

---

## Release and what is left

- [x] End-to-end run against a real provider (OpenRouter, `anthropic/claude-sonnet-4.5`). The request was
      captured at the HTTP transport as it left: for the synthetic Polish CV no mapping value was
      present, and the answer came back with the originals restored. Sample invoices from abroad,
      which are not part of the repository, showed three pipeline bugs, all fixed, and detection
      gaps: see "International formats" below.
- [x] Make the Docker image start. It could not have: the wheel carried no templates, static files or
      translations; the database defaulted to a path beside the installed package that the service
      account cannot write; and with no environment the session key was a value published in this
      repository. All three are fixed, and the `docker` job in CI builds the image and starts it with
      nothing configured.
- [x] Release pipeline. A `v*` tag runs `.github/workflows/release.yml`: the image goes to
      `ghcr.io/boromat/polgate` for amd64 and arm64, and the GitHub release takes its notes from the
      matching `CHANGELOG.md` section. polanon is released from its own repository.
- [x] Name. `polgate` for the repository, the package and the image, beside `polanon`. PyPI refused
      `piianon` as too similar to `pii-anon`, so the library is published as `polanon`, and the hub's
      working name `piigate` was blocked the same way by `pii-gate`. The hub is distributed as source
      and a Docker image.
- [x] Repositories, in this order:
      1. [x] The GitHub owner is `boromat` and the licence holder `BoroMat`.
      2. [x] The library goes first, as its own repository:
         [`boromat/polanon`](https://github.com/boromat/polanon), with its tests, the benchmark and
         the examples, published on PyPI as `polanon`.
      3. [x] The hub depends on the published `polanon` instead of its own copy; `piianon/`,
         `packaging/`, `benchmarks/`, `examples/` and the library's tests left this repository.
      4. [x] Create `boromat/polgate`, private until CI is green, and push one commit of its tree.
- [x] Screenshots and a demo GIF for the README: `docs/images/demo.gif` and `docs/images/audit.png`.
- [x] First release, 2026-11-03. The `0.1.0` heading in `CHANGELOG.md` was dated and CI went green
      on `main`; then the repository was made public, private vulnerability reporting (which
      `SECURITY.md` and the issue templates point to, and which GitHub offers only on a public
      repository) was switched on, and the tag `v0.1.0` was pushed. The release workflow published
      `ghcr.io/boromat/polgate` as `0.1.0`, `0.1` and `latest` for amd64 and arm64, which pulls
      without signing in, and the GitHub release with the changelog section as its notes. The
      description and topics are set.
- A later release: raise `polgate.__version__` and date its `CHANGELOG.md` heading in one commit,
      wait for CI on `main`, then push the tag `vX.Y.Z`. The workflow refuses a tag that differs from
      the package version or a heading still marked `Unreleased`.

### 0.2.0 (2026-11-13)
- [x] `start.bat` for Windows. A double click runs the hub; the first run creates `.venv`, installs
      the hub and the spaCy model and writes `.env` with generated keys. Tested from a clean copy:
      a first run, then a second one that starts in seconds.
- [x] polanon 0.2.1, then 0.2.2, as the lower bound. 0.2.2 adds the `LINK` type, which the admin
      panel lists and an existing policy switches on by itself, and masks a surname or a profile
      handle on its own once the full name or link was masked. That put three-letter surnames such
      as "Lis" into the mapping on their own, and the pre-send check found them inside "Lista" and
      refused the message; a one-word value under four letters now counts only as a word of its own
      or with a Polish case ending.
- [x] A test of the policy upgrade derives the newly enabled types from polanon instead of listing
      them, so a polanon release that adds a type does not fail this repository's CI.
- [x] A leak evaluation in `evals/`: 27 made-up conversations played through the running hub
      against a local provider that records every request. With polanon 0.2.2 and the mock judge,
      4 of 125 listed values reach the provider, 2 of 76 phrases are masked when they should not be,
      nothing is refused and every answer is restored exactly. CI runs it with `--check`, which
      accepts the misses in `evals/known_gaps.json` and fails on a new one; take a miss off that
      list when a polanon release fixes it. Ollama has not been measured yet.
- [x] polanon 0.3.3 as the lower bound. It fixed the four leaks and the two words masked by
      mistake, so `evals/known_gaps.json` is empty: nothing on the lists reaches the provider.
- [x] An existing policy gets the well-known names a polanon release adds, such as "ZUS" in
      0.3.3: `Policy.well_known_names_known` records what was offered, the way
      `entity_types_known` does for types, and a name the administrator deleted stays deleted. A
      policy saved before the column existed is taken to have been offered polanon's list as it was
      from 0.2.0 to 0.3.2 (`NAMES_BEFORE_TRACKING`).
- [x] A review of the whole project before the release, by area, every finding checked against the
      code. Fixed, each with a test: a UTF-16 text file read as cp1250 and sent unmasked; an empty
      generated key file accepted as a key; a 5 KB XLSX keeping a worker busy for minutes; the
      retention pass failing on a conversation with a message waiting to stream; the review step
      ending in a server error when the judge is down; a provider error lost because its audit row
      quoted a masked number; and retention periods that a typo switched off, a large number broke,
      or a shorter value did not apply to stored mappings.
- [x] `polgate.__version__` 0.2.0 and the dated `CHANGELOG.md` heading, in one commit.
- [ ] Push the tag `v0.2.0` once CI on `main` is green: the release workflow publishes the image and
      the GitHub release.

### Since 0.2.0 (on `feat/rag-attachments`, not released yet)
- [x] Search in long attachments, stage A: the files of one conversation. A file over the
      attachment budget is indexed on the first turn that needs it, in passages of
      `RETRIEVAL_CHUNK_CHARS`, embedded in batches of `EMBEDDING_BATCH_SIZE` through an
      OpenAI-compatible `/embeddings` endpoint, and stored in `AttachmentChunk`, unique per file,
      position and model. Each turn ranks the passages for this message and the previous one, by
      cosine and words in common with tokens matched exactly, and sends the best `retrieval_top_k`
      that fit the budget. The page says when it indexes and when a file is longer than
      `RETRIEVAL_MAX_CHUNKS_PER_FILE` passages. No new data flow until `EMBEDDING_MODEL` is set.
      - The passages are masked with the turn's mapping and checked by `assert_no_raw_values` before
        they are embedded, and so is the question. The conversation's vault mapping is what makes
        the tokens of a file and of a question meet, so polanon's stable pseudonyms are not needed
        here; they are for stage B, where documents and conversations do not share a mapping.
      - A passage with a token the mapping no longer has is skipped and counted (`stale_passages`
        on `message_sent`); an endpoint that fails or a refused text falls back to the file cut as
        before, audited as `attachment_search_failed` with the reason and the number of files.
      - Vectors are stored in plaintext, with the reasoning and its caveat in `docs/THREAT_MODEL.md`.
      - The admin policy page has the switch and the passages per file; retention and rejection
        delete the passages; the retention report counts them apart.
      - `evals/run.py` sends its attachments through the search and checks what reaches
        `/embeddings`: 0 of 125 values leak. `evals/retrieval.py` scores the ranking: 16 of 16
        answers past the budget reach the model with the search, 0 with the file cut, on stand-in
        vectors, and 16 of 16 with bge-m3 in LM Studio, which alone puts the answer first for all
        16 questions; `evals/README.md` has both runs and the steps.
- [x] Measure the search with a real embedding model: bge-m3 in LM Studio, on 13 November 2026,
      16 of 16 answers past the budget reach the model, and vectors alone put every one first.

### Next: a company knowledge base on polanon.index (stage B)
Documents the whole company may ask about, such as regulations and procedures, uploaded once by an
administrator rather than into one conversation. Stage A was built so that this adds a source of
passages rather than a second search:
- **Masking.** A knowledge base has no conversation mapping to share, so its documents are masked
  with stable tokens: an `Anonymizer` built with `polanon.Pseudonyms(key)`, the key kept beside the
  vault key, so the same person gets the same token in every document. The passages are cut with
  `split_passages` and masked by `polanon.index.index_chunks`, which puts what each token stands for
  into a `MappingStore` (its `add` and `get`), implemented over an encrypted table like `VaultEntry`,
  never into the passages.
- **Questions.** `polanon.index.mask_query` masks a question with the conversation's mapping and the
  same pseudonyms, so a person named in the chat and in a document meets as one token; the tokens it
  adds join the conversation's vault mapping, so the answer is restored as usual.
- **Storage.** A table of its own, `KnowledgeChunk` (document, position, masked text, vector, model),
  rather than a nullable `file_id` on `AttachmentChunk`: making a column nullable is not an additive
  change, and `polgate/schema.py` would refuse it on an existing installation.
- **Ranking and the rest.** `polgate/retrieval/select.py` as it is; the turn adds the knowledge
  base's best passages beside the attachments', checked by `assert_no_raw_values` like them. Still to
  decide: who may see which documents, how a document is withdrawn (its passages and its share of
  the mapping store go with it), and whether one index for the company still fits in memory.

### Found in the review before 0.2.0, still open
Most severe first; each was confirmed by reading the code, the first by running it.
- [x] **A token's number was given to somebody else once its vault row expired.** The allocator
  counts from the live mapping, so after `vault_ttl_days` the next person named became `<OSOBA_1>`
  again, while earlier messages, an attached file or an indexed passage still said `<OSOBA_1>` for
  the old one, and the answer came back with the new person's name. Fixed: messages and files record
  a keyed fingerprint of each token's value (`token_fingerprints`, `polgate/vault/fingerprints.py`);
  a replayed token whose value is gone or changed becomes "[dane wygasłe / expired data]", and a
  passage holding one is skipped. Rows from before carry no fingerprints and are checked only for a
  token that has no value at all.
- [x] **Two turns at once in one conversation could give one token to two people.** Both loaded
  the mapping before either stored it. Fixed with a lock per conversation
  (`vault.conversation_lock`) held by a turn and by an upload from reading the mapping to storing
  it. It is in memory, which covers the hub's single process; running more workers would need the
  database to arbitrate, with a unique key on (conversation, token) and a retry.
- [x] **Closing the tab while the answer streamed left no `message_sent` row,** though the provider
  had the message. The turn now records it, marked `interrupted`, on the way out.
- [x] **The answer stream** stopped without an error event on a chunk whose `delta` is `null`, and
  read an error the provider sends inside the stream as the end of a normal answer. Chunks of
  another shape are skipped now, and an error inside the stream ends the turn as a provider error.
- Smaller: OCR checks for tesseract but not its Polish and English data, so a missing language pack
  reads as "unreadable"; an image of about 170 megapixels still goes to tesseract; one unclosed
  quote makes a CSV over 128 KB unreadable; a `"` in `ADMIN_PASSWORD` breaks `start.bat`; the policy
  summary counts one entity type when none are on; `GET /chat` creates a conversation for a user
  with none; the retention pass runs on the event loop; `polgate/schema.py` would backfill a future
  enum column with the value where the database stores the name, and `--drop-orphan-columns` cannot
  drop an indexed column on SQLite.

### Known detection gaps worth an issue each
All but the last are polanon's and belong in
[its issue tracker](https://github.com/boromat/polanon/issues); the last is the hub's. Each was
checked again against polanon 0.3.3, which fixed the PESEL with a typo after its label, the street
without "ul." or a postcode, the password after "Hasło administratora:", the feminine surname in
another case and the over-masking of "ZUS" and "Płatnik".
- The login part of an e-mail address on its own: after "jan.nowak@example.com" is masked, "jan.nowak"
  later in the text comes out as "<OSOBA_1>.nowak" without the language model, so the surname
  leaves. With `pl_core_news_md` loaded the whole login is masked.
- A first name that is also a common word, in lowercase, before a surname that does not decline:
  "ani nowak" is not masked, while "Ani Nowak" and "jana kowalskiego" are. The rule has nothing to
  hold on to; the language model is the intended net, and it leans on capitals.
- A word an employee asks to mask that also occurs in `SYSTEM_PROMPT` ("company", "data") makes the
  final check refuse every turn of that conversation. It fails closed, so nothing leaks, but the
  conversation is stuck. Either keep the system prompt out of the check, since it is code, or refuse
  such a term when it is entered.

### After MVP (separate iterations)
- In polanon: a GLiNER-PL guard (optional extra) as a second engine, scored with its benchmark.
- Grow polanon's annotated corpus. Documents written by the author of the guards are a regression
  check, not evidence; documents from someone else would be worth more than any other single addition.
- [x] **International formats.** The owner chose to cover them. `InternationalGuard` finds foreign
  VAT and company numbers, bank accounts, phones, addresses, postcodes, legal forms and labelled
  names; a known value inside a longer word no longer makes the final check refuse a document; and
  three synthetic foreign invoices joined the benchmark. On the sample invoices from abroad nothing
  is refused and only city names still leave. Still open, in polanon: a second NER model for English
  prose, which would need the owner's approval to download (`en_core_web_sm`, about 13 MB) and a
  language switch in `NerGuard`.
- In polanon: surrogate mode (Faker `pl_PL`, declension-aware restore) behind a flag.
- Per-subject export and deletion, for data-subject requests.
- Alembic, once the schema starts changing in ways `polgate/schema.py` cannot express. That module
  adds columns and refuses everything else on purpose; a rename, a retype or a data migration still
  needs a real tool. Additive changes need nothing.
- [x] Split the library into its own distribution and repository: it is
  [polanon](https://github.com/boromat/polanon), released from there to PyPI, and this repository no
  longer carries a copy.
- MCP server and Open WebUI filter wrappers around `polanon.Anonymizer`.

## Pitfalls collected during research and implementation
- LiteLLM's Presidio guardrail has open bugs restoring tokens in responses (#22821, #6247). Restore
  yourself, deterministically, as `StreamRestorer` does.
- Presidio's Polish support is thin and its NER mapping does not fit the Polish spaCy models; that is why
  polanon has its own `recognizers_pl.py` and `ner.py`.
- Models sometimes inflect tokens in Polish (`<OSOBA_1>a`). `TOKEN_PATTERN` matches exactly, so the
  trailing suffix survives. Acceptable for the MVP; note it in the threat model and revisit with surrogates.
- Allocate tokens in reading order, then substitute right-to-left. Doing both backwards numbers the first
  person in a text `<OSOBA_2>`, which confuses readers and the model.
- Never store originals unencrypted and never put them in `AuditEvent.detail`; `audit.log` enforces this.
- Prefer adding a field to renaming one. `polgate/schema.py` can add a column to a running
  installation; it cannot rename one, and the old column left behind is `NOT NULL` with no default,
  which makes every insert into that table fail until somebody drops it.
- `\b` treats an underscore as a word character, so `\bpassword` never matches `DB_PASSWORD`. Use
  `(?<![A-Za-z0-9])` when a keyword can carry a prefix.
- Two patterns describing the same organisation from opposite ends produce overlapping spans, and
  `merge_spans` keeps only one of them. A guard that can do that has to hand over the union itself,
  as `_coalesce` in polanon's `guards/company_pl.py` does, or half the name stays in the clear.
- Nothing inline in a template. One `style="width: {{ x }}%"` is enough to force `unsafe-inline` into
  the Content-Security-Policy and undo the whole point of it; pass the number as `data-width` instead.
