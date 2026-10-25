---
title: "Decide answer-quality fixes on paired replays of real drafts, state what a replay cannot see, and check every consumer of a loosened test"
date: 2026-10-25
category: logic-errors
module: backend chat verification, coverage, items, meaning and tools (app/chat/verify.py, items.py, coverage.py, meaning.py, tools.py, columns.py) and the private evaluation method
problem_type: logic_error
component: service_layer
symptoms:
  - "An end-to-end A/B of two builds cannot decide whether wrong server additions, wrong removals or wrong gap lines fell, whatever the result"
  - "Noise bands larger than the control's own count make a 'fall beyond the band' rule impossible to pass"
  - "A fix shows no effect in the replay of stored drafts although its unit tests pass"
  - "Every judge quote newly admitted by a whitespace-tolerant verbatim test becomes a false 'read and not used' line"
  - "A correct sentence giving a price per built square metre is removed after the built-area word was added to the vocabulary"
  - "The same stored answer gets its coverage-note tables in a different order in two processes"
  - "A judge failure whose reason merely mentions a column is turned into a 'partly verified' mark although the source states another number"
  - "A claim kept by a rescue is removed again by the final check, or a decision for one row keeps an equal number of another row"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [evaluation, replay, paired-comparison, minimal-detectable-difference, grading, path-not-exercised, verbatim, loose-match, consumers, vocabulary, ocr-spelling, unknown-is-not-absence, determinism, hash-order, judge-backstop, settled-decision, datum-key, docker-disk, rag, chat]
---

# Decide answer-quality fixes on paired replays of real drafts, state what a replay cannot see, and check every consumer of a loosened test

## Problem
Round 22 of PR AriGabay/rag#2 (unmerged at the time of writing) measured its build end to end against a control and could not decide anything. Its noise bands came from two control runs graded by different graders with different counting conventions, one degree of freedom per set, turns treated as independent, and a pass rule ("fall by more than the band") that no build could meet when the band exceeded the control count. Round 23 diagnosed the remaining errors from the stored traces, fixed the mechanisms behind them, and had to decide each fix in a way the measurement could actually support. Along the way several general fixes carried their own failure modes that only real drafts exposed.

## Root cause
- **Power.** The answer-quality error classes are rare: tens per 190 turns. With about 60 conversations the 80%-power minimal detectable paired difference was 56-182% of the control count even at three repetitions, so a 25% effect would need roughly 900-3,200 conversations. An end-to-end run cannot decide these classes, so it can only guard.
- **Replay blind spots.** The private replay rebuilds a turn's workspace from what the turn stored: the cited sources, values from their stored public fields, and stored verdicts. It has no context data, no unit focus, no stored word positions and no uncited read sources. Fixes that act through those (rescue over sources the turn read, the follow-up identity guard, page-text column binding, the judge's column note) never fire there. A new per-value completeness flag is missing on every rebuilt value, so the replay sees "incomplete" everywhere.
- **One loosened comparison, several consumers.** A whitespace and line-break tolerant verbatim test was right for `take_value` and the anchor locator: a quote whose source splits a word now maps to the source's own token positions. The same test in the judge-support check admitted quotes that downstream presence tests read word by word against the answer, so they could not find the part the answer gives. Each newly admitted quote became a false "read and not used" line.
- **Vocabulary changes what is checked.** Recognising the built-area word turned an unchecked phrase into a basis claim. The source's header wrote the square-metre word in an OCR spelling (`מ" ר`, a space after the quote mark), so the evidence reader saw only the other basis in the row and removed a correct sentence.
- **Hash order.** Iterating a Python `set` of string ids (`coverage.tables_presented`) leaked hash randomisation into the order of the tables in a user-visible coverage note.
- **Keyword backstops.** A server backstop turned a judge failure into a mark when the judge's free-text reason mentioned a column and the source held any row without column positions. That also converted real contradictions.
- **Settled decisions keyed by the wrong identity.** A rescued claim was settled under the key of its pre-citation unit, but the composer then cited the holder, so the final check computed another key and judged again. A loose wording match also dropped the row discriminator, so a decision for one row could keep an equal number in another row.

## Solution
- **Paired stages, decided before measuring** (the private evaluation design records the pre-registered rule):
  - **Stage 1, identical drafts.** The new code's deterministic path runs against the control's on the same stored drafts and stored verdicts. Every changed server line is extracted by a script and graded against the source as `fixed`, `new_error`, `reworded` or `ok`. The rule: improved when fixed is at least 25% of the eligible traced failures, new errors are at most 10% of fixed, and no correct required datum is lost.
  - **Stage 2, live judging** on fixed drafts and evidence, for judge-dependent changes such as the bounded confirming reading.
  - **End to end** only for guards: failed, empty and timed-out turns, cost and time per turn, and required items. Target classes are reported there as "not decidable by design".
  - Units are enumerated by a script and only labelled by graders. Both arms of a conversation go to one blind grader. A conversation is one cluster.
- **Per-unit results** (stage 1 unless noted):
  - U1 gap lines: 19 fixed, 2 new. That misses the 10% rule by the strict count, and this is reported as such.
  - U2 source-statement notes: 26 fixed, 1 new.
  - `take_value` probe over 1,768 recorded calls: 61 fixed, 0 new.
  - Loose judge-support test: 0 fixed, 4 new, so it was withdrawn (`verify.SupportScope.verbatim` stays exact).
  - Confirming reading (stage 2): 3 calls, 1 wrong removal undone, 0 wrongly kept.
  - Tools-off completion inside the verification allowance: 0 rounds admitted in 34 turns. The path was not exercised, so it was deferred and not committed.
- **Say what a replay cannot see.** Each replay result is written with the paths it cannot exercise ("path not exercised"). A change whose only evidence is synthetic tests is reported that way.
- **Unknown is not absence.** An attribute the answer states that the registration lacks, or that an incomplete read could not confirm, is never grounds for a removal. Only the source stating another value is (`calc.Value.read`, `meaning.passage_read_in_full`). The shared reader normalises the source's unit spellings (`meaning._norm`), and the basis is compared per number against the number's own column.
- **Defined order.** Ids are iterated in id order (`coverage.tables_presented`).
- **Tie a backstop to its record.** `verify.column_unknown_marks` converts only when the judge led its reason with the marker, or the failure kind can be a column mix-up. Either way the claim's number must sit in a row recorded as unbound (`_in_unbound_row`). A keyword alone never converts a contradiction.
- **Key decisions by what the consumer sees.** `verify.settle_decision` also settles a rescued or split decision under the unit that cites the holder. `decision_for` compares the unit wording on the loose path too.
- **Operations.** Each measurement image adds about 2.3 GB plus build cache. On a nearly full disk, prune the build cache and old measurement images (with the user's approval) and trim the VM disk. When dependencies are unchanged, an overlay image (`FROM` the control image, `COPY` the new code) avoids a full rebuild.

## Prevention
Before reading any replay or end-to-end result, write down:
- which paths that harness can exercise;
- what a 25% change would need in conversations;
- for every loosened or widened test, which consumers read its output.

Then grade every changed line against the source rather than counting. When a fix's only evidence is "harmed = 0", report it as not shown to improve.
