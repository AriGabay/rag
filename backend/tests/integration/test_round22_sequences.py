"""Round 22 U11 sequences: history keeps meaning (KTD10, R19, R22, AE5). Synthetic world (office B, the round-20 lot-groups PDF, a scripted
model and judge): the first answer carries a condition the server wrote next to its value; what the NEXT turn's agent and resolve are
shown is read, and what the summary keeps of an old conversation.

- The condition reaches the next turn only as a marked system note, never in the answer prose, in the agent's history and in resolve's.
- A row whose document is no longer visible takes its notes with it.
- A summary rebuild for a new builder version writes the new summary and keeps the superseded text and its meta inside ``summary_meta``;
  no row is deleted.
"""

# ruff: noqa: F811 - the tests take the imported fixtures by name

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import api as chat_api
from app.db import tenant_tx
from app.providers.llm import Purpose
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_round18_sequences import DOCUMENT_OF, ingest_pdf, lenient_judge, read_table
from tests.integration.test_round20_sequences import FOOTNOTE, GROUP_A_ROW, GROUP_B_ROW, LOTS, lot_item
from tests.integration.test_search import add_chunks
from tests.support.scripted_agent import ScriptedAgent, call, component, final
from tests.unit.test_final_check import SEARCH, Judge, offline  # noqa: F401 - the fixture, taken by name
from tests.unit.test_final_check import _turn as engine_turn

pytestmark = pytest.mark.db

NOTE_LINE = re.compile(r'^\s*הערת מערכת על "[^"]*": .*$', re.M)


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return make_office(db, "משרד ב", "admin-b@example.test")


def first_turn(client, office, monkeypatch):
    """Turn one: group A's area, stated with the condition of its lease that the server writes next to the value (as round 21's AE8)."""
    doc = ingest_pdf(office, monkeypatch, LOTS)
    DOCUMENT_OF[0] = doc
    first = f"שטח קבוצת מגרשים א׳ הוא {GROUP_A_ROW[2]} מ״ר, לפי {GROUP_A_ROW[1]} [S1]."
    second = f"שטח קבוצת מגרשים ב׳ הוא {GROUP_B_ROW[2]} מ״ר, לפי {GROUP_B_ROW[1]} [S1]."
    outline = [call("outline", document=doc)]
    agent = ScriptedAgent([outline, read_table(LOTS["caption"]),
                           *[final(first, documents=[doc], items=[lot_item(GROUP_A_ROW, FOOTNOTE)]) for _ in range(5)]],
                          judge=lenient_judge, request=[component("השטח וחוזה החכירה של קבוצת מגרשים א׳")])
    cloud(monkeypatch, office, agent)
    login(client, "admin-b@example.test")
    cid = new_conversation(client)
    m1 = send(client, cid, "מהו השטח של קבוצת מגרשים א׳ ומהו חוזה החכירה שלה?")
    assert m1["status"] == "done" and "מינהל" in m1["answer"]["markdown"], m1
    agent.steps = [outline, read_table(LOTS["caption"]), *[final(second, documents=[doc]) for _ in range(5)]]
    return cid, agent, doc


def everything_seen(agent: ScriptedAgent, since_step: int, since_call: int) -> str:
    return "\n".join([str(items) for items in agent.seen[since_step:]] + [c.input for c in agent.calls[since_call:]])


def test_ae5_the_condition_of_the_first_answer_reaches_the_next_turn_as_a_system_note_for_the_agent_and_for_resolve(client, office, monkeypatch):
    cid, agent, _ = first_turn(client, office, monkeypatch)
    stored = _stored_answer(office, cid)
    assert any(sp["kind"] == "qualifier" for sp in stored["server_spans"]), stored["server_spans"]
    shown, calls = len(agent.seen), len(agent.calls)
    m2 = send(client, cid, "ומה השטח של קבוצת מגרשים ב׳?")
    assert m2["status"] == "done", m2
    context = agent.seen[shown][0]["content"]
    resolved = [c.input for c in agent.calls[calls:] if c.purpose == Purpose.RESOLVE]
    assert resolved, [c.purpose for c in agent.calls[calls:]]
    for seen in (context, resolved[0]):
        notes = NOTE_LINE.findall(seen)
        assert notes and any("מינהל" in n for n in notes), seen
        prose = [ln for ln in seen.splitlines() if ln.startswith("עוזר:")]
        assert prose and not any("מינהל" in ln or "התנאי לפי המקור" in ln for ln in prose), prose  # never as answer text
    # the second answer does not carry the first one's condition (the note is no source)
    assert "מינהל" not in m2["answer"]["markdown"], m2["answer"]["markdown"]


