"""Count the stored tables by class (round 21 U8, KTD6: "the first step is a read-only count").

Every table of every current document version is classified from its stored structure alone (``app.extraction.tables.classify_stored``,
the code the chat tools serve tables with): ``reversed_stored`` (a left-to-right table stored from its right edge to its left one, served
in the order it is drawn), ``drawn_stored`` (stored in the order it is drawn), ``no_boxes`` (no Hebrew and no cell box: the order cannot
be told, served as stored), ``hebrew`` (a Hebrew letter in a header or cell: served as stored) and ``other`` (no Hebrew, with boxes, but
no cue or boxes that do not settle the order: served as stored). A small ``reversed_stored`` or ``drawn_stored`` population is a limit on
how far the serve-time order generalises, and is stated as one.

Read-only: each office is read in one transaction set read-only, as rag_app in the system context (office ids from the owner-only
``maintenance_office_ids()``); nothing is written and no model is called. Only counts are printed: never an id, a title or any text of a
document.

    python scripts/count_stored_tables.py                  # every office
    python scripts/count_stored_tables.py --office <uuid>  # one office
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import TenantContext, system_ctx, tenant_tx  # noqa: E402
from app.extraction.tables import CLASSES, count_stored  # noqa: E402


def count_office(ctx: TenantContext) -> dict[str, int]:
    """The number of tables of the office's current document versions in each class."""
    with tenant_tx(ctx) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        structures = (r.structure or {} for r in conn.execute(text(
            "SELECT t.structure FROM extracted_tables t"
            " JOIN document_versions v ON v.id = t.version_id AND v.is_current"
            " JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL")))
        return count_stored(structures)


def report(counts: dict[str, int]) -> str:
    return "  ".join(f"{name}={counts[name]}" for name in CLASSES) + f"  tables={sum(counts.values())}"


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
    total = dict.fromkeys(CLASSES, 0)
    for office_id in office_ids:
        counts = count_office(system_ctx(office_id))
        print(f"office {office_id}  {report(counts)}")
        for name in CLASSES:
            total[name] += counts[name]
    print(f"total  {report(total)}")


if __name__ == "__main__":
    main()
