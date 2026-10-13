"""Search, read and list by the settled intent's pools (round 10 U8: R22-R31; KTD11, KTD12, KTD15).

The pool is decided by the server from the turn's settled intent, never by the model: a professional question searches
the standards and professional documents and sees appraisals only as labelled examples of one property's choice; a
property question searches appraisals and may read a standard for its method, labelled as such; a mixed question gets
both; a set question appraisals. A hit inside a standard carries its section, the limits that bind it and the call that
reads the whole section; identical text is counted once; ``find_standards`` lists documents with what each states, the
editions with whether their order is known, and the copies. An empty professional pool is a stated gap, and nobody sees a
standard of a group they are not in. The tools are run directly under a workspace of a given intent. Synthetic data
only: every standard says so and every number is invented."""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.chat.contexts import MSG_UNNAMED
from app.db import tenant_tx
from app.platform.search import SearchScope, _prepare, _scoped_versions, _trigram, search_evidence
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_intent import _focus, _next, _resolution
from tests.support.professional_files import (
    APPROACH,
    EXCEPTION,
    MARK,
    professional_file,
    standard_blocks,
)
from tests.support.scripted_agent import ScriptedAgent, call, final, intent_part

pytestmark = pytest.mark.db

QUERY = "עסקאות השוואה בנכסים דומים"
PROPERTY = "הנכס ברחוב הרקפות 5. נבחרה גישת ההשוואה לפי עסקאות השוואה בנכסים דומים בסביבה."
GLOSSARY = "גישת ההשוואה היא שיטה להערכת שווי לפי עסקאות השוואה בנכסים דומים."


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.private = make_group(a, "קבוצה סגורה")
    a.member = make_user(a, "member@example.test", [a.default_group_id, a.private])
    a.outsider = make_user(a, "outsider@example.test", [a.default_group_id])
    return a


def standard(office, number="98.1", group=None, kind="standard", title="תקן מדידה", sha=None, **kw):
    return professional_file(office, group or office.default_group_id, standard_blocks(number, **kw), title=title,
                             source_kind=kind, sha=sha)


def appraisal(office, text_=PROPERTY, title="שומה הרקפות 5 (סינתטי)", opening=None):
    """An appraisal whose first block (its title block) is ``opening`` — by default the address of its property."""
    opening = f"{MARK} — שומה לנכס ברחוב הרקפות 5" if opening is None else f"{MARK} — {opening}"
    return professional_file(office, office.default_group_id,
                             [("paragraph", None, [], opening, 1), ("paragraph", None, [], text_, 1)], title=title,
                             source_kind="appraisal")


def workspace(office, intent: str, user=None) -> T.Workspace:
    ws = T.Workspace(ctx=office.ctx() if user is None else office.ctx("employee", user))
    ws.intent = intent
    return ws


def run(ws, name, **args) -> str:
    return T.run_tool(ws, name, json.dumps(args, ensure_ascii=False))


def sources_of(output: str) -> list[dict]:
    """The header attributes of every ``<source>`` in a tool output (a listing too), with its body."""
    found = []
    for m in re.finditer(r"<source ([^>]*)>(.*?)</source>", output, re.S):
        found.append(dict(re.findall(r'(\w+)="([^"]*)"', m.group(1))) | {"body": m.group(2)})
    return found


# --- the pools of an intent ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("intent, pools", [
    ("professional", {"professional": "primary", "appraisal": "example"}),
    ("property", {"appraisal": "primary", "professional": "method"}),
    ("document", {"appraisal": "primary", "professional": "primary"}),
    ("mixed", {"appraisal": "primary", "professional": "primary"}),
    ("unknown", {"professional": "primary", "appraisal": "example"}),
    ("set", {"appraisal": "primary"}),
])
def test_the_pools_follow_the_settled_intent(office, intent, pools):
    ws = workspace(office, intent)
    assert ws.pools == pools
    ws.intent = "professional" if intent == "set" else "set"  # the pools are set where the intent is, never kept apart
    assert ws.pools != pools


