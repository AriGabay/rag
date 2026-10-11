"""Hebrew abbreviations of professional writing, and their spelled-out forms, for query variants.

Reports and questions write the same term both ways ("דמ"ש" and "דמי שכירות", "חוו"ד" and "חוות דעת"), and a
lexical match needs the form the passage uses. ``variants`` returns the query with each known form swapped
for the other, so search runs both. This is language, not topic: it names no question or document type.
"""

from __future__ import annotations

import re

# (abbreviation, spelled out). Quote marks inside the abbreviation are matched in any of their spellings. An
# abbreviation with several spelled-out forms is listed once per form: it is spelled out as its first, and every
# form is abbreviated.
PAIRS: tuple[tuple[str, str], ...] = (
    ('דמ"ש', "דמי שכירות"),
    ('שכ"ד', "שכר דירה"),
    ('דמ"נ', "דמי ניהול"),
    ('חוו"ד', "חוות דעת"),
    ('יח"ד', "יחידות דיור"),
    ('מ"ר', "מטר רבוע"),
    ('מ"ר', "מטר מרובע"),
    ('מע"מ', "מס ערך מוסף"),
    ('ת"ז', "תעודת זהות"),
    ('בע"מ', "בערבון מוגבל"),
    ('ע"י', "על ידי"),
    ('שפ"פ', "שטח פרטי פתוח"),
    ('שצ"פ', "שטח ציבורי פתוח"),
    ('תב"ע', "תוכנית בניין עיר"),
    ('תב"ע', "תכנית בניין עיר"),
)
_Q = "[\"״'׳]"
_PREFIX = "(?<![א-ת])([הבלמושכ]?)"


def _abbr_pattern(abbr: str) -> str:
    return re.escape(abbr).replace('\\"', _Q).replace('"', _Q)


def _full_pattern(full: str) -> re.Pattern:
    """The spelled-out form with a prefix on its first word and the definite article on the others, as a phrase is
    written definite ("השטח הפרטי הפתוח", "דמי השכירות")."""
    words = [re.escape(w) for w in full.split()]
    return re.compile(rf"(?<![א-ת])([הבלמושכ]{{0,2}}){words[0]}" + "".join(rf"\s+ה?{w}" for w in words[1:])
                      + "(?![א-ת])")


def variants(query: str, limit: int = 2) -> list[str]:
    """Up to ``limit`` rewrites of ``query`` with abbreviations spelled out or spelled-out terms abbreviated."""
    out: list[str] = []
    spelled: set[str] = set()
    for abbr, full in PAIRS:
        if len(out) >= limit:
            break
        pat = re.compile(_PREFIX + _abbr_pattern(abbr))
        if abbr not in spelled and pat.search(query):
            out.append(pat.sub(lambda m, full=full: m.group(1) + full, query))
        elif (written := _full_pattern(full)).search(query):
            out.append(written.sub(lambda m, abbr=abbr: m.group(1) + abbr, query))
        spelled.add(abbr)
    return [v for v in dict.fromkeys(out) if v != query][:limit]
