"""An assumption (A#) in a stored answer names the user message it quotes, so the breakdown can link back to it (U7).

Database-free: the turn's user messages are plain rows, and the stored assumptions are their public dicts. Round 7 U7
(KTD9, R25): a clarification's pending parameter is bound to the user's reply and its found values to their P#."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from app.chat import api, calc

U1 = UUID("00000000-0000-0000-0000-000000000011")
U2 = UUID("00000000-0000-0000-0000-000000000012")
U3 = UUID("00000000-0000-0000-0000-000000000013")
A1 = UUID("00000000-0000-0000-0000-0000000000a1")


def _row(id_, role, content):
    return SimpleNamespace(id=id_, role=role, content=content)


def _assumption(aid: str, turn: int, current: bool) -> dict:
    return calc.Assumption(aid, Decimal("5"), "5%", "percent", "עליית עלויות", "אם העלויות יעלו ב-5%", turn,
                           current).public()


def test_user_message_ids_follow_the_engine_numbering():
    # the engine numbers the history's user messages with content (1 = first), then the current message
    rows = [_row(U1, "user", "שאלה ראשונה"), _row(A1, "assistant", "תשובה"), _row(U2, "user", ""),
            _row(U3, "user", "שאלה שנייה")]
    assert api._user_message_ids(rows, "current-id") == [str(U1), str(U3), "current-id"]


def test_an_assumption_names_the_user_message_it_quotes():
    ids = [str(U1), str(U3), "current-id"]
    earlier, current = _assumption("A1", 1, False), _assumption("A2", 3, True)
    out = api._assumption_origins([earlier, current], ids)
    assert [a["message_id"] for a in out] == [str(U1), "current-id"]
    # the user's own words stay with it
    assert out[0]["quote"] == "אם העלויות יעלו ב-5%"


def test_an_assumption_whose_message_is_unknown_has_no_message_id():
    out = api._assumption_origins([_assumption("A1", 4, False), _assumption("A2", 0, False)], [str(U1)])
    assert [a["message_id"] for a in out] == [None, None]
    assert api._assumption_origins([_assumption("A1", 1, True)], None)[0]["message_id"] is None


# --- round 7 U7 (KTD9, R25): a clarification's pending parameter, bound to the user's reply -----------------------

PENDING = {"component": "N1", "text": "הרווח היזמי אם העלויות יעלו", "parameters": ["שיעור העלייה של העלויות"],
           "question": "באיזה שיעור יעלו העלויות?",
           "found": [{"id": "V1", "label": "ההכנסות", "value_text": "24,600,000", "source_id": "S1"},
                     {"id": "V2", "label": "העלויות", "value_text": "18,350,000", "source_id": "S1"}]}


def test_a_reply_with_a_number_is_bound_to_the_pending_parameter():
    from app.chat import resolve

    bound = resolve.bind_pending(PENDING, "8%")
    assert bound["parameter"] == "שיעור העלייה של העלויות" and bound["quote"] == "8%" and bound["value"] == "8"
    assert bound["component"]["kind"] == "calculation" and bound["component"]["text"] == PENDING["text"]
    (p,) = bound["component"]["parameters"]
    assert p == {"name": "שיעור העלייה של העלויות", "source": "given_by_user", "quote": "8%"}
    longer = resolve.bind_pending(PENDING, "נניח שהעלויות יעלו ב-8 אחוז")
    assert longer["quote"] == "נניח שהעלויות יעלו ב-8 אחוז" and longer["value"] == "8"


def test_a_reply_without_a_number_or_without_a_pending_parameter_binds_nothing():
    from app.chat import resolve

    assert resolve.bind_pending(PENDING, "לא יודע, מה אתה מציע?") is None
    assert resolve.bind_pending(None, "8%") is None
    assert resolve.bind_pending(dict(PENDING, parameters=[]), "8%") is None


def test_the_pending_found_values_are_reopened_through_the_previous_answers_references():
    # the previous answer's sources S1 (v1), S2 (a listing: no version) and S3 (v2), as ``_turn_input`` numbers them
    sources = {"S1": "P1", "S3": "P2"}
    out = api._pending_refs(PENDING, sources)
    assert [(v["id"], v["prior"]) for v in out["found"]] == [("V1", "P1"), ("V2", "P1")]
    # a value whose passage is not among the references keeps no P#
    moved = dict(PENDING, found=[dict(PENDING["found"][0], source_id="S9")])
    assert api._pending_refs(moved, sources)["found"][0]["prior"] is None


def test_the_turn_is_told_the_reply_gives_the_pending_parameter_and_where_the_found_values_are():
    from app.chat import engine, resolve

    pending = api._pending_refs(PENDING, {"S1": "P1"})
    inp = engine.TurnInput(question="8%", history=[], summary=None, focus_documents=[], prior_refs={"P1": {}},
                           pending=pending)
    text = engine._context_message(inp, binding=resolve.bind_pending(pending, "8%"))
    assert "שיעור העלייה של העלויות" in text and "assume" in text and "«8%»" in text
    assert "P1" in text and "24,600,000" in text and "search" in text


def test_a_resolutions_component_takes_the_reply_for_the_pending_parameter():
    from app.chat import engine, resolve

    binding = resolve.bind_pending(PENDING, "8%")
    resolved = [{"id": "N1", "text": "הרווח בתרחיש", "kind": "calculation",
                 "parameters": [{"name": "שיעור העלייה של העלויות", "source": "not_given_by_user", "quote": ""},
                                {"name": "מועד", "source": "not_given_by_user", "quote": ""}]}]
    (c,) = engine._with_binding(resolved, binding)
    assert c["parameters"] == [{"name": "שיעור העלייה של העלויות", "source": "given_by_user", "quote": "8%"},
                               {"name": "מועד", "source": "not_given_by_user", "quote": ""}]
    assert engine._with_binding(resolved, None) is resolved


TWO_PENDING = dict(PENDING, parameters=["שיעור העלייה של העלויות", "תקופת העלייה"],
                   question="מהי תקופת העלייה שלפיה לחשב?")


def test_a_reply_is_bound_to_the_parameter_the_clarification_asked_about_and_the_others_stay_pending():
    from app.chat import resolve

    bound = resolve.bind_pending(TWO_PENDING, "3")
    assert bound["parameter"] == "תקופת העלייה"
    assert bound["component"]["parameters"] == [
        {"name": "שיעור העלייה של העלויות", "source": "not_given_by_user", "quote": ""},
        {"name": "תקופת העלייה", "source": "given_by_user", "quote": "3"}]
    # a question that names both, or neither: the first parameter still waiting, in order
    for question in ("באיזה שיעור ולאיזו תקופה?", "מהי תקופת העלייה ומהו שיעור העלייה של העלויות?"):
        assert resolve.bind_pending(dict(TWO_PENDING, question=question), "8%")["parameter"] == \
            "שיעור העלייה של העלויות"


def test_only_a_short_reply_of_a_number_alone_is_a_number_only_reply():
    from app.chat import resolve

    for reply in ("8%", " 8 % ", "8 אחוז", "כ-8%", "1,500,000 ₪", "3.5.", "-2%"):
        assert resolve.number_only_reply(reply), reply
    for reply in ("מה השווי למ\"ר בקומה 3?", "נניח שהעלויות יעלו ב-8 אחוז", "דירה 4", "", "אחוז"):
        assert not resolve.number_only_reply(reply), reply
