"""A reprocess that cannot replace a reading ends as "kept previous" (U12, KTD9, R27): the job's terminal state for
a reindex is ``kept_previous``, never ``failed``, and what the version records says why, after how many attempts.
Other kinds of jobs keep failing as before. No database: the decisions are pure."""

from __future__ import annotations

import pytest

from app.platform import jobs, pipeline


def test_a_reindex_that_ends_keeps_the_previous_reading():
    assert jobs.failure_status("process", {"mode": "reindex"}, terminal=True) == "kept_previous"


def test_a_reindex_with_attempts_left_is_queued_again():
    assert jobs.failure_status("process", {"mode": "reindex"}, terminal=False) == "queued"


@pytest.mark.parametrize("kind,payload", [("process", None), ("process", {}), ("extract_measurements", {}),
                                          ("positions", {"positions_version": "p1"}), ("extract_facts", {})])
def test_other_jobs_still_fail(kind, payload):
    assert jobs.failure_status(kind, payload, terminal=True) == "failed"
    assert jobs.failure_status(kind, payload, terminal=False) == "queued"


def test_the_kept_record_names_the_reason_and_the_attempts():
    record = jobs.kept_previous_record("קריאת התמונות במודל נכשלה זמנית (timeout)", attempts=3)
    assert record["reason"] == "קריאת התמונות במודל נכשלה זמנית (timeout)" and record["attempts"] == 3
    assert record["at"]  # when it was kept


def test_the_kept_record_reason_is_bounded():
    assert len(jobs.kept_previous_record("א" * 2000, attempts=1)["reason"]) <= 500


def test_the_regression_message_does_not_make_an_admin_a_precondition():
    """The document stays available without an admin; accepting the new reading is optional."""
    msg = pipeline.MSG_REGRESSION.format(summary="עמוד 1: חסרים המספרים 120")
    assert "120" in msg and "בהרצה חוזרת" not in msg
    assert pipeline.ReadingRegression({"pages": [{"page": 1, "missing_numbers": ["120"]}]}).permanent is True
