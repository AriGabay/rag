# Failure analysis: real-model round 7 (regression set) and held-out v2 run 1

This diagnoses every failing turn of two real-model runs on commit `580eb29`, using OpenAI `gpt-5.4-mini` with office A in cloud mode:

- **Run A** is the regression set `questions_general.yaml`, run on 2026-10-06 at 13:16 UTC. It passed 46 of 60 items and 55 of 69 turns, so **14 turns failed**. Report: `real-model-sample.md`.
- **Run B** is the held-out set v2 `questions_holdout_v2.yaml`, run at 13:23 UTC. It passed 32 of 65 items and 34 of 75 turns, so **41 turns failed**. Report: `real-model-sample-holdout-v2.md`.

Nothing under `backend/app`, `frontend`, `tests`, `eval` or `scripts` was changed. No data was written.

## Method

- **Stored turns.** For each failing turn I read the result file's `raw.plan` (`model_plan` and the server's `turn_plan`), `raw.steps`, the full answer (claims, sources, `audit`, coverage, limitations) and the scorer facets that failed. The dumps are in `scratchpad/diag7/fails_A.txt` and `fails_B.txt`.
- **Facts and ledger.** I read the `facts` and `fact_extraction_ledger` rows (extraction version `x7`) of every attribute a failing computation used, including the per-document rejection reasons in `detail.reasons`. Script: `diag7/led.sh`.
- **Ground truth.**
  - Run A: `tests/fixtures/ground_truth.yaml` → `general_facts`.
  - Run B: `tests/fixtures/holdout_v2_truth.yaml`.
- **Code reading, to confirm each deterministic mechanism.** These paths were read directly:
  - `facts.validate_mention` / `units.convert` (unit dimensions);
  - `units.parse_mention_quantity` (number words);
  - `facts.value_predicate` (boolean filters);
  - `plan._relation` and `plan.normalize_model_plan`;
  - `turn._two_sided` and `compose.compose_answer`;
  - `verify.check_claim` / `strip_label_numbers`;
  - the scorer, `eval/general.py`.
- **Deterministic replays, with no model call.** Each ran inside the backend container, with every transaction rolled back.
  - `locate_evidence` with the stored queries of GQ50, HV64 and HV65, plus control query sets.
  - `search_evidence` with the r6 and r7 queries of GQ41.
  - `units.parse_mention_quantity` / `convert` on the K2, K8 and cap-rate quotes.
  - The scorer's `fact_value_stated` on the HV27.1 claim.
  - SQL recomputation of the record-path figures GQ47.1 and GQ51.2.
- **Real-model replays: 25 provider calls in total.** All ran inside the backend container with transactions rolled back.
  - Interpretation, 15 calls. HV45, HV58.1 and GQ02 ran 3 times each; HV21, HV16 and HV42 ran twice each.
  - Composition, 7 calls. HV23: 2 answer calls. HV29: 1 answer call. GQ42: 2 answer calls and 2 judge calls.
  - Extraction, 3 calls: H8 × "שטח החצר".
  - Raw outputs: `diag7/interp1.txt`, `interp2.txt`, `replay_hv23.txt`, `replay_hv29.txt`, `replay_gq42.txt` and `ex_yard_H8.txt`.
- **Computation audit.** Every answer that carries an `audit` block was recomputed from the audit's own values (`diag7/audit.py` → `audit_A.jsonl`, `audit_B.jsonl`) and then compared with the truth file.
- **Primary-category rule.** A turn's primary category is the stage that produced the user-visible error. A facet that fails only on a plan label (task or relation), while the answer itself is right, goes under question understanding or conversation context. A label failure that sits next to a wrong or missing figure is listed as secondary.

Caveats:

1. The orchestrator rebuilt `backend` and `worker` at `f38f58d` during this analysis. That commit changes `interpret.py` / `plan.py`: a word shared with an option is no longer a reply, and a clarify plan with no question gets a server question. The interpretation replays therefore ran on HEAD code, not on `580eb29`. None of the classifications below depends on those two changes.
2. The attribute list the interpreter sees has grown since the runs. Handle numbers in the replays may differ from the runs.
3. The database changed after run B. Two facts of "אחוזי תכסית" were **verified by someone at 13:35 and 13:40**:
   - K1 "שיעור תפוסה 92%";
   - K3v2 "ניכוי של 5%".

   Both have role `other` and both are wrong for that attribute. They do not affect either run, but they will affect later runs.
4. **v2 seed impact on run A.** "All documents" now spans 28 current versions; Ramat Gan spans 16 including K4. I checked every run-A computation. No K document holds a used value. K2 is `awaiting_review` in GQ38, and K4 is `awaiting_review` in GQ56, which passed. **No run-A failure is caused by the v2 seed.**

---

## Summary

### Run A: regression set, 14 failing turns

| primary category | turns | items |
|---|---|---|
| question understanding | 4 | GQ02.1, GQ17.1, GQ43.1, GQ49.2 (in all four the answer itself is correct; only the task label fails) |
| data extraction | 3 | GQ38.1, GQ39.1, GQ40.1 |
| wrong scorer / answer key | 2 | GQ13.1, GQ18.1 |
| document retrieval | 2 | GQ22.2, GQ50.1 |
| computation | 1 | GQ33.1 |
| verification | 1 | GQ42.1 |
| composition / wording | 1 | GQ41.1 |
| conversation context | 0 | — |
| harness / data (v2 seed) | 0 | the seed changed no expected run-A result |

### Run B: held-out v2, 41 failing turns

