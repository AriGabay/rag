"""Round 22 U11 with the real model (KTD10, R19; AE5): history keeps the meaning the server added to an earlier answer as a labelled
system note ("הערת מערכת על ..."), and the model must not take that note for a source fact. OPT-IN: ``-m real_model``; skipped without a
provider key (the repository's ``.env`` through the round-10 ``real_key`` fixture; the key is never printed). Host only, against
``rag_test``. Never run by the development workflow; the orchestrator runs it on the configured model (``gpt-6-luna``, no override).

Scenario (synthetic office, the round-20 lot-groups PDF of ``tests/fixtures/round20``, every name and figure invented): a two-turn
conversation in a new conversation, exactly three times.

1. The first answer states group A's area and the lease condition that the footnote of the document attaches to group A's contract; the
   server writes that condition next to the value (a ``qualifier`` or ``scope_note`` span), so turn two's history carries it as a system note.
2. The second turn asks for group B's area. The footnote belongs to group A's contract only.

Checks (by behaviour, never by wording; a pattern part is matched, so a Hebrew prefix does not change the outcome):

- ``shape_produced``: the first answer states group A's area with the condition (otherwise the repetition is counted as ``shape not produced``
  and is not a pass);
- ``note_not_copied_as_a_fact``: the second answer's own text (outside the spans the server wrote) states no condition of the land
  authority, no server span of it carries the first answer's condition or its claim ids, and the label ``הערת מערכת`` is not written in it as
  if it were a source;
- ``property_kept``: the second answer states group B's area and cites the same document, so the conversation's property is unchanged.

Recorded per repetition under ``real_documents/eval/round22/real-model/`` (gitignored)."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.integration.test_round10_real_model import new_conversation
from tests.integration.test_round10_real_model import real_key as real_key  # noqa: F401 - the key fixture
from tests.integration.test_round17_real_model import answered, cites_document, has
from tests.integration.test_round19_real_model import stated
from tests.integration.test_round21_real_model import (
    A4_FIRST,
    A4_SECOND,
    AREA_A,
    AREA_B,
    CONDITION,
    LOTS,
    SERVER_CONDITION_NOTE,
    Turn21,
    ingest_pdf,
)
from tests.integration.test_round21_real_model import (
    office as office,  # noqa: F401 - the fixture, taken by name
)

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round22" / "real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"history-note-{STAMP}.json"
ATTEMPTS = 3
CHECKS = ("answered_first", "shape_produced", "answered_second", "note_not_copied_as_a_fact", "property_kept")
SYSTEM_NOTE_LABEL = re.compile(r"הערת\s+מערכת")
RESULTS: list[dict] = []


def _flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "attempts": RESULTS}, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")


def _ask(c, cid: str, question: str, rec: dict) -> Turn21:
    started = time.perf_counter()
    r = c.post(f"/api/chat/conversations/{cid}/messages", json={"content": question})
    assert r.status_code == 200, r.text
    m = c.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    t = Turn21(c, m, question, time.perf_counter() - started)
    rec["turns"].append(t.record())
    _flush()
    assert m["status"] == "done", (m.get("status"), m.get("error"))
    return t


def _check(rec: dict, name: str, fn) -> None:
    try:
        fn()
    except AssertionError as exc:
        rec["checks"][name] = {"outcome": "fail", "failure": str(exc)[:2000]}
    else:
        rec["checks"][name] = {"outcome": "pass"}
    _flush()


def _condition_in(t: Turn21) -> bool:
    return has(t.markdown, CONDITION)


def _note_not_copied(first: Turn21, second: Turn21) -> None:
    bad = []
    carriers = [s for s in second.spans if CONDITION.search(s["text"]) or SERVER_CONDITION_NOTE.search(s["text"])]
    if carriers:
        bad.append(("a server span of the second answer carries the first answer's condition", [(s["kind"], s["text"][:160]) for s in carriers]))
    shared = {c for s in first.spans for c in s["claim_ids"]} & {c for s in second.spans for c in s["claim_ids"]}
    if shared:
        bad.append(("a claim id of the first answer's additions is in the second answer's registry", sorted(shared)))
    beside = [ln[:200] for ln in second.stated_lines if has(ln, CONDITION)]
    if beside:
        bad.append(("the second answer states the condition of the first as a fact", beside[:2]))
    labelled = [ln[:200] for ln in second.stated_lines if SYSTEM_NOTE_LABEL.search(ln)]
    if labelled:
        bad.append(("the second answer writes the system note's label as if it were a source", labelled[:2]))
    assert not bad, bad


def _attempt(office, doc: str, n: int) -> dict:
    """One repetition in a new conversation; recorded (and flushed) before anything is asserted."""
    rec = {"sequence": "history_note_not_copied", "attempt": n, "turns": [], "checks": {}, "info": {}, "outcome": None}
    RESULTS.append(rec)
    try:
        cid = new_conversation(office.client)
        first = _ask(office.client, cid, A4_FIRST, rec)
        produced = _condition_in(first) and stated(first, AREA_A)
        rec["info"] = {"shape_produced": produced,
                       "first_spans": [(s["kind"], s["text"][:160], s["claim_ids"], s["datum"]) for s in first.spans]}
        second = _ask(office.client, cid, A4_SECOND, rec)
        rec["info"] |= {"second_spans": [(s["kind"], s["text"][:160], s["claim_ids"], s["datum"]) for s in second.spans]}
        _check(rec, "answered_first", lambda: answered(first))
        _check(rec, "shape_produced", lambda: _shape(first, produced))
        _check(rec, "answered_second", lambda: answered(second))
        _check(rec, "note_not_copied_as_a_fact", lambda: _note_not_copied(first, second))
        _check(rec, "property_kept", lambda: _property_kept(second, doc))
    except AssertionError as exc:
        rec["aborted"] = str(exc)[:2000]
    for name in CHECKS:
        rec["checks"].setdefault(name, {"outcome": "fail", "failure": "never ran: " + rec.get("aborted", "?")})
    rec["outcome"] = "pass" if all(v["outcome"] == "pass" for v in rec["checks"].values()) else "fail"
    _flush()
    return rec


def test_history_note_is_not_copied_as_a_fact(office, monkeypatch):  # noqa: F811
    doc = ingest_pdf(office, monkeypatch, LOTS)
    failures: list[str] = []
    for n in range(1, ATTEMPTS + 1):
        rec = _attempt(office, doc, n)
        failures += [f"attempt {n} / {name}: {v['failure']}" for name, v in rec["checks"].items() if v["outcome"] == "fail"]
    assert not failures, failures


def _shape(first: Turn21, produced: bool) -> None:
    assert produced, ("SHAPE NOT PRODUCED: the first answer does not state group A's area with the lease condition", first.markdown[:500])


def _property_kept(second: Turn21, doc: str) -> None:
    assert stated(second, AREA_B), ("the second answer does not state group B's area", second.markdown[:500])
    cites_document(second, doc)


# --- round 22 U12 (KTD14, R20): two lists bind on the REAL resolve call ------------------------------------------------------------------
#
# The real model reads the new message against the lists the previous answer printed (``resolve._input`` shows each with its id, title and
# items) and claims ``prior_answer_item`` with the item, the list id or the item set; the SERVER then selects the list and validates the claim
# (``slots.bind``). The state is built by hand - what ``compose.compose`` stores for an answer that printed a plots list and a payments list -
# so the repetition measures the resolve call alone (no documents, no agent). Every name is invented. Four sequences, ``ATTEMPTS`` times each:
#
# - the payment: «ומה לגבי התשלום השני?» binds to item 2 of the payments list (its label), never plot 2;
# - the bare ordinal: «ומה לגבי השני?» over the two lists of one answer is never bound to either - the server asks one question that
#   names both lists;
# - the twelfth: «ומה לגבי המגרש ה-12?» binds to the twelfth of a list of fifteen;
# - both: «ומה לגבי שניהם?» binds to the two items of a two-item list.
#
# Checks are by the server's decision (``Request.intent``, ``clarify``); a repetition in which the model did not claim an item reference at all
# is recorded as ``shape not produced`` and is not a pass. Recorded under ``real_documents/eval/round22/real-model/`` (gitignored).

LISTS_RESULTS_FILE = RESULTS_DIR / f"two-lists-{STAMP}.json"
LISTS_RECORDS: list[dict] = []
PLOT_NAMES = ["הדקל", "התמר", "האלון", "הארז", "הברוש", "הגפן", "הדולב", "הזית", "החרוב", "הלוז", "המילה", "הנרקיס", "הסביון", "העפרוני", "הפרג"]


def _a_list(mid: str, n: int, title: str, labels: list[str], age: int = 0) -> dict:
    from app.chat import slots

    (entry,) = slots.stamp([{"title": title, "kind": slots._kind(labels), "items": [{"label": x} for x in labels], "total": len(labels),
                             "document_ids": ["doc-1"]}], mid)
    return entry | {"id": f"{mid}#{n}", "age": age}


def _answer_text(*lists: dict) -> str:
    out = []
    for entry in lists:
        out.append(f"**{entry['title']}:**\n" + "\n".join(f"{i}. {x['label']} [S1]" for i, x in enumerate(entry["items"], 1)))
    return "\n\n".join(out)


def _resolve_real(message: str, lists: list[dict]):
    """The follow-up resolved by the real model against the lists the previous answer printed; the validated request."""
    from app.chat import resolve
    from app.chat.engine import HistoryMessage
    from app.providers import llm

    history = [HistoryMessage("user", "אילו מגרשים ותשלומים יש בשומה?"), HistoryMessage("assistant", _answer_text(*lists))]
    focus = {"metric_as_written": "שטח", "metric_kind": "area", "unit": "unknown", "period": "unknown", "area_basis": "", "vat": "unknown",
             "subject": "הנכס בהתמר 9", "value_role": "appraiser_determination", "document_ids": ["doc-1"]}
    usage: list[dict] = []
    return resolve.resolve(llm.get_selected_provider(), focus, history, message, [{"document_id": "doc-1", "title": "שומה סינתטית"}],
                           lambda ids: {i for i in ids if i == "doc-1"}, ["שומה סינתטית"], usage, lists=lists)


def _record(name: str, attempt: int, request, outcome: str, info: dict) -> dict:
    rec = {"sequence": name, "attempt": attempt, "outcome": outcome, "info": info,
           "intent": request.intent.as_dict() if request is not None else None,
           "clarify": request.clarify if request is not None else None}
    LISTS_RECORDS.append(rec)
    LISTS_RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LISTS_RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "attempts": LISTS_RECORDS}, ensure_ascii=False, indent=2,
                                             default=str), encoding="utf-8")
    return rec


def _claimed_an_item(request) -> bool:
    """Whether the model's parse claimed a reference to an item of a list (the shape this test is about)."""
    claim = ((request.resolution.get("parse") or {}).get("intent") or {}) if request is not None else {}
    return claim.get("reference") == "prior_answer_item"


