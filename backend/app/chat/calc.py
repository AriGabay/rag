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
evaluated as code. The literals are structural only: 1 (as in ``1 + A1%``), 100 (percent points) and 12 (months
in a year); every other number must come from a source or from the user.

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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation, localcontext
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
            assumption: bool = False) -> Operand:
    """An input operand from its meaning, as the tools register it."""
    total = total or role == "total"
    dims = dims_of(unit)
    money = (_vat(vat) or "unknown") if "ILS" in dict(dims) else None
    return Operand(id, None if value is None else Decimal(str(value)), dims, _period(period), _vat(vat),
                   (basis or "").strip(), kind, role, norm_subject(subject), group, same, approx, None,
                   (Leaf(id, total, table, role, money),), (id,) if assumption else ())


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


def result_operand(cid: str, out: Outcome) -> Operand:
    """An earlier result as an input of a later calculation: its full value and its meaning."""
    roles = {lf.role for lf in out.leaves}
    return Operand(cid, out.value, out.dims, out.period, out.vat, out.basis, out.kind,
                   next(iter(roles)) if len(roles) == 1 else None, out.subject, None, None, out.approx, None,
                   tuple(out.leaves), tuple(out.assumptions))


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
ALLOWED = ("מותרים רק מזהים M#/V#/A#/C#, הקבועים 1, 100, 12, הפעולות + − × ÷, סוגריים, % אחרי ערך, "
           "והפונקציות " + ", ".join(FUNCS) + " על רשימת מזהים")


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
        self.n: int | None = None

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
            if x.dims not in ((), (("%", 1),)):
                raise CalcError(f"% מותר רק אחרי אחוז או מספר (למשל A1%); {render(node.node, lambda i: i)} הוא "
                                f"{unit_label(x.dims)}")
            out = Operand(None, _value(x) / 100, (), x.period, x.vat, x.basis, x.kind, x.role, x.subject, None, None,
                          x.approx, None, x.leaves, x.assumptions)
            return self.step(node, out)
        if isinstance(node, Agg):
            return self.step(node, self.aggregate(node))
        a, b = self.eval(node.left), self.eval(node.right)
        out = self.additive(node.op, a, b) if node.op in "+-" else self.multiplicative(node.op, a, b)
        return self.step(node, out)

    def step(self, node, out: Operand) -> Operand:
        if out.value is not None:
            self.steps.append((render(node, lambda i: i), out.value))
        return out

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
        if a.subject and b.subject and a.subject != b.subject and not (
                op == "+" and a.role == "component" and b.role == "component"):
            self.need(f"נושא: «{a.subject}» מול «{b.subject}»")
        value = _value(a) + _value(b) if op == "+" else _value(a) - _value(b)
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
                       _union(a.assumptions, b.assumptions))

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
        values = [_value(o) for o in items]
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
                       _union(*(o.assumptions for o in items)))

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
        kind = self._kind(op, a, b, dims)
        role = a.role if b.dims == () and op in "*/" else (b.role if a.dims == () and op == "*" else None)
        subject = a.subject if a.subject == b.subject or not b.subject else (b.subject if not a.subject else "")
        return Operand(None, value, dims, period, vat, basis, kind, role, subject, None, None, a.approx or b.approx,
                       None, a.leaves + b.leaves, _union(a.assumptions, b.assumptions))

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


def _value(o: Operand) -> Decimal:
    if o.value is None:
        raise CalcError(f"{_label(o)} הוא טווח ולא ערך יחיד")
    return o.value


def evaluate(node, operands: dict[str, Operand], justification: str | None = None) -> Outcome:
    """The exact value of a parsed expression with its meaning, or ``CalcError`` with the reason. What needs a
    justification (a different VAT status, area basis or subject) is refused without one, and with one the result
    is conditional on it."""
    ev = _Eval(operands)
    with localcontext(_PRECISION):
        try:
            out = ev.eval(node)
        except (InvalidOperation, ArithmeticError) as exc:
            raise CalcError(f"{type(exc).__name__}. {MSG_FAILED}") from None
    if ev.needs and not (justification or "").strip():
        raise CalcError("החישוב מערבב נתונים שאינם תואמים: " + "; ".join(ev.needs) + ". אפשר לחשב רק עם "
                        "justification שמסביר מדוע הערבוב תקף (התוצאה תסומן כמותנית), או לשאול את המשתמש")
    return Outcome(_value(out), out.dims, out.period, out.vat, out.basis, out.kind, out.subject, list(ev.needs),
                   ev.steps, list(out.assumptions), list(dict.fromkeys(out.leaves)),
                   list(dict.fromkeys(ids_of(node))), out.approx, ev.n if isinstance(node, Agg) else None)


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
                    scale: int = 1) -> bool:
    """Whether a number shown in an answer (as written, with a % sign after it or not, and in the ``scale`` its scale
    word gives it) is the full value rounded to the precision it shows: 14.3% is 0.143155…, 14.30% and 14.4% are
    not; 1.53 מיליון is 1,530,000.4, and 1.6 מיליון is not."""
    raw = written.replace(",", "").strip()
    try:
        shown = Decimal(raw)
    except InvalidOperation:
        return False
    places = len(raw.split(".", 1)[1]) if "." in raw else 0
    candidates = []
    if percent and dims == () and kind != "count":
        candidates.append(value * 100)
    if not percent or dims == (("%", 1),):
        candidates.append(value if percent or scale == 1 else value / Decimal(scale))
    return any(_round(abs(c), places) == abs(shown) for c in candidates)


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
                       approx=self.approx)

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
                "reading_note": self.reading_note}


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

    def operand(self) -> Operand:
        return operand(self.aid, self.value, self.unit, assumption=True)

    def public(self) -> dict:
        return {"id": self.aid, "value": str(self.value), "value_text": self.written, "unit": self.unit,
                "label": self.label, "quote": self.quote, "turn": self.turn, "current": self.current}


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
    def unit_label(self) -> str:
        return unit_label(self.dims, self.outcome.period)

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
                # the earlier shape, for readers of stored answers
                "operation": self.expression, "result": str(self.value)}
