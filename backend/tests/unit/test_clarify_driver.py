"""Round 26 U1: the measurement driver's policy logic, on fake payloads (no network, no server). Synthetic names only."""

from __future__ import annotations

import json

import pytest

from eval import clarify_driver as cd


def _clarify(options, cid="q-1", allow_all=False, state="open"):
    return {"id": cid, "question": "איזה נכס?", "reason": "יש כמה", "options": options, "allow_all": allow_all,
            "state": state}


def _msg(options=None, *, status="done", answer_status="clarification", on="answer", **extra):
    m = {"id": "m-1", "role": "assistant", "status": status, "answer": {"status": answer_status, "markdown": "x"}}
    if options is not None:
        c = _clarify(options, **extra)
        if on == "answer":
            m["answer"]["clarify"] = c
        else:
            m["clarify"] = c
    return m


OPTS = [
    {"id": "o1", "label": "שדרות הדקל 12", "detail": "שומה מיום 01.03.2024"},
    {"id": "o2", "label": "שדרות הדקל 14", "detail": "שומה מיום 15.09.2023"},
    {"id": "o3", "label": "רחוב הגפן 3", "detail": "שומה מיום 01.03.2024"},
]


def test_click_matches_label_and_sends_the_bound_choice():
    r = cd.resolve_reply("click:הגפן", _msg(OPTS), typed_equivalent="הנכס ברחוב הגפן")
    assert r.how == "clicked"
    assert r.clarification == {"id": "q-1", "option_id": "o3"}
    assert r.option["id"] == "o3"


def test_click_matches_detail_when_no_label_does():
    r = cd.resolve_reply("click:15.09.2023", _msg(OPTS))
    assert r.clarification == {"id": "q-1", "option_id": "o2"}


def test_label_match_beats_detail_match_and_several_matches_take_the_first():
    r = cd.resolve_reply("click:הדקל", _msg(OPTS))
    assert r.clarification["option_id"] == "o1" and r.matches == 2 and r.note
    # "01.03.2024" is only in details (o1, o3): first in order wins, flagged as ambiguous
    r = cd.resolve_reply("click:01.03.2024", _msg(OPTS))
    assert r.clarification["option_id"] == "o1" and r.matches == 2
    # a label hit on a later option wins over an earlier detail-only hit
    opts = [{"id": "a", "label": "אחר", "detail": "גפן"}, {"id": "b", "label": "גפן", "detail": ""}]
    assert cd.resolve_reply("click:גפן", _msg(opts)).clarification["option_id"] == "b"


def test_click_matching_ignores_case_and_spacing():
    opts = [{"id": "x", "label": "Block  7 /  Plot 2", "detail": None}]
    assert cd.resolve_reply("click:block 7 / plot 2", _msg(opts)).clarification["option_id"] == "x"


def test_base_arm_types_the_equivalent_when_there_is_no_clarify_payload():
    base = _msg(None)  # status clarification, text only
    r = cd.resolve_reply("click:הגפן", base, typed_equivalent="הנכס ברחוב הגפן")
    assert (r.how, r.content, r.clarification) == ("typed_equivalent", "הנכס ברחוב הגפן", None)
    # the same policy on the new arm clicks instead
    assert cd.resolve_reply("click:הגפן", _msg(OPTS), typed_equivalent="הנכס ברחוב הגפן").clarification


def test_click_without_payload_or_equivalent_types_the_match_and_says_so():
    r = cd.resolve_reply("click:הגפן", _msg(None))
    assert (r.content, r.how) == ("הגפן", "typed_fallback") and r.note


def test_click_that_matches_nothing_falls_back_to_typing_and_is_flagged():
    r = cd.resolve_reply("click:אין כזה", _msg(OPTS), typed_equivalent="הנכס השני")
    assert (r.how, r.content, r.clarification) == ("typed_fallback", "הנכס השני", None)


def test_payload_may_sit_on_the_message_itself():
    r = cd.resolve_reply("click:הגפן", _msg(OPTS, on="message"))
    assert r.clarification == {"id": "q-1", "option_id": "o3"}


