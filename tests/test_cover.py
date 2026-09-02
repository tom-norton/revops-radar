#!/usr/bin/env python3
"""Tests for the cover letter -- the offline half. No LibreOffice, no network, no git.

    python tests/test_cover.py        (or: python -m pytest tests/)

The letter is riskier than the CV and these tests are shaped around why.

1. **A letter is prose, so an invented claim leaves no hole.** A missing bullet is visible
   the moment Tom opens the PDF; a sentence that quietly credits him with something he
   never did reads exactly like the sentences around it. So the honesty screen is tested
   harder here than anywhere: what a claim may be made of, what a sentence about the
   company may be made of, and the fact that the posting is evidence for neither.
2. **One page is a rule, not an aspiration.** It is enforced off the rendered page count,
   and the trim that enforces it has to take the right paragraph.
3. **No listicles.** The renderer cannot draw a bullet and the cleaner strips markers
   before anything reaches it. Both halves are tested, because a rule with one route round
   it is not a rule.

The rendering itself is exercised for real in tests/test_letter_render.py, which is what
the smoke workflow runs. A stub that says the letter is one page long proves nothing.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import applyq  # noqa: E402
import coverletter  # noqa: E402
import cvbuild  # noqa: E402
from datetime import date  # noqa: E402
from test_apply import FakeBank, FakeTelegram, JOB  # noqa: E402


BASE = cvbuild.load_base()[0]
# Captured before anything stubs them: machine() replaces coverletter.render and
# coverletter.verify module-wide, and the verification tests below need the real ones.
REAL_VERIFY = coverletter.verify

# What Tom has actually done, as the screen sees it.
OWN = ("Managed a 5.2M ARR book across 22 enterprise accounts. "
       "Ran a monthly six-month renewal risk forecast, briefing CS and sales leadership.")
# What is knowable about the company. Kept apart from OWN on purpose: that separation is
# the whole reason the letter can name their funding round without the posting quietly
# becoming evidence for his own numbers.
COMPANY = ("Acme raised a 40M Series B in March and is expanding indirect tax coverage "
           "into 14 markets.")


def para(role, text, claims=()):
    return {"role": role, "text": text, "claims": list(claims)}


# ---------------------------------------------------------------- cleaning

def test_a_listicle_is_flattened_into_prose():
    """A model told not to write bullets writes a paragraph whose lines start with dashes.
    The markers come off before anything reaches a renderer that cannot draw them."""
    out = coverletter.clean_para("What I would bring:\n- Forecasting\n- SQL\n* Salesforce")
    assert out == "What I would bring: Forecasting SQL Salesforce", out


def test_numbered_and_lettered_markers_come_off_too():
    assert coverletter.clean_para("1. First\n2) Second\na) Third") == "First Second Third"


def test_em_dashes_are_normalised_rather_than_asked_about():
    assert " - " in coverletter.clean_para("I ran the forecast—every month")
    assert "—" not in coverletter.clean_para("one—two")


def test_whitespace_and_stray_newlines_collapse():
    assert coverletter.clean_para("  one\n\n  two  ") == "one two"


# ---------------------------------------------------------------- the honesty screen

def test_an_unsourced_claim_takes_its_paragraph_off_the_page():
    kept, dropped, _sent, fatal = coverletter.screen(
        [para("opening", "Your tax expansion is the problem I know best."),
         para("body", "I ran the renewal risk forecast monthly."),
         para("body", "I lifted net revenue retention by 14 points at NAVEX.",
              ["Lifted net revenue retention by 14 points at NAVEX."]),
         para("closing", "I would like to talk it through.")],
        OWN, COMPANY, "Acme")
    assert [p["role"] for p in kept] == ["opening", "body", "closing"], kept
    assert len(dropped) == 1 and "14" in dropped[0][1]
    assert not fatal


def test_a_claim_that_traces_back_to_his_sources_survives():
    kept, dropped, _sent, fatal = coverletter.screen(
        [para("opening", "Your tax expansion is the problem I know best."),
         para("body", "I managed a 5.2M ARR book across 22 enterprise accounts.",
              ["Managed a 5.2M ARR book across 22 enterprise accounts."]),
         para("closing", "I would like to talk it through.")],
        OWN, COMPANY, "Acme")
    assert dropped == [], dropped
    assert len(kept) == 3 and not fatal


def test_an_invented_number_drops_its_sentence_and_keeps_the_paragraph():
    """A local fault, unlike a claim that failed the screen: one figure from nowhere in an
    otherwise sourced paragraph."""
    kept, dropped_p, dropped_s, fatal = coverletter.screen(
        [para("opening", "Your tax expansion is the problem I know best."),
         para("body", "I ran the renewal risk forecast monthly. I lifted NRR by 14 points."),
         para("closing", "I would like to talk it through.")],
        OWN, COMPANY, "Acme")
    body = next(p for p in kept if p["role"] == "body")
    assert "14 points" not in body["text"]
    assert "renewal risk forecast" in body["text"]
    assert dropped_p == [] and len(dropped_s) == 1 and not fatal
    assert "14" in dropped_s[0][1]


def test_a_sentence_about_the_company_may_use_the_companys_own_numbers():
    kept, _p, dropped_s, _f = coverletter.screen(
        [para("opening", "Your 40M Series B in March is what caught my attention."),
         para("body", "I ran the renewal risk forecast monthly."),
         para("closing", "I would like to talk it through.")],
        OWN, COMPANY, "Acme")
    assert dropped_s == [], dropped_s
    assert "40M" in kept[0]["text"]


def test_the_same_number_in_a_sentence_about_tom_is_dropped():
    """The tell is who the sentence is about. Nothing else changes between this and the
    test above."""
    _kept, _p, dropped_s, _f = coverletter.screen(
        [para("opening", "The hook lands here."),
         para("body", "I raised a 40M Series B in March."),
         para("closing", "I would like to talk it through.")],
        OWN, COMPANY, "Acme")
    assert len(dropped_s) == 1 and "40" in dropped_s[0][1]


def test_the_posting_is_never_evidence_for_what_tom_did():
    """The rule the whole build is arranged around. A number that exists only in the job
    description cannot become a number about him."""
    posting = "We are hiring because our team of 47 analysts is growing."
    _kept, _p, dropped_s, _f = coverletter.screen(
        [para("opening", "The hook lands here."),
         para("body", "I supported 47 analysts at NAVEX."),
         para("closing", "I would like to talk it through.")],
        OWN, posting, "Acme")
    assert len(dropped_s) == 1, dropped_s


def test_a_letter_that_loses_its_opening_is_not_sent():
    _kept, _p, _s, fatal = coverletter.screen(
        [para("opening", "I lifted net revenue retention by 14 points.",
              ["Lifted net revenue retention by 14 points."]),
         para("body", "I ran the renewal risk forecast monthly."),
         para("closing", "I would like to talk it through.")],
        OWN, COMPANY, "Acme")
    assert "opening" in fatal


def test_a_letter_with_nothing_left_in_the_body_is_not_sent():
    _kept, _p, _s, fatal = coverletter.screen(
        [para("opening", "Your tax expansion is the problem I know best."),
         para("body", "I lifted net revenue retention by 14 points.",
              ["Lifted net revenue retention by 14 points."]),
         para("closing", "I would like to talk it through.")],
        OWN, COMPANY, "Acme")
    assert "body" in fatal


def test_paragraphs_print_in_the_order_code_chooses_not_the_order_returned():
    kept, _p, _s, fatal = coverletter.screen(
        [para("closing", "I would like to talk it through."),
         para("body", "I ran the renewal risk forecast monthly."),
         para("opening", "Your tax expansion is the problem I know best.")],
        OWN, COMPANY, "Acme")
    assert [p["role"] for p in kept] == ["opening", "body", "closing"] and not fatal


def test_an_unknown_role_is_treated_as_body():
    kept, _p, _s, _f = coverletter.screen(
        [para("opening", "Your tax expansion is the problem I know best."),
         para("PS", "One more thing about the forecast."),
         para("closing", "I would like to talk it through.")],
        OWN, COMPANY, "Acme")
    assert [p["role"] for p in kept] == ["opening", "body", "closing"]


# ---------------------------------------------------------------- one page

def test_the_trim_takes_the_last_body_paragraph():
    paras = [{"role": "opening", "text": "A"}, {"role": "body", "text": "B"},
             {"role": "body", "text": "C"}, {"role": "closing", "text": "D"}]
    out, cut = coverletter.trim_one(paras)
    assert cut == "C"
    assert [p["text"] for p in out] == ["A", "B", "D"]


def test_the_trim_refuses_to_empty_the_body():
    paras = [{"role": "opening", "text": "A"}, {"role": "body", "text": "B"},
             {"role": "closing", "text": "D"}]
    out, cut = coverletter.trim_one(paras)
    assert cut is None and len(out) == 3


# ---------------------------------------------------------------- the document

def test_the_layout_is_built_in_code_from_the_posting():
    spec = coverletter.assemble_spec(BASE, JOB, [{"role": "opening", "text": "Hello."}],
                                     date(2026, 9, 2), "REVENUE OPERATIONS MANAGER")
    assert spec["date"] == "2 September 2026"
    assert spec["recipient"] == ["Acme", "Amsterdam"]
    assert spec["subject"] == "Re: Revenue Operations Manager"
    assert spec["salutation"] == "Dear Acme Hiring Team,"
    assert spec["closing"] == "Sincerely,"
    assert spec["name"] == BASE["name"]
    assert spec["signature"] == "Tom Norton", "a signature in block capitals is shouting"
    assert spec["paragraphs"] == ["Hello."]


def test_the_letterhead_is_the_cvs_letterhead():
    """Not kept in step by hand: read off the same skeleton, so the phone number Tom set
    with /phone is on the letter the moment it is on the CV."""
    spec = coverletter.assemble_spec(BASE, JOB, [], date(2026, 9, 2))
    assert spec["contact"] == BASE["contact"]
    assert spec["theme"] == BASE["theme"]
    assert any("linkedin" in (c.get("href") or "").lower() for c in spec["contact"])


def test_no_hiring_manager_is_ever_invented():
    for company in ("Acme", ""):
        s = coverletter.salutation({"company": company})
        assert s.endswith("Hiring Team,") and "Mr" not in s and "Ms" not in s


def test_a_location_that_repeats_the_company_is_not_printed_twice():
    assert coverletter.recipient_block({"company": "Amsterdam Data", "location": "Amsterdam"}) \
        == ["Amsterdam Data"]


def test_the_plain_text_carries_the_whole_letter():
    """Half the application forms want a letter pasted into a box, and a PDF is no use for
    that, so the packet gets the text."""
    spec = coverletter.assemble_spec(BASE, JOB, [{"role": "opening", "text": "Hello there."},
                                                 {"role": "closing", "text": "Thank you."}],
                                     date(2026, 9, 2))
    text = coverletter.letter_text(spec)
    for want in ("2 September 2026", "Acme", "Re: Revenue Operations Manager",
                 "Dear Acme Hiring Team,", "Hello there.", "Thank you.", "Sincerely,"):
        assert want in text, want


# ---------------------------------------------------------------- verification

def _measured(pages=1, text=None, spec=None, fonts=("Carlito",), link=True):
    """verify() against a stubbed PDF. The real measurements are taken in
    tests/test_letter_render.py; what is being tested here is what verify does with them."""
    spec = spec or coverletter.assemble_spec(
        BASE, JOB, [{"role": "opening", "text": "Your tax expansion is the problem."}],
        date(2026, 9, 2))
    if text is None:
        # The letterhead too: letter_text() is what gets pasted into an application form,
        # and the rendered page carries the name and contact line above it.
        text = (spec["name"] + "\n"
                + " | ".join(c.get("text", "") for c in spec["contact"]) + "\n"
                + coverletter.letter_text(spec))
    saved = (cvbuild.pdf_pages, cvbuild.pdf_text, cvbuild.pdf_fonts, cvbuild.docx_has_link)
    cvbuild.pdf_pages = lambda p: pages
    cvbuild.pdf_text = lambda p: text
    cvbuild.pdf_fonts = lambda p: list(fonts)
    cvbuild.docx_has_link = lambda p, frag: link
    try:
        return REAL_VERIFY({"pdf": "x.pdf", "docx": "x.docx", "jpegs": ["p1.jpg"]}, spec)
    finally:
        (cvbuild.pdf_pages, cvbuild.pdf_text, cvbuild.pdf_fonts,
         cvbuild.docx_has_link) = saved


def test_one_page_passes_and_two_pages_do_not():
    problems, _w, facts = _measured(pages=1)
    assert problems == [] and facts["pages"] == 1
    problems, _w, _f = _measured(pages=2)
    assert any("one" in p for p in problems), problems


def test_a_paragraph_that_never_reached_the_page_is_a_problem():
    problems, _w, _f = _measured(text="2 September 2026 Acme Sincerely, Tom Norton")
    assert any("never reached the page" in p for p in problems), problems


def test_a_list_that_somehow_printed_is_a_problem():
    spec = coverletter.assemble_spec(BASE, JOB, [{"role": "opening", "text": "A"}],
                                     date(2026, 9, 2))
    problems, _w, _f = _measured(spec=spec, text="A\n- forecasting\n- SQL\nSincerely,")
    assert any("printed as a list" in p for p in problems), problems


def test_an_em_dash_on_the_page_is_a_problem_and_a_banned_word_is_a_warning():
    problems, warnings, _f = _measured(text="Your tax expansion is the problem. "
                                            "I leveraged — synergy. Sincerely,")
    assert any("banned character" in p for p in problems), problems
    assert any("Step 5c" in w for w in warnings), warnings
    assert not any("Step 5c" in p for p in problems)


def test_hedging_adverbs_ride_along_as_a_warning():
    _p, warnings, _f = _measured(text="Your tax expansion is the problem. "
                                      "I would arguably suit it. Sincerely,")
    assert any("hedging" in w for w in warnings), warnings


def test_the_wrong_font_is_a_problem():
    problems, _w, _f = _measured(fonts=("Liberation Serif",))
    assert any("not Calibri" in p for p in problems), problems


# ---------------------------------------------------------------- the state machine

def finished_role():
    """A role in the state a finished CV leaves behind: everything the letter needs, which
    is the reason /cover costs no round trip."""
    spec = cvbuild.assemble_spec(
        BASE, "BUILDER", "REVENUE OPERATIONS MANAGER",
        "Revenue operations operator with 11 years in enterprise B2B SaaS.", {},
        "Salesforce | SQL")
    return {
        "id": JOB["id"], "title": JOB["title"], "company": JOB["company"],
        "stage": "done", "cv_title": "REVENUE OPERATIONS MANAGER",
        "cv_stem": "2026-09-02-acme-revenue-operations-manager",
        "packet_file": "2026-09-02-acme-revenue-operations-manager.md",
        "audit": {"track": "BUILDER"},
        "brief": {"priorities": [{"priority": "Expand indirect tax coverage",
                                  "evidence": "Company blog, June 2026"}],
                  "challenges": [], "why_hiring": "", "culture": ["They publish postmortems"],
                  "visa_note": "", "thin": False},
        "answers": [{"kind": "gap", "keyword": "pipeline hygiene",
                     "question": "Did you do pipeline data cleanup?",
                     "answer": "Cleaned up 400 stale opportunities in Salesforce.",
                     "has_material": True}],
        "drafts": {"bullets": [{"text": "Cleaned up 400 stale opportunities in Salesforce."}]},
        "spec": spec, "summaries": [], "usage": {},
        "job_snapshot": {k: JOB.get(k) for k in ("id", "title", "company", "market",
                                                 "score", "url", "description")},
    }


LETTER = {
    "hook": "Their indirect tax expansion, from the June blog post.",
    "paragraphs": [
        {"role": "opening",
         "text": "Expanding indirect tax coverage is a data problem before it is a tax "
                 "problem, and it is the one I know best.",
         "claims": []},
        {"role": "body",
         "text": "At NAVEX I managed a 5.2M ARR book across 22 enterprise accounts.",
         "claims": ["Managed a 5.2M ARR book across 22 enterprise accounts."]},
        {"role": "closing", "text": "I would like to talk it through.", "claims": []},
    ],
    "notes": "Led on the tax expansion rather than the MBA.",
}


def machine(letter=None, pages=(1,), state=None, bank_files=None):
    """The cover stage wired to stubs. Returns (step, state, tg, bank, calls).

    `pages` is what the renderer reports on each successive attempt, which is how the
    one-page trim gets exercised without LibreOffice."""
    tg = FakeTelegram()
    bank = FakeBank(dict(bank_files or {applyq.BANK_FILE: "### NAVEX-01 - Book\nText: "
                                        "Managed a 5.2M ARR book across 22 enterprise "
                                        "accounts."}))
    calls = {"cover": 0, "render": 0, "steer": None, "as_built": None, "spec": None,
             "paragraphs": None}
    seen = {"n": 0}

    def fake_cover(api_key, job, audit, brief, as_built, answers, steer=""):
        calls["cover"] += 1
        # A real call moves the token counters, and the phase accounting only records a
        # phase that actually spent something.
        applyq.scan.USAGE["input_tokens"] = applyq.scan.USAGE.get("input_tokens", 0) + 9000
        calls["steer"] = steer
        calls["as_built"] = as_built
        return json.loads(json.dumps(letter if letter is not None else LETTER))

    def fake_render(spec, outdir, stem):
        calls["render"] += 1
        calls["spec"] = spec
        calls["paragraphs"] = list(spec.get("paragraphs") or [])
        os.makedirs(outdir, exist_ok=True)
        pdf = os.path.join(outdir, f"{stem}-cover.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 stub")
        return {"spec": "", "docx": "", "pdf": pdf, "jpegs": [f"{stem}-1.jpg"]}

    def fake_verify(paths, spec):
        i = min(seen["n"], len(pages) - 1)
        seen["n"] += 1
        n = pages[i]
        return ([] if n <= 1 else [f"{n} pages; a cover letter is one"], [],
                {"pages": n, "words": 210})

    applyq.write_cover = fake_cover
    applyq.coverletter.render = fake_render
    applyq.coverletter.verify = fake_verify
    applyq.coverletter.log_render = lambda *a, **k: None
    applyq.cvbuild.ensure_toolchain = lambda *a, **k: None
    applyq.CV_OUT_DIR = tempfile.mkdtemp(prefix="applyq-cover-")

    state = state if state is not None else {"last_cv": finished_role(), "history": [
        {"id": JOB["id"], "title": JOB["title"], "company": JOB["company"],
         "outcome": "done", "at": "2026-09-02T10:00:00+00:00",
         "packet": "2026-09-02-acme-revenue-operations-manager.md",
         "cv": "cv/2026-09-02-acme-revenue-operations-manager.pdf"}]}

    def step(message):
        """One command, then one advance -- but only into the cover stage. A role already
        in flight is somebody else's test, and running it here would take the whole audit
        pipeline with it."""
        _queue, rest = applyq.handle_commands([(0, message)], state, [], tg, bank)
        cur = state.get("current") or {}
        if cur.get("stage") == "cover":
            return applyq.advance(state, JOB, bank, tg, "key", rest[-1] if rest else "")
        return False

    return step, state, tg, bank, calls


def test_cover_writes_the_letter_and_ships_it_in_one_go():
    """No round trip: /cover carried the request and the CV run collected the rest."""
    step, state, tg, bank, calls = machine()
    assert step("/cover") is True
    assert calls["cover"] == 1 and calls["render"] == 1
    assert tg.documents, "the letter never reached Tom"
    assert "cv/2026-09-02-acme-revenue-operations-manager-cover.pdf" in bank.files
    assert state["current"] is None


def test_the_letter_is_named_after_the_cv_not_after_today():
    _step, _s, _tg, bank, _c = machine()
    _step("/cover")
    assert any(k.endswith("2026-09-02-acme-revenue-operations-manager-cover.pdf")
               for k in bank.files)


def test_steering_text_reaches_the_writer():
    step, _s, _tg, _b, calls = machine()
    step("/cover lead on the forecasting, not the MBA")
    assert calls["steer"] == "lead on the forecasting, not the MBA"


def test_the_letter_sees_the_page_as_it_shipped():
    _step, _s, _tg, _b, calls = machine()
    _step("/cover")
    assert "REVENUE OPERATIONS MANAGER" in calls["as_built"]


def test_a_letter_over_a_page_is_trimmed_and_re_rendered():
    step, _s, tg, _b, calls = machine(
        letter=dict(LETTER, paragraphs=LETTER["paragraphs"][:2]
                    + [{"role": "body", "text": "A second body paragraph.", "claims": []}]
                    + LETTER["paragraphs"][2:]),
        pages=(2, 1))
    assert step("/cover") is True
    assert calls["render"] == 2
    assert "A second body paragraph." not in calls["paragraphs"]
    assert tg.documents, "a trimmed letter should still ship"


def test_a_letter_that_will_not_fit_is_not_sent():
    step, state, tg, _b, calls = machine(pages=(2, 2, 2))
    assert step("/cover") is True
    assert not tg.documents
    assert "not passed" in tg.last() or "did not pass" in tg.last(), tg.last()
    assert state["history"][-1]["outcome"] == "cover-failed"


def test_a_fatal_screen_stops_the_send_and_says_so():
    step, state, tg, _b, calls = machine(letter=dict(LETTER, paragraphs=[
        {"role": "opening", "text": "I rebuilt their entire forecasting stack.",
         "claims": ["Rebuilt their entire forecasting stack."]},
        {"role": "body", "text": "I managed a 5.2M ARR book across 22 enterprise accounts.",
         "claims": ["Managed a 5.2M ARR book across 22 enterprise accounts."]},
        {"role": "closing", "text": "I would like to talk it through.", "claims": []}]))
    assert step("/cover") is True
    assert calls["render"] == 0, "a screened-out letter must not be rendered or sent"
    assert not tg.documents
    assert "not sent" in tg.last()
    assert state["history"][-1]["outcome"] == "cover-screened-out"


def test_the_cover_run_never_touches_the_bullet_bank():
    """Step 9 ran on the CV. A letter promotes nothing: it asserts no new material."""
    step, _s, _tg, bank, _c = machine()
    step("/cover")
    assert applyq.BANK_FILE not in [c for c in bank.commits]
    assert bank.files[applyq.BANK_FILE].startswith("### NAVEX-01")


def test_the_cv_stays_revisable_after_a_letter():
    """recoverable_cv() looks for the last history row carrying a CV. A cover row must not
    become that row, or /redo starts answering 'no CV to revise'."""
    step, state, _tg, _b, _c = machine()
    step("/cover")
    assert applyq.recoverable_cv(state)["cv"].endswith(
        "2026-09-02-acme-revenue-operations-manager.pdf")
    assert state["last_cv"]["spec"], "the page must still be there to revise"


def test_usage_is_recorded_against_the_cover_phase():
    step, state, _tg, _b, _c = machine()
    step("/cover")
    assert "cover" in (state["last_cv"].get("usage") or {})


def test_the_letters_text_goes_in_the_packet():
    step, _s, _tg, bank, _c = machine()
    step("/cover")
    packet = bank.files["packets/2026-09-02-acme-revenue-operations-manager.md"]
    assert "## Cover letter" in packet
    assert "Dear Acme Hiring Team," in packet
    assert "indirect tax coverage" in packet


def test_a_rejected_paragraph_is_written_down_where_tom_can_see_it():
    step, _s, _tg, bank, _c = machine(letter=dict(LETTER, paragraphs=LETTER["paragraphs"]
                                                  + [{"role": "body",
                                                      "text": "I lifted net revenue "
                                                              "retention by 14 points.",
                                                      "claims": ["Lifted net revenue "
                                                                 "retention by 14 "
                                                                 "points."]}]))
    step("/cover")
    packet = bank.files["packets/2026-09-02-acme-revenue-operations-manager.md"]
    assert "honesty screen rejected" in packet
    assert "lifted net revenue retention" in packet.lower()
    assert "14" in packet


# ---------------------------------------------------------------- the command

def test_cover_with_no_cv_yet_says_so():
    step, state, tg, _b, calls = machine(state={"history": []})
    assert step("/cover") is False
    assert calls["cover"] == 0
    assert "No CV" in tg.last()


def test_cover_waits_for_the_role_in_flight():
    st = {"current": {"title": "Something else", "stage": "ask"},
          "last_cv": finished_role(), "history": []}
    step, state, tg, _b, calls = machine(state=st)
    step("/cover")
    assert calls["cover"] == 0
    assert "once that one's finished" in tg.last()
    assert state["current"]["stage"] == "ask", "the role in flight must not be replaced"


def test_a_recovered_role_with_no_brief_is_flagged_before_the_letter_is_written():
    role = finished_role()
    role.pop("brief")
    step, _s, tg, _b, _c = machine(state={"last_cv": role, "history": []})
    step("/cover")
    assert "thinner letter" in tg.sent[0], tg.sent[0]


def test_cover_is_in_the_help():
    assert "/cover" in applyq.HELP


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
