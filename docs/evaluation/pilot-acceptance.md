# Pilot acceptance test and pilot gates

This document fixes, before any run, how the system is judged for a limited pilot in an appraisal office: which conversations are asked, how their references are written, what is measured and how, which material is exposed and which is still missing, and the rule that turns the measurements into one of three decisions. It is committed before the first acceptance run and is not edited after it. Anything that changes after a run is recorded as a new version of the test, and the results of the earlier version are not re-scored under it.

The questions, the references with their alternative readings, the recorded answers, the grades and the run records are customer data or derived from it. They stay outside git, under `real_documents/tests/pilot/` (a directory excluded from the repository). Only counts, rules and aggregate numbers appear here, with no addresses, names, quotes or values from any report.

The test does not claim the system is correct in general. It estimates how often it fails on a stated body of material, with an interval, and it names what that body does not cover.

## What the test decides

The round ends with exactly one of three decisions, taken by the gates below:

- **Allow a limited pilot** under the tested conditions (the material, the settings, the build and the models that were run).
- **Blocked** by a named defect.
- **Not enough evidence** to decide.

There is no fourth outcome and no partial "ready". A pilot that is allowed is limited to the tested conditions; the decision text names the material it was measured on.

## Categories and minimum counts

The ten categories below are the ones an appraiser asks about in daily work. A conversation has one primary category, which is the category it is counted in. It can also carry secondary tags (for example a table question that is also a follow-up), and secondary tags are reported but do not count toward a minimum.

| Category | What a conversation in it asks | Min. conversations | Min. turns |
|---|---|---:|---:|
| Facts | A value, a date, an area, a status, with its unit, period and basis | 12 | 18 |
| Standards questions | A professional concept or a requirement of a standard, answered with the standard's own conditions and exceptions, with and without a named property | 12 | 18 |
| Calculations | A result computed from values in the source, then a request to show how | 12 | 24 |
| Conditions and exceptions | A value that holds only under a condition, or an item that a negation or exception limits | 12 | 24 |
| Tables | A value, a row, a column or a set of rows from a table, including a table that continues over pages and a table in a scanned page | 12 | 18 |
| Comparisons | Two or more values, properties, appraisals or editions side by side | 12 | 24 |
| Multi-appraisal files | One file that binds several appraisals and appendices; the answer must be about the right appraisal | 12 | 24 |
| Time preference | The same datum in several appraisals of one property, with the "prefer current data" switch on and off, and a date or range stated by the user | 12 | 24 |
| Follow-ups | A reference back to the last answer: "the second", "both", "and the total?", "I meant the value" | 12 | 36 |
| Topic switch and return | Move to another property or topic and come back by reference | 12 | 48 |
| **Total** | | **120** | **258** |

**Why these numbers.**

- The unit of analysis is the conversation. Turns of one conversation are not independent, and counting them as if they were produced the unreliable noise bands of round 22. The paired cluster bootstrap resamples whole conversations, so the number of conversations, not the number of turns, sets the interval.
- Round 23 measured what about 60 conversations can show. With that many conversations the smallest paired difference detectable with 80% power was 56% to 182% of the control count for each rare error class. A set of 60 can therefore only rule out large effects, and most error classes were counted in single digits.
- 120 conversations double that sample. It narrows the intervals on the pooled counts that the gates use (correct, wrong and missing required items, failed turns). It does not make rare classes decidable: a 25% reduction in a class with five control errors would still need hundreds of conversations. Those classes are reported as "not decidable in advance", as in round 23, and no gate depends on them.
- Twelve per category means one conversation is at most 8.3% of a category, so a per-category count is not decided by a single case. Per-category figures are descriptive. No gate is evaluated per category, because 12 conversations per category cannot carry an interval.
- Turn minimums follow the shape of the category. Single-question categories need a question and a probe (units, a condition or the calculation steps); follow-ups need at least three turns; topic switch and return needs at least four (ask, switch, return by reference, one further question).
- The sampled gates (citations, continuity) have their own sample minimums below, drawn from these turns.

At least 120 conversations and 258 turns are needed in total. A smaller unseen set can run, and its results are reported, but a decision other than "not enough evidence" is not taken on it.

