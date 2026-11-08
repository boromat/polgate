# polgate

[![CI](https://github.com/boromat/polgate/actions/workflows/ci.yml/badge.svg)](https://github.com/boromat/polgate/actions/workflows/ci.yml)
[![Masking engine: polanon](https://img.shields.io/pypi/v/polanon?label=engine%3A%20polanon)](https://pypi.org/project/polanon/)
[![Docker image](https://img.shields.io/badge/docker-ghcr.io%2Fboromat%2Fpolgate-blue)](https://github.com/boromat/polgate/pkgs/container/polgate)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**English** | [Polski](README.pl.md)

**A self-hosted hub that lets employees use Claude or GPT on real work material without sending
personal data to the provider.** Personal data is masked locally before the request leaves the
network and restored in the answer. Polish-first: PESEL, NIP, REGON, ID cards and IBAN are validated
by checksum, KRS numbers are read next to their label, company names are found by their legal form,
and people and places come from a Polish NER model. Invoices from abroad are covered too: foreign
VAT and company numbers, bank accounts, addresses and phones are read by the labels and formats they
are printed in. The provider gets a token in place of everything the rules and the model find; what
they miss goes out as it was typed, and [the threat model](docs/THREAT_MODEL.md) lists the known
gaps.

The masking engine is [polanon](https://github.com/boromat/polanon), a separate library that pip
installs from [PyPI](https://pypi.org/project/polanon/); this repository holds the hub around it.

![A message with a name, a PESEL, a phone number, an e-mail address, an IBAN and a company is sent;
the answer streams back with the originals in place, and "What the model saw" shows that the provider
received only tokens such as OSOBA_1 and PESEL_1.](docs/images/demo.gif)

<sub>Recorded against a stand-in provider on the developer's machine, with synthetic data.</sub>

## What it does

```
employee ──▶ hub (your server) ──▶ cloud model
   "Anna Wiśniewska, PESEL 45831523452, konto PL61109010140000071219812874"
        │  stage 1  deterministic guards: regex + checksums + Polish NER + your denylist
        │  stage 2  optionally, a small local model (Ollama) reviews the masked text for what
        │           still identifies someone; the default JUDGE_BACKEND=mock is a demo stub
        ▼
   "<OSOBA_1>, PESEL <PESEL_1>, konto <IBAN_1>"        ← what the provider sees
        ▼
   the answer streams back with tokens, and the hub swaps the originals in as it streams
```

## Try it

**The hub, in one command.** Nothing to configure first: on its first start the hub generates its
own keys and an administrator password, and keeps them in the volume with the database.

```bash
docker run -d --name polgate -p 8000:8000 -v polgate-data:/data ghcr.io/boromat/polgate
until docker exec polgate test -s /data/initial-admin-password; do sleep 1; done  # wait for the first start
docker exec polgate cat /data/initial-admin-password
```

Open http://localhost:8000, sign in as `admin` with that password, and change it from the account
page. Masking, uploads, the "what the model saw" preview and the admin panel all work as they are;
to get answers, add your provider key with `-e LLM_API_KEY=...` (OpenRouter by default, any
OpenAI-compatible endpoint with `LLM_BASE_URL`). The image runs on amd64 and arm64.

**Just the masking, in your own code.** That is polanon, whose core needs nothing beyond the
standard library:

```bash
pip install polanon
polanon "Jan Kowalski, PESEL 50810112354, konto PL61109010140000071219812874"
# <OSOBA_1>, PESEL <PESEL_1>, konto <IBAN_1>
# the mapping back to the originals goes to stderr, or to a file with --mapping PATH
```

[polanon's README](https://github.com/boromat/polanon#readme) shows the Python API, streaming
restore, the optional language model and the stage-2 judge.

## How well it detects

Detection is polanon's, and so is the benchmark that measures it. On its eleven annotated sample
documents, eight Polish and three invoices from abroad, the rules alone fully mask 86% of the values
and the rules with `pl_core_news_md` mask 98%, with nothing left half-masked; Presidio, on the same
documents with the same model, fully masks 66%, 76% at its most lenient. Eleven short documents are a
regression check, not a research benchmark, and they were written by polanon's author, who tuned
its rules on them. The [benchmark notes](https://github.com/boromat/polanon/blob/main/benchmarks/README.md)
give the numbers per entity type and say what is missed and why.

## Features

- **Chat like any assistant**, with conversations, history and streamed answers.
- **"What the model saw"** next to every message, so nobody has to trust the masking blindly.
- **File uploads**: PDF, DOCX, XLSX, CSV, JSON, TXT, and images and scanned PDFs when tesseract is
  installed. Files can require approval before their masked text is attached.
- **Stable tokens**: the same person is `<OSOBA_1>` for as long as the vault keeps the mapping (30 days
  by default), so the model can reason about them across turns.
- **Encrypted vault** for the token mapping, and **retention that actually deletes**: mappings, message
  history and uploaded files all disappear on the schedule the admin sets.
- **Audit log** recording entity types, counts and hashes, never the values themselves.

  ![The audit log: a sent message lists the kinds of data it masked, EMAIL, IBAN, ORG, PERSON,
  PESEL and PHONE, one of each, and no value.](docs/images/audit.png)

- **Admin policies**: approval mode per channel, stage-2 behaviour, retention, which entity types to
  mask, names to always mask, and names never to mask — Facebook, Google Ads, Canva and the other
  platforms a team talks about, matched in every Polish case form.
- **Any OpenAI-compatible provider**: OpenRouter, OpenAI, or a fully local Ollama model. With
  `LLM_API_KEY` left empty no `Authorization` header is sent, so an endpoint that needs no key works.

## Security

The hub holds the decrypted history and the mapping that reverses every mask, so the browser side is
treated as seriously as the outbound side.

- **No known original leaves.** `assert_no_raw_values` runs before every chat request to the provider
  (the `remote` stage-2 judge, a testing aid, sends stage-1 text without it) and raises if any
  known original appears in the payload letter for letter, outside the conversation's tokens; a short
  number such as a PIN counts where polanon would mask it, so a date holding its digits does
  not. Enforced in code and tests, not by asking the model nicely.
- **Content-Security-Policy with no `unsafe-inline` and no `unsafe-eval`.** There is no inline script,
  no inline event handler and no `style=` attribute anywhere in the templates; behaviour lives in one
  file under `/static` behind `data-` hooks. A test fails if any of it creeps back.
- **CSRF**: `SameSite=Lax` session cookies plus an origin check that refuses cross-site state-changing
  requests.
- **Sign-in**: argon2id, an account lockout after repeated failures counted from the audit trail and
  holding against parallel guesses, sessions that end everywhere when the password changes, a
  10-character minimum, and `?next=` restricted to paths inside the hub.
- **At rest**: Fernet for the vault, message history and extracted file text; uploads stored `0600`.

`docs/THREAT_MODEL.md` is the honest version, including what none of this protects against.

## The engine and the hub

| Part | Where it lives | Purpose |
|---|---|---|
| [polanon](https://github.com/boromat/polanon) | its own repository, installed from [PyPI](https://pypi.org/project/polanon/) | the anonymization library: guards, judge, stable tokens, stream restore, output sweep |
| `polgate/` | this repository | the hub: FastAPI and HTMX interface, accounts, vault, files, audit, LLM routing |

polanon is not copied into this repository. `pyproject.toml` declares `polanon[ner,judge]`, and pip
fetches it from PyPI, spaCy included, like any other dependency; the Docker image installs it the
same way when it is built. polanon never imports the hub, and `tests/test_layering.py` checks the
installed version for that, and for the promise that importing it pulls in nothing but the
standard library.

A missed value, a false positive or a new detector is a change to polanon, so it goes to
[polanon's repository](https://github.com/boromat/polanon) and reaches the hub in a polanon release.

## Run from source

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"     # fetches polanon and spaCy from PyPI
.venv/bin/python -m spacy download pl_core_news_md
./run.sh
```

`run.sh` checks the environment, creates `.env` with freshly generated keys on the first run, and
starts the hub on http://127.0.0.1:8000 with auto-reload. It tells you the exact command to run if
something is missing, and it never installs system packages or overwrites an existing `.env`.

```bash
./run.sh 8080         # a different port
./run.sh --no-reload  # no file watcher
```

Sign in as `admin` with the password from `ADMIN_PASSWORD` in `.env`, or, when that is empty, from
`data/initial-admin-password`. Then change it from the account page: the admin panel keeps warning
you until you do. Set `LLM_API_KEY` to talk to a provider that needs a key; without a provider you
can still exercise masking, uploads, previews and the admin panel.

**On Windows**, double-click `start.bat` in the checkout, or run it from a terminal. It needs
Python 3.12 or newer from [python.org](https://www.python.org/downloads/); on its first run it
creates `.venv`, installs the hub and the spaCy model and writes `.env` with generated keys, which
takes a few minutes. Then it starts the hub and opens it in the browser. It takes the same port and
`--no-reload` arguments as `run.sh`, plus `--no-browser`.

```bat
start.bat
start.bat 8080 --no-reload
```

The same steps by hand, in PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m spacy download pl_core_news_md
copy .env.example .env     # then set LLM_API_KEY in it
python -m uvicorn polgate.main:app --reload
```

With the environment activated, the commands further down work without their `.venv/bin/` prefix.

For a company deployment, `docker compose up -d` builds the hub from this checkout and starts it.
It reads `.env` when there is one, so copy `.env.example` to `.env` first and set `LLM_API_KEY` in
it; keys left empty are generated into the data volume, as with `docker run`.
`docker compose --profile ollama up -d` adds a local Ollama for the stage-2 judge: set
`JUDGE_BACKEND=ollama` in `.env` and pull the model once with
`docker compose exec ollama ollama pull qwen2.5:1.5b`. Behind HTTPS, set `COOKIE_SECURE=true` in
`.env`. Keep a copy of the vault key (`VAULT_KEY`, or `vault.key` in the data volume) somewhere
other than the server: without it the stored history cannot be read again.

### Upgrading

With Docker, pull the new image and start the container again. From a source checkout, `git pull`
and run `.venv/bin/pip install -e ".[dev]"` before restarting, so that a dependency added or raised
since, polanon included, is installed. A column a newer model added is
applied at startup and backfilled with its default, so an existing database keeps working.
`run.sh` stops instead of starting the hub when the schema needs a decision it should not make on
its own, and prints what to run. An entity type a newer release detects is switched on in an
existing policy, unless masking is off, and the audit trail records it.

```bash
.venv/bin/python -m polgate.schema     # add what a newer model needs, report anything else
```

### Configuration

| Variable | Meaning |
|---|---|
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | the OpenAI-compatible provider; an empty key sends no `Authorization` header |
| `JUDGE_BACKEND` | stage 2: `mock` (the default, a demo stub that flags two fixed phrases), `ollama` (a local model) or `remote` (the provider above; masked text leaves the network, testing only) |
| `OLLAMA_BASE_URL`, `OLLAMA_JUDGE_MODEL`, `JUDGE_TIMEOUT_SECONDS` | the local judge |
| `VAULT_KEY` | Fernet key; generated into `vault.key` beside the database when empty. Back it up |
| `SECRET_KEY` | signs session cookies; generated into `session.key` when empty. Rotating it signs everyone out |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | the first administrator; left empty, a password is generated into `initial-admin-password` |
| `COOKIE_SECURE`, `SESSION_MAX_AGE_DAYS` | cookie hardening and session lifetime |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_MINUTES` | sign-in throttling |
| `PENDING_TURN_TTL_MINUTES` | how long a queued message waits for its stream |
| `HISTORY_BUDGET_CHARS`, `ATTACHMENT_BUDGET_CHARS` | how much conversation and how much of each attached document is replayed per turn |
| `SPACY_MODEL`, `DEFAULT_LANGUAGE` | NER model and the UI language a new visitor gets |

Retention, approval mode, the entity types to mask, and the names to always or never mask are set
in the admin panel rather than in `.env`, because they are policy rather than deployment.

## What this is not

**Foreign documents are covered by format, not by language.** The first real provider run lost
company names, addresses, phones, VAT numbers and Swedish giro accounts from English, UK and Swiss
invoices, and refused five of them outright. `InternationalGuard` now reads those values by their
labels and official shapes, and on the same samples nothing is refused and only city names still
leave. A name in running English or German prose, and any format the rules have never seen, still
depends on a Polish language model that reads those languages poorly.

Places, and names the guards do not know, are detected statistically, so some will be missed —
on polanon's benchmark, detection misses three places in its eleven documents. A surname standing alone in
lowercase, with no first name or form of address next to it, is left to that model, which leans on
capital letters.
Identification by context ("the finance director of the only steelworks in town") is exactly what
stage 2 is for, and a small local model will not catch all of it either. Read
`docs/THREAT_MODEL.md` before relying on this, and `docs/GDPR_NOTES.md` for how it maps to
pseudonymisation. Neither is legal advice, and running this hub does not by itself make an
organisation compliant with anything.

## Development

```bash
.venv/bin/pytest --cov                                    # 98% coverage
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy polgate                                    # strict
```

Detection is developed and measured in [polanon](https://github.com/boromat/polanon), so this
repository has no benchmark of its own. `CONTRIBUTING.md` has the full quality bar,
`docs/ARCHITECTURE.md` the data flow, `CHANGELOG.md` what changed, and `CLAUDE.md` the rules an AI
assistant should follow when working in this repository.

## License

MIT