def test_two_lists_bind_on_the_real_resolve_call(real_key):  # noqa: F811
    plots = _a_list("m-1", 1, "המגרשים בשומה", [f"מגרש {n}" for n in PLOT_NAMES[:3]])
    payments = _a_list("m-1", 2, "לוח התשלומים", ["תשלום ראשון", "תשלום שני", "תשלום שלישי"])
    fifteen = _a_list("m-1", 1, "מגרשי השומה", [f"מגרש {n}" for n in PLOT_NAMES])
    pair = _a_list("m-1", 1, "הבניינים בשומה", ["בניין הדקל", "בניין התמר"])
    failures: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        cases = [
            ("payment", "ומה לגבי התשלום השני?", [plots, payments],
             lambda r: r.intent.bound == "item" and r.intent.item_label == "תשלום שני" and r.intent.list_id == payments["id"]),
            ("bare_ordinal", "ומה לגבי השני?", [plots, payments],
             lambda r: r.intent.bound != "item" and r.server_clarify and "לוח התשלומים" in r.clarify and "המגרשים בשומה" in r.clarify),
            ("twelfth", "ומה לגבי המגרש ה-12?", [fifteen],
             lambda r: r.intent.bound == "item" and r.intent.item == 12 and r.intent.item_label == "מגרש הנרקיס"),
            ("both", "ומה לגבי שניהם?", [pair],
             lambda r: r.intent.bound == "item_set" and r.intent.item_set == (1, 2))]
        for name, message, lists, passes in cases:
            request = _resolve_real(message, lists)
            if request is None:
                _record(name, attempt, None, "fail", {"failure": "the resolve call failed"})
                failures.append(f"attempt {attempt} / {name}: the resolve call failed")
            elif not _claimed_an_item(request):
                _record(name, attempt, request, "shape not produced", {"note": "the model did not claim prior_answer_item"})
                failures.append(f"attempt {attempt} / {name}: SHAPE NOT PRODUCED (the model did not claim an item reference)")
            elif not passes(request):
                _record(name, attempt, request, "fail", {"decision": request.resolution.get("decisions", {}).get("item")})
                failures.append(f"attempt {attempt} / {name}: bound {request.intent.as_dict()} clarify={request.clarify!r}")
            else:
                _record(name, attempt, request, "pass", {})
    assert not failures, failures