def test_a_professional_question_searches_the_standard_and_shows_appraisals_as_labelled_examples(office):
    std, _ = standard(office)
    prop, _ = appraisal(office)
    gloss, _ = appraisal(office, GLOSSARY, title="מילון מונחים (סינתטי)", opening="מילון מונחים")
    out = run(workspace(office, "professional"), "search", query=QUERY)
    hits = {s["document_id"]: s for s in sources_of(out) if QUERY.split()[0] in s["body"] or s["document_id"] == std}
    assert hits[std]["source_kind"] == "standard" and "label" not in hits[std]
    # an appraisal that belongs to an identified property is that property's example; one that names none is only
    # named by its stored kind (office A's glossary is stored as an appraisal until an admin changes it)
    assert hits[prop]["label"].startswith(T.LABEL_EXAMPLE) and "הרקפות" in hits[prop]["label"]
    assert hits[gloss]["label"] == T.LABEL_APPRAISAL_KIND
    assert out.index(std) < out.index(prop)  # the professional pool leads


def test_an_appraisal_hit_under_professional_intent_never_claims_an_identity_it_has_not(office):
    gloss, _ = appraisal(office, GLOSSARY, title="מילון מונחים (סינתטי)", opening="מילון מונחים")
    standard(office)
    hit = next(s for s in sources_of(run(workspace(office, "professional"), "search", query=QUERY))
               if s["document_id"] == gloss and GLOSSARY in s["body"])
    assert MSG_UNNAMED not in hit["label"] and "דוגמה" not in hit["label"]


def test_the_model_cannot_widen_the_pool_the_server_chose(office):
    std, _ = standard(office)
    prop, _ = appraisal(office)
    ws = workspace(office, "professional")
    out = run(ws, "search", query=QUERY, pool="appraisal")  # appraisals under a professional question: examples only,
    docs = {s["document_id"] for s in sources_of(out)}  # read beside the standards, never instead of them (real-model run)
    assert docs == {std, prop} and T.MSG_POOL_EXAMPLES in out
    ws = workspace(office, "set")
    out = run(ws, "search", query=QUERY, pool="professional")  # a set question has no professional pool: clipped
    assert {s["document_id"] for s in sources_of(out)} == {prop} and T.MSG_POOL_CLIPPED in out
    out = run(workspace(office, "property"), "search", query=QUERY, pool="no-such-pool")
    assert {s["document_id"] for s in sources_of(out)} == {prop} and T.MSG_POOL_CLIPPED in out
    assert std not in out


def test_a_property_question_never_gets_standard_passages_unless_the_method_is_asked_for_and_then_labelled(office):
    std, _ = standard(office)
    prop, _ = appraisal(office)
    out = run(workspace(office, "property"), "search", query=APPROACH)
    assert std not in {s["document_id"] for s in sources_of(out)}
    ws = workspace(office, "property")
    out = run(ws, "search", query=APPROACH, pool="professional")
    (hit,) = [s for s in sources_of(out) if s["document_id"] == std][:1]
    assert hit["label"] == T.LABEL_METHOD and T.MSG_POOL_CLIPPED not in out
    # a method label is not property data: the source says so in the model's reading of it
    assert "לא נתון של נכס" in T.LABEL_METHOD


def test_a_mixed_question_gets_both_pools_and_a_document_question_the_named_document_only(office):
    std, _ = standard(office)
    prop, _ = appraisal(office)
    other, _ = appraisal(office, "הנכס ברחוב הדקל 9. עסקאות השוואה בנכסים דומים נבחרו אחרת.", title="שומה הדקל 9")
    docs = {s["document_id"] for s in sources_of(run(workspace(office, "mixed"), "search", query=QUERY))}
    assert {std, prop, other} <= docs
    ws = workspace(office, "document")
    ws.named_documents = (prop,)
    out = run(ws, "search", query=QUERY)
    assert {s["document_id"] for s in sources_of(out)} == {prop}
    # another document asked for explicitly is outside the named one: clipped, with the note
    out = run(ws, "search", query=QUERY, document_ids=[std])
    assert std not in {s["document_id"] for s in sources_of(out)} and T.MSG_POOL_CLIPPED in out


