---
title: "Read evidence never reached the answer, and appraisal units need page-structure signals that agree"
date: 2026-10-18
category: logic-errors
module: backend chat completeness, repair round and appraisal units (app/chat/verify.py, coverage.py, evidence.py, engine.py, units.py; app/platform/search.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "An answer leaves out a rate, a caveat or a list item that a passage the turn read states, and the requirements check calls the answer complete"
  - "A component is partial, the evidence is in a section the agent read, and no repair is asked because nothing was registered as a value"
  - "A repair round adds the missing part, but the final answer is the pre-repair one because the repair dropped one verified sentence"
  - "One unit of a long compendium holds the end of one report and the whole next report; appendix exports of a report become separate units"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [completeness, requirements-judge, read-passages, support-quote, repair-round, lost-supported, unit-boundaries, running-header, reference-audit, rag, chat]
---

# Read evidence never reached the answer, and appraisal units need page-structure signals that agree

## Problem
After round 15 (PR AriGabay/rag#2) a private diagnostic set still had nine turns where the agent had read the source holding the missing part, yet the answer stayed partial. Three independent blocks stopped completion. A fourth problem sat in ingestion: in a long compendium, unit boundaries came from one weak signal.

## Why the read part never reached the answer
1. **The judge never saw what was read.**
   - The requirements judge scored the frozen request components. These were coarse, often one component for the whole question.
   - Its workspace listed each read section only as "read: «section» in «title»", with no text.
   - Its policy asked it to name read and search ids only as "what searched for it".
   - So it judged against the question's wording and called the component `full`.
2. **Only registered data could trigger a repair.**
   - `verify._check_requirements` raised a repair problem only when `coverage.related_evidence` found values, measurements or calculations.
   - A read text source went to `checks`, and the branch continued.
   - An explanation, a condition, a caveat or a list item in prose could never start a completion.
3. **A grounded addition was thrown away.**
   - When a repair added the part but rephrased away one verified claim, `lost_supported` discarded the whole repaired answer.
   - The pre-repair answer, without the addition, was what the user saw.

## What fixed it, inside the existing mechanisms
- **The existing requirements call gets the read text.** It gains a bounded `<read_passages>` block:
  - It holds the passages the turn read in the asked units, cited or not. For a cited source shown only as an excerpt, it holds the segments the judge did not already see.
  - Long passages are clipped against the component texts (`evidence.fit_segments`), never by prefix, and the block is capped.
  - The judge may return `support: [{S#, quote}]` for a part the answer omits.
  - No judge round is added.
- **The server validates support before it counts.** Each condition drops the entry silently when it fails:
  - the source was read or is cited;
  - the quote is verbatim after normalisation, with table cells, line wraps and the prompt's look-alike angle brackets read back;
  - the passage lies inside an asked unit and not across a probable edge into a neighbour;
  - the document is one the answer is about;
  - the quote is at most a sentence or a row;
  - no single answer unit already holds the quote's number with at least half of its label words, not counting the component's own words, so an omitted rate equal to a listed one still counts.
- **Completion.**
  - A partial component with valid support gets `REQ_TEXT_FOUND`, which names the part, the passage and the quote.
  - A repair round whose problems are all text completions of non-calculation components runs with tools off. It completes from the evidence instead of searching again.
  - If it still fails, the gap line says the part was read at its place and not used. It never says "not found".
- **The repair keeps what was verified.**
  - Lost verified claims are restored at claim granularity, beside the unit that holds most of their words. After a table, a blank line separates them, or the renderer reads them as a table row.
  - The old discard remains only when the repair added nothing grounded or time is short.

## Why the units merged, and what bounds them now
- **What went wrong in the compendium.**
  - A foreign-language report started with a cover page. Every page then carried a running header "<report type> – <property>", and the running footer changed to the new firm's line.
  - There was no subject line, no Hebrew letter opening and no page-number drop, so nothing cut there.
  - Meanwhile, appendix exports that restart page numbering at every export cut the previous report into fragments.
  - Verification then removed correct values from the merged unit, which had no identity of its own, because another unit's card held a short brand that the whole compendium shares.
- **`units-v6` adds structural signals that must agree.**
  - **Running lines.** A page's top and bottom lines come from the page text, with digits, dates and page numbers stripped, and are grouped into local runs.
  - **The gate.** A long run that replaces another on the same edge cuts only when a second signal agrees: the other edge changes too, a cover page precedes it, or the page numbering restarts. The cut moves back onto that cover page. A divider page is not a cover.
  - **Continuity.** Every cut without a subject line or letter opening is dropped when its pages name the previous unit and state no new address. The check walks back over untitled units to the nearest named one.
  - **Identity.** A unit takes its name from its own constant top running segment and from its cover title. It never takes it from the bottom office line, or from a line carrying an e-mail address, a URL or a long number.
  - **Storage and rollout.** v6 rows are stored beside v5 and nothing is deleted. A read-only comparison of v5 and v6 cuts over every stored document came before rollout.

## The references were part of the gap
A reference audit with page images found 7 of 35 required needs that the question never asked for (reference over-demand), plus 2 wrong values and 4 readings that should be accepted alternatives. Graded against the corrected references, the previous build's full answers on the set rose from 12 to 18 of 29. Read a gap as a product failure only after the reference survives an audit against the source.

## Prevention
- **Check what the judge actually sees** before concluding "the model omitted it". A coarse component plus a judge blind to the read text always scores full.
- **Server-validate any evidence a judge can name.** A capped or clipped view is "not read", never "not stated" or "not found".
- **Never discard a grounded addition to protect a verified claim.** Restore the claim.
- **Require boundary signals to agree.** A lone page-number restart over-cuts appendices and under-cuts reports that lack a Hebrew opening. Audit a new derivation against the stored corpus before rollout.
- **Audit references** with page images before counting failures, and report results on both the old and the corrected references.

## Related Issues
- `docs/solutions/logic-errors/server-rules-overriding-the-model-and-layout-only-unit-identity-break-multi-appraisal-answers.md` (round 15): additive derivations, tagged handles, "an empty identity is unknown", and the shared-name rule that the v6 identity work builds on.
- `docs/solutions/logic-errors/datum-context-is-lost-between-extraction-cache-and-display.md` (round 14): "not read is not 'not stated'", which here becomes "read and not used is not 'not found'".
- Measured results for round 16: `docs/evaluation/conversational-rag.md`, round 16.
