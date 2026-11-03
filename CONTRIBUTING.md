# Contributing

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m spacy download pl_core_news_md
pre-commit install
```

The masking engine, [polanon](https://github.com/boromat/polanon), arrives from PyPI with the other
dependencies; none of its code is kept in this repository.

## Detection belongs in polanon
A missed value, a false positive, a new detector or a new entity type is a change to polanon. Open it
in the [polanon repository](https://github.com/boromat/polanon), where it is measured against the
benchmark: its [CONTRIBUTING.md](https://github.com/boromat/polanon/blob/main/CONTRIBUTING.md) has
the steps. An entity type a new polanon release adds appears in the admin panel and is switched on
in existing policies without a change here (`polgate/policy_loader.py`).

## Quality bar
- `ruff check .`, `ruff format .`, `mypy polgate` and `pytest --cov` must pass locally (CI enforces them).
- Type hints on every function signature. Google-style docstrings on public modules, classes and functions.
- Tests for every behaviour change; no network in tests (`httpx.MockTransport`).
- Coverage must not drop below 90%.
- No inline `<script>`, `on*=` handler, `hx-on` or `style=` attribute in a template: behaviour goes in
  `polgate/static/app.js` behind a `data-` hook, styling in `style.css`. The Content-Security-Policy
  refuses `unsafe-inline`, and `tests/test_security.py` fails if a template breaks the rule.
- Conventional Commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:` (e.g. `feat(pipeline): stable token numbering`).
- `README.md` is English and `README.pl.md` is its Polish translation: a change to one is made to the
  other in the same commit.
- Never commit `.env`, keys, real personal data, or model weights. Fixtures use synthetic data only.

## Layering
polanon (the library) must not import `polgate` (the hub) or any web/DB framework, and importing it
must pull in nothing but the standard library. `tests/test_layering.py` checks the installed version.

## Security
See `SECURITY.md` and `docs/THREAT_MODEL.md`. Any change that touches what leaves the process towards an
LLM provider needs a test proving no mapping value is present in the outgoing payload. Anything that
adds a route, a redirect or a template needs to keep the browser-side guarantees intact: no inline code,
no state-changing GET, and no redirect target that a request can point outside the hub.
