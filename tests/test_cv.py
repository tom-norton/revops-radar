#!/usr/bin/env python3
"""Tests for the CV build -- the offline half. No LibreOffice, no network, no git.

    python tests/test_cv.py        (or: python -m pytest tests/)

Three things are worth testing here and nothing else really is.

1. **The layout policy.** Section placement and project order come from the skill and are
   decided in code from the track, precisely so a model cannot quietly reorder them. If
   they can drift, the reason for putting them in code is gone.
2. **The honesty screen.** Phase 1's rule was that a gap Tom did not answer produces no
   bullet. Phase 2's equivalent is that nothing reaches the page, or the bank, that cannot
   be traced back to the bank, the base CV, or his own words. A fabricated number on a CV
   is a question he cannot answer in an interview.
3. **The round trip.** The whole architecture exists to keep waits down to one per phase.
   These assert that a role above the review line asks once and a role below it asks not at
   all.

The rendering itself is not tested here, because a stub that says the tab stop is fine
proves nothing. That runs for real in tests/test_cv_render.py.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import applyq  # noqa: E402
import bankwrite  # noqa: E402
import cvbuild  # noqa: E402
from test_apply import FakeBank, FakeTelegram, JOB, machine  # noqa: E402


BASE = cvbuild.load_base()[0]
# Captured before anything stubs it: machine() in test_apply.py replaces cvbuild.render
# module-wide, and the render-invocation test below needs the real one.
REAL_RENDER = cvbuild.render


# ---------------------------------------------------------------- the role title

def test_the_jd_title_is_used_when_it_is_usable():
    assert cvbuild.role_title("Revenue Operations Manager", "BUILDER") \
        == "REVENUE OPERATIONS MANAGER"


def test_employer_decoration_is_stripped_off_the_title():
    """Postings carry (m/f/d), a city, a req number. None of that belongs on a CV."""
    assert cvbuild.role_title("Revenue Operations Manager (m/f/d)", "BUILDER") \
        == "REVENUE OPERATIONS MANAGER"
    assert cvbuild.role_title("Sales Operations Lead - Amsterdam", "ANALYTICS") \
        == "SALES OPERATIONS LEAD"


def test_an_unusable_title_falls_back_to_the_track_standing_default():
    for track, default in cvbuild.STANDING_TITLES.items():
        assert cvbuild.role_title("", track) == default
        assert cvbuild.role_title("   ", track) == default


def test_the_standing_defaults_are_the_ones_the_skill_names():
    assert cvbuild.STANDING_TITLES["ANALYTICS"] == "REVENUE OPERATIONS & GTM STRATEGY"
    assert cvbuild.STANDING_TITLES["BUILDER"] == "REVENUE OPERATIONS & GTM SYSTEMS"
    assert cvbuild.STANDING_TITLES["CS"] == "ENTERPRISE CUSTOMER SUCCESS"


# ---------------------------------------------------------------- layout policy

def headings(spec):
    return [s["heading"] for s in spec["sections"]]


def project_leads(spec, track):
    """A project on the CV is one bullet with a bold lead-in, not an entry with a header
    line. That is how the base CV reads, so that is what the renderer produces."""
    title = cvbuild.PROJECTS_TITLE[track]
    section = next(s for s in spec["sections"] if s["heading"] == title)
    assert section["kind"] == "bullets", section["kind"]
    return [b["lead"] for b in section["bullets"]]


def test_revops_tracks_keep_projects_above_experience():
    """The reason it is above: a recruiter should meet the RevOps work before the CSM
    title, not after it."""
    for track in ("ANALYTICS", "BUILDER"):
        spec = cvbuild.assemble_spec(BASE, track, "T", "S", {}, "sk")
        h = headings(spec)
        assert h.index("REVOPS & GTM PROJECTS") < h.index("PROFESSIONAL EXPERIENCE"), track


def test_the_cs_track_moves_projects_below_experience_and_renames_them():
    spec = cvbuild.assemble_spec(BASE, "CS", "T", "S", {}, "sk")
    h = headings(spec)
    assert h == ["EDUCATION", "PROFESSIONAL EXPERIENCE", "PROJECTS & OTHER EXPERIENCE"]


def test_project_order_follows_the_track_table():
    orders = {t: project_leads(cvbuild.assemble_spec(BASE, t, "T", "S", {}, "sk"), t)
              for t in ("ANALYTICS", "BUILDER", "CS")}
    assert orders["ANALYTICS"][0].startswith("Factorial")
    assert orders["BUILDER"][0].startswith("GTM Health Diagnostic")
    assert orders["CS"][0].startswith("Sales-to-CS Handoff")
    # Debic is omitted on the CS track and present on the other two.
    assert not any("Debic" in p for p in orders["CS"])
    assert any("Debic" in p for p in orders["ANALYTICS"])
    assert any("Debic" in p for p in orders["BUILDER"])


def test_an_unknown_track_does_not_produce_a_sectionless_cv():
    """A track the audit never returns still has to render something, because the
    alternative is a CV with no projects and nobody noticing."""
    spec = cvbuild.assemble_spec(BASE, "SOMETHING-ELSE", "T", "S", {}, "sk")
    assert "REVOPS & GTM PROJECTS" in headings(spec)


def test_the_location_on_the_cv_stays_barcelona():
    spec = cvbuild.assemble_spec(BASE, "BUILDER", "T", "S", {}, "sk")
    assert any("Barcelona, Spain" in (c.get("text") or "") for c in spec["contact"])


def test_the_linkedin_line_carries_a_real_link():
    spec = cvbuild.assemble_spec(BASE, "BUILDER", "T", "S", {}, "sk")
    link = next(c for c in spec["contact"] if "linkedin" in (c.get("text") or ""))
    assert link["href"] == "https://www.linkedin.com/in/tom-p-norton/"


def experience_roles(spec):
    exp = next(s for s in spec["sections"] if s["heading"] == "PROFESSIONAL EXPERIENCE")
    return {(e["left"], r["sub_left"]): r for e in exp["entries"] for r in e["roles"]}


def test_bullets_land_on_the_role_they_were_assigned_to():
    spec = cvbuild.assemble_spec(BASE, "BUILDER", "T", "S",
                                 {"navex": ["Did the thing."]}, "sk")
    roles = experience_roles(spec)
    navex = roles[("NAVEX", "Customer Success Manager")]
    assert navex["bullets"] == ["Did the thing."]


def test_one_employer_with_two_titles_stays_one_employer():
    """LexisNexis is one company Tom worked at twice. Rendering it as two companies reads
    as two employers and makes an 11-year career look like a job-hopping one."""
    spec = cvbuild.assemble_spec(BASE, "ANALYTICS", "T", "S", {}, "sk")
    exp = next(s for s in spec["sections"] if s["heading"] == "PROFESSIONAL EXPERIENCE")
    lex = [e for e in exp["entries"] if e["left"] == "LexisNexis"]
    assert len(lex) == 1
    assert [r["sub_left"] for r in lex[0]["roles"]] == [
        "Account Manager (Corporate Legal)", "Account Manager (Print & Digital Solutions)"]


def test_a_role_the_tailoring_pass_ignores_keeps_its_base_bullets():
    """Saying nothing about a role is a real choice and often the right one. It must not
    mean an empty section."""
    spec = cvbuild.assemble_spec(BASE, "ANALYTICS", "T", "S",
                                 {"navex": ["Did the thing."]}, "sk")
    roles = experience_roles(spec)
    assert roles[("NAVEX", "Customer Success Manager")]["bullets"] == ["Did the thing."]
    kept = roles[("LexisNexis", "Account Manager (Corporate Legal)")]["bullets"]
    assert len(kept) == 3 and "renewal process" in cvbuild.bullet_text(kept[0])


def test_a_project_rewrite_replaces_the_body_and_keeps_the_name():
    """The lead-in is the project's name and what it was. That is a fact about Tom's life,
    not something a posting gets to tailor."""
    spec = cvbuild.assemble_spec(BASE, "ANALYTICS", "T", "S",
                                 {"factorial": ["A tighter body."]}, "sk")
    projects = next(s for s in spec["sections"]
                    if s["heading"] == "REVOPS & GTM PROJECTS")["bullets"]
    factorial = next(b for b in projects if "Factorial" in b["lead"])
    assert factorial["lead"] == "Factorial (ESADE MBA case study):"
    assert factorial["text"] == "A tighter body."


def test_an_empty_skills_line_falls_back_to_the_tracks_standing_one():
    spec = cvbuild.assemble_spec(BASE, "BUILDER", "T", "S", {}, "")
    assert spec["skills"].startswith("Python | HubSpot | Zapier")


# ---------------------------------------------------------------- Tom's two standing rules
#
# Both are in the skill, both were in the tailoring prompt, and neither was enforced -- so
# the first CV that shipped had eight bullets on NAVEX and a six-line summary. Asking is
# not the same as guaranteeing.

def test_no_job_gets_more_than_six_bullets():
    nine = [f"Ran a defined renewal process, item {i}." for i in range(9)]
    spec = cvbuild.assemble_spec(BASE, "ANALYTICS", "T", "S", {"navex": nine}, "sk")
    navex = experience_roles(spec)[("NAVEX", "Customer Success Manager")]
    assert len(navex["bullets"]) == cvbuild.MAX_BULLETS_PER_ROLE == 6
    # The six kept are the first six, because the tailoring pass is told to order them by
    # relevance to the posting. Dropping from the front would drop the KEY bullets.
    assert cvbuild.bullet_text(navex["bullets"][0]).endswith("item 0.")


def test_the_bullets_that_do_not_fit_are_reported_not_silently_lost():
    tailored = {"entries": [{"entry_id": "navex", "bullets": [
        {"text": f"Ran a defined renewal process, item {i}.", "source": "BANK", "key": False}
        for i in range(8)]}]}
    corpus = "Ran a defined renewal process item 0 1 2 3 4 5 6 7"
    by_entry, rejected = applyq.screened_entries(tailored, BASE, corpus)
    assert len(by_entry["navex"]) == 6
    assert len(rejected) == 2
    assert all("6-bullet limit" in why for _t, why in rejected)


def test_a_page_that_somehow_carries_seven_bullets_is_blocked():
    """The cap runs on the way in; this is the check on the way out, and verify() turns it
    into a problem that stops the send. Two routes past a rule is one too many."""
    spec = cvbuild.assemble_spec(BASE, "ANALYTICS", "T", "S", {}, "sk")
    assert cvbuild.over_bullet_limit(spec) == []
    exp = next(s for s in spec["sections"] if s["heading"] == "PROFESSIONAL EXPERIENCE")
    exp["entries"][0]["roles"][0]["bullets"] = [f"bullet {i}" for i in range(7)]
    over = cvbuild.over_bullet_limit(spec)
    assert over and "Customer Success Manager has 7" in over[0]


def test_the_summary_line_count_is_read_off_the_page_not_guessed():
    """Four PRINTED lines. Whether a sentence wraps is a question about Calibri's metrics,
    so it is counted from the rendered text between the role title and the first heading."""
    spec = {"role_title": "REVENUE OPERATIONS MANAGER",
            "sections": [{"heading": "EDUCATION"}, {"heading": "PROFESSIONAL EXPERIENCE"}]}
    page = ("                TOM NORTON\n"
            " Barcelona, Spain | tp.norton@pm.me\n"
            "\n"
            "REVENUE OPERATIONS MANAGER\n"
            "line one of the summary\n"
            "line two of the summary\n"
            "line three of the summary\n"
            "\n"
            "EDUCATION\n"
            "ESADE Business & Law School\n")
    assert cvbuild.summary_lines(page, spec) == 3
    assert cvbuild.summary_lines(page.replace("REVENUE OPERATIONS MANAGER\n", ""),
                                 spec) == 0


def test_an_over_long_summary_loses_its_last_sentence_not_its_last_words():
    """Trimmed by sentence, never mid-clause, and never rewritten: a rewrite is new text
    arriving after the honesty screen has already passed on it."""
    four = ("One sentence here. A second one follows it. Then a third arrives. "
            "And a fourth closes.")
    three = cvbuild.drop_last_sentence(four)
    assert three == "One sentence here. A second one follows it. Then a third arrives."
    assert cvbuild.drop_last_sentence(three).endswith("follows it.")
    assert cvbuild.drop_last_sentence("Only one sentence.") == ""
    assert cvbuild.drop_last_sentence("") == ""


# ---------------------------------------------------------------- the honesty screen

CORPUS = ("Managed a $5.2M ARR portfolio across 22 enterprise accounts. "
          "Beat net revenue retention targets three straight years at 108 to 110 percent. "
          "Cleaned up 400 stale opportunities over two quarters.")


def test_a_number_that_is_not_in_any_source_is_caught():
    assert cvbuild.invented_numbers("Grew revenue 34 percent", CORPUS) == ["34"]
    assert cvbuild.invented_numbers("Managed $5.2M ARR across 22 accounts", CORPUS) == []


def test_thousands_separators_and_trailing_zeros_are_the_same_claim():
    corpus = "retained $70,000 ARR and cut ramp 25.0 percent"
    assert cvbuild.invented_numbers("retained $70000 ARR", corpus) == []
    assert cvbuild.invented_numbers("cut ramp 25 percent", corpus) == []


def test_a_bullet_with_no_numbers_is_judged_on_its_words():
    assert cvbuild.untraceable("Invented a wholly different responsibility elsewhere",
                               CORPUS) is True
    assert cvbuild.untraceable("Cleaned up stale opportunities across two quarters",
                               CORPUS) is False


def test_the_screen_drops_the_fabrication_and_keeps_the_revision():
    kept, rejected = cvbuild.screen_bullets(
        ["Cleaned up 400 stale opportunities over two quarters",
         "Cleaned up 400 stale opportunities, lifting win rate 12 percent"], CORPUS)
    assert len(kept) == 1 and len(rejected) == 1
    assert "12" in rejected[0][1]


def test_the_posting_is_never_part_of_the_corpus():
    """The rule Phase 1 established and this phase inherits. The posting says what to look
    for; it is not evidence that Tom did any of it."""
    cur = {"answers": [{"answer": "Cleaned up 400 stale opportunities"}],
           "drafts": {"bullets": []}}
    bank = FakeBank({applyq.BANK_FILE: "Text: Managed a $5.2M ARR portfolio"})
    corpus = applyq.cv_corpus(bank, BASE, cur)
    assert "Cleaned up 400 stale opportunities" in corpus
    assert JOB["description"] not in corpus


def test_a_bullet_assigned_to_an_entry_that_does_not_exist_is_dropped():
    tailored = {"entries": [{"entry_id": "nowhere",
                             "bullets": [{"text": "Cleaned up 400 stale opportunities",
                                          "source": "NEW", "key": True}]}]}
    by_entry, rejected = applyq.screened_entries(tailored, BASE, CORPUS)
    assert by_entry == {}
    assert "unknown entry_id" in rejected[0][1]


# ---------------------------------------------------------------- summary variations

SUMMARIES = [{"label": "A", "angle": "Canonical-tight", "score": 7.0, "why": "",
              "changed": "", "text": "A text"},
             {"label": "B", "angle": "Role-forward", "score": 8.0, "why": "",
              "changed": "", "text": "B text"},
             {"label": "C", "angle": "Company-forward", "score": 6.0, "why": "",
              "changed": "", "text": "C text"}]


def test_the_agent_pick_is_the_highest_score():
    assert applyq.best_summary(SUMMARIES)["label"] == "B"


def test_a_tie_breaks_towards_the_bank_canonical():
    """A is the canonical with light edits, and the canonical is the tuned version. A tie
    is not a reason to move away from it."""
    tied = [dict(s, score=8.0) for s in SUMMARIES]
    assert applyq.best_summary(tied)["label"] == "A"


def test_a_letter_a_number_or_nothing_all_resolve():
    assert applyq.resolve_pick("b", SUMMARIES)[0]["label"] == "B"
    assert applyq.resolve_pick("C please", SUMMARIES)[0]["label"] == "C"
    assert applyq.resolve_pick("2", SUMMARIES)[0]["label"] == "B"
    chosen, recognised = applyq.resolve_pick("hmm not sure", SUMMARIES)
    assert recognised is False and chosen["label"] == "B"


def test_a_summary_is_never_truncated_in_the_message_that_asks_him_to_pick_one():
    """It was, at 460 characters, and he was left choosing between three things he could
    not finish reading. This is the one message in the run where he decides something."""
    real = ("Revenue operations professional drawn to the company's expansion across "
            "indirect tax coverage, where GTM systems have to keep pace with new markets. "
            "11 years in B2B SaaS, most recently managing a $5.2M ARR enterprise book to "
            "108-110% NRR three years running, with an ESADE MBA finishing in 2026. "
            "Strongest where post-sale data meets forecasting: renewal risk, customer "
            "health, and the handoffs between sales and CS that decide both. Built the "
            "funnel model, team sizing and CAC payback scenarios behind a live "
            "market-entry case, presented to the client's VP of CX.")
    assert len(real) > 460, "this sample has to be long enough to have been truncated"
    msg = applyq.format_variations(
        [dict(SUMMARIES[0], text=real)], JOB, {"track": "BUILDER"})
    assert "\u2026" not in msg
    assert real[-40:] in applyq.strip_tags(msg)


def test_a_variation_with_no_text_is_not_offered():
    out = applyq.summaries_of({"summaries": SUMMARIES + [
        {"label": "C", "angle": "", "score": 9.0, "why": "", "changed": "", "text": "  "}]})
    assert [s["label"] for s in out] == ["A", "B", "C"]
    assert all(s["text"].strip() for s in out)


# ---------------------------------------------------------------- the round trip

def run_to_cv(**kw):
    """Drive a role from the audit through to wherever the CV stage leaves it."""
    step, state, tg, bank, calls = machine(**kw)
    step()
    finished = step("Cleaned 400 stale opps over two quarters | Yes, in Salesforce")
    return step, state, tg, bank, calls, finished


def test_a_role_above_the_review_line_asks_once_and_only_once():
    step, state, tg, _bank, calls, finished = run_to_cv(score=8.4)
    assert finished is False
    assert state["current"]["stage"] == "pick"
    assert calls["render"] == 0
    assert "Pick a summary" in tg.last()
    # A tick with no reply does not re-ask, same rule as the gap questions.
    asked_at = state["current"]["asked_at"]
    before = len(tg.sent)
    step()
    assert len(tg.sent) == before and state["current"]["asked_at"] == asked_at
    # One letter finishes it.
    assert step("b") is True
    assert calls["render"] == 1
    assert state["history"][0]["outcome"] == "done"


def test_a_role_below_the_review_line_is_never_asked_at_all():
    _step, state, tg, _bank, calls, finished = run_to_cv(score=6.9)
    assert finished is True
    assert calls["render"] == 1
    assert not any("Pick a summary" in m for m in tg.sent)
    # He is still told which one was taken and what the others scored.
    assert any("Picked for you" in m for m in tg.sent)
    assert state["history"][0]["outcome"] == "done"


def test_the_gate_is_a_named_constant_at_seven_point_five():
    """Tom moves this once he has seen the volume, so it is a dial and not a rule buried
    in a comparison."""
    assert applyq.VARIATION_REVIEW_MIN_SCORE == 7.5
    _s, _st, tg, _b, calls, _f = run_to_cv(score=applyq.VARIATION_REVIEW_MIN_SCORE)
    assert "Pick a summary" in tg.last() and calls["render"] == 0


def test_toms_pick_beats_the_higher_scoring_variation():
    step, state, _tg, _bank, calls, _f = run_to_cv(score=8.4)
    step("a")
    assert calls["shipped"]["summary"] == "Revenue operations operator."


def test_the_packet_is_committed_before_anything_that_can_fail_runs():
    """A tailoring outage should not cost Tom the audit and the interview he already
    answered."""
    _step, state, tg, bank, _calls, _f = run_to_cv(score=8.4)
    assert any(c.startswith("Apply packet:") for c in bank.commits)
    assert [k for k in bank.files if k.startswith(applyq.PACKET_DIR + "/")]


def test_a_failing_tailoring_call_retries_then_stops_leaving_the_packet():
    step, state, tg, bank, _calls = machine()
    applyq.tailor_cv = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down"))
    step()
    step("Cleaned 400 stale opps | Yes, in Salesforce")
    for _ in range(applyq.MAX_STAGE_RETRIES - 2):
        step()
        assert state["current"] is not None
    step()
    assert state["current"] is None
    assert state["history"][0]["outcome"] == "tailor-failed"
    assert state["history"][0]["packet"]
    assert "api down" in tg.last()


def test_a_render_that_blows_up_retries_then_stops_instead_of_running_apt_forever():
    """Rendering shells out to apt, npm and LibreOffice, so it can fail for reasons that
    have nothing to do with this role. A broken toolchain must not mean a LibreOffice
    install on every firing with nobody watching."""
    step, state, tg, _bank, _calls = machine(score=6.9)
    applyq.cvbuild.render = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no soffice"))
    step()
    step("Cleaned 400 stale opps | Yes, in Salesforce")
    assert state["current"] is not None
    for _ in range(applyq.MAX_STAGE_RETRIES - 1):
        step()
    assert state["current"] is None
    assert state["history"][0]["outcome"] == "render-failed"
    assert "no soffice" in tg.last()


def test_a_cv_that_fails_its_checks_is_not_sent():
    """The reason the render is verified at all. A CV with the dates in the wrong place
    looks fine to the code that made it."""
    step, state, tg, bank, _calls = machine(score=6.9)
    applyq.cvbuild.verify = lambda paths, spec: (
        ["right-aligned tab stop is not holding"], [], {"pages": 2})
    step()
    step("Cleaned 400 stale opps over two quarters | Yes, in Salesforce")
    assert tg.documents == []
    assert "did not pass its checks" in tg.last()
    assert state["history"][0]["outcome"] == "cv-failed"
    # The PDF is still kept, because it is the fastest way to see what went wrong.
    assert any(k.startswith("cv/") for k in bank.files)


def test_a_role_that_only_ever_got_a_packet_can_be_run_again():
    """Phase 1 finished at the packet. Those roles have no CV, and re-applying should get
    them one rather than being told they are done."""
    tg = FakeTelegram()
    applyq.load_job = lambda job_id: JOB if job_id == JOB["id"] else None
    state = {"history": [{"id": JOB["id"], "title": JOB["title"], "outcome": "done",
                          "packet": "p.md"}]}
    state, queue, job = applyq.start_next(state, [JOB["id"]], tg)
    assert job is not None and state["current"]["stage"] == "audit"

    state = {"history": [{"id": JOB["id"], "title": JOB["title"], "outcome": "done",
                          "packet": "p.md", "cv": "cv/x.pdf"}]}
    state, queue, job = applyq.start_next(state, [JOB["id"]], tg)
    assert job is None
    assert "already has a CV" in tg.last()


def test_a_thin_company_brief_does_not_stop_the_role():
    step, state, tg, _bank, calls = machine(score=6.9)
    applyq.research_brief = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no search"))
    step()
    assert step("Cleaned 400 stale opps | Yes, in Salesforce") is True
    assert calls["tailor"] == 1
    assert state["history"][0]["outcome"] == "done"


def test_the_brief_is_researched_once_not_once_per_retry():
    step, state, tg, _bank, calls = machine(score=6.9)
    boom = [True]

    def flaky(*a, **k):
        if boom[0]:
            boom[0] = False
            raise RuntimeError("transient")
        return {"role_title_usable": True, "entries": [], "summaries": SUMMARIES,
                "skills": "", "keywords": [], "changes": []}

    applyq.tailor_cv = flaky
    step()
    step("Cleaned 400 stale opps | Yes, in Salesforce")
    step()
    assert calls["brief"] == 1


# ---------------------------------------------------------------- feedback (/redo)

def shipped(**kw):
    """Drive a role all the way to a delivered CV."""
    step, state, tg, bank, calls = machine(score=6.9, **kw)
    step()
    assert step("Cleaned 400 stale opps over two quarters | Yes, in Salesforce") is True
    return step, state, tg, bank, calls


def test_feedback_rebuilds_the_cv_with_no_second_round_trip():
    """He spent the round trip by sending the feedback. Asking him anything after that
    would be a second wait for a change he has already described."""
    step, state, tg, bank, calls = shipped()
    applyq.handle_commands([(0, "/redo cut the LexisNexis training bullet")],
                           state, [], tg, bank)
    assert state["current"]["stage"] == "revise"
    assert step() is True
    assert calls["revise"] == 1
    assert calls["feedback"] == "cut the LexisNexis training bullet"
    assert calls["shipped"]["summary"].startswith("Revised:")
    assert len(tg.documents) == 2                     # the original, then the rebuild
    caption = tg.documents[-1][1]
    assert "Rebuilt from your feedback" in caption
    assert "Cut the bullet you asked about" in caption   # what it did, in his hand


def test_the_revision_edits_the_page_he_read_not_the_draft_he_never_saw():
    _step, state, tg, bank, _calls = shipped()
    spec = state["last_cv"]["spec"]
    assert cvbuild.spec_bullets(spec), "the page as printed travels with the finished role"
    assert state["last_cv"]["job_snapshot"]["id"] == JOB["id"]


def test_a_revision_does_not_write_the_bank_a_second_time():
    """Those bullets went through the promotion test on the first build. Running it again
    would promote the same material twice."""
    step, state, tg, bank, calls = shipped()
    assert calls["bankwrite"] == 1
    applyq.handle_commands([(0, "/redo shorter please")], state, [], tg, bank)
    step()
    assert calls["bankwrite"] == 1


def test_redo_says_what_it_needs_rather_than_guessing():
    tg, bank = FakeTelegram(), FakeBank()
    state = {}
    applyq.handle_commands([(0, "/redo")], state, [], tg, bank)
    assert "No CV to revise yet" in tg.last()

    _step, state, tg, bank, _calls = shipped()
    applyq.handle_commands([(0, "/redo")], state, [], tg, bank)
    assert "Tell me what to change" in tg.last()
    assert state.get("current") is None

    state["current"] = {"title": "Something else", "stage": "ask"}
    applyq.handle_commands([(0, "/redo shorter")], state, [], tg, bank)
    assert "right now" in tg.last()


def test_a_revision_survives_the_role_dropping_off_the_dashboard():
    """A role ages off the dashboard in a week. "The CV you sent me yesterday" should
    still be revisable today."""
    _step, state, _tg, _bank, _calls = shipped()
    snap = state["last_cv"]["job_snapshot"]
    assert snap["description"] and snap["title"] == JOB["title"]


# ---------------------------------------------------------------- bank write-back

# Shaped exactly like the live bank: no brackets round the ID, an em dash after it, blank
# lines between fields, entries grouped under an employer heading. The format is the
# contract -- an entry written a little differently is a bullet that quietly stops being
# found, and nothing fails when that happens.
BANK_MD = """# CV BULLET BANK \u2014 Tom Norton