# --- round 22 U5 (KTD5, R8; finding g): a row hit of a table whose scale note sits only under the table -----------------------------------------
#
# The chunker writes a table's notes into the LAST table chunk only, so a search hit on one row reaches the agent without «(*) הסכומים באלפי ₪».
# U5 adds a short flag to such a hit («[לטבלה יש הערות: (*); לקריאתן: read(table=T#)]», marks only) and completes the note at the consumer
# (take_value, the condition spans, the verification) from the stored structure. This sequence asks for one value of a long synthetic table
# (a seeded document: the production chunker and persistence over an invented table, no PDF) and checks, by behaviour, that the answer states
# the value IN THOUSANDS: the stored cell says «4,250» under «(₪) (*)», the note says thousands. Every name and figure is invented.
#
# - ``answered`` / ``cited``: the answer is an answer and cites the document;
# - ``shape_produced``: the answer states the value at all (otherwise the repetition is ``shape not produced`` and is not a pass);
# - ``scale_kept``: a line of the answer states the value in thousands (4,250 thousand, or 4,250,000);
# - ``no_unscaled_value``: no line states the bare «4,250» as shekels (the harm the note prevents).
#
# Recorded per repetition under ``real_documents/eval/round22/real-model/`` (gitignored). NOT RUN by the development workflow.

