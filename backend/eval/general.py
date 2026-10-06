"""Run and score the held-out general question set (U12, KTD16; ``eval/questions_general.yaml``).

Shared by ``scripts/eval.py --set general`` (live stack, real or demo provider) and the gate 8 acceptance
test (scripted provider). Scoring follows the header of the question file; wording is never scored:

* the plan facets (task type, relation, main tool) come from the stored turn (``questions.plan`` and
  ``questions.steps``), read through a ``Hooks.plan_of`` callback;
* a source is "cited" when a claim or an ``[E#]`` marker in the text refers to it; an answer with neither
  (a template list or a computation) cites every source it returns;
* a fact value is "stated" when its document is cited at the fact's page and the value appears in the
  answer: a number (or its normalized form, or a small Hebrew number word), the Hebrew value text, or, for
  descriptive answer-key values such as ``"2022: kitchen"``, every number they contain;
* a computed figure is the reviewed figure when there is one, else the separately labeled preliminary
  figure (model-extracted values are preliminary until a person reviews them, KTD9).

Nothing here imports application code; expected values come from ``tests/fixtures/ground_truth.yaml``.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import cache
from pathlib import Path

import yaml

from eval.flows import Flow, ask_flow, post_ask
from eval.truth import USERS, Filters, docs, expected_stats, truth

QUESTIONS = Path(__file__).resolve().parent / "questions_general.yaml"
ANSWERED = ("numeric", "combined", "content")
PRICE_KEYS = ("data_kind", "date_field", "area_type", "property_type", "vat_basis")
NUMERIC_FIELDS = ("record_count", "mean_price_per_sqm", "weighted_price_per_sqm", "median_price_per_sqm",
                  "min_price_per_sqm", "max_price_per_sqm")
UNITS = {"m2": "sqm", "count": "unit", "m": "m", "year": "year", "percent": "percent"}
HEBREW_NUMBERS = {1: ("אחת", "אחד"), 2: ("שתי", "שני", "שתיים", "שניים"), 3: ("שלוש", "שלושה"),
                  4: ("ארבע", "ארבעה"), 5: ("חמש", "חמישה")}
NOT_RUN_CLOUD_OFF = ("not run: needs cloud use off for the office; the live office A is in cloud mode and the "
                     "evaluation never changes office settings")
_Q2 = Decimal("0.01")


# --- answer key ---------------------------------------------------------------------------------------

@cache
def general() -> dict:
    return truth()["general_facts"]


@cache
def facts() -> dict[str, dict]:
    return {f["id"]: f for f in general()["facts"]}


@cache
def general_docs() -> dict[str, dict]:
    return {d["id"]: d for d in general()["documents"]}


@cache
def filename_to_doc() -> dict[str, str]:
    """Fixture file name -> answer-key document id (original D*/DB* and held-out H* documents)."""
    out = {d["filename"]: d["id"] for d in truth()["documents"]}
    out.update({d["filename"]: d["id"] for d in general()["documents"]})
    return out


def doc_scope(doc_id: str) -> tuple[str, str]:
    """(office, group code) of an answer-key document; a new version inherits its document's group."""
    d = general_docs().get(doc_id) or docs()[doc_id]
    base = d.get("version_of")
    if base:
        d = general_docs().get(base) or docs()[base]
    return d["office"], d["group"]


def load_items(path: Path | str = QUESTIONS) -> list[dict]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))["items"]


def ae_tags(item: dict) -> list[str]:
    """Acceptance examples an item reproduces, from its notes (``AE1`` ... ``AE8``)."""
    notes = " ".join(str(x) for x in [item.get("note", "")] + [t["expect"].get("note", "") for t in item["turns"]])
    return sorted(set(re.findall(r"\bAE\d\b", notes)))


# --- hooks into the system under test ----------------------------------------------------------------

