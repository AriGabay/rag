"""Runtime configuration. Every secret comes from the environment; nothing is hard-coded."""

import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LLM_PROVIDER = "openai"
DEFAULT_OPENAI_MODEL = "gpt-6-luna"
# The model purposes with their own model and reasoning-effort settings (KTD1, R27). Every other call (the
# connection test, the earlier answer path) uses the agent's model at OPENAI_REASONING_EFFORT.
MODEL_PURPOSES = ("agent", "resolve", "verify", "measure", "vision", "summary")
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")  # a dated snapshot of a model: gpt-6-luna-2026-09-30


def base_model(model: str | None) -> str:
    """The model a dated snapshot belongs to (``gpt-6-luna-2026-09-30`` -> ``gpt-6-luna``)."""
    return _SNAPSHOT.sub("", model or "")


class ModelCapabilities(BaseModel):
    """What one model accepts beyond the required parameters (KTD1). Only what is listed here is ever sent."""

    efforts: tuple[str, ...]  # reasoning efforts the model accepts
    prompt_cache_options: bool = False  # ``prompt_cache_options`` (implicit or explicit cache mode)
    prompt_cache_key: bool = False  # ``prompt_cache_key``, the cache routing and accounting key
    image_detail: tuple[str, ...] = ()  # accepted ``detail`` values of an input image


# Verified against the provider's documentation (2026-10-08). A model absent from the table is sent no optional
# parameter at all (no reasoning effort, cache options, cache key or image detail). Neither model takes
# ``temperature``; gpt-6-luna has no "minimal" effort; ``prompt_cache_retention`` is deprecated and never sent.
MODEL_CAPABILITIES = {
    "gpt-6-luna": ModelCapabilities(efforts=("none", "low", "medium", "high", "xhigh", "max"),
                                    prompt_cache_options=True, prompt_cache_key=True,
                                    image_detail=("low", "high", "original", "auto")),
    "gpt-5.4-mini": ModelCapabilities(efforts=("none", "low", "medium", "high", "xhigh"), prompt_cache_key=True,
                                      image_detail=("low", "high", "auto")),
}


def model_capabilities(model: str | None) -> ModelCapabilities | None:
    """The capability row of a model or of the model its dated snapshot belongs to; None when unknown."""
    return MODEL_CAPABILITIES.get(model or "") or MODEL_CAPABILITIES.get(base_model(model))


class ModelPrice(BaseModel):
    """A model's list price in USD per million tokens, for estimating the cost of logged calls (KTD2).

    ``cache_write`` None: writing to the prompt cache costs nothing beyond ordinary input. Above
    ``long_context_threshold`` input tokens the whole request is billed at the multiplied rates."""

    input: float
    cached_input: float
    cache_write: float | None = None
    output: float
    long_context_threshold: int | None = None
    long_context_input_multiplier: float = 1.0  # applies to input, cached input and cache-write rates
    long_context_output_multiplier: float = 1.0


