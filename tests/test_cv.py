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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import applyq  # noqa: E402
import bankwrite  # noqa: E402
import cvbuild  # noqa: E402
from test_apply import FakeBank, FakeTelegram, JOB, machine  # noqa: E402


BASE = cvbuild.load_base()[0]


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
