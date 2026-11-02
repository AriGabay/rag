---
title: Follow-ups that keep the user's request - right property, right metric, meaning from the whole calculation, citable set counts - Plan
type: fix
date: 2026-10-07
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
---

# Follow-ups that keep the user's request - Plan

## Goal Capsule

- **Objective:** An appraiser can switch property, unit or document mid-conversation, ask for a total after a per-m² figure, or correct the metric, and get the datum they asked for:
  - from the property they named;
  - with the meaning the source gives it, even when that meaning is written in another line of the same calculation;
  - for a count over a set of documents, with a count that rests on a checked, citable listing.
- **Means:**
  - three explicit document sets, with authorized entity resolution (KTD1, KTD2);
  - a structured semantic parse checked for consistency rather than gated by a closed word list (KTD3);
  - request matching against the approved request only (KTD4);
  - meaning bound from the whole calculation, and a repair that fetches evidence (KTD5);
  - citable set listings (KTD6);
  - resolution diagnostics (KTD7);
  - before/after evaluation on the same build (KTD8).
- **Authority hierarchy:**
  1. the user's request in this session;
  2. this plan's Product Contract;
  3. the KTDs;
  4. each unit's Approach.

  The earlier plans of PR #2 govern everything this plan does not change. They are, in order:
  - `docs/plans/2026-10-06-0856-feat-general-question-engine-plan.md`;
  - `docs/plans/2026-10-07-0820-fix-conversational-rag-gaps-plan.md`;
  - `docs/plans/2026-10-07-1249-fix-chat-runtime-meaning-followups-plan.md`.
- **Stop conditions:**
  - a fix would weaken RLS, verification or evidence;
  - real report content would enter git;
  - a settled decision proves infeasible.
- **Execution profile:** `ce-work` in return-to-caller mode inside `/lfg`, on branch `feat/general-question-engine`. Commits and push are allowed, and PR #2 may be updated. No merge, force-push, data deletion or production deploy.

---

## Product Contract

### Summary

A focused round on the conversation logic that sends a correct user request to the wrong context or metric. The UI, ingestion, permissions and fail-closed verification stay as they are. Engine changes stay inside `backend/app/chat/`, plus one diagnostics migration and the evaluation tooling.

### Problem Frame

The round-3 evaluation (build `0a1b119`, whose app code `e150239` matches) left system failures at four stages. Case IDs refer to the private sets in `real_documents/eval/` (gitignored). Their content stays there.

- **Resolve keeps the old property** (R4, C5, D10):
  - `resolve.validate` accepts a subject change while `document_ids` stay those of the previous focus. The "requested" block then points the answering model at the old document.
  - `engine.run_turn` builds `visible` from the conversation's own documents, not from what the user may access. So the resolver cannot even name the new document.
- **Resolve rejects a correct parse** (R6, A4, C3, D6):
  - The resolver parsed a remainder-total phrasing as a change to a total. `justified_fields` rejected it because the quoted words were not in `VOCABULARY`. The unit fell back to per m², and the correct total was then flagged and dropped.
  - A rejected field silently takes the old focus value.
  - The "documents" field is accepted for any content word of three letters or more, which dropped the focus document for a generic word.
- **Request matching** compares the answer with fields the user never set: an old unit, or an unvalidated new-topic metric. A correct answer then gets a false mismatch note (A4, D6).
- **Meaning:**
  - A per-m² rate whose area basis is written in another row of the same calculation table is never bound to it.
  - The spelling "אקווי׳" is not recognized (R1).
  - A correct period that the cited passage does not repeat is blocked, and the repair deletes it instead of citing the line that states it (A1, A15).
- **Set count (D9):**
  - The model called `find_documents`, got the three matching documents, and answered with a count.
  - A document listing has no source ID, so the model cited an ID that was never issued (`S1`), then document UUIDs. Every claim was removed, and the coverage note said "based on 0 of them".
  - So the cause is not "answered without tools": the listing result is not citable.

### Requirements

**Documents and entities**
- R1. A turn distinguishes three document sets: the conversation focus (context only), the documents the user is authorized to access (decided by RLS against the whole office, not by the conversation), and the documents matching the current request.
- R2. When the user changes property, address, unit within a property, or document, no previous document filter survives that could contradict the new request. The new entity is resolved by an authorized lookup before anything is answered.
- R3. The new entity resolves as follows:
  - Not found, or several plausible matches → a short clarification (several matches list their titles).
  - Never a datum from the previous property as a substitute.
  - A unit in the same building that is found in the focus document keeps that document.
- R4. The same rules hold for:
  - units in the same building;
  - properties in different documents;
  - similar addresses in different cities.

**Intent**
- R5. The follow-up parse is structured. It separates:
  - a new question;
  - a continuation on the same datum;
  - a correction of what was understood;
  - a metric change;
  - a change between a per-area datum and a total amount.

  Each claimed change quotes the user's words as evidence.