def test_an_unknown_intent_searches_both_pools_only_when_the_office_holds_a_professional_document(office):
    prop, _ = appraisal(office)
    before = run(workspace(office, "unknown"), "search", query=QUERY)
    # an office with no standard: the search is the appraisal search it always was (no label, no note, no message)
    assert sources_of(before) and all("label" not in s for s in sources_of(before))
    assert T.MSG_NO_STANDARD_POOL not in before and T.MSG_POOL_CLIPPED not in before
    named = run(workspace(office, "unknown"), "search", query=QUERY, pool="appraisal")  # naming the pool changes nothing
    assert sources_of(named) and all("label" not in s for s in sources_of(named)) and T.MSG_POOL_EXAMPLES not in named
    std, _ = standard(office)
    after = {s["document_id"]: s for s in sources_of(run(workspace(office, "unknown"), "search", query=QUERY))
             if QUERY.split()[0] in s["body"]}
    assert after[std]["source_kind"] == "standard" and after[prop]["label"].startswith(T.LABEL_EXAMPLE)


# --- the hits of a standard ----------------------------------------------------------------------------------------

def test_a_hit_in_a_section_with_a_limit_carries_its_section_its_limit_and_the_call_that_reads_it_whole(office):
    std, _ = standard(office)
    ws = workspace(office, "professional")
    out = run(ws, "search", query=APPROACH)
    hit = next(s for s in sources_of(out) if s["document_id"] == std and APPROACH in s["body"])
    assert "2.1" in hit["location"] and "תקן 98.1" in hit["title"] and "גרסה 2.0" in hit["title"]
    assert EXCEPTION in hit["body"], "the exception that limits the provision comes with it"
    handle = re.search(r"read\(section=(§\d+)\)", hit["body"]).group(1)
    assert ws.handles[handle]["document_id"] == std
    # a hit in a section with no limit carries no limit block
    rec = next(s for s in sources_of(run(ws, "search", query="להוון את ההכנסה לפי שיעור שוק"))
               if "מומלץ" in s["body"])
    assert T.MSG_LIMITS not in rec["body"]


def test_reading_a_section_returns_the_whole_of_it_with_its_subsections_its_exception_and_its_references(office):
    std, _ = standard(office)
    ws = workspace(office, "professional")
    run(ws, "search", query=APPROACH)
    out = run(ws, "outline", document=std)  # the heading-path outline still works on a standard
    assert "§" in out
    section = next(h for h, d in ws.handles.items() if d["kind"] == "§" and d.get("psection") is not None)
    got = run(ws, "read", target={"source": None, "pages": None, "section": section, "table": None, "cursor": None})
    (src,) = sources_of(got)
    assert EXCEPTION in src["body"] and APPROACH in src["body"] and src["source_kind"] == "standard"
    assert "סעיף 2.1" in src["location"] and "status" in src and src["status"] == "complete"
    # the chapter holds its subsections: read whole, with their handles
    chapter = next(s for s in ws.sources.values() if s.section and s.standard and s.standard["section"] == "2.1")
    assert chapter.standard["section_title"] == "גישת ההשוואה"


def test_a_chapter_read_lists_its_subsections_and_resolves_its_references(office):
    std, _ = standard(office)
    ws = workspace(office, "professional")
    run(ws, "search", query="לפירוט שיטות ההערכה")
    first = next(h for h, d in ws.handles.items() if d.get("psection") is not None)
    out = run(ws, "read", target={"source": None, "pages": None, "section": first, "table": None, "cursor": None})
    # "ראו סעיף 2" resolves to a section handle; "תקן 97" is held by no visible document: stated as a gap
    assert re.search(r"סעיף 2[^\n]*§\d+", out), out
    assert "תקן 97" in out and "לא במאגר — התוכן אינו זמין" in out
    ws2 = workspace(office, "professional")
    other, _ = standard(office, "97", title="תקן שבו מפנים", sha="9" * 64)
    run(ws2, "search", query="לפירוט שיטות ההערכה")
    first = next(h for h, d in ws2.handles.items() if d.get("psection") is not None)
    out = run(ws2, "read", target={"source": None, "pages": None, "section": first, "table": None, "cursor": None})
    assert "לא במאגר" not in out and other in out


