"""Builds the stage-2 judge from settings and the admin policy."""

from __future__ import annotations

import logging

from polanon import Judge, MockJudge
from polanon.judge.ollama import OllamaJudge
from polanon.judge.remote import RemoteJudge

from polgate.config import Settings
from polgate.models import Policy

logger = logging.getLogger(__name__)

#: Phrases the mock judge flags, so the demo shows stage 2 doing something visible.
_MOCK_PHRASES = {
    "dyrektor jedynej huty w Stalowej Woli": "PERSON",
    "właściciel trzech aptek w Sanoku": "PERSON",
}


def build_judge(settings: Settings, policy: Policy) -> Judge | None:
    """Return the configured judge, or None when stage 2 is switched off."""
    if not policy.judge_enabled:
        return None

    backend = settings.judge_backend.lower()
    if backend == "ollama":
        return OllamaJudge(
            base_url=settings.ollama_base_url,
            model=settings.ollama_judge_model,
            timeout=settings.judge_timeout_seconds,
        )
    if backend == "remote":
        logger.warning(
            "Stage-2 judge is remote: masked text leaves the network. Use only for testing."
        )
        return RemoteJudge(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.judge_timeout_seconds,
        )
    if backend != "mock":
        logger.warning("Unknown JUDGE_BACKEND %r, falling back to the mock judge", backend)
    return MockJudge(_MOCK_PHRASES)
