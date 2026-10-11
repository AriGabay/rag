"""Gate 8 (U12, KTD16, R27): held-out questions run through the same API path as every other question.

World: the acceptance world of gates 1-7 plus the held-out corpus (``tests/fixtures/general/``, group G3
"ידע כללי", visible to admin-a and dana, never to yossi or office B), seeded with ``scripts/seed_demo.py``'s
own ``seed_general_corpus``. This module builds that world itself and leaves it marked dirty, so the shared
world of the other gates is rebuilt without the held-out group and their expectations are unaffected.

The provider is scripted (``tests/support/scripted_provider.py``); no real model is called:

* INTERPRET replays the plan the real model returned for the same question in the live real-model sample
  (``tests/fixtures/scripted_plans/general_plans.json``; provenance and the per-plan origin, ``recorded`` or
  ``synthetic``, are inside). Handles of attributes the live office had extracted are replaced by their
  description, so the plan resolves against this world's registry exactly as a new attribute.
* EXTRACT is an answer-key oracle (synthetic): for the requested attribute it reports the
  ``general_facts`` mentions of the document with their verbatim quote and the handle that holds it. The
  server still validates every mention (quote, naming term, unit), so a value the server cannot accept is
  rejected here as it would be with the real model.
* ANSWER and VERIFY are synthetic stubs: one claim quoting the first evidence, judged supported; for a
  question whose answer key is an abstention the stub reports insufficient evidence.

Asserted per turn (the facets of ``eval/general.py``): plan task type, turn relation, main tool, conversation
state, outcome kind, abstention kind, limited mode, computed result and extraction coverage, isolation and the
absence of a price clarification. Never wording, nor which passages the stub happened to cite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.answering.attributes import STRUCTURED_ATTRIBUTES
from app.answering.compose import COMPARE_POLICY
from app.answering.facts import GENERIC_MEASURE_WORDS
from app.config import get_settings
from app.db import tenant_tx
from app.providers.llm import CallStatus, Purpose
from eval.general import (
    Hooks,
    doc_scope,
    facts,
    filename_to_doc,
    general,
    general_docs,
    load_items,
    run_item,
    score_turn,
)
from eval.truth import USERS, docs, truth
from tests.acceptance.conftest import _SHARED, ADMIN_A, ADMIN_B, SEED, build_world, drain
from tests.support.scripted_provider import ScriptedProvider

pytestmark = pytest.mark.db

PLANS = Path(__file__).resolve().parents[1] / "fixtures" / "scripted_plans" / "general_plans.json"
ITEMS = load_items()
BY_ID = {i["id"]: i for i in ITEMS}
# facets that do not depend on the wording or on the passages a (stub) composer chose to cite
GATE_FACETS = ("flow", "task_type", "relation", "tool", "state", "no_price_clarification", "outcome",
               "abstention_kind", "limited_mode", "result", "coverage", "isolation", "idempotent")
# Items that fail even with a perfect (oracle) extractor and a correct plan: gaps of the system or of the
# question file, each also seen in the real-model sample (docs/evaluation/real-model-sample.md, "Failure
# analysis"). Letters: (a) interpretation/routing, (c) extraction/validation, (d) composition, (e) expectation.
# Each entry names the facets the gap covers ("facet" for every turn, "2.facet" for turn 2 only); every other
# facet of the item stays enforced. Strict per facet: once a named facet passes, the xfail turns into a failure and
# that facet (or the entry) must go.
# Removed (docs/evaluation/scorer-changes.md): GQ51/GQ52 (a clarification raised by the computation tool is the
# expected clarification, S4) and GQ28/GQ35/GQ36/GQ29/GQ49/GQ34 (the answer key now asserts the settled
# per-document state, S9: H3 / H4v2 / H2 awaiting review, H5 not stating a count).
_REVIEW_TIER = ("(c) the server routes these values to review (several values in one version, a synonym or "
                "plural naming term, an inferred unit), so no reviewed or preliminary figure exists")
_REJECTED = "(c) server validation rejects a stated value: {why}"
_OTHER_VERB = ("(c) {docs} name the year by another verb, not a form or root of the attribute's words; another "
               "phrasing goes to review, so {n} values are observed")
KNOWN_GAPS: dict[str, tuple[tuple[str, ...], str]] = {
    "GQ40": (("result", "coverage"),
             _REJECTED.format(why="a renovation is an event, not a quantity: 'no changes' (H1) has no year and "
                                  "another stated year is not accepted, so 3 of the 5 values are observed")),
    "GQ33": (("outcome",),
             _OTHER_VERB.format(docs="H2 (ואוכלס בשנת 2015) and H3 (הבניין הושלם בשנת 2004)", n="6 of the 8")
             + "; and H6/H7 (one property, block/parcel 6960/52) disagree (1958 / 1962): an unreviewed conflict "
             "that could be the minimum withholds the minimum (both values are shown), while the key expects 1958"),
    "GQ39": (("result", "coverage"), _REVIEW_TIER),
    "GQ37": (("task_type", "outcome", "abstention_kind"),
             "(a)/(d) without cloud use the question is planned as an answer (search) and the passages come "
             "with no abstention_kind; the file expects compute + not_extracted_or_verified"),
}
# Not strict: the recorded follow-up plan names S# handles issued in the live conversation; replayed here they
# match only when this conversation remembered its sources in the same order (the live run compared the wrong
# documents, see the failure analysis).
UNSTABLE: dict[str, str] = {
    "GQ22": "(a) replayed compare plan depends on the live conversation's S# handles",
}


# --- the scripted provider --------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = re.sub(r"[״\"”“]", "״", s or "")
    s = re.sub(r"[׳'’`]", "׳", s)
    return " ".join(s.split())


def _numbers(s: str) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)?", s or "")


UNIT_WORDS = ("מ״ר", "ס״מ", "מ׳", "מטר", "%")


def _title_to_doc() -> dict[str, str]:
    """Document title (from the original file name) -> the answer-key id of its current version."""
    out = {}
    every = list(truth()["documents"]) + list(general()["documents"])
    for d in sorted(every, key=lambda d: bool(d.get("version_of"))):  # a new version overrides its base
        base = d.get("version_of")
        first = (general_docs().get(base) or docs().get(base)) if base else d
        out[Path(first["filename"]).stem.replace("_", " ")] = d["id"]
    return out


class GeneralProvider(ScriptedProvider):
    name, model = "openai", "scripted-gate8"

    def __init__(self, plans: dict, attribute_map: dict[str, str], abstain_questions: set[str]):
        super().__init__()
        self.plans, self.attribute_map, self.abstain = plans, attribute_map, abstain_questions
        self.unscripted: list[str] = []
        self.titles = _title_to_doc()
        self.on(Purpose.INTERPRET, self._plan, repeat=True)
        self.on(Purpose.EXTRACT, self._extract, repeat=True)
        self.on(Purpose.ANSWER, self._answer, repeat=True)
        self.on(Purpose.VERIFY, self._verify, repeat=True)

    def _plan(self, instructions, input):
        payload = json.loads(input)
        question, fresh = payload["question"], not payload.get("recent_questions")
        entries = self.plans.get(question) or []
        entry = next((e for e in entries if e["state_empty"] == fresh), entries[0] if entries else None)
        if entry is None:
            self.unscripted.append(question)
            return CallStatus.ERROR
        return portable(entry["plan"])

    def _extract(self, instructions, input):
        label = re.search(r"המאפיין המבוקש: (.+?)(?: \(שמות נוספים|\. ממד היחידה|\. סוג הערך)", input).group(1)
        title = re.search(r'המסמך: "(.+?)"', input).group(1)
        attribute, doc = self.attribute_map.get(_norm(label)), self.titles.get(title)
        mentions = []
        for f in facts().values():
            if f["document"] != doc or f["attribute"] != attribute:
                continue
            m = oracle_mention(f, label, input)
            if m is not None:
                mentions.append(m)
        return {"mentions": mentions}

    def _answer(self, instructions, input):
        question = input.split("שאלת המשתמש:\n", 1)[1].split("\n", 1)[0]
        ids = re.findall(r'<evidence id="(E\d+)"', input)
        if question in self.abstain or not ids:
            out = {"claims": [], "insufficient": True, "missing_info": "הנתון אינו מופיע בקטעים"}
        else:
            out = {"claims": [{"text": "ראו את הקטע", "evidence_ids": [ids[0]], "kind": "explicit", "numbers": []}],
                   "insufficient": False, "missing_info": None}
        if COMPARE_POLICY in instructions:
            out["conflicts"] = []
        return out

    @staticmethod
    def _verify(instructions, input):
        return {"verdicts": [{"claim": int(n), "verdict": "supported"} for n in re.findall(r'<claim n="(\d+)"', input)]}


def _table_cell(prompt: str, quote: str, row_label: str | None) -> tuple[str, str] | None:
    """(handle, "header: cell" line) of the cell holding ``quote`` in the row that carries ``row_label``."""
    for body in re.findall(r'<row id="T\d+R\d+"[^>]*>\n(.*?)\n</row>', prompt, flags=re.S):
        lines = re.findall(r"\[(T\d+R\d+C\d+)\] (.*)", body)
        cells = [_norm(line.split(": ", 1)[-1]) for _, line in lines]
        if row_label and _norm(row_label) not in cells:
            continue
        for (handle, line), cell in zip(lines, cells, strict=True):
            if _norm(quote) == cell:
                return handle, line
    return None


def oracle_mention(f: dict, label: str, prompt: str) -> dict | None:
    """The answer-key mention of fact ``f`` as the prompt shows it (synthetic extraction)."""
    quote = f["quote"]
    source = f.get("source") or {}
    if source.get("kind") == "table":
        found = _table_cell(prompt, quote, source.get("row_label"))
        if found is None:
            return None
        handle, line = found
        header = line.split(": ", 1)[0]
        # a wide table names the attribute in its column header; a key/value table only in its row label
        names = {_bare(w) for w in _norm(label).split()}
        term = header if names & {_bare(w) for w in _norm(header).split()} else (source.get("row_label") or header)
    else:
        handle = None
        for h, body in re.findall(r'<chunk id="(C\d+)" page="[^"]*">\n(.*?)\n</chunk>', prompt, flags=re.S):
            if _norm(quote) in _norm(body):
                handle = h
                break
        if handle is None:
            return None
        term = naming_term(label, quote)
    value = f["value"]
    if isinstance(value, bool) or value in (None, "none"):
        return None  # a stated absence carries no value to extract
    nums = _numbers(quote)
    norm_value = str((f.get("normalized") or {}).get("value") or "")
    value_text = next((v for v in (str(value), norm_value) if v and v in nums), None)
    if value_text is None:
        if isinstance(value, int | float) or re.fullmatch(r"[\d.x]+", str(value)):
            value_text = nums[0] if nums else str(value)
        else:
            value_text = str(value) if str(value) in quote else quote
    unit = next((u for u in UNIT_WORDS if u in _norm(quote)), None)
    return {"entity_role": "subject", "entity_descriptor": None, "value_text": value_text, "unit_text": unit,
            "quote": quote, "source": handle, "attribute_term": term}


def _bare(word: str) -> str:
    return word[1:] if len(word) > 3 and word[0] in "הבלמוש" else word


def naming_term(label: str, quote: str) -> str:
    """The quote's words that name the attribute, as written: the span of quote words matching a label word
    (ignoring a one-letter prefix); when that span is only measure words ("שטח", "שנת"), or there is none,
    the words before the number."""
    words = quote.split()
    names = {_bare(w) for w in _norm(label).split()}
    hits = [i for i, w in enumerate(words) if _bare(_norm(w)) in names or _norm(w) in names]
    span = words[hits[0]:hits[-1] + 1] if hits else []
    generic = {_bare(w) for w in GENERIC_MEASURE_WORDS}
    if span and not all(_bare(_norm(w)) in generic for w in span):
        return " ".join(span)
    first_number = next((i for i, w in enumerate(words) if re.search(r"\d", w)), len(words))
    return " ".join(words[:first_number])


def portable(plan: dict) -> dict:
    """A recorded plan with the live office's extracted-attribute handles replaced by their description."""
    structured = len(STRUCTURED_ATTRIBUTES)

    def keep(handle: str | None) -> str | None:
        return handle if handle and int(handle[1:]) <= structured else None

    plan = json.loads(json.dumps(plan))
    if plan.get("attribute"):
        plan["attribute"]["handle"] = keep(plan["attribute"].get("handle"))
    for step in plan.get("steps") or []:
        step["attribute_handle"] = keep(step.get("attribute_handle"))
    return plan


