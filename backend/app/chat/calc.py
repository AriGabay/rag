"""The calculator over verified values (KTD10, R14–R18).

The model never computes and never runs code. It registers inputs — values it took from a source it read and
the server verified (``V#``), numbers the user gave for a scenario (``A#``), stored measurements (``M#``) and earlier
results (``C#``) — and writes an expression over their ids. The expression is parsed by a small hand parser whose
grammar is a whitelist:

    expr    := term (("+" | "-") term)*
    term    := factor (("*" | "/") factor)*
    factor  := primary "%"?
    primary := id | literal | "(" expr ")" | func "(" id ("," id)* ")"
    id      := ("M" | "V" | "A" | "C") digits
    literal := "1" | "100" | "12"
    func    := sum | mean | median | min | max | count

``×`` ``÷`` ``−`` are accepted for ``*`` ``/`` ``-``; ``A1%`` is ``A1 / 100``. Anything else — a name, an attribute,
a call outside the list, a power, another number, a unary minus — is refused with the reason; nothing is ever
evaluated as code. The literals are structural only (round 7 U7, KTD9, R25), and are checked where they are used:
1 only beside a dimensionless value (``1 + A1%``, ``1 − C1``) or as a reciprocal's numerator, 100 only to convert a
ratio to percentage points (``C1 × 100``) and back (``V3 ÷ 100``), 12 only as months in a year (``× 12``, ``÷ 12``).
A literal never takes ``%``, two literals never combine, and none divides or scales an amount into a rate
(``V2 × 12 ÷ 100``, ``V1 ÷ 100``): a rate, an increase or any other number of a scenario must come from a source
(``V#``, ``M#``) or from the user (``A#``), else the user is asked for it.

A rate written in a source has a rounding interval (round 7 KTD8, R23): half a unit of its last written digit
(``rounding_interval``: "כ-17%" is 16.5%–17.5%). ``rate_products`` finds the products an expression applies a rate in,
and ``applied_rates`` the ids it applies as rates; the tools use them to compare a product of a document rate with
the amounts its source states (``tools.tool_calculate``), and to tell whether a parameter the user did not give was
filled by a document rate (``verify.unfilled_parameters``).

Values are exact ``Decimal`` throughout; nothing is rounded before display. Each operand carries its meaning —
unit (as dimensions: ₪, מ״ר, דונם, %, ...), period, VAT, area basis, kind, role, subject, and where it came from —
and compatibility depends on the operation (R17):

- ``+`` ``−`` and the aggregates need the same unit and period: units never mix (₪ and מ״ר), months and years
  convert only through 12, and a recurring amount (rent, management fees) never combines with a capital value or a
  one-time amount. A sum never includes a total row and other rows of the same table (its own components). A
  different VAT status, area basis or (for ``−``) subject needs a justification, and the result is then
  ``conditional``. Income minus cost of the same scope is a profit.
- ``×`` ``÷`` derive units: ₪ למ״ר × מ״ר is ₪, a ratio of the same units is dimensionless, × (1 + A1%) keeps the
  unit, a monthly amount × 12 is yearly. Per-area values meet areas only on the same area basis (else a
  justification makes the result conditional). A percentage applies only through ``%``.

An amount carries the scale its source states it in (round 7 R14: ``Operand.scale``, from ``stated_scale`` — a
table "(באלפי ₪)", a column "אלפי ש״ח", a number's own "אלף", or the heading-like line above it that notes a scale
and holds no figure of its own, "ממצאי הבדיקה באלפי ₪", in its section: ``governing_note``; a percent is never
scaled), so its amount is value × scale; nothing is rescaled behind the model's back. Sums, differences and
aggregates of amounts of one scale keep it, and × or ÷ by a dimensionless value (a rate, 12) keep it; scales multiply and divide with the values (thousands ÷ thousands cancel),
and a dimensionless result carries none. Amounts of different scales are never combined as if they were one: each is
brought to units (value × scale) before it is added, subtracted or aggregated, the result is in units and is marked
``rescaled`` — chosen over refusing because the model cannot convert a scale itself (the literals are structural
only), and the conversion is exact and rests only on what the sources state. ``display_matches`` compares a shown
number in the scale its word gives it with the value in the scale it is in: 25,742.5 in thousands is "25.74 מיליון",
"25,742.5 אלף" and "25,742,500", never "25,742.5 מיליון".

Every input of a file holding several appraisals carries its appraisal context (round 7 U6, KTD7, R20–R21:
``Leaf.context``, from ``app.chat.contexts``). When the context checks are enforced (``evaluate(contexts=...)``), a
calculation over inputs of two or more contexts — through ``+`` ``−``, ``×`` ``÷`` or an aggregate alike, earlier
results included — is refused with the contexts it mixes, unless a frozen calculation component of the turn
compares them (its ``compares`` names every one of those contexts, KTD1). A justification does not lift it: a
figure of another appraisal is another property's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from typing import Any
from uuid import UUID

from app.measurements.extract import PERIOD_LABELS, UNIT_LABELS, VAT_LABELS

MAX_EXPRESSION = 4000  # characters (an aggregate may name a few hundred measurements)
MAX_TOKENS = 1000
FUNCS = ("sum", "mean", "median", "min", "max", "count")
FUNC_LABELS = {"sum": "סכום", "mean": "ממוצע", "median": "חציון", "min": "מינימום", "max": "מקסימום",
               "count": "ספירה"}
LITERALS = ("1", "100", "12")
_PRECISION = Context(prec=28)  # the decimal default, fixed so no caller's context changes a result

Dims = tuple[tuple[str, int], ...]

# a unit of the measurement vocabulary as dimensions; a dimensionless ratio has none
UNIT_DIMS: dict[str, dict[str, int]] = {
    "ILS": {"ILS": 1}, "ILS_per_sqm": {"ILS": 1, "sqm": -1}, "sqm": {"sqm": 1}, "dunam": {"dunam": 1},
    "meters": {"m": 1}, "percent": {"%": 1}, "units": {"unit": 1}, "years": {"yr": 1}, "months": {"mo": 1},
    "ratio": {}, "other": {"other": 1},
}
_DIM_LABELS = {"ILS": "₪", "sqm": "מ״ר", "dunam": "דונם", "m": "מטר", "%": "%", "unit": "יחידות", "yr": "שנים",
               "mo": "חודשים", "other": "יחידה אחרת"}
AREA_DIMS = ("sqm", "dunam")

# value kinds: the measurement vocabulary, with income and profit for development and scenario calculations
PER_AREA_BASE = {"value_per_area": "value", "price_per_area": "price", "rent_per_area": "rent",
                 "management_fee_per_area": "management_fee", "cost_per_area": "cost"}
BASE_PER_AREA = {v: k for k, v in PER_AREA_BASE.items()}
RECURRING_KINDS = {"rent", "rent_per_area", "management_fee", "management_fee_per_area"}
CAPITAL_KINDS = {"value", "value_per_area", "price", "price_per_area"}
NOT_SUMMED_KINDS = {"rate", "coefficient"}
ROLES = ("income", "cost", "total", "component", "comparison", "rate", "other")
ROLE_LABELS = {"income": "הכנסה", "cost": "עלות", "total": "סה״כ", "component": "רכיב", "comparison": "השוואה",
               "rate": "שיעור", "other": "אחר"}
RESULT_KINDS = {"scenario": "תרחיש לפי בקשה", "reproduces_report_value": "משחזר ערך מהשומה", "computed": "חישוב"}
TOTAL_WORDS = re.compile(r"סה[\"״']?כ|סך\s*ה?כו?ל|סיכום|(?<![A-Za-z])total(?![A-Za-z])", re.I)


class CalcError(Exception):
    """An expression or a combination the calculator refuses; the message (Hebrew) goes back to the model."""


# --- operands --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Leaf:
    """A registered input an operand rests on, for the checks that look through earlier results."""

    id: str
    total: bool = False
    table: tuple | None = None  # (version id, table index) of a value taken from a table
    role: str | None = None
    # the VAT status of an amount of money ("included", "excluded" or "unknown"); None for an input that is not money
    # (an area, a rate, a coefficient), which has no VAT to share with the result
    vat: str | None = None
    # the appraisal context it was read in (KTD7): (its key, as the tools describe it); None outside a file holding
    # several appraisals
    context: tuple[str, str] | None = None


@dataclass(frozen=True)
class Operand:
    id: str | None
    value: Decimal | None  # None: a range (only ``count`` takes it)
    dims: Dims = ()
    period: str | None = None  # "month" | "year" | "unknown" | None (no period: a capital or one-time amount)
    vat: str | None = None  # "included" | "excluded" | "unknown" | None (not applicable)
    basis: str = ""  # area basis key ("" not stated)
    kind: str | None = None
    role: str | None = None
    subject: str = ""
    group: str | None = None  # a measurement's value role (asking price, comparable, ...), for aggregates
    same: str | None = None  # what it is (a stored measurement's id): one value is counted once
    approx: bool = False
    literal: Decimal | None = None
    leaves: tuple[Leaf, ...] = ()
    assumptions: tuple[str, ...] = ()
    # the scale the source states the number in (``stated_scale``): 1,000 for "343,679" of a table "(באלפי ₪)", so
    # the amount is value × scale; 1 for a number in units or one whose scale no source states
    scale: int = 1


def dims_of(unit: str | None) -> Dims:
    return tuple(sorted(UNIT_DIMS.get(unit or "other", {"other": 1}).items()))


def _period(period: str | None) -> str | None:
    if period in ("month", "year"):
        return period
    return "unknown" if period == "unknown" else None


def _vat(vat: str | None) -> str | None:
    return vat if vat in ("included", "excluded", "unknown") else None


def norm_subject(subject: str | None) -> str:
    return " ".join((subject or "").replace("״", '"').replace("׳", "'").split())


def operand(id: str, value, unit: str, *, period: str | None = "none", vat: str | None = None, basis: str = "",
            kind: str | None = None, role: str | None = None, subject: str = "", total: bool = False,
            table: tuple | None = None, group: str | None = None, same: str | None = None, approx: bool = False,
            assumption: bool = False, context: tuple[str, str] | None = None, scale: int = 1) -> Operand:
    """An input operand from its meaning, as the tools register it."""
    total = total or role == "total"
    dims = dims_of(unit)
    money = (_vat(vat) or "unknown") if "ILS" in dict(dims) else None
    return Operand(id, None if value is None else Decimal(str(value)), dims, _period(period), _vat(vat),
                   (basis or "").strip(), kind, role, norm_subject(subject), group, same, approx, None,
                   (Leaf(id, total, table, role, money, context),), (id,) if assumption else (),
                   scale if dims and dims != (("%", 1),) else 1)


@dataclass
class Outcome:
    value: Decimal
    dims: Dims
    period: str | None
    vat: str | None
    basis: str
    kind: str | None
    subject: str
    conditional: list[str]  # what the justification was needed for
    steps: list[tuple[str, Decimal]]  # every intermediate result, by its formula in ids
    assumptions: list[str]
    leaves: list[Leaf]
    inputs: list[str]  # the ids the expression names, in order
    approx: bool
    n: int | None = None  # the number of values, for an expression that is one aggregate
    # the scale the result is in (``Operand.scale``): the amount is value × scale; the scale of each step, in order;
    # whether inputs of different scales were brought to units before they were combined
    scale: int = 1
    step_scales: list[int] = field(default_factory=list)
    rescaled: bool = False


def result_operand(cid: str, out: Outcome) -> Operand:
    """An earlier result as an input of a later calculation: its full value and its meaning."""
    roles = {lf.role for lf in out.leaves}
    return Operand(cid, out.value, out.dims, out.period, out.vat, out.basis, out.kind,
                   next(iter(roles)) if len(roles) == 1 else None, out.subject, None, None, out.approx, None,
                   tuple(out.leaves), tuple(out.assumptions), out.scale)


# --- parsing ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Ref:
    id: str


@dataclass(frozen=True)
class Lit:
    text: str


@dataclass(frozen=True)
class Bin:
    op: str
    left: Any
    right: Any


@dataclass(frozen=True)
class Pct:
    node: Any


@dataclass(frozen=True)
class Agg:
    func: str
    ids: tuple[str, ...]


_ID = re.compile(r"[MVAC]\d+(?![A-Za-z0-9_֐-׿])")
_WORD = re.compile(r"[A-Za-z_֐-׿][A-Za-z0-9_֐-׿]*")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_OPS = {"+": "+", "-": "-", "−": "-", "–": "-", "*": "*", "×": "*", "·": "*", "/": "/", "÷": "/", "%": "%",
        "(": "(", ")": ")", ",": ","}
ALLOWED = ("מותרים רק מזהים M#/V#/A#/C#, הקבועים המבניים 1, 100, 12, הפעולות + − × ÷, סוגריים, % אחרי מזהה, "
           "והפונקציות " + ", ".join(FUNCS) + " על רשימת מזהים")
# a literal outside its structural use (round 7 U7): never a rate, an increase or an assumption
MSG_LITERAL = ("הקבוע {lit} אינו מותר כאן: הקבועים מבניים בלבד — 1 ליד ערך ללא יחידה (1 + A1%), 100 להמרה בין יחס "
               "לאחוז (C1 × 100, V3 ÷ 100), 12 להמרה בין חודש לשנה (× 12, ÷ 12). קבוע לעולם אינו שיעור, תוספת או "
               "הנחה: שיעור נלקח מהמקור (take_value, V#) או מהמשתמש (assume, A#); אם אף אחד מהם לא נתן אותו — "
               "אל תניח אותו, שאל את המשתמש")
MSG_LITERAL_PERCENT = ("% מותר רק אחרי מזהה (V#/A#/C#), לא אחרי קבוע: {lit}% הוא שיעור שאיש לא נתן. שיעור נלקח מהמקור "
                       "(take_value, V#) או מהמשתמש (assume, A#); אם אף אחד מהם לא נתן אותו — אל תניח אותו, שאל את "
                       "המשתמש")


def _tokens(expression: str) -> list[tuple[str, str]]:
    s = expression or ""
    if not s.strip():
        raise CalcError("ביטוי ריק: " + ALLOWED)
    if len(s) > MAX_EXPRESSION:
        raise CalcError(f"הביטוי ארוך מדי (עד {MAX_EXPRESSION} תווים)")
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isspace():
            i += 1
            continue
        if m := _ID.match(s, i):
            if s.startswith(".", m.end()):
                raise CalcError(f"גישה למאפיין (.) אינה מותרת: {s[i:m.end() + 8]}. " + ALLOWED)
            out.append(("id", m.group(0)))
            i = m.end()
            continue
        if m := _WORD.match(s, i):
            word = m.group(0)
            after = s[m.end():].lstrip()
            if word in FUNCS and after.startswith("("):
                out.append(("func", word))
                i = m.end()
                continue
            call = " — קריאה לפונקציה שאינה ברשימה" if after.startswith("(") else ""
            raise CalcError(f"שם לא מותר בביטוי: «{word}»{call}. " + ALLOWED)
        if m := _NUM.match(s, i):
            if m.group(0) not in LITERALS:
                raise CalcError(f"קבוע לא מותר: {m.group(0)}. מספר שאינו 1, 100 או 12 חייב להירשם קודם כערך מהמקור "
                                "(take_value, V#) או כהנחת המשתמש (assume, A#)")
            if s.startswith(".", m.end()):
                raise CalcError("גישה למאפיין (.) אינה מותרת. " + ALLOWED)
            out.append(("lit", m.group(0)))
            i = m.end()
            continue
        if s.startswith("**", i) or ch == "^":
            raise CalcError(f"חזקה ({'**' if ch == '*' else '^'}) אינה מותרת. " + ALLOWED)
        if s.startswith("//", i):
            raise CalcError("האופרטור // אינו מותר. " + ALLOWED)
        if ch == ".":
            raise CalcError("גישה למאפיין (.) אינה מותרת. " + ALLOWED)
        if ch in _OPS:
            out.append(("op", _OPS[ch]))
            i += 1
            continue
        raise CalcError(f"התו «{ch}» אינו מותר. " + ALLOWED)
    if len(out) > MAX_TOKENS:
        raise CalcError("הביטוי ארוך מדי")
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.t = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise CalcError("הביטוי נגמר באמצע: חסר ערך או סוגר סוגר. " + ALLOWED)
        self.i += 1
        return tok

    def expr(self):
        node = self.term()
        while (tok := self.peek()) in (("op", "+"), ("op", "-")):
            self.i += 1
            node = Bin(tok[1], node, self.term())
        return node

    def term(self):
        node = self.factor()
        while (tok := self.peek()) in (("op", "*"), ("op", "/")):
            self.i += 1
            node = Bin(tok[1], node, self.factor())
        return node

    def factor(self):
        node = self.primary()
        if self.peek() == ("op", "%"):
            self.i += 1
            node = Pct(node)
        return node

    def primary(self):
        kind, value = self.take()
        if kind == "id":
            return Ref(value)
        if kind == "lit":
            return Lit(value)
        if kind == "func":
            self.take()  # "("
            ids: list[str] = []
            while True:
                tok = self.peek()
                if tok is None or tok[0] != "id":
                    raise CalcError(f"בפונקציה {value} מותרים רק מזהים מופרדים בפסיקים, למשל {value}(M1, M2)")
                ids.append(self.take()[1])
                tok = self.peek()
                if tok == ("op", ","):
                    self.i += 1
                    continue
                if tok == ("op", ")"):
                    self.i += 1
                    return Agg(value, tuple(ids))
                raise CalcError(f"בפונקציה {value} מותרים רק מזהים מופרדים בפסיקים, למשל {value}(M1, M2)")
        if (kind, value) == ("op", "("):
            node = self.expr()
            if self.peek() != ("op", ")"):
                raise CalcError("חסר סוגר סוגר ')'")
            self.i += 1
            return node
        if (kind, value) == ("op", "-"):
            raise CalcError("מינוס אונרי אינו מותר: כתוב הפרש בין שני ערכים (V1 - V2)")
        if (kind, value) == ("op", ")"):
            raise CalcError("סוגר ')' שאין לו פתיחה")
        raise CalcError(f"«{value}» אינו יכול לבוא כאן: צפוי מזהה, קבוע, סוגר או פונקציה. " + ALLOWED)


def parse(expression: str):
    """The expression's tree, or ``CalcError`` with the reason."""
    p = _Parser(_tokens(expression))
    node = p.expr()
    tok = p.peek()
    if tok is not None:
        if tok == ("op", ")"):
            raise CalcError("סוגר ')' שאין לו פתיחה")
        raise CalcError(f"חסר אופרטור לפני «{tok[1]}». " + ALLOWED)
    return node


