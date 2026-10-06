# Failure analysis: real-model round 6 (general question set)

Diagnosis of every failing turn of the sixth real-model run of `backend/eval/questions_general.yaml`
(2026-10-06 11:45 UTC, commit `70f35bc`, OpenAI `gpt-5.4-mini`, office A in cloud mode; 35/60 items,
41/69 turns passed). Rounds 3–6 (`09b9e95`, `0008e4c`, `e524c1b`, `70f35bc`) are compared for every item.
Nothing under `backend/app`, `frontend` or `tests` was changed; no data was written.

## Method

- **Stored turns.** For each failing turn: the stored `questions.plan` (`model_plan` and the server's
  `turn_plan`), `questions.steps`, the full answer (`raw.answer` of the result file), and the scorer facets that
  failed.
- **Facts.** The `facts` and `fact_extraction_ledger` rows of the attribute the turn used, at extraction
  version `x6`. Round 6 was the first run of `x6`, so these rows are what round 6 read. Ledger timestamps run
  11:46–11:50.
- **Ground truth.** Expected values come from `tests/fixtures/ground_truth.yaml` → `general_facts`.
- **Real replays: 72 provider calls in total.** Each ran inside `docker compose exec backend`. Every database
  transaction was rolled back, and nothing was written to `facts`, the ledger or `questions`.
  - Extraction, 42 calls: `read_version` + `build_prompt` + `provider.structured(Purpose.EXTRACT)` +
    `validate_mention`, 3 runs per (version, attribute), on 14 (version, attribute) pairs.
  - Interpretation, 21 calls: `interpret_with_model` 3 times on each of 7 turns, with the live gazetteer and
    `list_attribute_handles(useful_only=True)`. Follow-up turns used a reconstructed starting state (built from
    the previous turn's stored plan and the conversation's sources).
  - Composition, 9 calls: GQ24 and GQ20 run through `gather_sides` → `_call_answer` → `_layer_one` → judge.
- **Deterministic replays, no model call.** `locate_evidence` and `_locate` for GQ43, GQ49.2, GQ50 and GQ57.
  An audit recomputed every round-6 computation from the stored facts (section C).
- **Scripts and raw outputs.** All are under
  `/private/tmp/claude-501/-Users-arigabay-Code-Rag/7760d0eb-869a-4d0c-af31-bb217b3720da/scratchpad/diag/`
  (`out/*.txt`).

Caveat: the live database kept changing after round 6. The user asked five questions between 12:08 and
12:14; one of them extracted "שטח המרפסות בדירה" over 17 more versions. No attribute definition was created
after round 6, and no ledger row that round 6 read was changed.

---

## Summary

| primary category | turns | items |
|---|---|---|
| wrong scorer (behavior correct per the plan; the scorer or the answer key disagrees) | **10** | GQ14, GQ18, GQ22.1, GQ28, GQ35, GQ36, GQ51.1, GQ52.1, GQ55.2, GQ57 |
| verification (claim layer 1 / judge / absence detection; mention naming validation) | **6** | GQ20, GQ24, GQ29, GQ34, GQ45, GQ49.1 |
| question understanding (interpretation / plan) | **5** | GQ22.2, GQ33, GQ39, GQ40, GQ43 |
| data extraction (mention not found / wrong value) | **3** | GQ32, GQ38, GQ56 |
| conversation context (relation / inherited conditions) | **2** | GQ48.3, GQ49.2 |
| document retrieval (locate / search scope) | **2** | GQ50.1, GQ50.2 |
| computation | 0 | the arithmetic was correct on the facts used in every computation (section C). Two computation defects are real, but no round-6 turn failed on them: ignored `property_type` scope and missing dedup of H6/H7 |
| composition / wording | 0 (secondary only) | GQ20, GQ45, GQ49.1 |
| **total** | **28** | |

Where results changed between rounds, the stage that changed was found. **None of the 28 is attributed to
"model noise".** The causes fall into three groups:

- **Deterministic code paths: 15 turns.** GQ14, GQ18, GQ22.1, GQ24, GQ29, GQ49.1, GQ50.1, GQ50.2, GQ51.1,
  GQ52.1, GQ28, GQ35, GQ36, GQ48.3 (for the model output it received), GQ57 (a code change in `d405ae2`).
- **A specific stage whose output varied: 13 turns.** The varying stage is one of:
  - interpretation: GQ22.2, GQ33, GQ39, GQ40, GQ43, GQ49.2, GQ55.2;
  - extraction mentions: GQ32, GQ38, GQ56, GQ34 (naming flip);
  - answer wording that escapes a closed regex: GQ20, GQ45.

### All failing turns