| primary category | turns | items |
|---|---|---|
| question understanding | 15 | HV03, HV16, HV21, HV27.2, HV28, HV35, HV38, HV42, HV45, HV55.1, HV56.2, HV58.1, HV59.1, HV60.1, HV62 |
| data extraction | 12 | HV31, HV32, HV33, HV34, HV37, HV39, HV43, HV53.1, HV53.2, HV53.3, HV54.1, HV54.2 |
| verification | 4 | HV11, HV23, HV29, HV30 |
| conversation context | 4 | HV57.2, HV58.2, HV59.2, HV60.2 |
| document retrieval | 2 | HV64, HV65 |
| wrong scorer / answer key | 2 | HV27.1, HV55.2 |
| computation | 1 | HV41 |
| harness / data | 1 | HV40: K8 was still pending when the harness asked again; it is now extracted (45) |
| composition / wording | 0 (secondary only) | HV29/HV30 extractive fallback, HV31 "E1" names, HV58.2 claim dump |

### Systemic defects, ranked by turns affected

| # | defect (general class) | stage | deterministic? | turns |
|---|---|---|---|---|
| S1 | A `ratio` unit dimension never accepts a `%` value. `units.convert` raises `DimensionMismatch("percent measures percent, not ratio")`. The interpreter offers both `percent` and `ratio`, and chose `ratio` for cap rate, depreciation and occupancy | extraction validation | yes | HV31, HV33, HV37, HV45 (secondary), HV54.1, HV54.2 |
| S2 | Locate treats query variants as alternatives, so one un-negated variant ("מעלית", "אובדן הכנסות", "\"זיקות הנאה\"") admits every document that states the positive. A quoted variant also loses its negation word: the topic terms of `"אין זיקות הנאה"` are `['זיקות','הנאה']` | retrieval (locate) | yes, given the queries | GQ50.1, HV64, HV65: three wrong document lists |
| S3 | Layer 1 rejects a claim that names its document by the year in the document's title ("במסמך 2023", "במסמך משנת 2024"). The year is not in the cited passage, so the claim fails with `unsupported_number`. `strip_label_numbers` covers only labels of compare evidence | verification | yes (all 5 replayed claims) | HV11, HV23, HV29, HV30 |
| S4 | Number words are parsed only for the `count` dimension. "שישה מטרים", "עשר שנים", "שתי קומות" and "קומה אחת" all fail with `value_not_in_quote` | extraction validation | yes | HV34, HV39, GQ39, GQ40 (partly) |
| S5 | Boolean facts are stored as raw text ("אחת", "שתי", "75%", "רשומה", "לא רשומות") and never canonicalized. `value_predicate` compares that text with "true"/"false", so **every boolean `=`/`!=` filter counts 0** | computation (typing) | yes | HV41, HV38, and HV56.2 values |
| S6 | Nothing checks the model's attribute handle against its own description. HV45 used A34 "שיעור הפחת" for vacancy deduction in 3 of 3 replays. HV03 used A3 "מחיר למ״ר" (sale price per m²) for rent per m² | interpretation → server | model stable; server has no check | HV45, HV03 |
| S7 | `plan._relation` handles `change_clarification` with nothing pending in three ways that each misfire:<br>• "ובפתח תקווה?" → `topic_change`, because it compares free-text `topic` strings that contain the old city;<br>• a metric-only change ("והגבוה ביותר?") → `new_question`;<br>• a copied attribute plus a stated condition → `follow_up`, even after "שאלה חדשה:" | conversation context (server mapping) | yes, given the label | HV53.2, HV53.3, HV54.2, HV57.2 (HV57.2 is a wrong answer) |
| S8 | A server rule turns "answer" with metric `values` and a named attribute into `compute`. "Which documents …" questions become value lists | plan normalization | yes | HV16, HV56.2 |
| S9 | Two different properties at one street address ("הרצל 15" in Holon and in Petah Tikva) are treated like one property appraised twice (`EntityScope.ambiguous` → two-sided answer). The system never asks the referent question (R18) | question understanding / entity resolution | yes (3 of 3 replays, no clarify) | HV58.1, HV60.1, plus HV58.2 and HV60.2 inherited |
| S10 | The unit is taken from a column header "(₪)" while the row label says "למ״ר לחודש". There is no period dimension, so ₪/year cannot be told from ₪/month | extraction validation | yes | HV32 (wrong figure), HV55.1 |
| S11 | Min/max excludes a conflicted entity even when every one of its values beats the extremum. The answer names the next document as the oldest | computation | yes | GQ33.1 (wrong figure) |
| S12 | A scoped search with no supported passage abstains as `not_found` ("no relevant passages") instead of saying the named document does not state the datum (`not_stated`) | composition (abstention kind) | yes, given the queries | GQ41.1 |
| S13 | Cost-approach "replacement building" values are extracted with role `other` and go to review. "7.2 אלף ₪ למ״ר" parses as `thousand_ILS` (currency), not currency per area | extraction | yes | HV43, HV53.1–3 |

---

## Run A: every failing turn

