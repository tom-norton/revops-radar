"""tools/lamp_signals.py: the pure parts. No network, no git.

    python tests/test_lamp_signals.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lamp_signals as ls  # noqa: E402


def test_company_key_matches_the_dashboard_normaliser():
    assert ls.company_key("Salesforce Ireland Ltd") == "salesforce"
    assert ls.company_key("BWise (SAI360)") == "bwise"
    assert ls.company_key("Dun & Bradstreet") == "dun and bradstreet"
    assert ls.company_key("Salesforce Partner Canada") == "salesforce partner"


def test_names_and_keys_include_aliases_and_drop_the_bracket():
    c = {"company": "BWise (SAI360)", "aliases": ["SAI360"]}
    assert ls.names_of(c) == ["BWise", "SAI360"]
    assert ls.keys_of(c) == {"bwise", "sai360"}


def test_slug_guesses_never_fall_back_to_the_first_word():
    c = {"company": "Privacy Company", "aliases": [], "domain": "privacycompany.eu"}
    assert "privacy" not in ls.slug_guesses(c)
    assert ls.slug_guesses(c)[0] == "privacycompany"
    c = {"company": "Be Informed", "aliases": [], "domain": "https://www.beinformed.com/"}
    assert "be" not in ls.slug_guesses(c)
    assert "beinformed" in ls.slug_guesses(c)


def test_board_override_none_skips_the_board():
    assert ls.board_for({"company": "Relativity", "board": "none"}) == ("", "", [])


def test_burst_needs_three_more_sales_postings_and_no_relevant_role():
    assert ls.burst({"sales_cs_open": 10, "relevant_open": 0}, {"sales_cs_open": 7})
    assert not ls.burst({"sales_cs_open": 9, "relevant_open": 0}, {"sales_cs_open": 7})
    assert not ls.burst({"sales_cs_open": 20, "relevant_open": 1}, {"sales_cs_open": 7})
    assert not ls.burst({"sales_cs_open": 20, "relevant_open": 0}, None)


def test_newly_on_register_only_counts_a_change_onto_the_register():
    assert ls.newly_on({"uk": "on_register", "nl": "not_found"},
                       {"uk": "not_found", "nl": "not_found"}) == ["uk"]
    assert ls.newly_on({"uk": "on_register"}, {"uk": "on_register"}) == []
    assert ls.newly_on({"uk": "on_register"}, None) == []
    assert ls.newly_on({"uk": "unknown"}, {"uk": "not_found"}) == []


def test_count_board_splits_sales_and_relevant_roles():
    jobs = [{"title": "Account Executive", "location": "London"},
            {"title": "Senior Customer Success Manager", "location": "Amsterdam"},
            {"title": "Revenue Operations Manager", "location": "London"},
            {"title": "Backend Engineer", "location": "Amsterdam"}]
    n = ls.count_board(jobs)
    assert n["open_total"] == 4
    assert n["sales_cs_open"] == 2
    assert n["relevant_open"] == 2  # the RevOps role and the Dutch CSM role


def test_radar_now_and_past_roles_match_on_company_key():
    c = {"company": "Fonoa", "aliases": []}
    rows = [{"company": "Fonoa Ltd", "title": "RevOps Manager", "market": "UK-London",
             "found_at": "2026-11-10T08:00:00", "score": 7.0},
            {"company": "Fonoa", "title": "Old one", "market": "UK-London",
             "found_at": "2026-09-01T08:00:00", "score": 7.0},
            {"company": "Fonoa", "title": "Low score", "market": "UK-London",
             "found_at": "2026-11-12T08:00:00", "score": 5.0}]
    assert ls.radar_now(c, rows, date(2026, 11, 15)) == (["RevOps Manager (UK-London, 7.0)"], [])
    assert ls.past_roles(c, rows[1:2]) == ["Old one (UK-London, 2026-09-01)"]


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
