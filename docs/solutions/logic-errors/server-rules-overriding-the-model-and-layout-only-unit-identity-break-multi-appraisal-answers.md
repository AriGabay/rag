---
title: "Server rules that override the model, and layout-only unit identity, break answers about multi-appraisal files"
date: 2026-10-18
category: logic-errors
module: backend chat understanding, appraisal units and verification (app/chat/intent.py, entities.py, resolve.py, engine.py, tools.py, verify.py, coverage.py, contexts.py, units.py, cards.py; app/platform/search.py, pipeline.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "A purely professional question is answered as a question about a property, because one file title contains the professional phrase"
  - "The server answers with a clarification listing unrelated appraisals in about two seconds, with no model call and no search"
  - "The model asks a needless clarification even after it has read the right pages, because the server pinned another unit of the same file"
  - "Verification removes the correct final value as 'another property's value' because a sibling unit shares a non-distinguishing name"
  - "Most units of a long compendium have no title; two reports sit in one unit"
  - "A unit-scoped search returns passages from the whole file and the turn runs out of tool budget"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [intent, candidates, unit-identity, identity-cards, unit-derivation, verification, take-value, two-track-diagnosis, rag, chat]
---

# Server rules that override the model, and layout-only unit identity, break answers about multi-appraisal files

## Problem
Round 15 (PR AriGabay/rag#2) started from the report that the system still fails at its basic job: understand the question, find and read the right appraisal, and answer from it. Passing suites and good answers on some questions did not show that.

A private diagnostic set was built from real reported failures plus new cases: factual, professional, tables, comparisons, calculations, follow-ups and multi-appraisal files. It had 20 conversations and 29 turns. Every reference answer was written from the source blocks, not from an earlier system answer, and was graded by meaning (`full / partial / wrong / no_answer`, plus required items correct). Each turn was traced stage by stage: understanding, unit identification, evidence located, read and cited, removals, limits and cost. The base build answered 11 of 29 turns fully, 6 had no answer, and 53 of 82 required items were correct.

## Diagnosis method that mattered: two tracks
The same turns ran twice through the same engine in a second backend instance:
1. **Normal track:** the system as is.
2. **Gold track:** the correct source pages were injected as server-executed `read` calls before the first agent step, so they reached the model as ordinary sources.

The gold track reached 18 of 29 full answers and 0 wrong. Retrieval and reading therefore explained part of the gap. Six turns still failed with the right sources in hand, so those failures lay in understanding, early server decisions and verification. Without this split, every failure looks like a search problem, and the fix becomes "search more", which does not help those six turns.

Keep this harness and its sets out of git. They hold customer text.

## Root causes
1. **Server-side understanding overrode the model.**
   - A vocabulary and regex "evidence" layer could override the request analysis's intent. Its inputs included complete names taken from visible titles, title-word plus number addresses, and fuzzy alias matches. A professional question became a property question because a file title contained the phrase. A fuzzy match of one word against an alias produced a server clarification among unrelated units.
   - On a first message the server pinned a unit and locked searches to it. A wrong pin (another project in the same compendium) led to a needless clarification even on the gold track.
2. **Unit identity and boundaries were layout-only.** Every appraisal in a compendium opens with a subject line (`הנדון:`, sometimes inside a table) and a letter opening (a date and `לכבוד`). The derivation read neither:
   - most units had no title;
   - two reports were merged into one unit;
   - one unit held only appendices whose report body sat in the previous unit.

   This one cause fed both causes 1 and 3.
3. **Verification removed grounded content.**
   - `_wrong_context` matched the claim's naming against a merged sibling unit whose aliases shared a non-distinguishing name. The right unit had no name, so its correct value was removed.
   - Server-forced coverage wording ("the latest datum in the sources checked") was then judged unsupported.
   - Floor numbers in apartment table rows were treated as unstated.
   - The present-value term "מקדם הערך הנוכחי" was read as a claim that a value is the latest.
4. **Tool friction.**
   - `take_value` refused table-row quotes (` | `), whole rendered rows and per-area money labels, and read a column header such as "value per built m²" as the unit m².
   - It accounted for 201 of 365 tool calls, and only 91 of them registered a value. Turns ran into the step limit.
5. **Retrieval scope.** A search scoped to a unit returned passages from the whole file (10 to 18 thousand characters per search), and the tool budget ran out on answers that were otherwise right.

## What fixed it
- **Remove the override, do not add another rule.**
  - `intent.settle` no longer lets evidence overrule the request analysis's claim.
  - The model-path first message no longer pins a unit or locks searches.
  - Server clarifications were removed from model turns. The search-only path keeps its old behaviour on purpose.
- **Give the model candidates as data, not as control.**
  - A candidates block of at most `MAX_CARDS` (8, in `entities.py`) ranked unit cards states the basis each card is ranked on.
  - Cards come from **identity cards** read at ingestion by one model call per unit (`cards.py`, table `unit_cards` with the same row security as `document_units`). Only `subject` fields whose quote is verbatim in the unit's opening are kept.
  - The model declares the unit its answer is about in `focus.units`. `engine._declare_units` validates the declaration: the handle must be a unit the server offered, listed (`find_documents` or `outline`) or one the turn read, and the user must still be able to see it. Anything else is dropped.
- **Name and bound every unit from its own text.**
  - `units.py` cuts at a subject line and a letter opening. Both together give a certain cut, either alone a probable one, and a second opening within three pages does not cut.
  - The result is a new derivation (`UNITS_VERSION = "units-v5"`) stored **beside** the old rows. The `document_units` primary key includes `derivation_key` (migration 0023) and unit ids carry a tag (`…@v5`). An old anchor resolves by name, or by the new unit that contains its block range.
  - Nothing is deleted.
- **Verification reads what the server holds.**
  - Only distinguishing names count (`Contexts.named`). `_wrong_context` checks distinctive naming in the question, the claim and the components first, and the declared unit only after that.
  - The numbers on the source line a cited V# was taken from count as stated (`_value_origin`). Computation binding does not use this.
  - The present-value exemption applies only after a discounting term (`_is_present_value`).
- **Tools accept the forms the model actually produces.**
  - `take_value` resolves a row quote or whole row text to its cell, matches labels loosely, and reads a cell's unit from its header first.
  - A money-per-area row label means ₪ per m².
  - A value whose unit is ambiguous is flagged and refused by `calculate` and by derived computations alike.
- **Scoped search stays scoped.** Hits are clipped to the unit and capped at `SCOPED_PASSAGE_CHARS` (900). There is no silent widening, and a reach across a probable edge is labelled.

Measured on the diagnostic set (now a regression set), graded by meaning:

| Build | Full | No answer | Required items correct | Model calls | Wall time | Cost |
|---|---|---|---|---|---|---|
| Base | 11 | 6 | 53 of 82 | 281 | 1,898 s | $0.232 |
| After fixes | 15–17 | 1–3 | 60–65 of 82 | 260 | 1,753 s | $0.220 |

Run-to-run variance is about ±3 required items. The held-out set result is in `docs/evaluation/conversational-rag.md`, round 15.

## Traps found after the fixes (code review of the round, each then fixed)
- **A handle without its derivation tag.** A `U#` handle's data had no `units_tag`, while sources carry tagged unit ids (`@v5`). Coverage could therefore never match a declared or kept unit to the passages read in it. The fix stores the tag with the handle.
- **Only the previous derivation stored.** A version holding only units of the previous derivation was invisible to candidate ranking, because raw readers filter by the current key and the current rows are derived lazily. `entities.ensure_current_units` now derives them first. This is additive.
- **Too broad an exemption.** The first present-value exemption also covered "the current value of the property", which loosened a recency check.
- **Listed units could not be declared.** The prompt sends the model to `find_documents` for a unit outside the cards, but units listed there were not declarable, and the server dropped the declaration silently. Fixed with `Workspace.listed_units`.

## Trap found only by the held-out run on the final build
- **"Distinguishing" was measured against identities, not against text.** One unit's identity card held a brand name that several reports in the compendium share. The unit the value actually came from had no identity of its own, though its text wrote that brand throughout. Because no other *identity* held the name, `Contexts._distinguishing` treated it as distinguishing.
- **The effect:** the question and the claim "named" the other unit, and verification removed correct values as another property's figure. This happened in five of 22 held-out turns, and the whole answer was lost in some of them.
- **The fix:** `_wrong_context` now drops from the asked set a unit named only by a name (not by an address or parcel) when the passages the turn read inside the evidence's own unit write that same name (`Contexts.naming_names`). When nothing distinctive remains, the declared unit and then the located unit decide.
- **The lesson:** a name separates two units only if the other unit's text does not write it. An empty identity is "unknown", not "different".

## Prevention
- **Diagnose with two tracks before fixing.** Run the normal track against a gold-sources track on the same engine. A turn that fails both ways is not a retrieval problem.
- **When a server rule overrides the model, remove it before adding another rule.** A growing list of phrasings or keywords is the failure mode, not the fix.
- **Make every new derivation of stored data additive.** Every raw reader must filter by the current derivation key **and** make sure that derivation exists for the versions it reads. Grep every reader of the table.
- **Validate a model's declaration and never trust it blindly.** It never outranks distinctive naming in the text, and naming is distinctive only when the evidence's own unit does not write the same name.
- **Grade by meaning and attribution against references written from the source,** not by the presence of numbers or phrases. Keep the held-out set frozen and run it once on the final build.

## Related Issues
- `docs/solutions/logic-errors/hebrew-follow-up-entity-lookup-pitfalls.md` (rounds 4 and 10) is **partly superseded**. It says every lookup is gated behind a server-validated intent with hard evidence, and that a first message pins a unit provisionally. Round 15 removed both overrides for model turns. Its regression traps still apply to the candidate ranking (a word that titles share is no evidence; a professional question stays professional). The doc needs a refresh (`ce-compound-refresh`).
- `docs/solutions/logic-errors/datum-context-is-lost-between-extraction-cache-and-display.md` (round 14): its "not read is not 'not stated'" lesson recurs in the identity cards. An `empty` card is final until `CARD_VERSION` changes.
- `docs/solutions/logic-errors/datum-identity-needs-a-source-grounded-component-decided-pairwise.md` (round 13): server-held facts must reach the judge. Here the fact is the declared unit, shown to the judge as a header, never as number evidence.
