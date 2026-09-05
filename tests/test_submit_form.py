#!/usr/bin/env python3
"""The form fill, for real: a browser, a page, a filled form, a printed PDF, a submission.

    python tests/test_submit_form.py            skips if Playwright isn't installed
    python tests/test_submit_form.py --install   installs it first (this is what CI does)

Separate from test_submit.py for the same reason the render tests are separate from the
unit tests: this needs Chromium, which is a download and an apt install, and test_submit.py
runs on every 15-minute tick.

Why it exists at all: everything in test_submit.py is true of a form the code invented.
Whether a dropdown opens, whether a hidden file input takes a path, whether a printed page
carries the answers, and whether a submit button can be found and pressed are questions
about a browser, and only a browser answers them.

It runs against tests/fixtures/application-form.html and never against an employer. That
page is a stand-in built to carry the shapes a real board uses -- react-select's
combobox-plus-generated-option-ids most of all -- because a test that submits a real
application is a real application.
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import submit  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "application-form.html")
OUT = os.environ.get("APPLYQ_CV_OUT", "cv-out")

IDENT = {"first_name": "Tom", "last_name": "Norton", "full_name": "Tom Norton",
         "email": "tp.norton@pm.me", "phone": "+34 700 000 000",
         "location": "Barcelona, Spain", "linkedin": "https://www.linkedin.com/in/x",
         "nationality": "United States"}
JOB = {"title": "Revenue Operations Manager", "company": "Northwind Tax",
       "market": "IE-Dublin"}


def fixture_url(path=FIXTURE):
    return "file://" + os.path.abspath(path)


def a_cv(dirpath):
    """Something to attach. A file input does not care what is in it, only that it is
    there and that the path resolves."""
    path = os.path.join(dirpath, "cv.pdf")
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n% a stand-in CV\n")
    return path


def plan_against(url, tmp, answers=None):
    fields, _ = submit.read_form(url, "greenhouse")
    files = {"resume": a_cv(tmp)}
    known, _notes = submit.plan_known(fields, IDENT, JOB, files)
    known.update(answers or {})
    return submit.build_plan(url, "greenhouse", fields, known, files), fields


def check(name, cond, detail=""):
    print(f"  {'pass' if cond else 'FAIL'}  {name}{'' if cond else ': ' + str(detail)}")
    return bool(cond)


def main():
    if "--install" in sys.argv:
        submit.ensure_browser()
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("playwright is not installed; skipping. "
              "Run with --install to install it first.")
        return 0

    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="applyq-form-test-")
    url = fixture_url()
    ok = True

    # ---- reading the form
    fields, _name = submit.read_form(url, "greenhouse")
    by_id = {f["id"]: f for f in fields}
    ok &= check("every control on the page is read",
                {"first_name", "last_name", "email", "phone", "resume", "question_1",
                 "question_2", "question_3", "question_4", "consent[]"} <= set(by_id),
                sorted(by_id))
    ok &= check("a label is read as the question a person sees",
                "authorised to work" in by_id["question_1"]["label"],
                by_id["question_1"]["label"])
    ok &= check("the form's own required flag is carried",
                by_id["first_name"]["required"] and not by_id["phone"]["required"])
    ok &= check("a file input is a file field", by_id["resume"]["kind"] == submit.FILE)
    ok &= check("a textarea is a text box", by_id["question_3"]["kind"] == submit.TEXTAREA)
    # The one that only a browser can answer: react-select's options do not exist in the
    # markup, so they have to be read out of an opened menu.
    ok &= check("a dropdown's options are read out of its opened menu",
                by_id["question_2"]["options"] == ["LinkedIn", "A friend", "Other"],
                by_id["question_2"]["options"])

    # ---- planning and filling
    plan, _fields = plan_against(url, tmp, {
        "question_3": submit.answer("The forecasting work is what I want more of.",
                                    submit.BY_MODEL),
        "question_2": submit.answer("LinkedIn", submit.BY_MODEL)})
    ok &= check("work authorisation was answered from nationality and market",
                plan["answers"]["question_1"]["value"] == "No",
                plan["answers"].get("question_1"))
    # Declined rather than left empty: Tom asked for every demographic question to be
    # answered with the form's own decline option where it offers one. What this guards is
    # that the option chosen is that one and never a real answer.
    ok &= check("the gender question is declined, never answered",
                plan["answers"].get("question_4", {}).get("value")
                == "I don't wish to answer", plan["answers"].get("question_4"))

    pdf = os.path.join(OUT, "application-form.pdf")
    with submit.Session() as s:
        driver = submit.driver_for("greenhouse")
        driver.open(s.page, url)
        failed = submit.fill_form(s.page, plan, driver, tmp)
        ok &= check("every field took what was put in it", not failed, failed)
        values = s.page.evaluate(
            "() => ({first: document.getElementById('first_name').value,"
            " email: document.getElementById('email').value,"
            " auth: document.getElementById('question_1').value,"
            " essay: document.getElementById('question_3').value,"
            " consent: document.getElementById('consent[]').checked,"
            " resume: document.getElementById('resume').files.length})")
        ok &= check("the text fields carry the values", values["first"] == "Tom"
                    and values["email"] == IDENT["email"], values)
        ok &= check("the dropdown carries the option that was picked",
                    values["auth"] == "No", values)
        ok &= check("the text box carries the answer",
                    values["essay"].startswith("The forecasting"), values)
        ok &= check("the acknowledgement is ticked", values["consent"] is True, values)
        ok &= check("the CV is attached", values["resume"] == 1, values)
        submit.form_pdf(s.page, pdf)

    size = os.path.getsize(pdf) if os.path.exists(pdf) else 0
    with open(pdf, "rb") as f:
        head = f.read(5)
    ok &= check("the filled form printed to a real PDF", head == b"%PDF-" and size > 2000,
                f"{head!r} {size} bytes")
    print(f"  the printed form is at {pdf} ({size} bytes)")

    # ---- the automated-submission check is noticed and named
    with submit.Session() as s:
        s.page.goto(url, wait_until="domcontentloaded")
        ok &= check("a page with no anti-automation check reports none",
                    submit.anti_bot(s.page) == "", submit.anti_bot(s.page))
        guarded = os.path.join(tmp, "guarded.html")
        with open(FIXTURE, encoding="utf-8") as f:
            html = f.read()
        # Exactly how Greenhouse and Ashby carry it: a script reference on the page, and
        # in Ashby's case a hidden response field the widget fills in.
        html = html.replace("</body>", '<textarea id="g-recaptcha-response" '
                                       'name="g-recaptcha-response" hidden></textarea>\n'
                                       '<script src="https://www.recaptcha.net/recaptcha/'
                                       'enterprise.js"></script>\n</body>')
        with open(guarded, "w", encoding="utf-8") as f:
            f.write(html)
        s.page.goto(fixture_url(guarded), wait_until="domcontentloaded")
        ok &= check("a page that runs reCAPTCHA is reported as running it",
                    submit.anti_bot(s.page) == "reCAPTCHA", submit.anti_bot(s.page))

    # ---- a dropdown will not take a value it does not carry
    with submit.Session() as s:
        driver = submit.driver_for("greenhouse")
        driver.open(s.page, url)
        try:
            driver.pick_option(s.page, "question_2", "A recruiter emailed me")
            ok &= check("an option that is not on the menu is refused", False,
                        "it was accepted")
        except RuntimeError as e:
            ok &= check("an option that is not on the menu is refused",
                        "not on the menu" in str(e), e)

    # ---- the submission itself
    result = submit.submit_form(plan, os.path.join(OUT, "application-sent.pdf"), tmp)
    ok &= check("the form submits and the page confirms it",
                result.get("sent") is True, result.get("reason"))
    ok &= check("what the page said is carried back",
                "Thank you for applying" in (result.get("page_said") or ""),
                result.get("page_said"))

    # ---- a form that changed underneath is refused rather than guessed at
    changed = os.path.join(tmp, "changed.html")
    shutil.copy(FIXTURE, changed)
    with open(changed, encoding="utf-8") as f:
        html = f.read()
    html = html.replace('<label for="consent[]">',
                        '<label for="question_9">Notice period*</label>\n'
                        '<input type="text" id="question_9" required>\n'
                        '<label for="consent[]">')
    with open(changed, "w", encoding="utf-8") as f:
        f.write(html)
    stale = dict(plan, url=fixture_url(changed))
    result = submit.submit_form(stale, os.path.join(OUT, "application-stale.pdf"), tmp)
    ok &= check("a form that has changed since it was approved is not submitted",
                result.get("sent") is False and result.get("reason") == "changed", result)

    # ---- and neither is one with a required question still blank
    thin = submit.build_plan(url, "greenhouse", fields,
                             {k: v for k, v in plan["answers"].items()
                              if k != "question_3"}, plan["files"])
    result = submit.submit_form(thin, os.path.join(OUT, "application-thin.pdf"), tmp)
    ok &= check("a required question left blank stops the submission",
                result.get("sent") is False and result.get("reason") == "incomplete",
                result)

    # ---- the Ashby driver, against Ashby's own shapes
    ashby_url = fixture_url(os.path.join(HERE, "fixtures", "ashby-form.html"))
    afields, _ = submit.read_form(ashby_url, "ashby")
    aby = {f["id"]: f for f in afields}
    ok &= check("ashby: the whole-name field is read",
                "_systemfield_name" in aby, sorted(aby))
    ok &= check("ashby: the autofill file input is not a field",
                len([f for f in afields if f["kind"] == submit.FILE]) == 1,
                [f["id"] for f in afields if f["kind"] == submit.FILE])
    yn = aby.get("6cff3ad4-d55b") or {}
    ok &= check("ashby: a yes/no question reads as a dropdown, not a tick-box",
                yn.get("kind") == submit.SELECT and yn.get("options") == ["Yes", "No"], yn)
    ok &= check("ashby: the yes/no question's own text is found",
                "sponsorship" in (yn.get("label") or "").lower(), yn.get("label"))
    eeoc = aby.get("ef84__systemfield_eeoc_gender") or {}
    ok &= check("ashby: an EEOC radio group collapses into one field",
                eeoc.get("kind") == submit.SELECT
                and "Decline to self-identify" in (eeoc.get("options") or []), eeoc)
    ok &= check("ashby: that field is recognised as demographic",
                submit.is_demographic(eeoc.get("label") or ""), eeoc.get("label"))

    aident = dict(IDENT, full_name="Tom Norton")
    aplan, _ = plan_against(ashby_url, tmp, answers=None) if False else (None, None)
    known, _notes = submit.plan_known(afields, aident, dict(JOB, market="IE-Dublin"),
                                     {"resume": a_cv(tmp)})
    ok &= check("ashby: the whole name goes in one field",
                known.get("_systemfield_name", {}).get("value") == "Tom Norton",
                known.get("_systemfield_name"))
    # Not "left blank": Tom asked for every demographic question to be answered with the
    # form's own decline option, so what is checked here is that the ONE thing chosen is
    # the decline. Answering it with anything else is the failure this guards.
    ok &= check("ashby: the gender question is declined, never answered",
                known.get("ef84__systemfield_eeoc_gender", {}).get("value")
                == "Decline to self-identify",
                known.get("ef84__systemfield_eeoc_gender"))
    ok &= check("ashby: sponsorship is answered from nationality and market",
                known.get("6cff3ad4-d55b", {}).get("value") == "Yes",
                known.get("6cff3ad4-d55b"))

    aplan = submit.build_plan(ashby_url, "ashby", afields, known, {"resume": a_cv(tmp)})
    with submit.Session() as s:
        driver = submit.driver_for("ashby")
        driver.open(s.page, ashby_url)
        failed = submit.fill_form(s.page, aplan, driver, tmp)
        ok &= check("ashby: every planned field took its value", not failed, failed)
        vals = s.page.evaluate(
            "() => ({name: document.getElementById('_systemfield_name').value,"
            " spons: document.getElementById('6cff3ad4-d55b').checked,"
            " gender: [...document.querySelectorAll('[name=ef84__systemfield_eeoc_gender]')]"
            "          .filter(r => r.checked)"
            "          .map(r => r.parentElement.innerText.trim()),"
            " resume: document.getElementById('_systemfield_resume').files.length,"
            " autofill: document.querySelectorAll('input[type=file]')[0].files.length})")
        ok &= check("ashby: the name landed", vals["name"] == "Tom Norton", vals)
        ok &= check("ashby: pressing Yes ticked the hidden checkbox behind it",
                    vals["spons"] is True, vals)
        ok &= check("ashby: the only gender radio selected is the decline",
                    vals["gender"] == ["Decline to self-identify"], vals)
        ok &= check("ashby: the CV went to the resume field", vals["resume"] == 1, vals)
        ok &= check("ashby: the autofill input was never touched",
                    vals["autofill"] == 0, vals)

    result = submit.submit_form(aplan, os.path.join(OUT, "ashby-sent.pdf"), tmp)
    ok &= check("ashby: it submits and the page confirms it",
                result.get("sent") is True, result.get("reason"))

    # ---- the Lever driver, against Lever's own shapes
    lever_url = fixture_url(os.path.join(HERE, "fixtures", "lever-form.html"))
    lfields, _ = submit.read_form(lever_url, "lever")
    lby = {f["id"]: f for f in lfields}
    # Lever gives its controls a name and no id at all. If id_selector() only ever built
    # `[id="..."]`, every one of these would read fine and fill nothing.
    ok &= check("lever: a control with no id is found by its name",
                lby.get("name", {}).get("label") == "Full name", sorted(lby))
    rtw = lby.get("cards[ad5e3844-aecd-49b5-9a6a-5435d716b25f][field0]") or {}
    ok &= check("lever: a radio group collapses into one field with every option",
                rtw.get("kind") == submit.SELECT and rtw.get("options") == ["Yes", "No"],
                rtw)
    ok &= check("lever: the question above the radios is the field's label",
                "right to work" in (rtw.get("label") or "").lower(), rtw.get("label"))
    ok &= check("lever: a custom textarea gets the question written above it",
                (lby.get("cards[1ec307db-810e-459b-8ed8-9d2c310b408e][field0]") or {})
                .get("label") == "Why do you want to work here?", lby.keys())
    gender = lby.get("surveysResponses[a723b959-0a3b-4e61-a9e2-295417eb4c01]"
                     "[responses][field0]") or {}
    ok &= check("lever: the survey question is recognised as demographic",
                submit.is_demographic(gender.get("label") or ""), gender.get("label"))

    lident = dict(IDENT, full_name="Tom Norton")
    lknown, _notes = submit.plan_known(lfields, lident, dict(JOB, market="IE-Dublin"),
                                       {"resume": "cv.pdf"})
    ok &= check("lever: the demographic question is declined, never answered",
                lknown.get(gender["id"], {}).get("value") == "I'd prefer not to say",
                lknown.get(gender["id"]))

    lplan = submit.build_plan(lever_url, "lever", lfields, lknown, {"resume": "cv.pdf"})
    a_cv(tmp)
    with submit.Session() as s:
        driver = submit.driver_for("lever")
        driver.open(s.page, lever_url)
        failed = submit.fill_form(s.page, lplan, driver, tmp)
        ok &= check("lever: every planned field took its value", not failed, failed)
        vals = s.page.evaluate(
            "() => ({name: document.querySelector('[name=name]').value,"
            " email: document.querySelector('[name=email]').value,"
            " chosen: document.getElementById('selected-location').value,"
            " linkedin: document.querySelector('[name=\"urls[LinkedIn]\"]').value,"
            " rtw: [...document.querySelectorAll('[name^=cards][type=radio]')]"
            "        .filter(r => r.checked).map(r => r.value),"
            " gender: [...document.querySelectorAll('[name^=surveysResponses]')]"
            "        .filter(r => r.checked).map(r => r.value),"
            " resume: document.querySelector('[name=resume]').files.length})")
        ok &= check("lever: the name landed in a field with no id", 
                    vals["name"] == "Tom Norton", vals)
        ok &= check("lever: so did the bracketed one", vals["linkedin"].endswith("/in/x"),
                    vals)
        # The whole point of settle(). Typing "Barcelona, Spain" into the typeahead leaves
        # selectedLocation empty, and selectedLocation is what Lever submits.
        ok &= check("lever: the location typeahead had a suggestion chosen",
                    vals["chosen"] != "", vals)
        # A US citizen applying to a Dublin role is not already authorised to work there,
        # and that is a fact on file rather than a judgement call -- so this one IS
        # answered, from the same nationality-and-market rule as Ashby's sponsorship
        # question, and the radio behind it is pressed.
        ok &= check("lever: right-to-work is answered from nationality and market",
                    lknown.get(rtw["id"], {}).get("value") == "No",
                    lknown.get(rtw["id"]))
        ok &= check("lever: pressing that answer selected the radio behind it",
                    vals["rtw"] == ["No"], vals)
        ok &= check("lever: the gender question was declined, not answered",
                    vals["gender"] == ["I'd prefer not to say"], vals)
        ok &= check("lever: the CV went to the resume field", vals["resume"] == 1, vals)

    result = submit.submit_form(lplan, os.path.join(OUT, "lever-sent.pdf"), tmp)
    ok &= check("lever: it submits and the page confirms it",
                result.get("sent") is True, result.get("reason"))
    # And a required question with nothing in it still stops it here, exactly as it does
    # on Greenhouse: Lever's driver gets no exemption from the rule that blank beats
    # invented.
    thin_lever = submit.build_plan(
        lever_url, "lever", lfields,
        {k: v for k, v in lknown.items() if k != rtw["id"]}, {"resume": "cv.pdf"})
    result = submit.submit_form(thin_lever, os.path.join(OUT, "lever-thin.pdf"), tmp)
    ok &= check("lever: a required question left blank stops the submission",
                result.get("sent") is False and result.get("reason") == "incomplete",
                result)

    print("\nall passed" if ok else "\nFAILURES above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
