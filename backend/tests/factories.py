"""Test data builders. Offices are created through ``bootstrap_office`` (owner only);
everything else goes through the runtime role inside a tenant transaction."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from app.db import TenantContext, tenant_tx
from app.security import hash_password


@dataclass
class OfficeFixture:
    office_id: UUID
    admin_id: UUID
    default_group_id: UUID

    def ctx(self, role: str = "admin", user_id: UUID | None = None) -> TenantContext:
        return TenantContext(office_id=self.office_id, user_id=user_id or self.admin_id, role=role)

    @property
    def system(self) -> TenantContext:
        return TenantContext(office_id=self.office_id, user_id=None, role="system")


def make_office(owner_engine, name: str, admin_email: str, password: str = "secret-pass") -> OfficeFixture:
    with owner_engine.begin() as conn:
        office_id = conn.execute(
            text("SELECT bootstrap_office(:n, :e, :fn, :h)"),
            {"n": name, "e": admin_email, "fn": f"מנהל {name}", "h": hash_password(password)},
        ).scalar_one()
    sys_ctx = TenantContext(office_id=office_id, user_id=None, role="system")
    with tenant_tx(sys_ctx) as conn:
        admin_id = conn.execute(text("SELECT id FROM users WHERE role = 'admin'")).scalar_one()
        group_id = conn.execute(text("SELECT id FROM document_groups")).scalar_one()
    return OfficeFixture(office_id=office_id, admin_id=admin_id, default_group_id=group_id)


def make_group(office: OfficeFixture, name: str) -> UUID:
    with tenant_tx(office.ctx()) as conn:
        return conn.execute(
            text("INSERT INTO document_groups (office_id, name) VALUES (app_office(), :n) RETURNING id"), {"n": name}
        ).scalar_one()


def make_user(
    office: OfficeFixture, email: str, groups: list[UUID], role: str = "employee", can_upload: bool = False,
    password: str = "secret-pass",
) -> UUID:
    with tenant_tx(office.ctx()) as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (office_id, email, full_name, password_hash, role, can_upload)"
                " VALUES (app_office(), :e, :fn, :h, :r, :u) RETURNING id"
            ),
            {"e": email, "fn": email.split("@")[0], "h": hash_password(password), "r": role, "u": can_upload},
        ).scalar_one()
        for g in groups:
            conn.execute(
                text("INSERT INTO user_groups (user_id, group_id, office_id) VALUES (:u, :g, app_office())"),
                {"u": user_id, "g": g},
            )
    return user_id


def make_document(office: OfficeFixture, group_id: UUID, title: str = "מסמך", sha: str = "x" * 64) -> tuple[UUID, UUID]:
    with tenant_tx(office.ctx()) as conn:
        doc_id = conn.execute(
            text("INSERT INTO documents (office_id, group_id, title) VALUES (app_office(), :g, :t) RETURNING id"),
            {"g": group_id, "t": title},
        ).scalar_one()
        ver_id = conn.execute(
            text(
                "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
                " size_bytes, storage_key, status, is_current)"
                " VALUES (app_office(), :d, 1, :s, 'f.pdf', 'application/pdf', 10, 'k', 'ready', true) RETURNING id"
            ),
            {"d": doc_id, "s": sha},
        ).scalar_one()
    return doc_id, ver_id