def test_a_note_of_a_row_whose_document_is_no_longer_visible_leaves_with_the_row(client, office, monkeypatch):
    cid, agent, doc = first_turn(client, office, monkeypatch)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": doc})
    shown, calls = len(agent.seen), len(agent.calls)
    send(client, cid, "ומה השטח של קבוצת מגרשים ב׳?")
    seen = everything_seen(agent, shown, calls)
    assert not NOTE_LINE.findall(seen) and "מינהל" not in seen, seen


def _stored_answer(office, cid: str) -> dict:
    with tenant_tx(office.system) as conn:
        row = conn.execute(text("SELECT answer FROM messages WHERE conversation_id = :c AND role = 'assistant'"
                                " ORDER BY created_at DESC LIMIT 1"), {"c": cid}).one()
    return row.answer


# --- the summary builder is versioned; a rebuild keeps the superseded summary ------------------------------------------------------------

MARKER = "קרן-השקמה-4471"
OPEN = "סיכום: השווי למ\"ר בנוי במבנה הגפן הוא 9,500 ₪."


@pytest.fixture
def summaries(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    monkeypatch.setattr(chat_api, "HISTORY_MESSAGES", 2)
    monkeypatch.setattr(chat_api, "SUMMARY_EVERY", 2)
    a = make_office(db, "משרד א", "admin-a@example.test")
    g = make_group(a, "קבוצה 2")
    make_user(a, "emp@example.test", [g])
    public, _ = add_chunks(a, g, [OPEN], "2" * 64)
    a.public = str(public)
    return a


def _public_turn(doc: str) -> list:
    return [[call("search", query="שווי למ\"ר", document_ids=[doc], limit=None)],
            final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].", documents=[doc])]


def _summary_row(office, cid: str):
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT summary, summary_message_count, summary_meta FROM conversations WHERE id = :c"),
                            {"c": cid}).one()


def test_a_summary_built_by_an_older_builder_is_rebuilt_with_the_superseded_one_kept(client, summaries, monkeypatch):
    steps = [s for _ in range(4) for s in _public_turn(summaries.public)]
    agent = ScriptedAgent(steps)
    prompts: list[str] = []

    def summarize(instructions, input):
        prompts.append(input)
        return {"summary": input[-3000:]}

    agent.on(Purpose.SUMMARY, summarize, repeat=True)
    cloud(monkeypatch, summaries, agent)
    login(client, "emp@example.test")
    cid = new_conversation(client)
    for i in range(4):
        assert send(client, cid, f"שאלה מספר {i + 1}")["status"] == "done"
    built = _summary_row(summaries, cid)
    assert built.summary and built.summary_meta["builder_version"] == chat_api.SUMMARY_VERSION, built.summary_meta
    # the summary a previous builder wrote: valid in every other respect, without a builder version
    old_text = "סיכום ישן: נשאל על השווי, בלי התנאים."
    old_meta = {k: v for k, v in built.summary_meta.items() if k != "builder_version"} | {
        "summary_sha256": chat_api.summary_digest(old_text)}
    with tenant_tx(summaries.system) as conn:
        conn.execute(text("UPDATE conversations SET summary = :s, summary_meta = CAST(:m AS jsonb) WHERE id = :c"),
                     {"s": old_text, "m": json.dumps(old_meta), "c": cid})
        before = conn.execute(text("SELECT count(*) FROM messages WHERE conversation_id = :c"), {"c": cid}).scalar_one()
    agent.steps += _public_turn(summaries.public)
    prompts.clear()
    assert send(client, cid, "שאלה מספר 5")["status"] == "done"
    after = _summary_row(summaries, cid)
    # round 22 review #3: the re-fold of a still-usable old summary gets the old text as the previous summary (not "אין"), so what it carried from
    # earlier messages is not lost
    assert len(prompts) == 1 and prompts[0].startswith("סיכום קודם:\n" + old_text), prompts
    assert after.summary_meta["builder_version"] == chat_api.SUMMARY_VERSION
    assert after.summary_meta["superseded"]["text"] == old_text
    assert after.summary_meta["superseded"]["meta"]["summary_sha256"] == old_meta["summary_sha256"]
    with tenant_tx(summaries.system) as conn:
        now = conn.execute(text("SELECT count(*) FROM messages WHERE conversation_id = :c"), {"c": cid}).scalar_one()
    assert now == before + 2  # the new turn's two messages; nothing was deleted
    # an incremental fold later keeps the superseded record
    for i in range(2):
        agent.steps += _public_turn(summaries.public)
        assert send(client, cid, f"שאלה נוספת {i}")["status"] == "done"
    assert _summary_row(summaries, cid).summary_meta["superseded"]["text"] == old_text


