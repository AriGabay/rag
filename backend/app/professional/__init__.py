"""Professional sources: the derived structure and metadata of standards and professional documents (round 10 U4, U5:
KTD9, KTD10).

``sections`` derives a document's sections, limits, references and text digest from its stored blocks; ``profile``
(U5) extracts the metadata a standard states about itself, each value with the quote it rests on. Both are derived
lazily from the stored reading, never from the file, and both only for the ``standard`` and ``professional`` kinds.

``after_publish`` is what ingestion calls once a version is published (and once a reprocessed reading is swapped in):
it derives what a reader of a standard will want first. It is a convenience, not a stage: the readers derive the same
things on first use, so a version whose derivation failed here is not broken, only not yet derived.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from sqlalchemy import text

from app.db import TenantContext, tenant_tx

logger = logging.getLogger(__name__)

ERROR_KEY = "professional_error"


def _record(ctx: TenantContext, version_id: UUID | str, stage: str, exc: Exception) -> None:
    """Note on the version's ingestion report which derivation failed and with what; the message is left out (it may
    quote document text)."""
    entry = {"stage": stage, "error": type(exc).__name__}
    try:
        with tenant_tx(ctx) as conn:
            conn.execute(text("UPDATE document_versions SET ingestion = COALESCE(ingestion, '{}'::jsonb) ||"
                              " jsonb_build_object(CAST(:k AS text), CAST(:e AS jsonb)) WHERE id = :v"),
                         {"k": ERROR_KEY, "e": json.dumps(entry), "v": UUID(str(version_id))})
    except Exception:  # noqa: BLE001 - recording must not raise either
        logger.warning("the %s failure of version %s could not be recorded", stage, version_id, exc_info=True)


def _clear(ctx: TenantContext, version_id: UUID | str) -> None:
    try:
        with tenant_tx(ctx) as conn:
            conn.execute(text("UPDATE document_versions SET ingestion = ingestion - CAST(:k AS text)"
                              " WHERE id = :v AND ingestion ? CAST(:k AS text)"),
                         {"k": ERROR_KEY, "v": UUID(str(version_id))})
    except Exception:  # noqa: BLE001
        logger.warning("the derivation record of version %s could not be cleared", version_id, exc_info=True)


def after_publish(ctx: TenantContext, version_id: UUID | str) -> bool:
    """Derive the sections and then the profile of a just-published version of a standard or a professional document;
    a no-op for an appraisal. Never raises: each derivation that fails is logged and recorded in the version's
    ingestion report (``professional_error``, the stage and the error class) and publishing stands; the other one is
    still tried. Returns whether both were derived (and clears an earlier record)."""
    from app.professional import profile, sections

    ok = True
    for stage, derive in (("sections", sections.ensure), ("profile", profile.ensure_profile)):
        try:
            derive(ctx, version_id)
        except Exception as exc:  # noqa: BLE001 - derived data must never fail a publish
            logger.exception("%s of version %s could not be derived", stage, version_id)
            _record(ctx, version_id, stage, exc)
            ok = False
    if ok:
        _clear(ctx, version_id)
    return ok
