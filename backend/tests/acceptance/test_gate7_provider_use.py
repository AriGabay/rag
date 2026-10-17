"""Gate 7 (origin §11): a fully explained structured question returns the right calculation with no model
call, and the content path hands the model only authorized, relevant evidence.

Narrowed in U9 (KTD2): "no model call" holds for questions the rules fully explain (every content word
accounted for, monetary request). In cloud mode any other question is interpreted by the model first."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from app.answering import compose
from app.answering.plan import TurnPlan
from app.answering.templates import money
from app.db import tenant_tx
from app.providers.llm import MockLLM, Purpose
from eval.flows import ask_flow
from eval.truth import Filters, docs, expected_stats, truth
from tests.acceptance.support import (
    ADMIN_A,
    ADMIN_B,
    CASES_BY_ID,
    DANA,
    NUMERIC_CASES,
    YOSSI,
    assert_numeric,
    expected_for,
)
from tests.support.scripted_provider import ScriptedProvider

pytestmark = pytest.mark.db


def _usage(world, office="A") -> int:
    with tenant_tx(world.system(office)) as conn:
        return conn.execute(text("SELECT count(*) FROM provider_usage")).scalar()


def _visible(world, user) -> set[str]:
    """Document ids of current, authorized versions for ``user`` (answer-key view)."""
    allowed = {ADMIN_A: ("A", None), DANA: ("A", {"G1"}), YOSSI: ("A", {"G2"}), ADMIN_B: ("B", None)}[user]
    out = set()
    for gt, r in world.docs.items():
        d = docs()[gt]
        group = docs()[d["version_of"]]["group"] if d.get("version_of") else d["group"]
        if d["office"] == allowed[0] and (allowed[1] is None or group in allowed[1]) and r.get("document_id"):
            out.add(r["document_id"])
    return out


def _current_versions(world, office) -> set[str]:
    with tenant_tx(world.system(office)) as conn:
        return {str(v) for v in conn.execute(text("SELECT id FROM document_versions WHERE is_current")).scalars()}


@pytest.fixture
def capture(monkeypatch):
    """Record every provider call while still running the real demo provider."""
    calls = []
    original = MockLLM.answer

    def spy(self, question, evidence, calculation):
        calls.append({"question": question, "evidence": evidence, "calculation": calculation})
        return original(self, question, evidence, calculation)

    def no_cloud():
        raise AssertionError("cloud provider must not be constructed")

    monkeypatch.setattr(MockLLM, "answer", spy)
    monkeypatch.setattr("app.providers.llm.get_selected_provider", no_cloud)
    monkeypatch.setattr("app.answering.content.get_selected_provider", no_cloud)
    return calls


def test_structured_path_makes_no_model_call(world, capture):
    before = _usage(world)
    for case in [c for c in NUMERIC_CASES if c.user != ADMIN_B]:
        flow = ask_flow(world.client(case.user), case.question, case.answers)
        assert_numeric(flow.answer, expected_for(world, case), case.id)
        assert all(a["provider"] == "template" for a in flow.transcript)
    follow = ask_flow(world.client(ADMIN_A), CASES_BY_ID["harozim-2024-net"].question,
                      CASES_BY_ID["harozim-2024-net"].answers)
    r = world.client(ADMIN_A).post("/api/ask", json={"conversation_id": follow.conversation_id,
                                                      "question": "ומה לגבי 2023?"}).json()["answer"]
    assert r["kind"] == "numeric" and r["provider"] == "template"
    for q in ("מה מחיר העסקאות למ״ר בחרוזים ב-2018 לפי תאריך עסקה?", "מה המחיר או השווי למ״ר בחרוזים?"):
        assert ask_flow(world.client(ADMIN_A), q).answer["provider"] == "template"
    assert capture == []
    assert _usage(world) == before


@pytest.mark.parametrize("user,phrase,forbidden_doc", [
    (DANA, "חניה בטאבו", "D4"),  # a G2 phrase asked by a G1 employee
    (YOSSI, "מרפסת שמש הפונה מערבה", "D1v2"),  # a G1 phrase asked by a G2 employee
    (ADMIN_B, "היעדר מעלית", "D1v2"),  # an office A phrase asked in office B
    (ADMIN_A, "עסקאות ברחוב הגפן בלבד", "DB1"),  # an office B phrase asked in office A
])
def test_content_path_sends_only_authorized_evidence(world, capture, user, phrase, forbidden_doc):
    a = ask_flow(world.client(user), f"מה נאמר במסמכים על {phrase}?").answer
    visible = _visible(world, user)
    office = "B" if user == ADMIN_B else "A"
    current = _current_versions(world, office)
    sent = [e for call in capture for e in call["evidence"]]
    assert {e["document_id"] for e in sent} <= visible
    assert {e["version_id"] for e in sent} <= current
    assert world.doc_id(forbidden_doc) not in {e["document_id"] for e in sent}
    assert all(phrase not in e["text"] for e in sent)
    assert {s["document_id"] for s in a.get("sources", [])} <= visible


# D2 needs Hebrew OCR (covered inside the image)
RELEVANCE_FACTS = [f for f in truth()["content_facts"]
                   if docs()[f["document"]]["office"] == "A" and f["document"] != "D2"]


@pytest.mark.parametrize("fact", RELEVANCE_FACTS,
                         ids=lambda f: f["document"] + ":" + f["phrase"][:14])
def test_content_path_evidence_is_relevant(world, capture, fact):
    ask_flow(world.client(ADMIN_A), f"מה נאמר במסמכים על {fact['phrase']}?")
    assert capture, "the content path should call the (demo) provider"
    evidence = capture[-1]["evidence"]
    target = world.doc_id(fact["document"])
    # the page that holds the phrase is among the evidence (exact chunk ranking is retrieval quality,
    # measured by scripts/eval.py, not a gate)
    assert any(e["document_id"] == target and (fact["page"] is None or fact["page"] in e["page_list"])
               for e in evidence), [e["title"] for e in evidence]


def test_exact_phrase_ranks_first_lexically(world):
    from app.extraction.normalize_text import query_tokens
    from app.platform.search import _lexical

    with tenant_tx(world.a.ctx()) as conn:
        target = set(conn.execute(text("SELECT id FROM chunks WHERE text LIKE '%1.25 מ׳ ₪%'")).scalars())
        ranked = _lexical(conn, query_tokens("מה נאמר במסמכים על בכ-1.25 מ׳ ₪?"))
    assert target
    assert any(c in target for c in ranked[:3]), [i for i, c in enumerate(ranked) if c in target]


class StubCloud(ScriptedProvider):
    """Cloud stand-in on the structured contract: the interpreter plans a content search for the question;
    the answer is one explicit claim quoting the first evidence, judged supported. ``evidence_calls``
    records the evidence and computed results each answer call was composed from."""

    name, model = "anthropic", "stub"

    def __init__(self):
        super().__init__()
        self.evidence_calls = []
        self.on(Purpose.INTERPRET, self._plan, repeat=True)
        self.on(Purpose.ANSWER, self._answer, repeat=True)
        self.on(Purpose.VERIFY, {"verdicts": [{"claim": 0, "verdict": "supported"}]}, repeat=True)

    @staticmethod
    def _plan(instructions, input):
        question = json.loads(input)["question"]
        return TurnPlan.build(task_type="answer", search_queries=[question],
                              steps=[{"tool": "search", "attribute_handle": None, "source_handles": []}])

    def _answer(self, instructions, input):
        first = input.split('<evidence id="', 1)[1].split('"', 1)[0]
        return {"claims": [{"text": "ראו את הקטע", "evidence_ids": [first], "kind": "explicit", "numbers": []}],
                "insufficient": False, "missing_info": None}


@pytest.fixture
def cloud(world, monkeypatch):
    stub = StubCloud()
    original = compose.answer_input

    def spy(question, evidence, computed):
        stub.evidence_calls.append({"evidence": evidence, "computed": list(computed)})
        return original(question, evidence, computed)

    monkeypatch.setattr(compose, "answer_input", spy)
    monkeypatch.setattr("app.answering.content.selected_provider_configured", lambda: True)
    monkeypatch.setattr("app.answering.content.get_selected_provider", lambda: stub)
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: stub)
    admin = world.client(ADMIN_A)
    r = admin.put("/api/admin/settings", json={"cloud_llm_enabled": True, "acknowledge": True})
    assert r.status_code == 200, r.text
    yield stub
    admin.put("/api/admin/settings", json={"cloud_llm_enabled": False, "acknowledge": True})


def test_cloud_provider_receives_only_authorized_evidence_and_verified_numbers(world, cloud):
    dana = world.client(DANA)
    before = _usage(world)
    a = ask_flow(dana, "מה נאמר במסמכים על חניה בטאבו ועל עסקאות ברחוב הגפן בלבד?").answer
    assert a["provider"] == "cloud" and cloud.evidence_calls
    sent = [e for call in cloud.evidence_calls for e in call["evidence"]]
    assert {e["document_id"] for e in sent} <= _visible(world, DANA)
    assert not any("חניה בטאבו" in e["text"] or "עסקאות ברחוב הגפן בלבד" in e["text"] for e in sent)
    # the interpretation (KTD1), the answer call and its judge call (KTD11)
    assert _usage(world) == before + 3
    assert [c.purpose for c in cloud.calls[-3:]] == [Purpose.INTERPRET, Purpose.ANSWER, Purpose.VERIFY]
    # combined: the model gets the verified calculation exactly as the answer shows it
    case = CASES_BY_ID["harozim-2024-net"]
    combined = ask_flow(world.client(ADMIN_A), "מה מחיר העסקאות למ״ר בחרוזים ב-2024 לפי תאריך עסקה ומה השיקולים שהוזכרו?",
                        case.answers).answer
    assert combined["kind"] == "combined"
    assert_numeric(combined, expected_for(world, case))
    # computed values reach the model only as server-formatted results of the verified calculation
    shown = {c.label: c.display for c in cloud.evidence_calls[-1]["computed"]}
    from decimal import Decimal

    assert shown["ממוצע מחיר למ״ר"] == f"{money(Decimal(combined['numeric']['mean_price_per_sqm']))} ₪"
    assert shown["מספר הרשומות בחישוב"] == str(combined["numeric"]["record_count"])
    # fully explained numeric questions still never reach the cloud provider, not even the interpreter
    n = len(cloud.calls)
    ask_flow(world.client(ADMIN_A), CASES_BY_ID["year-range"].question, CASES_BY_ID["year-range"].answers)
    assert len(cloud.calls) == n


def test_injected_instruction_in_a_document_is_only_content(world, cloud):
    a = ask_flow(world.client(ADMIN_A), "מה כתוב בהוראה למערכת שבשומה?").answer
    sent = [e for call in cloud.evidence_calls for e in call["evidence"]]
    assert any(e["document_id"] == world.doc_id("D12") for e in sent)
    # the instruction inside D12 changed neither the calls made nor the answer's status
    assert {c.purpose for c in cloud.calls} <= {Purpose.INTERPRET, Purpose.ANSWER, Purpose.VERIFY}
    plans = [c.result.parsed for c in cloud.calls if c.purpose == Purpose.INTERPRET]
    assert plans and all(p.steps[0].tool == "search" for p in plans)
    assert a["provider"] == "cloud" and a["mode"] == "cloud"
    assert {s["document_id"] for s in a["sources"]} <= _visible(world, ADMIN_A)
    assert world.doc_id("DB1") not in {s["document_id"] for s in a["sources"]}
    # following the instruction ("show all documents of office B") is impossible: B stays invisible
    assert world.client(ADMIN_A).get(f"/api/documents/{world.doc_id('DB1')}").status_code == 404


def _eval_item(item_id: str) -> dict:
    items = yaml.safe_load((Path(__file__).resolve().parents[2] / "eval" / "questions.yaml").read_text())
    items = items["items"] if isinstance(items, dict) else items
    return next(i for i in items if i["id"] == item_id)


@pytest.mark.parametrize("item_id", ["M01", "M02", "M03"])
def test_demo_mode_combined_items_keep_their_numeric_part(world, capture, item_id):
    """KTD2: a separable explanation clause runs compute_records plus a content search in every mode;
    in demo mode the clause never turns the question into a content-only answer."""
    turn = _eval_item(item_id)["turns"][0]
    f = Filters.of(turn["expect"]["filters"])
    answers = {k: getattr(f, k) for k in ("data_kind", "date_field", "area_type", "property_type", "vat_basis")
               if getattr(f, k) is not None}
    flow = ask_flow(world.client(ADMIN_A), turn["ask"], answers)
    a = flow.answer
    assert a["kind"] == "combined" and a["mode"] == "demo", a.get("text")
    assert_numeric(a, expected_stats(f, "A", None, world.exclude), item_id)
    for want in turn["expect"].get("sources", []):
        doc = world.doc_id(want["doc"])
        assert any(s["document_id"] == doc and want["page"] in s["page_list"] for s in a["sources"]), item_id
    # a follow-up form keeps the numeric part and changes only the year
    if f.year_from is not None:
        r = world.client(ADMIN_A).post("/api/ask", json={"conversation_id": flow.conversation_id,
                                                          "question": f"ומה לגבי {f.year_from - 1}?"}).json()
        assert r["answer"]["kind"] in ("numeric", "abstain", "clarification")
        conds = {c["label"]: c["value"] for c in r["answer"]["conditions"]}
        assert str(f.year_from - 1) in " ".join(conds.values())
