---
title: "Attribution needs positive evidence and three states, evidence is not the requirement it fills, and coverage is one decision over the final text"
date: 2026-10-20
last_updated: 2026-10-21
category: logic-errors
module: backend chat verification, item ledger and number reading (app/chat/contexts.py, verify.py, tools.py, items.py, coverage.py, meaning.py, anchors.py; app/answering/numread.py, verify.py, compose.py; app/measurements/values.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "Values read on an appraisal's own later pages are removed as 'another property' because that page range is a separately cut unit with a name of its own"
  - "Every row of one side of a two-appraisal comparison is removed because that side's name did not resolve"
  - "A comparison claim that cites one source of its own side and one of the other side is removed"
  - "The server writes 'read and not used' next to items the answer states in a list item, a paragraph or a payment schedule"
  - "A decimal comma (13,2%) is read as a thousands separator and a negative percent loses its sign; an annual column header written with an abbreviation loses its period"
  - "An agent ledger item deletes a source item of another request component because their quotes overlap"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [attribution, another-property, three-state, comparison, item-ledger, coverage, gap-lines, decimal-comma, number-sign, controlled-measurement, rag, chat]
---

# Attribution needs positive evidence and three states, evidence is not the requirement it fills, and coverage is one decision over the final text

## Problem
After round 18 (PR AriGabay/rag#2), the round-18 held-out set gave a full answer in only 7 of 18 turns. Tracing that build's runs located four attribution failures:
- **"Another property" removals.** The deterministic check compared the context a claim's evidence cites with one "asked" set. It ignored the cited text, counted any unit heading as a name (a sentence ending with a colon, OCR digit noise), and in a comparison removed every row of the side whose name did not resolve.
- **False "read and not used" lines.** 33 of 46 item lines and 16 of 23 gap paragraphs were false. Coverage demanded every number of an item's value inside one sentence, picked identifiers as values, never checked items the agent had declared, and counted twin items left from earlier answer versions. None came from a technical failure.
- **Numbers.**
  - A round-18 split-word repair joined the last letter of an abbreviation to the next period word, which destroyed abbreviation headers.
  - A decimal comma was read as a thousands separator.
  - Negative numbers lost their sign.
- **The ledger.** Its key held the turn's S# handle, and `merge` deleted a source item of any component whose quote overlapped a later agent item.

## What fixed it, and the part the code does not say
- **Attribution is three-state and needs positive evidence.** `Contexts.compare` returns same, different or unknown.
  - **Binary continuity was wrong both ways.** "A unit with no identity continues the previous one" and "a separately cut unit is another property" were both wrong.
  - **Only different removes,** and only when the asked side is resolved.
  - **Unknown never removes.** It adds a neutral fact line to the judge call that already exists.
  - **Evidence, in order:**
    1. names, parcels and addresses in the cited text;
    2. the unit's own identity (a colon-ended sentence or digit noise is no identity);
    3. a link that conflicts with neither;
    4. established continuity.
- **"Distinguishing" is not "different".** A name no other unit holds was taken as a conflicting identity. On real data, the later pages of one appraisal (cut by a false subject-line signal) were named "<generic kind word> <the asked property's whole name>".
  - **Same-property evidence (`Contexts._overlaps`):**
    - containment of a whole multi-word distinguishing name, with only generic extra words;
    - a shared parcel;
    - a shared address where the parcels do not differ.
  - **Still different:** a longer name that adds a real name (another town), or a name that shares only a brand word.
  - **Found only on real documents.** This mechanism appeared after the synthetic reproductions of the diagnosed shape already passed. A synthetic fixture copies the shape you diagnosed, not the naming variety of real cut units.
- **Decide over all the evidence a claim cites.** Returning on the first foreign source removed comparison claims that also cited their own side. Now only a claim whose decided evidence is all another property's is removed. A mix keeps the claim and gives the judge one fact line.
- **Evidence identity is not requirement fulfilment.**
  - **Identity without the handle:** evidence is (document, appraisal unit, flat quote), without the turn's S# handle.
  - **One fill per component:** each request component gets its own fill, with state, reason, origin and version.
  - **Replacing:** an agent fill replaces only a fill of its own component. *Refined in round 20:* even within one component, quotes that overlap or nest do not make two fills one; only the same datum (values, dates, conditions) does. See `overlap-is-not-identity-keep-the-supported-cell-and-attribute-below-the-property.md`.
  - **Closed decisions stay closed:** a repeated "included" does not reopen a decision such as "not applicable" with a reason.
- **One coverage decision over the final text.**
  - **What it is:** a pure function over the model-written blocks of the answer as edited: a list item with all its sentences, a paragraph, a table row with its heading. Server lines cover nothing.
  - **Who reads it:** it is computed on every compose pass, and the body's completeness, the gap paragraph and the item lines all read the same result.
  - **Outcomes:**
    - covered;
    - partial, naming the missing detail in the source's words;
    - omitted;
    - "the check could not be completed".
  - **Twins:** items with one value that differ only by a date are told apart by that date. The twin check looked only at letter words, then demanded an unrelated label word that the answer wrote in another form.
- **Numbers: one shared comma reader** (`backend/app/answering/numread.py`).
  - **A comma is decimal only by context:**
    - a whole table cell;
    - next to a percent, a currency or a unit;
    - the document's own convention.
  - **Lists and thousands:** a bare "d,d" stays a list. A single ",ddd" stays thousands, a residual ambiguity that is stated in the module.
  - **Signs:** a negative source number keeps a signed form, so a negative claim needs a negative source.
  - **Split-word repair:** it never takes a letter after a quote mark.
- No model changed and no call was added. `JUDGE_VERSION` went from 17 to 19 inside the existing calls.

## What the measurement showed
- **Final build compared with a same-day run of the previous build (blind, each grader given one run of each build):**

  | Set | Measure | Previous build (same day) | Final build |
  |---|---|---|---|
  | Round-18 held-out (now regression) | Full answers | 5 / 5 | 7 / 8 |
  | | Required items correct, of 100 | 64 / 61 | 75 / 69 |
  | | Wrong removals of required items | 12 / 10 | 2 / 0 |
  | Round-17 held-out | Full answers | 8–9 | 10 / 11 |
- **Other sets:**
  - The diagnostic set is unchanged (mean 18.0 against 18.3).
  - A regression set lost 2 full answers, through agent behaviour and a timeout, not through any verification removal.
  - A new independent set gave 9 of 16 full answers.
- **False "read and not used" lines did not fall consistently.** They ran 6–8 per run on the round-18 set, as in the control, and 6 of 9 on the independent set.
- Details: `docs/evaluation/conversational-rag.md`, round 19.

## Prevention
- **Never let absence of identity decide attribution.** Absence is "unknown". Only positive evidence of whom a datum belongs to, compared with the claim's own attribution, removes.
- **Test identity rules against the naming variety of the real units**, not only the diagnosed shape: generic prefixes, longer names, shared parcels, a property named by name in the question and by address in its unit.
- **Measure against a run of the previous build on the same day, and give each blind grader one run of each build.**
  - Grading itself was consistent: the same run graded twice matched within 3 items.
  - Variance between days was not: the previous round's single run of the same set scored 8–18 items above the same-day control.
- **Before calling a timeout provider-side,** compare input size, agent steps and per-call latency with the same turns of the previous build and a concurrent control run. Failed turns stay in every total.
- **A fix found on a regression set means a new build and a full re-measurement.** The set it came from is regression from then on.

## Related Issues
- `docs/solutions/logic-errors/meaning-was-lost-in-verification-not-the-model-and-conditions-need-their-own-check.md` (round 18): the stage experiment and the per-mechanism fixes this round continues; its "still wrong" list is what this round addressed.
- `docs/solutions/logic-errors/completeness-belongs-to-the-answer-version-and-omissions-need-an-item-ledger.md` (round 17): the item ledger and answer-version completeness that the evidence/fill split and the one coverage decision refine.
- `docs/solutions/logic-errors/server-rules-overriding-the-model-and-layout-only-unit-identity-break-multi-appraisal-answers.md` (round 15): "an empty identity is unknown, not different", which this round turns into the three-state comparison.
