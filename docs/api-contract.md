# API contract

All endpoints live under `/api`. The browser reaches them through the Next.js proxy, so the session cookie (`rag_session`, HttpOnly, SameSite=Lax) is same-origin. The server derives the office and user from the session only; any `office_id` sent by a client is ignored.

Errors return `{"detail": "<Hebrew message>"}` with status 400/401/403/404/409/413/415/422/429. 404 is used for anything the user may not see, so existence never leaks.

Money, areas, and prices per sqm are serialized as **strings** (exact decimals). Dates are ISO `YYYY-MM-DD`.

## Auth

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/auth/login` | `{email, password}` | `{ok: true}` + cookie |
| POST | `/api/auth/logout` | — | `{ok: true}` |
| GET | `/api/auth/me` | — | `{user: {id, email, full_name, role: "admin"\|"employee", can_upload}, office: {id, name}, groups: [{id, name}], demo_mode}` |

## Documents

| Method | Path | Notes |
|---|---|---|
| POST | `/api/documents` | multipart: `files` (1–20), `group_id`, optional `document_id` (upload a new version). Response `{results: [{filename, status: "accepted"\|"duplicate"\|"rejected", reason?, document_id?, version_id?}]}`. A rejected file never blocks the others. |
| GET | `/api/documents?q=&status=` | `{documents: [DocumentSummary]}` |
| GET | `/api/documents/{id}` | `DocumentSummary` plus `versions: [Version]` |
| DELETE | `/api/documents/{id}` | logical delete, admin or uploader; `{ok: true}` |
| GET | `/api/documents/{doc_id}/versions/{version_id}/file` | streams the original (`application/pdf` inline). Link to a page with `#page=N`. |
| GET | `/api/search?q=&limit=` | hybrid content search (the same search the chat tools use: abbreviation variants, a focused pass inside a document the query names, at most two rows per table): `{results: [{chunk_id, document_id, version_id, title, page_list, section, snippet, text, kind, score}]}` |
| GET | `/api/documents/{doc_id}/versions/{version_id}/blocks?start=&end=` | the version's blocks in reading order (a window when given): `{title, is_current, mime_type, total, file_url, blocks: [{index, kind, section, section_path, paragraph_no, page, media, source, status, note, text, bbox, method, reader_version, content_hash, original_text, table?: {headers, caption, title, notes, source, media, rows}, media_url?, page_url?, region_url?}]}`. A DOCX block is located by section and paragraph; nothing invents pages. A PDF block has its page and its box (`bbox`: x0, top, x1, bottom in points from the page's top-left corner); `original_text` is the text as extracted when `text` is a verified font-map correction of it. `page_url` (PDF blocks with a page) and `region_url` (with a box as well) open the page or the region as an image. |
| GET | `/api/documents/{doc_id}/versions/{version_id}/media/{name}` | one raster picture of a DOCX, only by a media name a block of that version names (PNG/JPEG/GIF/BMP; vector pictures are shown as the table read from them). |
| GET | `/api/documents/{doc_id}/versions/{version_id}/pages/{page_no}/image` | one page of a PDF version as `image/png`, rendered on the server at a fixed scale (1.5, 108 dpi) with the long side capped at 2000 px. Only a page number from 1 to the version's page count; anything else (another number, a DOCX version) is 404. |
| GET | `/api/documents/{doc_id}/versions/{version_id}/regions/{block_index}/image` | one region of a PDF version as `image/png`: the box of a block stored for that version, with a 4-point margin, at a fixed scale (3.0, 216 dpi) with the long side capped at 2000 px. A block index the version does not have, or a block without a page and box, is 404. |

The page and region views check the version like the file and blocks: a version of a deleted document, or of a document in a group the user does not belong to (or in another office), is 404. Every request writes one `source_view` audit event (`page` or `block` in its details). Responses are `Cache-Control: private, max-age=600` with `X-Content-Type-Options: nosniff`.

`DocumentSummary`: `{id, title, group: {id, name}, deleted, created_at, current_version: Version | null, versions_count}`

`Version`: `{id, version_no, filename, status: "pending"|"processing"|"ready"|"needs_review"|"failed"|"superseded", status_reason, page_count, pages_incomplete, records_total, records_needing_review, is_current, created_at, processed_at, mime_type, reading}`

