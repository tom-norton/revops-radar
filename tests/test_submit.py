#!/usr/bin/env python3
"""Tests for the application form -- the offline half. No browser, no network, no git.

    python tests/test_submit.py        (or: python -m pytest tests/)

This is the one phase whose output cannot be taken back. A CV that ships wrong gets a
/redo; a letter that reads thin gets rewritten; an application that goes in has gone in, on
a real company's record, under Tom's name. So the tests here are weighted towards the
things that must never happen rather than the things that should:

1. **Nothing is submitted without him.** The fill stage has no route to a submit button,
   and the test asserts that by wiring one up and proving it is never called.
2. **A model never fills a fact.** Name, email, phone, LinkedIn, work authorisation and
   sponsorship are answered from what is on file, and the model is not shown those fields
   at all.
3. **Demographic questions are never answered.** Not by the model, which never sees them,
   and not by the code either, beyond taking a form's own decline option when it refuses to
   accept a blank.
4. **Blank beats invented.** An answer whose claim cannot be sourced is dropped and the
   field left empty, and a required field left empty stops the submission rather than
   quietly going up half-filled.
5. **What he approved is what gets sent.** The form is read again at submit time and
   refused if its shape has changed.

The browser half -- reading a real form, filling it, printing it -- is exercised against a
real page in tests/test_submit_form.py, which the smoke workflow runs. A stub that says a
form filled proves nothing about a form.
"""

import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import applyq  # noqa: E402
import cvbuild  # noqa: E402
import findform  # noqa: E402
import submit  # noqa: E402
from test_apply import FakeBank, FakeTelegram, JOB  # noqa: E402


BASE = cvbuild.load_base()[0]
IDENT = submit.identity(BASE)

# What Tom has actually done, as the screen sees it. Same shape as the letter's corpus,
# because it is the same corpus.
CORPUS = ("Managed a 5.2M ARR book across 22 enterprise accounts. "
          "Ran a monthly six-month renewal risk forecast for CS and sales leadership. "
          "Built an Excel model tracking renewal dates and ARR at risk.")


def F(fid, kind=submit.TEXT, label="", required=False, options=()):
    return submit.field(fid, kind, label or fid, required, options)


# A form with one of everything a real board puts on the page, modelled on the Greenhouse
# form this was built against.
def form():
    return [
        F("first_name", label="First Name", required=True),
        F("last_name", label="Last Name", required=True),
        F("preferred_name", label="Preferred First Name"),
        F("email", label="Email", required=True),
        F("phone", submit.TEXT, "Phone"),
        F("resume", submit.FILE, "Attach", required=True),
        F("resume_text", submit.TEXTAREA, "Enter manually"),
        F("cover_letter", submit.FILE, "Attach"),
        F("question_1", submit.SELECT, "Are you authorised to work in the country in "
          "which this role is located?", True, ("Yes", "No")),
        F("question_2", submit.SELECT, "Will you now or in the future require visa "
          "sponsorship?", True, ("Yes", "No")),
        F("question_3", submit.TEXT, "LinkedIn Profile"),
        F("question_4", submit.TEXTAREA, "What excites you most about this opportunity?",
          True),
        F("question_5", submit.SELECT, "How did you hear about this job?", True,
          ("LinkedIn", "A friend", "Other")),
        F("question_6", submit.SELECT, "Gender", True,
          ("Male", "Female", "I don't wish to answer")),
        F("question_7", submit.SELECT, "Are you Hispanic or Latino?", False,
          ("Yes", "No")),
        F("question_8", submit.TEXT, "What are your salary expectations?", True),
        F("consent[]", submit.CHECKBOX, "I acknowledge the privacy notice", True),
    ]


FILES = {"resume": "cv/2026-09-02-acme.pdf", "cover_letter": "cv/2026-09-02-acme-cover.pdf"}

# The same role as everywhere else, on a board that has a driver.
GH_JOB = dict(JOB, url="https://job-boards.greenhouse.io/acme/jobs/1", market="IE-Dublin")


# ---------------------------------------------------------------- which boards

