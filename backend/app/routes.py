"""Router registration. Each module owns its own APIRouter."""

from importlib import import_module

from fastapi import FastAPI

_ROUTERS = [
    "app.platform.auth",
    "app.platform.documents",
    "app.appraisal.review",
    "app.answering.facts_review",
    "app.measurements.review",
    "app.platform.search",
    "app.answering.api",
    "app.chat.api",
    "app.platform.admin",
]


def register_routes(app: FastAPI) -> None:
    for module_name in _ROUTERS:
        app.include_router(import_module(module_name).router)