def test_a_chapter_read_names_its_subsections_and_a_section_names_its_parent(office):
    std, _ = standard(office)
    ws = workspace(office, "professional")
    run(ws, "search", query=APPROACH)
    sub = next(h for h, d in ws.handles.items() if d.get("psection") is not None)
    one = run(ws, "read", target={"source": None, "pages": None, "section": sub, "table": None, "cursor": None})
    parent = re.search(r"סעיף אב: (§\d+)", one).group(1)
    out = run(ws, "read", target={"source": None, "pages": None, "section": parent, "table": None, "cursor": None})
    assert "תתי-סעיפים" in out and "2.1" in out and "2.2" in out
    # the chapter holds its subsections' text, exception included (what the turn already returned is sent as a pointer)
    whole = list(ws.sources.values())[-1]
    assert "מומלץ להוון" in whole.text and EXCEPTION in whole.text


# --- the same text, once -------------------------------------------------------------------------------------------

def test_identical_text_is_one_hit_and_the_listing_names_the_copy_as_the_same_text(office):
    first, _ = standard(office, title="עותק ראשון", sha="a" * 64)
    copy, _ = standard(office, title="עותק שני", sha="b" * 64)
    ws = workspace(office, "professional")
    out = run(ws, "search", query=APPROACH)
    docs = [s["document_id"] for s in sources_of(out) if APPROACH in s["body"]]
    assert docs == [first]  # one hit, in the earliest created
    listing = run(ws, "find_standards")
    (row,) = [s for s in sources_of(listing)]  # one listing source
    assert listing.count("document_id=" + first) == 1
    assert re.search(r"אותו נוסח כמו [^\n]*עותק ראשון", listing) and copy in listing
    # nothing is deleted: the copy is still stored and found when named
    explicit = run(workspace(office, "professional"), "search", query=APPROACH, document_ids=[copy])
    assert copy in {s["document_id"] for s in sources_of(explicit)}


def test_two_editions_with_different_text_or_a_different_stated_edition_are_never_one(office):
    old, _ = standard(office, title="מהדורה ישנה", sha="1" * 64, edition="גרסה 1.0", effective="01.01.2022")
    new, _ = standard(office, title="מהדורה חדשה", sha="2" * 64, edition="גרסה 2.0", effective="05.10.2025")
    changed, _ = standard(office, "99", title="נוסח אחר", sha="3" * 64, approach="על השמאי להסתמך על שלוש עסקאות לפחות.")
    ws = workspace(office, "professional")
    listing = run(ws, "find_standards")
    assert "אותו נוסח כמו" not in listing
    assert {old, new, changed} <= set(re.findall(r"document_id=([0-9a-f-]{36})", listing))


# --- find_standards ------------------------------------------------------------------------------------------------

def test_find_standards_lists_what_each_states_the_editions_with_their_order_and_what_is_unknown(office):
    old, _ = standard(office, title="מהדורה ישנה", sha="1" * 64, edition="גרסה 1.0", effective="01.01.2022")
    new, _ = standard(office, title="מהדורה חדשה", sha="2" * 64, edition="גרסה 2.0", effective="05.10.2025")
    bare, _ = professional_file(office, office.default_group_id,
                                [("paragraph", None, [], MARK, 1), ("paragraph", None, [], "מסמך ללא פרטים.", 1)],
                                title="חוזר מקצועי", source_kind="professional")
    ws = workspace(office, "professional")
    out = run(ws, "find_standards")
    assert "מספר: 98.1" in out and "גרסה 1.0" in out and "גרסה 2.0" in out
    assert "01.01.2022" in out and "05.10.2025" in out
    assert "סדר המהדורות: ידוע" in out
    assert re.search(r"האחרונה[^\n]*05\.10\.2025|המאוחרת[^\n]*05\.10\.2025", out), out
    row = next(line for line in out.splitlines() if bare in line)
    assert "מספר: לא ידוע" in row and "מהדורה: לא ידוע" in row and "בתוקף מ: לא ידוע" in row
    assert sources_of(out)[0]["kind"] == "listing"  # a listing source the answer may cite


