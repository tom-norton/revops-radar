#!/usr/bin/env python3
"""Tests for subscription scoring: scan.py's queue and apply step, and subscore.py.

    python tests/test_subscore.py        (or: python -m pytest tests/)

The two things that matter most: the session is asked exactly what score_job() would
have sent, and an answer turns into exactly the row score_job()'s answer would have made.
Both are checked against scan.py's own functions, not against copies.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import scan  # noqa: E402
import subscore  # noqa: E402

NOW = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)


def job(i="j1", **kw):
    base = {"id": i, "title": "Revenue Operations Manager", "company": "Acme",
            "location": "Amsterdam", "market": "NL", "sponsor": "on register",
            "source": "ats", "url": "https://boards.greenhouse.io/acme/jobs/1",
            "description": "Own the forecast and the territory model. " * 900,
            "_watched": True}
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


class Sandbox:
    """A temporary radar directory with the files the apply step reads and writes."""

    def __init__(self, queue, jobs=()):
        self.d = tempfile.TemporaryDirectory()
        self.old = os.getcwd()
        os.chdir(self.d.name)
        os.makedirs("docs"); os.makedirs("scoring")
        json.dump(queue, open(scan.SCORE_QUEUE_FILE, "w"))
        json.dump(list(jobs), open("docs/jobs.json", "w"))
        json.dump({"rows": []}, open("docs/excluded.json", "w"))
        json.dump({"last_run": "x"}, open("docs/status.json", "w"))
        scan.DROPS.clear(); scan.DROP_COUNTS.clear()

    def read(self, path):
        return json.load(open(path))

    def close(self):
        os.chdir(self.old)
        self.d.cleanup()


def entry(j=None, **kw):
    return scan.score_queue_entry(j or job(), now=NOW, **kw)


# ---------------------------------------------------------------- queue (scan.py)

def test_scoring_is_subscription_unless_told_otherwise():
    old = os.environ.pop("SCORER", None)
    try:
        assert scan.subscription_scoring()
        os.environ["SCORER"] = "api"
        assert not scan.subscription_scoring()
    finally:
        os.environ.pop("SCORER", None)
        if old is not None:
            os.environ["SCORER"] = old


def test_entry_carries_the_identical_prompt_and_a_clean_row():
    j = job(salary="EUR 80000")
    e = entry(j)
    assert e["message"] == scan.job_message(j)
    assert "_watched" not in e["job"]
    assert len(e["job"]["description"]) <= scan.DESC_STORE_CAP + 100
    assert e["id"] == "j1" and e["kind"] == "new" and not e["skip_screen"]


def test_queue_keeps_first_entry_but_a_rescore_replaces():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "q.json")
        scan.queue_for_scoring([entry(job(title="First"))], p)
        scan.queue_for_scoring([entry(job(title="Again"))], p)
        assert json.load(open(p))["j1"]["job"]["title"] == "First"
        scan.queue_for_scoring([entry(job(title="Rescore"), kind="rescore")], p)
        assert json.load(open(p))["j1"]["job"]["title"] == "Rescore"


# ---------------------------------------------------------------- outcome

def test_outcome_waits_for_a_complete_answer():
    e = entry()
    assert scan.subscription_outcome(e, None) is None
    assert scan.subscription_outcome(e, {"screen": {"keep": True}}) is None


def test_outcome_kill_overrule_and_score_match_the_scan():
    e = entry()
    out = scan.subscription_outcome(e, {"screen": {"keep": False, "reason": "wrong function: legal"}})
    assert out == {"outcome": "killed", "reason": "wrong function: legal"}
    # A quota kill on a core title is overruled by the scan's own rule, and then scored.
    answer = {"screen": {"keep": False, "reason": "quota-carrying"}, "score": score_data()}
    out = scan.subscription_outcome(e, answer)
    assert out["outcome"] == "scored"
    assert out["result"] == scan.parse_score_result(e["job"], score_data())


def test_home_domain_entry_skips_the_screen():
    e = entry(skip_screen=True)
    assert scan.subscription_outcome(e, {"score": score_data()})["outcome"] == "scored"


# ---------------------------------------------------------------- apply

def test_apply_adds_kills_and_empties_the_queue():
    q = {"j1": entry(job("j1")), "j2": entry(job("j2", title="Sales Operations Manager"))}
    results = {"j1": {"screen": {"keep": True}, "score": score_data(8)},
               "j2": {"screen": {"keep": False, "reason": "wrong function: legal"}}}
    s = Sandbox(q)
    try:
        r = scan.cmd_apply_scores(results, now=NOW, notify=False)
        assert r["scored"] == 1 and r["killed"] == 1 and r["pending"] == 0
        rows = s.read("docs/jobs.json")
        assert [x["id"] for x in rows] == ["j1"]
        assert rows[0]["score"] == scan.parse_score_result(q["j1"]["job"], score_data(8))["score"]
        assert rows[0]["found_at"] == NOW.isoformat() and "rank" in rows[0]
        assert s.read(scan.SCORE_QUEUE_FILE) == {}
        assert [k["id"] for k in s.read(scan.KILLS_FILE)] == ["j2"]
        assert s.read("docs/excluded.json")["rows"][0]["stage"] == "stage1-kill"
        assert s.read("docs/status.json")["scoring"]["waiting"] == 0
        # Idempotent: a second apply finds nothing left to do.
        assert scan.cmd_apply_scores(results, now=NOW, notify=False)["applied"] == 0
        assert len(s.read("docs/jobs.json")) == 1
    finally:
        s.close()


def test_apply_leaves_unanswered_and_expires_stale():
    fresh = entry(job("fresh"))
    stale = scan.score_queue_entry(job("stale"), now=NOW - timedelta(days=scan.SCORE_QUEUE_DAYS + 1))
    s = Sandbox({"fresh": fresh, "stale": stale})
    try:
        r = scan.cmd_apply_scores({}, now=NOW, notify=False)
        assert r["expired"] == 1 and r["pending"] == 1
        assert list(s.read(scan.SCORE_QUEUE_FILE)) == ["fresh"]
        assert s.read("docs/excluded.json")["rows"][0]["stage"] == "not-scored"
    finally:
        s.close()


def test_rescore_updates_the_row_in_place():
    board = dict(job("r1"), score=None, evidence="thin", hidden_marker=1)
    board.pop("_watched")
    e = scan.score_queue_entry(board, kind="rescore", skip_screen=True, now=NOW)
    s = Sandbox({"r1": e}, [board])
    try:
        scan.cmd_apply_scores({"r1": {"score": score_data(7)}}, now=NOW, notify=False)
        rows = s.read("docs/jobs.json")
        assert len(rows) == 1 and rows[0]["score"] is not None
        assert "evidence" not in rows[0] and rows[0]["hidden_marker"] == 1
    finally:
        s.close()


def test_disqualified_rescore_leaves_the_board():
    board = dict(job("r1"), score=6.0)
    board.pop("_watched")
    e = scan.score_queue_entry(board, kind="rescore", skip_screen=True, now=NOW)
    s = Sandbox({"r1": e}, [board])
    try:
        scan.cmd_apply_scores({"r1": {"score": score_data(7, language_hard_requirement=True)}},
                              now=NOW, notify=False)
        assert s.read("docs/jobs.json") == []
    finally:
        s.close()


# ---------------------------------------------------------------- subscore.py

def test_session_round_trip_screen_then_score():
    q = {"j1": entry(job("j1")), "h1": entry(job("h1"), skip_screen=True)}
    with tempfile.TemporaryDirectory() as d:
        old = os.getcwd(); os.chdir(d)
        try:
            os.makedirs("scoring")
            work = "scoring/work"
            subscore.cmd_prepare(q, {}, work)
            screen, score = subscore.cmd_todo(q, work)
            # The home-domain role goes straight to the deep score; the other is screened first.
            assert len(screen) == 1 and len(score) == 1
            text = open(screen[0], encoding="utf-8").read()
            assert scan.SCREEN_SYSTEM_SUBSCRIPTION in text and q["j1"]["message"] in text
            assert scan.score_system() in open(score[0], encoding="utf-8").read()
            # Answer the screen; the score for j1 then appears.
            ans = screen[0].replace(".screen.md", ".screen.answer.json")
            json.dump({"keep": True, "reason": "core"}, open(ans, "w"))
            _, score = subscore.cmd_todo(q, work)
            assert len(score) == 2
            for f in score:
                json.dump(score_data(), open(f.replace(".score.md", ".score.answer.json"), "w"))
            results = subscore.cmd_ingest(q, work)
            assert set(results) == {"j1", "h1"}
            assert scan.subscription_outcome(q["j1"], results["j1"])["outcome"] == "scored"
            assert subscore.cmd_todo(q, work) == ([], [])
        finally:
            os.chdir(old)


def test_session_kill_is_final_and_bad_answer_is_asked_again():
    q = {"j1": entry(job("j1")), "j2": entry(job("j2"))}
    with tempfile.TemporaryDirectory() as d:
        old = os.getcwd(); os.chdir(d)
        try:
            os.makedirs("scoring")
            work = "scoring/work"
            subscore.cmd_prepare(q, {}, work)
            screen, _ = subscore.cmd_todo(q, work)
            by = {f: f.replace(".screen.md", ".screen.answer.json") for f in screen}
            j1 = [f for f in screen if "/j1-" in f][0]
            j2 = [f for f in screen if "/j2-" in f][0]
            json.dump({"keep": False, "reason": "wrong function: legal"}, open(by[j1], "w"))
            open(by[j2], "w").write("not json")
            results = subscore.cmd_ingest(q, work)
            assert list(results) == ["j1"] and "score" not in results["j1"]
            assert subscore.cmd_todo(q, work)[0] == [j2]
        finally:
            os.chdir(old)


def test_prune_forgets_only_applied_and_old():
    with tempfile.TemporaryDirectory() as d:
        old = os.getcwd(); os.chdir(d)
        try:
            os.makedirs("scoring")
            recent = NOW.isoformat()
            aged = (NOW - timedelta(days=subscore.KEEP_APPLIED_DAYS + 1)).isoformat()
            json.dump({"waiting": {"at": aged}, "applied-new": {"at": recent},
                       "applied-old": {"at": aged}}, open(subscore.RESULTS_FILE, "w"))
            kept = subscore.cmd_prune({"waiting": {}}, now=NOW)
            assert set(kept) == {"waiting", "applied-new"}
        finally:
            os.chdir(old)


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
