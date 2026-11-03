---
name: quality-gate
description: Run the full local quality gate (ruff lint+format, mypy strict, pytest with coverage) and fix findings. Use before every commit and whenever asked to "check", "lint", "verify" or "make CI green".
allowed-tools: Bash, Read, Edit, Grep, Glob
---

# Quality gate

Run from the repo root with the venv active (`source .venv/bin/activate`).

```bash
ruff check . --fix && ruff format . && mypy polgate && pytest --cov --cov-report=term-missing
```

Interpret and fix, in this order:

1. **ruff**: fix real issues, do not blanket-`noqa`. Allowed per-line ignores: `S` rules on clearly safe code
   (document why), `D` on private helpers. Never ignore `B`, `F`, `E`.
2. **mypy strict**: add precise types; avoid `Any` and `# type: ignore` except for third-party libs without
   stubs (spacy, argon2, openpyxl) and then use `# type: ignore[import-untyped]` style narrow codes.
3. **pytest**: a failing test is a bug in the code unless the test contradicts the docstring spec.
4. **coverage < 90%**: add tests for uncovered branches instead of excluding files. The floor is in
   `pyproject.toml`; the suite currently sits well above it.

Also check:
- `python -W error -m py_compile $(git ls-files '*.py')` for stray invalid escape sequences in docstrings.
- `node --check polgate/static/app.js` if the behaviour file changed (node is not a dependency; skip if absent).
- Nothing inline crept into a template. `tests/test_security.py` covers it, but the fast manual check is
  `grep -rnE '<script>|\son[a-z]+=|hx-on|style="' polgate/templates/` and it must print nothing.
- `git diff --cached --name-only | grep -E "^\.env$|\.db$|\.key$"` must be empty (no secrets/data committed).
- Both i18n files have the same keys:
  `python -c "import json;a=set(json.load(open('polgate/i18n/pl.json')));b=set(json.load(open('polgate/i18n/en.json')));print(a^b or 'i18n OK')"`

Report the final numbers (tests passed, coverage %) in one line.
