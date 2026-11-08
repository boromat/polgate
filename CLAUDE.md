# CLAUDE.md — working rules for this repo

This repository is the hub, `polgate`. The masking engine is [polanon](https://github.com/boromat/polanon),
installed from PyPI as `polanon[ner,judge]`; its code, tests, benchmark and examples live in that
repository, not here.

Read `docs/HANDOFF.md` first: it records what was built and what is left. `docs/ARCHITECTURE.md`
explains the data flow.

## Non-negotiable rules
1. **Raw PII never leaves the process unmasked.** Anything sent to `llm/openai_compat.py` must be the
   `anonymized` text of polanon's `AnonymizationResult`. Tests must assert this (`assert_no_raw_values`).
2. **Never log raw PII.** Audit events, logs, exceptions: entity types, counts and sha256 hashes only.
3. **Treat polanon's public API and `polgate/models.py` as stable.** The hub imports `polanon`,
   `polanon.policy`, `polanon.types`, `polanon.allowlist` and `polanon.judge.*`. A change to any of
   them is made and released in the [polanon repository](https://github.com/boromat/polanon), and the
   hub then raises its `polanon` floor in `pyproject.toml`; never copy polanon code into this
   repository or patch the installed package. If you must change `polgate/models.py`, grep for usages
   and update tests. Model changes must be **additive**: `polgate/schema.py` adds a new column to a
   running installation at startup, but it cannot rename one, and the old column left behind blocks
   every insert into that table.
4. **Everything in Python.** UI = Jinja2 templates + HTMX attributes. No frontend build step.
   **No inline `<script>`, no `on*=` handler, no `hx-on`, no `style=` attribute in a template**: all
   behaviour lives in `polgate/static/app.js` behind `data-` hooks and all styling in `style.css`, so
   the Content-Security-Policy in `polgate/security.py` can stay free of `unsafe-inline`/`unsafe-eval`.
   `tests/test_security.py` fails if any of them come back.
5. **Code, comments and docstrings in English. UI strings only via `t("key")`** with entries in
   both `polgate/i18n/pl.json` and `en.json`. `README.md` is English and `README.pl.md` is its Polish
   translation: a change to one is made to the other in the same commit.
6. **Do not install system packages (no sudo).** Ollama and tesseract are optional at runtime; code must
   degrade gracefully (mock judge, "OCR unavailable").
7. Small machine (4 CPU, 8 GB RAM, no GPU): use `pl_core_news_md`, lazy-load models, never load GLiNER by default.
8. Prefer finishing a change end-to-end (code + tests + i18n keys) over starting the next one.

## Quality bar (this repo is a public portfolio piece: write it like a maintained OSS project)
- Type hints on every signature; `mypy polgate` (strict) must pass. Google-style docstrings on public API.
- `ruff check .` and `ruff format .` clean with the rule set in `pyproject.toml` (includes bandit `S` and naming `N`).
- Tests for every behaviour; coverage >= 90% (the floor in `pyproject.toml`); no network in tests.
  Name tests `test_<behaviour>_<condition>`.
- Small functions, early returns, no bare `except`, no mutable default args, no print() in hub code
  (use `logging.getLogger(__name__)`), no dead code or commented-out code, no TODO comments:
  open work is listed in `docs/HANDOFF.md` or an issue.
- Conventional Commits (`feat(scope): ...`), one logical change per commit, imperative mood.
- Errors that reach users are translated (`t(...)`); errors in logs are specific and never contain PII.
- Do not add dependencies without a reason in the commit message. Pin lower bounds only in `pyproject.toml`.
- Keep business logic out of route handlers: masking and detection belong in polanon, turn orchestration
  in `polgate/llm/router.py`, and the handlers stay thin.

## Project skills (use them, they encode the workflow)
| Skill | When |
|---|---|
| `/quality-gate` | ruff + mypy + pytest/coverage + i18n/secret checks; run before every commit |
| `/pii-safety-review [path\|staged]` | trace every outbound path for raw-PII leaks; run before committing llm/web/audit/vault changes |
| `/add-guard <what>` | new detector or entity type: made in polanon, then the hub requires the new release |
| `/add-page <what>` | new hub page / HTMX partial with i18n, auth, tests |
| `/env-check` | verify venv, deps, spaCy model, .env; tells the owner what needs sudo |

Detection is measured in polanon, not here: its `benchmarks/run.py` scores the guards against
`benchmarks/gold/`, and its `tests/test_benchmark.py` turns the numbers into floors, so a guard that
gets worse fails that build. The current numbers are in
[polanon's benchmark notes](https://github.com/boromat/polanon/blob/main/benchmarks/README.md).

## Commands
```bash
.venv/bin/pip install -e ".[dev]" && .venv/bin/python -m spacy download pl_core_news_md   # once
./run.sh                              # start the hub, checks the environment first
./run.sh 8080 --no-reload             # different port, no file watcher
start.bat                             # the same on Windows; first run sets up .venv and .env
.venv/bin/polanon "Jan Kowalski, PESEL 50810112354"   # the masking engine on its own
.venv/bin/pytest --cov
.venv/bin/ruff check . && .venv/bin/ruff format . && .venv/bin/mypy polgate
```

## Conventions
- FastAPI routers in `polgate/web/*.py`, prefix per area, return `render(request, "x.html", ...)`.
- HTMX partials live in `polgate/templates/partials/` and are returned by POST handlers.
- SQLModel sessions via `Depends(get_session)`. Commit inside the handler that mutates.
- Async where I/O touches the network (LLM, judge); sync for DB and regex.
- Tests: pytest, `asyncio_mode=auto`, no network. Mock httpx with `httpx.MockTransport`.