## How references are written

- **From the source, by reading it.** Each reference is written by a person who reads the source document, and records the exact place (document, page, table, row, column or paragraph). It is never copied, adapted or corrected from a system answer. No system answer exists for the unseen conversations when their references are written.
- **Required items, not model text.** A reference lists the items an answer must contain (each with its value, unit, period, basis, condition and place), the items that must not appear (a value of another property or appraisal, a value under the wrong condition) and, where the correct answer is "the source does not say", the place that was read to establish it.
- **Plausible alternative readings.** For each item the author lists every other reading a careful professional could defend: another table that holds the same figure, a value before and after a condition, a figure with and without VAT, another appraisal of the same property. Each alternative is marked acceptable, acceptable only if the answer says which reading it uses, or wrong. A calculation reference gives the accepted inputs, the accepted assumptions, the formula and the result with the precision to which it is accepted.
- **Graders never see the system answer as truth.** A grader receives the question, the source text of the cited and the referenced places, the reference with its alternatives, and the answer under test. It judges every claim in the answer against the source, not against the system's own statements. The system's own claim that an answer is complete, verified or sufficient is withheld from the grader.
- **Blind pairs.** For each turn the final and the control answers are presented as two unlabelled answers in a random order. The grader does not know which arm produced which, which build was run, or which settings were on. Items are counted by a script from the grader's structured output, not by the grader.
- **Opus adjudication.** Sonnet graders grade. Opus adjudicates every disagreement between graders, and every item graded as a reference error. It sees no arm label. A case that cannot be decided from the source is reported as undecidable and is not counted in either direction.
- **Second reading.** 20% of the conversations, chosen by a script before grading, are graded by two graders. Agreement is reported per metric as a kappa value. A metric whose kappa is below 0.6 is reported as unreliable and cannot support a gate; if a gate metric is unreliable the decision is "not enough evidence".
- **Reference errors.** If grading shows that a reference is wrong, the reference file is not changed. The item is marked "reference disputed", adjudicated and reported separately, and the same marking applies to both arms.
- **Freezing.** The question set, the references, the alternatives and the sources' file hashes are written with a sha256 before any run, and the hash is recorded in the round ledger. A question or reference that is changed after a run belongs to a new test.
- **Unseen references come from an appraiser.** The references for the unseen set are written by a practising appraiser who took no part in building the system and has not seen any system answer to these documents. A second appraiser, or the same appraiser a week later, re-reads a sample of 20% of the references against the source and the disagreements are settled before freezing.

## Metrics

The metrics are separate. They are never combined into one score, and the number of checks that pass is not a measure of quality. Each is graded per turn and reported with a paired interval against control.

| Metric | What is graded |
|---|---|
| Correctness | Each required item is correct, wrong, missing, or removed. "Removed" means the item was present in the draft or in the evidence and the verification step took it out. A wrong answer is an item or claim that contradicts the source or the reference. Missing items are split into missing silently and missing but named in the answer with the reason |
| Completeness | For a request for a set (members, rows, annual amounts, a value and its condition), every item the source gives for it is in the answer, or is named as not given with the reason. "Not found" is accepted only when the place that should hold the item was read in full |
| Property attribution | Each value is attributed to the property and appraisal unit asked about. A value of another property, another unit of the same file or another appraisal date is a wrong attribution |
| Conditions kept | A condition that the source ties to a value (a negation, "only", an exception, a status, a method, a limit in time or to a component) is kept with the value. A value given without a condition the source attaches to it is not given in full |
| Calculations | Graded in three parts: the inputs (right values from the right places), the assumptions (stated and acceptable), and the formula. The result is compared at the reference's precision. A correct result from wrong inputs fails the inputs part |
| Reference usefulness | For a sampled citation: opening it in the product's viewer lands on the cited place and highlights the cited text. A citation that opens a different place, no place, or shows no highlight fails |
| Conversation continuity | For a sampled follow-up or topic-return turn: the referent (list item, property, metric, unit, period, topic) is the one the user meant, and a return to an earlier topic restores that topic's state |
| Time and cost | Seconds and product-model USD per turn, from recorded costs and timestamps, reported as mean, median and 90th percentile |

