"""Smoke check that the app module imports and exposes its core routes."""

from app.main import create_app


def test_app_exposes_core_routes():
    paths = set(create_app().openapi()["paths"])
    assert {"/api/health", "/api/auth/login", "/api/documents"} <= paths