`reading` keeps apart what was read: `{passages, tables, measurements, measurements_state, images_total, images: {read, read_uncertain, no_text, decorative, unread}, unread: [{media, section, reason, page?}], partial, coverage, uncertain_blocks, corrected_blocks, corrections, repeated_images, ingestion_version}`. `partial` is true when any picture, region or page was not read, or text stayed uncertain after a font map could not be repaired; zero structured records never means zero searchable content.

- `coverage`: one entry per page with something to report, in page order: `{page, ok, method, corrected, regions: [{block, kind, status: "unread"|"read_uncertain", reason, bbox, section, media}], more?}`. `ok: false` is a page that was not read; `corrected` counts the page's blocks with corrected text; a region is a block left unread, or read uncertainly with a reason (Hebrew), located by its block index and box (`region_url` opens it). At most 40 regions are listed per page; `more` counts the rest. A DOCX has no pages: its regions are under `page: null`, located by section and media. A report written before coverage existed lists its unread pictures without block or box. An empty list means nothing to report.
- `uncertain_blocks`: text blocks left uncertain by a broken font map no verified repair fixed.
- `corrected_blocks`: blocks whose text is a verified font-map correction (the original is kept and shown on request); `corrections`: the accepted font × character mappings.
- `repeated_images`: pictures repeated through the document (a logo, a stamp, a watermark), each shown and read once rather than on every page.

## Review

| Method | Path | Notes |
|---|---|---|
| GET | `/api/review/queue?document_id=` | `{items: [ReviewItem]}` |
| GET | `/api/review/records/{occurrence_id}` | `RecordDetail` |
| POST | `/api/review/records/{id}/approve` | `{note?}` → `RecordDetail` |
| POST | `/api/review/records/{id}/correct` | `{field, value, note}` (note required) → `RecordDetail`; 422 with Hebrew message on invalid number/date |
| POST | `/api/review/records/{id}/reject` | `{note}` → `RecordDetail` |
| POST | `/api/review/dedup/{id}/merge` | → `{ok: true}` |
| POST | `/api/review/dedup/{id}/keep-separate` | → `{ok: true}` |

`ReviewItem` is one of:

- `{kind: "record", id, document: {id, title}, version_id, page_no, verification_status, summary: {data_kind, city, neighborhood, address, price, area, area_type, transaction_date, valuation_date}, flags: {conflict, missing_critical: [field], ocr}}`
- `{kind: "dedup", id, reason, a: TransactionSummary, b: TransactionSummary}` where `TransactionSummary = {transaction_id, data_kind, address, price, area, area_type, transaction_date, sources: [Source]}`

`RecordDetail`: `{id, document: {id, title}, version_id, page_no, table_index, row_index, text_span, is_docx, verification_status, conflict_flag, missing_critical, ocr, review_note, fields: [{field, label, original_text, normalized_value, status, source_path, previous: [...]}], computed_price_per_sqm, stated_price_per_sqm, calc_definition, file_url}`

Field names: `data_kind, city, neighborhood, address, block, parcel, sub_parcel, property_type, rooms, transaction_date, valuation_date, report_date, area, area_type, price, currency, vat_basis, price_per_sqm_stated`.

### Facts review (extracted attributes, KTD9, R16)

