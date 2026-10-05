"""Coverage limits shown with every answer (R26): what the repository has not (yet) verified."""

from __future__ import annotations

from sqlalchemy import Connection

from app.answering.conditions import QueryConditions
from app.appraisal.query import AWAITING, VERIFIED, eligible_ids
from app.platform.documents import latest_status_counts


def coverage(conn: Connection, c: QueryConditions | None = None) -> dict:
    counts = latest_status_counts(conn)
    pending = counts.get("pending", 0) + counts.get("processing", 0)
    failed = counts.get("failed", 0)
    needs_review = counts.get("needs_review", 0)
    awaiting = 0
    if c is not None and c.data_kind:
        verified = set(eligible_ids(conn, c, VERIFIED))
        awaiting = len(set(eligible_ids(conn, c, AWAITING)) - verified)

    parts = []
    if pending:
        parts.append(f"{pending} מסמכים עדיין בעיבוד")
    if needs_review:
        parts.append(f"{needs_review} מסמכים דורשים בדיקה")
    if failed:
        parts.append(f"{failed} מסמכים שעיבודם נכשל")
    if awaiting:
        parts.append(f"{awaiting} רשומות תואמות ממתינות לאימות ולא נכללו בחישוב")
    if parts:
        note = "מגבלת כיסוי: " + "; ".join(parts) + ". ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד."
    else:
        note = "כל המסמכים במאגר עובדו, והחישוב כולל את כל הרשומות המאומתות התואמות."
    return {"text": note, "docs_pending": pending, "docs_failed": failed, "docs_needs_review": needs_review,
            "records_awaiting_verification": awaiting}