def test_only_boards_with_a_driver_are_claimed():
    assert submit.detect_ats("https://job-boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert submit.detect_ats("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    for other in ("https://jobs.ashbyhq.com/vanta/abc",
                  "https://acme.wd1.myworkdayjobs.com/x/job/y",
                  "https://www.linkedin.com/jobs/view/123",
                  "https://jobs.lever.co/acme/abc", ""):
        assert submit.detect_ats(other) == "", other


# ---------------------------------------------------------------- who he is

def test_identity_comes_off_the_cv_skeleton():
    """The same file the letterhead is built from, so the form and the CV agree by
    construction rather than by luck."""
    assert IDENT["first_name"] == "Tom" and IDENT["last_name"] == "Norton"
    assert "@" in IDENT["email"]
    assert IDENT["linkedin"].startswith("http")
    assert IDENT["location"] == "Barcelona, Spain"
    assert IDENT["nationality"] == "United States"


def test_the_shouting_header_is_not_typed_into_a_form():
    ident = submit.identity({"name": "TOM NORTON", "contact": []})
    assert ident["full_name"] == "Tom Norton"


def test_a_phone_set_with_slash_phone_reaches_the_form():
    base = dict(BASE, contact=list(BASE["contact"]) + [{"text": "+34 700 000 000"}])
    assert submit.identity(base)["phone"] == "+34 700 000 000"


# ---------------------------------------------------------------- what code answers

def test_code_fills_the_facts_and_attaches_the_files():
    answers, _notes = submit.plan_known(form(), IDENT, JOB, FILES)
    assert answers["first_name"]["value"] == "Tom"
    assert answers["last_name"]["value"] == "Norton"
    assert answers["email"]["value"] == IDENT["email"]
    assert answers["question_3"]["value"].startswith("http")     # LinkedIn, by its label
    assert answers["resume"]["value"] == FILES["resume"]
    assert answers["cover_letter"]["value"] == FILES["cover_letter"]
    assert all(a["by"] == submit.BY_CODE for a in answers.values())


def test_a_preferred_first_name_is_his_first_name_not_a_blank():
    """A required preferred-name field left empty is a rejected submission, and the answer
    is not a judgement call."""
    answers, _ = submit.plan_known(form(), IDENT, JOB, FILES)
    assert answers["preferred_name"]["value"] == "Tom"


def test_the_paste_it_in_twin_of_a_file_field_stays_empty():
    """resume_text shadows the resume upload. Filling both attaches the CV twice."""
    answers, _ = submit.plan_known(form(), IDENT, JOB, FILES)
    assert "resume_text" not in answers


def test_work_authorisation_is_answered_from_nationality_and_market():
    """A US citizen applying to a Dublin role is not authorised there and does need
    sponsorship. That is legal status, not a judgement, and it is not a model's to
    infer."""
    answers, _ = submit.plan_known(form(), IDENT, dict(JOB, market="IE-Dublin"), FILES)
    assert answers["question_1"]["value"] == "No"      # authorised in Ireland
    assert answers["question_2"]["value"] == "Yes"     # needs sponsorship


def test_work_authorisation_flips_for_a_us_role():
    answers, _ = submit.plan_known(form(), IDENT, dict(JOB, market="US-Remote"), FILES)
    assert answers["question_1"]["value"] == "Yes"
    assert answers["question_2"]["value"] == "No"


def test_work_status_is_left_alone_when_the_market_is_unknown():
    answers, _ = submit.plan_known(form(), IDENT, dict(JOB, market=""), FILES)
    assert "question_1" not in answers and "question_2" not in answers


def test_a_dropdown_only_ever_gets_one_of_its_own_options():
    fields = [F("q", submit.SELECT, "Are you authorised to work in the country?", True,
                ("Absolutely", "Not at all"))]
    answers, _ = submit.plan_known(fields, IDENT, dict(JOB, market="US"), FILES)
    assert "q" not in answers, "invented an option that was not on the menu"


# ---------------------------------------------------------------- what nobody answers

def test_demographic_questions_are_never_answered():
    answers, _ = submit.plan_known(form(), IDENT, JOB, FILES)
    assert "question_7" not in answers, "answered an ethnicity question"


def test_a_required_demographic_question_takes_the_forms_own_decline():
    answers, notes = submit.plan_known(form(), IDENT, JOB, FILES)
    assert answers["question_6"]["value"] == "I don't wish to answer"
    assert any("declined" in n for n in notes)


def test_a_demographic_question_with_no_decline_option_is_left_blank():
    fields = [F("q", submit.SELECT, "Gender", True, ("Male", "Female"))]
    answers, _ = submit.plan_known(fields, IDENT, JOB, FILES)
    assert "q" not in answers


def test_the_model_is_never_shown_a_demographic_question():
    answers, _ = submit.plan_known(form(), IDENT, JOB, FILES)
    ids = {f["id"] for f in submit.open_questions(form(), answers)}
    assert "question_6" not in ids and "question_7" not in ids


def test_the_model_is_never_shown_a_field_code_already_filled():
    answers, _ = submit.plan_known(form(), IDENT, JOB, FILES)
    ids = {f["id"] for f in submit.open_questions(form(), answers)}
    assert not ids & {"first_name", "email", "resume", "question_1", "question_2"}
    assert {"question_4", "question_5", "question_8"} <= ids


def test_a_salary_question_is_flagged_as_money():
    answers, _ = submit.plan_known(form(), IDENT, JOB, FILES)
    money = [b for b in submit.blanks(form(), answers) if b["money"]]
    assert [b["id"] for b in money] == ["question_8"]


# ---------------------------------------------------------------- the honesty screen

def model(fid, value, claims=()):
    return {fid: {"value": value, "claims": list(claims), "why": ""}}


def test_an_answer_whose_claim_cannot_be_sourced_is_dropped():
    kept, rejected = submit.screen_answers(
        model("question_4", "I rebuilt the forecasting stack for a 400-person sales org.",
              ["Tom rebuilt a forecasting stack for a 400-person sales organisation."]),
        form(), CORPUS)
    assert not kept and rejected and rejected[0][0] == "question_4"


def test_an_invented_number_is_dropped_even_with_no_claim_declared():
    """The number check runs on the answer itself, so an answer that declares nothing is
    still not a free pass."""
    kept, rejected = submit.screen_answers(
        model("question_4", "I grew that book 47% in a year."), form(), CORPUS)
    assert not kept and "47" in rejected[0][2]


def test_a_sourced_answer_goes_through():
    kept, rejected = submit.screen_answers(
        model("question_4", "The forecasting work is what I want more of: I ran a monthly "
                            "six-month renewal risk forecast for leadership.",
              ["Tom ran a monthly six-month renewal risk forecast for leadership."]),
        form(), CORPUS)
    assert not rejected and kept["question_4"]["by"] == submit.BY_MODEL


def test_an_answer_that_asserts_nothing_needs_no_claims():
    kept, rejected = submit.screen_answers(model("question_5", "LinkedIn"), form(), CORPUS)
    assert not rejected and kept["question_5"]["value"] == "LinkedIn"


def test_a_dropdown_answer_off_the_menu_is_dropped_not_forced():
    kept, rejected = submit.screen_answers(
        model("question_5", "A recruiter emailed me"), form(), CORPUS)
    assert not kept and "not one of the options" in rejected[0][2]


def test_a_dropdown_answer_is_normalised_to_the_exact_option():
    kept, _ = submit.screen_answers(model("question_5", "linkedin"), form(), CORPUS)
    assert kept["question_5"]["value"] == "LinkedIn"


def test_best_option_never_guesses():
    assert submit.best_option("Spain", ["Spain (España)", "France"]) == "Spain (España)"
    assert submit.best_option("Barcelona, Spain", ["Spain", "Portugal"]) == "Spain"
    assert submit.best_option("Germany", ["Spain", "France"]) == ""


# ---------------------------------------------------------------- the plan

def plan_for(job=None, answers=None):
    fields = form()
    known, notes = submit.plan_known(fields, IDENT, job or dict(JOB, market="IE-Dublin"),
                                     FILES)
    known.update(answers or {})
    return submit.build_plan("https://job-boards.greenhouse.io/acme/jobs/1", "greenhouse",
                             fields, known, FILES, notes)


def test_a_required_question_nobody_could_answer_stops_the_form():
    plan = plan_for()
    missing = submit.missing_required(plan["fields"], plan["answers"])
    labels = {m["label"] for m in missing}
    assert "What excites you most about this opportunity?" in labels
    assert "What are your salary expectations?" in labels


def test_a_declined_demographic_question_is_not_counted_as_missing():
    plan = plan_for()
    assert not any(m["demographic"] for m in
                   submit.missing_required(plan["fields"], plan["answers"]))


def test_the_fingerprint_notices_a_changed_form():
    fields = form()
    assert submit.plan_is_current(plan_for(), fields)
    assert not submit.plan_is_current(plan_for(), fields + [F("question_9", label="New")])
    flipped = form()
    flipped[4]["required"] = True
    assert not submit.plan_is_current(plan_for(), flipped)


def test_consents_are_named_in_the_plan_rather_than_buried():
    assert plan_for()["consents"] == ["consent[]"]


def test_an_optional_tickbox_is_never_ticked():
    """A required box is the privacy notice. An optional one is a marketing opt-in, and
    agreeing to it on his behalf because the form offered it is not this code's call."""
    fields = form() + [F("marketing", submit.CHECKBOX,
                         "Please email me about future job openings")]
    plan = submit.build_plan("u", "greenhouse", fields,
                             submit.plan_known(fields, IDENT, JOB, FILES)[0], FILES)
    assert plan["consents"] == ["consent[]"]


def test_an_answer_to_a_question_the_model_was_never_asked_is_refused():
    """It is not shown the demographic fields or the ones code owns. An answer to one means
    it invented the id, and no amount of screening makes that safe."""
    kept, rejected = submit.screen_answers(
        {"question_6": {"value": "Male", "claims": [], "why": ""},
         "email": {"value": "tom@example.com", "claims": [], "why": ""}},
        form(), CORPUS)
    assert not kept and len(rejected) == 2
    assert all("never" in why or "not a question" in why for _f, _l, why in rejected)


def test_a_bracketed_dom_id_is_escaped_before_it_becomes_a_selector():
    assert submit.css_id("question_1[]_62") == r"question_1\[\]_62"


# ---------------------------------------------------------------- his own answers

def test_a_numbered_reply_maps_onto_the_questions_it_was_numbered_for():
    assert submit.parse_numbered("1. Dublin\n2) three months", 2) == \
        {1: "Dublin", 2: "three months"}
    assert submit.parse_numbered("1 yes\n2 no", 2) == {1: "yes", 2: "no"}


def test_one_question_takes_a_bare_answer_and_several_do_not():
    """An unnumbered paragraph split across three legal questions is exactly the kind of
    helpfulness that puts the wrong answer on a form."""
    assert submit.parse_numbered("Dublin", 1) == {1: "Dublin"}
    assert submit.parse_numbered("Dublin and three months", 3) == {}


def test_his_answers_go_in_as_his_and_are_not_screened():
    """The screen exists to stop a model inventing his experience. He cannot invent his
    own, so an unsourceable number in his own reply still goes on the form."""
    plan = plan_for()
    n = next(i for i, b in enumerate(applyq.numbered_blanks(plan), 1)
             if b["id"] == "question_4")
    plan, filled, unmatched = submit.apply_replies(
        plan, {n: "I want the forecasting rebuild. We ran 900 accounts."})
    assert filled and not unmatched
    assert "900" in plan["answers"]["question_4"]["value"]
    assert plan["answers"]["question_4"]["by"] == submit.BY_TOM


def test_a_dropdown_answer_in_his_own_words_is_not_forced_onto_the_form():
    """A near-miss on a work-authorisation dropdown is a wrong answer on an employer's
    record, so it comes back as a question rather than going in."""
    plan = plan_for()
    n = next(i for i, b in enumerate(applyq.numbered_blanks(plan), 1)
             if b["id"] == "question_5")
    plan, filled, unmatched = submit.apply_replies(plan, {n: "a recruiter emailed me"})
    assert not filled and unmatched and unmatched[0][0]["id"] == "question_5"


def test_an_unanswered_question_carries_its_options_to_the_message():
    plan = plan_for()
    q = next(b for b in applyq.numbered_blanks(plan) if b["id"] == "question_5")
    assert q["options"] == ["LinkedIn", "A friend", "Other"]
    assert "LinkedIn / A friend" in applyq.blank_line(1, q)


def test_an_answer_to_a_question_that_was_not_asked_is_ignored():
    plan = plan_for()
    _plan, filled, _unmatched = submit.apply_replies(plan, {9: "something"})
    assert not filled


def test_answering_clears_it_off_the_blanks_list():
    plan = plan_for()
    before = len(applyq.numbered_blanks(plan))
    n = next(i for i, b in enumerate(applyq.numbered_blanks(plan), 1)
             if b["id"] == "question_4")
    plan, _filled, _unmatched = submit.apply_replies(plan, {n: "The forecasting work."})
    assert len(applyq.numbered_blanks(plan)) == before - 1


# ---------------------------------------------------------------- finding the board
#
# Every case here is a real one from the current scan, which is why the numbers are what
# they are: the threshold has to admit Okta and refuse Braze, and both of those are
# decided by a tenth of a point.

class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def board(payload, status=200, seen=None):
    """A stand-in for the three public board APIs. Records what was asked for, because
    the order the slugs are tried in is part of the behaviour."""
    def fetch(url, **kw):
        if seen is not None:
            seen.append(url)
        for key, body in payload.items():
            if key in url:
                return FakeResponse(body)
        return FakeResponse({}, 404)
    return fetch


def gh_board(*jobs):
    return {"boards-api.greenhouse.io/v1/boards/acme":
            {"jobs": [{"id": 100 + i, "title": t, "location": {"name": loc},
                       "absolute_url": f"https://acme.com/careers/{100 + i}"}
                      for i, (t, loc) in enumerate(jobs)]}}


def test_a_slug_is_guessed_from_the_company_name():
    assert findform.board_slugs("Kong Inc.") == ["kong"]
    assert findform.board_slugs("commercetools") == ["commercetools"]
    assert findform.board_slugs("Northwind Tax") == ["northwindtax", "northwind-tax",
                                                     "northwind"]


def test_a_verified_slug_beats_a_guessed_one():
    seen = []
    fetch = board(gh_board(("Revenue Operations Manager", "Dublin, Ireland")), seen=seen)
    ats, slug, jobs = findform.find_board(
        "Acme Ltd", [{"name": "Acme Ltd", "ats": "greenhouse", "slug": "acme"}], fetch)
    assert (ats, slug) == ("greenhouse", "acme") and len(jobs) == 1
    assert len(seen) == 1, "guessed at slugs when a verified one was on file"


def test_the_url_it_returns_is_the_form_not_the_careers_page():
    """Greenhouse's absolute_url is wherever the employer chose to link the job, which is
    often their own site with the board embedded. The board-hosted URL is the same
    application and is the one with a driver behind it."""
    _a, _s, jobs = findform.find_board("Acme", [], board(gh_board(("RevOps", "Dublin"))))
    assert jobs[0]["url"] == "https://job-boards.greenhouse.io/acme/jobs/100"


def test_an_embedded_board_falls_back_to_the_embed_form():
    """Okta, in the real scan: the board URL 302s to okta.com and the form is in an embed.
    Same application, different endpoint."""
    assert submit.greenhouse_embed("https://job-boards.greenhouse.io/okta/jobs/8048254") \
        == "https://job-boards.greenhouse.io/embed/job_app?for=okta&token=8048254"
    assert submit.greenhouse_embed("https://jobs.ashbyhq.com/x/y") == ""
    # And an embed URL is still a form this can fill, or the fallback would be pointless.
    assert submit.detect_ats(
        "https://job-boards.greenhouse.io/embed/job_app?for=okta&token=1") == "greenhouse"


def test_a_title_that_merely_reads_similar_is_not_a_match():
    """Okta's board carries both "Customer Success Operations Manager, EMEA" (Dublin) and
    "Customer Success Operations Manager" (Toronto). They score 0.80 against each other and
    they are different jobs in different hemispheres."""
    assert findform.title_score("Customer Success Operations Manager, EMEA",
                                "Customer Success Operations Manager") < \
        findform.TITLE_MATCH_MIN


def test_the_market_separates_two_roles_with_the_same_title():
    """Intercom, in the real scan: "Senior Customer Success Manager" in London and in
    Dublin, both exact."""
    job = {"company": "Acme", "title": "Senior Customer Success Manager",
           "market": "IE-Dublin"}
    out = findform.find_form(job, [], board(gh_board(
        ("Senior Customer Success Manager", "London, England"),
        ("Senior Customer Success Manager", "Dublin, Ireland"))))
    assert out["outcome"] == "found"
    assert out["location"] == "Dublin, Ireland"


def test_four_cities_and_none_of_them_yours_is_refused():
    """Braze, in the real scan: four exact titles across US cities, posting was London."""
    job = {"company": "Acme", "title": "Senior CSM, Industry", "market": "UK-London"}
    out = findform.find_form(job, [], board(gh_board(
        ("Senior CSM, Industry", "San Francisco"), ("Senior CSM, Industry", "Austin"),
        ("Senior CSM, Industry", "Chicago"), ("Senior CSM, Industry", "New York City"))))
    assert out["outcome"] == "ambiguous" and len(out["candidates"]) == 4


def test_a_role_that_has_come_off_the_board_is_gone_not_the_nearest_thing():
    """Vanta, in the real scan: 109 roles on the board and the closest to the posting was
    "Strategic Channel Manager - EMEA" at 0.33."""
    job = {"company": "Acme", "title": "Revenue Operations Manager, Post Sales (EMEA)",
           "market": "IE-Dublin"}
    out = findform.find_form(job, [], board(gh_board(
        ("Strategic Channel Manager - EMEA", "London, UK"),
        ("Solutions Engineer (Upmarket, Pre-Sales) - EMEA", "Dublin, Ireland"))))
    assert out["outcome"] == "gone" and not out["candidates"]


def test_no_board_anywhere_is_its_own_answer():
    """Atlassian, in the real scan: they run their own careers site and nothing is found
    under any spelling."""
    out = findform.find_form({"company": "Acme", "title": "RevOps", "market": "NL"}, [],
                             board({}))
    assert out["outcome"] == "no-board"


def test_a_board_with_no_locations_on_it_still_resolves():
    """A single close match is a match. The market filter is a tie-break, not a gate."""
    job = {"company": "Acme", "title": "Revenue Operations Manager", "market": "NL"}
    out = findform.find_form(job, [], board(gh_board(("Revenue Operations Manager", ""))))
    assert out["outcome"] == "found"


def test_ashby_and_lever_boards_are_read_too():
    ashby = {"api.ashbyhq.com/posting-api/job-board/acme":
             {"jobs": [{"title": "RevOps Manager", "location": "Dublin, Ireland",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/1"}]}}
    lever = {"api.lever.co/v0/postings/acme":
             [{"text": "RevOps Manager", "categories": {"location": "Dublin, Ireland"},
               "hostedUrl": "https://jobs.lever.co/acme/1"}]}
    for payload, host in ((ashby, "ashbyhq"), (lever, "lever")):
        out = findform.find_form({"company": "Acme", "title": "RevOps Manager",
                                  "market": "IE-Dublin"}, [], board(payload))
        assert out["outcome"] == "found" and host in out["url"], (host, out)


# ---------------------------------------------------------------- the state machine

LETTER_TEXT = "Dear Acme Hiring Team, I have run renewal forecasting for four years."


def finished_role():
    """A role that has been through the CV and the cover letter, as /submit finds it."""
    return {"id": JOB["id"], "title": JOB["title"], "company": JOB["company"],
            "stage": "done", "cv_stem": "2026-09-02-acme-revenue-operations-manager",
            "cv_title": "Revenue Operations Manager",
            "cover_file": "cv/2026-09-02-acme-revenue-operations-manager-cover.pdf",
            "letter_text": LETTER_TEXT, "packet_file": "2026-09-02-acme.md",
            "as_built": "Managed a 5.2M ARR book across 22 enterprise accounts.",
            "audit": {"track": "BUILDER"}, "brief": {"priorities": ["indirect tax"]},
            "answers": [], "job_snapshot": dict(GH_JOB)}


def machine(fields=None, sent=True, reason="", state=None, model_answers=None, job=None,
            found=None):
    """The form stages wired to stubs. Returns (step, state, tg, bank, calls).

    The browser is replaced wholesale: these tests are about what the state machine does
    with a form, not about what Chromium does with one."""
    tg = FakeTelegram()
    bank = FakeBank({applyq.BANK_FILE: "### NAVEX-01 - Book\nText: " + CORPUS})
    bank.path = tempfile.mkdtemp(prefix="applyq-bank-")
    calls = {"read": 0, "preview": 0, "submit": 0, "browser": 0, "model": 0, "find": 0,
             "plan": None, "questions": None}
    the_fields = fields if fields is not None else form()

    def fake_read_form(url, ats, headless=True):
        calls["read"] += 1
        return [dict(f) for f in the_fields], ats

    def fake_preview(plan, out_pdf, bank_path="", headless=True):
        calls["preview"] += 1
        calls["plan"] = json.loads(json.dumps(plan))
        os.makedirs(os.path.dirname(out_pdf) or ".", exist_ok=True)
        with open(out_pdf, "wb") as f:
            f.write(b"%PDF-1.4 filled form")
        return out_pdf, [], [dict(f) for f in the_fields]

    def fake_submit_form(plan, out_pdf, bank_path="", headless=True):
        calls["submit"] += 1
        os.makedirs(os.path.dirname(out_pdf) or ".", exist_ok=True)
        with open(out_pdf, "wb") as f:
            f.write(b"%PDF-1.4 submitted form")
        if reason:
            return {"sent": False, "reason": reason, "pdf": out_pdf,
                    "missing": submit.missing_required(the_fields,
                                                       plan.get("answers") or {}),
                    "fields": [dict(f) for f in the_fields]}
        return {"sent": sent, "reason": "" if sent else "unconfirmed", "pdf": out_pdf,
                "page_said": "Thank you for applying" if sent else "Please try again",
                "url": plan.get("url")}

    def fake_answers(api_key, job, brief, as_built, letter_text, answers, fields_):
        calls["model"] += 1
        calls["questions"] = [f["id"] for f in fields_]
        applyq.scan.USAGE["input_tokens"] = applyq.scan.USAGE.get("input_tokens", 0) + 9000
        return (model_answers if model_answers is not None
                else {"question_4": {"value": "I ran a monthly six-month renewal risk "
                                              "forecast and want more of that work.",
                                     "claims": ["Tom ran a monthly six-month renewal risk "
                                                "forecast."], "why": ""},
                      "question_5": {"value": "LinkedIn", "claims": [], "why": ""}},
                [{"id": "question_8", "why": "no salary expectation on file"}],
                "Left the salary box for you.")

    def fake_find_form(job_, companies=None, fetch=None):
        calls["find"] += 1
        return found if found is not None else {"outcome": "no-board", "candidates": []}

    # The module reference is swapped rather than the function inside it: assigning to
    # applyq.findform.find_form would write through to the real module and leave every
    # findform test above running against this stub, which is a failure that depends on
    # the order the tests happen to run in.
    applyq.findform = types.SimpleNamespace(find_form=fake_find_form)
    applyq.submit.read_form = fake_read_form
    applyq.submit.preview = fake_preview
    applyq.submit.submit_form = fake_submit_form
    applyq.submit.ensure_browser = lambda *a, **k: calls.__setitem__("browser",
                                                                    calls["browser"] + 1)
    applyq.answer_questions = fake_answers
    the_job = dict(job or GH_JOB)
    applyq.load_job = lambda _id: dict(the_job)
    applyq.CV_OUT_DIR = tempfile.mkdtemp(prefix="applyq-form-")

    role = finished_role()
    role["job_snapshot"] = dict(the_job)
    state = state if state is not None else {"last_cv": role, "history": []}

    def step(message=""):
        rest = []
        if message:
            _queue, rest = applyq.handle_commands([(0, message)], state, [], tg, bank)
        cur = state.get("current") or {}
        if cur.get("stage") in ("fill", "approve", "send"):
            return applyq.advance(state, dict(the_job), bank, tg, "key",
                                  rest[-1] if rest else "")
        return False

    return step, state, tg, bank, calls


def test_submit_fills_the_form_and_sends_nothing():
    """The whole safety model in one test: the fill stage has no route to the button."""
    step, state, tg, bank, calls = machine()
    step("/submit")
    assert calls["preview"] == 1
    assert calls["submit"] == 0, "the fill stage pressed submit"
    assert state["current"]["stage"] == "approve"
    assert tg.documents, "the filled form never reached Tom"
    assert any("Nothing has been sent" in m for m in tg.sent), tg.sent


def test_the_filled_form_is_filed_in_the_bank():
    step, _state, _tg, bank, _calls = machine()
    step("/submit")
    assert "applications/2026-09-02-acme-revenue-operations-manager-form.pdf" in bank.files


def test_the_model_only_ever_sees_the_open_questions():
    step, _s, _tg, _b, calls = machine()
    step("/submit")
    assert calls["questions"] == ["question_4", "question_5", "question_8"], \
        calls["questions"]


def test_the_model_is_never_asked_for_a_fact_code_could_not_find():
    """The skeleton carries no phone number, because this repo is public. That makes the
    phone field unanswered; it does not make it a question for a model."""
    answers, _ = submit.plan_known(form(), IDENT, JOB, FILES)
    assert "phone" not in {f["id"] for f in submit.open_questions(form(), answers)}
    assert "phone" in {b["id"] for b in submit.blanks(form(), answers)}


def test_an_unsupported_board_is_said_once_with_the_link():
    """Not three ticks of trying: nothing here can drive a Workday, and saying so with the
    link is more use than a retry loop."""
    step, state, tg, _b, calls = machine(
        job=dict(GH_JOB, url="https://acme.wd1.myworkdayjobs.com/x/job/y"))
    assert step("/submit") is True
    assert calls["browser"] == 0 and calls["read"] == 0
    assert state["current"] is None
    assert state["history"][-1]["outcome"] == "submit-unsupported"
    assert any("could not find a board" in m for m in tg.sent), tg.sent


# ---------------------------------------------------------------- finding the real form

GH = "https://job-boards.greenhouse.io/acme/jobs/99"


def test_a_linkedin_link_is_traded_for_the_form_on_the_companys_own_board():
    """Half the roles on the radar arrive as a LinkedIn link, and none of those is a
    form."""
    step, state, tg, _b, calls = machine(
        job=dict(GH_JOB, url="https://www.linkedin.com/jobs/view/4451213852"),
        found={"outcome": "found", "url": GH, "ats": "greenhouse",
               "title": "Revenue Operations Manager", "location": "Dublin, Ireland",
               "candidates": []})
    step("/submit")
    assert calls["find"] == 1 and calls["preview"] == 1
    assert state["current"]["plan"]["url"] == GH
    assert state["current"]["plan"]["posting_url"].startswith("https://www.linkedin.com")
    # He is told which role it landed on, because that is the thing that could be wrong.
    assert any("Dublin, Ireland" in m for m in tg.sent), tg.sent


def test_a_role_on_a_board_with_no_driver_is_handed_over_as_a_direct_link():
    """Still a win: a link straight to the real application beats a LinkedIn page he has
    to search from."""
    step, state, tg, _b, calls = machine(
        job=dict(GH_JOB, url="https://www.linkedin.com/jobs/view/1"),
        found={"outcome": "found", "url": "https://jobs.ashbyhq.com/acme/abc",
               "ats": "ashby", "title": "Revenue Operations Manager",
               "location": "Dublin, Ireland", "candidates": []})
    assert step("/submit") is True
    assert calls["preview"] == 0
    assert state["history"][-1]["outcome"] == "submit-unsupported"
    assert any("jobs.ashbyhq.com/acme/abc" in m for m in tg.sent), tg.sent


def test_four_cities_sharing_one_title_are_never_guessed_between():
    """Braze, in the real scan: four identical titles across US cities, and the posting was
    London. A form filled for the wrong city is a wrong application, not a near miss."""
    cands = [{"title": "Senior CSM, Industry", "location": c,
              "url": f"https://job-boards.greenhouse.io/acme/jobs/{i}", "score": 1.0}
             for i, c in enumerate(["San Francisco", "New York City", "Chicago", "Austin"])]
    step, state, tg, _b, calls = machine(
        job=dict(GH_JOB, url="https://www.linkedin.com/jobs/view/1"),
        found={"outcome": "ambiguous", "ats": "greenhouse", "candidates": cands})
    assert step("/submit") is True
    assert calls["preview"] == 0
    assert state["history"][-1]["outcome"] == "submit-ambiguous"
    assert any("Chicago" in m and "Austin" in m for m in tg.sent), tg.sent


def test_a_role_missing_from_a_live_board_is_reported_as_probably_closed():
    """Vanta, in the real scan: 109 roles on the board and nothing above 0.33. The role was
    taken down, and saying so is worth more than filling the nearest thing to it."""
    step, state, tg, _b, calls = machine(
        job=dict(GH_JOB, url="https://www.linkedin.com/jobs/view/1"),
        found={"outcome": "gone", "ats": "greenhouse", "slug": "acme", "board_size": 109,
               "candidates": []})
    assert step("/submit") is True
    assert calls["preview"] == 0
    assert state["history"][-1]["outcome"] == "submit-role-gone"
    assert any("not one of them" in m for m in tg.sent), tg.sent
    # Said carefully: a slug guessed from a company name can land on somebody else's
    # board, so the board itself is linked for him to check by eye.
    assert any("job-boards.greenhouse.io/acme" in m for m in tg.sent), tg.sent


def test_a_link_that_is_already_a_form_is_not_looked_up_at_all():
    step, _state, _tg, _b, calls = machine()
    step("/submit")
    assert calls["find"] == 0, "went looking for a board it did not need"


def test_a_lookup_that_blows_up_does_not_take_the_role_with_it():
    step, state, tg, _b, _c = machine(
        job=dict(GH_JOB, url="https://www.linkedin.com/jobs/view/1"))
    def boom(*a, **k):
        raise RuntimeError("the board API is down")
    applyq.findform = types.SimpleNamespace(find_form=boom)
    assert step("/submit") is True
    assert state["history"][-1]["outcome"] == "submit-unsupported"


def test_send_is_refused_while_a_required_question_is_unanswered():
    step, state, tg, _b, calls = machine()
    step("/submit")
    step("/send")
    assert calls["submit"] == 0
    assert state["current"]["stage"] == "approve"
    assert any("still unanswered" in m for m in tg.sent), tg.sent


def test_answering_the_open_questions_refills_and_reprints_the_form():
    step, state, tg, _b, calls = machine()
    step("/submit")
    n = len(applyq.numbered_blanks(state["current"]["plan"]))
    step("1. 60,000 EUR" if n == 1 else "1. The forecasting rebuild\n2. 60,000 EUR")
    assert calls["preview"] == 2, "the form was not printed again"
    assert state["current"]["stage"] == "approve"
    assert not applyq.numbered_blanks(state["current"]["plan"])


def test_a_reply_nobody_can_map_is_asked_again_rather_than_guessed_at():
    """Two questions open and an unnumbered paragraph: nothing is guessed at, because
    guessing here puts the wrong answer in a box on a real application."""
    step, _state, tg, _b, calls = machine(model_answers={})
    step("/submit")
    step("sounds good, go for it")
    assert calls["preview"] == 1 and calls["submit"] == 0
    assert any("Number them" in m for m in tg.sent), tg.sent


def test_one_open_question_takes_the_reply_and_shows_him_what_went_in():
    """With a single question left there is nothing to mis-map, so a bare reply is the
    answer to it -- and the message says exactly which box it went into, before anything
    can be sent."""
    step, state, tg, _b, calls = machine()
    step("/submit")
    assert len(applyq.numbered_blanks(state["current"]["plan"])) == 1
    step("around 60k")
    assert calls["submit"] == 0
    assert any("Added" in m and "60k" in m for m in tg.sent), tg.sent
    assert state["current"]["plan"]["answers"]["question_8"]["by"] == submit.BY_TOM


def test_send_submits_once_and_records_it():
    step, state, tg, bank, calls = machine()
    step("/submit")
    step("1. The forecasting rebuild\n2. 60,000 EUR")
    step("/send")
    assert calls["submit"] == 1
    assert state["current"] is None
    assert state["history"][-1]["outcome"] == "submitted"
    assert any("Applied" in m for m in tg.sent), tg.sent
    assert "## Application form" in bank.files["packets/2026-09-02-acme.md"]


def test_a_submission_never_makes_the_cv_behind_it_unrevisable():
    """recoverable_cv() looks for the last history row carrying a `cv` key. An application
    row must not be one, or /redo starts pointing at a form."""
    step, state, _tg, _b, _c = machine()
    step("/submit")
    step("1. The forecasting rebuild\n2. 60,000 EUR")
    step("/send")
    assert "cv" not in state["history"][-1]


def test_a_form_that_changed_underneath_is_not_submitted():
    step, state, tg, _b, calls = machine(reason="changed")
    step("/submit")
    step("1. The forecasting rebuild\n2. 60,000 EUR")
    step("/send")
    assert state["current"]["stage"] == "fill", "did not go back to re-read the form"
    assert "plan" not in state["current"]
    assert any("has changed" in m for m in tg.sent), tg.sent


def test_a_click_with_no_confirmation_is_reported_as_exactly_that():
    """The one thing never said is that an application went in when the page did not say
    so."""
    step, state, tg, _b, _c = machine(sent=False)
    step("/submit")
    step("1. The forecasting rebuild\n2. 60,000 EUR")
    step("/send")
    assert state["history"][-1]["outcome"] == "submitted-unconfirmed"
    assert any("not confirmed" in m for m in tg.sent), tg.sent


def test_the_same_role_is_not_applied_to_twice():
    step, state, tg, _b, calls = machine()
    state["history"].append({"id": JOB["id"], "outcome": "submitted", "at": "now"})
    step("/submit")
    assert calls["preview"] == 0 and state.get("current") is None
    assert any("second application" in m for m in tg.sent), tg.sent


def test_a_role_with_no_link_left_is_said_plainly():
    step, state, tg, _b, calls = machine()
    applyq.load_job = lambda _id: None
    state["last_cv"]["job_snapshot"].pop("url")
    step("/submit")
    assert calls["preview"] == 0
    assert any("don't have a link" in m for m in tg.sent), tg.sent


def test_send_on_its_own_does_nothing():
    step, _state, tg, _b, calls = machine()
    step("/send")
    assert calls["submit"] == 0
    assert any("Nothing waiting to be sent" in m for m in tg.sent), tg.sent


def test_the_approve_stage_waits_rather_than_timing_out():
    """A form waits for Tom the same way a gap interview does: however long it takes."""
    assert "approve" in applyq.WAITING_STAGES


def test_both_commands_are_in_the_help():
    assert "/submit" in applyq.HELP and "/send" in applyq.HELP


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