_SYMBOL = {"+": "+", "-": "−", "*": "×", "/": "÷"}
_PREC = {"+": 1, "-": 1, "*": 2, "/": 2}


def render(node, name, hebrew: bool = False, ctx: int = 0, strict: bool = False) -> str:
    """The expression as text: ids through ``name`` (an id, or its Hebrew label), with only the parentheses it
    needs."""
    if isinstance(node, Ref):
        return name(node.id)
    if isinstance(node, Lit):
        return node.text
    if isinstance(node, Pct):
        return render(node.node, name, hebrew, 3) + "%"
    if isinstance(node, Agg):
        return f"{FUNC_LABELS[node.func] if hebrew else node.func}({', '.join(name(i) for i in node.ids)})"
    p = _PREC[node.op]
    s = (f"{render(node.left, name, hebrew, p)} {_SYMBOL[node.op]} "
         f"{render(node.right, name, hebrew, p, node.op in '-/')}")
    return f"({s})" if p < ctx or (strict and p == ctx) else s


def ids_of(node) -> list[str]:
    if isinstance(node, Ref):
        return [node.id]
    if isinstance(node, Agg):
        return list(node.ids)
    if isinstance(node, Pct):
        return ids_of(node.node)
    if isinstance(node, Bin):
        return ids_of(node.left) + ids_of(node.right)
    return []


