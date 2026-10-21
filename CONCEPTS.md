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

The focus is context, not a filter. When the user names another property, unit or document, the focus documents are dropped and the newly named documents — found only among those the user may see — replace them; when nothing or several documents match, the focus is not used as the answer's scope: the model gets the candidates and asks the user which one only when a material ambiguity remains.

### Question intent

What a request is about, decided for every message: a professional question (a concept, a method or a standard's requirement, answered without any particular property), a property question, a question about a specific document, a mixed question (professional knowledge applied to a named property), a question over a set of documents, or unknown. The request analysis's reading of the message decides the intent. What the message holds as identifying evidence (an address, a block and parcel, a unit label, or a name of a document or unit stated as a phrase) is only a hint: it ranks the candidate units and is recorded, and it never turns a professional question into a property question or an unknown one into a property one. A word that document titles share is never evidence, and a standard or section number is never a house number.

In a model-led turn the server does not answer in the model's place: a lookup that finds several documents or units reaches the model as candidate units, and a clarification to the user is the model's. Only the search-only path (an office without cloud sending, which has no model to read candidates) keeps the server's own reading of the evidence and its own clarifications.

### Candidate units

The appraisal units a first message, or a follow-up that names a subject other than the conversation's, points at, shown to the model as a short list of identity cards (at most eight, each with a handle, its file and pages, the basis of its ranking and the card's quotes). They come only from documents the user may see, are ranked by the identifying evidence in the message together with the cards' subject fields, and carry no volatile fields.

The list is data the model weighs, not a pin and not a scope: it does not lock a search to one unit, and a unit that is not on it can still be found by name. A follow-up that keeps its subject gets the conversation's unit alone. When the model asks the user which unit was meant, the units it offered are stored with the message, and the reply binds to them.

### Declared unit

The appraisal unit, or units, the model states that its answer is about. The model chooses the unit from the candidates shown (or finds it by search) and declares it with the answer. The server accepts a declared unit only if it offered that unit, listed it to the model in this turn (a document's units or a file's outline), or the model read it in this turn, and only if the user may read it now; anything else is dropped. A single declared unit becomes the conversation's unit for the next turn; several declared units are a comparison and set none.

Verification compares each value's evidence with the asked unit. The asked unit is taken first from a unit the question or the request's components name by a distinguishing identity, and only then from the declaration. A name or identifier that two units of one file share names neither, so a generic name never removes a value that came from the right unit. Whom a datum belongs to is decided in three states, the same property, another property, or unknown, and only positive evidence of another owner removes a claim: a name, parcel or address the cited text writes, or the unit's own identity, that conflicts with the claim's attribution. A unit with no identity of its own, or one whose name contains the asked property's whole name, is never another property by that alone; unknown is left to the judge. In a comparison each claim is checked against the side it is attributed to.

### Conversation slots

The parts of a conversation's state that are kept apart from one another after each answer: the professional topic with the items the answer listed, the selected property with its task parameters (metric, unit, period), the sources cited, and a pending clarification. A professional question leaves the property slot dormant, not dropped, so a later "in this property" returns to it. A pending clarification lives for one message and binds only a reply that refers to it. An ordinal such as "the second" binds to the previous answer's list. The slots are stored with the answer, survive a reload, and are checked against the user's permissions every turn.

### Time preference

How a conversation orders data that changes over time. With the switch "העדף נתונים עדכניים" on, the default for new conversations, the latest relevant datum comes first and earlier relevant data follows, newest first, with dates and sources; nothing older is hidden. With it off, no time order is preferred and dates are still shown. A date or range the question states overrides the switch. Each user message records the preference it was sent with, so retrying or refreshing never changes an answer, and the preference never changes the question intent or the property in focus.

### Datum date

The date a datum refers to: the valuation date of the appraisal whose own conclusion it is, a date stated in the datum's own row, cell or sentence, or a range stated by its table. A report's writing date, the upload date and the processing date never date a datum. A date the sources do not give stays unknown, and a forecast is never treated as measured. Only data of one datum identity form one timeline.

### Datum identity

What makes two values the same datum: the property and the appraisal unit, the metric, the unit after normalization, the periodicity, the area basis, the VAT basis, the scenario, and the facet. The facet is the component, consideration or cash flow the source states for the value: its table row or column, its own sentence, or failing those its lead-in or heading. A label the assistant wrote is never a facet. Two values are the same datum, different data, or undecided. Different components of one appraisal are shown side by side, not as competing values. A conflict is two values of the same datum that disagree for overlapping periods. When the facet cannot be read, the comparison stays undecided and is never called a conflict.

A facet has one of four states:
- **stated:** the source names the component.
- **read without a component:** the range that was read names none.
- **read but ambiguous:** a nearby phrase could not be tied to the value.
- **not read:** the context was not read.

