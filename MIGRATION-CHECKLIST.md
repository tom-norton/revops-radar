# Subscription trial: your checklist

This is for you, Tom. It lists everything you do by hand. Claude handles the rest and
updates this file as things change.

**The plan in one paragraph.** For one week, the radar runs exactly as it does now, API and
all. Alongside it, a scheduled Claude session on your Pro subscription scores the same roles
with the same prompts. At the end of the week you get a side-by-side comparison of the two,
plus your own notes on how much of your weekly Pro limit it used. Then you decide.

---

## Before the trial (about 5 minutes)

### ☑ 1. Merge the pull request
This turns on one small addition: each scan now also saves the roles it's about to score,
for the trial to pick up. Nothing else about the radar changes.

On your phone (GitHub app) or PC (github.com):
1. Open the **tom-norton/revops-radar** repository.
2. Tap **Pull requests**, then open the one called **"Subscription-scoring trial…"**.
3. Scroll to the bottom and tap **Merge pull request**, then **Confirm merge**.
4. If it asks you to delete the branch, tap **Delete branch**. That's safe.

### ☑ 2. Tell Claude it's merged
✅ Done 10 Nov. (You told Claude "merged".) Claude has:
- created the scheduled tasks: **8:30am and 8:30pm on weekdays, 9:30am on weekends**
  (Eastern). The 8:30am run picks up the 8am scan. The evening scan sometimes isn't finished
  by 8:30pm; when that happens, its roles are scored the next morning. Nothing is missed.
- run a test right away to check the plumbing. The first real scoring run, at 8:30am on
  10 Nov, shows whether your Pro plan can use the Opus model for scoring. If it can't, the
  trial stops and says so rather than quietly using a different model.

### ☐ 3. Check the first run worked
Claude will tell you, but if you want to see it yourself:
- In the Claude app, open **Code**. The trial runs show up as sessions. Open the newest one
  and scroll to the bottom for its three-line summary.

---

## Every day of the trial (30 seconds)

### ☐ 4. Write down your Pro usage
On claude.ai (phone browser or PC): **Settings → Usage**. Note the weekly percentage.
Also note any time you couldn't use Claude for an application because you hit the limit.
That's the most important thing to catch.

| Day | Date | Weekly usage % | Hit the limit? Notes |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |
| 6 | | | |
| 7 | | | |

Fill it in wherever is easiest, like a notes app. You don't need to edit this file. Bring
the numbers along on day 7.

Keep using Claude for applications exactly as you do now. The trial changes nothing about
Copy for Claude or your RevOps Career Pivot project.

---

## Day 7: decide

### ☐ 5. Look at the results
Ask Claude: **"summarise the trial"**. It reads `shadow/compare.md` and `shadow/runs.md`
on the `claude/shadow-results` branch and tells you:
- how closely the two sets of scores agreed
- any role one side would have shown you that the other wouldn't
- whether every role got scored, or a backlog built up
- your usage numbers next to how much scoring it did each day

### ☐ 6. Pick one
- **Switch over.** Claude writes the next plan: API scoring off (no more API bill), the
  dashboard moved into a private Claude page, and then the repo made private. More steps
  for you will be added here.
- **Not worth it.** Tell Claude to stop the trial (see below). Everything stays as it is
  today.
- **Not sure yet.** Let it run another week. It costs one more week of API, as now.

---

## Stopping, or undoing it

- **Stop the trial:** ask Claude to "delete the trial's scheduled task". You can also delete
  it yourself wherever your Claude scheduled tasks are listed (claude.ai/code).
- **Undo it completely:** on GitHub, open the merged pull request and tap **Revert**, then
  merge the pull request that creates. The radar goes back to exactly how it was before.
  The trial's results stay on their own branch, which you can ignore or delete.

---

## Things already worked out (no action needed)

- **Hide/Applied sync and the Google Sheet, if the dashboard moves into Claude later.** A Claude
  page can't talk to other websites. That rules out Firebase and your Sheet's web app, so
  the move would need two changes:
  - **Hide/Applied:** would use the page's built-in storage instead of Firebase. It still syncs
    across your phone and PC.
  - **Mark applied:** would reach the Sheet on the next scoring run rather than instantly.

  None of this affects the trial.
- **Making the repo private later.** Private repos get 2,000 free GitHub Actions minutes a
  month. The scans use about 1,400 today, and less once scoring moves out of them, so it
  should fit.
- **The weekly review** keeps working unchanged.