THOUSANDS_RESULTS_FILE = RESULTS_DIR / f"thousands-note-{STAMP}.json"
THOUSANDS_RECORDS: list[dict] = []
THOUSANDS_NOTE = "(*) הסכומים באלפי ₪"
THOUSANDS_HEADERS = ["בניין", "הכנסה שנתית (₪) (*)"]
THOUSANDS_ROWS = [("בניין הגפן", "4,250"), ("בניין התמר", "3,120"), ("בניין האלון", "5,980"), ("בניין הארז", "2,740"), ("בניין הברוש", "6,310"),
                  ("בניין הדולב", "1,980"), ("בניין הזית", "3,860"), ("בניין החרוב", "4,730"), ("בניין הלוז", "2,150"), ("בניין המילה", "5,420"),
                  ("בניין הנרקיס", "3,390"), ("בניין הסביון", "4,060"), ("בניין העפרוני", "2,870"), ("בניין הפרג", "5,110"), ("בניין הרימון", "3,640"),
                  ("בניין השקד", "4,490"), ("בניין התאנה", "2,330"), ("בניין הדקל", "6,020")]
THOUSANDS_QUESTION = "מהי ההכנסה השנתית של בניין הגפן לפי השומה?"
THOUSANDS_TARGET = "4,250"
IN_THOUSANDS = re.compile(r"4[,.]?250[,.]?000(?![\d])|4[,.]25\s*מיליון|(?<![\d,.])4[,.]?250\s*(?:אלפי|אלף|K\b)|(?:אלפי|אלף)\s*₪?\s*[:\-–]?\s*4[,.]?250(?![\d,.])")
BARE_TARGET = re.compile(r"(?<![\d,.])4[,.]?250(?![\d]|[.,]\d)")


def _seed_thousands_table(office) -> str:
    """A document of one long table (invented) whose thousands note «(*) הסכומים באלפי ₪» stands only under it, written by the PRODUCTION
    chunker and persistence as a reading would: the notes in the last table chunk, none in a row chunk. Returns the document id."""
    from app.db import tenant_tx
    from app.extraction.base import Block, ExtractionResult, PageResult, TableResult, TableRow
    from app.extraction.chunking import chunk_blocks
    from app.extraction.docx import render_table
    from app.platform import pipeline
    from app.platform.pipeline import VersionInfo
    from tests.factories import make_document

    title = "שומת הדגמה לתיק בניינים ברחוב הגפן — הכנסות לפי בניין"
    section = "3. הכנסות לפי בניין"
    lead = "הטבלה שלהלן מציגה את ההכנסה השנתית של כל בניין בתיק."
    table = TableResult(index=0, headers=list(THOUSANDS_HEADERS), units=[None, None], rows=[TableRow(page=1, cells=[n, v]) for n, v in THOUSANDS_ROWS],
                        page_start=1, page_end=1, section=section, caption=lead, notes=[THOUSANDS_NOTE], block_index=3)
    blocks = [Block(0, "heading", title, section=title, section_path=[title], page=1),
              Block(1, "paragraph", "מסמך סינתטי לדמו — כל הנתונים בדויים", section=title, section_path=[title], page=1),
              Block(2, "heading", section, section=section, section_path=[title, section], page=1),
              Block(3, "table", render_table(table), section=section, section_path=[title, section], page=1, table_index=0)]
    chunks = chunk_blocks(blocks, [table])
    assert not any("הסכומים באלפי" in c.text for c in chunks if c.kind == "table_row"), "a row chunk holds the note: the gap is not reproduced"
    doc, ver = make_document(office, office.default_group_id, title, sha="22" * 32)
    result = ExtractionResult(1, [PageResult(1, "\n".join(b.text for b in blocks), "text_layer", 1.0, True)], [table], chunks, blocks=blocks)
    with tenant_tx(office.system) as conn:
        pipeline.persist_extraction(conn, VersionInfo(ver, doc, "k", "application/pdf", None), result)
    pipeline.embed_stage(office.system, VersionInfo(ver, doc, "k", "application/pdf", None), 1e18)
    return str(doc)


