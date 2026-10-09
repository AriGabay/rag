"""Count the appraisal contexts of each document's current reading (round 7 U6, KTD7: "measured before enforced").

For every document of an office (or of every office), the current version's reading is derived into appraisal
contexts with the same code the chat tools use (``app.chat.contexts.of_version``), and one line is printed per
document: its id, its page count, the number of contexts, and for each context its page range and the KINDS of
identifiers that define it (``address``, ``block_parcel``) — never an identifier's value, a title or any other text
of the document. A summary counts the single- and multi-context documents. The enforcement setting
(``chat_appraisal_context_enforced``) is turned on only once every single-appraisal regression report derives
exactly one context.

Read-only: each office is read in one transaction set read-only before anything is read, as rag_app in the system
context (office ids from the owner-only ``maintenance_office_ids()``); nothing is written, no model is called.

    python scripts/count_appraisal_contexts.py                  # every office
    python scripts/count_appraisal_contexts.py --office <uuid>  # one office
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.chat import contexts  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import TenantContext, system_ctx, tenant_tx  # noqa: E402


def count_office(ctx: TenantContext) -> list[dict]:
    """Per current document version of the office: ``{"document_id", "pages", "contexts": [{"number", "pages",
    "kinds"}]}`` — ids, numbers and identifier kinds only."""
    out = []
    with tenant_tx(ctx) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        versions = conn.execute(text(
            "SELECT d.id AS document_id, v.id AS version_id, v.page_count, v.ingestion->>'reading_id' AS reading_id"
            " FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
            " WHERE d.deleted_at IS NULL ORDER BY d.created_at, d.id")).all()
        for v in versions:
            cx = contexts.of_version(conn, v.version_id, v.reading_id)
            found = []
            for n in cx.numbers:
                first, last = cx.pages(n)
                found.append({"number": n, "pages": (first, last), "kinds": sorted(cx.identity(n).kinds)})
            out.append({"document_id": str(v.document_id), "pages": v.page_count, "contexts": found})
    return out


def _pages(pages: tuple) -> str:
    first, last = pages
    if first is None:
        return "pages ?"
    return f"page {first}" if first == last else f"pages {first}-{last}"


def report(rows: list[dict]) -> str:
    """The lines to print: one per document, then the summary. Ids, counts, page numbers and kinds only."""
    lines = []
    for r in rows:
        parts = [f"[{c['number']}: {_pages(c['pages'])}, kinds={'+'.join(c['kinds']) or 'none'}]"
                 for c in r["contexts"]]
        lines.append(f"{r['document_id']}  pages={r['pages'] if r['pages'] is not None else '?'}"
                     f"  contexts={len(r['contexts'])}  " + " ".join(parts))
    single = sum(1 for r in rows if len(r["contexts"]) <= 1)
    lines.append(f"documents={len(rows)}  single_context={single}  multi_context={len(rows) - single}")
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
