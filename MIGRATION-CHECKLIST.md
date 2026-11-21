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
little later than before. The scan finds them at 8am, the scoring session runs at 9:30am,
and they appear a few minutes after it finishes. Strong-match phone alerts arrive then too.
Scoring runs at 9:30am and 9:30pm on weekdays and 10:30am at weekends (Eastern). Each run
is an hour after the scan, so the scan has always finished first. On 21 Nov you moved the
runs an hour later than the trial's times, after a scan finished after its scoring run.

### ☑ 1. Merge the pull request (done 21 Nov)
On your phone (GitHub app) or PC (github.com):
1. Open the **tom-norton/revops-radar** repository.
2. Tap **Pull requests**, then open the one called **"Score roles on the Claude subscription"**.
3. Scroll to the bottom and tap **Merge pull request**, then **Confirm merge**.
4. If it offers to delete the branch, tap **Delete branch**. That's safe.

### ☑ 2. Tell Claude it's merged (done 21 Nov)
**First real run, 21 Nov:** the Saturday scan queued 22 roles. The scoring session
screened all 22, dropped 2, and deep-scored 20 in about 5 minutes, using Haiku for the
screen and Opus for the score. 17 new roles reached the dashboard, and 3 were set aside
for hard disqualifiers. Nothing went to the API.

Go back to the Claude Code session where this was built and say **"merged"**. Claude will:
- point the two scheduled tasks at the new instructions and rename them from "trial" to
  "RevOps Radar scoring".
- run the weekday task once straight away and check that the new roles reached the
  dashboard.

### ☐ 3. Keep an eye on it for a few days
Nothing to do unless something looks off. Signs that something is wrong:
- no new roles on the dashboard for a whole day
- a phone alert you'd expect but don't get

If you see either, tell Claude.

---

## Stage 2: the dashboard becomes a private Claude page

**What changes for you:** the dashboard lives at a new address, a private page only you
can open: **https://claude.ai/artifact/5bdcqQY39voaY3CZWt7TFr**. It looks and works like
the old one, with three differences:
- New roles appear after each scoring run (9:30am / 9:30pm weekdays, 10:30am weekends),
  not after each scan.
- **Mark applied** still adds the role to Job Search Tracker, but on the next scoring run
  rather than instantly. The message at the bottom of the screen says "Queued".
- Referral and outreach tags come from the tracker as it stood at the last scoring run.

Your Hide and Applied history (548 hidden, 113 applied, 129 reasons) is already copied
over.

### ☐ 1. Open the new page on each device and check it
1. **PC:** open the link above in your browser. Sign in to Claude if it asks.
2. **Phone browser:** open the same link. Sign in to claude.ai if it asks.
3. **Claude phone app:** open the link from your phone (paste it into a note and tap it, or
   send it to yourself). It should open in the app.
4. On each one, check you see roles, and that the bottom of the list says "Board copied to
   this page" with today's date.
5. **Sync test:** on your phone, tap **Hide** on any role, then **Skip**. Within a few
   seconds it should move to "Hidden" on your PC too. Then tap **Restore** on the PC.

Tell Claude what you saw, and anything that looks wrong.

### ☐ 2. Merge the pull request
Same as stage 1: open the pull request called **"Move the dashboard into a private Claude
page"** and tap **Merge pull request**, then **Confirm merge**. After this the old
dashboard turns read-only, with a link to the new page.

### ☐ 3. Tell Claude it's merged
Claude copies over any marks you made on the old page in the meantime, runs one scoring
session straight away, and checks that the new page updated and that a Mark applied
reached the tracker.

### ☐ 4. Swap your bookmarks
Replace any bookmark or home-screen icon for the old dashboard with the new link.

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