| turn | question (short) | primary | secondary | facets failed (r6) | r3 | r4 | r5 | r6 | where rounds differ |
|---|---|---|---|---|---|---|---|---|---|
| GQ14.1 | איפה כתוב שהבניין בהירדן 30 אינו כולל מעלית | wrong scorer | — | values | fail | fail | fail | fail | identical every round |
| GQ18.1 | מדוע יתרת זכויות... תרומה מוגבלת | wrong scorer | — | values | fail | fail | fail | fail | identical |
| GQ20.1 | השווה גובה תקרה בין שתי השומות (בן יהודה) | verification | composition | outcome, sides | PASS | fail | fail | fail | answer-model wording + 3 compose paths |
| GQ22.1 | מה נכתב... על התכנית לתוספת קומה | wrong scorer | — | values | fail | fail | fail | fail | identical |
| GQ22.2 | אילו הנחות השתנו בין הגרסאות | question understanding | (scorer lenient on `changed`) | task_type | PASS | fail | fail | fail | plan task label (answer vs compare) |
| GQ24.1 | האם שיעור ההתאמה השתנה בין הגרסאות | verification | — | values | fail | fail | fail | fail | identical (4/4 rounds, 3/3 replays) |
| GQ28.1 | מה גודל ממ״ד ממוצע ברמת גן (AE1) | wrong scorer | data extraction | result, values | 10.75/n2 | 10.75/n2 | 10.75/n2 | 10.75/n2 | figure identical; coverage varies with extraction |
| GQ29.1 | שטח המרפסות הממוצע בגבעתיים | verification | wrong scorer | result, values, coverage | 2.00/n1 | 7.00/n1 | 7.00/n1 | 7.00/n1 | r3 used another attribute definition |
| GQ32.1 | שטח המחסן הממוצע | data extraction | scorer (found ≠ usable) | coverage | PASS | fail | PASS | fail | junk mentions in D6/D11 |
| GQ33.1 | הבניין הוותיק ביותר ומתי נבנה | question understanding | conversation state | task_type, outcome | 2017/n1 | 1958/n4 | clarify | clarify | plan (extra compare step) |
| GQ34.1 | בכמה שומות ברמת גן נבנה אחרי 2010 | verification | wrong scorer | result, values | 1/n1 | 0/n1 | 1/n2 | 1/n2 | extraction naming verdict for H3 |
| GQ35.1 | ממ״דים קטנים מ-11 מ״ר ברמת גן | wrong scorer | data extraction | result, values | 1/n2 | 1/n2 | 1/n2 | 1/n2 | identical figure |
| GQ36.1 | כמה מרפסות בממוצע בגבעתיים | wrong scorer | data extraction | result, values, coverage | 2.00/n1 | 2.00/n1 | 2.00/n1 | 2.00/n1 | identical |
| GQ38.1 | שטח החצר הממוצע בדירות הגן | data extraction | computation (scope) | coverage | fail | fail | PASS | fail | junk H5 mention |
| GQ39.1 | היקף יתרת זכויות הבנייה בכל שומה | question understanding | data extraction | result, values, coverage | values n4 | abstain | abstain | values n1 | attribute type flip → different definition |
| GQ40.1 | בכמה שומות שיפוץ משנת 2020 ואילך | question understanding | data extraction | result, values, coverage | 0/n1 | abstain | abstain | 0/n1 | plan filters / type differ every round |
| GQ43.1 | מה נכתב על בריכה בשינקין 18 | question understanding | document retrieval | task_type, outcome, abstention_kind | fail | fail | PASS | fail | plan task (locate vs answer) |
| GQ45.1 | (yossi) גודל ממ״ד ממוצע ברמת גן | verification | composition | outcome, abstention_kind | fail | PASS | fail | fail | answer wording / review state of D4 |
| GQ48.3 | ובתל אביב? | conversation context | — | relation | clarify | PASS | PASS | fail | model relation label → server mapping |
| GQ49.1 | שטחי המרפסות בגבעתיים | verification | wrong scorer, composition | result, values, coverage | fail | fail | fail | fail | identical from r4 |
| GQ49.2 | עכשיו בנושא אחר: היתר בנייה (AE4) | conversation context | — | relation, state, sources | PASS | fail | fail | fail | plan copies the Givatayim filter |
| GQ50.1 | באילו שומות אין מעלית | document retrieval | — | sources | fail | fail | fail | fail | identical scoring rule |
| GQ50.2 | תראה לי את המקור | document retrieval (inherited) | scorer double-counts | sources | fail | fail | fail | fail | inherits turn 1 |
| GQ51.1 | המחיר או השווי למ״ר בחרוזים 2024 (AE5) | wrong scorer | — | task_type | fail | fail | fail | fail | identical |
| GQ52.1 | same question (AE5) | wrong scorer | — | task_type | fail | fail | fail | fail | identical |
| GQ55.2 | עכשיו משהו אחר: ייעוד המגרש בארלוזורוב 88 | wrong scorer | question understanding | relation, source_precision | PASS | fail | PASS | fail | model relation label |
| GQ56.1 | (dana) ייעודי קרקע ברמת גן | data extraction | verification (naming) | coverage | fail | fail | fail | fail | junk mentions; r4/r5 had a wrong value |
| GQ57.1 | באיזו שומה נסגרה מרפסת בלי היתר | wrong scorer | document retrieval | sources, values | PASS | PASS | PASS | fail | code change `d405ae2` (locate variants) |

---

## Per-item evidence

### Wrong scorer (10)

#### GQ14.1: locate answer can never "state" a value

- **What happened.** The plan was `task_type=locate`, step `locate`, entity "רחוב הירדן 30". The answer is a
  template list: "• H8 … — עמ׳ 1 [E1]", snippet "נבנה בשנת 1972 ואינו כולל מעלית". The cited source
  H8 p1 is exactly the answer key's `all_of`.
- **Why the scorer fails it.** The key lists `values: [{fact: H8-F03, value: false}]`, and `fact_value_stated`
  returns True for a boolean. But `_claim_states` (general.py 266–275) iterates `answer.claims`, and a locate
  answer has none (`turn._locate` builds no claims). The facet therefore fails in every round (r3–r6).
- **Requirement.** R22 says an answer presents "documents and pages" as needed. For a locate task that list is
  the answer.

#### GQ18.1: the "2" of "m2" is demanded as a number

- **What happened.** Plan `answer` / `search`, scoped to H2. Claim 2, cited to E3 = H2 p1: "…יתרה של כ-60 מ״ר
  שטח עיקרי שטרם מומשה…". The answer is correct.
- **Why the scorer fails it.**
  - The key value is `"60 m2 main area"`. `fact_value_stated` (general.py 254–255) reduces a descriptive
    value to "every number it contains", and that set is {60, 2}: the 2 is the exponent of "m2".
  - The claim holds only 60.
  - Replaying the scorer function gives `False`. Identical r3–r6.

#### GQ22.1: Hebrew number word not accepted for a string value

- **What happened.** Claims (cited H4v2 p2): "תכנית גב/600, המאפשרת תוספת קומה אחת לבניין, אושרה… במאי 2023".
  H4v2-F09 passes.
- **Why the scorer fails it.**
  - H4v2-F10 `"1 additional floor"` needs the digit 1. The answer says "קומה אחת".
  - The header says a small Hebrew number word is accepted. `fact_value_stated` accepts word numbers only
    when the key value is an `int` (line 239), never inside a descriptive string.
  - Identical r3–r6.

#### GQ28.1 (AE1) and GQ35.1: H3 can only ever be "awaiting review"

- **What the system returned.**
  - GQ28: mean 10.75, n=2, from H1 12 and H2 9.5.
  - GQ35: count 1 of 2 below 11 m², namely H2.
  - Both figures are recomputed exactly in section C, and both are the same in r3–r6.
- **What the key expects.** The key includes H3 = 10.5 m², derived from "מרחב מוגן דירתי במידות פנים של 300 על
  350 ס״מ".
- **Extraction replay (H3, attribute "גודל ממ״ד", 3 runs):**
  - runs 1 and 3: the mention is accepted, `canon=10.5`, `assumed=True`, `synonym=True` → the fact is
    `needs_review`;
  - run 2: rejected `attribute_term_not_found`. The quote "מידות פנים של 300 על 350 ס״מ" omits the term
    "מרחב מוגן דירתי".
- **Why the key conflicts with the plan.** By KTD8 step 5 ("needs_review: … a conversion with assumptions") and
  KTD9/R15 ("facts needing review are excluded and counted"), H3 can never enter the preliminary figure. That
  was the settled decision. The expected `value 10.67`, `n 3` and "H3 cited" contradict it.
