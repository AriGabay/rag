"""Target guard shared by the seed scripts (round 25, U6, KTD10).

The seed scripts write to whatever stack `BACKEND_URL` and `OWNER_DATABASE_URL` point at, and both default to the
office stack (backend 8000, database 5433, database name `rag`). The guard runs first in `main()`: it prints the
target, and it lets the script continue only when

- `RAG_STACK=isolated` is set (the isolated stack of docker-compose.isolated.yml; `scripts/isolated_stack.sh seed`
  sets it) and the target does not use an office host port, or
- the operator passes `--i-understand-office-target` for an office run.

Without either, the office ports 5433 and 8000 on this host and the office database name `rag` are refused. The
isolated stack's own database is also called `rag`, inside its own container on another host port, so the name
alone is refused only when the marker is missing. Passwords are never printed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.engine import make_url

FLAG = "--i-understand-office-target"
OFFICE_BACKEND_PORT = 8000
OFFICE_DB_PORT = 5433
OFFICE_DB_NAME = "rag"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}  # noqa: S104


@dataclass(frozen=True)
class SeedTarget:
    backend_url: str
    backend_host: str
    backend_port: int | None
    db_host: str | None
    db_port: int | None
    database: str | None
    stack: str | None
    confirmed_office: bool

    def describe(self) -> str:
        db = f"{self.db_host}:{self.db_port}/{self.database}" if self.database else "not set"
        mark = f"stack={self.stack}" if self.stack else "stack=unmarked"
        return f"backend {self.backend_url} | database {db} | {mark}"


def _is_local(host: str | None) -> bool:
    return (host or "").lower() in LOCAL_HOSTS


def guard_seed_target(script: str, argv: list[str], env: dict[str, str] | None = None) -> SeedTarget:
    """Print the target and return it, or raise SystemExit. `argv` is only read; `env` defaults to os.environ."""
    env = os.environ if env is None else env
    backend_url = env.get("BACKEND_URL", "http://localhost:8000")
    parsed = urlparse(backend_url)
    backend_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    db_url = env.get("OWNER_DATABASE_URL") or env.get("DATABASE_URL")
    db_host = db_port = database = None
    if db_url:
        url = make_url(db_url)
        db_host, db_port, database = url.host, url.port or 5432, url.database
    target = SeedTarget(backend_url, parsed.hostname or "", backend_port, db_host, db_port, database,
                        env.get("RAG_STACK"), FLAG in argv)
    print(f"[{script}] target: {target.describe()}", flush=True)

    office_ports = []
    if _is_local(target.backend_host) and backend_port == OFFICE_BACKEND_PORT:
        office_ports.append(f"backend port {OFFICE_BACKEND_PORT}")
    if _is_local(db_host) and db_port == OFFICE_DB_PORT:
        office_ports.append(f"database port {OFFICE_DB_PORT}")

    if target.confirmed_office:
        print(f"[{script}] OFFICE target confirmed with {FLAG}", flush=True)
        return target
    if target.stack != "isolated":
        why = office_ports + ([f"database name {OFFICE_DB_NAME!r}"] if database == OFFICE_DB_NAME else [])
        raise SystemExit(
            f"{script}: refusing to write without a stack marker ({'; '.join(why) or 'target not marked'}). "
            f"Seed the isolated stack with RAG_STACK=isolated (scripts/isolated_stack.sh seed), or pass {FLAG} "
            "to seed the office stack on purpose.")
    if office_ports:
        raise SystemExit(
            f"{script}: RAG_STACK=isolated but the target uses the office {' and '.join(office_ports)}. "
            f"Point BACKEND_URL and OWNER_DATABASE_URL at the isolated stack, or pass {FLAG} for an office run.")
    return target