- R6. A closed vocabulary is never the only gate for a change. A quoted change is accepted when:
  - the quote occurs in the message;
  - it carries content beyond function words, and beyond a bare currency unit;
  - it is consistent with the stated relation and the context.

  Normalization and rules may add accuracy.
- R7. A currency unit alone never sets the metric kind or the requested change.
- R8. Material ambiguity, or a material contradiction between the parse and the user's explicit words, produces a clarification. A parse that fails its evidence check is never silently replaced by the old context value: that field becomes unknown.

**Request matching**
- R9. The answer is compared with the latest approved request, and only on dimensions the user set or that a correction carries over. A correct answer gets no mismatch note. A correct source datum that is not the requested datum is not a valid answer: it keeps the note and the partial status.

**Meaning**
- R10. A definition of area basis or period may come from a heading, title, note or another line of the same calculation as the datum. Never from a row or section that does not belong to the datum. Not all definitions need to be in one sentence.
- R11. Spellings and abbreviations of a qualifier are normalized for matching. The source's own wording is kept for display.
- R12. When the meaning check rejects a correct period or basis because the cited passage does not state it:
  - The repair looks for the evidence that does, and it is cited.
  - Deleting the qualifier alone is not the fix when the datum needs it.
  - With no evidence the qualifier stays unknown, and is never added from general knowledge.

**Set counts**
- R13. A count or list over the repository rests on an authorized scope, a stated match criterion and checked data. Never on conversation history or the model's memory.
- R14. The server verifies that a count claim cites a matching tool result (a listing or a computation). When the listing was read only in part, the count is marked partial.

**Proof**
- R15. Focused tests are added first for the two reproduced states:
  - a subject change that keeps old `document_ids`;
  - a total-request parse rejected by the vocabulary.
- R16. The documented failures run as a regression set, with additional phrasings not used for the fix. Regression sets run against the real model twice on the same build. A new held-out set, never used for development, runs once on the final build after the material failures close.
- R17. The report classifies every failure as one of:
  - model mis-parse;
  - correct parse the server changed or rejected;
  - wrong search or source selection;
  - verification or phrasing failure;
  - reference-answer error.

  It measures cost and time on the same cases before and after, including the resolve and repair calls. Verification is never weakened to improve a measure.
- R18. Real report content stays outside git. The chat stays working in Docker. The UI check covers:
  - switching between properties;
  - a total-sum request;
  - opening the right source.

### Key Decisions

