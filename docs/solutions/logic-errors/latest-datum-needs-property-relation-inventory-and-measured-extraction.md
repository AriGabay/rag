---
title: "The latest datum of a property: identity as a relation, an inventory before search, and date extraction measured on the real documents"
date: 2026-10-15
category: logic-errors
module: backend chat time ranking (app/chat/identity.py, property.py, tools.py, dates.py, meaning.py, verify.py, coverage.py, timeframe.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "Two appraisals at the same street and number in different cities answer as one property's history"
  - "A property spanning two parcels never gathers its own earlier appraisal"
  - "The newest appraisal of a property is never read when 12 other documents match the query better"
  - "An answer calls a value the latest although not every appraisal of the property was checked"
  - "9 of 16 real appraisals show no valuation date although every one states it"
  - "A correct headline value is removed because its date was written in the model's free-text scenario"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [recency, property-identity, coverage, provenance, valuation-date, extraction, real-model, rag, chat]
---

# The latest datum of a property: identity as a relation, an inventory before search, and date extraction measured on the real documents

## Problem
Round 11 (PR AriGabay/rag#2) let the chat rank data by the date each datum refers to. Round 12 found three foundations under that ranking that the round-11 tests did not exercise.
- **Property identity.** The key was block and parcel, else street and number. It had no city. A parcel set had to match exactly. Identifiers were read only from a unit's title lines, and most real units had none.
- **Retrieval.** Ranking reordered only the fused 12-document relevance window, and nothing showed whether the answer had checked every appraisal of the property.
- **Valuation dates.** These failed in silence on real documents, not on the synthetic fixtures.

## Symptoms
- **Wrong merges.** The same street and number in two cities merged into one property, and an address with no city merged with anything at that address.
- **Missing history.** Appraisal A of parcels {24, 40} and appraisal B of parcel {40} never linked.
- **Missing latest.** The latest appraisal ranked 15th by relevance never reached the answer.
- **Undated appraisals.** Measured on the office's 16 appraisal versions, 9 had no valuation date, and all 9 state it. Every miss was the extractor's:
  - extra words between the label and the date ("זו", "קרי", "מועד הביקור בנכס");
  - a two-digit year;
  - the label in a table header with the date in the cell below;
  - a date written before its label;
  - the label word "תאריך הקובע".
- **False conflicts.** After those fixes, 7 of the 8 remaining unit conflicts were again extraction errors:
  - a comparison sentence naming two valuation dates;
  - a "סימוכין" reference list;
  - an appraisal "שנערכה" by someone else;
  - a purchase;
  - a label after a relational word ("הסמוכות למועד הקובע");
  - a passing mention that contradicts the dedicated "המועד הקובע" section.

## What Didn't Work
- **A better key.** Any string key either merges different properties or splits one. Adding the city to the key still merges a cityless address, and parcel-set equality still splits a multi-parcel property. A key cannot record why two appraisals are one property.
- **Reordering the window.** Sorting the top 12 documents by date cannot show that the latest appraisal was found, because it may not be in the window at all.
- **Trusting the synthetic fixtures for extraction.** The round-11 fixtures wrote the label the way the extractor expected. The real reports did not.
- **Marking a date-only scenario as "source".** The model wrote the value's own date into the free-text `scenario`. Flipping that to source provenance fixed the judge, but it split one property's timeline by date, because the scenario is part of the comparability key. Review caught it.

## Solution
1. **Identity is a relation with a stored reason, not a key.**
   - Facts come from the unit's own identification parts: title run, identification and legal-status sections, labelled fields. They go into a derived table (`version_identities`, migration 0021, `identity.py`).
   - `property.relate` classifies a pair as parcel (shared, sub-parcel compatible), address_city, siblings, candidate or other.
   - A missing city never links by itself. It becomes a named candidate.
   - The property is the focus unit's compatible closure (`PropertyGraph.gather`), so separate lots never join through a third appraisal.
2. **Inventory before search.**
   - For an identified property in a timed turn, `tools.property_inventory` lists its appraisals with their valuation dates.
   - Each selected appraisal is searched on its own (`CAP_UNITS` 8, two slots kept for undated ones), and hits are ranked per (version, unit), not per file.
   - Every call records coverage (`coverage_for`). When coverage is partial, the answer says "הנתון המאוחר ביותר במקורות שנבדקו" and names the gap (`timeframe.coverage_note`). It never says the latest in the repository.
   - A turn that only read sources gets a reads-only partial record (`read_coverage`).
3. **Per-attribute provenance.**
   - A registered value's area basis, period or VAT is evidence only when its source states it (`meaning.value_qualifiers`). A measurement's attribute counts only when its quote, caption or header states it (`measurement_states`).
   - An unsupported qualifier is edited out and the source-backed number stays (`verify.plan_qualifier_edits`).
4. **Dates from the report's own structure** (`dates.py`, dates-v4, migration 0022):
   - A date read in the dedicated "המועד הקובע" section, or in a block led by the label, is primary and settles the unit.
   - Comparisons, references, prior appraisals, purchases and relational mentions never date it (`is_history`).
   - History keeps its own date. A genuine in-document discrepancy stays a conflict.
5. **What the model must not break has to reach the model and the judge as server text.**
   - A gathered appraisal at another house number was disowned by the model, and once removed by the judge as wrong_subject. The server now writes the link reason into the source header both read (`link_phrase`: "שומה של אותו נכס: חלקה X/Y משותפת").
   - A scenario that only restates the value's server-resolved date is cleared (`_back_date_words`). The date lives in `as_of`.

## Why This Works
- **Reasons can be read and checked.** Each property claim, latest claim or date now rests on a record that carries its reason: the link reason, the coverage of each call, or the primary-section flag. An answer can be truthful about what it checked, because the server knows which appraisals exist before it searches.
- **Extraction is grounded in real phrasing.** Each extraction rule exists because a real report used that phrasing, and each rule has a synthetic test.

## Prevention
- **Measure extraction on the real documents.** Before and after any text-extraction change, measure it on the real documents. Classify every miss as "not written" or "missed". Keep the counts in git and the content out.
- **Label fitted numbers.** Label those numbers "fitted", because the rules were derived from the same documents. Held-out sets measure meaning, sources and completeness, not the extractor.
- **Run the real model with logging.** Run each sequence on the real model three times, and log the search calls. The disowned link and the read-only turn showed only there.
- **Grep everything that reads a field.** Before changing a value's provenance to satisfy one consumer (the judge), grep every other reader of that field (the timeline key, conflicts, calculations).

## Related Issues
- `docs/solutions/logic-errors/time-ranked-answers-need-server-resolved-dates.md`: round 11's server-owned scope, dates and grouping. Its property key, "block and parcel, else street and number" (`property.property_key`, `same_property_units`), is superseded by the relation and closure above.
- `docs/solutions/logic-errors/hebrew-follow-up-entity-lookup-pitfalls.md`: a question-named property uses labelled identifiers only.
- `docs/solutions/workflow-issues/real-model-before-after-eval-on-the-local-stack.md`: the held-out discipline.
- Plan: `docs/plans/2026-10-14-1839-fix-time-ranking-reliability-plan.md`. U3b and U12b were added after measuring on the office data and on the real model.
