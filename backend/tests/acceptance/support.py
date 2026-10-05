"""Shared cases and assertions for the acceptance gates."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from decimal import Decimal

from pypdf import PdfReader
from sqlalchemy import text

from app.db import tenant_tx
from eval.flows import Flow, ask_flow
from eval.truth import USERS, Filters, expected_clarifications, expected_stats, gt_record_for

RG, HAROZIM = "רמת גן", "חרוזים"
TX, AV = "transaction_price", "appraised_value"


@dataclass
class Case:
    id: str
    user: str
    question: str
    filters: Filters
    stated: tuple[str, ...] = ()  # area/property/VAT conditions the question states itself
    notes: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def answers(self) -> dict:
        f = self.filters
        out = {"data_kind": f.data_kind, "date_field": f.date_field}
        for key in ("area_type", "property_type", "vat_basis"):
            if getattr(f, key) is not None:
                out[key] = getattr(f, key)
        return {k: v for k, v in out.items() if v is not None}


def F(kind, date_field=None, year=None, year_to=None, **kw) -> Filters:  # noqa: N802
    return Filters(data_kind=kind, date_field=date_field, year_from=year, year_to=year_to, **kw)


ADMIN_A, DANA, YOSSI, ADMIN_B = "admin-a@demo.test", "dana@demo.test", "yossi@demo.test", "admin-b@demo.test"

NUMERIC_CASES = [
    Case("harozim-2024-net", ADMIN_A, "מה מחיר העסקאות למ״ר בחרוזים לפי תאריך עסקה בשנת 2024?",
         F(TX, "transaction_date", 2024, city=RG, neighborhood=HAROZIM, area_type="net", property_type="apartment",
           vat_basis="included")),
    Case("harozim-2024-gross-over-top-k", ADMIN_A, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים",
         F(TX, "transaction_date", 2024, city=RG, neighborhood=HAROZIM, area_type="gross",
           property_type="apartment", vat_basis="included"), notes="10 unique records > retrieval top-k"),
    Case("transactions-by-valuation-date", ADMIN_A, "מה מחיר העסקאות למ״ר ברמת גן לפי המועד הקובע בשנת 2024?",
         F(TX, "valuation_date", 2024, city=RG, area_type="net", property_type="apartment", vat_basis="included")),
    Case("value-by-report-date-2025", ADMIN_A, "מה השווי למ״ר בשומות שנערכו ב-2025?",
         F(AV, "report_date", 2025), notes="D8: only its report date is in 2025"),
    Case("value-by-valuation-date", ADMIN_A, "מה השווי למ״ר ברמת גן לפי המועד הקובע ב-2024?",
         F(AV, "valuation_date", 2024, city=RG, area_type="net", vat_basis="included")),
    Case("year-range", ADMIN_A, "מחירי עסקאות למ״ר בחרוזים שנחתמו בין 2023 ל-2024",
         F(TX, "transaction_date", 2023, 2024, city=RG, neighborhood=HAROZIM, area_type="net",
           property_type="apartment", vat_basis="included")),
    Case("docx-bursa", ADMIN_A, "מה מחיר העסקאות למ״ר בשכונת הבורסה ב-2024 לפי תאריך עסקה?",
         F(TX, "transaction_date", 2024, city=RG, neighborhood="הבורסה", area_type="net")),
    Case("givatayim-2023", ADMIN_A, "מה מחיר העסקאות למ״ר בגבעתיים ב-2023 לפי תאריך עסקה?",
         F(TX, "transaction_date", 2023, city="גבעתיים")),
    Case("three-dates-report-date", ADMIN_A, "מה מחיר העסקאות למ״ר בנחלת גנים בשומות שנערכו ב-2025?",
         F(TX, "report_date", 2025, city=RG, neighborhood="נחלת גנים", area_type="net")),
    Case("penthouse-stated", ADMIN_A, "מחיר למ״ר של פנטהאוז בעסקאות שנחתמו ב-2024 בחרוזים",
         F(TX, "transaction_date", 2024, city=RG, neighborhood=HAROZIM, property_type="penthouse",
           area_type="equivalent"), stated=("property_type",)),
    Case("vat-excluded-stated", ADMIN_A, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים לא כולל מע״מ",
         F(TX, "transaction_date", 2024, city=RG, neighborhood=HAROZIM, vat_basis="excluded", area_type="gross"),
         stated=("vat_basis",)),
    Case("weighted-wording-net-stated", ADMIN_A, "מה המחיר המשוקלל למ״ר בעסקאות שנחתמו ב-2024 בחרוזים בשטח נטו",
         F(TX, "transaction_date", 2024, city=RG, neighborhood=HAROZIM, area_type="net", property_type="apartment",
           vat_basis="included"), stated=("area_type",)),
    Case("dedup-2023", ADMIN_A, "מה מחיר העסקאות למ״ר בחרוזים שנחתמו ב-2023?",
         F(TX, "transaction_date", 2023, city=RG, neighborhood=HAROZIM, area_type="net", property_type="apartment",
           vat_basis="included"), notes="D1v2-T01 = D4-T00 counted once"),
    Case("yossi-g2-2023", YOSSI, "מה מחיר העסקאות למ״ר בחרוזים שנחתמו ב-2023?",
         F(TX, "transaction_date", 2023, city=RG, neighborhood=HAROZIM, area_type="net")),
    Case("dana-g1-excluded-vat", DANA, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים לא כולל מע״מ",
         F(TX, "transaction_date", 2024, city=RG, neighborhood=HAROZIM, vat_basis="excluded", area_type="net"),
         stated=("vat_basis",)),
    Case("office-b-harozim-2024", ADMIN_B, "מה מחיר העסקאות למ״ר בחרוזים לפי תאריך עסקה ב-2024?",
         F(TX, "transaction_date", 2024, city=RG, neighborhood=HAROZIM)),
]
CASES_BY_ID = {c.id: c for c in NUMERIC_CASES}


def scope(user: str) -> tuple[str, frozenset | None]:
    return USERS[user]


def expected_for(world, case: Case, **world_kw):
    office, groups = scope(case.user)
    return expected_stats(case.filters, office, groups, world.exclude, **world_kw)


def clarifications_for(world, case: Case) -> list[str]:
    office, groups = scope(case.user)
    return expected_clarifications(case.filters, case.stated, office, groups, world.exclude)


def run_case(world, case: Case, client=None, conversation_id=None) -> Flow:
    return ask_flow(client or world.client(case.user), case.question, case.answers, conversation_id)


def assert_numeric(answer: dict, expected, label: str = "") -> None:
    assert answer["kind"] in ("numeric", "combined"), (label, answer.get("kind"), answer.get("text"))
    n = answer["numeric"]
    want = expected.as_answer()
    got = {k: n.get(k) for k in want}
    # the product hides the median below three records; everything else must match exactly
    assert got == want, (label, got, want)


def version_index(world) -> dict[str, str]:
    """version id -> fixture file name for every uploaded version."""
    from eval.truth import docs

    return {r["version_id"]: docs()[gt]["filename"] for gt, r in world.docs.items() if r.get("version_id")}


def source_record(world, source: dict, data_kind: str) -> dict | None:
    filename = version_index(world).get(source["version_id"])
    if filename is None:
        return None
    row = source.get("row")
    return gt_record_for(filename, 0 if row is not None else None, row, data_kind)


def stored_page_text(world, office: str, version_id: str, page: int) -> str:
    with tenant_tx(world.system(office)) as conn:
        return conn.execute(text("SELECT text FROM pages WHERE version_id = :v AND page_no = :p"),
                            {"v": version_id, "p": page}).scalar() or ""


def stored_table_cells(world, office: str, version_id: str, row: int) -> list[str]:
    with tenant_tx(world.system(office)) as conn:
        structure = conn.execute(text("SELECT structure FROM extracted_tables WHERE version_id = :v"
                                      " ORDER BY table_index LIMIT 1"), {"v": version_id}).scalar()
    rows = (structure or {}).get("rows", [])
    return rows[row]["cells"] if row is not None and row < len(rows) else []


def pdf_page_text(data: bytes, page: int) -> str:
    """Raw text of one page of the served file, read independently of the stored extraction.

    pdfplumber keeps digit runs intact (Hebrew comes out in visual order, which does not matter for
    prices); pypdf drops some numbers next to Hebrew, so it is only a fallback."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text_ = pdf.pages[page - 1].extract_text() or ""
    return text_ or (PdfReader(io.BytesIO(data)).pages[page - 1].extract_text() or "")


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def big_numbers(s: str) -> list[Decimal]:
    """Numbers >= 1,000 written in a text (prices, per-sqm figures); years are filtered out."""
    out = []
    for m in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", s or ""):
        v = Decimal(m.replace(",", ""))
        if v >= 1000 and not (1950 <= v <= 2100 and "," not in m and "." not in m):
            out.append(v)
    return out