# --- U8: row-level attribution and the applies-to-datum check, over the whole turn (KTD7; R12-R14; AE3) -------------------------------------------
#
# Round 22 U8. The offline engine (a scripted model and judge over synthetic passages, no database): the agent declares the row of one property and
# the answer states the equal value of another. The condition of the first row never lands next to the second row's figure, in either order of the
# search results, and it lands next to the first row's own.

U8_NOTE = "הזכויות חלות רק על פי תכנית מאושרת"
U8_HEAD = "סוג שטח | שטח (מ״ר) | הערה"
U8_NORTH, U8_SOUTH = "שטח קרקע צפונית", "שטח קרקע דרומית"
U8_ASK = "שטח הקרקע הצפונית"
U8_QUESTION = "מה שטח הקרקע הצפונית?"
U8_NORTH_ITEM = {"component": U8_ASK, "label": U8_NORTH, "source": "S1", "quote": f"{U8_NORTH} | 1,200", "value": "1,200", "condition": "",
                 "state": "included", "reason": ""}
U8_TABLE = "\n".join([U8_HEAD, f"{U8_NORTH} | 1,200 | {U8_NOTE}", f"{U8_SOUTH} | 1,200 | ללא הערה"])


def _u8_turn(offline, answer: str, texts: list[str], item: dict):
    offline.texts = texts
    judge = Judge(lambda t, i, n: "supported", asks={U8_ASK: "שטח"})
    return engine_turn(ScriptedAgent([SEARCH, *[final(answer, items=[item]) for _ in range(3)]], judge=judge, request=[component(U8_ASK)]), U8_QUESTION)


def test_ae3_the_condition_of_one_row_is_not_written_next_to_the_equal_value_of_another_row_over_a_whole_turn(offline):
    out = _u8_turn(offline, "שטח הקרקע הדרומית הוא 1,200 מ״ר [S1].", [U8_TABLE], U8_NORTH_ITEM)
    md = out.answer.answer_markdown
    assert U8_NOTE not in md.split("\n\n")[0], md  # the sentence is about the south row: no condition of the north row beside its figure
    assert not [s for s in out.server_spans if s.kind == "qualifier"], out.server_spans
    out = _u8_turn(offline, "שטח הקרקע הצפונית הוא 1,200 מ״ר [S1].", [U8_TABLE], U8_NORTH_ITEM)
    md = out.answer.answer_markdown
    assert f"1,200 מ״ר (התנאי לפי המקור: {U8_NOTE}) [S1]" in md, md
    (note,) = [s for s in out.server_spans if s.kind == "qualifier"]
    assert note.datum.startswith("I") and note.addition == "condition", note


def test_the_order_the_search_returned_the_passages_in_does_not_move_a_condition_between_properties(offline):
    north_table = "\n".join([U8_HEAD, f"{U8_NORTH} | 1,200 | {U8_NOTE}"])
    south_table = "\n".join([U8_HEAD, f"{U8_SOUTH} | 1,200 | ללא הערה"])
    for texts, north_sid, south_sid in (([north_table, south_table], "S1", "S2"), ([south_table, north_table], "S2", "S1")):
        item = U8_NORTH_ITEM | {"source": north_sid}
        out = _u8_turn(offline, f"שטח הקרקע הדרומית הוא 1,200 מ״ר [{south_sid}].", texts, item)
        assert U8_NOTE not in out.answer.answer_markdown.split("\n\n")[0], (texts, out.answer.answer_markdown)
        out = _u8_turn(offline, f"שטח הקרקע הצפונית הוא 1,200 מ״ר [{north_sid}].", texts, item)
        assert f"1,200 מ״ר (התנאי לפי המקור: {U8_NOTE}) [{north_sid}]" in out.answer.answer_markdown, (texts, out.answer.answer_markdown)
