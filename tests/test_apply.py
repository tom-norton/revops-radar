#!/usr/bin/env python3
"""Tests for the offline half of applyq.py -- the comp-risk gate, the answer handling, and
the multi-tick state machine. No network, no API key, no Telegram, no git.

    python tests/test_apply.py        (or: python -m pytest tests/)

The state-machine tests are the point of this file. Every stage boundary in applyq.py is a
place where the run stops, persists, and resumes on a later tick, possibly days later, and
none of that is observable by reading one run's log. They drive advance() across simulated
ticks with the model calls and the chat stubbed out, and assert what Tom would actually
see: one question at a time, no question the answer bank already covers, and -- the rule
this whole build hangs off -- no bullet drafted from a gap he did not answer.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import applyq  # noqa: E402


# ---------------------------------------------------------------- stubs

class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def last(self):
        return self.sent[-1] if self.sent else ""


class FakeBank:
    """In-memory stand-in for the private repo."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.commits = []

    def read(self, rel, default=""):
        return self.files.get(rel, default)

    def write(self, rel, text):
        self.files[rel] = text

    def append(self, rel, text):
        self.files[rel] = self.files.get(rel, "") + text

    def save_state(self, state):
        self.files[applyq.STATE_FILE] = json.dumps(state)

    def commit(self, message):
        self.commits.append(message)
        return "abc1234"


JOB = {
    "id": "az-nl-1", "title": "Revenue Operations Manager", "company": "Acme",
    "location": "Amsterdam", "market": "NL", "score": 7.8,
    "url": "https://example.com/job", "description": "Own the GTM stack. SQL required.",
    "verdict": "Strong fit.",
    "comp": {"stated": True, "min_base": 95000, "currency": "EUR"},
}

AUDIT = {
    "track": "BUILDER", "track_rationale": "Systems ownership leads the posting.",
    "bullets": [{"bank_id": "NAVEX-01", "text_start": "Managed 5.2M ARR across 22",
                 "decision": "KEEP+KEY", "rationale": "Core scope.", "new_to_bank": False}],
    "metric_gaps": [],
    "gaps": [
        {"keyword": "pipeline hygiene", "adjacent": "CRM cleanup at NAVEX",
         "answered_by": "", "question": "Did you do pipeline data cleanup?",
         "options": ["Yes, pipeline cleanup", "Yes, forecast accuracy",
                     "No meaningful experience"]},
        {"keyword": "dashboard building", "adjacent": "QBR reporting",
         "answered_by": "", "question": "Did you build dashboards yourself?",
         "options": ["Yes, in Salesforce", "No meaningful experience"]},
    ],
}


def machine(audit=None, comp=None, drafts=None):
    """A state machine wired to stubs. Returns (step, state, tg, bank, calls) where step()
    runs one tick with an optional inbound message from Tom."""
    tg, bank = FakeTelegram(), FakeBank({applyq.BANK_FILE: "### [NAVEX-01]\nText: ..."})
    calls = {"audit": 0, "draft": 0, "salary": 0, "drafted_from": None}
    job = dict(JOB, comp=comp if comp is not None else JOB["comp"])

    def fake_audit(api_key, j, profile, bank_md, answers_md):
        calls["audit"] += 1
        return json.loads(json.dumps(audit if audit is not None else AUDIT))

    def fake_draft(api_key, j, track, answered):
        calls["draft"] += 1
        calls["drafted_from"] = [(g.get("keyword"), a) for g, a in answered]
        return drafts if drafts is not None else {"bullets": [], "dropped": []}

    def fake_salary(api_key, j):
        calls["salary"] += 1
        return {"sources": ["Glassdoor - 80-95K"], "low": 80000, "high": 95000,
                "currency": "EUR", "thirty_percent_ruling": "yes",
                "verdict": "above_floor", "notes": ""}

    applyq.run_audit, applyq.draft_bullets, applyq.research_salary = (
        fake_audit, fake_draft, fake_salary)
    applyq.scan.load_profile = lambda: "(profile)"

    state = {"current": {"id": job["id"], "title": job["title"], "company": job["company"],
                         "stage": "salary_gate", "started_at": "2026-08-29T00:00:00+00:00",
                         "answers": [], "usage": {}},
             "history": []}

    def step(message=""):
        return applyq.advance(state, job, bank, tg, "key", message)

    return step, state, tg, bank, calls


# ---------------------------------------------------------------- comp risk

def test_comp_risk_fires_when_no_salary_is_stated():
    risky, reason = applyq.comp_risk({"market": "NL"})
    assert risky is True
    assert "no salary stated" in reason


