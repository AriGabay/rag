"""Run and score the held-out general question sets (U12, KTD16).

Two sets share this scorer: ``eval/questions_general.yaml`` (answer key ``general_facts`` of
``tests/fixtures/ground_truth.yaml``, documents H*; since the held-out v2 set exists it is the *regression* set)
and ``eval/questions_holdout_v2.yaml`` (answer key ``tests/fixtures/holdout_v2_truth.yaml``, documents K*). Fact,
document and attribute ids never collide between the two keys, so both keys are loaded side by side.

Shared by ``scripts/eval.py --set general|holdout_v2`` (live stack, real or demo provider) and the gate 8
acceptance test (scripted provider). Scoring follows the header of the question files; wording is never scored
(``docs/evaluation/scorer-changes.md`` lists every rule with the requirement it implements):

* the plan facets (task type, relation, main tool) come from the stored turn (``questions.plan`` and
  ``questions.steps``), read through a ``Hooks.plan_of`` callback; a result-changing clarification raised by
  the computation tool counts as the expected clarification (S4), and ``new_question`` / ``topic_change`` are
  equivalent only when the turn's ``cleared`` / ``kept`` expectations check the state they act on (S5);
* a source is "cited" when a claim or an ``[E#]`` marker in the text refers to it; an answer with neither
  (a template list or a computation) cites every source it returns. A page requirement is met by a source
  without pages only when the document has no pages (DOCX);
* a fact value is "stated" when a claim citing its document at an accepted page states it (a locate answer,
  which has no claims, states it in the cited snippet): a number as a whole token or a Hebrew number word
  (1-20, both genders, construct forms) next to the attribute's own words rather than another attribute's;
  a Hebrew value as whole tokens; a descriptive value by its own numbers and month names plus a word of the
  document's sentence; a boolean or "none" value with the right polarity (a general Hebrew negation check);
* a computed figure is the reviewed figure when there is one, else the separately labeled preliminary
  figure (model-extracted values are preliminary until a person reviews them, KTD9); coverage is checked per
  document through the answer's ``audit`` block, with a count-based fallback when an answer has none.

Nothing here imports application code; expected values come from the answer-key YAML files.
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
from eval.truth import USERS, Filters, docs, expected_stats, holdout_v2, truth

QUESTIONS = Path(__file__).resolve().parent / "questions_general.yaml"
QUESTIONS_V2 = Path(__file__).resolve().parent / "questions_holdout_v2.yaml"
SETS = {"general": QUESTIONS, "holdout_v2": QUESTIONS_V2}
# how each set is labeled in summaries and reports: the first held-out set is now the regression set
SET_LABELS = {"general": "regression", "holdout_v2": "held-out v2"}
ANSWERED = ("numeric", "combined", "content")
PRICE_KEYS = ("data_kind", "date_field", "area_type", "property_type", "vat_basis")
NUMERIC_FIELDS = ("record_count", "mean_price_per_sqm", "weighted_price_per_sqm", "median_price_per_sqm",
                  "min_price_per_sqm", "max_price_per_sqm")
UNITS = {"m2": "sqm", "count": "unit", "m": "m", "year": "year", "percent": "percent"}
COMPUTE_TOOLS = ("compute_records", "extract_and_compute")
EQUIVALENT_RELATIONS = frozenset({"new_question", "topic_change"})  # apply_turn resets the context for both
# audit states of a document whose value is an observation: counted, merged with a duplicate, or observed but
# outside a value filter ("> 2010" for a count: still one of the n observations)
OBSERVED_STATES = frozenset({"used", "duplicate", "filtered_out"})
# a document the key expects as a value may also hold it in a cross-document conflict (KTD8 step 6)
USED_STATES = OBSERVED_STATES | {"conflict"}
NOT_STATING_STATES = frozenset({"not_stated", "not_yet_extracted", "partial_scan"})
GAP_STATES = frozenset({"awaiting_review", "not_yet_extracted", "partial_scan"})
NOT_RUN_CLOUD_OFF = ("not run: needs cloud use off for the office; the live office A is in cloud mode and the "
                     "evaluation never changes office settings")
_Q2 = Decimal("0.01")


# --- answer keys --------------------------------------------------------------------------------------

@cache
def general() -> dict:
    """The first held-out answer key (``ground_truth.yaml`` → ``general_facts``)."""
    return truth()["general_facts"]


@cache
def answer_keys() -> tuple[dict, ...]:
    return (general(), holdout_v2())


@cache
def facts() -> dict[str, dict]:
    return {f["id"]: f for key in answer_keys() for f in key["facts"]}


@cache
def general_docs() -> dict[str, dict]:
    return {d["id"]: d for key in answer_keys() for d in key["documents"]}


@cache
def attribute_labels() -> dict[str, str]:
    return {name: a["label_he"] for key in answer_keys() for name, a in key["attributes"].items()}


@cache
def conflicts() -> tuple[dict, ...]:
    return tuple(c for key in answer_keys() for c in key.get("conflicts") or [])


@cache
def versions() -> tuple[dict, ...]:
    return tuple(v for key in answer_keys() for v in key.get("versions") or [])


@cache
def replaced_docs() -> frozenset[str]:
    """Superseded versions (H4, K3, D1): each key's ``versions[].replaces`` and every ``version_of``."""
    out = {v["replaces"] for v in versions() if v.get("replaces")}
    out |= {d["version_of"] for d in list(general_docs().values()) + list(docs().values()) if d.get("version_of")}
    return frozenset(out)


