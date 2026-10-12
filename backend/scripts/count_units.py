"""Count the appraisal units of each document's current reading (round 9 U7, KTD4/KTD5: "measured before enforced").

For every document of an office (or of every office), the current version's units are read from ``document_units``
when they are stored for its reading, and derived in memory with the same code the chat tools use (``app.chat.
units``) when they are not. One line is printed per document: its id, its page count, the number of units, and for
each unit its page range, kind and certainty — never a title, an alias, an identifier or any other text of the
document. A summary counts the single- and multi-unit documents and the appraisal units. Units are turned on for
chat only once every single-appraisal regression file derives exactly one appraisal unit at most.

Read-only: each office is read in one transaction set read-only before anything is read, as rag_app in the system
context (office ids from the owner-only ``maintenance_office_ids()``); nothing is written, no model is called.

    python scripts/count_units.py                  # every office
    python scripts/count_units.py --office <uuid>  # one office
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.chat import units  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import TenantContext, system_ctx, tenant_tx  # noqa: E402


def count_office(ctx: TenantContext) -> list[dict]:
    """Per current document version of the office: ``{"document_id", "pages", "units": [{"number", "pages",
    "pages_label", "kind", "certainty"}]}`` — ids, numbers, page ranges, kinds and certainty only."""
    out = []
    with tenant_tx(ctx) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        versions = conn.execute(text(
            "SELECT d.id AS document_id, v.id AS version_id, v.page_count"
            " FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
            " WHERE d.deleted_at IS NULL ORDER BY d.created_at, d.id")).all()
        for v in versions:
            found = [{"number": u.unit_no, "pages": (u.page_first, u.page_last), "pages_label": u.pages_label(),
                      "kind": u.kind, "certainty": u.certainty} for u in units.read_or_derive(conn, v.version_id)]
            out.append({"document_id": str(v.document_id), "pages": v.page_count, "units": found})
    return out


def report(rows: list[dict]) -> str:
    """The lines to print: one per document, then the summary. Ids, counts, page numbers, kinds and certainty only."""
    lines = []
    for r in rows:
        parts = [f"[{u['number']}: {u['pages_label']}, {u['kind']}, {u['certainty']}]" for u in r["units"]]
        lines.append(f"{r['document_id']}  pages={r['pages'] if r['pages'] is not None else '?'}"
                     f"  units={len(r['units'])}  " + " ".join(parts))
    single = sum(1 for r in rows if len(r["units"]) <= 1)
    appraisals = sum(1 for r in rows for u in r["units"] if u["kind"] == units.APPRAISAL)
    lines.append(f"documents={len(rows)}  single_unit={single}  multi_unit={len(rows) - single}"
                 f"  appraisal_units={appraisals}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--office", type=UUID, help="one office id (default: every office)")
    args = parser.parse_args()
    if args.office is not None:
        office_ids = [args.office]
    else:
        owner = create_engine(get_settings().owner_database_url)
        with owner.connect() as conn:
            office_ids = list(conn.execute(text("SELECT maintenance_office_ids()")).scalars())
        owner.dispose()
    for office_id in office_ids:
        print(f"office {office_id}")
        print(report(count_office(system_ctx(office_id))))


if __name__ == "__main__":
    main()
