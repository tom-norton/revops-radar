# Ashby's application form, as it actually is

Dumped from a real posting (Vanta, `jobs.ashbyhq.com/<slug>/<id>/application`) with
`tools/probe_form.py`, on 2 Sep 2026. Kept because the field map is the expensive half of
writing a driver and it does not change often. Nothing here is private: it is the public
structure of a public form.

## Why there is no Ashby driver yet

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
