"""Transient LLM provider errors are retried; everything else fails fast."""
from __future__ import annotations

import pytest

import src.core.claude as claude_mod
from src.config import settings
from src.core.claude import ClaudeClient, _is_transient


class _ProviderError(Exception):
    """Stand-in for SDK errors carrying an HTTP-ish status attribute."""

    def __init__(self, message, *, code=None, status_code=None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


@pytest.fixture
def client(monkeypatch):
    """A Gemini-routed client with no real backend, audit log or sleeping."""
    monkeypatch.setattr(claude_mod, "log_action", lambda **kwargs: None)
    monkeypatch.setattr(settings.llm, "max_retries", 2)
    monkeypatch.setattr(settings.llm, "retry_base_seconds", 1.0)

    c = ClaudeClient.__new__(ClaudeClient)
    c._provider = "gemini"
    c._model = "test-model"
    c._total_input_tokens = 0
    c._total_output_tokens = 0
    c._backend = None
    c.sleeps = []
    c._sleep = c.sleeps.append
    return c


def _script(client, outcomes):
    """Make the provider call return/raise each outcome in turn."""
    calls = []

    def fake(user_prompt, system_prompt, max_tokens, temperature):
        calls.append(user_prompt)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome, 10, 5

    client._generate_gemini = fake
    return calls


def test_transient_error_is_retried_then_succeeds(client):
    calls = _script(client, [
        _ProviderError("503 UNAVAILABLE. This model is currently experiencing high demand",
                       code=503),
        "ok",
    ])
    assert client.generate(user_prompt="hi") == "ok"
    assert len(calls) == 2
    assert client.sleeps == [1.0]


def test_permanent_error_fails_immediately_without_waiting(client):
    calls = _script(client, [_ProviderError("400 INVALID_ARGUMENT", code=400), "never"])
    with pytest.raises(_ProviderError):
        client.generate(user_prompt="hi")
    assert len(calls) == 1
    assert client.sleeps == []


def test_persistent_transient_error_gives_up_after_the_retries(client):
    busy = _ProviderError("overloaded", status_code=529)
    calls = _script(client, [busy, busy, busy, "never"])
    with pytest.raises(_ProviderError):
        client.generate(user_prompt="hi")
    assert len(calls) == 3  # first try + max_retries (2)
    assert client.sleeps == [1.0, 2.0]  # exponential backoff


@pytest.mark.parametrize("exc, expected", [
    (_ProviderError("server error", code=503), True),
    (_ProviderError("overloaded_error", status_code=529), True),
    (_ProviderError("429 RESOURCE_EXHAUSTED quota"), True),
    (_ProviderError("Connection error."), True),
    (_ProviderError("Request timed out."), True),
    (_ProviderError("400 INVALID_ARGUMENT", code=400), False),
    (_ProviderError("401 invalid api key", status_code=401), False),
    (ValueError("prompt not found"), False),
])
def test_transient_classification(exc, expected):
    assert _is_transient(exc) is expected