Facts are values of attributes nobody anticipated (for example a safe-room area), extracted on demand with a verbatim quote. Whoever can see a fact's document may review it; every read and write runs under the reviewer's RLS, so a fact in a document outside the reviewer's groups is a 404, and a conflicting value in such a document is never shown, not even as a flag. Only facts of current, undeleted versions at the attribute's current extraction version are listed.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/review/facts` | `{attributes: [{attribute: Attribute, documents: [{document: {id, title}, version_id, facts: [Fact]}]}]}`: facts with status `needs_review` or `auto_validated`, grouped by attribute then document, `needs_review` first |
| GET | `/api/review/facts/{fact_id}` | `Fact` plus `attribute: Attribute` and `facts_version` |
| POST | `/api/review/facts/{fact_id}/approve` | `{note?, expected_status?, expected_value?}` → status `verified`; returns the detail |
| POST | `/api/review/facts/{fact_id}/reject` | `{note, expected_status?, expected_value?}` (note required; 422 `"יש לתעד את סיבת הדחייה"`) → status `rejected` |
| POST | `/api/review/facts/{fact_id}/correct` | `{value, unit, note?, expected_status?, expected_value?}` → status `corrected`. `value` is a non-negative number (thousands separators allowed; otherwise 422 `"יש להזין מספר תקין (לדוגמה 12 או 12.5)"`); `unit` is one of the attribute's `unit_options[].code` (`""` = no unit where allowed; otherwise 422 `"יש לבחור יחידה המתאימה למאפיין זה"`); a non-numeric attribute cannot be corrected here (422 `"ניתן לתקן כאן רק ערכים מספריים"`) |

**Stale-list precondition.** Approve, reject and correct accept the state the reviewer acted on: `expected_status` (the listed `status`) and `expected_value` (the listed `value`, in the canonical unit; numbers compare by value, so `"12"` equals `"12.0"`; an explicit `null` expects a fact without a value). Both are optional; an absent field is not checked. They are checked on the locked row before any change. If the fact no longer matches (for example another reviewer rejected or corrected it after the list was loaded), the answer is **409** `"הערך השתנה בינתיים; טענו את הרשימה מחדש"` and nothing changes: no status, history, `facts_version` or audit event. The client reloads the list. A 422 for a missing reject note is reported before the 409.

`Attribute`: `{id, label, value_type: "numeric"|"text"|..., unit_dimension, canonical_unit, canonical_unit_label, unit_options: [{code, label}], facts_version}`.

`Fact`: `{id, status: "needs_review"|"auto_validated"|"verified"|"corrected"|"rejected", value, unit, unit_label, original: {value_text, unit, unit_label}, quote, page, url, document: {id, title}, version_id, entity_role: "subject"|"comparable"|"other", entity_descriptor, review_note, reviewed_at, previous: [history entries], conflicts: [brief Fact]}`. `value` is in the canonical unit; `original` is what the document says; `url` opens the source page (`#page=N`).

Every change appends the prior state to `previous`, bumps only that attribute's `facts_version` (answers built on the old facts become stale, see `Message.stale`) and writes an audit event (`fact_approve`, `fact_reject`, `fact_correct`). Trust tiers in answers: `verified` and `corrected` facts make the main figure; `auto_validated` facts are added only to the separately labeled preliminary figure; `needs_review` facts are excluded and counted in `coverage.facts.awaiting_review`.

## Chat (the earlier engine, `/api/ask`)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/conversations` | `{conversations: [{id, title, updated_at}]}` |
| POST | `/api/conversations` | new conversation with an empty context → `{id}` |
| GET | `/api/conversations/{id}` | `{id, title, confirmed_conditions, pending_clarification, context, messages: [Message]}`; another user's conversation is 404 |
| POST | `/api/ask` | `AskBody` → `{conversation_id, question_id, turn_id, answer: Answer}` |

`AskBody`:

```json
{
  "conversation_id": "uuid (optional; a new conversation when absent)",
  "turn_id": "uuid (optional; generated by the server when absent)",
  "question": "free text (optional when clarification, filters or remove is given)",
  "clarification": {"key": "data_kind", "value": "transaction_price"},
  "filters": {"city": "...", "neighborhood": "...", "data_kind": "...", "date_field": "...", "year_from": 2024, "year_to": 2024, "property_type": "...", "area_type": "...", "vat_basis": "..."},
  "remove": ["years", "city", "attribute"]
}
```

- **Turns are idempotent (`turn_id`).** The client generates one `turn_id` per user action and reuses it on retries. A turn that already finished returns its stored result unchanged; one still running answers **409** `"השאלה עדיין בעיבוד"` (poll by posting the same body again); a failed turn runs again.
  - A `turn_id` is unique per conversation and is looked up in the request's `conversation_id` only: the same `turn_id` posted to another conversation is a new turn there, never the other conversation's answer. A request without `conversation_id` (the first turn of a new conversation, or its retry) replays only the caller's turn with that `turn_id` that opened a conversation.
  - A reservation still `pending` after the turn deadline plus three attempts of the slowest model-call timeout (`turn_deadline_seconds + 3 × max(llm_timeout_*_seconds)`, 135 s by default) is treated as abandoned (its process died): the next post of the same `turn_id` runs the turn again on the same question row instead of answering 409 forever. Until then a pending turn answers 409. A run whose reservation was taken over this way never completes or fails the row; it answers 409 `"השאלה עדיין בעיבוד"` and the client's next poll returns the stored result.
