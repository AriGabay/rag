"""Runtime configuration. Every secret comes from the environment; nothing is hard-coded."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
