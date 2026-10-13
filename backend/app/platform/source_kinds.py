"""The source kind of a document (round 10 U2: KTD8; R16, R17).

``appraisal`` is a valuation report (the default, and what every document stored before the column existed is);
``standard`` an appraisal standard; ``professional`` another professional document (a guideline, a circular, a
professional article). The kind is chosen at upload and changed by an admin; it is never inferred from a file name or
a title (an appraisal named "תקן 19 …" stays an appraisal). Only appraisals take part in the property surfaces and
the appraisal stages of ingestion (U3, KTD12)."""

from __future__ import annotations

APPRAISAL = "appraisal"
STANDARD = "standard"
PROFESSIONAL = "professional"
SOURCE_KINDS = (APPRAISAL, STANDARD, PROFESSIONAL)

# the SQL condition of every appraisal-only surface (the property vocabulary, appraisal counts and listings, unit
# locating, measurement listings): the documents table aliased ``d``
APPRAISALS_SQL = f"d.source_kind = '{APPRAISAL}'"

MSG_BAD_KIND = "סוג המקור אינו תקין. הערכים האפשריים: appraisal (שומה), standard (תקן שמאות), professional (מסמך מקצועי)"


def is_valid(kind: str) -> bool:
    return kind in SOURCE_KINDS
