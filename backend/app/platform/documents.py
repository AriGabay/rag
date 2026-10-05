"""Document upload, listing, versions, and authenticated file access (U4, U12)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import PurePath
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import Connection, text

from app.audit import audit
from app.config import get_settings
from app.db import TenantContext, bump_data_version, tenant_tx
from app.deps import FORBIDDEN, NOT_FOUND, get_ctx, parse_uuid
from app.platform.jobs import enqueue_processing
from app.platform.storage import get_storage, storage_key

router = APIRouter(prefix="/api/documents", tags=["documents"])

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

MSG_TYPE = "סוג הקובץ אינו נתמך. ניתן להעלות PDF או DOCX"
MSG_SIZE = "הקובץ גדול מהמותר ({mb}MB)"
MSG_EMPTY = "הקובץ ריק"
MSG_ZIP = "קובץ DOCX פגום או חורג ממגבלות הגודל"
MSG_BATCH = "ניתן להעלות עד {n} קבצים בבת אחת"
MSG_DUP = "הקובץ כבר הועלה למאגר"


def _sniff(filename: str, data: bytes) -> str | None:
    """Return the mime type when extension and magic bytes agree, else None."""
    ext = PurePath(filename).suffix.lower()
    if ext == ".pdf" and b"%PDF-" in data[:1024]:
        return PDF_MIME
    if ext == ".docx" and data[:4] == b"PK\x03\x04":
        return DOCX_MIME
    return None


def _docx_within_limits(data: bytes) -> bool:
    limit = get_settings().max_docx_uncompressed_mb * 1024 * 1024
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            total = sum(i.file_size for i in zf.infolist())
    except zipfile.BadZipFile:
        return False
    return "word/document.xml" in names and total <= limit


def _can_use_group(conn: Connection, ctx: TenantContext, group_id: UUID) -> bool:
    exists = conn.execute(text("SELECT 1 FROM document_groups WHERE id = :g"), {"g": group_id}).first()
    if not exists:
        return False
    return ctx.is_admin or group_id in ctx.group_ids


def _visible_document(conn: Connection, document_id: UUID) -> bool:
    return conn.execute(
        text("SELECT 1 FROM documents WHERE id = :d AND deleted_at IS NULL"), {"d": document_id}
    ).first() is not None


def _store_one(ctx: TenantContext, filename: str, data: bytes, group_id: UUID, document_id: UUID | None) -> dict:
    settings = get_settings()
    result: dict = {"filename": filename}
    if not data:
        return result | {"status": "rejected", "reason": MSG_EMPTY}
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        return result | {"status": "rejected", "reason": MSG_SIZE.format(mb=settings.max_upload_mb)}
    mime = _sniff(filename, data)
    if mime is None:
        return result | {"status": "rejected", "reason": MSG_TYPE}
    if mime == DOCX_MIME and not _docx_within_limits(data):
        return result | {"status": "rejected", "reason": MSG_ZIP}

    sha = hashlib.sha256(data).hexdigest()
    with tenant_tx(ctx) as conn:
        if document_id is not None:
            group_row = conn.execute(
                text("SELECT group_id FROM documents WHERE id = :d AND deleted_at IS NULL"), {"d": document_id}
            ).first()
            if group_row is None:
                return result | {"status": "rejected", "reason": NOT_FOUND}
            group_id = group_row.group_id
            if not _can_use_group(conn, ctx, group_id):
                return result | {"status": "rejected", "reason": FORBIDDEN}

        clone_from: UUID | None = None
        for hit in conn.execute(text("SELECT * FROM docs_hash_lookup(:s)"), {"s": sha}).all():
            if _visible_document(conn, hit.document_id) and (document_id is None or hit.document_id == document_id):
                return result | {"status": "duplicate", "reason": MSG_DUP, "document_id": str(hit.document_id),
                                 "version_id": str(hit.version_id)}
            if hit.status in ("ready", "needs_review"):
                clone_from = hit.version_id  # same file held where this user cannot see: reuse outputs

        key = storage_key(ctx.office_id, sha)
        get_storage().put(key, data)
        if document_id is None:
            title = PurePath(filename).stem.replace("_", " ")[:200]
            document_id = conn.execute(
                text(
                    "INSERT INTO documents (office_id, group_id, title, created_by)"
                    " VALUES (app_office(), :g, :t, :u) RETURNING id"
                ),
                {"g": group_id, "t": title, "u": ctx.user_id},
            ).scalar_one()
        version_no = conn.execute(
            text("SELECT COALESCE(max(version_no), 0) + 1 FROM document_versions WHERE document_id = :d"),
            {"d": document_id},
        ).scalar_one()
        version_id = conn.execute(
            text(
                "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
                " size_bytes, storage_key, uploaded_by, cloned_from_version_id)"
                " VALUES (app_office(), :d, :n, :s, :f, :m, :b, :k, :u, :c) RETURNING id"
            ),
            {"d": document_id, "n": version_no, "s": sha, "f": filename[:255], "m": mime, "b": len(data), "k": key,
             "u": ctx.user_id, "c": clone_from},
        ).scalar_one()
        enqueue_processing(conn, version_id)
        audit(conn, "upload", ctx.user_id, "document_version", version_id, document_id=document_id,
              version_no=version_no, size=len(data))
    return result | {"status": "accepted", "document_id": str(document_id), "version_id": str(version_id)}


@router.post("")
def upload(
    files: list[UploadFile] = File(...),
    group_id: str | None = Form(None),
    document_id: str | None = Form(None),
    ctx: TenantContext = Depends(get_ctx),
) -> dict:
    settings = get_settings()
    if not ctx.can_upload:
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN)
    if len(files) > settings.max_batch_files:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, MSG_BATCH.format(n=settings.max_batch_files))
    doc_uuid = parse_uuid(document_id) if document_id else None
    if doc_uuid is None:
        if not group_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "יש לבחור קבוצת מסמכים")
        group_uuid = parse_uuid(group_id)
        with tenant_tx(ctx) as conn:
            if not _can_use_group(conn, ctx, group_uuid):
                raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN)
    else:
        group_uuid = UUID(int=0)  # replaced by the document's own group

    results = []
    limit = settings.max_upload_mb * 1024 * 1024
    for f in files:
        data = f.file.read(limit + 1)
        results.append(_store_one(ctx, f.filename or "file", data, group_uuid, doc_uuid))
    return {"results": results}


_VERSION_COLS = (
    "v.id, v.version_no, v.filename, v.status, v.status_reason, v.page_count, v.pages_incomplete,"
    " v.records_total, v.records_needing_review, v.is_current, v.created_at, v.processed_at, v.mime_type"
)


def _version_json(row) -> dict:
    return {
        "id": str(row.id), "version_no": row.version_no, "filename": row.filename, "status": row.status,
        "status_reason": row.status_reason, "page_count": row.page_count, "pages_incomplete": row.pages_incomplete,
        "records_total": row.records_total, "records_needing_review": row.records_needing_review,
        "is_current": row.is_current, "created_at": row.created_at.isoformat(),
        "processed_at": row.processed_at.isoformat() if row.processed_at else None, "mime_type": row.mime_type,
    }


def _list_documents(conn: Connection, ctx: TenantContext, q: str | None, status_filter: str | None,
                    document_id: UUID | None = None) -> list[dict]:
    rows = conn.execute(
        text(
            "SELECT d.id, d.title, d.created_at, d.deleted_at, g.id AS group_id, g.name AS group_name,"
            " (SELECT count(*) FROM document_versions v WHERE v.document_id = d.id) AS versions_count"
            " FROM documents d JOIN document_groups g ON g.id = d.group_id"
            " WHERE (:admin OR d.deleted_at IS NULL)"
            " AND (CAST(:q AS text) IS NULL OR d.title ILIKE '%' || CAST(:q AS text) || '%')"
            " AND (CAST(:doc AS uuid) IS NULL OR d.id = CAST(:doc AS uuid))"
            " ORDER BY d.created_at DESC"
        ),
        {"admin": ctx.is_admin, "q": q or None, "doc": document_id},
    ).all()
    out = []
    for r in rows:
        ver = conn.execute(
            text(f"SELECT {_VERSION_COLS} FROM document_versions v WHERE v.document_id = :d"
                 " ORDER BY v.version_no DESC LIMIT 1"),
            {"d": r.id},
        ).first()
        if status_filter and (ver is None or ver.status != status_filter):
            continue
        out.append({
            "id": str(r.id), "title": r.title, "group": {"id": str(r.group_id), "name": r.group_name},
            "deleted": r.deleted_at is not None, "created_at": r.created_at.isoformat(),
            "current_version": _version_json(ver) if ver else None, "versions_count": r.versions_count,
        })
    return out


@router.get("")
def list_documents(q: str | None = None, status_filter: str | None = None, ctx: TenantContext = Depends(get_ctx)):
    with tenant_tx(ctx) as conn:
        return {"documents": _list_documents(conn, ctx, q, status_filter)}


@router.get("/{document_id}")
def get_document(document_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    doc_uuid = parse_uuid(document_id)
    with tenant_tx(ctx) as conn:
        docs = _list_documents(conn, ctx, None, None, doc_uuid)
        if not docs:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        versions = conn.execute(
            text(f"SELECT {_VERSION_COLS} FROM document_versions v WHERE v.document_id = :d ORDER BY v.version_no DESC"),
            {"d": doc_uuid},
        ).all()
    return docs[0] | {"versions": [_version_json(v) for v in versions]}


@router.delete("/{document_id}")
def delete_document(document_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    doc_uuid = parse_uuid(document_id)
    with tenant_tx(ctx) as conn:
        row = conn.execute(
            text("SELECT created_by FROM documents WHERE id = :d AND deleted_at IS NULL"), {"d": doc_uuid}
        ).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        if not (ctx.role == "admin" or (ctx.can_upload and row.created_by == ctx.user_id)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN)
        conn.execute(
            text("UPDATE documents SET deleted_at = now(), deleted_by = :u WHERE id = :d"),
            {"u": ctx.user_id, "d": doc_uuid},
        )
        bump_data_version(conn)
        audit(conn, "document_delete", ctx.user_id, "document", doc_uuid)
    return {"ok": True}


@router.get("/{document_id}/versions/{version_id}/file")
def get_file(document_id: str, version_id: str, ctx: TenantContext = Depends(get_ctx)) -> Response:
    doc_uuid, ver_uuid = parse_uuid(document_id), parse_uuid(version_id)
    with tenant_tx(ctx) as conn:
        row = conn.execute(
            text(
                "SELECT v.storage_key, v.mime_type, v.filename FROM document_versions v"
                " JOIN documents d ON d.id = v.document_id"
                " WHERE v.id = :v AND d.id = :d AND d.deleted_at IS NULL"
            ),
            {"v": ver_uuid, "d": doc_uuid},
        ).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        audit(conn, "source_view", ctx.user_id, "document_version", ver_uuid)
    data = get_storage().get(row.storage_key)
    disposition = "inline" if row.mime_type == PDF_MIME else "attachment"
    return Response(
        content=data,
        media_type=row.mime_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(row.filename)}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
