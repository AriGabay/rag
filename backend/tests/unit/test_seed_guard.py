"""Round 25 U6: the seed scripts refuse the office stack unless told otherwise (KTD10).

The guard runs before any request or database write. It prints the target, needs the isolated-stack marker
(`RAG_STACK=isolated`) or an explicit confirmation flag, and refuses the office ports (5433, 8000) and the office
database name (`rag`) otherwise. No network, no database: the scripts are loaded from their files and every HTTP
client and engine is replaced by a function that fails the test."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS))

import seed_guard  # noqa: E402

FLAG = "--i-understand-office-target"
OFFICE_DB = "postgresql+psycopg://rag_owner:s3cret-owner-pw@localhost:5433/rag"
ISOLATED_DB = "postgresql+psycopg://rag_owner:s3cret-owner-pw@localhost:15433/rag"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("RAG_STACK", "BACKEND_URL", "OWNER_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


def _check(backend, db, *, stack=None, argv=()):
    env = {"BACKEND_URL": backend, "OWNER_DATABASE_URL": db}
    if stack is not None:
        env["RAG_STACK"] = stack
    return seed_guard.guard_seed_target("seed_x", list(argv), env)


def test_default_office_target_is_refused(capsys):
    with pytest.raises(SystemExit) as exc:
        seed_guard.guard_seed_target("seed_x", [], {})
    out = capsys.readouterr().out + str(exc.value)
    assert "localhost:8000" in out  # the target it would have written to is shown
    assert "RAG_STACK=isolated" in str(exc.value) and FLAG in str(exc.value)


@pytest.mark.parametrize("backend,db", [
    ("http://localhost:8000", ISOLATED_DB),
    ("http://127.0.0.1:18000", OFFICE_DB),
    ("http://localhost:18000", "postgresql+psycopg://u:p@localhost:15433/rag_other"),  # no marker: still refused
])
def test_without_marker_or_flag_everything_is_refused(backend, db):
    with pytest.raises(SystemExit):
        _check(backend, db)


def test_wrong_marker_value_is_refused():
    with pytest.raises(SystemExit):
        _check("http://localhost:18000", ISOLATED_DB, stack="office")


@pytest.mark.parametrize("backend,db", [
    ("http://localhost:18000", ISOLATED_DB),
    ("http://127.0.0.1:19000", "postgresql+psycopg://rag_owner:x@127.0.0.1:16000/rag"),
    ("http://localhost:18001", "postgresql+psycopg://rag_owner:x@localhost:15440/rag_iso"),
])
def test_isolated_target_is_accepted_and_printed(backend, db, capsys):
    target = _check(backend, db, stack="isolated")
    out = capsys.readouterr().out
    assert target.backend_port == int(backend.rsplit(":", 1)[1])
    assert backend in out and "stack=isolated" in out
    assert "s3cret" not in out and "rag_owner" not in out  # no credentials in the printed target


@pytest.mark.parametrize("backend,db", [
    ("http://localhost:8000", ISOLATED_DB),
    ("http://127.0.0.1:8000", ISOLATED_DB),
    ("http://localhost:18000", OFFICE_DB),
    ("http://localhost:18000", "postgresql+psycopg://u:p@127.0.0.1:5433/rag_test"),
])
def test_marker_does_not_excuse_office_ports(backend, db):
    with pytest.raises(SystemExit) as exc:
        _check(backend, db, stack="isolated")
    assert "office" in str(exc.value)


def test_office_ports_on_other_hosts_are_not_office():
    # Inside the isolated compose network the backend is `backend:8000` and the database `db:5432`.
    target = _check("http://backend:8000", "postgresql+psycopg://u:p@db:5432/rag", stack="isolated")
    assert target.database == "rag"


def test_office_database_name_alone_is_refused_without_marker():
    with pytest.raises(SystemExit) as exc:
        _check("http://localhost:18000", "postgresql+psycopg://u:p@localhost:15433/rag")
    assert "database" in str(exc.value)


def test_explicit_flag_allows_the_office_target_and_is_consumed(capsys):
    argv = [FLAG, "--other"]
    target = _check("http://localhost:8000", OFFICE_DB, argv=argv)
    assert argv == [FLAG, "--other"]  # the caller's list is not mutated
    assert target.confirmed_office is True
    out = capsys.readouterr().out
    assert "OFFICE" in out and "localhost:8000" in out and "s3cret" not in out


def test_flag_position_does_not_matter():
    assert _check("http://localhost:8000", OFFICE_DB, argv=["x", "y", FLAG]).confirmed_office


def test_database_url_is_the_fallback_for_the_owner_url():
    env = {"BACKEND_URL": "http://localhost:18000", "RAG_STACK": "isolated",
           "DATABASE_URL": "postgresql+psycopg://a:b@localhost:5433/rag"}
    with pytest.raises(SystemExit):
        seed_guard.guard_seed_target("seed_x", [], env)


def test_missing_database_url_is_shown_as_not_set(capsys):
    seed_guard.guard_seed_target("seed_x", [], {"BACKEND_URL": "http://localhost:18000", "RAG_STACK": "isolated"})
    assert "not set" in capsys.readouterr().out


def _load(name, monkeypatch):
    spec = importlib.util.spec_from_file_location(f"_guard_probe_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["seed_eval_office", "seed_demo"])
def test_scripts_refuse_the_default_target_before_any_request(name, monkeypatch):
    module = _load(name, monkeypatch)

    def boom(*a, **k):
        raise AssertionError("the script reached the network or the database before the guard")

    monkeypatch.setattr(module.httpx, "Client", boom)
    monkeypatch.setattr(module.httpx, "post", boom)
    monkeypatch.setattr(module, "create_engine", boom)
    monkeypatch.setattr(sys, "argv", [f"{name}.py"])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert "RAG_STACK=isolated" in str(exc.value)


@pytest.mark.parametrize("name", ["seed_eval_office", "seed_demo"])
def test_scripts_pass_the_guard_for_an_isolated_target(name, monkeypatch, capsys):
    monkeypatch.setenv("BACKEND_URL", "http://localhost:18000")
    monkeypatch.setenv("OWNER_DATABASE_URL", ISOLATED_DB)
    monkeypatch.setenv("RAG_STACK", "isolated")
    module = _load(name, monkeypatch)

    class Reached(Exception):
        pass

    def reached(*a, **k):
        raise Reached

    monkeypatch.setattr(module.httpx, "Client", reached)
    monkeypatch.setattr(module.httpx, "post", reached)
    monkeypatch.setattr(sys, "argv", [f"{name}.py"])
    with pytest.raises(Reached):  # past the guard, into the first request
        module.main()
    assert "localhost:18000" in capsys.readouterr().out
