"""Smoke check that the app module imports and exposes the health route."""

from app.main import create_app


def test_app_exposes_health_route():
    paths = {route.path for route in create_app().routes}
    assert "/api/health" in paths
    assert "/api/auth/login" in paths
