"""A transient vision failure during a reading (U12, KTD9): every other region is still read and its reading cached
(``image_readings``), so the job's next attempt calls the model only for the regions that failed. A configuration
error stops at once. Synthetic fixtures and a scripted reader only; no model is called."""

from __future__ import annotations

import time
import uuid

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


# --- the same, with the file read in windows through a staging area (U4) ------------------------------------------

OFFICE, VERSION = uuid.uuid4(), uuid.uuid4()


def _staged(path, vision, cache, tmp_path, window):
    """The file read through the staging area under ``tmp_path`` (a second call continues the first's)."""
    from app.config import get_settings
    from app.extraction import pdf_windows
    from app.platform.storage import LocalStorage

    get_settings().ingest_window_pages = window
    stage = pdf_windows.StorageStage(LocalStorage(tmp_path), OFFICE, VERSION, "a" * 64)
    return pdf_windows.read_pdf(path.read_bytes(), time.monotonic() + 600, get_settings(), vision, cache, stage), stage


@pytest.fixture
def windows():
    from app.config import get_settings

    before = get_settings().ingest_window_pages
    yield
    get_settings().ingest_window_pages = before


@pytest.mark.parametrize("path", [R1, R7], ids=["raster_table", "repeated_pictures"])
def test_regions_read_through_windows_give_the_same_reading_and_the_same_model_calls(path, tmp_path, windows):
    direct_vision, staged_vision = ScriptedVision(), ScriptedVision()
    direct = extract(path, direct_vision, MemoryCache())
    staged, _ = _staged(path, staged_vision, MemoryCache(), tmp_path, 1)
    assert [(b.kind, b.page, b.status, b.text, b.bbox, b.content_hash) for b in staged.blocks] == [
        (b.kind, b.page, b.status, b.text, b.bbox, b.content_hash) for b in direct.blocks]
    assert staged.tables == direct.tables and staged.repeated == direct.repeated
    assert sorted(staged_vision.calls) == sorted(direct_vision.calls)  # each distinct content read once, as in one go


def test_a_transient_failure_through_windows_is_continued_by_the_next_attempt_which_reads_only_the_failed_region(
        tmp_path, windows):
    cache = MemoryCache()
    first = ScriptedVision(fail={TABLE: "timeout"})
    with pytest.raises(VisionUnavailable) as exc:
        _staged(R1, first, cache, tmp_path, 1)
    assert exc.value.permanent is False and LOGO in first.calls
    retry = ScriptedVision()
    result, stage = _staged(R1, retry, cache, tmp_path, 1)
    assert stage.state["windows_done"] == 10  # the windows and the regions that were read stayed: not read again
    assert set(retry.calls) == {TABLE}  # only the failed region is read again
    assert result.tables == extract(R1, ScriptedVision()).tables
