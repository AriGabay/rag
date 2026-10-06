"""Gate 7 (origin §11): the structured path returns the right calculation with no model call, and
the content path hands the model only authorized, relevant evidence."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from app.providers.llm import LLMResult, MockLLM
from eval.flows import ask_flow
from eval.truth import docs, truth
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
    monkeypatch.setattr("app.providers.llm.get_cloud_provider", no_cloud)
    monkeypatch.setattr("app.answering.content.get_cloud_provider", no_cloud)
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


class StubCloud:
    name, model, demo = "anthropic", "stub", False

    def __init__(self):
        self.calls = []

    def answer(self, question, evidence, calculation):
        self.calls.append({"evidence": evidence, "calculation": calculation})
        first = evidence[0]["evidence_id"]
        return LLMResult(f"ראו את הקטע [{first}].", [first], False, 10, 5, 3)

    def parse_conditions(self, question, schema):
        return None


@pytest.fixture
def cloud(world, monkeypatch):
    stub = StubCloud()
    monkeypatch.setattr("app.answering.content.cloud_configured", lambda: True)
    monkeypatch.setattr("app.answering.content.get_cloud_provider", lambda: stub)
    monkeypatch.setattr("app.providers.llm.get_cloud_provider", lambda: stub)
    admin = world.client(ADMIN_A)
    r = admin.put("/api/admin/settings", json={"cloud_llm_enabled": True, "acknowledge": True})
    assert r.status_code == 200, r.text
    yield stub
    admin.put("/api/admin/settings", json={"cloud_llm_enabled": False, "acknowledge": True})


def test_cloud_provider_receives_only_authorized_evidence_and_verified_numbers(world, cloud):
    dana = world.client(DANA)
    before = _usage(world)
    a = ask_flow(dana, "מה נאמר במסמכים על חניה בטאבו ועל עסקאות ברחוב הגפן בלבד?").answer
    assert a["provider"] == "cloud" and cloud.calls
    sent = [e for call in cloud.calls for e in call["evidence"]]
    assert {e["document_id"] for e in sent} <= _visible(world, DANA)
    assert not any("חניה בטאבו" in e["text"] or "עסקאות ברחוב הגפן בלבד" in e["text"] for e in sent)
    assert _usage(world) == before + 1
    # combined: the model gets the verified calculation exactly as the answer shows it
    case = CASES_BY_ID["harozim-2024-net"]
    combined = ask_flow(world.client(ADMIN_A), "מה מחיר העסקאות למ״ר בחרוזים ב-2024 לפי תאריך עסקה ומה השיקולים שהוזכרו?",
                        case.answers).answer
    assert combined["kind"] == "combined"
    assert_numeric(combined, expected_for(world, case))
    assert cloud.calls[-1]["calculation"] == combined["numeric"]
    # numeric questions still never reach the cloud provider
    n = len(cloud.calls)
    ask_flow(world.client(ADMIN_A), CASES_BY_ID["year-range"].question, CASES_BY_ID["year-range"].answers)
    assert len(cloud.calls) == n


def test_injected_instruction_in_a_document_is_only_content(world, cloud):
    a = ask_flow(world.client(ADMIN_A), "מה כתוב בהוראה למערכת שבשומה?").answer
    sent = [e for call in cloud.calls for e in call["evidence"]]
    assert any(e["document_id"] == world.doc_id("D12") for e in sent)
    assert {s["document_id"] for s in a["sources"]} <= _visible(world, ADMIN_A)
    assert world.doc_id("DB1") not in {s["document_id"] for s in a["sources"]}
    # following the instruction ("show all documents of office B") is impossible: B stays invisible
    assert world.client(ADMIN_A).get(f"/api/documents/{world.doc_id('DB1')}").status_code == 404