| turn | question (short) | primary | secondary | evidence | where results vary |
|---|---|---|---|---|---|
| GQ02.1 | parking spaces at שינקין 18 | question understanding | — | The plan was `compute` / `values` on A7, so the scorer saw task `compute`. The answer is correct: "1" (H4v2 p1, "לדירה צמוד מקום חניה אחד") | Interpretation task label. r3–r6 planned `answer`. Replays: the model said `answer` 3 of 3, and the server kept `answer` 3 of 3 |
| GQ13.1 | which documents mention ממ"ד | wrong answer key | — | H3 p2 is listed: "המרחב המוגן והשדרוג שבוצע בדירה מוסיפים לסחירותה". It does mention the protected space. The key's `also_valid` has H3 p1 only | c08c741 now merges variant passages, so H3 p2 appears; r6 did not return it |
| GQ17.1 | why the balcony reduction (המאבק 25) | question understanding | — | The model planned `compute_explain`, with an `explain_previous` step the server dropped. The answer is correct (H5 p2) | Interpretation task label. r3–r6 planned `answer` |
| GQ18.1 | why limited contribution (המעגל 7) | wrong answer key | — | The claim gives the reason, citing H2 p1: "היתרה שייכת לכלל בעלי הדירות". The key also demands the "60 m2" figure, which a "why" answer does not need | Answer-model wording. In r6 the claim happened to include 60 |
| GQ22.2 | which assumptions changed between versions | document retrieval | — | The compare evidence was 4 passages per side: header, description, plan status and street. The "6. שיקולי השמאית" chunks, which hold the adjustment rate (6% in H4 → 10% in H4v2), were not selected. The answer lists planning status and dates correctly but omits the adjustment rate | Plan queries ("…הנחות") × the per-side evidence cap. Passed in r3 |
| GQ33.1 | oldest building, and when built | computation | data extraction (H2 "אוכלס בשנת 2015" → `needs_review`) | Audit: H6 = 1958 and H7 = 1962 share key `bp:6960/52/8`, so both were marked `conflict` and excluded. min over {2017, 2004, 1972, 1968, 2012} = **1968 (H5)**. Truth: 1958, n = 8, with the conflict shown. **Wrong figure** | Deterministic given the facts. The plan varied across rounds (clarify in r5/r6) |
| GQ38.1 | mean yard area, garden apartments | data extraction | composition (the content part states 85 while coverage says H8 does not state it) | Ledger for H8: `not_stated {quote_not_found: 1}`. Replay, 3 runs: run 1 accepted the sentence quote "חצר בשטח 85 מ״ר". Runs 2–3 quoted the table cell "חצר 85", which was rejected `unit_dimension`: the row chunk "חצר \| 85 \| בהצמדה בלעדית" carries no header unit | Extraction model's choice of quote: 1 of 3 accepted. Passed in r5 |
| GQ39.1 | remaining building rights per appraisal | data extraction | — | H2 "כ-60 מ״ר" and H6 "2.5 קומות" went to review (assumed unit). H8 "שתי קומות" and H4v2 "קומה אחת" were rejected for number words (S4). H1 "מומשו במלואן" (none) was rejected `value_not_in_quote`. The attribute has no dimension | Stable defect (S4); the attribute definition differed across rounds |
| GQ40.1 | renovations from 2020 on | data extraction | — | H2 "בשנת 2022 שופץ המטבח" → `value_not_in_quote`. H5 2018 → `value_not_in_quote` / `attribute_term_not_found`. H1 "לא בוצעו שינויים" was rejected. The model typed the attribute boolean and the server retyped it numeric because of the ≥ 2020 filter. Got 2 of 2 (H3, H7); truth is 3 of 5 | Extraction mentions; the attribute type varies by round |
| GQ41.1 | ceiling height at המעגל 7 | composition (abstention kind) | — | Deterministic replay: with the r7 queries (quoted, no "דירה") and the entity place terms, the topic terms are {גובה, התקרה, תקרה} and H2 has no supported passage, so 0 evidence and `not_found`. The r6 queries contained "דירה", which gave incidental support, so the answer model said "not stated" and passed | Interpretation query words. The defect (S12) is deterministic |
| GQ42.1 | mamad area at הירדן 30 | verification (absence detection) | — | The answer model returned `insufficient=True` plus a value claim about another datum ("שטח דירה 73 מ״ר … לא מזהים אותו כממ״ד"). Layer 1 and the judge kept it, so the answer kind was `content`, not an abstention. Replay 2 of 2: the same shape (claims about 73 m² and 95 m² next to an absence claim) | Answer-model output is stable in shape; whether it is reproduced depends on the evidence. Passed in r6 |
| GQ43.1 | what is written on a pool, שינקין 18 | question understanding | — | Plan `locate`, expected `answer`. The abstention is correct ("«בריכה» לא נמצאו") | Task label alternates locate/answer across r3–r7 |
| GQ49.2 | (topic change) which appraisals mention a building permit | question understanding | — | Plan `answer`, expected `locate`. The documents are exactly right: H2, H8, H5. The city and attribute were cleared | Task label: locate in r3–r6, answer in r7 |
| GQ50.1 | where it says there is no elevator | document retrieval | — | Deterministic replay of the stored queries ["אין מעלית בבניין", "בניין ללא מעלית", **"מעלית"**]: the bare variant fully supports D2, D3, H1, H2, H3, H4v2 and H5. All of them state the building **has** an elevator. With the r6 query set the result is only D1v2, H6, H8. **Wrong documents listed** | Interpretation added a bare variant; S2 is deterministic |

## Run B: every failing turn