Only the second means the document gave nothing in the range checked.

Two registrations of one span of the source are one datum.

A coefficient and the reduction derived from it, 1 minus the coefficient, are one adjustment in two forms.

### Property identity

Which appraisals are of one property. Two appraisal units are linked only by proving evidence from their own identification text: a shared block and parcel with a compatible sub-parcel, or the same street, house number and city. Each link keeps its reason. A street and number without a city never links by itself; such an appraisal is an unlinked candidate.

### Time coverage

The record of which appraisals of a property a time answer checked. Each appraisal is read (the datum or its section was read), located (only a passage was found), searched without a match, not searched (with the reason), or undated. Unidentified appraisals of the office are split three ways. A possible candidate mentions the property or has an unread identification part; it is named and keeps the record partial. An unlinkable appraisal has neither, and is counted. An appraisal proven to be another property is set aside. When coverage is partial, the answer calls its datum the latest in the sources checked, never the latest in the repository, and names what was not read.

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

Boundaries carry a certainty (certain, probable, or unsplit). When a boundary is uncertain, the reader may read past it, and nothing is invented to fill a missing name or boundary. The chat first locates a unit and then searches and reads inside it; passages and cited sources carry the unit they belong to. When several units fit a name, they reach the model as candidate units with the details that tell them apart; one is never picked by how many passages it has, and when the ambiguity is material the user is asked, with those details. A follow-up that changes only the metric keeps the same unit.

A report's own subject line, or the letter opening that introduces it, starts a unit; a cover letter and the report it introduces stay one unit. So does a change of the running header or footer that repeats across a report's pages, when another sign agrees (the other edge changes too, a cover page precedes it, or the page numbering restarts); the unit then starts at that cover page. Pages that still name the previous report and state no new address stay with it, so a report's appendices are never cut away by a page-number restart alone. A unit may also be named by the constant part of its own running header and by its cover title, never by an office or contact line. A new derivation of the units is stored beside the previous one: the old rows are kept and are read only to resolve a unit anchor stored before the change, never by unit number alone.

### Read and not used

A part of the request that a passage the turn read states, with the exact quote checked against that passage, but that the answer left out. It is completed from that passage in the repair round, without a new search; if it still is not given, the answer says where it was read and that it was not used — never that it was not found. Opening a source is not enough: only a verified quote inside the asked appraisal counts.

### Item ledger

The items, listed under each component of the frozen request, that the sources a turn read give for that component: each with its source, a verbatim quote, its value and condition when it has them, and an explicit state (given in the answer, cannot be verified with a reason, or does not apply under the request's conditions). An item counts as given only when an answer sentence states it with attribution to its own source, appraisal and date; the same words or number elsewhere in the answer do not count.

A component with an item that is neither given nor stated with a reason is not complete: it is completed from the source already read, and an item still missing is named in the answer as read and not used. The ledger never adds a requirement the user did not ask for. A component's completeness is decided on the answer version the user will see; an earlier verdict counts only while the sentences and the evidence it rested on are unchanged. An item is the fill of one component by one piece of evidence: the same passage can fill several components, each with its own state, and a decision once closed with a reason stays closed when the item is listed again. Two items are one only when their datum agrees (its headline values, dates and condition words); quotes that overlap or nest do not make them one, so one wide quote can hold several items, and a later answer version rewords an item only when it carries the same datum. Whether an item is given is one decision over the blocks the model wrote in the final answer (a list item with all its sentences, a paragraph, a table row with its heading), and the body's completeness and every gap message read that same decision.

An item's condition (a negation, "only", an exception, "subject to", a status, a method, or a limit in time or to a component, as its own source states it) is part of its meaning: an item given without it is not given in full, even when it is the only item of its kind. A missing condition is completed from the source or stated next to the value as written there, and is never a reason to remove the value. Besides the items the agent lists, the ledger holds items the server takes from the structure of the sources the answer cites (other rows of a cited table, other members of a list, other value-bearing sentences of a cited passage), but only those the request is shown to ask for. A note is an item's condition only when it is the item's own: a note marked with a sign belongs to the row or cell that carries that sign, never to a row of another table or another item.

### Identity card

What an appraisal unit says about its own subject, read once at ingestion from the unit's opening (its first pages) and kept for every unit of a file that holds several appraisals. A card holds only fields about the subject property: its name, address and city, block and parcel, property type, and the dates the opening writes as labelled text. Each field is stored together with a verbatim quote of the unit's blocks, and a field whose quote is not in those blocks is discarded, never the whole card.

The addressee, the sender and every person's or organisation's name are not kept: the client's name and the appraiser's office address are not the property's. A card's dates tell appraisals apart in the candidate list; they never date a datum, which keeps the dating rules of the datum date. A card is pending, done, failed, or empty (read, and nothing verified); a unit without a finished card keeps its structural title and identifiers.