- **The server holds the conversation context.** `filters` are explicit edits of the context chips, applied as a delta to this turn (they are not the whole panel state). An edit that contradicts a value the question itself states asks a `filter_conflict` clarification (options `"<key>:<value>"`). `remove` clears chips by their `context.chips[].key` (`city`, `neighborhood`, `years`, `date_field`, `data_kind`, `property_type`, `area_type`, `vat_basis`, `attribute`, `metric`). A request with only `filters`/`remove` re-runs the last answered task with the edited context.
- **Clarifications** can be answered with a button (`clarification: {key, value}`) or in free text (`question`). Free text that answers the pending clarification resolves it (`answer.interpretation_note` says how it was read); an unrelated question is answered and the clarification stays open. A button with no matching pending clarification is **409**.
- Two concurrent turns on one conversation: the later one is re-applied once to the fresh context; if the context changes again meanwhile it answers **409** `"השיחה עודכנה בבקשה אחרת באותו זמן. נסו לשאול שוב."`. A turn that answers or changes a pending clarification (a button, or free text read as `answer_to_clarification` / `change_clarification`) is re-applied only when the fresh context still holds the same pending clarification (same key and question, and the same `filter_conflict`); when another turn answered, replaced or cleared it meanwhile, the turn answers that same **409** instead of applying the answer to a different clarification.
- `"למה?"` and `"תראה לי את המקור"` answer from the previous answer's method and sources, re-authorized now (`answer.meta`), with no model call; they are never cached.
- A typed message while a clarification is pending is read as a change to it only when it keeps that task's attribute and metric and changes a condition ("ובגבעתיים?"); a self-contained question is a new question, and the clarification stays open.
- The stored turn (`questions.plan`) keeps `turn_plan`, the task the server ran after its policy (a turn the server answered with a clarification before any tool ran has task type `clarify`), and, for a model turn, `model_plan` as the model returned it.

`context` (for the chips): `{attribute, metric, city, neighborhood, years: {from, to} | null, data_kind, date_field, chips: [{key, label, value}]}`. `pending_clarification`: `{key, question, options}` or null; both survive refresh and reopen.

`Message`: `{question_id, question, answer: Answer | null, stale: bool, hidden: bool, created_at}`. `stale` means the data version or the user's permissions changed since the answer was produced, or a fact of an attribute the answer used was reviewed, corrected or newly extracted (only that attribute's facts version counts) — show a badge and "שאל שוב". `hidden` means a source was deleted or became unauthorized: render one placeholder line, no body, no sources.

`Answer`:

```json
{
  "kind": "numeric | clarification | content | combined | abstain",
  "text": "plain text; [E1] markers refer to sources by evidence id",
  "provider": "template | mock | cloud | extractive",
  "demo": false,
  "mode": "cloud | demo | limited | error",
  "clarification": {"key": "data_kind", "question": "...", "options": [{"value": "transaction_price", "label": "מחירי עסקאות"}]},
  "numeric": {
    "conditions": [{"label": "עיר", "value": "רמת גן"}],
    "record_count": 4,
    "mean_price_per_sqm": "25000.00",
    "weighted_price_per_sqm": "26666.67",
    "median_price_per_sqm": "25000.00",
    "min_price_per_sqm": "20000.00",
    "max_price_per_sqm": "30000.00",
    "currency": "ILS",
    "uncertain_duplicates": 0,
    "conflicts": 0
  },
  "preliminary": {"value": "13.00", "record_count": 2, "values": ["12", "14"]},
  "claims": [{"text": "...", "kind": "explicit | inferred | computed", "evidence_ids": ["E1"]}],
  "abstention_kind": "not_found | not_stated | not_extracted_or_verified | insufficient_permission_scope | null",
  "dropped_claims": 0,
  "sources": [{"evidence_id": "E1", "document_id": "...", "version_id": "...", "title": "...", "page_list": [3], "section": null, "row": 2, "snippet": "...", "url": "/api/documents/<d>/versions/<v>/file#page=3"}],
  "coverage": {"text": "...", "docs_pending": 0, "docs_failed": 1, "docs_needs_review": 0, "records_awaiting_verification": 2,
               "facts": {"in_scope": 9, "found": 5, "not_stated": 2, "partial_scan": 0, "pending": 2, "failed": 0, "not_yet_extracted": 2, "awaiting_review": 1, "conflicts": 0, "unknown_metadata": 0}},
  "method": "חילוץ הנתון מהמסמכים עם ציטוט מדויק וחישוב בקוד: 9 מסמכים בתחום, ערך נמצא ב-5",
  "conditions": [{"label": "עיר", "value": "רמת גן"}],
  "partial": false,
  "pending_extraction": 0,
  "interpretation_note": "התשובה הובנה כמענה לשאלת ההבהרה: מחירי עסקאות.",
  "cleared": [{"key": "city", "label": "עיר", "value": "גבעתיים"}],
  "limitations": ["..."],
  "cached": true
}
```