def any_doc(doc_id: str | None) -> dict | None:
    return general_docs().get(doc_id or "") or docs().get(doc_id or "")


def has_pages(doc_id: str | None) -> bool:
    """False only for a document the answer key knows to have no physical pages (DOCX)."""
    d = any_doc(doc_id)
    return d is None or d.get("page_count") is not None


@cache
def filename_to_doc() -> dict[str, str]:
    """Fixture file name -> answer-key document id (D*/DB*, held-out H* and held-out v2 K* documents)."""
    out = {d["filename"]: d["id"] for d in truth()["documents"]}
    out.update({d["filename"]: d["id"] for d in general_docs().values()})
    return out


def doc_scope(doc_id: str) -> tuple[str, str]:
    """(office, group code) of an answer-key document; a new version inherits its document's group."""
    d = any_doc(doc_id)
    base = d.get("version_of")
    if base:
        d = any_doc(base)
    return d["office"], d["group"]


def load_items(path: Path | str = QUESTIONS) -> list[dict]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))["items"]


def ae_tags(item: dict) -> list[str]:
    """Acceptance examples an item reproduces, from its notes (``AE1`` ... ``AE8``)."""
    notes = " ".join(str(x) for x in [item.get("note", "")] + [t["expect"].get("note", "") for t in item["turns"]])
    return sorted(set(re.findall(r"\bAE\d\b", notes)))


def effective(expect: dict) -> dict:
    """The expectations a correct system meets: ``result`` / ``values`` / ``coverage`` with the turn's
    ``settled`` block applied. ``result.facts`` and ``coverage`` outside ``settled`` record what the documents
    state (recomputed by tests/unit/test_fixtures_ground_truth.py); ``settled`` records the per-document state
    the product's settled extraction decisions require (for example a value that must wait for review)."""
    s = expect.get("settled")
    out = dict(expect)
    out["_document_facts"] = list((expect.get("result") or {}).get("facts") or [])
    if not s:
        return out
    if "result" in s:
        out["result"] = {**(expect.get("result") or {}), **s["result"]}
    if "values" in s:
        out["values"] = s["values"]
    if "coverage" in s:
        out["coverage"] = {**(expect.get("coverage") or {}), **s["coverage"]}
    return out


# --- hooks into the system under test ----------------------------------------------------------------

@dataclass
class Hooks:
    """How the runner reaches the system: an HTTP client per user, the answer-key document of a source,
    the stored plan of a question, the document ids a user must never see, and the review-queue state."""

    client: Callable[[str], object]
    doc_of: Callable[[dict], str | None]  # source or audit entry -> answer-key document id (H1, H4v2, K3v2, ...)
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
    previous: dict | None = None  # the previous turn's final answer in the same conversation (meta turns)

    def raw(self) -> dict:
        """Everything scoring reads, so a run can be scored again offline (``from_raw``)."""
        f = self.flow
        return {"answer": f.answer, "transcript": f.transcript, "conversation_id": f.conversation_id,
                "clarifications": f.clarifications, "latencies_ms": f.latencies_ms, "question_ids": f.question_ids,
                "error": f.error, "plan": self.plan, "steps": self.steps, "route": self.route, "before": self.before,
                "after": self.after, "resubmit": self.resubmit, "resubmit_after": self.resubmit_after,
                "previous": self.previous}

    @classmethod
    def from_raw(cls, raw: dict) -> TurnRun:
        flow = Flow(answer=raw["answer"], conversation_id=raw["conversation_id"], transcript=raw["transcript"],
                    clarifications=raw["clarifications"], latencies_ms=raw["latencies_ms"],
                    question_ids=raw["question_ids"], error=raw["error"])
        return cls(flow, raw["plan"], raw["steps"], raw["route"], raw["before"], raw["after"], raw["resubmit"],
                   raw["resubmit_after"], raw.get("previous"))


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
                      (stored or {}).get("parse_route"), before, after,
                      previous=runs[-1].flow.answer if runs else None)
        if expect.get("resubmit_same_turn"):
            data, _ms = post_ask(client, {"question": turn["ask"], "conversation_id": conversation,
                                          "turn_id": turn_id})
            run.resubmit = data.get("answer", data)
            run.resubmit_after = _conversation(client, conversation or "")
        runs.append(run)
        before = after
    return runs


# --- text matching ------------------------------------------------------------------------------------

