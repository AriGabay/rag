"""Held-out topic words never occur in application code (U12, KTD16, R2, R27).

Routing and extraction must work for attributes nobody anticipated, so the held-out attribute and topic
words of eval/questions_general.yaml and of the second, separate set eval/questions_holdout_v2.yaml may not
appear anywhere in backend/app: not in code, prompts, comments or docstrings. Matching is on Hebrew words after unifying gershayim/geresh variants
(ממ״ד = ממ"ד = ממ''ד) and stripping one- and two-letter prefixes (הממ״ד, במרפסת, ליתרת).
The idiom "בין היתר" ("among other things") is not the word for a building permit, and the bare
spelling ממד is only a question spelling: in code it is the ordinary word for "dimension".
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest
import yaml

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
QUESTIONS = BACKEND / "eval" / "questions_general.yaml"
QUESTIONS_V2 = BACKEND / "eval" / "questions_holdout_v2.yaml"

# The held-out attribute and topic terms of the general question set, with their usual inflections.
HELD_OUT_TERMS: tuple[str, ...] = (
    # safe room
    "ממ״ד", "ממ״דים", "מרחב מוגן", "מרחבים מוגנים", "מקלט",
    # balconies, yard
    "מרפסת", "מרפסות", "חצר",
    # parking and storage
    "חניה", "חנייה", "חניות", "חניון", "מחסן", "מחסנים",
    # ceiling height
    "תקרה", "תקרות", "גובה תקרה",
    # building age
    "נבנה", "שנת בנייה", "שנת בניה",
    # elevator
    "מעלית", "מעליות",
    # planning, zoning and building rights
    "תכנית", "תב״ע", "מצב תכנוני", "ייעוד", "יעוד", "ייעודי", "זכויות בנייה", "יתרת זכויות",
    # permits
    "היתר", "היתרים", "היתר בנייה",
    # renovation
    "שיפוץ", "שיפוצים", "שופץ", "שופצה", "משופצת",
    # an amenity no document has
    "בריכה", "בריכת",
    # --- held-out set v2 (eval/questions_holdout_v2.yaml): never used for tuning ---
    # rent, lease and indexation
    "שכירות", "דמי שכירות", "שכר דירה", "שכ״ד", "שוכר", "שוכרים", "שוכרת", "השכרה", "השכרת", "מושכר",
    "מושכרת", "מושכרים", "הושכר", "הושכרה", "צמודים למדד", "צמוד למדד", "הצמדה",
    # income approach
    "תשואה", "שיעור היוון", "היוון", "הוונה", "אובדן הכנסות", "תפוסה", "דמי ניהול",
    # plot, coverage, setbacks, access
    "שטח המגרש", "גודל המגרש", "תכסית", "קו בניין", "קווי בניין", "דרך גישה",
    # registry entries
    "זיקת הנאה", "זיקות הנאה", "הערת אזהרה", "הערות אזהרה",
    # condition and environment
    "תחזוקה", "דירוג אנרגטי", "אנרגטי", "רעש", "דציבל", "דציבלים",
    # cost approach and the residual method
    "פחת", "עלות בנייה", "עלות בניה", "עלות הקמה", "עלויות הקמה", "שיורית",
)
# Spellings questions may use that are ordinary words elsewhere ("ממד" = dimension): not scanned in code.
QUESTION_ONLY_SPELLINGS: tuple[str, ...] = ("ממד",)
_PREFIXES_2 = ("וה", "וב", "ול", "ומ", "וש", "שה", "מה", "בה", "לה", "כש", "שב", "של", "שמ", "וכ")
_PREFIXES_1 = "והבלמשכ"
_WORD = re.compile(r"[א-ת][א-ת״׳]*")


def canon(text: str) -> str:
    """Unify quote variants inside Hebrew words: ממ"ד, ממ''ד and ממ״ד all become ממ״ד."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=[א-ת])(?:''|[\"“”״])(?=[א-ת])", "״", text)
    # a geresh inside a word (ז'בוטינסקי); an apostrophe that ends a word is a string quote in code
    return re.sub(r"(?<=[א-ת])['`’‘׳](?=[א-ת])", "׳", text)


def _variants(word: str) -> set[str]:
    out = {word}
    for p in _PREFIXES_2:
        if word.startswith(p) and len(word) - len(p) >= 2:
            out.add(word[len(p):])
    if word[0] in _PREFIXES_1 and len(word) >= 3:
        out.add(word[1:])
    return out


_TERMS = [tuple(canon(t).split()) for t in HELD_OUT_TERMS]
_QUESTION_TERMS = _TERMS + [tuple(canon(t).split()) for t in QUESTION_ONLY_SPELLINGS]


def find_terms(text: str, terms: list[tuple[str, ...]] = _TERMS) -> list[str]:
    """Held-out terms (canonical spelling) found in ``text``."""
    words = _WORD.findall(canon(text))
    variants = [_variants(w) for w in words]
    hits = []
    for term in terms:
        for i in range(len(words) - len(term) + 1):
            if all(term[k] in variants[i + k] for k in range(len(term))):
                if term == ("היתר",) and i > 0 and words[i - 1] == "בין":
                    continue  # "בין היתר" = "among other things"
                hits.append(" ".join(term))
                break
    return hits


def app_hits() -> list[str]:
    found = []
    for path in sorted(APP.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for term in find_terms(line):
                found.append(f"{path.relative_to(BACKEND)}:{n}: {term}")
    return found


def test_matcher_unifies_spellings_and_prefixes():
    assert find_terms('בדירה ממ"ד בשטח 12') == ["ממ״ד"]
    assert find_terms("שטח הממ״ד") == ["ממ״ד"]
    assert find_terms("ממ''ד וגם ממד") == ["ממ״ד"]
    assert find_terms("ממד היחידה") == []  # "the unit's dimension"
    assert find_terms("שטח הממד", _QUESTION_TERMS) == ["ממד"]
    assert set(find_terms("גובה התקרה בדירה")) == {"תקרה", "גובה תקרה"}
    assert find_terms("ליתרת זכויות הבנייה") == ["זכויות בנייה", "יתרת זכויות"]
    assert find_terms("הובאו בחשבון, בין היתר: מיקום") == []
    assert find_terms("ניתן היתר בנייה") == ["היתר", "היתר בנייה"]
    assert find_terms("מחיר למ״ר ממוצע") == []
    assert find_terms("ממדים של וקטור") == []  # "dimensions" is not the safe room
    assert find_terms("('מרפסות' -> 'מרפסת')") == ["מרפסת", "מרפסות"]


def test_held_out_words_do_not_occur_in_backend_app():
    hits = app_hits()
    assert hits == [], "held-out topic words in backend/app:\n" + "\n".join(hits)


@pytest.mark.parametrize("questions", [QUESTIONS, QUESTIONS_V2], ids=lambda p: p.name)
def test_question_set_declares_its_held_out_terms(questions):
    items = yaml.safe_load(questions.read_text(encoding="utf-8"))["items"]
    known = {" ".join(t) for t in _QUESTION_TERMS}
    held_out = [i for i in items if i.get("held_out")]
    assert len(items) >= 40
    assert len(held_out) * 2 >= len(items), "at least half of the items are held out"
    for item in held_out:
        terms = item.get("terms") or []
        assert terms, item["id"]
        asked = set(find_terms(" ".join(turn["ask"] for turn in item["turns"]), _QUESTION_TERMS))
        for term in terms:
            covered = set(find_terms(term, _QUESTION_TERMS))
            assert covered and covered <= known, (item["id"], term, "add it to HELD_OUT_TERMS")
            assert covered & asked, (item["id"], term, asked)
