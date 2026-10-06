"""Document upload, listing, versions, and authenticated file access (U4, U12)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import PurePath
from types import SimpleNamespace
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
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


def source_file_url(document_id, version_id, page: int | None = None) -> str:
    """Authenticated source link; the browser's PDF viewer opens it at ``#page=N``."""
    return f"/api/documents/{document_id}/versions/{version_id}/file" + (f"#page={page}" if page else "")


def latest_status_counts(conn: Connection) -> dict[str, int]:
    """Count non-deleted documents by the status of their latest version."""
    return dict(conn.execute(text(
        "SELECT status, count(*) FROM (SELECT DISTINCT ON (v.document_id) v.status FROM document_versions v"
        " JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL"
        " ORDER BY v.document_id, v.version_no DESC) latest GROUP BY status")).all())


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


_PLAIN_COLS = (
    "v.id, v.version_no, v.filename, v.status, v.status_reason, v.page_count, v.pages_incomplete,"
    " v.records_total, v.records_needing_review, v.is_current, v.created_at, v.processed_at, v.mime_type,"
    " v.ingestion"
)
# What was read of a version, counted where it is stored (see ``_reading``).
_READING_COLS = (
    "passages", "tables_count", "measurements_count", "measurements_state",
)
_VERSION_COLS = (
    _PLAIN_COLS + ","
    " (SELECT count(*) FROM chunks c WHERE c.version_id = v.id AND c.kind <> 'table_row') AS passages,"
    " (SELECT count(*) FROM extracted_tables t WHERE t.version_id = v.id) AS tables_count,"
    " (SELECT count(*) FROM measurements m WHERE m.version_id = v.id AND m.status <> 'rejected') AS measurements_count,"
    " (SELECT r.state FROM measurement_runs r WHERE r.version_id = v.id ORDER BY r.updated_at DESC LIMIT 1)"
    " AS measurements_state"
)


def _version_json(row) -> dict:
    return {
        "id": str(row.id), "version_no": row.version_no, "filename": row.filename, "status": row.status,
        "status_reason": row.status_reason, "page_count": row.page_count, "pages_incomplete": row.pages_incomplete,
        "records_total": row.records_total, "records_needing_review": row.records_needing_review,
        "is_current": row.is_current, "created_at": row.created_at.isoformat(),
        "processed_at": row.processed_at.isoformat() if row.processed_at else None, "mime_type": row.mime_type,
        "reading": _reading(row),
    }


def _reading(row) -> dict:
    """What was read of a version, kept apart: searchable passages, tables, stored measurements, structured
    records, and pictures by status. ``partial`` when any picture or page was not read: zero structured records
    is not zero searchable content, and a document is never shown as fully read while parts were not."""
    ing = getattr(row, "ingestion", None) or {}
    images = ing.get("images") or {}
    return {
        "passages": getattr(row, "passages", 0) or 0, "tables": getattr(row, "tables_count", 0) or 0,
        "measurements": getattr(row, "measurements_count", 0) or 0,
        "measurements_state": getattr(row, "measurements_state", None),
        "images_total": sum(images.values()), "images": images, "unread": ing.get("unread") or [],
        "partial": bool(ing.get("partial")) or bool(row.pages_incomplete),
        "ingestion_version": ing.get("ingestion_version"),
    }


def _list_documents(conn: Connection, ctx: TenantContext, q: str | None, status_filter: str | None,
                    document_id: UUID | None = None) -> list[dict]:
    """One query: documents with their latest version (LATERAL), filtered in SQL."""
    names = [c.split(".")[1] for c in _PLAIN_COLS.split(", ")] + list(_READING_COLS)
    latest_cols = ", ".join(f"lv.{n} AS lv_{n}" for n in names)
    rows = conn.execute(
        text(
            "SELECT d.id, d.title, d.created_at, d.deleted_at, g.id AS group_id, g.name AS group_name,"
            " (SELECT count(*) FROM document_versions vc WHERE vc.document_id = d.id) AS versions_count,"
            f" {latest_cols}"
            " FROM documents d JOIN document_groups g ON g.id = d.group_id"
            f" LEFT JOIN LATERAL (SELECT {_VERSION_COLS} FROM document_versions v WHERE v.document_id = d.id"
            "   ORDER BY v.version_no DESC LIMIT 1) lv ON true"
            " WHERE (:admin OR d.deleted_at IS NULL)"
            " AND (CAST(:q AS text) IS NULL OR d.title ILIKE '%' || CAST(:q AS text) || '%')"
            " AND (CAST(:doc AS uuid) IS NULL OR d.id = CAST(:doc AS uuid))"
            " AND (CAST(:st AS text) IS NULL OR lv.status = CAST(:st AS text))"
            " ORDER BY d.created_at DESC"
        ),
        {"admin": ctx.is_admin, "q": q or None, "doc": document_id, "st": status_filter or None},
    ).all()
    out = []
    for r in rows:
        m = r._mapping
        ver = SimpleNamespace(**{k[3:]: v for k, v in m.items() if k.startswith("lv_")}) if m["lv_id"] else None
        out.append({
            "id": str(r.id), "title": r.title, "group": {"id": str(r.group_id), "name": r.group_name},
            "deleted": r.deleted_at is not None, "created_at": r.created_at.isoformat(),
            "current_version": _version_json(ver) if ver else None, "versions_count": r.versions_count,
        })
    return out


