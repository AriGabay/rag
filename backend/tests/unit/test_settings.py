"""Provider settings (U1, KTD4, R5, R6): key resolution, model default, secrets never rendered."""

import logging

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_OPENAI_MODEL, Settings

FAKE_KEY = "test-openai-key-not-real-0001"
FAKE_FALLBACK = "test-openai-fallback-not-real-0002"
PROVIDER_ENV = ("OPENAI_KEY", "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT", "LLM_PROVIDER",
                "ANTHROPIC_API_KEY")


@pytest.fixture
def env(monkeypatch):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def settings() -> Settings:
    return Settings(_env_file=None)


def test_openai_key_alone_resolves(env):
    env.setenv("OPENAI_KEY", FAKE_KEY)
    assert settings().openai_api_key.get_secret_value() == FAKE_KEY


def test_openai_key_wins_over_fallback(env):
    env.setenv("OPENAI_KEY", FAKE_KEY)
    env.setenv("OPENAI_API_KEY", FAKE_FALLBACK)
    assert settings().openai_api_key.get_secret_value() == FAKE_KEY


def test_fallback_used_when_only_openai_api_key_set(env):
    env.setenv("OPENAI_API_KEY", FAKE_FALLBACK)
    assert settings().openai_api_key.get_secret_value() == FAKE_FALLBACK


def test_empty_openai_key_counts_as_unset(env):
    """Compose always injects OPENAI_KEY, possibly empty; the fallback must still be read."""
    env.setenv("OPENAI_KEY", "")
    env.setenv("OPENAI_API_KEY", FAKE_FALLBACK)
    assert settings().openai_api_key.get_secret_value() == FAKE_FALLBACK


def test_no_key_resolves_to_empty(env):
    env.setenv("OPENAI_KEY", "")
    env.setenv("OPENAI_API_KEY", "")
    assert settings().openai_api_key.get_secret_value() == ""


@pytest.mark.parametrize("value", [None, "", "  "])
def test_model_defaults_when_unset_or_empty(env, value):
    if value is not None:
        env.setenv("OPENAI_MODEL", value)
    assert settings().openai_model == DEFAULT_OPENAI_MODEL == "gpt-5.4-mini"


def test_model_from_env(env):
    env.setenv("OPENAI_MODEL", "x")
    assert settings().openai_model == "x"


def test_provider_selection(env):
    assert settings().llm_provider == "openai"
    env.setenv("LLM_PROVIDER", "")
    assert settings().llm_provider == "openai"
    env.setenv("LLM_PROVIDER", "anthropic")
    assert settings().llm_provider == "anthropic"
    env.setenv("LLM_PROVIDER", "other")
    with pytest.raises(ValidationError):
        settings()


def test_keys_never_rendered(env, caplog):
    env.setenv("OPENAI_KEY", FAKE_KEY)
    env.setenv("OPENAI_API_KEY", FAKE_FALLBACK)
    env.setenv("ANTHROPIC_API_KEY", "test-anthropic-not-real-0003")
    s = settings()
    with caplog.at_level(logging.INFO):
        logging.getLogger("test.settings").info("settings: %s %s", s.model_dump(), s.model_dump_json())
    rendered = repr(s) + str(s) + caplog.text
    for secret in (FAKE_KEY, FAKE_FALLBACK, "test-anthropic-not-real-0003"):
        assert secret not in rendered
