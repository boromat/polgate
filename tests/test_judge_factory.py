"""Choosing the stage-2 judge from settings and policy."""

import pytest
from polanon import MockJudge
from polanon.judge.ollama import OllamaJudge
from polanon.judge.remote import RemoteJudge

from polgate.config import Settings
from polgate.judge_factory import build_judge
from polgate.models import Policy


def _settings(backend: str) -> Settings:
    return Settings(judge_backend=backend, llm_api_key="k", ollama_base_url="http://ollama.test")


def test_stage_two_is_skipped_when_the_policy_switches_it_off():
    assert build_judge(_settings("mock"), Policy(judge_enabled=False)) is None


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("mock", MockJudge), ("ollama", OllamaJudge), ("remote", RemoteJudge)],
)
def test_each_backend_name_builds_its_judge(backend, expected):
    assert isinstance(build_judge(_settings(backend), Policy()), expected)


def test_an_unknown_backend_falls_back_to_the_mock_and_says_so(caplog):
    judge = build_judge(_settings("nie-ma-takiego"), Policy())
    assert isinstance(judge, MockJudge)
    assert "Unknown JUDGE_BACKEND" in caplog.text


def test_choosing_the_remote_judge_warns_that_text_leaves_the_network(caplog):
    build_judge(_settings("remote"), Policy())
    assert "leaves the network" in caplog.text


def test_the_backend_name_is_not_case_sensitive():
    assert isinstance(build_judge(_settings("OLLAMA"), Policy()), OllamaJudge)
