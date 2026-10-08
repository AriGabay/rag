"""Provider settings (U1, KTD4, R5, R6; KTD1, R27): key resolution, model default per purpose, secrets never
rendered."""

import logging

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_OPENAI_MODEL, MODEL_PURPOSES, Settings

FAKE_KEY = "test-openai-key-not-real-0001"
FAKE_FALLBACK = "test-openai-fallback-not-real-0002"
PROVIDER_ENV = ("OPENAI_KEY", "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT", "LLM_PROVIDER",
                "ANTHROPIC_API_KEY", *(f"{kind}_{p.upper()}" for kind in ("MODEL", "EFFORT") for p in MODEL_PURPOSES))
DEFAULT_EFFORTS = {"agent": "low", "verify": "low", "vision": "low", "measure": "low", "resolve": "none",
                   "summary": "none"}


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
    assert settings().openai_model == DEFAULT_OPENAI_MODEL == "gpt-6-luna"


def test_model_from_env(env):
    env.setenv("OPENAI_MODEL", "x")
    assert settings().openai_model == "x"


def test_every_purpose_defaults_to_luna_with_its_effort(env):
    s = settings()
    assert set(MODEL_PURPOSES) == set(DEFAULT_EFFORTS)
    assert {p: s.model_for(p) for p in MODEL_PURPOSES} == {p: ("gpt-6-luna", e) for p, e in DEFAULT_EFFORTS.items()}
    # the calls with no purpose setting of their own (connection test, earlier answer path) ride on the agent's model
    assert s.model_for("test") == ("gpt-6-luna", "none")


def test_single_override_switches_every_purpose(env):
    env.setenv("OPENAI_MODEL", "gpt-5.4-mini")
    s = settings()
    assert {s.model_for(p)[0] for p in (*MODEL_PURPOSES, "test")} == {"gpt-5.4-mini"}
    assert {p: s.model_for(p)[1] for p in MODEL_PURPOSES} == DEFAULT_EFFORTS


def test_a_purpose_model_overrides_only_that_purpose(env):
    env.setenv("MODEL_VISION", "gpt-5.4-mini")
    s = settings()
    assert {p: s.model_for(p)[0] for p in MODEL_PURPOSES} == {p: "gpt-5.4-mini" if p == "vision" else "gpt-6-luna"
                                                               for p in MODEL_PURPOSES}
    env.setenv("OPENAI_MODEL", "gpt-5.4-mini")
    env.setenv("MODEL_VISION", "gpt-6-luna")
    env.setenv("EFFORT_VISION", "high")
    s = settings()
    assert s.model_for("vision") == ("gpt-6-luna", "high") and s.model_for("agent") == ("gpt-5.4-mini", "low")


def test_empty_purpose_settings_mean_default(env):
    """Compose passes every per-purpose variable through, empty when unset."""
    for p in MODEL_PURPOSES:
        env.setenv(f"MODEL_{p.upper()}", "")
        env.setenv(f"EFFORT_{p.upper()}", " ")
    s = settings()
    assert {p: s.model_for(p) for p in MODEL_PURPOSES} == {p: ("gpt-6-luna", e) for p, e in DEFAULT_EFFORTS.items()}


@pytest.mark.parametrize("name, value, model", [
    ("EFFORT_AGENT", "minimal", None),  # gpt-6-luna has no "minimal"
    ("EFFORT_VERIFY", "max", "gpt-5.4-mini"),  # gpt-5.4-mini stops at xhigh
    ("EFFORT_SUMMARY", "lowest", None),
    ("OPENAI_REASONING_EFFORT", "minimal", None),
])
def test_unsupported_effort_fails_at_startup(env, name, value, model):
    env.setenv(name, value)
    if model:
        env.setenv("OPENAI_MODEL", model)
    with pytest.raises(ValidationError) as info:
        settings()
    message = str(info.value)
    assert name in message and repr(value) in message and (model or "gpt-6-luna") in message


def test_effort_of_an_unknown_model_is_not_checked(env):
    """A model the capability table does not know is sent no optional parameter, so its effort is never sent."""
    env.setenv("MODEL_MEASURE", "house-model")
    env.setenv("EFFORT_MEASURE", "minimal")
    assert settings().model_for("measure") == ("house-model", "minimal")


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
