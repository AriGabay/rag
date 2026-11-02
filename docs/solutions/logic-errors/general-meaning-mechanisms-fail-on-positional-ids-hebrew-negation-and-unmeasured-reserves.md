---
title: "General meaning mechanisms fail on positional ids, Hebrew negation forms and unmeasured reserves, and only real drafts expose it"
date: 2026-10-23
category: logic-errors
module: backend chat verification, coverage, conversation state and question parsing (app/chat/verify.py, items.py, slots.py, tools.py; app/extraction/normalize_text.py, tables.py; app/answering/parser.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "A condition note is written next to a datum the judge never ruled on, after a repair round renumbers the addition questions"
  - "A 'read and not used' line names an item the answer states with a copular negation in another gender ('אינה' for the source's 'אינו')"
  - "A supported sentence such as 'the two areas אינם נתונים לאותו היקף' is dropped as an absence statement"
  - "A fact about the property ('חסר היתר') or a figure followed by 'ללא פירוט' loses protection as if it said that information is missing"
  - "Questions with inflected words ('שוויו', 'מחירו', 'נמכרים') stop being read as calculations after a whole-word matcher replaced a substring test"
  - "A follow-up opening with the function word 'ומה' binds to a list whose kind noun is 'שומה'"
  - "Late turns lose supported claims as 'not checked: timeout' after the first judge call is cut to leave a retry slice"
  - "A condition note reads 'התנאי לפי המקור של <the condition>: <the condition>'"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [verification, judge, positional-id, carried-votes, hebrew-negation, copular, absence, coverage, gap-line, whole-word-match, inflection, list-binding, retry-reserve, latency, replay, blind-grading, measurement, rag, chat]
---

# General meaning mechanisms fail on positional ids, Hebrew negation forms and unmeasured reserves, and only real drafts expose it

## Problem
Round 22 of PR AriGabay/rag#2 (unmerged at the time of writing) replaced narrow point fixes with general mechanisms that keep a datum's meaning from the stored table to the next turn:
- table continuation decided by structure, and one serve direction for every path;
- notes applied by scope;
- coverage only by a positive statement, through one absence reader;
- read and check states;
- row-level attribution, plus an applies-to-datum question inside the existing judge call;
- three-state provenance;
- one number reader;
- history kept as system notes;
- named conversation lists;
- one VAT reader.

Unit tests, property tests and a per-unit replay of stored real drafts were green ("harmed = 0" for every unit). Code review and blind-graded measurement still found that each general mechanism brought its own failure mode, and every one of these failure modes was silent.

## What went wrong, and the rule that prevents it

### 1. Key a model's answer by what it was asked, never by a positional id
- The applies-to-datum questions were numbered `A1`, `A2`, … in plan order. That order is re-planned in every verification round.
- Requirement votes carried into a repair round (`VerdictCache.reuse`, `_carried`) kept their `applies` answers. After the repair renumbered the plan, a stale "A1: applies" for one row was read as the answer for another row, and a condition was written next to a datum the judge never decided. Two reviewers found this independently.
- **Fix:**
  - A vote stores the asked `(kind, datum, unit)` with each answer it admits (`JudgeRequirement._asked`), and answers are matched by that triple.
  - A vote reused for moved units carries no addition answers.
  - The same-signature carry keeps its answers, because the plan there is identical.
- **Rule:** any id the server invents per call (A#, batch index, list position) is a display handle. Never use it as the key that joins an answer to its question across calls.

### 2. Hebrew negation is not one thing
- **Existential absence** says the information is not there: `אין`, `לא נמצא`, `ללא`, `חסר` + an information or document noun, and the participles of knowing and stating (`אינו ידוע`, `אינו מצוין`).
- **Copular negation** classifies: `אינו / אינה / אינם / אינן / איננו / איננה` + a noun means "X is not a <noun>", as in `אינה נתון השוואה` ("is not a comparison datum"). It states something positive about the datum. `normalize_text.COPULAR_NEGATIONS` keeps it out of the information-noun absence branch.
- **One negation family across inflections.** When the item's own quote negates (`אינו מהווה …`), an answer writing `אינה …` repeats the datum, it does not negate it. `normalize_text.negation_family` maps the copular inflections to one key for the item's own negations.
- **`חסר` + a feature is a fact about the property.** `חסר היתר`, `חסרה מעלית` are not missing information. They count as absence only when they predicate an information or document noun.
- **A figure before a prepositional negation keeps its clause positive.** In `50,000 ₪ ללא פירוט`, the figure stands.
- **What the mistake did:** false "read and not used" lines, and dropped supported sentences.

### 3. A whole-word matcher silently drops the forms a substring test read
- Replacing a substring test with a whole-word match (prefix letters allowed) stopped matching possessive and plural endings: `שוויו`, `מחירו`, `ממוצעו`, `נמכרים`, `מבוקשים`. Those questions quietly changed intent.
- **Rule:** before replacing a substring rule, diff its decisions against the base parser over inflected forms and list the forms explicitly (`parser.VALUE_FORMS` and the other word tuples).
- Keep the whole-word guard where a substring was wrong, for example `שוויון` ("equality").

### 4. Strip prefixes from the user's words only, never from the stored noun
- A list's kind noun is a base form. Prefix-stripping `שומה` yields `ומה`, which then matched the function word `ומה` in "ומה לגבי השני".
- `slots._forms(word, base=True)` never strips the noun.
- Function words (`מה`, `ומה`, `זה`, `לגבי` …) never name a list.

### 5. Size a retry reserve from measured latency
- A retry slice cut the first judge call to "deadline minus 20 s" whenever 35–65 s remained.
- Measured healthy judge calls, 485 of them: p50 20 s, p90 31 s, p99 40 s, max 44.5 s. So the cut timed out normal 25–45 s calls, and the 20 s retry then often failed too.
- **Rule:** reserve the retry slice only when the first call can still get the p99 of healthy calls (`verify.JUDGE_FIRST_CALL_MIN_SECONDS = 40`). Otherwise give the single call all the time, as before.

### 6. Name a row in a server note by the row's own label
- When one sentence merges several rows, the note names the row it applies to. The name was taken from the item's quote. When the quote was the condition sentence itself, the note said the condition twice.
- **Fix:** `items.row_name` returns the first cell of the quoted table row, or nothing. With no row label, the note is written without one.

## Method lessons
- **What fixed-draft replay proves, and what it does not.**
  - Replaying the server's deterministic path on stored real drafts (no model) is the right gate per unit, and it caught most harms before any model run.
  - It also showed that most new mechanisms almost never fire on real drafts. Their benefit is proven on synthetic shapes only.
  - Report that honestly; do not turn "harmed = 0" into "improved".
- **Separate code effects from model variance.** Replay the end-to-end drafts of the final build with the base code. In this round the large between-arm differences (removals, gap lines, cell swaps) gave identical output under both codes, so they came from the model's drafts and the judge's verdicts, not from the change.
- **Blind pair grading found real server regressions that the replay could not.** These were the garbled row-named note and the copular-negation gap line. They only appear on drafts the old build never produced, so grade the final build's own outputs, not only replays of old drafts.
- **Measurement hygiene:**
  - Run both arms concurrently and in balanced order.
  - After a provider outage, rerun the affected conversations for both arms with the same list, keep the originals aside, and say so.
  - Give each harness container its own oracle file. A shared one is emptied by any non-oracle run.

## Outcome (measured, all sets are regression)
- **Builds:** the measured final build of round 22 (PR AriGabay/rag#2) against a same-day control (round base plus measurement fields only).
- **Stage 2, fixed sources (58 turns per arm):**
  - required items correct: 230 against 216;
  - wrong removals of required items: 2 against 12;
  - supported information deleted: 7 against 17;
  - wrong server additions: 22 against 28;
  - wrong gap lines: 15 against 13.
- **Stage 3, end to end (190 turns per arm):**
  - wrong answers: 1 against 6;
  - required items: 610 against 610;
  - missing caveats: 35 against 44;
  - wrong server additions: 50 against 48;
  - wrong removals: 19 against 13;
  - wrong gap lines: 30 against 21.
- **Gate:** the criterion that the mechanism counters fall beyond the noise band was not met, so end-to-end improvement is not proven.
- **Details:** `docs/evaluation/conversational-rag.md`, round 22.

## Prevention
For every general mechanism that replaces a point fix, test the shapes it will meet on real drafts:
- ids that are renumbered between calls;
- the inflections of every word it matches;
- the grammatical role of a negation, not only its presence;
- the measured latency of any time budget it splits.

Then grade the final build's own end-to-end outputs blind before calling the round done.

## Related
- [server-additions-need-a-bound-datum-and-place-and-fixes-come-from-replaying-real-drafts.md](server-additions-need-a-bound-datum-and-place-and-fixes-come-from-replaying-real-drafts.md) (round 21)
- [overlap-is-not-identity-keep-the-supported-cell-and-attribute-below-the-property.md](overlap-is-not-identity-keep-the-supported-cell-and-attribute-below-the-property.md)
- [completeness-belongs-to-the-answer-version-and-omissions-need-an-item-ledger.md](completeness-belongs-to-the-answer-version-and-omissions-need-an-item-ledger.md)