def test_editions_without_stated_dates_have_no_order_and_none_is_called_the_latest(office):
    a, _ = standard(office, title="נוסח א", sha="1" * 64, edition="גרסה 1.0", effective=None)
    b, _ = standard(office, title="נוסח ב", sha="2" * 64, edition="גרסה 2.0", effective=None)
    out = run(workspace(office, "professional"), "find_standards")
    assert "סדר המהדורות: אינו ידוע" in out
    assert "האחרונה" not in out and "המאוחרת" not in out
    assert T.MSG_ORDER_UNKNOWN in out
    # a single edition with a date is not called "the latest" either: only an order between editions is a claim
    out = run(workspace(office, "professional"), "find_standards", query="נוסח א")
    assert "האחרונה" not in out


def test_an_amending_document_is_listed_with_the_standard_it_amends(office):
    base, _ = standard(office, title="התקן")
    amend, _ = professional_file(
        office, office.default_group_id,
        [("paragraph", None, [], MARK, 1), ("heading", None, [], "הבהרה לתקן 98.1", 1),
         ("paragraph", None, [], "הבהרה זו מסבירה את סעיף 2.1.", 1)], title="הבהרה", source_kind="professional")
    out = run(workspace(office, "professional"), "find_standards")
    line = next(ln for ln in out.splitlines() if amend in ln)
    assert "מבהיר" in line and "98.1" in line and base in out


# --- an empty professional pool ------------------------------------------------------------------------------------

def test_a_professional_search_with_no_standard_in_the_office_says_so_and_still_gives_the_examples(office):
    prop, _ = appraisal(office)
    ws = workspace(office, "professional")
    out = run(ws, "search", query=QUERY)
    assert T.MSG_NO_STANDARD_POOL in out
    assert {s["document_id"] for s in sources_of(out)} == {prop}  # the other permitted pool still answers
    assert ws.standards_absent is True
    ws2 = workspace(office, "property")
    out = run(ws2, "search", query=QUERY)
    assert T.MSG_NO_STANDARD_POOL not in out and ws2.standards_absent is False
    out = run(ws2, "search", query=QUERY, pool="professional")  # asked for the professional pool by name: told
    assert T.MSG_NO_STANDARD_POOL in out and ws2.standards_absent is True


def test_with_no_standard_the_offices_other_documents_answer_in_full_not_as_three_examples(office):
    # round 10 R29, office-A real-model run on f43430d: a glossary stored as an appraisal is the only professional text
    # until standards are uploaded; a professional question must still be able to answer from it, labelled by its kind
    docs = [appraisal(office, text_=f"{GLOSSARY} מונח {i}.", title=f"מילון {i} (סינתטי)", opening=f"מילון מונחים {i}")[0]
            for i in range(5)]
    ws = workspace(office, "professional")
    out = run(ws, "search", query=QUERY, limit=8)
    found = {s["document_id"] for s in sources_of(out)}
    assert len(found) > T.EXAMPLE_HITS and found <= set(docs)
    assert T.MSG_NO_STANDARD_POOL in out and "ענה מהמקורות האחרים" in out
    assert T.LABEL_APPRAISAL_KIND in out  # no property stated: labelled by its kind, not as a property's example


def test_asking_for_the_professional_pool_by_name_in_an_office_with_none_reads_the_other_documents(office):
    # office-A real-model run on 044fda4: the model called search(pool="professional") and got the gap note alone
    doc, _ = appraisal(office, text_=GLOSSARY, title="מילון (סינתטי)", opening="מילון מונחים")
    ws = workspace(office, "professional")
    out = run(ws, "search", query=QUERY, pool="professional")
    assert T.MSG_NO_STANDARD_POOL in out and {s["document_id"] for s in sources_of(out)} == {doc}


def test_find_standards_in_an_office_with_none_says_the_repository_holds_none(office):
    appraisal(office)
    ws = workspace(office, "professional")
    assert run(ws, "find_standards") == T.MSG_NO_STANDARDS
    assert ws.standards_absent is True


def test_a_search_that_finds_nothing_in_an_office_that_holds_standards_is_not_an_absent_standard(office):
    standard(office)
    ws = workspace(office, "professional")
    out = run(ws, "search", query="נושא שאינו בתקן כלל ועיקר")
    assert T.MSG_NO_STANDARD_POOL not in out and ws.standards_absent is False


