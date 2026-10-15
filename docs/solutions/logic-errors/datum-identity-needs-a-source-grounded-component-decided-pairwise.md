---
title: "A datum's identity needs its source-grounded component, decided pairwise: parallel components are not conflicting values"
date: 2026-10-15
category: logic-errors
module: backend chat verification, conflicts and timeline (app/chat/datum.py, timeline.py, coverage.py, verify.py, final_check.py, tools.py, identity.py, calc.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "Two coefficients from two rows of one table, each for a different consideration, are marked as conflicting sources"
  - "Capitalization rates for different cash flows of one appraisal are marked as conflicting, with an uncertain reading"
  - "A grounded coefficient is removed because its bullet also gives the derived reduction (1 − x as a percent)"
  - "A valuation date the server holds is cut as 'not in the cited sources'"
  - "The answer opens with an omission marker and still refers to 'the two coefficients above'"
  - "A property's group label joins the appraiser's own office address to the property's"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [datum-identity, facet, conflicts, verification, derived-numbers, coherence, identity, coverage, rag, chat]
---

# A datum's identity needs its source-grounded component, decided pairwise: parallel components are not conflicting values

## Problem
Round 13 (PR AriGabay/rag#2) started from two questions a user asked about real appraisals.
- **Question 1** asked about an appraisal that lists two reduction coefficients, each for a different consideration.
- **Question 2** asked about an appraisal that states a capitalization rate for each of several cash flows.

Both answers turned the appraisal's parallel components into competing values, and verification removed or broke what the source supported.

## Symptoms
- **Conflicts:** the timeline grouped the components as one datum and marked every pair as conflicting sources. Rates the model attributed to the appraiser were also marked uncertain.
- **Removals:**
  - a bullet was removed whole, grounded coefficient and all, because it also stated the derived reduction;
  - a valuation date the server stores for the unit was cut;
  - a structural lead-in was withheld as "not classified".
- **A broken answer:** it opened with an omission marker and kept a reference to removed bullets.
- **The group label:** it named the appraiser's office address from the expert declaration as if it identified the property.

## Root causes
- **No component in the key.** The comparability key (kind, unit, period, VAT, basis, scenario, stance) had no component. The row label and the value's own purpose phrase ("ל<cash flow>", "בגין <consideration>") were read at registration and then dropped.
- **Three keys for "same datum".** The timeline, the coverage conflict reason and the "latest" check each had their own key. Amounts were compared as raw strings.
- **Certainty followed an asserted attribution.** An attribution the model asserted (stance, speaker) made the whole number uncertain, although its number, kind and unit were grounded.
- **The judge lacked server facts.** The unit's valuation date reached the model's source header only on search hits.
- **No formula path.** A derived number had no way to be verified by its inputs.
- **No coherence check.** Nothing re-checked the answer as a whole after edits.
- **Identity read party blocks.** It read every labelled address, the appraiser's declaration included.

## What didn't work
- **A component field in the key.** "Different component" is not transitive. A bare value without a component can be the same as one component and different from another, so any key either merges or splits wrongly.
- **Words alone deciding "different".** Two wordings of one datum in two documents can share no words, so a real cross-document conflict would vanish.
- **Treating a missing component as unresolved everywhere.**
  - An appraisal that states a single unlabelled rate lost its history.
  - In non-component families (rent, value), a component-less value no longer met a labelled one in another appraisal, so cross-document conflicts disappeared.
  - Both were caught by review and the real model.

## Solution
1. **A facet on every value and measurement** (`datum.py`). The source's component, consideration or cash flow, with its part and tier:
   - **Own tier:** the row or column label, or the value's own purpose phrase.
   - **Context tier:** the caption, the list lead-in, or the nearest heading within three blocks of its section.
   - **Never a facet:** a label the model wrote.
2. **One pairwise decision: same, different or undecided** (`datum.relation`).
   - "Same" pairs are joined into classes by union-find inside the old key (`timeline.buckets`).
   - **Conflicts:** only inside a class, and only for overlapping periods.
   - **"Different":** needs structural evidence: the same table, list or section, or two own-sentence facets in one appraisal unit.
   - **A missing component:** undecided, never a verified conflict, with two exceptions.
     - An appraisal's only value of its kind is that appraisal's headline datum.
     - In a non-component family, a component-less value is the same datum as a labelled one across appraisals.
   - **One reader:** the timeline, `coverage.conflicting` and the recency check all read these classes.
3. **One canonical amount.** Scale is applied, and percent and fraction meet for rates and coefficients.
4. **Certainty by the attributes that define the number.** Only kind and unit count. Any other asserted attribute counts as not stated everywhere (`calc.Value.certainty`).
5. **Derived numbers verified by formula** (`verify.derive_numbers`).
   - **The formula set is bounded,** each formula behind a wording gate (reduction or addition words, or the relation named in the claim).
   - **A match is a server `derived` computation,** which the judge still sees.
   - **A failed derivation cuts only its clause,** and the grounded number stays.
6. **Server-held unit facts on every source** (`tools.stamp_valuation`). They reach the judge and the date check. Removals carry one of four reasons: contradicts, not supported, `context_not_read` (after a bounded re-read of the defining context), or a technical failure.
7. **Coherence rules as functions of the final state** (`VerifyReport.edit`, `final_check`).
   - A cut that leaves no words removes the whole unit, with no marker.
   - A lead-in whose items are gone goes with them.
   - A back-reference after a removal is judged again.
   - A cut never ends on a connector.
8. **Identity v2 skips party blocks**: the expert's, appraiser's and client's details, and the declaration.
9. **A components table beside the timeline** (`answer.time.breakdown`). A timeline is one datum's history.
   - Undecided and conflict marks name their counterparts.
   - An undecided mark goes only on the side that lacks a component.

## Traps found only on the rebuilt stack
Synthetic fixtures passed before each of these was found. The real reported questions, replayed after every rebuild, and the real model (three runs per sequence) found them all, and each is fixed.
- **Date line on search hits only.** A section read gave no date line, so the date was still removed.
- **"Partly read" was too broad.** Any uncertain OCR block made a unit partly read, so 23 of 26 unidentified units became "possible candidates". Only an unread identification region counts now.
- **Generic probe aliases.** An alias describing the property's type ("sheltered housing") matched every appraisal of that type. Only distinctive aliases are used now.
- **Undecided marks spread.** One bare rate's marks reached every row, until marks were set only on the unresolved side.
- **Marks needed class-level resolution.** If A ≡ A′ and A′ is different from B′, then A is different from B′.
- **The model writes the component into the free-text scenario,** which split the key. A scenario contained in the facet is now not part of the key.
- **Duplicates kept the uncited copy,** so the components block was skipped.

## Prevention
- **Decide identity pairwise, with a reason.** A pairwise decision with a stored reason replaces a key whenever "different" is not transitive. This is the same move round 12 made for property identity.
- **Replay the reported question after every rebuild.** Use a private script against the office data, not only synthetic fixtures. Facet extraction and coverage classification only failed on real wording.
- **Put uncertainty where the evidence is missing.** Mark the side that lacks evidence, not every row it touches. A flood of "undecided" marks reads as general doubt about the numbers.
- **Facts the judge must not contradict are server text on every source.** That covers valuation dates and links, on every source the answer can cite, not only on search hits.

## Related Issues
- **Round 12:** `docs/solutions/logic-errors/latest-datum-needs-property-relation-inventory-and-measured-extraction.md`. It names `dates-v4` as current. Round 13 moved `DATES_VERSION` to `dates-v5`, and migration 0022's downgrade now relabels the rows of the versions that know the column instead of leaving stale primary flags; nothing is deleted.
- **Round 11:** `docs/solutions/logic-errors/time-ranked-answers-need-server-resolved-dates.md`. Its rule that a model-asserted scenario never gates grouping is kept: a scenario that only restates the facet is cleared.
- **Plan:** `docs/plans/2026-10-15-0851-fix-datum-meaning-verification-plan.md`.
- **Tests:** `test_datum_facet.py`, `test_datum_decision.py`, `test_timeline.py`, `test_round13_sequences.py`, `test_round13_real_model.py` (opt-in), `test_identity.py`, `test_migration_0022.py`.
