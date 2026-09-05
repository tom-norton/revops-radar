#!/usr/bin/env python3
"""The application form: Step 10 of the job-application-workflow, in code.

Phases 0-3 end with a scored role, a tailored CV and a cover letter on Tom's phone, and
the form still to fill in by hand. This fills it.

Same division of labour as cvbuild.py and coverletter.py. A model answers the questions
only a person could answer -- "what excites you about this role", "why this company" -- and
this file does everything else: reads the form, maps the fields a model must never be
allowed to guess at, fills them, prints the filled page, and submits it. The rules that
matter are enforced here rather than asked for in a prompt, because a rule the code cannot
break is the only kind that holds:

  - **Nothing is ever submitted without Tom saying so.** preview() fills and prints. It
    has no route to the submit button at all: clicking it lives in submit_form(), which
    the state machine only reaches from an explicit /send.
  - **The form is read again at submit time and compared against what he approved.** The
    runner that filled the form is gone by then, so the plan is replayed rather than
    resumed, and a form whose fields have changed underneath it is refused rather than
    guessed at.
  - **Demographic questions are never answered.** Not by the model, which never sees them,
    and not by this file either: EEO, gender, race, veteran and disability questions are
    left blank, or set to the form's own decline option when it insists on one.
  - **A required question nobody can source stops the submission.** Blank beats invented:
    an unanswered required field is reported to Tom and the form waits for him.

Every answer that asserts something about Tom's experience goes through the same
cvbuild.screen_bullets() the CV and the letter use, against the same corpus. An
application form is the one place where an invented claim is not just on a page: it is a
sworn answer on a company's record of him.
"""

import hashlib
import json
import os
import re
import subprocess
import sys

import cvbuild

# The browser build the fill runs on. Chromium only: this drives real employer forms, and
# the one that renders them the way their own QA saw them is the one to use.
BROWSER_TIMEOUT_MS = 45000
# A form that has not settled in this long is not going to. Greenhouse hydrates its
# dropdowns client-side, so the fill cannot start against raw HTML.
SETTLE_MS = 2500
# Chromium prints the filled page to this, and Tom reads it on his phone exactly as he
# reads the CV and the letter.
PDF_FORMAT = "A4"

# ---------------------------------------------------------------- which forms are handled

# Only boards whose application form is a plain page that can be read, filled and checked.
# Workday and LinkedIn Easy Apply are deliberately absent: both want an account and a
# session, and an automated login is a credential in a runner and a different risk
# conversation than this one.
ATS_PATTERNS = (
    # Greenhouse hosts a regional board for data-residency customers --
    # job-boards.eu.greenhouse.io -- which is byte-for-byte the same application, react-
    # select and all, just on a different subdomain. Missed here until a real EU-hosted
    # row (Convera) turned up scored "greenhouse" by application_status() below but
    # unfillable, because this pattern required the host to be exactly boards.greenhouse.io
    # or job-boards.greenhouse.io.
    ("greenhouse", re.compile(r"^https?://(?:job-)?boards\.(?:[\w-]+\.)?greenhouse\.io/",
                              re.I)),
    ("ashby", re.compile(r"^https?://jobs\.ashbyhq\.com/[^/]+/", re.I)),
)


def detect_ats(url):
    """The board this posting's form lives on, or "" when it is one this cannot fill.

    Matched on the URL rather than sniffed from the page, because the answer decides
    whether a browser is started at all."""
    for name, pat in ATS_PATTERNS:
        if pat.match((url or "").strip()):
            return name
    return ""


def supported_boards():
    return [name for name, _ in ATS_PATTERNS]


# Every system out there that actually takes an application, whether or not this file can
# drive it yet -- ashby, workable and the rest are real applications this cannot fill, and
# the distinction from ATS_PATTERNS above matters in two places: findform.py ranks a link
# on one of these over the aggregator it was advertised on even when no driver exists, and
# the dashboard shows which board a role is on before anyone has run /submit.
APPLY_HOSTS = re.compile(
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|"
    r"recruitee\.com|teamtailor\.com|personio\.|bamboohr\.|jobvite\.|icims\.com|"
    r"myworkdayjobs\.com|myworkdaysite\.com|breezy\.hr|join\.com|careerpuck\.com|"
    r"pinpointhq\.com|rippling\.com|paylocity\.com|eightfold\.ai)", re.I)
# Boards and job-search sites that only ever point at an application, never host one.
# Checked second and always wins the tie: hiring.cafe's own domain never appears in
# APPLY_HOSTS, but a URL that merely mentions "jobs" near a company's name could in
# principle collide, and an aggregator mistaken for an application sends a browser
# somewhere with no form on it.
AGGREGATOR_HOSTS = re.compile(
    r"(linkedin\.com|adzuna\.|indeed\.|revopsroles\.com|hiring\.cafe|reed\.co\.uk|"
    r"glassdoor\.|ziprecruiter\.|talent\.com|jooble\.|otta\.com|welcometothejungle\.)",
    re.I)


def is_apply_host(url):
    """True for a URL on a system that actually takes applications."""
    return bool(APPLY_HOSTS.search(url or "")) and not AGGREGATOR_HOSTS.search(url or "")


# APPLY_HOSTS' own group text, normalised to the short name used everywhere else in this
# codebase (findform's `ats` field, ATS_PATTERNS above, the boards.md notes) -- "ashbyhq"
# is a domain fragment, not a name anyone would recognise.
_HOST_NAMES = {"ashbyhq": "ashby", "myworkdayjobs": "workday", "myworkdaysite": "workday"}


def apply_host_name(url):
    """A short label for the system a URL applies through -- "greenhouse", "workable" --
    or "" when it is not an application host at all. Used where the board's name is worth
    showing even though no driver exists for it yet (the dashboard, findform's messages)."""
    m = APPLY_HOSTS.search(url or "")
    if not m or AGGREGATOR_HOSTS.search(url or ""):
        return ""
    name = re.sub(r"\..*", "", m.group(1))
    return _HOST_NAMES.get(name, name)


def known_links(job, fillable=None):
    """Every URL a job record already carries, most useful first.

    Costs nothing: it is reading the row, not the internet. The order is what matters --
    a link a driver can fill outranks a link to a real application system, which outranks
    the aggregator page the role happened to be advertised on. `fillable` is a callable
    like detect_ats; without one this only sorts real applications ahead of adverts."""
    seen, urls = set(), []
    for u in [job.get("url")] + [a.get("url") for a in (job.get("also_seen") or [])]:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    can_fill = fillable or (lambda _u: False)
    return sorted(urls, key=lambda u: (not can_fill(u), not is_apply_host(u)))