# --- permissions ---------------------------------------------------------------------------------------------------

def test_an_employee_outside_the_group_sees_no_standard_in_any_tool(office):
    std, _ = standard(office, group=office.private)
    member = workspace(office, "professional", office.member)
    out = run(member, "search", query=APPROACH)
    assert std in {s["document_id"] for s in sources_of(out)}
    section = next(h for h, d in member.handles.items() if d.get("psection") is not None)
    outsider = workspace(office, "professional", office.outsider)
    out = run(outsider, "search", query=APPROACH)
    assert std not in out and T.MSG_NO_STANDARD_POOL in out and outsider.standards_absent is True
    assert run(outsider, "find_standards") == T.MSG_NO_STANDARDS
    refused = run(outsider, "search", query=APPROACH, document_ids=[std])
    assert "לא נמצאו" in refused and std not in {s["document_id"] for s in sources_of(refused)}
    # a handle issued to someone who may see the document opens nothing for someone who may not
    outsider.handles[section] = member.handles[section]
    stolen = run(outsider, "read", target={"source": None, "pages": None, "section": section, "table": None,
                                           "cursor": None})
    assert stolen.startswith("שגיאה") and EXCEPTION not in stolen


# --- the scope of the search itself ---------------------------------------------------------------------------------

def test_the_source_kinds_of_a_scope_restrict_every_stage_of_the_search(office):
    std, _ = standard(office)
    prop, _ = appraisal(office)
    with tenant_tx(office.ctx()) as conn:
        for kinds, wanted in ((("standard", "professional"), std), (("appraisal",), prop)):
            scope = SearchScope(source_kinds=kinds)
            assert {str(h["document_id"]) for h in search_evidence(conn, [QUERY], 10, scope=scope).hits} == {wanted}
            # the versions the trigram stage is given are resolved under the pool, whatever the other kinds hold
            assert {str(d) for _, d in _scoped_versions(conn, scope)} == {wanted}
            sql, _, _ = _prepare(conn, scope, None)
            sql = replace(sql, version_ids=[v for v, _ in _scoped_versions(conn, scope)])
            chunks = _trigram(conn, "עסקאות השוואה בנכסים דומים", sql)
            assert chunks and {str(r.document_id) for r in conn.execute(
                text("SELECT document_id FROM chunks WHERE id = ANY(:i)"), {"i": chunks})} == {wanted}
        both = search_evidence(conn, [QUERY], 10, scope=SearchScope()).hits
        assert {str(h["document_id"]) for h in both} == {std, prop}


def test_the_lexical_weights_of_a_pool_come_from_the_pool(office):
    from sqlalchemy import event

    standard(office)
    for i in range(3):  # appraisals that repeat the query's words make them common in the office as a whole
        appraisal(office, f"{GLOSSARY} שומה מספר {i}.", title=f"שומה {i}")
    seen: list[tuple[str, dict]] = []
    with tenant_tx(office.ctx()) as conn:
        event.listen(conn, "before_cursor_execute", lambda c, cur, sql, params, ctx, many: seen.append((sql, params)))
        search_evidence(conn, [QUERY], 3, scope=SearchScope(source_kinds=("standard", "professional")))
        counts = [p for sql, p in seen if "count(*) AS n" in sql]
        assert counts and all(p.get("idf_k") == ["standard", "professional"] for p in counts)
        seen.clear()
        search_evidence(conn, [QUERY], 3, scope=SearchScope())
        assert all("idf_k" not in p for sql, p in seen if "count(*) AS n" in sql)  # an open scope weighs by the office