@router.get("")
def list_documents(q: str | None = None, status_filter: str | None = Query(None, alias="status"),
                   ctx: TenantContext = Depends(get_ctx)):
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


# --- source view: a version's blocks and its pictures ----------------------------------------------------------

BLOCKS_MAX = 400
_MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "bmp": "image/bmp"}


def _version_row(conn: Connection, doc_uuid: UUID, ver_uuid: UUID):
    row = conn.execute(
        text("SELECT v.id, v.storage_key, v.mime_type, v.filename, v.is_current, d.title FROM document_versions v"
             " JOIN documents d ON d.id = v.document_id WHERE v.id = :v AND d.id = :d AND d.deleted_at IS NULL"),
        {"v": ver_uuid, "d": doc_uuid},
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return row


@router.get("/{document_id}/versions/{version_id}/blocks")
def get_blocks(document_id: str, version_id: str, start: int | None = Query(None, ge=0),
               end: int | None = Query(None, ge=0), ctx: TenantContext = Depends(get_ctx)) -> dict:
    """The version's blocks in reading order (a window when ``start``/``end`` are given), with each table's
    structure: what the source viewer shows around a cited location."""
    doc_uuid, ver_uuid = parse_uuid(document_id), parse_uuid(version_id)
    with tenant_tx(ctx) as conn:
        v = _version_row(conn, doc_uuid, ver_uuid)
        params: dict = {"v": ver_uuid, "a": start if start is not None else 0,
                        "b": end if end is not None else 1_000_000}
        rows = conn.execute(text(
            "SELECT block_index, kind, section, section_path, label, paragraph_no, page, media, source, status, note,"
            " table_index, text FROM document_blocks WHERE version_id = :v AND block_index BETWEEN :a AND :b"
            f" ORDER BY block_index LIMIT {BLOCKS_MAX}"), params).all()
        tables = {r.table_index: r.structure for r in conn.execute(text(
            "SELECT table_index, structure FROM extracted_tables WHERE version_id = :v"), {"v": ver_uuid})}
        total = conn.execute(text("SELECT count(*) FROM document_blocks WHERE version_id = :v"),
                             {"v": ver_uuid}).scalar_one()
        audit(conn, "source_view", ctx.user_id, "document_version", ver_uuid)
    blocks = []
    for r in rows:
        b = {"index": r.block_index, "kind": r.kind, "section": r.section, "section_path": list(r.section_path or []),
             "paragraph_no": r.paragraph_no, "page": r.page, "media": r.media, "source": r.source, "status": r.status,
             "note": r.note, "text": r.text}
        if r.table_index is not None and r.table_index in tables:
            st = tables[r.table_index] or {}
            b["table"] = {k: st.get(k) for k in ("headers", "caption", "title", "notes", "source", "media")} | {
                "rows": [x.get("cells") for x in st.get("rows") or []]}
        if r.media and r.media.rsplit(".", 1)[-1].lower() in _MEDIA_TYPES:
            b["media_url"] = f"/api/documents/{document_id}/versions/{version_id}/media/{quote(r.media)}"
        blocks.append(b)
    return {"document_id": document_id, "version_id": version_id, "title": v.title, "is_current": v.is_current,
            "mime_type": v.mime_type, "total": total, "blocks": blocks,
            "file_url": f"/api/documents/{document_id}/versions/{version_id}/file"}


@router.get("/{document_id}/versions/{version_id}/media/{name}")
def get_media(document_id: str, version_id: str, name: str, ctx: TenantContext = Depends(get_ctx)) -> Response:
    """One raster picture from inside a DOCX, by the media name a block of this version names (nothing else in
    the package is reachable)."""
    doc_uuid, ver_uuid = parse_uuid(document_id), parse_uuid(version_id)
    ext = name.rsplit(".", 1)[-1].lower()
    if ext not in _MEDIA_TYPES or "/" in name or "\\" in name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    with tenant_tx(ctx) as conn:
        v = _version_row(conn, doc_uuid, ver_uuid)
        known = conn.execute(text("SELECT 1 FROM document_blocks WHERE version_id = :v AND media = :m LIMIT 1"),
                             {"v": ver_uuid, "m": name}).first()
    if known is None or v.mime_type != DOCX_MIME:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    try:
        with zipfile.ZipFile(io.BytesIO(get_storage().get(v.storage_key))) as zf:
            data = zf.read(f"word/media/{name}")
    except (KeyError, zipfile.BadZipFile):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    return Response(content=data, media_type=_MEDIA_TYPES[ext],
                    headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"})