def test_typed_policies():
    prev = _msg(OPTS)
    assert cd.resolve_reply("number:2", prev).content == "2"
    assert cd.resolve_reply("number:2", prev).clarification is None
    assert cd.resolve_reply("dont_know", prev).content == "לא יודע"
    assert cd.resolve_reply("text:  השומה הישנה ", prev).content == "השומה הישנה"
    assert cd.resolve_reply("new_question:מה שטח הנכס?", prev).content == "מה שטח הנכס?"
    assert cd.resolve_reply("correction:התכוונתי לשומה הראשונה", prev).content == "התכוונתי לשומה הראשונה"
    # colons inside the argument are kept
    assert cd.resolve_reply("text:שעה 10:30", prev).content == "שעה 10:30"


def test_all_clicks_an_all_option_when_offered_else_types():
    with_all = OPTS + [{"id": "all", "label": "כולן", "detail": "כל השומות"}]
    r = cd.resolve_reply("all", _msg(with_all))
    assert r.clarification == {"id": "q-1", "option_id": "all"} and r.how == "clicked"
    r = cd.resolve_reply("all", _msg(OPTS))
    assert (r.content, r.clarification, r.how) == ("כולן", None, "typed_fallback")
    r = cd.resolve_reply("all", _msg(None))
    assert (r.content, r.clarification, r.how) == ("כולן", None, "typed_equivalent")


@pytest.mark.parametrize("bad", ["", "click", "click:", "number:x", "number:", "dont_know:1", "all:1", "shout:hi", "text:"])
def test_bad_policies_are_refused(bad):
    with pytest.raises(cd.CaseError):
        cd.parse_policy(bad)


def test_repeat_detector_compares_option_sets_not_order_or_case():
    first = cd.option_set(OPTS)
    assert cd.is_repeat(list(reversed(OPTS)), [first])
    shouting = [{"id": "z", "label": o["label"].upper(), "detail": f" {o['detail']} "} for o in OPTS]
    assert cd.is_repeat(shouting, [first])  # ids differ, labels and details are the same
    assert not cd.is_repeat(OPTS[:2], [first])  # a narrowed list is not a repeat
    assert not cd.is_repeat(OPTS, [])
    assert not cd.is_repeat([], [first, frozenset()])  # no options is never a repeat
    other = [{"id": "q", "label": "נכס אחר", "detail": "שומה אחרת"}]
    assert not cd.is_repeat(other, [first])
    assert cd.is_repeat(other, [first, cd.option_set(other)])  # any earlier set counts


def test_asked_detection():
    assert cd.asked_of(_msg(OPTS))
    assert cd.asked_of(_msg(None))  # answer status clarification, no payload (base arm)
    assert cd.asked_of({"status": "clarification", "answer": {}})
    assert cd.asked_of({"status": "done", "answer": {"status": "answered", "clarification": "איזה נכס?"}})
    assert not cd.asked_of(_msg(None, answer_status="answered"))
    assert not cd.asked_of(None)


def test_message_cost_sums_exposed_usage_and_is_none_without():
    m = {"usage": [{"cost_usd": 0.001}, {"cost_usd": 0.0025}, {"cost_usd": 0}]}
    assert cd.message_cost(m) == pytest.approx(0.0035)
    assert cd.message_cost({"usage": []}) is None
    assert cd.message_cost({}) is None


def test_turn_record_carries_the_measured_fields_and_flags_repeats():
    case = {"id": "c1", "split": "dev", "kind": "ambiguous"}
    answer_msg = _msg(OPTS)
    answer_msg["answer"] |= {"searches": [1, 2, 3], "steps": 4, "sources": [
        {"document_id": "d1"}, {"document_id": "d1"}, {"document_id": "d2"}]}
    answer_msg["usage"] = [{"cost_usd": 0.002}]
    rec = cd.turn_record(case, arm="new", run=2, index=3, step={"reply": "click:הגפן"}, sent=cd.Reply("x"),
                         user={"id": "u-1"}, assistant=answer_msg, seconds=4.5, earlier_sets=[cd.option_set(OPTS)])
    assert rec["asked"] and rec["has_clarify"] and rec["repeat_options"]
    assert [o["label"] for o in rec["options"]] == [o["label"] for o in OPTS]
    assert (rec["searches"], rec["steps"], rec["cost_usd"], rec["seconds"]) == (3, 4, 0.002, 4.5)
    assert rec["cited_document_ids"] == ["d1", "d2"]
    assert (rec["arm"], rec["run"], rec["turn"], rec["user_message_id"]) == ("new", 2, 3, "u-1")
    json.dumps(rec, ensure_ascii=False)  # serialisable