def _rate_id(node) -> str | None:
    """The id a node applies as a rate: ``X%``, or ``X ÷ 100`` of a percentage, over one id."""
    if isinstance(node, Pct) and isinstance(node.node, Ref):
        return node.node.id
    if (isinstance(node, Bin) and node.op == "/" and isinstance(node.left, Ref) and isinstance(node.right, Lit)
            and node.right.text == "100"):
        return node.left.id
    return None


def applied_rates(node) -> list[str]:
    """The ids an expression applies as rates (``A1%``, ``V3 ÷ 100``), in order, each once (round 7 KTD9): what
    fills a scenario's rate — a user's assumption or a value of a source, never a literal."""
    out: list[str] = []

    def walk(n) -> None:
        rate = _rate_id(n)
        if rate is not None:
            out.append(rate)
        elif isinstance(n, Pct):
            walk(n.node)
        elif isinstance(n, Bin):
            walk(n.left)
            walk(n.right)

    walk(node)
    return list(dict.fromkeys(out))


def rate_products(node) -> list[tuple[Any, str]]:
    """Each product an expression applies a rate in (round 7 KTD8): (the nearest ``×`` above the rate, the rate's
    id) — ``V1 × V2%`` itself, ``V2 × (1 + V3%)`` inside ``V1 − V2 × (1 + V3%)``."""
    out: list[tuple[Any, str]] = []

    def walk(n, product) -> None:
        rate = _rate_id(n)
        if rate is not None:
            if product is not None:
                out.append((product, rate))
        elif isinstance(n, Pct):
            walk(n.node, product)
        elif isinstance(n, Bin):
            inner = n if n.op == "*" else product
            walk(n.left, inner)
            walk(n.right, inner)

    walk(node, None)
    return out


_WRITTEN_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def rounding_interval(written: str) -> tuple[Decimal, Decimal] | None:
    """The values a number written with its precision may stand for (round 7 KTD8, R23): half a unit of its last
    written digit on each side — "17" (or "כ-17%") is 16.5–17.5, "17.5" is 17.45–17.55. None unless the text holds
    exactly one number."""
    found = _WRITTEN_NUMBER.findall(written or "")
    if len(found) != 1:
        return None
    raw = found[0].replace(",", "")
    places = len(raw.split(".", 1)[1]) if "." in raw else 0
    half = Decimal(5).scaleb(-(places + 1))
    value = Decimal(raw)
    return value - half, value + half


# --- evaluation ------------------------------------------------------------------------------------------------

_KNOWN_UNITS = {dims_of(k): v for k, v in UNIT_LABELS.items() if k in UNIT_DIMS and k not in ("other",)}


def unit_label(dims: Dims, period: str | None = None) -> str:
    """₪, ₪ למ״ר, מ״ר, % ... for dimensions, with the period."""
    if dims in _KNOWN_UNITS:
        label = _KNOWN_UNITS[dims]
    else:
        num = " × ".join(_DIM_LABELS.get(d, d) + (f"^{e}" if e > 1 else "") for d, e in dims if e > 0)
        den = " × ".join(_DIM_LABELS.get(d, d) + (f"^{-e}" if e < -1 else "") for d, e in dims if e < 0)
        label = (num or "1") + (f" ל{den}" if den else "") if dims else ""
    if period in ("month", "year"):
        label = f"{label} {PERIOD_LABELS[period]}".strip()
    return label


def _label(o: Operand) -> str:
    return o.id or "הערך"


def _combine(a: Dims, b: Dims, sign: int) -> Dims:
    out = dict(a)
    for d, e in b:
        out[d] = out.get(d, 0) + sign * e
    return tuple(sorted((d, e) for d, e in out.items() if e))


def _area(dims: Dims) -> bool:
    return any(d in AREA_DIMS for d, _ in dims)


def _union(*seqs) -> tuple:
    return tuple(dict.fromkeys(x for s in seqs for x in s))


