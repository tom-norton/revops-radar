"""The weekly review's tracker parsing: the one place a date has no year and a status can
be out of date, so both rules are pinned here."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tools"))
import review_data as rd  # noqa: E402

TODAY = datetime(2026, 9, 28, tzinfo=timezone.utc)


def test_a_yearless_date_is_the_latest_one_not_in_the_future():
    assert rd.tracker_date("9/25", TODAY).date().isoformat() == "2026-09-25"
    jan = datetime(2027, 1, 5, tzinfo=timezone.utc)
    assert rd.tracker_date("12/30", jan).date().isoformat() == "2026-12-30"
    assert rd.tracker_date("", TODAY) is None
    assert rd.tracker_date("soon", TODAY) is None


def test_three_weeks_in_applied_counts_as_no_response():
    """Tom's rule: he does not always move a row to "No response", so the review does."""
    old, fresh = rd.tracker_date("9/2", TODAY), rd.tracker_date("9/20", TODAY)
    assert rd.outcome_of({"Status": "Applied"}, old, TODAY) == "no_response"
    assert rd.outcome_of({"Status": "Applied"}, fresh, TODAY) == "pending"
    assert rd.outcome_of({"Status": "Rejected"}, fresh, TODAY) == "rejected"
    # An interview is the outcome that matters, even when the row ended in a rejection.
    assert rd.outcome_of({"Status": "Rejected", "Interview Date": "7/16"}, old,
                         TODAY) == "interview"


def test_interview_rate_counts_only_decided_applications():
    apps = [{"outcome": o, "m": "NL"} for o in ("interview", "rejected", "pending", "pending")]
    t = rd.outcome_table(apps, lambda a: a["m"])["NL"]
    assert t["applied"] == 4 and t["interview_rate"] == 0.5
