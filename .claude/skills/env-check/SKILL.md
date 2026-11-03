---
name: env-check
description: Verify the local dev environment (venv, deps, spaCy model, .env, optional Ollama/tesseract) and tell the owner exactly what to run if something needs sudo. Use at the start of a session or when imports/tests fail unexpectedly.
allowed-tools: Bash, Read
---

# Environment check

Claude must not use sudo or install system packages in this repo. Check, report, and ask the owner for the
one-liner when needed.

```bash
test -x .venv/bin/python && echo "venv OK" || echo "venv MISSING"
.venv/bin/python -c "import fastapi, sqlmodel, spacy, polanon; print('deps OK, polanon', polanon.__version__)" 2>&1 | tail -1
.venv/bin/python -c "import spacy; spacy.load('pl_core_news_md'); print('spacy model OK')" 2>&1 | tail -1
test -f .env && echo ".env OK" || echo ".env MISSING (cp .env.example .env)"
grep -q "^LLM_API_KEY=.\+" .env 2>/dev/null && echo "LLM key set" || echo "LLM key EMPTY (OpenRouter)"
grep -q "^VAULT_KEY=.\+" .env 2>/dev/null && echo "vault key set" || echo "vault key EMPTY (generated at startup into data/vault.key)"
command -v ollama >/dev/null && echo "ollama present" || echo "ollama absent -> JUDGE_BACKEND=mock"
command -v tesseract >/dev/null && echo "tesseract present" || echo "tesseract absent -> OCR disabled"
free -h | awk '/Mem/{print "RAM total " $2 ", available " $7}'
```

## If venv is MISSING
Tell the owner to run (needs sudo, once):

```bash
sudo apt install -y python3.12-venv python3-pip && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/python -m spacy download pl_core_news_md
```

Then stop until they confirm. Do not try `curl | sh` installers or `pip --user` workarounds.

## If only deps or the spaCy model are missing
```bash
.venv/bin/pip install -e ".[dev]" && .venv/bin/python -m spacy download pl_core_news_md
```

The masking engine, polanon, comes from PyPI with the first command; it is not a folder in this
repository. An older polanon than the floor in `pyproject.toml` is replaced by the same command.

## Optional local tools (owner decides)
- Ollama for the real stage-2 judge: https://ollama.com/download then `ollama pull qwen2.5:1.5b`, set `JUDGE_BACKEND=ollama`.
- OCR: `sudo apt install tesseract-ocr tesseract-ocr-pol` and `pip install -e ".[ocr]"`.

Report one line per check and the exact next action.