MSG_RENT_CAPITAL = ("דמי שכירות או דמי ניהול (ערך תקופתי) ושווי או מחיר הון לעולם אינם מתחברים בחיבור, בחיסור או "
                    "בצבירה ({ids}): אלה נתונים מסוגים שונים")
MSG_TOTAL = ("אי אפשר לחבר שורת סה״כ עם הרכיבים שלה מאותה טבלה ({ids}): הסכום יספור אותם פעמיים. אפשר לחבר רק את "
             "הרכיבים, או להשתמש בסה״כ לבדו")
MSG_UNITS = "יחידות שונות ({a} ו-{b}) אינן מתחברות ({ids})"
MSG_PERIODS = ("ערך לחודש וערך לשנה אינם מתחברים ({ids}): המר קודם ב-× 12 (חודשי לשנתי) או ב-÷ 12 (שנתי לחודשי)")
# a calculation that fails on values that were all found is a failed calculation, never missing data (R17)
MSG_FAILED = ("החישוב נכשל על הנתונים שנמצאו — זו תקלה בחישוב, לא נתון חסר: אמור למשתמש שהחישוב נכשל ומדוע, ואל "
              "תכתוב שהנתונים לא נמצאו")
MSG_RECURRING = ("ערך לתקופה (לחודש/לשנה, כמו דמי שכירות) וערך שאינו לתקופה (כמו שווי או עלות חד-פעמית) אינם "
                 "מתחברים ({ids})")


