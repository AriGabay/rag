"""Bring stored chunks up to the current search rules without re-OCR (KTD10):

- table-row chunks are re-rendered from ``extracted_tables.structure`` (``header (unit): value``) and get
  their ``table_index``/``row_index``; a row whose text changed is re-embedded;
- every chunk's ``normalized_text`` (and with it the generated tsvector) is recomputed.

Idempotent: a second run changes nothing. A version whose stored row chunks do not line up with its
tables is left as is and counted as skipped. Office ids come from the owner-only
``maintenance_office_ids()``; the rewrite runs per office as rag_app in the system context.

    docker compose exec backend python scripts/reindex_text.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import TenantContext, bump_data_version, system_ctx, tenant_tx  # noqa: E402
from app.extraction.chunking import render_row, table_rows  # noqa: E402
from app.extraction.normalize_text import normalize_for_search  # noqa: E402
from app.providers.embeddings import get_embedding_provider, to_pgvector  # noqa: E402


def _expected_rows(conn) -> dict:
    """version_id -> [(table_index, row_index, text)] in chunk order, as chunk_document emits them."""
    expected: dict = defaultdict(list)
    for t in conn.execute(text("SELECT version_id, table_index, structure FROM extracted_tables"
                               " ORDER BY version_id, table_index")).all():
        s = t.structure
        for row_index, row in table_rows(s.get("rows") or [], lambda r: r.get("cells") or []):
            expected[t.version_id].append(
                (t.table_index, row_index, render_row(s.get("headers") or [], row["cells"], s.get("units"))))
    return expected


def reindex_office(ctx: TenantContext) -> dict:
    counts = {"rows_rewritten": 0, "normalized": 0, "reembedded": 0, "versions_skipped": 0}
    provider = get_embedding_provider()
    with tenant_tx(ctx) as conn:
        expected = _expected_rows(conn)
        chunks: dict = defaultdict(list)
        for c in conn.execute(text("SELECT id, version_id, text, table_index, row_index FROM chunks"
                                   " WHERE kind = 'table_row' ORDER BY version_id, chunk_index")).all():
            chunks[c.version_id].append(c)
        reembed: list = []
        for version_id in set(chunks) | set(expected):
            have, want = chunks.get(version_id, []), expected.get(version_id, [])
            if len(have) != len(want):
                counts["versions_skipped"] += 1
                continue
            for c, (ti, ri, row_text) in zip(have, want, strict=True):
                if (c.text, c.table_index, c.row_index) == (row_text, ti, ri):
                    continue
                conn.execute(text("UPDATE chunks SET text = :t, table_index = :ti, row_index = :ri WHERE id = :i"),
                             {"t": row_text, "ti": ti, "ri": ri, "i": c.id})
                counts["rows_rewritten"] += 1
                if c.text != row_text:
                    reembed.append((c.id, row_text))
        for r in conn.execute(text("SELECT id, text, normalized_text FROM chunks")).all():
            normalized = normalize_for_search(r.text)
            if normalized != r.normalized_text:
                conn.execute(text("UPDATE chunks SET normalized_text = :n WHERE id = :i"), {"n": normalized, "i": r.id})
                counts["normalized"] += 1
        for start in range(0, len(reembed), 64):
            batch = reembed[start:start + 64]
            for (chunk_id, _), vec in zip(batch, provider.embed_passages([t for _, t in batch]), strict=True):
                conn.execute(text("UPDATE chunks SET embedding = CAST(:e AS vector), embedding_model = :m WHERE id = :i"),
                             {"e": to_pgvector(vec), "m": provider.model_id, "i": chunk_id})
        counts["reembedded"] = len(reembed)
        if counts["rows_rewritten"] or counts["normalized"]:
            bump_data_version(conn)
    return counts


def main() -> None:
    owner = create_engine(get_settings().owner_database_url)
    with owner.connect() as conn:
        office_ids = list(conn.execute(text("SELECT maintenance_office_ids()")).scalars())
    owner.dispose()
    totals: dict = defaultdict(int)
    for office_id in office_ids:
        for key, value in reindex_office(system_ctx(office_id)).items():
            totals[key] += value
    print(f"{len(office_ids)} offices: " + ", ".join(f"{k}={v}" for k, v in totals.items()))


if __name__ == "__main__":
    main()