Last updated: 2026-01-01

# NAVEX \u2014 Customer Success Manager (12/21 \u2013 08/25)

### NAVEX-01 \u2014 ARR portfolio
Status: CANONICAL | Tracks: ALL | Competencies: retention

Text: Managed a $5.2M ARR portfolio across 22 enterprise accounts.

Evidence: Base CV

Notes: none

### NAVEX-02 \u2014 QBR format
Status: CANONICAL | Tracks: CS | Competencies: QBR

Text: Designed a QBR format later adopted across CS and AE teams.

Evidence: Base CV

Notes: none

# PROJECTS

### PRJ-GTMHD \u2014 GTM Health Diagnostic
Status: CANONICAL | Tracks: BUILDER | Competencies: funnel analytics

Text: Built a Python and Streamlit diagnostic for the RevOps analyst.

Evidence: GitHub

Notes: none

# CHANGE LOG

| Date | Bullet ID | Change | Reason |
|---|---|---|---|
"""


def test_the_live_banks_entry_shape_parses():
    """The format check that matters. The skill's own illustration writes `### [ID]`; the
    real file writes `### ID \u2014 title`, and a parser matching the illustration finds
    nothing at all and reports a clean run."""
    spans = bankwrite.entry_spans(BANK_MD)
    assert sorted(spans) == ["NAVEX-01", "NAVEX-02", "PRJ-GTMHD"]
    assert bankwrite.entry_text(BANK_MD, "NAVEX-01").startswith("Managed a $5.2M")


def test_a_promotion_replaces_the_text_and_leaves_the_rest_of_the_entry_alone():
    md, applied, skipped = bankwrite.apply_changes(BANK_MD, [
        {"kind": "PROMOTE", "bank_id": "NAVEX-01", "why": "adds the NRR record",
         "text": "Managed a $5.2M ARR portfolio across 22 enterprise accounts, beating "
                 "NRR targets three straight years."}], "2026-09-01")
    assert applied == [("NAVEX-01", "PROMOTE")]
    assert "beating NRR targets" in bankwrite.entry_text(md, "NAVEX-01")
    assert "Tracks: ALL" in md and "Competencies: retention" in md
    assert bankwrite.entry_text(md, "NAVEX-02").startswith("Designed a QBR")


def test_every_pass_bumps_the_date_and_writes_a_change_log_row():
    """The skill says these two go in every time, no exceptions, even on a one-change
    session. They are what make the bank's history readable a year later."""
    md, _applied, _skipped = bankwrite.apply_changes(BANK_MD, [
        {"kind": "VARIANT", "bank_id": "NAVEX-02", "text": "A Fonoa-shaped angle.",
         "why": "failed portability"}], "2026-09-01", company="Fonoa")
    assert "Last updated: 2026-09-01" in md
    assert "2026-01-01" not in md
    assert "| 2026-09-01 | NAVEX-02 |" in md
    assert "Job-specific variant (NAVEX-02-VAR, Fonoa 2026-09-01): A Fonoa-shaped angle." in md