class _Eval:
    def __init__(self, operands: dict[str, Operand]):
        self.operands = operands
        self.needs: list[str] = []
        self.steps: list[tuple[str, Decimal]] = []
        self.step_scales: list[int] = []
        self.n: int | None = None
        self.rescaled = False

    def need(self, reason: str) -> None:
        if reason not in self.needs:
            self.needs.append(reason)

    def get(self, i: str) -> Operand:
        o = self.operands.get(i)
        if o is None:
            raise CalcError(f"מזהה לא מוכר: {i}. אפשר להשתמש רק במזהים שנרשמו בתור הזה (V# מ-take_value, A# מ-assume, "
                            "M# מ-find_measurements, C# מ-calculate)")
        return o

    def eval(self, node) -> Operand:
        if isinstance(node, Ref):
            return self.get(node.id)
        if isinstance(node, Lit):
            v = Decimal(node.text)
            return Operand(None, v, literal=v)
        if isinstance(node, Pct):
            x = self.eval(node.node)
            if not x.leaves:  # a literal, or literals combined: a rate nobody gave
                raise CalcError(MSG_LITERAL_PERCENT.format(lit=render(node.node, lambda i: i)))
            if x.dims not in ((), (("%", 1),)):
                raise CalcError(f"% מותר רק אחרי אחוז או מספר (למשל A1%); {render(node.node, lambda i: i)} הוא "
                                f"{unit_label(x.dims)}")
            out = Operand(None, _value(x) / 100, (), x.period, x.vat, x.basis, x.kind, x.role, x.subject, None, None,
                          x.approx, None, x.leaves, x.assumptions)
            return self.step(node, out)
        if isinstance(node, Agg):
            return self.step(node, self.aggregate(node))
        a, b = self.eval(node.left), self.eval(node.right)
        self._literals(node.op, a, b)
        out = self.additive(node.op, a, b) if node.op in "+-" else self.multiplicative(node.op, a, b)
        return self.step(node, out)

    @staticmethod
    def _literals(op: str, a: Operand, b: Operand) -> None:
        """A literal only in its structural use (module docstring): 1 beside a dimensionless value or over one, 100
        between a ratio and percentage points, 12 between a month and a year — never a rate or an assumption."""
        la, lb = not a.leaves, not b.leaves
        if not (la or lb):
            return
        if la and lb:
            raise CalcError(MSG_LITERAL.format(lit=_literal_text(a if a.literal is not None else b)))
        lit, other, left = (a, b, True) if la else (b, a, False)
        n = lit.literal
        pct = (("%", 1),)
        if op in "+-":
            ok = n == 1 and other.dims == ()
        elif op == "*":
            ok = n in (1, 12) or (n == 100 and other.dims == ())
        else:  # "/": a literal divisor converts; a literal numerator only as a reciprocal
            ok = (n == 1 and other.dims == ()) if left else (n in (1, 12) or (n == 100 and other.dims == pct))
        if not ok:
            raise CalcError(MSG_LITERAL.format(lit=_literal_text(lit)))

    def step(self, node, out: Operand) -> Operand:
        if out.value is not None:
            self.steps.append((render(node, lambda i: i), out.value))
            self.step_scales.append(out.scale)
        return out

    def same_scale(self, items: list[Operand]) -> tuple[list[Decimal], int]:
        """The values of operands that are added, subtracted or aggregated, and the scale of the result: their own
        scale when they share one; otherwise each is brought to units (value × scale) first and the result is in
        units — never two scales combined as if they were one."""
        scales = {o.scale for o in items}
        if len(scales) == 1:
            return [_value(o) for o in items], next(iter(scales))
        self.rescaled = True
        return [_value(o) * o.scale for o in items], 1

    # + −, and the aggregates ------------------------------------------------------------------------------------
    def _additive_checks(self, items: list[Operand], op: str) -> None:
        ids = ", ".join(_label(o) for o in items if o.id) or "הערכים"
        kinds = {o.kind for o in items}
        if kinds & RECURRING_KINDS and kinds & CAPITAL_KINDS:
            raise CalcError(MSG_RENT_CAPITAL.format(ids=ids))
        if op in ("+", "sum"):
            leaves = [(n, lf) for n, o in enumerate(items) for lf in o.leaves]
            for n, lf in leaves:
                if lf.total and lf.table is not None and any(
                        m != n and x.table == lf.table and not x.total for m, x in leaves):
                    raise CalcError(MSG_TOTAL.format(ids=ids))
        first = items[0]
        for o in items[1:]:
            if o.dims != first.dims:
                raise CalcError(MSG_UNITS.format(a=unit_label(first.dims) or "ללא יחידה",
                                                 b=unit_label(o.dims) or "ללא יחידה", ids=ids))
        periods = {o.period for o in items}
        if len(periods) > 1:
            if {"month", "year"} <= periods:
                raise CalcError(MSG_PERIODS.format(ids=ids))
            if "unknown" in periods and len(periods - {"unknown"}) <= 1:
                self.need("תקופה: לחלק מהערכים לא צוינה תקופה (לחודש/לשנה) ולאחרים כן")
            else:
                raise CalcError(MSG_RECURRING.format(ids=ids))
        vats = {o.vat for o in items if o.vat is not None}
        if len(vats) > 1:
            self.need("מע״מ: " + " מול ".join(sorted(VAT_LABELS[v] for v in vats)))
        if _area(first.dims) and len({o.basis for o in items}) > 1:
            self.need("בסיס שטח: " + " מול ".join(sorted(o.basis or "לא צוין" for o in {o.basis: o for o in items}.values())))

    def additive(self, op: str, a: Operand, b: Operand) -> Operand:
        self._additive_checks([a, b], op)
        if a.subject and b.subject and not same_subject(a.subject, b.subject) and not (
                op == "+" and a.role == "component" and b.role == "component"):
            self.need(f"נושא: «{a.subject}» מול «{b.subject}»")
        (va, vb), scale = self.same_scale([a, b])
        value = va + vb if op == "+" else va - vb
        if a.kind == b.kind or b.kind is None:
            kind = a.kind
        elif a.kind is None:
            kind = b.kind
        elif op == "-" and "income" in (a.kind, a.role) and "cost" in (b.kind, b.role):
            kind = "profit"
        else:
            kind = None
        return Operand(None, value, a.dims, a.period if a.period == b.period else (a.period or b.period),
                       a.vat if a.vat == b.vat else (a.vat or b.vat), a.basis or b.basis, kind,
                       a.role if a.role == b.role else None, a.subject if a.subject == b.subject else (
                           a.subject if not b.subject else b.subject if not a.subject else ""),
                       None, None, a.approx or b.approx, None, a.leaves + b.leaves,
                       _union(a.assumptions, b.assumptions), scale)

    def aggregate(self, node: Agg) -> Operand:
        seen, items = set(), []
        for i in node.ids:
            o = self.get(i)
            key = o.same or o.id
            if key not in seen:
                seen.add(key)
                items.append(o)
        if node.func == "count":
            self.n = len(items)
            return Operand(None, Decimal(len(items)), (), kind="count",
                           leaves=tuple(lf for o in items for lf in o.leaves),
                           assumptions=_union(*(o.assumptions for o in items)))
        ranges = [o.id for o in items if o.value is None]
        if ranges:
            raise CalcError("חלק מהנתונים הם טווחים ולא ערך יחיד (" + ", ".join(ranges)
                            + "). אפשר לחשב על הערכים היחידים בלבד או להציג את הטווחים")
        self._additive_checks(items, "sum" if node.func == "sum" else node.func)
        first = items[0]
        if node.func == "sum" and (any(d in AREA_DIMS and e < 0 for d, e in first.dims) or ("%", 1) in first.dims
                                   or {o.kind for o in items} & NOT_SUMMED_KINDS):
            raise CalcError("אי אפשר לסכם ערכים ליחידת שטח, שיעורים או מקדמים: סכום כזה אינו בעל משמעות "
                            "(ממוצע או חציון כן)")
        if len({o.kind for o in items}) > 1:
            self.need("סוגי נתונים שונים בצבירה: " + ", ".join(sorted(str(o.kind) for o in {o.kind: o for o in items}.values())))
        if len({o.group for o in items}) > 1:
            self.need("תפקידים שונים בצבירה (למשל מחיר מבוקש ועסקה)")
        values, scale = self.same_scale(items)
        self.n = len(items)
        if node.func == "sum":
            value = sum(values, Decimal(0))
        elif node.func == "mean":
            value = sum(values, Decimal(0)) / len(values)
        elif node.func == "median":
            s = sorted(values)
            mid = len(s) // 2
            value = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
        else:
            value = min(values) if node.func == "min" else max(values)
        kinds = {o.kind for o in items}
        subjects = {o.subject for o in items}
        return Operand(None, value, first.dims, first.period, first.vat, first.basis,
                       next(iter(kinds)) if len(kinds) == 1 else None, None,
                       next(iter(subjects)) if len(subjects) == 1 else "", first.group, None,
                       any(o.approx for o in items), None, tuple(lf for o in items for lf in o.leaves),
                       _union(*(o.assumptions for o in items)), scale)

    # × ÷ ----------------------------------------------------------------------------------------------------------
    def multiplicative(self, op: str, a: Operand, b: Operand) -> Operand:
        ids = ", ".join(_label(o) for o in (a, b) if o.id) or "הערכים"
        pct = ("%", 1)
        hundred = Decimal(100)
        if op == "*":
            for x in (a, b):
                if pct in x.dims:
                    raise CalcError(f"{_label(x)} הוא אחוז: כדי להחיל אותו כתוב {_label(x)}% (= {_label(x)}/100), "
                                    f"למשל V1 × (1 + {_label(x)}%)")
            if a.dims == () and b.literal == hundred and a.literal is None:
                dims = (pct,)
            elif b.dims == () and a.literal == hundred and b.literal is None:
                dims = (pct,)
            else:
                dims = _combine(a.dims, b.dims, 1)
        else:
            if _value(b) == 0:
                raise CalcError(f"חלוקה באפס ({ids}). {MSG_FAILED}")
            if a.dims == (pct,) and b.literal == hundred:
                dims = ()
            elif (pct in a.dims) != (pct in b.dims):
                x = a if pct in a.dims else b
                raise CalcError(f"{_label(x)} הוא אחוז: כדי להחיל אותו כתוב {_label(x)}% (= {_label(x)}/100)")
            else:
                dims = _combine(a.dims, b.dims, -1)
        if sum(1 for d, _ in dims if d in AREA_DIMS) > 1:
            self.need("יחידות שטח שונות (מ״ר ודונם) באותה תוצאה")
        # area that cancels between the operands must be measured on one basis
        for d in AREA_DIMS:
            ea, eb = dict(a.dims).get(d, 0), dict(b.dims).get(d, 0)
            if ea and eb and ((op == "*" and ea * eb < 0) or (op == "/" and ea * eb > 0)) and a.basis != b.basis:
                self.need(f"בסיס שטח: «{a.basis or 'לא צוין'}» מול «{b.basis or 'לא צוין'}» ({ids})")
        basis = (a.basis if _area(a.dims) else b.basis) if _area(dims) else ""
        # periods: a monthly amount × 12 is yearly, a yearly one ÷ 12 monthly; otherwise one side may carry it
        twelve = Decimal(12)
        if op == "*" and {a.period, b.period} == {"month", None} and twelve in (a.literal, b.literal):
            period = "year"
        elif op == "/" and a.period == "year" and b.literal == twelve:
            period = "month"
        else:
            pa, pb = a.period, b.period
            if pa and pb:
                if op == "/" and pa == pb and pa != "unknown":
                    period = None
                elif "unknown" in (pa, pb):
                    self.need("תקופה: לחלק מהערכים לא צוינה תקופה")
                    period = pa if pa != "unknown" else pb
                elif op == "/":
                    raise CalcError(MSG_PERIODS.format(ids=ids))
                else:
                    raise CalcError(f"מכפלה של שני ערכים לתקופה ({ids}) אינה בעלת משמעות")
            elif pb and op == "/":
                self.need(f"חלוקה בערך לתקופה ({_label(b)}) כשהמונה אינו לתקופה")
                period = None
            else:
                period = pa or pb
        # VAT: two money amounts with different VAT statuses
        if "ILS" in dict(a.dims) and "ILS" in dict(b.dims) and a.vat and b.vat and a.vat != b.vat:
            self.need("מע״מ: " + " מול ".join(sorted({VAT_LABELS[a.vat], VAT_LABELS[b.vat]})))
        vat = (a.vat if "ILS" in dict(a.dims) else b.vat) if "ILS" in dict(dims) else None
        value = _value(a) * _value(b) if op == "*" else _value(a) / _value(b)
        value, scale = _product_scale(value, a.scale * b.scale if op == "*" else Fraction(a.scale, b.scale), dims)
        kind = self._kind(op, a, b, dims)
        role = a.role if b.dims == () and op in "*/" else (b.role if a.dims == () and op == "*" else None)
        subject = a.subject if a.subject == b.subject or not b.subject else (b.subject if not a.subject else "")
        return Operand(None, value, dims, period, vat, basis, kind, role, subject, None, None, a.approx or b.approx,
                       None, a.leaves + b.leaves, _union(a.assumptions, b.assumptions), scale)

    @staticmethod
    def _kind(op: str, a: Operand, b: Operand, dims: Dims) -> str | None:
        if op == "*":
            if b.dims == ():
                return a.kind
            if a.dims == ():
                return b.kind
            for x, y in ((a, b), (b, a)):
                if x.kind in PER_AREA_BASE and _area(y.dims) and not _area(dims):
                    return PER_AREA_BASE[x.kind]
            return None
        if dims == () and (a.dims != () or b.kind == "count"):
            return "ratio"  # a share: of two quantities of one unit, or of a count
        if b.dims == ():
            return a.kind
        if "ILS" in dict(a.dims) and _area(b.dims) and a.kind in BASE_PER_AREA:
            return BASE_PER_AREA[a.kind]
        return None


def _product_scale(value: Decimal, scale: int | Fraction, dims: Dims) -> tuple[Decimal, int]:
    """The value and scale of a product or quotient: the scales multiply (an amount in thousands × a rate stays in
    thousands) and divide (thousands ÷ thousands cancel). A dimensionless result (a ratio, a percentage) carries no
    scale, nor does a scale that is not a whole multiplier: the value is brought to units instead."""
    if dims and ("%", 1) not in dims and Fraction(scale).denominator == 1:
        return value, int(scale)
    if scale == 1:
        return value, 1
    f = Fraction(scale)
    return value * Decimal(f.numerator) / Decimal(f.denominator), 1


