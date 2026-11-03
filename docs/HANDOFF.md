# HANDOFF: implementation status and next steps

Status: steps 1-12 are done and tested. The masking engine that steps 3-5 and 12 built lived here as
`piianon/` and is now a library of its own, [polanon](https://github.com/boromat/polanon), which the
hub installs from PyPI. CI builds the Docker image and starts it with nothing configured. What is
left is the first release and making the repository public, which needs the owner's account: the
one-time setup under *Remaining*, in the order given there.

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

## Remaining — all of it needs the owner

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
- [ ] One-time setup on GitHub and the first release, in this order:
      1. Replace `Unreleased` in `CHANGELOG.md`'s `## [0.1.0]` heading with the day's date and commit
         it; the release workflow refuses the tag otherwise.
      2. Wait for CI to go green on `main`.
      3. Make the repository public (Settings → General → Danger Zone → Change visibility).
      4. Settings → Code security: enable private vulnerability reporting, which `SECURITY.md` and the
         issue templates point to. GitHub offers it only on a public repository, hence after step 3.
      5. Tag `v0.1.0`, push the tag and wait for the release workflow to finish. Not `v0.1.0-mvp`: the
         workflow refuses a tag that differs from the package version.
      6. Packages → polgate → Package settings: check that `ghcr.io/boromat/polgate` is public, and
         change its visibility to public if it is not. GitHub's documentation disagrees with itself
         about the visibility a package pushed by a workflow starts with.
      7. Signed out of the registry (`docker logout ghcr.io`), `docker pull ghcr.io/boromat/polgate`
         must succeed: it is the README's first command, and the README's Docker badge links to the
         package.
      8. Description and topics, for search, from the gear beside *About* on the repository page:
         `pii`, `anonymization`, `pseudonymization`, `gdpr`, `rodo`, `llm`, `privacy`, `polish`,
         `nlp`, `self-hosted`, `fastapi`.

### Known detection gaps worth an issue each
The first is polanon's and belongs in [its issue tracker](https://github.com/boromat/polanon/issues);
the second is the hub's.
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
