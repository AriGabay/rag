# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Tenancy and access

### Office

An appraisal firm using the system, and the hard isolation boundary: every document, record, question, answer, and cached result belongs to exactly one office, and nothing of one office is ever visible to, counted for, or revealed to another.

A user belongs to exactly one office, and the office is always taken from the user's authenticated session, never from anything the client sends.

### Document group

A named set of an office's documents that is the unit of access below the office: an office admin sees every group, while an employee sees only the documents of the groups they are a member of.

Group visibility follows a document into everything derived from it, so a record or passage that comes from a group the user cannot see never appears in their answers, statistics, sources, or review queue.

## Conversation

### Conversation focus

The datum and documents a conversation is about after its last answer: what a follow-up such as "and the total?" or "I meant the value" refers to.

The focus is context, not a filter. When the user names another property, unit or document, the focus documents are dropped and the newly named documents — found only among those the user may see — replace them; when nothing or several documents match, the user is asked which one, never answered from the focus.

## Documents

### Reading status

What the system actually read of a document, kept per block and summarised per document: each heading, paragraph, table and picture or page region is read, read with uncertainty (OCR, a visual reading OCR could not confirm, or text repaired from a broken font map), without text, or unread with a reason.

A document with any meaningful unread region is *partly read*. That state is shown on the documents screen and given to the assistant, and an answer never treats a partly read source, or a section it read only in part, as evidence that a datum is absent.

### Page furniture

Content that repeats across a document's pages, such as a logo, a letterhead, a footer or a watermark, recognised by its content rather than its size and stored and read once for the whole document.

Its text counts as present on every page it appears on, so it is not mistaken for content lost between two readings. A small picture that does not repeat is never treated as furniture and is read like any other region.