def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _norm_he(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[׳'’`]", "׳", re.sub(r"[״\"”“]", "״", s or ""))).strip()


@dataclass(frozen=True)
class Tok:
    kind: str  # num | he | lat | kv (":" or "|") | sep (sentence or list break)
    text: str
    value: Decimal | None = None


# a number is a whole token: never the digits of "m2" (glued to a Latin letter) or of a longer number
_TOKEN = re.compile(r"(?P<num>(?<![A-Za-z\d.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?!\d))"
                    r"|(?P<he>[א-ת][א-ת״׳]*)|(?P<lat>[A-Za-z]+)|(?P<kv>[:|])|(?P<sep>[.;!?\n•])")


@cache
def _tokens(text: str) -> tuple[Tok, ...]:
    out = []
    for m in _TOKEN.finditer(_norm_he(text) if text else ""):
        kind = m.lastgroup
        raw = m.group(kind)
        if kind == "num":
            out.append(Tok("num", raw, _dec(raw).normalize()))
        elif kind == "he":
            out.append(Tok("he", raw.rstrip("״")))
        elif kind == "lat":
            out.append(Tok("lat", raw.lower()))
        else:
            out.append(Tok(kind, raw))
    return tuple(out)


def numbers_in(text: str) -> set[Decimal]:
    """Every number written in ``text``: digit tokens and Hebrew number words."""
    return {v for _, v in _number_hits(_tokens(text or ""))}


# Hebrew number words 1-20: both genders, construct forms; never with the article (השני / השנייה are ordinals)
_NUM_WORDS = {
    "אפס": 0, "אחד": 1, "אחת": 1, "שניים": 2, "שתיים": 2, "שני": 2, "שתי": 2,
    "שלושה": 3, "שלוש": 3, "שלושת": 3, "שלשה": 3, "שלש": 3, "ארבעה": 4, "ארבע": 4, "ארבעת": 4,
    "חמישה": 5, "חמשה": 5, "חמש": 5, "חמשת": 5, "שישה": 6, "ששה": 6, "שש": 6, "ששת": 6,
    "שבעה": 7, "שבע": 7, "שבעת": 7, "שמונה": 8, "שמונת": 8, "תשעה": 9, "תשע": 9, "תשעת": 9,
    "עשרה": 10, "עשר": 10, "עשרת": 10, "עשרים": 20,
}
_TEEN_HEADS = {**{w: v for w, v in _NUM_WORDS.items() if 1 <= v <= 9}, "שנים": 2, "שתים": 2}
_TEEN_TAILS = ("עשר", "עשרה")
_NUM_PREFIXES = "ובלכמש"


def _num_word(word: str, table: dict[str, int]) -> int | None:
    if word in table:
        return table[word]
    for k in (1, 2):
        if len(word) > k + 1 and all(c in _NUM_PREFIXES for c in word[:k]) and word[k:] in table:
            return table[word[k:]]
    return None


def _number_hits(toks: tuple[Tok, ...]) -> list[tuple[int, Decimal]]:
    """(token index, value) of every number: a digit token or a Hebrew number word (11-19 as two words)."""
    hits, i = [], 0
    while i < len(toks):
        t = toks[i]
        if t.kind == "num":
            hits.append((i, t.value))
        elif t.kind == "he":
            head = _num_word(t.text, _TEEN_HEADS)
            if head is not None and i + 1 < len(toks) and toks[i + 1].kind == "he" and toks[i + 1].text in _TEEN_TAILS:
                hits.append((i, Decimal(10 + head)))
                i += 2
                continue
            v = _num_word(t.text, _NUM_WORDS)
            if v is not None:
                hits.append((i, Decimal(v)))
        i += 1
    return hits


_PREFIX = "והבלמשכ"
_SUFFIXES = ("ות", "ים", "ת", "ה", "י")


@cache
def _stems(word: str) -> frozenset[str]:
    """Rough Hebrew word forms: up to two one-letter prefixes and a plural/feminine suffix removed."""
    w = word.strip("׳״")
    forms = {w}
    for k in (1, 2):
        if len(w) - k >= 3 and all(c in _PREFIX for c in w[:k]):
            forms.add(w[k:])
    out = set(forms)
    for f in forms:
        for suf in _SUFFIXES:
            if f.endswith(suf) and len(f) - len(suf) >= 3:
                out.add(f[:-len(suf)])
    return frozenset(out)


def _same_word(a: str, b: str) -> bool:
    return bool(_stems(a) & _stems(b))


# measure and filler words that never tell one attribute from another
GENERIC = frozenset({"שטח", "מספר", "קיום", "גובה", "שנת", "שנה", "בניין", "דירה", "נכס", "סך", "הכול", "כולל",
                     "של", "שיעור", "עד", "מ״ר", "למ״ר", "ס״מ", "מטר", "מ׳", "בגין", "לפי", "על", "את", "עם",
                     "הוא", "היא", "זה", "זו", "כי", "אשר", "או", "גם", "בין", "מאז", "בה", "בו", "יש", "הנכס"})
NEGATIONS = frozenset({"אין", "אינו", "אינה", "אינם", "אינן", "איננו", "איננה", "לא", "ללא", "בלי", "היעדר",
                       "בהיעדר", "טרם"})
_NEG_PREFIXES = ("", "ו", "ש", "וש", "כש", "מש")
MONTHS = {"january": "ינואר", "february": "פברואר", "march": "מרץ", "april": "אפריל", "may": "מאי",
          "june": "יוני", "july": "יולי", "august": "אוגוסט", "september": "ספטמבר", "october": "אוקטובר",
          "november": "נובמבר", "december": "דצמבר"}
_EN_NEGATION = re.compile(r"\b(?:not|no|none|without)\b", re.I)


def _is_neg(t: Tok) -> bool:
    return t.kind == "he" and any(t.text.startswith(p) and t.text[len(p):] in NEGATIONS for p in _NEG_PREFIXES)


def _generic(word: str) -> bool:
    return bool(_stems(word) & GENERIC) or word in GENERIC


def _words(text: str) -> list[str]:
    """Hebrew content words of ``text`` (no generic, negation or number words)."""
    out = []
    for t in _tokens(text or ""):
        if t.kind != "he" or len(t.text) < 3 or _generic(t.text) or _is_neg(t):
            continue
        if _num_word(t.text, _NUM_WORDS) is not None or any(_same_word(t.text, m) for m in MONTHS.values()):
            continue
        out.append(t.text)
    return out


def naming_words(fact: dict, terms: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Words that name the fact's attribute: its Hebrew label and the item's topic terms."""
    label = attribute_labels().get(fact.get("attribute") or "", "")
    return tuple(dict.fromkeys(_words(label) + [w for term in terms for w in _words(term)]))


@cache
def _other_words(attribute: str, own: tuple[str, ...]) -> tuple[str, ...]:
    """Words that name only other attributes of the answer keys (never one of ``own``)."""
    out = []
    for name, label in attribute_labels().items():
        if name == attribute:
            continue
        out += [w for w in _words(label) if not any(_same_word(w, o) for o in own)]
    return tuple(dict.fromkeys(out))


def _names(t: Tok, words) -> bool:
    return t.kind == "he" and any(_same_word(t.text, w) for w in words)


def _belongs(toks: tuple[Tok, ...], i: int, own, other) -> bool:
    """The number at ``i`` is not about another attribute: the nearest attribute word in its sentence (before
    it, else just after it) is not a word that names only another attribute."""
    for rng in (range(i - 1, max(-1, i - 9), -1), range(i + 1, min(len(toks), i + 4))):
        for j in rng:
            t = toks[j]
            if t.kind == "sep":
                break
            if _names(t, own):
                return True
            if _names(t, other):
                return False
    return True


def _negated_before(toks: tuple[Tok, ...], i: int) -> bool:
    """A negation shortly before the word at ``i``, in its sentence."""
    for j in range(i - 1, max(-1, i - 5), -1):
        if toks[j].kind == "sep":
            break
        if _is_neg(toks[j]):
            return True
    return False


def _negated_after(toks: tuple[Tok, ...], i: int) -> bool:
    """A table cell "<word>: אין" / "<word> | אין", or a clause ending "<word> אין"."""
    if i + 2 < len(toks) and toks[i + 1].kind == "kv" and _is_neg(toks[i + 2]):
        return True
    return i + 1 < len(toks) and _is_neg(toks[i + 1]) and (i + 2 == len(toks) or toks[i + 2].kind in ("sep", "kv"))


def _negated(toks: tuple[Tok, ...], i: int) -> bool:
    return _negated_before(toks, i) or _negated_after(toks, i)


def _phrases(occ: list[int]) -> list[tuple[int, int]]:
    """Runs of consecutive attribute words ("זיקת הנאה") as (first, last) token indexes."""
    out: list[tuple[int, int]] = []
    for i in occ:
        if out and out[-1][1] == i - 1:
            out[-1] = (out[-1][0], i)
        else:
            out.append((i, i))
    return out


def polarity_stated(fact: dict, text: str, absent: bool, terms: tuple[str, ...] = ()) -> bool:
    """``text`` states the fact's presence (``absent`` False) or absence (True): the document's own sentence,
    else the polarity of the phrases that name the attribute (each negated as a whole), else of the whole
    text."""
    quote = _norm_he(fact.get("quote") or "")
    if quote and len(quote) > 3 and quote in _norm_he(text):
        return True
    toks = _tokens(text or "")
    words = naming_words(fact, terms)
    occ = [i for i, t in enumerate(toks) if _names(t, words)]
    if occ:
        neg = [_negated_before(toks, first) or _negated_after(toks, last) for first, last in _phrases(occ)]
        return any(neg) if absent else not all(neg)
    has_neg = any(_is_neg(t) for t in toks)
    return has_neg if absent else not has_neg


def _number_stated(fact: dict, toks: tuple[Tok, ...], cands: set[Decimal], terms, associate: bool = True) -> bool:
    own = naming_words(fact, terms)
    other = _other_words(fact.get("attribute") or "", own) if associate else ()
    return any(v in cands and _belongs(toks, i, own, other) for i, v in _number_hits(toks))


def _sequence_stated(value: str, text: str) -> bool:
    """Every token of the Hebrew value, in order and whole (a prefix allowed on the first): "מגורים א׳" is not
    "מגורים אחרים"."""
    want = [t.text.strip("׳") for t in _tokens(value) if t.kind in ("he", "num", "lat")]
    have = [t.text.strip("׳") for t in _tokens(text or "") if t.kind in ("he", "num", "lat")]
    if not want:
        return False
    for j in range(len(have) - len(want) + 1):
        first = have[j]
        if not (first == want[0] or (len(first) - len(want[0]) in (1, 2) and first.endswith(want[0])
                                     and all(c in _PREFIX for c in first[:len(first) - len(want[0])]))):
            continue
        if have[j + 1:j + len(want)] == want[1:]:
            return True
    return False


def _descriptive_stated(fact: dict, text: str, terms) -> bool:
    """A descriptive key value ("approved (May 2023)", "60 m2 main area", "2022: kitchen"): its own numbers (as
    digits or words, never the 2 of "m2"), its month names, a word of the document's sentence (not negated for
    a positive value) and a negation for a negative value ("deposited, not yet approved")."""
    s = str(fact["value"])
    toks = _tokens(text or "")
    found = numbers_in(text)
    nums = {t.value for t in _tokens(s) if t.kind == "num"}
    norm = _dec((fact.get("normalized") or {}).get("value"))
    for n in nums:
        if n not in found and not (norm is not None and norm.normalize() in found and len(nums) == 1):
            return False
    for en, he in MONTHS.items():
        if re.search(rf"\b{en}\b", s, re.I) and not any(t.kind == "he" and _same_word(t.text, he) for t in toks):
            return False
    words = _words(fact.get("quote") or "")
    negative = bool(_EN_NEGATION.search(s))
    if negative and not any(_is_neg(t) for t in toks):
        return False
    if words:
        occ = [i for i, t in enumerate(toks) if _names(t, words)]
        if not occ or (not negative and all(_negated(toks, i) for i in occ)):
            return False
    return bool(nums) or bool(words) or negative


def fact_value_stated(fact: dict, text: str, terms: tuple[str, ...] = ()) -> bool:
    value = fact["value"]
    if isinstance(value, bool) or value is None or value == "none":
        return polarity_stated(fact, text, absent=value is not True, terms=terms)
    s = str(value)
    toks = _tokens(text or "")
    if isinstance(value, int | float) or re.fullmatch(r"\d[\d,]*(?:\.\d+)?", s):
        cands = {_dec(s).normalize()}
        if (fact.get("normalized") or {}).get("value") is not None:
            cands.add(_dec(fact["normalized"]["value"]).normalize())
        if _number_stated(fact, toks, cands, terms):
            return True
        return Decimal(0) in cands and polarity_stated(fact, text, absent=True, terms=terms)  # "אין" states 0
    if re.fullmatch(r"\d+(?:\.\d+)?x\d+(?:\.\d+)?", s):  # dimensions: the normalized area, or both dimensions
        found = numbers_in(text)
        norm = _dec((fact.get("normalized") or {}).get("value"))
        sides = {Decimal(x).normalize() for x in s.split("x")}
        return (norm is not None and norm.normalize() in found) or sides <= found
    if re.fullmatch(r"[A-Za-z]", s):  # a rating letter
        return any(t.kind == "lat" and t.text == s.lower() for t in toks)
    if re.search(r"[א-ת]", s):
        return _sequence_stated(s, text)
    return _descriptive_stated(fact, text, terms)


def without_addresses(text: str, question: str) -> str:
    """``text`` without the "<street> <number>" phrases the question names, so a house number is never read
    as the value ("האירוסים 12" when the value is 12)."""
    for word, num in re.findall(r"([א-ת][א-ת״׳\"']+)\s+(\d+)", question or ""):
        text = re.sub(rf"[א-ת]?{re.escape(word)}\s+{num}(?!\d)", " ", text)
    return text


# --- citations ----------------------------------------------------------------------------------------

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


def _page_ok(want: int | None, source: dict, doc: str | None) -> bool:
    """``want`` None accepts any page; a source without pages meets a page only for a page-less document."""
    if want is None:
        return True
    pages = source.get("page_list") or []
    return want in pages if pages else not has_pages(doc)


def _cites(sources: list[dict], doc_of, doc: str, page: int | None | set) -> bool:
    pages = page if isinstance(page, set) else {page}
    return any(doc_of(s) == doc and any(_page_ok(p, s, doc) for p in pages) for s in sources)


def allowed_pages(fact: dict, src: dict) -> set:
    """The fact's page plus every page the answer key accepts for its document (``all_of`` / ``also_valid``)."""
    if fact.get("page") is None:
        return {None}
    out = {fact["page"]}
    for s in (src.get("all_of") or []) + (src.get("also_valid") or []):
        if s["doc"] == fact["document"]:
            out.add(s["page"])
    return out


def claim_states(answer: dict, doc_of, fact: dict, question: str = "", terms: tuple[str, ...] = (),
                 pages: set | None = None, snippets: bool = False) -> bool:
    """Some claim citing the fact's document (at an accepted page) states the value. With ``snippets`` an answer
    without claims (a locate list) states it in the snippet of a cited source of that document."""
    pages = pages if pages is not None else {fact.get("page")}
    by_id = {s.get("evidence_id"): s for s in answer.get("sources") or []}
    claims = answer.get("claims") or []
    for c in claims:
        cited = [by_id[e] for e in c.get("evidence_ids") or [] if e in by_id]
        if _cites(cited, doc_of, fact["document"], pages) and \
                fact_value_stated(fact, without_addresses(c.get("text") or "", question), terms):
            return True
    if snippets and not claims:
        for s in cited_sources(answer):
            if _cites([s], doc_of, fact["document"], pages) and \
                    fact_value_stated(fact, without_addresses(s.get("snippet") or "", question), terms):
                return True
    return False


def _claims_value_or_quote(answer: dict, doc_of, fact: dict, question: str, terms) -> bool:
    """A claim citing the fact's document gives its value, or every number of its quote (a quote with two
    values, "12 מ״ר ... 6 מ״ר", whose sum the key records)."""
    if claim_states(answer, doc_of, fact, question, terms, pages={None}):
        return True
    nums = {t.value for t in _tokens(fact.get("quote") or "") if t.kind == "num"}
    if len(nums) < 2 or _dec(fact["value"]) in nums:  # only a quote whose values the key adds up
        return False
    by_id = {s.get("evidence_id"): s for s in answer.get("sources") or []}
    for c in answer.get("claims") or []:
        cited = [by_id[e] for e in c.get("evidence_ids") or [] if e in by_id]
        if _cites(cited, doc_of, fact["document"], None) and \
                nums <= numbers_in(without_addresses(c.get("text") or "", question)):
            return True
    return False


# --- figures and state --------------------------------------------------------------------------------

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


def _doc_pages(sources: list[dict], doc_of) -> set[tuple]:
    return {(doc_of(s), p) for s in sources for p in (s.get("page_list") or [None])}


def _tool_clarification(expect: dict, plan: dict, executed: list, planned: list, a: dict) -> bool:
    """S4: the expected clarification raised by the computation tool of a ``compute`` plan (a result-changing
    clarification, R20): same key, no figure; only the plan's task label differs."""
    return (expect.get("task_type") == "clarify" and expect.get("outcome") == "clarification"
            and plan.get("task_type") in ("compute", "compute_explain") and a.get("kind") == "clarification"
            and (a.get("clarification") or {}).get("key") == expect.get("clarify_key")
            and any(t in COMPUTE_TOOLS for t in executed + planned) and not figure_of(a).n)


def score_turn(hooks: Hooks, item: dict, index: int, run: TurnRun) -> dict:
    turn = item["turns"][index]
    expect = effective(turn["expect"])
    terms = tuple(item.get("terms") or ())
    ask = turn["ask"]
    user = item.get("user", "admin-a@demo.test")
    office, groups = USERS[user]
    a = run.flow.answer
    kind = a.get("kind", "error")
    plan = (run.plan or {}).get("turn_plan") or {}
    sc = Score()
    doc_of = hooks.doc_of
    sources = a.get("sources") or []
    cited = cited_sources(a)
    if run.flow.error:
        sc.check("flow", False, run.flow.error)
    if kind == "error":
        sc.check("flow", False, f"HTTP {a.get('status')}: {str(a.get('detail'))[:120]}")

    # interpretation facets
    executed = [s.get("tool") for s in run.steps]
    planned = [s.get("tool") for s in plan.get("steps") or []]
    if "task_type" in expect:
        got = plan.get("task_type")
        sc.check("task_type", got == expect["task_type"] or _tool_clarification(expect, plan, executed, planned, a),
                 f"{got} (expected {expect['task_type']})")
    if "relation" in expect:
        got, want = plan.get("turn_relation"), expect["relation"]
        same = got == want or (got in EQUIVALENT_RELATIONS and want in EQUIVALENT_RELATIONS
                               and bool(expect.get("cleared") or expect.get("kept")))
        sc.check("relation", same, f"{got} (expected {want})")
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

    # sources: recall for every outcome; precision for content answers. A meta turn ("show the source", R19)
    # is scored on re-showing the previous turn's sources, not on retrieving them again (S10).
    src = expect.get("sources") or {}
    all_of = src.get("all_of") or []
    also = src.get("also_valid") or []
    meta = (expect.get("tool") == "show_sources" or expect.get("relation") == "meta_sources") and \
        run.previous is not None
    if meta:
        before_pages, now_pages = _doc_pages(cited_sources(run.previous), doc_of), _doc_pages(sources, doc_of)
        sc.check("sources", before_pages == now_pages,
                 f"previous turn's sources not re-shown: missing {sorted(before_pages - now_pages, key=str)}, "
                 f"added {sorted(now_pages - before_pages, key=str)}")
    elif all_of and out != "abstain":
        missing = [f"{s['doc']} p{s['page']}" for s in all_of if not _cites(cited, doc_of, s["doc"], s["page"])]
        sc.check("sources", not missing, f"not cited: {', '.join(missing)} | cited: {_sources_brief(cited, doc_of)}")
    if not meta and out == "answer" and kind in ANSWERED and (all_of or also):
        allowed = all_of + also
        extra = [s for s in cited if not any(doc_of(s) == w["doc"] and _page_ok(w["page"], s, w["doc"])
                                             for w in allowed)]
        sc.check("source_precision", not extra, f"cited outside the answer key: {_sources_brief(extra, doc_of)}")

    # values
    if out in ("answer", "comparison") and kind in ANSWERED:
        template = expect.get("task_type") == "locate" and not a.get("claims") and not a.get("dropped_claims")
        for v in expect.get("values") or []:
            f = facts()[v["fact"]]
            pages = allowed_pages(f, src)
            if not _cites(cited, doc_of, f["document"], pages):
                sc.check("values", False, f"{v['fact']} ({f['document']} p{sorted(pages, key=str)}) not cited")
            elif "value" in v and not claim_states(a, doc_of, f | {"value": v["value"]}, ask, terms, pages, template):
                sc.check("values", False, f"{v['fact']} value {v['value']!r} not stated in a "
                         f"{'claim' if not template else 'claim or cited snippet'} citing {f['document']}")
    if out == "comparison" and kind in ANSWERED and expect.get("changed"):
        _score_changed(sc, expect, a, doc_of, ask, terms)
    if out == "computation" and kind in ("numeric", "combined"):
        _score_computation(sc, expect, a, fig, sources, doc_of, ask, terms)
    if expect.get("conflict") and kind in ANSWERED:
        _score_conflict(sc, expect, a, doc_of, ask, terms)
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
        "ae": ae_tags(item), "user": user, "question": ask, "expected": out, "got": kind,
        "task_type": plan.get("task_type"), "expected_task_type": expect.get("task_type"),
        "relation": plan.get("turn_relation"), "tools": executed, "planned_tools": planned, "route": run.route,
        "plan_attribute": (plan.get("attribute") or {}).get("description"), "plan_metric": plan.get("metric"),
        "search_queries": plan.get("search_queries"), "provider": a.get("provider"), "mode": a.get("mode"),
        "cached": bool(a.get("cached")), "partial": bool(a.get("partial")),
        "pending_extraction": a.get("pending_extraction"), "abstention_kind": a.get("abstention_kind"),
        "clarifications": run.flow.clarifications, "figure": str(fig.value) if fig.value is not None else None,
        "figure_n": fig.n, "figure_tier": fig.tier, "operation": fig.operation, "coverage": cov,
        "audit": bool(a.get("audit")),
        "sources": [f"{doc_of(s) or '?'} p{s.get('page_list')}" for s in sources],
        "cited": [f"{doc_of(s) or '?'} p{s.get('page_list')}" for s in cited],
        "dropped_claims": a.get("dropped_claims"), "limitations": a.get("limitations") or [],
        "latencies_ms": run.flow.latencies_ms, "total_ms": sum(run.flow.latencies_ms),
        "question_ids": run.flow.question_ids, "conversation_id": run.flow.conversation_id,
        "facets": sc.facets, "reasons": sc.reasons, "ok": not sc.reasons, "text": (a.get("text") or "")[:600],
        "raw": run.raw(),
    }


def _statement_fact(st: dict, attribute: str) -> dict:
    """A conflict or version statement as a fact (its answer-key fact when it names one)."""
    base = facts().get(st.get("fact") or "") or {}
    return base | {"document": st["document"], "page": st.get("page"), "value": st["value"],
                   "quote": st.get("quote") or base.get("quote") or "", "attribute": attribute}


def _score_changed(sc: Score, expect: dict, a: dict, doc_of, ask: str, terms) -> None:
    """L3: every changed assumption the key names is stated for both versions, each in a claim citing its own
    version (old value citing the old version, new value citing the new one)."""
    side_docs = {s["doc"] for s in expect.get("sides") or []}
    ver = next((v for v in versions() if {v["document"], v["replaces"]} == side_docs), None)
    if ver is None:
        sc.check("changed", False, f"no answer-key version pair for sides {sorted(side_docs)}")
        return
    for ch in ver["changed"]:
        if ch["attribute"] not in expect["changed"]:
            continue
        for side in ("old", "new"):
            f = _statement_fact(ch[side], ch["attribute"])
            if not claim_states(a, doc_of, f, ask, terms, {f["page"]}):
                sc.check("changed", False, f"{ch['attribute']}: {side} value {f['value']!r} not stated in a claim "
                         f"citing {f['document']}")


def _score_conflict(sc: Score, expect: dict, a: dict, doc_of, ask: str, terms) -> None:
    """L6: both conflicting statements are shown, each from a cited source: a claim citing the document states
    its value, or the computation lists that document's value (a value source or the audit)."""
    src = expect.get("sources") or {}
    wanted = {s["doc"] for s in (src.get("all_of") or []) + (src.get("also_valid") or [])}
    wanted |= {s["doc"] for s in expect.get("sides") or []}
    wanted |= {facts()[v["fact"]]["document"] for v in expect.get("values") or []}
    conflict = next((c for c in conflicts() if {st["document"] for st in c["statements"]} <= wanted), None)
    if conflict is None:
        sc.check("conflict", False, f"no answer-key conflict among {sorted(wanted)}")
        return
    audit = {doc_of(e): e for e in (a.get("audit") or {}).get("documents") or [] if e.get("value") is not None}
    for st in conflict["statements"]:
        f = _statement_fact(st, conflict["attribute"])
        want = _dec(st["value"])
        listed = any(doc_of(s) == f["document"] and _dec(s.get("value")) == want for s in a.get("sources") or []
                     if s.get("value") is not None) or (f["document"] in audit and
                                                       _dec(audit[f["document"]]["value"]) == want)
        if not (listed or claim_states(a, doc_of, f, ask, terms, {f["page"]})):
            sc.check("conflict", False, f"{f['document']} value {st['value']} not shown from a cited source")


def _audit_value_ok(fact: dict, value) -> bool:
    """The value the audit reports for a used document is the fact's value (numbers compared as Decimals)."""
    want = fact["value"]
    if isinstance(want, bool) or want is None or value is None:
        return True
    got = _dec(value)
    cands = {_dec(want)} | {_dec((fact.get("normalized") or {}).get("value"))}
    cands.discard(None)
    if got is not None and cands:
        return got.normalize() in {c.normalize() for c in cands}
    if re.search(r"[א-ת]", str(want)):
        return _sequence_stated(str(want), str(value))
    return True


def _score_computation(sc: Score, expect: dict, a: dict, fig: Figure, sources: list[dict], doc_of, ask: str,
                       terms) -> None:
    res = expect.get("result")
    if not res:
        return
    metric = res.get("metric")
    if fig.operation is not None and metric is not None:
        sc.check("result", fig.operation == metric, f"operation {fig.operation} (expected {metric})")
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
    not_stated = list(cov.get("not_stated_includes") or [])
    no_value = not_stated + list(cov.get("stated_absent") or []) + list(cov.get("mentioned_without_value") or [])
    awaiting = list(cov.get("awaiting_review") or [])
    as_value = sorted({doc_of(s) for s in value_sources} & (set(no_value) | set(awaiting) | replaced_docs()))
    sc.check("coverage", not as_value, f"counted as a value: {as_value}")
    used = {facts()[f]["document"]: facts()[f] for f in res.get("facts") or []}
    audit = a.get("audit")
    if audit:
        _score_audit(sc, audit, doc_of, used, not_stated, no_value, awaiting)
    else:  # no audit: the counts are all there is (L2 fallback)
        if cov.get("not_stated_includes") and got:
            unresolved = sum(got.get(k, 0) or 0 for k in ("not_stated", "not_yet_extracted", "pending", "failed",
                                                          "partial_scan", "awaiting_review"))
            sc.check("coverage", unresolved >= len(cov["not_stated_includes"]),
                     f"{unresolved} documents reported without a value (expected >= {len(cov['not_stated_includes'])})")
        if awaiting and got:
            sc.check("coverage", (got.get("awaiting_review") or 0) >= len(awaiting),
                     f"{got.get('awaiting_review') or 0} values awaiting review (expected >= {len(awaiting)}: "
                     f"{', '.join(awaiting)})")
    _score_contradiction(sc, expect, a, doc_of, value_sources, got, ask, terms)


def _score_audit(sc: Score, audit: dict, doc_of, used: dict[str, dict], not_stated: list[str], no_value: list[str],
                 awaiting: list[str]) -> None:
    """Coverage per document from the answer's audit block (L2, S8, S9). ``not_stated`` documents must be listed
    as not stating the datum (or not yet extracted / partly read); every ``no_value`` document (also a stated
    absence or a mention without a value, which may be outside the scope or lack metadata) is never a value."""
    states: dict[str | None, set[str]] = {}
    values: dict[str | None, list] = {}
    for e in audit.get("documents") or []:
        d = doc_of(e)
        states.setdefault(d, set()).add(e.get("state"))
        if e.get("state") in USED_STATES:
            values.setdefault(d, []).append(e.get("value"))
    expected_used = set(used)
    for d in sorted(expected_used):
        st = states.get(d, set())
        if not st & USED_STATES:
            sc.check("coverage", False, f"{d} not used (audit state {sorted(st) or 'absent'})")
            continue
        fact = used[d]
        bad = [v for v in values.get(d, []) if not _audit_value_ok(fact, v)]
        sc.check("coverage", not bad, f"{d} used with value {bad} (expected {fact['value']})")
    for d in sorted(awaiting):
        st = states.get(d, set())
        sc.check("coverage", st == {"awaiting_review"},
                 f"{d} must be reported as awaiting review (audit state {sorted(st) or 'absent'})")
    for d in sorted(set(not_stated)):
        st = states.get(d, set())
        sc.check("coverage", bool(st) and st <= NOT_STATING_STATES,
                 f"{d} must be reported as not stating the datum (audit state {sorted(st) or 'absent'})")
    for d in sorted(set(no_value) | replaced_docs()):
        st = states.get(d, set())
        sc.check("coverage", not st & OBSERVED_STATES, f"{d} counted as a value (audit state {sorted(st)})")
    extra = sorted(d for d, st in states.items() if d is not None and st & OBSERVED_STATES
                   and d not in expected_used and d not in set(no_value) | replaced_docs())
    sc.check("coverage", not extra, f"counted as a value: {extra}")
    gaps = sorted(d or "?" for d, st in states.items() if st & GAP_STATES)
    if audit.get("completeness") == "complete":
        sc.check("coverage", not gaps and not audit.get("unknown_metadata"),
                 f"presented as complete with gaps: {gaps or ''} unknown metadata {audit.get('unknown_metadata')}")
    if audit.get("completeness") == "insufficient":
        sc.check("coverage", False, "audit completeness insufficient (expected a computed figure)")


def _score_contradiction(sc: Score, expect: dict, a: dict, doc_of, value_sources: list[dict], got: dict, ask: str,
                         terms) -> None:
    """L7: the content part of a combined answer must not state a value for a document that the coverage (or
    the audit) reports as not stating the datum."""
    if not a.get("claims"):
        return
    audit = a.get("audit")
    states: dict[str | None, set[str]] = {}
    for e in (audit or {}).get("documents") or []:
        states.setdefault(doc_of(e), set()).add(e.get("state"))
    value_docs = {doc_of(s) for s in value_sources}
    others = sum(got.get(k, 0) or 0 for k in ("pending", "not_yet_extracted", "failed", "partial_scan",
                                              "awaiting_review"))
    for fid in expect.get("_document_facts") or []:
        f = facts()[fid]
        d = f["document"]
        if audit:
            reported_absent = bool(states.get(d)) and states[d] <= {"not_stated", "partial_scan"}
        else:  # nothing pending or awaiting review, and not a value: the counts put it among "not stated"
            reported_absent = bool(got) and others == 0 and d not in value_docs
        if reported_absent and _claims_value_or_quote(a, doc_of, f, ask, terms):
            sc.check("coverage", False, f"the answer states {d}'s value in its content part while the coverage "
                     "reports it as not stating the datum")


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
        versions_ = {x.get("version_id") for x in cmp.get("sides") or [] if x.get("version_id")}
        labels = {x.get("label") for x in cmp.get("sides") or []}
        sc.check("sides", len(versions_) >= 2 and len(labels) >= 2,
                 f"sides not labeled by version ({[x.get('label') for x in cmp.get('sides') or []]})")


def rescore(hooks: Hooks, items: list[dict], results: list[dict]) -> list[dict]:
    """Score stored runs again (``raw``) with the current scorer, without asking anything. Runs stored before
    ``previous`` was recorded get the previous stored turn of the same item."""
    by_id = {i["id"]: i for i in items}
    out = []
    last: dict[str, dict] = {}
    for r in results:
        if r.get("not_run") or "raw" not in r:
            out.append(r)
            continue
        raw = dict(r["raw"])
        if raw.get("previous") is None and r["turn"] > 1:
            raw["previous"] = last.get(r["id"])
        last[r["id"]] = raw["answer"]
        new = score_turn(hooks, by_id[r["id"]], r["turn"] - 1, TurnRun.from_raw(raw))
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
