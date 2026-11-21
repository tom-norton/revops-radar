# Switching the radar to your Claude subscription: your checklist

This is for you, Tom. It lists everything you do by hand. Claude handles the rest and
updates this file as things change.

**Where things stand.** The trial is finished. It ran from 10 to 21 Nov and scored 307
roles both ways. On average the two scores were less than a quarter of a point apart, no
role crossed your 7.5 line, and you never hit your Pro limit. You chose to switch over and
to do all of it now. There are three stages:

1. **Scoring moves to your subscription.** No more API bill.
2. **The dashboard becomes a private Claude page.**
3. **The repo is made private, and the old API key is deleted.**

---

## Stage 1: scoring moves to your subscription

**What changes for you:** almost nothing. New roles still show up on the dashboard, a
little later than before. The scan finds them at 8am, the scoring session runs at 8:30am,
and they appear a few minutes after it finishes. Strong-match phone alerts arrive then too.

### ☐ 1. Merge the pull request
On your phone (GitHub app) or PC (github.com):
1. Open the **tom-norton/revops-radar** repository.
2. Tap **Pull requests**, then open the one called **"Score roles on the Claude subscription"**.
3. Scroll to the bottom and tap **Merge pull request**, then **Confirm merge**.
4. If it offers to delete the branch, tap **Delete branch**. That's safe.

### ☐ 2. Tell Claude it's merged
Go back to the Claude Code session where this was built and say **"merged"**. Claude will:
- point the two scheduled tasks at the new instructions and rename them from "trial" to
  "RevOps Radar scoring". The times stay the same: 8:30am and 8:30pm on weekdays, 9:30am
  at weekends (Eastern).
- run the weekday task once straight away and check that the new roles reached the
  dashboard.

### ☐ 3. Keep an eye on it for a few days
Nothing to do unless something looks off. Signs that something is wrong:
- no new roles on the dashboard for a whole day
- a phone alert you'd expect but don't get

If you see either, tell Claude.

---

## Stage 2: the dashboard becomes a private Claude page

Steps will be added here once stage 1 is running. You'll be asked to open the new page on
your phone browser, in the Claude app and on your PC, and to say whether anything looks
wrong. The old dashboard stays up until you're happy.

---

## Stage 3: lock it down

Detailed steps will be added here. In outline:
- **Make the repo private** on GitHub. This comes after stage 2, because a private repo on
  GitHub's free plan can't host the old dashboard website.
- **Turn off the old dashboard website** (a GitHub setting called Pages).
- **A few days after everything has run cleanly:** delete the API key on the Anthropic
  Console, turn off any auto top-up, and delete `ANTHROPIC_API_KEY` from the repo's GitHub
  secrets.

---

## Undoing stage 1

On GitHub, open the merged pull request and tap **Revert**, then merge the pull request
that creates. Then ask Claude to put the API key back on the scan. Until stage 3, the key
is still there, so this works at any time.

---

## Things already worked out (no action needed)

- **Hide/Applied and the Google Sheet, once the dashboard moves into Claude.** A Claude page
  can't talk to other websites. So:
  - **Hide/Applied:** moves from Firebase into the page's own storage. Your history is
    copied over, and it still syncs across your phone and PC.
  - **Mark applied:** reaches the Sheet on the next scoring run, not instantly.
  - **Contacts and outreach lists:** copied from the Sheet into the page on every scoring
    run, so edits you make in the Sheet show up within about 12 hours.
- **Making the repo private.** Private repos get 2,000 free GitHub Actions minutes a month.
  The scans use about 1,400 today, and fewer once they stop scoring.
- **Your skills here on your PC** (job application workflow, score this role) keep working
  with a private repo, because this computer already signs in to GitHub.
- **The weekly review and the outreach-signals refresh** currently download the repo
  without signing in. Claude will fix that in stage 3, before the repo goes private.

---

## The trial, for the record

10–21 Nov 2026, twice a day, no backlog, no usage limit hit. 307 roles scored both ways:
the subscription scored +0.04 on average against the API and was 0.24 away either way; 306
of 307 were within a point. 27 roles landed on different sides of the 6.5 apply line, all
but a few within half a point, and you accepted that. The full comparison is
`shadow/compare.md` on the `claude/shadow-results` branch.