def application_status(job):
    """(ats, fillable) for a job record, from what it already carries -- no network call.

    `ats` is the board name worth showing (its own url first, then the best also_seen
    link), and `fillable` is true only when a driver already exists for it. This is
    deliberately the free half of findform.find_form(): it never guesses a company's board
    slug, so it costs nothing to compute for every row on every scan, and it is exactly
    what a dashboard checkmark can promise without ever opening a browser."""
    links = known_links(job, fillable=detect_ats)
    own = job.get("url") or ""
    ats = detect_ats(own) or apply_host_name(own)
    if not ats:
        for u in links:
            ats = detect_ats(u) or apply_host_name(u)
            if ats:
                break
    fillable = any(detect_ats(u) for u in ([own] + links))
    return ats, fillable


# ---------------------------------------------------------------- the field model
#
# A field is what came off the page, never what this file hoped would be there:
#   id        the DOM id, which is what the fill addresses it by
#   kind      text | textarea | select | file | checkbox
#   label     the question as a person reads it
#   required  the form's own required flag
#   options   for a select, its choices in order

TEXT, TEXTAREA, SELECT, FILE, CHECKBOX = "text", "textarea", "select", "file", "checkbox"


def field(fid, kind, label="", required=False, options=None):
    return {"id": fid, "kind": kind, "label": " ".join((label or "").split()),
            "required": bool(required), "options": list(options or [])}


