"""Hebrew abbreviations of professional writing, and their spelled-out forms, for query variants.

Reports and questions write the same term both ways ("דמ"ש" and "דמי שכירות", "חוו"ד" and "חוות דעת"), and a
lexical match needs the form the passage uses. ``variants`` returns the query with each known form swapped
for the other, so search runs both. This is language, not topic: it names no question or document type.
"""

from __future__ import annotations

import re

# (abbreviation, spelled out). Quote marks inside the abbreviation are matched in any of their spellings.
PAIRS: tuple[tuple[str, str], ...] = (
    ('דמ"ש', "דמי שכירות"),
    ('שכ"ד', "שכר דירה"),
    ('דמ"נ', "דמי ניהול"),
    ('חוו"ד', "חוות דעת"),
    ('יח"ד', "יחידות דיור"),
    ('מ"ר', "מטר רבוע"),
    ('מע"מ', "מס ערך מוסף"),
    ('ת"ז', "תעודת זהות"),
    ('בע"מ', "בערבון מוגבל"),
    ('ע"י', "על ידי"),
)
_Q = "[\"״'׳]"


def _abbr_pattern(abbr: str) -> str:
    return re.escape(abbr).replace('\\"', _Q).replace('"', _Q)


def variants(query: str, limit: int = 2) -> list[str]:
    """Up to ``limit`` rewrites of ``query`` with abbreviations spelled out or spelled-out terms abbreviated."""
    out: list[str] = []
    for abbr, full in PAIRS:
        if len(out) >= limit:
            break
        pat = re.compile(rf"(?<![א-ת])([הבלמושכ]?){_abbr_pattern(abbr)}")
        if pat.search(query):
            out.append(pat.sub(lambda m, full=full: m.group(1) + full, query))
        elif full in query:
            out.append(query.replace(full, abbr))
    return [v for v in dict.fromkeys(out) if v != query][:limit]
