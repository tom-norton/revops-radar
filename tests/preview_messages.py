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

SUMMARIES = [
    {"label": "A", "angle": "Canonical-tight", "score": 7.5,
     "why": "Keeps the tuned bank summary and swaps in the JD's own vocabulary.",
     "changed": "Swapped 'customer success' for 'revenue operations' in the opener.",
     "text": "Revenue operations and customer success operator with 11 years in B2B SaaS, "
             "most recently managing $5.2M ARR across 22 enterprise accounts. ESADE MBA "
             "finishing 2026, with GTM funnel modelling and CRM architecture work behind "
             "it. Strongest where post-sale data meets forecasting."},
    {"label": "B", "angle": "Role-forward", "score": 8.5,
     "why": "Leads with systems ownership, which is the top third of the posting.",
     "changed": "Resequenced to open on the GTM stack rather than the CS tenure.",
     "text": "Revenue operations builder who has owned the GTM stack end to end, from lead "
             "routing and CRM architecture to renewal forecasting. 11 years in B2B SaaS "
             "and an ESADE MBA finishing 2026. Most recently managed $5.2M ARR at NAVEX "
             "while building the reporting the forecast ran on."},
    {"label": "C", "angle": "Company-forward", "score": 6.5,
     "why": "Ties to the tax-automation expansion, but spends a sentence doing it.",
     "changed": "Opens on Fonoa's move into indirect tax coverage.",
     "text": "Revenue operations professional drawn to Fonoa's expansion across indirect "
             "tax coverage, where GTM systems have to keep pace with new markets. 11 years "
             "in B2B SaaS, ESADE MBA finishing 2026, and $5.2M ARR most recently managed "
             "at NAVEX."},
]

show("THE OTHER BIG ONE: summary pick (score at or above the gate)",
     applyq.format_variations(SUMMARIES, JOB, AUDIT))
show("below the gate: picked for him, no question asked",
     applyq.format_auto_pick(applyq.best_summary(SUMMARIES), SUMMARIES, JOB, AUDIT))
show("packet done, CV starting", "\n".join([
    f"<b>{applyq.esc(applyq.clip(JOB['title'], 70))}</b>",
    applyq.esc(" · ".join([JOB["company"], AUDIT["track"], "2 new bullets",
                           "1 gap left open"])),
    "", "<i>Packet done. Building the CV.</i>"]))
show("CV delivered (this is the caption on the PDF)", "\n".join([
    f"✓ <b>{applyq.esc(applyq.clip('REVENUE OPERATIONS LEAD, EMEA', 70))}</b>",
    applyq.esc(" · ".join([JOB["company"], AUDIT["track"], "2 pages", "14 bullets",
                           "1 rejected", "bank +2"])),
    "<i>Summary B · picked by Tom</i>"]))
show("CV failed its checks", "\n".join(
    ["⚠ <b>" + applyq.esc(applyq.clip(JOB["title"], 70)) + "</b>",
     applyq.esc(f"{JOB['company']} · CV built but did not pass its checks, so I have not "
                f"sent it."), "",
     "• " + applyq.esc("right-aligned tab stop is not holding: 'Dec 2021 - Aug 2025' ends "
                       "at 412pt, margin is 558pt"),
     "", "<i>The PDF and the page images are in the run log and at "
         "<code>cv/2026-09-01-fonoa-revenue-operations-lead-emea.pdf</code>.</i>"]))
show("the skeleton is missing something (said once, not per role)",
     "<b>Two minutes on the CV skeleton</b>\n\n"
     + "\n".join(f"\u2022 {applyq.esc(g)}"
                 for g in applyq.cvbuild.skeleton_gaps(applyq.cvbuild.load_base()[0]))
     + f'\n\n<a href="{applyq.cvbuild.BASE_EDIT_URL}">Open cv-base.json</a>'
     + "\n\n<i>The CV builds either way. The phone number is left out of the public repo "
       "on purpose, so the private copy is where it goes.</i>")

show("CV rebuilt from feedback", "\n".join([
    f"✓ <b>{applyq.esc(applyq.clip('REVENUE OPERATIONS LEAD, EMEA', 70))}</b>",
    applyq.esc(" · ".join([JOB["company"], AUDIT["track"], "2 pages", "13 bullets",
                           "2 cut"])),
    "<i>Rebuilt from your feedback.</i>", "",
    applyq.esc("Cut the LexisNexis training bullet and merged the two QBR bullets into "
               "one. Left the $150K figure alone: you asked to round it up and the bank "
               "has it as roughly $150K, so rounding would be a claim I can't source."),
    "", "<i>Summary was over four lines, so I dropped: Built the Excel tracking model "
        "covering renewal dates, ARR at risk and NRR progress.</i>"]))
show("/redo with nothing after it",
     "<b>Tell me what to change.</b>\n\n"
     "<i>/redo cut the LexisNexis training bullet, it's the weakest</i>\n"
     "<i>/redo lead the summary with forecasting, not the MBA</i>\n"
     "<i>/redo the NAVEX section is too long</i>")

show("/cover, on its way", "\n".join([
    f"<b>Writing the cover letter</b>  {applyq.esc(JOB['company'])}", "",
    f"<i>{applyq.esc('lead on the forecasting rebuild, not the MBA')}</i>"]))
show("cover letter delivered (this is the caption on the PDF)", "\n".join([
    f"✓ <b>Cover letter</b>  {applyq.esc(JOB['company'])}",
    applyq.esc("287 words · one page"), "",
    "<i>" + applyq.esc("Their move into indirect tax coverage across new markets, from "
                       "the June engineering post, which is the closest thing in the brief "
                       "to work he has actually done.") + "</i>", "",
    applyq.esc("Led on the rates pipeline rather than the MBA, and left the 30% ruling out "
               "of it: the posting doesn't raise relocation and guessing at it reads as "
               "asking."),
    "", "<i>The text is in the packet too, for forms that want it pasted in.</i>"]))
show("cover letter stopped by the honesty screen", "\n".join([
    f"⚠ <b>Cover letter not sent</b>  {applyq.esc(JOB['company'])}",
    applyq.esc("the opening paragraph did not survive the honesty screen, and a letter "
               "without its hook is not a letter"), "",
    "<i>Nothing was invented onto a page: it was caught before it printed. Send /cover "
    "again, with an angle, and I'll write it a different way.</i>"]))
show("cover letter that would not fit on a page", "\n".join([
    f"⚠ <b>Cover letter</b>  {applyq.esc(JOB['company'])}",
    applyq.esc("Built but did not pass its checks, so I have not sent it."), "",
    "• " + applyq.esc("2 pages; a cover letter is one"), "",
    "<i>It is at <code>cv/2026-09-01-fonoa-revenue-operations-lead-emea-cover.pdf</code> "
    "in the bank, with its text in the packet. /cover again and I'll write it shorter.</i>"]))

show("still open (nudge)", "<b>Still open</b>\n\n"
     + f"<b>1</b>  {applyq.esc(applyq.clip(QS[2]['question'], 160))}\n\n"
     + "<i>Answer, or say skip and I'll leave it out.</i>")
show("/help", applyq.HELP)