| turn | question (short) | primary | secondary | evidence | where results vary |
|---|---|---|---|---|---|
| HV03 | tenant's rent per m² per month (הברזל 31) | question understanding | — | The model chose handle A3 "מחיר למ״ר" (structured, sale price) with `data_kind=asking_price`, so `compute_records` found 0 records. The content claim "85 ₪ למ״ר לחודש" is correct (S6) | Model handle choice |
| HV11 | maintenance score, הפלדה 12, 2023 appraisal | verification | retrieval scope: the year-2023 chip was shown, but the entity scope searched both K5 and K6; property_type misread as `office` | `provider extractive`, `dropped 1`. The S3 mechanism was reproduced on the same documents (HV23 and HV29 replays). The fallback bullets do show "ציון תחזוקה \| 3" from K5, but the scorer needs a claim | Stable (S3) |
| HV16 | which appraisals have a warning note | question understanding | — | The server turned the plan into `compute` / `values` (S8). Replays: the model said compute 1 of 2 and answer+values 1 of 2, and the server made both compute. The content (K2 one, K7 none, K8 two) is correct | Model label varies; the server rule is deterministic |
| HV21 | why a vacancy deduction was added (סוקולוב 60) | question understanding | — | Model `compute_explain`; the answer is correct (5%, the tenant's notice). Replays 2 of 2: model `abstain`, and the server turned that into `answer` | Interpretation task label |
| HV23 | basis of the 30% depreciation (הפלדה 12) | verification | composition (extractive fallback) | Replay 2 of 2: "במסמך 2023 נקבע פחת בשיעור 30% …" → layer 1 `unsupported_number` (2023 is not in E1). A second run also lost two more claims ("במסמך 2024 …") (S3) | Stable |
| HV27.1 | cap rate, סוקולוב 60 | wrong scorer | — | Claim: "…ההכנסה הוונה בשיעור של 7.25%" citing K3v2 p1. Correct. Replay of `fact_value_stated` returns False: the naming words are only ('היוון',); "הוונה" does not match; the nearest attribute word "ההכנסה" names `vacancy_allowance`, so `_belongs` is False | Deterministic scorer |
| HV27.2 | what else changed between versions | question understanding | wrong scorer (the same `_belongs` defect fails `changed: cap_rate`) | Plan task `answer` with compare steps; the server ran compare twice. The answer correctly gives cap rate 6.75 → 7.25 and vacancy 0 → 5% | Model task label |
| HV28 | did the vacancy deduction differ between versions | question understanding | — | The model planned `abstain` with no steps; the server turned it into `answer` with a search over the current version only. The answer gives v2's 5% and says the July version is missing | Model task label (the HV21 replays show `abstain` is common for this phrasing) |
| HV29 | plot area, הפלדה 12 | verification | composition: the extractive fallback showed headers, not the "2,400" / "2,450" rows | Replay 1 of 1: both claims ("במסמך משנת 2023 … 2,400", "במסמך משנת 2024 … 2,450") → `unsupported_number` (S3) | Stable |
| HV30 | is there a conflict on plot size | verification | composition | Compare: `dropped 2`, `incomplete`, `missing_sides` = both. Same mechanism as HV29 | Stable |
| HV31 | mean cap rate | data extraction | composition: "בשומה E1" wording; header "המידע אינו נאמר במפורש" followed by a stated 6.5% | Ledger: K1, K6 and K3v2 rejected `unit_dimension`; K2 `quote_not_found`; K4 0 mentions. Attribute `cap rate`, dimension `ratio` (S1) | Interpreter's dimension choice (ratio vs percent); S1 is deterministic |
| HV32 | mean monthly rent | data extraction | — | K2 fact, auto-validated: "דמי שכירות למ״ר לחודש (₪) 62" taken as a monthly rent (header "(₪)"; the "למ״ר לחודש" in the label is ignored). K2's annual 1,450,800 ₪ was not normalized. **Wrong figure 31,322.40**; truth is 55,490 (S10) | Deterministic |
| HV33 | count of cap rates > 7% | data extraction | — | As HV31 (S1). 0 found | — |
| HV34 | narrowest access road | data extraction | — | K8 "ברוחב שישה מטרים" → `value_not_in_quote` × 2 (S4). The answer says **min 12 m, completeness "complete"**; truth is 6 m (K8). **Wrong figure** | Deterministic |
| HV35 | where the loudest noise was measured | question understanding | — | Plan `answer`, expected `compute`. The answer is correct: 72 dB at K8, plus 68 dB and 55 dB | Model task label |
| HV37 | mean depreciation, cost approach | data extraction | — | `שיעור הפחת`, dimension `ratio`. K2, K3v2, K5 and K7 rejected `unit_dimension` (S1) | — |
| HV38 | count of properties with occupancy not full | question understanding | computation (S5) | The model typed occupancy as **boolean** "התפוסה" = false. Facts are raw "75%" (K2) and "100%" (K4), so the filter compares text and both are `filtered_out`. **Wrong figure 0**; truth is 2 (K1 92%, K2 75%) | Model typing; S5 is deterministic |
| HV39 | mean lease term | data extraction | wrong scorer (unit month vs year: 36 months equals K4's 3 years) | K1 "5 שנים" and K6 "7 שנים" went to `needs_review` (synonym). K3v2 "עשר שנים" was rejected for number words (S4). Mean 36 months over n = 1 | — |
| HV40 | which coverage percentages appear | harness / data (timing) | — | At answer time K8 and K3v2 were `not_yet_extracted` (2 pending after the re-ask). The ledger now shows K8 `found` at 13:32:54 with 45%, auto-validated. The engine reported the gap correctly | Background-job timing versus the harness's single re-ask |
| HV41 | count of properties with ≥ 1 warning note | computation | data extraction (K7 "לא רשומות … הערות אזהרה" → `value_not_in_quote`) | Boolean attribute with filter `= true`. Facts are raw "אחת" (K2) and "שתי" (K8), both `filtered_out`. **Wrong figure 0**; truth is 2 of 3 (S5) | Deterministic |
| HV42 | mean rent per m² in office buildings | question understanding | composition: the clarification text says "שכ״ד למ״ר" but its option maps to A3 "מחיר למ״ר" (sale price) | Model `clarify` (key attribute), with options A31 monthly rent / A3 price per m² / other. Replays: the model asked an attribute clarification 2 of 2 | Stable |
| HV43 | mean construction cost per m² | data extraction | — | K5 "4,200 ₪ למ״ר בנוי" and K7 "6,800 ₪ למ״ר בנוי" were extracted with role **other** ("מבנה חדש דומה" / "בניין חלופי") → `needs_review`, not counted. K8 "7.2 אלף ₪ למ״ר" → `unit_dimension`, because "אלף ₪" wins as currency (S13). 0 found | Deterministic, except the role choice |
| HV45 | vacancy deduction rate per appraisal | question understanding | data extraction (S1: the attribute is a ratio too) | Handle A34 is "שיעור הפחת", depreciation. The audit says attribute "שיעור הפחת". Replays 3 of 3: model `handle A34`, description "שיעור הניכוי בגין אובדן הכנסות" (S6). The content claims (K1 0, K2 10%, K3v2 5%) are correct | Stable |
| HV53.1 | mean construction cost, Holon | data extraction | — | As HV43; scope = 3 (K5, K6, K8) | — |
| HV53.2 | "ובפתח תקווה?" | data extraction | conversation context (`topic_change` via the topic-string compare, S7; a "cleared: attribute" chip is shown) | K7 role `other` → not counted; scope K2, K7 | — |
| HV53.3 | "ובלי הגבלה לעיר?" | data extraction | conversation context (S7) | As HV43 | — |
| HV54.1 | lowest occupancy | data extraction | harness (14 still pending at the re-ask) | Attribute "התפוסה", dimension `ratio`. After the extraction finished (visible in HV54.2), 0 found: K1, K2, K4, K5 and K8 rejected `unit_dimension` (S1) | — |
| HV54.2 | "והגבוה ביותר?" | data extraction | conversation context (metric-only `change_clarification` → `new_question`, S7) | 0 found (S1) | — |
| HV55.1 | where rent is not CPI-linked | question understanding | data extraction (K2 62 as monthly rent, S10) | Plan: `compute` count on monthly rent with filter `!= "צמוד למדד"`. The server dropped the filter and counted 5 rent values. The content claim "K4 not linked" is correct, but the numeric part presents "5" and lists K2 = 62 as a monthly rent | Model plan |
| HV55.2 | "מאיפה המידע הזה?" | wrong scorer | — | `show_sources` re-showed exactly the 11 sources of turn 1 (E1–E11 are identical). The scorer compares with turn 1's **cited** sources only (K4) | Deterministic scorer |
| HV56.2 | (topic switch) which properties have an easement | question understanding | — | The model planned `answer` with locate/show_sources; the server made it `compute` / `values` (S8). The values are right but raw text: "זיקת הנאה למעבר כלי רכב", "רשומה", "לא רשומות" | Deterministic server rule |
| HV57.2 | (new question) office units at הברזל 31 | conversation context | — | The model labeled `change_clarification` and copied the previous attribute A33 "ציון התחזוקה" and the Petah Tikva city. With nothing pending, the same attribute and a stated condition, the server mapped it to `follow_up`. The numeric block shows **"ציון התחזוקה … 1 (פתח תקווה)" for K1, which is in Tel Aviv**. The claim "36 יחידות משרד" is correct | Model label; mapping deterministic (S7) |
| HV58.1 | plot area at הרצל 15 (two cities) | question understanding | — | No referent clarification. The answer covers both documents ("השאלה מתאימה ל-2 מסמכים"). Replays 3 of 3: no clarification; 2 of 3 even added `city=הרצליה`, which the grounded-places rule removes. The server's `_two_sided` treats the address as one ambiguous entity (S9) | Stable |
| HV58.2 | "התכוונתי לזה שבחולון" | conversation context (inherited from HV58.1) | composition (15 claims for a one-value question) | Nothing was pending, so `new_question`. The answer "1.8 דונם" is correct | — |
| HV59.1 | mean "percentage" in Petah Tikva | question understanding | — | The model picked A37 "אחוזי תכסית" without asking. The figure 37.5% is correct for coverage | — |
| HV59.2 | "התכוונתי לתכסית" | conversation context (inherited) | — | `change_clarification` → `follow_up` (nothing pending). The result 37.5 is correct | — |
| HV60.1 | front setback at הרצל 15 | question understanding | — | As HV58.1 (S9) | — |
| HV60.2 | (new question while a clarification "should" be open) | conversation context (inherited) | — | Nothing was pending. `change_clarification` became `topic_change` because the attribute differs. The answer 21,850 ₪ is correct | — |
| HV62 | which property is not let | question understanding | — | Plan `answer`, expected `locate`. The answer is correct: K5 | — |
| HV64 | which appraisal says there are no easements | document retrieval | — | Stored queries are all quoted. Locate topic terms = ['זיקות','הנאה']: the negation is lost. K5 and K7 (easement **registered**) are listed with K8. K8's snippet is the fragment "הנאה.". **Wrong documents** (S2) | Model quoted its queries; S2 deterministic |
| HV65 | where no vacancy deduction was considered | document retrieval | — | Replay: the variants "ניכוי בגין אובדן הכנסות" / "אובדן הכנסות" admit K2 (10%) and K3v2 (5%). The negated variant alone returns only K1. **Wrong documents** (S2) | Deterministic given the queries |

---

## Computation audits

All values in both runs are `preliminary` (`auto_validated`). No figure had verified facts (`main n = 0` everywhere). `duplicates_merged` = 0 in every audit.

Column meanings:

- **scope** is `in_scope / unknown_metadata`.
- **arith** says whether the arithmetic is correct on the observations used. Recomputed values are mean (half-up to 0.01), min, max, count after the filter, and values.
- **vs truth** says whether extraction found the needed data.

### Run A

| turn | attribute — op (unit) | scope | observations used | missing by state | completeness | shown | recomputed | arith | vs truth |
|---|---|---|---|---|---|---|---|---|---|
| GQ02.1 ✗ | parking spaces — values (unit) | 1/0 | H4v2=1 | — | complete | [1] | [1] | ✓ | ✓ (H4v2-F07 = 1) |
| GQ28.1 ✓ | גודל ממ״ד — mean (sqm) | 16/0 (K4 in scope, not stated) | H1=12, H2=9.5 | awaiting_review H3; not_stated 13 | subset | 10.75 | 10.75 | ✓ | ✓ settled key (H3 review by design) |
| GQ29.1 ✓ | שטח המרפסות — mean (sqm) | 3/0 | H5=7 | awaiting_review H4v2; not_stated 1 | subset | 7.00 | 7.00 | ✓ | ✓ settled |
| GQ30.1 ✓ | גובה התקרה — mean (m) | 28/0 | H1=2.75, H3=2.60, H5=2.80, H6=3.05 | not_stated 24 | complete | 2.80 | 2.80 | ✓ | ✓ |
| GQ31.1 ✓ | parking — mean (unit) | 3/0 | H5=0, H4v2=1 | not_stated 1 | complete | 0.50 | 0.50 | ✓ | ✓ |
| GQ32.1 ✓ | שטח המחסן — mean (sqm) | 28/0 | H1=5, H6=4, H4v2=7 | not_stated 25 | complete | 5.33 | 5.33 | ✓ | ✓ |
| GQ33.1 ✗ | שנת הבנייה — min (year) | 28/0 | H1=2017, H3=2004, H8=1972, H5=1968, H4v2=2012 | awaiting_review H2; **conflict H6=1958, H7=1962**; not_stated 20 | subset | 1968 | 1968 | ✓ on the facts used, but **excluding a conflicted entity from a min is wrong** (S11) | ✗ truth 1958, n 8; H2 2015 is in review |
| GQ34.1 ✓ | שנת הבנייה — count > 2010 | 16/0 | H1=2017 | awaiting_review H2; filtered_out H3=2004, H8=1972; not_stated 12 | subset | 1 | 1 | ✓ | ✓ settled |
| GQ35.1 ✓ | גודל ממ״ד — count < 11 | 16/0 | H2=9.5 | awaiting_review H3; filtered_out H1=12 | subset | 1 | 1 | ✓ | ✓ settled |
| GQ36.1 ✓ | מספר המרפסות — mean | 3/0 | H4v2=2 | not_stated 2 | complete | 2.00 | 2.00 | ✓ | ✓ settled |
| GQ38.1 ✗ | שטח החצר — mean (sqm) | 28/0 | — | awaiting_review D6 (plot 300), K2 (plot 3,050); not_stated 26 incl. **H8** | insufficient | — | — | n/a | ✗ truth 85 (H8): quote and cell-unit rejection |
| GQ39.1 ✗ | יתרת זכויות — values (none) | 28/0 | — | awaiting_review H2 (60 m², assumed), H6 (2.5 floors, assumed); not_stated 26 incl. H1, H4v2, H8 | insufficient | — | [] | ✓ | ✗ truth n 5 (number words, "none") |
| GQ40.1 ✗ | שיפוץ — count ≥ 2020 (none) | 28/0 | H3=2020, H7=2023 | awaiting_review D2; not_stated 25 incl. H1, H2, H5 | subset | 2 | 2 | ✓ | ✗ truth 3 of 5 (H2 2022 missed) |
| GQ45.1 ✓ | גודל ממ״ד — mean (yossi) | 1/0 | — | not_stated 1 | complete | — | — | n/a | ✓ abstain |
| GQ48.1–3 ✓ | גובה התקרה — mean | 16 / 3 / 3 | H1, H3 → 2.68; H5 → 2.80; H6 → 3.05 | — | complete | as recomputed | — | ✓ | ✓ |
| GQ49.1 ✓ | שטח המרפסות בדירה — values | 3/0 | H5=7 | awaiting_review H4v2 | subset | [7] | [7] | ✓ | ✓ settled |
| GQ56.1 ✓ | ייעודי קרקע — values (text) | 15/0 | H1=מגורים ג׳, H2=מגורים ב׳, H3=מגורים ב׳ | awaiting_review D1v2, D3, D5, D8, **K4** | subset | distinct [ב׳, ג׳] | same | ✓ | ✓ (the K4 review does not change the key) |
| GQ60.1 ✓ | גובה התקרה — count > 2.70 | 28/0 | H1, H5, H6 | filtered_out H3=2.60 | complete | 3 | 3 | ✓ | ✓ |
| GQ47.1 ✓ (records) | price per m², חרוזים 2023 | records | 3 records: 24,225.35; 29,473.68; 25,000 | — | — | mean 26,233.01; weighted 26,099.29; median 25,000 | SQL: the same three values | ✓ | ✓ |
| GQ51.2 ✓ (records) | price per m², חרוזים 2024 gross | records | 10 apartment, VAT-included, verified records | — | — | mean 26,474.68; median 26,218.75 | SQL sum 264,746.80 / 10; median (25,937.50 + 26,500) / 2 | ✓ | ✓ |

### Run B

| turn | attribute — op (unit) | scope | observations used | missing by state | completeness | shown | recomputed | arith | vs truth |
|---|---|---|---|---|---|---|---|---|---|
| HV09.1 ✓ | cap rate — values (ratio), K2 | 1/0 | — | not_stated 1 (unit_dimension) | complete | — | [] | ✓ | answered by search (7.25) |
| HV16.1 ✗ | הערת אזהרה — values (boolean) | 28/0 | K2="אחת", K8="שתי" | awaiting_review D2; not_stated 25 incl. **K7 (states none)** | subset | [אחת, שתי] | same | ✓ | values are raw text (S5) |
| HV31.1 ✗ | cap rate — mean (**ratio**) | 28/0 | — | not_stated 28 (K1, K6, K3v2 unit_dimension; K2 quote_not_found) | **complete** | — | — | n/a | ✗ truth 7.15, n 5 (S1) |
| HV32.1 ✗ | דמי שכירות חודשיים — mean (ILS) | 28/0 | K1=35,700; **K2=62**; K4=21,850; K6=72,000; K3v2=27,000 | not_stated 23 | complete | **31,322.40** | 156,612 / 5 = 31,322.40 | ✓ on the facts, but **K2 is rent per m²** | ✗ truth 55,490 (K2 = 120,900/month) |
| HV33.1 ✗ | cap rate — count > 7 (ratio) | 28/0 | — | not_stated 28 | complete | — | 0 | n/a | ✗ truth 3 of 5 |
| HV34.1 ✗ | דרך הגישה — min (m) | 28/0 | K2=20, K5=12 | not_stated 26 incl. **K8 (6 in words)** | **complete** | **12** | 12 | ✓ | ✗ truth 6 |
| HV36.1 ✓ | ציון התחזוקה — mean | 28/0 | K1=4, K2=3, K4=4, K5=3, K7=4 | not_stated 23 | complete | 3.60 | 18 / 5 = 3.60 | ✓ | ✓ |
| HV37.1 ✗ | שיעור הפחת — mean (ratio) | 28/0 | — | not_stated 28 (K2, K3v2, K5, K7 unit_dimension) | complete | — | — | n/a | ✗ truth 25 (S1) |
| HV38.1 ✗ | התפוסה — count = false (boolean) | 28/0 | — | awaiting_review D1v2, D2, D5, H1, H3, H6, H7, K3v2; **filtered_out K2="75%", K4="100%"**; not_stated 18 incl. K1 (role other) | subset | **0** | 0 | ✓ as coded, but the text "75%" ≠ "false" | ✗ truth 2 of 4 (S5, model typing) |
| HV39.1 ✗ | משך השכירות — mean (month) | 28/0 | K4=36 (3 years × 12) | awaiting_review K1 (5 y), K6 (7 y); not_stated 25 incl. K3v2 (words) | subset | 36.00 months | 36 | ✓, conversion correct | ✗ truth 6.25 y over n 4 |
| HV40.1 ✗ | אחוזי תכסית — values (percent) | 28/0 | K2=40, K5=60, K7=35 | **not_yet_extracted K3v2, K8** | subset | [35, 40, 60] | same | ✓ | ✗ only because K8 was pending (now 45) |
| HV41.1 ✗ | הערת אזהרה — count = true (boolean) | 28/0 | — | awaiting_review D2; **filtered_out K2="אחת", K8="שתי"**; not_stated K7 | subset | **0** | 0 | ✓ as coded, but text ≠ "true" | ✗ truth 2 of 3 (S5) |
| HV43.1 ✗ | עלות ההקמה למ״ר — mean (ILS/sqm) | 28/0 | — | not_stated 28: K5, K7 found but role `other` → review, not counted; K8 unit_dimension | complete | — | — | n/a | ✗ truth 6,066.67, n 3 (S13) |
| HV45.1 ✗ | **שיעור הפחת** (wrong attribute) — values | 28/0 | — | not_stated 28 | complete | — | [] | n/a | ✗ truth 0 / 10 / 5 % (S6, S1) |
| HV51.1 ✓ | cap rate — mean (yossi) | 1/0 | — | not_stated 1 | complete | — | — | n/a | ✓ abstain |
| HV53.1 / .2 / .3 ✗ | עלות ההקמה למ״ר — mean | 3 / 2 / 28 | — | not_stated all (as HV43) | complete | — | — | n/a | ✗ truth 5,700 / 6,800 / 6,066.67 |
| HV54.1 ✗ | התפוסה — min (ratio) | 28/0 | — | **not_yet_extracted 14**; not_stated 14 | insufficient | — | — | n/a | ✗ truth 75 (S1 after drain) |
| HV54.2 ✗ | התפוסה — max (ratio) | 28/0 | — | not_yet_extracted 5; not_stated 23 | insufficient | — | — | n/a | ✗ truth 100 |
| HV55.1 ✗ | דמי שכירות חודשיים — count | 28/0 | as HV32 (K2=62) | not_stated 23 | complete | 5 | 5 | ✓ | not a computation in the key |
| HV56.1 ✓ | מפלס הרעש — mean | 28/0 | K4=68, K7=55, K8=72 | not_stated 25 | complete | 65.00 | 195 / 3 = 65.00 | ✓ | ✓ |
| HV56.2 ✗ | זיקת הנאה — values (boolean) | 28/0 | K5, K7, K8 (raw text) | not_stated 25 | complete | 3 texts | same | ✓ | locate expected |
| HV57.1 ✓ | ציון התחזוקה — mean, Petah Tikva | 2/0 | K2=3, K7=4 | — | complete | 3.50 | 3.50 | ✓ | ✓ |
| HV57.2 ✗ | ציון התחזוקה — count, K1 | 1/0 | K1=4 | — | complete | 1 | 1 | ✓ | **wrong attribute and city** (S7) |
| HV59.1 / .2 ✗ | אחוזי תכסית — mean, Petah Tikva | 2/0 | K2=40, K7=35 | — | complete | 37.50 | 37.50 | ✓ | ✓ value; turn 1 should have asked |
| HV66.1 ✓ | ציון התחזוקה — mean (dana) | 27/0 | as HV36 | — | complete | 3.60 | 3.60 | ✓ | ✓ |

**Verdict.** In every audit of both runs, the arithmetic is correct on the observations it used, and so are both record-path figures. The computation stage still produced **four wrong results**, all through its rules rather than its arithmetic:

- **GQ33**: a min that excludes the conflicted entity (S11).
- **HV38 and HV41**: boolean filters over raw text (S5).
- **HV45 and every "not stated in 28" `complete` verdict**: `completeness()` returns `complete` with 0 observations whenever there are no gaps. A wholesale extraction failure is therefore reported as "complete; not stated in 28 documents".

---

## Answers that state something wrong

Partial answers and abstentions are not listed. The wrong statements fall into three classes.

**(a) A wrong figure or value presented as the result**

1. **GQ33.1.** "Oldest building: 1968 (H5)". בן יהודה 140 (H6 1958 / H7 1962) is older. Its values were excluded as a conflict, and the conflict was noted only as a count.
2. **HV32.1.** "Mean monthly rent 31,322.4 ₪". K2's **rent per m² (62)** is used as a monthly rent; the truth is 55,490.
3. **HV34.1.** "Narrowest access road 12 m", marked complete. K8 states 6 m.
4. **HV38.1.** "0" properties with occupancy not full. The truth is 2 (K1 92%, K2 75%).
5. **HV41.1.** "0" properties with a warning note. The truth is 2 (K2, K8).
6. **HV57.2.** The numeric block "ציון התחזוקה: 1 (פתח תקווה)" for K1, a Tel Aviv building, in reply to a question about office units. Wrong attribute and wrong city chip; the claim "36" is correct.
7. **HV55.1.** The numeric part "5 monthly-rent values" lists K2 = 62 (rent per m²) as a monthly rent. HV55.2 re-shows that list.

**(b) Documents listed that state the opposite of what was asked**

8. **GQ50.1.** D2, D3, H1, H2, H3, H4v2 and H5 are listed for "no elevator". All of them state an elevator exists.
9. **HV64.** K5 and K7 are listed for "explicitly no easements". Both state an easement **is** registered.
10. **HV65.** K2 (10%) and K3v2 (5%) are listed for "no vacancy deduction considered".

**(c) False "the datum is not stated" coverage lines**

The coverage line reports a document as not stating the datum when the document does state it.

- **GQ38.** H8 is reported as not stating the yard area, while the same answer's content part says 85 m² from H8.
- **GQ39.** H1, H4v2 and H8.
- **GQ40.** H1, H2 and H5.
- **HV31, HV33, HV37, HV43, HV45, HV53.1–3 and HV54.2.** "ב-28 (or 2–3) הנתון אינו מצוין", while 2–5 documents state the datum. HV31 adds the header "המידע המבוקש אינו נאמר במפורש" and then states 6.5%.
- **HV34.** K8.
- **HV39.** K3v2.

These come from extraction false negatives (S1, S4, S13), shown as `complete`.

Borderline, misleading but not false:

- **HV42.** The clarification text offers "שכ״ד למ״ר", but the option behind it is A3, sale price per m².
- **HV59.1.** A guessed attribute, "coverage", gives a correct figure.

---

## Where results vary

Each varying turn is located at a stage, with evidence. None is put down to unexplained noise.

| stage whose output varied | turns | evidence |
|---|---|---|
| Interpretation: task label | GQ02, GQ17, GQ43, GQ49.2, HV16, HV21, HV27.2, HV28, HV35, HV62 | GQ02: compute in r7, then answer in r3–r6 and in 3 of 3 replays. HV21: compute_explain in the run, abstain → answer in 2 of 2 replays. HV16: compute 1 of 2 and answer+values 1 of 2, with the server rule (S8) making both compute. GQ43 / GQ49.2: locate and answer alternate across r3–r7 |
| Interpretation: search-query wording | GQ41, GQ42, GQ50, HV64 | GQ41: r6 queries had "דירה" (incidental support → not_stated); r7 queries were quoted, without it (0 evidence → not_found). GQ50: r7 added a bare "מעלית" variant. HV64: all queries quoted, so the negation was lost |
| Interpretation: attribute handle, type or dimension | HV45 (A34, 3 of 3, stable), HV03 (A3), HV38 (boolean), HV31 / HV37 / HV54 (`ratio`, where coverage got `percent`) | The plans stored with the runs, and `interp1.txt` |
| Interpretation: relation label | HV53.2, HV53.3, HV54.2, HV57.2, HV59.2, HV60.2 | The model emitted `change_clarification` with nothing pending 6 times. The server mapping (S7) is deterministic |
| Extraction: quote choice | GQ38 (H8) | Replay: 1 of 3 accepted (sentence quote); 2 of 3 rejected (table-cell quote, no header unit). The run gave `quote_not_found` |
| Answer model: document naming | HV11, HV23, HV29, HV30 | Not variance. All 5 claims replayed named the document by its title year and were rejected (S3) |
| Answer model: absence plus off-topic value | GQ42 | 2 of 2 replays had the same shape: `insufficient` plus a claim about 73 m², kept → content |
| Background extraction timing | HV40, HV54.1 | `pending` / `not_yet_extracted` at answer time. Ledger timestamps run to 13:33:31; K8 coverage was found at 13:32:54 |
| Code change between rounds | GQ13 | c08c741 (merged variant passages) brought in H3 p2, which is valid; the answer key lacks it |

## Findings that contradict or qualify `code-review.md`

1. **"A change_clarification with nothing pending that only states a condition is a follow-up (GQ48.3)" — not reliably fixed.**
   - "ובפתח תקווה?" (HV53.2) became `topic_change`. `_relation` compares the free-text `topic` ("…בחולון" vs "…בפתח תקווה") before the condition rule, and a topic string carries its city.
   - A metric-only change, "והגבוה ביותר?" (HV54.2), becomes `new_question`.
   - A copied attribute plus a condition becomes `follow_up` even after "שאלה חדשה:" (HV57.2). That turn produced a wrong answer.
2. **Locate negation (#32, c08c741).** "A negation must govern its word" holds within a variant. Because variants are alternatives, a single un-negated variant still admits every positive document, and a quoted variant drops its negation word. The result is three wrong document lists: GQ50, HV64 and HV65.
3. **"Server label numbers" (68a5847) covers only compare labels.** In an ordinary two-document answer, the model names documents by the year in their title, and layer 1 rejects the claim. Four v2 turns failed this way, and the extractive fallback then shows header passages.
4. **"Numbers written as words" (#3/#5 tests) is limited to counts.** "שישה מטרים", "עשר שנים" and "שתי קומות" are rejected. This is not listed as a residual.
5. **Attribute canonicalization says unit dimensions must not conflict, but `percent` vs `ratio` is an unconvertible pair** offered to the interpreter. Every ratio attribute rejects every "%" value (S1). Not listed.
6. **"Text, boolean and date requests share one definition."** Boolean facts are never canonicalized to true/false, so boolean value filters always count 0 (S5). Not listed.
7. **The `other_attribute` rejection** did not stop "דמי שכירות למ״ר לחודש (₪) 62" from being auto-validated as a monthly rent. The header unit "(₪)" satisfied the currency check, and "למ״ר לחודש" in the label is not checked against the dimension (S10).
8. **Residual "two appraisals of one property are one entity; a differing value is a conflict sent to review".** For min/max this turns into a wrong extremum (GQ33). The truth note asks that both values be shown.
9. **Nothing validates a model-chosen attribute handle against the model's own description** (HV45, HV03). This is not among the residuals.
10. **The scorer changes (#10) introduced a false negative.** The value-to-attribute association (`_belongs`) rejects "ההכנסה הוונה בשיעור של 7.25%" for cap rate (HV27.1 and HV27.2 `changed`). The meta-sources check compares with the previous turn's cited sources only (HV55.2).
