# Weekly review

The instructions the Monday routine follows. Edit this file to change what the review
looks at; the routine itself only says "follow tools/weekly_review.md".

## What it is for

The radar judges one role at a time and never looks back. This review is the look back:
once a week, read what the radar found, what it threw away, what Tom applied to and what
came of it, and tell him what that says about the targeting and about his search. It is
for Tom, not for the code. Write it the way a sharp colleague would brief him over coffee:
half radar tuning, half job-search coaching.

It **reads and reports**. It never edits `scan.py`, `profile.md`, `companies.json`, the
prompts, or any other file that changes what the radar does, and it never writes to
Firebase. When a change looks worth making, it says what the change is and why, in the
report, and Tom decides.

## Steps

1. `git pull` so the data is the latest scan's.
2. Export Tom's application tracker. With the Google Drive connector, find the
   spreadsheet titled "Job Search Tracker", download it with export type
   `text/csv` (that exports the first tab, Applications), base64-decode the content and
   save it to `/tmp/tracker.csv`. Use the download, not the read tool: the read tool only
   returns a sample of rows. If the connector is missing or the export fails, carry on
   without it and say so in the report's applications section.
3. `pip install requests` if needed, then
   `python tools/review_data.py --tracker /tmp/tracker.csv > /tmp/review.json`
   (drop `--tracker` if step 2 failed). Every count in the report comes from this file.
   Do not recount by hand.
4. Read `/tmp/review.json`. Open `docs/jobs.json` or `docs/excluded.json` only to look at
   specific rows the numbers point to; both are large, so query them with a short Python
   snippet rather than reading them whole.
5. Read `docs/review/notes.md`, Tom's replies to earlier reviews, before anything else
   in `docs/review/`. Everything in it is true and in force unless the note says it has
   expired. It overrides your own reading of the data: a role he says he's decided not
   to apply to is a decision, not a missed role, so don't list it as untouched or nudge
   him toward it. Where a note asks you to watch for something (a company, a kind of
   role), check for it every week and report it even when nothing turns up. Never edit
   `notes.md`. It's his file. If a note looks out of date (a role closed, a date passed),
   say so in one line and leave it to him.
6. Read last week's report in `docs/review/` if there is one, so you can say whether last
   week's suggestions changed anything.
7. Write the report to `docs/review/YYYY-MM-DD.md` (today's date).
8. Commit only that file, on `main`, with the message `review: YYYY-MM-DD`. Scans push to
   `main` often: `git pull --rebase` before pushing, and retry the push if it is rejected.
9. Send Tom a push notification:
   `curl -s -H "Title: RevOps Radar weekly review" -H "Tags: bar_chart" -H "Click: <link>"
   -d "<3 short lines: the headline finding and the one action>" https://ntfy.sh/<topic>`
   where the topic is `NTFY_TOPIC` in `scan.py` and the link is
   `https://github.com/tom-norton/revops-radar/blob/main/docs/review/YYYY-MM-DD.md`.

## The report

Short. A page and a half, not a dossier. Lead with what changed and what to do about it. Skip any
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
   - Strong roles still untouched: list them. He may have missed them. Leave out any
     role `notes.md` covers.
4. **Your applications**: the coaching section, from `applications` (the tracker) and
   `tom.applied_rows_this_window` (the dashboard). This is the section that should get
   sharper every week as outcomes accumulate.
   - What he applied to this week: count, market and track mix, score range, and whether
     it matches where interviews have actually come from. Name the pattern, not the list.
   - Outcomes to date, all time: interviews, rejections, no-responses, pending. The
     interview rate by market, track, score band and referral vs cold. Say plainly when a
     group is too small to mean anything (under ~10 decided applications is anecdote).
   - Where the radar score and the outcomes disagree: if high-scoring applications do no
     better than mid ones, say so, and look at what the interviews had in common instead.
   - The gap that keeps costing him: `hard_gaps_on_rejected` plus the rows' `radar`
     detail. One concrete thing to do about it (a bullet from real work, a certification,
     a referral route).
   - `stale_in_sheet`: rows still marked Applied after three weeks. They are already
     counted as no response here; list them in one line so he can update the sheet.
   - One coaching suggestion for the week ahead, grounded in the numbers above (e.g. more
     referrals at a market that rejects cold applications fast, fewer applications in a
     band that has never converted). Encouraging is fine; flattering is not.
   - This repo is public. Give outcomes as counts and rates, and name a company only for
     an interview or a pending application. Never list which companies rejected him or
     ignored him; the stale-row line gives company and date only, not an outcome.
5. **What your hide notes say**: tally the reasons, and what they point at. Map each
   pattern to the specific thing that would fix it: a title filter in `scan.py`, the
   market gate, a line in `profile.md`, the Haiku screen prompt, a source to add or drop.
   Quote his own notes where they help. If there are few notes, say so and move on.
6. **Suspect rejections**: from the stage-1 kills, pick at most five that look like roles
   he would want, with the screen's reason and why it looks wrong. He can restore them
   with `python scan.py --unkill`. If the same kind of mistake repeats, say what in the
   screen prompt causes it.
7. **Recurring gaps**: the hard gaps that keep appearing, and what would close the most
   common one: a certification, a CV bullet from work he has done, a small project.
8. **Suggested changes**: at most three, each with the file, the change, the evidence,
   and what it would cost or risk. Nothing here is applied. Don't re-suggest a change
   `notes.md` says he declined, unless the evidence has changed. If it has, say what
   changed.
9. **From your notes**: one line per note in `notes.md` that bears on this week: what
   you did with it, and anything you were asked to watch for. Skip this section if
   there are no notes.

Feed health: `docs/status.json` carries `warnings` (feeds that failed or came back far
smaller than usual on the last run) and `raw_history` (each feed's raw count over recent
runs). Check them before you explain a drop in a market's volume. Re-posts of roles he
already applied to or hid are dropped at the stage named `repost` in the excluded log.

## Tone

Plain English. Numbers where they help, never a wall of them. No hedging filler, no
"great week!". If the data is too thin to support a conclusion, say that rather than
reaching.
