"""Post-generation checks on model answers (R25, R29): citations must exist and be authorized,
every number must come from the evidence or the verified calculation, and no links or markup."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_CITE = re.compile(r"\[(E\d+)\]")
_NUMBER = re.compile(r"(?<![\w])\d[\d,]*(?:\.\d+)?")
_FORBIDDEN = re.compile(r"(https?://|www\.|!\[|\]\(|<[a-zA-Z/])")


def _numbers(text: str) -> set[str]:
    out = set()
    for raw in _NUMBER.findall(_CITE.sub(" ", text)):
        token = raw.rstrip(".,").replace(",", "")
        try:
            value = Decimal(token)
        except InvalidOperation:
            continue
        out.add(str(value.normalize()))
        if value == value.to_integral_value():
            out.add(str(int(value)))
    return out


def allowed_numbers(evidence_texts: list[str], calculation: dict | None) -> set[str]:
    allowed: set[str] = set()
    for t in evidence_texts:
        allowed |= _numbers(t)
    if calculation:
        for v in calculation.values():
            if isinstance(v, (str, int, Decimal)) and v is not None:
                allowed |= _numbers(str(v))
    return allowed


def verify_answer(text: str, used_ids: list[str], allowed_ids: set[str], numbers: set[str]) -> list[str]:
    """Return a list of problems; empty means the answer may be shown."""
    problems = []
    if _FORBIDDEN.search(text):
        problems.append("links_or_markup")
    cited = set(_CITE.findall(text)) | set(used_ids)
    if not cited:
        problems.append("no_citations")
    if cited - allowed_ids:
        problems.append("unknown_citation")
    unsupported = {n for n in _numbers(text) if n not in numbers and len(n.replace(".", "")) > 1}
    if unsupported:
        problems.append("unsupported_number")
    return problems
