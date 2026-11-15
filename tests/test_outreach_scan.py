"""The scan's push for new roles at outreach-list companies (scan.notify_outreach_list).

    python tests/test_outreach_scan.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scan  # noqa: E402

COMPANIES = [
    {"company": "BWise (SAI360)", "keys": ["bwise", "sai360"], "active": True, "signal": "Warm"},
    {"company": "Workiva", "keys": ["workiva"], "active": False, "signal": "Hot"},
]


def test_outreach_key_matches_the_dashboard_normaliser():
    assert scan.outreach_key("Salesforce Ireland Ltd") == "salesforce"
    assert scan.outreach_key("BWise (SAI360)") == "bwise"
    assert scan.outreach_key("Miró") == "miro"
    assert scan.outreach_key("Salesforce Partner Canada") == "salesforce partner"


def test_any_score_at_an_active_company_is_a_hit():
    hits = scan.outreach_hits([{"company": "SAI360 Inc.", "title": "CSM", "score": 4.2}], COMPANIES)
    assert len(hits) == 1 and hits[0][1]["company"] == "BWise (SAI360)"


def test_a_listed_company_that_is_not_active_needs_the_floor():
    low = {"company": "Workiva", "title": "Analyst", "score": scan.FLOOR - 0.5, "domain_scored": False}
    high = {"company": "Workiva Netherlands B.V.", "title": "RevOps Manager", "score": scan.FLOOR}
    hits = scan.outreach_hits([low, high], COMPANIES)
    assert [h[0]["title"] for h in hits] == ["RevOps Manager"]


def test_a_company_not_on_the_list_is_never_a_hit():
    assert scan.outreach_hits([{"company": "Salesforce", "title": "x", "score": 9}], COMPANIES) == []
    assert scan.outreach_hits([{"company": "Salesforce", "title": "x", "score": 9}], []) == []


def test_no_token_means_no_request():
    calls = []
    old = os.environ.pop("TRACKER_TOKEN", None)
    real = scan.requests.post
    scan.requests.post = lambda *a, **k: calls.append(1)
    try:
        scan.notify_outreach_list([{"company": "SAI360", "title": "x", "score": 7}])
    finally:
        scan.requests.post = real
        if old is not None:
            os.environ["TRACKER_TOKEN"] = old
    assert calls == []


def test_a_failed_fetch_gives_an_empty_list():
    def boom(*a, **k):
        raise RuntimeError("down")
    assert scan.outreach_companies("tok", get=boom) == []

    class R:
        def json(self):
            return {"ok": False, "error": "bad token"}
    assert scan.outreach_companies("tok", get=lambda *a, **k: R()) == []


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print("ok  ", t.__name__)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL", t.__name__, repr(e))
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
