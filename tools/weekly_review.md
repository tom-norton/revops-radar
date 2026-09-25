# Weekly review

The instructions the Monday routine follows. Edit this file to change what the review
looks at; the routine itself only says "follow tools/weekly_review.md".

## What it is for

The radar judges one role at a time and never looks back. This review is the look back:
once a week, read what the radar found, what it threw away, and what Tom did with it, and
tell him what that says about the targeting. It is for Tom, not for the code. Write it the
way a sharp colleague would brief him over coffee.

It **reads and reports**. It never edits `scan.py`, `profile.md`, `companies.json`, the
prompts, or any other file that changes what the radar does, and it never writes to
Firebase. When a change looks worth making, it says what the change is and why, in the
report, and Tom decides.

## Steps

1. `git pull` so the data is the latest scan's.
2. `pip install requests` if needed, then `python tools/review_data.py > /tmp/review.json`.
   Every count in the report comes from this file. Do not recount by hand.
3. Read `/tmp/review.json`. Open `docs/jobs.json` or `docs/excluded.json` only to look at
   specific rows the numbers point to; both are large, so query them with a short Python
   snippet rather than reading them whole.
4. Read last week's report in `docs/review/` if there is one, so you can say whether last
   week's suggestions changed anything.
5. Write the report to `docs/review/YYYY-MM-DD.md` (today's date).
6. Commit only that file, on `main`, with the message `review: YYYY-MM-DD`. Scans push to
   `main` often: `git pull --rebase` before pushing, and retry the push if it is rejected.
7. Send Tom a push notification:
   `curl -s -H "Title: RevOps Radar weekly review" -H "Tags: bar_chart" -H "Click: <link>"
   -d "<3 short lines: the headline finding and the one action>" https://ntfy.sh/<topic>`
   where the topic is `NTFY_TOPIC` in `scan.py` and the link is
   `https://github.com/tom-norton/revops-radar/blob/main/docs/review/YYYY-MM-DD.md`.

## The report

Short. A page, not a dossier. Lead with what changed and what to do about it. Skip any
section with nothing worth saying and say so in one line.

1. **Headline**: two or three sentences. The one thing he should know this week, and the
   one thing to do.
2. **This week against the last four**: roles scored, strong matches (7.5+), average
   score, and the market and source mix, each against the baseline weekly average
   (baseline counts are four-week totals; divide by four). If a number moved, give the
   most likely reason, checked against the data, and say how sure you are.
3. **Where you and the scores disagree**: the calibration cases.
   - Roles he hid that scored 7.5+, and roles he applied to that scored under 6.5. Look
     for what they have in common (market, track, title wording, company type, a
     dimension) and say what the scorer is getting wrong.
   - Strong roles still untouched: list them. He may have missed them.
4. **What your hide notes say**: tally the reasons, and what they point at. Map each
   pattern to the specific thing that would fix it: a title filter in `scan.py`, the
   market gate, a line in `profile.md`, the Haiku screen prompt, a source to add or drop.
   Quote his own notes where they help. If there are few notes, say so and move on.
5. **Suspect rejections**: from the stage-1 kills, pick at most five that look like roles
   he would want, with the screen's reason and why it looks wrong. He can restore them
   with `python scan.py --unkill`. If the same kind of mistake repeats, say what in the
   screen prompt causes it.
6. **Recurring gaps**: the hard gaps that keep appearing, and what would close the most
   common one: a certification, a CV bullet from work he has done, a small project.
7. **Suggested changes**: at most three, each with the file, the change, the evidence,
   and what it would cost or risk. Nothing here is applied.

## Tone

Plain English. Numbers where they help, never a wall of them. No hedging filler, no
"great week!". If the data is too thin to support a conclusion, say that rather than
reaching.
