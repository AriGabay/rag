"""Recompute chunks.normalized_text (and with it the generated tsvector) after the search
normalization rules change. No re-OCR and no re-embedding. Office ids come from the owner-only
``maintenance_office_ids()``; the rewrite runs per office as rag_app in the system context.

    docker compose exec backend python scripts/reindex_text.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import tenant_tx  # noqa: E402
from app.extraction.normalize_text import normalize_for_search  # noqa: E402
from app.platform.pipeline import system_ctx  # noqa: E402


def main() -> None:
    owner = create_engine(get_settings().owner_database_url)
    with owner.connect() as conn:
        office_ids = list(conn.execute(text("SELECT maintenance_office_ids()")).scalars())
    owner.dispose()
    total = 0
    for office_id in office_ids:
        with tenant_tx(system_ctx(office_id)) as conn:
            rows = conn.execute(text("SELECT id, text FROM chunks")).all()
            for r in rows:
                conn.execute(text("UPDATE chunks SET normalized_text = :n WHERE id = :i"),
                             {"n": normalize_for_search(r.text), "i": r.id})
            conn.execute(text("UPDATE office_data_versions SET version = version + 1"))
            total += len(rows)
    print(f"re-normalized {total} chunks in {len(office_ids)} offices")


if __name__ == "__main__":
    main()