def test_an_added_bullet_gets_the_next_free_id_in_its_family():
    md, applied, _skipped = bankwrite.apply_changes(BANK_MD, [
        {"kind": "ADD", "bank_id": "NAVEX-99", "title": "Pipeline cleanup",
         "text": "Cleaned up 400 stale opportunities over two quarters.",
         "tracks": "BUILDER", "competencies": "CRM hygiene", "why": "from the interview"}],
        "2026-09-01")
    assert applied == [("NAVEX-03", "ADD")]
    assert "### NAVEX-03 \u2014 Pipeline cleanup" in md
    assert "Tracks: BUILDER" in md
    # It joins its own family rather than landing at the end of the file. A NAVEX bullet
    # filed under PROJECTS is a bullet the next run reads in the wrong context.
    assert md.index("### NAVEX-03") < md.index("# PROJECTS")
    assert md.index("### NAVEX-03") > md.index("### NAVEX-02")


def test_retiring_changes_the_status_and_nothing_else_on_the_line():
    md, _applied, _skipped = bankwrite.apply_changes(BANK_MD, [
        {"kind": "RETIRE", "bank_id": "NAVEX-02", "why": "cut on three roles"}],
        "2026-09-01")
    line = next(l for l in md.split("\n")
                if l.startswith("Status:") and "CS" in l)
    assert line.startswith("Status: RETIRED")
    assert "Tracks: CS" in line and "Competencies: QBR" in line


