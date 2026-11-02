"""Round 19 acceptance with the real model (U5: R19; AE1, AE3, AE4, AE7 and the gap-line rules R13-R17): the comparison, calculation-
page, two-component, gap-line and number shapes shown on the configured model and judge (``gpt-6-luna`` with the repository's
settings, no override; the key comes from the repository's ``.env`` through the round-10 ``real_key`` fixture and is never
printed). Synthetic documents only (every name, address and figure invented): one PDF of ``tests/fixtures/round19`` per sequence
(``scripts/generate_fixtures.py --only round19``; the contents are the manifest's), ingested through the worker into a synthetic
office of the ``rag_test`` database.

- ``d1`` a comparison of two appraisals of one file (AE4): R19c, side A (the property at HaShaked 7) on pages 1-2 and side B (the
  property at HaOren 2) on pages 3-4, each with a value and an area. Asked to compare the value and the area of the two, all four
  figures reach the answer, none is removed as the figure of another property (no ``wrong_subject`` removal and no removal sentence),
  and each figure stands on its own side (a table cell under the other side's header, or a sentence that names only the other side,
  fails; a place that does not tell the side is recorded as unknown, not judged);
- ``d2a`` / ``d2b`` calculation pages (AE3): R19a (the calculation page opens with a descriptive sentence that ends in a colon)
  and R19b (it opens with a line of digit noise), the table of each states the parcel of the asked property, and a second property
  of another parcel stands later in the file. Asked for the annual net income and the capitalised value of the asked property, both
  values reach the answer with a citation and nothing is removed as another property's. Whether the other property's value reached
  the answer is reported, not asserted (the model's own choice, not the server's);
- ``d3`` one table row that answers two components (AE1): R19d, a total-area row whose note says the rights apply only under an
  approved plan. Asked for the area and the condition, both reach the answer with a citation, no item of the ledger is left
  ``omitted`` and no gap line the server wrote stands in the answer. The request components and the ledger are recorded;
- ``d4a`` / ``d4b`` gap lines (AE7, R13-R17): R19g, a rights table with two rows (a main area and a basement), each with an area, a
  number of floors and a plan identifier. ``d4a`` asks for the building rights; ``d4b`` asks for the area, the floors and the plan of
  each row. Gating: no gap line (``item_gap``) names a row whose area and floors the answer states (a "read and not used" line is
  false, and a "partial" line whose missing figures the answer does state is false), and every item the ledger ends ``omitted``
  has a line. Whether a true omission happened at all (the model can answer everything) is reported as ``true_omission_exercised``
  and never forced;
- ``d5`` REPORT ONLY (no assert beyond a finished turn): R19b, asked for the comparison-approach value of the first property while
  the file also holds a second property of another parcel. The record classifies what happened to the second property's value
  (``not_claimed`` / ``removed`` / ``flagged`` / ``let_through``), with the removals, the server spans and the ledger, for the
  ownership fact line path (rule 34 of the verification prompt) on evidence that names no property;
- ``d6`` a decimal comma and a negative percent (AE6, U1): R19f, a table that writes 13,2% and a negative 3,5% beside 3,596,696.
  Asked for the three, the rate reads as 13.2, the change keeps its sign (a minus sign, or a word of decline) and is never turned
  into a rise or into 35 or 3.5 positive, the value keeps its thousands commas, and no removal sentence stands in the answer.

Each sequence is asked in a **new conversation, exactly three times**; every repetition is recorded before anything is asserted - a
failure in any repetition is reported and a later pass never hides it - and a sequence passes only when all three repetitions
pass. The gating checks are by behaviour and never by wording (a pattern part is matched, so a Hebrew definite article or prefix
does not change the outcome).

Recorded per repetition (``real_documents/eval/round19/real-model/``, gitignored: the documents are synthetic, so the answers are
kept for diagnosis): the whole turn (final text, rounds, tools, item ledger, the request components, the spans the server wrote
with their kind and text, the removals, usage), the pass or fail of each check, and **the stage that produced the part** -
``agent_draft``, ``completion_repair``, ``server_attachment`` or ``omitted`` (read from the diagnostics and the server-span
registry, as in round 18). A summary file per sequence is written when the sequence ends, and one for the run at the end of the
module.

Opt in with ``-m real_model``; skipped without a key. Host only, against ``rag_test`` (``tests/conftest.py`` guard)."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_round10_real_model import new_conversation, upload
from tests.integration.test_round10_real_model import real_key as real_key  # noqa: F401 - the key fixture
from tests.integration.test_round17_real_model import (
    CITED,
    answered,
    cites_document,
    has,
    reached_all,
    stage,
)
from tests.integration.test_round18_real_model import PLAN, Turn18, figure

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round19" / "manifest.json").read_text(encoding="utf-8"))["documents"]
CALC_SENTENCE, CALC_NOISE, COMPARISON = MANIFEST["calc_sentence"], MANIFEST["calc_noise"], MANIFEST["comparison"]
TWO_COMPONENTS, DECIMAL, ROWS = MANIFEST["two_components"], MANIFEST["decimal_comma"], MANIFEST["coverage_rows"]
SIDE_A, SIDE_B = COMPARISON["reports"]
OTHER = CALC_SENTENCE["other"]
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round19" / "real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"results-{STAMP}.json"
SUMMARY_FILE = RESULTS_DIR / f"summary-{STAMP}.json"
RESULTS: list[dict] = []
ATTEMPTS = 3  # exactly three, each in a new conversation
STARTED = time.perf_counter()
MODEL = "gpt-6-luna (settings)"

ADMIN = "admin-r19@example.test"


# --- what each sequence looks for -----------------------------------------------------------------------------------------

# d1: the two sides of the comparison, the figures of each, and the words that tell the side (a street name, whatever the prefix)
SIDE_WORDS = {"A": re.compile(r"שקד"), "B": re.compile(r"אורן")}
COMPARED = {"A": {"value": figure(SIDE_A["value"].replace(",", "")), "area": figure(SIDE_A["area"])},
            "B": {"value": figure(SIDE_B["value"].replace(",", "")), "area": figure(SIDE_B["area"])}}
# d2: the asked property's two values on the calculation page, and the value of the other property in the same file
INCOME, CAPITALISED = figure("610000"), figure("9384615")
OTHER_VALUE = figure(OTHER["value"].replace(",", ""))
# d3: the area of the row, and its condition (the round-18 pattern for "an approved plan")
TOTAL_AREA = figure("1200")
# d4: the two rows of R19g by their labels, and the figures that make a row "given" (its area and its number of floors)
ROW_LABELS = {"main": re.compile(r"עיקרי"), "basement": re.compile(r"מרתף")}
ROW_HEADLINE = {"main": (figure("1200"), re.compile(r"(?<![\d.,])6(?![\d.,])|שש")),
                "basement": (figure("340"), re.compile(r"(?<![\d.,])2(?![\d.,])|שתי|שניים|שני"))}
GAP_KINDS = ("item_gap", "gap_paragraph")  # the server spans that carry the lines about items the answer does not give
NOT_USED = re.compile(r"לא\s+נעשה\s+בו\s+שימוש")
PARTIAL_MISSING = re.compile(r"לא\s+נאמר\s+בה:\s*(.*)")
# d6: the three figures of R19f and the ways an answer can lose the sign of the change
YIELD = re.compile(r"(?<![\d.,])13[.,]2(?!\d)")
CHANGE = re.compile(r"(?<![\d.,])3[.,]5(?!\d)")
SIGN = re.compile(r"[-−–‐‑]\s*3[.,]5(?!\d)|3[.,]5\s*%?\s*[-−–‐‑]")
DECLINE = re.compile(r"ירידה|ירד|שלילי|פחת|קטנ|הפחת|נסוג|היחלש")
RISE = re.compile(r"עלייה|עליה|עלו|עלה|גידול|התייקר|התייקרו|התעלה|עלית")
WRONG_RATE = re.compile(r"(?<![\d.,])(?:132|35)(?![\d]|[.,]\d)\s*%")
PROPERTY_VALUE = figure("3596696")
# the removal sentences the server writes (``REMOVAL_SENTENCES``) and the failure kind of a claim given to another property
REMOVAL_SENTENCE = re.compile(r"הוסרה\s+טענה")
ANOTHER_PROPERTY = "wrong_subject"
# the notes the server puts beside a claim it kept in part
FLAG_KINDS = {"partial_mark", "qualifier", "removal_note", "replacement", "marker", "limit_sentence"}


# --- environment ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def office(db, client, monkeypatch, real_key):  # noqa: F811 - the key fixture is the imported one
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    o = make_office(db, "משרד בדיקה סינתטי", ADMIN)
    with tenant_tx(o.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    login(client, ADMIN)
    o.client, o.ids = client, {}
    return o


def ingest_pdf(office, monkeypatch, spec: dict) -> str:
    """A round-19 PDF fixture read by the worker; ``office.ids[...]`` is its document id."""
    key = spec.get("title") or spec["file"]
    office.ids[key] = upload(office, FIXTURES / spec["file"], "appraisal", monkeypatch)
    return office.ids[key]


# --- turns and records ----------------------------------------------------------------------------------------------------

def flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": MODEL, "attempts": RESULTS}, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")


class Turn19(Turn18):
    """The round-18 turn record plus the request components the requirements trace ended with, the server's gap lines and the
    answer without them (what the answer itself states)."""

    def __init__(self, c, m: dict, question: str, wall: float) -> None:
        super().__init__(c, m, question, wall)
        d = c.get(f"/api/chat/messages/{m['id']}/diagnostics")
        body = d.json() if d.status_code == 200 else {}
        rounds = body.get("requirements_trace") or []
        last = rounds[-1] if rounds else {}
        self.request_components = [{"id": comp.get("id"), "final_status": comp.get("final_status"),
                                    "items": [{k: i.get(k) for k in ("id", "state", "origin", "declared", "why", "label")}
                                              for i in comp.get("items") or []]}
                                   for comp in last.get("components") or []]

    @property
    def gap_lines(self) -> list[str]:
        """The lines of the spans the server wrote about items (``item_gap``, ``gap_paragraph``), whitespace collapsed."""
        out = []
        for s in self.spans:
            if s["kind"] in GAP_KINDS:
                out += [re.sub(r"\s+", " ", ln).strip() for ln in s["text"].splitlines() if ln.strip()]
        return out

    @property
    def stated_lines(self) -> list[str]:
        """The answer's lines without the spans of the gap kinds: what the answer states by itself."""
        md, cuts = self.markdown, sorted((s["start"], s["end"]) for s in self.spans if s["kind"] in GAP_KINDS)
        out, pos = [], 0
        for a, b in cuts:
            out.append(md[pos:a])
            pos = max(pos, b)
        out.append(md[pos:])
        raw = "".join(out)
        markup = re.compile(r"[*`‎‏‪-‮⁦-⁩]")
        return [re.sub(r"\s+", " ", markup.sub("", ln)).strip() for ln in raw.splitlines() if ln.strip()]

    @property
    def removed_as_another_property(self) -> list[dict]:
        return [r for r in self.removed if r.get("failure") == ANOTHER_PROPERTY]

    def record(self) -> dict:
        return super().record() | {"request_components": self.request_components, "gap_lines": self.gap_lines}