- Three document sets and authorized entity resolution (session-settled: user-directed — chosen over keeping previous `document_ids` on a subject change and over a `visible` set built from the conversation: the old property's data was returned). Governs R1–R4.
- Structured semantic parse with consistency checks; the quote stays as evidence; the vocabulary is not the sole gate; ambiguity → clarification (session-settled: user-directed — chosen over adding remainder or summing phrasings to the vocabulary and over inferring the metric from a currency unit: a correct total parse was rejected). Governs R5–R8.
- Request matching against the latest approved request (session-settled: user-directed — chosen over comparing with a stale metric: a correct answer was flagged). Governs R9.
- Meaning bound from the same calculation, with the original wording kept and an evidence-fetching repair (session-settled: user-directed — chosen over dropping the period as the repair, over adding qualifiers from general knowledge and over requiring one sentence). Governs R10–R12.
- Server-validated set counts (session-settled: user-directed — chosen over counts from history or memory). Governs R13–R14.
- Evaluation discipline (session-settled: user-directed — chosen over scoring on the fix's own phrasings and over weakening verification). Governs R15–R17.

### Acceptance Examples

All with invented names.
- **AE1.** A conversation on "הדרור 5, גבעת השקד" (unit A3), then "טעיתי, התכוונתי לדירה B7 ברחוב הסנונית 12":
  - the request's documents are the הסנונית 12 document, found by lookup;
  - the answer cites only it;
  - no datum of הדרור 5 appears.
- **AE2.** Two documents, "הנרקיס 4, עין ורד" and "הנרקיס 4, כפר גפן". After a question on another property, "ובהנרקיס 4?" gets a clarification listing both titles. "ובהנרקיס 4 בכפר גפן?" resolves to the second.
- **AE3.** In one building document with units A1 and A2, "ומה עם A2?" after A1 keeps that document and answers A2's datum.
- **AE4.** After a per-m² value, a remainder-total follow-up whose words are not in any list, which the resolver parses as `scale: total` with a quote from the message, is accepted. The total from the source is answered without a mismatch note.
- **AE5.** A follow-up whose only "change" is a currency sign ("ובש״ח?") does not change the metric or the scale.
- **AE6.** "ומה המחיר למ״ר שם?" after a percentage datum → the price per m². A correct answer gets no note.
- **AE7.** A rent→value correction after a rent per m² → a value per m². A total value answer keeps the mismatch note and is marked partial.
- **AE8.** A table whose rate row reads "דמ״ש למ״ר | 75" and whose area row reads "סה״כ מ״ר אקווי׳ | 1,240":
  - an answer "75 ₪ למ״ר" is annotated with the basis as written, citing the table;
  - a two-basis table binds nothing.
- **AE9.** The answer writes "70 ₪ למ״ר לחודש" and cites a passage without the period, while another passage of the same document says "70 ₪ למ״ר לחודש":
  - the period stays, with that passage cited;
  - with no such passage anywhere in the document, the period is removed and nothing is added.
- **AE10.** "כמה שומות יש לנו ב-X ועל אילו נכסים?":
  - the answer cites the listing, with its count and titles, and passes verification;
  - with a second unread page, the count is marked partial;
  - a count with no listing or computation cited is removed.

### Success Criteria

- The documented system failures pass in both regression runs: R4, R6, C5, D6, D9, D10, A4, A1, A15, R1. Or the report names the stage that still fails, with evidence.
- The new phrasings of those failures pass.
- The held-out set is reported once, by category.
- Cost per turn and latency are reported before and after on the same cases.

### Scope Boundaries

- No change to ingestion, the UI shell, RLS policies, the judge's policy or the verification thresholds.
- No model switch (gpt-5.4-mini stays). No new model call per turn, beyond the existing resolve call; entity lookup and evidence lookup are database queries.
- No repository-wide pre-extraction, and no fixed-field answering route.
- The "reference narrowness" cases B14 and C9 are reference work only, not engine changes.

---

## Planning Contract

### Key Technical Decisions

**KTD1 — Three document sets, decided by the server** (session-settled: user-directed — chosen over keeping previous `document_ids` on a subject change and over passing only previous-context documents as `visible`: R4/C5/D10 returned the old property's data). Governs R1, R2.
- **Focus** (`inp.focus_documents`, `inp.focus.document_ids`) is context for the parse and the answer, never a filter by itself.
- **Authorized** is decided per id by a database query under the turn's tenant context (`documents` under RLS, not deleted), the same check `api._visible_ids` makes. `resolve` receives a callable, `authorized(ids) -> set[str]`, instead of a set built from the conversation.
- **Matching** is what the entity lookup (KTD2) returns for the current request.
- The validated request's `document_ids` are:
  - the focus documents, when neither the subject nor the documents changed;
  - otherwise the matching set alone — the focus documents only through the matching set (AE3).

  Every id passes `authorized`.
- When the subject or documents changed:
  - `engine._context_message` drops the focus subject, the focus documents and the "keep the same documents" line, plus the previous turn's `P#` references;
  - it lists the resolved documents instead.

**KTD2 — Entity resolution by an authorized lookup** (session-settled: user-directed, the "resolve the new entity through authorized search" part of R2–R4).
- **Placement:** inside the resolve step, after the parse and before the answer. Server-side, through `app.platform.search.documents_matching` under RLS, so it adds no model call.
- **Rejected alternative:** a lookup tool inside the resolve model call, which costs a second model round.
- **When it runs:**
  - when the parse claims a subject or documents change with `scope: entity`;
  - and also when the message carries an identifying token absent from the focus subject and the focus titles, even if the parse claimed no change. A token is identifying if it contains a digit or Latin letters, or is a word that occurs in an authorized title. This way a model that parses "ובהנרקיס 4?" as `same_datum` cannot keep the old property.
- **Set requests:** a parse with `scope: set` (a count, list or comparison over documents; KTD3) never takes the single-entity outcomes. The request drops the focus `document_ids`, and the set is left to `find_documents` / `list_documents`.
- **Terms:** the content words of the subject's or documents' quote (user words; stopwords removed; the existing prefix forms of `documents_matching`), all required.
- **When nothing matches,** one relaxation: only the quote's identifying tokens.
- **Tiers:** the top tier is the documents with the highest count of quote terms found in their title. That count includes one-character numeric tokens, such as a house number, which `_title_words` drops today.
  - When no document matches a term in its title, the tier is every text-matched document.
  - Text hit counts never decide between documents. They only order the titles a clarification lists.
- **Outcomes, applied to the tier:**
  - exactly one document → the scope;
  - several, of which exactly one is a focus document → that one (a unit in the same building, AE3; a generic word that also occurs in the focus document);
  - several otherwise → a server-built clarification listing up to five titles (AE2);
  - none → a server-built "not found in the documents you may see, which property or document did you mean?". The subject becomes unknown, and the request carries no `document_ids`. The previous property is never the fallback (R3, R8).
- **Clarification text:** server clarifications carry only the user's words and authorized titles. The rule that a model-written clarification may hold no digits stays for the model's clarifications.
- **Clarification state:** a server clarification records in the turn's request its candidates (ids and titles) and the quote, with no focus `document_ids`.
  - It records the candidate ids in the answer (`clarification_documents`). `api._answer_documents` includes them, so the visibility gate hides the clarification once any candidate is revoked.
  - `_turn_input` passes the previous server clarification's candidates to the resolve step. A reply with relation `clarification_answer` is resolved only among those candidates, each re-checked with `authorized`. It resolves when exactly one candidate's title contains the reply's terms, or by an ordinal ("השני"). Otherwise the turn asks again. Never the old focus.

**KTD3 — Structured parse, evidence and consistency instead of a vocabulary gate** (session-settled: user-directed — chosen over adding remainder or summing phrasings to `VOCABULARY` and over inferring the metric from a currency unit: R6's correct parse was rejected). Governs R5–R8.
- **Schema.** `ResolvedRequest` gains:
  - `relation`: `new_question` | `same_datum` | `correction` | `metric_change` | `scale_change` | `clarification_answer`;
  - `scope`: `entity` | `set`. A count, list or comparison over documents is `set`;
  - `scale` as a `ChangedField.field` value, with its own quote. Its parsed value is `per_area` | `total` | `unknown`.

  The existing `kind` maps from `relation`, and stored answers keep reading.
- **How an accepted scale is applied:** through `_with_class` to `metric_kind`, and it sets `unit` (per_area → `ILS_per_sqm`, total → `ILS`). `mismatch` keeps reading the class from `metric_kind`. So `scale`, `metric_kind` and `unit` never disagree in the validated request.
- **Evidence rule:** always runs first, for every field:
  - the normalized quote occurs in the normalized message;
  - its tokens are not all stopwords, and not only a currency unit or a number;
  - for subject and documents, the KTD2 lookup decides.

  `VOCABULARY` only short-circuits the consistency check, for a quote that already passed the evidence rule. It is no longer the gate. "ש״ח" and "₪" leave `VOCABULARY["unit"]`, so a bare currency quote is never accepted (R7, AE5). The held-out vocabulary guard (`tests/unit/test_no_topic_vocabulary.py`) already forbids a word like "יתרה" in `backend/app`, so the R6 fix cannot be a list entry.
- **Consistency rules:**
  - A `scale_change` must change the scale relative to the focus.
  - A `metric_change` or `correction` names `metric_kind`, `unit` or `scale`.
  - `same_datum` changes none of them.
  - An explicit area-unit marker (a ל-prefixed area unit, read with `app.measurements` unit parsing: למ״ר, למטר, לדונם, ליחידה) sets `per_area` when the parse leaves the scale unknown.
  - That marker contradicts a `total` parse only when it lies inside the words the parse quotes for the requested scale or metric. That contradiction is material → clarification (R8). A marker elsewhere in the message, such as the previous per-m² figure the total is computed from, leaves an accepted total parse in place.
- **Rejected claims:** a claim that fails the evidence rule sets its field to `unknown`, never to the focus value. It is listed in `rejected`, and the request block shows "לא צוין" for it.
- **Inheritance:** when the user did not set the scale, a correction or metric change of the same subject carries the focus scale into the request block, as guidance for the answering model. Only a correction holds the answer to it (KTD4, AE7).
- **Clarification:** the model's `ambiguity` field still gives the one clarification for a correction.

**KTD4 — Match against the approved request only** (session-settled: user-directed — chosen over comparing with a stale metric: A4 and D6 got false notes). Governs R9.
- `resolve.mismatch` compares only approved dimensions:
  - the base metric, when `metric_kind` was accepted with a quote;
  - the scale, when it was accepted with a quote, or inherited by a correction of the same subject. A metric change compares the scale only when the user quoted it.
- On a `new_question`, nothing is inherited: only quoted dimensions count.
- Unknown on either side → no note.
- The note and the partial status stay for an answer whose datum differs on an approved dimension (AE7).

**KTD5 — Meaning from the same calculation, and a repair that fetches evidence** (session-settled: user-directed — chosen over dropping the period as the repair, over adding qualifiers from general knowledge and over requiring one sentence: A1/A15/R1). Governs R10–R12.
- **(a) Spellings.** The `_BASIS` and `_PERIOD` patterns accept spelling and abbreviation variants of the same key: אקוו׳/אקווי׳/אקוי׳/אקוויוולנטי, and לח׳/לחו׳ already exist. Matching stays per key; the matched text is kept for display.
- **(b) Calculation tables.** In a table source, a per-area amount (its row label or column header carries a per-area unit) inherits the area basis that the table states:
  - in its title, caption or notes;
  - or in an area row, whose label carries a basis and whose amount is in מ״ר.

  It inherits only when exactly one basis key occurs in the whole table. Two bases → nothing is bound (AE8). Text outside the table never binds to its rows.
- **(c) Expansion.** For a cited passage that lies inside a table or a section, the meaning check reads the enclosing table, or the enclosing section up to the existing section clip, from the database once per verification. It uses the same block-range queries as `tool_open_source`.
  - When the expansion is what attests a qualifier, it is registered as a new workspace source `S#`. The annotation and the repair message cite it, so the user can open the line that states it.
- **(d) Evidence-fetching repair.** For a blocking "unsupported period or basis" problem, the server looks for the qualifier, in order:
  1. the turn's other sources;
  2. the expansion of (c);
  3. a document-scoped lexical lookup of the number's written forms in the cited documents' chunks, under RLS, in at most four lookups per verification.

  The number must occur with that qualifier attached, by the same attachment rules. A match from steps 1 and 3 also has to belong to the same datum (R10), and is otherwise not accepted:
  - It lies in the same section or the same calculation table as the cited passage.
  - Or the stored measurement for that number in that passage has the same metric kind as the datum and the subject value role.
  - A comparables, survey or transactions table, whose rows describe other properties, never attests a qualifier for the subject's datum.
  - **Found:** the found `S#` is added to the unit's cited ids before the number, meaning and judge checks of that same verification round. The unit is judged with the evidence it will cite.
    - The problem becomes a non-removing `needs_citation` with the `S#`, and the repair round is told to cite it.
    - If the final answer still lacks the citation, the server appends it after the qualifier.
  - **Not found:** the blocking problem stands, so the qualifier is removed and stays unknown.
- **Missing qualifiers:** the rule for a needed qualifier the answer omits is unchanged (annotation "as written in the source"), now also over the expanded evidence (AE8).

**KTD6 — Set listings are citable sources** (session-settled: user-directed — chosen over counts from history or memory: D9). Governs R13, R14.
- `find_documents` and `list_documents` register their page as a workspace source of kind `listing`, with an `S#` shown at the head of the tool output. Its text holds:
  - the scope query (the criterion);
  - the total ("סה״כ N מסמכים מתאימים");
  - the page line;
  - each title with why it matched.
- **Identity:** a listing source has no document identity of its own.
  - It is excluded from the answer's `documents` (the next turn's focus), from `coverage.cited_documents`, and from the source term of `api._answer_documents`.
  - It carries `listed_document_ids`, the ids on its page. `_answer_documents` includes them, so the visibility gate covers every listed title.
  - The frontend renders a `kind: listing` source as inline text, with no block fetch.
- **Numbers:**
  - The number check finds the count and the titles' numbers in a cited listing.
  - A count of documents (the `_small_ordinal` pattern: a number up to 10 followed by מסמכים, שומות or מקורות) is exempt only when the unit cites a listing or computation that states that count. Otherwise it is a deterministic problem: the server, not only the judge, verifies counts (R14).
  - Listing sources are excluded from `_all_numbers`, the pool used for uncited units.
- **Partial pages:** a listing whose scope has unread pages states it in its own text, and the answer's status becomes partial when it cites it for a set-wide count.
- **Coverage:** a membership answer is one whose cited ids are all listings.
  - Its ledger is complete when every page of the listing's scope was read. The note says the set was located by its criterion and the documents' content was not read, and the status is not capped.
  - It no longer reports "based on 0 of them".
  - An answer that also cites document content keeps the existing set rules. Ledger levels are unchanged.
- **Policy:** the answering policy says that listings are citable for counts and lists, and that a count never comes from history.

**KTD7 — Resolution diagnostics behind the same permission.**
- Migration `0009` adds a nullable `resolution jsonb` to `message_diagnostics`, under its existing owner-or-admin RLS policy. Its downgrade drops the column.
- It stores:
  - the model's raw parse;
  - each field's validation decision (accepted, rejected and why);
  - the entity lookup's terms and outcome.
- The answer keeps only the validated request, as today. `chat_eval` stores `resolution` with each turn, so the report can tell a model mis-parse from a server rejection (R17).
- Every document id in the stored `resolution` (lookup candidates and matches) is added to `message_diagnostics.document_ids`. So the diagnostics endpoint's visibility check covers them.

**KTD8 — Before and after on the same cases** (session-settled: user-directed). Governs R15–R17.
- **Before, existing sets:** the round-3 final runs (`real_documents/eval/final3/`, app code identical to `e150239`) for v3–v6.
- **Before, new regression set:** a new private set, the documented failures with new phrasings, is written before any code change. It runs twice on the running `e150239` stack before the first engine edit.
- **After:** the same sets, twice each, on the final build.
- **Held-out:** a new set is written before any run of this round and run once on the final build.
  - Its documents are new invented synthetic documents in office C, in new invented towns. They are a separate subset that no test fixture and no regression case uses.
  - It covers the same categories as AE1–AE10, with phrasings and documents that the U2–U6 tests and the regression set do not use.
- **Corpus confound:** seeding new documents into office C can change office-wide behaviour (title-word sharing, whole-repository listings). So v5 C and v6 C also run once more on `e150239` after the seed, as their "before".
- **Cost:** reported per purpose (agent, resolve, verify).

### Assumptions

- The KTD2 relaxation and ranking are enough for the evaluated phrasings. An unusual address form that defeats the lookup gives a clarification, never another property's datum. The report counts such clarifications.
- Server-built clarifications may raise the clarification count. The evaluation reports them, by category.
- Expanding tables and sections (KTD5c) keeps the meaning-check cost within the verification allowance. Lookups are bounded and cached per verification.

### Sequencing

U1 runs before any engine edit. Then come U2 and U3, test-first. Then U4, U5 and U6, which are independent of each other. U7 comes before the after-runs, and U8 comes last.

### Risks and Mitigations

- **False clarifications** from the entity lookup on generic words. Mitigation: identifying-token rule; focus preference for same-building units; held-out cases for the common forms.
- **Binding a wrong basis** from a table. Mitigation: the single-basis rule and the same-table boundary; a two-basis test.
- **Listing sources widening the number pool.** Mitigation: listings are excluded from `_all_numbers` (the pool for uncited units); a cited unit is checked against its own cited sources.
- **A document text that mentions the new address as a comparable** can put the focus document in the same text tier as the target. Mitigation: the title tier comes first; the held-out set includes this shape. The report counts any such case.
- **More latency from lookups.** Mitigation: database-only, bounded per turn, measured in U8.

---

## Implementation Units

### U1. Private regression set with new phrasings, held-out set, and the "before" runs

**Goal:** fix the cases and the baseline before anything changes (R16, R17, KTD8).

**Requirements:** R16, R17, R18.

**Dependencies:** none.

**Files:**
- `real_documents/eval/real_v7_regression.yaml` (private, gitignored);
- `real_documents/eval/real_v8_heldout.yaml` (private);
- `backend/scripts/make_eval_docs.py` (a v8 set of invented documents: one building with several units; two similar addresses in two invented towns; a calculation table whose area basis sits in another row; a document set for a count);
- `backend/scripts/seed_eval_office.py` (seeds them into office C);
- `backend/tests/fixtures/eval_v8/*.docx` (generated, synthetic).

**Approach:**
1. Write the regression set:
   - the documented failures (R1, R4, R6, A1, A4, A15, C3, C5, D6, D9, D10);
   - plus at least one new phrasing of each, never shown to the engine during development;
   - office A for real cases (stored only under `real_documents/`), office C for synthetic ones.
2. Write the held-out set before any run, references included, on its own document subset (KTD8). Do not read its results until the final build.
3. Seed the new synthetic documents into office C, synthetic data only.
4. Run the regression set twice, and v5 C and v6 C once each, on the running `e150239` stack. Keep the results under `real_documents/eval/round4/before/`.

**Test scenarios:** the generated documents' ground truth is checked by the existing fixtures test pattern (`tests/unit/test_fixtures_ground_truth.py`) where it applies.

**Verification:** two stored "before" result files, and a seed that leaves office A untouched.

### U2. Three document sets and authorized entity resolution

**Goal:** a property, unit or document change never keeps an old filter, and the new entity is resolved by an authorized lookup (R1–R4, KTD1, KTD2).

**Requirements:** R1, R2, R3, R4, R15.

**Dependencies:** U1.

**Files:**
- `backend/app/chat/resolve.py`
- `backend/app/chat/engine.py` (passes `authorized`; context message per KTD1; early return for server clarifications)
- `backend/app/chat/entities.py` (new: the lookup, the tiers and the outcome; wraps `documents_matching`)
- `backend/app/chat/api.py` (`_turn_input` passes the previous server clarification's candidates; `_answer_documents` reads `clarification_documents`)
- `backend/tests/unit/test_resolve.py`
- `backend/tests/integration/test_chat_entities.py` (new, test database, office B synthetic documents)

**Approach:**
1. Write the failing test first: a subject change with the focus `document_ids` kept is reproduced. It fails at `e150239`.
2. `validate` takes `authorized` and a `lookup(terms) -> Outcome`. On a subject or documents change, the request's documents come only from the outcome.
3. `engine.run_turn` builds `authorized` from the tenant context. When the subject or documents changed, it builds the context message without the old focus subject, documents and `P#`.

**Test scenarios:**
- A subject change that keeps the old `document_ids` → the documents become the lookup's match. This is the reproduced state, written first.
- AE1 (another property), AE2 (similar addresses in two towns: clarification, then resolution with the town), AE3 (same building).
- Nothing found → server clarification, no answer step, and never the focus documents. This holds both with an identifying token and with a quote whose words occur in no title and carry no digit.
- A generic word claimed as a documents change (C3 shape), which also occurs in the focus document → the focus document.
- The parse says `same_datum`, but the message names an address absent from the focus → the lookup runs anyway.
- Tiers:
  - a non-focus document with more text hits than the focus document, for a unit-only quote → the focus (AE3);
  - two AE2 documents with different hit counts → still a clarification.
- Replies to a clarification: a bare town ("בכפר גפן") and an ordinal ("השני") after the AE2 clarification resolve among the candidates. A reply matching neither asks again.
- A clarification whose candidate is revoked is hidden, and is not carried into context.
- `scope: set` asked as a follow-up (AE10 on the second turn) → no clarification; the listing path.
- A document the user cannot access never enters the request, even when the model names it.
- A server clarification may list titles with digits. A model clarification with digits is still not used.

**Verification:** unit and integration tests pass. The reproduced test failed before the change.

### U3. Structured parse with evidence and consistency

**Goal:** a correct parse is accepted on evidence, an inconsistent one asks, and nothing silently reverts to the old context (R5–R8, KTD3).

**Requirements:** R5, R6, R7, R8, R15.

**Dependencies:** U2.

**Files:**
- `backend/app/chat/resolve.py` (schema, policy, `justified_fields` replaced by an evidence-and-consistency validation)
- `backend/tests/unit/test_resolve.py`

**Approach:**
1. Write the failing test first: a total-scale parse quoting words absent from `VOCABULARY` is rejected at `e150239`.
2. Extend the schema and policy.
3. Implement the evidence rule, the consistency rules and the "rejected → unknown" rule.
4. Map `relation` to `kind` for stored answers and the API.

**Test scenarios:**
- AE4: a remainder-total quote outside every list, with `scale: total` → accepted. This is the reproduced state, written first, with invented words.
- AE5: a currency sign as the only change → no change.
- A quote not in the message → the field is unknown, not the focus value, and is listed as rejected.
- An explicit per-m² marker with the scale left unknown → per area. With `scale: total` and the marker inside the quoted words → clarification.
- A total request that mentions the previous per-m² figure → accepted as `total`.
- "ובש״ח?" → no change (AE5), even though the vocabulary once held "ש״ח".
- `same_datum` with claimed changes → the inconsistent changes are not applied.
- The rent→value correction after per m² keeps per m² (the existing test stays green).
- New phrasings for each relation, with new invented property names.

**Verification:** unit tests pass. The reproduced test failed before the change.

### U4. Request matching against the approved request

**Goal:** no false mismatch note; a wrong datum still keeps it (R9, KTD4).

**Requirements:** R9.

**Dependencies:** U3.

**Files:**
- `backend/app/chat/resolve.py` (`mismatch`; the request records which dimensions are approved)
- `backend/tests/unit/test_resolve.py`
- `backend/tests/integration/test_chat_followups.py` (or the existing chat integration test module that scripts turns)

**Test scenarios:**
- AE6, with "מחיר", "שווי" and "ערך" in natural phrasings.
- AE7: a total for a per-m² correction → note and partial.
- An explicit per-m² → total switch answered with a total → no note.
- A new question on another metric → only quoted dimensions are compared.
- An unknown scale on either side → no note.
- A metric change after a total, answered per m², with the scale unquoted → no note.

**Verification:** unit and integration tests pass.

### U5. Meaning from the same calculation, and an evidence-fetching repair

**Goal:** a basis or period stated elsewhere in the calculation is bound and cited; a correct qualifier is cited, not deleted; an unsupported one stays removed (R10–R12, KTD5).

**Requirements:** R10, R11, R12.

**Dependencies:** none, after U1.

**Files:**
- `backend/app/chat/meaning.py` (spellings, table inheritance, expansion hook)
- `backend/app/chat/verify.py` (`needs_citation` problems; the repair message; the server citation in `apply`)
- `backend/app/chat/tools.py` (a block-range read for an enclosing table or section, shared with `tool_open_source`; a document-scoped lexical lookup of a number's forms)
- `backend/app/chat/engine.py` (the repair prompt names the `S#` to cite)
- `backend/tests/unit/test_meaning.py`, `backend/tests/unit/test_verify_units.py`, `backend/tests/integration/test_chat_meaning.py` (new or extended)

**Test scenarios:**
- AE8 one-basis table → annotated, with the citation. Two-basis table → nothing bound. A basis in text outside the table never binds.
- "אקווי׳", "אקוי׳" and "אקוו׳" are one key, each displayed as written.
- AE9: a period in another passage of the same calculation → the unit is judged with that passage, and the final answer cites it. No passage → the period is removed, and the rest of the unit stays when it is supported.
- The same number with a period appears only in a comparables table → the period is removed.
- A section heading that states a period for the calculation binds it. A heading of another section does not.
- The lookup budget is never exceeded, and verification still fails closed when the database read fails.

**Verification:** unit and integration tests pass. The existing meaning tests stay green.

### U6. Citable set listings and count validation

**Goal:** a set count rests on a cited listing or computation, and partial listings make partial counts (R13, R14, KTD6).

**Requirements:** R13, R14.

**Dependencies:** none, after U1.

**Files:**
- `backend/app/chat/tools.py` (listing sources)
- `backend/app/chat/verify.py` (a listing is a source for the number check and the judge)
- `backend/app/chat/coverage.py` (the membership note; the partial status for a set-wide count from a partial listing)
- `backend/app/chat/engine.py` (policy lines)
- `backend/app/chat/api.py` (`_answer_documents` reads `listed_document_ids`; listings excluded from `documents`)
- `frontend/components/chat/Message.tsx`, `frontend/components/chat/SourcePanel.tsx`, `frontend/lib/chatTypes.ts` (a listing renders inline, with no block fetch)
- `backend/tests/unit/test_absence.py` or a new `backend/tests/integration/test_chat_listing.py`; `frontend/e2e/chat-ledger.spec.ts` (a listing citation opens inline)

**Test scenarios:**
- AE10, all three branches.
- A listing cited for a datum it does not state fails the number check.
- An uncited "3 שומות", or one citing a listing with another count → a deterministic problem.
- The listing's titles and count support a list of property names with house numbers.
- The coverage note for a membership answer says the content was not read. A fully read listing gives a complete ledger. An answer that also cites content keeps the set rules.
- A listing never becomes the next turn's focus document, and revoking a listed document hides the answer.

**Verification:** tests pass. A scripted D9-shape turn ends with the count and titles cited.

### U7. Resolution diagnostics and evaluation fields

**Goal:** each failure can be classified by stage (R17, KTD7).

**Requirements:** R17.

**Dependencies:** U2, U3.

**Files:**
- `backend/alembic/versions/0009_diagnostics_resolution.py`
- `backend/app/chat/api.py` (`_diagnostics` stores `resolution`)
- `backend/app/chat/resolve.py`, `backend/app/chat/engine.py` (carry the raw parse and the decisions)
- `backend/eval/chat_eval.py` (stores `resolution`; the report lists, for each failed turn, the parse, the server decision and the request)
- `backend/tests/integration/test_migration_0009.py`, `backend/tests/unit/test_chat_eval_scoring.py`

**Test scenarios:**
- Upgrade and downgrade keep row security forced.
- A non-owner, non-admin gets no resolution detail.
- The message API never returns the raw parse.
- The eval report shows the server's decision for a rejected field.
- After access to a lookup candidate is revoked, the diagnostics endpoint returns 404 for that message.

**Verification:** tests pass. The diagnostics endpoint returns `resolution` to the owner.

### U8. Evaluation, UI check and report

**Goal:** prove the fix against the real model, and report it honestly (R16–R18).

**Requirements:** R16, R17, R18.

**Dependencies:** U1–U7.

**Files:**
- `docs/evaluation/conversational-rag.md` (a round-4 section with aggregates only)
- `real_documents/eval/round4-analysis.md` (private)
- `docs/solutions/workflow-issues/real-model-before-after-eval-on-the-local-stack.md` (only when a new learning arises)

**Approach:**
1. Rebuild the backend and worker in Docker. Record the commit and the image.
2. Run v3, v4, v5 (A and C), v6 (A and C) and the round-4 regression set twice each on that build.
3. Then run the held-out set once.
4. Classify every failure into the five categories with the resolution diagnostics. Correct reference errors only through `reference_corrected`, with evidence.
5. Report cost and time per purpose against `final3` and U1's "before" runs.
6. Check through the UI, in the environment left running:
   - a property switch;
   - a total-sum request;
   - opening the cited source.

**Verification:** the report and the UI check. No real content in git.

---

## Verification Contract

- **Unit and integration tests:** `cd backend && .venv/bin/python -m pytest -q -p no:warnings`, run alone.
- **Lint and type checks:** ruff; `cd frontend && npm run typecheck && npm run lint`.
- **Browser:** Playwright `frontend/e2e/chat-ledger.spec.ts` and `chat.spec.ts`, when the frontend changes.
- **Real-model checks** in Docker: as U8 lists them.

## Definition of Done

- U1–U8 are done, and AE1–AE10 are proven by tests.
- The held-out set covers the same categories, with phrasings and documents not used in the U2–U6 tests or the regression set.
- The report separates the five failure categories and gives cost and time before and after.
- The chat answers through the UI in Docker.
- No real data is in git.
- The PR is updated, not merged.