@dataclass
class Hooks:
    """How the runner reaches the system: an HTTP client per user, the answer-key document of a source,
    the stored plan of a question, the document ids a user must never see, and the review-queue state."""

    client: Callable[[str], object]
    doc_of: Callable[[dict], str | None]  # source -> answer-key document id (H1, H4v2, D1v2, ...)
    plan_of: Callable[[str], dict | None]  # question id -> {"plan": ..., "steps": [...], "parse_route": ...}
    forbidden: Callable[[str, str | None], set[str]]  # (user, rule) -> document ids outside the scope
    unverified: set[str] = field(default_factory=set)  # answer-key record ids still in the review queue
    cloud_off: bool = False  # the office runs without cloud use (items with cloud: "off" run only then)


# --- running ------------------------------------------------------------------------------------------

@dataclass
class TurnRun:
    flow: Flow
    plan: dict | None
    steps: list[dict]
    route: str | None
    before: dict  # conversation context before the turn
    after: dict  # conversation context and pending clarification after the turn
    resubmit: dict | None = None  # the answer to the same turn posted again (resubmit_same_turn)
    resubmit_after: dict | None = None

    def raw(self) -> dict:
        """Everything scoring reads, so a run can be scored again offline (``from_raw``)."""
        f = self.flow
        return {"answer": f.answer, "transcript": f.transcript, "conversation_id": f.conversation_id,
                "clarifications": f.clarifications, "latencies_ms": f.latencies_ms, "question_ids": f.question_ids,
                "error": f.error, "plan": self.plan, "steps": self.steps, "route": self.route, "before": self.before,
                "after": self.after, "resubmit": self.resubmit, "resubmit_after": self.resubmit_after}

    @classmethod
    def from_raw(cls, raw: dict) -> TurnRun:
        flow = Flow(answer=raw["answer"], conversation_id=raw["conversation_id"], transcript=raw["transcript"],
                    clarifications=raw["clarifications"], latencies_ms=raw["latencies_ms"],
                    question_ids=raw["question_ids"], error=raw["error"])
        return cls(flow, raw["plan"], raw["steps"], raw["route"], raw["before"], raw["after"], raw["resubmit"],
                   raw["resubmit_after"])


def _conversation(client, cid: str) -> dict:
    if not cid:
        return {}
    r = client.get(f"/api/conversations/{cid}")
    return r.json() if r.status_code == 200 else {}


def run_item(hooks: Hooks, item: dict) -> list[TurnRun]:
    """One conversation: every turn in order, answering button clarifications from ``clarify`` and typed
    replies from ``replies``; ``allow_further_clarification`` keys are answered with the first option."""
    user = item.get("user", "admin-a@demo.test")
    client = hooks.client(user)
    conversation: str | None = None
    before: dict = {}
    runs: list[TurnRun] = []
    for turn in item["turns"]:
        expect = turn["expect"]
        turn_id = str(uuid.uuid4())
        flow = ask_flow(client, turn["ask"], dict(turn.get("clarify") or {}), conversation,
                        replies=dict(turn.get("replies") or {}), turn_id=turn_id)
        allowed = set(expect.get("allow_further_clarification") or [])
        while flow.kind == "clarification" and flow.answer["clarification"]["key"] in allowed:
            key = flow.answer["clarification"]["key"]
            options = flow.answer["clarification"]["options"]
            if not options or len(flow.transcript) > 8:
                break
            data, ms = post_ask(client, {"conversation_id": flow.conversation_id,
                                         "clarification": {"key": key, "value": options[0]["value"]}})
            flow.answer = data.get("answer", data)
            flow.transcript.append(flow.answer)
            flow.latencies_ms.append(ms)
            flow.question_ids.append(data.get("question_id") or "")
            if flow.kind == "clarification":  # ask_flow records the keys it saw; record the ones after it
                flow.clarifications.append(flow.answer["clarification"]["key"])
        conversation = flow.conversation_id or conversation
        after = _conversation(client, conversation or "")
        stored = hooks.plan_of(flow.question_ids[0]) if flow.question_ids and flow.question_ids[0] else None
        run = TurnRun(flow, (stored or {}).get("plan"), list((stored or {}).get("steps") or []),
                      (stored or {}).get("parse_route"), before, after)
        if expect.get("resubmit_same_turn"):
            data, _ms = post_ask(client, {"question": turn["ask"], "conversation_id": conversation,
                                          "turn_id": turn_id})
            run.resubmit = data.get("answer", data)
            run.resubmit_after = _conversation(client, conversation or "")
        runs.append(run)
        before = after
    return runs