- A price question keeps the `numeric` block above. Another computed attribute uses `numeric: {conditions, attribute, operation, value, unit, record_count, values, value_filter, value_type}`; for an extracted attribute `record_count` and `value` cover reviewed values only, and `preliminary` (labeled separately) adds the values the server validated but no person reviewed. `coverage.facts` holds the extraction coverage counts.
  - `value_type` is `numeric`, or `text` / `boolean` / `date` for an attribute stated in words (a designation, a status): its operation is `count` or `values`, and `values` lists the distinct normalized values (the text shows how many documents state each).
  - `value_filter` (`{op: "<"|"<="|">"|">="|"="|"!=", value}` or null) is a condition on each case's value ("smaller than 11"). With `operation: "count"`, `value` is how many cases meet it and `record_count` is how many cases were observed (`"2"` of `3`); the text lists the matching documents. The condition also appears in `conditions` as `{"label": "תנאי על הערך", "value": "קטן מ-11 מ״ר"}`.
  - A question that names an address, block/parcel or document title computes and searches over the documents that name it only (never a mean over other properties); `steps[].args.documents` in the stored turn lists them. When one address is the subject of several documents, each document's statement is shown and `limitations` says so; an address that names no visible document is stated in `limitations`.
- Fact sources also carry `value` and `tier` (`verified | preliminary`); their `snippet` is the verbatim quote.
- A comparison adds `compare: {sides: [{label, document_id, version_id, evidence_count}], incomplete, missing_sides, conflicts}`. Sides come from the conversation's sources or from the documents the question names; one named document with several versions compares its two latest versions (labeled by version). With neither, a `referent` clarification asks which documents to compare.
- A document list (`locate`) shows only documents whose passages carry the question's topic, best first, with the pages of their supporting passages.
- `abstention_kind` is set on every abstention, including the price path when no verified record matches (`not_found`, or `not_extracted_or_verified` when matching records await verification) and a combined answer whose computation found nothing.
- `partial` is true when the turn deadline cut a step short or documents in scope are still being extracted; such answers are never cached. `cached` appears only on a cache hit (sources re-authorized, coverage recomputed). `cleared` lists the context items this turn cleared (a new question or topic change). `interpretation_note` is one line for the UI, or null.

### Measurements review (quantities with their meaning)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/review/measurements?document_id=&status=&kind=&q=&offset=` | `{items: [Measurement], total, counts, kinds, units, periods, vats}`; rejected ones only with `status=rejected` |
| POST | `/api/review/measurements/{id}/verify` | `{expected_status, note?}` |
| POST | `/api/review/measurements/{id}/correct` | `{expected_status, value_text?, unit?, period?, vat?, area_basis?, metric?, metric_kind?, note?}` |
| POST | `/api/review/measurements/{id}/reject` | `{expected_status, note}` (note required) |

`Measurement`: the metric and value as written (`metric`, `value_text`, `quote`, `section`, `block_index`, `table_index`) and their meaning, each with a Hebrew label: `metric_kind`, `value_form` (exact/approximate/range/minimum/maximum), `unit`, `period` (month/year/one_time/none/unknown), `vat` (included/excluded/unknown/not_applicable), `area_basis` as written, `subject`, `subject_role`, `value_role`; `status`, `issues`, `conflict` (a newer extraction that disagrees with a reviewed value), `previous`. A change whose `expected_status` no longer matches is a 409; every change moves the data version.