def load_plans() -> tuple[dict, dict[str, str]]:
    data = json.loads(PLANS.read_text(encoding="utf-8"))
    plans = data["plans"]
    attribute_map: dict[str, str] = {}
    for entry in (e for entries in plans.values() for e in entries):
        desc = ((entry["plan"].get("attribute") or {}).get("description") or "").strip()
        attr = entry.get("answer_key_attribute")
        if desc and attr:
            attribute_map.setdefault(_norm(desc), attr)
    return plans, attribute_map


# --- the world ------------------------------------------------------------------------------------------

class GeneralWorld:
    def __init__(self, world, provider: GeneralProvider):
        self.world, self.provider = world, provider
        self.filenames: dict[str, str] = {}  # version id -> fixture file name
        self.documents: dict[str, str] = {}  # document id -> answer-key document id
        for email in (ADMIN_A, ADMIN_B):
            c = world.client(email)
            for d in c.get("/api/documents").json()["documents"]:
                for v in c.get(f"/api/documents/{d['id']}").json().get("versions", []):
                    self.filenames[v["id"]] = v["filename"]
                    gt = filename_to_doc().get(v["filename"])
                    if gt:
                        self.documents.setdefault(d["id"], gt)

    def doc_of(self, source: dict) -> str | None:
        return filename_to_doc().get(self.filenames.get(str(source["version_id"]), ""))

    def plan_of(self, question_id: str) -> dict | None:
        for office in ("A", "B"):
            with tenant_tx(self.world.system(office)) as conn:
                row = conn.execute(text("SELECT plan, steps, parse_route FROM questions WHERE id = :q"),
                                   {"q": question_id}).first()
            if row is not None:
                return dict(row._mapping)
        return None

    def forbidden(self, user: str, rule: str | None) -> set[str]:
        office, groups = USERS[user]
        out = set()
        for doc_id, gt in self.documents.items():
            o, g = doc_scope(gt)
            if rule == "other_office" and o != office:
                out.add(doc_id)
            if rule == "other_group" and (o != office or (groups is not None and g not in groups)):
                out.add(doc_id)
        return out

    def hooks(self, cloud_off: bool = False) -> Hooks:
        return Hooks(client=self.world.client, doc_of=self.doc_of, plan_of=self.plan_of, forbidden=self.forbidden,
                     unverified=set(self.world.exclude), cloud_off=cloud_off)

    def run(self, item: dict, cloud_off: bool = False) -> list[dict]:
        """One item through the API; asked again once the worker drained when extraction was pending."""
        hooks = self.hooks(cloud_off)
        runs = run_item(hooks, item)
        if any((r.flow.answer.get("pending_extraction") or 0) > 0 for r in runs):
            drain()
            runs = run_item(hooks, item)
        return [score_turn(hooks, item, i, r) for i, r in enumerate(runs)]


