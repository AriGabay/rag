"""Warm one office's time records: the identification facts (`version_identities`, round 12 U5) and the valuation dates
(`version_dates`) of its current appraisal versions, from their stored text (round 12 KTD15; R21).

The records are derived lazily on first read; this script derives them ahead of the first chat turn, so the first question
after a migration does not pay for it. It is idempotent and it only ever INSERTS: a version that already has rows of the
current `IDENTITY_VERSION` / `DATES_VERSION` is left alone, a document is never re-read, and nothing is deleted or
updated. Run it only after `scripts/backup.sh` and migration 0021 on the office database:

    docker compose exec backend python scripts/derive_time_records.py --office <office uuid> [--dry-run]

``--dry-run`` writes nothing and reports how many current versions still lack identity rows. Each batch of versions is its
own transaction, so an interrupted run keeps what it wrote and a second run continues.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Connection, text  # noqa: E402

from app.chat import dates, identity  # noqa: E402
from app.db import TenantContext, system_ctx, tenant_tx  # noqa: E402
from app.platform.source_kinds import APPRAISALS_SQL  # noqa: E402

BATCH = 25

_CURRENT_SQL = ("SELECT v.id AS version_id, v.document_id FROM document_versions v JOIN documents d"
                " ON d.id = v.document_id AND d.deleted_at IS NULL"
                f" WHERE v.is_current AND v.status IN ('ready', 'needs_review') AND {APPRAISALS_SQL} ORDER BY v.id")
_WITH_IDENTITIES_SQL = ("SELECT DISTINCT version_id FROM version_identities WHERE version_id = ANY(:v)"
                        " AND identity_version = :iv")


def current_versions(conn: Connection) -> list[tuple[UUID, UUID]]:
    """(version, document) of every current appraisal version of the office, in a stable order."""
    return [(r.version_id, r.document_id) for r in conn.execute(text(_CURRENT_SQL))]


def _missing_identities(conn: Connection, versions: list[tuple[UUID, UUID]]) -> list[tuple[UUID, UUID]]:
    """The (version, document) pairs that have no identity row of the current `IDENTITY_VERSION`."""
    have = {r.version_id for r in conn.execute(text(_WITH_IDENTITIES_SQL), {"v": [v for v, _ in versions],
                                                                             "iv": identity.IDENTITY_VERSION})}
    return [(v, d) for v, d in versions if v not in have]


def warm_identities(conn: Connection, versions: list[tuple[UUID, UUID]], cache: dict) -> int:
    """Derive the identity rows of the versions that have none of the current `IDENTITY_VERSION` (the lazy read does the
    writing, in a savepoint). Returns how many versions it derived."""
    todo = _missing_identities(conn, versions)
    for v, d in todo:
        identity.ensure(conn, v, d, cache=cache)
    return len(todo)


def warm_dates(conn: Connection, version_ids: list[UUID], cache: dict) -> int:
    """Warm the valuation dates of the versions through the existing read, `chat.dates.valuation_dates`, which derives and
    stores rows of whichever `DATES_VERSION` is current (a new dates version needs no change here) and returns the
    versions that are readable. Returns how many versions it read."""
    return len(dates.valuation_dates(conn, version_ids, cache=cache))


def warm_office(ctx: TenantContext, *, dry_run: bool = False, batch: int = BATCH) -> dict:
    """Warm every current appraisal version of one office, a batch per transaction."""
    counts = {"versions": 0, "identities_derived": 0, "dates_read": 0}
    with tenant_tx(ctx) as conn:
        versions = current_versions(conn)
    counts["versions"] = len(versions)
    for start in range(0, len(versions), batch):
        chunk = versions[start:start + batch]
        with tenant_tx(ctx) as conn:
            if dry_run:
                counts["identities_derived"] += len(_missing_identities(conn, chunk))
                continue
            cache: dict = {}
            counts["identities_derived"] += warm_identities(conn, chunk, cache)
            counts["dates_read"] += warm_dates(conn, [v for v, _ in chunk], cache)
    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--office", required=True, type=UUID, help="the office to warm")
    parser.add_argument("--dry-run", action="store_true", help="write nothing; count the versions that lack identities")
    args = parser.parse_args(argv)
    result = warm_office(system_ctx(args.office), dry_run=args.dry_run)
    label = "would derive" if args.dry_run else "derived"
    print(f"office {args.office}: {result['versions']} current appraisal versions; {label} identities for "
          f"{result['identities_derived']}; dates read for {result['dates_read']}")


if __name__ == "__main__":
    main()
