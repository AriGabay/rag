"""Runtime configuration. Every secret comes from the environment; nothing is hard-coded."""

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LLM_PROVIDER = "openai"
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"


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
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_reasoning_effort: str = "none"  # empty means: send no reasoning setting
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
    # The conversational answering loop: reasoning effort of its model steps, its step bound and wall clock.
    chat_reasoning_effort: str = "low"
    measure_reasoning_effort: str = "low"  # measurements: completeness over speed (a background job)
    judge_reasoning_effort: str = "low"  # the answer verifier reads meaning, not only words
    resolve_reasoning_effort: str = "low"  # resolving a follow-up in its context (a small structured call)
    chat_max_steps: int = 8
    chat_turn_seconds: int = 150
    chat_workers: int = 6
    chat_run_inline: bool = False  # tests: run a turn inside the request instead of a background thread
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


    @field_validator("llm_provider", "openai_model", mode="before")
    @classmethod
    def _empty_means_default(cls, value, info):
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value.strip() if isinstance(value, str) else value

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