@pytest.fixture(scope="module")
def gworld(owner_engine):
    if _SHARED["world"] is not None:
        _SHARED["world"].close()
        _SHARED["world"] = None
    w = build_world(owner_engine)
    w.dirty()  # the held-out group must not leak into the other gates' shared world
    mp = pytest.MonkeyPatch()
    mp.setattr(SEED, "wait_for_processing", lambda clients, timeout=0: drain())
    SEED.seed_general_corpus(w.client(ADMIN_A), truth()["general_facts"])
    plans, attribute_map = load_plans()
    abstain = {t["ask"] for i in ITEMS for t in i["turns"] if t["expect"]["outcome"] == "abstain"}
    provider = GeneralProvider(plans, attribute_map, abstain)
    for target in ("app.answering.content", "app.providers.llm"):
        mp.setattr(f"{target}.selected_provider_configured", lambda: True)
        mp.setattr(f"{target}.get_selected_provider", lambda: provider)
    r = w.client(ADMIN_A).put("/api/admin/settings", json={"cloud_llm_enabled": True, "acknowledge": True})
    assert r.status_code == 200 and r.json()["mode"] == "cloud", r.text
    gw = GeneralWorld(w, provider)
    yield gw
    mp.undo()
    w.close()


def _gate_reasons(results: list[dict]) -> list[tuple[int, str, str]]:
    """(turn, facet, reason) of every failed gate facet."""
    return [(r["turn"], reason.split(":", 1)[0], reason) for r in results for reason in r["reasons"]
            if reason.split(":", 1)[0] in GATE_FACETS]