def test_a_change_naming_an_id_the_bank_does_not_have_is_skipped_not_guessed():
    md, applied, skipped = bankwrite.apply_changes(BANK_MD, [
        {"kind": "PROMOTE", "bank_id": "GHOST-77", "text": "x", "why": "y"}], "2026-09-01")
    assert applied == [] and skipped and "no entry with that ID" in skipped[0][1]
    assert md == BANK_MD                     # nothing touched


def test_three_cuts_retires_a_bullet_and_two_do_not():
    counts = {}
    audit = {"bullets": [{"bank_id": "NAVEX-02", "decision": "CUT"}]}
    assert bankwrite.bump_cut_counts(counts, audit) == []
    assert bankwrite.bump_cut_counts(counts, audit) == []
    assert bankwrite.bump_cut_counts(counts, audit) == ["NAVEX-02"]
    # And not again on the fourth, so it is not re-retired every role after that.
    assert bankwrite.bump_cut_counts(counts, audit) == []


def test_the_write_back_screens_a_fabrication_before_it_reaches_the_bank():
    """A fabricated bullet on one CV is one bad application. The same bullet promoted into
    the bank is every application after it."""
    bank = FakeBank({applyq.BANK_FILE: BANK_MD})
    state = {}
    proposed = {"changes": [
        {"kind": "PROMOTE", "bank_id": "NAVEX-01", "why": "sharper",
         "text": "Managed a $5.2M ARR portfolio across 22 accounts, lifting NRR 14 points."},
        {"kind": "VARIANT", "bank_id": "NAVEX-02", "why": "angle",
         "text": "Designed a QBR format later adopted across CS and AE teams."}],
        "didnt_qualify": []}
    applied, blocked, _dq = applyq.write_back(
        bank, state, JOB, {"bullets": []}, proposed, CORPUS + " " + BANK_MD)
    assert [k for k, _ in applied] == ["NAVEX-02"]
    assert blocked and "14" in blocked[0][1]
    assert "lifting NRR" not in bank.files[applyq.BANK_FILE]


