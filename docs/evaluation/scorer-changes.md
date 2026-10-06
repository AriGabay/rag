# Scorer and answer-key changes (held-out general sets)

This file lists every change to the scorer (`backend/eval/general.py`), the harness (`backend/scripts/eval.py`,
`backend/eval/truth.py`), the answer key of the first held-out set (`backend/eval/questions_general.yaml`) and the
gate 8 known gaps (`backend/tests/acceptance/test_gate8_generality.py`). It was made after the round-6
failure analysis (`docs/evaluation/failure-analysis.md`, section A).

The rule for every change: a check may be relaxed only where the system's behavior is correct under the
Product Contract (`docs/plans/2026-10-06-0856-feat-general-question-engine-plan.md`, R1–R29, KTD1–KTD16). A wrong
answer, an opposite polarity, a wrong document, a partial computation presented as complete, or another
attribute's value must still fail. Every rule has a positive case and a negative control in
`backend/tests/unit/test_eval_scorer.py`. Each negative control must fail.

## Sets

| set | file | answer key | label |
|---|---|---|---|
| `--set general` | `backend/eval/questions_general.yaml` (61 items; 54 held-out items run with cloud on) | `tests/fixtures/ground_truth.yaml` → `general_facts` (H1–H8, H4v2) | **regression** |
| `--set holdout_v2` | `backend/eval/questions_holdout_v2.yaml` (66 items, fixed before any run) | `tests/fixtures/holdout_v2_truth.yaml` (K1–K8, K3v2) | **held-out v2** |

The two keys are loaded side by side. Their fact, document and attribute ids never collide. No content of
`questions_holdout_v2.yaml` was changed: no harness-format bug was found in it. Every v2 turn loads and is
scored (`test_every_turn_of_both_sets_scores_and_an_empty_answer_fails`).

## Scorer rules

### Too lenient (tightened)