def _literal_text(o: Operand) -> str:
    return fmt(o.value) if o.value is not None else "?"


def _value(o: Operand) -> Decimal:
    if o.value is None:
        raise CalcError(f"{_label(o)} הוא טווח ולא ערך יחיד")
    return o.value


MSG_CONTEXTS = ("החישוב משלב ערכים מכמה שומות או נכסים באותו קובץ: {which}. ערך של שומה אחת אינו ערך של נכס אחר, "
                "ואף רכיב חישוב של הבקשה אינו משווה ביניהם. חשב מערכים של הקשר אחד בלבד (קח את הערך מההקשר של הנכס "
                "שנשאל עליו), או — אם המשתמש ביקש להשוות ביניהם — שאל אותו")


def _check_contexts(out: Operand, allowed: list[frozenset[str]] | None) -> None:
    """A calculation over inputs of several appraisal contexts (KTD7) is refused unless one of ``allowed`` — the
    contexts a frozen comparison component names — holds them all; ``allowed`` None: not enforced."""
    if allowed is None:
        return
    mixed = dict.fromkeys(lf.context for lf in out.leaves if lf.context is not None)
    keys = {k for k, _ in mixed}
    if len(keys) > 1 and not any(keys <= a for a in allowed):
        raise CalcError(MSG_CONTEXTS.format(which="; ".join(label for _, label in mixed)))



# a one- or two-letter Hebrew prefix (ו, ה, ב, ל, מ, ש, כ) before a word of three letters or more
_SUBJECT_PREFIX = re.compile(r"^[והבלמשכ]{1,2}(?=[א-ת]{3,})")


def _subject_words(subject: str) -> set[str]:
    """The words of a subject that name its scope: the metric's own words ("רווח", "עלויות" — whole words of the
    value's kind vocabulary, ``meaning.metric_word``) are left out, and each word is taken without its prefix
    letters."""
    from app.chat.meaning import _norm, metric_word

    return {_SUBJECT_PREFIX.sub("", w) for w in re.findall(r"[\w\"']+", _norm(subject)) if not metric_word(w)}


def same_subject(a: str, b: str) -> bool:
    """Whether two subjects name one scope: the same words once the metric is left out ("רווח פרויקט X" and
    "עלויות פרויקט X"), or one a narrower wording of the other that shares at least two of its words and adds no
    number ("הפרויקט במתחם X" and "מתחם X"). A generic wording against a numbered one ("הדירה" and "דירה 5 עסקת
    השוואה"), or two that each name something the other lacks, stay two subjects."""
    if a.strip() == b.strip():
        return True
    x, y = _subject_words(a), _subject_words(b)
    if not x or not y:
        return False
    if x == y:
        return True
    small, large = (x, y) if len(x) <= len(y) else (y, x)
    return small <= large and len(small) >= 2 and not any(any(ch.isdigit() for ch in w) for w in large - small)


def evaluate(node, operands: dict[str, Operand], justification: str | None = None,
             contexts: list[frozenset[str]] | None = None) -> Outcome:
    """The exact value of a parsed expression with its meaning, or ``CalcError`` with the reason. What needs a
    justification (a different VAT status, area basis or subject) is refused without one, and with one the result
    is conditional on it. ``contexts``: when enforced, the sets of appraisal contexts the turn's comparison
    components allow to be combined (``_check_contexts``); None leaves contexts unchecked."""
    ev = _Eval(operands)
    with localcontext(_PRECISION):
        try:
            out = ev.eval(node)
        except (InvalidOperation, ArithmeticError) as exc:
            raise CalcError(f"{type(exc).__name__}. {MSG_FAILED}") from None
    _check_contexts(out, contexts)
    if ev.needs and not (justification or "").strip():
        raise CalcError("החישוב מערבב נתונים שאינם תואמים: " + "; ".join(ev.needs) + ". אפשר לחשב רק עם "
                        "justification שמסביר מדוע הערבוב תקף (התוצאה תסומן כמותנית), או לשאול את המשתמש")
    return Outcome(_value(out), out.dims, out.period, out.vat, out.basis, out.kind, out.subject, list(ev.needs),
                   ev.steps, list(out.assumptions), list(dict.fromkeys(out.leaves)),
                   list(dict.fromkeys(ids_of(node))), out.approx, ev.n if isinstance(node, Agg) else None,
                   out.scale, list(ev.step_scales), ev.rescaled)


# --- display -----------------------------------------------------------------------------------------------------