def test_a_memoized_search_is_kept_per_pool_and_per_excluded_versions(office):
    std, _ = standard(office)
    prop, _ = appraisal(office)
    ws = workspace(office, "mixed")
    with tenant_tx(office.ctx()) as conn:
        a = T.memoized_search(ws, conn, QUERY, 5, SearchScope(source_kinds=("standard",)))
        b = T.memoized_search(ws, conn, QUERY, 5, SearchScope(source_kinds=("appraisal",)))
        again = T.memoized_search(ws, conn, QUERY, 5, SearchScope(source_kinds=("standard",)))
        c = T.memoized_search(ws, conn, QUERY, 5, SearchScope(source_kinds=("standard",), exclude_versions=(
            a[0]["version_id"],)))
    assert {str(h["document_id"]) for h in a} == {std} and {str(h["document_id"]) for h in b} == {prop}
    assert [h["chunk_id"] for h in again] == [h["chunk_id"] for h in a] and len(ws.search_memo) == 3 and c == []


# --- a section longer than one part -----------------------------------------------------------------------------------

def test_a_long_section_is_read_in_parts_that_continue_with_a_cursor_to_its_end(office, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_read_chars", 160)
    long_text = [f"פסקה {i}: על השמאי לתעד את הנתונים שנאספו בסעיף זה ולשמור אותם בתיק העבודה." for i in range(1, 9)]
    path = ["3. תיעוד"]
    blocks = [("paragraph", None, [], MARK, 1), ("heading", "3", path, "3. תיעוד", 1)]
    blocks += [("paragraph", None, path, t, 1 + i // 4) for i, t in enumerate(long_text)]
    std, _ = professional_file(office, office.default_group_id, blocks, title="תקן ארוך")
    ws = workspace(office, "professional")
    run(ws, "search", query="לשמור אותם בתיק העבודה")
    handle = next(h for h, d in ws.handles.items() if d.get("psection") is not None)
    got, seen = [], set()
    out = run(ws, "read", target={"source": None, "pages": None, "section": handle, "table": None, "cursor": None})
    while True:
        (src,) = sources_of(out)
        got.append(src)
        more = re.search(r"read\(cursor=(K\d+)\)", src["body"])
        if more is None:
            break
        assert src["status"] == "clipped" and more.group(1) not in seen
        seen.add(more.group(1))
        out = run(ws, "read", target={"source": None, "pages": None, "section": None, "table": None,
                                      "cursor": more.group(1)})
    assert len(got) > 1 and got[-1]["status"] == "complete"
    assert all(src["source_kind"] == "standard" for src in got)
    whole = "\n".join(s.text for s in ws.sources.values() if s.status in ("clipped", "complete"))
    assert all(t in whole for t in long_text)


# --- a document question of a turn ------------------------------------------------------------------------------------

def test_a_document_question_reads_only_the_documents_the_request_named(client, office, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    named, _ = appraisal(office, "השווי למ\"ר הוא 9,500 ₪ לפי עסקאות השוואה.", title="שומה הרקפות 5 (סינתטי)")
    other, _ = appraisal(office, "השווי למ\"ר הוא 11,000 ₪ לפי עסקאות השוואה.", title="שומה הדקל 9 (סינתטי)",
                         opening="שומה לנכס ברחוב הדקל 9")
    assert other in run(workspace(office, "property"), "search", query="עסקאות השוואה")  # unrestricted, it reads both
    first = ScriptedAgent([[call("search", query="עסקאות השוואה", document_ids=[named], limit=None)],
                           final("השווי הוא 9,500 ₪ [S1].", focus=_focus(named),
                                 claims=[{"text": "השווי הוא 9,500 ₪", "source_ids": ["S1"], "basis": "explicit"}])])
    cloud(monkeypatch, office, first)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    assert send(client, cid, "מה השווי למ\"ר?")["status"] == "done"
    second = ScriptedAgent([[call("search", query="עסקאות השוואה", document_ids=None, limit=None)],
                            final("המסמך מתבסס על עסקאות השוואה [S1].", focus=_focus(named),
                                  claims=[{"text": "המסמך מתבסס על עסקאות השוואה", "source_ids": ["S1"],
                                           "basis": "explicit"}])])
    second.on("resolve", _resolution(relation="same_datum", intent=intent_part("document", "named_entity"),
                                     document_ids=[named]))
    _next(monkeypatch, second)
    assert send(client, cid, "על מה המסמך הזה מתבסס?")["status"] == "done"
    seen = "\n".join(second.tool_outputs(1))
    assert named in seen and other not in seen