def fingerprint(fields):
    """A hash of the form's shape: every field's id, kind and required flag, in order.

    This is what makes replaying a plan safe. Between the preview and the /send the runner
    has been destroyed and the form has been fetched again, possibly days later and
    possibly after the employer edited it. Same fingerprint, same form. Different
    fingerprint, and the answers Tom approved no longer describe the page in front of
    us."""
    shape = [f"{f['id']}:{f['kind']}:{int(bool(f['required']))}" for f in fields or []]
    return hashlib.sha256("|".join(shape).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- what nobody answers

# EEO and self-identification questions. Never answered by the model -- it does not even
# see them -- and never answered here either. They are voluntary by law in the US and by
# convention everywhere else these boards operate, they say nothing about whether he can do
# the job, and an automated answer to one of them is a claim about a person made by a
# script.
DEMOGRAPHIC_RE = re.compile(
    r"\b(gender|sex|race|ethnic|ethnicity|hispanic|latino|latinx|veteran|disabilit\w*|"
    r"self[- ]identif\w*|sexual orientation|orientation|transgender|pronoun|"
    r"marital status|religion|religious|national origin|age range|date of birth|"
    r"protected veteran|eeo|eeoc|equal employment)\b", re.I)

# The option to take when a demographic question is required and will not accept blank.
DECLINE_RE = re.compile(r"(decline|prefer not|don't wish|do not wish|choose not|"
                        r"not to (?:say|answer|disclose|specify)|no response)", re.I)

# Questions whose answer is a number about money. Never guessed: a salary expectation Tom
# did not give is a negotiating position invented on his behalf, and it is binding on him
# in a way no other field on the form is.
MONEY_RE = re.compile(r"\b(salary|compensation|comp expectation|expected pay|pay "
                      r"expectation|desired (?:salary|compensation|rate)|rate|package)\b",
                      re.I)


def is_demographic(label):
    return bool(DEMOGRAPHIC_RE.search(label or ""))


def decline_option(options):
    """The form's own "prefer not to say", when it has one."""
    for o in options or []:
        if DECLINE_RE.search(o or ""):
            return o
    return ""


def is_money(label):
    return bool(MONEY_RE.search(label or ""))


# ---------------------------------------------------------------- who he is
#
# Everything in here is read off the CV skeleton, which is the same file the letterhead is
# built from. That is deliberate: the name and contact details on the form are then the
# name and contact details on the CV attached to it, by construction, and a phone number
# set with /phone reaches the form the moment it reaches the page.

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/\S+", re.I)
NATIONALITY_RE = re.compile(r"nationality\s*:\s*(.+)", re.I)


def identity(base):
    """Name, email, phone, location, LinkedIn -- from the skeleton, not from a model.

    A model that gets to write the email address on an application form is a model that
    can typo it, and a form with a typo'd email is an application that silently never
    happened."""
    name = " ".join((base or {}).get("name", "").split())
    # The skeleton carries the header in caps, which is a design choice about the page and
    # not how anybody writes their name into a form.
    pretty = " ".join(w.capitalize() if w.isupper() else w for w in name.split())
    parts = pretty.split()
    out = {"full_name": pretty,
           "first_name": parts[0] if parts else "",
           "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
           "email": "", "phone": "", "location": "", "linkedin": "", "nationality": ""}
    for c in (base or {}).get("contact", []):
        text = " ".join((c.get("text") or "").split())
        href = c.get("href") or ""
        if not text:
            continue
        if EMAIL_RE.fullmatch(text):
            out["email"] = out["email"] or text
        elif LINKEDIN_RE.match(text):
            out["linkedin"] = out["linkedin"] or (href or f"https://{text}")
        elif cvbuild.PHONE_RE.match(text):
            out["phone"] = out["phone"] or text
        elif NATIONALITY_RE.match(text):
            out["nationality"] = NATIONALITY_RE.match(text).group(1).strip()
        elif "," in text and not out["location"]:
            out["location"] = text
    return out


# The fields code fills and a model never touches, matched against the DOM id first and the
# visible label second. Order matters: "preferred first name" has to be seen before
# "first name", and "current location" before "country".
IDENTITY_RULES = (
    ("first_name", re.compile(r"^(?:legal[_ ]?)?first[_ ]?name$|^given[_ ]?name$", re.I),
     re.compile(r"^(?:legal |preferred )?first name", re.I)),
    ("last_name", re.compile(r"^(?:legal[_ ]?)?last[_ ]?name$|^(?:sur|family)[_ ]?name$",
                             re.I), re.compile(r"^(?:legal )?(?:last|family|sur)ame?",
                                               re.I)),
    ("full_name", re.compile(r"^(?:full[_ ]?)?name$", re.I),
     re.compile(r"^full name$|^name$", re.I)),
    ("email", re.compile(r"e?[_ ]?mail", re.I), re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|mobile|telephone", re.I),
     re.compile(r"phone|mobile|telephone", re.I)),
    ("linkedin", re.compile(r"linked[_ ]?in", re.I), re.compile(r"linked ?in", re.I)),
    ("location", re.compile(r"^(?:current[_ ]?)?(?:location|city)$", re.I),
     re.compile(r"^(?:current |your )?(?:location|city)\b", re.I)),
)

# The two file inputs, in the order a board names them.
RESUME_RE = re.compile(r"resume|cv\b", re.I)
COVER_FILE_RE = re.compile(r"cover[_ ]?letter", re.I)
# The "Enter manually" textareas that shadow those file inputs. Filling both would attach
# the CV twice, once as a PDF and once as a wall of text.
MANUAL_TEXT_RE = re.compile(r"^(resume|cover_letter)_text$", re.I)


def identity_answer(f, ident):
    """(value, why) for a field code can fill on its own, or ("", "") when it cannot.

    "Preferred first name" is deliberately answered with his first name rather than left
    to the model: a blank preferred name on a form that requires one is a rejected
    submission, and the answer is not a judgement call."""
    fid, label = f.get("id") or "", f.get("label") or ""
    if f.get("kind") not in (TEXT, TEXTAREA, SELECT):
        return "", ""
    if is_demographic(label):
        return "", ""
    for key, id_re, label_re in IDENTITY_RULES:
        if id_re.search(fid) or label_re.search(label):
            value = ident.get(key) or ""
            if key == "first_name" and re.search(r"preferred", fid + " " + label, re.I):
                value = ident.get("first_name") or ""
            if not value:
                return "", ""
            # A dropdown is not a text box: the form has a fixed list and the answer has
            # to be on it. Country and location dropdowns are handled by the select
            # matcher instead of pretending a free-text value will take.
            if f.get("kind") == SELECT:
                match = best_option(value, f.get("options"))
                return (match, f"from the CV skeleton") if match else ("", "")
            return value, "from the CV skeleton"
    return "", ""


def best_option(value, options):
    """The option a value means, or "" when the list does not carry it.

    Exact match, then case-insensitive, then containment either way -- "Spain" against
    "Spain (España)", "Barcelona, Spain" against "Spain". Never a fuzzy score: a wrong
    country on an application is worse than a blank one Tom is told about."""
    value = " ".join((value or "").split())
    if not value or not options:
        return ""
    for o in options:
        if o == value:
            return o
    low = value.lower()
    for o in options:
        if (o or "").strip().lower() == low:
            return o
    for o in options:
        ol = (o or "").strip().lower()
        if ol and (ol in low or low in ol):
            return o
    return ""


# ---------------------------------------------------------------- yes/no from the profile

# Questions the profile answers as fact rather than judgement. Work authorisation and
# sponsorship are the two every board asks and the two a model has no business inferring:
# they are legal status, they are stated in the profile in one line, and getting one wrong
# is a withdrawn offer rather than a weaker application.
YES, NO = "yes", "no"

AUTHORISED_RE = re.compile(r"(authori[sz]ed|legally (?:able|entitled|permitted)|right to "
                           r"work|work permit|eligible to work)", re.I)
SPONSOR_RE = re.compile(r"(sponsor\w*|visa support|immigration support|require .*visa|"
                        r"need .*(?:visa|sponsorship))", re.I)


def work_status(label, market, nationality):
    """(answer, why) for a work-authorisation or sponsorship question, or ("", "").

    Answered here, off the market the role is in and the nationality on the CV, because
    both of those are facts on file. A US citizen applying to a Dublin role needs
    sponsorship and is not already authorised, and that is true whatever a model would like
    the answer to be. Anything more complicated than those two shapes is left to Tom."""
    label = label or ""
    us = bool(re.search(r"united states|u\.?s\.?a?\b|american", nationality or "", re.I))
    if not us:
        return "", ""
    # Which market the posting is in. The profile's markets are "IE-Dublin", "US-Remote"
    # and the like, so the country is the part before the dash.
    country = (market or "").split("-")[0].strip().upper()
    if not country:
        return "", ""
    home = country in ("US", "USA")
    if SPONSOR_RE.search(label):
        return (NO if home else YES), ("US citizen; the role is in "
                                       f"{country}, per the posting")
    if AUTHORISED_RE.search(label):
        return (YES if home else NO), ("US citizen; the role is in "
                                       f"{country}, per the posting")
    return "", ""


def yes_no_option(answer, options):
    """The option on a dropdown that means yes, or no. "" when the list has neither."""
    want = re.compile(r"^\s*yes\b", re.I) if answer == YES else re.compile(r"^\s*no\b", re.I)
    for o in options or []:
        if want.match(o or ""):
            return o
    return ""


# ---------------------------------------------------------------- the plan
#
# A plan is the whole submission as data: every field, the value going into it, and who
# decided that value. It is what Tom approves, what gets stored in the bank, and what is
# replayed at /send. Nothing reaches the form that is not in it.

BY_CODE, BY_MODEL, BY_TOM = "code", "model", "you"


def answer(value, by, why=""):
    return {"value": value, "by": by, "why": why}


def plan_known(fields, ident, job, files):
    """Everything code can answer on its own: identity, files, work status, declines.

    Runs before the model is asked anything, and the model is then shown only what is
    left. That ordering is the point -- a model that is never shown the email field cannot
    put the wrong address in it."""
    answers, notes = {}, []
    for f in fields:
        fid, label, kind = f["id"], f["label"], f["kind"]
        if kind == FILE:
            if RESUME_RE.search(fid) or RESUME_RE.search(label):
                if files.get("resume"):
                    answers[fid] = answer(files["resume"], BY_CODE, "the tailored CV")
            elif COVER_FILE_RE.search(fid) or COVER_FILE_RE.search(label):
                if files.get("cover_letter"):
                    answers[fid] = answer(files["cover_letter"], BY_CODE,
                                          "the cover letter")
            continue
        if MANUAL_TEXT_RE.match(fid):
            # The paste-it-in twin of a file input. The PDF is going up; this stays empty.
            continue
        if is_demographic(label):
            # Declined wherever the form offers a way to decline, required or not. This
            # used to fire only on required fields and leave the rest blank, which is the
            # same answer in practice but says nothing on the form itself; Tom would
            # rather it said so. What has not changed is the part that matters: nothing
            # here ever answers one of these questions, and a form with no decline option
            # is still left empty rather than answered.
            opt = decline_option(f.get("options")) if kind == SELECT else ""
            if opt:
                answers[fid] = answer(opt, BY_CODE, "self-identification, declined")
                notes.append(f"{label[:60]}: declined")
            continue
        value, why = identity_answer(f, ident)
        if value:
            answers[fid] = answer(value, BY_CODE, why)
            continue
        status, why = work_status(label, job.get("market"), ident.get("nationality"))
        if status:
            opt = yes_no_option(status, f.get("options")) if kind == SELECT else \
                (status.capitalize())
            if opt:
                answers[fid] = answer(opt, BY_CODE, why)
    return answers, notes


def code_owned(f):
    """True for a field only code may fill: an identity detail, or work authorisation.

    Note what this does NOT depend on: whether code managed to fill it. A phone field on a
    skeleton with no phone number in it is still not a question for a model -- an empty
    box Tom can fill in one reply is a better outcome than a plausible phone number that
    reaches an employer."""
    fid, label = f.get("id") or "", f.get("label") or ""
    for _key, id_re, label_re in IDENTITY_RULES:
        if id_re.search(fid) or label_re.search(label):
            return True
    return bool(AUTHORISED_RE.search(label) or SPONSOR_RE.search(label))


def open_questions(fields, answers):
    """What is left for the model: the questions a person would actually have to think
    about. Demographic questions and the facts code owns are filtered out here as well as
    in plan_known(), so that they are absent from the prompt itself rather than merely
    unanswered in it. A model that is never shown the phone field cannot put a number in
    it.

    A field whose question could not be read off the page is filtered out too. Some boards
    put the question in text above the control rather than in a label, and where that text
    cannot be found the honest answer is that nobody here knows what is being asked -- so
    it goes to Tom as a blank on the printed form, where he can read the question himself,
    rather than to a model that would answer it from the option list alone."""
    out = []
    for f in fields:
        if f["id"] in answers or f["kind"] in (FILE, CHECKBOX):
            continue
        if not readable_question(f):
            continue
        if is_demographic(f["label"]) or MANUAL_TEXT_RE.match(f["id"]):
            continue
        if code_owned(f):
            continue
        out.append(f)
    return out


# A label that is only an id, or nothing at all, is not a question anybody can answer.
_ID_LIKE = re.compile(r"^[0-9a-f-]{8,}$|^_systemfield|^question_\d+$", re.I)


def readable_question(f):
    """True when the field carries a question a person could actually answer."""
    label = " ".join((f.get("label") or "").split())
    return bool(label) and not _ID_LIKE.match(label) and label != f.get("id")


def screen_answers(model_answers, fields, corpus):
    """(answers, rejected). Every model answer, checked before it can reach a form.

    Two rules, both borrowed unchanged from the CV and the letter:

      - a claim that cannot be traced to the bank, the skeleton, his interview answers or
        the page that shipped does not go on the form;
      - a number that appears in none of those does not either.

    A rejected answer leaves its field blank, which is visible to Tom in the preview and
    reported to him in words. The alternative -- a plausible sentence he never said, on a
    form he signs -- is the failure this whole file exists to prevent."""
    by_id = {f["id"]: f for f in fields}
    kept, rejected = {}, []
    for fid, item in (model_answers or {}).items():
        f = by_id.get(fid)
        value = " ".join(str((item or {}).get("value") or "").split())
        if not f or not value:
            continue
        # Belt and braces on the two rules above. The model is never shown a demographic
        # question or a field code owns, so an answer to one means it wrote to a field it
        # invented an id for, and that answer does not go anywhere near the form however
        # well it screens.
        if is_demographic(f["label"]) or code_owned(f) or f["kind"] in (FILE, CHECKBOX):
            rejected.append((fid, f["label"], "not a question the model was asked"))
            continue
        claims = [c for c in ((item or {}).get("claims") or []) if str(c or "").strip()]
        # An answer that asserts nothing about his experience -- "London", "LinkedIn" --
        # still gets its numbers checked; one that does is checked whole.
        _ok, bad = cvbuild.screen_bullets(claims, corpus)
        if bad:
            rejected.append((fid, f["label"], "; ".join(w for _t, w in bad)))
            continue
        invented = cvbuild.invented_numbers(value, corpus)
        if invented:
            rejected.append((fid, f["label"],
                             f"numbers not in any source: {', '.join(invented)}"))
            continue
        if f["kind"] == SELECT:
            opt = best_option(value, f.get("options"))
            if not opt:
                rejected.append((fid, f["label"],
                                 f"'{value[:40]}' is not one of the options"))
                continue
            value = opt
        kept[fid] = answer(value, BY_MODEL, (item or {}).get("why") or "")
    return kept, rejected


def consent_fields(fields):
    """The tick-boxes a form will not submit without: privacy notices, data-processing
    acknowledgements, "I confirm the above is accurate".

    Required ones only. An optional tick-box is a marketing opt-in ("email me about future
    openings") or a diversity survey, and neither is something to agree to on somebody's
    behalf because the form happened to offer it. Required ones are ticked as part of the
    plan and named in the preview rather than buried in it: Tom is the one agreeing, so he
    is the one who has to see them before he says send."""
    return [f for f in fields if f["kind"] == CHECKBOX and f.get("required")
            and not is_demographic(f["label"])]


def blanks(fields, answers):
    """Every field with nothing in it, required ones first. This is the list Tom reads."""
    out = []
    for f in fields:
        if f["id"] in answers or f["kind"] in (FILE, CHECKBOX):
            continue
        if MANUAL_TEXT_RE.match(f["id"]):
            continue
        out.append({"id": f["id"], "label": f["label"], "required": f["required"],
                    "demographic": is_demographic(f["label"]),
                    "money": is_money(f["label"]),
                    # Carried on the blank itself so the message that asks Tom for an
                    # answer can show him what the dropdown will actually accept. A
                    # question he answers in his own words, on a field that only takes
                    # three fixed strings, is a round trip wasted.
                    "options": list(f.get("options") or [])})
    out.sort(key=lambda b: (not b["required"], b["label"]))
    return out


def missing_required(fields, answers):
    """The required fields that are still empty. Non-empty means the form cannot go."""
    return [b for b in blanks(fields, answers) if b["required"] and not b["demographic"]]


def build_plan(url, ats, fields, answers, files, notes=()):
    return {"url": url, "ats": ats, "fingerprint": fingerprint(fields),
            "fields": fields, "answers": answers, "files": files,
            "consents": [f["id"] for f in consent_fields(fields)],
            "blanks": blanks(fields, answers), "notes": list(notes)}


def plan_is_current(plan, fields):
    """True when the form on screen is still the form Tom approved."""
    return bool(plan) and plan.get("fingerprint") == fingerprint(fields)


def interview_questions(plan):
    """Every blank worth putting to Tom, in the order he is asked them.

    One list, used by the message that asks, by the reply that answers, and by the refusal
    that lists what is still open -- because the numbering IS the contract. Ask off one
    list and map the reply against another and his answer to question two lands in
    question three's box.

    Required first, since those are what stop the send, then the optional ones that are
    still real questions somebody could not answer: a salary expectation nothing could
    source, an answer the honesty screen dropped, a phone number that is not on file.

    Two kinds are deliberately absent. Demographic questions, which are never asked of him
    or of anything else. And fields whose question could not be read off the page: there is
    no way to ask a question nobody can state, so those are pointed at on the printed form
    instead."""
    out = [b for b in (plan.get("blanks") or [])
           if not b.get("demographic") and readable_question(b)]
    return [b for b in out if b.get("required")] + \
           [b for b in out if not b.get("required")]


def apply_replies(plan, replies):
    """Tom's own answers to the questions nothing could source, keyed by the numbers the
    preview showed him. Returns (plan, filled, unmatched).

    His answer goes in as his words, unscreened, because the screen exists to stop a model
    inventing his experience and he cannot invent his own. The one thing it will not do is
    put a value on a dropdown that does not carry it: that comes back in `unmatched` and he
    is shown the options again, rather than having a near-miss forced onto the form."""
    unanswered = interview_questions(plan)
    filled, unmatched = [], []
    for n, text in sorted((replies or {}).items()):
        idx = int(n) - 1
        if not (0 <= idx < len(unanswered)) or not str(text or "").strip():
            continue
        b = unanswered[idx]
        f = next((x for x in plan["fields"] if x["id"] == b["id"]), None)
        value = " ".join(str(text).split())
        if f and f["kind"] == SELECT:
            opt = best_option(value, f.get("options"))
            if not opt:
                unmatched.append((b, value))
                continue
            value = opt
        plan["answers"][b["id"]] = answer(value, BY_TOM, "your answer")
        filled.append((b["label"], value))
    plan["blanks"] = blanks(plan["fields"], plan["answers"])
    return plan, filled, unmatched


# ---------------------------------------------------------------- the browser

def ensure_browser(quiet=False):
    """Install Playwright and its Chromium, the first time a form is actually filled.

    Not a workflow step, for the same reason the LibreOffice install is not one: this runs
    at most once per application, and a workflow step runs on all ~96 ticks a day."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        if not quiet:
            print("  installing playwright")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "playwright"],
                       check=True, timeout=600)
    # `install` is idempotent and cheap once the browser is on disk, so it runs every time
    # rather than being guarded by a path that changes with every Playwright release.
    if not quiet:
        print("  ensuring chromium is present")
    subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps",
                    "chromium"], check=True, timeout=1200)


# What the page hands back for every control on it. Kept in JavaScript rather than
# assembled from Playwright locators because a form is a DOM and this is the one question
# the DOM answers directly: what is on you, in order, with what labels.
READ_FIELDS_JS = r"""
() => {
  const seen = new Set();
  const out = [];
  const labelFor = (el) => {
    if (el.labels && el.labels.length) return el.labels[0].innerText || '';
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const n = document.getElementById(by.split(' ')[0]);
      if (n) return n.innerText || '';
    }
    return el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
  };
  // The nearest readable text around a control, for the boards that put a question above
  // its input rather than in a <label>. Approximate by construction, so it is only ever a
  // fallback -- and a field that ends up with nothing better is never handed to a model,
  // see open_questions().
  const nearText = (el) => {
    let n = el, hops = 0;
    while (n && hops < 4) {
      const t = (n.innerText || '').trim();
      if (t && t.length < 200) return t.split('\n')[0];
      n = n.parentElement; hops++;
    }
    return '';
  };
  document.querySelectorAll('input, select, textarea').forEach((el) => {
    const type = (el.type || '').toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button') return;
    const id = el.id || el.name || '';
    if (!id || seen.has(id)) return;
    seen.add(id);
    let kind = 'text';
    if (el.tagName === 'TEXTAREA') kind = 'textarea';
    else if (el.tagName === 'SELECT') kind = 'select';
    else if (type === 'file') kind = 'file';
    else if (type === 'checkbox' || type === 'radio') kind = 'checkbox';
    else if (el.getAttribute('role') === 'combobox') kind = 'select';
    let options = [];
    if (el.tagName === 'SELECT') {
      options = [...el.options].map((o) => (o.text || '').trim()).filter(Boolean);
    }
    out.push({
      id,
      kind,
      // name, type and hidden are what a driver's reshape() needs to tell a radio group
      // from a tick-box and a real consent box from a hidden yes/no. Unused by boards
      // whose controls are already plain inputs.
      name: el.name || '',
      type,
      context: nearText(el),
      hidden: !(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
      label: (labelFor(el) || '').replace(/\*/g, ' ').trim(),
      required: el.required || el.getAttribute('aria-required') === 'true',
      options,
    });
  });
  return out;
}
"""


# Invisible anti-automation. Both boards this code knows about run one: Greenhouse loads
# reCAPTCHA Enterprise (GOOGLE_RECAPTCHA_INVISIBLE_KEY, recaptcha/enterprise.js) into the
# application page, and Ashby loads an invisible reCAPTCHA v2 with a hidden
# g-recaptcha-response field on the form itself.
#
# Nothing here tries to get past one, and nothing here ever will: that is what these
# checks are for, and a job application is exactly the kind of submission an employer is
# entitled to want a person behind. What this does instead is say so -- in the preview,
# before Tom decides to send, and again if a submission goes through unconfirmed -- so
# that a form which bounces is a known risk he took rather than a mystery.
ANTI_BOT_JS = r"""
() => {
  const html = document.documentElement.innerHTML;
  const found = [];
  if (/recaptcha/i.test(html)) found.push('reCAPTCHA');
  if (/hcaptcha/i.test(html)) found.push('hCaptcha');
  if (/turnstile/i.test(html)) found.push('Turnstile');
  return [...new Set(found)].join(', ');
}
"""


def anti_bot(page):
    """What automated-submission check the page carries, or "". Never acted on beyond
    saying it out loud."""
    try:
        return page.evaluate(ANTI_BOT_JS) or ""
    except Exception:
        return ""


def read_fields(page, driver):
    """Every control on the form, with its label, in page order.

    A combobox carries its choices only once it has been opened, so the driver opens each
    one, reads the list and closes it again. That is slow and it is the only honest way to
    know what a dropdown will accept before trying to put something in it.

    A board whose controls are not plain inputs gets a `reshape` hook: Ashby draws a yes/no
    question as a hidden checkbox behind two buttons, and an EEOC question as a radio group
    where the generic reader sees one field per radio. Reshaping there rather than in the
    JS keeps one reader for every board and puts each board's oddities in its own class."""
    raw = page.evaluate(READ_FIELDS_JS)
    if hasattr(driver, "reshape"):
        raw = driver.reshape(raw)
    fields = []
    for r in raw:
        f = field(r["id"], r["kind"], r["label"], r["required"], r.get("options"))
        if f["kind"] == SELECT and not f["options"]:
            f["options"] = driver.read_options(page, f["id"])
        fields.append(f)
    return fields


def fill_form(page, plan, driver, bank_path=""):
    """Put the plan on the page. Returns the ids it could not fill.

    Never raises for one stubborn field: a form is filled as far as it goes and the
    preview shows Tom the result, which is more use than an exception naming the first
    thing that would not take."""
    failed = []
    by_id = {f["id"]: f for f in plan.get("fields", [])}
    for fid, item in (plan.get("answers") or {}).items():
        f = by_id.get(fid)
        if not f:
            continue
        value = item.get("value")
        try:
            if f["kind"] == FILE:
                path = value if os.path.isabs(value) else os.path.join(bank_path, value)
                if not os.path.exists(path):
                    failed.append(fid)
                    continue
                page.set_input_files(id_selector(fid), path, timeout=BROWSER_TIMEOUT_MS)
            elif f["kind"] == SELECT:
                driver.pick_option(page, fid, value)
            elif f["kind"] == CHECKBOX:
                page.check(id_selector(fid), timeout=BROWSER_TIMEOUT_MS)
            else:
                page.fill(id_selector(fid), str(value), timeout=BROWSER_TIMEOUT_MS)
        except Exception as e:
            print(f"  could not fill {fid}: {str(e)[:120]}")
            failed.append(fid)
    for fid in plan.get("consents", []):
        try:
            page.check(id_selector(fid), timeout=BROWSER_TIMEOUT_MS)
        except Exception as e:
            print(f"  could not tick {fid}: {str(e)[:120]}")
            failed.append(fid)
    return failed


def css_id(fid):
    """A DOM id as a CSS selector. Greenhouse names its checkbox groups
    `question_123[]_456`, and an unescaped bracket is a CSS attribute selector."""
    return re.sub(r"([\[\]().:,+~*^$|>/])", r"\\\1", fid)


def id_selector(fid):
    """A selector for one element by id, whatever the id happens to be.

    An attribute selector rather than `#id`, because Ashby names every custom question
    after a UUID and a CSS id selector cannot start with a digit -- `#4b728746-d0b1` is a
    syntax error, not a miss, so it takes the whole fill down with it. Only the quote
    needs escaping here, which is the point: no character class to keep up to date."""
    return '[id="{}"]'.format(str(fid).replace("\\", "\\\\").replace('"', '\\"'))


# A4 at 96dpi is 794 CSS pixels wide. The fill runs in a 1280px window because that is
# the layout a board's own QA looked at, but printing that straight to A4 crops the right
# edge off every field -- which is exactly where the answers are. So the window is narrowed
# to print width first and the page is given a moment to reflow, which every one of these
# forms does because they are all built for a phone too.
PRINT_WIDTH = 820
PRINT_SCALE = 0.9


def form_pdf(page, path):
    """The filled form, printed. This is what Tom approves.

    A PDF and not a screenshot: he reads the CV and the letter as PDFs on his phone, a
    full-page screenshot of a long form is a single unreadable strip, and the print view
    puts the answers next to the questions they answer."""
    page.emulate_media(media="screen")
    page.set_viewport_size({"width": PRINT_WIDTH, "height": 1400})
    page.wait_for_timeout(400)
    page.pdf(path=path, format=PDF_FORMAT, print_background=True, scale=PRINT_SCALE,
             margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
    return path


# ---------------------------------------------------------------- the boards
#
# One class per board, holding the three things that are genuinely board-specific: how its
# dropdowns open, how the form is submitted, and how it says the application went through.
# Everything else -- reading the fields, planning the answers, screening them, printing the
# page -- is the same for every board and lives above.


# A board-hosted Greenhouse posting, and the same application as an embeddable form.
#
# Not every employer serves the form at the board URL. Okta's board link 302s to
# okta.com/company/careers/..., which renders the application in an embed, so a browser
# sent to the board URL lands on a page with no fields on it at all. The embed endpoint is
# the same application, is not redirected, and is what the company's own careers page is
# showing inside itself. So the board URL is tried first and this is the fallback.
GREENHOUSE_JOB_URL = re.compile(
    r"^https?://(?:job-)?boards\.greenhouse\.io/(?!embed\b)([\w-]+)/jobs/(\d+)", re.I)
GREENHOUSE_EMBED = ("https://job-boards.greenhouse.io/embed/job_app?for={slug}"
                    "&token={job_id}")


def greenhouse_embed(url):
    """The embed form for a board posting, or "" when the URL is not one."""
    m = GREENHOUSE_JOB_URL.match((url or "").strip())
    return GREENHOUSE_EMBED.format(slug=m.group(1), job_id=m.group(2)) if m else ""


class Greenhouse:
    """Greenhouse's hosted boards (job-boards.greenhouse.io).

    The application form is usually on the posting's own page, so there is usually nothing
    to navigate to -- and where there is, see greenhouse_embed() above.

    The dropdowns are react-select, which means the visible control is a text input with
    `role="combobox"` and the choices exist only while the menu is open; react-select gives
    each option the id `react-select-<field>-option-<n>`, which is what the two methods
    below address them by."""

    name = "greenhouse"
    submit_selectors = ("button[type=submit]",
                        "button:has-text('Submit application')",
                        "button:has-text('Submit Application')")
    # What the page says once it has taken the application. Checked as text on the page
    # rather than as a URL, because a board is free to confirm in place.
    confirmation = re.compile(r"(thank you for applying|application (?:has been )?"
                              r"submitted|we(?:'| ha)ve received your application|"
                              r"thanks for applying)", re.I)

    FORM_READY = "#first_name, input[type=file]"

    def open(self, page, url):
        if not self._land(page, url):
            # No fields on the page. Either the employer redirected the board URL to a
            # careers page that embeds the form, or the posting is gone; the embed
            # endpoint answers both, because a dead posting has no embed either.
            embed = greenhouse_embed(url)
            if embed and embed != url:
                print(f"  no form at the board URL; trying the embed")
                self._land(page, embed)
        page.wait_for_timeout(SETTLE_MS)

    def _land(self, page, url):
        """Navigate, and say whether a form actually turned up. The wait is on the first
        field existing rather than on a network idle, which a board with a live chat widget
        on it never reaches."""
        page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        try:
            page.wait_for_selector(self.FORM_READY, timeout=BROWSER_TIMEOUT_MS)
            return True
        except Exception:
            return False

    def _menu_options(self, page, fid):
        return page.eval_on_selector_all(
            f"[id^='react-select-{fid}-option']",
            "els => els.map(e => (e.innerText || '').trim()).filter(Boolean)")

    def read_options(self, page, fid):
        """Open the menu, read it, close it. Returns [] for a combobox that has none."""
        sel = id_selector(fid)
        try:
            page.click(sel, timeout=BROWSER_TIMEOUT_MS)
            page.wait_for_timeout(250)
            options = self._menu_options(page, fid)
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)
            return options
        except Exception as e:
            print(f"  could not read the options on {fid}: {str(e)[:120]}")
            return []

    def pick_option(self, page, fid, value):
        """Choose one option by its exact text. Raises when the option is not there, which
        is the right outcome: a dropdown that has changed since the preview is a form that
        has changed, and a near-miss on a work-authorisation dropdown is not a typo."""
        sel = id_selector(fid)
        page.click(sel, timeout=BROWSER_TIMEOUT_MS)
        page.wait_for_timeout(250)
        options = self._menu_options(page, fid)
        if value not in options:
            page.keyboard.press("Escape")
            raise RuntimeError(f"{fid}: '{value}' is not on the menu ({len(options)} "
                               f"options)")
        page.click(f"[id^='react-select-{fid}-option'] >> text='{value}'",
                   timeout=BROWSER_TIMEOUT_MS)
        page.wait_for_timeout(150)

    def submit(self, page):
        for sel in self.submit_selectors:
            try:
                page.click(sel, timeout=8000)
                return True
            except Exception:
                continue
        return False

    def confirmed(self, page):
        try:
            body = page.inner_text("body", timeout=BROWSER_TIMEOUT_MS)
        except Exception:
            body = ""
        return bool(self.confirmation.search(body or "")), (body or "")[:400]


class Ashby:
    """Ashby's hosted boards (jobs.ashbyhq.com).

    Written against a real posting, dumped with tools/probe_form.py -- the field map and
    the reasoning are in tools/notes/boards.md. Four things differ from Greenhouse and all
    four are why this needed its own class:

      - **The form is a page further on.** findform hands back the posting
        (`/<slug>/<id>`); the application is `/<slug>/<id>/application`.
      - **One name field, not two.** `#_systemfield_name` takes the whole name, which
        IDENTITY_RULES already covers by matching a bare `name`.
      - **A yes/no question is two buttons over a hidden checkbox.** The checkbox carries
        the question's id in its `name` and is never clickable; the buttons are what a
        person presses. So it is read as a dropdown of Yes/No and filled by pressing one.
      - **A second, id-less file input sits above the form** ("Autofill from resume"). It
        feeds Ashby's parser and rewrites the fields underneath it, so the resume must go
        to `#_systemfield_resume` by id and never to `input[type=file]` generally. The
        generic reader skips it already, because it has neither id nor name.

    The submit button is behind an invisible reCAPTCHA, exactly as Greenhouse's is. Nothing
    here tries to get past it -- see anti_bot() and the note in boards.md."""

    name = "ashby"
    submit_selectors = ("button:has-text('Submit Application')",
                        "button:has-text('Submit application')",
                        "button[type=submit]")
    confirmation = re.compile(r"(thank you for applying|application (?:has been )?"
                              r"submitted|we(?:'| ha)ve received your application|"
                              r"thanks for applying|your application)", re.I)
    FORM_READY = "#_systemfield_name, #_systemfield_email"

    def open(self, page, url):
        page.goto(self.application_url(url), wait_until="domcontentloaded",
                  timeout=BROWSER_TIMEOUT_MS)
        try:
            page.wait_for_selector(self.FORM_READY, timeout=BROWSER_TIMEOUT_MS)
        except Exception:
            pass
        # Ashby renders the form client-side after the shell, so the fields arrive a beat
        # after the page does. A plain fetch of this URL returns a spinner and nothing else.
        page.wait_for_timeout(SETTLE_MS)

    # jobs.ashbyhq.com/<slug>/<posting id>, which is what findform hands back. Matched
    # rather than assumed, so that a URL which is already the form, or is not an Ashby
    # posting at all, is left exactly as it is instead of growing a path segment.
    POSTING_URL = re.compile(
        r"^https?://jobs\.ashbyhq\.com/[^/]+/[^/?#]+/?$", re.I)

    @classmethod
    def application_url(cls, url):
        """The application page for a posting URL, or the URL untouched when it is not
        one. Idempotent: a URL already pointing at the form has no posting shape."""
        u = (url or "").strip()
        return u.rstrip("/") + "/application" if cls.POSTING_URL.match(u) else u

    # A yes/no question: a checkbox that is not visible, whose name is the question's id.
    # The generic reader reports it as a checkbox, which would make it a consent box and
    # get it silently ticked. It is a question, and it is answered by pressing a button.
    def reshape(self, raw):
        out, radios = [], {}
        for r in raw:
            name = (r.get("name") or "").strip()
            # An EEOC radio group: one entry per radio from the generic reader, and the
            # group's name is the question. Collapsed into one dropdown whose options are
            # the radios' own labels, so screen_answers() and the decline rule see a
            # single field with a "Decline to self-identify" option on it.
            if r["kind"] == CHECKBOX and r.get("type") == "radio" and name:
                g = radios.get(name)
                if not g:
                    g = {"id": name, "kind": SELECT, "label": "", "required": r["required"],
                         "options": [], "type": "radio"}
                    radios[name] = g
                    out.append(g)
                if r.get("label"):
                    g["options"].append(r["label"])
                g["required"] = g["required"] or r["required"]
                continue
            if r["kind"] == CHECKBOX and r.get("hidden") and name:
                # The question is not on the checkbox -- it is the text above the two
                # buttons. nearText() reaches it when it is close enough, and returns the
                # buttons' own "Yes / No" when it is not, which is no question at all and
                # is dropped so that open_questions() treats the field as unreadable.
                label = r.get("label") or r.get("context") or ""
                if re.fullmatch(r"\s*(yes|no)(\s*[/|,]\s*(yes|no))*\s*", label, re.I):
                    label = ""
                out.append({"id": name, "kind": SELECT, "label": label,
                            "required": r["required"], "options": ["Yes", "No"],
                            "type": "yesno"})
                continue
            out.append(r)
        # A radio group's question is not on any radio; it is the text above them. The
        # generic reader's `context` is the nearest readable ancestor text, which is that
        # question, so it stands in when nothing better is on the field itself.
        for g in radios.values():
            if not g["label"]:
                g["label"] = _EEOC_LABELS.get(
                    re.sub(r"^.*__", "", g["id"]), g["id"])
        return out

    def read_options(self, page, fid):
        """A reshaped field already knows its options; anything else here is a real
        combobox (Ashby's location autocomplete), which has no fixed list to read."""
        return []

    def pick_option(self, page, fid, value):
        """Press the button that says it. Ashby's yes/no and EEOC answers are buttons and
        radio labels, not menu items, so there is no menu to open."""
        for sel in (f"[name='{fid}'] ~ * >> text='{value}'",
                    f"xpath=//*[@name='{fid}']/ancestor::*[position()<=4]"
                    f"//*[normalize-space(text())='{value}']",
                    f"label:has-text('{value}')"):
            try:
                page.click(sel, timeout=6000)
                page.wait_for_timeout(150)
                return
            except Exception:
                continue
        raise RuntimeError(f"{fid}: could not find anything to press for '{value}'")

    def submit(self, page):
        for sel in self.submit_selectors:
            try:
                page.click(sel, timeout=8000)
                return True
            except Exception:
                continue
        return False

    def confirmed(self, page):
        try:
            body = page.inner_text("body", timeout=BROWSER_TIMEOUT_MS)
        except Exception:
            body = ""
        return bool(self.confirmation.search(body or "")), (body or "")[:400]


# Ashby names an EEOC radio group `<uuid>__systemfield_eeoc_<what>`, and the question text
# itself is not on any of the radios. These are the labels a person reads above them, so a
# reshaped group is recognisable to is_demographic() -- which is the whole point, since
# that is what keeps every one of them unanswered.
_EEOC_LABELS = {
    "systemfield_eeoc_gender": "Gender",
    "systemfield_eeoc_race": "Race / ethnicity",
    "systemfield_eeoc_veteran_status": "Protected veteran status",
    "systemfield_eeoc_disability": "Disability status",
}


DRIVERS = {"greenhouse": Greenhouse, "ashby": Ashby}


def driver_for(ats):
    cls = DRIVERS.get(ats)
    return cls() if cls else None


class Session:
    """A browser, for the length of one fill. Chromium, headless, one page.

    Deliberately not reused between the preview and the submission. The runner that filled
    the form has been destroyed by the time Tom approves it, often days later, so the
    submission opens the form again and replays the plan onto it -- see plan_is_current()
    for what stops that being a guess."""

    def __init__(self, headless=True):
        self.headless = headless
        self._pw = None
        self.browser = None
        self.page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launch = {"headless": self.headless}
        # The sandbox this is developed in ships a browser at a fixed path and blocks the
        # download; a runner has neither problem. Honour the override when it is set.
        exe = os.environ.get("APPLYQ_CHROMIUM")
        if exe:
            launch["executable_path"] = exe
        proxy = os.environ.get("APPLYQ_BROWSER_PROXY")
        if proxy:
            launch["proxy"] = {"server": proxy}
        self.browser = self._pw.chromium.launch(**launch)
        context = self.browser.new_context(
            viewport={"width": 1280, "height": 1600},
            # A real form filled by a real person. Nothing here evades a bot check: the
            # default headless string simply makes some boards render a fallback page,
            # and a fallback page is a form this cannot read.
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"))
        context.set_default_timeout(BROWSER_TIMEOUT_MS)
        self.page = context.new_page()
        return self

    def __exit__(self, *exc):
        for close in (getattr(self.browser, "close", None),
                      getattr(self._pw, "stop", None)):
            try:
                if close:
                    close()
            except Exception:
                pass
        return False


# ---------------------------------------------------------------- the two entry points

def read_form(url, ats, headless=True):
    """(fields, driver_name). Open the form and read it, touching nothing."""
    driver = driver_for(ats)
    if not driver:
        raise RuntimeError(f"no driver for {ats or 'this board'}")
    with Session(headless=headless) as s:
        driver.open(s.page, url)
        return read_fields(s.page, driver), driver.name


def preview(plan, out_pdf, bank_path="", headless=True):
    """Fill the form and print it. Returns (pdf_path, failed_ids, fields_now, anti_bot).

    There is no submit in this function and there is no argument that would add one. That
    is the safety model: the only code that can click the button is submit_form(), and the
    only thing that calls submit_form() is an explicit /send from Tom."""
    driver = driver_for(plan.get("ats"))
    with Session(headless=headless) as s:
        driver.open(s.page, plan["url"])
        fields_now = read_fields(s.page, driver)
        failed = fill_form(s.page, plan, driver, bank_path)
        form_pdf(s.page, out_pdf)
        return out_pdf, failed, fields_now, anti_bot(s.page)


def submit_form(plan, out_pdf, bank_path="", headless=True):
    """Replay the approved plan and send it. Returns a result dict.

    Three things have to hold before the button is clicked, and each of them is a refusal
    rather than a warning:

      - the form is the one Tom approved (same fingerprint);
      - every required field it has is filled;
      - the fill itself did not fail on a field the form requires.

    After the click the page is read back and the confirmation checked. An application
    that was clicked but not confirmed is reported as exactly that -- not as sent -- so
    that the one thing Tom is told with certainty is the one thing the page actually
    said."""
    driver = driver_for(plan.get("ats"))
    with Session(headless=headless) as s:
        driver.open(s.page, plan["url"])
        fields_now = read_fields(s.page, driver)
        if not plan_is_current(plan, fields_now):
            return {"sent": False, "reason": "changed", "fields": fields_now}
        failed = fill_form(s.page, plan, driver, bank_path)
        required_failed = [f["id"] for f in fields_now
                           if f["required"] and f["id"] in failed]
        still_missing = missing_required(fields_now, plan.get("answers") or {})
        if required_failed or still_missing:
            form_pdf(s.page, out_pdf)
            return {"sent": False, "reason": "incomplete", "failed": required_failed,
                    "missing": still_missing, "pdf": out_pdf, "fields": fields_now}
        # The filled page is printed BEFORE the click, so that what he is told went out is
        # a document of what actually went out, whatever the confirmation page then says.
        form_pdf(s.page, out_pdf)
        if not driver.submit(s.page):
            return {"sent": False, "reason": "no-button", "pdf": out_pdf,
                    "fields": fields_now}
        checks = anti_bot(s.page)
        s.page.wait_for_timeout(SETTLE_MS * 2)
        ok, body = driver.confirmed(s.page)
        return {"sent": bool(ok), "reason": "" if ok else "unconfirmed", "pdf": out_pdf,
                "page_said": body, "url": s.page.url, "fields": fields_now,
                "anti_bot": checks}


# ---------------------------------------------------------------- his own answers

# "1. Dublin  2) 3 months" -- the shape a numbered list comes back in on a phone, allowing
# for the numbers he does and does not bother to type.
# The separator is optional because "1 yes" is what gets typed on a phone, and the number
# has to open a line, which is the only thing keeping "3 months notice" from reading as
# answer three. An answer that genuinely starts with a number needs its own number in
# front of it.
NUMBERED_RE = re.compile(
    r"(?:^|\n)[ \t]*(\d{1,2})[ \t]*[.)\]:-]?[ \t]+(.+?)(?=\n[ \t]*\d{1,2}[ \t]*[.)\]:-]?[ \t]+|$)",
    re.S)


def parse_numbered(reply, n_expected):
    """{index: answer} from a reply to the numbered questions in a preview.

    Parsed in code, not by a model: the preview numbered the questions itself, so the
    mapping is arithmetic. A reply with no numbers at all is taken as the answer to the
    only question when there is only one, and ignored when there is more than one -- an
    unnumbered paragraph split across three legal questions is exactly the kind of
    helpfulness that puts the wrong answer on a form."""
    found = {int(m.group(1)): " ".join(m.group(2).split())
             for m in NUMBERED_RE.finditer("\n" + (reply or ""))}
    found = {k: v for k, v in found.items() if v and 1 <= k <= max(n_expected, 1)}
    if not found and n_expected == 1 and (reply or "").strip():
        return {1: " ".join(reply.split())}
    return found