## Conversational chat (`/api/chat`)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/chat/conversations?q=&archived=&before=&limit=` | `{conversations: [{id, title, updated_at, created_at, archived, engine}], next}` (search covers titles and message text) |
| POST | `/api/chat/conversations` | a new, empty conversation |
| PATCH | `/api/chat/conversations/{id}` | `{title?, archived?}` |
| DELETE | `/api/chat/conversations/{id}` | deletes the conversation and its messages |
| GET | `/api/chat/conversations/{id}/messages?before=<message id>&limit=` | `{conversation, messages: [ChatMessage], has_more}`, oldest first |
| POST | `/api/chat/conversations/{id}/messages` | `{content, client_id?}` → `{user, assistant}`; the assistant message starts `running`. The same `client_id` returns the same pair and starts nothing. 409 while an answer of this conversation is still working. |
| GET | `/api/chat/messages/{id}` | one message (poll while `running`/`cancelling`) |
| POST | `/api/chat/messages/{id}/cancel` | asks the server to stop; `cancelling` until the worker reaches its next check, then `cancelled` |
| POST | `/api/chat/messages/{id}/retry` | replaces a finished/failed/cancelled answer with a new run |
| GET | `/api/chat/messages/{id}/diagnostics` | what each verification round removed or repaired, and why, and how a follow-up was resolved: `{message_id, rounds, removed, resolution}`. `resolution` (nullable) holds the resolving model's raw parse, the server's decision on each claimed field, and the entity lookup's words and outcome. Only the message's owner or an office admin (audited), and only while every document behind the answer — and every document the lookup found or offered — is visible to them; otherwise 404 |

`ChatMessage`: `{id, role, content, status: "running"|"cancelling"|"cancelled"|"done"|"failed", error, progress: [{step, label}], answer, reply_to, client_id, created_at, stale, cancel_requested}`. `answer`: `{kind: "rag"|"search_only", status: "answered"|"partial"|"not_found"|"clarification", markdown, claims, clarification, missing, sources, measurements, computations, documents, verification: {judged, judge_status, removed, partial, annotated, request_mismatch}, searches, coverage, ledger, scope_kind, focus, request, requested}`. Citations in `markdown` are `[S#]` (passage), `[M#]` (measurement), `[C#]` (computation).

- A source of kind `listing` is a page of documents a count or a list cites (from `find_documents` / `list_documents`): its `document_id` and `version_id` are null, its `text` is the listing (criterion, total, titles), and `listed_document_ids` names the documents on it. The answer is shown only while all of them are visible; a listing is never one of the answer's `documents`.

- `verification` gives counts only: claims removed, claims marked partly verified, and numbers whose missing qualifier (area basis, period, approximation) the server wrote from the source, marked "כפי שנכתב במקור". The removed text is in the diagnostics endpoint. Answers stored before this are reduced to counts on read.
- `ledger` is the server's coverage record: `{scope_kind, scope_query, cited, matching, levels, read, retrieved_only, with_data, not_checked, unused, partially_read, omitted, also_matching, tables, complete, note}`.
  - Every document entry is `{document_id, title}`; `omitted` entries add `{what, why}`.
  - `levels` maps each matching document to how deep the turn reached: `located`, `retrieved` (a passage), `read` (a section or table, or its measurements), or `verified` (the answer cites its datum).
  - `tables` lists each cited table: `{document_id, title, location, rows, presented}`.
  - `complete` is document coverage. A set answer that is not `complete` carries the note in `markdown` (`> **כיסוי:** …`) and its status is at most `partial`. A complete one whose documents were only retrieved, not read, carries a note that says so.
  - An answer that cites only listings (a count or a list of documents) has `membership: true`, `pages` and `pages_read`: it is `complete` when every page of each cited listing was listed, its note says the documents' content was not read, and it is `partial` otherwise.
- `request` is the follow-up as resolved in its context before the search (`{kind, standalone_question, metric_kind, unit, period, area_basis, vat, subject, document_ids, changed, rejected, relation, scope, approved, candidates}`), or null. `relation` is the parse's relation to the previous turn (`new_question`, `same_datum`, `correction`, `metric_change`, `scale_change`, `clarification_answer`); `scope` is `entity` or `set`; `approved` lists the dimensions (`metric`, `scale`) the answer is held to; `candidates` are the documents (`{document_id, title}`) a server clarification offered, resolved among on the next turn.
- `requested` lists each datum the question asked for, with the status the turn's actions support: `found`, `not_found_search`, `source_partial` or `section_checked_absent` (with the section).
- `focus` holds the datum at the centre of the answer: `{metric_as_written, metric_kind, unit, period, area_basis, vat, subject, value_role, document_ids}`. The next turn receives it, or the answer's `request` when the focus is null.
- `usage` lists the token counts per model call, with no content: `[{purpose, status, input_tokens, cached_input_tokens, output_tokens, latency_ms}]`.
- A turn whose answer could not be verified ends `failed` with `"לא ניתן היה לאמת את התשובה מול המקורות. אפשר לנסות שוב."`. An answer whose source was deleted or is no longer visible comes back `hidden`; one built before a data or permission change is `stale`.