def _thousands_record(attempt: int, rec: dict) -> None:
    THOUSANDS_RECORDS.append(rec | {"attempt": attempt})
    THOUSANDS_RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    THOUSANDS_RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "attempts": THOUSANDS_RECORDS}, ensure_ascii=False, indent=2,
                                                 default=str), encoding="utf-8")


# the scale stated in the value's own line in words: «4,250 אלפי ₪» or «4,250; ... הסכומים הם באלפי ₪» says the same as «4,250,000 ₪»
_THOUSANDS_WORDS = re.compile(r"(?<![א-ת])ב?אלפי\s*(?:₪|ש[\"״]?ח|שקל)")


def _in_thousands(ln: str) -> bool:
    return has(ln, IN_THOUSANDS) or (has(ln, BARE_TARGET) and bool(_THOUSANDS_WORDS.search(ln)))


def _scale_kept(t: Turn21) -> None:
    assert any(_in_thousands(ln) for ln in t.stated_lines), ("no line states the value in thousands", t.markdown[:500])


def _no_unscaled_value(t: Turn21) -> None:
    bad = [ln[:200] for ln in t.stated_lines if has(ln, BARE_TARGET) and not _in_thousands(ln)]
    assert not bad, ("a line states the bare value as if it were shekels", bad[:2])


def test_a_row_hit_with_a_thousands_note_is_read_with_its_scale(office, monkeypatch):  # noqa: F811
    doc = _seed_thousands_table(office)
    failures: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        rec = {"sequence": "row_hit_thousands_note", "turns": [], "checks": {}, "info": {}, "outcome": None}
        try:
            cid = new_conversation(office.client)
            started = time.perf_counter()
            r = office.client.post(f"/api/chat/conversations/{cid}/messages", json={"content": THOUSANDS_QUESTION})
            assert r.status_code == 200, r.text
            m = office.client.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
            t = Turn21(office.client, m, THOUSANDS_QUESTION, time.perf_counter() - started)
            rec["turns"].append(t.record())
            assert m["status"] == "done", (m.get("status"), m.get("error"))
            produced = stated(t, BARE_TARGET) or stated(t, IN_THOUSANDS)
            rec["info"] = {"shape_produced": produced, "markdown": t.markdown[:600],
                           "spans": [(s["kind"], s["text"][:160], s["claim_ids"], s["datum"]) for s in t.spans]}
            _thousands_checks(rec, t, doc, produced)
        except AssertionError as exc:
            rec["aborted"] = str(exc)[:2000]
        for name in ("answered", "cited", "shape_produced", "scale_kept", "no_unscaled_value"):
            rec["checks"].setdefault(name, {"outcome": "fail", "failure": "never ran: " + rec.get("aborted", "?")})
        rec["outcome"] = "pass" if all(v["outcome"] == "pass" for v in rec["checks"].values()) else "fail"
        _thousands_record(attempt, rec)
        failures += [f"attempt {attempt} / {name}: {v['failure']}" for name, v in rec["checks"].items() if v["outcome"] == "fail"]
    assert not failures, failures


def _must(condition: bool, message: str, t: Turn21) -> None:
    assert condition, (message, t.markdown[:500])


def _thousands_checks(rec: dict, t: Turn21, doc: str, produced: bool) -> None:
    """The five checks of one repetition, each recorded on its own (a failure never stops the others)."""
    _check(rec, "answered", lambda: answered(t))
    _check(rec, "cited", lambda: cites_document(t, doc))
    _check(rec, "shape_produced", lambda: _must(produced, "SHAPE NOT PRODUCED: the answer does not state the value of the first building", t))
    _check(rec, "scale_kept", lambda: _scale_kept(t))
    _check(rec, "no_unscaled_value", lambda: _no_unscaled_value(t))