**Timeouts and failures are part of the user-experience result.** A turn that fails, times out or returns empty counts as a failed turn. Every required item of that turn also counts as missing in correctness and completeness, so a system cannot improve by failing. A failure must show a clear message to the user; a failure without one is a defect named in the gates. Cost and time are computed on all turns, failed ones included.

**Sampling for the two checked metrics.** Citations and continuity turns are checked on a sample, drawn by a script with a fixed seed, stratified over the categories, before grading:

- At least 60 cited places, checked in the product's viewer with the display-frame highlighting. With 60 checks and no failure the one-sided 95% lower bound of the pass rate is about 95%; with failures it is lower, so 60 is the smallest sample at which the 95% threshold can be met with confidence.
- At least 60 follow-up or topic-return turns, drawn from the follow-up and the topic switch and return categories.
- If the whole set holds fewer than that many, all of them are checked and the gate is judged "not enough evidence" unless it fails.

## Regression and unseen material

The two kinds of material are never mixed in one figure.

**Regression material (exposed).** All the sets that existed before this test are exposed: `diag`, `v18`, `v20`, `v21` and `v22`, and any other set used to find or confirm a fix. Their questions or documents have shaped changes to the code, so a result on them can show protection against regressions and operational defects, and cannot show generalisation. They are labelled "regression" everywhere they appear, and their results are reported as in-sample. A set that is used to make a fix after the test is frozen becomes regression too.

**Unseen material (not yet available).** To make a claim beyond the exposed material the test needs documents that no earlier round used and that nobody on the project read to design a fix, with references written by an appraiser and frozen before the first run:

| Document type | Min. documents | Notes |
|---|---:|---|
| Residential appraisal reports | 6 | At least three distinct properties |
| Commercial or income-approach reports | 6 | Must hold tables of rates, areas and yields |
| Land, rights or development valuations | 4 | Conditions, exceptions and assumptions |
| Bound files holding several appraisals with appendices | 4 | At least one with an uncertain unit boundary |
| Scanned or mixed-quality PDFs | 4 | At least one with OCR-read tables |
| Appraisal standards and professional guidance | 4 | Includes two editions of one standard |
| **Total** | **28** | |

- **Time preference:** at least three properties, each with at least three appraisals of different valuation dates, among the documents above.
- **Variety:** documents from at least two authors or firms when available, so the set does not rest on one writing style.
- **Categories:** between them, the documents must support all ten categories at the minimums above, and every category must have conversations over more than one document type.
- **References:** written by an appraiser as described above.
- **Not in the data store:** the documents are not loaded into any office before the references are frozen.
- **Handling:** the documents live only in a private location under `real_documents/`, and are ingested into a test office that is separate from the office holding real reports.

Until this material exists, any acceptance run is a regression run. It can find defects and can block, and it cannot allow a pilot on grounds wider than the exposed material. The decision text must say so.

## Measurement protocol

- **Arms.** The final arm is the build with the round's settings. The control arm is the reference commit recorded in the round ledger, with the same settings except the ones being tested. Each run records the commit, the image ids, the settings, the models seen in the calls, and each document's ingestion version and reading id.
- **Identical conditions.** Both arms run against the same ingested documents, in the same environment, with the same models, the same product settings except the tested ones, the same concurrency, the same time limits, and in the same time window, so provider conditions apply to both. Every conversation starts from a new conversation with the same user.
- **Two repetitions.** The whole set is run twice per arm. The repetitions measure model variance. They are not extra clusters: both repetitions of a conversation belong to one cluster in the scoring.
- **Balanced order.** In repetition one the arm that goes first alternates by conversation, and in repetition two the order is reversed, so neither arm always runs first or second.
- **Provider failures.** A turn in which the provider returned a rate limit, an overload or a server error is a provider failure, and is told apart from the product's own timeouts and failures. A conversation with a provider failure in either arm is rerun in both arms, in the same repetition and under the same rules, until both arms complete it or three attempts have been made. The original files are kept. If a conversation cannot be completed in both arms it is dropped from both arms for that repetition and listed. If more than 10% of the conversations are dropped, the decision is "not enough evidence". Product timeouts and product failures are not provider failures and are never rerun.
- **Scoring.** The existing paired cluster bootstrap (the round 23 method, resampling conversations, 95% interval), applied to the difference between the arms in each count. "Not worse" means the interval does not show the final arm as worse. An interval that includes zero is reported as "not decided", never as an improvement.
- **Protective metrics versus target metrics.** End-to-end runs are protective measures. Error classes whose counts are too small to decide are reported as "not decidable in advance".
- **Order of the work.** Gates, categories, protocol and the hash of the frozen set are committed first. Then runs. Then grading. Grading does not start while a run is still being edited.