def ask(c, cid: str, question: str, rec: dict) -> Turn19:
    """One turn through the API; recorded (and flushed) before anything is asserted about it."""
    started = time.perf_counter()
    r = c.post(f"/api/chat/conversations/{cid}/messages", json={"content": question})
    assert r.status_code == 200, r.text
    m = c.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    t = Turn19(c, m, question, time.perf_counter() - started)
    rec["turns"].append(t.record())
    flush()
    assert m["status"] == "done", (m.get("status"), m.get("error"))
    return t


def check(rec: dict, name: str, fn) -> None:
    """Run one named assertion of a repetition and record its outcome; a failure never stops the repetition's other checks."""
    try:
        fn()
    except AssertionError as exc:
        rec["checks"][name] = {"outcome": "fail", "failure": str(exc)[:2000]}
    else:
        rec["checks"][name] = {"outcome": "pass"}
    flush()


def sequence_summary(sequence: str) -> dict:
    runs = [r for r in RESULTS if r["sequence"] == sequence]
    attempts = []
    for r in runs:
        info = r.get("info") or {}
        turn = (r.get("turns") or [{}])[-1]
        attempts.append({"attempt": r["attempt"], "outcome": r["outcome"],
                         "checks": {k: v["outcome"] for k, v in r["checks"].items()},
                         "failures": {k: v["failure"] for k, v in r["checks"].items() if v["outcome"] == "fail"},
                         "stage": info.get("stage"), "info": {k: v for k, v in info.items() if k != "stage"},
                         "final_text": turn.get("markdown"), "server_spans": turn.get("server_spans"),
                         "rounds": turn.get("rounds"), "tools": turn.get("tools")})
    passed = sum(a["outcome"] == "pass" for a in attempts)
    return {"model": MODEL, "sequence": sequence, "attempts": attempts, "passed_runs": f"{passed}/{len(attempts)}",
            "passed": len(attempts) == ATTEMPTS and passed == ATTEMPTS}


