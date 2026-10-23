---
title: "Quote overlap is not item identity, a row keeps its supported cells, and attribution has levels below the property"
date: 2026-10-21
category: logic-errors
module: backend chat verification, item ledger, number reading and table extraction (app/chat/items.py, final_check.py, scopes.py, verify.py, meaning.py; app/answering/verify.py; app/extraction/hebrew.py, tables.py, pdf.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "Two values in one paragraph, several payments in one quote, or equal values with different conditions collapse into one ledger item, which carries one datum's quote and another datum's value"
  - "A whole table row is removed because one cell was only partly verified, or because one cell states that a detail does not appear in the passage read"
  - "A building-rights figure passes attributed to another plan, or a footnote of one lot group becomes the condition of another lot group, inside the right appraisal"
  - "The server writes the same condition three times into one sentence, or attaches a note marked (***) to rows of another table"
  - "The server adds '(gross, as written in the original)' after a floor number or '(month, ...)' after a form number, or replaces a noun's 'annual' with 'period not stated in the source'"
  - "A claim is removed as contradicting its source although a sentence of the same list item cites a source that states it"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [item-ledger, same-datum, final-check, cell-cut, attribution, scope, footnote, rescue, qualifier, controlled-measurement, rag, chat]
---

# Quote overlap is not item identity, a row keeps its supported cells, and attribution has levels below the property

## Problem
Round 19 of PR AriGabay/rag#2 fixed attribution between properties. Round 20 traced three failures that were still left:
- **Items were lost.** Different data merged because their quotes overlapped.
- **Supported rows were removed.** One unverified cell sent the whole table row out.
- **Data passed with the wrong attribution.** A figure was attached to another plan, lot group or contract inside the right appraisal.

The first measurement after the planned fixes found two more shapes from round-20 code, and six older server mechanisms that wrote wrong qualifiers, conditions and removals. All of them are fixed in the same PR, which is not yet merged.

## What fixed it, and the part the code does not say

### Overlap is structure, not identity
- **When two items are the same:** only when their datum agrees (`items.Datum`, `same_datum`). The datum is the headline values, the dates and the condition words. Containment counts only when one item has no value.
- **What else follows from that:**
  - One wide quote can carry several data that the same question needs.
  - A later-version rewording replaces an item only when the datum is the same (`Ledger._supersede`).
  - A closed decision moves only through a real twin.
  - A condition check is deferred only to an item of the same component and the same datum (`_defers`).
- **Two rules that looked right and failed:**
  - "One fill per component, an overlapping agent fill replaces it" (round 19) merged different data.
  - "Never merge in the same version" broke a legitimate wider quote of the same row.
- **Replay of round-19 turns:** harmful merges in the same version went from 2 to 0.

### A row keeps its supported cells
- **When a cell is cut or marked alone:** it is not the key cell, no other claim rests on it, and its text carries no qualifier the other cells read through (`verify.row_cut_safe`, used by the final check too).
- **The guard that broke on real data.** The qualifier guard read a negation word as a condition. A cell in which the model wrote "X does not appear in the passage read" then sent the whole row out, together with a supported cell of another column.
  - **The fix:** an absence statement (`states_absence`) is not a qualifier.
  - **The lesson:** a guard that protects conditions must tell a condition apart from an absence statement.
  - **Found only on the real runs.** The synthetic reproductions all passed.
- **Where the partial mark goes:** inside the cell. Written after the row's last bar, it added a column.
- **Draft verdicts:** the final check reuses the draft verdict when only the context line above the table changed (`verify.key_context`). A key that held the context line re-judged the row and removed it.

### Attribution has levels below the property
- **The levels:** plans, lots, parcels, contracts, alternatives, stages and approved or proposed states inside one appraisal (`backend/app/chat/scopes.py`, `SUBREFERENT_CHECK = "scope_mismatch"`).
- **Footnotes:**
  - A footnote binds by its mark or by stored word geometry.
  - Marks include bracketed stars with inner spaces, such as `(***)` and `( ***)`.
  - A note whose mark another item's quote carries is not this item's condition (`items.condition_spans`, `_note_of_row`).
- **Duplicates:** the same condition is never written twice into one unit.

### A judge that sees only a sentence's own citations removes supported claims
- **The failure:** the claim was stated one sentence later, in a source the same list item cites.
- **The deterministic rescue** (`rescue_judged`, `rescue_unstated`, `_holds_claim`) is safe only with strict gates:
  - every number, content word and negation of the claim stands in **one** sentence or row of a source cited beside it;
  - the context check and the scope check find no difference;
  - the claim is kept, cited to that source, and marked partly verified, never fully verified;
  - the mark says honestly whether the holder is the sentence's own source or a neighbour's.
- **Safety replay on the stored real runs:** of 121 claims the judge failed, the rescue kept 2. Both were right, and 0 were wrong.

### Label digits are not amounts, and an answer-side rule stays on the answer side
- **Identifiers, not amounts:** a form, regulation, floor, basement or parking-level number, and a footnote mark, are identifiers (`backend/app/answering/verify.py` `not_an_amount`, `meaning.note_mark_at`).
- **The exception:** a counted noun after the label keeps the number a quantity ("8 apartments on each floor").
- **A noun's period word** ("the annual profit") does not attach to a later percent (`meaning._noun_period`).
  - Run on the source side as well, the rule spread a line's period to unrelated numbers. A private replay found this before the rebuild.
  - `parse_source` passes `noun_period=False`.

### Text-layer order
- **On a Hebrew-free page,** drawing order is kept only with evidence: a Latin header, or ascending years or quarters (`tables.reads_left_to_right`, `hebrew.page_has_hebrew`).
- There is no blanket reversal and no OCR change.
- Stored readings keep the old order until an approved re-read. `READER_VERSION` is unchanged.

## What the measurement showed
- **The method:**
  - Blind pairs: each grader got one run of the final build and one run of a control or of the intermediate build.
  - Control runs were graded twice by different graders, who agreed within 0–2 items.
- **What the first build showed:** the first build after the planned fixes had two regressions of its own and six older mechanisms. It was fixed, rebuilt and fully re-measured.
- **Final build compared with a same-day run of the round-19 build:**

  | Set | Measure | Control (round 19) | Final |
  |---|---|---|---|
  | All | Whole table rows removed wrongly | 1 | 0 in 9 runs (intermediate build: 6) |
  | Round-21 regression set | Full answers / required items | 7 / 74 | 7–8 / 72–79 |
  | Round-20 regression set | Full answers / required items | 11–12 / 80 | 12–14 / 74–84 |
  | Round-22 set | Full answers / required items | 7 / 60–62 | 7–9 / 63–66 |
  | Round-22 set | Another property's data passed | 0–2 | 0 |
  | Diagnostic set | Full answers / required items | 17 / 64 | 14–18 / 55–59 |
  | Round-18 regression set | Full answers | 21 | 19 |

- **The two weaker sets:**
  - The diagnostic-set items were agent omissions and one provider timeout, not removals.
  - The round-18 set lost two conditions that the model did not write.
- **Cost per full answer:** flat.
- **Still wrong:**
  - a server condition note of one list item attached to a sibling item;
  - a period tag on a capitalised total;
  - 1–4 false "read and not used" lines per run, on numbered lists and parallel wording;
  - the model swapping annual and quarterly columns.
- Details: `docs/evaluation/conversational-rag.md`, round 20.

## Prevention
- **Never treat overlap or containment of quotes as identity.** Compare the datum, and keep the version rule ("same item, reworded") separate from the batch rule ("two data in one quote").
- **Pair every protective guard with the shape it must not catch.** For the qualifier guard that is an absence statement. For the label-digit rule it is a counted noun. For an answer-side rule it is the source side.
- **A rule that keeps a removed claim must leave it marked.** Show it as partly verified, never as verified, and replay it over every stored judge failure before the rebuild.
- **Measure the post-change build before calling the round done.** A blind pair against a same-day control found regressions that all the synthetic reproductions passed. A fix found there means a new build and a full re-measurement.

## Related Issues
- `docs/solutions/logic-errors/attribution-needs-positive-evidence-three-states-and-one-coverage-decision.md` (round 19). Its "an agent fill replaces only a fill of its own component" rule is refined here: overlap alone never makes two items one.
- `docs/solutions/logic-errors/meaning-was-lost-in-verification-not-the-model-and-conditions-need-their-own-check.md` (round 18). This is the condition check whose server notes now bind by mark and are written once.
- `docs/solutions/logic-errors/completeness-belongs-to-the-answer-version-and-omissions-need-an-item-ledger.md` (round 17). This is the item ledger that the same-datum gate refines.
