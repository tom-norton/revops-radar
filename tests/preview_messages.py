#!/usr/bin/env python3
"""Print every message the bot sends, with realistic content, roughly as Telegram renders
it. Prints; asserts nothing.

    python tests/preview_messages.py

Formatting can only be judged by looking at it, and the alternative is judging it in Tom's
chat after it has already annoyed him. Run this after touching any message, and check both
the character count and the widest line: a phone fits about 40 characters, so a 110-column
question is three wrapped lines. test_apply.py enforces the ceilings; this is how you see
whether the result actually reads well.
"""
import re, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import applyq  # noqa: E402

JOB = {"id": "az-nl-88", "title": "Revenue Operations Lead, EMEA",
       "company": "Fonoa", "market": "NL", "score": 7.9,
       "url": "https://boards.greenhouse.io/fonoa/jobs/4210",
       "comp": {"stated": False}}

AUDIT = {"track": "BUILDER",
         "track_rationale": ("Systems ownership and the GTM tool stack lead the "
                             "responsibilities, and this is an early RevOps hire at a "
                             "scaleup, so Builder over Analytics."),
         "gaps": []}

# Gaps written the way the prompt now asks for. The salary question is not hardcoded --
# build_questions() produces it, so the preview reflects the real code.
GAPS_SHORT = [
    {"keyword": "pipeline hygiene", "answered_by": "",
     "question": "Any pipeline hygiene or CRM cleanup at NAVEX?",
     "options": ["Yes, data cleanup", "Yes, forecast accuracy",
                 "Informal, hard to claim", "Nothing here"]},
    {"keyword": "dashboard building", "answered_by": "",
     "question": "Did you build reporting dashboards, or specify them?",
     "options": ["Built them", "Specified them", "Nothing here"]},
]
# What a chatty model produces, to show the clip actually biting.
GAPS_LONG = [
    {"keyword": "pipeline hygiene", "answered_by": "",
     "question": "The posting leans heavily on pipeline hygiene and CRM data quality, so did you do any of that kind of work at NAVEX that isn't already on your CV?",
     "options": ["Yes, I did quite a lot of pipeline data cleanup work",
                 "No meaningful experience"]},
]
QS = applyq.build_questions(dict(AUDIT, gaps=GAPS_SHORT), JOB)

SALARY = {"sources": ["Glassdoor: RevOps Manager Amsterdam, EUR 72-94K base, 41 reports",
                      "levels.fyi: Fonoa does not publish; comparable NL scaleups EUR 80-100K"],
          "low": 78000, "high": 96000, "currency": "EUR",
          "thirty_percent_ruling": "yes", "verdict": "above_floor",
          "notes": "Range is wide because the level is ambiguous; the posting reads Manager-grade."}


def show(label, text):
    plain = applyq.strip_tags(text)
    width = max((len(l) for l in plain.split("\n")), default=0)
    print(f"\n\033[1m--- {label}  ({len(plain)} chars, "
          f"{len(plain.splitlines())} lines, widest {width}) ---\033[0m")
    # Approximate Telegram's rendering so the shape is judged, not the tags.
    r = text
    r = re.sub(r"<b>(.*?)</b>", lambda m: "\033[1m" + m.group(1) + "\033[0m", r, flags=re.S)
    r = re.sub(r"<i>(.*?)</i>", lambda m: "\033[3m" + m.group(1) + "\033[0m", r, flags=re.S)
    r = re.sub(r"<code>(.*?)</code>", lambda m: "\033[7m" + m.group(1) + "\033[0m", r, flags=re.S)
    r = re.sub(r'<a href="[^"]*">(.*?)</a>', lambda m: "\033[4m" + m.group(1) + "\033[0m", r, flags=re.S)
    r = r.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    print(r)


show("THE BIG ONE: questions", applyq.format_questions(QS, JOB, AUDIT))
show("same, if the model ignores the length rule",
     applyq.format_questions(applyq.build_questions(dict(AUDIT, gaps=GAPS_LONG), JOB),
                             JOB, AUDIT))
show("salary result", applyq.format_salary(SALARY))
show("nothing to ask", applyq.job_line(JOB, AUDIT) + "\n\n"
     + "<i>Nothing to ask - 2 already in the answer bank. Finishing it off.</i>")

n_new, n_drop, name, sha = 2, 1, "2026-09-01-fonoa-revenue-operations-lead-emea.md", "a1b2c3d"
tally = [f"{n_new} new bullet" + ("" if n_new == 1 else "s"),
         f"{n_drop} gap" + ("" if n_drop == 1 else "s") + " left open"]
show("done", "\n".join([
    f"✓ <b>{applyq.esc(applyq.clip(JOB['title'], 70))}</b>",
    applyq.esc(" · ".join([JOB["company"], AUDIT["track"]] + tally)),
    "", f"<code>{name}</code>",
    f'<a href="https://{applyq.BANK_REPO}/commit/{sha}">packet</a> · '
    f'<a href="{JOB["url"]}">posting</a>']))

show("still open (nudge)", "<b>Still open</b>\n\n"
     + f"<b>1</b>  {applyq.esc(applyq.clip(QS[2]['question'], 160))}\n\n"
     + "<i>Answer, or say skip and I'll leave it out.</i>")
show("/help", applyq.HELP)
