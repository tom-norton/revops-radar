#!/usr/bin/env python3
"""Tests for the subscription-scoring trial: scan.py's capture hook and shadow.py.

    python tests/test_shadow.py        (or: python -m pytest tests/)

What matters most here is that the trial asks precisely what the API side asks. If the
prompt drifts, a score gap is a prompt difference rather than a model difference and the
whole week of comparison measures the wrong thing -- so the prompt tests compare against
scan.py's own functions, not against a copy.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scan  # noqa: E402
import shadow  # noqa: E402

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def job(i="j1", **kw):
    base = {"id": i, "title": "Revenue Operations Manager", "company": "Acme",
            "location": "Amsterdam", "market": "NL", "sponsor": "on register",
            "source": "ats", "description": "Own the forecast. " * 200,
            "_watched": True, "score": 7}
    base.update(kw)
    return base


def score_data(n=7, **kw):
    d = {"dimensions": {k: n for k in scan.RUBRIC_KEYS}, "function_match": "core",
         "company_standout": False, "language_hard_requirement": False,
         "salary_stated": False, "salary_min_base": 0, "salary_max_base": 0,
         "salary_currency": "", "posting_location": "Amsterdam",
         "work_arrangement": "hybrid", "employment_type": "permanent",
         "hard_gaps": [], "flags": [], "verdict": "ok"}
    d.update(kw)
    return d


# ---------------------------------------------------------------- capture (scan.py)

def test_capture_is_off_without_the_flag():
    old = os.environ.pop("SHADOW_QUEUE", None)
    try:
        assert not scan.shadow_enabled()
        os.environ["SHADOW_QUEUE"] = "0"
        assert not scan.shadow_enabled()
        os.environ["SHADOW_QUEUE"] = "1"
        assert scan.shadow_enabled()
    finally:
        os.environ.pop("SHADOW_QUEUE", None)
        if old is not None:
            os.environ["SHADOW_QUEUE"] = old


def test_captured_row_rebuilds_the_identical_prompt():
    j = job(description="x" * 50000, salary="EUR 80000")
    row = scan.shadow_row(j, NOW)
    assert scan.job_message(row) == scan.job_message(j)
    # carries only what the prompt needs, not pipeline-internal state
    assert "_watched" not in row and "score" not in row
    assert row["captured_at"] == NOW.isoformat()


def test_merge_keeps_first_capture_and_prunes_old_rows():
    old = scan.shadow_row(job("old"), NOW - timedelta(days=scan.SHADOW_KEEP_DAYS + 1))
    first = scan.shadow_row(job("j1", title="First"), NOW)
    again = scan.shadow_row(job("j1", title="Again"), NOW)
    q = scan.shadow_merge({"old": old, "j1": first}, [again], NOW)
    assert "old" not in q
    assert q["j1"]["title"] == "First"


def test_flush_writes_and_never_raises():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "shadow", "queue.json")
        scan.shadow_flush([scan.shadow_row(job(), NOW)], path, NOW)
        assert list(json.load(open(path))) == ["j1"]
        # an unwritable path is reported, not raised
        scan.shadow_flush([scan.shadow_row(job(), NOW)], "/proc/nope/queue.json", NOW)


# ---------------------------------------------------------------- prompts (shadow.py)

def test_prompts_carry_scans_own_system_and_user_text():
    row = scan.shadow_row(job(), NOW)
    s = shadow.screen_prompt(row, "a.json")
    assert scan.SCREEN_SYSTEM in s and scan.job_message(row) in s
    system = scan.score_system()
    p = shadow.score_prompt(row, "b.json", system)
    assert system in p and scan.job_message(row) in p
    assert json.dumps(scan.SCORE_SCHEMA, indent=1) in p
    assert "ANSWER FILE: b.json" in p


def test_keys_are_filename_safe_and_distinct():
    a = shadow.key_for("https://x.com/a/b?c=1")
    b = shadow.key_for("https_//x.com/a/b_c=1")
    assert "/" not in a and ":" not in a and "?" not in a
    assert a != b


# ---------------------------------------------------------------- prepare / ingest

def _in_tmp(fn):
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            return fn()
        finally:
            os.chdir(cwd)


def test_round_trip_screen_kill_keep_and_score():
    def run():
        queue = {i: scan.shadow_row(job(i), NOW) for i in ("keep", "kill", "half")}
        shadow.cmd_prepare(queue=queue, results={})
        man = json.load(open("shadow/work/manifest.json"))
        key = {v: k for k, v in man.items()}
        P = {i: shadow.paths(key[i]) for i in queue}
        json.dump({"keep": True, "reason": ""}, open(P["keep"]["screen_answer"], "w"))
        json.dump(score_data(8), open(P["keep"]["score_answer"], "w"))
        # a plain function kill on a non-core-title row is not overruled...
        queue["kill"]["title"] = "Marketing Coordinator"
        json.dump({"keep": False, "reason": "marketing role"},
                  open(P["kill"]["screen_answer"], "w"))
        # ...and a kept row with no score answer yet waits for the next pass
        json.dump({"keep": True, "reason": ""}, open(P["half"]["screen_answer"], "w"))
        res = shadow.cmd_ingest(queue=queue)
        assert res["keep"]["outcome"] == "scored"
        assert res["keep"]["score"] == scan.parse_score_result(
            queue["keep"], score_data(8))["score"]
        assert res["kill"]["outcome"] == "killed"
        assert "half" not in res
        screen, score = shadow.cmd_todo()
        assert screen == [] and score == [P["half"]["score"]]
    _in_tmp(run)


def test_overruled_kill_goes_on_to_be_scored():
    def run():
        queue = {"q": scan.shadow_row(job("q"), NOW)}
        shadow.cmd_prepare(queue=queue, results={})
        p = shadow.paths(next(iter(json.load(open("shadow/work/manifest.json")))))
        json.dump({"keep": False, "reason": "quota-carrying role"},
                  open(p["screen_answer"], "w"))
        json.dump(score_data(6), open(p["score_answer"], "w"))
        res = shadow.cmd_ingest(queue=queue)
        assert res["q"]["outcome"] == "scored" and res["q"]["overruled"]
    _in_tmp(run)


def test_unreadable_answer_is_asked_again():
    def run():
        queue = {"q": scan.shadow_row(job("q"), NOW)}
        shadow.cmd_prepare(queue=queue, results={})
        p = shadow.paths(next(iter(json.load(open("shadow/work/manifest.json")))))
        open(p["screen_answer"], "w").write("I think this is a keep")
        res = shadow.cmd_ingest(queue=queue)
        assert "q" not in res and not os.path.exists(p["screen_answer"])
    _in_tmp(run)


# ---------------------------------------------------------------- compare

def test_compare_counts_agreement_and_flips():
    results = {
        "a": {"outcome": "scored", "score": 7.0, "title": "A", "company": "x", "at": "2026-11-10"},
        "b": {"outcome": "scored", "score": 6.0, "title": "B", "company": "x", "at": "2026-11-10"},
        "c": {"outcome": "killed", "at": "2026-11-11"},
        "d": {"outcome": "scored", "score": 5.0, "at": "2026-11-11"},
    }
    api = shadow.api_outcomes(
        jobs=[{"id": "a", "score": 7.4}, {"id": "b", "score": 6.8},
              {"id": "z", "score": None, "evidence": "thin"}],
        kills=[{"id": "c"}], excluded={"rows": [{"id": "d", "stage": "stage1-kill"}]})
    s = shadow.compare(results, api, queue_size=6)
    assert s["n"] == 2
    assert s["mean_diff"] == -0.6 and s["mean_abs"] == 0.6
    assert s["screen"]["both kept"] == 2 and s["screen"]["both killed"] == 1
    assert s["screen"]["API killed, trial kept"] == 1
    assert [t[0] for t in s["gate_flips"]] == ["b"]      # 6.0 vs 6.8 straddles 6.5
    assert s["pending"] == 2
    assert s["per_day"]["2026-11-10"] == {"screened": 2, "scored": 2}
    md = shadow.render(s)
    assert "2 roles scored by both" in md and "B @ x" in md


def _run():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
