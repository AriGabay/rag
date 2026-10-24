"""The tools the answering model may call, and the evidence they produce.

The model never runs SQL and never sees anything the user may not: every tool runs in its own short
transaction under the user's tenant context (RLS decides what is visible), validates its arguments, and returns
plain text the model reads. Every passage a tool returns is registered as a source with an id (``S1``...); every
stored measurement as ``M1``...; every value taken from a source as ``V1``..., every user assumption as ``A1``...
and every calculation as ``C1``.... Every tool also records how deep it reached
into each document — located (listed in a set or outlined), a passage retrieved, a section or table read (or its
measurements listed) — and which sections and tables it opened, so the server can state an answer's coverage
itself; a document whose datum the verified answer cites is ``verified`` (``coverage.build``). An
answer may cite only ids issued in the same turn, so a previous answer is never a source: an earlier turn's
passages are offered as ``P`` references the model must re-open (``read``), which reads them again under the
current permissions and only while the document's reading is the one they were read from.

Documents, sections, tables, continuations and unread regions get turn-local short handles (``D#``, ``§#``,
``T#``, ``K#``, ``R#``) the server maps to what they name; every handle but ``D#`` is bound to the version and
the reading it was issued for, and every use resolves the document again under the current permissions (KTD9).

Every result has a bounded size (``chat_read_chars``, ``chat_table_rows``, ``chat_passage_chars``) and every
output sent to the model counts against the turn's tool-output budget (``chat_tool_output_chars``, KTD12). Once it
is spent, the reading tools (``READING_TOOLS``) return a header with the call that would read on and no body —
nothing is read or counted as read — and ``Workspace.limits_hit`` records ``tool_budget``; registering values and
calculating over what was already read still work. The reading deadline (``Workspace.read_until``) is checked the same
way before a tool starts (``DEADLINE_TOOLS``; ``inspect`` has its own check): one that starts after it does no work,
returns the same kind of header with ``time_limit``, and records ``time_limit`` (round 9 U11, R16, R18, KTD9). An
identical read in one turn (the same window of the same reading) is not fetched again: it returns a new citable source
that points at the first (``Workspace.read_memo``).

Tools:

- ``search``: hybrid retrieval (lexical, trigram, semantic) over text, tables, table rows and picture text,
  optionally within named documents, in the pools the turn's intent allows (``pool``; see **Pools** below);
- ``read``: one locator — a source (``S#``/``P#``: the paragraphs around it, or its whole table), a page range of
  a document, a section (``§#``, its sub-sections included), a table (``T#``) or the continuation of an earlier
  read (``K#``). A result is a part of bounded size whose header states whether it is complete, clipped (and the
  ``K#`` that continues it), has unread regions (marked in place with their ``R#``) or uncertain reading; blocks
  the turn already returned are sent as a pointer to the source that returned them (``app.chat.reader``);
- ``find_standards``: the standards and professional documents the user may see, with what each states about itself
  ("לא ידוע" where it states nothing), the editions of a number with whether their order is known, the document an
  amending one amends, and the copies of one text; a listing source;
- ``find_documents``: every document that contains all the terms that define a set (a place, a document type),
  in its title or text — the scope of a question about a set of documents, paged, with the total;
- ``list_documents``: visible documents with their reading status (fully read, partial: what was not read), paged;
- ``outline``: a document's sections and tables as openable handles, with their sizes and unread regions;
- ``inspect``: a visual reading of a region (``R#``) or a page of a PDF that ingestion did not read (or read
  uncertainly without text): rendered at a legible scale and read once by the vision model, stored per version,
  reading and region (``region_readings``) for later turns, capped per turn (``chat_max_inspections``); a region or
  page ingestion did read returns that reading without a model call. The stored reading keeps, per number and table
  cell, whether OCR of the same crop (or the region's text layer) confirms it in its place, and the crop's frame;
  each table it transcribed is a vision ``T#``, usable by ``take_value`` in the same turn (KTD6, R16);
- ``find_measurements``: stored measurements with their meaning (kind, unit, period, area basis, VAT, role,
  subject), grouped by what can be compared, with the coverage of the documents in scope, paged; beside them the
  values verified in earlier turns from the current readings (``verified_values``, ``Q#``, KTD12);
- ``take_value``: a value of a source the turn read, verified by the server — the cell at a named row and column
  of the table the source is (``extracted_tables.structure``), or a number inside an exact quote of the source —
  with its meaning; what the source attests about it is recorded as the source's, the rest as the model's (``V#``).
  A cell of a table ``inspect`` read resolves through the stored region reading (a quoted row of it as its cell):
  verified only when OCR of the crop or the region's text layer puts its number in its row and column, otherwise
  uncertain with the reason; its anchor is the region and, when confirmed, the cell's box (KTD6, R17–R19).
  A value read clearly is cached per version, reading and locator with what its source attests only, and a ``Q#``
  is taken again with that turn's meaning checked against it. A value carries the scale its source states it in
  (``calc.stated_scale``, round 7 R14): its own scale word ("5,600 אלף ₪"), else a note of its cell, row or column,
  else of its table's caption, title or notes ("(באלפי ₪)", "אלפי ש״ח", "K ₪"); for a quote, of the quote, else of
  its line; else, for either, of the heading-like line above it in its section that notes a scale and holds no
  figure of its own ("ממצאי הבדיקה באלפי ₪" over one figure per paragraph: ``calc.governing_note``, the nearest
  table with its own note stopping it); a number followed directly by a currency is in units, and a percent is
  never scaled;
- ``assume``: a number the user gave for a scenario, quoted from the user's own message (``A#``);
- ``calculate``: an expression over ``M#``/``V#``/``A#``/``C#`` (``app.chat.calc``): exact decimals, compatibility
  by operation, every result a ``C#`` with its formula, inputs, assumptions and sources that later calculations
  may use, and the scale its inputs' sources state (a result over amounts "באלפי ₪" is in thousands; inputs of
  different scales are brought to units first, and the model is told so); a result resting on an uncertain input is
  conditional and says why each input is uncertain (R18). A
  product of a document rate (a ``V#`` percentage, never the user's ``A#``) is compared with the amounts the rate's
  source and section state for the same quantity (round 7 U7, KTD8, R23): one within the product's range over the
  rate's rounding interval ("כ-17%": 16.5%–17.5%) is reported and kept in the record (``explicit_amount_available``,
  with its quote), and a result built on that product carries it — the model is told to take the stated amount
  unless the user asked for the rate, and nothing is substituted; one outside the interval is recorded as differing
  (``stated_amount_differs``), to be shown beside the result. The literals stay structural (``calc``): a rate nobody
  gave is refused, never applied.

**Pools** (round 10 U8: KTD11–KTD13, KTD15; R22–R32): what a search reads is decided by the server from the turn's settled
intent (``Workspace.pools``), never by the model. A professional question reads the standards and professional documents
(the professional pool) and sees appraisals only as labelled examples of one property's choice; a property question reads
appraisals, and a standard only on request and labelled as method, never as a property's datum; a mixed question reads
both; a set question appraisals; a document question the documents it names. The ``pool`` argument chooses among the
pools the intent allows; one it may not read is clipped with a note. A hit of a standard carries the document's identity
(``Source.standard``: number, name, edition, effective date, and the section it is in), rendered in its title and location
("תקן 98.1 (גרסה 2.0, בתוקף מ-…) — סעיף 2.1, עמוד 4"), the conditions and exceptions that limit its provision, and the call
that reads the whole section (``read(section=§#)``: the section with its subsections, the sections its references name and
the standards it names that no visible document carries, stated as absent). Identical text is counted once: the later
copies of one text and edition are left out of the pool (``professional.catalog``) and named in ``find_standards``. An
office that holds no standard says so, and the turn records the gap (``Workspace.standards_absent``).

**Two levels** (round 9 U9: KTD7, KTD8; R10–R13, R15): a file holding several appraisals is also searched in two steps.
``find_documents`` matches by *unit* (``platform.search.units_matching``: all the terms inside one unit — in its title or
a stated alias, or in its passages — so terms split between two appraisals match neither, and an appendix, or a property
named only in a comparison table, is reported as a comparable, never as an appraisal) and lists the units that match
(title or that it has none, pages, identifiers) with turn handles (``U#``, bound to the version and the reading like
``§#``); the units that follow a listed one with no identity of their own (an untitled part, an appendix) are listed under it as
its continuations (``↳``, round 17 U6), never as subjects. ``search`` (``unit=``), ``read`` (``pages`` with ``document=U#``) and ``outline`` (``unit=``) work inside one unit:
the scope is the unit's blocks, extended over the neighbouring unit up to the next *certain* edge where an edge of it is
only ``probable`` (``contexts.Contexts.reach``, R8); a unit search that finds no passage holding the query's words says
the unit was searched and nothing found, and names its uncertain edges: it never widens to the file on its own (round 15
U2, KTD10 — the model widens by an explicit call). A hit of a unit search shows at most ``SCOPED_PASSAGE_CHARS`` around the
query's terms and points at ``read(source=S#)`` for the rest. The outline of a file of more than ``OUTLINE_INLINE_UNITS`` units lists its units and
then the chosen unit's sections only, so no section list is cut at ``OUTLINE_MAX``. Every search hit, read source,
value, measurement and citation anchor of such a file carries the unit (``unit_id``: version, reading, number; with
title and pages for the answer's payload); a file with one unit shows and checks nothing new. A text hit that begins in a
unit and runs on into the next unit's blocks is cut at the unit's last block (``_clip_to_unit``: its text, last block
and pages; a table chunk the same, its rows by their contexts: ``_rows_in_scope``), and a read through a unit handle reads no block outside the unit's reach, so a cited passage never holds
another unit's text (R10).

**The unit in the conversation** (round 9 U10, KTD8; round 15 U6, KTD1, KTD3): when the request is in a unit — kept from the
previous turn across a metric change, or named by the user — the engine gives it a handle (``Workspace.unit_focus``), and
the candidate units a question points at get handles of their own (``Workspace.offered_units``); the model names the unit it
works in (``unit=U#``, ``document=U#``) and declares it in ``focus.units``. A call that names no unit is not silently scoped
(``focus_unit_for``); a call that names another document or a unit of its own is as asked. Only the server's pin of the
search-only path (``locate_unit``, ``Workspace.unit_default``: the unit the question names by its name or, where the units
state none, by their passages, ``entities.first_message``) scopes the calls that name no unit and no other document to it —
a second unit located in the turn is a comparison and drops that default; the pin is also the asked property when no title
or alias names one (``located_contexts``). The units a turn's tools worked in are kept (``Workspace.unit_scopes``), so a turn
cut short by a limit leaves the conversation in the unit it was reading.

**Time search** (round 12 U7: KTD6, KTD7; R9-R14, R20): in a turn with a time mode (the switch, or a requested time) a
``search`` about an identified property — a call naming a unit (whether or not it is the conversation's: round 15 U2, KTD10),
the documents a call names, or the address or parcel the question itself names — lists the property's appraisal units and their valuation dates first
(``property_inventory``, from the turn's property graph), selects up to ``CAP_UNITS`` of them in the mode's order, searches
each on its own (``_property_hits``) and ranks the hits per (version, unit) (``ranked_by_time``). What each call checked is a
coverage record (``coverage_record``: per unit located, no match or not searched for the cap, with an ``undated`` flag;
unlinked candidates; the rest of the office in three classes — possible candidates, unlinkable units, units proven to be
another property, from one probe, ``classify_undetermined``) kept in ``Workspace.time_calls`` with the S# the call returned;
the answer's coverage is that of the calls that produced the data it cites (``coverage_for``, ``merge_coverage``), settled
with what the turn read (``settle_coverage``: a unit is ``read`` when a datum of the cited datum's base key was registered
from it, a source of it is cited, or its section or table was opened; a newer unit only ``located`` blocks, round 13 U8). The server states
nothing in words here: the wording and the payload are built from the record. Without a time mode none of this runs.
Round 12 U12b: each source of a gathered unit other than the focus carries the server's link in its header (``link``:
"שומה של אותו נכס: חלקה 8401/40 משותפת"), the search result lists the gathered units with their reasons, and the judge reads
the link as part of that source's evidence. A timed turn answered from sources it only read (no time-ranked search) has the
record ``read_coverage`` builds: the units the answer cites ``read``, the others ``not_searched``.

A file holding several appraisals, or an appendix about a comparison property, is read as several appraisal contexts
(round 7 U6: KTD7; R20–R22; ``app.chat.contexts``, derived from the stored reading and cached per reading): search
hits, reads (with a marker where a context starts inside a part), the outline (each context's sections apart, a
merged table's rows by context) and ``take_value`` say the context; a ``V#`` records the context of its own block or
row (``calc.Value.context``) and whether its model-given subject names that context (``subject_from``: ``context``,
``asserted`` or ``contradicted``); an ``M#`` records its block's. What the request asks about (its components'
subjects, else the question) is matched against the contexts, and the tools say when it names none or several.
With ``chat_appraisal_context_enforced``, ``calculate`` refuses values of several contexts unless a frozen
calculation component compares them (``allowed_contexts``). A file with one context shows and checks nothing new.
"""

from __future__ import annotations

import calendar
import functools
import json
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Container, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import islice
from typing import Any, NamedTuple
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from app.answering.numread import read_separators
from app.answering.verify import numbers_in
from app.chat import anchors, calc, columns, contexts, datum, meaning, reader, scopes
from app.chat import dates as chat_dates
from app.chat import units as unit_store
from app.chat import unread as unread_text
from app.chat.contexts import MSG_NO_TITLE
from app.chat.evidence import (
    PART_REASON_MARKS,
    TABLE_AFTER,
    TABLE_BEFORE,
    TABLE_HEADING_LIKE,
    TABLE_PATH_PREFIX,
    TABLE_SIZE_PREFIX,
)
from app.chat.intent import MIXED, PROFESSIONAL
from app.chat.property import (
    REASON_ADDRESS_CITY,
    REASON_MENTIONED,
    REASON_PARCEL_PREFIX,
    REASON_PARTLY_READ,
    Gathered,
    Partition,
    PropertyGraph,
    build_graph,
    label_of,
)
from app.chat.timeframe import (
    B_CANDIDATES,
    B_LATER_UNDECIDED,
    B_LOCATED,
    B_NEWER,
    B_NO_MATCH,
    B_NOT_IDENTIFIED,
    B_NOT_SEARCHED,
    B_POSSIBLE,
    B_RANGE,
    B_SEVERAL,
    B_UNDATED,
    B_UNDETERMINED,
    B_WINDOW,
    LOCATED,
    NO_MATCH,
    NOT_SEARCHED,
    NOT_SEARCHED_CAP,
    PROPERTY,
    READ,
    UNIT,
    WINDOW,
    TimeMode,
)
from app.chat.timing import CURSORS_KEPT, StageClock
from app.config import get_settings
from app.db import TenantContext, current_data_version, tenant_tx
from app.extraction import tables as stored_tables
from app.extraction.chunking import render_row
from app.measurements.extract import (
    EXTRACTION_VERSION,
    PERIOD_LABELS,
    STANCE_LABELS,
    STANCES,
    UNIT_LABELS,
    VAT_LABELS,
    Attribution,
    attribution_at,
    attribution_in,
    names_match,
    stance_label,
)
from app.platform import dates as platform_dates
from app.platform import source_kinds
from app.platform.dates import precision_of, shown_date, shown_period
from app.platform.documents import coverage_of, display_box_of, reading_notes
from app.platform.search import (
    SearchScope,
    measure_searches,
    search_passages,
    topic_tokens,
    unread_documents,
    unread_matching,
    unread_named,
)
from app.platform.source_kinds import APPRAISALS_SQL, PROFESSIONALS_SQL
from app.professional import catalog, profile, sections

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 6
SEARCH_MAX = 12
# the characters read around a source, by scope; "candidates" (round 14 U2, KTD3) is not a read scope: it caps the text above a value that
# the bounded context read looks at, in place of a count of blocks
CONTEXT_CHARS = {"neighbors": 3500, "section": 9000, "table": 14000, "candidates": 6000}
READ_SCOPES = ("neighbors", "section", "table")
CANDIDATE_BLOCKS = 40  # the most blocks above a value the context read fetches in one query; a read that needs more is capped
# the reader that wrote a cached facet (round 14 U3, KTD5): a record without ``facet_reader`` is the round-13 reader's. A cached take whose
# facet is not stated, or was read by an older reader, reads the context again from the stored blocks (no model call); the verifier
# version stays, because a bump would also drop ``_known_clear`` reuse and the re-reads that follow can call a model (R17)
FACET_READER_VERSION = 2
MEASUREMENT_FACET_ROWS = 40  # the text measurements of one listing whose context is read; the rows beyond it are not read (reason cap)
MEASUREMENTS_MAX = 120  # per page
LIST_MAX = 60  # per page
SCOPE_PAGE = 30

ROLE_LABELS = {
    "appraiser_determination": "קביעת השמאי", "actual_contract": "חוזה בפועל",
    "comparable_transaction": "עסקת השוואה", "asking_price": "מחיר מבוקש", "survey_statistic": "נתון סקר",
    "calculation": "תוצאת תחשיב", "planning_legal": "תכנוני/משפטי", "other": "אחר",
}
SUBJECT_ROLE_LABELS = {
    "appraised_property": "הנכס הנישום", "comparable": "נכס השוואה", "survey": "סקר", "asking": "היצע",
    "contract": "חוזה", "general": "כללי", "other": "אחר",
}
KIND_LABELS = {
    "value_per_area": "שווי ליחידת שטח", "price_per_area": "מחיר ליחידת שטח", "rent_per_area": "דמי שכירות ליחידת שטח",
    "management_fee_per_area": "דמי ניהול ליחידת שטח", "cost_per_area": "עלות ליחידת שטח", "value": "שווי",
    "price": "מחיר", "rent": "דמי שכירות", "management_fee": "דמי ניהול", "cost": "עלות", "levy": "היטל/מס",
    "area": "שטח", "rights_area": "שטח זכויות", "rate": "שיעור", "coefficient": "מקדם", "count": "כמות",
    "duration": "משך", "other": "אחר",
}

# A value's or measurement's status (R25), as the frontend shows it (``VALUE_STATUS`` in ``frontend/lib/format.ts``):
# its own text and icon each, never colour alone. Using a value never waits for a person; "checked by a person" is
# said only when a person's review is recorded.
STATUS_AUTO = "✓ נבדק אוטומטית"
STATUS_HUMAN = "✎ אומת או תוקן על ידי אדם"
STATUS_UNCERTAIN = "? לא ודאי"
STATUS_UNREAD = "∅ לא נקרא"
PERSON_DECISIONS = ("verified", "corrected")  # the statuses only a person's review sets (``measurements.review``)
REREADS_PER_VALUE = 2  # focused re-reads of an unclear value's region per turn (R28)


# how deep the turn reached into a document, in order: listed in a set or outlined; a passage retrieved (a search
# hit, or the paragraphs around it); a section or table read (or its stored measurements listed); its datum cited
# by the verified answer
LEVELS = ("located", "retrieved", "read", "verified")
NOT_REACHED = "not_reached"  # a document of the set the turn never touched
READ_TO_END = "end"  # a read target read from its beginning to its end (``Workspace.read_progress``)
# The state of a read target (round 22 U7, R18, R11; ``Workspace.read_state``): the only state that may produce absence wording is a
# completed read - and only for the region it read, of a datum that read did not hold ("completed with no finding", decided by the consumer).
# The one gate is ``coverage.reason_of`` through ``Workspace.failed_reads`` (a read that broke for a technical reason blocks the wording);
# a partial read is told apart by ``document_read_in_part`` / ``region_not_read`` (``Workspace.activity``), not by this state
READ_COMPLETED = "completed"  # read to its end, no unread region
READ_PARTIAL = "partial"  # read in part: ``ReadState.unread`` says how (``clipped``: stopped before its end; ``unread``: a region the document's reading left unread)
READ_FAILED = "failed"  # the read broke for a technical reason: the tool raised, or a vision reading failed
READ_BUDGET_SKIPPED = "budget_skipped"  # the tool budget or the reading time stopped the call before it ran
READ_REFUSED = "refused"  # the server refused the call (a bad handle, a missing table): the request's fault, no region was read
TECHNICAL_READ_FAILURES = frozenset({"tool_failed", "read_failed"})  # the ``failed_with`` keys of a read that broke (not a refusal)
TOOL_BUDGET = "tool_budget"  # ``Workspace.limits_hit``: the turn's tool-output budget is spent
TIME_LIMIT = "time_limit"  # ``Workspace.limits_hit``: the turn's reading time is up (``inspect``, or the loop's clock)
STEP_LIMIT = "step_limit"  # ``Workspace.limits_hit``: the turn took the most steps it may (``engine``)


def read_chars() -> int:
    """The size of a part of a section, a page range or the paragraphs around a source (KTD12)."""
    return get_settings().chat_read_chars


def table_rows() -> int:
    """The rows in a part of a table (KTD12)."""
    return get_settings().chat_table_rows


class ReadState(NamedTuple):
    """The state of one read target (``Workspace.read_state``): ``state`` - one of ``READ_COMPLETED``, ``READ_PARTIAL``, ``READ_FAILED``,
    ``READ_BUDGET_SKIPPED``, ``READ_REFUSED`` - and, for a partial read, ``unread``: ``clipped`` or ``unread``."""

    state: str
    unread: str = ""


class ToolError(Exception):
    """A tool call the server refuses (bad arguments, unknown ids); its message goes back to the model. ``key``: a stable
    short reason (``refused`` unless the raise site names one), which the audit of a failed registration records
    (round 14 U1, KTD11)."""

    def __init__(self, message: str = "", key: str = "refused") -> None:
        super().__init__(message)
        self.key = key


@dataclass
class Source:
    sid: str
    document_id: UUID | None  # None for a listing: it names documents, it is not one
    version_id: UUID | None
    title: str
    section: str | None
    location: str
    kind: str
    text: str
    block_start: int | None = None
    block_end: int | None = None
    table_index: int | None = None
    chunk_id: UUID | None = None
    page_list: list[int] | None = None
    partial_document: bool = False
    same_as: str | None = None  # an earlier id of this turn that returned the same text (not sent again)
    listed: list[str] = field(default_factory=list)  # a listing's documents (its page), for permission checks
    # a listing's set: {"key", "criterion", "total", "page", "pages", "documents": [{document_id, title}]}
    listing: dict | None = None
    # the reading its blocks were read from (``document_versions.ingestion.reading_id``, KTD7): a reference to it
    # after the version was read again is stale
    reading_id: str | None = None
    # what a ``read`` returned (KTD9): complete | clipped | has_unread_regions | uncertain_reading; whether it was
    # clipped, the R# handles of the regions in it that were not read, and the handle that reads on (K#, or the
    # section around paragraphs)
    status: str | None = None
    clipped: bool = False
    unread_regions: list[str] = field(default_factory=list)
    more: str | None = None
    resume: dict | None = None  # where it continues, as a position that outlives the turn (a later P#)
    # what was sent to the model when it is not ``text``: blocks the turn already returned are sent as a pointer,
    # while ``text`` stays the full text the verifier checks against
    body: str | None = None
    tags: dict = field(default_factory=dict)  # handles shown on the source's tag: {"document": "D1", "table": "T2"}
    table_part: bool = False  # some rows of a table read in parts, not all of them
    method: str | None = None  # how its text was read when not from the text layer (``vision`` for an inspection)
    row_index: int | None = None  # a table-row search hit: the body row of its table (``structure.rows``) it is
    # a visual reading by ``inspect`` (KTD6): {"region", "page", "reading": its ``PictureReading`` (tables and OCR
    # evidence), "tables": the vision T# of each table}; its cells are taken through the reading, never through
    # ``extracted_tables``
    vision: dict | None = None
    # the appraisal contexts it is in, in a file holding several (round 7 U6, KTD7): their numbers, and as shown
    contexts: list[int] = field(default_factory=list)
    context: str | None = None
    # the unit it is in, in a file holding several (round 9 U9, KTD7, R10): {"id": version#reading#number, "no", "title",
    # "label", "kind", "pages": [first, last]}; the stable id outlives the turn, the unit handle (U#) does not
    unit: dict | None = None
    # the identity a file of one subject unit states for itself (its unit's label: the title and identifiers read from
    # the document), shown to the judge beside the source; empty in a file of several contexts, whose sources say
    # their context instead
    subject: str = ""
    # the nearest lot or section heading above a hit whose chunk carries none (round 21 U8, KTD6, R11): the heading path of its first
    # block, shown in the envelope so a row or a paragraph under «מגרש 2» is read as that lot's; empty when the chunk has its own
    heading: str = ""
    # the kind of document it is from, and — for a standard or another professional document — what identifies the
    # passage (round 10 U8, KTD13): {number, title, edition, effective, section, section_title}, each None where the
    # document states nothing; title and location render it. ``label``: how the pool labels the hit for the model (an
    # appraisal's example under a professional question, a standard's method under a property question)
    source_kind: str = source_kinds.APPRAISAL
    standard: dict | None = None
    label: str | None = None
    # what the sections of a professional passage say about how binding they are (round 10 U9, KTD14): {"limits": the
    # condition and exception sentences that bind the provision, "modality": the counts of requirement, recommendation,
    # example, alternative and exception wording}, read from the stored section structure when the passage was
    # returned; the verifier's evidence for it. None for an appraisal and for a passage in no section
    rules: dict | None = None
    # the date line of a hit of an appraisal (round 11 U8, KTD6): "למועד הקובע 15/04/2025" — the valuation date of the unit
    # holding the passage — or "מועד לא ידוע"; shown in the source's header whatever the time mode
    date_line: str | None = None
    # the server's link of a hit of a gathered appraisal other than the focus (round 12 U12b, R3): "שומה של אותו נכס: חלקה
    # 8401/40 משותפת" — shown in the header beside the date line and to the judge as part of the source's evidence, so the
    # appraisal at another house number is not disowned as another property's. None for the focus and outside a time search
    link: str | None = None
    # a source the server made for its own checks during verification (a re-read's context, an expansion, a trial): the agent was
    # never shown it, so it is no read passage and no support for the requirements judge (round 16 follow-up)
    server_made: bool = False
    # the context parts a table read carries, as data (round 18 U4, KTD5): the blocks before and after the table with the evidence
    # that attributes each to it (``TablePart``); empty for a source that is not a table read. Verification reads them from here,
    # not from the tags in ``text``, and needs no database read for it
    table_parts: list = field(default_factory=list)
    # the notes of its table that apply to the rows a search hit holds and that its text lacks (round 22 U5, KTD5, R8; finding g): the chunker writes a
    # table's notes only into the last table chunk, so a row chunk or an earlier group chunk is completed from the stored structure when the hit is
    # made (``_hit_notes``). Marked notes only for the rows that carry the mark, unmarked ones for the table; empty for a source that is no table hit.
    # The verifier reads them as notes of the table (``verify.context_of_source``); they are not text the agent was shown, so they are no evidence of a value
    table_notes: list = field(default_factory=list)

    @property
    def is_listing(self) -> bool:
        return self.kind == "listing"

    def public(self) -> dict:
        out = {"id": self.sid, "document_id": str(self.document_id) if self.document_id else None,
               "version_id": str(self.version_id) if self.version_id else None,
               "title": self.title, "section": self.section, "location": self.location, "kind": self.kind,
               "text": self.text, "block_start": self.block_start, "block_end": self.block_end,
               "table_index": self.table_index, "page_list": self.page_list, "reading_id": self.reading_id,
               "status": self.status, "clipped": self.clipped, "unread_regions": len(self.unread_regions),
               "more": self.resume, "method": self.method, "row_index": self.row_index,
               "source_kind": self.source_kind, "standard": self.standard, "date_line": self.date_line}
        if self.context:
            out["context"] = self.context
        if self.link:
            out["link"] = self.link
        if self.unit:
            out["unit"] = self.unit
        if self.is_listing:
            out["listed_document_ids"] = list(self.listed)
        return out


@dataclass
class Measurement:
    mid: str
    id: UUID
    document_id: UUID
    version_id: UUID
    title: str
    row: Any
    listings: set = field(default_factory=set)  # the find_measurements listings that returned it
    context: dict | None = None  # its appraisal context in a file holding several (KTD7), from its block
    # the date it refers to (round 11 U8, KTD4; ``dates.measurement_as_of``): the stored date counts only when the quote holds
    # it; None when unknown. A forecast with no date is ``{"date": None, ..., "forecast": True}``
    as_of: dict | None = None
    # the component its source states (round 13 U3, KTD2; ``datum.read_cell`` / ``read_quote``): resolved when listed, from
    # its table row or its quote; None until then (``public`` then says not read)
    facet: dict | None = None

    def public(self) -> dict:
        r = self.row
        return {"id": self.mid, "measurement_id": str(self.id), "document_id": str(self.document_id),
                "version_id": str(self.version_id), "title": self.title, "metric": r.metric,
                "metric_kind": r.metric_kind, "value_text": r.value_text, "unit": r.unit, "period": r.period,
                "vat": r.vat, "area_basis": r.area_basis, "subject": r.subject, "value_role": r.value_role,
                "status": r.status, "quote": r.quote, "section": r.section, "block_index": r.block_index,
                "table_index": r.table_index, "anchor_lost": anchor_lost(r),
                "reading_id": getattr(r, "reading_id", None),
                # optional (KTD8): who stated it and how, only as its text attested at extraction
                "stated_by": getattr(r, "stated_by", None), "stance": getattr(r, "stance", None),
                # the unit its block is in, in a file holding several (round 9 U9, R10)
                "unit_id": self.context["unit_id"] if self.context else None,
                "as_of": dict(self.as_of) if self.as_of else None,
                "facet": datum.public_facet(self.facet)}


def anchor_lost(row) -> bool:
    """A reviewed measurement whose place was not found again when its document was reprocessed (KTD7): its
    value keeps the reviewer's decision, but it is not verified against a cell or passage of the current reading."""
    return getattr(row, "anchor_lost", None) is not None


def measurement_uncertain(row) -> bool:
    """A measurement that cannot be presented as certain: its place was lost on reprocessing, or extraction flagged
    it for a look (``needs_review``). Uncertain, not blocked: it is still usable, shown as such."""
    return anchor_lost(row) or row.status == "needs_review"


def measurement_status(row) -> str:
    """A stored measurement's status label (M#): uncertain first; a person's decision only when a review set it and
    recorded who made it (``reviewed_by``); otherwise checked automatically, which needs no approval to be used."""
    if measurement_uncertain(row):
        return STATUS_UNCERTAIN
    if row.status in PERSON_DECISIONS and getattr(row, "reviewed_by", True) is not None:
        return STATUS_HUMAN
    return STATUS_AUTO


@dataclass
class Workspace:
    """Everything one turn gathered: sources, measurements, computations, and earlier-turn references."""

    ctx: TenantContext
    sources: dict[str, Source] = field(default_factory=dict)
    measurements: dict[str, Measurement] = field(default_factory=dict)
    computations: dict[str, calc.Computation] = field(default_factory=dict)
    values: dict[str, calc.Value] = field(default_factory=dict)  # V#: values verified in a source of the turn
    assumptions: dict[str, calc.Assumption] = field(default_factory=dict)  # A#: numbers the user gave
    # the user's messages visible to the turn, oldest first, the current one last: [{"turn", "text", "current"}]
    user_messages: list[dict] = field(default_factory=list)
    prior: dict[str, dict] = field(default_factory=dict)  # P# -> {version_id, block_start, block_end, chunk_id}
    searches: list[str] = field(default_factory=list)
    coverage: list[dict] = field(default_factory=list)
    # document id -> {"title", "level", "read", "partial", "openings": [{"sid", "scope", "name", "target"}]}: how deep
    # the turn's tools reached into each document (``LEVELS``), whether a section or table of it was read, and the
    # sections, tables and page ranges they opened — and the sections a read of paragraphs or pages returned whole
    # (``covers``, ``_whole_sections``; several sections: scope ``sections``, ``name`` their quoted names)
    activity: dict[str, dict] = field(default_factory=dict)
    # the set a question is about: {"query", "matching": [{document_id, title}], "pages", "pages_read"}
    scope: dict | None = None
    # find_measurements listings: filter key -> {"pages", "pages_read", "total"}
    listings: dict[tuple, dict] = field(default_factory=dict)
    returned: dict[tuple, str] = field(default_factory=dict)  # what was already returned -> its first id
    fetched: dict = field(default_factory=dict)  # what the meaning check read for the turn (``meaning.Fetcher``)
    # version id -> its reading id as the turn first read it (KTD7). Pinned for the turn: a version read again
    # mid-turn makes the turn's earlier sources of it stale, never silently re-bound.
    readings: dict = field(default_factory=dict)
    # turn-local handles (D#, §#, T#, K#, R#) -> what they name ({"kind", "version_id", "reading_id", ...})
    handles: dict[str, dict] = field(default_factory=dict)
    handle_ids: dict[tuple, str] = field(default_factory=dict)
    # version id -> block index -> the S# that first returned the block's whole text (block-level dedupe)
    sent: dict[str, dict[int, str]] = field(default_factory=dict)
    # what was read of each section, page range or table (a ``read`` target): {"to": how far it was read
    # contiguously from its beginning (``READ_TO_END`` at its end), "unread": a region of it was not read,
    # "document_id"}
    reads: dict[tuple, dict] = field(default_factory=dict)
    # the read cursors (K#) the agent used in ``read`` (round 23 U0, KTD11): ``unfollowed_cursors`` lists those it was given and never used
    followed_cursors: set = field(default_factory=set)
    # the turn's settled intent (round 10 U6; ``intent.Intent.value``): what the tools' pools are set from; ``unknown``
    # until the request is read, and when it is not known
    intent: str = "unknown"
    # the turn's time mode and requested time (round 11 U7, KTD5; ``timeframe.resolve``), set once per turn by the engine
    # from the question and the user message's recorded state: what the tools' ranking reads (U8). It is the turn's own: no
    # slot, intent or pool is set from it (R16)
    time: TimeMode = field(default_factory=TimeMode)
    # the valuation dates and units read for the turn's ranking and date lines (``dates.valuation_dates``'s ``cache``)
    date_cache: dict = field(default_factory=dict)
    # a caller whose group of results is capped by contract (search-only mode: at most 5 appraisal passages) keeps the pass
    # limit exact: a time mode orders the relevant documents but never returns more hits than the limit (round 11 U8)
    exact_limit: bool = False
    # a document question's named documents (round 10 U8): the only documents its searches read (``tool_search``)
    named_documents: tuple[str, ...] = ()
    # version id -> (source kind, what its profile states) for a professional version, False for an appraisal (the
    # identity ``standard_for`` gives a source); True once a professional pool search or listing found the office holds
    # no standard or professional document (the coverage gap "נוסח התקן אינו במאגר", KTD15)
    standards: dict[str, Any] = field(default_factory=dict)
    standards_absent: bool = False
    profiled: bool = False  # the turn derived the profiles that were missing (``profiles``)
    # the copies of professional text the pool leaves out, read once per data version: (version, excluded versions)
    copies: tuple | None = None
    # read once per data version: the visible professional documents' editions (``profiles``) and whether any exists
    # (``has_professional``), each as (data version, value); a professional version's sections, by (version id, data
    # version) (``sections_in``)
    profile_memo: tuple | None = None
    professional_memo: tuple | None = None
    section_cache: dict[tuple, tuple] = field(default_factory=dict)
    inspections: int = 0  # vision model calls ``inspect`` made in the turn (capped by ``chat_max_inspections``)
    # the turn's reading deadline (``time.monotonic()``; None outside a turn): a vision call of ``inspect`` is cut to
    # it, and none starts with less than ``chat_inspect_reserve_seconds`` left
    read_until: float | None = None
    audited: set = field(default_factory=set)  # documents the turn already wrote a ``source_view`` audit row for
    limits_hit: list[str] = field(default_factory=list)  # turn limits reached (``inspect_cap``, ``tool_budget``...)
    # characters of tool output the turn may send the model (``chat_tool_output_chars``; None: no budget), and how
    # many it sent: once they are spent, reading tools return a header and how to read on, never a body
    tool_budget: int | None = None
    tool_chars: int = 0
    # the turn's model-call cost records (``usage_entry``), when the turn passes its own: a tool's model call is then
    # logged with the turn's calls; None: the tool's reader logs its call itself
    usage: list[dict] | None = None
    # S#, V#, M# -> where it points, as the tool read it (``app.chat.anchors``): snapshotted when the answer is stored
    anchors: dict[str, dict] = field(default_factory=dict)
    # a value's locator (version, reading, where in it) -> the focused re-reads of its unclear region the turn made
    # (at most ``REREADS_PER_VALUE``); V# -> why it stays uncertain after them (R28)
    rereads: dict[tuple, int] = field(default_factory=dict)
    uncertain_values: dict[str, str] = field(default_factory=dict)
    # V# whose unit its source leaves ambiguous -> why (round 15 U1, KTD9): registered with the model's unit, never an input
    # of ``calculate``
    ambiguous_units: dict[str, str] = field(default_factory=dict)
    # V# whose own region was read clearly (at ingestion, or by a re-read): an uncertain region elsewhere in its
    # source does not make it uncertain
    settled_values: set = field(default_factory=set)
    # version id -> its appraisal contexts as the turn derived them (round 7 U6, KTD7; ``app.chat.contexts``)
    contexts: dict[str, Any] = field(default_factory=dict)
    # the turn's frozen requirements (``verify.TurnRequirements``), set by the engine: the subject a component asks
    # about, and the subjects a calculation component compares
    requirements: Any = None
    context_notes: set = field(default_factory=set)  # documents whose contexts a read or a search already listed
    # documents the turn's lookups matched by title whose latest version was not read (round 9 U6, KTD11): document id
    # -> its state (``search.unread_documents``). Never located or retrieved: nothing was searched in them
    unread_named: dict[str, dict] = field(default_factory=dict)
    _unread_files: list[dict] | None = None
    # the reply's binding to the pending parameter of the previous turn's clarification (``resolve.bind_pending``),
    # set by the engine: an ``assume`` of the bound number from the current message records that parameter (KTD9)
    binding: dict | None = None
    # read once per turn: (version id, table index) -> (its stored structure, the block it is read at) (``_table_at``);
    # (version id, reading id) -> every block's index, section path and status (``reader.section_blocks``), and each
    # table's first block with its rows' positions (``_table_rows``)
    tables: dict[tuple, tuple] = field(default_factory=dict)
    # facets resolved this turn (round 13 U3, KTD2): a value's by its place and family, a measurement's by its id, so a
    # datum met again is not read again
    facets: dict[tuple, dict] = field(default_factory=dict)
    # V# and M# -> where its number is in its source (round 14 U1, KTD1; ``value_span``, ``measurement_span``): the datum
    # decision reads it, so one number registered twice is one datum. Absent: the place is not known to a number
    spans: dict[str, tuple] = field(default_factory=dict)
    # stored measurement row id -> its Measurement, kept by ``add_measurement`` (``measurement_of``)
    _by_row: dict = field(default_factory=dict, repr=False)
    # the take_value calls the server refused (round 14 U1, KTD11, R15), at most ``REGISTRATION_LOG_MAX``: stored in the
    # message diagnostics as ``resolution["registrations"]``
    registration_log: list[dict] = field(default_factory=list)
    section_blocks: dict[tuple, list] = field(default_factory=dict)
    table_rows: dict[tuple, dict] = field(default_factory=dict)
    # round 23 U4 (M3, KTD8): the page-text column binding of the turn (``columns``, ``_served_rows``). ``page_words``: (version, page) -> the page's
    # blocks with word positions and its rendered width; ``column_bindings``: (version, block, text) -> its ``columns.Binding``; ``column_unknown``:
    # (version, block) of every row of numbers whose columns could not be told - the judge's "column binding unknown" fact (``column_binding_note``)
    page_words: dict[tuple, tuple] = field(default_factory=dict)
    column_bindings: dict[tuple, columns.Binding] = field(default_factory=dict)
    column_unknown: set[tuple] = field(default_factory=set)
    # the turn's salt for claim and unit ids (round 8 KTD2, ``verify.assign_ids``): an id is a hash of the salt and the
    # claim's context, so it is stable across the turn's rounds and reveals no text. Random per turn; a test sets it.
    claim_salt: str = field(default_factory=lambda: uuid4().hex[:16])
    # the bounded re-read of claims (round 8 U5, KTD5, ``verify.verify_answer``): the claim ids whose one expansion
    # the turn spent (``chat_reread_claims`` of them at most), and each claim's re-reads for the log
    # ({"source", "scope", "outcome", ...}), kept across the turn's verification rounds
    reread_done: set = field(default_factory=set)
    reread_log: dict = field(default_factory=dict)
    # the neutral fact line the judge reads beside a unit whose evidence's owner the server could not settle (round 19 U2, KTD2, R6):
    # (the unit's normalised text, its cited ids) -> the line; written by the context check (``verify._context_check``), read by the
    # judge's input and the unit's reuse key
    ownership_facts: dict = field(default_factory=dict)
    # the blocks above a cited source with their word geometry, preloaded for the scope reader (round 20 U3, KTD3; ``rereader.ContextReader.preload``,
    # ``scopes.Layout``): S# -> its layout, or None when the fetcher could not read one. And the table cells a scope check found under another
    # plan, parcel, contract or alternative than their column names: (the unit's normalised text, its cited ids) -> {cell index: reason}
    layouts: dict = field(default_factory=dict)
    scope_findings: dict = field(default_factory=dict)
    # the lines of the title block of a unit of a file holding several (the blocks at the top of its first page, up to its first heading), read
    # for the same-property fact (``verify._name_link``): (version id, unit number) -> the lines
    unit_titles: dict = field(default_factory=dict)
    # the blocks of the section a re-read source cites, kept for the turn (``rereader._section_rows``): (version, first block,
    # reading) -> (the heading chain, the blocks), so the section is loaded once for the lead-in, the notes and the heading
    section_rows: dict = field(default_factory=dict)
    # what depends only on a source's text, kept for the turn (``verify._unit_candidates``): id -> (text, the numbers in
    # it, the words in it); an entry is used only while the source still has that text
    source_terms: dict = field(default_factory=dict)
    # the document lookups of the coverage ledger that depend only on the question or the scope query, read once per
    # turn (``coverage.build``): ("named", question), ("shared", question) or ("matching", query) -> the rows
    lookups: dict = field(default_factory=dict)
    # the turn's time per stage and per tool call (round 9 R16, KTD9, ``app.chat.timing``), fed by ``run_tool`` and the engine
    clock: StageClock = field(default_factory=StageClock)
    # the unit the conversation is in (round 9 U10, KTD8), set by the engine from the resolved request: {"handle": its
    # U#, "document_id", "display"}. A ``search`` or ``outline`` that names no unit and no other document works inside it
    unit_focus: dict | None = None
    # the unit this turn's own lookup located for the question (``locate_unit``: ``find_documents`` listing exactly one
    # subject unit, or the engine's look at a first message): {"key": its unit id, "anchor"}; ``unit_focus`` is then its
    # handle. ``turn_located``: every unit so located (unit id -> anchor) — a second one is a comparison, not one property
    located_unit: dict | None = None
    turn_located: dict[str, dict] = field(default_factory=dict)
    # the units the turn's tools worked in through a unit handle: unit id -> {"document_id", "version_id", "reading_id",
    # "unit_no"}; the unit a turn that ended on a limit leaves the conversation in when nothing it cited says
    unit_scopes: dict[str, dict] = field(default_factory=dict)
    # round 15 U6 (KTD1, KTD3, KTD7): ``unit_default`` — the server's pin scopes a call that names no unit to ``unit_focus``
    # (set only by the search-only path, which has no model to choose a unit); ``offered_units`` — the handles of the
    # candidate units the turn's first message showed the model; ``declared_units`` — the units the answer declared in
    # ``focus.units`` and the server validated against the offered ones and the ones the turn read (anchors, in the model's
    # order), which the next unit's verification and the turn's unit read; ``declared_handles`` — the same units keyed by the
    # handle (U#) the model declared each by (``engine._declare_units`` sets both); ``listed_units`` — the handles of the
    # units ``find_documents`` and ``outline`` listed: a clarification may offer them, though they are neither read nor
    # offered by the server (so ``coverage._offered_only`` does not count them)
    unit_default: bool = False
    offered_units: set = field(default_factory=set)
    listed_units: set = field(default_factory=set)
    declared_units: list = field(default_factory=list)
    declared_handles: dict[str, dict] = field(default_factory=dict)
    # the passages an identical search returned, kept for the turn (round 9 R16, KTD3, ``memoized_search``):
    # (query, scope, limit, the office's data version) -> hits
    search_memo: dict = field(default_factory=dict)
    # the turn's property graph, per the office's data version (round 12 U6, KTD5: ``property_graph``, which also keeps the
    # gathered sets and the partitions it computes); the property a call's named documents are of, per (the documents, the
    # data version) (round 11 U16)
    property_memo: dict = field(default_factory=dict)
    # the time-ranked search calls of the turn (round 12 U7, KTD7; ``TimeCall``): each with the coverage record of its own
    # inventory, built from the same graph snapshot, and the S# its results became. ``time_coverage`` is the record of all
    # of them together (``merge_coverage``); U8 asks ``coverage_for`` for the calls that produced the cited data. Empty and
    # None in a turn without a time mode (R14)
    time_calls: list = field(default_factory=list)
    time_coverage: dict | None = None
    # chunk id -> (first block, last block): what the ranking and the rendering of a search's hits read (``chunk_blocks``)
    chunk_memo: dict = field(default_factory=dict)
    # the windows an identical read returned, kept for the turn (round 9 R16, KTD9, ``_memoized_read``): (a page range,
    # section or cursor window, or a table part; the reading; the office's data version) -> the source it returned
    read_memo: dict = field(default_factory=dict)

    @property
    def pools(self) -> dict[str, str]:
        """The pools the turn's tools may read and the role of each (round 10 U8, KTD12), from the settled intent — the one
        place the intent decides them, so they can never disagree with it: ``primary`` answers the question, ``example``
        shows an appraisal only as one property's choice, ``method`` shows a standard only for the method it explains,
        never as a property's datum."""
        return dict(INTENT_POOLS.get(self.intent, INTENT_POOLS["unknown"]))

    def spend(self, output: str) -> str:
        """Count a tool output sent to the model against the turn's budget; the output that reaches it is sent
        whole (each result is bounded by its own size), and the budget is then spent (``TOOL_BUDGET``)."""
        self.tool_chars += len(output)
        if self.tool_budget is not None and self.tool_chars >= self.tool_budget and TOOL_BUDGET not in self.limits_hit:
            self.limits_hit.append(TOOL_BUDGET)
        return output

    @property
    def budget_spent(self) -> bool:
        return TOOL_BUDGET in self.limits_hit

    @property
    def requirement_items(self) -> list[dict]:
        """The turn's frozen components (``requirements.items``); none before the engine sets them."""
        return getattr(self.requirements, "items", None) or []

    def handle(self, prefix: str, key: tuple, **data) -> str:
        """The turn's short handle for what ``key`` names; the same thing keeps its handle."""
        h = self.handle_ids.get((prefix, key))
        if h is None:
            h = f"{prefix}{sum(1 for x in self.handles.values() if x['kind'] == prefix) + 1}"
            self.handles[h] = {"kind": prefix} | data
            self.handle_ids[(prefix, key)] = h
        return h

    def doc_handle(self, document_id) -> str:
        return self.handle("D", (str(document_id),), document_id=str(document_id))

    def read_progress(self, target: tuple, start, nxt, document_id: str, unread: bool) -> None:
        """Record a part of ``target`` read from ``start`` (None: its beginning) to ``nxt`` (None: its end). A target
        is read completely only when its parts followed each other from the beginning to the end; the document's
        activity then says whether each target it read was read completely (``read_complete``) or not
        (``read_partial``)."""
        st = self.reads.setdefault(target, {"to": None, "unread": False, "document_id": document_id})
        if start is None or st["to"] == start:
            to = READ_TO_END if nxt is None else nxt
            if st["to"] is None or to == READ_TO_END or (st["to"] != READ_TO_END and to > st["to"]):
                st["to"] = to
        st["unread"] = st["unread"] or unread
        done = [s["to"] == READ_TO_END and not s["unread"] for s in self.reads.values()
                if s["document_id"] == document_id]
        a = self.activity.get(document_id)
        if a is not None:
            a["read_complete"], a["read_partial"] = any(done), not all(done)

    def unfollowed_cursors(self) -> list[dict]:
        """The continuation cursors (K#) the turn's reads returned and the agent never followed (round 23 U0, KTD11, R15), for the diagnostics only:
        the cursor, the first source that carried it, its document, the kind of its target, the position it reads on from and how much remained
        after the part (``left``: blocks and characters of a window, rows of a table - recorded when the part was cut). A cursor is followed
        when ``read`` was given it, or when the same target was since read to its end or past its position (by any route). A pointer to read a
        section whole (a § handle in ``Source.more``) is no cursor. Numbers, handles and ids: no text."""
        out: list[dict] = []
        seen: set[str] = set()
        for sid, s in self.sources.items():
            cursor = s.more
            data = self.handles.get(cursor) if cursor else None
            if data is None or data.get("kind") != "K" or cursor in seen or cursor in self.followed_cursors:
                continue
            seen.add(cursor)
            if self._read_past(data.get("target"), data.get("pos")):
                continue
            doc = str(data.get("document_id") or s.document_id or "")
            pos = data.get("pos")
            out.append({"cursor": cursor, "source": sid, "document_id": doc,
                        "document": self.handle_ids.get(("D", (doc,))), "target": (data.get("target") or ("",))[0],
                        "pos": list(pos) if isinstance(pos, tuple) else pos, "left": data.get("left")})
            if len(out) >= CURSORS_KEPT:
                break
        return out

    def _read_past(self, target, pos) -> bool:
        """Whether ``target`` was read to its end, or beyond ``pos`` (where a cursor reads on from)."""
        st = self.reads.get(target) if target is not None else None
        if st is None:
            return False
        if st["to"] == READ_TO_END:
            return True
        try:
            return st["to"] is not None and st["to"] > pos
        except TypeError:
            return False

    def read_complete(self, target) -> bool | None:
        """Whether a target was read to its end with no unread region (None: never read as a target)."""
        st = self.reads.get(target) if target is not None else None
        return None if st is None else st["to"] == READ_TO_END and not st["unread"]

    def read_state(self, target) -> ReadState | None:
        """The state of a read target (round 22 U7, R18): a section, table or page range (the tuple ``read_progress`` keeps) is completed or
        partial (``unread`` says how); a call's locator (the string ``_read_target`` writes on the stage clock, U0) is completed, failed,
        budget-skipped or refused. None for a target never read. The two records are the ones that already existed; this reads them together."""
        if isinstance(target, str):
            st = self.clock.reads.get(target)
            if st is None:
                return None
            if st["completed"]:
                return ReadState(READ_COMPLETED)
            if st["budget_skipped"] or st["failed_with"] == TIME_LIMIT:
                return ReadState(READ_BUDGET_SKIPPED)
            return ReadState(READ_FAILED if st["failed_with"] in TECHNICAL_READ_FAILURES else READ_REFUSED)
        st = self.reads.get(target) if target is not None else None
        if st is None:
            return None
        if st["to"] == READ_TO_END and not st["unread"]:
            return ReadState(READ_COMPLETED)
        return ReadState(READ_PARTIAL, "unread" if st["to"] == READ_TO_END else "clipped")

    def failed_reads(self, documents: Iterable[str] | None = None) -> list[str]:
        """The call locators of the turn whose read broke for a technical reason and was not completed since, those that named one of
        ``documents`` (every one when None or empty): a document with such a read was not searched in the region that read opens, so no
        line may say the datum is not in it (round 22 U7, R18). A failed read whose document could not be resolved (it named no handle or
        source the workspace knows) counts for every document: it is not known which one it left unsearched (round 22 review #5)."""
        wanted = {str(d) for d in documents or ()}
        return [t for t in self.clock.reads
                if (state := self.read_state(t)) is not None and state.state == READ_FAILED
                and (not wanted or not (docs := self.clock.read_docs.get(t)) or wanted & docs)]

    def note_readings(self, conn: Connection, version_ids) -> None:
        """Pin the reading ids of versions the turn is reading, in the transaction that reads them."""
        todo = list({str(v) for v in version_ids if v is not None} - set(self.readings))
        if todo:
            for r in conn.execute(text("SELECT id, ingestion->>'reading_id' AS reading_id FROM document_versions"
                                       " WHERE id = ANY(:v)"), {"v": [UUID(v) for v in todo]}):
                self.readings[str(r.id)] = r.reading_id

    def once(self, source: Source, key: tuple) -> Source:
        """Mark a source whose text this turn already returned: it keeps its own id (citable, verified against
        the same text) but is sent to the model as a reference to the first one."""
        first = self.returned.get(key)
        if first is not None and first != source.sid:
            source.same_as = first
        else:
            self.returned[key] = source.sid
        return source

    def touch(self, document_id, title: str, level: str, partial: bool = False, opening: dict | None = None) -> None:
        a = self.activity.setdefault(str(document_id), {"title": title, "level": "located", "read": False,
                                                        "partial": False, "openings": [], "read_complete": False,
                                                        "read_partial": False})
        if LEVELS.index(level) > LEVELS.index(a["level"]):
            a["level"] = level
        a["read"] = a["read"] or level == "read"  # kept apart: a verified datum may come from a passage only
        a["partial"] = a["partial"] or partial
        if opening is not None:
            a["openings"].append(opening)

    def name_unread(self, files: list[dict]) -> None:
        """Keep the unread documents a lookup of the turn matched by title (the gap line of a component says so)."""
        for d in files:
            self.unread_named[str(d["document_id"])] = d | {"document_id": str(d["document_id"])}

    def unread_files(self, conn: Connection | None = None) -> list[dict]:
        """Every visible file that was not read, read once per turn (none outside a database: no context); a caller
        that holds a transaction passes it, so the first read does not open another."""
        if self._unread_files is None:
            if self.ctx is None:
                self._unread_files = []
            elif conn is not None:
                self._unread_files = unread_documents(conn)
            else:
                with tenant_tx(self.ctx) as tx:
                    self._unread_files = unread_documents(tx)
        return self._unread_files

    def _sid(self) -> str:
        return f"S{len(self.sources) + 1}"

    def add_source(self, **kw) -> Source:
        return self.adopt(Source(sid="", **kw))

    def measurement_of(self, row_id) -> Measurement | None:
        """The turn's measurement of a stored row, by the row's id (an index kept by ``add_measurement``; rebuilt when
        ``measurements`` was filled directly)."""
        if len(self._by_row) != len(self.measurements):
            self._by_row = {}
            for m in self.measurements.values():
                self._by_row.setdefault(m.id, m)
        return self._by_row.get(row_id)

    def mid_of(self, row_id) -> str | None:
        """The M# of a stored row's measurement, or None while the turn holds none."""
        known = self.measurement_of(row_id)
        return known.mid if known is not None else None

    def add_measurement(self, row, block_text: str | None = None, *, span=...) -> Measurement:
        """A stored measurement as the turn's M#; one found again keeps its id, so it is never counted twice. ``block_text``: the
        text of the block holding a text measurement, read by the listing, so its number's place is found in it (round 14 U3, KTD1);
        ``span``: the number's place when the caller computed it already (``measurement_span`` of the same row and text; None: none)."""
        known = self.measurement_of(row.id)
        if known is None:
            known = Measurement(f"M{len(self.measurements) + 1}", row.id, row.document_id, row.version_id, row.title,
                                row)
            known.facet = _measurement_facet_of(self, None, row)
            cx = self.contexts.get(str(row.version_id))
            if cx is not None and cx.multi:  # its appraisal context, from its block (KTD7)
                known.context = value_context(cx, cx.at_block(getattr(row, "block_index", None)))
            self.measurements[known.mid] = known
            self._by_row[row.id] = known
            self.anchors[known.mid] = anchors.measurement_stub(known)
        if known.mid not in self.spans:
            placed = measurement_span(row, block_text) if span is ... else span
            if placed is not None:
                self.spans[known.mid] = placed  # a table row that names its cell; a text row's, once its block is read
        return known

    def adopt(self, source: Source) -> Source:
        """Register a source the server read for its own checks, once it is cited (a new id)."""
        if source.version_id is not None and source.reading_id is None:
            source.reading_id = self.readings.get(str(source.version_id))
        source.sid = self._sid()
        self.sources[source.sid] = source
        stub = anchors.source_stub(source)
        if stub is not None:
            self.anchors[source.sid] = stub
        return source


# --- helpers ---------------------------------------------------------------------------------------------

def _location(section: str | None, kind: str, page_list, block_start, block_end, paragraphs, media) -> str:
    parts = []
    if page_list:
        parts.append(f"עמוד {page_list[0]}" if len(page_list) == 1 else f"עמודים {page_list[0]}–{page_list[-1]}")
    if section:
        parts.append(f"סעיף \"{section}\"")
    if kind in ("table", "table_row"):
        parts.append("טבלה" + (" (מתוך תמונה)" if media else ""))
    elif kind == "image":
        parts.append("תמונה")
    elif paragraphs:
        a, b = paragraphs
        if a and b:
            parts.append(f"פסקה {a}" if a == b else f"פסקאות {a}–{b}")
    return ", ".join(parts) or "המסמך"


def _block_info(conn: Connection, version_id: UUID, start: int | None, end: int | None, *, with_text: bool = False) -> tuple:
    """(the paragraph numbers the blocks ``start``..``end`` span, their picture media, the blocks themselves). ``with_text``: the
    blocks also carry ``block_index``, ``kind``, ``status`` and ``text`` (``_register_shown`` reads them from this one query, not
    its own); without it the third part is the blocks' bare ``paragraph_no`` and ``media``."""
    if start is None:
        return (None, None), None, []
    rows = conn.execute(text(
        "SELECT paragraph_no, media" + (", block_index, kind, status, text" if with_text else "")
        + " FROM document_blocks WHERE version_id = :v AND block_index BETWEEN :a AND :b"
        " ORDER BY block_index"), {"v": version_id, "a": start, "b": end if end is not None else start}).all()
    nums = [r.paragraph_no for r in rows if r.paragraph_no]
    media = next((r.media for r in rows if r.media), None)
    return ((min(nums), max(nums)) if nums else (None, None)), media, rows


@dataclass(frozen=True)
class ReadingGaps:
    """Where a version's reading has unread regions (R28): the pages with an unread region or whose reading failed,
    and, for a document without pages (DOCX), the sections of its unread regions (``unplaced``: one whose section is
    unknown too). ``partial``: the version is partly read anywhere (unread or uncertain), which bounds what a search
    of it can say is absent."""

    partial: bool
    pages: frozenset = frozenset()
    sections: frozenset = frozenset()
    unplaced: bool = False

    @classmethod
    def of(cls, ingestion: dict | None, failed_pages, partial: bool) -> ReadingGaps:
        pages, sections, unplaced = set(failed_pages or ()), set(), False
        for e in coverage_of(ingestion or {}):
            if not e.get("ok", True) and e.get("page") is not None:
                pages.add(e["page"])
            for r in e.get("regions") or []:
                if r.get("status") != reader.UNREAD:
                    continue
                if e.get("page") is not None:
                    pages.add(e["page"])
                elif r.get("section"):
                    sections.add(r["section"])
                else:
                    unplaced = True
        return cls(partial, frozenset(pages), frozenset(sections), unplaced)

    def cites(self, page_list, section: str | None) -> bool:
        """Whether a passage on ``page_list`` (or, without pages, in ``section``) is in or next to an unread region:
        only then is it marked as read in part. An unread region elsewhere in the document does not."""
        if page_list:
            return bool(self.pages.intersection(page_list))
        return self.unplaced or (section is not None and section in self.sections)


def _reading_gaps(conn: Connection, version_ids: list[UUID]) -> dict[UUID, ReadingGaps]:
    if not version_ids:
        return {}
    return {r.id: ReadingGaps.of(r.ingestion, r.failed, bool(r.partial)) for r in conn.execute(text(
        "SELECT v.id, v.ingestion, (COALESCE(v.pages_incomplete, 0) > 0 OR"
        " COALESCE((v.ingestion->>'partial')::boolean, false)) AS partial,"
        " ARRAY(SELECT p.page_no FROM pages p WHERE p.version_id = v.id AND NOT p.ok) AS failed"
        " FROM document_versions v WHERE v.id = ANY(:v)"), {"v": version_ids})}


def _visible_documents(conn: Connection, ids: list[str]) -> list[UUID]:
    out = []
    for raw in ids or []:
        try:
            out.append(UUID(str(raw)))
        except ValueError:
            raise ToolError(f"מזהה מסמך לא תקין: {raw}") from None
    if not out:
        return []
    found = {r.id for r in conn.execute(text(
        "SELECT id FROM documents WHERE id = ANY(:d) AND deleted_at IS NULL"), {"d": out})}
    missing = [str(d) for d in out if d not in found]
    if missing:
        raise ToolError("מסמכים לא נמצאו או שאין הרשאה אליהם: " + ", ".join(missing))
    return out


_TABLE_ROWS = re.compile(re.escape(TABLE_SIZE_PREFIX) + r"\s*(\d+)\s*שורות")


def table_row_count(text: str) -> int | None:
    """The row count a source's table size line states (``table_size_line``), or None."""
    m = _TABLE_ROWS.search(text or "")
    return int(m.group(1)) if m else None


def table_size_line(structure: dict | None) -> str:
    """"הטבלה: 9 שורות; ערכים לפי עמודה: ..." — so a single row is never read as the whole table."""
    st = structure or {}
    rows = [r.get("cells") or [] for r in st.get("rows") or []]
    if not rows:
        return ""
    headers = st.get("headers") or []
    counts = []
    for i, h in enumerate(headers):
        n = sum(1 for r in rows if i < len(r) and re.search(r"\d", r[i] or ""))
        if h and n:
            counts.append(f"{h} {n}")
    return f"{TABLE_SIZE_PREFIX} {len(rows)} שורות" + (f"; ערכים מספריים לפי עמודה: {', '.join(counts[:6])}" if counts else "")


def _table_sizes(conn: Connection, keys: set[tuple]) -> dict[tuple, str]:
    if not keys:
        return {}
    vids = list({k[0] for k in keys})
    out = {}
    for r in conn.execute(text("SELECT version_id, table_index, structure FROM extracted_tables"
                               " WHERE version_id = ANY(:v) AND table_index = ANY(:t)"),
                          {"v": vids, "t": list({k[1] for k in keys})}):
        if (r.version_id, r.table_index) in keys:
            out[(r.version_id, r.table_index)] = table_size_line(r.structure)
    return out


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n].rstrip() + " …[קוצר]"


def _with_size(size: str | None, body: str) -> str:
    return f"{size}\n{body}" if size else body


# the attributes of a source's tag that belong to its document and are the same on every hit of it (round 17 U6, KTD6 item 3)
DOCUMENT_ATTRS = ("document", "title", "subject", "link")  # ``date`` and ``source_kind`` stay on each hit: readers take them there


def _envelope(s: Source) -> list[tuple[str, str]]:
    """The attributes of a source's tag after its ``id`` and ``document_id``, in the order they are printed (each value
    already escaped for a prompt attribute)."""
    out: list[tuple[str, str]] = [(k, str(v)) for k, v in s.tags.items()]
    if s.context:  # the appraisal context it is in, in a file holding several (KTD7)
        out.append(("context", _attr(s.context)))
    elif s.subject:  # the identity a file of one subject unit states for itself
        out.append(("subject", _attr(s.subject)))
    if s.heading:  # the lot or section heading its chunk lacks (round 21 U8)
        out.append(("heading", _attr(s.heading)))
    if s.unit:  # its stable unit id (version, reading, number), beside the turn's handle in the tags (round 9 U9)
        out.append(("unit_id", _attr(s.unit["id"])))
    if s.date_line:  # the valuation date of the unit holding the passage, or that it is unknown (round 11 U8, KTD6)
        out.append(("date", _attr(s.date_line)))
    if s.link:  # why the server gathered this appraisal as the property's (round 12 U12b, R3)
        out.append(("link", _attr(s.link)))
    if s.status:  # a reading: how complete it is, the reading it is of, and how to read on
        out += [("status", s.status), ("version_id", str(s.version_id)), ("reading_id", _attr(s.reading_id))]
        if s.more:
            out.append(("more", str(s.more)))
    out.append(("title", _attr(s.title)))
    return out


def _render_source(s: Source, hoisted: frozenset | set = frozenset()) -> str:
    """The text a source is sent to the model as. ``hoisted``: the attributes of its document that are printed once in the
    ``<document>`` tag above it (``_render_sources``) and not repeated here. The ``document_id`` stays on every source, the
    key that readers of the output (the eval harness, the progress label) take a hit's document by. What the verifier checks
    a claim against is ``Source.text`` and does not depend on this envelope."""
    if s.is_listing:
        return f'<source id="{s.sid}" kind="listing" title="{_attr(s.title)}">\n{_txt(s.text)}\n</source>'
    if s.same_as:
        return (f'<source id="{s.sid}" same_as="{s.same_as}" document_id="{s.document_id}" title="{_attr(s.title)}"'
                f' location="{_attr(s.location)}"/> (אותו טקסט כמו {s.same_as}, שכבר הוחזר בתור הזה)')
    flag = " (המסמך נקרא חלקית: חלק מהתמונות לא נקראו)" if s.partial_document else ""
    parts = [(k, v) for k, v in _envelope(s) if k not in hoisted]
    title = next((f' title="{v}"' for k, v in parts if k == "title"), "")
    tags = "".join(f' {k}="{v}"' for k, v in parts if k != "title")
    # what kind of document it is, when that is not an appraisal, and how its pool labels it (round 10 U8)
    pool = "" if s.source_kind == source_kinds.APPRAISAL else f' source_kind="{s.source_kind}"'
    pool += f' label="{_attr(s.label)}"' if s.label else ""
    return (f'<source id="{s.sid}" document_id="{s.document_id}"{tags}{title}'
            f' location="{_attr(s.location)}" kind="{s.kind}"{pool}>{flag}\n{_txt(s.body if s.body is not None else s.text)}'
            "\n</source>")


def _render_sources(sources: list[Source]) -> list[str]:
    """The sources of one search, each as ``_render_source`` prints it, except that the hits of one document are printed
    together under one ``<document>`` tag that states what they share once — the document's handle and title, and the
    subject and link when they are the same on every hit (about 45% of the output of a search was this
    envelope, repeated per hit; round 17 U6, KTD6 item 3). Each hit keeps its own id, ``document_id``, location, kind and
    unit tags. A document with a single hit, a listing and a pointer to an earlier source are printed as before; the order is
    that of the first hit of each document, and a group's hits follow it in their own order."""
    by_document: dict = {}
    for s in sources:
        if not s.is_listing and not s.same_as and s.document_id is not None:
            by_document.setdefault(s.document_id, []).append(s)
    groups = {d: g for d, g in by_document.items() if len(g) > 1}
    out: list[str] = []
    done: set = set()
    for s in sources:
        group = groups.get(s.document_id) if not s.is_listing and not s.same_as else None
        if group is None:
            out.append(_render_source(s))
        elif s.document_id not in done:
            done.add(s.document_id)
            shared = {}
            envs = [dict(_envelope(x)) for x in group]
            for name in DOCUMENT_ATTRS:
                seen = [env.get(name) for env in envs]
                if seen[0] is not None and all(v == seen[0] for v in seen):
                    shared[name] = seen[0]
            head = "".join(f' {k}="{v}"' for k, v in shared.items())
            out.append(f"<document{head}>\n" + "\n\n".join(_render_source(x, frozenset(shared)) for x in group)
                       + "\n</document>")
    return out


def _attr(v: str | None) -> str:
    from app.providers.llm import prompt_attr

    return prompt_attr(v or "")


def _txt(v: str | None) -> str:
    from app.providers.llm import prompt_text

    return prompt_text(v or "")


# --- appraisal contexts inside one file (round 7 U6: KTD7; R20–R22) ------------------------------------------------
#
# A file holding several appraisals (or an appendix about a comparison property) is read as several contexts
# (``app.chat.contexts``): every source of it says the context it is in (``context="הקשר 2: ..."``), a read marks
# where a context starts inside it, the outline lists each context's sections apart, a table merged across two of them
# lists its rows by context, and a value records the context of its own block or row. What the request asks about
# (each component's subject, else the current question) is matched against the contexts, and the tools say when it
# names none of them or several, so the turn reads the right context or asks. A file with one context shows nothing.

MSG_CONTEXTS = "בקובץ הזה כמה שומות או נכסים, כל אחד בהקשר משלו — {which}. ערך שייך לנכס של ההקשר שבו הוא כתוב."
MSG_ASKED_ONE = "הנכס שבשאלה («{asked}») הוא של {which}."
MSG_ASKED_NONE = ("הנכס שבשאלה («{asked}») אינו מזוהה באף אחד מההקשרים בקובץ: קרא את ההקשר שעוסק בו, או שאל את "
                  "המשתמש לאיזה נכס הוא מתכוון.")
MSG_ASKED_MANY = ("הנכס שבשאלה («{asked}») מתאים לכמה הקשרים ({which}): קרא את ההקשר הנכון, או שאל את המשתמש לאיזה "
                  "נכס הוא מתכוון.")
MSG_ASKED_UNNAMED = ("השאלה אינה נוקבת באחד מהנכסים האלה: ענה לכל הקשר בנפרד וציין את הנכס שלו, או שאל את המשתמש על "
                     "איזה נכס מדובר.")
MSG_CONTEXT_STARTS = "[מכאן {which}]"
MSG_TABLE_ROWS = "[שורות {first}–{last} — {which}]"
MSG_FOREIGN_ROWS = ("שורות {first}–{last}{pages} של {handle} שייכות להקשר הזה (הטבלה נשמרה כאחת עם טבלה של הקשר אחר): "
                    "read(table={handle})")


def contexts_of(ws: Workspace, conn: Connection, version_id, reading_id: str | None = None):
    """A version's appraisal contexts, derived once per reading (``contexts.of_version``) and kept for the turn."""
    key = str(version_id)
    cx = ws.contexts.get(key)
    if cx is None:
        cx = ws.contexts[key] = contexts.of_version(conn, version_id, reading_id or ws.readings.get(key))
    return cx


def asked_subjects(ws: Workspace) -> tuple[list[str], bool]:
    """What the request asks about: each frozen component's subject (True), else the current question (False)."""
    items = ws.requirement_items
    subjects = list(dict.fromkeys(i["subject"] for i in items if i.get("subject")))
    if subjects:
        return subjects, True
    current = next((m["text"] for m in reversed(ws.user_messages) if m.get("current")), "")
    return ([current] if current else []), False


def located_contexts(ws: Workspace, cx) -> set[int]:
    """The context of ``cx`` that is the unit the turn located for the question (``locate_unit``), when it is in this
    file: the asked property of a question whose words name no unit's title (a file whose units state none)."""
    located = getattr(ws, "located_unit", None)  # a workspace without one (a bare fixture) located nothing
    return {n for n in cx.numbers if located and cx.is_subject(n) and cx.key(n) == located["key"]} if cx.multi else set()


# where a request's subject is two (or more) sides: "X ו-Y", "בין X לבין Y", "X מול Y". The Hebrew conjunction is a prefix, so it divides a
# subject only before the article, a hyphen, a digit, a Latin letter or a word that opens a property's name ("ושומת", "ונכס", "ובית");
# never before a name that merely carries a number, for a street that starts with the letter ("ויצמן 5") is one side; the explicit
# connectors divide a free text too (the question)
_SIDE_WORDS = "נכס|שומ|דוח|חוו|מתחם|פרויקט|בניין|בית|דירה|מגרש|חלק|גוש|רחוב|עמוד|עמ"
_SIDE_EXPLICIT = r"\s+(?:לבין|לעומת|מול|ובין)\s+|\s+&\s+"
_DASHES = "-־‐‑‒–—―"  # a hyphen, a maqaf, and the dashes a pasted or typed subject carries in its place (round 21 R3)
_SIDE_SPLIT = re.compile(rf"{_SIDE_EXPLICIT}|\s+ו(?=[{_DASHES}]|ה[א-ת]|\d|[A-Za-z]|(?:{_SIDE_WORDS}))")
_SIDE_SPLIT_EXPLICIT = re.compile(_SIDE_EXPLICIT)
# a conjunction before a word the subject already named ("X 12 וX 14-18"): the project word repeated opens the second side (round 21 R3)
_SIDE_REPEAT = re.compile(rf"\s+ו[{_DASHES}]?(?=([א-ת]{{3,}}))")


def _side_word(word: str) -> str:
    return word[1:] if len(word) > 3 and word[0] == "ה" else word


def _repeat_split(part: str) -> list[str]:
    """``part`` divided before a "ו" that opens a word standing earlier in it (round 21 R3). A street whose name starts with the
    conjunction letter ("ויצמן 5") is no such word: its rest stands nowhere before it."""
    cuts, seen = [], 0
    for m in _SIDE_REPEAT.finditer(part):
        earlier = {_side_word(w) for w in re.findall(r"[א-ת]{3,}", part[:m.start()])}
        if _side_word(m.group(1)) in earlier:
            cuts.append((seen, m.start()))
            seen = m.end()
    return [part[a:b] for a, b in cuts] + [part[seen:]] if cuts else [part]


def subject_sides(text: str | None, explicit: bool = False) -> list[str]:
    """The sides a subject is written as (round 19 U2, KTD2, R7): "appraisals X and Y" is two; a subject with no conjunction is one.
    ``explicit``: divide only where a connector says it ("בין X לבין Y"), as a free text (the question) needs."""
    split = _SIDE_SPLIT_EXPLICIT if explicit else _SIDE_SPLIT
    pieces = split.split(text or "")
    if not explicit:
        pieces = [y for x in pieces for y in _repeat_split(x)]
    parts = [side for x in pieces if x and (side := x.strip(f" {_DASHES},."))]
    return parts if len(parts) > 1 else ([text.strip()] if text and text.strip() else [])


def resolved_sides(cx, text: str | None, explicit: bool = False) -> tuple[set[int], bool]:
    """The subject contexts a subject text names, by each of its sides (``subject_sides``; a side by its name, identifier or page
    range), and whether the subject is resolved: a subject of one side always is (it names what it names, or nothing); a compound
    subject is only when every side names a context (round 19 U2, R7). A compound that resolves only in part is not."""
    if not text:
        return set(), True
    sides = subject_sides(text, explicit)
    found = [set(cx.resolve(x)) for x in sides]
    # a subject of one side that is the text itself is resolved once (``subject_sides`` strips a lone side, so a text with blanks around it
    # is resolved as written)
    whole = found[0] if len(sides) == 1 and sides[0] == text else set(cx.resolve(text))
    union = whole.union(*found)
    # a text that names a context as a whole while none of its pieces does (a name or a street that holds the conjunction letter) is one side
    return union, len(sides) <= 1 or all(found) or len(whole) >= len(sides) or (bool(whole) and not any(found))


def asked_sides(ws: Workspace, cx) -> tuple[set[int], bool]:
    """The contexts of a file the request names (its components' subjects, else its question), and whether every side of a
    compound subject resolved (``resolved_sides``); when no title or alias names one, the unit the turn located."""
    subjects, _ = asked_subjects(ws)
    found = [resolved_sides(cx, x) for x in subjects]
    named = set().union(*(n for n, _ in found)) if found else set()
    return (named, all(c for _, c in found)) if named else (located_contexts(ws, cx), True)


def asked_contexts(ws: Workspace, cx) -> set[int]:
    """The contexts of a file the request names (its components' subjects, else its question); when no title or alias
    names one, the unit the turn located."""
    return asked_sides(ws, cx)[0]


def _context_note(ws: Workspace, cx) -> str:
    """The file's contexts and which of them the request asks about — none, or several, said explicitly (R21)."""
    lines = [MSG_CONTEXTS.format(which="; ".join(cx.describe(n) for n in cx.numbers))]
    subjects, from_components = asked_subjects(ws)
    for subject in subjects:
        named = cx.named(subject)
        if not from_components:
            lines.append(MSG_ASKED_ONE.format(asked=_txt(_clip(" · ".join(cx.label(n) for n in named), 120)),
                                              which=", ".join(f"הקשר {n}" for n in named))
                         if len(named) == 1 else MSG_ASKED_UNNAMED if len(cx.subjects) > 1 else "")
        elif len(named) == 1:
            lines.append(MSG_ASKED_ONE.format(asked=_txt(_clip(subject, 120)), which=cx.describe(named[0])))
        elif not named:
            lines.append(MSG_ASKED_NONE.format(asked=_txt(_clip(subject, 120))))
        else:
            lines.append(MSG_ASKED_MANY.format(asked=_txt(_clip(subject, 120)),
                                               which=", ".join(f"הקשר {n}" for n in named)))
    return "\n".join(dict.fromkeys(x for x in lines if x))


def _note_once(ws: Workspace, document_id, cx) -> str | None:
    """The file's contexts note, the first time a read or a search of the turn returns a part of the file."""
    if not cx.multi or str(document_id) in ws.context_notes:
        return None
    ws.context_notes.add(str(document_id))
    return _context_note(ws, cx)


def unit_info(cx, number: int) -> dict:
    """A unit as a source, a value and the answer's payload carry it (round 9 U9, R10): its stable id, number, title,
    label, kind and pages."""
    seg = cx.segment(number)
    first, last = cx.pages(number)
    return {"id": cx.key(number), "no": number, "title": seg.title, "label": cx.label(number), "kind": seg.kind,
            "pages": [first, last] if first else []}


def unit_handle_of(ws: Workspace, document_id, version_id, reading_id: str | None, number: int,
                   units_tag: str | None = None) -> str:
    """The turn's handle (U#) for a unit: bound to the version and the reading it was issued for, and the same for
    the same unit whichever tool names it. ``units_tag`` is the tag of the derivation the caller read the unit from (a
    ``Contexts.tag``, an anchor's or a row's ``units_tag``; None: ``units-v4``): kept in the handle's data, not in its
    identity, so that the handle names the unit by the same stable id as ``Contexts.key`` (``contexts.anchor_key``,
    round 15 U4). A handle first minted without a tag takes the tag of a later caller that knows it."""
    h = ws.handle("U", (str(version_id), reading_id or None, number), document_id=str(document_id),
                  version_id=str(version_id), reading_id=reading_id or None, unit_no=number,
                  units_tag=units_tag or None)
    if units_tag and not ws.handles[h].get("units_tag"):
        ws.handles[h]["units_tag"] = units_tag
    return h


def read_unit_handles(ws: Workspace) -> set[str]:
    """The handles (U#) of the units the turn read: the unit of any passage its tools returned, and any unit a tool worked
    inside through a handle (``Workspace.unit_scopes``). A unit only listed (an outline, a find) is not read."""
    out = {h for s in ws.sources.values() if not s.is_listing and (h := s.tags.get("unit"))}
    for scope in ws.unit_scopes.values():
        h = ws.handle_ids.get(("U", (scope["version_id"], scope["reading_id"] or None, scope["unit_no"])))
        if h:
            out.add(h)
    return out


def unit_handle(ws: Workspace, cx, number: int, document_id) -> str:
    """The handle (U#) of a unit of a version's contexts."""
    return unit_handle_of(ws, document_id, cx.version_id, cx.reading_id, number, cx.tag)


def _mark(s: Source, cx, numbers, ws: Workspace | None = None) -> Source:
    """A source of a file holding several contexts says the ones it is in, and the unit it begins in (the stable id,
    and — with ``ws`` — the handle ``search``, ``read`` and ``outline`` take); one of a file of a single subject unit
    carries that unit's label as its ``subject``."""
    if cx is not None and cx.multi and numbers:
        s.contexts = list(dict.fromkeys(numbers))
        s.context = "; ".join(cx.describe(n) for n in s.contexts)
        s.unit = unit_info(cx, s.contexts[0])
        if ws is not None and s.document_id is not None:
            s.tags["unit"] = unit_handle(ws, cx, s.contexts[0], s.document_id)
            if s.sid in ws.anchors:  # the citation's anchor says the unit too (``anchors.snapshot``)
                ws.anchors[s.sid]["unit"] = s.unit
    elif cx is not None and not cx.multi and len(cx.subjects) == 1:
        # a file of one subject unit: its label is the identity the judge is shown (no context, unit, tag or anchor)
        s.subject = cx.label(cx.subjects[0]).strip()
    return s


def _table_at(ws: Workspace, conn: Connection, version_id, table_index: int) -> tuple[dict | None, Any]:
    """A table's stored structure (None: not stored) and the block it is read at (None: none), read once per turn."""
    key = (str(version_id), table_index)
    found = ws.tables.get(key)
    if found is None:
        found = ws.tables[key] = (reader.table_structure(conn, version_id, table_index),
                                  reader.table_block(conn, version_id, table_index))
        if found[0] is not None:  # round 22 U0: the multi-page stored tables the turn served (measurement only)
            ws.clock.note_table(key, stored_tables.spans_pages(found[0]))
    return found


def _row_line(r: dict) -> str:
    """The cells of a stored row on one line, joined by « | »."""
    return " | ".join(str(c) for c in r.get("cells") or [])


def _notes_of_rows(st: dict | None, rows: Iterable[int] | None) -> list[str]:
    """The notes of a stored table (``structure.notes``) that govern the rows ``rows`` (indexes into ``structure.rows``; None or none: the rows are
    not known), by their scope (round 22 U5, KTD5, R8): a note that opens with a mark («*», «(1)», «(***)») belongs to the rows that carry the mark,
    a mark in a header cell to every row (``scopes.note_rows``, the one reader ``items._note_of_row`` uses); a note without a mark, and a mark no cell
    of the table carries, to its own table. With the rows unknown, a marked note applies only when the mark governs every row. The table's OWN notes
    alone: never those of another table, and never across a candidate link (``candidate_of``) to the other part."""
    notes = [str(n) for n in (st or {}).get("notes") or [] if n]
    if not notes:
        return []
    body = [_row_line(r) for r in st.get("rows") or []]
    header = [str(h) for h in st.get("headers") or []]
    held = set(rows) if rows is not None else set()
    out: list[str] = []
    for note in notes:
        mark = scopes.note_mark(note)
        bound = scopes.note_rows(mark, header, body) if mark else None
        if bound is not None and not (held & bound if held else len(bound) >= len(body)):
            continue
        out.append(note)
    return out


def _mark_label(mark: str) -> str:
    """A note mark as ``scopes.note_mark`` keys it, written the way notes are: «*» -> «(*)», «#1» -> «(1)»."""
    return f"({mark.removeprefix('#')})"


def _hit_notes(ws: Workspace, conn: Connection | None, h: dict, passage: str, handle: str = "") -> tuple[list[str], str]:
    """The notes of the table of a search hit on a stored table (a table chunk or a row chunk) that apply to the rows the hit holds and that the hit's own
    text lacks, and the flag that tells the agent they exist (round 22 U5, KTD5, R8; finding g): the chunker writes a table's notes only into its last table
    chunk, so every other chunk reaches the agent and the verifier without them. ``passage`` is the hit's stored text (its rows are found in the stored
    structure by their lines). The flag names the marks only («[לטבלה יש הערות: (*) (1); לקריאתן: read(table=T3)]»), never the text of a note: the agent reads
    the table for it. The structure is the per-turn cache (``_table_at``), read by ``_served_hit_text`` for the same hit already. ([], "") for any other hit."""
    if not _is_table_hit(h):
        return [], ""
    st = _table_at(ws, conn, h["version_id"], h["table_index"])[0]
    if not st or not st.get("notes"):
        return [], ""
    if h["kind"] == "table_row":
        rows = [int(h["row_index"])] if h.get("row_index") is not None else None
    else:
        lines = {ln.strip() for ln in passage.split("\n")}
        rows = [i for i, r in enumerate(st.get("rows") or []) if (line := _row_line(r).strip()) and line in lines]
    shown = meaning._flat(passage)
    held = [n for n in _notes_of_rows(st, rows) if meaning._flat(n) not in shown]
    if not held:
        return [], ""
    marks = list(dict.fromkeys(_mark_label(m) for n in held if (m := scopes.note_mark(n))))
    if any(not scopes.note_mark(n) for n in held):
        marks.append("ללא סימן")
    return held, f"[לטבלה יש הערות: {' '.join(marks)}" + (f"; לקריאתן: read(table={handle})" if handle else "") + "]"


def _quote_table_notes(ws: Workspace, conn: Connection | None, src: Source, loc: dict) -> list[str]:
    """The notes of the table of ``src`` that govern the quote ``loc['quote']`` of it, read from the stored structure (round 22 U5, KTD5, R8): the row of a
    row chunk, else the rows whose line holds the quote, else (the quote is in no unique row) the notes whose mark the quote carries itself and those
    that govern every row. [] for a source that is no table (a paragraph quote gets no table notes)."""
    if src.table_index is None or src.kind not in ("table", "table_row") or src.version_id is None:
        return []
    st = _table_at(ws, conn, src.version_id, src.table_index)[0]
    if not st or not st.get("notes"):
        return []
    quote = loc.get("quote") or ""
    if src.row_index is not None:
        return _notes_of_rows(st, [src.row_index])
    shaped = _loose(quote)
    rows = [i for i, r in enumerate(st.get("rows") or []) if shaped and shaped in _loose(_row_line(r))]
    if len(rows) == 1:
        return _notes_of_rows(st, rows)
    whole, carried = _notes_of_rows(st, None), scopes.marks_of(quote)
    return [n for n in map(str, st["notes"]) if n and (n in whole or scopes.note_mark(n) in carried)]


def _note_above(full: str, start: int) -> str:
    """The scale note that governs a number at ``start`` of a source's text from a line above it
    (``calc.governing_note`` over the text's lines, nearest first, all in the source's own section)."""
    above = full[:start].split("\n")[:-1]
    return calc.governing_note([("paragraph", ln, True) for ln in reversed(above) if ln.strip()])


@dataclass(frozen=True)
class _ScaleNote:
    text: str
    block_index: int
    status: str


def _note_units(governing: str | _ScaleNote) -> set[str]:
    """A clearly read scale heading also attests its unit; callers prefer a number's nearer unit."""
    note = governing.text if isinstance(governing, _ScaleNote) else governing
    units = meaning.units_attested(note)
    if units and isinstance(governing, _ScaleNote) and governing.status in (reader.UNREAD, reader.UNCERTAIN):
        raise ToolError(f"היחידה בבלוק {governing.block_index} לא ודאית: קרא ואמת את ההערה לפני לקיחת הערך")
    return units


def _value_scale(own: str, start: int, end: int, *local: str, governing: str | _ScaleNote = "") -> int:
    """An inherited scale must be read clearly; nearer, explicit scales need no inherited evidence."""
    note = governing.text if isinstance(governing, _ScaleNote) else governing
    scale = calc.stated_scale(own, start, end, *local, meaning._norm(note))
    if isinstance(governing, _ScaleNote) and governing.status in (reader.UNREAD, reader.UNCERTAIN) \
            and scale != calc.stated_scale(own, start, end, *local):
        raise ToolError(f"קנה המידה בבלוק {governing.block_index} לא ודאי: קרא ואמת את ההערה לפני לקיחת הערך")
    return scale


def _governing_note(ws: Workspace, conn: Connection, version_id, block_index: int | None,
                    candidate_of: int | None = None) -> str | _ScaleNote:
    """The scale note that governs a figure in block ``block_index`` from above (``calc.governing_note``): the
    blocks before it, nearest first, each with whether it is in the figure's section; a table's with its caption,
    title and notes. "" when there is none (or the block is not known). ``candidate_of``: the table this one may continue (round 22
    U3, KTD3): the search stops at it, so nothing above the other part governs this one."""
    if block_index is None or block_index <= 0:
        return ""
    rows = reader.blocks_between(conn, version_id, max(0, block_index - calc.NOTE_WINDOW), block_index)
    held = next((r for r in rows if r.block_index == block_index), None)
    cx = contexts_of(ws, conn, version_id)
    segment = cx.segment_at(block_index) if cx.multi else None
    own = reader.effective_path(cx, block_index, getattr(held, "section_path", None))
    preceding = []
    candidates = []
    for r in reversed([r for r in rows if r.block_index < block_index]):
        if segment is not None and r.block_index < segment.first:
            break
        text_, kind = r.text or "", getattr(r, "kind", None) or "paragraph"
        if candidate_of is not None and kind == "table" and getattr(r, "table_index", None) == candidate_of:
            break
        if kind == "table" and getattr(r, "table_index", None) is not None:
            st = _table_at(ws, conn, version_id, r.table_index)[0] or {}
            text_ = "\n".join(x for x in [text_, st.get("caption") or "", *(st.get("title") or []),
                                          *(st.get("notes") or [])] if x)
        preceding.append((kind, text_, reader.effective_path(cx, r.block_index, getattr(r, "section_path", None)) == own))
        candidates.append((r, text_))
    note = calc.governing_note(preceding)
    if not note:
        return ""
    row = next(r for r, text_ in candidates if text_ == note)
    return _ScaleNote(note, row.block_index, row.status)


def _row_contexts(ws: Workspace, conn: Connection, cx, version_id, table_index: int) -> tuple[list[dict], dict]:
    st, block = _table_at(ws, conn, version_id, table_index)
    st = st or {}
    return reader.table_contexts(cx, st, block.block_index if block else st.get("block_index"),
                                 block.page if block else st.get("page")), st


def _is_table_hit(h: dict) -> bool:
    """Whether a search hit is a stored table or a row of one (it has a table whose rows are placed by context)."""
    return h.get("table_index") is not None and h["kind"] in ("table", "table_row")


def _hit_contexts(cx, h: dict, bs, be, table: tuple[list[dict], dict] | None) -> list[int]:
    """The contexts of a search hit: of its rows for a table or a table row (``table``: ``_row_contexts`` of its table),
    else of its blocks."""
    if table is not None:
        groups = table[0]
        if h["kind"] == "table_row" and h.get("row_index") is not None:
            n = int(h["row_index"]) + 1
            return [g["context"] for g in groups if g["first"] <= n <= g["last"]]
        return list(dict.fromkeys(g["context"] for g in groups))
    return cx.spanned(bs, be)


def _context_number(ws: Workspace, conn: Connection, cx, version_id, anchor: dict,
                    holder: int | None) -> int | None:
    """The context a value is in, from where it was read (KTD7): a table cell's row (its page and top), else the
    block holding its number or quote; None in a file with one context."""
    if cx is None or not cx.multi:
        return None
    if anchor.get("table_index") is not None and isinstance(anchor.get("row"), int):
        st, block = _table_at(ws, conn, version_id, anchor["table_index"])
        rows = (st or {}).get("rows") or []
        if 0 <= anchor["row"] < len(rows) and rows[anchor["row"]].get("page"):
            row = rows[anchor["row"]]
            return cx.at_position(row["page"], reader.row_top(row))
        return cx.at_block(block.block_index) if block is not None else None
    blocks = _value_blocks(anchor) or ([holder] if holder is not None else [])
    found = {cx.at_block(b) for b in blocks} - {None}
    return next(iter(found)) if len(found) == 1 else None


MSG_SUBJECT_CONTRADICTED = ("הנושא שנתת («{subject}») אינו הנכס של ההקשר שבו הערך כתוב: הערך כתוב ב{where}, והנושא "
                            "שנתת הוא של {other}. ערך של נכס אחר אינו ערך של הנכס הזה — קח את הערך מההקשר של הנכס "
                            "הנכון.")
MSG_SUBJECT_ASSERTED = "הנושא שנתת («{subject}») אינו מזוהה מתוך ההקשר שבו הערך כתוב ({where}): זו קביעה שלך."
MSG_VALUE_NOT_ASKED = ("שים לב: הערך כתוב ב{where}, והשאלה עוסקת ב{asked}: הוא אינו ערך של הנכס שנשאל עליו.")


def _context_lines(ws: Workspace, cx, value: calc.Value) -> list[str]:
    """What ``take_value`` says about a value's context: where it is, and its subject or the request against it."""
    if not value.context:
        return []
    where = value.context["described"]
    lines = [f"הקשר: {where}"]
    if value.subject_from == "contradicted":
        other = ", ".join(cx.describe(n) for n in cx.named(value.subject))
        lines.append(MSG_SUBJECT_CONTRADICTED.format(subject=_txt(value.subject), where=where, other=other))
    elif value.subject_from == "asserted":
        lines.append(MSG_SUBJECT_ASSERTED.format(subject=_txt(value.subject), where=where))
    asked, complete = asked_sides(ws, cx)
    # the hint says the value is another property's only when the contexts' identities say it (``Contexts.compare``): a value whose
    # context states no identity of its own is not told to be another property's (round 19 U2, R6)
    if asked and value.context["number"] not in asked and cx.compare(value.context["number"], asked, complete=complete) == contexts.DIFFERENT:
        lines.append(MSG_VALUE_NOT_ASKED.format(where=where, asked=", ".join(cx.describe(n) for n in sorted(asked))))
    return lines


def value_context(cx, number: int | None) -> dict | None:
    """A value's context record (``calc.Value.context``), None outside a file holding several contexts."""
    if cx is None or not cx.multi or number is None:
        return None
    first, last = cx.pages(number)
    return {"key": cx.key(number), "unit_id": cx.key(number), "number": number, "label": cx.label(number),
            "described": cx.describe(number), "pages": [first, last] if first else [],
            "subject": cx.is_subject(number), "title": cx.segment(number).title}


def subject_from(cx, number: int | None, subject: str) -> str:
    """Where a value's subject stands against its context (R21; round 19 U2): it names the context's identifiers (``context``),
    another context's only AND the contexts' identities say so (``contradicted``), or neither (``asserted``: it names none of them,
    or the context states no identity that settles it); "" without a subject or contexts."""
    if cx is None or not cx.multi or number is None or not (subject or "").strip():
        return ""
    named, complete = resolved_sides(cx, subject)
    # the same comparison as the verification's (round 19 U2, KTD2, R6): the subject is contradicted only when the contexts' identities
    # say the value is another property's; a context that states none (a noise heading, calculation pages) leaves it asserted
    return {contexts.SAME: "context", contexts.DIFFERENT: "contradicted"}.get(
        cx.compare(number, named, complete=complete), "asserted")


# --- pools and professional sources (round 10 U8: KTD11-KTD13, KTD15; R22-R32) -------------------------------------------
#
# What a search may read is decided by the server from the turn's settled intent (``Workspace.pools``), never by the model:
# the ``pool`` argument of ``search`` only chooses among the pools the intent allows, and a pool it may not read is clipped
# with a note. A hit of the professional pool carries its section, the limits that bind its provision and the call that
# reads the whole section; an appraisal hit under a question that is not about a property is labelled for what it is.

PROFESSIONAL_POOL, APPRAISAL_POOL = "professional", "appraisal"
POOL_KINDS = {PROFESSIONAL_POOL: (source_kinds.STANDARD, source_kinds.PROFESSIONAL),
              APPRAISAL_POOL: (source_kinds.APPRAISAL,)}
PRIMARY, EXAMPLE, METHOD = "primary", "example", "method"
# the role of each pool the intent allows (``Workspace.pools``); `document` reads the named documents of either pool
INTENT_POOLS = {
    PROFESSIONAL: {PROFESSIONAL_POOL: PRIMARY, APPRAISAL_POOL: EXAMPLE},
    "property": {APPRAISAL_POOL: PRIMARY, PROFESSIONAL_POOL: METHOD},
    "document": {APPRAISAL_POOL: PRIMARY, PROFESSIONAL_POOL: PRIMARY},
    MIXED: {APPRAISAL_POOL: PRIMARY, PROFESSIONAL_POOL: PRIMARY},
    "unknown": {PROFESSIONAL_POOL: PRIMARY, APPRAISAL_POOL: EXAMPLE},
    "set": {APPRAISAL_POOL: PRIMARY},
}
# the pools a search reads when the call names none, in the order its hits come
DEFAULT_POOLS = {
    PROFESSIONAL: (PROFESSIONAL_POOL, APPRAISAL_POOL),
    "property": (APPRAISAL_POOL,),
    "document": (APPRAISAL_POOL, PROFESSIONAL_POOL),
    MIXED: (APPRAISAL_POOL, PROFESSIONAL_POOL),
    "unknown": (PROFESSIONAL_POOL, APPRAISAL_POOL),
    "set": (APPRAISAL_POOL,),
}
EXAMPLE_HITS = 3  # appraisal hits (or a method's) beside the pool that answers
LIMIT_SHOWN = 6  # limits shown with one hit

LABEL_EXAMPLE = "דוגמה לבחירה של נכס מסוים — אינה כלל מקצועי"
LABEL_APPRAISAL_KIND = "מסמך מסוג שומה"
LABEL_APPRAISAL_DATA = "נתוני נכס מתוך שומה"
LABEL_METHOD = "שיטה — לא נתון של נכס"
LABEL_PROFESSIONAL = "מקור מקצועי (תקן או מסמך מקצועי)"
# search-only mode's two groups of results (round 11 U3, KTD11): the headings, and what the second one is not
LABEL_GROUP_APPRAISALS = "שומות"
LABEL_GROUP_PROFESSIONAL = "תקנים ומסמכים מקצועיים"
LABEL_NOT_PROPERTY_DATA = "אינם מקור לנתונים של נכס: להסבר מקצועי בלבד"
MSG_POOL_CLIPPED = "המאגר שביקשת אינו מותר לשאלה מהסוג הזה: החיפוש הוגבל למאגרים שהשרת קבע לה."
MSG_POOL_EXAMPLES = ("שאלה מקצועית: שומות מוצגות כדוגמה לבחירה של נכס מסוים בלבד, ולא ככלל מקצועי; כלל מקצועי נלקח "
                     "ממקור מקצועי.")
MSG_NO_STANDARD_POOL = ("במאגר שהמשתמש מורשה לראות אין תקן או מסמך מקצועי מסווג, ולכן נוסח התקן אינו במאגר: אל "
                        "תשלים מהזיכרון מה שתקן קובע. ענה מהמקורות האחרים שהוחזרו כשהם מסבירים את המושג או השיטה "
                        "(למשל מילון מונחים או הסבר כללי), ציין שהם מסמכים מסוג שומה ושנוסח התקן אינו במאגר. ערך, "
                        "מקדם או הנחה שנבחרו לנכס מסוים (מסומנים כדוגמה) אינם כלל מקצועי.")
MSG_NO_STANDARDS = "אין במאגר תקן או מסמך מקצועי שהמשתמש מורשה לראות."
MSG_STANDARDS_UNUSED = "שאלה על קבוצת מסמכים משתמשת בשומות בלבד: רשימת תקנים אינה חלק ממנה."
MSG_LIMITS = "תנאים וחריגים בסעיף (הכלל נקרא יחד איתם, לא בלעדיהם):"
MSG_ORDER_UNKNOWN = ("סדר המהדורות אינו ידוע: התאריכים שנכתבו במסמכים אינם מסדרים אותן, ותאריך ההעלאה אינו קובע איזו "
                     "מהן בתוקף. אין לקבוע איזו מהן עדכנית.")
UNKNOWN = "לא ידוע"
RELATIONS = {"clarifies": "מבהיר", "amends": "מתקן", "replaces": "מחליף", "appendix_of": "נספח של"}


def has_professional(ws: Workspace, conn: Connection) -> bool:
    """Whether the user may see a ready professional document at all, read once per data version."""
    version = current_data_version(conn)
    if ws.professional_memo is None or ws.professional_memo[0] != version:
        ws.professional_memo = (version, bool(conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
            f" AND v.status IN ('ready', 'needs_review') WHERE d.deleted_at IS NULL AND {PROFESSIONALS_SQL})"
        )).scalar_one()))
    return ws.professional_memo[1]


def profiles(ws: Workspace, conn: Connection) -> tuple[list, int]:
    """The visible professional documents' editions (``profile.visible_editions``), after the profiles that were not
    derived yet are — once per turn, for documents ingested before profiles existed (``ingest`` derives them). Read
    once per data version."""
    version = current_data_version(conn)
    if ws.profile_memo is None or ws.profile_memo[0] != version:
        loaded = profile.visible_editions(conn)
        if loaded[1] and not ws.profiled:
            ws.profiled = True
            profile.ensure_all(ws.ctx)
            loaded = profile.visible_editions(conn)
        ws.profile_memo = (version, loaded)
    return ws.profile_memo[1]


def sections_in(ws: Workspace, conn: Connection, version_id) -> tuple[sections.Section, ...]:
    """The sections of a version (``sections.sections_of``: derived on first use), loaded once per version and data
    version in the turn."""
    key = (str(version_id), current_data_version(conn))
    found = ws.section_cache.get(key)
    if found is None:
        found = ws.section_cache[key] = sections.sections_of(conn, version_id)
    return found


def copies_left_out(ws: Workspace, conn: Connection) -> tuple:
    """The versions of professional text the pool leaves out because the same text, with the same stated edition, is
    stored earlier (KTD11), read once per data version."""
    version = current_data_version(conn)
    if ws.copies is None or ws.copies[0] != version:
        profiles(ws, conn)  # the stated editions that tell copies from editions
        ws.copies = (version, catalog.excluded(catalog.entries(conn)))
    return ws.copies[1]


def _base_identity(ws: Workspace, conn: Connection, version_id) -> tuple[str, dict] | None:
    """(source kind, what the document's profile states) of a professional version, None for an appraisal."""
    key = str(version_id)
    found = ws.standards.get(key)
    if found is None:
        kind = conn.execute(text("SELECT d.source_kind FROM documents d JOIN document_versions v ON v.document_id = d.id"
                                 " WHERE v.id = :v"), {"v": UUID(key)}).scalar_one_or_none()
        if kind is None or kind == source_kinds.APPRAISAL:
            found = False
        else:
            stored = profile.profile_of(conn, UUID(key)) or profile.ensure_profile(ws.ctx, UUID(key))
            found = (kind, {"number": stored.stated("number") if stored else None,
                            "title": stored.stated("title") if stored else None,
                            "edition": stored.stated("edition") if stored else None,
                            "effective": stored.stated("effective") if stored else None,
                            "section": None, "section_title": None})
        ws.standards[key] = found
    return found or None


def standard_for(ws: Workspace, conn: Connection, version_id, block_index: int | None,
                 secs: tuple[sections.Section, ...] | None = None) -> tuple[str, dict] | None:
    """The identity of a passage of a professional version at ``block_index``: its source kind and ``{number, title,
    edition, effective, section, section_title}`` — what the document states about itself and the section the block is
    in. None for an appraisal. ``secs``: the version's sections when the caller holds them (``sections_in``)."""
    base = _base_identity(ws, conn, version_id)
    if base is None:
        return None
    kind, std = base
    sec = None
    if block_index is not None:
        sec = sections.innermost(sections_in(ws, conn, version_id) if secs is None else secs, block_index)
    return kind, std | ({"section": sec.label, "section_title": sec.title} if sec is not None else {})


def standard_title(std: dict, document_title: str) -> str:
    """"תקן 98.1 (גרסה 2.0, בתוקף מ-05.10.2025)": the number, else the stated name, else the document's own title, with
    the edition and the effective date when it states them."""
    detail = ", ".join(x for x in (std["edition"], f"בתוקף מ-{std['effective']}" if std["effective"] else None) if x)
    head = f"תקן {std['number']}" if std["number"] else (std["title"] or document_title)
    return head + (f" ({detail})" if detail else "")


def section_phrase(std: dict) -> str | None:
    """"סעיף 2.1", "נספח א'" or "סעיף «הגדרות»": where in the document, in the words a reader cites it."""
    label, title = std.get("section"), std.get("section_title")
    if label and label.startswith("נספח"):
        return label
    if label and re.fullmatch(r"\d+(?:\.\d+)*", label):
        return f"סעיף {label}"
    return f"סעיף «{title or label}»" if title or label else None


def standard_location(std: dict, page_list, kind: str = "text") -> str:
    parts = [section_phrase(std)]
    if kind in ("table", "table_row"):
        parts.append("טבלה")
    if page_list:
        parts.append(f"עמוד {page_list[0]}" if len(page_list) == 1 else f"עמודים {page_list[0]}–{page_list[-1]}")
    return ", ".join(p for p in parts if p) or "המסמך"


def section_string(std: dict) -> str | None:
    """The section as a source names it: "2.1 גישת ההשוואה" (its label and its title, whichever the document states)."""
    label, title = std.get("section"), std.get("section_title")
    return f"{label} {title}" if label and title and title != label else label or title


def _psection_handle(ws: Workspace, document_id, version_id, reading_id, section_no: int) -> str:
    """The handle (§#) of a section of a professional document, read whole by ``read(section=§#)``."""
    return ws.handle("§", (str(version_id), "psection", section_no), document_id=str(document_id),
                     version_id=str(version_id), reading_id=reading_id, psection=section_no)


def _limits_of(secs: tuple, sec: sections.Section) -> list[tuple[dict, sections.Section]]:
    """The limits that bind a provision of ``sec``: its own, then those of the sections around it (a chapter's
    exception reaches the sections it holds), each with the section it was written in."""
    by_no = {x.section_no: x for x in secs}
    out, at = [], sec
    while at is not None:
        out += [(lim, at) for lim in at.limits]
        at = by_no.get(at.parent_no) if at.parent_no is not None else None
    return out


def limit_lines(secs: tuple, sec: sections.Section) -> list[str]:
    limits = _limits_of(secs, sec)
    if not limits:
        return []
    lines = [MSG_LIMITS]
    for lim, at in limits[:LIMIT_SHOWN]:
        where = "" if at.section_no == sec.section_no else f" (בסעיף {at.label or at.title})"
        lines.append(f"- «{_txt(lim['sentence'])}»{where}")
    if len(limits) > LIMIT_SHOWN:
        lines.append(f"- ועוד {len(limits) - LIMIT_SHOWN} תנאים וחריגים: read עם הסעיף כולו")
    return lines


RULES_LIMITS = 20  # limits kept on a source for the verifier (it needs every one a provision has; a few sections have more)


def section_rules(secs: tuple, first: int | None, last: int | None) -> dict | None:
    """What the sections of the blocks ``first``..``last`` say about how binding they are, for the verifier (round 10 U9,
    KTD14, R33): ``limits`` — the condition and exception sentences that bind a provision there (its section's and those
    of the sections around it, ``_limits_of``), ``modality`` — the counts of requirement, recommendation, example,
    alternative and exception wording in those sections, ``more`` — limits beyond ``RULES_LIMITS``. The innermost section
    of a block changes only where a section starts or ends, so only those blocks are looked at. None for a passage in no
    section."""
    if first is None:
        return None
    last = first if last is None else max(first, last)
    marks = {first, last} | {n for s in secs for n in (s.block_first, s.block_last + 1) if first <= n <= last}
    held: dict[int, sections.Section] = {}
    for block in marks:
        holding = [s for s in secs if s.block_first <= block <= s.block_last]
        if holding:
            inner = max(holding, key=lambda s: (s.depth, s.section_no))
            held[inner.section_no] = inner
    if not held:
        return None
    limits: dict[tuple, str] = {}
    modality = dict.fromkeys(sections.MODALITY_CLASSES, 0)
    for sec in sorted(held.values(), key=lambda s: s.section_no):
        for lim, _ in _limits_of(secs, sec):
            limits.setdefault((lim["block"], lim["sentence"]), lim["sentence"])
        for cls, n in sec.modality.items():
            modality[cls] = modality.get(cls, 0) + n
    ordered = [sentence for _, sentence in sorted(limits.items())]
    return {"limits": ordered[:RULES_LIMITS], "modality": modality, "more": max(0, len(ordered) - RULES_LIMITS)}


def _rules_of_part(ws: Workspace, conn: Connection, version_id, part: reader.Part,
                   secs: tuple[sections.Section, ...] | None = None) -> dict | None:
    """``section_rules`` of the blocks of a part read from a professional version."""
    if not part.items:
        return None
    indexes = [r.block_index for r, _, _ in part.items]
    return section_rules(sections_in(ws, conn, version_id) if secs is None else secs, min(indexes), max(indexes))


def _identified(s: Source, cx) -> str | None:
    """The property an appraisal passage belongs to, or None. A passage in a file of several units belongs to the unit it
    begins in when that is an appraisal (not an appendix or a comparables part) with a title or identifiers; a passage of
    a file of one subject unit belongs to the property whose address or block and parcel the document states. A document
    that names no property (a glossary stored as an appraisal, a file with a title and no address) names none."""
    if s.unit:
        label = s.unit.get("label") or ""
        subject = s.unit.get("kind") not in contexts.NON_SUBJECT_KINDS
        return label if subject and (s.unit.get("title") or label) and label != contexts.MSG_UNNAMED else None
    if cx is not None and not cx.multi and len(cx.subjects) == 1:
        ident = cx.identity(cx.subjects[0])
        return " · ".join(ident.shown) if not ident.empty else None
    return None


def _label_of(s: Source, cx, pool: str, role: str, intent: str) -> str | None:
    """How a hit of a pool is labelled for the model (None: not at all). An appraisal under a question that is not about
    a property is an example of a property's choice only when the passage belongs to an identified property; otherwise it
    is named by its stored kind and nothing more."""
    if pool == PROFESSIONAL_POOL:
        return LABEL_METHOD if role == METHOD else LABEL_PROFESSIONAL if intent in (MIXED, "unknown") else None
    if role == EXAMPLE:
        who = _identified(s, cx)
        return f"{LABEL_EXAMPLE}: {who}" if who else LABEL_APPRAISAL_KIND
    return LABEL_APPRAISAL_DATA if intent == MIXED else None


@dataclass
class _Pass:
    pool: str
    role: str
    limit: int


def _plan(ws: Workspace, conn: Connection, pool: str | None, limit: int) -> tuple[list[_Pass], list[str], bool]:
    """The passes of a search — which pools, in what role, with how many hits each — from the intent and the call's
    ``pool`` (clipped to the allowed ones, with a note), and whether the call names the pool it reads. An unknown intent in
    an office that holds no standard is the appraisal search it always was: appraisals alone, in no role."""
    allowed, notes = ws.pools, []
    default = DEFAULT_POOLS.get(ws.intent, DEFAULT_POOLS["unknown"])
    if ws.intent == "unknown" and not has_professional(ws, conn):
        allowed, default = {APPRAISAL_POOL: PRIMARY, PROFESSIONAL_POOL: allowed[PROFESSIONAL_POOL]}, (APPRAISAL_POOL,)
    named = pool not in (None, "") and pool in allowed
    if pool not in (None, "") and not named:
        notes.append(MSG_POOL_CLIPPED)
    chosen = [pool] if named else [p for p in default if p in allowed]
    if named and allowed[pool] != PRIMARY:
        # a pool the intent allows only beside its primary one (examples, a method) is read beside it, never instead
        # of it: a professional question naming the appraisals still reads the standards first (KTD12)
        chosen = [p for p in default if allowed.get(p) == PRIMARY and p != pool] + chosen
    if named and allowed[pool] == EXAMPLE:
        notes.append(MSG_POOL_EXAMPLES)
    primaries = [p for p in chosen if allowed[p] == PRIMARY]
    passes = []
    for p in chosen:
        if len(chosen) == 1 or (allowed[p] == PRIMARY and len(primaries) == 1):
            n = limit
        elif allowed[p] == PRIMARY:
            n = max(3, (limit + 1) // 2)
        else:
            n = min(limit, EXAMPLE_HITS)
        passes.append(_Pass(p, allowed[p], n))
    return passes, notes, named


# --- the date of a datum and the order of a time mode (round 11 U8: R4, R6, R7, R12; KTD6, KTD7) ------------------------------
#
# The turn's time mode (``Workspace.time``, set once per turn by the engine) decides how documents, values and measurements
# are ordered. ``recent``: the data dated not later than the turn's anchor date, newest first; then what is later-dated or
# worded as a forecast; then the unknown, in relevance order. ``explicit``: the data inside the requested time first (an
# inclusive range, by the precision of both dates), then the nearest to it, then the unknown. ``none``: no preference —
# today's order; the dates are still shown (R3). An unknown date is never guessed (R6), and ties keep their relevance order.

MSG_DATE_VALUATION = "למועד הקובע {date}"
MSG_DATE_UNKNOWN = "מועד לא ידוע"
MSG_DATE_CONFLICT = "מועד לא ידוע (בחלק הזה של המסמך כמה מועדים קובעים שונים: {dates})"
MSG_DATUM_DATE = "מועד הנתון: {date} ({how})"
MSG_DATUM_DATE_UNKNOWN = "מועד הנתון: מועד לא ידוע (המקור אינו נותן אותו)"
MSG_FORECAST = "תחזית או אומדן לעתיד — אינו נתון שנמדד ואינו הנתון העדכני"
DATUM_DATE_HOW = {chat_dates.SOURCE: "כתוב בשורה או במשפט של הנתון", chat_dates.SOURCE_TABLE: "טווח שבכותרת הטבלה",
                  chat_dates.UNIT_VALUATION: "המועד הקובע של היחידה שהנתון הוא מסקנתה"}
NO_VALUATION = chat_dates.UnitValuation("none", None, (), None)


def valuation_line(uv: chat_dates.UnitValuation) -> str:
    """The date line of a passage: its unit's valuation date, or that it is unknown (a conflict shows every date)."""
    if uv.status == "stated" and uv.date is not None:
        return MSG_DATE_VALUATION.format(date=shown_date(uv.date.iso, uv.date.precision))
    if uv.status == "conflict":
        return MSG_DATE_CONFLICT.format(dates=", ".join(dict.fromkeys(shown_date(d.iso, d.precision) for d in uv.dates)))
    return MSG_DATE_UNKNOWN


def valuation_of(ws: Workspace, conn: Connection, version_id, block_index: int | None) -> chat_dates.UnitValuation:
    """The valuation of the unit that holds a block (unknown for a block no chunk or row places, and with no connection
    to read the dates record from: the date stays unknown rather than failing the value)."""
    if block_index is None or conn is None:
        return NO_VALUATION
    return chat_dates.unit_valuation(conn, version_id, block_index, cache=ws.date_cache)


def datum_line(as_of: dict | None) -> str:
    """What a value or measurement reports about its date: the date and where it came from, or that it is unknown, and
    that it is a forecast when it is worded as one (R7)."""
    if as_of is None or as_of.get("date") is None:
        out = MSG_DATUM_DATE_UNKNOWN
    else:
        shown = shown_period(as_of["date"], as_of["precision"], as_of.get("end"))
        out = MSG_DATUM_DATE.format(date=shown, how=DATUM_DATE_HOW.get(as_of["provenance"], as_of["provenance"]))
    return out + (f" | {MSG_FORECAST}" if as_of and as_of.get("forecast") else "")


def datum_as_of(ws: Workspace, sentence: str, as_of: dict | None) -> dict | None:
    """``as_of`` kept as the contract has it, plus the forecast that needs no date: a sentence worded as a forecast with no
    date found is ``{"date": None, ..., "forecast": True}``, so a forecast is never ranked or shown as measured (R7)."""
    if as_of is not None:
        return dict(as_of)
    if chat_dates.is_forecast(sentence, None, ws.time.anchor):
        return {"date": None, "precision": None, "end": None, "provenance": None, "forecast": True, "locator": None}
    return None


def dated(as_of: dict | None) -> tuple[str, str, str | None] | None:
    """(date, precision, end) of an ``as_of``, None when it holds no date (unknown, or a forecast with no date)."""
    return None if as_of is None or as_of.get("date") is None else (as_of["date"], as_of["precision"], as_of.get("end"))


def measurement_date(ws: Workspace, row, caption: str | None = None) -> dict | None:
    """A stored measurement's ``as_of`` (KTD4): the stored date counts only when the quote holds it; else the date its quote
    states, or its table's range (``caption``, else the row's own); a forecast wording marks it even with no date. Dated
    once per measurement in a turn (the turn's date cache)."""
    key = ("as_of", row.id)
    if key not in ws.date_cache:
        ws.date_cache[key] = datum_as_of(ws, row.quote, chat_dates.measurement_as_of(
            row.quote, row.effective_date, table_caption=getattr(row, "caption", None) if caption is None else caption,
            anchor=ws.time.anchor))
    found = ws.date_cache[key]
    return None if found is None else dict(found)


def bounds(iso: str, precision: str) -> tuple[date, date]:
    """The first and the last day of the period an ISO date (in its precision) names."""
    parts = [int(x) for x in iso.split("-")]
    if precision == "year":
        return date(parts[0], 1, 1), date(parts[0], 12, 31)
    if precision == "month":
        return date(parts[0], parts[1], 1), date(parts[0], parts[1], calendar.monthrange(parts[0], parts[1])[1])
    day = date(*parts)
    return day, day


def period_bounds(iso: str, precision: str, end: str | None = None) -> tuple[date, date]:
    """The first day of the period an ISO date names and the last day of its end (a range runs to its end's last day; no
    end: the date's own period)."""
    return bounds(iso, precision)[0], bounds(end or iso, precision)[1]


@functools.lru_cache(maxsize=256)
def _wanted(requested) -> tuple[tuple[date | None, date | None], ...]:
    """The periods the question asks about, as inclusive day bounds (None: open). A comparison asks about two points.
    ``Requested`` is frozen, so the turn's one requested time is read once, not once per hit."""
    if requested.kind == "compare":
        return tuple(bounds(x, requested.precision) for x in (requested.start, requested.end))
    return ((bounds(requested.start, requested.precision)[0] if requested.start else None,
             bounds(requested.end, requested.precision)[1] if requested.end else None),)


def _gap_to_requested(requested, first: date, last: date) -> int:
    """The days between a period (first to last day) and the nearest period the question asks about: 0 when they overlap
    or when the asked period is open on the side the datum lies."""
    return min(((lo - last).days if lo and last < lo else (first - hi).days if hi and first > hi else 0)
               for lo, hi in _wanted(requested))


def time_key(time: TimeMode, dated: tuple[str, str, str | None] | None, forecast: bool = False) -> tuple:
    """The sort key of a datum under the turn's time mode (KTD6): smaller is first. ``dated``: (ISO date, precision, end of
    a range) or None for an unknown date. Equal keys are not ordered here: the caller's order (relevance, or the id) breaks
    the tie. A date never makes a datum rank by more than it says: a year is its first day, a range its last period."""
    if time.mode == "explicit" and time.requested is not None:
        if dated is None:
            return (2, 0)
        iso, precision, end = dated
        gap = _gap_to_requested(time.requested, *period_bounds(iso, precision, end))
        return (0, 0) if gap == 0 else (1, gap)
    if dated is None:
        return (2, 0)
    iso, precision, end = dated
    at = bounds(end or iso, precision)[0].toordinal()
    if forecast or chat_dates.is_after_anchor(end or iso, precision, time.anchor):
        return (1, at)
    return (0, -at)


def chunk_blocks(ws: Workspace, conn: Connection, chunk_ids: Iterable) -> dict:
    """chunk id -> (first block, last block) of the chunks, read once per turn: the ranking of a time search and the
    rendering of its hits ask for the same chunks. A chunk the database does not know is not in the result."""
    wanted = list(dict.fromkeys(chunk_ids))
    missing = [c for c in wanted if c not in ws.chunk_memo]
    if missing:
        for r in conn.execute(text("SELECT id, block_start, block_end FROM chunks WHERE id = ANY(:i)"), {"i": missing}):
            ws.chunk_memo[r.id] = (r.block_start, r.block_end)
    return {c: ws.chunk_memo[c] for c in wanted if c in ws.chunk_memo}


def prime_dates(ws: Workspace, conn: Connection, version_ids: Iterable) -> None:
    """Read the valuation dates (and, with them, the units) of many versions at once into the turn's date cache, so the
    ``valuation_of`` of each hit that follows costs no query. Only versions that will be dated are given: reading the
    dates of a version derives and stores them."""
    ids = sorted(dict.fromkeys(version_ids), key=str)  # a stable order: derivations lock per version (chat.dates)
    if ids:
        chat_dates.prime_units(conn, ids, ws.date_cache)
        chat_dates.valuation_dates(conn, ids, cache=ws.date_cache)


def _time_memo_key(time: TimeMode) -> tuple:
    """The part of a search memo key the time mode adds (KTD2): the mode and the requested time."""
    r = time.requested
    return (time.mode, None if r is None else (r.kind, r.start, r.end, r.precision))


def ranked_by_time(ws: Workspace, conn: Connection, hits: list[dict], limit: int) -> list[dict]:
    """The hits of the fused window, with the relevant appraisal units ordered by the turn's time mode (KTD6, R4, R12; round
    12 U7, R10, KTD6). Hits are grouped by (version, unit), never by file: a file of several appraisals is not dated by one
    date, each of its units is a group with its own valuation date. Relevance comes first: the relevant set is the first
    ``SEARCH_MAX`` groups by the rank of their best passage in the window (the first ``limit`` for a workspace with
    ``exact_limit``), and only those are ordered by date — a barely relevant newer unit is not lifted above them, and the
    groups past the set keep their relevance order after it. A group is dated by the valuation date of its unit; an unknown
    or conflicting date ranks as unknown. The output covers the best hit of every group of the ordered set (so a small
    ``limit`` never cuts a relevant dated unit, and never more than ``SEARCH_MAX`` hits), then, while room remains under
    ``max(limit, that count)`` (``limit`` itself for a workspace with ``exact_limit``), the remaining hits: the set's further
    hits in the same group order (each group's in relevance order), then the hits past the set in relevance order. A hit
    whose chunk has no block belongs to its version's undated group."""
    if not hits:
        return hits
    blocks = chunk_blocks(ws, conn, [h["chunk_id"] for h in hits])
    starts = {h["chunk_id"]: blocks.get(h["chunk_id"], (None, None))[0] for h in hits}
    # the units of every hit are needed to group them: the dates (and, with them, the units) of the versions are read
    # together before the loop
    prime_dates(ws, conn, [h["version_id"] for h in hits if starts[h["chunk_id"]] is not None])
    by_unit: dict[tuple, list[dict]] = {}
    valued: dict[tuple, chat_dates.UnitValuation] = {}
    for h in hits:
        uv = valuation_of(ws, conn, h["version_id"], starts[h["chunk_id"]])
        key = (str(h["version_id"]), uv.unit_no)
        by_unit.setdefault(key, []).append(h)
        valued.setdefault(key, uv)
    groups = list(by_unit.items())
    # a workspace with ``exact_limit`` (a search-only answer) shows ``limit`` groups: those are the relevant set, so the
    # date order only permutes the units relevance chose and never cuts the best older match (review #4)
    cut = min(SEARCH_MAX, limit) if ws.exact_limit else SEARCH_MAX
    relevant, past = groups[:cut], groups[cut:]
    keyed = []
    for position, (key, group) in enumerate(relevant):
        uv = valued[key]
        when = (uv.date.iso, uv.date.precision, None) if uv.status == "stated" and uv.date is not None else None
        keyed.append((time_key(ws.time, when), position, group))
    keyed.sort(key=lambda k: k[:2])
    firsts = [g[0] for *_, g in keyed]
    further = [h for *_, g in keyed for h in g[1:]] + [h for _, g in past for h in g]
    return (firsts + further)[:limit if ws.exact_limit else min(SEARCH_MAX, max(limit, len(firsts)))]


# --- tools -----------------------------------------------------------------------------------------------

def memoized_search(ws: Workspace, conn: Connection, query: str, limit: int, scope: SearchScope | None,
                    window: bool = False, version: int | None = None) -> list[dict]:
    """``search_passages`` once per (query, scope, limit) in a turn (R16, KTD3): an identical second search runs no
    search SQL and no embedding. The office's data version is part of the key — read in the caller's transaction, one
    indexed row — so a document processed, deleted or republished mid-turn is never answered from before it. So are the
    turn's time mode and requested time (round 11 KTD2), and whether the fused window was read whole (``window``).
    ``version`` is that data version when the caller already read it for a run of searches."""
    scope_key = None if scope is None else (
        tuple(sorted(map(str, scope.document_ids))), tuple(sorted(map(str, scope.version_ids))),
        scope.include_noncurrent, scope.blocks, tuple(sorted(scope.source_kinds)),
        tuple(sorted(map(str, scope.exclude_versions))))
    key = (query, scope_key, limit, current_data_version(conn) if version is None else version,
           _time_memo_key(ws.time), window)
    hits = ws.search_memo.get(key)
    if hits is None:
        # round 22 U0 (R10): the search's candidate counts and seconds go to the turn's stage clock (the next tool record); the
        # hits are the same with or without it
        started = time.monotonic()
        with measure_searches() as ran:
            hits = ws.search_memo[key] = (search_passages(conn, query, limit, scope=scope, window=True) if window
                                          else search_passages(conn, query, limit, scope=scope))
        ws.clock.note_search({"lexical": sum(r["lexical"] for r in ran), "fused": sum(r["fused"] for r in ran),
                              "depth": max((r["depth"] for r in ran), default=0), "evidence_calls": len(ran),
                              "returned": len(hits), "seconds": round(time.monotonic() - started, 3)})
    else:
        ws.clock.note_search(None)
    return [dict(h) for h in hits]


MSG_UNIT_SEARCHED = ("חיפשתי ביחידה {unit} ({pages}) {extended}ולא נמצא קטע שמכיל את מילות החיפוש \"{query}\". היחידה "
                     "נחקרה; לא חיפשתי בשאר הקובץ. אפשר לנסות ניסוח אחר או מונחים נרדפים.")
MSG_UNIT_EXTENDED = ("גבול היחידה אינו ודאי (נקבע על סמך סימן אחד): החיפוש כלל גם את {which} עד הגבול הוודאי הבא.")
# round 15 U2 (R8, KTD10): a unit search with no hit does not widen on its own; it names the uncertain edge it crossed and
# the neighbour it reached, and the model widens by an explicit call (a search of that neighbour, or of no unit)
MSG_UNIT_EDGES = ("הגבול הלא ודאי: החיפוש הגיע עד {which} ולא רחוק יותר, ולא הורחב מעצמו. אם הנתון עשוי להיות מעבר לגבול — "
                  "חפש במפורש ביחידה השכנה (unit=U#) או בלי unit.")
MSG_PASSAGE_CUT = ("[הקטע קוצר סביב מילות החיפוש; read(source={sid}) מחזיר את הקטע כולו]")
MSG_TABLE_CUT = "[שורות הטבלה שמעבר לגבול היחידה הושמטו]"
SCOPED_PASSAGE_CHARS = 900  # a hit of a unit-scoped search (round 15 U2, R9); the other searches keep ``chat_passage_chars``
MSG_UNIT_ARGS = "היחידה (unit) והמסמך שנבחר אינם של אותו קובץ: היחידה קובעת את המסמך — בחר רק אחד מהם"
MSG_PROPERTY_SEARCHED = ("(הנכס שהשיחה עוסקת בו: החיפוש כולל את כל השומות של הנכס הזה בארגון, לא רק את השומה שבמוקד. "
                         "כל תוצאה מסומנת בתאריך הקובע של השומה שלה.)")
# round 12 U12b (S2, R3): the link of a gathered appraisal other than the focus, as the model and the judge read it. The
# coverage record keeps the reason as property.py states it ("parcel B/P", "address_city"); these words say it
MSG_LINK_SAME_PROPERTY = "שומה של אותו נכס: {why}"
MSG_LINK_PARCEL = "חלקה {parcels} משותפת"
MSG_LINK_ADDRESS = "אותם רחוב, מספר ועיר"
MSG_LINK_FOCUS = "השומה שבמוקד"
MSG_PROPERTY_UNITS_HEAD = "השומות שהשרת קישר לנכס"
MSG_PROPERTY_UNITS = (MSG_PROPERTY_UNITS_HEAD + " ({count}): {units}. מספר בית או כתובת אחרים בשומה כזו אינם סיבה להחריג "
                      "אותה: הקישור נקבע על ידי השרת, והיא שומה של הנכס שנשאלת עליו השאלה.")
PROPERTY_UNITS_SHOWN = 10  # units the line names; the rest are counted


def link_phrase(reason: str | None) -> str | None:
    """A link reason of the property graph in the words the model and the judge read (None for no link): a shared parcel
    ("parcel 8401/40") or the same street, number and city ("address_city")."""
    if not reason:
        return None
    if reason.startswith(REASON_PARCEL_PREFIX):
        return MSG_LINK_SAME_PROPERTY.format(why=MSG_LINK_PARCEL.format(parcels=reason.removeprefix(REASON_PARCEL_PREFIX)))
    if reason == REASON_ADDRESS_CITY:
        return MSG_LINK_SAME_PROPERTY.format(why=MSG_LINK_ADDRESS)
    return MSG_LINK_SAME_PROPERTY.format(why=reason)


MSG_PROPERTY_NOTHING = ("חיפשתי בכל השומות של הנכס שהשיחה עוסקת בו ({count} יחידות) ולא נמצא קטע שמכיל את מילות החיפוש "
                        "\"{query}\". אפשר לנסות ניסוח אחר או מונחים נרדפים.")


def _unit_of(conn: Connection, ws: Workspace, handle: str):
    """(the version, its contexts, the unit's number) of a unit handle (U#), under the current permissions and only
    while its reading is the current one."""
    handle = str(handle or "").strip()
    data = _handle(ws, handle, "U")
    v = _bound(conn, ws, handle, data)
    cx = contexts_of(ws, conn, v.version_id, v.reading_id)
    if data["unit_no"] not in cx.numbers:
        raise ToolError(MSG_STALE_HANDLE.format(handle=handle))
    ws.readings.setdefault(str(v.version_id), v.reading_id)
    if cx.multi:
        ws.unit_scopes[cx.key(data["unit_no"])] = {"document_id": str(v.document_id), "version_id": str(v.version_id),
                                                   "reading_id": v.reading_id, "unit_no": data["unit_no"]}
    return v, cx, data["unit_no"]


MSG_UNIT_FOCUS = "(היחידה שהשיחה עוסקת בה: {unit})"


def _documents_of_focus(ws: Workspace, documents: list[str] | str | None) -> bool:
    """Whether a call's documents are the focus unit's own file or nothing (a call that names any other document is as
    asked)."""
    focus = ws.unit_focus
    if not focus:
        return False
    named = {str(x).strip() for x in ([documents] if isinstance(documents, str) else documents or ()) if str(x).strip()}
    return named <= {focus["document_id"], ws.doc_handle(focus["document_id"])}


def focus_unit_for(ws: Workspace, documents: list[str] | str | None) -> str | None:
    """The handle of the unit a call that names no unit is scoped to, or None. Since round 15 U6 (KTD1, KTD3) a model turn
    scopes nothing it was not told to: a call works inside a unit only when it names that unit (``unit=U#``, also the
    focus unit's own handle), so a call that names none is never silently scoped. Only the server's pin of the search-only
    path (``Workspace.unit_default``), which has no model to choose, scopes the calls that name no unit and no other document
    than the pinned unit's file to it (KTD8 of round 9)."""
    focus = ws.unit_focus
    if not focus or not ws.unit_default:
        return None
    return focus["handle"] if _documents_of_focus(ws, documents) else None


def names_focus_unit(ws: Workspace, unit: str | None, documents: list[str] | str | None = None) -> bool:
    """Whether a call's ``unit`` is the conversation's own unit (round 11 U16, R4, R11, KTD6): the model sees the focus
    unit's handle in the context and passes it, which is no choice of another unit — the search then says it works in the
    conversation's unit (``MSG_UNIT_FOCUS``). Since round 15 U2 (KTD10) this decides no widening: the time search follows
    the unit any call names. A handle is the same handle for the same unit (``unit_handle_of``), but the version, reading
    and unit number are compared too; a call that names another document is no focus call."""
    focus = ws.unit_focus
    if not focus or not unit or not _documents_of_focus(ws, documents):
        return False
    unit = str(unit).strip()
    if unit == focus["handle"]:
        return True
    a, b = ws.handles.get(unit), ws.handles.get(focus["handle"])
    return bool(a and b and a["kind"] == b["kind"] == "U" and (a["version_id"], a["reading_id"], a["unit_no"])
                == (b["version_id"], b["reading_id"], b["unit_no"]))


def locate_unit(ws: Workspace, anchor: dict, version_id) -> bool:
    """The server pins a unit for the question (round 9; since round 15 U6, KTD3, only the search-only path does, because
    it has no model to choose a unit — the model turn gets candidates instead): it becomes the turn's working unit
    (``unit_focus``) and the default scope of the calls that name no unit and no other document
    (``Workspace.unit_default``, ``focus_unit_for``). A second unit located in the turn is a comparison, not one property:
    the turn's own default scope is dropped; a unit kept from the conversation is never replaced. Returns whether the
    turn's unit is now ``anchor``."""
    from app.chat import entities

    key = contexts.anchor_key(version_id, anchor)  # carries the derivation tag, so it matches ``cx.key(n)`` (round 15 U2)
    ws.turn_located[key] = anchor
    if len(ws.turn_located) > 1:
        if ws.located_unit is not None:
            ws.unit_focus = ws.located_unit = None
            ws.unit_default = False
        return False
    if ws.unit_focus is not None:
        return False
    handle = unit_handle_of(ws, anchor["document_id"], version_id, anchor.get("reading_id"), anchor["unit_no"],
                            anchor.get("units_tag"))
    ws.unit_focus = {"handle": handle, "document_id": str(anchor["document_id"]), "display": entities.focus_text(anchor)}
    ws.located_unit = {"key": key, "anchor": anchor}
    ws.unit_default = True
    return True


def _unit_pages(first: int | None, last: int | None) -> str:
    """A unit's pages as the tools show them ("עמוד ?" when its first page is not known)."""
    return contexts.pages_text(first, last, "עמוד ?")


def _unit_search(ws: Workspace, conn: Connection, handle: str, query: str, limit: int,
                 document_ids: list[str] | None) -> tuple[list[dict], list[str], str | None]:
    """A search inside one unit (KTD7): the chunks that begin in its blocks, and in the neighbouring unit up to the next
    certain edge where an edge of it is only probable. It never widens to the whole file on its own (round 15 U2, R8,
    KTD10): with no hit it says the unit was searched and nothing found, and — next to a probable edge — names the
    neighbour the search reached, so the model widens by an explicit call. Each hit carries the block range it was searched
    in (``scope_blocks``), for the table clip. Returns (hits, notes for the model, the answer when nothing was found)."""
    v, cx, number = _unit_of(conn, ws, handle)
    _same_file(ws, v, document_ids)
    if cx.multi:
        reach = cx.reach(number)
        scope = SearchScope(version_ids=(v.version_id,), blocks=(reach.first, reach.last))
        unit = f"{handle} «{_txt(cx.label(number))}»"
    else:  # a file with one unit: the file is its unit
        reach, scope, unit = None, SearchScope(version_ids=(v.version_id,)), f"{handle} (כל הקובץ)"
    notes = []
    extended = which = ""
    if reach is not None and reach.extended:
        which = ", ".join(unit_handle(ws, cx, n, v.document_id) for n in (*reach.before, *reach.after))
        extended = MSG_UNIT_EXTENDED.format(which=which) + " "
        notes.append(extended.strip())
    # "a hit" of a unit search is a passage that holds the query's words (exactly or nearly): the passages nearest
    # by meaning exist in any unit, and are no answer to "is it in this unit"
    hits = memoized_search(ws, conn, query, limit, scope)
    if any(h.get("lexical_support") for h in hits):
        return [h | {"scope_blocks": scope.blocks} for h in hits], \
            [f"חיפוש בתוך היחידה {unit} ({_unit_pages(*cx.pages(number))})", *notes], None
    nothing = MSG_UNIT_SEARCHED.format(unit=unit, pages=_unit_pages(*cx.pages(number)), extended=extended, query=_txt(query))
    return [], [], nothing + (" " + MSG_UNIT_EDGES.format(which=which) if which else "")


def _same_file(ws: Workspace, v, document_ids: list[str] | None) -> None:
    """A call that names a unit and documents names them of the unit's own file only: the unit fixes the document."""
    if document_ids and {str(x) for x in document_ids} - {str(v.document_id), ws.doc_handle(v.document_id)}:
        raise ToolError(MSG_UNIT_ARGS)


def _memo(ws: Workspace, key: tuple, build: Callable[[], Any]) -> Any:
    """``ws.property_memo[key]``, built on first use. ``key`` is (name, ..., data version): the entries of the same name (and
    the same middle parts, e.g. a property's seed) at another data version are evicted first — a data version that moved
    on is never read again."""
    found = ws.property_memo.get(key)
    if found is None:
        for old in [k for k in ws.property_memo if k[0] == key[0] and k[1:-1] == key[1:-1]]:
            del ws.property_memo[old]
        found = ws.property_memo[key] = build()
    return found


def property_graph(ws: Workspace, conn: Connection) -> PropertyGraph:
    """The turn's property graph (round 12 U6, KTD5): the relation of every visible, current subject unit to every other,
    built once per turn and data version and kept in ``ws.property_memo``. Retrieval (``_property_search``, ``_named_gathered``, ``_question_gathered``), the timeline
    (``property_partition``) and the coverage read this one snapshot. Identities are derived on first use and cached in
    the turn's ``date_cache`` (``identity.identities``)."""
    return _memo(ws, ("graph", current_data_version(conn)), lambda: build_graph(conn, cache=ws.date_cache))


def property_gathered(ws: Workspace, conn: Connection, version_id, unit_no: int) -> Gathered | None:
    """The focus property of a unit: its compatible closure in the turn's graph, with the link reasons, the named
    candidates and the undetermined units (``property.Gathered``). None when the unit is not in the graph (not a visible
    subject unit of a current appraisal)."""
    graph = property_graph(ws, conn)
    node = graph.find(version_id, unit_no)
    return graph.gather(node.key) if node is not None else None


def property_partition(ws: Workspace, conn: Connection) -> Partition:
    """Every unit of the turn's graph in one group (round 12 U6, KTD5): the conversation's focus property as one group,
    and the other units grouped without a seed (``property.PropertyGraph.partition``). The focus is the conversation's unit
    (``ws.unit_focus``) while its handle's reading is still the current one; without one, no unit is the focus."""
    graph = property_graph(ws, conn)
    focus = None
    handle = (ws.unit_focus or {}).get("handle")
    data = ws.handles.get(handle) if handle else None
    if data and data.get("kind") == "U":
        head = reader.version(conn, UUID(data["version_id"]))
        node = graph.find(data["version_id"], data["unit_no"]) if head is not None and head.reading_id == data["reading_id"] else None
        focus = graph.gather(node.key) if node is not None else None
    return graph.partition(focus)


# --- round 12 U7: the inventory, the focused per-unit search and the coverage record ---------------------------------
#
# A time question about an identified property does not search a relevance window and hope the property's latest appraisal
# is in it (a window of twelve documents cannot show that). The server lists the property's appraisals and their valuation
# dates first (the *inventory*: the focus unit's gathered set in the turn's property graph), selects up to ``CAP_UNITS`` of
# them in the order the turn's mode gives, searches each selected unit on its own, ranks the hits per (version, unit)
# (``ranked_by_time``) and records, per unit, whether it was searched with a match, searched without one, or not searched
# for the cap. The record is built from the graph snapshot the inventory came from and kept on the workspace for the answer's
# wording and payload (U8): it states nothing in words.

CAP_UNITS = 8          # appraisal units one call reads (KTD6)
CAP_UNDATED = 2        # of them, up to this many are kept for units with no valuation date, in document order
NAMED_CANDIDATES = 3   # unlinked candidates the record names, newest first; the rest are counted
COVERAGE_V = 2         # the record's version (``answer.time.coverage.v``); 2: the three classes and ``located``/``read`` (U8)
PROBE_LIMIT = 60       # passages the coverage probe reads (round 13 U8, KTD15): one search over the undetermined units
PROBE_IDENTIFIERS = 4  # addresses, parcels and aliases the probe query names, each
# the record's unit statuses, scopes and blocking reasons are the strings ``timeframe`` words the note from (one definition,
# imported here: ``timeframe`` is a leaf and cannot import this module)


def stamp_valuation(ws: Workspace, conn: Connection, src: Source) -> Source:
    """The date line of a passage of an appraisal the turn opened by any route (round 13 U9, KTD10, R13): a read of a section,
    pages, paragraphs or a table, an inspection, a cell taken from a cached read — not only a search hit. The unit's stated
    valuation date, as ``valuation_line`` words it, from the block the passage begins at; a source that has one (a search
    hit, a source returned again), a professional source and a passage with no place in the document keep what they have.
    The verifier reads it as the server's own fact about the unit (``verify._valuation_dates``)."""
    if src.date_line is None and _dated_appraisal(src):
        src.date_line = valuation_line(valuation_of(ws, conn, src.version_id, src.block_start))
    return src


def _stamp_new_sources(ws: Workspace, before: int) -> None:
    """``stamp_valuation`` for the sources a tool call added that no route stamped (one read of the dates, in one
    transaction); a date that cannot be read leaves the source without a line, never fails the tool."""
    fresh = [s for s in islice(ws.sources.values(), before, None) if s.date_line is None and _dated_appraisal(s)]
    if not fresh:
        return
    try:
        with tenant_tx(ws.ctx) as conn:
            for s in fresh:
                stamp_valuation(ws, conn, s)
    except Exception:  # noqa: BLE001 - the line is evidence, not a result
        logger.warning("valuation date line not stamped on %d sources", len(fresh))


@dataclass
class InventoryUnit:
    """One appraisal unit of the property's inventory: what it is, its stated valuation date (``date``: ISO date and
    precision; None when the unit states none or several that disagree — ``date_status`` says which), the reason of the
    link it joined by, and its search status (``LOCATED``, ``NO_MATCH`` or ``NOT_SEARCHED_CAP``; ``READ`` once the answer is
    known to rest on the unit, ``settle_coverage``). ``undated`` is a flag, not a status: an undated unit is searched or not
    like any other."""
    document_id: Any
    version_id: Any
    unit_no: int
    title: str | None
    date: tuple[str, str] | None
    date_status: str
    link: str | None = None
    focus: bool = False
    status: str = NOT_SEARCHED_CAP

    @property
    def key(self) -> tuple[str, int]:
        return (str(self.document_id), self.unit_no)

    @property
    def undated(self) -> bool:
        return self.date is None

    @property
    def dated(self) -> tuple[str, str, str | None] | None:
        return None if self.date is None else (self.date[0], self.date[1], None)


@dataclass
class TimeCall:
    """One time-ranked search call of a turn: its query, the coverage record of what it checked, and the S# its results
    became (the sources the answer may cite), so the coverage of a datum is that of the call that produced it (KTD7)."""
    query: str
    record: dict | None = None
    sids: list[str] = field(default_factory=list)
    # the link phrase of each gathered unit other than the focus, by (version id, unit number) — what the call's sources
    # carry in their header (round 12 U12b); ``numbers`` the unit numbers the inventory holds per version
    links: dict = field(default_factory=dict)
    numbers: dict = field(default_factory=dict)

    def link_of(self, version_id, unit_no: int | None) -> str | None:
        """The link a hit in unit ``unit_no`` of a version carries; a version of one unit is that unit whatever number the
        hit's date reading gives it (a DOCX has no units of its own)."""
        known = self.numbers.get(str(version_id)) or []
        if unit_no in known:
            return self.links.get((str(version_id), unit_no))
        return self.links.get((str(version_id), known[0])) if len(known) == 1 else None


def unit_dating(ws: Workspace, conn: Connection, version_id, unit_no: int) -> tuple[tuple[str, str] | None, str]:
    """((date, precision) or None, the unit's date status) of one unit: its stated valuation date, ``conflict`` when its dates
    disagree (the unit ranks as unknown), ``none`` when it states none. Read from the turn's date cache."""
    dates = chat_dates.valuation_dates(conn, [version_id], cache=ws.date_cache).get(UUID(str(version_id)), ())
    uv = chat_dates.resolve_unit([d for d in dates if d.unit_no == unit_no], unit_no)
    return ((uv.date.iso, uv.date.precision) if uv.status == "stated" and uv.date is not None else None), uv.status


def _inventory_key(time: TimeMode, dated: tuple[str, str, str | None] | None) -> tuple:
    """The order key of an inventory unit under the turn's mode (KTD6; smaller is first). ``recent``: ``time_key`` (the
    data not later than the anchor newest first, then the later-dated, then the undated). ``explicit``: the units dated in
    the requested time first (0), then the units dated after it (1, nearest first: a later appraisal may list earlier
    data), then the undated (2), then the units dated before it (3, which cannot hold data of the requested time and are
    read only if the cap leaves room)."""
    if time.mode != "explicit" or time.requested is None:
        return time_key(time, dated)
    if dated is None:
        return (2, 0)
    iso, precision, end = dated
    first, last = period_bounds(iso, precision, end)
    gap = _gap_to_requested(time.requested, first, last)
    if gap == 0:
        return (0, 0)
    return (1, gap) if any(lo is None or last >= lo for lo, _ in _wanted(time.requested)) else (3, gap)


def property_inventory(ws: Workspace, conn: Connection, gathered: Gathered) -> list[InventoryUnit]:
    """The property's appraisal units with their valuation dates, ordered by the turn's mode (KTD6): the gathered set of the
    focus unit, each member dated per unit (``chat.dates``, assigned by block range, so a file of several units is never
    dated by one date). Ties break by document id and unit number, so the order is total."""
    members = gathered.members
    prime_dates(ws, conn, [n.version_id for n in members])
    reasons = gathered.reasons()
    out = []
    for n in members:
        date_, status = unit_dating(ws, conn, n.version_id, n.unit_no)
        out.append(InventoryUnit(n.document_id, n.version_id, n.unit_no, n.title, date_, status, reasons.get(n.key),
                                 n.key == gathered.seed))
    return sorted(out, key=lambda u: (_inventory_key(ws.time, u.dated), u.key))


def select_units(ordered: list[InventoryUnit]) -> list[InventoryUnit]:
    """The units one call searches (KTD6): at most ``CAP_UNITS``, in the order they are given. Up to ``CAP_UNDATED`` slots
    are kept for undated units (in document order) so a unit with no date is never silently left out behind a long dated
    history; slots the dated units do not fill go to further undated ones."""
    undated = [u for u in ordered if u.undated]
    dated = [u for u in ordered if not u.undated]
    chosen = dated[:CAP_UNITS - min(CAP_UNDATED, len(undated))]
    chosen += undated[:CAP_UNITS - len(chosen)]
    keys = {u.key for u in chosen}
    return [u for u in ordered if u.key in keys]


def _unit_scope(ws: Workspace, conn: Connection, unit: InventoryUnit) -> SearchScope:
    """The search scope of one inventory unit: its version, and inside a file of several units its block range
    (``contexts.reach``, extended over a neighbouring unit only up to the next certain edge)."""
    version = UUID(str(unit.version_id))
    kinds = POOL_KINDS[APPRAISAL_POOL]
    cx = contexts_of(ws, conn, version)
    if cx.multi:
        reach = cx.reach(unit.unit_no)
        return SearchScope(version_ids=(version,), blocks=(reach.first, reach.last), source_kinds=kinds)
    return SearchScope(version_ids=(version,), source_kinds=kinds)


def _titles(ws: Workspace, conn: Connection, document_ids: Iterable) -> dict[str, str]:
    """document id -> title, for the documents a record names (under the caller's permissions). The titles read are kept
    per turn and data version in ``ws.property_memo``; only the ids not read yet are queried."""
    ids = sorted({str(d) for d in document_ids})
    if not ids:
        return {}
    known = _memo(ws, ("titles", current_data_version(conn)), dict)
    missing = [i for i in ids if i not in known]
    if missing:
        known.update({str(r.id): r.title for r in conn.execute(
            text("SELECT id, title FROM documents WHERE id = ANY(CAST(:d AS uuid[]))"), {"d": missing})})
    return {i: known[i] for i in ids if i in known}


def _no_candidates() -> dict:
    """The record's candidates when the property has none to name: a fresh dict each time (the record is stored and
    merged, never shared)."""
    return {"named": [], "count": 0}


def _coverage_item(ws: Workspace, conn: Connection, node, titles: dict[str, str], reason: str, elsewhere: bool) -> dict:
    """One candidate of the record (``_candidates_of``, ``_classify_uncached``): its place, title, date, the ``reason`` it is
    a candidate, whether every mention stands under another subject (``elsewhere``) and ``read`` (False until
    ``settle_coverage``)."""
    date_, status = unit_dating(ws, conn, node.version_id, node.unit_no)
    return {"document_id": str(node.document_id), "version_id": str(node.version_id), "unit_no": node.unit_no,
            "title": node.title or titles.get(str(node.document_id)), "date": date_[0] if date_ else None,
            "precision": date_[1] if date_ else None, "date_status": status, "reason": reason,
            "elsewhere": elsewhere, "read": False}


def _candidates_of(ws: Workspace, conn: Connection, gathered: Gathered, elsewhere: dict | None = None) -> dict:
    """The unlinked candidates of the property for the record (KTD7): up to ``NAMED_CANDIDATES`` named newest first with
    their dates and the reason they are not linked, the rest counted. Round 13 U8 (KTD15): every candidate is kept as an
    ``item`` with ``elsewhere`` (the probe found its mentions under another subject, ``elsewhere``) and ``read`` (False until
    ``settle_coverage``), so one the turn read under another subject leaves the class (``_settle_class``)."""
    if not gathered.candidates:
        return _no_candidates()
    prime_dates(ws, conn, [c.node.version_id for c in gathered.candidates])
    titles = _titles(ws, conn, [c.node.document_id for c in gathered.candidates])
    items = [_coverage_item(ws, conn, c.node, titles, c.link.reason,
                            (elsewhere or {}).get((str(c.node.version_id), c.node.unit_no), False))
             for c in gathered.candidates]
    group = {"named": [], "count": 0, "read": 0, "items": items}
    _settle_class(group)
    return group


def _alias_owners(ws: Workspace, conn: Connection) -> dict[str, set[tuple[str, int]]]:
    """Each alias (``contexts.name_key``) of the office's units -> the (version, unit number) of the units that carry it: the
    units of every version in the turn's property graph, read from the units the graph build already cached. Once per turn
    and data version."""
    key = ("aliases", current_data_version(conn))

    def build() -> dict:
        owners = ws.property_memo[key] = {}  # stored before it is filled, as the other per-turn tables are
        for version in sorted({str(n.version_id) for n in property_graph(ws, conn).nodes}):
            for u in chat_dates.units_of(conn, UUID(version), ws.date_cache):
                for a in u.aliases:
                    owners.setdefault(contexts.name_key(a), set()).add((version, u.unit_no))
        return owners

    return _memo(ws, key, build)


def _node_unit(ws: Workspace, conn: Connection, node):
    """The stored unit a graph node stands for (its version's units, kept in the turn's ``date_cache``); None when it has none."""
    return next((u for u in chat_dates.units_of(conn, UUID(str(node.version_id)), ws.date_cache)
                 if u.unit_no == node.unit_no), None)


def _focus_identity(ws: Workspace, conn: Connection, gathered: Gathered) -> contexts.Identity:
    """The focus property's identifiers as the probe and the matcher read them (round 13 U8, KTD15): the street core and number
    of each address of its members, their block and parcel pairs, and the document-grounded aliases of their units. An alias
    counts only when it is distinctive: two words or more (a one-word alias would match any text) and carried by no unit
    outside the property's members — a description of the property's type ("דיור מוגן") is on every unit of that type and
    would make each of them a candidate. ``contexts.Identity.identifier_in`` and ``name_spans`` match them in free text with
    the prefix letters and the label optional, as for a question."""
    own = {(str(n.version_id), n.unit_no) for n in gathered.members}
    owners = _alias_owners(ws, conn)
    addresses, parcels, streets, aliases = set(), set(), [], []
    for n in gathered.members:
        for core, number in sorted(n.identity.addresses):
            if (core, number) in addresses:
                continue
            addresses.add((core, number))
            m = re.fullmatch(r"(\d+)([א-ת]?)", number)
            streets.append((tuple(core.split()), m.group(1) if m else number, m.group(2) if m else ""))
        parcels |= {(b, p) for b, p, _ in n.identity.parcels}
        if (u := _node_unit(ws, conn, n)) is not None:  # cached by ``_alias_owners`` for every version of the graph
            aliases += [a for a in (contexts.name_key(x) for x in u.aliases)
                        if len(a.split()) >= 2 and owners.get(a, set()) <= own]
    return contexts.Identity(frozenset(addresses), frozenset(parcels), (), tuple(streets), tuple(dict.fromkeys(aliases)))


def _probe_query(identity: contexts.Identity) -> str:
    """The words of the probe's one search: each address (street core and number), each block and parcel, each alias, at most
    ``PROBE_IDENTIFIERS`` of each kind in a stable order."""
    streets = [f"{' '.join(w)} {num}{letter}" for w, num, letter in sorted(identity.streets)[:PROBE_IDENTIFIERS]]
    parcels = [f"גוש {b} חלקה {p}" for b, p in sorted(identity.parcels)[:PROBE_IDENTIFIERS]]
    return " ".join([*streets, *parcels, *identity.names[:PROBE_IDENTIFIERS]])


# the words that put a mention under another subject: a comparison, a transaction, an appendix (round 13 U8, KTD15). The
# section name of the passage and the lines that carry the mention are read, never the whole chunk
_OTHER_SUBJECT = re.compile(r"השוואה|השוואתי|עסק(?:ה|ת|אות)|מכירות|נכסים דומים|נכס דומה|נספח")


def _mentions_elsewhere(identity: contexts.Identity, hit: dict) -> bool:
    """Whether a passage's mention of the property stands under another subject: its section's name, or the lines of the
    passage that carry the mention, say comparison, transaction or appendix."""
    lines = [ln for ln in (hit.get("text") or "").splitlines() if identity.named_in(ln)]
    return bool(_OTHER_SUBJECT.search(" ".join([hit.get("section") or "", *lines])))


def _partly_read(ws: Workspace, conn: Connection, nodes: Iterable) -> set[tuple[str, int]]:
    """The (version, unit number) of the units whose identification region was not read (round 13 U8, KTD15): a block with
    status ``unread`` in the unit's opening — its first ``units.RUN_MAX`` blocks (the title run the identity reader reads)
    or any block on its first page. Such a unit may state the property where nothing could read it, so it is a possible
    candidate even with no mention. An uncertain block was read (it is searchable, so the probe covered it) and a block
    unread elsewhere in a large unit is the ordinary case: neither counts, nor does a version that is partial elsewhere.
    One query for all the units."""
    nodes = list(nodes)
    versions = sorted({str(n.version_id) for n in nodes})
    if not versions:
        return set()
    unread: dict[str, list[tuple[int, int | None]]] = {}
    for r in conn.execute(text("SELECT version_id, block_index, page FROM document_blocks WHERE version_id ="
                               " ANY(CAST(:v AS uuid[])) AND status = :a"), {"v": versions, "a": reader.UNREAD}):
        unread.setdefault(str(r.version_id), []).append((r.block_index, r.page))
    found = set()
    for n in nodes:
        v = str(n.version_id)
        unit = _node_unit(ws, conn, n)
        if unit is None:
            continue
        if any(unit.block_first <= b <= unit.block_last and (b < unit.block_first + unit_store.RUN_MAX
                                                             or (page is not None and page == unit.page_first))
               for b, page in unread.get(v, ())):
            found.add((v, n.unit_no))
    return found


def _mentioned(ws: Workspace, conn: Connection, nodes: Iterable, identity: contexts.Identity) -> dict[tuple[str, int], bool]:
    """The units among ``nodes`` (the undetermined units and the unlinked candidates) whose text mentions the focus
    property's identifiers, from ONE scoped search over their versions (round 13 U8, KTD15; the search is memoized, so a
    second call of the turn costs none): (version, unit number) -> whether every mention stands under another subject
    (``_mentions_elsewhere``). A passage counts only when the identifiers are in its text (``identifier_in``); the search
    ranks, it does not decide."""
    by_unit = {(str(n.version_id), n.unit_no): n for n in nodes}
    by_version: dict[str, list[tuple[str, int]]] = {}
    for k in by_unit:
        by_version.setdefault(k[0], []).append(k)
    query = _probe_query(identity)
    if not by_unit or not query:
        return {}
    kinds = POOL_KINDS[APPRAISAL_POOL]
    scope = SearchScope(version_ids=tuple(sorted({UUID(v) for v, _ in by_unit})), source_kinds=kinds)
    hits = [h for h in memoized_search(ws, conn, query, PROBE_LIMIT, scope, window=True) if identity.named_in(h.get("text") or "")]
    placed = chunk_blocks(ws, conn, [h["chunk_id"] for h in hits])
    out: dict[tuple[str, int], bool] = {}
    for h in hits:
        version = str(h["version_id"])
        cx = contexts_of(ws, conn, version)
        number = cx.at_block(placed.get(h["chunk_id"], (None, None))[0])
        if (version, number) not in by_unit:
            alone = by_version.get(version, [])
            if cx.multi or len(alone) != 1:
                continue
            number = alone[0][1]
        out[(version, number)] = out.get((version, number), True) and _mentions_elsewhere(identity, h)
    return out


@dataclass(frozen=True)
class Classes:
    """What the probe made of the rest of the office (``classify_undetermined``): the property with its undetermined units
    classified, the possible candidates as record items, and, for every probed unit, whether all its mentions stand under
    another subject."""
    gathered: Gathered
    possible: list[dict]
    elsewhere: dict


def classify_undetermined(ws: Workspace, conn: Connection, gathered: Gathered) -> Classes:
    """The property with its undetermined units in the classes of R18 (round 13 U8, KTD15), once per turn and data version:
    one bounded lexical probe for the focus property's identifiers over the undetermined units' and the unlinked candidates'
    versions, then

    - **possible candidates** — an undetermined unit with a hit, or whose identification region (its opening) was not read: they block completeness and are
      named in the note;
    - **unlinkable** — the other undetermined units: counted only, they block nothing;
    - **excluded** — units proven to be another property (counted by the record, never searched).

    The possible candidates are record items (document, version, unit, title, date, the reason, ``elsewhere``: every mention
    stands under another subject, and ``read``: False until ``settle_coverage``). No search runs when there is nothing to
    probe or the property states no identifier."""
    if not gathered.undetermined and not gathered.candidates:
        return Classes(gathered, [], {})
    return _memo(ws, ("classes", gathered.seed, current_data_version(conn)),
                 lambda: _classify_uncached(ws, conn, gathered))


def _classify_uncached(ws: Workspace, conn: Connection, gathered: Gathered) -> Classes:
    mentioned = _mentioned(ws, conn, [*gathered.undetermined, *(c.node for c in gathered.candidates)],
                           _focus_identity(ws, conn, gathered))
    partly = _partly_read(ws, conn, gathered.undetermined)
    nodes = [n for n in gathered.undetermined if (str(n.version_id), n.unit_no) in mentioned
             or (str(n.version_id), n.unit_no) in partly]
    classified = gathered.classified(n.key for n in nodes)
    prime_dates(ws, conn, [n.version_id for n in nodes])
    titles = _titles(ws, conn, [n.document_id for n in nodes])
    items = []
    for n in nodes:
        where = (str(n.version_id), n.unit_no)
        items.append(_coverage_item(ws, conn, n, titles, REASON_MENTIONED if where in mentioned else REASON_PARTLY_READ,
                                    mentioned.get(where, False)))
    return Classes(classified, items, mentioned)


def _no_possible() -> dict:
    return {"named": [], "count": 0, "read": 0, "items": []}


def _settle_class(group: dict) -> None:
    """Name and count the candidates of a class that still block (round 13 U8, KTD15): all of them, except a candidate whose
    mention the turn read and whose mention stands under another subject (``elsewhere``), which left the class and is counted
    apart as ``read``. ``named``: at most ``NAMED_CANDIDATES``, newest first (undated last). A group with no ``items`` (a
    record built by hand, or from before round 13) keeps its ``named`` and ``count``."""
    if "items" not in group:
        return
    items = group["items"]
    blocking = [i for i in items if not (i.get("read") and i.get("elsewhere"))]
    ordered = sorted(sorted(blocking, key=lambda i: (i["document_id"], i["unit_no"])), key=lambda i: i.get("date") or "",
                     reverse=True)  # stable: equal dates keep the key order, the undated ("") come last
    group["count"] = len(blocking)
    group["read"] = len(items) - len(blocking)
    group["named"] = [dict(i) for i in ordered[:NAMED_CANDIDATES]]


def coverage_record(ws: Workspace, conn: Connection, *, scope: str, via: str | None, units: list[InventoryUnit],
                    gathered: Gathered | None = None) -> dict:
    """The coverage record of one call (KTD7), fully materialized (strings, numbers, no foreign keys): the mode and the
    requested time; what it covers (``scope``: a gathered ``property``, one ``unit``, a relevance ``window``) and how the
    property was identified (``via``: the conversation's ``focus``, the call's named ``documents``, the ``question``'s own
    address or parcel); the property's label and link reasons; every inventory unit with its date, ``undated`` flag, search
    status, title, link reason and whether it keeps the answer from being complete; the unlinked candidates; and — round 13
    U8, R18 — the three classes of the rest of the office: ``possible`` (unidentified units that mention the property or were
    read in part: named, counted, blocking), ``unlinkable`` (their count) and ``excluded`` (the count of the units proven to
    be another property). ``complete`` and ``blocking`` come from ``assess_coverage``."""
    titles = _titles(ws, conn, {u.document_id for u in units})
    time = ws.time
    possible, elsewhere = _no_possible(), {}
    if gathered is not None:
        classes = classify_undetermined(ws, conn, gathered)
        gathered, elsewhere = classes.gathered, classes.elsewhere
        possible["items"] = classes.possible
    record = {
        "v": COVERAGE_V, "mode": time.mode, "requested": time.requested.as_dict() if time.requested else None,
        "scope": scope, "via": via, "identified": gathered is not None and gathered.identified and scope == PROPERTY,
        "property": ({"label": label_of(gathered.members), "reasons": list(dict.fromkeys(
            j.link.reason for j in gathered.joined)), "units": len(gathered.members)} if gathered is not None else None),
        "units": [{"document_id": str(u.document_id), "version_id": str(u.version_id), "unit_no": u.unit_no,
                   "title": u.title or titles.get(str(u.document_id)), "date": u.date[0] if u.date else None,
                   "precision": u.date[1] if u.date else None, "date_status": u.date_status, "undated": u.undated,
                   "status": u.status, "link": u.link, "focus": u.focus, "blocks": False} for u in units],
        "candidates": _candidates_of(ws, conn, gathered, elsewhere) if gathered is not None else _no_candidates(),
        "possible": possible,
        "unlinkable": len(gathered.unlinkable) if gathered is not None else 0,
        "excluded": len(gathered.excluded) if gathered is not None else 0,
    }
    return assess_coverage(time, record)


def assess_coverage(time: TimeMode, record: dict) -> dict:
    """Mark the units that keep a record from being complete, list why, and set ``complete`` (KTD7). Completeness is relative
    to the appraisals visible to the user. A record is never complete for a turn with no identified property (its coverage is
    the search window, or the one unit), when it merges calls of different scopes, or while an unlinked candidate or a
    possible candidate (an unidentified unit that mentions the property or was read in part, R18) remains. Unlinkable units
    and units proven to be another property block nothing.

    - **Recent:** complete only when the unit holding the newest datum found is the newest dated inventory unit (or ties
      it): a dated unit that comes before the best hit in the mode's order (``time_key``) and was not searched, or was
      searched without a match, is named (it may hold a newer datum phrased differently). Older units left unsearched do
      not matter (AE4). Round 13 U8 (R19, KTD16): once any unit is ``READ`` (a datum of the cited datum's base key came
      from it, or its section or table was read), the best is the newest ``READ`` unit, and a newer unit that is only
      ``LOCATED`` (a passage returned, nothing read) blocks with ``B_LOCATED`` — only a unit of the property (scope property
      or unit), never one of a window, whose units are whatever the search found, other properties among them; with no unit
      read, a located unit counts as checked, as in round 12.
    - **Explicit:** complete when every unit whose date can fall in or after the requested time (the first two classes of
      ``_inventory_key``) was searched.
    - **Undated units** (no stated date, or dates that disagree) always keep a record from being complete: their data
      cannot be called the latest or placed in the range.
    - **Reads only** (round 12 U12b, ``read_coverage``): a unit the answer's sources were read from (``READ``) counts as
      checked; a unit nothing searched or read (``NOT_SEARCHED``) keeps the record partial in both modes, whatever its date."""
    explicit = time.mode == "explicit" and time.requested is not None
    units = record["units"]

    def key_of(u: dict) -> tuple:
        return _inventory_key(time, None if u["date"] is None else (u["date"], u["precision"], None))

    best = min((key_of(u) for u in units if u["status"] in (LOCATED, READ) and not u["undated"]), default=None)
    best_read = min((key_of(u) for u in units if u["status"] == READ and not u["undated"]), default=None)
    if best_read is not None:
        best = best_read  # the datum was read: a newer unit that was only located is not checked (R19)
    blocking: list[str] = []
    for u in units:
        reason = None
        if u["undated"]:
            reason = B_UNDATED
        elif u["status"] == NOT_SEARCHED:  # the answer rests on reads: a unit nothing searched is a gap, whatever its age
            reason = B_NOT_SEARCHED
        elif explicit:
            if u["status"] == NOT_SEARCHED_CAP and key_of(u)[0] in (0, 1):
                reason = B_RANGE
        elif u["status"] not in (LOCATED, READ) and (best is None or key_of(u) < best):
            reason = B_NEWER if u["status"] == NOT_SEARCHED_CAP else B_NO_MATCH
        elif (u["status"] == LOCATED and best_read is not None and key_of(u) < best_read
              and record["scope"] != WINDOW):  # a window's units are not the property's: no property to be newer in
            reason = B_LOCATED
        u["blocks"] = reason is not None
        if reason is not None:
            blocking.append(reason)
    _settle_class(record["candidates"])
    if record["candidates"]["count"]:
        blocking.append(B_CANDIDATES)
    if record.get("possible") is not None:
        _settle_class(record["possible"])
        if record["possible"]["count"]:
            blocking.append(B_POSSIBLE)
    if record.get("undetermined"):  # a v1 record: every undetermined unit blocked
        blocking.append(B_UNDETERMINED)
    if record.get("later_undecided"):
        blocking.append(B_LATER_UNDECIDED)
    if record["scope"] == UNIT or (record["scope"] == PROPERTY and not record["identified"]):
        blocking.append(B_NOT_IDENTIFIED)
    if record["scope"] == WINDOW:
        blocking.append(B_WINDOW)
    record["blocking"] = list(dict.fromkeys(blocking))
    record["complete"] = bool(record["identified"]) and record["scope"] == PROPERTY and not record["blocking"]
    return record


def merge_coverage(time: TimeMode, records: list[dict]) -> dict | None:
    """The coverage of several calls of one turn (KTD7): for calls over the same gathered property a unit counts as
    searched only if every call searched it (``not_searched_cap`` if any did not), and has a match if any call matched; the
    completeness is assessed again over the merged statuses. Calls of different scopes or properties have no common
    coverage: the first record stands, marked ``several_scopes`` and never complete."""
    if not records:
        return None
    first = records[0]
    if len(records) == 1:
        return first
    refs = [(u["document_id"], u["unit_no"]) for u in first["units"]]
    same = all(r["scope"] == PROPERTY and first["scope"] == PROPERTY and
               [(u["document_id"], u["unit_no"]) for u in r["units"]] == refs for r in records)
    merged = json.loads(json.dumps(first))
    merged["calls"] = len(records)
    if not same:
        merged["complete"] = False
        merged["blocking"] = list(dict.fromkeys([*merged["blocking"], B_SEVERAL]))
        return merged
    for i, u in enumerate(merged["units"]):
        seen = [r["units"][i]["status"] for r in records]
        u["status"] = NOT_SEARCHED_CAP if NOT_SEARCHED_CAP in seen else LOCATED if LOCATED in seen else NO_MATCH
    return assess_coverage(time, merged)


def coverage_for(ws: Workspace, sids: Iterable[str] | None = None) -> dict | None:
    """The turn's coverage for the data the answer cites (KTD7): the merged record of the calls whose results include one of
    the cited ``sids``; of every call when none does, or when ``sids`` is None. None for a turn that made no time-ranked
    call (off mode, R14)."""
    calls = ws.time_calls
    if sids is not None:
        cited = set(sids)
        calls = [c for c in calls if cited & set(c.sids)] or calls
    return merge_coverage(ws.time, [c.record for c in calls if c.record is not None])


def opened_places(ws: Workspace) -> set[tuple[str, int | None]]:
    """(version id, unit number) of every place the turn read a section, a table or a page window of (round 13 U8, KTD16):
    the openings its tools recorded (``Workspace.touch`` with an opening). ``None`` for a place in no unit of a file of
    several: it names the version's only unit, if it has one."""
    out: set[tuple[str, int | None]] = set()
    for a in ws.activity.values():
        for o in a.get("openings") or []:
            src = ws.sources.get(o.get("sid"))
            if src is not None and src.version_id is not None:
                out |= {(str(src.version_id), n) for n in (src.contexts or [None])}
    return out


def read_places(ws: Workspace, ids: Iterable[str] = (), datum_places: Iterable[tuple[str, int | None]] = ()
                ) -> set[tuple[str, int | None]]:
    """Where the turn read the datum the answer is about (round 13 U8, KTD16): the places of the cited ids (a cited passage,
    value or measurement: ``_cited_places``), the places the turn registered a V# or M# of the cited datum's base key from
    (``datum_places``: the timeline's buckets know them; (version id, unit number)), and the sections and tables it opened."""
    out = {(v, n) for v, nums in _cited_places(ws, ids) for n in (nums or [None])}
    return out | {(str(v), n) for v, n in datum_places} | opened_places(ws)


def settle_coverage(ws: Workspace, record: dict | None, places: Iterable[tuple[str, int | None]],
                    later_undecided: Iterable[dict] = ()) -> dict | None:
    """The coverage record with what the turn read (round 13 U8, R19, KTD16, KTD15), on a copy: a unit the turn read from
    (``places``) is ``READ`` whatever it was before, so a newer unit left only ``LOCATED`` blocks (``B_LOCATED``); a possible
    candidate the turn read is marked read, and one whose mention stands under another subject leaves the candidate class
    (counted apart); ``later_undecided`` — the newer data of a class that could not be compared with the cited one (U4) —
    joins the record, which is then never complete. The record is assessed again. None stays None."""
    if record is None:
        return None
    out = json.loads(json.dumps(record))
    by_version: dict[str, list[dict]] = {}
    for u in out["units"]:
        by_version.setdefault(u["version_id"], []).append(u)
    for version, number in places:
        held = by_version.get(version, [])
        for u in held:
            if u["unit_no"] == number or (number is None and len(held) == 1):
                u["status"] = READ
    read = {(v, n) for v, n in places}
    for group in (out["candidates"], out.get("possible") or {}):
        for item in group.get("items") or []:
            if (item["version_id"], item["unit_no"]) in read or (item["version_id"], None) in read:
                item["read"] = True
    if later_undecided:
        out["later_undecided"] = list(later_undecided)
    return assess_coverage(ws.time, out)


def _register_call(ws: Workspace, call: TimeCall | None, sources: Iterable[Source] = ()) -> None:
    """Keep a finished call and its results on the workspace, and refresh the turn's coverage (``Workspace.time_coverage``,
    what the model is told): with the sections and tables the turn has opened so far, which count as read."""
    if call is None or call.record is None:
        return
    call.sids = [s.sid for s in sources]
    ws.time_calls.append(call)
    ws.time_coverage = settle_coverage(ws, coverage_for(ws), opened_places(ws))


def _cited_places(ws: Workspace, ids: Iterable[str]) -> list[tuple[str, list[int] | None]]:
    """(version id, the numbers of the units) of the places the cited ids were read from (round 12 U12b): a cited passage's
    own contexts, a value's or a measurement's unit; ``None`` for a place in no unit of a file of several (a version of one
    unit is that unit). A listing and an id that names no document are no place."""
    out: list[tuple[str, list[int] | None]] = []
    for i in dict.fromkeys(ids):
        if i in ws.values:
            v = ws.values[i]
            src = ws.sources.get(v.source_id)
            nums = [v.context["number"]] if v.context else (list(src.contexts) if src is not None else [])
            out.append((str(v.version_id), nums))
        elif i in ws.measurements:
            m = ws.measurements[i]
            out.append((str(m.version_id), [m.context["number"]] if m.context else []))
        elif i in ws.sources and not ws.sources[i].is_listing and ws.sources[i].version_id is not None:
            out.append((str(ws.sources[i].version_id), list(ws.sources[i].contexts)))
    return out


def read_coverage(ws: Workspace, ids: Iterable[str]) -> dict | None:
    """The coverage record of a timed turn that ran no time-ranked search (round 12 U12b, S3: the model answered from a
    source it read, e.g. a source of the turn before), or None. The property is the conversation's focus, else the one the
    cited documents name, else the one the question's address or parcel names (``unit_coverage`` and `tool_search` find it
    the same ways); its inventory is the same snapshot a search would list, but nothing was searched: a unit whose source the
    answer cites is ``READ``, every other unit ``NOT_SEARCHED``, so the record is partial (``B_NOT_SEARCHED``) unless every
    unit was read, and the note says only the read sources were checked (KTD7, KTD8, R12). None for a turn with no time
    mode (R14), for a turn that ran a time-ranked search (its calls' records stand), and for citations that are of no unit of
    an identified property. Memoized per turn, the cited places and the office's data version."""
    if ws.time.mode == "none" or ws.time_calls or ws.ctx is None:
        return None
    places = _cited_places(ws, ids)
    if not places:
        return None
    with tenant_tx(ws.ctx) as conn:
        key = ("read", tuple(sorted((v, tuple(n or ())) for v, n in places)), current_data_version(conn))
        if key not in ws.property_memo:
            ws.property_memo[key] = _read_coverage_uncached(ws, conn, places)
        return ws.property_memo[key]


def _read_coverage_uncached(ws: Workspace, conn: Connection, places: list[tuple[str, list[int] | None]]) -> dict | None:
    for gathered, via in _answer_properties(ws, conn, places):
        inventory = property_inventory(ws, conn, gathered)
        by_version: dict[str, list[InventoryUnit]] = {}
        for u in inventory:
            by_version.setdefault(str(u.version_id), []).append(u)
        read: set[tuple[str, int]] = set()
        for version, numbers in places:
            units = by_version.get(version, [])
            read |= {u.key for u in units if (numbers and u.unit_no in numbers) or (not numbers and len(units) == 1)}
        if not read:
            continue
        for u in inventory:
            u.status = READ if u.key in read else NOT_SEARCHED
        record = coverage_record(ws, conn, scope=PROPERTY, via=via, units=inventory, gathered=gathered)
        record["read_only"] = True
        return record
    return None


def _answer_properties(ws: Workspace, conn: Connection, places: list[tuple[str, list[int] | None]]):
    """The gathered properties a reads-only answer may be about, most specific first, each with how it was identified
    (``via``, as `coverage_record` says): the conversation's focus, the property the cited documents name, and the one the
    question names."""
    handle = (ws.unit_focus or {}).get("handle")
    if handle:
        try:
            v, _, number = _unit_of(conn, ws, handle)
        except ToolError:  # the focus unit's reading is no longer the current one
            gathered = None
        else:
            gathered = property_gathered(ws, conn, v.version_id, number)
        if gathered is not None and gathered.identified:
            yield gathered, "focus"
    cited = sorted({v for v, _ in places})
    docs = [UUID(str(r.document_id)) for r in conn.execute(
        text("SELECT DISTINCT document_id FROM document_versions WHERE id = ANY(CAST(:v AS uuid[]))"), {"v": cited})]
    if (named := _named_gathered(ws, conn, docs)) is not None:
        yield named, "documents"
    if (asked := _question_gathered(ws, conn)) is not None:
        yield asked, "question"


def unit_coverage(ws: Workspace, conn: Connection, handle: str, found: bool) -> dict:
    """The record of a call that searched one unit only (the unit search stood): the property's inventory of one unit when
    the unit is in the graph and states an identity (the property is appraised by no other unit, but its candidates and
    undetermined units still count); otherwise a ``unit`` record, which says the property was not identified (R13) and is
    never complete. ``found``: the search matched. The call names the unit in a turn with a time mode — the unit it works in,
    whether the conversation kept it or the first message's candidates offered it (round 15 U2, KTD10); the record's ``via``
    is ``focus``, as the widened search records."""
    v, cx, number = _unit_of(conn, ws, handle)
    gathered = property_gathered(ws, conn, v.version_id, number)
    if gathered is not None and gathered.identified:
        units = property_inventory(ws, conn, gathered)
        for u in units:
            u.status = LOCATED if found else NO_MATCH
        return coverage_record(ws, conn, scope=PROPERTY, via="focus", units=units, gathered=gathered)
    prime_dates(ws, conn, [v.version_id])
    date_, status = unit_dating(ws, conn, v.version_id, number)
    title = cx.segment(number).title if cx.multi else None
    unit = InventoryUnit(v.document_id, v.version_id, number, title, date_, status, None, True, LOCATED if found else NO_MATCH)
    return coverage_record(ws, conn, scope=UNIT, via="focus", units=[unit], gathered=None)


def window_coverage(ws: Workspace, conn: Connection, hits: list[dict]) -> dict:
    """The record of a time-ranked relevance window (no identified property: KTD7, "its coverage is the search window"): the
    (version, unit) groups the ranked hits are in, each searched with a match. Never complete."""
    prime_dates(ws, conn, [h["version_id"] for h in hits])
    blocks = chunk_blocks(ws, conn, [h["chunk_id"] for h in hits])
    units: dict[tuple, InventoryUnit] = {}
    for h in hits:
        start = blocks.get(h["chunk_id"], (None, None))[0]
        unit_no = valuation_of(ws, conn, h["version_id"], start).unit_no
        if unit_no is None or (str(h["document_id"]), unit_no) in units:
            continue
        date_, status = unit_dating(ws, conn, h["version_id"], unit_no)
        units[(str(h["document_id"]), unit_no)] = InventoryUnit(h["document_id"], h["version_id"], unit_no, h["title"],
                                                                date_, status, None, False, LOCATED)
    return coverage_record(ws, conn, scope=WINDOW, via=None, units=list(units.values()), gathered=None)


def _property_hits(ws: Workspace, conn: Connection, gathered: Gathered, query: str, limit: int,
                   call: TimeCall | None = None, via: str = "focus") -> tuple[list[dict], list[str], str | None]:
    """The time search over the units of one property (round 11 U14, U16; round 12 U7, R9-R11, KTD6): the inventory (the
    gathered set with the units' valuation dates, ordered by the turn's mode), the units it selects (the cap), and one
    search per selected unit inside its own blocks — a unit of a file of several never reads another property of that file,
    and a file of one unit is read whole. A unit counts as matched only when a hit holds the query's words (exactly or
    nearly), as in ``_unit_search``; the hits of matched units are merged by score and ranked per (version, unit) by the
    mode (``ranked_by_time``). The coverage record (what was matched, not matched and not searched) is set on ``call``.
    Returns (hits, notes for the model, the answer when nothing was found)."""
    inventory = property_inventory(ws, conn, gathered)
    selected = select_units(inventory)
    searched = {u.key for u in selected}
    hits: list[dict] = []
    for u in inventory:
        if u.key not in searched:
            u.status = NOT_SEARCHED_CAP
    version = current_data_version(conn) if selected else None
    for u in selected:
        scope = _unit_scope(ws, conn, u)
        found = memoized_search(ws, conn, query, limit, scope, version=version)
        u.status = LOCATED if any(h.get("lexical_support") for h in found) else NO_MATCH
        if u.status == LOCATED:
            hits += [h | {"scope_blocks": scope.blocks} for h in found] if scope.blocks else found
    if call is not None:
        call.record = coverage_record(ws, conn, scope=PROPERTY, via=via, units=inventory, gathered=gathered)
        for u in inventory:
            call.numbers.setdefault(str(u.version_id), []).append(u.unit_no)
            if u.link and not u.focus:
                call.links[(str(u.version_id), u.unit_no)] = link_phrase(u.link)
    if not hits:
        return [], [], MSG_PROPERTY_NOTHING.format(count=len(selected), query=_txt(query))
    hits = sorted(hits, key=lambda h: -h["score"])
    return ranked_by_time(ws, conn, hits, limit), [MSG_PROPERTY_SEARCHED, _units_line(ws, conn, inventory)], None


def _units_line(ws: Workspace, conn: Connection, inventory: list[InventoryUnit]) -> str:
    """The property line of a property-wide search (round 12 U12b, R3): the gathered units — the document's handle and title,
    the unit's valuation date, and why the server gathered it (the focus: that it is the focus) — in the inventory's order,
    so the model reads the link reasons with the hits and does not disown an appraisal at another house number."""
    titles = _titles(ws, conn, {u.document_id for u in inventory})
    shown = []
    for u in inventory[:PROPERTY_UNITS_SHOWN]:
        when = shown_period(u.date[0], u.date[1], None) if u.date else MSG_DATE_UNKNOWN
        why = MSG_LINK_FOCUS if u.focus else link_phrase(u.link) or ""
        shown.append(f"{ws.doc_handle(u.document_id)} «{_txt(titles.get(str(u.document_id)) or u.title or '')}» ({when})"
                     + (f" — {why}" if why else ""))
    rest = len(inventory) - len(shown)
    return MSG_PROPERTY_UNITS.format(count=len(inventory), units="; ".join(shown) + (f"; ועוד {rest}" if rest > 0 else ""))


def _property_search(ws: Workspace, conn: Connection, handle: str, query: str, limit: int,
                     call: TimeCall | None = None, document_ids: list[str] | None = None
                     ) -> tuple[list[dict], list[str], str | None] | None:
    """The time search of the property of the unit a call names (round 11 U14, R4, R11, R12, KTD6, KTD9; round 12 U6, KTD4,
    KTD5; U7; round 15 U2, R8, KTD10): when the turn has a time mode and the unit's property is appraised by other units too
    (its gathered set in the turn's property graph: linked by a shared parcel, or by the same street, number and city, each
    link with its reason), the search reads all of them instead of the unit alone — a question about 2022 asked in a
    conversation on the 2025 appraisal must read the 2022 one (``_property_hits``). The unit is the one the call names,
    whichever call of the turn comes first: it needs no kept focus. None: the unit is gathered with no other unit (it states
    no identity, or nothing proves another appraisal is its property) — the unit search stands.
    Returns (hits, notes for the model, the answer when nothing was found), as ``_unit_search`` does."""
    v, cx, number = _unit_of(conn, ws, handle)
    _same_file(ws, v, document_ids)
    gathered = property_gathered(ws, conn, v.version_id, number)
    if gathered is None or len(gathered.members) < 2:
        return None
    return _property_hits(ws, conn, gathered, query, limit, call)


def _named_property(ws: Workspace, conn: Connection, docs: list[UUID]) -> list[tuple] | None:
    """The units of the one property the documents of a call name (round 11 U16, R4, R11; round 12 U6, KTD5): the model
    passes ``document_ids`` (one appraisal of the property, from the answer before) where it passes no unit, so a time
    question must widen from the documents too. Every named document must be a visible, current appraisal whose subject
    units all state an identity and fall in ONE gathered set (a single-unit file, or the subject units of a file of several
    that are one property); a document or a unit with no identity, a document that is not an appraisal, or units of two
    properties (a compendium of two) leave the call as asked. None, too, when the property is appraised by no other unit.
    Returns the units of that gathered set."""
    gathered = _named_gathered(ws, conn, docs)
    return [(n.document_id, n.version_id, n.unit_no) for n in gathered.members] if gathered is not None else None


def _named_gathered(ws: Workspace, conn: Connection, docs: list[UUID]) -> Gathered | None:
    """The gathered set ``_named_property`` reads (round 12 U7: the inventory needs the set with its reasons, candidates and
    undetermined units, not only its units), once per turn and data version."""
    if not docs:
        return None
    version = current_data_version(conn)
    memo_key = ("named", tuple(sorted(map(str, docs))), version)  # the call's documents, once per turn and data version
    if memo_key not in ws.property_memo:
        ws.property_memo[memo_key] = _named_gathered_uncached(ws, conn, docs)
    return ws.property_memo[memo_key]


def _named_gathered_uncached(ws: Workspace, conn: Connection, docs: list[UUID]) -> Gathered | None:
    """``_named_gathered`` without its memo: the first of the named units, in key order, whose gathered set holds all of
    them (the closure depends on its seed, so every named unit is tried; the order makes the answer deterministic)."""
    graph = property_graph(ws, conn)
    wanted = {str(d) for d in docs}
    named = [n for n in graph.nodes if str(n.document_id) in wanted]
    if {str(n.document_id) for n in named} != wanted or not all(n.identity.identified for n in named):
        return None
    gathered = _covering_closure(graph, named, {n.key for n in named})
    return gathered if gathered is not None and len(gathered.members) >= 2 else None


def _covering_closure(graph: PropertyGraph, seeds: Iterable, keys: set) -> Gathered | None:
    """The gathered set of the first seed (in the given order) whose compatible closure holds every one of ``keys`` (the
    closure depends on its seed, so every seed is tried); None when no seed's set covers them."""
    for seed in seeds:
        gathered = graph.gather(seed.key)
        if keys <= gathered.keys:
            return gathered
    return None


def _question_gathered(ws: Workspace, conn: Connection) -> Gathered | None:
    """The property the current question names by an address or a parcel, when the entity lookup located no unit for it
    (round 12 U7, KTD6): the question's identifiers (``contexts.identifiers``) are matched against the identity facts of the
    turn's graph (``version_identities``: street core and number, block and parcel), and the matching units must all fall in
    ONE gathered set — the same relation rules as everywhere (a street and number in two cities are two properties). None
    when the question names no identifier, no unit matches, or the matches are of several properties. The graph is built
    only when the question names an identifier."""
    current = next((m["text"] for m in reversed(ws.user_messages) if m.get("current")), "")
    named = contexts.identifiers(current)
    if not named.addresses and not named.parcels:
        return None
    parcels = {(b, p) for b, p in named.parcels}
    graph = property_graph(ws, conn)
    matched = [n for n in graph.nodes if n.identity.addresses & named.addresses
               or parcels & {(b, p) for b, p, _ in n.identity.parcels}]
    return _covering_closure(graph, matched, {n.key for n in matched})


def _reach_contexts(cx, blocks: tuple[int, int]) -> set[int]:
    """The contexts a search over the block range ``blocks`` reaches: the units whose blocks lie wholly inside it (the unit
    searched and, across a probable edge, the neighbours up to the next certain one)."""
    first, last = blocks
    return {seg.number for seg in cx.segments if first <= seg.first and seg.last <= last}


def _clip_to_unit(conn: Connection, version_id, cx, kind: str, bs, be, body: str,
                  pages: list[int] | None) -> tuple[str, int | None, list[int] | None]:
    """A passage of a file of several units, cut at the last block of the unit it begins in (round 9 R10; round 15 U2,
    KTD10): a chunk that runs on into the next unit's title block is labelled with the unit it begins in, so its text is that
    unit's blocks only (the chunk's text without the blocks past the unit, else the unit's own blocks joined), its last block
    the unit's, and its pages the pages of the blocks left. A table chunk over several blocks is cut the same way (its table
    blocks count with the prose). Returns (text, last block, pages); unchanged for a table row (placed by
    ``_row_contexts``; ``_rows_in_scope`` cuts a stored table's rows), a passage inside its unit, a table inside one block and
    a file with one unit."""
    if not cx.multi or bs is None or be is None or kind == "table_row" or (kind == "table" and bs == be):
        return body, be, pages
    seg = cx.segment_at(bs)
    if seg is None or be <= seg.last:
        return body, be, pages
    rows = conn.execute(text(
        "SELECT block_index, kind, text, page FROM document_blocks WHERE version_id = :v AND block_index BETWEEN :a"
        " AND :b ORDER BY block_index"), {"v": version_id, "a": bs, "b": be}).all()
    inside = [r for r in rows if r.block_index <= seg.last]
    prose = ("heading", "paragraph", "textbox", *(("table",) if kind == "table" else ()))
    past = "\n".join(r.text for r in rows if r.block_index > seg.last and r.text and r.kind in prose)
    own = body[:-len(past)].rstrip("\n") if past and body.endswith(past) else "\n".join(
        r.text for r in inside if r.text and r.kind in prose)
    if not own.strip():
        return body, be, pages
    held = sorted({r.page for r in inside if r.page})
    return own, seg.last, [p for p in pages or [] if p in held] or held or pages


def _rows_in_scope(chunk: str, rows: list[dict], groups: list[dict], keep: set[int]) -> str | None:
    """The text of a stored table's chunk without the rows of contexts outside ``keep`` (round 15 U2, R8, KTD10: a table
    that runs on into another unit shows that unit's rows only when the search reaches it). ``rows``: the table's rows
    (``structure["rows"]``); ``groups``: its rows by context (``reader.table_contexts``, rows counted from 1). A chunk line
    is a row when it equals the row's cells joined by `` | ``, read in order; the other lines (caption, header, notes) stay.
    Returns the chunk unchanged when no row is outside, None when every row of it is, else the kept lines and
    ``MSG_TABLE_CUT``. A table stored without rows or contexts is not cut."""
    if not rows or not groups:
        return chunk
    context_of = {n: g["context"] for g in groups for n in range(g["first"], g["last"] + 1)}
    cells = [_row_line(r) for r in rows]
    lines, at, kept, tagged, dropped = chunk.split("\n"), 0, [], 0, 0
    for line in lines:
        found = next((i for i in range(at, len(cells)) if cells[i] == line), None)
        if found is None:
            kept.append(line)
            continue
        at, tagged = found + 1, tagged + 1
        if context_of.get(found + 1) in keep or found + 1 not in context_of:
            kept.append(line)
        else:
            dropped += 1
    if not dropped:
        return chunk
    if dropped == tagged:
        return None
    return "\n".join([*kept, MSG_TABLE_CUT])


def _scoped_terms(query: str) -> list[str]:
    """The topic terms of a query a unit-scoped hit is shown around (``_around_terms``): each once, two characters or more."""
    return [t for t in dict.fromkeys(topic_tokens(query)) if len(t) >= 2]


def _around_terms(passage: str, terms: list[str], width: int, by_line: bool = False) -> tuple[str, bool]:
    """At most ``width`` characters of ``passage`` around the query's terms (``_scoped_terms``; round 15 U2, R9): the window
    that holds the most of the topic terms, starting at the first of them it can hold, cut at a space (at a line for a table)
    and marked with an ellipsis where the passage goes on. A passage of ``width`` characters or less is returned as it is. A
    passage with none of the terms (a hit by meaning) shows its start. Returns (text, whether it was cut)."""
    if len(passage) <= width:
        return passage, False
    room = width - 2  # the two ellipses
    low = passage.lower()
    spots = sorted({(at, t) for t in terms for at in islice((m.start() for m in re.finditer(re.escape(t), low)), 60)})
    start, best = 0, 0
    inside: Counter[str] = Counter()  # the terms of the spots in the window [lo, lo + room), moved on as lo only grows
    left = right = 0
    for at, _ in spots:
        lo = max(0, at - room // 4)
        while right < len(spots) and spots[right][0] < lo + room:
            inside[spots[right][1]] += 1
            right += 1
        while left < right and spots[left][0] < lo:
            inside[spots[left][1]] -= 1
            if not inside[spots[left][1]]:
                del inside[spots[left][1]]
            left += 1
        if len(inside) > best:
            start, best = lo, len(inside)
    stops = "\n" if by_line else " \n"
    if start > 0:  # begin after a break, not inside a word
        cut = max((passage.rfind(c, max(0, start - 40), start + 1) for c in stops), default=-1)
        start = cut + 1 if cut >= 0 else start
    end = min(len(passage), start + room)
    if end < len(passage):
        cut = max((passage.rfind(c, start + room // 2, end) for c in stops), default=-1)
        end = cut if cut > start else end
    shown = passage[start:end].strip()
    return ("…" if start > 0 else "") + shown + ("…" if end < len(passage) else ""), True


def tool_search(ws: Workspace, query: str, document_ids: list[str] | None = None, limit: int | None = None,
                unit: str | None = None, pool: str | None = None) -> str:
    query = (query or "").strip()
    if len(query) < 2:
        raise ToolError("שאילתת חיפוש ריקה")
    limit = max(3, min(int(limit or SEARCH_LIMIT), SEARCH_MAX))
    ws.searches.append(query)
    notes: list[str] = []
    timed = ws.time.mode != "none"  # the turn has a time mode (the question's time, or the switch)
    call = TimeCall(query) if timed else None  # what this call checked (round 12 U7); off mode records nothing (R14)
    in_focus = False
    if not unit:
        # the conversation's unit, unless the call names the professional pool and the intent reads it as a primary pool
        # (a mixed or document question): the pool the model chose is not the unit's file, and the unit's search would
        # silently answer it from the appraisal (round 13 U14, KTD18). A call that names a unit or documents is as asked
        own_pool = pool == PROFESSIONAL_POOL and ws.pools.get(PROFESSIONAL_POOL) == PRIMARY and not document_ids
        unit = None if own_pool else focus_unit_for(ws, document_ids)
        in_focus = unit is not None
    elif timed and names_focus_unit(ws, unit, document_ids):
        in_focus = True  # the model names the focus unit it sees in the context: still the conversation's unit (U16)
    scoped = bool(unit)  # a call that names a unit (or the conversation's) reads inside it: compact hits (round 15 U2, R9)
    with tenant_tx(ws.ctx) as conn:
        if unit:
            # the unit the call names, in a turn with a time mode: its property's other appraisals are read too (U14). It
            # is the named unit that decides, not whether the conversation kept it: a first message's first call widens
            # exactly as a pinned one does (round 15 U2, R8, KTD10)
            widened = _property_search(ws, conn, unit, query, limit, call, document_ids) if timed else None
            hits, notes, nothing = widened or _unit_search(ws, conn, unit, query, limit, document_ids)
            if call is not None and widened is None:  # the unit search stood: the coverage is that of the unit (R13)
                call.record = unit_coverage(ws, conn, unit, bool(hits))
            if in_focus:
                focus_note = MSG_UNIT_FOCUS.format(unit=unit)
                if nothing:
                    _register_call(ws, call)
                    return f"{focus_note} {nothing}"
                notes.insert(0, focus_note)
            elif nothing:
                _register_call(ws, call)
                return nothing
            lookup_unread = False  # a unit names its document: no unread document is looked up by title
            hits = [h | {"pool": APPRAISAL_POOL, "role": PRIMARY} for h in hits]
        else:
            docs = _visible_documents(conn, document_ids or [])
            if ws.intent == "document" and ws.named_documents:  # a document question reads the named documents only
                named = [UUID(d) for d in ws.named_documents]
                kept = [d for d in docs if d in named]
                if docs and len(kept) != len(docs):
                    notes.append(MSG_POOL_CLIPPED)
                docs = kept or named
            passes, pool_notes, asked = _plan(ws, conn, pool, limit)
            notes += pool_notes
            hits = []
            absent = False
            # a time question naming the documents of one property reads every appraisal of it (U16); a document question
            # reads the named documents only
            # (and a question that names an address or a parcel no unit was located for reads the property its identity
            # facts name, round 12 U7)
            reads_property = timed and not (ws.intent == "document" and ws.named_documents)
            property_set, via = (_named_gathered(ws, conn, docs), "documents") if docs and reads_property else (None, None)
            if not docs and reads_property and any(q.pool == APPRAISAL_POOL and q.role == PRIMARY for q in passes):
                property_set, via = _question_gathered(ws, conn), "question"
            for p in passes:
                if p.pool == PROFESSIONAL_POOL and (ws.intent in (PROFESSIONAL, MIXED) or asked) \
                        and not has_professional(ws, conn):
                    ws.standards_absent = absent = True  # the office holds none: a stated gap, not a failed search (KTD15)
                    notes.append(MSG_NO_STANDARD_POOL)
                    if not any(q.pool == APPRAISAL_POOL for q in passes) and APPRAISAL_POOL in ws.pools:
                        # the call asked for the professional pool by name, and there is none: the office's other
                        # documents are read in its place (R29), in the role the intent gives them
                        passes.append(_Pass(APPRAISAL_POOL, ws.pools[APPRAISAL_POOL], limit))
                    continue
                # with no standard to answer from, the office's other documents are read in full, not as a few examples:
                # a glossary or a general explanation stored as an appraisal still explains a concept, labelled by its
                # kind (R29: until standards are uploaded the existing sources keep answering)
                n = limit if absent and p.pool == APPRAISAL_POOL else p.limit
                left_out = copies_left_out(ws, conn) if p.pool == PROFESSIONAL_POOL and not docs else ()
                scope = SearchScope(document_ids=tuple(docs), source_kinds=POOL_KINDS[p.pool], exclude_versions=left_out)
                wide_hits, wide_notes, wide_nothing = (
                    _property_hits(ws, conn, property_set, query, n, call, via)
                    if property_set is not None and p.pool == APPRAISAL_POOL and p.role == PRIMARY else (None, [], None))
                if wide_hits is not None and not wide_nothing:
                    found = wide_hits
                    notes += wide_notes
                elif timed and p.pool == APPRAISAL_POOL and p.role == PRIMARY:
                    # the appraisals that answer: the fused window, its units ordered by date, then cut (KTD6); the
                    # professional pool and the examples beside it are read as they always were (R16). A property the
                    # inventory found nothing in falls back to the window, whose coverage is then that of the window
                    found = ranked_by_time(ws, conn, memoized_search(ws, conn, query, n, scope, window=True), n)
                    if call is not None and (wide_hits is None or wide_nothing):
                        call.record = window_coverage(ws, conn, found)
                else:
                    found = memoized_search(ws, conn, query, n, scope)
                hits += [h | {"pool": p.pool, "role": p.role} for h in found]
            lookup_unread = not docs
        # a document the query names by its title that was not read has no passages: say so, never "not found" (KTD11)
        named = unread_named(conn, query, ws.unread_files(conn)) if lookup_unread else []
        ws.name_unread(named)
        extra = chunk_blocks(ws, conn, [h["chunk_id"] for h in hits])
        gaps = _reading_gaps(conn, list({h["version_id"] for h in hits}))
        whole = ReadingGaps(False)
        ws.note_readings(conn, {h["version_id"] for h in hits})
        sizes = _table_sizes(conn, {(h["version_id"], h.get("table_index")) for h in hits
                                    if h["kind"] in ("table", "table_row") and h.get("table_index") is not None})
        # a table chunk already holds its rows: a row hit of a returned table part adds nothing
        tables = [h for h in hits if h["kind"] == "table"]
        # matching rows the per-table cap left out: the handle of their table, to open it whole
        capped = [(_table_handle(ws, h["document_id"], h["version_id"], ws.readings.get(str(h["version_id"])),
                                 h["table_index"]), h["rows_not_shown"])
                  for h in hits if h.get("rows_not_shown") and h.get("table_index") is not None]
        out = []
        noted: dict[str, object] = {}  # the files holding several contexts the hits are in, not yet listed
        rendered = [h for h in hits if not (h["kind"] == "table_row" and any(
            t["version_id"] == h["version_id"] and t.get("table_index") == h.get("table_index")
            and h["text"].split(": ", 1)[-1][:40] in t["text"] for t in tables))]
        # the valuation dates of the appraisal passages are read together before their date lines are written
        prime_dates(ws, conn, [h["version_id"] for h in rendered if h["pool"] != PROFESSIONAL_POOL
                               and extra.get(h["chunk_id"], (None, None))[0] is not None])
        valuations = functools.partial(_hit_valuations, ws, conn, {})  # the property's dates, read when a hit needs them
        around = _scoped_terms(query) if scoped else []
        for h in rendered:
            bs, be = extra.get(h["chunk_id"], (None, None))
            if h["pool"] == PROFESSIONAL_POOL:
                src = _professional_hit(ws, conn, h, bs, be, sizes, gaps.get(h["version_id"], whole), h["role"], valuations)
                out.append(ws.once(src, ("chunk", h["chunk_id"])))
                ws.touch(h["document_id"], h["title"], "retrieved", gaps.get(h["version_id"], whole).partial)
                continue
            cx = contexts_of(ws, conn, h["version_id"])
            if cx.multi:
                noted[str(h["document_id"])] = cx
            # a passage that runs into the next unit is that of the unit it begins in (R10): cut there
            passage, be, page_list = _clip_to_unit(conn, h["version_id"], cx, h["kind"], bs, be, h["text"], h["page_list"])
            table = _row_contexts(ws, conn, cx, h["version_id"], h["table_index"]) if cx.multi and _is_table_hit(h) else None
            held = _hit_contexts(cx, h, bs, be, table) if cx.multi else []
            if cx.multi and h.get("scope_blocks") and h["kind"] in ("table", "table_row"):
                # a stored table's rows are placed by their own contexts: only those inside the search's reach are shown
                kept = _reach_contexts(cx, h["scope_blocks"])
                if h["kind"] == "table_row" and held and not set(held) & kept:
                    continue  # a row of the table in another unit is no hit of this scope
                if h["kind"] == "table" and table is not None:
                    groups, st = table
                    passage = _rows_in_scope(passage, st.get("rows") or [], groups, kept)
                    if passage is None:
                        continue
                held = [n for n in held if n in kept] or held
            stored = passage
            passage = _bound_passage(ws, conn, h, bs, be, _served_hit_text(ws, conn, h, passage))
            paragraphs, media, blocks = _block_info(conn, h["version_id"], bs, be,
                                                    with_text=h["kind"] not in ("table", "table_row"))
            tags = {"document": ws.doc_handle(h["document_id"])}
            if h.get("table_index") is not None:  # a matched row opens its whole table
                tags["table"] = _table_handle(ws, h["document_id"], h["version_id"],
                                              ws.readings.get(str(h["version_id"])), h["table_index"])
            # the notes of its table that govern the rows it holds and its text lacks (round 22 U5, KTD5): completed from the stored structure
            table_notes, notes_flag = _hit_notes(ws, conn, h, stored, tags.get("table", ""))
            # a hit of a unit-scoped search shows the passage around the query's terms, the rest on demand (round 15 U2, R9)
            shown, cut = (_around_terms(passage, around, SCOPED_PASSAGE_CHARS, h["kind"] in ("table", "table_row"))
                          if scoped else (_clip(passage, get_settings().chat_passage_chars), False))
            src = _mark(ws.add_source(
                document_id=h["document_id"], version_id=h["version_id"], title=h["title"], section=h["section"],
                location=_location(h["section"], h["kind"], page_list, bs, be, paragraphs, media),
                kind=h["kind"], text=_with_size(sizes.get((h["version_id"], h.get("table_index"))), shown),
                block_start=bs, block_end=be,
                table_index=h.get("table_index"), chunk_id=h["chunk_id"], page_list=page_list or None,
                # read in part only when the page or section it cites has an unread region (R28)
                partial_document=gaps.get(h["version_id"], whole).cites(page_list, h["section"]), tags=tags,
                row_index=h.get("row_index") if h["kind"] == "table_row" else None, table_notes=table_notes),
                cx, held, ws)
            if not h["section"]:
                src.heading = _heading_of(conn, h["version_id"], bs)
            if cut:  # what the model reads says where the rest is; what a claim is checked against is the passage shown
                src.body = f"{src.text}\n{MSG_PASSAGE_CUT.format(sid=src.sid)}"
            if notes_flag:  # the agent is told the table has notes (marks only); the claims are checked against ``text`` and the completed notes
                src.body = f"{src.body if src.body is not None else src.text}\n{notes_flag}"
            src.label = _label_of(src, cx, h["pool"], h["role"], ws.intent)
            uv = valuation_of(ws, conn, h["version_id"], bs)
            src.date_line = valuation_line(uv)
            if call is not None:  # a hit of a gathered appraisal other than the focus says why it is the property's (U12b)
                src.link = call.link_of(h["version_id"], uv.unit_no)
            out.append(ws.once(src, ("chunk", h["chunk_id"])))
            _register_shown(ws, src, h["kind"], passage, shown, blocks)
            # the document as a whole: a datum not found may be in a part of it that was not read
            ws.touch(h["document_id"], h["title"], "retrieved", gaps.get(h["version_id"], whole).partial)
    _register_call(ws, call, out)
    block = unread_text.tool_block(named)
    lead = "\n\n".join(notes)
    if not out:
        return (f'לא נמצאו קטעים עבור "{query}". אפשר לנסות ניסוח אחר, מונחים נרדפים או חיפוש בתוך מסמך מסוים.'
                + (f"\n\n{lead}" if lead else "") + (f"\n\n{block}" if block else ""))
    hints = [MSG_ROWS_NOT_SHOWN.format(handle=t, n=n) for t, n in capped] + ([block] if block else [])
    hints += [f"{ws.doc_handle(d)}: {note}" for d, cx in noted.items() if (note := _note_once(ws, d, cx))]
    return "\n\n".join([*notes, *_render_sources(out), *hints])


HEADING_CHARS = 160


def _heading_of(conn: Connection, version_id, block_index: int | None) -> str:
    """The heading path of a block (the lot or section it is under), as one breadcrumb, for a hit whose chunk carries no section
    (round 21 U8, KTD6): the chunk says where its text is, the document says what it is under. Empty when the block has none."""
    if block_index is None:
        return ""
    path = [p for p in reader.section_path_of(conn, version_id, block_index) if p]
    return _clip(" › ".join(path), HEADING_CHARS)


def _served_hit_text(ws: Workspace, conn: Connection, h: dict, passage: str) -> str:
    """The text of a search hit on a stored table (a table chunk or a row chunk) in the order the table is drawn, each cell with its
    column label, when the table was stored in reversed cell order (round 21 U8, KTD6): re-rendered now from the stored rows through
    the same function as a table read. The stored chunk text and its embedding are not touched; any other hit is returned as it is."""
    if not _is_table_hit(h):
        return passage
    st = _table_at(ws, conn, h["version_id"], h["table_index"])[0]
    if st is None:
        return passage
    if h["kind"] == "table_row":
        return stored_tables.serve_row_text(st, h.get("row_index"), passage)
    return stored_tables.serve_text(st, passage)


def _register_shown(ws: Workspace, src: Source, kind: str, passage: str, shown: str, blocks) -> None:
    """Record in ``ws.sent`` the blocks a search hit displayed whole, so a later read sends only the blocks the turn has not
    returned (KTD6 item 2). Only a hit of paragraph text whose passage was shown as it is — not cut by the scoped window or
    the passage cap, not a reference to an earlier hit (``same_as``) — and only the blocks whose full text is in what was
    shown. A table or table-row hit registers nothing: its text is rows and a caption, not the table block. ``blocks``: the
    blocks of the hit's range, as ``_block_info(with_text=True)`` read them (one window of them, as a read would send)."""
    if (kind in ("table", "table_row") or src.same_as or src.block_start is None or src.block_end is None
            or shown != passage):
        return
    flat = _flat(shown)
    registered = ws.sent.setdefault(str(src.version_id), {})
    for r in blocks[:reader.WINDOW_MAX]:
        if r.kind in ("heading", "paragraph", "textbox") and r.status == "read" and r.text and _flat(r.text) in flat:
            registered.setdefault(r.block_index, src.sid)


def _hit_valuations(ws: Workspace, conn: Connection, memo: dict) -> list:
    """``property_valuations`` for the professional hits of one search: read once per set of appraisal passages the turn
    holds, so a hit still sees the passages added before it in the loop and a hit after no new passage reuses the answer."""
    held = sum(1 for src in ws.sources.values() if _dated_appraisal(src))
    if memo.get("held") != held:
        memo["held"], memo["valuations"] = held, property_valuations(ws, conn)
    return memo["valuations"]


def _professional_hit(ws: Workspace, conn: Connection, h: dict, bs, be, sizes: dict, gaps: ReadingGaps,
                      role: str, valuations: Callable[[], list] | None = None) -> Source:
    """A hit of the professional pool (KTD13, R22): the passage under the identity of its standard — the number, the
    edition and the effective date it states, the section it is in — with the limits that bind its provision, and the
    call that reads the whole section. A document that states nothing is named by its own title. ``valuations``: the
    search's memo of the property's valuation dates (``_edition_note``)."""
    vid = h["version_id"]
    kind, std = standard_for(ws, conn, vid, bs)
    secs = sections_in(ws, conn, vid)
    sec = sections.innermost(secs, bs) if bs is not None else None
    body = _with_size(sizes.get((vid, h.get("table_index"))),
                      _clip(_served_hit_text(ws, conn, h, h["text"]), get_settings().chat_passage_chars))
    notes = []
    if sec is not None:
        handle = _psection_handle(ws, h["document_id"], vid, ws.readings.get(str(vid)), sec.section_no)
        where = " ".join(x for x in (section_phrase(std), f"«{_txt(sec.title)}»" if sec.title and sec.label else None) if x)
        notes.append(f"[{where}: read(section={handle}) קורא את הסעיף כולו, עם תתי-הסעיפים שלו]")
        notes += limit_lines(secs, sec)
    notes += _edition_note(ws, conn, vid, std, valuations)
    tags = {"document": ws.doc_handle(h["document_id"])}
    if h.get("table_index") is not None:
        tags["table"] = _table_handle(ws, h["document_id"], vid, ws.readings.get(str(vid)), h["table_index"])
    table_notes, notes_flag = _hit_notes(ws, conn, h, h["text"], tags.get("table", ""))  # round 22 U5
    if notes_flag:
        notes.append(notes_flag)
    src = ws.add_source(
        document_id=h["document_id"], version_id=vid, title=standard_title(std, h["title"]), section=section_string(std),
        location=standard_location(std, h["page_list"], h["kind"]), kind=h["kind"], text=body,
        body="\n".join([body, *notes]), block_start=bs, block_end=be, table_index=h.get("table_index"),
        chunk_id=h["chunk_id"], page_list=h["page_list"] or None, tags=tags,
        partial_document=gaps.cites(h["page_list"], h["section"]), source_kind=kind, standard=std,
        rules=section_rules(secs, bs, be),
        row_index=h.get("row_index") if h["kind"] == "table_row" else None, table_notes=table_notes)
    src.label = _label_of(src, None, PROFESSIONAL_POOL, role, ws.intent)
    return src


MSG_ROWS_NOT_SHOWN = ("בטבלה {handle} התאימו לחיפוש עוד {n} שורות שלא הוצגו כאן. לקריאת הטבלה כולה, עם הכותרות "
                      "וההערות: read עם table={handle}.")


MSG_STALE_REF = ("המקור {source_id} נקרא מקריאה קודמת של המסמך: המסמך עובד מחדש מאז, והמיקום שלו אינו תקף עוד. "
                 "לא נפתח דבר. כדי להסתמך על התוכן יש לחפש אותו מחדש במסמך (search או outline).")


def _ref(ws: Workspace, source_id: str) -> dict:
    sid = (source_id or "").strip()
    if sid in ws.sources:
        s = ws.sources[sid]
        return {"version_id": s.version_id, "block_start": s.block_start, "block_end": s.block_end,
                "table_index": s.table_index, "chunk_id": s.chunk_id, "reading_id": s.reading_id}
    if sid in ws.prior:
        return ws.prior[sid]
    raise ToolError(f"מזהה מקור לא מוכר: {source_id}. אפשר לפתוח רק מקורות שהוחזרו בתור הזה (S#) או הפניות מתורות קודמות (P#).")


MSG_UNAVAILABLE = "המקור אינו זמין (נמחק, או שאין הרשאה אליו)"
MSG_DOCUMENT_UNAVAILABLE = "המסמך אינו זמין (לא נמצא, נמחק, או שאין הרשאה אליו)"
MSG_STALE_HANDLE = ("{handle} נפתח מקריאה קודמת של המסמך: המסמך עובד מחדש מאז, והמיקום אינו תקף עוד. לא נפתח דבר. "
                    "אפשר לפתוח את מבנה המסמך מחדש (outline) או לחפש שוב.")
LOCATORS = ("source", "pages", "section", "table", "cursor")
NEIGHBORS = 3  # blocks on each side of a source
PAGES_MAX = 30  # pages in one page range
SECTIONS_LISTED = 8  # section handles listed with a page range
STATUS_LABELS = {"complete": "נקרא במלואו", "clipped": "נקרא חלק ממנו; יש המשך",
                 "has_unread_regions": "יש בו אזורים שלא נקראו", "uncertain_reading": "חלק ממנו נקרא בקריאה לא ודאית"}
_TABLE_SOURCES = {"emf": "טבלה מתוך תמונה וקטורית (נקראה במדויק)", "vision": "טבלה מתוך תמונה (קריאה חזותית)",
                  "ocr": "טבלה מתוך תמונה (OCR)"}


def _table_text(st: dict, rows: list, marks: dict[int, str] | None = None, before: Iterable[str] = (),
                after: Iterable[str] = ()) -> str:
    """A table as the model and the verifier read it: its caption and titles, its size, its headers, the given
    rows and its notes. ``marks``: a line to send before a row (by its place among ``rows``) — where the rows of
    another appraisal context start (KTD7); never part of what the verifier reads. ``before``/``after``: the lines of the
    context parts a table read adds above and below the table (``_table_context``, round 17 U4)."""
    # a table stored in reversed cell order is read in the order it is drawn, each cell with its column label (round 21 U8, KTD6): the
    # same text for the agent and for the verifier, which read ``Source.text``; the stored rows are not changed
    served = stored_tables.serve_structure(st)
    place = {id(r): i for i, r in enumerate(st.get("rows") or [])} if served is not st else {}
    labels = stored_tables.served_labels(served) if served is not st else None  # the same for every row of the table
    lines = [x for x in [st.get("caption"), *(st.get("title") or []), table_size_line(served)] if x]
    if any(served.get("headers") or []):
        lines.append(" | ".join(served["headers"]))
    for n, r in enumerate(rows):
        if marks and n in marks:
            lines.append(marks[n])
        if served is st:
            lines.append(" | ".join(r.get("cells") or []))
        elif id(r) in place:
            lines.append(stored_tables.render_served_row(served, place[id(r)], labels))
        else:
            lines.append(stored_tables.render_served_cells(served, list(r.get("cells") or []), labels))
    lines += st.get("notes") or []
    note = _TABLE_SOURCES.get(st.get("source") or "", "")
    return (note + "\n" if note else "") + "\n".join([*before, *lines, *after])


# --- the context of a table read (round 17 U4, KTD4; R9, R12, R13) ---------------------------------------------------
#
# A table's meaning is often stated beside it: the section it is in, the paragraph that introduces it, and a note after it
# that says what its values refer to (one valuation approach, a unit, a date). The extraction does not store those with the
# table, so a read of the table takes them from the blocks around it, by position and kind only: no phrase decides what a
# note is. Each part is a whole block (never cut, so it can be registered as sent), tagged with its kind and block number;
# the parts stop at a heading, a table or a picture, at the edge of the appraisal unit, and at a character cap.

TABLE_CONTEXT_BEFORE_CHARS = 700  # the paragraph above a table is a part only when it is at most this long
TABLE_CONTEXT_AFTER_CHARS = 1200  # the paragraphs after a table, together
TABLE_CONTEXT_SCAN = 12  # the blocks read after a table to find its parts and what follows
TABLE_FOLLOWS = 4  # the blocks after the parts that the "what follows" line names
TABLE_HEADING_SCAN = 4  # the blocks above a table searched for the paragraph before it and for the labels of its section
_BLOCK_KINDS = {"paragraph": "פסקה", "textbox": "תיבת טקסט", "heading": "כותרת", "table": "טבלה", "image": "תמונה"}
_UNREAD_REGION = "אזור שלא נקרא"
MSG_TABLE_PART = "[{side}{between} · {kind} · בלוק {block}] {text}"
MSG_TABLE_PART_SENT = "[{side}{between} · {kind} · בלוק {block}: כבר הוחזר ב-{sid}]"
MSG_TABLE_FOLLOWS = "[אחרי הטבלה: {kinds} — להמשך הקריאה מהקטע הבא: read(cursor={cursor})]"
MSG_TABLE_FOLLOWS_UNPLACED = "[אחרי הטבלה: {kinds}]"
MSG_TABLE_NOTHING_AFTER = "[אחרי הטבלה: אין עוד קטעים ביחידה]"
MSG_TABLE_AFTER_NOT_READ = "[אחרי הטבלה: הבלוקים שאחריה לא נקראו כאן]"  # the scan stopped before the unit's end: unknown, not empty
# the status of a table read speaks of the table: it says nothing of the section the table is in (KTD6 item 1)
TABLE_STATUS_LABELS = {"complete": "הטבלה נקראה במלואה (כל שורותיה, כותרותיה והערותיה); הסעיף שהיא בו לא נקרא בקריאה הזו",
                       "clipped": "נקרא חלק מהטבלה; יש המשך", "uncertain_reading": "הטבלה נקראה בקריאה לא ודאית"}


class TablePart(NamedTuple):
    """A block beside a table that its read carries: ``side`` is ``before`` or ``after``; ``between``: a paragraph that
    lies between two tables, which belongs to neither one's own text more than to the other's.

    The evidence that attributes it to the table (round 18 U4, KTD5), recorded when the table is read: ``page`` (the block's page),
    ``gap`` (the blocks that stand between it and the table, other than the table's own), ``page_gap`` (the pages between it and
    the table's nearest edge: 0 the same page, 1 the next; None when a page is unknown), ``reference`` (what its wording points to:
    ``back``, ``forward``, ``both`` or empty, by ``reference_of``; None when it was not read from a table), and ``heading_like``
    (a short label the extractor stored as a paragraph, which is part of the heading scope of the table below it)."""

    side: str
    kind: str
    block_index: int
    text: str
    between: bool
    page: int | None = None
    gap: int = 0
    page_gap: int | None = None
    reference: str | None = None
    heading_like: bool = False

    @property
    def reasons(self) -> tuple[str, ...]:
        """Why the part is NOT attributed to the table: ``between`` (it lies between two tables), ``not_adjacent`` (another block
        stands between it and the table, or its page is not the table's or the one next to it) and ``no_reference`` (its wording does
        not point to the table from where it stands: a paragraph after the table points back, one before it points forward). A
        paragraph between two tables is attributed all the same when it is adjacent and points to this one. Empty: attributed. A part
        made without evidence (``reference`` None) has only ``between``. A heading-like label is the heading scope: always attributed."""
        if self.heading_like:
            return ()
        if self.reference is None:
            return ("between",) if self.between else ()
        far = self.gap > 0 or (self.page_gap is not None and self.page_gap > 1)
        expected = "back" if self.side == "after" else "forward"
        silent = self.reference not in (expected, "both")
        if not (far or silent):
            return ()
        return tuple(r for r, on in (("between", self.between), ("not_adjacent", far), ("no_reference", silent)) if on)

    @property
    def attributed(self) -> bool:
        return not self.reasons


# the words with which a paragraph points to a table beside it: a closed set, used only together with a table word and with the
# paragraph's place beside the table (``TablePart.reasons``), never alone (round 18 U4, KTD5)
_TABLE_WORD = re.compile(r"(?<![א-ת])[הבלמכש]{0,2}טבל(?:ה|ת|אות)(?![א-ת])")
_POINTS_BACK = re.compile(r"(?<![א-ת])(?:ש?לעיל|הנ[\"״׳']ל|האמור(?:ה|ות))(?![א-ת])")
_POINTS_FORWARD = re.compile(r"(?<![א-ת])(?:ש?להלן|הבא(?:ה|ות)|כדלקמן)(?![א-ת])")
_LABEL_NUMBER = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?|[א-תA-Za-z][.)])\s+\S")
TABLE_LABEL_WORDS = 12  # a label the extractor stored as a paragraph has at most this many words, and
TABLE_LABEL_SHORT = 6  # ... without a number in front of it at most these


def reference_of(text: str) -> str:
    """What a paragraph's wording points to: ``back`` (a table with a word like «לעיל»), ``forward`` («להלן», «הבאה»), ``both`` or
    empty. A reference word counts only beside a word for a table."""
    flat = _flat(text)
    if not _TABLE_WORD.search(flat):
        return ""
    back, forward = bool(_POINTS_BACK.search(flat)), bool(_POINTS_FORWARD.search(flat))
    return "both" if back and forward else "back" if back else "forward" if forward else ""


def heading_like(text: str) -> bool:
    """A short label that stands alone, which an extraction that took it for a paragraph left out of the heading path: a number and a
    few words (``4. גישת היוון``), or at most ``TABLE_LABEL_SHORT`` words, with a letter and no sentence or colon at its end."""
    flat = _flat(text)
    words = len(flat.split())
    if not flat or words > TABLE_LABEL_WORDS or flat[-1] in ".!?:;,…" or not re.search(r"[א-תA-Za-z]", flat):
        return False
    return words <= TABLE_LABEL_SHORT or bool(_LABEL_NUMBER.match(flat))


@dataclass
class TableContext:
    """What surrounds a table: its heading path, its parts, the blocks that follow the parts (kind, block) and where they
    can be read on from (a section target and position), and the blocks of the table itself. ``ended``: the scan after the table
    reached the end of its unit (or of the document), so an empty ``follows`` means that nothing follows; else the blocks after
    the scanned window were not read."""

    path: tuple[str, ...] = ()
    parts: list[TablePart] = field(default_factory=list)
    follows: list[tuple[str, int]] = field(default_factory=list)
    resume: tuple[tuple, tuple[int, int]] | None = None
    table_blocks: list[int] = field(default_factory=list)
    ended: bool = False


def _flat(s: str | None) -> str:
    return " ".join((s or "").split())


def _is_prose(r) -> bool:
    """A block a table part can be: a paragraph of text that was read (not a heading, a table or a picture)."""
    return (r.kind not in ("heading", "table", "image") and bool((r.text or "").strip())
            and r.status not in (reader.UNREAD, reader.UNCERTAIN, "decorative", "no_text"))


def _kind_of(r) -> str:
    return _UNREAD_REGION if r.status == reader.UNREAD else _BLOCK_KINDS.get(r.kind, "קטע")


def _page_gap(page: int | None, edge: int | None, *, after: bool) -> int | None:
    """The pages between a block and the nearest edge of its table (0: the same page), or None when a page is unknown."""
    if page is None or edge is None:
        return None
    return max(0, page - edge if after else edge - page)


def _table_context(ws: Workspace, conn: Connection, v: reader.Version, table_index: int, st: dict, block, cx
                   ) -> TableContext:
    """The context of a table (``TableContext``) from the blocks around its first block ``block``, within its appraisal unit:
    the paragraph just above it when that is not its caption, and the paragraphs just after it up to the next heading, table
    or picture, within ``TABLE_CONTEXT_AFTER_CHARS`` (a paragraph that does not fit ends the parts: none is cut). A paragraph
    between two tables is tagged as such. Nothing is taken from another unit. The blocks after the parts are named, with
    the position that reads on from the first of them. Each part records the evidence that attributes it to the table
    (``TablePart``: the blocks between, the page, the wording); the short labels of the table's own section that stand above it
    as paragraphs (``heading_like``) are parts of its heading scope (round 18 U4, KTD5)."""
    if block is None:
        return TableContext()
    first = block.block_index
    seg = cx.segment_at(first) if cx.multi else None

    def inside(index: int) -> bool:
        return not cx.multi or (seg is not None and seg.first <= index <= seg.last)

    ctx = TableContext(path=reader.effective_path(cx, first, tuple(block.section_path or ())))
    captions = {_flat(x) for x in [st.get("caption"), *(st.get("title") or []), block.text, *ctx.path] if x}
    above = reader.blocks_above(conn, v.version_id, first, lo=seg.first if seg is not None else 0,
                                limit=TABLE_HEADING_SCAN)
    near = above[-1] if above else None
    section = tuple(block.section_path or ())
    labels = []  # the labels of the table's own section above it, nearest first, up to the first heading, table or picture
    for r in reversed(above):
        if r.status == "decorative":
            continue
        if not _is_prose(r):
            break
        if tuple(r.section_path or ()) == section and heading_like(r.text) and _flat(r.text) not in captions:
            labels.append(r)

    def before_part(r, *, label: bool) -> TablePart:
        text_ = r.text.strip()
        gap = sum(1 for x in above if x.block_index > r.block_index and x.status != "decorative")
        return TablePart("before", r.kind, r.block_index, text_,
                         len(above) >= 2 and above[-2].kind == "table" and r is near, r.page, gap,
                         _page_gap(r.page, block.page, after=False), reference_of(text_), label)

    ctx.parts += [before_part(r, label=True) for r in reversed(labels)]
    if near is not None and _is_prose(near) and _flat(near.text) not in captions \
            and len(near.text) <= TABLE_CONTEXT_BEFORE_CHARS and not any(r is near for r in labels):
        ctx.parts.append(before_part(near, label=False))
    rows = reader.blocks_between(conn, v.version_id, first, first + TABLE_CONTEXT_SCAN, limit=TABLE_CONTEXT_SCAN + 1)
    lead = 0
    while lead < len(rows) and rows[lead].table_index == table_index:  # the table's own blocks come first
        ctx.table_blocks.append(rows[lead].block_index)
        lead += 1
    last_page = rows[lead - 1].page if lead else block.page  # the page the table ends on
    run, end = [], None  # the paragraphs after the table, and the block that ends them
    # the window was not filled, or no block (of this unit) lies beyond it: nothing was left unscanned
    ctx.ended = bool(rows) and (rows[-1].block_index < first + TABLE_CONTEXT_SCAN or not any(
        inside(r.block_index) for r in reader.blocks_between(conn, v.version_id, rows[-1].block_index + 1,
                                                             rows[-1].block_index + 1, limit=1)))
    for r in rows[lead:]:
        if not inside(r.block_index):
            ctx.ended = True  # a block of another unit follows: this unit ended
            break
        if r.status == "decorative":
            continue
        if _is_prose(r):
            run.append(r)
            continue
        end = r
        break
    between = end is not None and end.kind == "table"
    used, shown = 0, []
    for r in run:
        if used + len(r.text.strip()) > TABLE_CONTEXT_AFTER_CHARS:
            break
        used += len(r.text.strip())
        shown.append(r)
    ctx.parts += [TablePart("after", r.kind, r.block_index, r.text.strip(), between, r.page, gap,
                            _page_gap(r.page, last_page, after=True), reference_of(r.text))
                  for gap, r in enumerate(shown)]  # the blocks between a paragraph and the table: those of the run before it
    last = shown[-1].block_index if shown else (ctx.table_blocks[-1] if ctx.table_blocks else first)
    tail = [r for r in rows[lead:] if r.block_index > last and inside(r.block_index) and r.status != "decorative"]
    ctx.follows = [(_kind_of(r), r.block_index) for r in tail[:TABLE_FOLLOWS]]
    if tail:
        path = reader.effective_path(cx, tail[0].block_index, tuple(tail[0].section_path or ()))
        ctx.resume = (_section_target(v, path, reader.section_window(cx, tail[0].block_index, path)),
                      (tail[0].block_index, 0))
    return ctx


def _part_line(p: TablePart, sent: dict[int, str] | None = None) -> str:
    """A part as one line, with its kind and block reference; as a pointer to the source that returned it when ``sent``
    (the blocks the turn returned whole) holds it. The tag carries the marks of why the part is not attributed to the table
    (``TablePart.reasons``), or the mark of a heading-like label."""
    marks = [TABLE_HEADING_LIKE] if p.heading_like else [PART_REASON_MARKS[r] for r in p.reasons]
    fields = {"side": TABLE_BEFORE if p.side == "before" else TABLE_AFTER, "between": "".join(f" · {m}" for m in marks),
              "kind": _BLOCK_KINDS.get(p.kind, "קטע"), "block": p.block_index}
    if sent is not None and p.block_index in sent:
        return MSG_TABLE_PART_SENT.format(sid=sent[p.block_index], **fields)
    return MSG_TABLE_PART.format(text=_flat(p.text), **fields)


def _table_context_lines(ctx: TableContext, sent: dict[int, str] | None = None, *, path: bool = True
                   ) -> tuple[list[str], list[str]]:
    """The lines a table read puts above and below the table: the heading path (unless ``path`` is False) and the parts
    before it; the parts after it. ``sent``: the blocks already returned, which become pointers (what the model reads); the
    text the verifier reads is built without it."""
    head = [f"{TABLE_PATH_PREFIX} {' › '.join(ctx.path)}"] if path and ctx.path else []
    return (head + [_part_line(p, sent) for p in ctx.parts if p.side == "before"],
            [_part_line(p, sent) for p in ctx.parts if p.side == "after"])


def _follows_line(ws: Workspace, v: reader.Version, ctx: TableContext) -> str:
    """The "what follows" pointer of a table read: the kinds and blocks after its parts, and the cursor that reads on."""
    if not ctx.follows:
        return MSG_TABLE_NOTHING_AFTER if ctx.ended else MSG_TABLE_AFTER_NOT_READ
    kinds = ", ".join(f"{kind} (בלוק {index})" for kind, index in ctx.follows)
    if ctx.resume is None:
        return MSG_TABLE_FOLLOWS_UNPLACED.format(kinds=kinds)
    return MSG_TABLE_FOLLOWS.format(kinds=kinds, cursor=_cursor(ws, v, *ctx.resume))


def read_scope(ws: Workspace, source_id: str, scope: str = "neighbors", ref: dict | None = None) -> Source:
    """The context around a source (``neighbors``), its whole top-level section, or its whole table, read for the
    server's own checks (``app.chat.meaning``) through the same reader as ``read``: the source is not registered
    and the turn's coverage does not change — the caller registers it (``Workspace.adopt``) only if it cites it."""
    if scope not in READ_SCOPES:
        raise ToolError("scope חייב להיות neighbors, section או table")
    ref = ref or _ref(ws, source_id)
    vid = UUID(str(ref["version_id"]))
    with tenant_tx(ws.ctx) as conn:
        head = reader.version(conn, vid)
        if head is None:
            raise ToolError(MSG_UNAVAILABLE)
        if "reading_id" in ref and ref["reading_id"] != head.reading_id:
            # block numbers of an earlier reading mean other text in the current one: nothing is opened
            raise ToolError(MSG_STALE_REF.format(source_id=source_id))
        ws.readings.setdefault(str(vid), head.reading_id)

        def make(**kw) -> Source:
            return Source(sid="", document_id=head.document_id, version_id=vid, title=head.title,
                          partial_document=head.partial, reading_id=head.reading_id, **kw)

        start, end = ref.get("block_start"), ref.get("block_end")
        table_index = ref.get("table_index")
        if start is None and ref.get("chunk_id") and not (scope == "table" and table_index is not None):
            row = conn.execute(text("SELECT text, section, page_list, kind, table_index, row_index FROM chunks"
                                    " WHERE id = :c AND version_id = :v"), {"c": ref["chunk_id"], "v": vid}).first()
            if row is None:
                raise ToolError(MSG_UNAVAILABLE)
            shown = _served_hit_text(ws, conn, {"version_id": vid, "kind": row.kind, "table_index": row.table_index,
                                                "row_index": row.row_index}, row.text)
            return make(section=row.section, kind=row.kind, text=_clip(shown, CONTEXT_CHARS[scope]),
                        location=_location(row.section, row.kind, row.page_list, None, None, (None, None), None),
                        page_list=row.page_list)
        if scope == "table":
            if table_index is None and start is not None:
                table_index = next((r.table_index for r in reader.blocks_between(
                    conn, vid, start, end if end is not None else start) if r.table_index is not None), None)
            st = reader.table_structure(conn, vid, table_index) if table_index is not None else None
            if st is None:
                raise ToolError("למקור הזה אין טבלה")
            return make(section=st.get("section"), kind="table",
                        location=_location(st.get("section"), "table", None, None, None, (None, None), st.get("media")),
                        text=_clip(_table_text(st, st.get("rows") or []), CONTEXT_CHARS["table"]),
                        block_start=st.get("block_index"), block_end=st.get("block_index"), table_index=table_index)
        if start is None:
            raise ToolError("למקור הזה אין מיקום במסמך להרחבה")
        top = None
        if scope == "section":  # the top-level section, within the appraisal context of the source (KTD7)
            cx = contexts_of(ws, conn, vid, head.reading_id)
            top = reader.effective_path(cx, start, reader.section_path_of(conn, vid, start))[:1]
            rows = reader.blocks_in_section(conn, vid, top, window=reader.section_window(cx, start, top))
        else:
            rows = reader.blocks_between(conn, vid, max(0, start - NEIGHBORS),
                                         (end if end is not None else start) + NEIGHBORS)
        rows = [r for r in _served_rows(ws, conn, vid, rows) if r.text]
        if not rows:
            raise ToolError("אין טקסט בהקשר הזה")
        nums = [r.paragraph_no for r in rows if r.paragraph_no]
        section = rows[0].section if scope == "neighbors" else ((top[0] if top else None) or rows[0].section)
        return make(section=section, kind="context", text=_clip("\n".join(r.text for r in rows), CONTEXT_CHARS[scope]),
                    location=_location(section, "text", None, rows[0].block_index, rows[-1].block_index,
                                       (min(nums), max(nums)) if nums else (None, None), None),
                    block_start=rows[0].block_index, block_end=rows[-1].block_index)


# --- read: one locator, a bounded part, a stated completeness ----------------------------------------------------

def _table_handle(ws: Workspace, document_id, version_id, reading_id, table_index: int) -> str:
    return ws.handle("T", (str(version_id), table_index), document_id=str(document_id), version_id=str(version_id),
                     reading_id=reading_id, table_index=table_index)


def _section_handle(ws: Workspace, v: reader.Version, path: tuple, window: tuple[int, int] | None = None) -> str:
    """The handle of a section; in a file holding several appraisal contexts, of the section within one context
    (``window``: the blocks it is read within, ``reader.section_window``)."""
    if window is None:
        return ws.handle("§", (str(v.version_id), tuple(path)), document_id=str(v.document_id),
                         version_id=str(v.version_id), reading_id=v.reading_id, path=tuple(path))
    return ws.handle("§", (str(v.version_id), tuple(path), *window), document_id=str(v.document_id),
                     version_id=str(v.version_id), reading_id=v.reading_id, path=tuple(path), window=tuple(window))


def _section_name(path: tuple, later: bool = False) -> str:
    if path:
        return path[-1]
    return "תחילת ההקשר (לפני הכותרת הראשונה שלו)" if later else "תחילת המסמך (לפני הכותרת הראשונה)"


def _later_context(cx, window: tuple[int, int] | None) -> bool:
    """Whether a section's window is in a context after the file's first (its title part is then the context's)."""
    return window is not None and cx is not None and cx.multi and cx.segment_at(window[0]) is not cx.segments[0]


def _sections_of(cx, items: list) -> dict[tuple, None]:
    """The sections the blocks of a part are in — each prefix of a block's section path, within its context — as
    (path, window), in reading order."""
    paths: dict[tuple, None] = {}
    for r, _, _ in items:
        path = reader.effective_path(cx, r.block_index, r.section_path)
        for i in range(1, len(path) + 1):
            paths.setdefault((path[:i], reader.section_window(cx, r.block_index, path[:i])), None)
    return paths


def _section_target(v: reader.Version, path: tuple, window: tuple[int, int] | None) -> tuple:
    return ("section", str(v.version_id), tuple(path)) + (tuple(window) if window is not None else ())


def _pages_label(pages: list[int]) -> str:
    if not pages:
        return "המסמך"
    return f"עמוד {pages[0]}" if pages[0] == pages[-1] else f"עמודים {pages[0]}–{pages[-1]}"


def _handle(ws: Workspace, value: str, kind: str) -> dict:
    data = ws.handles.get(value)
    if data is None or data["kind"] != kind:
        what = {"§": "סעיף (§# מ-outline)", "T": "טבלה (T# מ-outline או מתוצאת חיפוש)", "K": "המשך (K# מ-more)",
                "U": "יחידה (U# מ-find_documents, outline או תוצאת חיפוש)"}[kind]
        raise ToolError(f"מזהה לא מוכר: {value}. נדרש מזהה {what} שהוחזר בתור הזה.")
    return data


def _bound(conn: Connection, ws: Workspace, handle: str, data: dict) -> reader.Version:
    """The version a handle was issued for, under the current permissions and only while its reading is current."""
    v = reader.version(conn, UUID(data["version_id"]))
    if v is None:
        raise ToolError(MSG_UNAVAILABLE)
    if v.reading_id != data["reading_id"]:
        raise ToolError(MSG_STALE_HANDLE.format(handle=handle))
    return v


def _document(conn: Connection, ws: Workspace, ref) -> reader.Version:
    """The current version of a document named by its D# or its id, under the current permissions. A document
    the user may not see is not available, with nothing about it (not its title, not its size)."""
    ref = str(ref or "").strip()
    data = ws.handles.get(ref)
    if data is not None and data["kind"] != "D":
        raise ToolError(f"{ref} אינו מסמך: מסמך נבחר ב-D# (מ-list_documents, find_documents או search) או ב-document_id")
    try:
        document_id = UUID(data["document_id"] if data is not None else ref)
    except ValueError:
        raise ToolError(f"מזהה מסמך לא מוכר: {ref}") from None
    v = reader.current_version(conn, document_id)
    if v is None:
        raise ToolError(MSG_DOCUMENT_UNAVAILABLE)
    ws.readings.setdefault(str(v.version_id), v.reading_id)
    return v


def _target_json(target: tuple, pos) -> dict | None:
    """A read target and a position in it as JSON: where a clipped source continues, kept with an answer's
    sources so a later turn's P# can continue from there."""
    if pos is None or target[0] not in ("section", "pages", "table"):
        return None
    return {"target": [target[0], target[1], *[list(x) if isinstance(x, tuple) else x for x in target[2:]]],
            "pos": list(pos) if isinstance(pos, tuple) else pos}


def _target_from_json(resume, version_id: str) -> tuple[tuple, object] | None:
    """The target and position of ``_target_json`` back, when they are well formed and of ``version_id``."""
    try:
        t, pos = resume["target"], resume["pos"]
        if str(t[1]) != version_id:
            return None
        if t[0] == "section":  # a section of one appraisal context also names its block window
            window = (int(t[3]), int(t[4])) if len(t) >= 5 else ()
            return ("section", version_id, tuple(str(x) for x in t[2])) + window, (int(pos[0]), int(pos[1]))
        if t[0] == "pages":
            return ("pages", version_id, int(t[2]), int(t[3])), (int(pos[0]), int(pos[1]))
        if t[0] == "table":
            return ("table", version_id, int(t[2])), int(pos)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return None


def _cursor(ws: Workspace, v: reader.Version, target: tuple, pos, within: tuple[int, int] | None = None) -> str:
    """The handle that reads on from ``pos``; ``within``: the blocks (first, last) of the unit the read is scoped to,
    which its continuation keeps."""
    return ws.handle("K", (target, pos, within), document_id=str(v.document_id), version_id=str(v.version_id),
                     reading_id=v.reading_id, target=target, pos=pos, within=within)


def _left_after(rows: list, part: reader.Part) -> dict | None:
    """What a window had left after ``part`` (round 23 U0, diagnostics only): the blocks from the one the next part starts in, and their
    characters (the next part's own offset into its first block left out). None when the part ends the window."""
    if part.next is None:
        return None
    block, offset = part.next
    rest = [r for r in rows if r.block_index >= block]
    return {"blocks": len(rest), "chars": max(0, sum(len(r.text or "") for r in rest) - offset)}


def _emit(ws: Workspace, v: reader.Version, target: tuple, pos, part: reader.Part, *, name: str, scope: str,
          location: str, section: str | None, lines: list[str], read: bool, more: str | None = None,
          cut: bool = False, unread_pages: list[int] | None = None, cx=None,
          within: tuple[int, int] | None = None, standard: tuple[str, dict] | None = None,
          rules: dict | None = None, left: dict | None = None) -> Source:
    """A part of a window as a source of the turn. Its ``text`` is the part's full text (what the verifier
    checks); what is sent leaves out blocks the turn already returned whole, as a pointer to the source that
    returned them, and marks unread and uncertain regions where they are in reading order with their R#.
    ``standard``: (kind, identity) of a part of a professional document (``standard_for``), which names the source;
    ``rules``: the limits and wording of the sections it is in (``section_rules``), for the verifier."""
    vid = str(v.version_id)
    sent = ws.sent.setdefault(vid, {})
    content, body, regions, run = [], [], [], []
    unread, uncertain = bool(unread_pages), False
    # where a later appraisal context starts inside the part (KTD7): marked in place, in what is sent only
    starts = {seg.first: seg for seg in cx.segments[1:]} if cx is not None and cx.multi else {}
    first_index = part.items[0][0].block_index if part.items else None

    def flush() -> None:
        if run:
            body.append(f"[{len(run)} קטעים שכבר הוחזרו בתור הזה ב-{', '.join(dict.fromkeys(run))}: לא נשלחים שוב]")
            run.clear()

    for r, piece, whole in part.items:
        if r.block_index in starts and r.block_index != first_index:
            flush()
            body.append(MSG_CONTEXT_STARTS.format(which=cx.describe(starts[r.block_index].number)))
        if r.status == "decorative":
            continue
        region = None
        if r.status in (reader.UNREAD, reader.UNCERTAIN):
            region = ws.handle("R", (vid, r.block_index), document_id=str(v.document_id), version_id=vid,
                               reading_id=v.reading_id, block_index=r.block_index, page=r.page, bbox=r.bbox,
                               status=r.status, note=r.note)
            regions.append(region)
        if r.status == reader.UNREAD:
            unread = True
            flush()
            where = f"עמוד {r.page}, " if r.page else ""
            body.append(f"[אזור שלא נקרא {region}: {where}{'תמונה' if r.kind == 'image' else 'קטע'}"
                        + (f" — {r.note}" if r.note else "") + "]")
            continue
        if r.status == "no_text":
            flush()
            body.append(f"[תמונה ללא טקסט{': ' + r.note if r.note else ''}]")
            continue
        uncertain = uncertain or r.status == reader.UNCERTAIN
        if piece:
            content.append(piece)
        if whole and r.block_index in sent:
            run.append(sent[r.block_index])
            continue
        flush()
        if r.status == reader.UNCERTAIN:
            body.append(f"[קריאה לא ודאית {region}" + (f" — {r.note}" if r.note else "") + "]")
        if piece:
            body.append(piece)
        if r.kind == "table" and r.table_index is not None:
            t = _table_handle(ws, v.document_id, v.version_id, v.reading_id, r.table_index)
            body.append(f"[טבלה {t}: כל השורות, הכותרות וההערות — read(table={t})]")
    flush()
    if part.next is not None:
        more = _cursor(ws, v, target, part.next, within)
        if left is not None:
            ws.handles[more].setdefault("left", left)  # round 23 U0: what remained after this part, for the diagnostics
    clipped = part.next is not None or cut
    status = ("clipped" if clipped else "has_unread_regions" if unread
              else "uncertain_reading" if uncertain else "complete")
    head = [f"מצב: {STATUS_LABELS[status]}" + (f"; להמשך: read(cursor={more})" if part.next is not None
                                               else f"; להמשך: read(section={more})" if more else "")]
    if unread_pages:
        head.append("עמודים שלא נקראו בעיבוד המסמך: " + ", ".join(str(p) for p in unread_pages))
    indexes = [r.block_index for r, _, _ in part.items]
    pages = sorted({r.page for r, _, _ in part.items if r.page})
    if standard is None:  # an appraisal (or any document that states no identity) is named as it always was
        title, shown_section, shown_location = v.title, section, location or _pages_label(pages)
        source_kind, identity, source_rules = source_kinds.APPRAISAL, None, None
    else:
        title, shown_section = standard_title(standard[1], v.title), section_string(standard[1])
        shown_location = standard_location(standard[1], pages)
        source_kind, identity, source_rules = standard[0], standard[1], rules
    s = ws.add_source(document_id=v.document_id, version_id=v.version_id, title=title, section=shown_section,
                      location=shown_location, kind="context", text="\n".join(content), block_start=min(indexes),
                      block_end=max(indexes), page_list=pages or None, partial_document=v.partial,
                      reading_id=v.reading_id, status=status, clipped=clipped, unread_regions=regions, more=more,
                      resume=_target_json(target, part.next), body="\n".join(head + lines + body),
                      tags={"document": ws.doc_handle(v.document_id)}, source_kind=source_kind, standard=identity,
                      rules=source_rules)
    if cx is not None:
        _mark(s, cx, cx.spanned(min(indexes), max(indexes)) if cx.multi else [], ws)
    if cx is not None and cx.multi:
        note = _note_once(ws, v.document_id, cx)
        if note:
            s.body = "\n".join([s.body.split("\n", 1)[0], note, *s.body.split("\n", 1)[1:]])
    ws.once(s, ("read", target, pos))
    for r, _, whole in part.items:
        if whole:
            sent.setdefault(r.block_index, s.sid)
    if read:
        ws.touch(v.document_id, v.title, "read", v.partial, {"sid": s.sid, "scope": scope, "name": name,
                                                             "target": target})
        ws.read_progress(target, pos, part.next, str(v.document_id), unread)
    else:
        ws.touch(v.document_id, v.title, "retrieved", v.partial)
    return s


def _memoized_read(ws: Workspace, conn: Connection, key: tuple, v: reader.Version,
                   build: Callable[[], Source]) -> Source:
    """The source of a read of this turn: when an identical read (the same window of the same reading, the office's
    data unchanged) was made before, a new source that points at the first — the window is not fetched or cut again,
    and the model is told it already has it (R16, KTD9); the new source is citable all the same. Otherwise
    ``build()`` reads it, and the turn keeps it."""
    key = (*key, v.reading_id, current_data_version(conn))
    earlier = ws.read_memo.get(key)
    if earlier is not None:
        return ws.adopt(replace(earlier, sid="", same_as=earlier.same_as or earlier.sid))
    ws.read_memo[key] = s = build()
    return s


SIBLINGS_LISTED = 8
MSG_SIBLINGS = "סעיפים אחים שלא נקראו בסעיף «{parent}» (לקריאה: read עם section=§#): {items}"


def _unread_siblings(ws: Workspace, conn: Connection, v: reader.Version, cx, path: tuple, window: tuple[int, int] | None
                     ) -> list[str]:
    """For a section read: the other sub-sections of its parent that the turn has not read, as handles that open each
    (round 17 U6, KTD6 item 4: an item listed in a sibling sub-section was missed because nothing pointed to it). A
    sibling is read once every one of its blocks that holds text was returned to the reader (``ws.sent``); one line, or
    none when nothing is left. In a file of several contexts only the siblings within the same context."""
    parent = tuple(path[:-1])
    found, _ = reader.outline(conn, v.version_id, cx, tables=False)
    siblings = [e for e in found if len(e.path) == len(path) and tuple(e.path[:-1]) == parent
                and tuple(e.path) != tuple(path) and (window is None or e.window == window)]
    if not siblings:
        return []
    lo, hi = min(e.first for e in siblings), max(e.last for e in siblings)
    held = [r.block_index for r in conn.execute(text(
        "SELECT block_index FROM document_blocks WHERE version_id = :v AND block_index BETWEEN :a AND :z"
        " AND COALESCE(text, '') <> ''"), {"v": v.version_id, "a": lo, "z": hi})]
    sent = ws.sent.get(str(v.version_id), {})
    unread = [e for e in siblings if any(i not in sent for i in held if e.first <= i <= e.last)]
    if not unread:
        return []
    items = "; ".join(f"{_section_handle(ws, v, e.path, e.window)} «{_txt(_section_name(e.path))}»"
                      for e in unread[:SIBLINGS_LISTED])
    if len(unread) > SIBLINGS_LISTED:
        items += f" (ועוד {len(unread) - SIBLINGS_LISTED})"
    return [MSG_SIBLINGS.format(parent=_txt(_section_name(parent)), items=items)]


def _read_window(ws: Workspace, conn: Connection, v: reader.Version, target: tuple, pos,
                 within: tuple[int, int] | None = None) -> Source:
    """A part of a section (its sub-sections included) or of a page range, from ``pos`` (None: its beginning).
    ``within``: the blocks (first, last) of the unit it is scoped to (R10): no block outside them is read."""
    return _memoized_read(ws, conn, ("window", target, pos, within), v,
                          lambda: _read_window_once(ws, conn, v, target, pos, within))


def _read_window_once(ws: Workspace, conn: Connection, v: reader.Version, target: tuple, pos,
                      within: tuple[int, int] | None = None) -> Source:
    vid = v.version_id
    unread_pages = None
    from_block = pos[0] if pos else None  # ``take`` starts at ``pos``: the blocks before it are not loaded
    if target[0] == "psection":
        return _read_professional_section(ws, conn, v, target, pos)
    cx = contexts_of(ws, conn, vid, v.reading_id)
    if target[0] == "section":
        path = target[2]
        window = (target[3], target[4]) if len(target) >= 5 else None  # a section of one context (KTD7)
        rows = reader.blocks_in_section(conn, vid, path, from_block, window)
        name = _section_name(path, _later_context(cx, window))
        lines = []
        if pos is None and len(path) >= 2:  # a sub-section opened from its start: the sub-sections beside it not yet read
            lines += _unread_siblings(ws, conn, v, cx, path, window)
    else:
        first, last = target[2], target[3]
        rows = reader.blocks_on_pages(conn, vid, first, last, from_block)
        if within is not None:  # a page shared with the next unit: only the unit's own blocks
            rows = [r for r in rows if within[0] <= r.block_index <= within[1]]
        unread_pages = reader.unread_pages(conn, vid, first, last)
        name = _pages_label([first, last])
        lines = []
    if not rows:
        if unread_pages:
            raise ToolError(f"{name}: לא נקראו בעיבוד המסמך (אין בהם טקסט שנקרא)")
        raise ToolError(f"אין קטעים שנשמרו ב{name}" if target[0] == "pages" else "אין קטעים בסעיף הזה")
    served = _served_rows(ws, conn, vid, rows)
    part = reader.take(served, pos, read_chars(), ws.sent.get(str(vid), {}))
    pages = sorted({r.page for r, _, _ in part.items if r.page})
    if target[0] == "section":
        location = _location(name, "text", pages, None, None, (None, None), None)
        section = name
        if window is not None and cx.multi:
            lines += _foreign_rows(ws, conn, v, cx, window)
    else:
        location, section = _pages_label(pages), None
        # the sections these pages are in, as handles that open each of them whole (each within its context)
        paths = _sections_of(cx, part.items)
        if paths:
            lines.append("סעיפים בעמודים האלה: " + "; ".join(
                f"{_section_handle(ws, v, p, w)} «{_section_name(p)}»" for p, w in list(paths)[:SECTIONS_LISTED]))
    std = standard_for(ws, conn, vid, part.items[0][0].block_index)
    s = _emit(ws, v, target, pos, part, name=name, scope=target[0], location=location, section=section,
              lines=lines, read=True, unread_pages=unread_pages if pos is None else None, cx=cx, within=within,
              standard=std, rules=_rules_of_part(ws, conn, vid, part) if std else None, left=_left_after(served, part))
    if target[0] == "pages":  # a section opened by name is its own opening
        _whole_sections(ws, conn, v, cx, part, s)
    return s


MSG_SECTION_GONE = "הסעיף אינו קיים בקריאה הנוכחית של המסמך"
MSG_REF_UNRESOLVED = "{ref} — הסעיף אינו במסמך"
MSG_REF_EXTERNAL_ABSENT = "{ref} — תקן {number}: לא במאגר — התוכן אינו זמין. אל תמלא את תוכנו מהזיכרון."
MSG_REF_EXTERNAL_HELD = "{ref} — תקן {number} במאגר: {held}"
SUBSECTIONS_LISTED = 12


def _read_professional_section(ws: Workspace, conn: Connection, v: reader.Version, target: tuple, pos) -> Source:
    """A whole section of a standard (``sections.section_text``, R22, R23): every block from the section's first to its last,
    its subsections' included, in bounded parts that continue with a cursor. The header names the section above it, the
    subsections below it as handles that read each of them, and — for each reference the section makes — the section it
    names (a handle), or that it names a section the document does not hold, or a standard that no visible document
    carries (a gap, never filled in)."""
    vid = v.version_id
    secs = sections_in(ws, conn, vid)
    st = sections.section_text(conn, vid, int(target[2]), secs=secs, structure_only=True)  # its blocks are read below
    if st is None:
        raise ToolError(MSG_SECTION_GONE)
    sec = st.section
    by_no = {x.section_no: x for x in secs}

    def handle(x: sections.Section) -> str:
        return _psection_handle(ws, v.document_id, vid, v.reading_id, x.section_no)

    def called(x: sections.Section) -> str:
        return section_string({"section": x.label, "section_title": x.title}) or ""

    def named(x: sections.Section) -> str:
        return f"{handle(x)} «{_txt(called(x))}»"

    lines = []
    if sec.parent_no is not None and sec.parent_no in by_no:
        lines.append(f"סעיף אב: {named(by_no[sec.parent_no])}")
    children = [x for x in st.subsections if x.parent_no == sec.section_no]
    if children:
        lines.append("תתי-סעיפים (הטקסט שלהם כלול בקריאה הזו; כל אחד נקרא גם לבדו): "
                     + "; ".join(named(x) for x in children[:SUBSECTIONS_LISTED])
                     + (f" ועוד {len(children) - SUBSECTIONS_LISTED}" if len(children) > SUBSECTIONS_LISTED else ""))
    if pos is None:  # the references of the whole section, once, at its head
        lines += _reference_lines(ws, conn, st, by_no, handle)
    # ``take`` starts at ``pos``: the blocks before it are not loaded (as for the appraisal windows)
    rows = reader.blocks_between(conn, vid, max(sec.block_first, pos[0]) if pos else sec.block_first, sec.block_last,
                                 limit=None)
    served = _served_rows(ws, conn, vid, rows)
    part = reader.take(served, pos, read_chars(), ws.sent.get(str(vid), {}))
    std = standard_for(ws, conn, vid, part.items[0][0].block_index if part.items else sec.block_first, secs)
    name = called(sec)
    return _emit(ws, v, target, pos, part, name=name, scope="section", location="", section=name, lines=lines,
                 read=True, standard=std, rules=section_rules(secs, sec.block_first, sec.block_last) if std else None,
                 left=_left_after(served, part))


def _reference_lines(ws: Workspace, conn: Connection, st, by_no: dict, handle) -> list[str]:
    """What each reference of a section names (R23): a section of this document (its handle), a section it does not hold,
    or a standard — held by a visible document (which), or not in the repository."""
    out, seen = [], set()
    loaded = keyed = None
    for r in st.refs:
        ref = f"«{_txt(r['text'])}»"
        if ref in seen:
            continue
        seen.add(ref)
        if r["status"] == "resolved":
            target = by_no[r["target"]]
            out.append(f"הפניה {ref} → {handle(target)} «{_txt(target.label or target.title)}» (read(section={handle(target)}))")
        elif r["status"] == "unresolved":
            out.append("הפניה " + MSG_REF_UNRESOLVED.format(ref=ref))
        else:
            if loaded is None:
                loaded = profiles(ws, conn)
                keyed = profile.by_key(loaded[0])
            group = profile.group_of(loaded, r["standard"], keyed)
            if group.editions:
                held = "; ".join(f"{ws.doc_handle(e.document_id)} document_id={e.document_id} «{_txt(e.title)}»"
                                 + (f" ({_txt(e.edition)})" if e.edition else "") for e in group.editions)
                out.append("הפניה " + MSG_REF_EXTERNAL_HELD.format(ref=ref, number=r["standard"], held=held))
            else:
                out.append("הפניה " + MSG_REF_EXTERNAL_ABSENT.format(ref=ref, number=r["standard"]))
    return out


def _whole_sections(ws: Workspace, conn: Connection, v: reader.Version, cx, part: reader.Part, s: Source) -> None:
    """The sections a read of paragraphs or pages (``read(source)``, a page window or its cursor) returned whole: every
    block of the section, from its start to its end in the stored outline and within one appraisal context, among
    the blocks this read returned in full. Such a read is a complete opening of those sections (round 7 KTD4, R8,
    R10): it is recorded as an opening of the read's S# (``covers``: the broadest such sections, by name), so a claim
    that a datum is not there names them (``coverage._openings``). A part of a section opens nothing whole."""
    whole = {r.block_index for r, _, full in part.items if full}
    if not whole:
        return
    # every block's section and status, read once per reading of the turn: each candidate is checked from them
    key = (str(v.version_id), v.reading_id)
    every = ws.section_blocks.get(key)
    if every is None:
        every = ws.section_blocks[key] = reader.section_blocks(conn, v.version_id)
    covered = []
    for path, window in _sections_of(cx, part.items):
        if any(len(p) < len(path) and path[:len(p)] == p and w == window for p, w, _ in covered):
            continue  # inside a section already covered whole: the broader one is named
        blocks = reader.in_section(every, path, window)  # (index, section path, status)
        if blocks and {b[0] for b in blocks} <= whole:
            covered = [c for c in covered if not (len(path) < len(c[0]) and c[0][:len(path)] == path
                                                  and c[1] == window)]
            covered.append((path, window, blocks))
    if not covered:
        return
    names = [_section_name(path, _later_context(cx, window)) for path, window, _ in covered]
    targets = tuple(_section_target(v, path, window) for path, window, _ in covered)
    target = targets[0] if len(targets) == 1 else ("sections", str(v.version_id), targets)
    unread = any(b[2] == reader.UNREAD for _, _, blocks in covered for b in blocks)
    name = names[0] if len(names) == 1 else (", ".join(f'"{n}"' for n in names[:-1]) + f' ו"{names[-1]}"')
    ws.touch(v.document_id, v.title, "read", v.partial,
             {"sid": s.sid, "scope": "section" if len(names) == 1 else "sections", "name": name, "target": target,
              "covers": True})
    ws.read_progress(target, None, None, str(v.document_id), unread)


def _foreign_rows(ws: Workspace, conn: Connection, v: reader.Version, cx, window: tuple[int, int]) -> list[str]:
    """Rows of a table stored with another context's block (a table the extraction merged across two appraisals)
    that lie within this section of this context: where to read them."""
    first, last = window
    edges = reader.blocks_at(conn, v.version_id, (first, last + 1))
    if first not in edges:
        return []
    start = (edges[first].page or 0, contexts.top_of(edges[first]))
    end = (edges[last + 1].page or 0, contexts.top_of(edges[last + 1])) if last + 1 in edges else None
    number = cx.at_block(first)
    lines = []
    for index, (block_index, placed) in _table_rows(ws, conn, v).items():
        if first <= block_index <= last:
            continue
        rows = [(n, page) for n, page, top in placed if start <= (page, top or 0.0)
                and (end is None or (page, top or 0.0) < end) and cx.at_position(page, top) == number]
        if rows:
            h = _table_handle(ws, v.document_id, v.version_id, v.reading_id, index)
            lines.append(MSG_FOREIGN_ROWS.format(first=rows[0][0], last=rows[-1][0], handle=h,
                                                 pages=_rows_pages(page for _, page in rows)))
    return lines


def _table_rows(ws: Workspace, conn: Connection, v: reader.Version) -> dict[int, tuple[int, list[tuple]]]:
    """Each table of a version stored with a block: its first block, and its rows that have a page as (row number
    from 1, page, top), read once per reading of the turn."""
    key = (str(v.version_id), v.reading_id)
    found = ws.table_rows.get(key)
    if found is None:
        firsts = reader.table_first_blocks(conn, v.version_id)
        found = ws.table_rows[key] = {
            index: (firsts[index], [(n, row["page"], reader.row_top(row))
                                    for n, row in enumerate(st.get("rows") or [], 1) if row.get("page")])
            for index, st in reader.tables_of(conn, v.version_id).items() if index in firsts}
    return found


class _ServedBlock:
    """A stored block served in another form than it is stored in: a table block read with its text in the order the table is drawn
    (round 21 U8, KTD6), or a page-text row served as "label: value" pairs in drawn order (round 23 U4, KTD8: ``parts`` maps each verbatim piece
    of the served text back to the stored word it copies). Every other attribute is the stored row's. Used by the reads that show blocks to the
    agent and to the verifier; a quote's word span is looked up in the stored blocks (``take_value``) through ``parts``, which this never replaces."""

    __slots__ = ("_row", "text", "parts")

    def __init__(self, row, text_: str, parts: tuple = ()) -> None:
        self._row, self.text, self.parts = row, text_, parts

    @property
    def stored_text(self) -> str | None:
        return self._row.text

    def __getattr__(self, name: str):
        return getattr(self._row, name)


MSG_COLUMNS_UNKNOWN = ("שיוך המספרים לעמודות בשורות הטקסט האלה לא נקבע (אין מיקומי מילים שמורים או כותרת עמודות שמתאימה להם): "
                       "סדר המספרים בשורה אינו ראיה לעמודה שלהם")


def _served_rows(ws: Workspace, conn: Connection, version_id, rows: list) -> list:
    """``rows`` (stored blocks) with the text of each block of a table stored in reversed cell order served in the order it is drawn,
    each cell with its column label (``stored_tables.serve_text``), and each page-text row of numbers whose columns the stored word positions
    tell served as "label: value" pairs in drawn order (``_bound_rows``, round 23 U4); every other block, and every table or row served as
    stored, as it is. The one function the agent's reads and the verifier's evidence take their text from."""
    bound = _bound_rows(ws, conn, version_id, rows)
    out = []
    for r in rows:
        if (b := bound.get(r.block_index)) is not None:
            out.append(_ServedBlock(r, b.served, b.parts))
            continue
        st = (_table_at(ws, conn, version_id, r.table_index)[0]
              if getattr(r, "kind", None) == "table" and getattr(r, "table_index", None) is not None else None)
        served = stored_tables.serve_text(st, r.text or "") if st else None
        out.append(r if served is None or served == (r.text or "") else _ServedBlock(r, served))
    return out


def _bound_rows(ws: Workspace, conn: Connection, version_id, rows: list) -> dict[int, columns.Binding]:
    """The page-text rows of ``rows`` (text-layer paragraphs on a page, each a left-to-right row of numbers) whose columns are told, by block index
    (round 23 U4, M3, KTD8, R5). Decided from the stored word positions of the row and of the header lines above it (the row's page, then
    the previous ones for a continuation); nothing stored is changed. A row the positions cannot place is served as stored, and remembered
    in ``ws.column_unknown`` for the judge (``column_binding_note``)."""
    vid = str(version_id)
    todo = [r for r in rows if getattr(r, "kind", None) in ("paragraph", "textbox") and getattr(r, "page", None) and r.text
            and getattr(r, "status", "read") == "read" and (vid, r.block_index, r.text) not in ws.column_bindings
            and columns.row_shape(r.text) is not None]
    if todo:
        wanted = {p for r in todo for p in range(max(1, r.page - columns.PAGES_BACK), r.page + 1)}
        missing = {p for p in wanted if (vid, p) not in ws.page_words}
        if missing:
            blocks, widths = reader.page_words(conn, UUID(vid), missing)
            for p in missing:
                ws.page_words[(vid, p)] = (blocks.get(p, []), widths.get(p))
        for r in todo:
            found = _bind_row(ws, vid, r)
            ws.column_bindings[(vid, r.block_index, r.text)] = found
            if found.status == columns.UNKNOWN and columns.unbound_note_due(r.text):
                ws.column_unknown.add((vid, r.block_index))
    return {r.block_index: b for r in rows
            if getattr(r, "text", None) and (b := ws.column_bindings.get((vid, r.block_index, r.text))) is not None
            and b.status == columns.BOUND}


def _bind_row(ws: Workspace, vid: str, row) -> columns.Binding:
    blocks, width = ws.page_words[(vid, row.page)]
    mine = next(((t, s) for i, t, s in blocks if i == row.block_index), None)
    if mine is None or mine[0] != row.text:
        return columns.Binding(columns.UNKNOWN, "no word positions")
    attempts = [columns.page_lines(row.page, blocks, before=row.block_index)]
    for back in range(1, columns.PAGES_BACK + 1):
        page = row.page - back
        if page < 1:
            break
        earlier, other = ws.page_words.get((vid, page), ([], None))
        if width is not None and other is not None and abs(width - other) > 1.0:
            continue  # another page size: its x positions are no columns of this one
        attempts.append(columns.page_lines(page, earlier))
    return columns.bind(row.text, mine[1], attempts)


def column_binding_note(ws: Workspace, src: Source) -> str:
    """The neutral fact the judge reads beside a source that holds a row of numbers whose columns could not be told (round 23 U4, KTD8): empty
    when the source holds none. The server states what it could not settle, never a verdict."""
    if src.version_id is None or src.block_start is None or not ws.column_unknown:
        return ""
    vid = str(src.version_id)
    end = src.block_end if src.block_end is not None else src.block_start
    return MSG_COLUMNS_UNKNOWN if any(v == vid and src.block_start <= b <= end for v, b in ws.column_unknown) else ""


def _bound_passage(ws: Workspace, conn: Connection, h: dict, bs: int | None, be: int | None, passage: str) -> str:
    """The text of a search hit of page text with each line that is a bound row of numbers served as the read serves it (round 23 U4): the
    chunk's own text and embedding are not touched. A line that is not exactly one row of the hit's blocks stays as it is."""
    if h["kind"] in ("table", "table_row") or bs is None or be is None or not any(columns.row_shape(ln.strip()) for ln in passage.splitlines()):
        return passage
    rows = reader.blocks_between(conn, h["version_id"], bs, be, limit=reader.WINDOW_MAX)
    lines = passage.split("\n")
    for r in _served_rows(ws, conn, h["version_id"], rows):
        if isinstance(r, _ServedBlock) and r.parts:
            at = [i for i, ln in enumerate(lines) if ln.strip() == (r.stored_text or "").strip()]
            if len(at) == 1:
                lines[at[0]] = r.text
    return "\n".join(lines)


def _read_table(ws: Workspace, conn: Connection, v: reader.Version, table_index: int, row0: int = 0) -> Source:
    """``table_rows()`` rows of a table from ``row0``, each part with the table's caption, size, headers and
    notes (a continuation keeps the headers and the notes that carry the units)."""
    return _memoized_read(ws, conn, ("table", str(v.version_id), table_index, row0), v,
                          lambda: _read_table_once(ws, conn, v, table_index, row0))


def _read_table_once(ws: Workspace, conn: Connection, v: reader.Version, table_index: int, row0: int = 0) -> Source:
    st = reader.table_structure(conn, v.version_id, table_index)
    if st is None:
        raise ToolError("הטבלה לא נמצאה")
    ws.clock.note_table((str(v.version_id), table_index), stored_tables.spans_pages(st))  # round 22 U0: measurement only
    rows = st.get("rows") or []
    size = table_rows()
    window = rows[row0:row0 + size]
    nxt = row0 + len(window) if row0 + len(window) < len(rows) else None
    block = reader.table_block(conn, v.version_id, table_index)
    target = ("table", str(v.version_id), table_index)
    vid = str(v.version_id)
    more = _cursor(ws, v, target, nxt) if nxt is not None else None
    if more is not None:
        ws.handles[more].setdefault("left", {"rows": len(rows) - nxt})  # round 23 U0: what remained, for the diagnostics only
    uncertain = block is not None and block.status == reader.UNCERTAIN
    status = "clipped" if nxt is not None else "uncertain_reading" if uncertain else "complete"
    head = [f"מצב הטבלה: {TABLE_STATUS_LABELS[status]}" + (f"; להמשך: read(cursor={more})" if more else "")]
    if len(rows) > size:
        head.append(f"שורות {row0 + 1}–{row0 + len(window)} מתוך {len(rows)}")
    # what the table is read with: its heading path and the blocks before and after it (KTD4). The verifier's text holds
    # every part in full; what is sent points to the parts an earlier read of the turn returned (KTD6 item 2)
    cx = contexts_of(ws, conn, v.version_id, v.reading_id)
    ctx = _table_context(ws, conn, v, table_index, st, block, cx)
    already = ws.sent.get(vid, {})
    full_above, full_below = _table_context_lines(ctx)
    body = _table_text(st, window, before=full_above, after=full_below)
    # a table the extraction merged across appraisal contexts: its rows of each context, marked where they start
    groups = reader.table_contexts(cx, st, block.block_index if block is not None else st.get("block_index"),
                                   block.page if block is not None else st.get("page"))
    numbers = [g["context"] for g in groups if g["first"] <= row0 + len(window) and g["last"] > row0]
    marks = None
    if len(groups) > 1:
        marks = {max(g["first"] - 1, row0) - row0: MSG_TABLE_ROWS.format(
            first=g["first"], last=g["last"], which=cx.describe(g["context"])) for g in groups
            if g["first"] <= row0 + len(window) and g["last"] > row0}
    above, below = _table_context_lines(ctx, already)
    sent = _table_text(st, window, marks, before=above, after=below) + "\n" + _follows_line(ws, v, ctx)
    page = block.page if block is not None else st.get("page")
    index = block.block_index if block is not None else st.get("block_index")
    name = st.get("caption") or (st.get("title") or [""])[0] or st.get("section") or "טבלה"
    s = ws.add_source(document_id=v.document_id, version_id=v.version_id, title=v.title, section=st.get("section"),
                      location=_location(st.get("section"), "table", [page] if page else None, None, None,
                                         (None, None), st.get("media")),
                      kind="table", text=body, block_start=index, block_end=index, table_index=table_index,
                      page_list=[page] if page else None, partial_document=v.partial, reading_id=v.reading_id,
                      status=status, clipped=nxt is not None, more=more, resume=_target_json(target, nxt),
                      table_part=len(window) < len(rows), table_parts=list(ctx.parts),
                      body="\n".join(head) + "\n" + sent,
                      tags={"document": ws.doc_handle(v.document_id),
                            "table": _table_handle(ws, v.document_id, vid, v.reading_id, table_index)})
    if not st.get("section") and block is not None:
        s.heading = _heading_of(conn, v.version_id, block.block_index)
    _mark(s, cx, numbers, ws)
    ws.once(s, ("read", target, row0))
    ws.touch(v.document_id, v.title, "read", v.partial, {"sid": s.sid, "scope": "table", "name": name,
                                                         "target": target})
    ws.read_progress(target, row0 or None, nxt, str(v.document_id), False)
    # the blocks this read returned whole: every context part (each is sent in full or points to the source that sent
    # it), and the table's own blocks only when this one read holds the whole table (KTD6 item 2)
    registered = ws.sent.setdefault(vid, {})
    for p in ctx.parts:
        registered.setdefault(p.block_index, s.sid)
    if status == "complete" and row0 == 0:
        for b in ctx.table_blocks:
            registered.setdefault(b, s.sid)
    return s


def _neighbors(rows: list, start: int, end: int, sent: Container[int]) -> tuple[list, bool]:
    """The source's blocks and as many around them as fit, nearest first, kept contiguous; whether some were
    left out. Blocks the turn already returned cost nothing: they are sent as a pointer."""
    def cost(r) -> int:
        return 0 if r.block_index in sent else len(r.text or "")

    core = [r for r in rows if start <= r.block_index <= end]
    if not core:
        return [], False
    budget = read_chars() - sum(cost(r) for r in core)
    lo, hi = rows.index(core[0]), rows.index(core[-1])
    grow = True
    while grow:
        grow = False
        for i in (lo - 1, hi + 1):
            if 0 <= i < len(rows) and cost(rows[i]) <= budget:
                budget -= cost(rows[i])
                lo, hi = min(lo, i), max(hi, i)
                grow = True
    return rows[lo:hi + 1], lo > 0 or hi < len(rows) - 1


def _read_source(ws: Workspace, conn: Connection, sid: str) -> Source:
    """The paragraphs around a source (S#/P#), or the whole table a table passage is from."""
    if sid in ws.sources and ws.sources[sid].is_listing:
        raise ToolError("רשימת מסמכים אינה מקום במסמך: מסמך נפתח ב-outline או ב-pages עם ה-D# שלו")
    ref = _ref(ws, sid)
    v = reader.version(conn, UUID(str(ref["version_id"])))
    if v is None:
        raise ToolError(MSG_UNAVAILABLE)
    if "reading_id" in ref and ref["reading_id"] != v.reading_id:
        # block numbers of an earlier reading mean other text in the current one: nothing is opened
        raise ToolError(MSG_STALE_REF.format(source_id=sid))
    vid = str(v.version_id)
    ws.readings.setdefault(vid, v.reading_id)
    lines = []
    resumed = _target_from_json(ref["resume"], vid) if ref.get("resume") else None
    if resumed is not None:  # an earlier turn read it only in part: where it stopped
        lines.append(f"בתור הקודם המקור נקרא רק בחלקו; להמשך מהמקום שבו נעצרה הקריאה: read(cursor="
                     f"{_cursor(ws, v, *resumed)})")
    if ref.get("table_index") is not None:
        s = _read_table(ws, conn, v, ref["table_index"])
        if lines:
            s.body = "\n".join(lines) + "\n" + s.body
        return s
    start, end = ref.get("block_start"), ref.get("block_end")
    if start is None:
        if not ref.get("chunk_id"):
            raise ToolError("למקור הזה אין מיקום במסמך להרחבה")
        row = conn.execute(text("SELECT text, section, page_list, kind FROM chunks WHERE id = :c AND version_id = :v"),
                           {"c": ref["chunk_id"], "v": v.version_id}).first()
        if row is None:
            raise ToolError(MSG_UNAVAILABLE)
        clipped = len(row.text) > read_chars()
        status = "clipped" if clipped else "complete"
        s = ws.add_source(document_id=v.document_id, version_id=v.version_id, title=v.title, section=row.section,
                          location=_location(row.section, row.kind, row.page_list, None, None, (None, None), None),
                          kind=row.kind, text=row.text[:read_chars()], page_list=row.page_list,
                          partial_document=v.partial, reading_id=v.reading_id, status=status, clipped=clipped,
                          tags={"document": ws.doc_handle(v.document_id)})
        s.body = "\n".join([f"מצב: {STATUS_LABELS[status]}"] + lines + [s.text])
        ws.touch(v.document_id, v.title, "retrieved", v.partial)
        return s
    end = end if end is not None else start
    cx = contexts_of(ws, conn, v.version_id, v.reading_id)
    rows = _served_rows(ws, conn, v.version_id,
                        reader.blocks_between(conn, v.version_id, max(0, start - NEIGHBORS), end + NEIGHBORS))
    chosen, cut = _neighbors(rows, start, end, ws.sent.get(vid, {}))
    if not chosen:
        raise ToolError("למקור הזה אין קטעים בקריאה הנוכחית של המסמך")
    own = next((r for r in chosen if start <= r.block_index <= end), None)
    path = reader.effective_path(cx, own.block_index, own.section_path) if own is not None else ()

    def window(p: tuple) -> tuple[int, int] | None:
        return reader.section_window(cx, own.block_index, p) if own is not None else None

    handles = [f"{_section_handle(ws, v, path[:i], window(path[:i]))} «{_section_name(path[:i])}»"
               for i in range(1, len(path) + 1)]
    if handles:
        lines.append("בסעיף: " + " / ".join(handles) + "; לקריאת הסעיף כולו: read(section=§#)")
    part = reader.Part([(r, r.text or "", True) for r in chosen], None)
    pages = sorted({r.page for r in chosen if r.page})
    section = path[-1] if path else None
    std = standard_for(ws, conn, v.version_id, start)
    s = _emit(ws, v, ("neighbors", vid, start, end), None, part, name=section or "", scope="neighbors",
              location=_location(section, "text", pages, None, None, (None, None), None), section=section,
              lines=lines, read=False, more=_section_handle(ws, v, path, window(path)) if cut and path else None,
              cut=cut, cx=cx, standard=std, rules=_rules_of_part(ws, conn, v.version_id, part) if std else None)
    _whole_sections(ws, conn, v, cx, part, s)
    return s


def tool_read(ws: Workspace, target: dict) -> str:
    """Open one place of a document (``LOCATORS``). Every access resolves the document again under the current
    permissions, and a handle only while the reading it was issued for is the document's current one."""
    if not isinstance(target, dict):
        raise ToolError("target חייב להיות אובייקט עם מיקום אחד")
    given = {k: v for k, v in target.items() if k in LOCATORS and v not in (None, "", {})}
    if len(given) != 1:
        raise ToolError("יש לבחור מיקום אחד בדיוק: source (S#/P#), pages, section (§#), table (T#) או cursor (K#)")
    ((kind, value),) = given.items()
    if kind != "pages":
        value = str(value).strip()
        data = ws.handles.get(value)
        if data is not None and data["kind"] in ("§", "T", "K"):  # a handle given in another field opens as itself
            kind = {"§": "section", "T": "table", "K": "cursor"}[data["kind"]]
        elif data is not None and data["kind"] == "D":
            raise ToolError(f"{value} הוא מסמך: את המבנה שלו פותחים ב-outline, ועמודים ב-pages")
    elif not isinstance(value, dict):
        raise ToolError("pages חייב לכלול document, from_page ו-to_page")
    note = ""
    with tenant_tx(ws.ctx) as conn:
        if kind == "source":
            s = _read_source(ws, conn, value)
        elif kind == "pages":
            by_unit = str(value.get("document") or "").strip()
            if by_unit and (ws.handles.get(by_unit) or {}).get("kind") != "U":
                by_unit = focus_unit_for(ws, by_unit) or by_unit  # the file of the conversation's unit: its unit
            if (ws.handles.get(by_unit) or {}).get("kind") == "U":  # the pages of a unit (U#), within the unit
                v, ucx, number = _unit_of(conn, ws, by_unit)
            else:
                v, ucx, number = _document(conn, ws, value.get("document")), None, None
            try:
                first, last = int(value["from_page"]), int(value["to_page"])
            except (KeyError, TypeError, ValueError):
                raise ToolError("טווח עמודים לא תקין: נדרשים from_page ו-to_page") from None
            if first < 1 or last < first:
                raise ToolError("טווח עמודים לא תקין: from_page מ-1 ומעלה, ו-to_page לא קטן ממנו")
            if not v.is_pdf:
                raise ToolError("למסמך הזה אין עמודים (DOCX): פותחים בו סעיף מתוך outline")
            if v.page_count and first > v.page_count:
                raise ToolError(f"למסמך יש {v.page_count} עמודים")
            if ucx is not None and ucx.multi:
                first, last, note = _within_unit(ucx, number, by_unit, first, last)
            last = min(last, v.page_count or last, first + PAGES_MAX - 1)
            reach = ucx.reach(number) if ucx is not None and ucx.multi else None
            s = _read_window(ws, conn, v, ("pages", str(v.version_id), first, last), None,
                             (reach.first, reach.last) if reach is not None else None)
        elif kind == "section":
            data = _handle(ws, value, "§")
            v = _bound(conn, ws, value, data)
            if data.get("psection") is not None:  # a section of a standard, read whole with its subsections (R22)
                s = _read_window(ws, conn, v, ("psection", str(v.version_id), data["psection"]), None)
            else:
                s = _read_window(ws, conn, v, _section_target(v, data["path"], data.get("window")), None)
        elif kind == "table":
            data = _handle(ws, value, "T")
            if data.get("vision"):  # read whole by inspect: nothing more to read, its cells are taken from its source
                _bound(conn, ws, value, data)
                raise ToolError(MSG_VISION_TABLE_READ.format(handle=value, sid=data["vision"]["source"]))
            s = _read_table(ws, conn, _bound(conn, ws, value, data), data["table_index"])
        else:
            data = _handle(ws, value, "K")
            v = _bound(conn, ws, value, data)
            ws.followed_cursors.add(value)  # round 23 U0: the agent used this cursor (diagnostics only)
            if data["target"][0] == "table":
                s = _read_table(ws, conn, v, data["target"][2], data["pos"])
            else:
                s = _read_window(ws, conn, v, data["target"], data["pos"], data.get("within"))
        stamp_valuation(ws, conn, s)
    return _render_source(s) + (f"\n\n{note}" if note else "")


MSG_PAGES_OUTSIDE = "העמודים {first}–{last} אינם ביחידה {unit} ({pages}): לא נפתח דבר. פתח עמודים ביחידה, או את המסמך (D#)."
MSG_PAGES_CLAMPED = "הטווח נחתך לעמודי היחידה {unit} ({pages}): העמודים שמחוץ לה לא נפתחו."


def _within_unit(cx, number: int, handle: str, first: int, last: int) -> tuple[int, int, str]:
    """A page range cut to the pages of a unit (with the neighbour its probable edge reaches over), and what to say
    when it was cut; a range wholly outside it is refused."""
    reach = cx.reach(number)
    lo, hi = reach.first_page, reach.last_page
    if lo is None or hi is None:
        return first, last, ""
    pages = _pages_label(list(range(lo, hi + 1)))
    unit = f"{handle} «{_txt(cx.label(number))}»"
    if last < lo or first > hi:
        raise ToolError(MSG_PAGES_OUTSIDE.format(first=first, last=last, unit=unit, pages=pages))
    cut = (max(first, lo), min(last, hi))
    return cut[0], cut[1], MSG_PAGES_CLAMPED.format(unit=unit, pages=pages) if cut != (first, last) else ""


def _paginate(page, total: int, size: int) -> tuple[int, int]:
    """(the requested page, the number of pages) for ``total`` items, ``size`` per page; refuses a page out of
    range."""
    pages = math.ceil(total / size)
    try:
        page = int(page or 1)
    except (TypeError, ValueError):
        raise ToolError("מספר עמוד לא תקין") from None
    if page < 1 or (pages and page > pages):
        raise ToolError(f"אין עמוד {page}; יש {pages} עמודים")
    return page, pages


_LISTED_ID = re.compile(r"(?:D\d+\s+)?document_id=[0-9a-fA-F-]+\s*\|\s*")


def _listing(ws: Workspace, title: str, lines: list[str], documents: list[tuple], *, key: tuple, criterion: str,
             total: int, page: int, pages: int, source_kind: str = source_kinds.APPRAISAL) -> str:
    """A page of documents as a source of the turn: a count or a list over the set cites it (S#), and is
    checked against it like any passage. It has no document of its own; the documents it names are kept for the
    permission checks of the answer that cites it, and the set it is a page of (``key``: the tool and its query)
    for the coverage of that answer. ``source_kind``: the kind of the documents it lists (round 11 U4, KTD12): a listing of
    standards is a professional source, so a claim of a property datum resting only on it is the standard's, never the
    property's (``verify._professional``); listings of appraisals stay appraisals."""
    # the source keeps the text without the document ids: their hex digits are no numbers of the set
    docs = [{"document_id": str(d), "title": t} for d, t in documents]
    s = ws.add_source(document_id=None, version_id=None, title=title, section=None, location="רשימת מסמכים",
                      kind="listing", text="\n".join(_LISTED_ID.sub("", ln) for ln in lines),
                      listed=[d["document_id"] for d in docs], source_kind=source_kind,
                      listing={"key": key, "criterion": criterion, "total": total, "page": page, "pages": pages,
                               "documents": docs})
    kind_tag = "" if source_kind == source_kinds.APPRAISAL else f' source_kind="{source_kind}"'
    return (f'<source id="{s.sid}" kind="listing" title="{_attr(title)}"{kind_tag}>\n' + "\n".join(_txt(ln) for ln in lines)
            + f"\n</source>\nאפשר לצטט את הרשימה הזו ({s.sid}) לספירה או לרשימה של המסמכים בתחום.")


def new_scope(query: str, documents, pages: int) -> dict:
    """The turn's scope: the set a question is about (``documents`` as (id, title) pairs), read page by page."""
    return {"query": query, "matching": [{"document_id": str(i), "title": t} for i, t in documents],
            "pages": pages, "pages_read": set()}


def _page_line(page: int, pages: int, total: int, what: str) -> str:
    more = f" — יש עוד; לקבלת הבאים: page={page + 1}" if page < pages else ""
    return f"עמוד {page} מתוך {max(pages, 1)}; סה\"כ {total} {what}{more}"


def tool_list_documents(ws: Workspace, query: str | None = None, page: int | None = None) -> str:
    """Visible documents with their reading state, a page at a time. Without a title query the listing is the
    whole repository the user sees, and it becomes the turn's scope (all of it), read page by page."""
    words = [w for w in re.findall(r"[\w\"״׳'-]+", query or "") if len(w) >= 2][:6]
    with tenant_tx(ws.ctx) as conn:
        params: dict = {"e": EXTRACTION_VERSION}
        cond = ""
        if words:
            params["p"] = [f"%{w.lower()}%" for w in words]
            cond = " AND lower(d.title) LIKE ANY(:p)"
        total = conn.execute(text("SELECT count(*) FROM documents d JOIN document_versions v ON v.document_id = d.id"
                                  f" AND v.is_current WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL}{cond}"),
                             params).scalar_one()
        page, pages = _paginate(page, total, LIST_MAX)
        rows = conn.execute(text(
            "SELECT d.id, d.title, v.id AS vid, v.status, v.page_count, v.pages_incomplete, v.ingestion,"
            " v.created_at, r.state AS mstate FROM documents d JOIN document_versions v ON v.document_id = d.id"
            " AND v.is_current LEFT JOIN measurement_runs r ON r.version_id = v.id AND r.extraction_version = :e"
            f" WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL}{cond} ORDER BY d.title, d.id LIMIT {LIST_MAX} OFFSET :o"),
            params | {"o": (page - 1) * LIST_MAX}).all()
        if not words:
            # the whole repository is the scope only when no set was looked up (find_documents keeps precedence)
            if ws.scope is None:
                everything = conn.execute(text(
                    "SELECT d.id, d.title FROM documents d JOIN document_versions v ON v.document_id = d.id"
                    f" AND v.is_current WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL} ORDER BY d.title, d.id")).all()
                ws.scope = new_scope("", [(r.id, r.title) for r in everything], pages)
            if ws.scope["query"] == "":
                ws.scope["pages_read"].add(page)
    if not rows:
        return ("לא נמצאו מסמכים" + (f' שכותרתם כוללת "{query}"' if query else "") + "." + _unread_listing(ws, words))
    criterion = (f'מסמכים שכותרתם כוללת את אחת המילים "{_txt(query)}"' if words
                 else "המסמכים שהמשתמש מורשה לראות")
    lines = [f"תחום: {criterion}", _page_line(page, pages, total, "מסמכים")]
    for r in rows:
        partial, details = reading_notes(r.ingestion or {}, r.pages_incomplete)
        status = "נקרא חלקית" if partial else "נקרא במלואו"
        mstate = {"done": "חולצו", "partial": "חולצו חלקית", "failed": "החילוץ נכשל", "pending": "בתהליך"}.get(
            r.mstate or "", "טרם חולצו")
        lines.append(f'- {ws.doc_handle(r.id)} document_id={r.id} | "{_txt(r.title)}" | עיבוד: {r.status} | קריאה: {status}'
                     + (f" ({'; '.join(details)})" if details else "") + f" | נתונים כמותיים: {mstate}")
    return _listing(ws, "רשימת מסמכים", lines, [(r.id, r.title) for r in rows], key=("list_documents", " ".join(words)),
                    criterion=criterion, total=total, page=page, pages=pages) + _unread_listing(ws, words)


# --- the edition in force at the property's valuation date (round 11 U8: R8; KTD7) -----------------------------------------
#
# For a property or mixed question, the edition of a standard that applies is the one in force at the property's valuation
# date: the latest edition whose stated effective date, else published date, is on or before it. This is applicability, not
# recency: it runs with the switch on or off, and it never touches ``profile._ordered`` (the order of the editions stays as
# round 10 built it). With no valuation date the line says so and no edition — the newest included — is called applicable.

MSG_EDITION_IN_FORCE = ("המהדורה בתוקף למועד הקובע {date} של הנכס, מתוך תקן {number}: {name} «{edition}» (בתוקף מ-{since}); "
                        "זו המהדורה שחלה על הנכס, ולא המאוחרת שבמאגר.")
MSG_EDITION_DATE_UNKNOWN = ("המועד הקובע של הנכס אינו ידוע (השומה אינה מציינת אותו, או שהיא מציינת כמה מועדים שונים): אי "
                            "אפשר לקבוע איזו מהדורה של התקן חלה על הנכס, ואין לקרוא לאף מהדורה — גם למאוחרת — \"החלה\".")
MSG_EDITION_NONE_YET = ("אף אחת מהמהדורות שבמאגר לא הייתה בתוקף במועד הקובע של הנכס: אין לקרוא לאף אחת מהן \"החלה\" "
                        "עליו (ייתכנו מהדורות קודמות שאינן במאגר).")
MSG_EDITION_UNDETERMINED = ("אי אפשר לקבוע מהתאריכים שנכתבו במהדורות איזו מהן הייתה בתוקף במועד הקובע {date} של הנכס "
                            "(תאריך תחילה חסר, או שהוא נופל בתוך התקופה שהמועד הקובע מציין): אין לקרוא לאף מהדורה \"החלה\".")
MSG_EDITION_THIS_IN_FORCE = "מהדורה זו היא זו שבתוקף למועד הקובע {date} של הנכס."
MSG_EDITION_THIS_NOT = "מהדורה זו אינה זו שבתוקף למועד הקובע {date} של הנכס; בתוקף אז: {edition}."


def _dated_appraisal(src: Source) -> bool:
    """Whether a source is a passage of an appraisal with a place in it: the sources whose unit's valuation date says what
    the property's valuation date is."""
    return src.version_id is not None and src.source_kind == source_kinds.APPRAISAL and src.block_start is not None


def property_valuations(ws: Workspace, conn: Connection) -> list[chat_dates.StatedDate | None]:
    """The valuation dates of the property the question is about (R8): the located unit's when the turn has one, else those
    of the appraisal passages the turn returned — each distinct stated date once, then None if any is unknown. Empty for a
    question that is not about a property, and when the turn has not reached an appraisal yet (nothing to apply)."""
    if ws.intent not in ("property", MIXED):
        return []
    found: list[chat_dates.UnitValuation] = []
    focus = ws.handles.get((ws.unit_focus or {}).get("handle", ""))
    if focus is not None and focus["kind"] == "U":
        v = UUID(focus["version_id"])
        stated = chat_dates.valuation_dates(conn, [v], cache=ws.date_cache).get(v, ())
        found.append(chat_dates.resolve_unit([d for d in stated if d.unit_no == focus["unit_no"]], focus["unit_no"]))
    else:
        found += [valuation_of(ws, conn, src.version_id, src.block_start)
                  for src in ws.sources.values() if _dated_appraisal(src)]
    stated_dates = sorted({(u.date.iso, u.date.precision): u.date for u in found if u.status == "stated"
                           and u.date is not None}.items())
    return [d for _, d in stated_dates] + ([None] if any(u.status != "stated" for u in found) else [])


def edition_in_force(group, valued: chat_dates.StatedDate) -> tuple[Any, str]:
    """(the edition, how it was found) for one valuation date among the editions of a standard number: ``found``; ``none_yet``
    (every edition starts after the date); ``undetermined`` (an edition states no date, the order is not known, or an edition
    starts inside the period a month or year valuation names, so the date does not tell)."""
    if not group.order_known or any(not (e.effective or e.published) for e in group.editions):
        return None, "undetermined"
    first, last = bounds(valued.iso, valued.precision)
    started = []
    for e in group.editions:
        since = e.effective or e.published
        start = bounds(since, precision_of(since))[0]
        if start <= first:
            started.append((start, e))
        elif start <= last:
            return None, "undetermined"
    return (max(started, key=lambda x: x[0])[1], "found") if started else (None, "none_yet")


def _edition_line(group, valued: chat_dates.StatedDate | None, on_found: Callable[[Any, str], str]) -> str:
    """The line for one valuation date of the property against the editions of ``group`` (KTD7): the date is unknown, no
    edition was in force, the editions' dates do not tell — or, for the edition found in force, ``on_found(edition, the
    date as shown)``, which is what differs between the search results and the note under a passage."""
    if valued is None:
        return MSG_EDITION_DATE_UNKNOWN
    shown = shown_date(valued.iso, valued.precision)
    e, how = edition_in_force(group, valued)
    if how == "found":
        return on_found(e, shown)
    return MSG_EDITION_NONE_YET if how == "none_yet" else MSG_EDITION_UNDETERMINED.format(date=shown)


def applicability_lines(group, valuations: list, names: dict) -> list[str]:
    """What the property's valuation date says about which edition of ``group`` applies (KTD7), one line per date."""
    if len(group.editions) < 2:
        return []
    return [_edition_line(group, valued, lambda e, shown: MSG_EDITION_IN_FORCE.format(
        date=shown, number=_txt(group.number), name=names[e.version_id], edition=_txt(e.edition or UNKNOWN),
        since=_txt(e.effective_text or e.published_text or UNKNOWN))) for valued in valuations]


def _edition_note(ws: Workspace, conn: Connection, version_id, std: dict,
                  valuations: Callable[[], list] | None = None) -> list[str]:
    """For a property or mixed question: whether the edition a passage is of is the one in force at the property's valuation
    date, or that the date is unknown (KTD7). Nothing for a standard with a single edition, for any other question, and
    while the turn holds no valuation date to apply (``valuations``: the caller's memo of ``property_valuations``, read
    first so that no profile is loaded for nothing)."""
    if ws.intent not in ("property", MIXED) or not std.get("number"):
        return []
    valued_at = valuations() if valuations is not None else property_valuations(ws, conn)
    if not valued_at:
        return []
    group = profile.group_of(profiles(ws, conn), std["number"])
    if len(group.editions) < 2:
        return []

    def on_found(e, shown: str) -> str:
        if e.version_id == UUID(str(version_id)):
            return MSG_EDITION_THIS_IN_FORCE.format(date=shown)
        return MSG_EDITION_THIS_NOT.format(date=shown, edition=_txt(e.edition or UNKNOWN))

    return [_edition_line(group, valued, on_found) for valued in valued_at]


STANDARD_KIND_WORDS = {source_kinds.STANDARD: "תקן שמאות", source_kinds.PROFESSIONAL: "מסמך מקצועי"}


def _edition_lines(ws: Workspace, group, names: dict) -> list[str]:
    """The editions of one standard number with whether their order is known (KD12): the latest is named only when the
    dates the documents state order them, and then only among the editions the repository holds."""
    if len(group.editions) < 2 and not group.related:
        return []
    out = []
    if group.editions:
        out.append(f"מהדורות של תקן {group.number}: " + "; ".join(
            f"{names[e.version_id]} «{_txt(e.edition) if e.edition else UNKNOWN}» בתוקף מ: "
            f"{_txt(e.effective_text) if e.effective_text else UNKNOWN}" for e in group.editions))
    if len(group.editions) >= 2:
        if group.order_known:
            last = group.editions[-1]
            basis = "תאריך התחילה" if all(e.effective for e in group.editions) else "תאריך הפרסום"
            out.append(f"סדר המהדורות: ידוע — לפי {basis} שנכתב בכל אחת. המאוחרת מבין המהדורות שבמאגר: "
                       f"{names[last.version_id]} (בתוקף מ-{last.effective_text or last.published_text}); ייתכנו מהדורות "
                       "שאינן במאגר.")
        else:
            out.append(f"סדר המהדורות: אינו ידוע. {MSG_ORDER_UNKNOWN}")
    return out


def _stated(value: str | None) -> str:
    return _txt(value) if value else UNKNOWN


def tool_find_standards(ws: Workspace, query: str | None = None, page: int | None = None) -> str:
    """The standards and professional documents the user may see, a page at a time (KTD12, R24, R30): each with what it
    states about itself — number, name, edition, the two dates, issuer, scope — or "לא ידוע", the document it amends, the
    editions of its number with whether their order is known, and the later copies of its text ("אותו נוסח כמו …"). A
    listing source the answer may cite. An office with none says so, and the turn records the gap."""
    if PROFESSIONAL_POOL not in ws.pools:
        return MSG_STANDARDS_UNUSED
    words = [w.lower() for w in re.findall(r"[\w\"״׳'.-]+", query or "")][:6]
    with tenant_tx(ws.ctx) as conn:
        loaded = profiles(ws, conn)
        found = catalog.entries(conn)
        if not found:
            ws.standards_absent = True
            return MSG_NO_STANDARDS
        heads = catalog.canonical(found)
        later = catalog.copies(found)
        by_version = {ed.version_id: ed for ed in loaded[0]}
        shown = [e for e in found if heads[e.version_id].version_id == e.version_id]
        if words:
            def haystack(e) -> str:
                ed = by_version.get(e.version_id)
                return " ".join(x for x in (e.title, ed.number if ed else None, ed.name if ed else None,
                                            ed.edition if ed else None) if x).lower()

            shown = [e for e in shown if all(w in haystack(e) for w in words)]
        head_loaded = ([ed for ed in loaded[0] if heads[ed.version_id].version_id == ed.version_id], loaded[1])
        head_keyed, keyed = profile.by_key(head_loaded[0]), profile.by_key(loaded[0])  # built once for every group, link
        groups = {}
        for e in shown:
            ed = by_version.get(e.version_id)
            if ed is not None and ed.key and ed.key not in groups:
                groups[ed.key] = profile.group_of(head_loaded, ed.key, head_keyed)
        # by number, the editions of one number oldest first when their order is known; documents with no number last
        place = {x.version_id: (n, k) for n, g in enumerate(sorted(groups.values(), key=lambda g: g.number or ""))
                 for k, x in enumerate(g.editions)}
        shown.sort(key=lambda e: (*place.get(e.version_id, (len(groups), 0)), e.title, str(e.document_id)))
        total = len(shown)
        if not total:
            return f'לא נמצא תקן או מסמך מקצועי שמתאים ל"{_txt(query)}".'
        page, pages = _paginate(page, total, LIST_MAX)
        page_rows = shown[(page - 1) * LIST_MAX:page * LIST_MAX]
        names = {e.version_id: ws.doc_handle(e.document_id) for e in found}
        lines = ["תחום: תקנים ומסמכים מקצועיים שהמשתמש מורשה לראות (טקסט זהה נספר פעם אחת; כל הקבצים נשמרים)",
                 _page_line(page, pages, total, "תקנים ומסמכים מקצועיים")]
        listed: list[tuple] = []
        for e in page_rows:
            ed = by_version.get(e.version_id)
            fields = [f"סוג: {STANDARD_KIND_WORDS.get(e.source_kind, e.source_kind)}",
                      f"מספר: {_stated(ed.number if ed else None)}", f"שם: {_stated(ed.name if ed else None)}",
                      f"מהדורה: {_stated(ed.edition if ed else None)}",
                      f"פורסם: {_stated(ed.published_text if ed else None)}",
                      f"בתוקף מ: {_stated(ed.effective_text if ed else None)}",
                      f"גוף מפרסם: {_stated(ed.issuer if ed else None)}", f"תחולה: {_stated(ed.scope if ed else None)}"]
            if ed is not None and ed.amends is not None:
                link = profile.linked(loaded, ed, keyed).amends
                held = (f"במאגר: {names[link['link']['version_id']]}" if link.get("link") else
                        "כמה מהדורות במאגר — לא נקשר לאחת" if link.get("ambiguous") else "התקן אינו במאגר")
                fields.append(f"{RELATIONS.get(link['relation'], link['relation'])} תקן {_txt(link['standard'])} ({held})")
            lines.append(f'- {names[e.version_id]} document_id={e.document_id} | "{_txt(e.title)}" | '
                         + " | ".join(fields))
            listed.append((e.document_id, e.title))
            for c in later.get(e.version_id, []):
                lines.append(f'  - {names[c.version_id]} document_id={c.document_id} | "{_txt(c.title)}" — אותו נוסח '
                             f'כמו {names[e.version_id]} «{_txt(e.title)}»')
                listed.append((c.document_id, c.title))
        on_page = {e.version_id for e in page_rows}
        valuations = property_valuations(ws, conn)  # R8: the edition in force at the property's valuation date
        for g in groups.values():
            if any(x.version_id in on_page for x in g.editions):
                lines += _edition_lines(ws, g, names) + applicability_lines(g, valuations, names)
    return _listing(ws, "תקנים ומסמכים מקצועיים", lines, listed, key=("find_standards", " ".join(words)),
                    criterion="תקנים ומסמכים מקצועיים שהמשתמש מורשה לראות", total=total, page=page, pages=pages,
                    source_kind=source_kinds.PROFESSIONAL)


def _unread_listing(ws: Workspace, words: list[str]) -> str:
    """For the model, after a listing: the visible documents whose latest version was not read, outside the listing (a
    count or a list over the set never includes them) — those whose title holds one of the words, or all of them for
    the whole repository. They are kept as the turn's named unread documents (KTD11)."""
    files = ws.unread_files()
    if words:
        # a document is named by the query only when its title holds all the words; the whole repository names none
        named = unread_matching(None, " ".join(words), files)
        lowered = [w.lower() for w in words]
        files = [d for d in files if any(w in d["title"].lower() for w in lowered)]
        ws.name_unread(named)
    block = unread_text.tool_block(files[:LIST_MAX])
    return f"\n\n{block}" if block else ""


MSG_UNITS_LISTED = "יחידות שמכילות את כל המונחים בקבצים של כמה שומות (U# נבחר ב-search, outline ו-read):"
MSG_SEVERAL_UNITS = ("כמה יחידות מתאימות. אם השאלה אינה מכריעה ביניהן — שאל את המשתמש לאיזו התכוון, ופרט כותרת, עמודים "
                     "ומזהים; אל תבחר לפי מספר הקטעים שבהם המונחים מופיעים.")
MSG_SPLIT_TERMS = ("המונחים מופיעים בקובץ «{title}» ({n} יחידות) אך ביחידות שונות: אין בו יחידה אחת שמכילה את כולם, "
                   "ולכן אין בו התאמה. אפשר לשאול את המשתמש לאיזו שומה התכוון.")
MSG_ONLY_COMPARABLE = ("בקובץ «{title}» המונחים מופיעים רק כנכס השוואה ב{label} ({pages}): זו אינה שומה של הנכס שנשאל "
                       "עליו.")


def _listed_handle(ws: Workspace, u: dict) -> str:
    """The handle of a unit ``find_documents`` lists, noted as one the model was shown: it may be declared in a clarification
    (round 15 U6)."""
    h = unit_handle_of(ws, u["document_id"], u["version_id"], u["reading_id"], u["unit_no"], u.get("units_tag"))
    ws.listed_units.add(h)
    return h


def _unit_detail(ws: Workspace, u: dict) -> str:
    """A unit found by ``find_documents``: handle, title (or that it has none), kind, pages, identifiers, and why it
    matched (a name the document states, or passages)."""
    h = _listed_handle(ws, u)
    why = {"alias": "שם שכתוב במסמך", "file_title": "כותרת הקובץ"}.get(u["matched_by"], "קטעים ביחידה")
    return (f'  - {h} | «{_txt(u["title"]) if u["title"] else MSG_NO_TITLE}» | {UNIT_KIND_WORDS.get(u["kind"], u["kind"])}'
            f' | {_unit_pages(u["page_first"], u["page_last"])}' + (f' | {_txt(" · ".join(u["identifiers"]))}' if u["identifiers"] else "")
            + f" | קובץ: «{_txt(u['file_title'])}» | התאמה: {why}"
            + (" | גבול לא ודאי" if u["certainty"] == "probable" else ""))


MSG_CONTINUATION_OF = "המשך של {subject} (חלק מאותה שומה, לא נושא בפני עצמו)"


def _continuation_detail(ws: Workspace, u: dict, subject: str) -> str:
    """A unit that continues a subject unit ``find_documents`` listed (an appendix or an untitled part after it, round 17
    U6, KTD6 item 5): its handle, kind and pages, labelled as the continuation of ``subject`` and never as a subject. Its
    handle is a unit the model was shown, so it may be read, searched in and declared."""
    h = _listed_handle(ws, u)
    return (f'    ↳ {h} | «{_txt(u["title"]) if u["title"] else MSG_NO_TITLE}» | {UNIT_KIND_WORDS.get(u["kind"], u["kind"])}'
            f' | {_unit_pages(u["page_first"], u["page_last"])} | {MSG_CONTINUATION_OF.format(subject=subject)}')


def _units_with_continuations(ws: Workspace, shown: list[dict]) -> list[str]:
    """The unit lines of the documents shown: each subject unit, then the continuation units that follow it."""
    lines: list[str] = []
    for d in shown:
        follow = d.get("continuations") or []
        for u in d["units"]:
            lines.append(_unit_detail(ws, u))
            handle = unit_handle_of(ws, u["document_id"], u["version_id"], u["reading_id"], u["unit_no"], u.get("units_tag"))
            lines += [_continuation_detail(ws, c, handle) for c in follow if c["continuation_of"] == u["unit_no"]]
    return lines


def tool_find_documents(ws: Workspace, query: str, page: int | None = None) -> str:
    """The set a question is about: every visible document containing all the terms that define it (in its title
    or text). The server keeps the whole set as the turn's scope; the model reads it a page at a time. A file of
    several units matches by unit (KTD7, R11): the terms must all be inside one unit of it; its units that match are
    listed with their pages and identifiers, and a property that appears only as a comparable is said to be one."""
    from app.platform.search import _scope_terms, units_matching

    query = (query or "").strip()
    if len(query) < 2:
        raise ToolError("שאילתת תחום ריקה")
    with tenant_tx(ws.ctx) as conn:
        matches = units_matching(conn, query)
        files = unread_matching(conn, query)
    found = matches.documents
    ws.name_unread(files)
    block = unread_text.tool_block(files)
    notes = [MSG_SPLIT_TERMS.format(title=_txt(d["title"]), n=d["units"]) for d in matches.split]
    notes += [MSG_ONLY_COMPARABLE.format(title=_txt(u["file_title"]), label=_txt(u["title"] or MSG_NO_TITLE),
                                         pages=_unit_pages(u["page_first"], u["page_last"])) for u in matches.comparables]
    extra = "".join(f"\n\n{n}" for n in notes) + (f"\n\n{block}" if block else "")
    total = len(found)
    page, pages = _paginate(page, total, SCOPE_PAGE)
    if ws.scope is None or ws.scope.get("query") != query:
        ws.scope = new_scope(query, [(d["document_id"], d["title"]) for d in found], pages)
    ws.scope["pages_read"].add(page)
    shown = found[(page - 1) * SCOPE_PAGE:page * SCOPE_PAGE]
    for d in shown:
        ws.touch(d["document_id"], d["title"], "located")
    if not found:
        return f'לא נמצאו מסמכים שמכילים את כל המונחים של "{query}". אפשר לנסות מונחים אחרים או פחות מונחים.' + extra
    n_terms = len(_scope_terms(query))
    in_title = sum(1 for d in found if len(d["in_title"]) == n_terms)
    lines = [f'תחום: מסמכים שמכילים את כל המונחים "{_txt(query)}" (בכותרת או בתוכן)',
             _page_line(page, pages, total, "מסמכים מתאימים"),
             f"מתוכם {in_title} שכל המונחים בכותרתם; השאר מזכירים אותם בתוכן בלבד.",
             "אלה כל המסמכים בתחום; תשובה על התחום צריכה לבדוק את כולם או לומר אילו לא נבדקו."]
    for d in shown:
        why = []
        if d["in_title"]:
            why.append("בכותרת: " + ", ".join(d["in_title"]))
        if d["in_text"]:
            why.append(f"בתוכן: {d['hits']} קטעים")
        lines.append(f'- {ws.doc_handle(d["document_id"])} document_id={d["document_id"]} | "{_txt(d["title"])}" | '
                     + "; ".join(why))
    if page < pages:
        lines.append(f"הרשימה חלקית: זה עמוד {page} מתוך {pages}.")
    out = _listing(ws, f'מסמכים בתחום "{query}"', lines, [(d["document_id"], d["title"]) for d in shown],
                   key=("find_documents", query), criterion=f'מסמכים שמכילים את כל המונחים "{query}"', total=total,
                   page=page, pages=pages)
    # the units are beside the listing, not in it: a count over the set cites the documents, never a page number
    units = [u for d in shown for u in d["units"]]
    if units:
        out += "\n\n" + "\n".join([MSG_UNITS_LISTED, *_units_with_continuations(ws, shown)]
                                    + ([MSG_SEVERAL_UNITS] if len(units) > 1 else []))
    return out + extra


OUTLINE_MAX = 150  # sections listed in one outline
OUTLINE_INLINE_UNITS = 3  # a file of at most this many units is outlined whole; a larger one by unit (R15)
UNIT_KIND_WORDS = {**contexts.UNIT_KIND_MARKS, "appraisal": "שומה", "document": "מסמך"}
MSG_UNITS_HEAD = "יחידות בקובץ (כל שומה או נספח הם יחידה; סעיפים נפתחים לפי יחידה):"
MSG_UNITS_CHOOSE = ("הקובץ כולל {n} יחידות: סעיפי יחידה ממפה את outline עם unit=U# (או document=U#); חיפוש ביחידה — "
                    "search עם unit=U#; עמודים ביחידה — read עם pages ו-document=U#.")
MSG_UNIT_EDGE = "גבול לא ודאי (נקבע על סמך סימן אחד): קריאה וחיפוש ביחידה מגיעים עד הגבול הוודאי הבא"


def _unit_line(ws: Workspace, cx, number: int, document_id) -> str:
    """One line per unit for the outline: the context as everywhere else (number, label, pages), its handle and stable
    id, its kind, and whether its opening edge is certain."""
    seg = cx.segment(number)
    edge = f" | {MSG_UNIT_EDGE}" if seg.certainty == "probable" else ""
    title = "" if seg.title else f" ({MSG_NO_TITLE})"
    handle = unit_handle(ws, cx, number, document_id)
    ws.listed_units.add(handle)  # a unit the model was shown may be declared in a clarification (round 15 U6)
    return (f"{cx.describe(number)}{title} | {handle} unit_id={cx.key(number)} | "
            f"{UNIT_KIND_WORDS.get(seg.kind, seg.kind)}{edge}")


def tool_outline(ws: Workspace, document: str, unit: str | None = None) -> str:
    """A document's sections (sub-sections included) and tables as handles ``read`` opens, each with its pages,
    its size and its unread regions. A file of several units lists them (U#) first; with ``unit`` (or a U# as the
    document) only that unit's sections and tables are listed (KTD7, R15), so a long file is never cut at
    ``OUTLINE_MAX``; a file of more than ``OUTLINE_INLINE_UNITS`` units lists only its units until one is chosen."""
    with tenant_tx(ws.ctx) as conn:
        ref = str(document or "").strip()
        by_unit = unit or (ref if (ws.handles.get(ref) or {}).get("kind") == "U" else None)
        if by_unit is None and ref:  # the conversation's unit, when the document asked for is its file (KTD8)
            by_unit = focus_unit_for(ws, ref)
            ref = by_unit or ref
        if by_unit:
            v, cx, number = _unit_of(conn, ws, by_unit)
            named = (ws.handles.get(ref) or {}).get("document_id", ref) if ref and ref != by_unit else ""
            if named and named != str(v.document_id):  # a document and a unit of another one
                raise ToolError(MSG_UNIT_ARGS)
        else:
            v = _document(conn, ws, document)
            cx, number = contexts_of(ws, conn, v.version_id, v.reading_id), None
        whole_units = cx.multi and number is None
        sections, tables = reader.outline(conn, v.version_id, cx) if not (
            whole_units and len(cx.numbers) > OUTLINE_INLINE_UNITS) else ([], [])
    if number is not None and cx.multi:
        sections = [e for e in sections if e.context == number]
        tables = [t for t in tables if any(g["context"] == number for g in t["contexts"])]
    ws.touch(v.document_id, v.title, "located", v.partial)
    d = ws.doc_handle(v.document_id)
    head = (f'מבנה המסמך "{_txt(v.title)}" ({d}; ' + (f"{v.page_count} עמודים; " if v.page_count else "")
            + f"גרסה {v.version_id}; קריאה {_txt(v.reading_id or '')}):")
    out = [head]
    if cx.multi and number is not None:
        out.append(f"סעיפי היחידה {unit_handle(ws, cx, number, v.document_id)} בלבד:")
    elif whole_units and len(cx.numbers) > OUTLINE_INLINE_UNITS:
        return "\n".join([*out, MSG_UNITS_HEAD, *(_unit_line(ws, cx, n, v.document_id) for n in cx.numbers),
                          MSG_UNITS_CHOOSE.format(n=len(cx.numbers))])
    if not sections:
        return "\n".join(out) + "\nלמסמך לא נשמרו קטעים לפי מבנה; אפשר לחפש בו (search עם document_ids)."
    if v.partial:
        out.append("המסמך נקרא חלקית: בסעיפים שמסומנים בהם אזורים שלא נקראו ייתכן מידע שלא נקרא.")
    if cx.multi:  # a file holding several appraisals: each context's sections apart (KTD7)
        out.append(_context_note(ws, cx))
    out.append("סעיפים:")
    shown = None  # the context segment whose sections are being listed
    for e in sections[:OUTLINE_MAX]:
        seg = cx.segment_at(e.first) if e.context is not None else None
        if seg is not None and seg is not shown:
            out.append(_unit_line(ws, cx, e.context, v.document_id))  # the context as before, with its handle
            shown = seg
        h = _section_handle(ws, v, e.path, e.window)
        extra = (f", {e.unread} אזורים שלא נקראו" if e.unread else "") + (
            f", {e.uncertain} קטעים בקריאה לא ודאית" if e.uncertain else "")
        pages = _pages_label(sorted(e.pages)) + ", " if e.pages else ""
        later = seg is not None and seg is not cx.segments[0]
        out.append("  " * max(len(e.path) - 1, 0) + f"- {h} «{_txt(_section_name(e.path, later))}» — {pages}"
                   f"{e.chars:,} תווים{extra}")
    if len(sections) > OUTLINE_MAX:
        out.append(f"(ועוד {len(sections) - OUTLINE_MAX} סעיפים; אפשר לפתוח עמודים ב-pages)")
    if tables:
        out.append("טבלאות:")
        for t in tables:
            h = _table_handle(ws, v.document_id, v.version_id, v.reading_id, t["table_index"])
            where = (f"עמוד {t['page']}, " if t["page"] else "")
            line = (f"- {h} «{_txt(t['caption'] or 'טבלה')}» — {where}{t['rows']} שורות"
                    + (f", בסעיף «{_txt(t['section'])}»" if t["section"] else ""))
            if len(t["contexts"]) > 1:  # rows of several contexts, merged into one table by the extraction
                line += "; " + "; ".join(
                    f"שורות {g['first']}–{g['last']}{_rows_pages(g['pages'])}: הקשר {g['context']}" for g in t["contexts"])
            elif t["contexts"]:
                line += f" (הקשר {t['contexts'][0]['context']})"
            out.append(line)
    out.append("פתיחה: read עם section=§# או table=T#; עמודים: read עם pages (document=" + d + ").")
    return "\n".join(out)


def _rows_pages(pages) -> str:
    """" (עמודים 3–4)" for the distinct pages of a run of rows; nothing without pages."""
    pages = sorted(set(pages))
    return f" ({_pages_label(pages)})" if pages else ""


# --- inspect: a visual reading of a region or a page (KTD9) -------------------------------------------------------

MSG_INSPECT_TARGET = "יש לבחור region=R# (אזור שסומן בקריאה), או document (D#) ו-page — לא את שניהם"
MSG_INSPECT_UNKNOWN = "מזהה לא מוכר: {value}. נדרש R# של אזור שסומן בתוצאת read בתור הזה."
MSG_INSPECT_NOT_PDF = "קריאה חזותית אפשרית רק לעמוד או לאזור של מסמך PDF"
MSG_INSPECT_NO_CLOUD = ("קריאה חזותית אינה זמינה במשרד הזה: המשרד אינו מאפשר שליחת תוכן מסמכים למודל בענן, ולכן "
                        "{what} לא נקרא. אם התשובה תלויה בו — ציין שהוא לא נקרא.")
MSG_INSPECT_CAP = ("מגבלה: בתור הזה כבר נעשו {n} קריאות חזותיות, המספר המרבי לתור. {what} לא נקרא. אם התשובה תלויה "
                   "בו — ציין שהוא לא נקרא בשל מגבלת הקריאות החזותיות.")
MSG_INSPECT_TIME = ("מגבלה: הזמן לקריאה בתור הזה כמעט הסתיים, ולכן {what} לא נקרא בקריאה חזותית. ענה ממה שכבר "
                    "נקרא, ואם התשובה תלויה בו — ציין שהוא לא נקרא בשל מגבלת הזמן.")
MSG_INSPECT_FAILED = "הקריאה החזותית של {what} נכשלה ({status}); הוא נשאר לא נקרא. אפשר לציין שהוא לא נקרא."
MSG_INSPECT_RENDER = "לא ניתן היה להציג את {what} כתמונה; הוא נשאר לא נקרא."
INSPECT_LIMIT = "inspect_cap"


def inspect_reader(ctx: TenantContext):
    """The office's vision reader for ``inspect``: only when the office allows its document content to reach the
    cloud model (the same consent as ingestion's visual reading); None otherwise."""
    from app.platform.pipeline import vision_reader

    return vision_reader(ctx)


@dataclass
class _Spot:
    """What an inspection reads: a block's region (``block`` with a box) or a whole page."""

    v: reader.Version
    region: str  # the stored key: block:<index> | page:<number>
    page: int
    bbox: list[float] | None
    what: str  # how it is named to the model
    block: Any = None
    handle: str | None = None  # the R# it was asked by


def _audit_view(ws: Workspace, conn: Connection, v: reader.Version, region: str) -> None:
    """One ``source_view`` audit row per turn and document, whatever the turn inspects in it."""
    from app.audit import audit

    if str(v.document_id) not in ws.audited:
        audit(conn, "source_view", ws.ctx.user_id, "document_version", v.version_id, tool="inspect", region=region)
        ws.audited.add(str(v.document_id))


def _inspected(ws: Workspace, spot: _Spot, key: tuple, *, text_: str, body: list[str], kind: str, status: str,
               method: str | None, location: str, table_index: int | None = None) -> Source:
    v = spot.v
    tags = {"document": ws.doc_handle(v.document_id)} | ({"region": spot.handle} if spot.handle else {})
    index = spot.block.block_index if spot.block is not None else None
    s = ws.add_source(document_id=v.document_id, version_id=v.version_id, title=v.title,
                      section=getattr(spot.block, "section", None), location=location, kind=kind, text=text_,
                      block_start=index, block_end=index, table_index=table_index,
                      page_list=[spot.page] if spot.page else None,
                      partial_document=v.partial, reading_id=v.reading_id, status=status, clipped=False, more=None,
                      body="\n".join(body + [text_]), tags=tags, method=method)
    ws.once(s, key)
    ws.touch(v.document_id, v.title, "retrieved", v.partial)
    return s


def _stored_region(ws: Workspace, conn: Connection, spot: _Spot) -> Source:
    """A region ingestion read (with or without certainty): its stored reading, no model call."""
    r = spot.block
    st = reader.table_structure(conn, spot.v.version_id, r.table_index) if r.table_index is not None else None
    uncertain = r.status == reader.UNCERTAIN
    status = "uncertain_reading" if uncertain else "complete"
    head = [f"מצב: {STATUS_LABELS[status]}; {spot.what} נקרא בעיבוד המסמך ({r.method or 'לא ידוע'}): זו הקריאה "
            "השמורה, בלי קריאה חזותית נוספת"]
    if uncertain and r.note:
        head.append(f"קריאה לא ודאית: {r.note}")
    return _inspected(ws, spot, ("inspect", str(spot.v.version_id), spot.region),
                      text_=_table_text(st, st.get("rows") or []) if st else (r.text or ""), body=head,
                      kind="table" if st else "image", status=status, method=r.method,
                      location=_location(r.section, "table" if st else "image", [spot.page] if spot.page else None,
                                         None, None,
                                         (None, None), r.media),
                      table_index=r.table_index if st else None)


def _spot(ws: Workspace, conn: Connection, target: dict) -> tuple[_Spot, bool]:
    """What the target names, under the current permissions, and whether ingestion read it (its stored reading
    is then the answer)."""
    region = str(target.get("region") or "").strip()
    document, page = target.get("document"), target.get("page")
    if bool(region) == bool(document or page is not None) or (not region and (not document or page is None)):
        raise ToolError(MSG_INSPECT_TARGET)
    if region:
        data = ws.handles.get(region)
        if data is None or data["kind"] != "R":
            raise ToolError(MSG_INSPECT_UNKNOWN.format(value=region))
        v = _bound(conn, ws, region, data)
        rows = reader.blocks_between(conn, v.version_id, data["block_index"], data["block_index"], limit=1)
        if not rows:
            raise ToolError(MSG_STALE_HANDLE.format(handle=region))
        r = rows[0]
        read_at_ingestion = r.status in ("read", reader.UNCERTAIN) and bool((r.text or "").strip())
        if not read_at_ingestion and (not v.is_pdf or not r.page):
            raise ToolError(MSG_INSPECT_NOT_PDF)
        bbox = [float(x) for x in r.bbox] if r.bbox and len(r.bbox) == 4 else None
        if bbox is None and not read_at_ingestion:  # a region without a box: its whole page is read
            return _Spot(v, f"page:{r.page}", r.page, None, f"עמוד {r.page}", None, region), False
        return _Spot(v, f"block:{r.block_index}", r.page or 0, bbox, f"האזור {region}", r, region), read_at_ingestion
    v = _document(conn, ws, document)
    if not v.is_pdf:
        raise ToolError(MSG_INSPECT_NOT_PDF)
    try:
        page = int(page)
    except (TypeError, ValueError):
        raise ToolError("מספר עמוד לא תקין") from None
    if page < 1 or (v.page_count and page > v.page_count):
        raise ToolError(f"מספר עמוד לא תקין: למסמך יש {v.page_count} עמודים" if v.page_count else "מספר עמוד לא תקין")
    rows = reader.blocks_on_pages(conn, v.version_id, page, page)
    read_at_ingestion = (not reader.unread_pages(conn, v.version_id, page, page)
                         and any((r.text or "").strip() for r in rows)
                         and not any(r.status == reader.UNREAD or (r.status == reader.UNCERTAIN
                                                                   and not (r.text or "").strip()) for r in rows))
    return _Spot(v, f"page:{page}", page, None, f"עמוד {page}"), read_at_ingestion


def inspect_config(vision) -> str:
    """The configuration component of an inspection's key (``region_readings.model_config``, KTD11): the vision
    model and the OCR languages a visual reading's numbers are checked with (``_crop_ocr``)."""
    from app.extraction import regions

    return regions.model_config(vision, get_settings().ocr_languages)


def _cached(conn: Connection, spot: _Spot, config: str):
    from app.extraction.images import PictureReading
    from app.extraction.vision import INSPECT_READER_VERSION

    row = conn.execute(text(
        "SELECT reading FROM region_readings WHERE version_id = :v AND reading_id = :r AND region = :g"
        " AND reader_version = :rv AND model_config = :m"),
        {"v": spot.v.version_id, "r": spot.v.reading_id or "", "g": spot.region, "rv": INSPECT_READER_VERSION,
         "m": config}).first()
    if row is None:
        return None
    return PictureReading.from_json(row.reading)


MSG_VISION_TABLE = ("טבלה {handle} (קריאה חזותית{which}): {rows} שורות. ערך מתא שלה נלקח ב-take_value מ-{sid} עם row "
                    "ו-column{table}; הוא מאומת רק כש-OCR של אותו חיתוך רואה את המספר בשורה ובעמודה של התא, ואחרת "
                    "הוא לא ודאי.")
MSG_VISION_TABLE_READ = ("{handle} היא טבלה שנקראה במלואה בקריאה חזותית ב-{sid}: אין בה עוד מה לקרוא. ערך ממנה נלקח "
                         "ב-take_value מ-{sid} (row ו-column).")


def _visual(ws: Workspace, spot: _Spot, reading, earlier: bool) -> Source:
    """A visual reading as the turn's source: always ``uncertain_reading`` (a model's transcription, not the
    document's own text), citable, at its page and region, with nothing more to read. Each table it transcribed is
    a table source of its own (a vision ``T#``, KTD6): its cells are taken from this source, through the reading."""
    from app.extraction.vision import reading_text

    head = [f"מצב: {STATUS_LABELS['uncertain_reading']}; קריאה חזותית של {spot.what} (תמלול של מודל מתמונת העמוד, "
            "לא טקסט המסמך עצמו)" + ("; נקראה כבר בתור קודם, בלי קריאה נוספת" if earlier else "")]
    if reading.status == "read_uncertain" and reading.note:
        head.append(f"קריאה לא ודאית: {reading.note}")
    if reading.status == "no_text":
        head.append("אין בו טקסט קריא" + (f": {reading.note}" if reading.note else ""))
    where = f"עמוד {spot.page}, " + ("אזור בעמוד" if spot.bbox is not None else "העמוד כולו") + " (קריאה חזותית)"
    text_ = reading_text(reading)
    s = _inspected(ws, spot, ("inspect", str(spot.v.version_id), spot.region), text_=text_,
                   body=head, kind="image", status="uncertain_reading", method="vision", location=where)
    v = spot.v
    handles = [ws.handle("T", ("vision", str(v.version_id), spot.region, i), document_id=str(v.document_id),
                         version_id=str(v.version_id), reading_id=v.reading_id, table_index=None,
                         vision={"region": spot.region, "table": i, "source": s.sid})
               for i in range(len(reading.tables))]
    s.vision = {"region": spot.region, "page": spot.page, "reading": reading, "tables": handles}
    if handles:
        s.tags["table"] = " ".join(handles)
        many = len(handles) > 1
        head += [MSG_VISION_TABLE.format(handle=h, which=f", טבלה {i + 1} מתוך {len(handles)}" if many else "",
                                         rows=len(t.rows), sid=s.sid, table=f" ו-table={h}" if many else "")
                 for i, (h, t) in enumerate(zip(handles, reading.tables, strict=True))]
        s.body = "\n".join(head + [text_])
    return s


def _crop_ocr(png: bytes) -> list[dict] | None:
    """The confident OCR words of a rendered crop with their boxes (in the pixels of the picture OCR read: the crop
    enlarged by ``images.ocr_upscale``), which a visual reading's numbers and table cells are checked against (as at
    ingestion, KTD6); None when OCR is not available or fails."""
    import io

    from PIL import Image

    from app.extraction import images
    from app.extraction.ocr import ocr_available

    languages = get_settings().ocr_languages
    if not ocr_available(languages):
        return None
    try:
        words = images._ocr_words(Image.open(io.BytesIO(png)).convert("L"), languages)
    except Exception:  # noqa: BLE001 - no OCR check leaves the reading uncertain, never fails the tool
        return None
    return images.confident_words(words) if words is not None else None


def _layer_words(conn: Connection, version_id, page: int, crop: list[float] | None) -> list[tuple[str, list[float]]]:
    """The region's own text layer: the stored words (``document_blocks.spans``, rendered frame) of the page whose
    centre lies in the crop (the whole page without one), as (text, box in points)."""
    from app.extraction.geometry import Span

    out = []
    for r in conn.execute(text("SELECT text, spans FROM document_blocks WHERE version_id = :v AND page = :p"
                               " AND spans IS NOT NULL"), {"v": version_id, "p": page}):
        for raw in r.spans or []:
            try:
                sp = Span.from_json(raw)
            except (TypeError, ValueError, IndexError):
                continue
            word = (r.text or "")[sp.start:sp.end].strip()
            cx, cy = (sp.box[0] + sp.box[2]) / 2, (sp.box[1] + sp.box[3]) / 2
            if word and (crop is None or (crop[0] <= cx <= crop[2] and crop[1] <= cy <= crop[3])):
                out.append((word, list(sp.box)))
    return out


def _in_pixels(words: list[tuple[str, list[float]]], frame: dict) -> list[dict]:
    """Text-layer words (rendered-frame points) in the pixels OCR's words are in, so both pass the same placement
    test (``images.cell_evidence``)."""
    per_point = frame["scale"] * frame["upscale"]
    ox, oy = frame["origin"]
    return [{"text": w, "conf": 100.0, "left": (b[0] - ox) * per_point, "top": (b[1] - oy) * per_point,
             "width": (b[2] - b[0]) * per_point, "height": (b[3] - b[1]) * per_point} for w, b in words]


def _vision_read(ws: Workspace, spot: _Spot):
    """The visual reading of a spot through the inspect path: the stored reading of the same version, reading,
    region, reader, model and OCR languages, or one new model call (capped per turn and cut to the turn's reading
    deadline, never a costlier model). A new reading is stored only when OCR of its crop ran (``_crop_ocr``): one
    made without that evidence can never have its cells confirmed, so it serves this turn only and a later inspect
    reads again. Returns ``(reading, earlier)``, or the message saying why no reading was made (the cap or the
    time); a refusal (no cloud reading, a page that cannot be rendered, a failed call) is a ``ToolError``."""
    import io

    from PIL import Image

    from app.extraction import regions
    from app.extraction.images import VISION_MAX_SIDE, VisionCallFailed, ocr_upscale
    from app.extraction.render import RenderError, render_region
    from app.extraction.vision import INSPECT_READER_VERSION, transcribe
    from app.platform.storage import get_storage

    with tenant_tx(ws.ctx) as conn:
        vision = inspect_reader(ws.ctx)
        if vision is None:
            raise ToolError(MSG_INSPECT_NO_CLOUD.format(what=spot.what))
        config = inspect_config(vision)
        cached = _cached(conn, spot, config)
        if cached is not None:
            _audit_view(ws, conn, spot.v, spot.region)
            return cached, True
        cap = get_settings().chat_max_inspections
        if ws.inspections >= cap:
            if INSPECT_LIMIT not in ws.limits_hit:
                ws.limits_hit.append(INSPECT_LIMIT)
            return MSG_INSPECT_CAP.format(n=cap, what=spot.what)
        if ws.read_until is not None and ws.read_until - time.monotonic() < get_settings().chat_inspect_reserve_seconds:
            # a vision call now would end past the reading window and leave no time to verify the answer
            if TIME_LIMIT not in ws.limits_hit:
                ws.limits_hit.append(TIME_LIMIT)
            return MSG_INSPECT_TIME.format(what=spot.what)
        ws.inspections += 1
        # the stored box is in the text reader's frame; the crop is cut in the rendered page's frame (rotation,
        # CropBox offset), exactly as the region view cuts it
        crop = display_box_of(conn, spot.v.version_id, spot.page, spot.bbox) if spot.bbox else None
        layer = _layer_words(conn, spot.v.version_id, spot.page, crop)
    # rendered and read outside any transaction: a model call never holds one open
    try:
        shot = render_region(get_storage().get(spot.v.storage_key), spot.page, crop,
                             scale=regions.READ_DPI / 72, max_side=VISION_MAX_SIDE)
    except RenderError:
        raise ToolError(MSG_INSPECT_RENDER.format(what=spot.what), key="read_failed") from None
    png = shot.png
    # where a pixel of the crop is on the page: the crop is already in the rendered frame, so an OCR box found in it
    # is mapped back with these alone (``anchors.page_box``), never through ``display_box_of`` again (KTD6)
    frame = {"page": spot.page, "origin": [round(shot.origin[0], 4), round(shot.origin[1], 4)],
             "scale": shot.scale, "upscale": ocr_upscale(Image.open(io.BytesIO(png)).size[0])}
    if hasattr(vision, "usage"):
        vision.usage = ws.usage
    ocr = _crop_ocr(png)
    try:
        reading = transcribe(vision, png, deadline=ws.read_until, evidence=True, ocr_boxes=ocr,
                             layer_words=_in_pixels(layer, frame))
    except VisionCallFailed as exc:
        raise ToolError(MSG_INSPECT_FAILED.format(what=spot.what, status=exc.status), key="read_failed") from None
    reading.ocr["frame"] = frame
    with tenant_tx(ws.ctx) as conn:
        v = reader.version(conn, spot.v.version_id)  # access may have changed while the model read
        if v is None:
            raise ToolError(MSG_UNAVAILABLE)
        if v.reading_id != spot.v.reading_id:
            raise ToolError(MSG_STALE_HANDLE.format(handle=spot.handle or spot.what))
        if ocr is None:
            # OCR of the crop was unavailable or failed: the reading has no evidence that could confirm its cells,
            # so it is used in this turn only and never stored — a later inspect reads again, with OCR
            _audit_view(ws, conn, v, spot.region)
            return reading, False
        conn.execute(text(
            "INSERT INTO region_readings (office_id, document_id, version_id, reading_id, region, reader_version,"
            " model_config, page, bbox, status, reading, created_by) VALUES (app_office(), :d, :v, :r, :g, :rv, :m,"
            " :p, CAST(:b AS jsonb), :s, CAST(:x AS jsonb), :u) ON CONFLICT DO NOTHING"),
            {"d": v.document_id, "v": v.version_id, "r": v.reading_id or "", "g": spot.region,
             "rv": INSPECT_READER_VERSION, "m": config, "p": spot.page, "b": json.dumps(spot.bbox),
             "s": reading.status, "x": reading.to_json(),
             "u": ws.ctx.user_id})
        _audit_view(ws, conn, v, spot.region)
    return reading, False


def tool_inspect(ws: Workspace, target: dict) -> str:
    """A visual reading of a region (``R#``) or a page. Permission is resolved again on every call, a stored
    reading included; a reading ingestion made is returned as it is; otherwise the stored visual reading of the
    same version, reading, region, reader and model, or one new model call (capped per turn)."""
    if not isinstance(target, dict):
        raise ToolError(MSG_INSPECT_TARGET)
    with tenant_tx(ws.ctx) as conn:
        spot, read_at_ingestion = _spot(ws, conn, target)
        if read_at_ingestion:
            if spot.block is not None:
                s = _stored_region(ws, conn, spot)
            else:  # the page through the same reader as ``read``
                s = _read_window(ws, conn, spot.v, ("pages", str(spot.v.version_id), spot.page, spot.page), None)
                s.body = (f"{spot.what} נקרא בעיבוד המסמך: זו הקריאה השמורה שלו, בלי קריאה חזותית\n" + s.body)
            _audit_view(ws, conn, spot.v, spot.region)
            return _render_source(s)
    got = _vision_read(ws, spot)
    if isinstance(got, str):
        return got
    reading, earlier = got
    return _render_source(_visual(ws, spot, reading, earlier=earlier))


# --- a focused re-read of an unclear value (R28) ------------------------------------------------------------------

MSG_REREAD_CLEAR = "האזור של הערך נקרא בעיבוד המסמך בקריאה לא ודאית; קריאה חזותית ממוקדת שלו קראה את הערך בבירור."
MSG_REREAD_UNCLEAR = ("האזור של הערך נקרא בקריאה לא ודאית, וגם הקריאה החזותית הממוקדת שלו לא קראה אותו בבירור: "
                      "הערך לא ודאי.")
MSG_REREAD_SPENT = ("האזור של הערך נקרא בקריאה לא ודאית, והוא כבר נקרא שוב {n} פעמים בתור הזה: הערך לא ודאי.")
MSG_REREAD_NONE = "האזור של הערך נקרא בקריאה לא ודאית ואין דרך לקרוא אותו שוב ({why}): הערך לא ודאי."


def _value_blocks(anchor: dict) -> list[int] | None:
    """The blocks a quoted value is in, from its anchor: the cited number's block, else the blocks holding the
    quote; None when the anchor does not say (a table cell, or a quote not located)."""
    if anchor.get("number"):
        return [anchor["number"][0]]
    return list(anchor["blocks"]) if anchor.get("blocks") else None


def _block_spot(v: reader.Version, block) -> _Spot | None:
    """What a re-read of a block reads: its region when it has a box, else its page; None for a block that cannot
    be rendered (not a PDF, or no page)."""
    if not v.is_pdf or not block.page:
        return None
    bbox = [float(x) for x in block.bbox] if block.bbox and len(block.bbox) == 4 else None
    if bbox is None:
        return _Spot(v, f"page:{block.page}", block.page, None, f"עמוד {block.page}")
    return _Spot(v, f"block:{block.block_index}", block.page, bbox, f"האזור של הערך בעמוד {block.page}", block)


def _reread_value(ws: Workspace, v: reader.Version, block, forms: frozenset, key: tuple) -> tuple[bool, str]:
    """One focused visual re-read of the unclear region a value was taken from, through the inspect path, at most
    ``REREADS_PER_VALUE`` times per value (``key``: its locator) per turn. The value is settled only when the re-read
    is clear and writes the value's number; otherwise it stays uncertain. Returns (settled, the note why)."""
    from app.extraction.ocr import ocr_available

    n = ws.rereads.get(key, 0)
    if n >= REREADS_PER_VALUE:
        return False, MSG_REREAD_SPENT.format(n=n)
    if not ocr_available(get_settings().ocr_languages):
        # nothing independent would confirm a new visual reading: the model's own reading is no verification
        return False, MSG_REREAD_NONE.format(why="אין OCR לאימות קריאה חוזרת")
    spot = _block_spot(v, block)
    if spot is None:
        return False, MSG_REREAD_NONE.format(why="אין לו עמוד להצגה")
    ws.rereads[key] = n + 1
    try:
        got = _vision_read(ws, spot)
    except ToolError as e:
        return False, MSG_REREAD_NONE.format(why=str(e))
    if isinstance(got, str):
        return False, MSG_REREAD_NONE.format(why=got)
    from app.extraction.vision import reading_text

    reading, _ = got
    if reading.status == "read" and forms & numbers_in(reading_text(reading)):
        return True, MSG_REREAD_CLEAR
    return False, MSG_REREAD_UNCLEAR


# --- measurements and computation ----------------------------------------------------------------------------

def _compat_key(r) -> tuple:
    return (r.metric_kind, r.unit, r.period, r.vat, (r.area_basis or "").strip(), r.value_role)


def _describe_key(key: tuple) -> str:
    kind, unit, period, vat, basis, role = key
    parts = [KIND_LABELS.get(kind, kind), UNIT_LABELS.get(unit, unit or ""), PERIOD_LABELS.get(period, ""),
             VAT_LABELS.get(vat, ""), f"בסיס שטח: {basis}" if basis else "בסיס שטח לא צוין",
             ROLE_LABELS.get(role, role)]
    return ", ".join(p for p in parts if p)


class _TextRead(NamedTuple):
    """What a listing read of the blocks around a text measurement (``_text_reads``, round 14 U3, KTD5): ``reach`` the bounded
    candidates above its quote, nearest first (None: no context was read for it), ``block_text`` the text of the block holding
    it (its number's place is found in it), ``picture`` its block is a picture's text (the text around it is not read), and
    ``capped`` the listing's cap on context reads was spent before it."""

    reach: datum.Reach | None = None
    block_text: str | None = None
    picture: bool = False
    capped: bool = False


def _measurement_facet(row, structure: dict | None, read: _TextRead | None = None) -> dict:
    """The facet of a stored measurement (round 13 U3, R1, KTD2; round 14 U3, R4, KTD5): a table row's from its table (the row label
    of the body row ``row_index``, the column header its row carries, the caption, the title lines and the notes), read through the
    table structure — not read when that is not stored; a text row's from its quote (the purpose phrase of the sentence holding its
    number, else what the bounded read of the blocks above it accepts, ``read``). A text row read without those blocks, in a family
    that needs a facet, is not read: never ``none``, which would claim a read nobody made."""
    if row.table_index is not None and structure is None:
        return datum.not_read()
    if row.table_index is not None:
        rows = structure.get("rows") or []
        ri = row.row_index
        cells = (rows[ri].get("cells") if ri is not None and 0 <= ri < len(rows) else None) or []
        return _cell_facet(row, cells, structure.get("caption"), structure.get("title"), structure.get("notes") or ())
    return _quote_facet(row, read)


def _row_family(row) -> str:
    """The facet family a stored measurement reads its component as (``datum.facet_family``): its kind, quote and metric."""
    return datum.facet_family(row.metric_kind or "", row.quote or "", row.metric or "")


def _written_of(row) -> str:
    """The number of a stored measurement as its value text writes it (the first number of the text, else empty)."""
    found = _numbers_of(meaning._norm(row.value_text or ""))
    return found[0][0] if found else ""


def _cell_facet(row, cells: list, caption: str | None, title, notes=()) -> dict:
    """The facet of a table row from what it reads of its table: the cells of the row (the first is the row label; three or
    more make a matrix), the column header the row carries, the caption, the title lines and the notes (a note that names the row
    label, round 14 U2)."""
    family = _row_family(row)
    return datum.read_cell(cells[0] if cells else "", getattr(row, "header", None), caption,
                           [x for x in title or [] if x], family=family, matrix=len(cells) >= 3,
                           notes=[str(x) for x in notes or [] if x])


def _quote_sentence(row, read: _TextRead | None, written: str) -> str:
    """The sentence of a stored text measurement that holds its number: the whole sentence of its block when the block is at hand
    (``read.block_text``; a PDF line wrap is no sentence end, round 14 follow-up), else the one in its quote (``datum.sentence_with``)."""
    found = datum.own_sentence(meaning._norm(read.block_text), meaning._norm(row.quote or ""), written) if (
        read is not None and read.block_text and row.quote) else None
    return found or datum.sentence_with(row.quote or "", written)


def _quote_facet(row, read: _TextRead | None = None) -> dict:
    family = _row_family(row)
    written = _written_of(row)
    sentence = _quote_sentence(row, read, written)
    if read is None or not datum.needs_facet(family) or datum.own_facet(sentence, written, family) is not None:
        # no blocks at hand, a family that needs none, or an own sentence that settles it (a phrase, a label, a description):
        # nothing above it is read (round 14 U2 follow-up)
        return datum.read_quote(sentence, written, family, None)
    if read.picture:
        return datum.not_read(datum.REASON_PICTURE_CELL)
    if read.capped:
        return datum.not_read(datum.REASON_CAP)
    return _read_quote_placed(sentence, written, family, read.reach)


def _listed_facet(row, notes=(), read: _TextRead | None = None) -> dict:
    """``_measurement_facet`` of a row that carries what its table gives the facet (the columns of
    ``meaning.TABLE_TEXT_COLUMNS``: whether the table is stored, the cells of its own row, the title lines), with no read of
    the table's structure. ``notes``: the table's notes, which the listing reads for the rows still without a facet; ``read``: the
    blocks around a text row (``_text_reads``)."""
    if row.table_index is None:
        return _quote_facet(row, read)
    if not row.table_stored:
        return datum.not_read()
    return _cell_facet(row, row.row_cells or [], row.caption, row.title_lines, notes)


def _memoized(ws: Workspace, keys: Iterable[tuple], read: Callable[[], dict]) -> dict:
    """The facet kept in the workspace under one of ``keys``, else ``read()``, kept under all of them only when it is stated
    (round 14 U3, KTD5, R5): a ``none``, ambiguous or not-read result is never kept, so a wider read at the same place later in the
    turn is not answered with the earlier, weaker one."""
    keys = list(keys)
    for key in keys:
        if key in ws.facets:
            return dict(ws.facets[key])
    found = read()
    if found["status"] == datum.STATUS_STATED:
        for key in keys:
            ws.facets[key] = found
    return found


def _facet_memo(ws: Workspace, key: tuple, read: Callable[..., dict], *args, **kwargs) -> dict:
    """The facet kept under ``key`` in the workspace, else ``read(*args, **kwargs)``, kept only when it is stated."""
    return _memoized(ws, [key], lambda: read(*args, **kwargs))


def _span_facet_key(span: tuple, family: str) -> tuple:
    """The workspace key of a facet by the source span of its number and its family (round 14 U3, KTD5): a value (V#) and a
    stored measurement (M#) of one number share it."""
    return ("facet", *span, family)


def _measurement_facet_of(ws: Workspace, conn: Connection | None, row, read: _TextRead | None = None, *, notes=(),
                          span: tuple | None = None) -> dict:
    """``_measurement_facet`` memoized in the workspace, by the measurement's id and by the span of its number when known (shared
    with a value taken at the same place). A row that carries its table's text (the listing's own columns,
    ``meaning.TABLE_TEXT_COLUMNS``) is read from them; any other table row is read with the turn's cached table structure
    (``_table_at``) when a connection is at hand, and is otherwise not read (and not memoized). A text row that comes with no
    ``read`` of its blocks has them read now (one query) when a connection is at hand; ``span``: the number's place, from the
    block of that read."""
    mid = ws.mid_of(row.id)
    span = span or (ws.spans.get(mid) if mid else None)
    family = _row_family(row)
    keys = [("m", str(row.id)), *([_span_facet_key(span, family)] if span else [])]

    def reading() -> dict:
        own = read
        if own is None and conn is not None and row.table_index is None:
            own = _text_reads(ws, conn, [row]).get(row.id)
            if own is not None and mid and mid not in ws.spans and (placed := measurement_span(row, own.block_text)):
                ws.spans[mid] = placed
        if hasattr(row, "table_stored"):
            return _listed_facet(row, notes, own)
        structure = None
        if row.table_index is not None and conn is not None:
            structure = _table_at(ws, conn, row.version_id, row.table_index)[0]
        return _measurement_facet(row, structure, own)

    return _memoized(ws, keys, reading)


def _text_reads(ws: Workspace, conn: Connection, rows: list) -> dict:
    """The blocks around the text measurements of ``rows``, read in ONE query (round 14 U3, KTD1, KTD5): for each row that needs
    a facet (a family that needs one, its own sentence states none, none kept for it yet) the blocks above its own, down to the
    first of its appraisal unit — the bounded candidates of its context read; for each row whose number's place is not known, its own
    block, whatever the facet cap. At most ``MEASUREMENT_FACET_ROWS`` rows read their context per call: the rest are ``capped``.
    Returns ``{row id: _TextRead}``; a table row, and a text row with no block or no quote, has none."""
    wanted = []
    for r in rows:
        if r.table_index is not None or getattr(r, "block_index", None) is None or not r.quote:
            continue
        mid = ws.mid_of(r.id)
        family = _row_family(r)
        written = _written_of(r)
        context = (datum.needs_facet(family) and ("m", str(r.id)) not in ws.facets
                   and datum.own_facet(datum.sentence_with(r.quote, written), written, family) is None)
        if context or mid is None or mid not in ws.spans:
            wanted.append((r, context))
    if not wanted:
        return {}
    budget, plan = MEASUREMENT_FACET_ROWS, []
    for r, context in wanted:
        cx = contexts_of(ws, conn, r.version_id, getattr(r, "reading_id", None))
        seg = cx.segment_at(r.block_index) if cx.multi else None
        floor = seg.first if seg is not None else 0
        reads_context = context and budget > 0
        budget -= 1 if reads_context else 0
        plan.append((r, cx, context, not reads_context and context,
                     max(floor, r.block_index - CANDIDATE_BLOCKS) if reads_context else r.block_index))
    got = conn.execute(text(
        "SELECT version_id, block_index, kind, section_path, text FROM document_blocks WHERE EXISTS (SELECT 1 FROM"
        " unnest(CAST(:v AS uuid[]), CAST(:a AS int[]), CAST(:z AS int[])) AS w(version_id, lo, hi) WHERE"
        " w.version_id = document_blocks.version_id AND document_blocks.block_index BETWEEN w.lo AND w.hi)"
        " ORDER BY version_id, block_index"),
        {"v": [str(r.version_id) for r, *_ in plan], "a": [lo for *_, lo in plan],
         "z": [r.block_index for r, *_ in plan]}).all()
    blocks: dict[str, dict[int, Any]] = {}
    for b in got:
        blocks.setdefault(str(b.version_id), {})[b.block_index] = b
    out: dict = {}
    for r, cx, context, capped, lo in plan:
        mine = blocks.get(str(r.version_id), {})
        holder = mine.get(r.block_index)
        if holder is None:
            out[r.id] = _TextRead()  # its block is not stored (any more): nothing was read
        elif not context:
            out[r.id] = _TextRead(None, holder.text)
        elif holder.kind == "image":  # the text of a picture: the text around it is not what it was read with
            out[r.id] = _TextRead(None, holder.text, picture=True)
        elif capped:
            out[r.id] = _TextRead(None, holder.text, capped=True)
        else:
            path = tuple(holder.section_path or ())
            window = reader.section_window(cx, r.block_index, path)
            first = window[0] if window is not None else 0
            above = [mine[i] for i in range(max(first, lo), r.block_index) if i in mine]
            out[r.id] = _TextRead(datum.reach([*above, holder], r.block_index, r.quote, path=path, lo=first,
                                              chars=CONTEXT_CHARS["candidates"], truncated=len(above) >= CANDIDATE_BLOCKS),
                                  holder.text)
    return out


def _table_notes(conn: Connection, rows: list) -> dict:
    """The notes of the tables of ``rows`` (the table measurements the listing could not give a component yet), in ONE query:
    ``{(version id, table index): [note, ...]}``."""
    pairs = sorted({(str(r.version_id), r.table_index) for r in rows})
    if not pairs:
        return {}
    got = conn.execute(text(
        "SELECT version_id, table_index, structure->'notes' AS notes FROM extracted_tables WHERE EXISTS (SELECT 1 FROM"
        " unnest(CAST(:v AS uuid[]), CAST(:t AS int[])) AS w(version_id, table_index) WHERE"
        " w.version_id = extracted_tables.version_id AND w.table_index = extracted_tables.table_index)"),
        {"v": [p[0] for p in pairs], "t": [p[1] for p in pairs]}).all()
    return {(str(g.version_id), g.table_index): [str(n) for n in g.notes or [] if n] if isinstance(g.notes, list) else []
            for g in got}


def tool_find_measurements(ws: Workspace, query: str, metric_kinds: list[str] | None = None,
                           document_ids: list[str] | None = None, value_roles: list[str] | None = None,
                           page: int | None = None) -> str:
    words = [w for w in re.findall(r"[֐-׿\w\"״׳']+", query or "") if len(w) >= 2]
    with tenant_tx(ws.ctx) as conn:
        docs = _visible_documents(conn, document_ids or [])
        params: dict = {"e": EXTRACTION_VERSION}
        conds = ["v.is_current", "d.deleted_at IS NULL", APPRAISALS_SQL, "m.status <> 'rejected'"]
        if docs:
            params["d"] = docs
            conds.append("m.document_id = ANY(:d)")
        if metric_kinds:
            params["k"] = list(metric_kinds)
            conds.append("m.metric_kind = ANY(:k)")
        if value_roles:
            params["r"] = list(value_roles)
            conds.append("m.value_role = ANY(:r)")
        if words and not metric_kinds:
            params["w"] = [f"%{w}%" for w in words]
            conds.append("(m.metric ILIKE ANY(:w) OR m.quote ILIKE ANY(:w) OR m.subject ILIKE ANY(:w))")
        # the rows carry their table's caption and the header of their column (round 12 U4, KTD14): the caption dates an
        # adopted measurement and both are where a period the quote does not hold is read (`meaning.measurement_states`)
        # (version, table index) is unique in the tables, so the left join adds no row: the count leaves it out
        source = " FROM measurements m JOIN document_versions v ON v.id = m.version_id JOIN documents d ON d.id = m.document_id"
        where = source + meaning.TABLE_TEXT_JOIN + " WHERE " + " AND ".join(conds)
        fields = "SELECT m.*, d.title, v.ingestion->>'reading_id' AS reading_id, " + meaning.TABLE_TEXT_COLUMNS
        if ws.time.mode == "none":
            total = conn.execute(text("SELECT count(*)" + source + " WHERE " + " AND ".join(conds)), params).scalar_one()
            page, pages = _paginate(page, total, MEASUREMENTS_MAX)
            # a total order (the id breaks ties), so no row falls between two pages
            rows = conn.execute(text(
                fields + where + " ORDER BY d.title, d.id, m.block_index NULLS FIRST, m.row_index NULLS FIRST, m.id"
                f" LIMIT {MEASUREMENTS_MAX} OFFSET :o"), params | {"o": (page - 1) * MEASUREMENTS_MAX}).all()
            as_ofs = {r.id: measurement_date(ws, r) for r in rows}
        else:
            # the time mode orders the WHOLE filtered set — ids, quotes and stored dates are read, dated and sorted by the
            # KTD6 order, then by id — and only then is the page cut and its rows fetched by id: the page boundary follows
            # the date order and no row is skipped (KTD7). The listing is the whole filtered set, so its length is the total,
            # and the dates computed here are the page rows' dates (the table caption comes with each row)
            light = conn.execute(text(
                "SELECT m.id, m.version_id, m.table_index, m.quote, m.effective_date, et.structure->>'caption' AS caption"
                + where), params).all()
            total = len(light)
            page, pages = _paginate(page, total, MEASUREMENTS_MAX)
            as_ofs = {r.id: measurement_date(ws, r) for r in light}
            keyed = sorted(((time_key(ws.time, dated(as_ofs[r.id]), bool(as_ofs[r.id] and as_ofs[r.id].get("forecast"))),
                             str(r.id), r.id) for r in light), key=lambda k: k[:2])
            wanted = [k[2] for k in keyed[(page - 1) * MEASUREMENTS_MAX:page * MEASUREMENTS_MAX]]
            fetched = {r.id: r for r in conn.execute(text(fields + where + " AND m.id = ANY(:ids)"),
                                                     params | {"ids": wanted})}
            rows = [fetched[i] for i in wanted if i in fetched]
        scope_rows = conn.execute(text(
            "SELECT d.id, d.title, v.id AS vid, r.state, COALESCE((v.ingestion->>'partial')::boolean, false) AS partial"
            " FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
            " LEFT JOIN measurement_runs r ON r.version_id = v.id AND r.extraction_version = :e"
            f" WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL}" + (" AND d.id = ANY(:d)" if docs else "")),
            params).all()
        cached, cached_total = _cached_listing(conn, docs, metric_kinds, words)
        for vid, reading in {(str(r.version_id), r.reading_id) for r in rows}:
            contexts_of(ws, conn, vid, reading)  # a measurement records its appraisal context (KTD7)
        reads = _text_reads(ws, conn, rows)  # the blocks of the text rows: their context and their number's place, in one query
        # the place of each row's number, found once: a row the turn already holds a place for keeps it (``ws.spans``)
        spans: dict = {}
        facets = {}  # a table row's from its table (round 13 U3), a text row's from its blocks (round 14 U3)
        for r in rows:
            spans[r.id] = None if ws.mid_of(r.id) in ws.spans else measurement_span(
                r, reads[r.id].block_text if r.id in reads else None)
            facets[r.id] = _measurement_facet_of(ws, conn, r, reads.get(r.id), span=spans[r.id])
        bare = [r for r in rows if r.table_index is not None and facets[r.id]["status"] != datum.STATUS_STATED
                and hasattr(r, "table_stored") and r.table_stored
                and datum.needs_facet(_row_family(r))]
        if bare:  # a table row still without a component: the notes of its table, in one query
            noted = _table_notes(conn, bare)
            for r in bare:
                if notes := noted.get((str(r.version_id), r.table_index)):
                    facets[r.id] = _measurement_facet_of(ws, conn, r, notes=notes, span=spans[r.id])
    key = (" ".join(words) if not metric_kinds else "", tuple(sorted(metric_kinds or [])),
           tuple(sorted(str(d) for d in docs)), tuple(sorted(value_roles or [])))
    listing = ws.listings.setdefault(key, {"pages": pages, "pages_read": set(), "total": total})
    listing["pages_read"].add(page)
    groups: dict[tuple, list] = {}
    for r in rows:
        m = ws.add_measurement(r, reads[r.id].block_text if r.id in reads else None, span=spans[r.id])
        m.as_of = as_ofs[r.id]
        m.facet = facets[r.id]
        m.listings.add(key)
        ws.touch(r.document_id, r.title, "read")
        groups.setdefault(_compat_key(r), []).append(m)
    cov = {"documents": len(scope_rows), "extracted": sum(1 for s in scope_rows if s.state == "done"),
           "total": total, "page": page, "pages": pages,
           "partial_extraction": [s.title for s in scope_rows if s.state == "partial"],
           "not_extracted": [s.title for s in scope_rows if s.state not in ("done", "partial")],
           "partially_read": [s.title for s in scope_rows if s.partial]}
    ws.coverage.append(cov)
    out = [f"כיסוי: {cov['extracted']} מתוך {cov['documents']} מסמכים בתחום חולצו לנתונים כמותיים.",
           _page_line(page, pages, total, "נתונים מתאימים")]
    if cov["not_extracted"]:
        out.append("טרם חולצו (נתוניהם לא נכללים כאן; אפשר לחפש בהם בטקסט): "
                   + "; ".join(_txt(t) for t in cov["not_extracted"]))
    if cov["partial_extraction"]:
        out.append("חולצו חלקית: " + "; ".join(_txt(t) for t in cov["partial_extraction"]))
    if cov["partially_read"]:
        out.append("מסמכים שנקראו חלקית (תמונות שלא נקראו): " + "; ".join(_txt(t) for t in cov["partially_read"]))
    if not rows:
        out.append("לא נמצאו נתונים כמותיים מתאימים.")
    if rows and pages > 1:
        out.append("חישוב על כל הנתונים המתאימים דורש לקרוא את כל העמודים; חישוב על חלקם יסומן כחלקי.")
    for key, ms in groups.items():
        out.append(f"\nקבוצה [{_describe_key(key)}] — {len(ms)} ערכים:")
        for m in ms:
            r = m.row
            status = measurement_status(r)
            extra = f" | נושא: {_txt(r.subject)}" if r.subject else ""
            if getattr(r, "stance", None):
                extra += f" | ייחוס: {stance_label(r.stance)}" + (
                    f" של {_txt(r.stated_by)}" if getattr(r, "stated_by", None) else "")
            if m.context:
                extra += f" | הקשר: {_txt(m.context['described'])}"
            if m.facet and m.facet["status"] != datum.STATUS_NOT_READ:
                extra += f" | {_txt(datum.line(m.facet))}"
            issues = f" | הערות: {_txt('; '.join(r.issues))}" if r.issues else ""
            if anchor_lost(r):
                issues += " | המיקום במסמך אבד בעיבוד מחדש: הערך אינו מאומת מול הקריאה הנוכחית של המסמך"
            out.append(f'  {m.mid}: {_txt(r.metric)} = {_txt(r.value_text)} | מסמך: "{_txt(m.title)}"{extra}'
                       f" | {datum_line(m.as_of)} | סטטוס: {status}{issues}\n    ציטוט: {_txt(_clip(r.quote, 300))}")
    out += _cached_lines(ws, cached, cached_total)
    return "\n".join(out)


def _cached_listing(conn: Connection, docs: list, metric_kinds: list[str] | None, words: list[str]) -> tuple[list, int]:
    """Cached verified values (KTD12) of the current readings of the documents the user may see (row security), by
    the same filters as the measurements: the documents, the kinds their source attests, else words of their quote,
    section or column. At most ``CACHED_MAX``, with the total."""
    params: dict = {"verifier_version": VALUE_VERIFIER_VERSION}
    conds = ["d.deleted_at IS NULL", APPRAISALS_SQL, "c.value->>'verifier_version' = :verifier_version"]
    if docs:
        params["d"] = docs
        conds.append("c.document_id = ANY(:d)")
    if metric_kinds:
        params["k"] = list(metric_kinds)
        conds.append("c.value->'record'->>'kind' = ANY(:k)")
    elif words:
        params["w"] = [f"%{w}%" for w in words]
        conds.append("(c.value->'record'->>'quote' ILIKE ANY(:w) OR c.value->'record'->>'section' ILIKE ANY(:w)"
                     " OR c.locator->>'column' ILIKE ANY(:w))")
    where = (" FROM verified_values c JOIN document_versions v ON v.id = c.version_id AND v.is_current"
             " AND v.ingestion->>'reading_id' = c.reading_id JOIN documents d ON d.id = c.document_id WHERE "
             + " AND ".join(conds))
    rows = conn.execute(text(
        "SELECT c.document_id, c.version_id, c.reading_id, c.locator, c.value, d.title" + where
        + f" ORDER BY d.title, d.id, c.created_at, c.locator::text LIMIT {CACHED_MAX}"), params).all()
    if len(rows) < CACHED_MAX:  # the page holds them all: no count needed
        return rows, len(rows)
    return rows, conn.execute(text("SELECT count(*)" + where), params).scalar_one()


def _cached_lines(ws: Workspace, rows: list, total: int) -> list[str]:
    """The listing of cached verified values: each as its ``Q#`` (or the V# the turn already registered for it), with
    its value, document, place and what its source attests."""
    if not rows:
        return []
    out = ["", MSG_CACHED_HEADER]
    if total > len(rows):
        out.append(f"מוצגים {len(rows)} מתוך {total} ערכים שמורים; צמצם לפי מסמך או סוג נתון כדי לראות את השאר.")
    # a record written before the key named the number (round 14 U3, KTD5) is not listed beside the record of the same number
    keyed = {(str(r.version_id), r.reading_id, (r.value.get("record") or {}).get("quote"), (r.value.get("record") or {}).get("value"))
             for r in rows if "at" in r.locator}
    # the V# the turn registered for each place, under the value's own locator and under its cache key (the first V# of a place wins)
    registered: dict[tuple, str] = {}
    for v in ws.values.values():
        for loc in (v.locator, _cache_locator(v.locator, ws.spans.get(v.vid))):
            registered.setdefault(_locator_key(v.version_id, v.reading_id, loc), v.vid)
    for r in rows:
        record = r.value.get("record") or {}
        if "at" not in r.locator and (str(r.version_id), r.reading_id, record.get("quote"), record.get("value")) in keyed:
            continue
        held = registered.get(_locator_key(r.version_id, r.reading_id, r.locator))
        name = held or _cached_handle(ws, r.version_id, r.reading_id, r.document_id, r.locator)
        ws.touch(r.document_id, r.title, "retrieved")
        # the cached fields are strings or absent, and no label table has a None or "" key
        attested = [VALUE_KINDS.get(record.get("kind"), ""), UNIT_LABELS.get(record.get("unit"), ""),
                    PERIOD_LABELS.get(record.get("period"), ""), VAT_LABELS.get(record.get("vat"), ""),
                    f"בסיס שטח: {record['area_basis']}" if record.get("area_basis") else ""]
        if record.get("stance"):
            attested.append(f"ייחוס: {stance_label(record['stance'])}"
                            + (f" של {record['stated_by']}" if record.get("stated_by") else ""))
        says = "; ".join(x for x in attested if x) or "המקור אינו מעיד על משמעותו"
        facet_line = (f" | {_txt(datum.line(record['facet']))}"
                      if (record.get("facet") or {}).get("status") in (datum.STATUS_STATED, datum.STATUS_NONE) else "")
        out.append(f'  {name}{" (כבר נרשם בתור הזה)" if held else ""}: {_txt(record.get("value_text"))} | מסמך: '
                   f'"{_txt(r.title)}" | מקום: {_txt(_cached_where(record))}'
                   + (f" | סעיף: {_txt(record['section'])}" if record.get("section") else "")
                   + f" | המקור מעיד: {_txt(says)}{facet_line} | סטטוס: {STATUS_AUTO}"
                   + f"\n    ציטוט: {_txt(_clip(record.get('quote') or '', 300))}")
    return out


# --- values, assumptions and calculation (KTD10) ---------------------------------------------------------------

VALUE_KINDS = {**KIND_LABELS, "income": "הכנסות", "profit": "רווח"}
_SIGNED = re.compile(r"([-−]\s*)?(\()?\s*(\d[\d,]*(?:\.\d+)?)\s*(\))?")
MSG_VALUE_UNAVAILABLE = ("{ids}: המסמך שממנו נלקח הערך אינו זמין עוד (נמחק, או שאין הרשאה אליו); אי אפשר להשתמש בו "
                         "בחישוב")
MSG_VALUE_STALE = ("{ids}: המסמך שממנו נלקח הערך עובד מחדש מאז שנלקח; הערך אינו תקף עוד. יש לקרוא את המקור מחדש "
                   "ולקחת את הערך שוב")
MSG_UNCERTAIN_INPUTS = "הערכים {ids} אינם ודאיים"
MSG_SOURCE_UNCERTAIN = "המקור נקרא בקריאה לא ודאית"


def value_uncertain(ws: Workspace, vid: str, consulted=None) -> bool:
    """A value (V#) that cannot be presented as certain: its kind or unit is not confirmed by its source (the attributes that define the
    number: ``calc.Value.certainty`` — stated differently there, or supported by nothing; a kind the source is silent about, with the
    unit's family, is not; an asserted stance, speaker, scenario, period, area basis or VAT counts as not stated and leaves it certain,
    round 13 U5, KTD6), its source was read uncertainly (a visual transcription included), or its region stayed unclear after the focused
    re-reads (``Workspace.uncertain_values``). ``consulted``: the (id, attribute) pairs one calculation read (``calc.Outcome.consulted``):
    the value is then uncertain for that calculation only for the unconfirmed attributes it read (round 22 U9, KTD8)."""
    v = ws.values[vid]
    if consulted is None:
        return v.certainty != "verified" or reading_unclear(ws, vid)
    return v.reading == "uncertain" or reading_unclear(ws, vid) or bool(v.unconfirmed(consulted))


def reading_unclear(ws: Workspace, vid: str) -> bool:
    """The reading half of ``value_uncertain``: the value's region stayed unclear after its focused re-reads, or its source was
    read uncertainly and nothing settled the value since. A model-asserted kind or unit is not a reading question."""
    v = ws.values[vid]
    source = ws.sources.get(v.source_id)
    return (vid in ws.uncertain_values
            or (source is not None and source.status == "uncertain_reading" and vid not in ws.settled_values
                and v.reading != "clear"))


def value_status(ws: Workspace, i: str) -> str:
    """The status label of a value (V#) or a measurement (M#) of the turn. A value taken in chat is never a person's
    decision: it is checked automatically, or uncertain."""
    if i in ws.measurements:
        return measurement_status(ws.measurements[i].row)
    return STATUS_UNCERTAIN if value_uncertain(ws, i) else STATUS_AUTO


MSG_ATTR_KIND_DIFFERENT = "סוג הנתון: המקור מציין «{stated}» והחישוב מניח «{assumed}»"
MSG_ATTR_KIND_ASSERTED = "סוג הנתון לא נאמר במקור, והחישוב מניח שהוא «{assumed}»"
MSG_ATTR_KIND_SILENT = "סוג הנתון לא נאמר במפורש בשורה או בכותרת, והחישוב מניח שהוא «{assumed}»"
MSG_ATTR_UNIT_DIFFERENT = "היחידה: המקור מציין יחידה אחרת מ«{assumed}», שבה החישוב משתמש"
MSG_ATTR_UNIT_ASSERTED = "היחידה לא נאמרה במקור, והחישוב מניח {assumed}"


def unconfirmed_attributes(ws: Workspace, vid: str, consulted=None) -> list[str]:
    """The defining attributes (kind, unit) of a value that its source does not confirm — stated differently there, or supported by
    nothing (``calc.UNCONFIRMED``) — and that a calculation read (``consulted``; None: any), when that is the only thing making it
    uncertain: a value whose reading is unclear is uncertain for its reading and has none (round 21 U3, KTD3, R3; round 22 U9, KTD8)."""
    v = ws.values.get(vid)
    if v is None or reading_unclear(ws, vid):
        return []
    return v.unconfirmed(consulted)


def attribute_clauses(ws: Workspace, vid: str, attributes: list[str]) -> list[str]:
    """What the source does not confirm about each of ``attributes`` of a value, in words the user reads (round 22 U9, KTD8): the attribute
    named, what the source says when it says another thing, and what the calculation assumes. Never that the value is not in the source."""
    v = ws.values[vid]
    out = []
    for a in attributes:
        state = v.provenance.get(a)
        if a == "kind":
            assumed = VALUE_KINDS.get(v.kind, v.kind)
            out.append(MSG_ATTR_KIND_DIFFERENT.format(stated=v.stated.get("kind") or "סוג אחר", assumed=assumed) if state == "stated_different"
                       else MSG_ATTR_KIND_SILENT.format(assumed=assumed) if state == "silent"
                       else MSG_ATTR_KIND_ASSERTED.format(assumed=assumed))
        else:
            assumed = UNIT_LABELS.get(v.unit, v.unit) or v.unit
            out.append((MSG_ATTR_UNIT_DIFFERENT if state == "stated_different" else MSG_ATTR_UNIT_ASSERTED).format(assumed=assumed))
    return out


def uncertain_reason(ws: Workspace, vid: str, consulted=None) -> str:
    """Why an uncertain value is uncertain when the reading is clear and its kind or unit is not confirmed by the source: what the source
    says and what the calculation assumes; "" for any other (round 22 U9)."""
    attrs = unconfirmed_attributes(ws, vid, consulted)
    return "; ".join(attribute_clauses(ws, vid, attrs)) if attrs else ""


def uncertain_inputs(ws: Workspace, ids, consulted=None) -> list[str]:
    """The values and measurements among ``ids`` that are uncertain: a calculation resting on any of them is
    conditional, never certain (R28). ``consulted`` (``calc.Outcome.consulted``): a value counts for its kind or unit only when the
    calculation read it (``value_uncertain``); a measurement counts as before."""
    return [i for i in ids if (i in ws.values and value_uncertain(ws, i, consulted))
            or (i in ws.measurements and measurement_uncertain(ws.measurements[i].row))]


def _parse_number(raw: str) -> tuple[str, Decimal] | None:
    """A number as written, tolerant of ₪, %, NBSP, spaces, thousands commas and a sign: ("12,450,000", 12450000).
    None when it holds no number or more than one."""
    s = read_separators(meaning._norm(str(raw or "")).replace(" ", " ").replace(" ", " "), cell=True)
    found = [m for m in _SIGNED.finditer(s) if m.group(3)]
    if len(found) != 1:
        return None
    m = found[0]
    try:
        value = Decimal(m.group(3).replace(",", ""))
    except ArithmeticError:
        return None
    if m.group(1) or (m.group(2) and m.group(4)):  # "-5" / "(5)": a negative amount
        value = -value
    return m.group(3), value


MSG_DATE_CITE = ("תאריך אינו ערך שנרשם: צטט אותו כקטע (S#) שבו הוא כתוב, בלי take_value. ערך מתוארך שהתשובה מציגה "
                 "נרשם כרגיל, והשרת מצרף לו את התאריך שלו")


MSG_UNIT_AMBIGUOUS_WHY = "המקור מזכיר ליד הערך יותר מיחידה אחת ({units}) ואינו קובע איזו מהן שלו"
MSG_UNIT_AMBIGUOUS_TAKEN = ("היחידה של הערך לא ברורה במקור: {why}. הערך נרשם ביחידה שנתת, אפשר לצטט ולהציג אותו, אבל "
                            "אי אפשר לחשב איתו")
MSG_UNIT_AMBIGUOUS_INPUT = ("אי אפשר לחשב עם {inputs}: היחידה של הערך לא ברורה במקור. אל תחשב עם ערך כזה — קח "
                            "מהמסמך ערך שהיחידה שלו כתובה (תא עם כותרת שמציינת יחידה, או משפט), או הצג את הערך בלי חישוב")


def _date_cite() -> ToolError:
    """The refusal of a date offered as a value: it is cited as a passage, not registered."""
    return ToolError(MSG_DATE_CITE, "date_not_registered")


def _not_one_number(raw) -> ToolError:
    """The refusal for a ``number`` that is not one number: a date gets its own message — it is cited, not registered (round 15 U1,
    KTD9, R12)."""
    if platform_dates.parse(str(raw or "")) is not None:
        return _date_cite()
    return ToolError(f"number «{raw}» אינו מספר אחד")


def _header_units(header: str, column_unit: str = "") -> set[str]:
    """The unit a column header states for its cells (round 15 U1, KTD9): ``meaning.units_attested`` over the header and the
    unit read from it (``extraction.tables.units_for``). A money kind per area with no currency ("שווי למ״ר בנוי") is ₪
    per m² — the header names an amount, and the area is its denominator, not its unit."""
    text_ = f"{header} {column_unit}"
    units = meaning.units_attested(text_)
    if units == {"sqm"} and meaning._PER_SQM.search(meaning._norm(text_)) \
            and any(meaning.kind_attested(k, text_) for k in meaning.MONEY_KINDS):
        return {"ILS_per_sqm"}
    return units


_AREA_MARK = re.compile(r"מ\"ר|מטר")


def _label_units(label: str) -> set[str]:
    """The unit a row label states by naming a money quantity per area (round 15 U1 follow-up, KTD9): "שווי מ״ר מבונה", "מחיר
    למ״ר" — a money kind with the area as its denominator, so a cell that states only the currency is ₪ per m². A label that
    holds a number beside the area ("שווי דירה 90 מ״ר") describes a property, not a rate: none."""
    text_ = meaning._norm(label)
    if not any(meaning.kind_attested(k, text_) for k in meaning.MONEY_KINDS) or not _AREA_MARK.search(text_):
        return set()
    if meaning._PER_SQM.search(text_) or not re.search(r"\d", text_):
        return {"ILS_per_sqm"}
    return set()


def _numbers_of(text_: str) -> list[tuple[str, int, int, Decimal]]:
    """Every number written in a text: (as written, start, end, value)."""
    from app.answering.numread import numbers_written

    out = []
    for written, start, end, token in numbers_written(text_ or ""):
        try:  # a decimal comma is read by context (``numread``): "13,2" is 13.2, "1,250" is 1250
            out.append((written, start, end, Decimal(token.rstrip(".,").replace(",", ""))))
        except ArithmeticError:
            continue
    return out


def _key(s) -> str:
    """A row label or a column header for matching: one spelling of the abbreviation marks, one space."""
    return meaning._flat(str(s or "")).strip(" :")


_LOOSE = re.compile(r"[\W_]+")


def _loose(s) -> str:
    """A label for matching when two spellings differ only in spaces, punctuation or bracket marks ("ממוצע (*)" and
    "ממוצע ( *)"): its letters and digits alone (round 15 U1, KTD9). Matching only, never shown."""
    return _LOOSE.sub("", _key(s))


def _label_tiers(wanted: str) -> list[Callable[[str], bool]]:
    """How a label may name an option, strictest first: the same label, the same letters and digits, a label inside
    the option, its letters and digits inside the option's."""
    k, loose = _key(wanted), _loose(wanted)
    return [lambda o: _key(o) == k, lambda o: bool(loose) and _loose(o) == loose,
            lambda o: bool(k) and k in _key(o), lambda o: bool(loose) and loose in _loose(o)]


def _match_one(wanted: str, options: list[str], what: str) -> int:
    """The index of the option a label names: the first tier (``_label_tiers``) with a match decides — the only option
    of it, else a refusal naming the several."""
    for same in _label_tiers(wanted):
        hit = [i for i, o in enumerate(options) if same(o)]
        if len(hit) == 1:
            return hit[0]
        if hit:
            raise ToolError(f"{what} «{wanted}» מתאים לכמה ({', '.join(str(i + 1) for i in hit[:8])}); בחר לפי מספר")
    raise ToolError(f"{what} «{wanted}» לא נמצא בטבלה. האפשרויות: " + "; ".join(f"«{o}»" for o in options[:20] if o))


def _rows_named(rows: list[list[str]], label: str) -> list[int]:
    """The rows a label names (round 15 U1, KTD9, R12): per tier, rows whose first cell it names, else rows with any
    non-numeric cell it names — a group column or an empty first cell does not hide the label. The first tier with
    a hit decides; empty: no row."""
    scopes = (lambda r: r[:1], lambda r: [c for c in r if _parse_number(c) is None])
    for same in _label_tiers(label):
        for cells_of in scopes:
            hit = [i for i, r in enumerate(rows) if any(c and same(c) for c in cells_of(r))]
            if hit:
                return hit
    return []


_ROW_JOIN = " | "
_ROW_TEXT_SHARE = 0.8


def _is_row_text(label) -> bool:
    """Whether a ``row`` is a whole rendered row (cells joined with " | ") and not a label (round 15 U1 follow-up, KTD9)."""
    return _ROW_JOIN in str(label or "")


def _rows_like_text(rows: list[list[str]], text_: str) -> list[tuple[int, float, int]]:
    """The rows a rendered row text matches, best first: (index, share of the given cells found loosely in the row, cells
    equal to one of its cells). A cell is found when its letters and digits lie inside the row's (``_loose``), so OCR
    spaces inside a word, a trailing '*' or an empty cell do not hide the row. A row matches at ``_ROW_TEXT_SHARE`` of the
    given non-empty cells."""
    given = [g for g in (_loose(c) for c in str(text_).split("|")) if g]
    if not given:
        return []
    out = []
    for i, r in enumerate(rows):
        own = [_loose(c) for c in r]
        joined = "".join(own)
        found = sum(1 for g in given if g in joined)
        if found / len(given) >= _ROW_TEXT_SHARE:
            out.append((i, found / len(given), sum(1 for g in given if g in own)))
    return sorted(out, key=lambda x: (-x[1], -x[2], x[0]))


def _row_index(row_number, rows: list[list[str]]) -> int | None:
    """The 0-based row a 1-based ``row_number`` names, or None when none is given or it is outside the table."""
    if row_number is None:
        return None
    at = int(row_number) - 1
    return at if 0 <= at < len(rows) else None


def _row_of_text(rows: list[list[str]], text_: str, row_number) -> int:
    """The row a whole rendered row names (round 15 U1 follow-up, KTD9): the ``row_number`` row when it matches, else the
    only best match; a refusal naming the candidate rows when none matches or several match equally."""
    like = _rows_like_text(rows, text_)
    given = _row_index(row_number, rows)
    if given is not None and any(i == given for i, *_ in like):
        return given
    if like:
        best = [i for i, share, eq in like if (share, eq) == like[0][1:]]
        if len(best) == 1:
            return best[0]
        raise ToolError(f"השורה «{_clip(text_, 80)}» מתאימה לכמה שורות — {_rows_listed(rows, best)}; בחר לפי row_number")
    raise ToolError(f"השורה «{_clip(text_, 80)}» אינה שורה בטבלה"
                    + (f". שורה {given + 1} היא «{_clip(' | '.join(rows[given]), 120)}»" if given is not None else "")
                    + ". השורות: " + _rows_listed(rows))


def _column_of(wanted: str, headers: list[str]) -> int:
    """The column a header label names; a multi-level header written with " | " ("קבוצה | 101") is matched loosely: a header
    that holds every level, else the leaf level alone (round 15 U1 follow-up, KTD9)."""
    try:
        return _match_one(wanted, headers, "העמודה")
    except ToolError:
        if not _is_row_text(wanted):
            raise
    levels = [g for g in (_loose(x) for x in wanted.split("|")) if g]
    own = [_loose(h) for h in headers]
    for need in (levels, levels[-1:]):
        hit = [i for i, h in enumerate(own) if h and need and all(g in h for g in need)]
        if len(hit) > 1:
            exact = [i for i in hit if own[i] == "".join(need)]
            hit = exact or hit
        if len(hit) == 1:
            return hit[0]
        if hit:
            raise ToolError(f"העמודה «{wanted}» מתאימה לכמה ({', '.join(str(i + 1) for i in hit[:8])}); בחר לפי מספר")
    raise ToolError(f"העמודה «{wanted}» לא נמצאה בטבלה. האפשרויות: " + "; ".join(f"«{o}»" for o in headers[:20] if o))


def _row_name(cells: list[str]) -> str:
    """A row as the model names it in a refusal: its non-numeric cells."""
    return " | ".join(c for c in cells if c and _parse_number(c) is None) or " | ".join(cells)


def _rows_listed(rows: list[list[str]], at: list[int] | None = None) -> str:
    """The rows ``at`` (the first eight; default: every row) by number and name, as a refusal lists them."""
    return "; ".join(f"שורה {i + 1} «{_clip(_row_name(rows[i]), 80)}»" for i in (list(range(len(rows))) if at is None else at)[:8])


MSG_NO_ROW = "חסרה שורה (row: התווית, או row_number). השורות בטבלה: {rows}. למשל row_number=1"
MSG_NO_COLUMN = "חסרה עמודה (column: הכותרת, או column_number). העמודות בטבלה: {columns}. למשל column_number=1"
MSG_QUOTE_OF_ANOTHER_ROW = ("הציטוט אינו מהשורה שנבחרה. התא שנבחר ({where}) הוא «{cell}»{held}. הסר את quote, או בחר את "
                            "השורה שהציטוט שלה")
MSG_QUOTE_HELD_BY = "; הציטוט הוא מ{rows}: row_number={row}, column_number={column}"
MSG_NUMBER_AT = "{at}. הלוקטור הנכון: row_number={row}, column_number={column}"
MSG_LOCATOR_SHAPE = ("locator: תא בטבלה (row או row_number, ו-column או column_number; אפשר גם table) או ציטוט (quote ו-number)"
                     " — חייב להיות אחד מהם. למשל {example}")
MSG_LOCATOR_EXAMPLE_TABLE = '{"row_number": 1, "column_number": 2} או {"quote": "השורה כפי שנכתבה", "number": "המספר"}'
MSG_LOCATOR_EXAMPLE_TEXT = '{"quote": "המשפט כפי שנכתב", "number": "המספר"}'


def _columns_listed(headers: list[str], width: int) -> str:
    """The columns of a table by number and header, as a refusal lists them (the count alone when it has no headers)."""
    if not any(headers):
        return f"{width} עמודות ללא כותרות (column_number מ-1 עד {width})"
    return "; ".join(f"{i + 1} «{_clip(h, 60)}»" for i, h in enumerate(headers[:20]) if h)


def _table_of(ws: Workspace, src: Source, given) -> int:
    """The table a cell locator names: the source's own table, or the T# / table S# given (of the same table)."""
    index = src.table_index
    if given:
        g = str(given).strip()
        h = ws.handles.get(g)
        if h is not None and h["kind"] == "T" and h.get("vision"):
            raise ToolError(f"{g} היא טבלה שנקראה בקריאה חזותית: קח את התא מהמקור שבו היא נקראה "
                            f"({h['vision']['source']})")
        if h is not None and h["kind"] == "T":
            if h["version_id"] != str(src.version_id):
                raise ToolError(f"{g} היא טבלה של מסמך אחר מזה של {src.sid}")
            named = h["table_index"]
        elif g in ws.sources and ws.sources[g].table_index is not None:
            if ws.sources[g].version_id != src.version_id:
                raise ToolError(f"{g} הוא מקור של מסמך אחר מזה של {src.sid}")
            named = ws.sources[g].table_index
        else:
            raise ToolError(f"טבלה לא מוכרת: {g}. נדרש T# או S# של טבלה שנקראה בתור הזה")
        if index is not None and named != index:
            raise ToolError(f"{src.sid} אינו מהטבלה {g}: קח את התא מהמקור שבו הטבלה נקראה")
        index = named
    if index is None:
        raise ToolError(f"{src.sid} אינו טבלה: פתח את הטבלה (read עם table=T#) וקח את התא מהמקור שהוחזר, או צטט "
                        "משפט (quote) שבו המספר כתוב")
    return index


def _take_cell(ws: Workspace, conn: Connection, src: Source, full: str, loc: dict) -> dict:
    """The number in the cell at a named row and column of the source's table, verified against the table's stored
    structure; a number the model names that is in another row or column is refused, with where it is."""
    index = _table_of(ws, src, loc.get("table"))
    st, block = _table_at(ws, conn, src.version_id, index)
    if st is None:
        raise ToolError("הטבלה לא נמצאה בקריאה הנוכחית של המסמך")
    return _cell_of(src, full, loc, st, index,
                    _governing_note(ws, conn, src.version_id, block.block_index if block is not None else None,
                                    st.get("candidate_of")),
                    unconfirmed=_later_page_unconfirmed(ws, conn, src.version_id, index, st, block))


MSG_LATER_PAGE_UNCONFIRMED = ("שורות העמוד {page} של הטבלה באות אחרי «{text}» (כותרת או פסקה בין חלקי הטבלה): לא ודאי שכותרת הטבלה, "
                              "קנה המידה וההערות שלה חלים עליהן, ולכן הערך מותנה בכך")


def _later_page_blocks(conn: Connection, version_id, st: dict, block) -> tuple[list, Any, Any]:
    """What the stored data offer to tell whether the rows of a stored table on a later page continue it (round 22 U3, KTD3, R9): the
    headings and paragraphs after the table's block on its pages, with their tops on the rendered page; the page furniture among them (a
    text that recurs at the same place on a large share of the pages, looked up for these texts only); the stored text of the pages. The
    narrowest lookup: three queries, made only for a table whose rows span pages."""
    pages = sorted({r["page"] for r in st.get("rows") or [] if isinstance(r, dict) and r.get("page")})
    cols = ("b.page, b.bbox, b.text, p.mediabox, p.cropbox, p.rotation, p.display_width, p.display_height, p.geometry_issue")
    join = "LEFT JOIN pages p ON p.version_id = b.version_id AND p.page_no = b.page"
    kinds = "b.kind IN ('heading', 'paragraph') AND b.table_index IS NULL"
    rows = conn.execute(text(
        f"SELECT {cols} FROM document_blocks b {join} WHERE b.version_id = :v AND {kinds} AND b.page BETWEEN :a AND :z"
        " AND b.block_index > :after ORDER BY b.block_index"),
        {"v": version_id, "a": pages[0], "z": pages[-1], "after": block.block_index if block is not None else -1}).all()
    blocks = [stored_tables.BlockAt(r.page, contexts.top_of(r) if r.bbox else None, r.text or "") for r in rows]
    texts = sorted({b.text for b in blocks if b.text})
    seen = conn.execute(text(
        f"SELECT {cols} FROM document_blocks b {join} WHERE b.version_id = :v AND {kinds} AND b.text = ANY(:t)"),
        {"v": version_id, "t": texts}).all() if texts else []
    total = conn.execute(text("SELECT count(*) FROM pages WHERE version_id = :v"), {"v": version_id}).scalar_one()
    furniture = stored_tables.furniture_of(((r.page, contexts.top_of(r) if r.bbox else None, r.text or "") for r in seen), total or None)
    page_texts = dict(conn.execute(text("SELECT page_no, text FROM pages WHERE version_id = :v AND page_no BETWEEN :a AND :z"),
                                   {"v": version_id, "a": pages[0], "z": pages[-1]}).all())
    return blocks, furniture, page_texts.get


def _later_page_unconfirmed(ws: Workspace, conn: Connection, version_id, index: int, st: dict, block) -> dict[int, str]:
    """The later pages of a stored table whose rows come after a separation (``tables.unconfirmed_pages``), read once per turn:
    ``{}`` for a table of one page (no lookup is made) and for a table nothing separates (its caption's scale and notes apply to every
    page, as before)."""
    if not stored_tables.spans_pages(st):
        return {}
    key = (f"later:{version_id}", index)
    found = ws.tables.get(key)
    if found is None:
        blocks, furniture, page_text = _later_page_blocks(conn, version_id, st, block)
        found = ws.tables[key] = stored_tables.unconfirmed_pages(st, blocks, furniture, page_text)
    return found


def _cell_facet_in(label: str, header: str, st: dict, cells: list) -> dict:
    """What the facet of a table cell is read from (round 13 U3, KTD2; round 14 U2, KTD3): its row label, its column header, and its
    table's caption, title lines and notes (a note that names the row label). The same for a cell taken now and for one a cached
    value is read again from (round 14 U3, KTD5)."""
    return {"cell": True, "row": label, "header": header, "caption": st.get("caption") or "",
            "title": [x for x in st.get("title") or [] if x], "matrix": len(cells) >= 3,
            "notes": [str(x) for x in st.get("notes") or [] if x]}


def _row_shape(text_: str) -> str:
    """A quote or a row in the spelling the quote of a row is compared in: by its letters and digits, the cell borders kept, so
    "( *)" and "(*)" are one."""
    return "|".join(_loose(part) for part in meaning._flat(text_).split("|"))


def _quote_too_short(shaped: str) -> bool:
    """A quote (``_row_shape``) with under three letters or digits: too short to say which row it is of."""
    return len(shaped.replace("|", "")) < 3


def _rows_holding(rows: list[list[str]], shaped: str) -> list[int]:
    """The indexes of the rows whose cells hold the quote (``_row_shape``)."""
    return [i for i, r in enumerate(rows) if shaped in _row_shape(" | ".join(r))]


def _check_quote_of_row(quote: str, rows: list[list[str]], ri: int, ci: int, where: str, cell: str) -> None:
    """A quote given beside cell fields must be (part of) the row of the cell they name (round 17 U6, KTD6 item 6); the
    refusal names the cell found, and the row the quote is from. Compared as ``_row_quote_locator`` does, by letters and
    digits and the cell borders. A quote too short to compare (under three letters) checks nothing."""
    wanted = _row_shape(quote)
    if _quote_too_short(wanted) or wanted in _row_shape(" | ".join(rows[ri])):
        return
    held = _rows_holding(rows, wanted)
    raise ToolError(MSG_QUOTE_OF_ANOTHER_ROW.format(
        where=where, cell=cell,
        held=(MSG_QUOTE_HELD_BY.format(rows=_rows_listed(rows, held[:1]), row=held[0] + 1, column=ci + 1) if held else "")),
        "quote_not_in_row")


def _cell_of(src: Source, full: str, loc: dict, st: dict, index, governing: str | _ScaleNote = "",
             unconfirmed: dict[int, str] | None = None) -> dict:
    """The cell a locator names in a table structure (``extracted_tables.structure``, or a visual reading's table in
    the same shape), its number checked against the cell and the source's text, with what the table attests.
    ``governing``: the scale note that governs the table from a block above it (``calc.governing_note``). A table stored in reversed
    cell order is resolved in the order it is drawn (what the agent was shown): the column the locator names is a served position or
    a served label; the value, its box and the datum's identity are looked up by the STORED column (round 21 U8, KTD6).
    ``unconfirmed``: the pages of a stored multi-page table whose rows come after a separation (``tables.unconfirmed_pages``, round 22 U3,
    KTD3): a cell on one carries the table's scale as unconfirmed, and no unit, VAT or attribution is taken from the table's caption,
    title or notes for it (the unit's provenance is then not the source's), so a value read from it is conditional."""
    st = stored_tables.serve_structure(st)
    rows = [list(r.get("cells") or []) for r in st.get("rows") or []]
    headers = list(st.get("headers") or [])
    if st.get("columns") is not None and not any(headers):
        headers = stored_tables.served_labels(st)[0]  # a Latin header row is no header to the extraction, but it labels the columns
    if not rows:
        raise ToolError("לטבלה אין שורות")
    if _is_row_text(loc.get("row")):
        # the model passed the whole rendered row: its cells decide, the row_number only breaks a tie (round 15 U1 follow-up)
        ri = _row_of_text(rows, loc["row"], loc.get("row_number"))
    elif loc.get("row_number") is not None:
        ri = _row_index(loc["row_number"], rows)
        if ri is None:
            raise ToolError(f"לטבלה {len(rows)} שורות; row_number מ-1 עד {len(rows)}")
        if loc.get("row"):
            # the label decides when the number disagrees with it and the label names exactly one row (round 15 U1, KTD9)
            named = _rows_named(rows, loc["row"])
            here = any(c and same(c) for same in _label_tiers(loc["row"])[:2] for c in rows[ri])
            if ri not in named and not here:
                if len(named) == 1:
                    ri = named[0]
                else:
                    raise ToolError(f"שורה {ri + 1} אינה «{loc['row']}»: היא «{' | '.join(rows[ri])}»"
                                    + (f". «{loc['row']}» נמצא ב{_rows_listed(rows, named)}; בחר לפי row_number"
                                       if named else ""))
    elif loc.get("row"):
        named = _rows_named(rows, loc["row"])
        if not named:
            raise ToolError(f"השורה «{loc['row']}» לא נמצאה בטבלה. השורות: " + _rows_listed(rows))
        if len(named) > 1:
            raise ToolError(f"השורה «{loc['row']}» מתאימה לכמה שורות — {_rows_listed(rows, named)}; בחר לפי row_number")
        ri = named[0]
    else:
        raise ToolError(MSG_NO_ROW.format(rows=_rows_listed(rows)), "no_row")
    cells = rows[ri]
    if loc.get("column_number") is not None:
        ci = int(loc["column_number"]) - 1
        if not 0 <= ci < len(cells):
            raise ToolError(f"בשורה {len(cells)} עמודות; column_number מ-1 עד {len(cells)}")
    elif loc.get("column"):
        if not any(headers):
            raise ToolError("לטבלה אין כותרות עמודה: בחר עמודה לפי column_number")
        ci = _column_of(loc["column"], headers)
        if ci >= len(cells):
            raise ToolError(f"בשורה «{cells[0] if cells else ''}» אין תא בעמודה «{headers[ci]}»")
    else:
        raise ToolError(MSG_NO_COLUMN.format(columns=_columns_listed(headers, max(len(cells), len(headers)))),
                        "no_column")
    cell = cells[ci]
    header = headers[ci] if ci < len(headers) else ""
    numbers = _numbers_of(meaning._norm(cell))
    where = f"שורה «{cells[0] if cells else ri + 1}», עמודה «{header or ci + 1}»"
    if loc.get("quote"):  # cell fields and a quote together (round 17 U6, KTD6): the cell decides, the quote must be its row's
        _check_quote_of_row(loc["quote"], rows, ri, ci, where, cell)
    if loc.get("number"):
        wanted = _parse_number(loc["number"])
        if wanted is None:
            raise _not_one_number(loc["number"])
        hit = [n for n in numbers if n[3] == wanted[1]]
        if not hit:
            elsewhere = [(r, c) for r, row in enumerate(rows) for c, x in enumerate(row)
                         if any(n[3] == wanted[1] for n in _numbers_of(meaning._norm(x)))]
            if elsewhere:
                r, c = elsewhere[0]
                at = f"שורה «{rows[r][0] if rows[r] else r + 1}», עמודה «{headers[c] if c < len(headers) else c + 1}»"
                raise ToolError(MSG_NUMBER_AT.format(
                    at=f"המספר {loc['number']} נמצא בטבלה ב{at}, לא ב{where} שנבחרו (שם כתוב «{cell}»). ערך נלקח רק "
                       "מהשורה והעמודה שהוא שייך להן", row=r + 1, column=c + 1), "number_elsewhere")
            raise ToolError(f"המספר {loc['number']} אינו בתא שנבחר ({where}: «{cell}») ואינו בטבלה")
        written, value = hit[0][0], hit[0][3]
        at_cell = hit[0][1:3]
    else:
        if len(numbers) != 1:
            if numbers and platform_dates.parse(cell) is not None:
                raise _date_cite()
            raise ToolError(f"בתא שנבחר ({where}) " + ("אין מספר" if not numbers else f"יש כמה מספרים («{cell}»); "
                                                       "ציין number"))
        written, value = numbers[0][0], numbers[0][3]
        at_cell = numbers[0][1:3]
        sign = _parse_number(cell)
        if sign is not None and sign[1] == -value:
            value = -value
    forms = frozenset(numbers_in(written))
    if not forms & numbers_in(full):
        raise ToolError(f"הערך {written} ({where}) אינו בטקסט של {src.sid}: קרא את חלק הטבלה שבו הוא נמצא "
                        "(read עם table=T# או cursor) וקח אותו מהמקור הזה")
    line = " | ".join(cells)
    label = cells[0] if cells else ""
    if not label:  # an empty first cell (a group column left blank): the row is named by its first label cell
        label = next((c for c in cells if c and _parse_number(c) is None), "")
    column_units = st.get("units") or []
    near = [cell, label, header, str(column_units[ci]) if ci < len(column_units) and column_units[ci] else ""]
    # the notes that govern THIS row (round 22 U5, KTD5, R8): a note marked «(*)» belongs to the rows that carry the mark; the quote path reads the same ones
    row_notes = _notes_of_rows(st, [ri])
    table_text = [st.get("caption") or "", *(st.get("title") or []), *row_notes]
    row_page = (st.get("rows") or [])[ri].get("page")
    later = (unconfirmed or {}).get(row_page)  # why this cell's page is unconfirmed (None: the caption's own, or a continuation)
    stated_text = [] if later else table_text  # what the table says of this cell: nothing is taken from it when unconfirmed
    # the parts the source states the cell's meaning in, nearest first (round 12 U9, KTD9): the words of each attribute
    # are anchored to the first that writes them
    caption_text = " ".join(x for x in [st.get("caption") or "", *(st.get("title") or [])] if x)
    notes_text = " ".join(x for x in row_notes if x)
    vat_parts = [("cell", cell), ("row", label), ("header", header), ("header", near[3]), ("caption", caption_text),
                 ("note", notes_text)]
    vats = meaning.vat_attested("\n".join(near + stated_text), forms)
    # the unit of the cell, its row or its column; the table's caption or notes only when those state none. A cell that
    # states no unit of its own takes the one its column header states, before the cell, row label and header are read
    # together (round 15 U1, KTD9): "שווי למ״ר" over a row that names "דירה 3 חדרים" is ₪ per m², not m²
    head_units = set() if meaning.units_attested(cell) else _header_units(header, near[3])
    units = head_units or meaning.units_attested(" ".join(near)) or meaning.units_attested(" ".join(stated_text))
    # a row label that names money per area gives a cell that states only the currency that unit; a cell or a header
    # that states another unit keeps it (round 15 U1 follow-up, KTD9)
    label_units = (_label_units(label) if not head_units and meaning.units_attested(cell) == {"ILS"}
                   and meaning.units_attested(" ".join([header, near[3]])) <= {"ILS"} else set())
    if label_units:
        units = label_units
    # where the source states the unit (R10): the cell, its row label, its column header (or the column's unit
    # read from it), else the table's caption, title or notes
    unit_from = "header" if head_units else "row" if label_units else next(
        (where for where, t in (("cell", near[0]), ("row", near[1]), ("header", near[2] + " " + near[3]))
         if meaning.units_attested(t)), "table" if units else None)
    # a unit only the row's own label mentions, beside a caption that states another, and nothing in the cell or its
    # header: "דירה 90 מ״ר | 21,000" under "(₪)" — the m² describes the row's property, the amount is the caption's. The
    # source does not say which: the value registers with the model's unit and a flag that blocks calculating with it
    row_units = meaning.units_attested(label)
    caption_units = meaning.units_attested(" ".join(stated_text))
    ambiguous = (not head_units and not label_units and not meaning.units_attested(" ".join([cell, header, near[3]])) and bool(row_units)
                 and bool(caption_units) and row_units != caption_units and bool(re.search(r"\d", label)))
    if ambiguous:
        units, unit_from = set(), None
    own_kind_not_money = meaning.nonmonetary(" ".join(near))
    if not units and not own_kind_not_money and not ambiguous and not later:
        units = _note_units(governing)
        if units:
            unit_from = "governing_note"
    # the scale the table states it in (R14): the cell's own scale word, else a note of its cell, row or column
    # ("הכנסות (אלפי ₪)"), else of the table's caption, title or notes ("טבלה 4 (באלפי ₪)"), else of the heading-like
    # line above the table that governs it ("ממצאי הבדיקה באלפי ₪") — a table's own note is nearer than that line's
    scale = (calc.stated_scale(meaning._norm(cell), *at_cell)
             if own_kind_not_money or units and not units & {"ILS", "ILS_per_sqm"} else
             _value_scale(meaning._norm(cell), *at_cell, meaning._norm(" ".join(near)),
                          meaning._norm(" ".join(x for x in table_text if x)), governing=governing))
    # who stated it: the column header, else the row label, else the table's caption, title or notes (KTD8)
    said = (attribution_in(header, adopted=True) or attribution_in(label, adopted=True)
            or attribution_in(" ".join(x for x in stated_text if x), adopted=True))
    return {"written": written, "value": value, "forms": forms, "quote": line,
            "qualifiers": meaning.number_qualifiers(_table_text(st | {"notes": row_notes}, st.get("rows") or []), forms, line),
            "units": units, "vat": vats, "vat_from": _vat_anchors(vats, vat_parts, "note"),
            "kind_context": " ".join([label, header, st.get("caption") or ""]),
            "kind_parts": [("row", label), ("header", header), ("caption", st.get("caption") or "")],
            "locator": {"table_index": index, "row": cells[0] if cells else "", "row_number": ri + 1,
                        "column": header, "column_number": stored_tables.stored_column(st, ci) + 1,
                        **({"column_shown": ci + 1} if st.get("columns") is not None else {})},
            "total": bool(calc.TOTAL_WORDS.search(meaning._norm(cells[0] if cells else ""))),
            "table": (str(src.version_id), index), "said": said, "context": " ".join([*near, *table_text]),
            "meaning_from": {"unit": unit_from} if unit_from else {}, "scale": scale,
            **({"unit_ambiguous": sorted(row_units | caption_units)} if ambiguous else {}),
            **({"unconfirmed": MSG_LATER_PAGE_UNCONFIRMED.format(page=row_page, text=later)} if later else {}),
            # what the facet is read from (round 13 U3, KTD2): the row label, the column header and the caption and title
            "facet_in": _cell_facet_in(label, header, st, cells),
            # the cell as stored (KTD1): its box is looked up when the answer is stored, never searched for
            "anchor": {"table_index": index, "row": ri, "column": stored_tables.stored_column(st, ci),
                       "pages": [p] if (p := (st.get("rows") or [])[ri].get("page")) else []}}


# --- a cell of a table read by inspect (KTD6, R16–R19) -----------------------------------------------------------------
#
# A table ``inspect`` transcribed is addressed through its source (and its vision ``T#``) and its cells resolve through
# the stored region reading, never ``extracted_tables``. A cell's value is verified only with evidence beyond the
# transcription: OCR of the same crop (or the region's text layer) sees its number once, in the cell's place — inside
# the row band of its row label and the column band of its header, or on its row's line and in its column's band of
# the numbers' own grid (``images.cell_evidence``, ``images.placed_cells``). Otherwise it is uncertain, with the reason,
# and a calculation using it is conditional. No focused re-read is spent on it: the only re-read of a region is its
# stored reading, which cannot add evidence. Its anchor carries the region key and, when confirmed, the confirming
# word's box mapped into the rendered page (``anchors.page_box``), never an ``extracted_tables`` index.

# keyed by a confirmed cell's ``by`` (``images.BY_OCR``, ``images.BY_TEXT_LAYER``) and an uncertain cell's status
# (``images.CELL_*``), written out: this module imports ``app.extraction.images`` only where it is used
MSG_VISION_CELL = {
    "ocr": "המספר אומת מול OCR של אותו חיתוך: OCR ראה אותו פעם אחת, בשורה ובעמודה של התא.",
    "text_layer": "המספר אומת מול שכבת הטקסט של האזור: הוא כתוב בה פעם אחת, בשורה ובעמודה של התא.",
}
MSG_VISION_UNCERTAIN = {
    "not_seen": "OCR של אותו חיתוך לא ראה את המספר הזה: הוא תמלול של המודל בלבד, ולכן הערך לא ודאי.",
    "repeated": "המספר מופיע בחיתוך יותר מפעם אחת, ולכן OCR אינו מראה שהוא בתא הזה: הערך לא ודאי.",
    "not_placed": ("OCR ראה את המספר, אבל לא בשורה ובעמודה של התא (או שלא ניתן היה לאתר אותן בחיתוך): ייתכן שהתמלול "
                   "שייך אותו לתא אחר, ולכן הערך לא ודאי."),
    "no_ocr": "אין OCR של החיתוך שיאמת את התמלול: הערך לא ודאי.",
}


def _vision_table(ws: Workspace, src: Source, given) -> int:
    """The table of a visual reading a cell locator names: the only one, or the vision T# given (of this reading)."""
    handles = src.vision["tables"]
    if given:
        g = str(given).strip()
        h = ws.handles.get(g)
        if h is not None and h.get("vision") and h["version_id"] == str(src.version_id) \
                and h["vision"]["region"] == src.vision["region"]:
            return h["vision"]["table"]
        if g != src.sid:
            raise ToolError(f"{g} אינה טבלה של הקריאה החזותית ב-{src.sid}"
                            + (f": הטבלאות בה {', '.join(handles)}" if handles else ""))
    if not handles:
        raise ToolError(f"בקריאה החזותית ב-{src.sid} אין טבלה: צטט (quote) את המשפט שבו המספר כתוב")
    if len(handles) > 1:
        raise ToolError(f"ב-{src.sid} יש {len(handles)} טבלאות: בחר אחת ב-table ({', '.join(handles)})")
    return 0


def _take_vision_cell(ws: Workspace, src: Source, full: str, loc: dict, governing: str | _ScaleNote = "") -> dict:
    """A cell of a table read by inspect, through its stored reading, with its OCR evidence (``evidence``: the
    cell's ``images.cell_evidence`` entry, ``why``: the message for its status) and an anchor of the region and the
    cell's box in the rendered page when OCR confirmed it in its place. ``governing``: the scale note that governs
    the region from a block above it (``calc.governing_note``)."""
    from app.extraction.images import BY_OCR, CELL_CONFIRMED, CELL_NO_OCR, placed_cells

    vis = src.vision
    reading = vis["reading"]
    ti = _vision_table(ws, src, loc.get("table"))
    t = reading.tables[ti]
    st = {"headers": list(t.headers), "rows": [{"cells": list(r)} for r in t.rows], "title": list(t.title),
          "notes": list(t.notes), "source": "vision"}
    taken = _cell_of(src, full, loc, st, ti, governing)
    ri, ci = taken["anchor"]["row"], taken["anchor"]["column"]
    ocr = reading.ocr or {}
    cells = placed_cells(reading)  # a reading stored before the numbers' grid placed cells is placed again here
    try:
        evidence = cells[ti][ri][ci]
    except (IndexError, TypeError):
        evidence = None
    evidence = evidence or {"status": CELL_NO_OCR, "box": None, "by": None}
    confirmed = evidence["status"] == CELL_CONFIRMED
    box = anchors.page_box(evidence.get("box"), ocr["frame"]) if confirmed and ocr.get("frame") else None
    row_cells = t.rows[ri]
    header = t.headers[ci] if ci < len(t.headers) else ""
    unit_note = next((n for n in t.notes if meaning.units_attested(n)), None)
    taken["locator"] = {"region": vis["region"], "vision_table": ti, "row": row_cells[0] if row_cells else "",
                        "row_number": ri + 1, "column": header, "column_number": ci + 1}
    taken["table"] = (str(src.version_id), f"{vis['region']}#{ti}")
    taken["anchor"] = {"vision": {"region": vis["region"], "table": ti, "row": ri, "column": ci, "page": vis["page"],
                                  "cell_box": box, "title": next((x for x in t.title if x), None),
                                  "row_label": (row_cells[0] or None) if row_cells else None, "row_number": ri + 1,
                                  "column_header": header or None, "column_number": ci + 1, "unit_note": unit_note,
                                  "notes": list(t.notes[:3])},
                       "pages": [vis["page"]] if vis["page"] else []}
    taken["evidence"] = evidence
    taken["why"] = (MSG_VISION_CELL.get(evidence.get("by") or BY_OCR, MSG_VISION_CELL[BY_OCR]) if confirmed
                    else MSG_VISION_UNCERTAIN.get(evidence["status"], MSG_VISION_UNCERTAIN["no_ocr"]))
    return taken


def _vision_quote_cell(ws: Workspace, src: Source, loc: dict) -> dict | None:
    """A quote of a visual reading that is (part of) one row of one of its tables and holds the number in one cell
    of it: that cell's locator, so it is taken and checked as a cell. None otherwise (the quote is taken as a
    quote)."""
    wanted = _parse_number(loc.get("number") or "")
    if wanted is None:
        return None
    quote = meaning._flat(loc["quote"])
    hits = []
    for ti, t in enumerate(src.vision["reading"].tables):
        for ri, row in enumerate(t.rows):
            if quote and quote in meaning._flat(" | ".join(row)):
                cols = [ci for ci, c in enumerate(row)
                        if any(n[3] == abs(wanted[1]) for n in _numbers_of(meaning._norm(c)))]
                hits += [(ti, ri, ci) for ci in cols]
    if len(hits) != 1:
        return None
    ti, ri, ci = hits[0]
    return {"table": src.vision["tables"][ti], "row_number": ri + 1, "column_number": ci + 1, "number": loc["number"]}


def _row_quote_locator(sid: str, st: dict, loc: dict) -> dict:
    """The cell locator of a quote that is (part of) one row of the source's table (round 15 U1, KTD9, R12): the row whose cells
    hold the quote, and the cell of it that holds the number and is inside the quote. No such row, or several rows or cells,
    is a refusal that names the rows found. The quote and the cells are compared by their letters and digits and the cell
    borders, so "( *)" and "(*)" are one."""
    if not loc.get("number"):
        raise ToolError("ציטוט (quote) דורש גם את המספר כפי שנכתב בו (number)")
    wanted = _parse_number(loc["number"])
    if wanted is None:
        raise _not_one_number(loc["number"])
    rows = [list(r.get("cells") or []) for r in (stored_tables.serve_structure(st) or {}).get("rows") or []]  # as drawn (U8)
    quote = _row_shape(loc["quote"])
    if _quote_too_short(quote):
        raise ToolError(f"הציטוט אינו מופיע כלשונו ב-{sid}: העתק את המשפט מהמקור בדיוק (או בחר תא בטבלה)")
    held = _rows_holding(rows, quote)
    if not held:
        raise ToolError(f"הציטוט אינו שורה בטבלה של {sid}. השורות: " + _rows_listed(rows))
    places = [(i, ci) for i in held for ci, c in enumerate(rows[i])
              if any(n[3] == abs(wanted[1]) for n in _numbers_of(meaning._norm(c))) and _loose(c) and _loose(c) in quote]
    if not places:
        raise ToolError(f"המספר {loc['number']} אינו בתאים של הציטוט ({_rows_listed(rows, held)}): צטט את השורה "
                        "כשהמספר בתוכה, או בחר תא לפי row ו-column")
    if len(places) > 1:
        raise ToolError("הציטוט מתאים לכמה תאים: " + "; ".join(f"שורה {i + 1} «{_clip(_row_name(rows[i]), 80)}», עמודה {ci + 1}"
                                                                  for i, ci in places[:8]) + ". בחר לפי row_number ו-column_number")
    i, ci = places[0]
    return {"row_number": i + 1, "column_number": ci + 1, "number": loc["number"]}


def _cell_forms(label: str, unit, cell: str, column: int) -> list[str]:
    """How a table cell is written in the text a source shows: the cell alone, and after its column label as a row chunk or a
    served row renders it (``render_row``: ``label: cell``, the unit after the label when the label lacks it, ``עמודה N`` for a
    column with no label)."""
    name = label or f"עמודה {column + 1}"
    forms = [cell, f"{name}: {cell}"]
    if unit and f"({unit})" not in name:
        forms.append(f"{name} ({unit}): {cell}")
    return forms


def _quote_cell_locator(src: Source, full: str, st: dict, loc: dict) -> dict | None:
    """The cell locator of a quote of a table source that holds exactly one cell of its table (round 22 U4, KTD4, R7): the cell
    whose number is the quote's number and whose text, alone or after its column label as the source shows it, holds the quote — in the
    rows the source shows (the one row of a table-row hit). It is then taken as that cell, so its anchor carries the table, the row and
    the stored column, and the table's scale, unit and notes apply, as for a cell taken by row and column. None (the quote is taken as
    a quote) when the number is not in the quote, no cell or several cells hold it, or the quote is also written outside the table's rows
    of this source (a note, a caption): then the quote does not say which cell it is."""
    wanted = _parse_number(loc.get("number") or "")
    shaped = _loose(loc.get("quote"))
    if wanted is None or len(shaped) < 3 or meaning._flat(loc["quote"]) not in meaning._flat(full) \
            or not any(n[3] == abs(wanted[1]) for n in _numbers_of(meaning._norm(loc["quote"]))):
        return None
    sst = stored_tables.serve_structure(st)
    labels = stored_tables.served_labels(sst)[0] if sst is not st else list(st.get("headers") or [])
    units = list(sst.get("units") or [])
    stored_labels, stored_units = list(st.get("headers") or []), list(st.get("units") or [])
    whole = _loose(full)
    rest = full
    found: list[tuple[int, int]] = []
    for i, row in enumerate(sst.get("rows") or []):
        if src.row_index is not None and i != src.row_index:
            continue
        cells = list(row.get("cells") or [])
        stored_row = list((st.get("rows") or [])[i].get("cells") or []) if i < len(st.get("rows") or []) else cells
        lines = [" | ".join(cells), render_row(stored_labels, stored_row, stored_units or None)]
        if sst is not st:
            lines.append(stored_tables.render_served_row(sst, i, (labels, None)))
        lines = [ln for ln in dict.fromkeys(lines) if ln and (key := _loose(ln)) and key in whole]
        if not lines:
            continue
        for ln in lines:
            rest = rest.replace(ln, "")
        for ci, cell in enumerate(cells):
            label = labels[ci] if ci < len(labels) else ""
            unit = units[ci] if ci < len(units) else None
            if any(n[3] == abs(wanted[1]) for n in _numbers_of(meaning._norm(cell))) \
                    and any(shaped in _loose(f) for f in _cell_forms(label, unit, cell, ci)):
                found.append((i, ci))
    if len(found) != 1 or shaped in _loose(rest):
        return None
    i, ci = found[0]
    return {"row_number": i + 1, "column_number": ci + 1, "number": loc["number"]}


MSG_QUOTE_AMBIGUOUS = ("הציטוט מופיע ב-{sid} יותר מפעם אחת, ובמקומות שונים כתוב בו מספר אחר: צטט ציטוט ארוך יותר, "
                       "שמופיע במקור פעם אחת בלבד ושהמספר כתוב בו במלואו")


def _block_sentence(blocks: Iterable[tuple] | None, anchor: dict, quote: str) -> str | None:
    """The whole sentence a quote is in, read from the text of its block (round 14 follow-up): PDF extraction stores a paragraph
    as lines, and a line wrap is no sentence end (``datum.own_sentence``). The blocks are those the anchor says hold the quote (else
    all of the cited range); None when they do not agree on one sentence, or there are no blocks — the caller keeps the sentence
    of the source line."""
    held = _value_blocks(anchor)
    found = {datum.own_sentence(meaning._norm(b[1] or ""), quote)
             for b in blocks or () if held is None or b[0] in held}
    return found.pop() if len(found) == 1 else None


def _stored_place(found: anchors.Located, remap: dict[int, tuple]) -> anchors.Located:
    """``found`` (a quote located in the text a source showed) in the stored blocks: the segments and the cited number of a bound page-text
    row, which the source showed as "label: value" pairs, are the stored word spans of the verbatim pieces they cover (round 23 U4, KTD8). A
    block the source showed as stored keeps its place."""
    segments: list[list[int]] = []
    for b, start, end in found.segments:
        if b in remap:
            segments += [[b, a, c] for a, c in columns.stored_segments(remap[b], start, end)]
        else:
            segments.append([b, start, end])
    number = found.number
    if number is not None and number[0] in remap:
        placed = columns.stored_segments(remap[number[0]], number[1], number[2])
        number = [number[0], *placed[0]] if len(placed) == 1 else None
    return anchors.Located(segments, number, found.blocks)


def _take_quote(src: Source, full: str, loc: dict, blocks: list[tuple] | None = None,
                governing: Callable[[int], str | _ScaleNote] | None = None, table_notes: Iterable[str] = (),
                remap: dict[int, tuple] | None = None) -> dict:
    """The number inside an exact quote of the source: the quote must occur in the source's full text, and the
    number inside the quote. ``blocks``: the source's blocks (``(block_index, text, page)``), where the quote is
    located within the cited range only (KTD4): one occurrence records its block and word span; several that write
    the same number record the blocks holding them (block precision); several that write different numbers are
    refused, asking for a longer quote. Never the first occurrence. ``governing``: the scale note that governs a
    block from a block above it (``calc.governing_note``), for the block the number is in. ``table_notes``: the notes of the table the quote is of that govern
    its row and that the source's text lacks (``_quote_table_notes``, round 22 U5): read as the cell path reads them, for the scale, the unit and the VAT.
    ``remap``: for a block of ``blocks`` that is a bound page-text row shown as "label: value" pairs, the pieces of its shown text that copy
    stored words (``columns.Part``); the anchor is then the stored word span (round 23 U4)."""
    if not loc.get("number"):
        raise ToolError("ציטוט (quote) דורש גם את המספר כפי שנכתב בו (number)")
    wanted = _parse_number(loc["number"])
    if wanted is None:
        raise _not_one_number(loc["number"])
    quote = meaning._flat(loc["quote"])
    text_ = meaning._flat(full)
    if len(quote) < 3 or quote not in text_:
        raise ToolError(f"הציטוט אינו מופיע כלשונו ב-{src.sid}: העתק את המשפט מהמקור בדיוק (או בחר תא בטבלה)")
    if " | " in quote:
        raise ToolError("הציטוט הוא שורת טבלה: קח את הערך כתא (row ו-column), כדי שהשרת יבדוק לאיזו עמודה הוא שייך")
    hit = [n for n in _numbers_of(quote) if n[3] == abs(wanted[1])]
    if not hit:
        if any(n[3] == abs(wanted[1]) for n in _numbers_of(text_)):
            raise ToolError(f"המספר {loc['number']} נמצא ב-{src.sid} אבל לא בתוך הציטוט: צטט את המשפט שבו הוא כתוב")
        raise ToolError(f"המספר {loc['number']} אינו בציטוט ואינו ב-{src.sid}")
    written, start, end, value = hit[0]
    anchor: dict = {}
    if blocks:
        try:
            found = anchors.locate_quote(blocks, loc["quote"], abs(wanted[1]))
        except anchors.AmbiguousQuote:
            raise ToolError(MSG_QUOTE_AMBIGUOUS.format(sid=src.sid), "quote_ambiguous") from None
        if found is not None and remap:
            found = _stored_place(found, remap)
        if found is not None:
            pages = {b[0]: b[2] for b in blocks if len(b) > 2}
            anchor = ({"segments": found.segments, "number": found.number} if found.segments
                      else {"blocks": found.blocks})
            anchor["pages"] = sorted({pages[i] for i in found.blocks if pages.get(i)})
    at = text_.index(quote)
    local = text_[max(0, at + start - 8):at + end + 16]
    forms = frozenset(numbers_in(written))
    line = next((ln for ln in meaning._norm(full).split("\n") if quote[:40] in " ".join(ln.split())), quote)
    sign = -1 if wanted[1] < 0 else 1
    units = meaning.units_attested(local)
    if not units and len(_numbers_of(quote)) == 1:
        units = meaning.units_attested(quote)
    if units == {"ILS"} and meaning._PER_SQM.search(quote):
        # "השווי למ״ר ... 9,500 ₪": a per-area amount whose "למ״ר" is not next to it; either reading is the source's
        units = {"ILS", "ILS_per_sqm"}
    # who stated it: the words of the number's own clause, else of its sentence (KTD8); never the first occurrence
    # elsewhere — the number as quoted
    said = attribution_at(text_, at + start, at + end)
    # the scale the source states it in (R14): its own scale word, else a note of the quote, else of its line, else
    # of the heading-like line above that governs its block ("ממצאי הבדיקה באלפי ₪" over one figure per paragraph)
    held = _value_blocks(anchor)
    table_notes = [str(n) for n in table_notes if n]
    note_text = meaning._norm(" ".join(table_notes))
    notes = [governing(b) for b in sorted(held)] if governing is not None and held else [""]
    # a duration, count, coefficient, area or rate takes neither the currency nor the scale of a heading
    own_kind_not_money = meaning.nonmonetary(local) or (len(_numbers_of(quote)) == 1 and meaning.nonmonetary(quote))
    scales = ({calc.stated_scale(text_, at + start, at + end)}
              if own_kind_not_money or units and not units & {"ILS", "ILS_per_sqm"} else
              {_value_scale(text_, at + start, at + end, quote, line, note_text, governing=note) for note in notes})
    if len(scales) != 1:
        raise ToolError(MSG_QUOTE_AMBIGUOUS.format(sid=src.sid))
    scale = scales.pop()
    unit_from = "quote" if units else None
    if not units and not own_kind_not_money and table_notes:  # the table's own notes state the unit, as for a cell (round 22 U5)
        units = set(meaning.units_attested(note_text))
        if units:
            unit_from = "table"
    if not units and not own_kind_not_money:
        inherited = {frozenset(_note_units(note)) for note in notes}
        if len(inherited) > 1:
            raise ToolError(MSG_QUOTE_AMBIGUOUS.format(sid=src.sid))
        units = set(inherited.pop())
        if units:
            unit_from = "governing_note"
    whole = "\n".join([full, *table_notes])
    vats = meaning.vat_attested(line, forms, whole)
    sentence = _block_sentence(blocks, anchor, quote)
    return {"written": written, "value": sign * value, "forms": forms, "quote": loc["quote"].strip(),
            **({"sentence": sentence} if sentence else {}),
            "qualifiers": meaning.number_qualifiers(full, forms, quote),
            "units": units, "vat": vats,
            "vat_from": _vat_anchors(vats, [("sentence", quote), ("paragraph", line), ("note", whole)], "paragraph"),
            "kind_context": quote, "kind_parts": [("sentence", quote)], "locator": {"quote": loc["quote"].strip()},
            "source_text": full,  # the source the quote is of: a kind or a unit its fragment lacks is confirmed in the number's own clause (round 21 U3)
            "total": False, "table": None,
            "anchor": anchor, "said": said, "context": f"{line}\n{quote}",
            "meaning_from": {"unit": unit_from} if unit_from else {}, "scale": scale,
            "facet_in": {"cell": False, "written": written}}


def _vat_anchors(vats: set[str], parts: list[tuple[str, str]], fallback: str) -> dict[str, dict]:
    """For each VAT status the source gives a number, the part (of ``parts``: ``(part, text)``, nearest first) that
    writes it and the phrase as written (round 12 U9, KTD9). A status the number's own clause gave through the
    nearest-number rule that no part's phrase shows is anchored to ``fallback`` with no words."""
    out: dict[str, dict] = {}
    for status in sorted(vats):
        hit = next(({"part": part, "written": words[status]} for part, words_of in parts
                    if (words := meaning.vat_words(words_of)) and status in words), None)
        out[status] = hit or {"part": fallback, "written": ""}
    return out


def _kind_anchor(kind: str, parts: list[tuple[str, str]]) -> dict:
    """The part whose words name the value's kind and those words: the first part that names it whole (a per-area kind
    also needs its area words), else the first that has the kind's words at all."""
    for part, words_of in parts:
        if meaning.kind_attested(kind, words_of):
            return {"part": part, "written": meaning.kind_written(kind, words_of)}
    hit = next(({"part": part, "written": words} for part, words_of in parts
                if (words := meaning.kind_written(kind, words_of))), None)
    return hit or {"part": parts[0][0] if parts else "paragraph", "written": ""}


def _kind_state(kind: str, unit: str, context: str, *, whole: bool) -> tuple[str, str]:
    """How the words around a number (its row label, column header and caption; the quote of a quoted value) stand to the kind the agent
    registered it with, and the source's own words for a kind they state instead (round 22 U9, KTD8):

    - ``source``: they name the registered kind;
    - ``stated_different``: they name another kind that could be this number's — one of the unit's family, so a "מ״ר" in a heading does not
      make a ₪ amount an area — and not the registered one (the words are returned);
    - ``silent``: they name no kind, and the registered kind goes with the unit (``calc.kind_fits_unit``: ILS with value, price, rent and
      the other amounts, % with a rate). ``whole`` False: the words are a fragment, so their silence is no evidence;
    - ``model_asserted``: nothing supports the kind (the words are silent and it does not go with the unit, or they are a fragment)."""
    if meaning.kind_attested(kind, context):
        return "source", ""
    base = kind.removesuffix("_per_area")
    for other in meaning.KIND_WORDS:
        if other != base and calc.kind_in_unit_family(other, unit) and (words := meaning.kind_written(other, context)):
            return "stated_different", words
    return ("silent", "") if whole and calc.kind_fits_unit(kind, unit) else ("model_asserted", "")


def _settle_meaning(taken: dict, given: dict) -> tuple[dict, dict, dict]:
    """The value's meaning: what the source attests about the number is the source's (and fills what the model left
    unknown); what it does not is the model's (``model_asserted``); a contradiction is refused. With the meaning and
    its provenance, the anchor of each attribute that is the source's — ``{part, written}`` for the period, area
    basis, VAT and kind (round 12 U9, KTD9): the part of the source that states it and its words; none for one the
    model asserted."""
    written = taken["written"]
    q: meaning.Qualifiers = taken["qualifiers"]
    out = dict(given)
    prov: dict[str, str] = {}
    anchors: dict[str, dict] = {}
    units = taken["units"]
    if units and given["unit"] not in units:
        raise ToolError(f"המקור נותן ל-{written} יחידה {', '.join(UNIT_LABELS.get(u, u) for u in sorted(units))}, "
                        f"לא {UNIT_LABELS.get(given['unit'], given['unit']) or given['unit']}")
    prov["unit"] = "source" if units else "model_asserted"
    periods = q.keys("period")
    if periods:
        if given["period"] == "unknown" and len(periods) == 1:
            out["period"] = next(iter(periods))
        elif given["period"] not in periods:
            raise ToolError(f"המקור נותן ל-{written} תקופה "
                            + " / ".join(PERIOD_LABELS.get(p, p) for p in sorted(periods))
                            + f", לא {PERIOD_LABELS.get(given['period'], given['period']) or given['period']}")
        prov["period"] = "source"
        anchors["period"] = {"part": q.part_of("period", out["period"]) or "paragraph",
                             "written": q.found["period"][out["period"]]}
    else:
        prov["period"] = "model_asserted" if given["period"] in ("month", "year") else "not_stated"
    vats = taken["vat"]
    if vats:
        if given["vat"] in ("unknown", "not_applicable") and len(vats) == 1:
            out["vat"] = next(iter(vats))
        elif given["vat"] not in vats:
            raise ToolError(f"המקור נותן ל-{written} " + " / ".join(VAT_LABELS[v] for v in sorted(vats))
                            + f", לא {VAT_LABELS.get(given['vat']) or given['vat']}")
        prov["vat"] = "source"
        anchors["vat"] = (taken.get("vat_from") or {}).get(out["vat"]) or {"part": "paragraph", "written": ""}
    else:
        prov["vat"] = "model_asserted" if given["vat"] in ("included", "excluded") else "not_stated"
    bases = q.found.get("basis", {})
    said = (given.get("area_basis") or "").strip()
    if bases:
        keys = meaning.Qualifiers()
        for _, kind, key, w in meaning._scan(meaning._norm(said)):
            keys.add(kind, key, w)
        if not said:
            out["area_basis"] = meaning.display(" ".join(bases.values()))
        elif not keys.keys("basis") & set(bases):
            raise ToolError(f"המקור נותן ל-{written} בסיס שטח «{meaning.display(' '.join(bases.values()))}», לא «{said}»")
        else:
            out["area_basis"] = meaning.display(" ".join(bases.values()))
        prov["area_basis"] = "source"
        anchors["area_basis"] = {"part": q.part_of("basis", next(iter(bases))) or "paragraph",
                                 "written": out["area_basis"]}
    else:
        prov["area_basis"] = "model_asserted" if said else "not_stated"
    parts = taken.get("kind_parts") or [("paragraph", taken["kind_context"])]
    quoted = bool(taken.get("source_text"))  # a quote is a fragment of its sentence; a table cell has its row, header and caption
    prov["kind"], stated = _kind_state(given["kind"], out["unit"], taken["kind_context"], whole=not quoted)
    if quoted and (prov["kind"] != "source" or prov["unit"] == "model_asserted"):
        # the quote of a value is often a fragment of its sentence: the same meaning reader is asked about the number's own clause of the
        # source before the value is called the agent's (round 21 U3, KTD3, C1). A kind or a unit the clause states is the source's; a
        # clause that is the number's alone and names no kind is the source being silent about it (round 22 U9, KTD8)
        kind_stated, unit_stated, clause = meaning.confirmed_in_source(taken["source_text"], taken["forms"], taken["quote"],
                                                                       given["kind"], out["unit"])
        if prov["kind"] != "source" and kind_stated:
            prov["kind"], stated, parts = "source", "", [("sentence", clause)]
        elif prov["kind"] != "source" and clause:
            prov["kind"], stated = _kind_state(given["kind"], out["unit"], taken["kind_context"] + " " + clause, whole=True)
        if prov["unit"] == "model_asserted" and unit_stated:
            prov["unit"] = "source"
    if prov["kind"] == "source":
        anchors["kind"] = _kind_anchor(given["kind"], parts)
    elif prov["kind"] == "stated_different":
        anchors["kind_stated"] = {"written": stated}  # the source's words, for the conditional note; not a meaning_from anchor (``_new_value``)
    return out, prov, anchors


def _section_said(path: tuple) -> Attribution | None:
    """What the value's section path attests (a party's position, never an adoption): its deepest heading that
    says anything."""
    return next((a for h in reversed(path) if (a := attribution_in(h, adopted=False)) is not None), None)


def _settle_attribution(given: dict, said: Attribution | None, context: str) -> tuple[dict, dict]:
    """Who stated the value, how, and its scenario (KTD8): what the words around it attest is the source's (and
    fills a stance or speaker the model left unknown when the text gives one); what they do not is the model's
    (``model_asserted``: it counts as not stated in every reader and never makes the value uncertain, round 13 U5). A
    stance the text contradicts is kept as the model's, asserted, beside the text's words — never as found."""
    stance = given.get("stance") or "unknown"
    who = " ".join(str(given.get("stated_by") or "").split())
    scenario = " ".join(str(given.get("scenario") or "").split())
    named = said.stated_by if said is not None else ""
    prov: dict[str, str] = {}
    if said is not None and stance == "unknown" and said.stance is not None:
        stance, prov["stance"] = said.stance, "source"
    elif stance == "unknown":
        prov["stance"] = "not_stated"
    else:
        prov["stance"] = "source" if said is not None and stance in said.stances else "model_asserted"
    if not who:
        if named and said is not None and said.stance is not None:
            who, prov["stated_by"] = named, "source"
        else:
            prov["stated_by"] = "not_stated"
    else:
        prov["stated_by"] = "source" if named and names_match(who, named) else "model_asserted"
    if not scenario:
        prov["scenario"] = "not_stated"
    else:
        prov["scenario"] = "source" if names_match(scenario, context) else "model_asserted"
    return {"stance": stance, "stated_by": who, "scenario": scenario}, prov


# a stance or a speaker the model gave where the words around the number say nothing of who stated it or how: it counts
# as not stated (round 13 U5, KTD6; the value is not left uncertain for it), and the model is told how to take it as the
# source has it
MSG_ATTRIBUTION_UNSTATED = ("המקור אינו אומר מי קבע את הערך או באיזה מעמד (אומץ, טענה, הצעה או אומדן). אם אין לכך "
                            "בסיס במילים המצוטטות, בכותרת השורה או העמודה או בסעיף — קח את הערך שוב ב-take_value עם "
                            "stance=unknown ו-stated_by ריק: כך הוא נרשם כפי שהמקור כותב אותו")
MSG_SCENARIO_UNSTATED = ("המקור אינו מציין את התרחיש או המועד שנתת. אם המילים המצוטטות, כותרת השורה או העמודה או "
                         "הסעיף אינם מציינים תרחיש — קח את הערך שוב ב-take_value עם scenario ריק: תרחיש שאינו "
                         "במקור נחשב כלא צוין ואינו נכנס להשוואה בין ערכים")


def _attribution_line(value: calc.Value, said: Attribution | None) -> str:
    """The value's attribution as the tool reports it: the stance and speaker, what was asserted, and the source's
    own words when they say something else."""
    p = value.provenance
    out = f"ייחוס: {stance_label(value.stance)}" + (f" של {value.stated_by}" if value.stated_by else "")
    if value.scenario:
        out += f" | תרחיש/מועד: {_txt(value.scenario)}"
    asserted = [n for k, n in (("stance", "העמדה"), ("stated_by", "מי אמר"), ("scenario", "התרחיש"))
                if p.get(k) == "model_asserted"]
    if asserted:
        out += " | קביעה שלך שלא נמצאה במקור: " + ", ".join(asserted)
    if said is not None and (p.get("stance") == "model_asserted" or p.get("stated_by") == "model_asserted"):
        text_says = " / ".join(STANCE_LABELS[s] for s in sorted(said.stances)) + (
            f" של {said.stated_by}" if said.stated_by else "")
        out += f" | המקור מציג אותו כ: {text_says} («{_txt(_clip(said.evidence, 160))}»)"
    elif p.get("stance") == "model_asserted" or p.get("stated_by") == "model_asserted":
        out += " | " + MSG_ATTRIBUTION_UNSTATED
    elif value.stance == "unknown":
        out += " (המקור אינו אומר מי קבע את הערך או אם אומץ)"
    if p.get("scenario") == "model_asserted":
        out += " | " + MSG_SCENARIO_UNSTATED
    return out


def _given_meaning(meaning_: dict | None) -> dict:
    """The model's meaning of a value, its vocabulary checked."""
    given = dict(meaning_ or {})
    for name, allowed in (("kind", VALUE_KINDS), ("unit", UNIT_LABELS), ("period", PERIOD_LABELS),
                          ("vat", VAT_LABELS), ("role", calc.ROLE_LABELS)):
        if given.get(name) not in allowed:
            raise ToolError(f"meaning.{name} חייב להיות אחד מ: " + ", ".join(allowed))
    if (given.get("stance") or "unknown") not in STANCES:
        raise ToolError("meaning.stance חייב להיות אחד מ: " + ", ".join(STANCES))
    return given


def _settle(taken: dict, given: dict, path: tuple) -> tuple[dict, dict, dict, Attribution | None, dict]:
    """The value's meaning and attribution from what its source gave (``taken``, with the section path of the block
    holding it) and the model's meaning: ``(fields, provenance, who, said, anchors)``. The same for a fresh take and for a
    cached value's facts (KTD12), so a reused value is checked exactly as a fresh one."""
    fields, prov, anchors = _settle_meaning(taken, given)
    said = taken.get("said") or _section_said(path)
    context = "\n".join([taken.get("context") or "", *path])
    who, prov_who = _settle_attribution(given, said, context)
    return fields, prov | prov_who, who, said, anchors


def _new_value(ws: Workspace, src: Source, sid: str, taken: dict, settled: tuple, given: dict, label: str,
               path: tuple, reading_id: str | None) -> calc.Value:
    """The value a take builds, with the next V# and its facet. It changes nothing in the workspace: ``_register_value`` does,
    as the last step of the registration (round 14 U1, KTD2), so a step that fails in between leaves no orphan V#."""
    fields, prov, who, said, anchors = settled
    anchors = dict(anchors)
    stated = anchors.pop("kind_stated", None)  # the source's words for a kind it states differently: the value's, not an anchor
    where = taken["locator"]
    default = (f"{where['row']} — {where['column']}" if "row" in where
               else f"{VALUE_KINDS[fields['kind']]} {fields.get('subject') or ''}".strip())
    value = calc.Value(f"V{len(ws.values) + 1}", taken["value"], taken["written"], sid, src.document_id,
                       src.version_id, reading_id, src.title, src.location,
                       (label or "").strip() or default, fields["kind"], fields["unit"], fields["period"],
                       fields["vat"], fields.get("area_basis") or "", (fields.get("subject") or "").strip(),
                       fields["role"], prov, where, taken["quote"], taken["total"] or given["role"] == "total",
                       taken["table"], "approx" in taken["qualifiers"].keys("approx"),
                       section=" › ".join(path), stated_by=who["stated_by"], stance=who["stance"],
                       scenario=who["scenario"], attribution=said.evidence if said is not None else "",
                       meaning_from=dict(taken.get("meaning_from") or {}) | anchors, scale=int(taken.get("scale") or 1))
    if stated:
        value.stated["kind"] = stated["written"]
    value.facet = _value_facet(ws, src, taken, fields["kind"])
    return value


def _value_facet(ws: Workspace, src: Source, taken: dict, kind: str) -> dict:
    """The facet of a value as it enters the turn (round 13 U3, R1, R2, KTD2; round 14 U2, KTD3, KTD4): the source's own words
    that name the component it is for — a cell's row label (else its column header, else the caption as context, else a note of
    its table that names its row), or the purpose phrase of a quote's own sentence (else what the bounded read of the text above
    it accepts, ``taken["preceding"]``) — never the model's label or subject. Memoized in the workspace by place and family; a
    cached value (``taken["facet"]``) keeps the one it was cached with. A cell of a picture reads no text around it: not read,
    with the reason."""
    if taken.get("facet") is not None:
        return dict(taken["facet"])
    inp = taken.get("facet_in")
    if inp is None:
        return datum.not_read(datum.REASON_PICTURE_CELL)
    if inp["cell"]:
        family = datum.facet_family(kind, inp["row"], inp["header"], inp["caption"])
        key = _span_facet_key(value_span(src.version_id, src.reading_id, taken), family)
        return _facet_memo(ws, key, datum.read_cell, inp["row"], inp["header"], inp["caption"], inp["title"],
                           family=family, matrix=inp["matrix"], notes=inp.get("notes") or ())
    sentence = _own_sentence(taken)
    family = datum.facet_family(kind, sentence)
    return _facet_memo(ws, _quote_facet_key(src, taken, family), _read_quote_placed, sentence, inp["written"], family,
                       taken.get("preceding"))


def _quote_facet_key(src: Source, taken: dict, family: str) -> tuple:
    """The workspace key of a quote value's facet: the span of its number (block and offset, from the anchor) and its family
    (round 14 U2, U3, KTD5): one sentence written in two sections is two places with two contexts, and the facet read in one is
    never the other's; a stored measurement of the same number shares it. A quote located to block precision only has no span and
    keeps its quote's place."""
    span = datum.quote_span(src.version_id, src.reading_id, taken.get("anchor"))
    if span is not None:
        return _span_facet_key(span, family)
    return (*_locator_key(src.version_id, src.reading_id, taken["locator"]), family)


def _read_quote_placed(sentence: str, written: str, family: str, preceding: datum.Reach | None) -> dict:
    """``datum.read_quote``, with the place of a context sentence stamped on the facet it reads (round 14 U2, KTD3, R3): ``at``
    is the stub ``anchors.locate_quote`` gives for that sentence in its block, kept apart from the value's own anchor. A facet of
    the value's own sentence has none."""
    found = datum.read_quote(sentence, written, family, preceding)
    block, index = found.get("block"), found.get("sentence")
    if block is None or index is None or not isinstance(preceding, datum.Reach) or block not in preceding.blocks:
        return found
    sentences = datum.sentences_of(preceding.blocks[block])  # the stored text: same offsets as the unwrapped one
    if not 0 <= index < len(sentences):
        return found
    try:
        located = anchors.locate_quote([(block, preceding.blocks[block])], sentences[index], Decimal("NaN"))
    except anchors.AmbiguousQuote:  # no number to tell the occurrences apart by
        located = None
    if located is not None:
        found["at"] = {"segments": located.segments} if located.segments else {"blocks": located.blocks}
    return found


def _facet_known(ws: Workspace, src: Source, taken: dict, kind: str) -> bool:
    """Whether ``_value_facet`` settles a quote value's facet without the blocks above it: the workspace holds it for this place
    and family, or its own sentence settles it (an own phrase, a line's label or a description no rule ties to a component is read
    before any context, and a description borrows none: round 14 U2 follow-up)."""
    inp = taken.get("facet_in")
    if taken.get("facet") is not None or inp is None or inp["cell"]:
        return False
    sentence = _own_sentence(taken)
    family = datum.facet_family(kind, sentence)
    return (_quote_facet_key(src, taken, family) in ws.facets
            or datum.own_facet(sentence, inp["written"], family) is not None)


def _preceding_blocks(conn: Connection, cx, version_id, region: list, taken: dict, kind: str) -> datum.Reach | None:
    """The candidates above a quote value's own block, for its bounded context read (round 14 U2, R1, R2, KTD3): the earlier
    sentences of its block, the earlier paragraphs of its section and the section's heading or lead-in, nearest first, inside
    the value's appraisal unit (``cx``: its first block ends the read; ``reader.section_window``) and section. No fixed count of
    blocks: ``CONTEXT_CHARS["candidates"]`` caps the text, and a search that reaches it is capped (``datum.Reach``). None: not
    read (not a family that needs a facet, or no block of its own)."""
    if not region or not datum.needs_facet(datum.facet_family(kind, _own_sentence(taken))):
        return None
    holder = region[0]
    first = holder.block_index
    path = tuple(getattr(holder, "section_path", None) or ())
    window = reader.section_window(cx, first, path)
    lo = window[0] if window is not None else 0
    above = reader.blocks_above(conn, version_id, first, lo, CANDIDATE_BLOCKS) if first > lo else []
    return datum.reach([*above, holder], first, taken["quote"], path=path, lo=lo, chars=CONTEXT_CHARS["candidates"],
                       truncated=len(above) >= CANDIDATE_BLOCKS)


def _own_sentence(taken: dict) -> str:
    """The row or sentence a taken value is in: a table cell's row as read; for a quote, the whole sentence of its block that holds
    the quote, a PDF line wrap not ending it (``taken["sentence"]``, set where the block is at hand: ``_take_quote``,
    ``_facet_again``; round 14 follow-up), else the sentence of its source line that holds the quote (the quote itself when it spans
    sentences) — where a date of the value's own is looked for."""
    if taken.get("table"):
        return taken["quote"]
    if taken.get("sentence"):
        return taken["sentence"]
    line = (taken.get("context") or "").split("\n")[0]
    quote = meaning._flat(taken["quote"])
    return next((x for x in sections.sentences(line) if quote in meaning._flat(x)), taken["quote"])


def _date_inputs(ws: Workspace, conn: Connection, version_id, taken: dict, block_index: int | None,
                 vision: bool = False) -> tuple[chat_dates.UnitValuation, str | None, bool]:
    """What the value's date is resolved from beyond its own sentence (KTD4): the valuation of the unit holding its block,
    for a cell of a stored table the table's caption with the column's header (where a range may be stated), and whether
    the table holds comparables or transactions (round 11 U15: such a row never takes the unit's date). A table whose
    structure is not stored (a visual read) cannot be shown to be anything else, so it counts as one."""
    caption, comparables = None, False
    if taken.get("table"):
        st = None if vision else _table_at(ws, conn, version_id, taken["locator"]["table_index"])[0]
        if st:
            caption = " ".join(x for x in (st.get("caption"), taken["locator"].get("column")) if x) or None
            comparables = chat_dates.is_comparables_table(st.get("headers") or [], st.get("caption"),
                                                          *(st.get("title") or []))
        else:
            comparables = True
    return valuation_of(ws, conn, version_id, block_index), caption, comparables


def _stamp_date(ws: Workspace, value: calc.Value, taken: dict, inputs: tuple, path: tuple = ()) -> None:
    """Set ``value.as_of`` (round 11 U8, U15, KTD4, R5-R7): the date the server finds in the value's own row or sentence,
    in its table's range, or — for a value of the appraisal unit that is not a party's claim, another party's, a
    comparable, a transaction, a row of a comparables table or history — the unit's valuation date; unknown otherwise.
    ``path`` is the value's section path: under a prior-appraisal heading, or in a sentence of the past, the value is
    history and is never dated by the unit (round 12 U4, R16, KTD12); it then records why (`date_origin`). A sentence
    worded as a forecast is marked even when no date is found."""
    uv, caption, comparables = inputs
    sentence = _own_sentence(taken)
    found = chat_dates.resolve_as_of(sentence, table_caption=caption, stance=value.stance, role=value.role,
                                     stated_by=value.stated_by,
                                     stated_by_provenance=value.provenance.get("stated_by"), comparables=comparables,
                                     unit_valuation=uv, section_path=path, anchor=ws.time.anchor)
    value.as_of = datum_as_of(ws, sentence, found)


def _back_date_words(value: calc.Value) -> None:
    """A scenario that is nothing but the value's own date as the server resolved it ("למועד הקובע 30/06/2025" for a value
    whose ``as_of`` is that date) is no scenario at all (round 12 U12b, S3; `verify._date_backed` for the number check): the
    record states the date in ``as_of``, so the scenario is cleared and recorded as not stated. The judge then never reads it as
    "קביעת המודל, לא נמצא במקור", the value is not left uncertain for it, and the timeline's comparability key does not carry
    the date, so two appraisals of one property still fall into one history. A different date, or any word of the model's own,
    stays the model's. Set after the value is cached: the cache keeps only what the source attests."""
    if (value.provenance.get("scenario") == "model_asserted" and value.scenario
            and chat_dates.restates_as_of(value.scenario, value.as_of)):
        value.scenario = ""
        value.provenance["scenario"] = "not_stated"


def _value_report(ws: Workspace, value: calc.Value, said: Attribution | None, note: str | None,
                  context: list[str] | None = None) -> str:
    """What ``take_value`` reports about the value it registered."""
    where, prov = value.locator, value.provenance
    shown = [UNIT_LABELS.get(value.unit, ""), PERIOD_LABELS.get(value.period, ""), VAT_LABELS.get(value.vat, ""),
             f"בסיס שטח: {value.area_basis}" if value.area_basis else ""]
    place = (f"שורה «{where['row']}» (מס' {where['row_number']}), עמודה «{where['column']}»" if "row" in where
             else f"ציטוט «{_clip(value.quote, 200)}»")
    names = {"unit": "יחידה", "period": "תקופה", "vat": "מע\"מ", "area_basis": "בסיס שטח", "kind": "סוג",
             "stance": "עמדה", "stated_by": "מי אמר", "scenario": "תרחיש"}
    asserted = [k for k, p in prov.items() if p == "model_asserted"]
    lines = [f"{value.vid} נרשם: «{_txt(value.label)}» = {value.written} ({'; '.join(x for x in shown if x) or 'ללא יחידה'})"
             f" | סוג: {VALUE_KINDS[value.kind]} | תפקיד: {calc.ROLE_LABELS[value.role]}"
             + (f" | נושא: {_txt(value.subject)}" if value.subject else "") + (" | שורת סה\"כ" if value.total else "")
             + f" | {_txt(datum.line(value.facet))}",
             f"אומת ב-{value.source_id}: {_txt(place)}" + (f" | סעיף: {_txt(value.section)}" if value.section else ""),
             _attribution_line(value, said)]
    if value.scale != 1:
        lines.append(MSG_VALUE_SCALE.format(scale=calc.scaled_label("", value.scale), written=value.written,
                                            full=calc.fmt(value.value * value.scale)))
    if value.approx:
        lines.append("המקור כותב את הערך כמקורב.")
    lines.append(datum_line(value.as_of))
    if (src := ws.sources.get(value.source_id)) is not None and src.link:  # the appraisal is the property's (U12b)
        lines.append(src.link)
    # only the kind and the unit lower the certainty (round 13 U5, KTD6), and only when the source does not confirm them (stated differently,
    # or supported by nothing; round 22 U9, KTD8); any other attribute the model asserted is reported as not stated, which neither lowers it
    # nor enters a comparison or a calculation
    defining = value.unconfirmed()
    other = [names[k] for k in asserted if k not in calc.DEFINING_ATTRIBUTES]
    if defining:
        sure = "נמוכה יותר — " + "; ".join(attribute_clauses(ws, value.vid, defining))
    elif prov.get("kind") == "silent":
        sure = "הערך כתוב במקור; סוג הנתון לא נאמר במפורש בשורה או בכותרת (נרשם כ" + VALUE_KINDS.get(value.kind, value.kind) \
               + " בהתאם ליחידה), ואין בכך הפחתה בוודאות"
    else:
        sure = "כל התכונות שמגדירות את המספר נמצאו במקור"
    lines.append("ודאות: " + sure + (" | נחשבות כלא צוינו, ואינן מורידות את הוודאות: " + ", ".join(other) if other else ""))
    if note:
        lines.append(note)
    lines += context or []
    lines.append(f"סטטוס: {value_status(ws, value.vid)}")
    return "\n".join(lines)


# a value its source states in a scale (R14): the model computes with it as written and shows the result in that scale
MSG_VALUE_SCALE = ("קנה מידה: המקור מציין את הערך ב{scale} — {written} הוא {full} ביחידות מלאות. חשב איתו כפי שהוא "
                   "כתוב; תוצאת חישוב עליו תהיה באותו קנה מידה")


# --- one datum per source span (round 14 U1, R6, R7, KTD1, KTD2) -----------------------------------------------------------

REGISTRATION_LOG_MAX = 40  # failed take_value calls kept for the diagnostics of one turn
MSG_ALREADY_REGISTERED = ("המספר הזה כבר נרשם בתור הזה, באותו מקום במסמך: זה אותו ערך ולא ערך חדש, והמזהה שלו הוא זה "
                          "שבתחילת התשובה. התווית, הנושא והתפקיד של הרישום הראשון נשארו; לא נוצר מזהה נוסף.")
MSG_SPAN_CORRECTED = ("תיקון: {fields} של הערך הוחלפו לפי הרישום הזה. הרישום הראשון קבע אותם בלי שהמקור מעיד עליהם, "
                      "והמקור אינו סותר את הרישום הזה.")
MSG_SPAN_CONTRADICTS = ("המספר הזה כבר נרשם כ-{vid} באותו מקום במסמך, ו{field} של {vid} הוא מה שהמקור מעיד: {old}, לא "
                        "{new}. השתמש ב-{vid} כמו שהוא, או קח מספר אחר מהמקור.")
# the defining attributes a repeated registration may contradict, with how each is shown to the model
_DEFINING_LABELS = {"kind": ("הסוג", lambda x: VALUE_KINDS.get(x, x)), "unit": ("היחידה", lambda x: UNIT_LABELS.get(x, x))}
# the fields a repeated registration fills when the first left them empty and this one has them from the source: field -> empty
_FILLABLE = {"period": "unknown", "vat": "unknown", "area_basis": "", "stance": "unknown", "stated_by": "", "scenario": ""}


def value_span(version_id, reading_id: str | None, taken: dict) -> tuple | None:
    """Where the number of a taken value is in its source (round 14 U1, KTD1): a cell is its version, reading, table, row and
    column; a quote value is its version, reading, the block holding the number and the number's offset in it — the anchor
    ``take_value`` located it with. None when the quote was located only to block precision."""
    if taken.get("table"):
        where = taken["locator"]
        return datum.cell_span(version_id, reading_id, taken["table"][1], where["row_number"], where["column_number"])
    return datum.quote_span(version_id, reading_id, taken.get("anchor"))


def measurement_span(row, block_text: str | None = None) -> tuple | None:
    """Where the number of a stored measurement is in its source, in the same form as ``value_span`` so a V# and an M# of one
    number meet (KTD1). A table row: its table, row and the one cell of the row that holds the value (a row that holds it twice
    names no column here: no span). A text row: its quote located in ``block_text``, the text of its block, with
    ``anchors.locate_quote``; None without the block's text (the caller that reads blocks supplies it) or when the quote is not
    found in it, or found only to block precision."""
    value = getattr(row, "value", None)
    if value is None:
        return None
    wanted = abs(Decimal(str(value)))
    version, reading = row.version_id, getattr(row, "reading_id", None)
    if getattr(row, "table_index", None) is not None:
        cells, index = getattr(row, "row_cells", None), getattr(row, "row_index", None)
        if not cells or index is None or index < 0:
            return None
        hits = [i for i, c in enumerate(cells) if any(n[3] == wanted for n in _numbers_of(meaning._norm(str(c))))]
        return datum.cell_span(version, reading, row.table_index, index + 1, hits[0] + 1) if len(hits) == 1 else None
    block = getattr(row, "block_index", None)
    if block_text is None or block is None or not getattr(row, "quote", None):
        return None
    try:
        found = anchors.locate_quote([(block, block_text)], row.quote, wanted)
    except anchors.AmbiguousQuote:
        return None
    return datum.quote_span(version, reading, {"number": found.number}) if found is not None and found.number else None


def _adopt(first: calc.Value, name: str, value, prov: dict, anchors_: dict, *, anchor: bool = True) -> None:
    """Set one field of the value already registered to what this call settled, with its provenance (``prov[name]``) and, when
    ``anchor``, the place the source states it (``anchors_[name]``), as ``_registered_again`` takes a call's meaning."""
    setattr(first, name, value)
    first.provenance[name] = prov[name]
    if anchor and name in anchors_:
        first.meaning_from[name] = anchors_[name]
    if name == "kind":  # the source's words for a kind it states differently follow the kind adopted
        first.stated.pop("kind", None)
        if stated := anchors_.get("kind_stated"):
            first.stated["kind"] = stated["written"]


def _registered_at(ws: Workspace, span: tuple | None) -> calc.Value | None:
    """The value ``_registered_again`` answers a take at ``span`` with: the first value registered there, unless its reading is
    not clear — a reading that is not clear keeps its re-read: the take goes the whole way again (``_reread_value``, at most
    ``REREADS_PER_VALUE`` per place), and ``datum.relation`` rule 0 still joins the registrations into one class. None: a take
    there registers a new value."""
    first = next((v for i, v in ws.values.items() if span and ws.spans.get(i) == span), None)
    return None if first is None or reading_unclear(ws, first.vid) else first


def _registered_again(ws: Workspace, span: tuple | None, settled: tuple, facet_of: Callable[[str], dict]) -> tuple[calc.Value, str] | None:
    """The value already registered at ``span`` with what this call adds, and the note to tell the model; None when the span
    is not known or nobody registered it (round 14 U1, KTD2, R6). Run before any step that costs a read or a model call.

    - the first value's reading is not clear (``reading_unclear``): None, the call registers as before this round;
    - same meaning: the first value stays whole — its label, subject, role and stance are the first call's. Source-backed
      meaning this call adds fills only the fields the first left empty; the facet moves forward only (``datum.forward``). The
      one exception is the model's own unsupported stance, speaker or scenario on the first value, which this call replaces;
    - a defining attribute (``calc.DEFINING_ATTRIBUTES``: kind and unit) that differs: when the first value's field is not the source's
      (the model's own assertion, a kind the source states differently or is silent about; round 22 U9), this call corrects it and the
      value's facet is read again in the new family; when the source attests the first value's field, the call fails naming the first V#,
      and nothing changes.

    ``settled``: this call's ``_settle`` (fields, provenance, who, said, anchors); ``facet_of(kind)``: this call's facet for the
    kind it settled."""
    first = _registered_at(ws, span)
    if first is None:
        return None
    fields, prov, who, _, anchors_ = settled
    corrected = []
    for name in calc.DEFINING_ATTRIBUTES:
        if fields[name] == getattr(first, name):
            continue
        if first.provenance.get(name) == "source":
            label, show = _DEFINING_LABELS[name]
            raise ToolError(MSG_SPAN_CONTRADICTS.format(vid=first.vid, field=label, old=show(getattr(first, name)),
                                                        new=show(fields[name])), "span_contradiction")
        corrected.append(name)
    moved_family = datum.family_of(first.kind) != datum.family_of(fields["kind"])
    for name in corrected:
        _adopt(first, name, fields[name], prov, anchors_)
    mine = fields | who
    for name, empty in _FILLABLE.items():
        if getattr(first, name) in (empty, None) and mine.get(name) not in (empty, None) and prov.get(name) == "source" \
                and first.provenance.get(name) != "source":
            _adopt(first, name, mine[name], prov, anchors_)  # ``prov[name]`` is "source" here
    # who stated it, how, and the scenario: a word of the model's own that the source does not attest yields to this call's (the
    # model took the value again as the source has it, as ``MSG_ATTRIBUTION_UNSTATED`` and ``MSG_SCENARIO_UNSTATED`` tell it to)
    for name in ("stance", "stated_by", "scenario"):
        if first.provenance.get(name) == "model_asserted" and mine[name] != getattr(first, name):
            _adopt(first, name, mine[name], prov, anchors_, anchor=False)
    read = facet_of(fields["kind"])
    first.facet = read if corrected and moved_family else datum.forward(first.facet, read)
    note = MSG_ALREADY_REGISTERED
    if corrected:
        note += " " + MSG_SPAN_CORRECTED.format(fields=" ו".join(_DEFINING_LABELS[n][0] for n in corrected))
    return first, note


def _register_value(ws: Workspace, value: calc.Value, span: tuple | None, *, stub: dict | None = None,
                    settled: bool = False, uncertain: str | None = None, ambiguous: str | None = None) -> None:
    """The last step of a registration (round 14 U1, KTD2): the V# enters the workspace with its span, its anchor stub and
    how its region was read. Every step before it works on the value alone, so a failure in any of them leaves no V#, no S# the
    value was the reason for and no entry keyed by an id nobody holds."""
    ws.values[value.vid] = value
    if span is not None:
        ws.spans[value.vid] = span
    if settled:
        ws.settled_values.add(value.vid)
    if uncertain:
        ws.uncertain_values[value.vid] = uncertain
    if ambiguous:
        ws.ambiguous_units[value.vid] = ambiguous
    if stub:
        ws.anchors[value.vid] = stub


def tool_take_value(ws: Workspace, source: str, locator: dict | None, meaning_: dict | None, label: str = "") -> str:
    """Register a value of a source the turn read (V#) once the server verified that the number is the one the
    locator names: the cell at that row and column of the table, or a number inside an exact quote. ``source`` may
    also be a verified value cached in an earlier turn (``Q#`` from ``find_measurements``): it is reused, under the
    current permissions and only while its reading is current, with this turn's meaning checked against what its
    source attests (KTD12). A value read clearly is cached for later turns."""
    sid = (source or "").strip()
    loc = {k: v for k, v in (locator or {}).items() if v not in (None, "")}
    data = ws.handles.get(sid)
    if data is not None and data["kind"] == CACHE_HANDLE:
        return _take_cached(ws, sid, data, loc, _given_meaning(meaning_), label)
    src = ws.sources.get(sid)
    if src is None:
        raise ToolError(f"מקור לא מוכר: {source}. ערך נלקח רק ממקור S# שהוחזר בתור הזה (search או read)",
                        "unknown_source")
    if src.is_listing or src.version_id is None:
        raise ToolError("רשימת מסמכים אינה מקור לערך: קח את הערך ממקור במסמך", "not_a_source")
    full = src.text or (ws.sources[src.same_as].text if src.same_as in ws.sources else "")
    given = _given_meaning(meaning_)
    cell = any(k in loc for k in ("table", "row", "row_number", "column", "column_number"))
    if not cell and "quote" not in loc:  # cell fields with a quote: the cell is taken and the quote checked (round 17 U6)
        raise ToolError(MSG_LOCATOR_SHAPE.format(
            example=MSG_LOCATOR_EXAMPLE_TABLE if src.table_index is not None or src.vision is not None
            else MSG_LOCATOR_EXAMPLE_TEXT), "locator_shape")
    vision = src.vision is not None
    if vision and not cell:  # a quoted row of a table read by inspect is its cell, checked as one
        resolved = _vision_quote_cell(ws, src, loc)
        if resolved is not None:
            loc, cell = resolved, True
    with tenant_tx(ws.ctx) as conn:
        v = reader.version(conn, src.version_id)
        if v is None:
            raise ToolError(MSG_UNAVAILABLE, "unavailable")
        if src.reading_id is not None and v.reading_id != src.reading_id:
            raise ToolError(MSG_STALE_REF.format(source_id=sid), "stale_reference")
        if not cell and not vision and src.table_index is not None and "|" in (loc.get("quote") or ""):
            # a quoted table row is the row of the source's table that holds it, and the cell with the number is taken
            # as a cell (round 15 U1, KTD9)
            st, _ = _table_at(ws, conn, src.version_id, src.table_index)
            if st is None:
                raise ToolError("הטבלה לא נמצאה בקריאה הנוכחית של המסמך")
            loc, cell = _row_quote_locator(sid, st, loc), True
        elif not cell and not vision and src.table_index is not None and loc.get("quote"):
            # a quote of a table source that holds one cell of its table is that cell, with its table, row and stored column (round 22 U4, KTD4)
            st, _ = _table_at(ws, conn, src.version_id, src.table_index)
            resolved = _quote_cell_locator(src, full, st, loc) if st is not None else None
            if resolved is not None:
                loc, cell = resolved, True
        rows: list = []
        if cell and vision:
            taken = _take_vision_cell(ws, src, full, loc, _governing_note(ws, conn, src.version_id, src.block_start))
            at = None
        elif cell:
            taken = _take_cell(ws, conn, src, full, loc)
            _, block = _table_at(ws, conn, src.version_id, taken["locator"]["table_index"])
            rows, at = ([block] if block is not None else []), None
        else:
            blocks, served = None, {}
            if src.block_start is not None:
                rows = reader.blocks_between(conn, src.version_id, src.block_start,
                                             src.block_end if src.block_end is not None else src.block_start)
                # a bound page-text row is located in the form the source showed it (the quote is of that text) and mapped back to its stored
                # word span, so the anchor, the pages and the governing note are the stored ones (round 23 U4, KTD8)
                served = {r.block_index: r for r in _served_rows(ws, conn, src.version_id, rows)
                          if isinstance(r, _ServedBlock) and r.parts}
                blocks = [(r.block_index, (served[r.block_index].text if r.block_index in served else r.text) or "", r.page) for r in rows]
            remap = {i: r.parts for i, r in served.items()}
            taken = _take_quote(src, full, loc, blocks, lambda b: _governing_note(ws, conn, src.version_id, b),
                                table_notes=_quote_table_notes(ws, conn, src, loc), remap=remap)
            at = _value_blocks(taken.get("anchor") or {})
        reading_id = src.reading_id or v.reading_id
        # the number's place, from the anchor ``take`` located (no read): a value already registered there answers this call
        # (``_registered_again``), which needs neither its date inputs nor its context number
        span = value_span(src.version_id, reading_id, taken)
        registered = _registered_at(ws, span) is not None
        # the value's own region: read clearly, or read uncertainly at ingestion (then re-read, below, at most
        # REREADS_PER_VALUE times; the same region again is served from the stored readings)
        region = [r for r in rows if at is None or r.block_index in at]
        unclear = next((r for r in region if r.status == reader.UNCERTAIN), None)
        dating = None if registered else _date_inputs(
            ws, conn, src.version_id, taken, region[0].block_index if region else src.block_start, vision)
        cx = contexts_of(ws, conn, src.version_id, v.reading_id)
        if not cell:  # a quote's facet: the text above its own block, bounded by its unit and section, read once (round 14 U2, KTD3)
            kind = _settle_meaning(taken, given)[0]["kind"]  # the family is the settled kind's, as ``_value_facet`` reads it
            taken["preceding"] = (None if _facet_known(ws, src, taken, kind) else
                                  _preceding_blocks(conn, cx, src.version_id, region, taken, kind))
        here = None
        if cx.multi and not (cell and vision) and not registered:
            here = _context_number(ws, conn, cx, src.version_id, taken.get("anchor") or {},
                                   region[0].block_index if region else src.block_start)
            if here is None and len(src.contexts) == 1:
                here = src.contexts[0]
    # its section path: the block holding the number (a quote), or the table's block (a cell); else the source's
    holder = region[0] if region else None
    path = tuple(getattr(holder, "section_path", None) or ()) or ((src.section,) if src.section else ())
    settled = _settle(taken, given, path)
    # the number is already registered at this place (round 14 U1, KTD2): the first value is returned, before any re-read or
    # cache lookup of this call, and a stated facet this call read upgrades the first one's
    again = _registered_again(ws, span, settled, lambda kind: _value_facet(ws, src, taken, kind))
    if again is not None:
        return _value_report(ws, again[0], settled[3], again[1], _context_lines(ws, cx, again[0]))
    # the same value verified clearly before, in this reading: its earlier clear reading is reused (KTD12)
    known = _known_clear(ws, src.version_id, reading_id, taken["locator"], span) if unclear is not None else None
    reread = None
    if unclear is not None:
        if known is not None and known["record"].get("value") == str(taken["value"]):
            reread = (True, MSG_CACHED_CLEAR)
        else:
            key = _locator_key(src.version_id, reading_id, taken["locator"])
            reread = _reread_value(ws, v, unclear, taken["forms"], key)
    value = _new_value(ws, src, sid, taken, settled, given, label, path, reading_id)
    _stamp_date(ws, value, taken, dating, path)
    # the appraisal context of the value's own block or row (KTD7, R20), and its subject against it (R21)
    if vision:
        number = cx.at_block(src.block_start) if cx.multi else None
    else:
        number = here
    value.context, value.subject_from = value_context(cx, number), subject_from(cx, number, value.subject)
    note = reread[1] if reread is not None else None
    # how its own region was read, on the value itself, so coverage and the stored answer see it (R28); the workspace learns
    # it with the registration, in its last step
    settled_region, uncertain = False, None
    if "evidence" in taken:  # a cell of a table read by inspect: confirmed by OCR in its place, or uncertain (R18)
        from app.extraction.images import CELL_CONFIRMED

        note = taken["why"]
        if taken["evidence"]["status"] == CELL_CONFIRMED:
            settled_region = True
            value.reading = "clear"
        else:
            uncertain = note
            value.reading, value.reading_note = "uncertain", note
    elif reread is not None and not reread[0]:
        uncertain = reread[1]
        value.reading, value.reading_note = "uncertain", reread[1]
    elif taken.get("unconfirmed"):
        # a cell of a later page of a stored multi-page table, after a heading or paragraph: its scale, unit and notes are the
        # caption's only if the rows continue the table, which the stored data do not show (round 22 U3, KTD3)
        uncertain = taken["unconfirmed"]
        value.reading, value.reading_note = "uncertain", uncertain
        note = " ".join(x for x in (note, uncertain) if x)
    elif (reread is not None and reread[0]) or (
            region and all(r.status not in (reader.UNREAD, reader.UNCERTAIN) for r in region)):
        # confirmed by a focused re-read, or its own region read clearly (never a region left unread, which only a
        # visual transcription made now has read)
        settled_region = True  # its own region read clearly: another region of the source does not matter
        value.reading = "clear"
    elif src.status == "uncertain_reading":
        value.reading, value.reading_note = "uncertain", MSG_SOURCE_UNCERTAIN
    stub = anchors.source_stub(src)
    if stub is not None:  # where the value is, as taken (KTD1): the source's range, narrowed to its span or cell
        extra = dict(taken.get("anchor") or {})
        pages = extra.pop("pages", None)
        stub = {k: v for k, v in stub.items() if k != "row"} | extra | {
            "kind": "cell" if cell else "quote", "reading_id": value.reading_id}
        if value.meaning_from:  # where its meaning is stated: a cell's header is anchored with the cell (R10)
            stub["context"] = {k: (v["part"] if isinstance(v, dict) else v) for k, v in value.meaning_from.items()}
        if pages:
            stub["pages"] = pages
    if known is None and not taken.get("unit_ambiguous") and _cacheable(ws, value, src, reread):
        blocks = (min(r.block_index for r in region), max(r.block_index for r in region)) if region else None
        _store_verified(ws, value, _cache_doc(value, taken, path, stub, src, blocks), _cache_locator(value.locator, span))
    _back_date_words(value)
    ambiguous = (MSG_UNIT_AMBIGUOUS_WHY.format(units=", ".join(UNIT_LABELS.get(u, u) for u in taken["unit_ambiguous"]))
                 if taken.get("unit_ambiguous") else None)
    if ambiguous:
        note = " ".join(x for x in (note, MSG_UNIT_AMBIGUOUS_TAKEN.format(why=ambiguous)) if x)
    _register_value(ws, value, span, stub=stub, settled=settled_region, uncertain=uncertain,
                    ambiguous=ambiguous)  # the last step (KTD2)
    return _value_report(ws, value, settled[3], note, _context_lines(ws, cx, value))


# --- verified values cached per reading (KTD12, R29) --------------------------------------------------------------
#
# A value ``take_value`` verified and whose own region was read clearly is kept in ``verified_values``, keyed by its
# version, reading id and locator. Only what its source attests is kept: the number as written, its quote and
# locator, its anchor, the facts the source gave (``_facts_json``) and the fields of its record whose provenance is
# the source — never a field the model asserted, nor its label, subject or role. ``find_measurements`` lists the
# cached values of current readings the user may see as ``Q#``; ``take_value`` on a ``Q#`` re-checks the document
# and the reading, and settles that turn's meaning against the cached facts exactly as a fresh take would. A value
# of an earlier reading is never listed or reused: its key names the reading.

CACHE_HANDLE = "Q"
CACHED_MAX = 40  # cached values listed by one find_measurements call
SOURCE_FIELDS = ("kind", "unit", "period", "vat", "area_basis", "stance", "stated_by", "scenario")
RECORD_FIELDS = ("value", "value_text", "document_id", "version_id", "reading_id", "title", "location", "locator",
                 "quote", "total", "approx", "section", "attribution", "meaning_from", "facet")
MSG_CACHED_CLEAR = ("האזור של הערך נקרא בעיבוד המסמך בקריאה לא ודאית; הערך כבר אומת בקריאה ברורה של אותה קריאת "
                    "מסמך (ערך מאומת שמור), בלי קריאה חוזרת.")
MSG_CACHED_TAKEN = ("נלקח מערך מאומת שמור ({handle}) של הקריאה הנוכחית של המסמך, בלי קריאה נוספת; המשמעות שנתת "
                    "נבדקה שוב מול מה שהמקור מעיד.")
MSG_CACHED_LOCATOR = ("{handle} הוא הערך ב{where}; ה-locator שנתת מצביע על מקום אחר. ל-{handle} שלח locator ריק, "
                      "ולערך אחר קרא את המקור (read) וקח אותו ממנו.")
MSG_CACHED_HEADER = ("ערכים שאומתו בתורות קודמים מתוך הקריאה הנוכחית של המסמכים (Q#; לא נקראו מחדש בתור הזה). "
                     "לשימוש בחישוב רשום ערך ב-take_value עם source=Q# ו-locator ריק; המשמעות שתיתן תיבדק שוב מול "
                     "המקור:")


def _cacheable(ws: Workspace, value: calc.Value, src: Source, reread) -> bool:
    """Whether a value just taken may be cached: its reading is known, its own region was read clearly (as ingested,
    or settled by a focused re-read), and it is not a number of a visual transcription the turn made."""
    if not value.reading_id or value.reading == "uncertain" or value.vid in ws.uncertain_values:
        return False
    if value.reading != "clear" and src.status == "uncertain_reading":
        return False
    transcribed = src.method == "vision" and src.status == "uncertain_reading"
    return not transcribed or (reread is not None and bool(reread[0]))


def _facts_json(taken: dict, path: tuple) -> dict:
    """What the source gave about a taken value (``_take_cell`` / ``_take_quote``), with the section path of the
    block holding it, as JSON: everything ``_settle`` reads, so a later take settles its meaning against it."""
    said = taken.get("said")
    return {"written": taken["written"], "value": str(taken["value"]), "forms": sorted(taken["forms"]),
            "quote": taken["quote"], "qualifiers": {k: dict(v) for k, v in taken["qualifiers"].found.items()},
            "qualifier_parts": {k: dict(v) for k, v in taken["qualifiers"].parts.items()},
            "units": sorted(taken["units"]), "vat": sorted(taken["vat"]),
            "vat_from": {k: dict(v) for k, v in (taken.get("vat_from") or {}).items()},
            "kind_context": taken["kind_context"], "kind_parts": [list(x) for x in taken.get("kind_parts") or []],
            "locator": dict(taken["locator"]), "total": bool(taken["total"]),
            "table": list(taken["table"]) if taken.get("table") else None,
            "said": ({"stances": sorted(said.stances), "stated_by": said.stated_by, "evidence": said.evidence}
                     if said is not None else None),
            "context": taken.get("context") or "", "meaning_from": dict(taken.get("meaning_from") or {}),
            "anchor": dict(taken.get("anchor") or {}), "path": list(path), "scale": int(taken.get("scale") or 1),
            **({"sentence": taken["sentence"]} if taken.get("sentence") else {})}


def _facts_from_json(d: dict) -> tuple[dict, tuple]:
    """The facts of ``_facts_json`` back as ``take_value``'s ``taken``, and the section path."""
    said = d.get("said")
    taken = {"written": d["written"], "value": Decimal(d["value"]), "forms": frozenset(d["forms"]),
             "quote": d["quote"],
             "qualifiers": meaning.Qualifiers({k: dict(v) for k, v in d["qualifiers"].items()},
                                              {k: dict(v) for k, v in (d.get("qualifier_parts") or {}).items()}),
             "units": set(d["units"]), "vat": set(d["vat"]),
             "vat_from": {k: dict(v) for k, v in (d.get("vat_from") or {}).items()},
             "kind_context": d["kind_context"], "kind_parts": [tuple(x) for x in d.get("kind_parts") or []],
             "locator": dict(d["locator"]), "total": bool(d["total"]),
             "table": tuple(d["table"]) if d.get("table") else None,
             "said": (Attribution(frozenset(said["stances"]), said["stated_by"], said["evidence"])
                      if said is not None else None),
             "context": d.get("context") or "", "meaning_from": dict(d.get("meaning_from") or {}),
             "anchor": dict(d.get("anchor") or {}), "scale": int(d.get("scale") or 1)}
    if d.get("sentence"):  # the whole sentence of the quote's block (round 14 follow-up); an older record has none
        taken["sentence"] = d["sentence"]
    return taken, tuple(d.get("path") or ())


def _cache_record(value: calc.Value) -> dict:
    """A value's public record as cached: the number, its place and the fields its source attests, with their
    provenance; a field the model asserted, and the label, subject and role it named, are left out."""
    pub = value.public()
    record = {k: pub[k] for k in RECORD_FIELDS if k != "facet"}
    record["facet"] = dict(value.facet)  # whole: the reader's place (``at``) stays with the cached value, the public one has none
    # the anchors of the attributes the source states (round 12 U9): never one of a field the model asserted
    record["meaning_from"] = {k: v for k, v in pub["meaning_from"].items()
                              if not isinstance(v, dict) or value.provenance.get(k) == "source"}
    record |= {f: pub[f] for f in SOURCE_FIELDS if value.provenance.get(f) == "source"}
    record["provenance"] = {k: p for k, p in value.provenance.items() if p == "source"}
    record["facet_reader"] = FACET_READER_VERSION  # the reader that read its facet (round 14 U3, KTD5)
    return record


# v3: scoped table periods and clearly read governing currency as well as scale; v4 (round 12 U9, KTD9): the anchor
# {part, written} of each source attribute in the record and the part of each qualifier in the facts — a v3 row is
# inert (neither listed nor reused), never deleted; v5 (round 13 U3, KTD19): the facet — the component the source states
# for the value — in the record, so a v4 row (without one) is inert in the same way
VALUE_VERIFIER_VERSION = "value-v5"


def _cache_doc(value: calc.Value, taken: dict, path: tuple, stub: dict | None, src: Source,
               span: tuple[int, int] | None) -> dict:
    """The ``verified_values.value`` of a value: its record, the facts, its anchor stub and where it was read (the
    blocks of its own region, else the source's)."""
    start, end = span if span is not None else (src.block_start, src.block_end)
    return {"verifier_version": VALUE_VERIFIER_VERSION,
            "record": _cache_record(value), "facts": _facts_json(taken, path), "anchor": stub,
            "source": {"kind": src.kind, "section": src.section, "location": src.location, "block_start": start,
                       "block_end": end, "table_index": src.table_index,
                       "page_list": list((stub or {}).get("pages") or src.page_list or [])}}


def _locator_agrees(record: dict, loc: dict) -> bool:
    """Whether a locator given with a ``Q#`` names the cached value: every part given matches its place."""
    where = record.get("locator") or {}
    if loc.get("quote") and meaning._flat(loc["quote"]).strip() != meaning._flat(where.get("quote") or "").strip():
        return False
    for k in ("row", "column"):
        if loc.get(k) and (k not in where or _key(loc[k]) != _key(where[k])):
            return False
    for k in ("row_number", "column_number"):
        if loc.get(k) is not None:
            try:
                # a table served in the order it is drawn names its column by the position shown (``column_shown``, round 21 U8)
                if int(loc[k]) != (where.get("column_shown", where.get(k)) if k == "column_number" else where.get(k)):
                    return False
            except (TypeError, ValueError):
                return False
    if loc.get("number"):
        n = _parse_number(str(loc["number"]))
        if n is None or abs(n[1]) != abs(Decimal(str(record.get("value")))):
            return False
    return True


def _cached_value(conn: Connection, version_id, reading_id: str, locator: dict) -> dict | None:
    """The cached value at a version, reading and locator the user may see (row security), or None."""
    row = conn.execute(text(
        "SELECT value FROM verified_values WHERE version_id = :v AND reading_id = :r AND locator = CAST(:l AS jsonb)"
        " AND value->>'verifier_version' = :verifier_version"),
        {"v": version_id, "r": reading_id, "l": json.dumps(locator, ensure_ascii=False),
         "verifier_version": VALUE_VERIFIER_VERSION}).first()
    return row.value if row is not None else None


def _cache_locator(locator: dict, span: tuple | None) -> dict:
    """The locator a verified value is keyed by (round 14 U3, KTD5): a quote value's own locator with the place of its number in
    its block (``at``: block and character offset), so two numbers of one quote, and one sentence written in two sections, are
    two rows. A cell's locator already names its row and column; a quote located to block precision only has no span and keeps
    the quote alone. The record keeps the value's own locator."""
    if span is not None and span[0] == "quote":
        return dict(locator) | {"at": [span[3], span[4]]}
    return dict(locator)


def _cached_value_at(conn: Connection, version_id, reading_id: str, locator: dict, span: tuple | None) -> dict | None:
    """The cached value of a place (round 14 U3, KTD5): under the key with its number's place; a miss falls back to the
    quote-only key of a record written before the key named the number, but only when that quote holds a single number (with
    two, the older record may be either one's)."""
    key = _cache_locator(locator, span)
    found = _cached_value(conn, version_id, reading_id, key)
    if found is not None or key == locator:
        return found
    quote_ = locator.get("quote")
    if quote_ and len(_numbers_of(meaning._norm(quote_))) == 1:
        return _cached_value(conn, version_id, reading_id, locator)
    return None


def _known_clear(ws: Workspace, version_id, reading_id: str | None, locator: dict, span: tuple | None = None) -> dict | None:
    """The cached value at a locator of a reading, looked up in its own transaction (only for a value whose region
    was read uncertainly, which it spares a re-read); a cache that cannot be read only costs that re-read."""
    if not reading_id:
        return None
    try:
        with tenant_tx(ws.ctx) as conn:
            return _cached_value_at(conn, version_id, reading_id, locator, span)
    except Exception:  # noqa: BLE001
        logger.warning("verified value cache lookup failed")
        return None


def _store_verified(ws: Workspace, value: calc.Value, doc: dict, locator: dict | None = None) -> None:
    """Cache a verified value, in its own transaction, only while the user still sees its version and its reading
    is the current one. A cache that cannot be written only costs a later re-verification. ``locator``: the key
    (``_cache_locator``), else the value's own. The upsert skips a row of the same verifier version, so a facet this read states
    completes the row in place (``_write_facet``: only the facet and its reader), never replacing it."""
    key = locator if locator is not None else value.locator
    try:
        with tenant_tx(ws.ctx) as conn:
            v = reader.version(conn, value.version_id)
            if v is None or v.reading_id != value.reading_id:
                return
            written = conn.execute(text(
                "INSERT INTO verified_values (office_id, document_id, version_id, reading_id, locator, value)"
                " VALUES (app_office(), :d, :v, :r, CAST(:l AS jsonb), CAST(:x AS jsonb))"
                " ON CONFLICT (version_id, reading_id, locator) DO UPDATE SET value = EXCLUDED.value"
                " WHERE verified_values.value->>'verifier_version' IS DISTINCT FROM :verifier_version"),
                {"d": v.document_id, "v": value.version_id, "r": value.reading_id,
                 "l": json.dumps(key, ensure_ascii=False), "x": json.dumps(doc, ensure_ascii=False),
                 "verifier_version": VALUE_VERIFIER_VERSION}).rowcount
            # a row this call wrote holds ``doc``, whose record carries the current reader (``_cache_record``): there is nothing
            # to complete. Only a row of the same verifier version, which the upsert left as it was, may take the facet
            if written <= 0 and ((doc.get("record") or {}).get("facet") or {}).get("status") == datum.STATUS_STATED:
                _write_facet(conn, value.version_id, value.reading_id, key, doc["record"]["facet"])
    except Exception:  # noqa: BLE001 - the value itself is registered; only its reuse is lost
        logger.warning("verified value cache store failed")


def _write_facet(conn: Connection, version_id, reading_id: str, locator: dict, facet: dict) -> None:
    """Complete a cached record in place with a stated facet (round 14 U3, KTD5, R5): only ``value.record.facet`` and
    ``value.record.facet_reader`` of the row change (``jsonb_set``), the rest of the cached document stays, and nothing is deleted.
    It moves forward only: a row whose facet is already stated by this reader or a later one is left as it is. The one backward
    step (review #7): a facet that states nothing (``none`` or ``unparsed``, the careful read's verdict) replaces a stated
    context-tier facet an older reader wrote (``datum.reread``) — never a facet of this reader, an own-tier one or a row that holds
    no stated facet, so a late or repeated write cannot lower a row."""
    if facet.get("status") == datum.STATUS_STATED:
        guard = ("(value #>> '{record,facet,status}' IS DISTINCT FROM 'stated'"
                 " OR COALESCE((value #>> '{record,facet_reader}')::int, 1) < :ri)")
    else:
        guard = ("(value #>> '{record,facet,status}' = 'stated' AND value #>> '{record,facet,tier}' = :tier"
                 " AND COALESCE((value #>> '{record,facet_reader}')::int, 1) < :ri)")
    conn.execute(text(
        "UPDATE verified_values SET value = jsonb_set(jsonb_set(value, '{record,facet}', CAST(:f AS jsonb)),"
        " '{record,facet_reader}', CAST(:rj AS jsonb)) WHERE version_id = :v AND reading_id = :r"
        " AND locator = CAST(:l AS jsonb) AND value->>'verifier_version' = :verifier_version AND " + guard),
        {"f": json.dumps(facet, ensure_ascii=False), "rj": json.dumps(FACET_READER_VERSION), "ri": FACET_READER_VERSION,
         "v": version_id, "r": reading_id, "l": json.dumps(locator, ensure_ascii=False),
         "verifier_version": VALUE_VERIFIER_VERSION, "tier": datum.TIER_CONTEXT})


def _update_cached_facet(ws: Workspace, version_id, reading_id: str, locator: dict, facet: dict) -> None:
    """``_write_facet`` in its own tenant transaction, failing soft: a facet that cannot be written is read again next time."""
    try:
        with tenant_tx(ws.ctx) as conn:
            _write_facet(conn, version_id, reading_id, locator, facet)
    except Exception:  # noqa: BLE001 - the value is registered with the facet; only the cache misses it
        logger.warning("verified value cache facet update failed")


def _locator_key(version_id, reading_id: str | None, locator: dict) -> tuple:
    """A place in a reading as a hashable key: the version, the reading and the locator in one canonical spelling, so
    the same place read again or listed from the cache is one handle."""
    return (str(version_id), reading_id, json.dumps(locator, sort_keys=True, ensure_ascii=False))


def _cached_handle(ws: Workspace, version_id, reading_id: str, document_id, locator: dict) -> str:
    """The turn's ``Q#`` for a cached value, bound to its version and reading like every handle."""
    return ws.handle(CACHE_HANDLE, _locator_key(version_id, reading_id, locator), version_id=str(version_id),
                     reading_id=reading_id, document_id=str(document_id), locator=dict(locator))


def _cached_where(record: dict) -> str:
    where = record.get("locator") or {}
    return (f"שורה «{where.get('row')}», עמודה «{where.get('column')}»" if "row" in where
            else f"ציטוט «{_clip(where.get('quote') or record.get('quote') or '', 200)}»")


def _facet_stale(record: dict) -> bool:
    """Whether a cached record's facet is read again on a take (round 14 U3, KTD5, R5): it is not stated (nothing, or only
    an ambiguous read, was found when it was cached), or the reader that read it is older than ``FACET_READER_VERSION`` (a record
    without ``facet_reader`` is the round-13 reader's)."""
    return (record.get("facet") or {}).get("status") != datum.STATUS_STATED or _older_reader(record)


def _older_reader(record: dict) -> bool:
    """Whether a cached record's facet was written by a reader older than ``FACET_READER_VERSION`` (absent: the round-13 reader's)."""
    reader_ = record.get("facet_reader")
    return (reader_ if isinstance(reader_, int) else 1) < FACET_READER_VERSION


def _picture_cell(taken: dict) -> bool:
    """Whether a taken value is a cell of a picture (a table a visual reading transcribed): the text around it is not read."""
    return bool(taken.get("table")) and ("region" in taken["locator"] or bool((taken.get("anchor") or {}).get("vision")))


def _stated_at(ws: Workspace, span: tuple | None) -> bool:
    """Whether a value of the turn at ``span`` already has a stated facet: a take there again reads nothing."""
    return span is not None and any(ws.spans.get(i) == span and v.facet.get("status") == datum.STATUS_STATED
                                    for i, v in ws.values.items())


def _facet_again(ws: Workspace, src: Source, taken: dict, kind: str, doc: dict) -> dict | None:
    """The facet of a cached value read again from the stored blocks (round 14 U3, KTD5): the same reader as a fresh take
    (``_value_facet`` over the candidates ``_preceding_blocks`` builds, or a cell's row label, header, caption, title and notes), in
    one transaction, with no model call. None: nothing to read (a picture cell, a family that needs no facet, a table or blocks no
    longer stored) or the read failed — the cached facet stands."""
    if _picture_cell(taken) or not datum.needs_facet(datum.facet_family(kind, _own_sentence(taken))):
        return None
    taken["facet"] = None  # read as a fresh take reads it
    try:
        with tenant_tx(ws.ctx) as conn:
            if taken.get("table"):
                where = taken["locator"]
                st = _table_at(ws, conn, src.version_id, where["table_index"])[0]
                rows = (st or {}).get("rows") or []
                ri, ci = where["row_number"] - 1, where["column_number"] - 1
                if st is None or not 0 <= ri < len(rows):
                    return None
                cells = rows[ri].get("cells") or []
                headers = st.get("headers") or []
                taken["facet_in"] = _cell_facet_in(cells[0] if cells else "", headers[ci] if 0 <= ci < len(headers) else "",
                                                   st, cells)
            else:
                taken["facet_in"] = {"cell": False, "written": taken["written"]}
                place = doc.get("source") or {}
                if place.get("block_start") is None or _facet_known(ws, src, taken, kind):
                    taken["preceding"] = None
                else:
                    held = reader.blocks_between(conn, src.version_id, place["block_start"],
                                                 place["block_end"] if place.get("block_end") is not None else place["block_start"])
                    at = _value_blocks(taken.get("anchor") or {})
                    region = [r for r in held if at is None or r.block_index in at]
                    if not taken.get("sentence"):  # a record of an older reader: the quote's whole sentence, from its block now
                        sentence = _block_sentence([(r.block_index, r.text or "", r.page) for r in region],
                                                   taken.get("anchor") or {}, meaning._flat(taken["quote"]))
                        taken |= {"sentence": sentence} if sentence else {}
                    taken["preceding"] = _preceding_blocks(conn, contexts_of(ws, conn, src.version_id, src.reading_id),
                                                           src.version_id, region, taken, kind)
            return _value_facet(ws, src, taken, kind)
    except Exception:  # noqa: BLE001 - the cached facet stands; a read that failed is read again on the next take
        logger.warning("cached facet read again failed")
        return None


def _take_cached(ws: Workspace, handle: str, data: dict, loc: dict, given: dict, label: str) -> str:
    """Register a cached verified value (``Q#``) as this turn's V#: the document is resolved again under the current
    permissions and the reading must still be the one it was verified in; its meaning and attribution are settled
    from the cached facts and this turn's meaning, as on a fresh take; its anchor is the cached one. A facet that is not stated, or
    was read by an older reader, is read again from the stored blocks and, when it now states a component, written to the row in
    place (round 14 U3, KTD5, R5)."""
    with tenant_tx(ws.ctx) as conn:
        v = _bound(conn, ws, handle, data)
        doc = _cached_value(conn, v.version_id, data["reading_id"], data["locator"])
        cx = contexts_of(ws, conn, v.version_id, v.reading_id)
        place = (doc or {}).get("source") or {}
        number = _context_number(ws, conn, cx, v.version_id, (doc or {}).get("anchor") or {},
                                 place.get("block_start")) if doc is not None else None
        facts = _facts_from_json(doc["facts"])[0] if doc is not None else None
        dating = (_date_inputs(ws, conn, v.version_id, facts, place.get("block_start"), _picture_cell(facts))
                  if doc is not None else None)  # a picture's table is not a stored one
    if doc is None:
        raise ToolError(MSG_UNAVAILABLE, "unavailable")
    record = doc["record"]
    if not _locator_agrees(record, loc):
        raise ToolError(MSG_CACHED_LOCATOR.format(handle=handle, where=_cached_where(record)), "cached_locator")
    taken, path = _facts_from_json(doc["facts"])
    taken["facet"] = old = record.get("facet") or datum.not_read()  # the facet it was cached with (round 13 U3)
    settled = _settle(taken, given, path)
    place = doc.get("source") or {}
    # the source is built here and enters the workspace with the value, in the last step: a failure before it leaves no S#
    src = Source(sid="", document_id=v.document_id, version_id=v.version_id, title=v.title, section=place.get("section"),
                 location=place.get("location") or record.get("location") or "", kind=place.get("kind") or "context",
                 text=record.get("quote") or taken["quote"], block_start=place.get("block_start"),
                 block_end=place.get("block_end"), table_index=place.get("table_index"),
                 page_list=place.get("page_list") or None, partial_document=v.partial, reading_id=v.reading_id,
                 status="complete", tags={"document": ws.doc_handle(v.document_id)})
    span = value_span(v.version_id, v.reading_id, taken)
    if _facet_stale(record) and not _stated_at(ws, span):
        if _picture_cell(taken):  # its text was never read: that is what it says, and nothing is read for it now
            taken["facet"] = old if old.get("status") == datum.STATUS_STATED else datum.not_read(datum.REASON_PICTURE_CELL)
        elif (fresh := _facet_again(ws, src, dict(taken), settled[0]["kind"], doc)) is not None:
            # equal states take the new read (it carries its reason); never lower, except that the careful read's "nothing" or
            # "ambiguous" replaces a context facet a round-13 reader borrowed (review #7)
            taken["facet"] = datum.reread(fresh, old, older_reader=_older_reader(record))
            if taken["facet"] == fresh and (fresh["status"] == datum.STATUS_STATED or old.get("status") == datum.STATUS_STATED):
                _update_cached_facet(ws, v.version_id, v.reading_id, data["locator"], fresh)
    # the number is already registered at this place (round 14 U1, KTD2): no new source and no new id
    again = _registered_again(ws, span, settled, lambda kind: dict(taken["facet"]))
    if again is not None:
        return _value_report(ws, again[0], settled[3], again[1] + " " + MSG_CACHED_TAKEN.format(handle=handle),
                             _context_lines(ws, cx, again[0]))
    ws.touch(v.document_id, v.title, "retrieved", v.partial)
    value = _new_value(ws, src, "", taken, settled, given, label, path, v.reading_id)
    _stamp_date(ws, value, taken, dating, path)
    value.reading = "clear"  # cached only when its own region was read clearly
    value.context, value.subject_from = value_context(cx, number), subject_from(cx, number, value.subject)
    _mark(src, cx, [number] if number else [])
    stub = doc.get("anchor")
    if stub:
        stub = dict(stub) | {"reading_id": value.reading_id} | ({"unit": src.unit} if src.unit else {})
    _back_date_words(value)
    ws.adopt(src)  # the S# and the V# are made together, as the last steps
    value.source_id = src.sid
    _register_value(ws, value, span, stub=stub, settled=True)
    return _value_report(ws, value, settled[3], MSG_CACHED_TAKEN.format(handle=handle), _context_lines(ws, cx, value))


def _bound_parameter(ws: Workspace, number: Decimal, current: bool) -> str | None:
    """The pending parameter an assumption fills (round 7 KTD9): the one the reply to a clarification was bound to
    (``Workspace.binding``), when the assumption is the bound number quoted from the current message and no earlier
    assumption of the turn already fills it; else None."""
    b = ws.binding
    if not b or not current:
        return None
    try:
        bound = Decimal(str(b.get("value") or "").replace(",", ""))
    except ArithmeticError:
        return None
    name = b.get("parameter") or None
    if bound != number or any(a.parameter == name for a in ws.assumptions.values()):
        return None
    return name


def tool_assume(ws: Workspace, value: str, quote: str, label: str = "") -> str:
    """Register a number the user gave for a scenario (A#): quoted from a user message visible to the turn — the
    current one or an earlier one, never an answer's or a document's text."""
    wanted = _parse_number(value)
    if wanted is None:
        raise ToolError("value חייב להיות מספר אחד כפי שהמשתמש כתב אותו (למשל 5%)")
    q = meaning._flat(quote or "")
    if len(q) < 2:
        raise ToolError("quote חייב להיות ציטוט מדויק מהודעת המשתמש")
    found = None
    for msg in [m for m in ws.user_messages if m["current"]] + [m for m in reversed(ws.user_messages)
                                                                if not m["current"]]:
        if q in meaning._flat(msg["text"]):
            found = msg
            break
    if found is None:
        raise ToolError("הציטוט אינו מופיע בהודעות המשתמש בשיחה הזו. הנחה היא רק מספר שהמשתמש עצמו כתב (לא מתשובה "
                        "קודמת ולא ממסמך). אם התרחיש דורש מספר שהמשתמש לא נתן — שאל את המשתמש")
    hit = [n for n in _numbers_of(q) if n[3] == abs(wanted[1])]
    if not hit:
        raise ToolError(f"הערך {value} אינו בתוך הציטוט «{_txt(quote)}». הנחה נרשמת רק כפי שהמשתמש כתב אותה; אם "
                        "הערך לא נכתב — שאל את המשתמש")
    written, _, end, number = hit[0]
    after = q[end:end + 8]
    raw = meaning._norm(value)
    unit = ("percent" if "%" in raw or after.lstrip().startswith(("%", "אחוז")) else
            "ILS" if re.match(r"\s*(?:₪|ש\"ח|שקל)", after) else
            "sqm" if re.match(r"\s*מ\"ר", after) else "ratio")
    a = calc.Assumption(f"A{len(ws.assumptions) + 1}", number if wanted[1] >= 0 else -number, written, unit,
                        (label or "").strip() or "הנחת המשתמש", (quote or "").strip(), found["turn"], found["current"],
                        _bound_parameter(ws, number, found["current"]))
    ws.assumptions[a.aid] = a
    turn = "מההודעה הנוכחית" if a.current else "מהודעה קודמת"
    return (f"{a.aid} נרשם: «{_txt(a.label)}» = {written}{'%' if unit == 'percent' else ''} — הנחת המשתמש, מצוטטת "
            f"{turn}: «{_txt(a.quote)}». בחישוב: {a.aid}% הוא {a.aid}/100. בתשובה הצג אותה כהנחת המשתמש, בנפרד "
            "מנתוני המסמך, וצטט [" + a.aid + "].")


def _operand(ws: Workspace, i: str) -> calc.Operand:
    if i in ws.values:
        return ws.values[i].operand()
    if i in ws.assumptions:
        return ws.assumptions[i].operand()
    if i in ws.computations:
        return calc.result_operand(i, ws.computations[i].outcome)
    if i in ws.measurements:
        m = ws.measurements[i]
        r = m.row
        return calc.operand(i, r.value, r.unit, period=r.period, vat=r.vat, basis=meaning.basis_key(r.area_basis),
                            kind=r.metric_kind, subject=r.subject or "",
                            total=bool(calc.TOTAL_WORDS.search(meaning._norm(r.metric or ""))),
                            table=(str(m.version_id), r.table_index) if r.table_index is not None else None,
                            group=r.value_role, same=str(m.id), approx=r.value_form != "exact",
                            context=(m.context["key"], m.context["described"])
                            if contexts.context_subject(m.context) else None)
    raise ToolError(f"מזהה לא מוכר: {i}. אפשר להשתמש רק במזהים שנרשמו בתור הזה: V# (take_value), A# (assume), "
                    "M# (find_measurements), C# (calculate)")


def allowed_contexts(ws: Workspace) -> list[frozenset[str]] | None:
    """The appraisal contexts a calculation may combine (KTD7, KTD1): for each frozen calculation component that
    compares subjects, the contexts its ``compares`` names, when every subject it compares names one; None when the
    context checks are not enforced."""
    if not contexts.enforced():
        return None
    allowed = []
    for item in ws.requirement_items:
        compared = [x for x in item.get("compares") or [] if x.strip()]
        if item.get("kind") != "calculation" or len(compared) < 2:
            continue
        keys: set[str] = set()
        for cx in ws.contexts.values():
            if cx.multi:
                keys |= {cx.key(n) for x in compared for n in cx.named(x)}
        if keys:
            allowed.append(frozenset(keys))
    return allowed


def _leaves(ws: Workspace, ids) -> list[str]:
    """The registered values and measurements an expression rests on, through earlier results."""
    out: list[str] = []
    for i in ids:
        if i in ws.computations:
            out += ws.computations[i].leaves
        elif i not in out:
            out.append(i)
    return list(dict.fromkeys(out))


def _check_available(ws: Workspace, leaves: list[str]) -> None:
    """Every document a value rests on is still visible, in the reading the value was taken from (R13)."""
    versions: dict[str, list[str]] = {}
    for i in leaves:
        vid = (ws.values[i].version_id if i in ws.values else ws.measurements[i].version_id if i in ws.measurements
               else None)
        if vid is not None:
            versions.setdefault(str(vid), []).append(i)
    if not versions:
        return
    with tenant_tx(ws.ctx) as conn:
        for vid, ids in versions.items():
            v = reader.version(conn, UUID(vid))
            if v is None:
                raise ToolError(MSG_VALUE_UNAVAILABLE.format(ids=", ".join(ids)))
            stale = [i for i in ids if i in ws.values and ws.values[i].reading_id is not None
                     and ws.values[i].reading_id != v.reading_id]
            if stale:
                raise ToolError(MSG_VALUE_STALE.format(ids=", ".join(stale)))


def _reproduces(ws: Workspace, out: calc.Outcome, leaves: list[str]) -> dict | None:
    """A number the report writes that the result equals at the precision it is written in, in the turn's sources of
    the documents the inputs come from (not one of the inputs themselves)."""
    versions = {ws.values[i].version_id for i in leaves if i in ws.values}
    versions |= {ws.measurements[i].version_id for i in leaves if i in ws.measurements}
    inputs = set()
    for i in leaves:
        if i in ws.values:
            inputs |= numbers_in(ws.values[i].written)
        elif i in ws.measurements:
            inputs |= numbers_in(ws.measurements[i].row.value_text or "")
    texts = [(s.sid, s.text) for s in ws.sources.values() if s.version_id in versions and not s.is_listing]
    texts += [(i, ws.measurements[i].row.quote or "") for i in leaves if i in ws.measurements]
    for sid, t in texts:
        for written, _, end, _ in _numbers_of(t):
            if numbers_in(written) & inputs or len(re.sub(r"\D", "", written).lstrip("0")) < 3:
                continue
            percent = t[end:end + 2].lstrip().startswith("%")
            scale = 1 if percent else calc.scale_after(t, end - len(written), end)[0]
            if calc.display_matches(written, percent, out.value, out.dims, out.kind, scale, out.scale):
                return {"source": sid, "as_written": written + ("%" if percent else "")}
    return None


# --- a stated amount beside a rounded rate (round 7 U7, KTD8, R23) ------------------------------------------------

# words that name no quantity, left out when an amount's line is matched with the rate's words
_FUNCTION_WORDS = frozenset({"של", "את", "על", "עם", "או", "גם", "כי", "אם", "לפי", "בין", "כל", "זה", "זו", "הוא",
                             "היא", "הם", "הן", "אשר", "כפי", "לכל", "בכל", "מתוך", "עבור"})
_HEBREW_WORD = re.compile(r"[א-ת][א-ת\"״׳']*")
MSG_NEAR_MISS = ("שים לב — סכום כתוב לעומת שיעור מעוגל: התוצאה חושבה מ-{rate} (שיעור כתוב «{written}%», שטווח העיגול "
                 "שלו {low}%–{high}%), וב-{source}, באותו מקום במסמך, כתוב סכום מפורש בתוך הטווח הזה: {amount} "
                 "(«{quote}»). אלא אם המשתמש ביקש לחשב לפי השיעור — קח את הסכום הכתוב ב-take_value מ-{source} והשתמש "
                 "בו במקום התוצאה הזו; אל תציג את התוצאה הזו כסכום שבמסמך.")
MSG_NEAR_MISS_CARRIED = ("שים לב: החישוב נשען על {origin}, שחושב משיעור מעוגל ({rate}) בעוד שהמסמך מציין סכום מפורש "
                         "לאותו נתון: {amount} ({source}, «{quote}»). אלא אם המשתמש ביקש לחשב לפי השיעור — קח את "
                         "הסכום הכתוב ב-take_value וחשב ממנו מחדש.")
MSG_STATED_DIFFERS = ("באותו מקום במסמך ({source}) כתוב סכום {amount} («{quote}») מחוץ לטווח העיגול של {rate} "
                      "({low}%–{high}%): הוא אינו עיגול של החישוב הזה. אם תציג את התוצאה, הצג לידה גם את הסכום הכתוב "
                      "ואת הפער ביניהם.")


def _content_words(text_: str) -> set[str]:
    """The words of a text that may name a quantity, with one conjunction and one prefix letter dropped, so
    "והרווח" and "רווח" meet."""
    out = set()
    for w in _HEBREW_WORD.findall(meaning._norm(text_ or "")):
        w = w.strip("\"'״׳")
        if len(w) >= 4 and w[0] == "ו":
            w = w[1:]
        if len(w) >= 4 and w[0] in "הבלמשכ":
            w = w[1:]
        if len(w) >= 3 and w not in _FUNCTION_WORDS:
            out.add(w)
    return out


def _line_of(text_: str, start: int, end: int) -> str:
    a = text_.rfind("\n", 0, start) + 1
    b = text_.find("\n", end)
    return " ".join(text_[a:b if b >= 0 else len(text_)].split())


def _last_part(section: str | None) -> str:
    return " ".join((section or "").split("›")[-1].split())


def _rate_sources(ws: Workspace, value: calc.Value) -> list[Source]:
    """The rate's own source and the turn's other sources of its section, in its document and appraisal context."""
    out = []
    for s in ws.sources.values():
        if s.is_listing or s.version_id != value.version_id:
            continue
        if s.sid != value.source_id and not (value.section and _last_part(s.section) == _last_part(value.section)):
            continue
        if value.context and s.contexts and value.context.get("number") not in s.contexts:
            continue
        out.append(s)
    return out


def user_gave(ws: Workspace, written: str) -> bool:
    """The user wrote the rate itself ("לפי 17%"): a calculation by it is what was asked."""
    raw = re.sub(r"[^\d.]", "", written or "").strip(".")
    if not raw:
        return False
    pattern = re.compile(r"(?<![\d.,])" + re.escape(raw) + r"(?:\.0+)?\s*(?:%|אחוז)")
    return any(pattern.search(meaning._norm(m["text"])) for m in ws.user_messages)


def _amount_record(sid: str, written: str, line: str, number: Decimal, rate: calc.Value,
                   interval: tuple[Decimal, Decimal], exact: Decimal, low: Decimal, high: Decimal) -> dict:
    return {"amount": written, "value": str(number), "source": sid, "quote": _clip(line, 300), "rate": rate.vid,
            "rate_written": rate.written, "interval": [str(interval[0]), str(interval[1])], "computed": str(exact),
            "range": [str(low), str(high)], "from": None}


def _near_misses(ws: Workspace, node, operands: dict[str, calc.Operand], justification: str | None,
                 label: str) -> tuple[dict | None, dict | None]:
    """For each product of a document rate (a V# percentage, never the user's A#) in the expression, the amount its
    source and section state for the same quantity (the amount's line shares a word with the rate's quote or label,
    or with the calculation's label): within the product's range over the rate's rounding interval — a near-miss
    (``explicit_amount_available``); else, within a factor of two of it, an amount that differs (a material gap).
    An amount the product reproduces at its precision, or an input's own number, is neither. Amounts are compared
    in units: the product as value × its scale, each stated amount as its number × the scale its source states it in
    (``calc.stated_scale``: its own scale word, else a note of its line, else of the line above that governs it:
    ``_note_above``), so a cost of a table "(באלפי ₪)" times a
    rate meets a profit stated in full ₪ in the prose; the record's ``value``, ``computed`` and ``range`` are in
    units."""
    near = differs = None
    near_key = differs_key = None
    for product, rid in calc.rate_products(node):
        rate = ws.values.get(rid)
        if rate is None or rate.unit != "percent" or user_gave(ws, rate.written):
            continue
        interval = calc.rounding_interval(rate.written)
        if interval is None:
            continue
        try:
            out = calc.evaluate(product, operands, justification)
            ends = [calc.evaluate(product, operands | {rid: replace(operands[rid], value=x)}, justification)
                    for x in interval]
        except calc.CalcError:
            continue
        # compared in units: the product is in its base's scale (a cost of a table "(באלפי ₪)" times a rate stays in
        # thousands), and each stated amount in the scale its own source states (R14)
        exact = out.value * out.scale
        low, high = min(e.value * e.scale for e in ends), max(e.value * e.scale for e in ends)
        own = set()
        for i in _leaves(ws, calc.ids_of(product)):
            if i in ws.values:
                own |= numbers_in(ws.values[i].written)
        words = _content_words(" ".join([rate.quote, rate.label, label]))
        seen = set()
        for src in _rate_sources(ws, rate):
            full = src.text or ""
            for written, start, end, number in _numbers_of(full):
                if numbers_in(written) & own or len(re.sub(r"\D", "", written).lstrip("0")) < 3:
                    continue
                if full[end:end + 2].lstrip().startswith("%"):
                    continue
                line = _line_of(full, start, end)
                if (written, line) in seen:
                    continue
                seen.add((written, line))
                overlap = len(words & _content_words(line))
                stated = calc.stated_scale(full, start, end, line, _note_above(full, start))
                if not overlap or calc.display_matches(written, False, out.value, (), scale=stated,
                                                       source_scale=out.scale):
                    continue
                number *= stated
                key = (overlap, src.sid == rate.source_id, -abs(number - exact))
                record = _amount_record(src.sid, written, line, number, rate, interval, exact, low, high)
                if low <= number <= high:
                    if near_key is None or key > near_key:
                        near, near_key = record, key
                elif abs(exact) / 2 <= number <= abs(exact) * 2 and (differs_key is None or key > differs_key):
                    differs, differs_key = record, key
    return near, (None if near is not None else differs)


# the scale of a result (R14): kept from inputs of one scale, and inputs of different scales brought to units first
MSG_RESULT_SCALE = ("התוצאה ב{unit}, כקנה המידה שבו המקור מציין את הקלטים (= {full} ביחידות מלאות): הצג אותה כך, או "
                    "ביחידות מלאות, או במילת סדר גודל לפי הערך המלא (למשל מיליון)")
MSG_RESCALED = ("הקלטים מצוינים במקורות בקני מידה שונים (למשל באלפי ₪ וב-₪): כל אחד הובא ליחידות מלאות לפני החישוב, "
                "והתוצאה ביחידות מלאות")


def _input_name(ws: Workspace, i: str) -> str:
    """The label a value, assumption, result or measurement id is shown under in a calculation."""
    if i in ws.values:
        return ws.values[i].label
    if i in ws.assumptions:
        return ws.assumptions[i].label
    if i in ws.computations:
        return ws.computations[i].label
    return ws.measurements[i].row.metric if i in ws.measurements else i


def _computation_inputs(ws: Workspace, out) -> list[dict]:
    """The record of each input a calculation read (``out.inputs``), as ``tool_calculate`` and the verifier's derived
    results (``verify._derived_computation``) keep it."""
    inputs = []
    for i in out.inputs:
        kind = ("value" if i in ws.values else "assumption" if i in ws.assumptions else
                "computation" if i in ws.computations else "measurement")
        value = (ws.values[i].value if i in ws.values else ws.assumptions[i].value if i in ws.assumptions
                 else ws.computations[i].value if i in ws.computations else ws.measurements[i].row.value)
        entry = {"id": i, "label": _input_name(ws, i), "kind": kind, "value": None if value is None else str(value),
                 "display": None if value is None else calc.fmt(Decimal(str(value)))}
        if i in ws.values:
            entry |= {"source_id": ws.values[i].source_id, "value_text": ws.values[i].written,
                      "certainty": ws.values[i].certainty}
        elif i in ws.assumptions:
            entry |= {"quote": ws.assumptions[i].quote, "value_text": ws.assumptions[i].written}
        elif i in ws.measurements:
            entry |= {"value_text": ws.measurements[i].row.value_text}
        inputs.append(entry)
    return inputs


def _leaf_sources_docs(ws: Workspace, leaves: list[str]) -> tuple[list[str], int]:
    """The source ids the ``leaves`` (values and measurements) rest on, in order, and how many documents hold them."""
    sources = list(dict.fromkeys([ws.values[i].source_id for i in leaves if i in ws.values]
                                 + [i for i in leaves if i in ws.measurements]))
    docs = {str(ws.values[i].document_id) for i in leaves if i in ws.values}
    docs |= {str(ws.measurements[i].document_id) for i in leaves if i in ws.measurements}
    return sources, len(docs)


def computation_operands(ws: Workspace, ids: list[str]) -> dict[str, calc.Operand]:
    """The operands of the turn's ids a calculation reads (``ToolError`` for an id the turn does not hold)."""
    return {i: _operand(ws, i) for i in ids}


def carry_consulted(ws: Workspace, out) -> frozenset:
    """The (id, attribute) pairs a calculation read, with those of the earlier results it was built on (``calc.Outcome.consulted``): a
    result over C1 rests on what C1 read, so V1's kind that C1 consulted still counts (round 22 U9, KTD8). Set on ``out`` once."""
    out.consulted = frozenset(out.consulted).union(*(ws.computations[i].outcome.consulted for i in out.inputs if i in ws.computations))
    return out.consulted


def register_computation(ws: Workspace, node, out, leaves: list[str], *, label: str = "", label_prefix: str = "",
                         kind: str, justification: str | None = None, justified: bool = False,
                         notes: list[str] | None = None, reproduces: dict | None = None, near: dict | None = None,
                         differs: dict | None = None, conditional_in_note: bool = False) -> tuple[calc.Computation, str]:
    """Register the C# of an evaluated expression (``out``, over the turn's ``leaves``), as ``tool_calculate`` and the
    verifier's derived results (``verify._derived_computation``) both keep it (round 13): the record of its inputs, the
    sources and documents behind them, the conditional that an uncertain input makes (R28; the reason each is uncertain,
    when the turn knows it) and the ``calc.Computation`` in ``ws.computations``. Returns it with its conditional text
    ("מותנה: ..." and the justification when the mix needed one; empty when it is not conditional), which a derived result
    also keeps in its note (``conditional_in_note``). ``label`` is the model's own name for it, else ``label_prefix`` and
    the formula by the labels of the inputs."""
    def name(i: str) -> str:
        return _input_name(ws, i)

    inputs = _computation_inputs(ws, out)
    sources, documents = _leaf_sources_docs(ws, leaves)
    consulted = carry_consulted(ws, out)
    uncertain = uncertain_inputs(ws, leaves, consulted)
    if uncertain:
        # why each is uncertain, when the turn knows (an unclear region, a cell OCR did not confirm in its place)
        why = [f"{i}: {r}" for i in uncertain
               if (r := ws.uncertain_values.get(i) or (ws.values[i].reading_note if i in ws.values else "") or uncertain_reason(ws, i, consulted))]
        out.conditional.append(MSG_UNCERTAIN_INPUTS.format(ids=", ".join(uncertain))
                               + (f" ({'; '.join(why)})" if why else ""))
    conditional = ("מותנה: " + "; ".join(out.conditional)
                   + (f" — לפי ההצדקה: {justification}" if justified else "")
                   if out.conditional else "")
    note = " ".join([*(notes or []), *([conditional] if conditional and conditional_in_note else [])])
    c = calc.Computation(f"C{len(ws.computations) + 1}",
                         (label or "").strip() or label_prefix + calc.render(node, name, True),
                         calc.render(node, lambda i: i), calc.render(node, lambda i: f"«{name(i)}»", True), out,
                         inputs, sources, documents, kind, justification if justified else None, note, reproduces,
                         [lf.id for lf in out.leaves], near, differs, calc.applied_rates(node), list(uncertain))
    ws.computations[c.cid] = c
    return c, conditional


def tool_calculate(ws: Workspace, expression: str, label: str = "", justification: str | None = None) -> str:
    """Evaluate an expression over the turn's ids (``app.chat.calc``) and register the result as a C#."""
    try:
        node = calc.parse(expression)
    except calc.CalcError as e:
        raise ToolError(f"ביטוי לא תקין: {e}") from None
    ids = list(dict.fromkeys(calc.ids_of(node)))
    operands = computation_operands(ws, ids)
    leaves = _leaves(ws, ids)
    unclear = [i for i in leaves if i in ws.ambiguous_units]
    if unclear:  # a value whose unit the source leaves ambiguous is never an input (round 15 U1, KTD9)
        raise ToolError(MSG_UNIT_AMBIGUOUS_INPUT.format(
            inputs=", ".join(f"{i} «{_input_name(ws, i)}» ({ws.ambiguous_units[i]})" for i in unclear)), "unit_ambiguous")
    _check_available(ws, leaves)
    justification = (justification or "").strip() or None
    try:
        out = calc.evaluate(node, operands, justification, contexts=allowed_contexts(ws))
    except calc.CalcError as e:
        raise ToolError(f"אי אפשר לחשב: {e}") from None

    notes = []
    if out.approx:
        notes.append("חלק מהערכים מקורבים; התוצאה מקורבת בהתאם.")
    if out.rescaled:
        notes.append(MSG_RESCALED)
    elif out.scale != 1:
        notes.append(MSG_RESULT_SCALE.format(unit=calc.scaled_label(calc.unit_label(out.dims, out.period), out.scale),
                                             full=calc.fmt(out.value * out.scale)))
    consulted = carry_consulted(ws, out)
    for i in leaves:  # what the source does not confirm about an input, among what this calculation read (round 22 U9, KTD8)
        if i in ws.values and (reason := uncertain_reason(ws, i, consulted)):
            notes.append(f"ודאות נמוכה יותר — {i}: {reason}.")
    lost = [i for i in leaves if i in ws.measurements and anchor_lost(ws.measurements[i].row)]
    if lost:
        notes.append("מיקומם של חלק מהערכים במסמך אבד בעיבוד מחדש (" + ", ".join(lost)
                     + "); הם אינם מאומתים מול הקריאה הנוכחית.")
    # values chosen from a listing whose later pages were not read: more matching values may exist
    unread = [ws.listings[k] for k in {k for i in leaves if i in ws.measurements for k in ws.measurements[i].listings}
              if len(ws.listings[k]["pages_read"]) < ws.listings[k]["pages"]]
    if unread:
        notes.append("חישוב חלקי: לא נקראו כל העמודים של הנתונים המתאימים (נקראו "
                     f"{len(unread[0]['pages_read'])} מתוך {unread[0]['pages']}).")
    # each input's status; using a value never waits for a review (R25), but an uncertain one makes the result
    # conditional, never certain (R28)
    statuses = [f"{i} {value_status(ws, i)}" for i in leaves if i in ws.values or i in ws.measurements]
    if statuses:
        notes.append("מצב הערכים: " + "; ".join(statuses) + ".")
    justified = bool(out.conditional)  # conditional on the justification the mix needed
    reproduces = None if out.assumptions else _reproduces(ws, out, leaves)
    kind = "scenario" if out.assumptions else "reproduces_report_value" if reproduces else "computed"
    # a product of a rounded document rate beside the amount its section states (KTD8): reported and kept in the
    # record, never substituted; a result built on such a product carries it
    near, differs = _near_misses(ws, node, operands, justification, label)
    if near is not None:
        notes.append(MSG_NEAR_MISS.format(rate=near["rate"], written=near["rate_written"], low=near["interval"][0],
                                          high=near["interval"][1], source=near["source"], amount=near["amount"],
                                          quote=near["quote"]))
    else:
        carried = next((ws.computations[i] for i in out.inputs if i in ws.computations
                        and ws.computations[i].explicit_amount), None)
        if carried is not None:
            near = dict(carried.explicit_amount)
            near["from"] = near.get("from") or carried.cid
            notes.append(MSG_NEAR_MISS_CARRIED.format(origin=near["from"], rate=near["rate"], amount=near["amount"],
                                                      source=near["source"], quote=near["quote"]))
    if differs is not None:
        notes.append(MSG_STATED_DIFFERS.format(source=differs["source"], amount=differs["amount"],
                                               quote=differs["quote"], rate=differs["rate"],
                                               low=differs["interval"][0], high=differs["interval"][1]))
    c, conditional = register_computation(ws, node, out, leaves, label=label, kind=kind, justification=justification,
                                          justified=justified, notes=notes, reproduces=reproduces, near=near,
                                          differs=differs)
    inputs = c.inputs
    return json.dumps({"id": c.cid, "label": c.label, "expression": c.expression, "formula": c.formula,
                       "value": str(c.value), "display": c.display(), "unit": c.unit_label, "scale": c.scale,
                       "result_kind": calc.RESULT_KINDS[kind], "reproduces": reproduces, "conditional": c.conditional,
                       "inputs": [x["id"] for x in inputs], "assumptions": c.assumptions, "n": out.n,
                       "documents": c.documents, "note": " ".join(x for x in (c.note, conditional) if x),
                       "explicit_amount_available": near, "stated_amount_differs": differs,
                       "how_to_show": f"הצג את התוצאה מעוגלת (display) וצטט [{c.cid}]; הנחות המשתמש בנפרד עם [A#]"},
                      ensure_ascii=False)


# --- registry ----------------------------------------------------------------------------------------------

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}


_NULLABLE_IDS = {"type": ["array", "null"], "items": {"type": "string"}}

TOOLS = [
    _fn("search",
        "חיפוש קטעים במסמכי המשרד (טקסט, טבלאות, שורות טבלה וטקסט מתוך תמונות). מחזיר מקורות S#. קובץ אחד יכול להכיל "
        "כמה שומות או נספחים — יחידות (U#, מ-find_documents או outline): כשהשאלה על יחידה אחת, חפש בתוכה עם unit, ולא "
        "בכל הקובץ. תוצאה בקובץ של כמה יחידות מציינת את היחידה שהיא בה (unit_id).",
        {"query": {"type": "string", "description": "שאילתה בעברית, במילים שעשויות להופיע במסמך"},
         "document_ids": {**_NULLABLE_IDS, "description": "הגבלה למסמכים מסוימים, או null לכל המאגר"},
         "limit": {"type": ["integer", "null"], "description": "מספר תוצאות (3-12), null לברירת מחדל"},
         "unit": {"type": ["string", "null"],
                  "description": "U# של יחידה: חיפוש רק בתוכה (ללא document_ids), או null"},
         "pool": {"type": ["string", "null"],
                  "description": "המאגר שמחפשים בו: professional — תקנים ומסמכים מקצועיים; appraisal — שומות; או null "
                                 "למאגרים שמתאימים לסוג השאלה. השרת קובע אילו מאגרים מותרים לשאלה (שומה בשאלה מקצועית "
                                 "היא דוגמה לבחירה של נכס מסוים, ותקן בשאלה על נכס הוא הסבר שיטה) ומגביל בקשה שחורגת "
                                 "מהם"}},
        ["query", "document_ids", "limit", "unit", "pool"]),
    _fn("read",
        "פתיחת מקום במסמך בלי צורך בחיפוש קודם. בוחרים מיקום אחד בדיוק ב-target (השאר null): source — S# מהתור הזה "
        "או P# מתור קודם (הפסקאות סביבו; קטע מטבלה — הטבלה כולה); pages — עמודים של מסמך PDF; section — סעיף §# "
        "מ-outline (כולל תתי-הסעיפים); table — טבלה T# (כותרות, שורות והערות); cursor — K# להמשך קריאה (more). "
        "כל תוצאה מציינת status: complete, clipped (יש המשך ב-more), has_unread_regions (אזורים שלא נקראו, R#) או "
        "uncertain_reading.",
        {"target": {"type": "object", "additionalProperties": False,
                    "required": list(LOCATORS),
                    "properties": {
                        "source": {"type": ["string", "null"], "description": "S# או P#"},
                        "pages": {"type": ["object", "null"], "additionalProperties": False,
                                  "required": ["document", "from_page", "to_page"],
                                  "properties": {"document": {"type": "string",
                                                              "description": "D# או document_id; U# של יחידה — "
                                                                             "העמודים בתוכה בלבד"},
                                                 "from_page": {"type": "integer"}, "to_page": {"type": "integer"}}},
                        "section": {"type": ["string", "null"], "description": "§# מ-outline"},
                        "table": {"type": ["string", "null"], "description": "T# מ-outline או מתוצאת חיפוש"},
                        "cursor": {"type": ["string", "null"], "description": "K# מ-more של קריאה קודמת"}}}},
        ["target"]),
    _fn("find_documents",
        "התחום של שאלה על קבוצת מסמכים (סקירה, השוואה, רשימה או חישוב על כמה מסמכים; שאלה \"בעיר/באזור X\"): כל "
        "המסמכים שמכילים את כל המונחים שמגדירים את הקבוצה, בכותרת או בתוכן. מחזיר את כולם בעמודים, עם הסך הכול. בקובץ "
        "של כמה שומות ההתאמה היא ליחידה אחת: המונחים כולם חייבים להיות באותה שומה, והיחידות שמתאימות מוחזרות (U#) עם "
        "כותרת, עמודים ומזהים; נכס שמופיע רק כנכס השוואה מוגדר ככזה ואינו שומה.",
        {"query": {"type": "string", "description": "רק המונחים שמגדירים את הקבוצה (מקום, סוג מסמך) — לא המדד"},
         "page": {"type": ["integer", "null"], "description": "מספר עמוד, null לראשון"}},
        ["query", "page"]),
    _fn("find_standards",
        "רשימת התקנים והמסמכים המקצועיים (לא שומות) שהמשתמש מורשה לראות, בעמודים: לכל אחד מה שהוא עצמו מציין — מספר, "
        "שם, מהדורה, תאריך פרסום ותאריך תחילה, גוף מפרסם ותחולה (\"לא ידוע\" כשאינו מציין) — המסמך שהוא מבהיר או מתקן, "
        "המהדורות של אותו מספר ואם סדרן ידוע, וקבצים עם אותו נוסח. מחזיר מקור S# של הרשימה. מהדורה לא נקראת האחרונה "
        "אלא אם הרשימה אומרת שסדר המהדורות ידוע. מסמך מקצועי נקרא אחר כך עם search (pool=professional) ו-read.",
        {"query": {"type": ["string", "null"], "description": "מילים מהמספר, השם או הכותרת, או null לכל התקנים"},
         "page": {"type": ["integer", "null"], "description": "מספר עמוד, null לראשון"}}, ["query", "page"]),
    _fn("list_documents", "רשימת המסמכים הזמינים למשתמש, עם מצב הקריאה שלהם, בעמודים. אפשר לסנן לפי מילים בכותרת.",
        {"query": {"type": ["string", "null"], "description": "מילים בכותרת, או null לכל המסמכים"},
         "page": {"type": ["integer", "null"], "description": "מספר עמוד, null לראשון"}}, ["query", "page"]),
    _fn("outline",
        "מבנה מסמך: סעיפים (§#) וטבלאות (T#) שאפשר לפתוח ב-read, עם עמודים, גודל ואזורים שלא נקראו. קובץ של כמה "
        "שומות מציג קודם את יחידותיו (U#, עם כותרת, עמודים ומזהים); את הסעיפים של יחידה אחת מקבלים עם unit.",
        {"document": {"type": "string", "description": "D# או document_id (או U# של יחידה)"},
         "unit": {"type": ["string", "null"], "description": "U# של יחידה: הסעיפים והטבלאות שלה בלבד, או null"}},
        ["document", "unit"]),
    _fn("find_measurements",
        "נתונים כמותיים שחולצו מהמסמכים עם משמעותם (סוג מדד, יחידה, תקופה, בסיס שטח, מע\"מ, תפקיד, נושא), מקובצים "
        "לפי מה שניתן להשוות, עם כיסוי המסמכים. מחזיר מזהי M# לחישוב, ולצדם ערכים שאומתו בתורות קודמים (Q#) "
        "שאפשר לרשום ב-take_value בלי לקרוא שוב.",
        {"query": {"type": "string", "description": "תיאור הנתון המבוקש"},
         "metric_kinds": {**_NULLABLE_IDS, "description": "סוגי מדד מתוך: " + ", ".join(KIND_LABELS)},
         "document_ids": _NULLABLE_IDS,
         "value_roles": {**_NULLABLE_IDS, "description": "תפקידים מתוך: " + ", ".join(ROLE_LABELS)},
         "page": {"type": ["integer", "null"], "description": "מספר עמוד, null לראשון"}},
        ["query", "metric_kinds", "document_ids", "value_roles", "page"]),
    _fn("take_value",
        "רישום ערך ממקור S# שקראת בתור הזה, אחרי שהשרת מאמת שהמספר הוא זה שבמיקום שבחרת: תא בטבלה (שורה ועמודה; "
        "השאר null) או ציטוט מדויק מהמקור שהמספר בתוכו (quote ו-number; השאר null), או שניהם יחד — אז התא קובע והציטוט נבדק "
        "מול השורה שלו — עם משמעות הערך. מחזיר V#. מספר "
        "שנמצא בשורה או בעמודה אחרת נדחה עם הסיבה; מה שהמקור מעיד על הערך נרשם כשל המקור, והשאר כקביעה שלך. שורת "
        "טבלה שצוטטה כפי שהיא כתובה (עם \" | \") נרשמת כתא שלה; תווית שנבדלת רק ברווחים או בסימני פיסוק מתאימה; "
        "יחידה לא ברורה במקור נרשמת בלי אפשרות לחשב איתה; תאריך אינו נרשם. ערך "
        "מאומת שמור (Q# מ-find_measurements) נרשם עם locator ריק (כל השדות null).",
        {"source": {"type": "string",
                    "description": ("S# מהתור הזה (טבלה שנקראה, גם טבלה בקריאה חזותית של inspect, שורת טבלה "
                                    "מחיפוש, או קטע), או Q# מ-find_measurements")},
         "locator": {"type": "object", "additionalProperties": False,
                     "required": ["table", "row", "row_number", "column", "column_number", "quote", "number"],
                     "properties": {
                         "table": {"type": ["string", "null"], "description": "T# או S# של הטבלה (לא חובה)"},
                         "row": {"type": ["string", "null"], "description": "תווית השורה כפי שכתובה בטבלה"},
                         "row_number": {"type": ["integer", "null"], "description": "מספר השורה (מ-1)"},
                         "column": {"type": ["string", "null"], "description": "כותרת העמודה כפי שכתובה"},
                         "column_number": {"type": ["integer", "null"], "description": "מספר העמודה (מ-1)"},
                         "quote": {"type": ["string", "null"], "description": "ציטוט מדויק מהמקור שהמספר בתוכו"},
                         "number": {"type": ["string", "null"], "description": "המספר כפי שנכתב"}}},
         "meaning": {"type": "object", "additionalProperties": False,
                     "required": ["kind", "unit", "period", "vat", "area_basis", "subject", "role", "stated_by",
                                  "stance", "scenario"],
                     "properties": {
                         "kind": {"type": "string", "enum": list(VALUE_KINDS)},
                         "unit": {"type": "string", "enum": list(UNIT_LABELS)},
                         "period": {"type": "string", "enum": list(PERIOD_LABELS)},
                         "vat": {"type": "string", "enum": list(VAT_LABELS)},
                         "area_basis": {"type": "string", "description": "בסיס השטח כפי שנכתב, או ריק"},
                         "subject": {"type": "string", "description": "הנכס, השלב, התקופה או מערך הנתונים — בלי שם המדד (\"פרויקט X\", לא \"רווח פרויקט X\"), ובאותו נוסח לכל ערכי אותו נכס"},
                         "role": {"type": "string", "enum": list(calc.ROLE_LABELS)},
                         "stated_by": {"type": "string",
                                       "description": "מי אמר את הערך (צד, שמאי, הכרעה) — רק כשהמילים המצוטטות, "
                                                      "כותרת השורה או העמודה, או הסעיף אומרים זאת; אחרת ריק"},
                         "stance": {"type": "string", "enum": list(STANCES),
                                    "description": "adopted — נקבע ואומץ; claim/proposal/estimate — טענה, הצעה או "
                                                   "אומדן — רק כשהמילים המצוטטות, כותרת השורה או העמודה, או הסעיף "
                                                   "אומרים זאת; אחרת unknown"},
                         "scenario": {"type": "string",
                                      "description": "התרחיש, השלב או המועד שהערך שייך להם כפי שכתוב, או ריק"}}},
         "label": {"type": "string", "description": "שם קצר בעברית לערך, כפי שיוצג בנוסחה"}},
        ["source", "locator", "meaning", "label"]),
    _fn("assume",
        "רישום מספר שהמשתמש נתן לתרחיש (למשל \"העלויות יעלו ב-5%\") כהנחת משתמש A#, עם ציטוט מדויק מהודעת המשתמש "
        "(הנוכחית או קודמת). לעולם לא מספר מתשובה קודמת או ממסמך; אם המשתמש לא נתן את המספר — שאל אותו.",
        {"value": {"type": "string", "description": "המספר כפי שהמשתמש כתב (למשל 5%)"},
         "quote": {"type": "string", "description": "ציטוט מדויק מהודעת המשתמש שהמספר בתוכו"},
         "label": {"type": "string", "description": "שם קצר בעברית להנחה"}},
        ["value", "quote", "label"]),
    _fn("calculate",
        "חישוב מדויק בקוד על מזהים: V# (take_value), A# (assume), M# (find_measurements), C# (חישוב קודם). מותר: + - * / "
        "(או × ÷), סוגריים, % אחרי ערך (A1% = A1/100), sum/mean/median/min/max/count(מזהים), והקבועים 1, 100, 12 בלבד. "
        "מסרב לחבר יחידות או תקופות שונות, שכירות עם שווי, או סה\"כ עם הרכיבים שלו; ערבוב מע\"מ, בסיסי שטח או נושאים "
        "מותר רק עם justification, והתוצאה מותנית. מחזיר C# בדיוק מלא.",
        {"expression": {"type": "string", "description": "למשל V1 - V2 * (1 + A1%) או mean(M1, M2, M3)"},
         "label": {"type": "string", "description": "שם קצר בעברית לתוצאה"},
         "justification": {"type": ["string", "null"],
                           "description": "רק כשהחישוב מערבב מע\"מ, בסיס שטח או נושא: למה זה תקף; אחרת null"}},
        ["expression", "label", "justification"]),
    _fn("inspect",
        "קריאה חזותית של אזור או עמוד במסמך PDF שהטקסט שלו חסר או לא ודאי: region — R# מסימון [אזור שלא נקרא R#] "
        "או [קריאה לא ודאית R#] בתוצאת read; או document (D#) ו-page — עמוד שלם (השאר null). מחזיר מקור S# עם "
        "תמלול (status uncertain_reading: תמלול של מודל, לא טקסט המסמך) שאפשר לצטט. אזור שנקרא בעיבוד המסמך מוחזר "
        "כפי שנקרא. מספר הקריאות החזותיות בתור מוגבל: השתמש רק כשהתשובה תלויה בתוכן שלא נקרא.",
        {"target": {"type": "object", "additionalProperties": False, "required": ["region", "document", "page"],
                    "properties": {
                        "region": {"type": ["string", "null"], "description": "R# מתוצאת read בתור הזה"},
                        "document": {"type": ["string", "null"], "description": "D# או document_id (עם page)"},
                        "page": {"type": ["integer", "null"], "description": "מספר העמוד (מ-1), עם document"}}}},
        ["target"]),
]

HANDLERS = {
    "search": lambda ws, a: tool_search(ws, a["query"], a.get("document_ids"), a.get("limit"), a.get("unit"),
                                        a.get("pool")),
    "read": lambda ws, a: tool_read(ws, a["target"]),
    "find_documents": lambda ws, a: tool_find_documents(ws, a["query"], a.get("page")),
    "list_documents": lambda ws, a: tool_list_documents(ws, a.get("query"), a.get("page")),
    "find_standards": lambda ws, a: tool_find_standards(ws, a.get("query"), a.get("page")),
    "outline": lambda ws, a: tool_outline(ws, a.get("document") or a.get("document_id") or a.get("unit"),
                                          a.get("unit")),
    "find_measurements": lambda ws, a: tool_find_measurements(ws, a["query"], a.get("metric_kinds"),
                                                              a.get("document_ids"), a.get("value_roles"),
                                                              a.get("page")),
    "take_value": lambda ws, a: tool_take_value(ws, a["source"], a.get("locator"), a.get("meaning"), a.get("label") or ""),
    "assume": lambda ws, a: tool_assume(ws, a["value"], a["quote"], a.get("label") or ""),
    "calculate": lambda ws, a: tool_calculate(ws, a["expression"], a.get("label") or "", a.get("justification")),
    "inspect": lambda ws, a: tool_inspect(ws, a["target"]),
}


# the tools whose results carry document content: once the turn's tool-output budget is spent they are not run
READING_TOOLS = frozenset({"search", "read", "outline", "find_documents", "list_documents", "find_standards",
                           "find_measurements", "inspect"})
MSG_BUDGET = ("מגבלת היקף הקריאה לשאלה אחת הגיעה לסופה: זה לא נקרא ולא נשלח דבר מתוכו (more — הקריאה שהייתה פותחת "
              "אותו). אל תציג את התשובה כמלאה. רישום ערכים וחישוב (take_value, assume, calculate) על מה שכבר נקרא "
              "עדיין אפשריים.")
# the reading tools that check the turn's reading deadline before they start (``inspect`` keeps its own check, which also
# cuts a vision call to the time left)
DEADLINE_TOOLS = frozenset({"search", "read", "outline", "find_documents", "list_documents", "find_standards",
                            "find_measurements"})
# the tools that register or compute on what was already read: they stay open when the reading is over (the tool budget)
COMPUTE_TOOLS = ("calculate", "take_value", "assume")
MSG_TIME = ("מגבלת הזמן לקריאה בשאלה אחת הגיעה לסופה: זה לא נקרא ולא נשלח דבר מתוכו (more — הקריאה שהייתה פותחת "
            "אותו). אל תציג את התשובה כמלאה, וחלק שלא נקרא לא נבדק — לא נמצא שהוא חסר במסמך. רישום ערכים וחישוב "
            "(take_value, assume, calculate) על מה שכבר נקרא עדיין אפשריים.")


def compute_tool_choice() -> dict:
    """A ``tool_choice`` that lets the model call the compute tools and nothing else. The tools sent stay the same, so
    the cached prefix survives (KTD12)."""
    return {"type": "allowed_tools", "mode": "auto", "tools": [{"type": "function", "name": n} for n in COMPUTE_TOOLS]}


def _call_text(name: str, args: dict) -> str:
    """A call as the model would write it, with its given arguments only: ``read(section=§3)``."""
    given = args.get("target") if name in ("read", "inspect") and isinstance(args.get("target"), dict) else args
    parts = []
    for k, v in given.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, dict):
            v = "{" + ", ".join(f"{a}={b}" for a, b in v.items() if b not in (None, "")) + "}"
        elif isinstance(v, list):
            v = "[" + ", ".join(str(x) for x in v) + "]"
        parts.append(f"{k}={v}")
    return f"{name}({', '.join(parts)})"


def _withheld(name: str, args: dict, status: str = TOOL_BUDGET) -> str:
    """What a reading tool returns once the budget is spent or the reading time is up: a header and how to read on, no
    body — nothing is read, registered or counted as read."""
    return (f'<not_read tool="{name}" status="{status}" more="{_attr(_call_text(name, args))}"/>\n'
            + (MSG_TIME if status == TIME_LIMIT else MSG_BUDGET))


PAGE_TOOLS = frozenset({"read", "inspect"})  # the tools whose record on the stage clock says which pages they returned


def run_tool(ws: Workspace, name: str, arguments: str) -> str:
    """Run one tool call and return what is sent to the model, counted against the turn's tool-output budget. The
    call is timed on the turn's stage clock (a tool that does not exist is not a stage's time); a read or an
    inspection records the first and last page of the sources it returned (numbers only)."""
    started = ws.clock.now()
    before = len(ws.sources)
    output = ws.spend(_run_tool(ws, name, arguments))
    _stamp_new_sources(ws, before)
    if name in HANDLERS:
        pages = ({p for s in islice(ws.sources.values(), before, None) for p in s.page_list or ()}
                 if name in PAGE_TOOLS else None)
        ws.clock.tool(name, ws.clock.now() - started, len(output), (min(pages), max(pages)) if pages else None)
    return output


READ_TARGET_TOOLS = frozenset({"read", "inspect", "outline"})  # the tools whose call opens a target of a document (R18)


def _read_target(name: str, args: dict) -> str:
    """The handle-level name of what a reading call opens (``read:section=§3``, ``read:pages=D1:3-5``, ``inspect:region=R2``):
    the locator the model gave, never document text. Clipped; the states of the turn are kept by it (``StageClock.note_read``)."""
    given = args.get("target") if name in ("read", "inspect") and isinstance(args.get("target"), dict) else args
    parts = []
    for k, v in sorted(given.items()):
        if v in (None, "", [], {}):
            continue
        if isinstance(v, dict):
            v = ":".join(str(x) for x in (v.get("document"), f"{v.get('from_page')}-{v.get('to_page')}"
                                          if "from_page" in v else None) if x not in (None, ""))
        parts.append(f"{k}={v}")
    return _clip(f"{name}:" + ",".join(parts), 120)


def _documents_of(ws: Workspace, args) -> set[str]:
    """The documents a call names through the handles it gives (``D1``, ``§3``, ``T2``, ``K1``, ``R4`` of ``ws.handles``), anywhere in its
    arguments (round 22 U7): what ``Workspace.failed_reads`` finds a read by. A handle names its document; so does a source id (``S3``, a
    ``read(source=...)`` locator; a listing has no document, round 22 review #5). A locator that names none gives no document."""
    found: set[str] = set()
    if isinstance(args, dict):
        for v in args.values():
            found |= _documents_of(ws, v)
    elif isinstance(args, (list, tuple)):
        for v in args:
            found |= _documents_of(ws, v)
    elif isinstance(args, str) and (h := ws.handles.get(args)) is not None and h.get("document_id"):
        found.add(str(h["document_id"]))
    elif isinstance(args, str) and (src := ws.sources.get(args)) is not None and src.document_id is not None:
        found.add(str(src.document_id))
    return found


def _note_read(ws: Workspace, name: str, args: dict, **state) -> None:
    """Record the state of a reading call's target on the stage clock (round 22 U0, R18): attempted, completed, failed with,
    skipped for the tool budget. Diagnostics only: nothing a tool returns or decides reads it, and it never raises."""
    if name not in READ_TARGET_TOOLS:
        return
    try:
        ws.clock.note_read(_read_target(name, args), name, documents=_documents_of(ws, args), **state)
    except Exception:  # noqa: BLE001 - a measurement must never fail a tool call
        logger.debug("read state not recorded", exc_info=True)


def _note_refusal(ws: Workspace, name: str, args: dict, key: str, message: str) -> None:
    """Record a take_value call the server refused (round 14 U1, KTD11, R15): the tool, the source id, a clipped locator, the
    reason key and message, and the time. It is stored in the message diagnostics (owner and office admin only) as
    ``resolution["registrations"]``; the model never sees it, and a successful call leaves nothing here."""
    if name != "take_value" or len(ws.registration_log) >= REGISTRATION_LOG_MAX:
        return
    where = {k: v for k, v in (args.get("locator") or {}).items() if v not in (None, "")} if isinstance(
        args.get("locator"), dict) else {}
    ws.registration_log.append({
        "tool": name, "source": _clip(str(args.get("source") or ""), 40),
        "locator": _clip(json.dumps(where, ensure_ascii=False, sort_keys=True), 160), "key": key,
        "message": _clip(" ".join(message.split()), 240), "at": datetime.now(UTC).isoformat(timespec="seconds")})


def _run_tool(ws: Workspace, name: str, arguments: str) -> str:
    handler = HANDLERS.get(name)
    if handler is None:
        return f"כלי לא קיים: {name}"
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        _note_refusal(ws, name, {}, "bad_json", "")
        return "ארגומנטים לא תקינים (JSON)"
    if name in READING_TOOLS and ws.budget_spent:
        _note_read(ws, name, args, attempted=False, budget_skipped=True)
        return _withheld(name, args)
    if name in DEADLINE_TOOLS and ws.read_until is not None and time.monotonic() >= ws.read_until:
        # the loop checks the clock only between steps: a tool that starts after the reading deadline does no work
        if TIME_LIMIT not in ws.limits_hit:
            ws.limits_hit.append(TIME_LIMIT)
        _note_read(ws, name, args, attempted=False, failed_with=TIME_LIMIT)
        return _withheld(name, args, TIME_LIMIT)
    try:
        out = handler(ws, args)
    except ToolError as e:
        _note_refusal(ws, name, args, e.key, str(e))
        _note_read(ws, name, args, attempted=True, failed_with=e.key)
        return f"שגיאה: {e}"
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("tool %s rejected its arguments: %s", name, type(e).__name__)
        _note_refusal(ws, name, args, "bad_arguments", type(e).__name__)
        _note_read(ws, name, args, attempted=True, failed_with="bad_arguments")
        return "שגיאה: ארגומנטים לא תקינים לכלי"
    except Exception as e:  # noqa: BLE001 - a failing tool is reported to the model; the turn goes on
        logger.exception("tool %s failed", name)
        _note_refusal(ws, name, args, "tool_failed", type(e).__name__)
        _note_read(ws, name, args, attempted=True, failed_with="tool_failed")
        return "שגיאה: הכלי נכשל. אפשר לנסות שוב או לנסות דרך אחרת"
    if isinstance(out, str) and out.startswith("<not_read"):  # the tool withheld itself (``inspect``'s own time check)
        _note_read(ws, name, args, attempted=False, failed_with=TIME_LIMIT if f'status="{TIME_LIMIT}"' in out[:160] else None,
                   budget_skipped=f'status="{TOOL_BUDGET}"' in out[:160])
    else:
        _note_read(ws, name, args, attempted=True, completed=True)
    return out