## Pilot gates

These are fixed before any run and are never changed after seeing results. The decision is taken in this order: blocked is checked first; then allow; otherwise not enough evidence.

**Allow a limited pilot** only when all of these hold:

- the acceptance end-to-end ran against control under identical conditions with two repetitions;
- correct required items, wrong answers and missing or removed required items are each not worse by the paired interval. Not worse means the 95% paired cluster-bootstrap interval of final minus control excludes a worsening larger than a pre-set margin of 5% of the control arm's count. An interval that includes such a worsening is not decided, and a not-decided guard cannot give "allow". The scorer is the private paired cluster bootstrap used since round 23 (10,000 resamples, clustered by conversation);
- failed or timed-out turns are ≤ 5% of turns;
- cited references open the right place with highlighting in ≥ 95% of sampled citations;
- follow-up and topic-return continuity is correct in ≥ 90% of sampled turns;
- the U6 walkthrough passed every step;
- no defect is open in permissions, data exposure or wrong-property attribution.

**Blocked** by any of:

- a permission or data-exposure defect;
- a report or diagnostics loss;
- wrong pairing in kept text above the E3 bound;
- a failure without a clear message;
- a walkthrough step that fails without a workaround.

**Not enough evidence:** neither condition holds, including when the acceptance end-to-end was not run.

How the terms in the gates are read, fixed now:

- "Sampled" means the samples defined under Metrics. A sample below its minimum cannot satisfy its gate.
- "Wrong-property attribution" is the property attribution metric above; a confirmed wrong-property attribution in a kept answer is an open defect until it is fixed and shown fixed by a test that failed before the fix.
- "The E3 bound" is the bound round 25's experiment E3 fixed before it ran: wrong additions (pairing, lost condition, wrong attribution or calculation) at most 10% of completions, and no kept wrong pairing.
- "The U6 walkthrough" is the operational walkthrough on the isolated real-model stack (unit U6 of the round-25 plan). Its steps are fixed by that unit, not here.
- A decision of "allow" is stated together with the material it was measured on (regression only, or regression and unseen). Passing on regression material alone is stated as in-sample.

## Cost estimate

The report of the round states, for each run, its cost from recorded costs, and for any run not done an estimate and exact scope.

- **Regression sets, two repetitions, both arms:** a paired two-repetition run on the current regression sets costs about USD 4.9, at the last measured per-turn product-model cost (about USD 0.0104 per turn in round 23). Rerun conversations after provider failures are included in the estimate only if they happen, and their cost is added.
- **Unseen set:** the cost scales with its number of turns. Roughly, cost = turns × 2 arms × 2 repetitions × cost per turn. At the minimum of 258 turns and the same per-turn cost that is about USD 10.7, before reruns, grading and a margin for questions that cost more than the regression average. The per-turn cost must be re-measured from the latest recorded costs before the run, not taken from this figure.
- **Grading** uses Claude subagents of the session and is not counted in product-model spend.
- Spend is accounted from recorded costs, and a run that would push cumulative spend past the limit set for the round is not started; its estimate and scope are reported instead.

## Limits of this test

- It measures failure rates on a stated body of material. It does not show that the system is always right and it makes no promise of 100% accuracy. A pilot is a controlled use with the instructions and limits of the pilot documents, not a guarantee.
- The regression sets cannot support a claim of generalisation, and the unseen set requested above does not exist until an appraiser writes its references.
- Rare error classes are not decidable at this size; they are counted and reported, not decided.
- Reference quality is bounded by the appraiser's reading. Disputed references are reported openly rather than corrected after the run.
- Results hold for the build, models, settings and documents that were run. Any change to them after the run is a new run.