# --- scoring helpers ----------------------------------------------------------------------------------

def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def numbers_in(text: str) -> set[Decimal]:
    out = set()
    for m in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", text or ""):
        v = _dec(m)
        if v is not None:
            out.add(v.normalize())
    return out


def _norm_he(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[׳'’`]", "׳", re.sub(r"[״\"”“]", "״", s or ""))).strip()


def answer_text(answer: dict) -> str:
    parts = [answer.get("text") or ""] + [c.get("text") or "" for c in answer.get("claims") or []]
    return "\n".join(parts)


def cited_sources(answer: dict) -> list[dict]:
    """Sources a claim or an [E#] marker refers to; all sources when the answer refers to none by id."""
    ids = {e for c in answer.get("claims") or [] for e in c.get("evidence_ids") or []}
    ids |= set(re.findall(r"\[(E\d+)\]", answer.get("text") or ""))
    sources = answer.get("sources") or []
    if not ids:
        return list(sources)
    return [s for s in sources if s.get("evidence_id") in ids]


def _page_ok(want: int | None, source: dict) -> bool:
    pages = source.get("page_list") or []
    return want is None or not pages or want in pages


def _cites(sources: list[dict], doc_of, doc: str, page: int | None) -> bool:
    return any(doc_of(s) == doc and _page_ok(page, s) for s in sources)


def fact_value_stated(fact: dict, text: str) -> bool:
    value = fact["value"]
    if isinstance(value, bool) or value in (None, "none"):
        return True  # a stated absence: the cited document is the evidence
    found = numbers_in(text)
    if isinstance(value, int | float):
        candidates = {Decimal(str(value)).normalize()}
        if isinstance(value, int) and value in HEBREW_NUMBERS and any(w in text for w in HEBREW_NUMBERS[value]):
            return True
        return bool(candidates & found)
    s = str(value)
    if re.fullmatch(r"[\d.,]+", s):
        candidates = {_dec(s).normalize()}
        if (fact.get("normalized") or {}).get("value"):
            candidates.add(_dec(fact["normalized"]["value"]).normalize())
        return bool(candidates & found)
    if re.fullmatch(r"\d+x\d+", s):  # dimensions: the normalized area, or both dimensions
        norm = _dec((fact.get("normalized") or {}).get("value"))
        sides = {Decimal(x) for x in s.split("x")}
        return (norm is not None and norm.normalize() in found) or sides <= found
    if re.search(r"[א-ת]", s):
        return _norm_he(s).rstrip("׳") in _norm_he(text)
    digits = {Decimal(x).normalize() for x in re.findall(r"\d+(?:\.\d+)?", s)}
    return digits <= found


def without_addresses(text: str, question: str) -> str:
    """``text`` without the "<street> <number>" phrases the question names, so a house number is never read
    as the value ("האירוסים 12" when the value is 12)."""
    for word, num in re.findall(r"([א-ת][א-ת״׳\"']+)\s+(\d+)", question or ""):
        text = re.sub(rf"[א-ת]?{re.escape(word)}\s+{num}(?!\d)", " ", text)
    return text


def _claim_states(answer: dict, doc_of, fact: dict, question: str = "") -> bool:
    """Some claim cites the fact's document (at its page) and states the value (header: "stated as a claim
    cited to its doc"); quoted passages of a fallback answer are not claims."""
    by_id = {s.get("evidence_id"): s for s in answer.get("sources") or []}
    for c in answer.get("claims") or []:
        cited = [by_id[e] for e in c.get("evidence_ids") or [] if e in by_id]
        claim = without_addresses(c.get("text") or "", question)
        if _cites(cited, doc_of, fact["document"], fact["page"]) and fact_value_stated(fact, claim):
            return True
    return False


@dataclass
class Figure:
    value: object = None  # Decimal, or a list for "values"
    n: int | None = None
    operation: str | None = None
    unit: str | None = None
    tier: str | None = None  # verified | preliminary | records


def figure_of(answer: dict) -> Figure:
    num = answer.get("numeric") or {}
    if not num:
        return Figure()
    if "operation" in num:
        if num.get("record_count"):
            vals = num.get("values")
            return Figure(vals if num["operation"] == "values" else _dec(num.get("value")), num["record_count"],
                          num["operation"], num.get("unit"), "verified")
        pre = answer.get("preliminary") or {}
        if pre.get("record_count"):
            vals = pre.get("values")
            return Figure(vals if num["operation"] == "values" else _dec(pre.get("value")), pre["record_count"],
                          num["operation"], num.get("unit"), "preliminary")
        return Figure(None, 0, num["operation"], num.get("unit"))
    return Figure(_dec(num.get("mean_price_per_sqm")), num.get("record_count"), "mean", "ILS/sqm", "records")


def _context_value(conv: dict, key: str):
    ctx = conv.get("context") or {}
    if key in ("year_from", "year_to", "years"):
        years = ctx.get("years") or {}
        return years.get({"year_to": "to"}.get(key, "from")) if years else None
    if key in ctx and key != "chips":
        return ctx.get(key)
    chips = {c["key"]: c["value"] for c in ctx.get("chips") or []}
    return chips.get(key)


# --- scoring ------------------------------------------------------------------------------------------

@dataclass
class Score:
    facets: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def check(self, facet: str, ok: bool, reason: str) -> None:
        self.facets[facet] = self.facets.get(facet, True) and bool(ok)
        if not ok:
            self.reasons.append(f"{facet}: {reason}")


def _sources_brief(sources: list[dict], doc_of) -> str:
    return ", ".join(f"{doc_of(s) or (s.get('title') or '?')[:12]} p{s.get('page_list')}" for s in sources) or "none"


def score_turn(hooks: Hooks, item: dict, index: int, run: TurnRun) -> dict:
    turn = item["turns"][index]
    expect = turn["expect"]
    user = item.get("user", "admin-a@demo.test")
    office, groups = USERS[user]
    a = run.flow.answer
    kind = a.get("kind", "error")
    plan = (run.plan or {}).get("turn_plan") or {}
    sc = Score()
    doc_of = hooks.doc_of
    sources = a.get("sources") or []
    cited = cited_sources(a)
    text = answer_text(a)
    if run.flow.error:
        sc.check("flow", False, run.flow.error)
    if kind == "error":
        sc.check("flow", False, f"HTTP {a.get('status')}: {str(a.get('detail'))[:120]}")

    # interpretation facets
    if "task_type" in expect:
        got = plan.get("task_type")
        sc.check("task_type", got == expect["task_type"], f"{got} (expected {expect['task_type']})")
    if "relation" in expect:
        got = plan.get("turn_relation")
        sc.check("relation", got == expect["relation"], f"{got} (expected {expect['relation']})")
    executed = [s.get("tool") for s in run.steps]
    planned = [s.get("tool") for s in plan.get("steps") or []]
    if "tool" in expect:
        sc.check("tool", expect["tool"] in executed or (not executed and expect["tool"] in planned),
                 f"executed {executed or '-'} planned {planned or '-'} (expected {expect['tool']})")
    for key in expect.get("cleared") or []:  # the earlier value no longer applies: removed, or replaced
        was, now = _context_value(run.before, key), _context_value(run.after, key)
        sc.check("state", now in (None, "none") or (was not in (None, "none") and now != was),
                 f"{key} not cleared ({was!r} -> {now!r})")
    for key in expect.get("kept") or []:
        was, now = _context_value(run.before, key), _context_value(run.after, key)
        sc.check("state", now not in (None, "none") and (was in (None, "none") or was == now),
                 f"{key} not kept ({was!r} -> {now!r})")
    if expect.get("pending_kept"):
        pend = run.after.get("pending_clarification") or {}
        prev = run.before.get("pending_clarification") or {}
        sc.check("state", bool(pend) and pend.get("key") == prev.get("key"),
                 f"pending clarification {prev.get('key')!r} not kept ({pend.get('key')!r})")
    for key, value in (expect.get("resolved") or {}).items():
        got = _context_value(run.after, key)
        sc.check("state", got == value, f"{key} resolved to {got!r} (expected {value!r})")
        if key in run.flow.clarifications[1:]:
            sc.check("state", False, f"{key} asked again after the free-text reply")
    if expect.get("no_price_clarification"):
        asked = [k for k in run.flow.clarifications if k in PRICE_KEYS]
        sc.check("no_price_clarification", not asked, f"price-path clarification asked: {asked}")

    out = expect["outcome"]
    fig = figure_of(a)
    if out == "answer":  # a reasoned abstention ("the documents do not state it") is not an answer
        sc.check("outcome", kind in ANSWERED and not a.get("abstention_kind"),
                 f"kind {kind} (expected an answer; abstention {a.get('abstention_kind')})")
    elif out == "computation":
        sc.check("outcome", kind in ("numeric", "combined") and fig.n,
                 f"kind {kind}, figure n={fig.n} (expected a computation; abstention {a.get('abstention_kind')})")
    elif out == "comparison":
        cmp = a.get("compare") or {}
        if expect.get("incomplete"):
            sc.check("outcome", bool(cmp.get("incomplete")),
                     f"kind {kind}, compare {'incomplete' if cmp.get('incomplete') else cmp or 'absent'}"
                     " (expected an incomplete comparison)")
        else:
            sc.check("outcome", kind in ANSWERED and (bool(cmp) or expect.get("tool") != "compare"),
                     f"kind {kind}, compare block {'present' if cmp else 'absent'} (expected a comparison)")
    elif out == "abstain":  # header: no figure in the answer and a matching abstention kind (any answer kind)
        sc.check("outcome", not fig.n and kind not in ("clarification", "error") and bool(a.get("abstention_kind")),
                 f"kind {kind}, figure n={fig.n}, abstention {a.get('abstention_kind')} (expected an abstention)")
        want = expect.get("abstention_kind_any_of") or ([expect["abstention_kind"]] if "abstention_kind" in expect
                                                        else [])
        if want:
            sc.check("abstention_kind", a.get("abstention_kind") in want,
                     f"{a.get('abstention_kind')} (expected {' | '.join(want)})")
        if expect.get("limited_mode"):
            sc.check("limited_mode", a.get("mode") == "limited", f"mode {a.get('mode')} (expected limited)")
    elif out == "clarification":
        got = (a.get("clarification") or {}).get("key")
        sc.check("outcome", kind == "clarification" and got == expect.get("clarify_key"),
                 f"kind {kind} key {got} (expected a clarification about {expect.get('clarify_key')})")

    # sources: recall for every outcome; precision for content answers
    src = expect.get("sources") or {}
    all_of = src.get("all_of") or []
    also = src.get("also_valid") or []
    if all_of and out != "abstain":
        missing = [f"{s['doc']} p{s['page']}" for s in all_of if not _cites(cited, doc_of, s["doc"], s["page"])]
        sc.check("sources", not missing, f"not cited: {', '.join(missing)} | cited: {_sources_brief(cited, doc_of)}")
    if out == "answer" and kind in ANSWERED and (all_of or also):
        allowed = all_of + also
        extra = [s for s in cited if not any(doc_of(s) == w["doc"] and _page_ok(w["page"], s) for w in allowed)]
        sc.check("source_precision", not extra, f"cited outside the answer key: {_sources_brief(extra, doc_of)}")

    # values
    if out in ("answer", "comparison") and kind in ANSWERED:
        for v in expect.get("values") or []:
            f = facts()[v["fact"]]
            if not _cites(cited, doc_of, f["document"], f["page"]):
                sc.check("values", False, f"{v['fact']} ({f['document']} p{f['page']}) not cited")
            elif "value" in v and not _claim_states(a, doc_of, f | {"value": v["value"]}, turn["ask"]):
                sc.check("values", False, f"{v['fact']} value {v['value']!r} not stated in a claim citing "
                         f"{f['document']}")
    if out == "computation" and kind in ("numeric", "combined"):
        _score_computation(sc, expect, a, fig, sources, doc_of, office, groups, hooks)
    if expect.get("conflict") and kind in ANSWERED:
        conflict = next(c for c in general()["conflicts"])
        for st in conflict["statements"]:
            if not _cites(sources, doc_of, st["document"], st["page"]):
                sc.check("conflict", False, f"{st['document']} ({st['value']}) not cited")
            elif Decimal(st["value"]) not in numbers_in(text):
                sc.check("conflict", False, f"{st['document']} value {st['value']} not shown")
    if out == "comparison" and kind in ANSWERED + ("abstain",):
        _score_comparison(sc, expect, a, cited, doc_of)
    if expect.get("forbid"):
        bad = {s["document_id"] for s in sources} & hooks.forbidden(user, expect["forbid"])
        sc.check("isolation", not bad, f"LEAK: {len(bad)} source(s) outside the user's scope")
    if out in ("abstain", "clarification") and fig.n:
        sc.check("outcome", False, "a figure was shown")
    if expect.get("filters"):
        # the conditions the conversation confirmed: the stated filters plus the clarifications answered so far
        confirmed = {k: v for t in item["turns"][:index + 1] for k, v in (t.get("clarify") or {}).items()}
        f = Filters.of(confirmed | expect["filters"])
        got = _context_value(run.after, "year_from")
        if f.year_from is not None:
            sc.check("state", got == f.year_from, f"year {got} (expected {f.year_from})")
        if item.get("truth") == "records" and out == "computation" and kind in ("numeric", "combined"):
            exp = expected_stats(f, office, groups, hooks.unverified).as_answer()
            got_num = {k: (a.get("numeric") or {}).get(k) for k in NUMERIC_FIELDS}
            diff = {k: (got_num[k], exp[k]) for k in NUMERIC_FIELDS if got_num[k] != exp[k]}
            sc.check("result", not diff, f"numbers differ (got, expected): {diff}")
    if run.resubmit is not None:
        same = run.resubmit.get("kind") == kind and _context_value(run.resubmit_after or {}, "year_from") == \
            _context_value(run.after, "year_from")
        sc.check("idempotent", same, f"resubmitted turn gave {run.resubmit.get('kind')} / year "
                 f"{_context_value(run.resubmit_after or {}, 'year_from')}")

    cov = (a.get("coverage") or {}).get("facts") or {}
    return {
        "id": item["id"], "turn": index + 1, "category": item["category"], "held_out": bool(item.get("held_out")),
        "ae": ae_tags(item), "user": user, "question": turn["ask"], "expected": out, "got": kind,
        "task_type": plan.get("task_type"), "expected_task_type": expect.get("task_type"),
        "relation": plan.get("turn_relation"), "tools": executed, "planned_tools": planned, "route": run.route,
        "plan_attribute": (plan.get("attribute") or {}).get("description"), "plan_metric": plan.get("metric"),
        "search_queries": plan.get("search_queries"), "provider": a.get("provider"), "mode": a.get("mode"),
        "cached": bool(a.get("cached")), "partial": bool(a.get("partial")),
        "pending_extraction": a.get("pending_extraction"), "abstention_kind": a.get("abstention_kind"),
        "clarifications": run.flow.clarifications, "figure": str(fig.value) if fig.value is not None else None,
        "figure_n": fig.n, "figure_tier": fig.tier, "operation": fig.operation, "coverage": cov,
        "sources": [f"{doc_of(s) or '?'} p{s.get('page_list')}" for s in sources],
        "cited": [f"{doc_of(s) or '?'} p{s.get('page_list')}" for s in cited],
        "dropped_claims": a.get("dropped_claims"), "limitations": a.get("limitations") or [],
        "latencies_ms": run.flow.latencies_ms, "total_ms": sum(run.flow.latencies_ms),
        "question_ids": run.flow.question_ids, "conversation_id": run.flow.conversation_id,
        "facets": sc.facets, "reasons": sc.reasons, "ok": not sc.reasons, "text": (a.get("text") or "")[:600],
        "raw": run.raw(),
    }


def _score_computation(sc: Score, expect: dict, a: dict, fig: Figure, sources: list[dict], doc_of, office: str,
                       groups, hooks: Hooks) -> None:
    res = expect.get("result")
    if not res:
        return
    metric = res.get("metric")
    if fig.operation is not None and metric is not None:
        sc.check("result", fig.operation == metric or (metric == "count" and fig.operation == "count"),
                 f"operation {fig.operation} (expected {metric})")
    if res.get("n") is not None:
        sc.check("result", fig.n == res["n"], f"n={fig.n} (expected {res['n']})")
    if metric == "values":
        pass  # the listed values are checked through their citations below
    elif res.get("value") is not None:
        want = Decimal(str(res["value"]))
        got = fig.value if isinstance(fig.value, Decimal) else None
        ok = got is not None and got.quantize(_Q2, ROUND_HALF_UP) == want.quantize(_Q2, ROUND_HALF_UP)
        sc.check("result", ok, f"figure {fig.value} ({fig.tier}) (expected {res['value']})")
    if res.get("unit") and fig.unit and res.get("unit") in UNITS and metric != "count":  # a count has no unit
        sc.check("result", fig.unit == UNITS[res["unit"]], f"unit {fig.unit} (expected {UNITS[res['unit']]})")
    value_sources = [s for s in sources if s.get("value") is not None or s.get("tier")]
    for v in expect.get("values") or []:
        f = facts()[v["fact"]]
        if not _cites(value_sources or sources, doc_of, f["document"], v.get("page", f["page"])):
            sc.check("values", False, f"{v['fact']} ({f['document']} p{v.get('page', f['page'])}) not cited")
    cov = expect.get("coverage") or {}
    got = (a.get("coverage") or {}).get("facts") or {}
    if "values_found" in cov and got:
        sc.check("coverage", got.get("found") == cov["values_found"],
                 f"found {got.get('found')} (expected {cov['values_found']}); coverage {got}")
    elif "values_found" in cov:
        sc.check("coverage", False, "no extraction coverage in the answer")
    never = list(cov.get("not_stated_includes") or []) + list(cov.get("stated_absent") or []) + \
        list(cov.get("mentioned_without_value") or []) + ["H4"]
    as_value = sorted({doc_of(s) for s in value_sources} & set(never))
    sc.check("coverage", not as_value, f"counted as a value: {as_value}")
    if cov.get("not_stated_includes") and got:
        unresolved = sum(got.get(k, 0) or 0 for k in ("not_stated", "not_yet_extracted", "pending", "failed",
                                                      "partial_scan", "awaiting_review"))
        sc.check("coverage", unresolved >= len(cov["not_stated_includes"]),
                 f"{unresolved} documents reported without a value (expected >= {len(cov['not_stated_includes'])})")


def _score_comparison(sc: Score, expect: dict, a: dict, cited: list[dict], doc_of) -> None:
    cmp = a.get("compare") or {}
    sources = a.get("sources") or []
    sides = expect.get("sides") or []
    if expect.get("incomplete"):
        missing = expect.get("missing_side")
        present = [s for s in sides if s["doc"] != missing]
        for s in present:
            if not _cites(sources, doc_of, s["doc"], s["page"]):
                sc.check("sides", False, f"side {s['doc']} not shown")
        sc.check("sides", bool(cmp.get("missing_sides")), "the side without evidence is not named")
        return
    for s in sides:
        if not _cites(cited, doc_of, s["doc"], s["page"]):
            sc.check("sides", False, f"side {s['doc']} p{s['page']} not cited")
    if expect.get("labeled_by_version"):
        versions = {x.get("version_id") for x in cmp.get("sides") or [] if x.get("version_id")}
        labels = {x.get("label") for x in cmp.get("sides") or []}
        sc.check("sides", len(versions) >= 2 and len(labels) >= 2,
                 f"sides not labeled by version ({[x.get('label') for x in cmp.get('sides') or []]})")


def rescore(hooks: Hooks, items: list[dict], results: list[dict]) -> list[dict]:
    """Score stored runs again (``raw``) with the current scorer, without asking anything."""
    by_id = {i["id"]: i for i in items}
    out = []
    for r in results:
        if r.get("not_run") or "raw" not in r:
            out.append(r)
            continue
        new = score_turn(hooks, by_id[r["id"]], r["turn"] - 1, TurnRun.from_raw(r["raw"]))
        new["item_ms"] = r.get("item_ms")
        out.append(new)
    return out


def item_ok(results: list[dict]) -> bool:
    return bool(results) and all(r["ok"] for r in results)


def run_and_score(hooks: Hooks, items: list[dict], on_turn: Callable[[dict], None] | None = None) -> list[dict]:
    """Every item in order; an item that needs cloud use off is recorded as not run unless ``cloud_off``."""
    out: list[dict] = []
    for item in items:
        if item.get("cloud", "on") == "off" and not hooks.cloud_off:
            r = {"id": item["id"], "turn": 1, "category": item["category"], "held_out": bool(item.get("held_out")),
                 "ae": ae_tags(item), "user": item.get("user", "admin-a@demo.test"),
                 "question": item["turns"][0]["ask"], "expected": item["turns"][0]["expect"]["outcome"],
                 "got": None, "not_run": NOT_RUN_CLOUD_OFF, "ok": None, "reasons": [], "facets": {}}
            out.append(r)
            if on_turn:
                on_turn(r)
            continue
        if item.get("cloud", "on") == "on" and hooks.cloud_off:
            continue
        started = time.perf_counter()
        runs = run_item(hooks, item)
        for i, run in enumerate(runs):
            r = score_turn(hooks, item, i, run)
            r["item_ms"] = (time.perf_counter() - started) * 1000
            out.append(r)
            if on_turn:
                on_turn(r)
    return out


def summarize(results: list[dict]) -> dict:
    """Item-level pass rates: all, held-out, per category and per acceptance example."""
    by_item: dict[str, list[dict]] = {}
    for r in results:
        by_item.setdefault(r["id"], []).append(r)
    run = {k: v for k, v in by_item.items() if not v[0].get("not_run")}
    passed = {k for k, v in run.items() if item_ok(v)}
    held = {k for k, v in run.items() if v[0]["held_out"]}
    cats: dict[str, list[int]] = {}
    for k, v in run.items():
        c = cats.setdefault(v[0]["category"], [0, 0])
        c[0] += k in passed
        c[1] += 1
    ae: dict[str, list[str]] = {}
    for k, v in by_item.items():
        for tag in v[0]["ae"]:
            ae.setdefault(tag, []).append(k)
    turns = [r for r in results if not r.get("not_run")]
    task = [r for r in turns if r.get("expected_task_type")]
    return {
        "items": len(by_item), "run": len(run), "not_run": sorted(set(by_item) - set(run)),
        "passed": len(passed), "held_out": (len(held & passed), len(held)),
        "turns": (sum(r["ok"] for r in turns), len(turns)),
        "task_type": (sum(r["task_type"] == r["expected_task_type"] for r in task), len(task)),
        "by_category": dict(sorted(cats.items())),
        "ae": {tag: {"items": ids, "passed": [i for i in ids if i in passed],
                     "not_run": [i for i in ids if i not in run]} for tag, ids in sorted(ae.items())},
        "failed": sorted(set(run) - passed),
        "cached": sum(r.get("cached", False) for r in turns),
        "facet_failures": dict(sorted(_facet_failures(run).items(), key=lambda kv: -kv[1])),
        "only_precision": sorted(k for k, v in run.items() if k not in passed and all(
            reason.split(":", 1)[0] == "source_precision" for r in v for reason in r["reasons"])),
    }


def _facet_failures(run: dict[str, list[dict]]) -> dict[str, int]:
    """Items failing each facet (an item counts once per facet)."""
    out: dict[str, int] = {}
    for v in run.values():
        for facet in {reason.split(":", 1)[0] for r in v for reason in r["reasons"]}:
            out[facet] = out.get(facet, 0) + 1
    return out