def test_comp_risk_is_quiet_on_a_band_clear_of_the_floor():
    assert applyq.comp_risk(
        {"market": "NL", "comp": {"stated": True, "min_base": 95000,
                                  "currency": "EUR"}})[0] is False


def test_comp_risk_fires_inside_the_margin_above_the_floor():
    # NL floor is 71,304; 73,000 clears it but sits inside the 10% margin, which is the
    # case the gate exists for -- a band whose bottom is the visa minimum leaves nothing
    # for the gap between the advert and the offer.
    assert applyq.comp_risk(
        {"market": "NL", "comp": {"stated": True, "min_base": 73000,
                                  "currency": "EUR"}})[0] is True


def test_comp_risk_never_guesses_across_currencies():
    risky, reason = applyq.comp_risk(
        {"market": "NL", "comp": {"stated": True, "min_base": 90000, "currency": "GBP"}})
    assert risky is True
    assert "not directly comparable" in reason


# ---------------------------------------------------------------- answers

def test_a_bare_number_picks_the_option():
    assert applyq.resolve_answer("2", {"options": ["a", "b", "c"]}) == "b"
    assert applyq.resolve_answer("2.", {"options": ["a", "b", "c"]}) == "b"


def test_free_text_is_kept_verbatim():
    gap = {"options": ["a", "b"]}
    assert applyq.resolve_answer("cleaned up 400 stale opps", gap) == "cleaned up 400 stale opps"


def test_an_out_of_range_number_is_not_silently_an_option():
    assert applyq.resolve_answer("7", {"options": ["a", "b"]}) == "7"


def test_no_material_answers_are_recognised():
    for a in ("no", "No meaningful experience", "nothing", "n/a", "  ", ""):
        assert applyq.has_material(a) is False, a
    assert applyq.has_material("Yes, ran the cleanup for two quarters") is True


def test_gaps_the_answer_bank_covers_are_never_asked():
    gaps = applyq.pending_gaps({"gaps": [
        {"question": "q1", "answered_by": "A-20260101-01"},
        {"question": "q2", "answered_by": ""}]})
    assert [g["question"] for g in gaps] == ["q2"]


def test_gap_questions_are_capped():
    many = {"gaps": [{"question": f"q{i}", "answered_by": ""} for i in range(9)]}
    assert len(applyq.pending_gaps(many)) == applyq.MAX_GAP_QUESTIONS


# ---------------------------------------------------------------- Telegram plumbing

def test_only_toms_chat_is_read():
    updates = [{"update_id": 1, "message": {"text": "hi", "chat": {"id": 99}}},
               {"update_id": 2, "message": {"text": "yes", "chat": {"id": 42}}}]
    assert applyq.message_texts(updates, "42") == [(2, "yes")]


def test_firebase_object_form_queue_reads_as_a_list():
    assert applyq.as_id_list({"1": "b", "0": "a"}) == ["a", "b"]
    assert applyq.as_id_list(None) == []
    assert applyq.as_id_list(["a", None, ""]) == ["a"]


# ---------------------------------------------------------------- state machine

def test_no_comp_risk_skips_the_salary_gate_entirely():
    step, state, tg, _bank, calls = machine()
    step()
    assert calls["salary"] == 0
    assert state["current"]["salary"] is None
    # Straight through the gate and the audit, stopping on the first question.
    assert calls["audit"] == 1
    assert "Question 1 of 2" in tg.last()


def test_comp_risk_asks_before_spending_anything_on_research():
    step, state, tg, _bank, calls = machine(comp={"stated": False})
    step()
    assert calls["salary"] == 0 and calls["audit"] == 0
    assert "Comp risk" in tg.sent[0]
    assert state["current"]["stage"] == "salary_gate"

    step("1")                       # yes, research it
    assert calls["salary"] == 1
    assert state["current"]["stage"] == "gap_interview"


def test_declining_research_proceeds_without_it():
    step, state, tg, _bank, calls = machine(comp={"stated": False})
    step()
    step("2")                       # no
    assert calls["salary"] == 0
    assert state["current"]["salary"]["skipped"] is True
    assert calls["audit"] == 1


def test_questions_are_asked_one_at_a_time_across_ticks():
    step, state, tg, _bank, _calls = machine()
    step()
    assert "Question 1 of 2" in tg.last()
    assert "pipeline hygiene" in tg.last()

    # A tick with no message from Tom changes nothing and asks nothing again. This is the
    # "no timeout" rule: the run sits here for as long as it takes.
    before = len(tg.sent)
    step()
    assert len(tg.sent) == before
    assert state["current"]["gap_index"] == 0

    step("1")
    assert "Question 2 of 2" in tg.last()
    assert state["current"]["gap_index"] == 1
    assert state["current"]["answers"][0]["answer"] == "Yes, pipeline cleanup"


