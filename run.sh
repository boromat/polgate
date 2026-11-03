#!/usr/bin/env bash
#
# Start the hub locally for testing.
#
#   ./run.sh              # http://127.0.0.1:8000, auto-reload on file changes
#   ./run.sh 8080         # a different port
#   ./run.sh --no-reload  # no file watcher, closer to how it runs in production
#
# The script verifies the environment first and tells you exactly what to run if something is
# missing. It never installs system packages, and it never overwrites an existing .env.

set -euo pipefail
cd "$(dirname "$0")"

PORT=8000
RELOAD="--reload"
for arg in "$@"; do
  case "$arg" in
    --no-reload) RELOAD="" ;;
    [0-9]*) PORT="$arg" ;;
    -h | --help)
      awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

PYTHON=".venv/bin/python"

fail() {
  echo
  echo "  $1"
  echo
  [ $# -gt 1 ] && printf '      %s\n\n' "$2"
  exit 1
}

# 1. Virtual environment.
if [ ! -x "$PYTHON" ]; then
  fail "No virtual environment found in .venv" \
    "sudo apt install -y python3.12-venv python3-pip && python3 -m venv .venv"
fi

# 2. Dependencies.
if ! "$PYTHON" -c "import fastapi, sqlmodel, spacy, polanon" >/dev/null 2>&1; then
  fail "Dependencies are missing" '.venv/bin/pip install -e ".[dev]"'
fi

# 3. Language model for the Polish name detector.
SPACY_MODEL="$(grep -E '^SPACY_MODEL=' .env 2>/dev/null | cut -d= -f2 || true)"
SPACY_MODEL="${SPACY_MODEL:-pl_core_news_md}"
if ! "$PYTHON" -c "import spacy; spacy.load('$SPACY_MODEL')" >/dev/null 2>&1; then
  fail "spaCy model '$SPACY_MODEL' is not installed" \
    ".venv/bin/python -m spacy download $SPACY_MODEL"
fi

# 4. Configuration. A first run gets a working .env with freshly generated keys.
if [ ! -f .env ]; then
  echo "No .env found, creating one from .env.example with generated keys."
  cp .env.example .env
  "$PYTHON" - <<'PY'
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

path = Path(".env")
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith("SECRET_KEY="):
        line = f"SECRET_KEY={secrets.token_urlsafe(48)}"
    elif line.startswith("VAULT_KEY="):
        line = f"VAULT_KEY={Fernet.generate_key().decode()}"
    lines.append(line)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
  echo "Set LLM_API_KEY in .env to talk to a real provider."
fi

# 5. Database schema. Columns a newer model added are applied automatically; anything this cannot
#    put right on its own stops the run, because the hub would start and then fail on every message.
if ! SCHEMA_REPORT="$("$PYTHON" -m polgate.schema 2>&1)"; then
  echo
  echo "  The database does not match the current models:"
  echo
  echo "$SCHEMA_REPORT" | sed 's/^/      /' 
  echo
  exit 1
fi
case "$SCHEMA_REPORT" in
  *added:*) echo "Schema updated: $(echo "$SCHEMA_REPORT" | grep -c '^added:') column(s) added." ;;
esac

# 6. Warn about anything that limits what you can test, without blocking the run.
if ! grep -qE '^LLM_API_KEY=.+' .env; then
  echo "Note: LLM_API_KEY is empty. Masking, uploads and the admin panel work;"
  echo "      a provider that needs a key refuses messages until you set one in .env."
fi
if grep -qE '^ADMIN_PASSWORD=admin$' .env; then
  echo "Note: ADMIN_PASSWORD is still 'admin'. Change it in .env, or from the account page"
  echo "      once you have signed in, before this machine is reachable by anyone else."
fi
JUDGE="$(grep -E '^JUDGE_BACKEND=' .env | cut -d= -f2 || true)"
if [ "${JUDGE:-mock}" = "ollama" ] && ! command -v ollama >/dev/null 2>&1; then
  echo "Note: JUDGE_BACKEND=ollama but the ollama command is not installed."
  echo "      Stage 2 will fail and the fallback policy will apply."
fi

ADMIN_USER="$(grep -E '^ADMIN_USERNAME=' .env | cut -d= -f2 || echo admin)"
if grep -qE '^ADMIN_PASSWORD=.+' .env; then
  PASSWORD_HINT="the password from .env"
else
  PASSWORD_HINT="the password in data/initial-admin-password (written on first start)"
fi
echo
echo "  Hub starting on http://127.0.0.1:$PORT"
echo "  Sign in as '${ADMIN_USER:-admin}' with $PASSWORD_HINT"
echo "  Press Ctrl+C to stop"
echo

# shellcheck disable=SC2086 # RELOAD is intentionally word-split or empty.
exec .venv/bin/uvicorn polgate.main:app --host 127.0.0.1 --port "$PORT" $RELOAD