def _round(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


DISPLAY_PLACES = 2  # decimals a result is shown with
SMALL_PLACES = 4  # decimals for a value strictly between -1 and 1 (other than 0)


def _places(value: Decimal, places: int = DISPLAY_PLACES) -> int:
    return places if abs(value) >= 1 or value == 0 else max(places, SMALL_PLACES)


def fmt(value: Decimal, places: int = DISPLAY_PLACES) -> str:
    """A value for reading: thousands separated, rounded half up, without trailing zeros."""
    s = f"{_round(value, _places(value, places)):,f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def rounding_rule(value: Decimal, dims: Dims, kind: str | None = None) -> dict:
    """The display rule ``display`` applies to a result, exactly as ``fmt`` does it: rounded half up to
    ``decimals`` places (``SMALL_PLACES`` for a value between -1 and 1), trailing zeros dropped, thousands
    separated; ``percent_decimals`` for its percentage form when it has one. The breakdown view shows it next to
    the full-precision value."""
    shown = display(value, dims, kind)
    out = {"rule": "half_up", "decimals": _places(value), "trailing_zeros": False, "thousands_separator": ",",
           "percent": "percent" in shown}
    if out["percent"]:
        pct = value if dims == (("%", 1),) else value * 100
        out["percent_decimals"] = _places(pct)
    text = f"מעוגל חצי למעלה עד {out['decimals']} ספרות אחרי הנקודה, בלי אפסים בסוף"
    if out["percent"]:
        text += f"; האחוז עד {out['percent_decimals']} ספרות"
    out["text"] = text
    return out


def display(value: Decimal, dims: Dims, kind: str | None = None) -> dict:
    """The forms a result is shown in: the value, and a percentage for a ratio or a percent (a count, also
    dimensionless, is a number of values and has none)."""
    out = {"value": fmt(value)}
    if dims == () and kind != "count":
        out["percent"] = fmt(value * 100) + "%"
    elif dims == (("%", 1),):
        out["percent"] = fmt(value) + "%"
    return out


# the scale words an answer writes after an amount ("1.53 מיליון ₪", "850 אלף", "2.1 מיליארד", "1.53M"), with their
# multiplier; "מ׳" is also a metre, so it is a million only next to a currency ("₪1.53 מ׳", "1.53 מ׳ ש״ח")
_SCALE = re.compile(r"[ \u00a0]?(?:(?P<word>מיליארדי|מיליארד|מיליוני|מיליון|אלפי|אלפים|אלף|מיל[׳'])"
                    r"|(?P<abbr>מ[׳'])|(?P<latin>[KkM])(?![A-Za-z]))(?![א-ת])")
SCALES = {"מיליארד": 10**9, "מיליארדי": 10**9, "מיליון": 10**6, "מיליוני": 10**6, "מיל׳": 10**6, "מיל'": 10**6,
          "מ׳": 10**6, "מ'": 10**6, "אלף": 10**3, "אלפי": 10**3, "אלפים": 10**3, "K": 10**3, "k": 10**3, "M": 10**6}
_CURRENCY = re.compile(r"₪|ש[\"״']ח|שקל")


def scale_after(text: str, start: int, end: int) -> tuple[int, int]:
    """The scale a number written at ``text[start:end]`` is shown in — the multiplier of the scale word right after
    it (1 when there is none) — and where that word ends."""
    m = _SCALE.match(text, end)
    if m is None:
        return 1, end
    if m.group("abbr") and not (_CURRENCY.search(text[max(0, start - 3):start])
                                or _CURRENCY.match(text[m.end():m.end() + 5].lstrip())):
        return 1, end
    return SCALES[m.group(0).strip(" \u00a0")], m.end()


def display_matches(written: str, percent: bool, value: Decimal, dims: Dims, kind: str | None = None,
                    scale: int = 1, source_scale: int = 1) -> bool:
    """Whether a number shown in an answer (as written, with a % sign after it or not, and in the ``scale`` its scale
    word gives it) is the full value rounded to the precision it shows: 14.3% is 0.143155…, 14.30% and 14.4% are
    not; 1.53 מיליון is 1,530,000.4, and 1.6 מיליון is not. ``source_scale``: the scale the value itself is in (an
    amount of a table "באלפי ₪", ``stated_scale``) — the amount is value × source_scale, so 38,043.5 in thousands is
    "38.04 מיליון", "38,043.5 אלף" and "38,043,500"; a number shown with no scale word may also repeat the value as
    the source writes it ("38,043.5", the scale said by the answer's own header or words). A number wrong at its
    scale is never one of them: "38.4 מיליון", "38,043.5 מיליון"."""
    raw = written.replace(",", "").strip()
    try:
        shown = Decimal(raw)
    except InvalidOperation:
        return False
    places = len(raw.split(".", 1)[1]) if "." in raw else 0
    candidates = []
    if percent and dims == () and kind != "count":
        candidates.append(value * 100)
    if percent and dims == (("%", 1),):
        candidates.append(value)
    elif not percent:
        full = value * source_scale
        if scale == 1:
            candidates += [value, full] if source_scale != 1 else [value]
        else:
            candidates.append(full / Decimal(scale))
    return any(_round(abs(c), places) == abs(shown) for c in candidates)


# the scale a source states its amounts in, as a note for a table, a column, a row or a sentence ("(באלפי ₪)",
# "אלפי ש״ח", "במיליוני ₪", "₪ באלפים", "אש״ח", "מלש״ח", "K ₪") — never a scale word right after a number, which is
# that number's own ("1,530 אלפי ₪": ``scale_after``)
_NOT_AFTER_NUMBER = r"(?<!\d)(?<!\d[ \u00a0])"
_NOTE_CURRENCY = r"(?:₪|ש[\"״']ח|שקל(?:ים)?(?![א-ת])|ש\"ח)"
_NOTE_SCALE = re.compile(
    rf"{_NOT_AFTER_NUMBER}(?<![א-ת])ב?(?P<construct>אלפי|מיליוני|מיליארדי)\s*{_NOTE_CURRENCY}"
    rf"|{_NOTE_CURRENCY}\s*\(?ב?(?P<plural>אלפים|מיליונים|מיליארדים)(?![א-ת])"
    rf"|{_NOT_AFTER_NUMBER}(?<![א-ת])ב?(?P<plural_first>אלפים|מיליונים|מיליארדים)\s*{_NOTE_CURRENCY}"
    rf"|(?<![א-ת])(?P<abbr>אש[\"״']ח|אלש[\"״']ח|מלש[\"״']ח)(?![א-ת])"
    rf"|(?<![\dA-Za-z.,])(?P<latin>[KkM])\s*₪|₪\s*(?P<latin_after>[KkM])(?![A-Za-z])")
_NOTE_VALUES = {"אלפי": 10**3, "מיליוני": 10**6, "מיליארדי": 10**9, "אלפים": 10**3, "מיליונים": 10**6,
                "מיליארדים": 10**9, "K": 10**3, "k": 10**3, "M": 10**6}
SCALE_LABELS = {10**3: "אלפי", 10**6: "מיליוני", 10**9: "מיליארדי"}
_UNITS_AFTER = re.compile(r"[ \u00a0]?(?:₪|ש[\"״']ח)")
_PERCENT_AT = re.compile(r"[ \u00a0]?%")
# blocks looked at above a figure for the note that governs it (``governing_note``)
NOTE_WINDOW = 6
# a heading's enumeration ("7.", "7.2", "(3)") is not a number of its own
_ENUMERATION = re.compile(r"^\s*\(?\d+(?:\.\d+)*[.)]?\s")


def scale_notes(text: str) -> set[int]:
    """The scales the notes of a text state (``_NOTE_SCALE``)."""
    out = set()
    for m in _NOTE_SCALE.finditer(text or ""):
        word = next(g for g in m.groups() if g)
        if word in _NOTE_VALUES:
            out.add(_NOTE_VALUES[word])
        else:  # אש״ח (אלפי ש״ח), מלש״ח (מיליוני ש״ח)
            out.add(10**6 if word.startswith("מ") else 10**3)
    return out


def stated_scale(own: str, start: int, end: int, *contexts: str) -> int:
    """The scale a source states a number in (R14): the scale word right after it ("5,600 אלף ₪": ``scale_after``);
    else 1 when a currency follows it directly ("5,000,000 ₪" is in units) or it is a percent ("12.7%": a rate has no
    scale); else the scale the nearest context that notes one states — ``contexts`` from the nearest: the cell, its
    row label and column header before the table's caption, title and notes; the quote before the line it is in;
    last, the note that governs it from a line above (``governing_note``). A context whose notes state two scales
    says nothing, and none is looked for further. 1 when no source states a scale."""
    word = scale_after(own, start, end)[0]
    if word != 1:
        return word
    if _UNITS_AFTER.match(own, end) or _PERCENT_AT.match(own, end) or own[max(0, start - 1):start] == "%":
        return 1
    for text in contexts:
        notes = scale_notes(text)
        if notes:
            return next(iter(notes)) if len(notes) == 1 else 1
    return 1


def governing_note(preceding) -> str:
    """The note that states the scale for the figures under it (R14): a figure whose own words, line, cell, row,
    column or table state no scale is in the scale of the nearest block above it, in its section, that notes one and
    holds no figure of its own — a heading-like line "ממצאי הבדיקה באלפי ₪ לא כולל מע״מ" over one figure per
    paragraph, or the section's heading itself. ``preceding``: the blocks above the figure's own, nearest first, as
    (kind, text, in the figure's section); for a table, its text with its caption, title and notes. At most
    ``NOTE_WINDOW`` blocks are looked at, and the search stops — with no note — at a block of another section, at the
    section's heading when it notes none, at a table with a note of its own (its note is its own cells'), and at a
    block noting a scale beside a figure of its own (that note is that figure's). The note's text, or ""; a note of
    two scales says nothing (``stated_scale``)."""
    for n, (kind, text, same) in enumerate(preceding):
        if n >= NOTE_WINDOW or (not same and kind != "heading"):
            return ""
        notes = scale_notes(text)
        if kind == "table":
            if notes:
                return ""
            continue
        if notes:
            return text if kind == "heading" or not re.search(r"\d", _ENUMERATION.sub("", text or "")) else ""
        if kind == "heading" or not same:
            return ""
    return ""


def scaled_label(label: str, scale: int) -> str:
    """A unit label in a scale: "אלפי ₪" for ₪ in thousands, "פי 100 ₪" for a multiplier with no word."""
    if scale == 1:
        return label
    word = SCALE_LABELS.get(scale) or f"פי {scale:,}"
    return f"{word} {label}".strip() if label else word


# --- what the turn registers ---------------------------------------------------------------------------------------

@dataclass
class Value:
    """A value the server verified in a source the turn read (V#)."""

    vid: str
    value: Decimal
    written: str
    source_id: str
    document_id: UUID
    version_id: UUID
    reading_id: str | None
    title: str
    location: str
    label: str
    kind: str
    unit: str
    period: str
    vat: str
    area_basis: str
    subject: str
    role: str
    provenance: dict[str, str]  # field -> "source" | "model_asserted" | "not_stated"
    locator: dict  # {"table_index", "row", "row_number", "column"} or {"quote"}
    quote: str  # the quote, or the table row as read
    total: bool = False
    table: tuple | None = None
    approx: bool = False
    # the section path of the block the value is in (KTD8): the judge reads a party's section as its position
    section: str = ""
    # who stated it, how (``extract.STANCES``) and the scenario, stage or date it belongs to; each has a
    # ``provenance`` entry like the meaning fields: found in the text around it, or asserted by the model
    stated_by: str = ""
    stance: str = "unknown"
    scenario: str = ""
    attribution: str = ""  # the source's words that say who stated it or how ("לטענת המשיבה ..."), when found
    meaning_from: dict = field(default_factory=dict)  # field -> where the source states it ("header", "cell", ...)
    # how its own region was read (R28): "clear" (as ingested, or settled by a focused re-read), "uncertain" (still
    # unclear after the re-reads, or in an uncertain reading with no region of its own), "" (not known: the source's
    # reading status decides)
    reading: str = ""
    reading_note: str = ""
    # its appraisal context in a file holding several (KTD7, R20): {"key", "number", "label", "pages"}, from the block
    # or table row it was read at (the source's, never the model's); None in a file with one context
    context: dict | None = None
    # where its subject stands against that context (R21): "context" (the subject names the context's identifiers),
    # "contradicted" (it names another context's), "asserted" (it names none of them: the model's word only), ""
    # (no subject, or a file with one context)
    subject_from: str = ""
    # the scale its source states it in (``stated_scale``, R14): 1,000 for a cell of a table "(באלפי ₪)" — the amount
    # is value × scale; 1 when the source states none
    scale: int = 1

    @property
    def certainty(self) -> str:
        """"model_asserted": part of its meaning or attribution was asserted rather than found in its source;
        "uncertain_reading": its region stayed unclear; otherwise "verified"."""
        if "model_asserted" in self.provenance.values():
            return "model_asserted"
        return "uncertain_reading" if self.reading == "uncertain" else "verified"

    def operand(self) -> Operand:
        from app.chat.meaning import basis_key

        return operand(self.vid, self.value, self.unit, period=self.period, vat=self.vat, basis=basis_key(self.area_basis),
                       kind=self.kind, role=self.role, subject=self.subject, total=self.total, table=self.table,
                       approx=self.approx,
                       context=(self.context["key"], self.context.get("described") or self.context["label"])
                       if self.context else None, scale=self.scale)

    def public(self) -> dict:
        return {"id": self.vid, "value": str(self.value), "value_text": self.written, "label": self.label,
                "source_id": self.source_id, "document_id": str(self.document_id), "version_id": str(self.version_id),
                "reading_id": self.reading_id, "title": self.title, "location": self.location, "kind": self.kind,
                "unit": self.unit, "unit_label": UNIT_LABELS.get(self.unit, ""), "period": self.period,
                "vat": self.vat, "area_basis": self.area_basis, "subject": self.subject, "role": self.role,
                "provenance": dict(self.provenance), "certainty": self.certainty, "locator": dict(self.locator),
                "quote": self.quote, "total": self.total, "approx": self.approx, "section": self.section,
                "stated_by": self.stated_by, "stance": self.stance, "scenario": self.scenario,
                "attribution": self.attribution, "meaning_from": dict(self.meaning_from), "reading": self.reading,
                "reading_note": self.reading_note, "context": dict(self.context) if self.context else None,
                "subject_from": self.subject_from, "scale": self.scale}


@dataclass
class Assumption:
    """A number the user gave for a scenario, quoted from the user's own message (A#)."""

    aid: str
    value: Decimal
    written: str
    unit: str
    label: str
    quote: str
    turn: int  # the user message it quotes, counted from the conversation's visible start (1 = first)
    current: bool  # quoted from this turn's message
    # the calculation parameter it fills (round 7 KTD9): the pending parameter a reply to a clarification was bound
    # to (``resolve.bind_pending``); None when nothing links it to one — it then fills one parameter, in the
    # component's order (``verify.unfilled_parameters``)
    parameter: str | None = None

    def operand(self) -> Operand:
        return operand(self.aid, self.value, self.unit, assumption=True)

    def public(self) -> dict:
        return {"id": self.aid, "value": str(self.value), "value_text": self.written, "unit": self.unit,
                "label": self.label, "quote": self.quote, "turn": self.turn, "current": self.current} | (
            {"parameter": self.parameter} if self.parameter else {})


@dataclass
class Computation:
    """A result (C#): its exact value and display forms, formula, inputs, assumptions and sources, and what it is."""

    cid: str
    label: str
    expression: str  # with ids
    formula: str  # with Hebrew labels
    outcome: Outcome
    inputs: list[dict]  # [{"id", "label", "value", "kind": "value" | "assumption" | "measurement" | "computation"}]
    sources: list[str]  # the S# and M# behind the inputs
    documents: int
    result_kind: str  # "scenario" | "reproduces_report_value" | "computed"
    justification: str | None
    note: str
    reproduces: dict | None = None  # {"source": S#|M#, "as_written"} when it equals a number the report writes
    leaves: list[str] = field(default_factory=list)
    # round 7 KTD8 (R23): a product of a document rate whose source states an amount within the rate's rounding
    # interval — {"amount", "value", "source", "quote", "rate", "rate_written", "interval", "computed", "range",
    # "from" (the C# it came through, or None)} — reported, kept in the record, never substituted; and an amount the
    # same source states outside that interval (the same shape), which the answer shows beside the result as a gap
    explicit_amount: dict | None = None
    stated_amount_differs: dict | None = None
    rates: list[str] = field(default_factory=list)  # the ids it applies as rates (``applied_rates``, KTD9)
    # the uncertain inputs (V#, M#) that made it conditional when it was computed (R28): what the server's conditional
    # qualifier names next to a result shown without saying so (``verify.conditional_qualifier``)
    uncertain: list[str] = field(default_factory=list)

    @property
    def value(self) -> Decimal:
        return self.outcome.value

    @property
    def dims(self) -> Dims:
        return self.outcome.dims

    @property
    def conditional(self) -> bool:
        return bool(self.outcome.conditional)

    @property
    def assumptions(self) -> list[str]:
        return list(self.outcome.assumptions)

    @property
    def measurement_ids(self) -> list[str]:
        return [i for i in self.leaves if i.startswith("M")]

    @property
    def scale(self) -> int:
        """The scale the result is in, as its inputs' sources state theirs (``Outcome.scale``)."""
        return self.outcome.scale

    @property
    def unit_label(self) -> str:
        """Its unit, in its scale ("אלפי ₪" for a result in thousands)."""
        return scaled_label(unit_label(self.dims, self.outcome.period), self.scale)

    @property
    def vat(self) -> str | None:
        """The VAT basis of an amount of money: the status every money input it rests on shares, through earlier
        results ("included" or "excluded"); None for a result that is not money, or whose inputs differ or do not
        say — VAT phrasing of the result is checked against it."""
        if "ILS" not in dict(self.dims):
            return None
        statuses = {lf.vat for lf in self.outcome.leaves if lf.vat is not None}
        return next(iter(statuses)) if len(statuses) == 1 and statuses <= {"included", "excluded"} else None

    def display(self) -> dict:
        return display(self.value, self.dims, self.outcome.kind)

    def public(self) -> dict:
        return {"id": self.cid, "label": self.label, "expression": self.expression, "formula": self.formula,
                "value": str(self.value), "display": self.display(), "unit": self.unit_label,
                "kind": self.outcome.kind, "result_kind": self.result_kind,
                "result_kind_label": RESULT_KINDS[self.result_kind], "inputs": self.inputs,
                "assumptions": self.assumptions, "sources": self.sources, "documents": self.documents,
                "conditional": self.conditional, "conditions": list(self.outcome.conditional), "vat": self.vat,
                "justification": self.justification, "note": self.note, "reproduces": self.reproduces,
                "n": self.outcome.n,
                # the intermediate results (the last step is the result itself), at full precision and as shown
                "steps": [{"expression": t, "value": str(v), "display": fmt(v)} for t, v in self.outcome.steps[:-1]],
                "rounding": rounding_rule(self.value, self.dims, self.outcome.kind),
                "explicit_amount_available": self.explicit_amount, "stated_amount_differs": self.stated_amount_differs,
                "rates": list(self.rates), "scale": self.scale,
                # the earlier shape, for readers of stored answers
                "operation": self.expression, "result": str(self.value)}