## Admin (admin role only)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/admin/users` | `{users: [{id, email, full_name, role, can_upload, is_active, group_ids}]}` |
| POST | `/api/admin/users` | `{email, full_name, password, role, can_upload, group_ids}` |
| PATCH | `/api/admin/users/{id}` | any of `{role, can_upload, is_active, group_ids, password}`; deactivation revokes sessions |
| GET | `/api/admin/groups` | `{groups: [{id, name, document_count}]}` |
| POST | `/api/admin/groups` | `{name}` |
| GET | `/api/admin/settings` | `{cloud_llm_enabled, provider: "openai"\|"anthropic", provider_name: "OpenAI"\|"Anthropic (Claude)", model, key_present, mode, mode_status, untested, last_test: {provider, model, ok, status, tested_at} \| null, retention_note, acknowledged_at}` (see below) |
| PUT | `/api/admin/settings` | `{cloud_llm_enabled, acknowledge: true}`; enabling without `acknowledge: true` → 422; enabling records the provider selected now as the one acknowledged (`office_settings.cloud_provider`; disabling clears it); every change bumps `settings_version`; returns the settings object |
| POST | `/api/admin/provider/test` | no body; runs the connection test and returns the settings object with the new `last_test`; employee → 403 |
| POST | `/api/admin/reprocess` | `{all?: false}` → `{queued, versions, ingestion_version}`: reads current documents again (blocks, pictures, chunks, embeddings); records and reviews are kept, cached answers dropped, the data version moves. Without `all`, only versions read by an older reader. |
| POST | `/api/admin/measurements` | `{all?: false}` → `{queued, extraction_version}`: measurement extraction for current versions (cloud mode only) |
| GET | `/api/admin/jobs` | `{jobs: [{kind, status, count}]}` |
| GET | `/api/admin/coverage` | `{documents_by_status: {status: count}, records: {total, verified, awaiting_verification, needs_review}, open_dedup_candidates, review_queue_count}` |

Provider status (R8, KTD5; derived in `backend/app/providers/status.py`, the only place that decides which provider answers):

- `key_present` is a boolean only. The key, or any prefix of it, never appears in a response, log line or audit event.
- Consent is per provider: cloud use counts as enabled only when the acknowledged provider (`office_settings.cloud_provider`, written by `PUT /api/admin/settings`) is the provider selected now. An office acknowledged for another provider (for example `anthropic` while OpenAI is selected, as offices acknowledged before the provider switch are) keeps `cloud_llm_enabled: true` but is in `limited` mode with `mode_status: "reacknowledge_required"`: no office content is sent to any provider, demo mode or not, until an admin acknowledges again with `PUT /api/admin/settings {cloud_llm_enabled: true, acknowledge: true}`. A row enabled outside that route with no provider recorded is read as consent for the selected provider.
- `mode`:
  - `cloud`: cloud use enabled for the selected provider, key present, and the last connection test passed or has not run for the current provider and model (`untested: true`).
  - `error`: enabled for the selected provider and the last test failed, or enabled with no key. `mode_status` names the failure (`missing_key` or the test status). Questions are then answered from the sources only, with a limitation naming the failure; never with the demo mock.
  - `demo`: disabled and `DEMO_MODE` on: answers come from the clearly labeled mock (`provider: "mock"`, `demo: true`).
  - `limited`: disabled and not demo, or enabled for another provider than the selected one (`mode_status: "reacknowledge_required"`, re-acknowledge required): answers are assembled from the sources only.
- Connection test: one structured call (purpose `test`) through the selected provider with a fixed synthetic Hebrew prompt (no office content), checking the parsed echo. It is therefore allowed while cloud use is off. With no key it records `missing_key` without any network call. `status` is one of `ok`, `missing_key`, `auth`, `model_unavailable`, `timeout`, `rate_limited`, `quota`, `refusal`, `incomplete`, `invalid`, `error`. The result is stored per office in `office_settings` (`provider_test_*`, `provider_tested_at`), audited as `provider_test`, and a result that changes the mode bumps `settings_version` so no answer cached under the old mode is served.
- `retention_note`: the selected provider's data-retention note shown with the acknowledgement (OpenAI: `store=false`, yet abuse-monitoring logs may be kept up to 30 days unless the organization has Zero Data Retention).