- **Secondary: a real extraction defect, not scored.** In round 6 the ledger stored H3 as `not_stated`, with
  reasons `{value_not_in_quote: 1, attribute_term_not_found: 1}`. It is the run-2 behavior, so the coverage
  line tells the user H3 does not state the datum. D7 is counted as `found` because the extractor returned
  D7's whole-property area ("שטח הנכס: 90 מ״ר רשום", role `other`, `needs_review`). The scorer's coverage facet
  passed only by coincidence (see A-L2).

#### GQ36.1: implicit singular count

- **What happened.** H4v2 "בדירה שתי מרפסות" → 2. H5 ("לדירה מרפסת חזית בשטח 7 מ״ר") has no number for the count.
- **Extraction replay (H5, "מספר המרפסות", 3 runs).** The model reports value "1" in every run, and every run is
  rejected `value_not_in_quote`.
- **Why the key conflicts with the plan.** KTD8 step 4: "code parses the value and unit from the quote". So
  H5 → not stated, and the system's 2.00 / n=1 / "2 documents do not state it" is the specified behavior. The
  key's 1.50 / n=2 needs a product decision, either "a singular noun counts as 1" or an answer-key change.
- **Side finding.** Replay run 3 also accepted "אין בדירה ממ״ד" as a balcony count of 0. It was sent to
  review as a synonym, not rejected.

#### GQ51.1 and GQ52.1 (AE5): clarification asked by the computation, task stays `compute`

- **What happened.**
  - Rules path (`parse_route=rules`); plan `compute` / `compute_records`.
  - `_records` asks the `data_kind` clarification only because the records differ (R20, KTD2, and the
    `MODEL_CLARIFY_KEYS` comment in plan.py).
  - The outcome facet passes: a clarification with key `data_kind`.
- **Why the scorer fails it.** Only `task_type: compute (expected clarify)` fails (general.py 352–354), in
  every round. The rules fast path never produces task `clarify` by design. A clarification raised by a tool is
  recorded on the answer, not in the plan.

#### GQ55.2: `new_question` vs `topic_change`, and a header-page citation

- **The relation facet.** The model labeled the turn `new_question`. `apply_turn` treats `new_question` and
  `topic_change` identically: both reset the context. The scorer's own `cleared: [attribute]` check passed.
  The replay shows the label is unstable: 1 of 3 runs `topic_change`, 2 of 3 `new_question`. It also flipped
  between rounds (r3/r5 pass, r4/r6 fail).
- **The precision facet.** The answer is correct: "ייעוד המגרש … הוא מגורים ב׳" [H3 p2]. A second claim,
  "בשומה מצוין שהנכס הוא דירה בכתובת ארלוזורוב 88", cites H3 p1 (the report header). `source_precision` fails
  only because p1 is not in `also_valid`.

#### GQ57.1: page binding for "which appraisal" plus a locate regression

- **What happened.** Round 6 located H5 with one passage, p2: "סגירת המרפסת ללא היתר עלולה לחייב הריסה…".
  Rounds 3–5 also returned the p1–2 chunk: "…שנסגרה בתריסים ללא היתר בנייה".
- **The regression is a code change, not variance.**
  - Commit `d405ae2` (between r5 and r6) made locate variants alternatives.
  - `locate_evidence` now keeps, per document, only the passages of the single best-scoring variant. Variant 2
    ("סגירת מרפסת ללא היתר") scores 1.25 with p2 only; variants 1 and 3 hold the p1–2 chunk at 1.0.
  - The replay with the r3 queries and the current code also returns p2 only.
- **Why the scorer is too strict.** The question asks which appraisal, and H5 is correct. The key already accepts
  H5 p2 for sources (`also_valid`). But the `values` check binds H5-F05 to page 1 (general.py 433), ignoring
  `also_valid`.

### Verification (6)

#### GQ24.1: layer 1 rejects the server's own version labels

- **What happened.** All claims were dropped in r3–r6, and the answer fell back to quoted passages.
- **Replay (3 runs of answer + layer 1).**
  - The model writes, for example, "בגרסה 1 הובאה בחשבון התאמה בשיעור 6% …" [E1] and "בגרסה 2 … 10%" [E5].
  - Every claim fails `unsupported_number` in all 3 runs: the "1" and "2" come from the compare side labels
    "H4 …, גרסה 1/2" (compare.py `_label`, line 79), which the model sees in `answer_input`. They do not occur in
    the cited passage text.
- **Why GQ25 passes.** Its model wrote "בגרסה הראשונה/השנייה", in words.
- **Requirement.** R23 is meant to check that numbers are supported by evidence. Here the check rejects a number
  that the server itself put into the label, not a number the model invented.

#### GQ20.1 (AE6): a side with no datum is counted as covered

H7 states no ceiling height. AE6 requires "the comparison is incomplete", with H7 named. Three different paths
failed in three rounds:

- **r6.** The claim "בשומה השנייה לא נמסר גובה התקרה בדירה" [E1–E3 = H7] is not recognized as an absence claim.
  The `_ABSENCE` regex (compose.py 131) has no "נמסר". The claim went to the judge (`partial`), was kept, and
  marked H7 as cited. Worse, the model's conflict list turned it into "הבדל בין המקורות לגבי גובה התקרה":
  missing data is presented as a conflict.
- **r5.** The H7 claim was dropped, and `_dropped_sides` quoted H7's title line "שומת מקרקעין — בן יהודה 140"
  as H7's statement. H7 counted as covered.
- **r4.** Every claim was dropped, and `_abstain_stated` returned `not_stated`. That path never sets
  `incomplete`/`missing_sides`.
- **r3 passed.** The model wrote "לא צוין", which the regex catches.
- **Replay (3 runs).** Run 1 wrote "לא צוין" (caught); runs 2 and 3 made no H7 claim. All 3 would be marked
  incomplete.

The stage that varies is the answer model's wording. The defect is the closed-list regex plus the two other
paths.

#### GQ45.1 (yossi): an absence claim kept as an answer

- **What happened.**
  - The computation correctly abstained: in scope D4 only, `not_stated`.
  - The search part's claim "במסמכי הראיות יש שומת דירה ברמת גן, אך אין בהם נתון ישיר על גודל ממ״ד" escapes
    `_ABSENCE`. "אין בהם נתון" puts "בהם" between "אין" and "נתון", which the regex does not allow.
  - The judge rated it `partial`, it was kept, and `combined_abstention_kind` therefore returned None.
