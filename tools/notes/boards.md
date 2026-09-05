# Ashby's application form, as it actually is

Dumped from a real posting (Vanta, `jobs.ashbyhq.com/<slug>/<id>/application`) with
`tools/probe_form.py`, on 2 Sep 2026. Kept because the field map is the expensive half of
writing a driver and it does not change often. Nothing here is private: it is the public
structure of a public form.

## Why the driver waited (historical)

*Superseded 3 Sep 2026: the driver is built. Kept because the reasoning is still the
reasoning, and the reCAPTCHA half of it has not changed.*

Not because the page is hard. It is not: the ids below are stable and the whole thing is
more tractable than Greenhouse's react-select dropdowns.

The reason is the last line of the dump. **The form runs an invisible reCAPTCHA v2** (an
`iframe` from `recaptcha.net/recaptcha/api2/anchor?...&size=invisible`, with a hidden
`g-recaptcha-response` textarea for the widget to fill in). Greenhouse does the same thing
by a different route: `GOOGLE_RECAPTCHA_INVISIBLE_KEY` and
`recaptcha/enterprise.js` are in every application page it serves.

Nothing in this repo tries to get past either, and nothing in it ever should. That check is
there precisely to make a person be behind a submission, and a job application is exactly
the kind of submission an employer is entitled to want a person behind.

So the value of an Ashby driver depends entirely on a question no amount of reading
answers: **does a real submission from a runner get through the check at all?** The
Greenhouse path already answers it, the first time Tom runs `/submit` and `/send` on a
Greenhouse role he actually wants. If it goes through, this driver is worth an hour. If it
bounces, an Ashby driver would produce a filled form nobody can send, and the direct link
findform.py already hands him is the whole of the available value.

**What changed:** that test never happened, because there was nothing to run it on. With
the dashboard cut to the last 7 days there was exactly **one** auto-fillable Greenhouse
role at 6.0+ in the window, against five on Ashby. Waiting on a test that cannot be run is
not caution, it is a stall -- and the same reCAPTCHA sits in front of both boards, so
building Ashby is now the way the question gets answered at all rather than a bet placed
before it. Ashby also leads the whole dataset (21 rows), and the driver took the hour the
note predicted.

## The field map

| Field | How to address it | Notes |
|---|---|---|
| Full Name | `#_systemfield_name` | **One** name field, not first/last. Required. |
| Email | `#_systemfield_email` | Required. |
| Resume | `#_systemfield_resume` | Required. |
| "Autofill from resume" | a **second** `input[type=file]` with no id, above the form | Do not touch it. It feeds their parser and rewrites the fields. Target the id above, never `input[type=file]` generally. |
| Custom questions | `#<uuid>` / `[name="<uuid>"]` | Text, tel, textarea. The question is in the `<label>`, which `read_fields()` already reads. |
| Location | `input[role=combobox]`, no id, placeholder "Start typing..." | An autocomplete, not a fixed list. Needs typing and a menu selection. |
| Yes/No questions | a hidden `input[type=checkbox]` named `<uuid>`, driven by visible **buttons** reading Yes and No | Not a dropdown. Click the button, do not check the box. |
| Texting consent | `input[type=radio][name=communicationConsent]` | Optional. Leave alone. |
| EEOC | `input[type=radio]` named `<uuid>__systemfield_eeoc_gender`, `..._race`, `..._veteran_status` | Every one offers "Decline to self-identify", which is the only option this code would ever pick, and only when required. |
| Submit | a `button` reading "Submit Application" | |

No `data-testid` anywhere on the page, so ids and label text are the handles.

## What a driver would have to add beyond the Greenhouse one

1. `open()` appends `/application` to a posting URL, since findform returns the posting.
2. A `read_options`/`pick_option` pair for the Yes/No **button** pattern.
3. The location combobox: type, wait for the menu, pick.
4. Radio groups as a field kind, which the Greenhouse driver never needed.
5. `identity()` already produces `full_name`, and `IDENTITY_RULES` already matches a bare
   `name` field, so the one-name-field shape needs nothing new.


---

# Ashby: built, 3 Sep 2026

The map below is what the driver in `submit.py` was written against, and it held: the only
surprises came from the browser test, not the page.

Two bugs it caught that no amount of reading would have:

1. **A CSS id selector cannot start with a digit.** Every Ashby custom question is named
   after a UUID, so `#4b728746-d0b1` is a *syntax error* rather than a miss, and it took
   the whole fill down with it. Fields are addressed with `[id="..."]` now
   (`submit.id_selector`), which has no such rule and needs no character class kept up to
   date.
2. **A hidden yes/no checkbox reads as a consent box.** Before reshaping it is a checkbox,
   and `consent_fields()` ticks required checkboxes -- so the fill was answering a question
   nobody had chosen an answer to. It is reshaped into a Yes/No dropdown before anything
   sees it.

The one thing still worth knowing: **a yes/no question's text is not on its checkbox.** It
is in the markup above the buttons, and `nearText()` in `READ_FIELDS_JS` reaches it when it
is close enough. When it does not, the field ends up with no readable question, and
`readable_question()` keeps it away from the model entirely -- it goes to Tom as a blank on
the printed form, where he can read the question himself. That rule is board-agnostic and
worth keeping.

---

# Workable, and why the link in the row is not the form either

Probed 2 Sep 2026 against Triptease's Revenue Operations Manager, the role that sent
`/submit` back for a second look.

`jobs.workable.com/view/<token>/<slug>` is **Workable's own job board, not an application
form**. The probe found:

