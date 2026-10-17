---
title: "A datum's context is lost between extraction, cache and display: read it with evidence, key it by span, and carry its state"
date: 2026-10-17
category: logic-errors
module: backend chat datum context, registration, caches and the components table (app/chat/datum.py, tools.py, timeline.py, compose.py, verify.py, api.py; frontend components/chat)
problem_type: logic_error
component: service_layer
symptoms:
  - "The answer body names the consideration of a 10% reduction, while the components table shows the same 10% as 'component not stated in the source' and 'comparison undecided'"
  - "One reduction appears three times in the components table, each row undecided against the others"
  - "Rows the answer never cites, and counterparts the user cannot see, appear in the components table"
  - "A '(verified in part)' mark opens no detail of what was or was not verified"
  - "After a line-wrap fix, a coefficient and its reduction swap components, and two values of one line are marked as conflicting"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [datum-identity, facet, context, span, cache, pdf-line-wrap, components-table, verification, rag, chat]
---

# A datum's context is lost between extraction, cache and display: read it with evidence, key it by span, and carry its state

## Problem
Round 14 (PR AriGabay/rag#2) started from one answer about an appraisal that reduces a base land value twice, each time for a different consideration. The answer body named both considerations correctly. The components table showed the second reduction three times, each row "component not stated in the source" and "comparison undecided". The body and the table had reached two different decisions about the same values.

## Where the context was lost
Tracing every record from the source through extraction, registration, cache, verification and the response found six separate losses. Each one alone was enough to break the table.

1. **The preceding sentence was never read.** The consideration was stated two sentences before the value, in the same paragraph flow, with no other value of the same family in between. The value's own sentence only said "an additional reduction of 10%". The context reader read the value's own sentence, a heading, or a paragraph ending with a colon, and nothing else.
2. **An empty context list meant "read, nothing found".** The stored-measurement path called the facet reader with an empty context list. The result was status `none`, the state for a range that was read and named nothing, although nothing had been read.
3. **A cached facet was final.** A cached `none` or `not_read` facet was returned as is, and the facet memo cached `none` results too, so a better reader never ran.
4. **The display merged two states.** `none` and `not_read` became the same string, so "the context was not read" was shown as "the source does not state it". The response carried only that string.
5. **One span, three records.** After failed take calls, the model registered the same quote span three times under three labels. Each copy had no facet, so each was undecided against the others and they never met in deduplication.
6. **The table took every registered value.** Values the answer never cited were shown, and an undecided mark named a counterpart that was not shown at all.

Separately, the "(verified in part)" mark came from a recency check that read "in the current opinion" (describing the document) as a claim that the value is the latest. Partial marks were not removals, so the UI had no detail to open.

## What didn't work
- **Borrowing the nearest neighbour's component.** Reading more context first made things worse. A value whose own sentence names something, but not in a form the parser recognises, borrowed a neighbour's component. A frozen precision sample of real values caught four such misattributions before any user saw them. Context is now borrowed only when the value's own sentence is bare or refers back ("additional", "as stated", "in light of the above", "the above").
- **Treating every PDF line end as a sentence end, or none.** Joining every wrapped line fixed sentences cut by a wrap. It also joined label–value lines ("<label>: 0.8 / <next label>: 0.9"), so the phrase after one number was read as that number's own. That swapped a coefficient and its reduction and produced a false conflict. Only the real model on the rebuilt stack showed it.
- **Deduplicating by equal amounts.** Equal rates can be different adjustments. The headline rule ("an appraisal's only unlabelled value of its kind") first counted amounts, so two different bare 10% values were taken as one.

## Solution
1. **Span identity at registration** (`tools`, `datum.relation` rule 0). A span is (version, reading, block, number offset), or (table, row, column). A repeated take of one span returns the first record's id, and facets only move forward. A contradiction on a defining attribute is corrected when the model asserted it, and refused when the source attests it. Every registration is logged.
2. **Context completion with evidence** (`datum.reach`, `datum.read_context`). Candidates are the holder's earlier sentences, earlier paragraphs, the heading or lead-in, and the table notes. They are bounded by the appraisal unit and section window, and capped by block count and characters; past the cap the facet is `not_read` with a reason. A candidate counts only with a cue word and a purpose phrase, and with no value of the same family between it and the number. Two components in range leave the facet unresolved. A totals column header is never a component. A list item "<label> – <number>" is the number's own facet.
3. **PDF line wraps read with structure** (`datum.unwrap`). A wrap inside a paragraph is a space of the same length, so offsets keep. The break stays after punctuation or a colon, before a list marker, in a run of breaks, and after a number, unless the next line opens a purpose phrase. A phrase after a number belongs to that number only when no cue word or clause break lies between them.
4. **Four facet states with reasons**, kept apart end to end. `stated`, `none` (read, nothing named), `unparsed` (a phrase that could not be tied), `not_read` (with the reason: cap, not needed, picture cell, and so on). Only `none` means the document gave nothing in the range checked.
5. **Caches complete in place.** The value verifier version is unchanged. A facet reader version and a facet-only `jsonb_set` update upgrade a cached record without deleting anything. The locator key carries the span offset, with a fallback to the legacy key. Only stated facets are memoised, and a stated context facet from an older reader can be re-read.
6. **A structured row state in the response.** Each row carries the reading, the component (status, reason, wording, part, tier, and the context's own location and short quote), the claim support (cited or history), the comparison, and the reason. Rows come only from values the answer uses, and a block needs at least two. The public facet exposes only status, reason, wording, part and tier.
7. **Coefficient and reduction reconcile.** `datum.reconciles` decides reconciled, conflict or unrelated for a coefficient x and a reduction 1 − x. The timeline adds a cross-bucket step that shows them as one adjustment with its formula and input, and `coverage.conflicting` sees the same decision.
8. **Partial marks open the existing detail.** A partial span keeps its ordinal and claim ids. The verification detail lists what is supported, what was not verified, why, and what was checked. A recency claim ignores "current" after a document noun.

## Prevention
- **Freeze a precision sample before fixing extraction.** Real values with their expected components, frozen and hashed before any fix is measured. Synthetic fixtures never contained a neighbour's component in a form that tempted the reader. Use a second sample, frozen after the first was spent, for the final measurement.
- **Replay the reported conversation on the real model after every rebuild.** The line-wrap regression passed every synthetic test and only appeared in the real model's runs.
- **Key a datum by its span, not by its label or amount.** The model's label is free text and its amount can repeat. The span is the only evidence that two records are one datum.
- **Never let "not read" travel as "not stated".** Carry the status and reason in the response contract, not a display string, and never pass an empty context list for a context that was not read.
- **A cache stores a result, not a verdict.** A partial result in a cache must be upgradeable in place by a better reader.

## Related Issues
- **Round 13:** `docs/solutions/logic-errors/datum-identity-needs-a-source-grounded-component-decided-pairwise.md`. Its context tier ("the caption, the list lead-in, or the nearest heading within three blocks") is superseded by the bounded candidate reader above. Its pairwise decision is kept, with span identity added as rule 0.
- **Plan:** `docs/plans/2026-10-16-2330-fix-datum-context-consistency-plan.md`.
- **Concepts:** `CONCEPTS.md` → Datum identity (the four facet states, span identity, coefficient and reduction).
- **Tests:** `test_datum_facet.py`, `test_datum_decision.py`, `test_timeline.py`, `test_cache_keys.py`, `test_round14_sequences.py`, `test_round14_real_model.py` (opt-in), `test_datum_facet_db.py`, `frontend/e2e/round14-context.spec.ts`.