# Verified against the provider's published prices (2026-10-08). A model absent from the table is priced as
# unknown, never as free. Override wholesale with LLM_PRICES (JSON: {model: {input, cached_input, ...}}).
DEFAULT_LLM_PRICES = {
    "gpt-6-luna": ModelPrice(input=0.10, cached_input=0.01, cache_write=0.125, output=0.50,
                             long_context_threshold=272_000, long_context_input_multiplier=2.0,
                             long_context_output_multiplier=1.5),
    "gpt-5.4-mini": ModelPrice(input=0.75, cached_input=0.075, output=4.50),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Runtime connection uses rag_app (NOBYPASSRLS). Migrations use rag_owner.
    database_url: str = "postgresql+psycopg://rag_app:change-me-app@localhost:5433/rag"
    owner_database_url: str = "postgresql+psycopg://rag_owner:change-me-owner@localhost:5433/rag"

    file_storage_root: str = "./data/files"
    session_secret: str = "dev-only-session-secret"
    session_ttl_hours: int = 12
    cookie_secure: bool = False

    demo_mode: bool = True
    embedding_provider: str = "local"  # local | hash
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384

    # Cloud model provider (KTD4). Keys are SecretStr so repr, model_dump and logs never show them.
    llm_provider: Literal["openai", "anthropic"] = DEFAULT_LLM_PROVIDER
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-5-5"
    # Resolved key: the first non-empty of OPENAI_KEY, then OPENAI_API_KEY (Compose injects both, maybe empty).
    openai_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    # OPENAI_MODEL is every purpose's model, a single switch for all of them (the evaluation's comparison
    # columns); MODEL_<PURPOSE> overrides it for one purpose. EFFORT_<PURPOSE> is that purpose's reasoning effort,
    # checked against the model's capabilities at startup.
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_reasoning_effort: str = "none"  # calls with no purpose setting; empty means: send no reasoning setting
    model_agent: str = ""  # one step of the conversational answering loop
    model_resolve: str = ""  # resolving a follow-up in its context (a small structured call)
    model_verify: str = ""  # the answer verifier (judge)
    model_measure: str = ""  # measurements with their meaning (a background job)
    model_vision: str = ""  # reading a picture embedded in a document (ingestion)
    model_summary: str = ""  # the conversation summary
    effort_agent: str = "low"
    effort_resolve: str = "none"
    effort_verify: str = "low"  # the verifier reads meaning, not only words
    effort_measure: str = "low"  # completeness over speed
    effort_vision: str = "low"
    effort_summary: str = "none"
    # Price table for the cost of logged model calls; no budget logic reads it.
    llm_prices: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_LLM_PRICES))

    # Per-purpose model call timeouts, all below the turn deadline.
    llm_timeout_interpret_seconds: float = 20
    llm_timeout_clarify_seconds: float = 15
    llm_timeout_extract_seconds: float = 30
    llm_timeout_answer_seconds: float = 30
    llm_timeout_verify_seconds: float = 45  # a judge call reads every claim of an answer with its evidence
    llm_timeout_test_seconds: float = 20
    llm_timeout_vision_seconds: float = 90
    llm_timeout_resolve_seconds: float = 30
    llm_timeout_summary_seconds: float = 60  # a background call after the answer is stored
    # The conversational answering loop: its step bound and wall clock (KTD12). Every limit the loop reaches ends
    # the reading with a forced final step and a visible limitation, never an answer that looks complete.
    # model steps of a turn, its repair rounds included: reading takes up to 8, the last of them forced to answer
    chat_max_steps: int = 10
    chat_turn_seconds: int = 150  # verification may run ``VERIFY_ALLOWANCE_SECONDS`` past it
    chat_workers: int = 6
    chat_run_inline: bool = False  # tests: run a turn inside the request instead of a background thread
    chat_max_inspections: int = 3  # visual readings (``inspect``) one turn may make with the vision model
    # characters of tool output one turn may send the model; past it, reading tools return a header only (0: none)
    chat_tool_output_chars: int = 60_000
    chat_read_chars: int = 3500  # a part of a section, a page range or the paragraphs around a source
    chat_table_rows: int = 40  # rows in a part of a table
    chat_passage_chars: int = 1600  # a search hit
    # The verification reserve: reading stops this long before the turn's deadline, and one step per repair round
    # before its step bound, so the answer, its verification and its repair rounds still fit
    chat_repair_rounds: int = 2  # 1: a repair with tools; 2: then a rewrite from verified content only
    chat_verify_reserve_seconds: int = 40
    chat_verify_min_seconds: int = 15  # less time than this left for a verification fails the turn
    llm_timeout_agent_seconds: float = 60
    llm_timeout_measure_seconds: float = 90

    # Turn limits (KTD8, KTD13).
    turn_deadline_seconds: int = 45
    extract_sync_max_versions: int = 6
    extract_async_max_pending: int = 50
    extract_doc_char_budget: int = 60_000

    max_upload_mb: int = 50
    max_pages: int = 300
    max_batch_files: int = 20
    max_render_pixels: int = 40_000_000
    max_docx_uncompressed_mb: int = 100
    job_timeout_seconds: int = 900
    job_lease_seconds: int = 120
    job_max_attempts: int = 3

    ocr_languages: str = "heb+eng"
    ocr_dpi: int = 300
    ocr_timeout_seconds: int = 60  # per Tesseract call; a hung call fails the page instead of the whole job


    @field_validator("llm_provider", "openai_model", *(f"{kind}_{p}" for kind in ("model", "effort")
                                                        for p in MODEL_PURPOSES), mode="before")
    @classmethod
    def _empty_means_default(cls, value, info):
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value.strip() if isinstance(value, str) else value

    def model_for(self, purpose: str) -> tuple[str, str]:
        """(model, reasoning effort) of an OpenAI call of ``purpose``: its own settings for a purpose in
        ``MODEL_PURPOSES``, else the agent's model at ``openai_reasoning_effort``."""
        if purpose in MODEL_PURPOSES:
            return getattr(self, f"model_{purpose}") or self.openai_model, getattr(self, f"effort_{purpose}")
        return self.model_for("agent")[0], self.openai_reasoning_effort

    @model_validator(mode="after")
    def _check_efforts(self) -> "Settings":
        """An effort the purpose's model does not accept fails startup instead of being sent (KTD1)."""
        if self.llm_provider != "openai":
            return self
        checks = [(f"EFFORT_{p.upper()}", *self.model_for(p)) for p in MODEL_PURPOSES]
        checks.append(("OPENAI_REASONING_EFFORT", *self.model_for("test")))
        for name, model, effort in checks:
            caps = model_capabilities(model)
            if effort and caps is not None and effort not in caps.efforts:
                raise ValueError(f"{name}={effort!r} is not a reasoning effort {model} accepts"
                                 f" (allowed: {', '.join(caps.efforts)})")
        return self

    @model_validator(mode="after")
    def _resolve_openai_key(self) -> "Settings":
        for candidate in (self.openai_key, self.openai_api_key):
            if candidate.get_secret_value().strip():
                self.openai_api_key = SecretStr(candidate.get_secret_value().strip())
                break
        else:
            self.openai_api_key = SecretStr("")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
