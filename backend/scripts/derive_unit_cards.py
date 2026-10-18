"""Read the identity cards of one office's multi-appraisal files (round 15 U5: KTD5, KTD6; R5, R7; KD8).

New uploads queue their cards at publication; this backfill gives the files that are already stored theirs. Per current
appraisal version it counts the units of the current derivation, skips the versions with one unit (a single-unit file
keeps its structural identity) and, when the office's provider mode is not cloud, every version (the consent that lets
document text reach a model), then reads one card per unit that needs it: one model call on the unit's opening blocks.
It is idempotent and it never deletes: cards are inserted, a card that is `done` or `empty` is never called again, and
the only row it updates is a `pending` or `failed` card of its own, by the result of its call. Units beyond the per-version
ceiling (`UNIT_CARDS_MAX_PER_VERSION`, default 60) stay `pending`; a second run reads the next ones. Run it only after
`scripts/backup.sh` and migration 0023 on the office database:

    docker compose exec backend python scripts/derive_unit_cards.py --office <office uuid> [--dry-run]

`--dry-run` writes no card and calls no model (reading a version's units may derive its current-version unit rows, beside
the older ones, as any first read does): it prints the units, the versions skipped (single unit, not cloud), the
cards it would call, the units over the ceiling and a token estimate. Each unit's card is its own transaction, so an
interrupted run keeps what it wrote and a second run continues.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Connection, text  # noqa: E402

from app.chat import cards  # noqa: E402
from app.chat import units as unit_store  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import TenantContext, system_ctx, tenant_tx  # noqa: E402
from app.platform.source_kinds import APPRAISALS_SQL  # noqa: E402

_CURRENT_SQL = ("SELECT v.id AS version_id FROM document_versions v JOIN documents d"
                " ON d.id = v.document_id AND d.deleted_at IS NULL"
                f" WHERE v.is_current AND v.status IN ('ready', 'needs_review') AND {APPRAISALS_SQL} ORDER BY v.id")

COUNTS = ("versions", "multi_unit_versions", "single_unit_skipped", "not_cloud_skipped", "units", "to_call",
          "over_ceiling", "not_subject_units", "read_already")


def current_versions(conn: Connection) -> list[UUID]:
    """Every current appraisal version of the office, in a stable order."""
    return list(conn.execute(text(_CURRENT_SQL)).scalars())


def plan_version(conn: Connection, version_id: UUID, ceiling: int) -> tuple[cards.Plan, int] | None:
    """The cards a run would call for one version and the tokens (input and output) they would use; None for a version
    with one unit (or that has none: not an appraisal)."""
    units = unit_store.ensure(conn, version_id)
    if len(units) <= 1:
        return None
    reading = unit_store.reading_of(conn, version_id)
    plan = cards.plan_units(units, cards.cards_of(conn, version_id, reading.reading_id if reading else None), ceiling)
    inputs = [cards.build_input(cards.opening_blocks(cards.opening_rows(conn, version_id, u), u)) for u in plan.to_call]
    return plan, sum(cards.estimate_tokens(inputs))


def backfill_office(ctx: TenantContext, *, dry_run: bool = False, provider=None, ceiling: int | None = None) -> dict:
    """Plan (``dry_run``) or read the cards of every current appraisal version of one office. ``provider``: the model the
    calls go to (default: the selected provider; tests pass a scripted one)."""
    from app.platform import pipeline

    ceiling = get_settings().unit_cards_max_per_version if ceiling is None else ceiling
    out = dict.fromkeys(COUNTS, 0) | {"tokens_estimate": 0, "called": 0, "done": 0, "failed": 0, "empty": 0}
    cloud = pipeline.vision_reader(ctx) is not None
    with tenant_tx(ctx) as conn:
        versions = current_versions(conn)
    out["versions"] = len(versions)
    for v in versions:
        with tenant_tx(ctx) as conn:
            planned = plan_version(conn, v, ceiling)
        if planned is None:
            out["single_unit_skipped"] += 1
            continue
        plan, tokens = planned
        out["multi_unit_versions"] += 1
        out["units"] += plan.units
        out["not_subject_units"] += len(plan.not_subject)
        out["read_already"] += plan.read
        if not cloud:  # nothing would be called: the consent that lets document text reach a model is missing
            out["not_cloud_skipped"] += 1
            continue
        out["to_call"] += len(plan.to_call)
        out["over_ceiling"] += len(plan.over_ceiling)
        out["tokens_estimate"] += tokens
        if dry_run:
            continue
        provider = provider or _selected_provider()
        try:
            run = cards.generate(ctx, v, provider, ceiling=ceiling)
        except cards.CardRunFailed:   # recorded as failed cards; the next run calls them again
            out["failed"] += len(plan.to_call)
            continue
        out["called"] += run.called
        out["done"] += run.done
        out["failed"] += run.failed
        out["empty"] += run.empty
    return out


def _selected_provider():
    from app.providers.llm import get_selected_provider

    return get_selected_provider()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--office", required=True, type=UUID, help="the office to read cards for")
    parser.add_argument("--dry-run", action="store_true",
                        help="write nothing and call no model; print the units, the skips and a token estimate")
    args = parser.parse_args(argv)
    r = backfill_office(system_ctx(args.office), dry_run=args.dry_run)
    skipped = (f"skipped: {r['single_unit_skipped']} single-unit versions, {r['not_cloud_skipped']} multi-unit "
               f"versions not in cloud mode")
    units = (f"{r['units']} units in {r['multi_unit_versions']} multi-unit versions "
             f"({r['not_subject_units']} not properties, {r['read_already']} already read)")
    if args.dry_run:
        print(f"office {args.office}: {r['versions']} current appraisal versions; {units}; {skipped}; would call "
              f"{r['to_call']} cards ({r['over_ceiling']} units over the ceiling stay pending); estimated "
              f"~{r['tokens_estimate']} tokens (input and output); nothing written")
    else:
        print(f"office {args.office}: {r['versions']} current appraisal versions; {units}; {skipped}; "
              f"called {r['called']} cards: {r['done']} done, {r['empty']} empty, {r['failed']} failed; "
              f"{r['over_ceiling']} units over the ceiling stay pending")


if __name__ == "__main__":
    main()
