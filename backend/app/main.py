"""FastAPI application factory."""

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine

logger = logging.getLogger("app")


def create_app() -> FastAPI:
    app = FastAPI(title="Appraisal Knowledge Engine", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.get("/api/health")
    def health() -> JSONResponse:
        try:
            with get_engine().connect() as conn:
                row = conn.execute(
                    text("SELECT current_user, (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)")
                ).one()
            return JSONResponse({"status": "ok", "db_role": row[0], "db_bypass_rls": bool(row[1])})
        except Exception:  # noqa: BLE001 - health must never leak details
            logger.exception("health check failed")
            return JSONResponse({"status": "error"}, status_code=503)

    from app.routes import register_routes

    register_routes(app)

    @app.on_event("startup")
    def _startup() -> None:
        from app.db import check_database_locale

        check_database_locale()
        settings = get_settings()
        if settings.embedding_provider == "local":
            from app.providers.embeddings import get_embedding_provider

            get_embedding_provider().warmup()

    return app


app = create_app()
