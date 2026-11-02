---
title: Ranking answers by a datum's date - the server, not the model, must own scope, dates and grouping
date: 2026-10-14
category: logic-errors
module: backend chat recency ranking and timeline (app/chat/dates.py, property.py, tools.py, timeline.py, verify.py, timeframe.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "A follow-up asking for an earlier year (\"ומה היה השווי ב-2022?\") says the year is not stated, although an appraisal of that year is in the office"
  - "A recent-first answer lists dated values in prose but carries no timeline"
  - "A correct value with its source's VAT basis (\"2,450,000 ₪ כולל מע״מ\") is removed by verification"
  - "A correct valuation date written in a claim is cut as a number no cited source states"
  - "The newest value is missing from a timeline that lists the older ones"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [recency, timeline, datum-date, property-scope, provenance, verification, real-model, rag, chat]
---

# Ranking answers by a datum's date - the server, not the model, must own scope, dates and grouping

## Problem
Round 11 of PR AriGabay/rag#2 added a per-conversation "prefer recent data" switch. Answers rank data by the date each datum refers to, and the server builds a timeline of comparable dated values. Every scripted test passed. The real model (`gpt-6-luna`) failed on synthetic fixtures in five ways the code alone did not reveal. The fixtures hold one property appraised at three valuation dates, each in its own file (`backend/tests/fixtures/round11/`).

## Symptoms
- **Scope.** After a turn about the property, "ומה היה השווי ב-2022?" read only the 2025 appraisal.
- **No timeline.** Values registered from header lines ("שווי הנכס: X ₪") stayed undated.
- **Removed values.** Correct VAT bases and correct dates were removed from claims.
- **Split timeline.** The newest value fell out of the timeline.

## What Didn't Work
- **Scripted tests.** The scripted agent registers exactly the values and scopes the test intends. The real model names the focus unit (`unit=U#`), names one document from the previous answer (`document_ids=[...]`), cites passages (S#) instead of registered values, and writes the date into free-text fields.
- **Gating a datum's date on the appraiser's "adopted" stance.** Header-line values carry no stated stance, so almost nothing was dated.
- **Trusting the cited passage for qualifiers.** The VAT basis and the valuation date often sit on a line the quote or passage leaves out, while the server already resolved them into the record.

## Solution
Each guarantee now comes from a server-resolved record with provenance.

1. **Scope is the property, not the focus unit.** In a recent or explicit time turn, three kinds of search widen to every appraisal sharing the same property key: one confined to the focus unit, one naming the focus unit itself, and one naming documents that all share one key. The key is block and parcel, else street and number (`property.property_key`, `same_property_units`; `tools._property_search`, `_named_property`, `names_focus_unit`). The search never crosses keys, never widens in "none" mode, and never touches intent or the property slot.
2. **Dates come from the dates record.** `version_dates` is derived lazily from stored blocks. The unit's valuation date dates every value of that unit, except:
   - a party's claim, proposal or estimate;
   - a value another named party stated;
   - a comparable role;
   - a row of a comparables table.

   A date in the row or sentence itself wins (`dates.resolve_as_of`). The report date never dates a datum.
3. **Verification reads the record.** A cited V#'s source-provenanced VAT backs the same VAT in the claim (`verify._vat_problems`). A cited V#/M#'s `as_of` backs the same date at its precision (`verify._date_backed`). A different VAT or date is still removed.
4. **Model free text never gates grouping.** Only source-stated fields enter the comparability key (`timeline._fields`).
   - A model-asserted `scenario` counts as no scenario. The model wrote "המועד הקובע 20/01/2025" into it and dropped every value.
   - Adopted, unknown and missing stance are one class (`timeline.stance_class`).
5. **Ask the model to register what the server must order.** The time block in the turn's context (`timeframe.BLOCK_RECENT`) asks for every dated value to be registered and cited (V#). Judge rule (28) (`JUDGE_VERSION` 7) says an undated datum does not undo "the latest" among the dated.

## Why This Works
Order, scope and the timeline are server guarantees. Each one is computed from something the server resolved with provenance: the dates record, the value record and the property key. The model's citations, free-text fields and wording are treated as hints. When the model narrows a search or mislabels a field, the guarantee still holds, and verification checks claims against the same records the timeline is built from.

## Prevention
- Run each sequence three times on the real model with synthetic multi-date fixtures, and log the actual search calls (unit, `document_ids`). The scope failures showed only in those logs, never in the answers' wording (`backend/tests/integration/test_round11_real_model.py`; the scope cases in `test_property_scope.py`).
- When a check removes a value or a qualifier, first ask whether the server already holds the fact with source provenance. The VAT and date removals were both blind checks reading less than the record held.
- Test-database note: migration tests that downgrade and upgrade leave dropped columns on `pages`, and `rag_test` eventually hits Postgres's 1600-column limit. The symptom is `TooManyColumns` in an old migration test followed by hundreds of errors. Rebuild the test schema with `alembic downgrade base` + `upgrade head` on `rag_test` only, and never on the office database.

## Related Issues
- `docs/solutions/logic-errors/hebrew-follow-up-entity-lookup-pitfalls.md` (intent gate and property identity on follow-ups)
- `docs/solutions/workflow-issues/real-model-before-after-eval-on-the-local-stack.md`
- Plan: `docs/plans/2026-10-14-0837-feat-recency-preference-round10-fixes-plan.md` (KTD3–KTD10; units U14–U16 were added during implementation)