```
--- 0 controls
--- buttons
{"text": "Apply now"}   x2
{"text": "Accept all"}  (cookie banner)
```

Zero inputs. Two "Apply now" buttons that navigate in JavaScript rather than carrying an
href, so the real form is one hop further on, at the employer's own
`apply.workable.com/<account>/j/<SHORTCODE>/apply/`. And it carries an anti-automation
check, like every other board here.

That makes `jobs.workable.com/view/...` a slightly optimistic thing for `is_apply_host()`
to return true for: it is the canonical posting rather than an advert, which is why it
still beats a revopsroles link and is worth handing to Tom, but a driver could not fill it
without clicking through first.

So a Workable driver needs one thing the Greenhouse one does not: `open()` has to press
"Apply now" and wait for the real form before reading any fields. Worth knowing, not worth
building until the reCAPTCHA question above is settled.

# Lever: built, 5 Sep 2026

Probed against a live Octopus Energy posting
(`jobs.lever.co/octoenergy/<id>/apply`, 45 controls). The plainest form of the three:
server-rendered HTML, no react-select, nothing hydrated after load. What it does have is
one shape neither Greenhouse nor Ashby has, and it broke two things that looked generic.

## The field map

| what | how it is addressed |
| --- | --- |
| CV | `input[name=resume]`, `#resume-upload-input`, `data-qa=input-resume` |
| full name | `input[name=name]`, `data-qa=name-input` |
| email / phone | `input[name=email]` / `input[name=phone]` |
| current location | `#location-input` + hidden `#selected-location` |
| current company | `input[name=org]` |
| links | `input[name="urls[LinkedIn]"]`, and Twitter / GitHub / Portfolio / Other |
| custom question | `cards[<uuid>][field<n>]` — text, textarea, select or radio group |
| demographic survey | `surveysResponses[<uuid>][responses][field<n>]` |
| submit | `#btn-submit`, `data-qa=btn-submit` |

## The two things that were not board-specific after all

Both were latent bugs in the generic half, not Lever quirks, and both would have silently
filled nothing rather than raised:

- **`id_selector()` only ever built `[id="..."]`.** `READ_FIELDS_JS` has always identified
  a control as `el.id || el.name`, so a form whose inputs have names and no ids read
  perfectly and then matched nothing at fill time. Lever is that form: `name`, `email`,
  `phone`, `org` and every custom question have no id at all. It now selects on either.
- **Radios sharing a name collapsed to one option.** The reader deduped on `el.id ||
  el.name`, which is fine for Ashby (its EEOC radios have distinct ids) and wrong for
  Lever (its radios have none), where a four-option question read as one option. The
  dedupe key now includes the radio's value.

## The one that genuinely is Lever's

A custom question's text is written **once**, above the controls that answer it
(`.application-question .application-label .text`); the radios below carry only their own
option text. No amount of rearranging the field list reaches it, so `reshape()` is now
handed the page and `LEVER_QUESTIONS_JS` reads the question block for every control.
`reshape(raw, page)` is the contract for every driver now; Ashby ignores the page.

And **current location is a typeahead**. Typing the city is not choosing it: Lever submits
the hidden `selectedLocation`, which stays empty until a suggestion is clicked. Hence the
`settle()` hook — a last pass after the fill, for a board whose fields are not finished the
moment they are filled.

## hCaptcha, and the same rule as ever

The apply page loads an invisible hCaptcha (`div#h-captcha[data-sitekey]`, a hidden
`h-captcha-response` input, `newassets.hcaptcha.com` enclave iframes). A different vendor
from Greenhouse's reCAPTCHA Enterprise and Ashby's reCAPTCHA v2; the same rule. Nothing
tries to get past it. `anti_bot()` already matched `hcaptcha`, so it is named in the
preview before Tom decides to send, and again if a submission goes through unconfirmed.

# SmartRecruiters: read the board, do not drive the form

Worth writing down, because the board API is genuinely useful and the driver is not
possible, and those two facts look contradictory from the outside.

**The board API is in.** `api.smartrecruiters.com/v1/companies/<slug>/postings` needs no
key and is how ServiceNow's London and Dublin roles stopped showing as "no board found".

**The form is not.** Two things, found by probing:

1. The apply form is not on the posting page. A probe of
   `jobs.smartrecruiters.com/<company>/<id>` found **one** control, and it was a WeChat
   share input. The application lives on a separate host:
   `jobs.smartrecruiters.com/oneclick-ui/company/<company>/publication/<uuid>?dcr_ci=<company>`,
   where the uuid is already in the board API response.
2. That host is behind **DataDome**. A probe of the oneclick URL returned **0 controls**
   and a `geo.captcha-delivery.com/captcha/` iframe — bot detection that stops the page
   rendering at all, rather than a captcha at the submit button. There is nothing to read,
   let alone fill.

So `detect_ats()` deliberately does not claim SmartRecruiters, `application_status()` shows
it as a board with no tick, and the dashboard link is one Tom opens himself.

## A caution about measuring board coverage

SmartRecruiters answers **200 with `totalFound: 0`** for a slug that does not exist, rather
than 404. Reading the status line alone, I reported "nine of nine companies reachable" and
was wrong: most of those 200s were empty. `board_jobs()` treats an empty board as no board
for exactly this reason, and `test_an_empty_smartrecruiters_answer_is_not_a_board` pins it.
Recruitee has the same trap in a different shape: `google.recruitee.com` resolves, and is a
demo account with one "Senior Marketer (Sample)" posting on it.
