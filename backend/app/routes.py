"""Router registration. Each module owns its own APIRouter."""

from importlib import import_module

from fastapi import FastAPI

_ROUTERS = [
    "app.platform.auth",
    "app.platform.documents",
]


def register_routes(app: FastAPI) -> None:
    for module_name in _ROUTERS:
        app.include_router(import_module(module_name).router)