- **Result.** kind `combined` with no abstention kind, so the outcome and abstention_kind facets fail.
- **Other rounds.** r4 passed (`not_stated`). r3 and r5 returned `not_extracted_or_verified`, because a D4 value was
  awaiting review (D4 states no safe-room size, so that value was junk).

#### GQ29.1 and GQ49.1: balcony area of H4v2 rejected by naming validation, every time

- **The ledger.** Round 6 stored H4v2 as `not_stated` with `{attribute_term_not_found: 2}`.
- **Replay (3/3 runs identical):**
  - the mentions are "מרפסת סלון בשטח 12 מ״ר" and "ומרפסת חדר שינה בשטח 6 מ״ר", with `attribute_term="מרפסות"`;
  - `_naming` (facts.py 509–519) requires the term to occur verbatim in the quote, or in a table cell's context;
  - the plural "מרפסות" is in the chunk ("בדירה שתי מרפסות") but not in the singular quote, so both mentions are
    rejected.
- **What the user sees.** The coverage line says H4v2 does not state the datum. In GQ49.1 the same answer's
  content part says "בדירה בשינקין … מרפסת סלון בשטח 12 מ״ר ומרפסת חדר שינה בשטח 6 מ״ר": the answer
  contradicts itself.
- **Secondary: the key needs a decision.** The key expects H4v2 = 18 m², the sum of two values. Under KTD8 two
  values in one version make the fact `needs_review`, and the extraction policy forbids computing. Even a fixed
  validator yields 7.00 n=1 with 1 awaiting review, not 12.50 n=2.
