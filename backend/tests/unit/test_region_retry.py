"""A transient vision failure during a reading (U12, KTD9): every other region is still read and its reading cached
(``image_readings``), so the job's next attempt calls the model only for the regions that failed. A configuration
error stops at once. Synthetic fixtures and a scripted reader only; no model is called."""

from __future__ import annotations

import pytest

from app.extraction import regions
from app.extraction.base import ExtractionError
from app.extraction.images import VisionUnavailable
from tests.unit.test_regions import (  # noqa: F401
    LOGO,
    R1,
    R7,
    TABLE,
    TILE,
    MemoryCache,
    ScriptedVision,
    extract,
    no_ocr,
)


@pytest.fixture(params=[1, 4], ids=["one_worker", "four_workers"])
def workers(request, monkeypatch):
    monkeypatch.setattr(regions, "REGION_WORKERS", request.param)
    return request.param


def test_a_transient_failure_still_reads_and_caches_every_other_region(workers):
    cache = MemoryCache()
    first = ScriptedVision(fail={TABLE: "timeout"})
    with pytest.raises(VisionUnavailable) as exc:
        extract(R1, first, cache)
    assert exc.value.permanent is False
    assert LOGO in first.calls  # read despite the table's failure
    assert any(r.text == "משרד שמאות לדוגמה" for r in cache.rows.values())

    retry = ScriptedVision()
    result = extract(R1, retry, cache)
    assert set(retry.calls) == {TABLE}  # only the region that failed is read again
    fresh = ScriptedVision()
    assert result.tables == extract(R1, fresh).tables and retry.count(TABLE) == fresh.count(TABLE)


def test_several_transient_failures_are_all_left_for_the_next_attempt(workers):
    cache = MemoryCache()
    first = ScriptedVision(fail={LOGO: "rate_limited", TILE: "timeout"})
    with pytest.raises(VisionUnavailable):
        extract(R7, first, cache)
    retry = ScriptedVision()
    extract(R7, retry, cache)
    fresh = ScriptedVision()
    extract(R7, fresh)
    assert set(retry.calls) == {LOGO, TILE}  # the stamp and the table were read and cached by the first attempt
    assert retry.count(LOGO) == fresh.count(LOGO) and retry.count(TILE) == fresh.count(TILE)


def test_a_configuration_error_stops_without_reading_the_rest():
    first = ScriptedVision(fail={TABLE: "auth", LOGO: "auth"})
    with pytest.raises(ExtractionError) as exc:
        extract(R1, first, MemoryCache())
    assert exc.value.permanent is True