def test_case_validation():
    ok = {"id": "a", "split": "dev", "kind": "clear", "turns": [{"ask": "מה השטח?"}, {"reply": "number:1"}]}
    assert cd.validate_cases([ok]) == [ok]
    for bad in (
        ok | {"split": "train"},
        ok | {"kind": "weird"},
        ok | {"turns": []},
        ok | {"turns": [{"reply": "number:1"}]},
        ok | {"turns": [{"ask": "x", "reply": "number:1"}]},
        ok | {"turns": [{"ask": "x"}, {"reply": "click"}]},
        ok | {"id": ""},
    ):
        with pytest.raises(cd.CaseError):
            cd.validate_cases([bad])
    with pytest.raises(cd.CaseError):
        cd.validate_cases([ok, ok])  # repeated id


def test_load_select_and_dry_plan(tmp_path, capsys):
    f = tmp_path / "cases.yaml"
    f.write_text(
        "cases:\n"
        "  - {id: a, split: dev, kind: ambiguous, typed_equivalent: 'הנכס הראשון', turns: [{ask: 'שאלה'}, {reply: 'click:ראשון'}]}\n"
        "  - {id: b, split: heldout, kind: clear, turns: [{ask: 'שאלה ברורה'}]}\n", encoding="utf-8")
    cases = cd.load_cases(f)
    assert [c["id"] for c in cd.select_cases(cases, split="heldout")] == ["b"]
    assert [c["id"] for c in cd.select_cases(cases, only={"a"})] == ["a"]
    assert any("base arm types: הנכס הראשון" in line for line in cd.plan_lines(cases[0]))
    # --dry prints the plan and touches no network, no env file
    assert cd.main(["--cases", str(f), "--arm", "base", "--out", str(tmp_path / "o.jsonl"), "--dry"]) == 0
    out = capsys.readouterr().out
    assert "a [dev/ambiguous]" in out and "reply click:ראשון" in out and not (tmp_path / "o.jsonl").exists()


def test_env_file_credentials_and_no_secret_in_errors(tmp_path):
    env = tmp_path / "e.env"
    env.write_text("# c\nexport DEMO_PASSWORD='s3cret-value'\nEVAL_EMAIL=user@example.test\n", encoding="utf-8")
    assert cd.credentials(cd.read_env_file(env), None, "DEMO_PASSWORD") == ("user@example.test", "s3cret-value")
    assert cd.credentials(cd.read_env_file(env), "other@example.test", "DEMO_PASSWORD")[0] == "other@example.test"
    with pytest.raises(cd.CaseError) as e:
        cd.credentials({"EVAL_EMAIL": "a@b.test"}, None, "DEMO_PASSWORD")
    assert "DEMO_PASSWORD" in str(e.value)


def test_stale_refusal_and_5xx_are_handled_without_a_server():
    class R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

        def json(self):
            return {}

    class C:
        def __init__(self, r):
            self.r = r

        def post(self, *a, **k):
            return self.r

    stale = cd.send(C(R(409, '{"detail":"clarification_stale"}')), "c", cd.Reply("x"), None)
    assert "refused" in stale
    with pytest.raises(cd.StepFailed):
        cd.send(C(R(502)), "c", cd.Reply("x"), None)
    with pytest.raises(cd.StepFailed):
        cd.send(C(R(409, "busy")), "c", cd.Reply("x"), None)


def test_a_case_without_its_own_split_takes_the_split_named_by_the_file(tmp_path):
    f = tmp_path / "cases.yaml"
    f.write_text("set: clarify_v1_heldout\ncases:\n  - {id: H1, kind: clear, turns: [{ask: 'מה שטח המגרש?'}]}\n", encoding="utf-8")
    assert cd.load_cases(f)[0]["split"] == "heldout"


def test_a_file_that_names_no_split_still_requires_one_per_case(tmp_path):
    f = tmp_path / "cases.yaml"
    f.write_text("set: other\ncases:\n  - {id: X1, kind: clear, turns: [{ask: 'שאלה'}]}\n", encoding="utf-8")
    with pytest.raises(cd.CaseError):
        cd.load_cases(f)
