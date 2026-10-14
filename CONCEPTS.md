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

### Question intent

What a request is about, decided for every message and checked by the server against the message: a professional question (a concept, a method or a standard's requirement, answered without any particular property), a property question, a question about a specific document, a mixed question (professional knowledge applied to a named property), a question over a set of documents, or unknown. A property or document is looked up only for an intent that refers to one, and only on identifying evidence in the message: an address, a block and parcel, a unit label, or a name of a document or unit stated as a phrase. A word that document titles share is never evidence, and a standard or section number is never a house number.

### Conversation slots

The parts of a conversation's state that are kept apart from one another after each answer: the professional topic with the items the answer listed, the selected property with its task parameters (metric, unit, period), the sources cited, and a pending clarification. A professional question leaves the property slot dormant, not dropped, so a later "in this property" returns to it. A pending clarification lives for one message and binds only a reply that refers to it. An ordinal such as "the second" binds to the previous answer's list. The slots are stored with the answer, survive a reload, and are checked against the user's permissions every turn.

### Time preference

How a conversation orders data that changes over time. With the switch "העדף נתונים עדכניים" on, the default for new conversations, the latest relevant datum comes first and earlier relevant data follows, newest first, with dates and sources; nothing older is hidden. With it off, no time order is preferred and dates are still shown. A date or range the question states overrides the switch. Each user message records the preference it was sent with, so retrying or refreshing never changes an answer, and the preference never changes the question intent or the property in focus.

### Datum date

The date a datum refers to: the valuation date of the appraisal whose own conclusion it is, a date stated in the datum's own row, cell or sentence, or a range stated by its table. A report's writing date, the upload date and the processing date never date a datum. A date the sources do not give stays unknown, and a forecast is never treated as measured. Only data of the same property, metric, unit, periodicity, area basis, VAT basis and scenario form one timeline.

## Documents

### Reading status

What the system actually read of a document, kept per block and summarised per document: each heading, paragraph, table and picture or page region is read, read with uncertainty (OCR, a visual reading OCR could not confirm, or text repaired from a broken font map), without text, or unread with a reason.

A document with any meaningful unread region is *partly read*. That state is shown on the documents screen and given to the assistant, and an answer never treats a partly read source, or a section it read only in part, as evidence that a datum is absent.

### Display frame

The coordinates of a page as it is shown to a reader: the visible part of the page, turned upright, measured from its top-left corner. It differs from the frame a PDF text reader reports positions in whenever the page is rotated or its visible area is cropped from the full sheet.

Every highlight, crop or cited place shown to a person or a model is expressed in the display frame, converted from the stored page geometry. A position taken from the text reader and used without that conversion lands in the right place only on upright, uncropped pages, so a test on such a page cannot tell the two frames apart.

### Page furniture

Content that repeats across a document's pages, such as a logo, a letterhead, a footer or a watermark, recognised by its content rather than its size and stored and read once for the whole document.

Its text counts as present on every page it appears on, so it is not mistaken for content lost between two readings. A small picture that does not repeat is never treated as furniture and is read like any other region.

### Source kind

What a stored document is, chosen when it is uploaded and changeable only by an admin: an appraisal (the default), an appraisal standard, or another professional document (a guideline, a clarification, a glossary). The kind is never inferred from a file name or title, so an appraisal named after the standard it follows stays an appraisal. Appraisals alone take part in property lookup, appraisal counts and unit locating. Standards and professional documents form the professional pool that professional questions search, and are never a source for a property's area, rent or value.

A standard keeps its structure — sections, definitions, the conditions and exceptions that limit each provision, notes, appendices and references — and its stated metadata (number, name, issuer, edition, publication and effective dates, scope, the document it amends), each value with the page and quote it came from or marked unknown. Editions are ordered only by the dates they state, never by upload date, and identical text stored twice is counted once.

### Appraisal unit

A part of a stored document that the system treats as one appraisal or one internal document, kept apart from the physical file, because a single file may bind several appraisals together with their appendices. Each unit has a stable identity, a title and aliases taken only from the document's own words, identifying details such as the address, block and parcel, or project name, the page or block range it covers, and the evidence that set its identity and boundaries. A unit is of kind appraisal, appendix, comparables, front matter, or a whole unsplit document; comparable properties and appendices are never treated as appraisals.

Boundaries carry a certainty (certain, probable, or unsplit). When a boundary is uncertain, the reader may read past it, and nothing is invented to fill a missing name or boundary. The chat first locates a unit and then searches and reads inside it; passages and cited sources carry the unit they belong to. When several units fit a name, the user is asked, with the details that tell them apart, rather than one being picked by how many passages it has. A follow-up that changes only the metric keeps the same unit.