def test_the_write_back_runs_without_asking_and_says_nothing_when_nothing_qualifies():
    bank = FakeBank({applyq.BANK_FILE: BANK_MD})
    applied, blocked, dq = applyq.write_back(
        bank, {}, JOB, {"bullets": []},
        {"changes": [], "didnt_qualify": ["a keyword reshuffle"]}, CORPUS)
    assert applied == [] and blocked == []
    assert dq == ["a keyword reshuffle"]
    assert bank.files[applyq.BANK_FILE] == BANK_MD


def test_the_bank_is_only_written_after_a_cv_that_actually_passed():
    """Promoting off a build that failed its checks would put a bullet in the bank on the
    strength of a page nobody ever saw."""
    step, _state, _tg, _bank, calls = machine(score=6.9)
    applyq.cvbuild.verify = lambda paths, spec: (["broken"], [], {"pages": 2})
    step()
    step("Cleaned 400 stale opps over two quarters | Yes, in Salesforce")
    assert calls["bankwrite"] == 0


# ---------------------------------------------------------------- the skeleton

def test_the_seed_skeleton_parses_and_covers_every_project_the_tracks_name():
    ids = set((BASE.get("projects") or {}).keys())
    for track, order in cvbuild.PROJECT_ORDER.items():
        assert set(order) <= ids, (track, set(order) - ids)