def _in_gap(turn: int, facet: str, named: tuple[str, ...]) -> bool:
    return facet in named or f"{turn}.{facet}" in named


def _gate_failures(results: list[dict]) -> list[str]:
    out = []
    for r in results:
        reasons = [reason for reason in r["reasons"] if reason.split(":", 1)[0] in GATE_FACETS]
        out += [f"{r['id']}.{r['turn']} {reason}" for reason in reasons]
        if reasons:  # what the turn did, for diagnosis
            out.append(f"{r['id']}.{r['turn']} -> task {r['task_type']} tools {r['tools']} kind {r['got']} "
                       f"abstention {r['abstention_kind']} figure {r['figure']} n={r['figure_n']} "
                       f"coverage { {k: v for k, v in (r['coverage'] or {}).items() if v} }")
    return out


def _check(results: list[dict], item_id: str) -> None:
    failures = _gate_failures(results)
    if item_id in UNSTABLE and failures:
        pytest.xfail(UNSTABLE[item_id] + " | " + "; ".join(failures)[:400])
    if item_id in KNOWN_GAPS:  # strict, and only for the named facets: every other facet stays enforced
        named, why = KNOWN_GAPS[item_id]
        reasons = _gate_reasons(results)
        outside = [f"{item_id}.{t} {reason}" for t, facet, reason in reasons if not _in_gap(t, facet, named)]
        assert not outside, "\n".join(outside + [f for f in failures if " -> task " in f])
        fixed = [n for n in named if not any(_in_gap(t, facet, (n,)) for t, facet, _ in reasons)]
        if fixed:
            pytest.fail(f"{item_id}: the known-gap facets {fixed} pass now: remove them from KNOWN_GAPS")
        pytest.xfail(why + " | " + "; ".join(failures)[:400])
    assert not failures, "\n".join(failures)


