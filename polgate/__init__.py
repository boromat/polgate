"""polgate: self-hosted company LLM hub with local PII anonymization.

The masking itself is the polanon library, installed from PyPI. Package layout (see
docs/ARCHITECTURE.md and docs/HANDOFF.md):
    config         settings from .env
    db, models     SQLite via SQLModel
    auth           passwords, cookie sessions, role dependencies
    judge_factory  builds polanon's stage-2 judge from settings
    vault          encrypted token -> original value store
    llm            OpenAI-compatible client, routing, streaming restore, output sweep
    files          text extraction from uploads
    audit          audit log (hashes only, never raw PII)
    web            HTML routers (Jinja2 + HTMX)
"""

__version__ = "0.1.0"