def test_entry_ids_are_unique_across_the_skeleton():
    ids = cvbuild.entry_ids(BASE)
    assert len(ids) == len(set(ids)), ids


def test_the_banks_copy_of_the_skeleton_wins_over_the_repo_seed():
    edited = json.loads(json.dumps(BASE))
    edited["name"] = "Edited By Tom"
    bank = FakeBank({cvbuild.BASE_FILE: json.dumps(edited)})
    base, from_bank = cvbuild.load_base(bank)
    assert from_bank is True and base["name"] == "Edited By Tom"


def test_a_broken_skeleton_in_the_bank_fails_loudly_rather_than_silently_reverting():
    """Falling back to the seed would build a CV with none of Tom's edits in it and say
    nothing about it."""
    bank = FakeBank({cvbuild.BASE_FILE: "{not json"})
    try:
        cvbuild.load_base(bank)
    except RuntimeError as e:
        assert "not valid JSON" in str(e)
    else:
        raise AssertionError("a corrupt skeleton was accepted")


# ---------------------------------------------------------------- the render invocation

def test_libreoffice_gets_an_absolute_profile_uri_even_from_a_relative_outdir():
    """The bug that cost a ten-minute CI timeout and every real CV build.

    LibreOffice's UserInstallation takes a file:// URI. A RELATIVE one does not fail, it
    HANGS: "file://cv-out/.lo-profile" parses as host "cv-out" with path "/.lo-profile",
    and soffice sits there until something kills it. The workflow passes a relative
    "cv-out"; every local run of the render test happened to pass an absolute path, which
    is exactly why nothing caught it. So this asserts on the argv, not on the output."""
    seen = []

    def fake_run(cmd, cwd=None, timeout=600):
        seen.append(cmd)
        if cmd[0] == "soffice":                      # stand in for the conversion
            stem = os.path.splitext(cmd[-1])[0]
            open(stem + ".pdf", "wb").write(b"%PDF-1.4")
        return ""

    real_run = cvbuild._run
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="cv-relative-")
    try:
        cvbuild._run = fake_run
        os.chdir(tmp)
        REAL_RENDER({"sections": []}, "cv-out", "x")   # relative, as the workflow does
        soffice = next(c for c in seen if c[0] == "soffice")
        env = next(a for a in soffice if a.startswith("-env:UserInstallation="))
        assert env.startswith("-env:UserInstallation=file:///"), env
        assert cvbuild.SOFFICE_TIMEOUT <= 180, "a wedged conversion should fail fast"
    finally:
        cvbuild._run = real_run
        os.chdir(cwd)