# --- tests ----------------------------------------------------------------------------------------------

def test_recorded_plans_cover_every_model_question():
    plans, _ = load_plans()
    data = json.loads(PLANS.read_text(encoding="utf-8"))
    assert data["provenance"]["model"] and data["provenance"]["source"]
    entries = [e for v in plans.values() for e in v]
    assert all(e["origin"] in ("recorded", "synthetic") for e in entries)
    assert all(e.get("reason") for e in entries if e["origin"] == "synthetic")
    asked = {t["ask"] for i in ITEMS for t in i["turns"]}
    assert set(plans) <= asked


def test_held_out_corpus_is_seeded_and_isolated(gworld):
    group = {g["name"]: g for g in gworld.world.client(ADMIN_A).get("/api/admin/groups").json()["groups"]}
    assert group[general()["group"]["name"]]["document_count"] == len(
        [d for d in general()["documents"] if not d.get("version_of")])
    held = {d for d, gt in gworld.documents.items() if gt in general_docs()}
    for email, visible in (("dana@demo.test", True), ("yossi@demo.test", False), (ADMIN_B, False)):
        listed = {d["id"] for d in gworld.world.client(email).get("/api/documents").json()["documents"]}
        assert bool(listed & held) is visible, email


@pytest.mark.parametrize("item_id", [i["id"] for i in ITEMS if i.get("cloud", "on") == "on"])
def test_general_item(gworld, item_id):
    results = gworld.run(BY_ID[item_id])
    assert gworld.provider.unscripted == [] or not any(
        t["ask"] in gworld.provider.unscripted for t in BY_ID[item_id]["turns"]), gworld.provider.unscripted
    _check(results, item_id)


@pytest.mark.parametrize("item_id", [i["id"] for i in ITEMS if i.get("cloud") == "off"])
def test_general_item_with_cloud_off(gworld, item_id, monkeypatch):
    """AE2: without cloud use the same question is answered in limited mode, passages only, no number."""
    admin = gworld.world.client(ADMIN_A)
    monkeypatch.setattr(get_settings(), "demo_mode", False)
    admin.put("/api/admin/settings", json={"cloud_llm_enabled": False, "acknowledge": True})
    calls = len(gworld.provider.calls)
    try:
        results = gworld.run(BY_ID[item_id], cloud_off=True)
    finally:
        admin.put("/api/admin/settings", json={"cloud_llm_enabled": True, "acknowledge": True})
    assert len(gworld.provider.calls) == calls, "no provider call while cloud use is off"
    _check(results, item_id)


def test_a_held_out_attribute_needs_no_code_change(gworld):
    """A held-out attribute that exists only in the corpus and the question set is computed with coverage through the
    generic path: no keyword, column or route was added for it."""
    item = BY_ID["GQ38"]  # yard area: "חצר"
    results = gworld.run(item)
    assert not _gate_failures(results), _gate_failures(results)
    r = results[0]
    assert r["tools"] and "extract_and_compute" in r["tools"]
    assert r["figure_n"] == 1 and r["coverage"]["found"] == 1