| # | rule | before | after | items affected (round 6) | requirement | why no wrong answer passes |
|---|---|---|---|---|---|---|
| L1 | boolean / `none` fact values | `fact_value_stated` returned True for `bool`, `None` and `"none"`; citing the document was enough, even for the opposite claim | the claim (or the snippet, see S1) must state the right polarity. One of three things holds: (1) the text contains the document's own sentence; (2) the attribute's words (label + the item's terms), taken as phrases, are negated for an absence and not all negated for a presence; (3) when the attribute is not named, the whole text is checked for negation. The negation check is general Hebrew (אין/אינו/אינה/אינם/אינן/לא/ללא/בלי/היעדר/טרם, with ו/ש prefixes, plus a table cell `<word>: אין`). A numeric 0 is also stated by such an absence. | GQ11, GQ59 still pass; GQ14 is now scored by polarity (see S1); v2: HV07, HV22, HV55, HV56, HV62, HV64 | R22, R23 ("with the same meaning") | an opposite claim ("בבניין יש מעלית" for no elevator; "לא רשומה זיקת הנאה" for a registered one; "שופצה בשנת 2021" for "no changes") fails |
| L2 | coverage by document | `found == values_found` and `not_stated_includes` checked only as a total | with the answer's `audit` block, every document is checked by its state. Documents expected as values must be `used` / `duplicate` / `filtered_out` / `conflict`, with the answer key's value. `not_stated_includes` documents must be `not_stated` / `not_yet_extracted` / `partial_scan`. `coverage.awaiting_review` documents (S9) must be `awaiting_review`. `stated_absent`, `mentioned_without_value`, superseded versions and every document the key does not expect may never be an observation. Without an audit block the count checks remain, plus a count of documents awaiting review. | every computation turn; GQ28 / GQ35 now fail in round 6 on `found` 3 (D7's whole-property area counted as found) | R14, R15, KTD8 steps 6–7 | a junk value from another attribute (D7 90 m²), a document that states nothing, a wrong value for a right document, or a superseded version (H4, K3, D1) fails |
| L3 | `changed` of a version diff | never read | every changed attribute the key names (from the key's `versions[].changed`) must be stated for both versions. The old value goes in a claim citing the old version and the new value in a claim citing the new version, at the changed page. | GQ22.2 now also fails on `changed` (the 6% → 10% adjustment rate was omitted); HV27.2 | R11, R22 | a missing assumption, a wrong old value, or a value cited to the other version fails |
| L4 | descriptive values (`"approved (May 2023)"`, `"2022: kitchen"`) | reduced to "every digit they contain" | the value's own numbers (whole tokens or Hebrew number words; never the 2 of "m2"), its month names (English → Hebrew), a non-negated content word of the document's sentence (a negation for a negative value such as "deposited, not yet approved") | GQ04, GQ22.1, GQ58 (still pass); GQ18, GQ22.1 now pass (S2/S3) | R22, R23 | "הופקדה בשנת 2023", "הופקדה במאי 2023" and "טרם אושרה במאי 2023" all fail for "approved (May 2023)" |
| L5 | page of a page-less source | a source with an empty `page_list` matched every page | it matches only a document the answer key knows has no pages (DOCX: H7, D11, K6) | none in round 6 | R22 (documents and pages) | a page-less chunk of a PDF cannot stand in for the required page |
| L6 | conflict | every returned source and any number anywhere in the text | each conflicting statement must be stated in a claim citing its own document at its page, or listed as that document's value by the computation (a value source or the audit) | GQ26, GQ27, GQ33; v2 HV29, HV30 | R22 (conflicts), KTD8 step 6 | a value that appears only in an uncited source or only in the answer text, with no cited claim, fails |
| L7 | combined answers | only the figure and coverage were scored | the content part may not state a value (or every number of a quote whose values the key adds up) for a document that the audit reports as `not_stated` / `partial_scan`. Without an audit, this applies to a document that is not a value while nothing is pending or awaiting review. | GQ49.1 now fails ("12 and 6 מ״ר" for H4v2 while coverage says H4v2 does not state it) | R14, R22 | it adds a failure, so it cannot pass anything |
| #10 | number and Hebrew value matching | regex substrings: "12" in "120"; the "2" of "m2"; "השנייה" for 2; "מגורים א" inside "מגורים אחרים" | numbers are whole tokens. Hebrew number words 1–20 are accepted, in both genders, in construct forms, and with ו/ב/ל/כ/מ/ש prefixes; 11–19 are read as two words; an article (השני / השנייה) means an ordinal and is never a number. A number must not sit next to a word that names only another attribute: the nearest attribute word in its sentence decides. Hebrew values must match as whole tokens. | all value checks | R22, R23 | "ממ״ד 9.5 מ״ר ומחסן 12 מ״ר" no longer states a 12 m² safe room, and "מגורים אחרים" no longer matches "מגורים א׳" |

### Too strict (relaxed only where the behavior is correct)

| # | rule | before | after | items (round 6) | requirement | why no wrong answer passes |
|---|---|---|---|---|---|---|
| S1 | values of a locate answer | a value needed a *claim*; template lists have none, so GQ14 failed in every round | when the item's task is `locate` and the answer has no claims and dropped none (it is not a verification fallback), the value is checked in the snippet of a cited source of the fact's document, at an accepted page, with the same matching (polarity included) | GQ14 → pass | R1 (locate task), R22 (documents and pages are the answer) | the snippet must state the value with the right polarity, from the right document and page; a snippet that says the building has an elevator, or a snippet from H6, fails; a fallback answer (dropped claims) is not accepted |
| S2 | unit exponent | "60 m2 main area" required {60, 2} | numbers glued to a Latin letter are not numbers of the value | GQ18 → pass | header ("wording is never scored"), R22 | 61 (or any other number) still fails, and a word of the document's sentence is still required (L4) |
| S3 | Hebrew number words for descriptive values | only for `int` values, 1–5 | for any number of any value, 1–20 (see #10) | GQ22.1 → pass ("קומה אחת") | header promise; R22 | "שתי קומות" for "1 additional floor" fails; teens and ordinals are not misread |
| S4 | task type of a tool-raised clarification | exact `clarify` | `compute` / `compute_explain` is also accepted when **all** of these hold: the item expects a clarification about key K; the answer is a clarification about K with no figure; a computation tool (`compute_records` / `extract_and_compute`) was planned or executed | GQ51.1, GQ52.1 → pass | R20 (only result-changing clarifications), KTD2 (the rules fast path), plan.py `MODEL_CLARIFY_KEYS` | the outcome facet still requires the exact key and no figure; another key, a non-compute tool, or an `answer` plan fails |
| S5 | `new_question` vs `topic_change` | exact label | the two are equivalent **only** when the turn's `cleared` or `kept` expectations exist; those expectations are checked on the stored state anyway | GQ55.2 → pass (with S6); GQ49.2 still fails (`follow_up`, Givatayim kept) | R18 (what is cleared, not the label); `apply_turn` treats both the same | a turn without a state expectation still needs the exact label; `follow_up` never passes; a state that was not cleared fails the `state` facet |
| S6 | precision of a header-page citation | H3 p1 (the report header naming the property) was outside the key | **answer-key change**: GQ55.2 `also_valid: [{doc: H3, page: 1}]` | GQ55.2 → pass | R22 | only the same document's header page was added; another document still fails precision (negative control), and the value must still be stated citing H3 p2 |
| S7 | page of a value | bound to the fact's page only | the fact's page or any page the key accepts for that document (`all_of` / `also_valid`) | GQ57 values → pass (the item still fails on `sources`: H5 p1 not cited) | R22; the key itself lists H5 p2 | the pages come from the answer key for the same document; another document fails |
| S8 | `found` semantics | `found` counted any version holding a fact of any status or role | `found` is compared with the settled `values_found` (with the engine's audit, `found` counts usable subject values; per-document states are checked through the audit, L2) | GQ28, GQ35, GQ34 | R14, R15 ("values found" and "values awaiting review" are separate) | a junk or review-only value no longer counts as found, so the check is stricter |
| S9 | answer-key values that contradict settled decisions | see "Answer-key changes" | a `settled` block in the turn asserts the per-document state | GQ28, GQ29, GQ34, GQ35, GQ36, GQ49.1 | KTD8 steps 4–5, KTD9, R15 | the key now requires the specific state (H3 / H4v2 / H2 *awaiting review*: not used, not "not stated"; H5 *not stated*), which is stricter than "n=3"; the old figures (10.67 etc.) now fail |
| S10 | meta turns ("תראה לי את המקור") | scored on the full expected retrieval of the previous turn | scored on re-showing exactly the previous turn's cited documents and pages (`TurnRun.previous`; a stored run without it gets the previous stored turn on `--rescore`) | GQ50.2 → pass (GQ50.1 still fails on retrieval) | R19 | the retrieval is scored on the turn that retrieved; a meta turn that adds or drops a source fails |

### Harness

| change | before | after | why |
|---|---|---|---|
| sets | `--set general` only | `--set general` is labeled the **regression** set in summaries and reports; `--set holdout_v2` loads `questions_holdout_v2.yaml` with `holdout_v2_truth.yaml`; `--questions-holdout-v2`; the report defaults to `docs/evaluation/real-model-sample-holdout-v2.md` for v2 | user instruction: 54 v1 held-out items become the regression set, and v2 is the new held-out measurement |
| v2 seeding guard | — | `--set holdout_v2` (without `--rescore`) refuses before asking anything when the K* documents are not in office A, and points to `scripts/seed_demo.py` | the live stack had no K* documents at the time of writing, and asking without them would score retrieval failures |
| users | dana: G1, G3 | dana: G1, G3, G4 (`truth.USERS`); `GROUP_NAMES` maps "ידע כללי ב" → G4 | the v2 group is visible to dana (`holdout_v2_truth.yaml` → `group.visible_to`) |
| superseded documents | hard-coded `"H4"` | from every key's `versions[].replaces` and every `version_of` (H4, K3, D1) | v2 has K3 → K3v2 |
| number words | 1–5 | 1–20, both genders, construct forms (#10) | v2 writes values in words (עשר, שישה) |
| gate 8 `KNOWN_GAPS` | an entry xfailed every facet of the item | each entry names its facets (`"result"`, or `"2.relation"` for one turn). Every other failing facet fails the test. A named facet that passes fails the test ("remove it"). | user instruction (#35): an xfail must not mask an unrelated regression |

## Answer-key changes (`backend/eval/questions_general.yaml`)

`result` / `values` / `coverage` outside `settled` still record what the documents state. That part is
recomputed by `tests/unit/test_fixtures_ground_truth.py`, so it is unchanged. The scorer applies `settled` on
top of it. `test_settled_block_is_derived_from_the_document_facts` recomputes every settled figure from the
fact values.

| item | old expected (scored before) | new expected (`settled`) | basis |
|---|---|---|---|
| GQ28 (AE1) | mean 10.67, n=3 (H1 12, H2 9.5, H3 10.5); values H1, H2, H3; found 3 | mean **10.75, n=2** (H1, H2); values H1, H2; found 2; **H3 awaiting review** | KTD8 step 5 (a conversion with assumptions is `needs_review`), KTD9, R15. H3 gives only inner dimensions in cm (300 על 350 ס״מ). |
| GQ35 | count 2 of 3 below 11 m² (H2, H3); found 3 | count **1 of 2** (H2); found 2; **H3 awaiting review** | as GQ28 |
| GQ29 | mean 12.50, n=2 (H4v2 18 = 12 + 6, H5 7); found 2 | mean **7.00, n=1** (H5); found 1; **H4v2 awaiting review** | KTD8 step 5 (several values for the subject in one version) and step 4 (the value is parsed from the quote; the server never adds values) |
| GQ49.1 (AE4) | values n=2 (H4v2 18, H5 7); found 2 | values **n=1** (H5); found 1; **H4v2 awaiting review** | as GQ29 |
| GQ34 | count 2 of 4 built after 2010 (H1 2017, H2 2015); found 4 | count **1 of 3** (H1; H3 2004 and H8 1972 observed); found 3; **H2 awaiting review** | KTD8 step 5, R15: H2 states only the year of occupancy (ואוכלס בשנת 2015). Reading it as the build year is an assumption. |
| GQ36 | mean 1.50, n=2 (H4v2 2, H5 1); found 2 | mean **2.00, n=1** (H4v2); found 1; **H5 not stated** | KTD8 step 4: H5 names its one balcony only by a singular noun (מרפסת חזית בשטח 7 מ״ר), with no number or number word to parse. |
| GQ55.2 | `sources.all_of: [H3 p2]` | `also_valid: [H3 p1]` added (S6) | R22: the same report's header page names the appraised property |

Not changed, but flagged: **GQ33** still counts H2's occupancy year (n=8) and H6 and H7 as two observations of
the same unit (KTD8 step 6 deduplicates them by entity key). The same settled decisions apply there, but it was
not on the list of items to change. It stays a scoped gate 8 gap (`result`, `coverage`). **GQ57** still
requires H5 p1 in `sources.all_of`. The round-6 regression that returns only p2 is a locate defect (failure
analysis D.5), not a scorer one.

## Effect on the stored round-6 results (offline rescore, no model call)

`general_real_r6.json` (commit `70f35bc`, `gpt-5.4-mini`) was scored twice with the same live document mapping:
once with the old scorer and old key, once with the new ones.

|  | items passed | held-out (regression) items | turns |
|---|---|---|---|
| before | 35/60 | 31/54 | 41/69 |
| after | 41/60 | 36/54 | 49/69 |

| turn | before | after | why | could a wrong answer pass this way? |
|---|---|---|---|---|
| GQ14.1 | fail: value False not stated in a claim | **pass** | S1: snippet "נבנה בשנת 1972 ואינו כולל מעלית" cited at H8 p1, polarity negative | no: an opposite or other-document snippet fails (tests) |
| GQ18.1 | fail: "2" of "m2" demanded | **pass** | S2: "כ-60 מ״ר שטח עיקרי שטרם מומשה" citing H2 p1 | no: another number fails; a sentence word is required |
| GQ22.1 | fail: "1" of "1 additional floor" | **pass** | S3: "קומה אחת"; F09 checked by L4 ("אושרה … במאי 2023") | no: "שתי קומות" or "הופקדה במאי 2023" fail |
| GQ22.2 | fail: task type | fail: task type **+ changed** (6% → 10% not stated) | L3 (stricter) | n/a |
| GQ28.1 | fail: n=2, 10.75 (expected n=3, 10.67) | fail: found 3 (expected 2) | S9 key + S8: the figure is now right; the coverage counted D7's junk value as found | n/a (still fails) |
| GQ29.1 | fail: n, value, H4v2 not cited, found | fail: 0 awaiting review (expected H4v2) | S9: H4v2 was rejected as "not stated" | n/a |
| GQ34.1 | fail: n=2, value 1 (expected n=4, 2) | fail: n=2 (expected 3), found 4 (expected 3) | S9: H3 (an unambiguous "הושלם בשנת 2004") went to review | n/a |
| GQ35.1 | fail: n, value | fail: found 3 (expected 2) | as GQ28 | n/a |
| GQ36.1 | fail: n=1, 2.00 (expected n=2, 1.50) | **pass** | S9 key: H5 does not state a count (KTD8 step 4); the answer showed 2.00, n=1 and "2 documents do not state it" | no: this is a settled-decision key change, not a looser rule; 1.50 now fails |
| GQ49.1 | fail: n, H4v2 not cited, found | fail: 0 awaiting review; **content contradicts coverage** | S9 + L7 | n/a |
| GQ50.2 | fail: inherited retrieval of turn 1 | **pass** | S10: re-showed turn 1's source (D1v2 p1) exactly | no: an added or dropped source fails; turn 1 (and the item) still fail |
| GQ51.1 | fail: task type compute | **pass** (item passes) | S4: the `compute_records` plan raised the `data_kind` clarification | no: another key, a figure, or a non-compute plan fails |
| GQ52.1 | fail: task type compute | **pass** (item passes) | S4 | as GQ51 |
| GQ55.2 | fail: relation new_question; precision H3 p1 | **pass** | S5 (the `cleared: [attribute]` check passed) + S6 key | no: without the state check, or with `follow_up`, it fails; another document fails precision |
| GQ57.1 | fail: sources (H5 p1) and values | fail: sources only | S7 | n/a |

No turn went from pass to fail. Every turn that passed before (GQ11, GQ59, GQ26, GQ27 included) still passes
under L1, L4 and L6. No item flipped to pass through a rule that would also pass a wrong answer. The flips
come from S1, S2, S3, S4, S5, S10 and from the S6/S9 key changes. For each of those, a wrong value, the
opposite polarity, a wrong document, a wrong key or a wrong relation still fails, and the negative controls in
`test_eval_scorer.py` check this. Items now passing: GQ14, GQ18, GQ36, GQ51, GQ52, GQ55.

`summary.task_type` remains a raw label-match metric (60/65): it counts GQ51.1 and GQ52.1 as mismatches by
design. The S4 rule applies only to the scored facet.

## Gate 8 (scripted provider, `rag_test_b`)

`54 passed, 6 failed, 4 xfailed`. The four xfails are the scoped gaps: GQ33 and GQ40 (`result`, `coverage`),
GQ39 (`outcome`, `result`, `coverage`) and GQ37 (`task_type`, `outcome`, `abstention_kind`). The six failures
come from the engine's new completeness rule, developed in parallel (`facts.completeness`). That rule counts
a version with unknown filter metadata (one in every city-scoped set of the gate world) as a gap, and it
abstains when the gaps are at least the observations:

| item | engine result | expected |
|---|---|---|
| GQ28 (AE1), GQ35 | `combined`, no figure, completeness `insufficient` (found 2, awaiting 1, unknown metadata 1) | the preliminary figure over H1 and H2 with H3 awaiting review. AE1 requires a computed answer with n and coverage. The per-document states (H3 awaiting review) already pass. |
| GQ29, GQ49.1 (AE4) | `abstain` (found 1, awaiting 1, unknown metadata 1) | H5's value with H4v2 awaiting review |
| GQ36 | `abstain` (found 1, unknown metadata 1) | H4v2's count with H5 not stated |
| GQ48.2, GQ48.3 | `abstain` (found 1, unknown metadata 1) | 2.80 (Givatayim) and 3.05 (Tel Aviv) |

These failures are left visible on purpose: no known gap masks them.

## Limits of the matching (documented, not hidden)

- Hebrew forms are compared by removing up to two one-letter prefixes and a plural/feminine suffix. This is
  not a morphological analyzer.
- A number belongs to the nearest attribute word in its sentence. A sentence that names no attribute at all is
  accepted, as before.
- A descriptive value needs one word of the document's sentence. "בשנת 2022 שופץ חדר הרחצה" would still match
  "2022: kitchen" through "שופץ". Before, any claim with "2022" passed.
- Without an `audit` block, coverage is checked by counts. In round 6, GQ28's awaiting-review count of 1 was
  D7's junk value, not H3. With the audit block, which the engine now emits, this is checked per document.