- **Other rounds.** r3 used the older definition "שטח המרפסות" and returned 2.00 with unit "unit" (apparently
  H4v2's balcony count). That answer was wrong.

#### GQ34.1: synonym verdict depends on which words the model puts in `attribute_term`

- **Round-6 facts.** H1 2017 `auto_validated`; H8 1972 `auto_validated`; H2 2015 `needs_review` ("אוכלס בשנת
  2015"); H3 2004 `needs_review` ("הבניין הושלם בשנת 2004").
- **Replay H3 (3 runs):**
  - term "הושלם בשנת" → `synonym=True` → `needs_review` (runs 1–2);
  - term "הבניין הושלם בשנת" → `synonym=False` → `auto_validated` (run 3), because "הבניין" shares a word with
    the label "שנת הבנייה של הבניין";
  - run 1 also extracted the renovation year 2020 as the build year: a wrong value, which made a conflict.
- **Replay H2 (3 runs).** `needs_review` every time. Occupancy year as build year is uncertain, and review is
  the settled behavior.
- **Answer.** 1 of 2 (correct on the used facts). It also renders the year as "2,017": a wording defect.
- **Secondary.** The key counts H2 (2015) as built after 2010 and n=4. Under "uncertain values go to review" the
  best achievable answer is 1 of 3 with 1 awaiting review.
- **Other rounds.** r4 answered 0 of 1: a wrong answer, since H1 was missing.

### Question understanding (5)

#### GQ22.2: compare executed under task `answer`

- **What happened.** The model plan was `task_type=answer`, relation `follow_up`, step `compare [S1, S2]`. The
  compare ran, both versions are labeled, and all sources are cited. Only the label is wrong (R1).
- **Variance.** The replay gave 3/3 `compare`; the rounds gave r3 compare, r4 answer, r5 compare, r6 answer.
  This is interpretation variance with no user-visible effect.
- **Not caught by the scorer.** The answer lists the valuation and report dates and the planning status. It
  omits the main changed assumption, the adjustment rate 6% → 10%: the side retrieval (`PER_SIDE_LIMIT=4`) did
  not return those passages. The key's `changed` field is never scored (A-L3).

#### GQ33.1: an extra compare step turns a computation into a referent clarification

- **What happened.**
  - r6 model plan: `compute_explain`, steps `extract_and_compute A9`, `extract_and_compute A13`, `compare A9`
    with no handles and no entities.
  - `apply_turn → _referent_problem` (state.py 195–210) turns a compare step without sides into
    `clarification referent`.
  - r5 was the same; r3 and r4 computed (r3 wrong: 2017; r4 1958 n=4).
- **Replay (3 runs).** All `compute`, no compare step. So the varying stage is the plan.
- **A second risk in the same plan.**
  - Handle **A9 is the junk definition "הבניין הוותיק ביותר"** (numeric, no dimension), created by this question
    in round 1. It is visible because it holds `auto_validated` facts. 2 of 3 replays chose A9.
  - `_execute_steps` runs only the first compute step. With A9 it computes on the junk definition (cold at `x6`:
    20 new reads) instead of A13 "שנת הבנייה של הבניין".

#### GQ39.1: the attribute type flipped between the first run and the re-run

- **The first run (11:48).** It used the numeric, dimensionless definition `x_a41ce30a7bfe`: 6 inline reads,
  14 queued.
- **The re-run after the worker drained (11:50).** The interpreter said `value_type=text`.
  - `resolve_attribute` refuses the numeric handle for a text request and matches the text definition
    `x_875c6fcde567` by label. Its `x6` ledger was empty.
  - Another 6 inline reads, 14 pending.
  - The scored answer is partial: "14 מסמכים טרם חולצו".
- **The only listed value is wrong.** D6's phrase "עם זכויות בנייה לא מנוצלות" was `auto_validated` and is
  presented as a quantity of remaining rights.
- **Fully extracted now (audit):**
  - text values "אין", "טרם נוצלו", "עם זכויות בנייה לא מנוצלות";
  - H2 "כ-60" is `needs_review`;
  - H4v2 (1 floor) is not stated, and H6 (2.5 floors) is rejected or in review.
  - Replay of H6 (3 runs): "2.5 קומות" accepted once and rejected once (`attribute_term_not_found`); the other
    values were "טרם מומשו". Replay of H4v2: 0 mentions in 3/3 runs.
- **Secondary: data extraction.** Neither typing can produce the key's five quantities with mixed units.

#### GQ40.1: hallucinated scope and the wrong kind of filter

- **r6 plan.** `conditions.city = "גבעתיים"`, which the question never says, plus `value_filter >= 2020` on
  "שיפוץ שבוצע בדירה". The model gave type `date`; the server made it numeric.
- **Effect.** The document set had 3 versions (D6, H4v2, H5), and the answer was "0 (מתוך 1 תצפיות, גבעתיים)".
  That is a wrong answer for a question about all appraisals: the true count is 3.
- **Replay (3 runs).**
  - Type `text` in 3/3. A10 has three duplicate labels; see section B.
  - In 2/3 runs `year_from: 2020`: a document-date filter in place of a value condition, against the prompt's
    explicit rule.
  - `property_type: apartment` in 3/3.
- **Other rounds.** The plan differed every round: r3 boolean with `value_filter = "כן"` and `year_from 2020`;
  r4 boolean with `year_from 2020`; r5 numeric `>= 2020`.
- **Secondary: data extraction.** Text-typed extraction cannot answer it. Replay on H2/H3 gives phrases ("שופץ",
  "ברמה גבוהה"), never a year.

#### GQ43.1: "מה נכתב על" read as locate, and locate lists a document without the topic

- **Replay (3 runs).** `locate` in 3/3, and in 3/3 the model added `city = תל אביב-יפו` for Shenkin 18, which is
  in Givatayim. Entity scope overrode the filter, so the city had no effect. r5, the one passing round, planned
  `answer` and the composer abstained `not_stated`.
- **Secondary: document retrieval, a wrong answer.** Locate listed H4v2 p1 and p2 ("תיאור הנכס והבניין", "…
  תוספת קומה אחת לבניין") as relevant to a pool. The deterministic replay shows why:
  - "בריכה" has df=0 in scope, so `present` excludes it (search.py 565);
  - "בניין" alone then carries the whole remaining weight, which makes it a "complete" passage;
  - the document is listed, with only the soft note "בחלק מהמסמכים נמצאו רק חלק ממילות השאלה".

### Data extraction (3)

| turn | figure (correct) | what inflated coverage `found` | replay frequency |
|---|---|---|---|
| GQ32.1 | 5.33, n=3 (H1 5, H4v2 7, H6 4) | D11 "שטח הנכס: 72 מ״ר נטו" and D6 "שטח הנכס: 160 מ״ר רשום" extracted as storage area, role subject, `needs_review` → found 5 vs 3 | D6 1/3, D11 0/3; r3 and r5 passed (found 3) |
| GQ38.1 | 85.00, n=1 (H8) | H5 "מרפסת חזית בשטח 7 מ״ר" extracted as yard area, `needs_review` → found 2 vs 1 | 0/3; r5 passed |
| GQ56.1 (dana) | values מגורים ב׳ (2), מגורים ג׳ (1), n=3 | 9 junk mentions, all in review: D2/D12 "סוג נכס: דירה", D8 "בסיס מע״מ" and "נטו/ברוטו/רשום" (comparables), D3 "ללא שעבודים" and "פינוי ובינוי" → found 7 vs 3 | D12 "דירה" 2/3, D8 0/3 |

- **How junk reaches the review queue.** In all three items the extractor returned values of another attribute.
  The validator did not reject them: a term that shares no word with the attribute ("שטח הנכס", "סוג נכס") is
  classified as a *synonym* and sent to review (facts.py `_naming`, plus `fact_rows` line 544). The figures stay
  right, but the review queue fills with junk, and `found` counts any version holding a fact of any status or
  role (facts.py 703).
- **A wrong value that reached figures.** In r4 and r5, GQ56 included **"שינוי ייעוד"** (D3, about the
  *neighboring* plot) as an `auto_validated` zoning value.

### Conversation context (2)

#### GQ48.3: "ובתל אביב?"

- **What happened.**
  - The model returned `turn_relation=change_clarification` with nothing pending (3/3 in the replay).
  - `plan._relation` (plan.py 328–345) maps that to `new_question`, because the attribute and topic are
    unchanged. It never maps it to `follow_up`.
  - The computation was still right (3.05, n=1) because the model repeated the attribute and metric. The
    relation and the state reset are wrong (R18).
- **Other rounds.** r4 and r5 passed because the model said `follow_up`. r3 asked an attribute clarification.

#### GQ49.2 (AE4): the topic change keeps Givatayim

- **What happened.**
  - r6 plan: `follow_up`, with `conditions.city=גבעתיים` and `property_type=apartment` copied into the delta.
  - Locate ran with the Givatayim filter and returned only H5.
  - The unscoped replay of the same queries returns H2, H5 and H8.
- **Replay (3 runs).**
  - `topic_change` in 2/3, but run 1 still carries `city=גבעתיים`.
  - `follow_up` in 1/3.
- **Why the server does not catch it.** `apply_turn` resets the context on a topic change and then re-applies
  the delta, so a condition the model copied from the old state survives. This violates AE4: the scope is
  silently narrowed.

### Document retrieval (2)

#### GQ50.1 and GQ50.2: fully supported documents dropped by the relative threshold

- **What happened.**
  - Round 6 queries: "אין מעלית בבניין שומה", "ללא מעלית בבניין", "מעלית לא קיימת". Result: D1v2 only, the same
    in r3, r5 and r6.
  - r4 listed H2, which says the building *has* an elevator. That was a wrong answer.
- **Deterministic replay, with the threshold disabled:**
  - H6 ("…בבניין בן ארבע קומות ללא מעלית") and H8 are both `full=True`, score 1.0;
  - D1v2 scores 1.25, because its phrase bonus comes from "מעלית בבניין" being adjacent;
  - `RELATIVE_THRESHOLD 0.9 × 1.25 = 1.125` drops the two fully supported documents (search.py 600).
- **GQ50.2.** `show_sources` correctly re-shows turn 1's sources (R19). It fails only by inheritance.

---

## A. Scorer problems

### Too strict

| # | rule (backend/eval/general.py) | effect | contradicts |
|---|---|---|---|
| S1 | `_claim_states` (266–275), used by the values check (435) | A `values` entry with a `value` needs a *claim*; template answers (locate) have none → GQ14 fails forever | R22 (documents and pages are the answer of a locate task); R1 locate task |
| S2 | `fact_value_stated` (254–255): every digit of a descriptive value | "60 m2 main area" requires 2 (from "m2") → GQ18 | header "wording is never scored"; R22 |
| S3 | `fact_value_stated` (239): Hebrew number words only for `int` values | "1 additional floor" vs "קומה אחת" → GQ22.1 | header promises small Hebrew number words |
| S4 | task_type exact match (352–354) | Rules-path money questions are planned `compute`; the computation asks the result-changing clarification → GQ51.1/GQ52.1 fail although the clarification outcome passes | R20, KTD2 (rules fast path), plan.py `MODEL_CLARIFY_KEYS` decision |
| S5 | relation exact match (355–357) | `new_question` vs `topic_change` have identical effects in `apply_turn`; the `cleared` check already verifies the behavior → GQ55.2 | R18 is about what is cleared, not the label |
| S6 | `source_precision` (424–427) | A supporting citation to the same document's header page fails precision → GQ55.2 | R22 |
| S7 | values page binding (433) ignores `also_valid` | "Which appraisal" answered with the H5 p2 passage that states the same fact → GQ57 | R22; the key itself lists H5 p2 as valid |
| S8 | coverage `found == values_found` (521–523) | `found` is a ledger state that includes versions whose only fact is in review. Correct behavior fails whenever a legitimate value goes to review (H3 in GQ28 when accepted). In r6 it also caught real junk (GQ32/38/56), so the check is useful but mis-specified | R14/R15 list "values found" and "values awaiting review" separately |
| S9 | answer-key values (questions_general.yaml) | GQ28/GQ35 (H3 W×H conversion → review), GQ29/GQ49.1 (H4v2 = sum of two stated values), GQ34 (H2 occupancy year as build year), GQ36 (H5 count from a singular noun) | KTD8 steps 4–5, KTD9, R15 ("uncertain values go to review", values parsed from the quote) |
| S10 | meta turns scored on the full expected source list | GQ50.2 re-scores turn 1's retrieval although `show_sources` behaved per R19 | R19 |

### Too lenient

| # | rule | effect |
|---|---|---|
| L1 | `fact_value_stated` returns True for `bool`, `None` and `"none"` (234–235) | Any answer that cites the document passes, even the opposite claim. GQ11 passed with "לדירה ברחוב המאבק 25 יש מחסן: אין" ("has a storage room: none"). GQ59 passes the same way |
| L2 | coverage compares counts, not documents (521–523, 530–534) | GQ28 r6 passed coverage with found = {H1, H2, D7-junk} instead of {H1, H2, H3}. `not_stated_includes` checks only a total (`unresolved >= len(...)`), never that H8 is among them |
| L3 | `changed` (GQ22 turn 2) is never read | The "assumptions changed" answer omitted the 6% → 10% adjustment rate and would have passed if the task label matched |
| L4 | descriptive values reduced to their digits (254–255) | "approved (May 2023)" is satisfied by any claim containing "2023", including "deposited in 2023" (GQ22.1 F09, GQ58) |
| L5 | `_page_ok` (223–225): a source with an empty `page_list` matches every page | Any DOCX or page-less chunk satisfies any page requirement |
| L6 | conflict check uses all returned `sources` and any number in the text (440–446) | Values that appear only in quoted fallback passages or uncited sources count as "both statements shown" |
| L7 | combined answers: only the figure and coverage are scored | GQ49.1's content part contradicts its own coverage line (H4v2 "not stated" vs a claim listing H4v2's 12 + 6 m²); not detected |

### Harness note (not the scorer)

`scripts/eval.py` re-asks items with pending extraction after the worker drains (about lines 652–665). It
assumes the re-run hits the same attribute definition. GQ39 shows it does not when the interpreter's
`value_type` changes: the re-run started a cold extraction and was scored partial.

---

## B. Attribute-definition proliferation

### Live `attribute_definitions`, office A

There are 4 structured definitions plus 23 extracted ones. All are at `x6`, and every extracted one is still
`proposed`. "Reads" counts ledger rows over all extraction versions (≈ extraction calls).

| meaning (unit) | definitions (key: label, type) | created by (question, time UTC) | reads | facts | duplicate? |
|---|---|---|---|---|---|
| safe-room area (m²) | `x_2ef805c0f3ff` גודל ממ״ד (numeric/area) | GQ28 / GQ45 wording, 08:41 | 96 | 31 | canonical |
| | `x_9d152ff168cc` שטח המרחב המוגן (numeric/area) | GQ08, 09:00 | 20 | 1 | **dup** |
| balcony area (m²) | `x_7e0629217a08` שטח המרפסות | GQ29, 09:02 | 6 | 1 | canonical (hidden: no usable fact) |
| | `x_9ac0772fbb6a` שטח המרפסת | GQ49.1, r3 10:50 | 3 | 2 | **dup** |
| | `x_5ceb36ee24ba` שטח המרפסות בדירה | GQ29, r4 11:21 | 26 | 7 | **dup** (now the visible one) |
| yard area (m²) | `x_a563dd0525ad` צמודה חצר; שטח החצר | GQ06, 08:57 | 40 | 13 | canonical (hidden) |
| | `x_dc13a8901296` שטח החצר | GQ38, r3 10:49 | 20 | 1 | **dup** |
| | `x_55696eab8df3` חצר צמודה לדירה | GQ06, r4 11:19 | 60 | 5 | **dup** (visible) |
| unused building rights | `x_a41ce30a7bfe` היקף יתרת זכויות הבנייה שטרם נוצלו (numeric, no dimension) | GQ39, 09:03 | 100 | 6 | canonical |
| | `x_875c6fcde567` same label (text) | during GQ18 (an explanation question), r3 10:39 | 80 | 22 | **dup by type** |
| renovation | `x_407022f21503` שיפוץ שבוצע בדירה (numeric, no dimension) | GQ40, 09:03 | 63 | 11 | canonical |
| | `x_216d7e7b4462` same label (boolean) | GQ59, r3 10:43 | 40 | 18 | **dup by type** |
| | `x_24bf063fb051` same label (text) | GQ40, r4 11:22 | 40 | 15 | **dup by type** |
| zoning | `x_2afab649d10c` ייעודי קרקע (numeric, no dimension) | GQ56, 09:04 | 14 | 3 | canonical (hidden) |
| | `x_3698660fae9b` same label (text) | GQ56, r3 10:50 | 56 | 55 | **dup by type** |
| year built (year) | `x_ae20b8b51d3b` הבניין הוותיק ביותר (numeric, no dimension) | GQ33, 09:02 | 40 | 20 | **junk** (question-shaped) |
| | `x_89bf10254148` שנת הבנייה של הבניין (numeric/year) | GQ34, r3 10:41 | 75 | 28 | canonical |
| property addresses | `x_04ad70342d0f` / `x_4405eb74df96` הכתובות של הנכסים (numeric / text) | manual question "תכתוב לי את כל הכתובות של הנכסים ברמת גן בשנת 2024", 10:08 and 11:33 | 11 + 11 | 86 + 74 | **junk** (a listing request turned into an attribute) |
| ceiling height (m) | `x_9bb70c5e0825` גובה התקרה | 09:02 | 100 | 20 | unique |
| storage area (m²) | `x_f6e9947c50dd` שטח המחסן | GQ32, 09:02 | 100 | 17 | unique |
| parking count | `x_c06e1cf68856` מספר מקומות חניה צמודים לדירה | GQ02, 08:59 | 15 | 6 | unique |
| balcony count | `x_1d92fe8545cb` מספר המרפסות | GQ19, r3 10:39 | 14 | 6 | unique |

Each creator was matched by the question whose plan `attribute.description` equals the label, asked within 60
seconds before the definition's `created_at`.

### What it costs

- **Duplicate and junk definitions.** They account for about 407 of 1,030 ledger reads (≈ 40%):
  - 20 for safe-room area;
  - 29 for balcony area;
  - 80 for yard area;
  - 80 for unused rights;
  - 80 for renovation;
  - 56 for zoning;
  - 22 for addresses;
  - 40 for "the oldest building".
- **Prompt-version bumps.** Moving from x1 to x6 re-read about 689 of the 1,030 (`upgrade_extraction_version`
  bumps every definition). Round 6 alone made 170 extraction calls: the x6 bump re-read all 12 definitions it
  touched.
- **Inside round 6.** GQ39's type flip caused 20 more reads of the same question within the same round.

### Why definitions multiply (code)

1. **Unproven definitions are hidden.** `list_attribute_handles(useful_only=True)` (attributes.py 234–248) hides
   proposed definitions with no `auto_validated`/`verified` fact. "שטח המרפסות", "צמודה חצר; שטח החצר" and
   "שטח המרחב המוגן" became invisible after a bad extraction. The next phrasing created a new definition and a
   full re-read.
2. **The value type is part of the key.** `proposed_key` (189) includes `value_type`, and `resolve_attribute`
   refuses a handle of another type. Every numeric/text/boolean flip of the interpreter (GQ39, GQ40, GQ56, GQ59)
   makes a parallel definition.
3. **The interpreter cannot tell duplicates apart.** It sees `handle, label, aliases, unit_dimension, source,
   description`, never `value_type` (interpret.py 246). A10, A14 and A18 are all "שיפוץ שבוצע בדירה", with no
   way to choose.
4. **Exact-label merging only.** "שטח המרפסת" ≠ "שטח המרפסות" ≠ "שטח המרפסות בדירה" after normalization;
   singular/plural and a trailing "בדירה" are not merged.
5. **Question-shaped descriptions become definitions.** "הבניין הוותיק ביותר" and "הכתובות של הנכסים" are
   accepted as attributes. Because they hold auto-validated facts they stay visible, and they keep attracting
   later questions: 2 of 3 GQ33 replays chose A9.

---

## C. Computation audits (round 6)

Every computation in round 6 was recomputed independently from the stored `x6` facts, using subject facts with
status `auto_validated`, `verified` or `corrected`, one value per entity. All figures are the preliminary tier:
no fact was reviewed by a person.

| item | authorized document set | observations used | arithmetic | vs ground truth (extraction completeness) | duplicates / units / status notes |
|---|---|---|---|---|---|
| GQ28 RG safe-room mean | 15 RG current versions (D1v2, D2, D3, D4, D5, D7–D12, H1, H2, H3, H8); unknown metadata 0 | H1 12, H2 9.5 | 21.5/2 = 10.75 ✓ | H3 (10.5 by assumption) missing: rejected in r6, in review at best; D2 mention without value correctly rejected | D7 "שטח הנכס 90" stored as an `other` fact, `needs_review`; ledger `found` |
| GQ35 RG safe-room < 11 | same 15 | H1 12, H2 9.5 | 1 of 2 ✓ | as GQ28 | — |
| GQ29 Giv balcony-area mean | D6, H4v2, H5 | H5 7 | 7.00 ✓ | H4v2 12 + 6 rejected (naming) | `property_type: apartment` in the plan silently dropped (`MetadataFilters` has no property type) |
| GQ49.1 Giv balcony-area values | D6, H4v2, H5 | H5 7 | [7] ✓ | as GQ29 | content part contradicts the coverage line |
| GQ30 all ceiling-height mean (pass) | 20 current versions | H1 2.75, H3 2.60, H5 2.80, H6 3.05 | 11.20/4 = 2.80 ✓ | complete | H7 (same unit as H6) states no height; no dedup effect |
| GQ60 > 2.70 m (pass) | 20 | same 4 | 3 of 4 ✓ | complete | — |
| GQ48.1 / 48.2 / 48.3 | RG 15 / Giv 3 / TA 2 (H6, H7) | {H1 2.75, H3 2.60} / {H5 2.80} / {H6 3.05} | 2.675 → 2.68 (half up) ✓ / 2.80 ✓ / 3.05 ✓ | complete | — |
| GQ31 Giv parking mean (pass) | D6, H4v2, H5 | H4v2 1 ("אחד"), H5 0 ("אין") | 0.50 ✓ | complete | stated absence as 0 accepted, per policy |
| GQ32 storage mean | 20 | H1 5, H4v2 7, H6 4 | 16/3 = 5.33 ✓ | complete; H2/H5 "אין" rejected (stated absence, not a value) | D6 160 and D11 72 (whole-property areas) in review; found 5 |
| GQ34 RG built > 2010 | 15 RG | H1 2017, H8 1972 | 1 of 2 ✓ | H3 2004 in review (naming); H2 2015 in review (occupancy) | year rendered "2,017" |
| GQ36 Giv balcony count | D6, H4v2, H5 | H4v2 2 | 2.00 ✓ | H5 (implicit 1) rejected | — |
| GQ38 yard mean | **all 20** (plan `property_type: garden_apartment` dropped) | H8 85 | 85.00 ✓ | complete (only H8 has a yard, so the dropped scope did not change the figure) | H5 balcony 7 in review |
| GQ39 unused rights (text) | 20 | at answer time D6 only (14 pending); now D6, H1 "אין", H8 "טרם נוצלו" | listing ✓ | none of the 5 quantities (60 m², 1, 2.5, 2 floors, none) extracted as values | text values are phrases; D6 is not a quantity |
| GQ40 renovation ≥ 2020 | **D6, H4v2, H5** (hallucinated Givatayim) | H5 2018 | 0 of 1 ✓ | scope wrong; text or untyped extraction cannot give the year | — |
| GQ45 yossi safe-room | D4 only (RLS: G2) ✓ | none | — | correct isolation | — |
| GQ56 dana zoning values | 14 (G1 + G3 RG; D4 excluded) ✓ | H1 ג׳, H2 ב׳, H3 ב׳ | ✓ | complete | 9 junk facts in review |
| GQ33 oldest building (not computed in r6) | 20 | would be H1 2017, H4v2 2012, H5 1968, H6 1958, H7 1962, H8 1972 | min 1958 | H2, H3 in review | **H6 and H7 describe the same unit (block 6960/52/8) but are keyed `doc:<id>`: never deduplicated, conflict never flagged** |

### Conclusions

- **The arithmetic is right in every case on the facts it used.** Decimal and half-up rounding are correct, the
  value filters are applied, and the n reported for a filtered count is the observed total.
- **The figure is wrong or incomplete** because of the facts that were missing (extraction and validation) or
  the scope (an interpretation that hallucinated a city; a silently dropped `property_type`). The computation
  itself is not at fault.
- **Two defects inside the computation layer:**
  1. Dedup misses H6/H7. `_subject_key` (facts.py 222) reads only `occurrences`, and the H documents have none;
     their header block/parcel is never used. This contradicts KTD8 step 6 and R13 ("handle duplicates").
  2. Coverage `found` counts versions whose only fact is `needs_review` or a non-subject role, so "נמצא ערך
     ב-N" over-states the documents that hold a usable value (R14).

---

## D. Top systemic defects (ranked by failing turns, with evidence)

1. **Naming validation is lexical and brittle** (facts.py `_naming` 509–519 and `fact_rows` 544).
   - It rejects correct mentions deterministically: the term must be a verbatim substring, and plural ≠
     singular. GQ29 and GQ49.1 (H4v2, 3/3).
   - It sends unambiguous statements to review: "הושלם" shares no word with "שנת הבנייה". GQ34; GQ28 and GQ35
     (H3).
   - It lets unrelated values in as "synonyms", into review: GQ32, GQ38, GQ56. That fills the review queue and
     inflates coverage.
2. **Layer-1 number check vs server-issued labels** (verify.py `check_claim`; compare.py `_label`). Every claim
   that repeats "גרסה 1/2" is dropped: GQ24 4/4 rounds, 3/3 replays.
3. **A side or answer with no datum is mishandled.**
   - Absence claims are a closed regex (compose.py 131); "לא נמסר" and "אין בהם נתון" are kept as substantive
     claims (GQ20, GQ45).
   - The dropped-side quote presents a title line as the side's statement (GQ20 r5).
   - The all-dropped path loses `incomplete` (GQ20 r4).
   - Missing data is rendered as a "conflict" (GQ20 r6).
4. **Attribute proliferation** (section B). About 40% of all extraction reads went to duplicate or junk
   definitions. A type flip makes cold re-reads in the same round (GQ39). Junk definitions keep attracting the
   interpreter (GQ33 A9).
5. **Locate scoring** (search.py).
   - The relative threshold plus the phrase bonus drops fully supported documents (GQ50, deterministic).
   - A df=0 topic word leaves a generic word "complete" (GQ43, a wrong document list).
   - The variant merge keeps one variant's passages (GQ57, regression from `d405ae2`).
6. **Relation normalization and inherited conditions** (plan.py `_relation`; `apply_turn` re-applies copied
   conditions). `change_clarification` with nothing pending → `new_question` (GQ48.3). A topic change keeps the
   old city (GQ49.2, AE4).
7. **Interpretation instability on types and filters.**
   - `value_type` numeric/text/boolean/date flips (GQ39, GQ40).
   - `year_from` used in place of a value filter (GQ40, 2/3 replays).
   - A hallucinated city (GQ40 Givatayim; GQ43 Tel Aviv 3/3).
   - A stray compare step → referent clarification (GQ33).
   - The server validates the schema but not these semantics.
8. **The entity key ignores report header metadata.** It produces no dedup and no conflict for the same unit
   appraised twice (H6/H7).
9. **Scope conditions silently dropped.** `property_type` from the plan is not a `MetadataFilters` field
   (GQ38, GQ29), while the answer's condition chips show it.

---

## E. Turns where the system said something wrong (not just incomplete)

### Round 6

- **GQ43:** H4v2 listed as the document relevant to "pool". It never mentions a pool.
- **GQ40:** "0 (מתוך 1 תצפיות, גבעתיים)" for a question about all appraisals. The true count is 3, and
  Givatayim came from the interpreter.
- **GQ49.2:** only H5 listed for "which appraisals mention a building permit" after an explicit topic change.
  The scope was silently Givatayim; H2 and H8 are missing.
- **GQ39:** the only value given for "the scope of remaining building rights" is D6's phrase "עם זכויות בנייה לא
  מנוצלות". That is not a scope, and D6 is a document the key lists as "mentioned without a value".
- **GQ20:** "no ceiling height in H7" is shown as a "difference between the sources" (a conflict).
- **GQ29 and GQ49.1:** the coverage line says H4v2 does not state the balcony area. It states 12 and 6 m². In
  GQ49.1 the same answer then lists those values.
- **GQ28 and GQ35:** the coverage says H3 does not state the safe-room size (it gives 300×350 cm). D7 is counted
  as "found" from its whole-property area.
- **GQ32, GQ38 and GQ56:** "נמצא ערך ב-N" counts documents whose only extracted "value" belongs to another
  attribute (property area, property type, VAT basis).
- **Wording.** GQ34 renders a year as "2,017". GQ11 (scored pass) says "לדירה … יש מחסן: אין".

### Earlier rounds, same items

- **GQ33 r3:** "the oldest building" = 2017.
- **GQ34 r4:** 0 buildings after 2010 (H1 2017 missing).
- **GQ50 r4:** H2 listed as having no elevator. It has one.
- **GQ56 r4 and r5:** "שינוי ייעוד" of a neighboring plot given as a zoning value.
- **GQ29 r3:** 2.00 "unit" as an average balcony *area*.

---

## Appendix: replay records

| replay | file | calls |
|---|---|---|
| safe-room on H3 | `out/ex_saferoom_H3.txt` | 3 |
| balcony area on H4v2; balcony count on H5 | `out/ex_balconyarea.txt`, `out/ex_balconycount.txt` | 6 |
| storage on D11, D6; yard on H5 | `out/ex_storage.txt`, `out/ex_yard.txt` | 9 |
| year built on H3, H2 | `out/ex_yearbuilt.txt` | 6 |
| zoning on D8, D12; unused rights on H6, H4v2; renovation (text) on H2, H3 | `out/ex_text_attrs.txt` | 18 |
| interpretation: GQ33, GQ43, GQ40, GQ22.2, GQ48.3, GQ49.2, GQ55.2 | `out/interpret.txt` | 21 |
| compose GQ24 (answer + layer 1), GQ20 (answer + layer 1 + judge) | `out/compose_gq24.txt`, `out/compose_gq20.txt` | 9 |
| locate (no model) GQ43, GQ49.2, GQ50, GQ57 | `out/locate.txt` | 0 |
| computation audit (no model) | `out/audit.txt` | 0 |

The scripts sit next to these outputs: `replay_extract.py`, `replay_interpret.py`, `replay_compose.py`,
`replay_locate.py`, `audit.py`, `prolif.py`, `fails2.py`, `steps.py` and `ans.py`.
