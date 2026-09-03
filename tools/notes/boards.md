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