def test_the_seed_is_the_real_cv_not_a_placeholder():
    """The seed carries Tom's actual base CV, so a first build is a real CV rather than a
    scaffold."""
    assert "no bullets" not in " ".join(cvbuild.skeleton_gaps(BASE))
    emptied = json.loads(json.dumps(BASE))
    for entry in emptied["experience"] + emptied["education"]:
        for role in entry["roles"]:
            role["bullets"] = []
    emptied["projects"] = {}
    assert any("no bullets" in g for g in cvbuild.skeleton_gaps(emptied))


def test_the_phone_number_goes_on_from_a_chat_message():
    """Tom is not a developer and said so. Asking him to hand-edit JSON in a private repo
    to get his own phone number onto his own CV was the wrong shape of answer, and it is
    where the first real run stopped."""
    bank = FakeBank()
    tg = FakeTelegram()
    applyq.handle_commands([(0, "/phone +34 722 719 046")], {"current": None}, [], tg, bank)
    base = json.loads(bank.files[cvbuild.BASE_FILE])
    texts = [c["text"] for c in base["contact"]]
    assert texts[1] == "+34 722 719 046", texts       # second, right after the location
    assert "Barcelona" in texts[0]
    assert not cvbuild.skeleton_gaps(base)
    assert "Phone set" in tg.last()

    # Changing it replaces rather than stacks, and it can be taken off again.
    applyq.handle_commands([(0, "/phone +31 6 1234 5678")], {"current": None}, [], tg, bank)
    base = json.loads(bank.files[cvbuild.BASE_FILE])
    assert sum(1 for c in base["contact"]
               if cvbuild.PHONE_RE.match(c["text"])) == 1
    applyq.handle_commands([(0, "/phone off")], {"current": None}, [], tg, bank)
    base = json.loads(bank.files[cvbuild.BASE_FILE])
    assert not any(cvbuild.PHONE_RE.match(c["text"]) for c in base["contact"])