def run_sequence(sequence: str, checks: tuple[str, ...], office, body) -> None:
    """``body(office, client, conversation_id, record)`` in a new conversation, exactly ``ATTEMPTS`` times; every repetition is
    recorded and none is skipped after a failure. The summary file of the sequence is written before anything is asserted. The
    test fails when any check of any repetition failed (or never ran because a turn did not finish), with every failure in the
    message."""
    failures: list[str] = []
    for n in range(1, ATTEMPTS + 1):
        rec = {"sequence": sequence, "attempt": n, "turns": [], "checks": {}, "info": {}, "outcome": None}
        RESULTS.append(rec)
        try:
            body(office, office.client, new_conversation(office.client), rec)
        except AssertionError as exc:
            rec["aborted"] = str(exc)[:2000]
        for name in checks:
            rec["checks"].setdefault(name, {"outcome": "fail", "failure": "never ran: " + rec.get("aborted", "?")})
        rec["outcome"] = "pass" if all(v["outcome"] == "pass" for v in rec["checks"].values()) else "fail"
        flush()
        failures += [f"attempt {n} / {name}: {v['failure']}" for name, v in rec["checks"].items() if v["outcome"] == "fail"]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"summary-{sequence}-{STAMP}.json").write_text(
        json.dumps(sequence_summary(sequence), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    assert not failures, "\n".join(failures)


def base_info(t: Turn19, reached: dict[str, bool], parts: dict[str, str]) -> dict:
    """The record shared by the sequences: which parts reached the answer and in which stage, what the turn reports."""
    return {"parts_reached": reached, "stage": parts, "completeness": t.completeness.get("status"),
            "completed_by_server": t.completed_by_server, "repair_rounds": t.repair_rounds, "tools": t.tools,
            "server_span_kinds": [s["kind"] for s in t.spans],
            "removed": [(r["action"], r["check"], r["failure"]) for r in t.removed],
            "request_components": [(c["id"], c["final_status"]) for c in t.request_components],
            "ledger": [(i["label"], i["value"], i["state"], i["declared"], i["origin"]) for i in t.items]}


def stated(t: Turn19, anchor: re.Pattern) -> bool:
    """Whether the answer states ``anchor`` in a line of its own (not in a line the server wrote about an item)."""
    return any(has(ln, anchor) for ln in t.stated_lines)


def window(t: Turn19, anchor: re.Pattern) -> list[str]:
    """For each stated line with ``anchor``: it with the lines before and after it (the stretch in which a citation or a label
    counts as next to a value), the server's gap lines left out."""
    lines, out = t.stated_lines, []
    for i, ln in enumerate(lines):
        if has(ln, anchor):
            out.append(" ".join(lines[max(0, i - 1):i + 2]))
    return out


def cited_next_to(t: Turn19, anchor: re.Pattern, doc: str) -> bool:
    return any(CITED.search(s) and doc in t.documents_cited_in(s) for s in window(t, anchor))


def no_removal_as_another_property(t: Turn19) -> None:
    removed = t.removed_as_another_property
    assert not removed, ("a claim was removed as the datum of another property", removed[:3])
    assert not REMOVAL_SENTENCE.search(t.markdown), ("the answer carries a removal sentence", t.markdown[:400])


# --- d1: which side a figure stands on ------------------------------------------------------------------------------------

def sides_in(fragment: str) -> set[str]:
    return {k for k, w in SIDE_WORDS.items() if w.search(fragment)}


def cells_of(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def placements(t: Turn19, anchor: re.Pattern) -> list[tuple[str, str]]:
    """For each place the answer states ``anchor``: (the side the place tells - ``A``, ``B`` or ``unknown`` - and the line). A table
    cell is told by the header of its column, else by the label of its row; a sentence is told by the side it names, else by the
    nearest line before it that names one (three lines back at most); a place that names both sides is not told."""
    lines, out = t.stated_lines, []
    for i, ln in enumerate(lines):
        if not has(ln, anchor):
            continue
        told: set[str] = set()
        if ln.lstrip().startswith("|"):
            cells = cells_of(ln)
            idx = next((k for k, c in enumerate(cells) if has(c, anchor)), 0)
            start = i
            while start > 0 and lines[start - 1].lstrip().startswith("|"):
                start -= 1
            header = cells_of(lines[start]) if start != i else []
            if idx > 0 and len(header) == len(cells):
                told = sides_in(header[idx])
            if not told and idx > 0:
                told = sides_in(cells[0])
        else:
            told = sides_in(ln)
            for back in range(1, 4):
                if told or i - back < 0:
                    break
                told = sides_in(lines[i - back])
        out.append((next(iter(told)) if len(told) == 1 else "unknown", ln))
    return out


# =========================================================================================================================
# the sequences
# =========================================================================================================================

def test_d1_the_cells_of_a_comparison_stay_on_their_sides_three_times(office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, COMPARISON)

    def figures_stand(t: Turn19) -> None:
        missing = [(side, name) for side, parts in COMPARED.items() for name, pat in parts.items() if not stated(t, pat)]
        assert not missing, ("a figure of the comparison is not in the answer", missing, t.markdown[:400])

    def on_their_sides(t: Turn19) -> None:
        wrong = []
        for side, parts in COMPARED.items():
            for name, pat in parts.items():
                wrong += [(side, name, told, ln[:200]) for told, ln in placements(t, pat) if told not in (side, "unknown")]
        assert not wrong, ("a figure stands on the other side's cell or sentence", wrong[:3])

    def each_cited(t: Turn19) -> None:
        bad = [(side, name) for side, parts in COMPARED.items() for name, pat in parts.items()
               if stated(t, pat) and not cited_next_to(t, pat, doc)]
        assert not bad, ("a figure of the comparison has no citation of the appraisals file next to it", bad)

    def body(o, c, cid, rec):
        t = ask(c, cid, "השווה בין שווי הנכס ושטחו ברחוב השקד 7 לבין שווי הנכס ושטחו בדרך האורן 2.", rec)
        reached = {f"{side}_{name}": stated(t, pat) for side, parts in COMPARED.items() for name, pat in parts.items()}
        parts = {f"{side}_{name}": stage(t, reached[f"{side}_{name}"]) for side, parts in COMPARED.items() for name in parts}
        rec["info"] = base_info(t, reached, parts)
        where = {f"{side}_{name}": [told for told, _ in placements(t, pat)] for side, ps in COMPARED.items() for name, pat in ps.items()}
        rec["info"] |= {"figure_places": where, "unknown_places": sum(p == "unknown" for v in where.values() for p in v),
                        "removed_as_another_property": t.removed_as_another_property}
        check(rec, "answered", lambda: answered(t))
        check(rec, "cited", lambda: cites_document(t, doc))
        check(rec, "all_four_figures_stated", lambda: figures_stand(t))
        check(rec, "no_figure_removed_as_another_property", lambda: no_removal_as_another_property(t))
        check(rec, "each_figure_on_its_side", lambda: on_their_sides(t))
        check(rec, "each_figure_cited", lambda: each_cited(t))

    run_sequence("d1_comparison_cells_on_their_sides",
                 ("answered", "cited", "all_four_figures_stated", "no_figure_removed_as_another_property",
                  "each_figure_on_its_side", "each_figure_cited"), office, body)


CALC_QUESTION = "מהי ההכנסה נטו השנתית והשווי המהוון של מתחם גבעת הברוש בחישוב ההיוון?"


def calculation_sequence(office, monkeypatch, spec: dict, name: str) -> None:
    doc = ingest_pdf(office, monkeypatch, spec)

    def values_cited(t: Turn19) -> None:
        bad = [n for n, pat in (("income", INCOME), ("capitalised", CAPITALISED)) if not cited_next_to(t, pat, doc)]
        assert not bad, ("a value of the calculation has no citation of the appraisal next to it", bad)

    def body(o, c, cid, rec):
        t = ask(c, cid, CALC_QUESTION, rec)
        reached = {"income": stated(t, INCOME), "capitalised": stated(t, CAPITALISED)}
        rec["info"] = base_info(t, reached, {n: stage(t, r) for n, r in reached.items()})
        rec["info"] |= {"other_property_value_in_answer": stated(t, OTHER_VALUE),
                        "removed_as_another_property": t.removed_as_another_property}
        check(rec, "answered", lambda: answered(t))
        check(rec, "cited", lambda: cites_document(t, doc))
        check(rec, "values_kept", lambda: reached_all(reached))
        check(rec, "no_another_property_removal", lambda: no_removal_as_another_property(t))
        check(rec, "values_cited", lambda: values_cited(t))

    run_sequence(name, ("answered", "cited", "values_kept", "no_another_property_removal", "values_cited"), office, body)


def test_d2a_values_on_calculation_pages_headed_by_a_sentence_are_kept_three_times(office, monkeypatch):
    calculation_sequence(office, monkeypatch, CALC_SENTENCE, "d2a_calculation_pages_sentence_heading")


def test_d2b_values_on_calculation_pages_headed_by_digit_noise_are_kept_three_times(office, monkeypatch):
    calculation_sequence(office, monkeypatch, CALC_NOISE, "d2b_calculation_pages_digit_noise_heading")


def test_d3_a_row_that_answers_two_components_covers_both_three_times(office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, TWO_COMPONENTS)

    def both_stated(t: Turn19) -> None:
        missing = [n for n, ok in (("area", stated(t, TOTAL_AREA)), ("condition", stated(t, PLAN))) if not ok]
        assert not missing, ("a component of the question is not in the answer", missing, t.markdown[:400])

    def items_covered(t: Turn19) -> None:
        omitted = [(i["id"], i["component"], i["label"], i["why"]) for i in t.items if i["state"] == "omitted"]
        assert not omitted, ("an item the turn read is left omitted although the answer is asked for it", omitted[:3])
        assert t.items, "the ledger holds no item"
        not_full = [(c["id"], c["final_status"]) for c in t.request_components if c["final_status"] not in ("full",)]
        assert not not_full, ("a request component is not fully answered", not_full)

    def no_gap_line(t: Turn19) -> None:
        assert not t.gap_lines, ("the server wrote a gap line about an item the answer states", t.gap_lines[:3])

    def body(o, c, cid, rec):
        t = ask(c, cid, "מהו השטח הכולל לבנייה בנכס ברחוב הגפן ובאיזה תנאי חלות זכויות הבנייה?", rec)
        reached = {"area": stated(t, TOTAL_AREA), "condition": stated(t, PLAN)}
        rec["info"] = base_info(t, reached, {"area": stage(t, reached["area"]), "condition": t.part_stage(PLAN)})
        rec["info"] |= {"request_component_items": [(c["id"], [(i["state"], i["origin"], i["declared"]) for i in c["items"]])
                                                    for c in t.request_components],
                        "components_with_an_item": sorted({i["component"] for i in t.items})}
        check(rec, "answered", lambda: answered(t))
        check(rec, "cited", lambda: cites_document(t, doc))
        check(rec, "both_components_stated", lambda: both_stated(t))
        check(rec, "items_covered_no_fill_deleted", lambda: items_covered(t))
        check(rec, "no_gap_line", lambda: no_gap_line(t))

    run_sequence("d3_both_components_covered",
                 ("answered", "cited", "both_components_stated", "items_covered_no_fill_deleted", "no_gap_line"), office, body)


def row_given(t: Turn19, row: str) -> bool:
    """A row counts as given when a stretch of the answer that names it states its area and its number of floors (R14)."""
    area, floors = ROW_HEADLINE[row]
    return any(has(s, ROW_LABELS[row]) and has(s, area) and has(s, floors) for s in window(t, area))


def gap_sequence(office, monkeypatch, question: str, name: str) -> None:
    doc = ingest_pdf(office, monkeypatch, ROWS)

    def no_false_line(t: Turn19) -> None:
        """No gap line names a row the answer gives: a "read and not used" line is false for it, and a "partial" line is false
        when every figure it says is missing does stand in the stretch of the answer that gives the row."""
        false = []
        for row in ROW_LABELS:
            if not row_given(t, row):
                continue
            area, _ = ROW_HEADLINE[row]
            given = " ".join(window(t, area))
            for ln in t.gap_lines:
                if not has(ln, ROW_LABELS[row]):
                    continue
                missing = PARTIAL_MISSING.search(ln)
                numbers = re.findall(r"\d[\d,.]*\d|\d", missing.group(1)) if missing else []
                if NOT_USED.search(ln) or (numbers and all(n in given for n in numbers)):
                    false.append((row, ln[:240]))
        assert not false, ("a gap line names a row the answer gives", false)

    def omitted_have_a_line(t: Turn19) -> None:
        """Every item the ledger ends omitted has a line naming its row (an item of no row: some gap line)."""
        missing = []
        for i in t.items:
            if i["state"] != "omitted":
                continue
            rows = [r for r, pat in ROW_LABELS.items() if has(str(i["label"] or ""), pat)]
            ok = any(has(ln, ROW_LABELS[r]) for r in rows for ln in t.gap_lines) if rows else bool(t.gap_lines)
            if not ok:
                missing.append((i["id"], i["label"], i["why"]))
        assert not missing, ("an item left omitted has no line", missing[:3], t.gap_lines)

    def body(o, c, cid, rec):
        t = ask(c, cid, question, rec)
        reached = {f"{row}_given": row_given(t, row) for row in ROW_LABELS}
        rows_missing = [row for row in ROW_LABELS if not row_given(t, row)]
        rec["info"] = base_info(t, reached, {k: stage(t, v) for k, v in reached.items()})
        omitted = [i["label"] for i in t.items if i["state"] == "omitted"]
        rec["info"] |= {"gap_lines": t.gap_lines, "rows_not_given": rows_missing, "omitted_items": omitted,
                        "true_omission_exercised": bool(omitted or rows_missing)}
        check(rec, "answered", lambda: answered(t))
        check(rec, "cited", lambda: cites_document(t, doc))
        check(rec, "no_false_gap_line", lambda: no_false_line(t))
        check(rec, "omitted_items_have_a_line", lambda: omitted_have_a_line(t))

    run_sequence(name, ("answered", "cited", "no_false_gap_line", "omitted_items_have_a_line"), office, body)


def test_d4a_a_row_the_answer_states_gets_no_gap_line_three_times(office, monkeypatch):
    gap_sequence(office, monkeypatch, "מהן זכויות הבנייה בנכס ברחוב הדובדבן?", "d4a_no_false_gap_line_rights_question")


def test_d4b_a_row_the_answer_states_gets_no_gap_line_when_every_figure_is_asked_three_times(office, monkeypatch):
    gap_sequence(office, monkeypatch, "מהם השטח, מספר הקומות והתכנית של כל אחד מרכיבי זכויות הבנייה בנכס ברחוב הדובדבן?",
                 "d4b_gap_lines_every_figure_asked")


def test_d5_report_only_a_claim_of_another_property_on_evidence_that_names_no_property_three_times(office, monkeypatch):
    """REPORT ONLY: the gating check is only that the turn finished. What happened to the second property's value is classified
    and recorded: ``not_claimed`` (the answer does not state it), ``removed`` (a removal of a claim for another property, or a
    removal sentence, and the answer does not state it), ``flagged`` (stated, with a server note beside it) or ``let_through``."""
    doc = ingest_pdf(office, monkeypatch, CALC_NOISE)

    def body(o, c, cid, rec):
        t = ask(c, cid, "מהו שווי הנכס בגישת ההשוואה של מתחם גבעת הברוש?", rec)
        own = figure("9400000")
        reached = {"own_value": stated(t, own), "other_property_value": stated(t, OTHER_VALUE)}
        removals = t.removed_as_another_property
        flags = [s["kind"] for s in t.spans if s["kind"] in FLAG_KINDS]
        if not reached["other_property_value"]:
            outcome = "removed" if removals or REMOVAL_SENTENCE.search(t.markdown) else "not_claimed"
        else:
            outcome = "flagged" if flags else "let_through"
        rec["info"] = base_info(t, reached, {k: stage(t, v) for k, v in reached.items()})
        rec["info"] |= {"other_property_outcome": outcome, "removed_as_another_property": removals, "flag_span_kinds": flags,
                        "removal_sentence_in_answer": bool(REMOVAL_SENTENCE.search(t.markdown)),
                        "other_property_value_cited": cited_next_to(t, OTHER_VALUE, doc)}
        check(rec, "answered", lambda: answered(t))

    run_sequence("d5_report_only_another_property_identityless_evidence", ("answered",), office, body)


def test_d6_a_decimal_comma_and_a_negative_percent_keep_their_values_and_sign_three_times(office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, DECIMAL)

    def yield_kept(t: Turn19) -> None:
        assert stated(t, YIELD), ("the gross yield 13,2% is not stated as 13.2", t.markdown[:400])

    def sign_kept(t: Turn19) -> None:
        places = window(t, CHANGE)
        assert places, ("the annual change is not stated", t.markdown[:400])
        own = [ln for ln in t.stated_lines if has(ln, CHANGE)]
        signed = any(SIGN.search(ln) or DECLINE.search(ln) for ln in own)
        positive = any(RISE.search(ln) and not (SIGN.search(ln) or DECLINE.search(ln)) for ln in own)
        assert signed and not positive, ("the negative change lost its sign", own[:2])

    def no_wrong_rate(t: Turn19) -> None:
        wrong = [ln for ln in t.stated_lines if WRONG_RATE.search(ln)]
        assert not wrong, ("a rate of 132 or 35 percent stands in the answer", wrong[:2])

    def body(o, c, cid, rec):
        t = ask(c, cid, "מהם שיעור התשואה הגולמית, השינוי השנתי במחירי הנכסים באזור ושווי הנכס?", rec)
        reached = {"yield": stated(t, YIELD), "change": stated(t, CHANGE), "value": stated(t, PROPERTY_VALUE)}
        rec["info"] = base_info(t, reached, {n: stage(t, r) for n, r in reached.items()})
        rec["info"] |= {"change_lines": [ln for ln in t.stated_lines if has(ln, CHANGE)]}
        check(rec, "answered", lambda: answered(t))
        check(rec, "cited", lambda: cites_document(t, doc))
        check(rec, "yield_read_as_decimal", lambda: yield_kept(t))
        check(rec, "negative_change_keeps_its_sign", lambda: sign_kept(t))
        check(rec, "property_value_kept", lambda: reached_all({"value": reached["value"]}))
        check(rec, "no_wrong_rate", lambda: no_wrong_rate(t))
        check(rec, "no_removal_sentence", lambda: no_removal_as_another_property(t))

    run_sequence("d6_decimal_comma_negative_percent",
                 ("answered", "cited", "yield_read_as_decimal", "negative_change_keeps_its_sign", "property_value_kept",
                  "no_wrong_rate", "no_removal_sentence"), office, body)


# --- the summary ----------------------------------------------------------------------------------------------------------

def build_summary() -> dict:
    turns = [t for r in RESULTS for t in r.get("turns") or []]
    purposes: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for t in turns:
        for k, n in t["usage"]["purposes"].items():
            purposes[k] = purposes.get(k, 0) + n
        for k, n in t["usage"]["statuses"].items():
            statuses[k] = statuses.get(k, 0) + n
    costs = [t["usage"]["cost_usd"] for t in turns if t["usage"]["cost_usd"] is not None]
    names = list(dict.fromkeys(r["sequence"] for r in RESULTS))
    sequences = {}
    for name in names:
        s = sequence_summary(name)
        sequences[name] = {"passed": s["passed"], "passed_runs": s["passed_runs"],
                           "attempts": [{k: a[k] for k in ("attempt", "outcome", "checks", "failures", "stage", "info")}
                                        for a in s["attempts"]]}
    return {"model": MODEL, "turns": len(turns), "attempts": len(RESULTS), "sequences": sequences,
            "usage_purposes": purposes, "usage_statuses": statuses, "cost_usd": round(sum(costs), 4) if costs else None,
            "model_latency_s": round(sum(t["usage"]["model_latency_ms"] for t in turns) / 1000, 1),
            "wall_s": round(time.perf_counter() - STARTED, 1)}


def teardown_module(module) -> None:  # noqa: ARG001
    if RESULTS:
        flush()
        SUMMARY_FILE.write_text(json.dumps(build_summary(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\nreal_model results: {os.path.relpath(RESULTS_FILE, ROOT)}\nsummary: {os.path.relpath(SUMMARY_FILE, ROOT)}")
