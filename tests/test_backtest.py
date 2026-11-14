"""tools/backtest.py: the join from a tracker row to a radar row, and the rank on the day.
Run with: python tests/test_backtest.py"""
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import backtest  # noqa: E402

TRACKER = """Company,Title,Location,Score,Date Applied,Status,Link,Referral?,Interview Date
Acme,Revenue Operations Manager,UK,7,11/4,Rejected,https://jobs.ashbyhq.com/acme/1?utm=x,,
Beta BV,Senior Customer Success Manager,Netherlands,7,11/4,Rejected,,,11/10
Gamma,Sales Ops Lead,UK,7,11/4,Rejected,,,
"""


def _snaps():
    rows = [
        {"id": "a", "company": "Acme", "title": "Revenue Operations Manager", "market": "UK-London",
         "url": "https://jobs.ashbyhq.com/acme/1", "score": 6.0, "found_at": "2026-11-02T00:00"},
        {"id": "b", "company": "Beta", "title": "Senior Customer Success Manager",
         "market": "NL", "url": "https://x/b", "score": 8.0, "found_at": "2026-11-03T00:00"},
        {"id": "c", "company": "Other", "title": "RevOps Lead", "market": "NL",
         "url": "https://x/c", "score": 7.0, "found_at": "2026-11-03T00:00"},
    ]
    return {"2026-11-02": rows[:1], "2026-11-04": rows}


def test_join_by_link_then_by_company_and_title():
    apps = backtest.run(TRACKER, _snaps(),
                        today=datetime(2026, 11, 14, tzinfo=timezone.utc))
    by = {a["company"]: a for a in apps}
    assert by["Acme"]["matched"] and by["Acme"]["id"] == "a"          # link, query stripped
    assert by["Beta BV"]["matched"] and by["Beta BV"]["id"] == "b"    # name and title
    assert not by["Gamma"]["matched"]
    assert by["Beta BV"]["outcome"] == "interview"
    # ranks on that day's board: Beta 8.0 is the top of three, Acme 6.0 the bottom
    assert by["Beta BV"]["pct"]["radar score"] == 1.0
    assert by["Acme"]["pct"]["radar score"] == 0.0


def test_summary_counts_groups():
    apps = backtest.run(TRACKER, _snaps(), today=datetime(2026, 11, 14, tzinfo=timezone.utc))
    s = {g["group"]: g for g in backtest.summary(apps)}
    assert s["screens"]["screens"] == 1 and s["screens"]["on_radar"] == 1
    assert s["UK"]["applied"] == 2


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("  pass ", n)
            except AssertionError as e:
                fails += 1; print("  FAIL ", n, e)
    sys.exit(1 if fails else 0)