def test_a_full_run_ends_with_a_packet_and_a_single_commit():
    step, state, tg, bank, calls = machine(
        drafts={"bullets": [{"gap": "pipeline hygiene", "text": "Cleaned up the pipeline.",
                             "tracks": "BUILDER", "competencies": "CRM",
                             "evidence": "Gap interview", "notes": ""}],
                "dropped": []})
    step()
    step("1")
    finished = step("Yes, built them in Salesforce")
    assert finished is True
    assert state["current"] is None
    assert state["history"][0]["outcome"] == "done"

    packets = [k for k in bank.files if k.startswith(applyq.PACKET_DIR + "/")]
    assert len(packets) == 1
    body = bank.files[packets[0]]
    assert "Track: **BUILDER**" in body
    assert "Cleaned up the pipeline." in body
    # One commit for the packet, the answers and the state together, so a crash can never
    # leave a packet on disk that the state file thinks is unfinished.
    assert len(bank.commits) == 1


def test_answers_reach_the_answer_bank_verbatim():
    step, _state, _tg, bank, _calls = machine()
    step()
    step("1")
    step("Built two Salesforce dashboards for the CS team")
    bank_text = bank.files[applyq.ANSWER_FILE]
    assert "Built two Salesforce dashboards for the CS team" in bank_text
    assert "Did you build dashboards yourself?" in bank_text
    # Job id recorded, so a future run can see which role earned the answer.
    assert JOB["id"] in bank_text


def test_a_no_answer_is_still_banked_so_it_is_not_asked_twice():
    step, _state, _tg, bank, calls = machine()
    step()
    step("No meaningful experience")
    step("No meaningful experience")
    assert "No meaningful experience" in bank.files[applyq.ANSWER_FILE]
    # ...but nothing was handed to the drafter, so nothing can be written from it.
    assert calls["drafted_from"] == []


def test_an_unanswered_gap_never_reaches_the_drafter():
    """The single most important rule in the build: a gap with no material in the answer
    produces no bullet, and the drafting call is not even shown it."""
    step, _state, _tg, _bank, calls = machine()
    step()
    step("Cleaned 400 stale opportunities over two quarters")
    step("no")
    assert calls["drafted_from"] == [
        ("pipeline hygiene", "Cleaned 400 stale opportunities over two quarters")]


def test_a_drafting_failure_is_loud_and_still_leaves_a_packet():
    step, _state, tg, bank, _calls = machine()

    def boom(*a, **k):
        raise RuntimeError("model unavailable")

    applyq.draft_bullets = boom
    step()
    step("1")
    step("1")
    assert any("drafting failed" in s for s in tg.sent)
    assert [k for k in bank.files if k.startswith(applyq.PACKET_DIR + "/")]


def test_a_role_with_no_gaps_finishes_without_asking_anything():
    step, state, tg, bank, calls = machine(audit=dict(AUDIT, gaps=[]))
    finished = step()
    assert finished is True
    assert calls["drafted_from"] == []
    assert not any("Question" in s for s in tg.sent)
    assert state["history"][0]["outcome"] == "done"


def test_a_failing_audit_retries_a_few_ticks_then_gives_up_loudly():
    """A blip deserves a retry. A broken call deserves to stop, because at 15-minute ticks
    an audit that always fails is an Opus call every quarter hour with nobody watching."""
    step, state, tg, _bank, _calls = machine()

    def boom(*a, **k):
        raise RuntimeError("500 from the API")

    applyq.run_audit = boom
    for _ in range(applyq.MAX_STAGE_RETRIES - 1):
        step()
        assert state["current"] is not None
        assert not tg.sent          # quiet while it is still just retrying
    step()
    assert state["current"] is None
    assert state["history"][0]["outcome"] == "audit-failed"
    assert "Gave up" in tg.last() and "500 from the API" in tg.last()


def test_answer_bank_ids_do_not_collide_within_a_day():
    existing = "### [A-%s-01] - x\n" % applyq.datetime.now(
        applyq.timezone.utc).strftime("%Y%m%d")
    assert applyq.next_answer_index(existing) == 2
    assert applyq.next_answer_index("") == 1


def _run():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  pass  {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}: {e or '(assertion)'}")
        except Exception as e:
            failed.append(name)
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