def test_a_phone_number_that_is_not_one_is_refused_rather_than_written():
    bank, tg = FakeBank(), FakeTelegram()
    applyq.handle_commands([(0, "/phone call me maybe")], {"current": None}, [], tg, bank)
    assert cvbuild.BASE_FILE not in bank.files
    assert "doesn't look like a phone number" in tg.last()


def test_the_phone_reaches_the_rendered_contact_line():
    bank = FakeBank()
    applyq.handle_commands([(0, "/phone +34 722 719 046")], {"current": None}, [],
                           FakeTelegram(), bank)
    base, from_bank = cvbuild.load_base(bank)
    assert from_bank is True
    spec = cvbuild.assemble_spec(base, "ANALYTICS", "T", "S", {}, "sk")
    assert [c["text"] for c in spec["contact"]][1] == "+34 722 719 046"


def test_the_public_seed_carries_no_phone_number_and_says_so():
    """This repo is public. A phone number in a public repo gets scraped; the same number
    on a CV sent to a named recruiter does not. It lives in the bank's private copy, and
    the gap is reported rather than left to be noticed on a finished PDF."""
    assert not any(cvbuild.PHONE_RE.match((c.get("text") or "").strip())
                   for c in BASE["contact"])
    assert any("phone" in g for g in cvbuild.skeleton_gaps(BASE))
    withphone = json.loads(json.dumps(BASE))
    withphone["contact"].insert(1, {"text": "+34 722 719 046"})
    assert not any("phone" in g for g in cvbuild.skeleton_gaps(withphone))


def test_the_skeleton_is_nudged_about_once_not_every_role():
    step, state, tg, _bank, _calls = machine(score=6.9)
    step()
    assert step("Cleaned 400 stale opps | Yes, in Salesforce") is True
    assert sum("CV skeleton" in m for m in tg.sent) == 1
    assert state["cv_base_nudged"] is True


def test_first_person_in_a_summary_is_flagged_not_silently_shipped():
    """Not theoretical: the bank's own SUM-BUILDER canonical says "the RevOps tooling I
    spent a decade working around" and carries a NEEDS REWRITE flag. Variation A starts
    from the canonical, so it can reach the page."""
    hits = [m.group(0) for m in cvbuild.FIRST_PERSON_RE.finditer(
        "MBA candidate now building the RevOps tooling I spent a decade working around.")]
    assert hits == ["I"]
    # And does not fire on ordinary CV wording.
    assert not cvbuild.FIRST_PERSON_RE.search(
        "Major in Marketing, Minor in International Business")
    assert not cvbuild.FIRST_PERSON_RE.search("Implemented a minimum viable process")


def test_the_base_cv_is_part_of_what_a_revision_can_be_made_of():
    corpus = " ".join(cvbuild.base_bullets(BASE))
    assert "108-110%" in corpus and "$735K" in corpus
    # And so the base CV's own numbers never read as invented.
    assert cvbuild.invented_numbers(
        "Exceeded NRR targets three straight years (108-110%).", corpus) == []


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
