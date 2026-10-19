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


class _Reply:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise rd.requests.HTTPError(f"{self.status_code} for url: https://x/exec?token=SECRET")


CSV = ("Company,Title,Location,Score,Date Applied,Status,Link,Referral?,Interview Date\r\n"
       'Acme,"Revenue Operations Manager, EMEA",Netherlands,7.4,9/25,Applied,,,\r\n')


def test_the_tracker_comes_from_the_web_app_and_parses():
    import io
    seen = {}

    def get(url, params, timeout):
        seen.update(params)
        return _Reply(CSV)
    text = rd.fetch_tracker("https://x/exec", "SECRET", get=get)
    assert seen == {"action": "applications", "token": "SECRET"}
    apps = rd.applications(io.StringIO(text), [], TODAY, TODAY.isoformat())
    assert apps["total"] == 1
    assert apps["by_market"]["NL"]["applied"] == 1


def test_a_tracker_error_is_refused_and_never_carries_the_token():
    cases = [_Reply('{"ok":false,"error":"bad token"}'),
             _Reply('{"ok":false,"error":"unknown action"}'),
             _Reply("<!DOCTYPE html><html>Sign in</html>"),
             _Reply("", status=403)]
    for reply in cases:
        try:
            rd.fetch_tracker("https://x/exec", "SECRET", get=lambda *a, **k: reply)
        except ValueError as e:
            assert "SECRET" not in str(e), str(e)
        else:
            raise AssertionError(f"accepted {reply.text!r}")
