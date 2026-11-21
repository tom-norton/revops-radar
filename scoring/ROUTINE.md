# Subscription scoring routine

The instructions the scheduled scoring session follows. The scheduled task's own prompt
only says "follow scoring/ROUTINE.md". Edit this file to change what a run does.

## What it is for

The radar's scans no longer call the Claude API. Every role that clears the scan's free
checks waits on `scoring/queue.json` on main, and this session answers it on Tom's Claude
subscription: a quick screen (keep or kill), then a deep score for the roles it keeps.
Your answers are pushed to the `claude/scores` branch, and a GitHub workflow puts them on
the dashboard within a few minutes of each push. This replaced an 11-day trial
(`shadow/`), in which these same prompts agreed with the API to within 0.24 of a point.

You are the person running the queue, not the scorer. **Do not score any role yourself.**
Every answer comes from a fresh subagent that sees only that role's prompt file. That is
how a fresh API call works, and it keeps your context from building up.

## Hard rules

- **Never commit or push to `main`.** Everything you write goes to `claude/scores`.
- Never edit `scan.py`, `profile.md`, the prompts, or any file outside `scoring/`.
- The dashboard step (8) writes only to the dashboard page's storage, Firebase and Job
  Search Tracker, and only through `tools/dashboard_sync.py` and the plan it writes.
- **No cap.** Work through every waiting role, best first, until none are left or the
  usage limit stops you. Whatever is left waits for the next run. Nothing is skipped.
- **Model.** Screens use the `haiku` model and deep scores use the `opus` model with
  effort `medium`. If `opus` is refused or unavailable, **stop.** Do not fall back to
  another model. Write what happened in `scoring/runs.md` and finish the run.

## Steps

1. Get the branch:
   ```
   git config user.name "radar-scorer"; git config user.email "bot@users.noreply.github.com"
   git fetch origin main
   git fetch origin claude/scores && git checkout -B claude/scores origin/claude/scores \
     || git checkout -B claude/scores origin/main
   git merge --no-edit origin/main
   ```
   The merge is always clean, because main never touches `scoring/results.json` or
   `scoring/runs.md`.
2. `python subscore.py prepare`. This writes the prompt files into `scoring/work/`, which
   is git-ignored.
3. `python subscore.py todo` prints JSON listing `screen` prompt files and `score` prompt
   files.
   - For each **screen** file, launch a subagent with model `haiku` and this prompt:
     `Read <file> and do exactly what it says.`
   - For each **score** file, launch a subagent with model `opus` and effort `medium`,
     with the same one-line prompt.
   - Run up to 4 subagents at once. Each one writes its JSON answer to the path the file
     names and replies "done". Do not read the answers yourself.
4. `python subscore.py ingest`.
5. After every batch of about 8 roles, commit and push, so the dashboard fills as you go
   and a run cut off by the usage limit keeps what it did:
   ```
   git add scoring/results.json && git commit -qm "scores: progress" ; git push -u origin claude/scores
   ```
6. Repeat steps 3–5 until `todo` returns two empty lists, or a subagent fails because the
   usage limit is reached. (A role the screen keeps shows up under `score` on the next
   `todo`.)
   - On the limit: stop launching subagents, run `python subscore.py ingest`, and go to
     step 7.
   - If a single subagent errors for any other reason, skip it. Its role stays waiting and
     is retried next run.
7. `python subscore.py prune`, then add one line to `scoring/runs.md` with:
   - date and time (UTC)
   - roles screened and roles deep-scored this run
   - whether it finished, or was stopped by the usage limit or a model refusal
   - roles still waiting (from `python subscore.py status`)

   Then:
   ```
   git add scoring/results.json scoring/runs.md
   git commit -m "scores: $(date -u +%F)"; git push -u origin claude/scores
   ```
   If the push fails because the branch moved, run `git pull --rebase origin claude/scores`
   and push again.
8. **Update the dashboard.** Do this on every run, even one with nothing to score or one
   the usage limit cut short. The dashboard is a private Claude page,
   https://claude.ai/artifact/5bdcqQY39voaY3CZWt7TFr. It cannot reach any other website,
   so this step carries everything in and out of it. `tools/dashboard_sync.py` does the
   work; you make the `ArtifactData` calls, because only you can. Use the page's URL above
   in every call. **Never publish the page itself** (no `Artifact` publish): you only write
   its data.

   a. Save what the page holds, using absolute paths. Here `$W` is
      `$(pwd)/scoring/work/dash`; start with `rm -rf scoring/work/dash`.
      - `ArtifactData` `get`, collection `board`, doc_id `meta`, out_dir `$W/db`. Note
        the version the result prints. If the document does not exist, the version is 0.
      - `ArtifactData` `get`, collection `state`, doc_id `marks`, out_dir `$W/db`.
      - `ArtifactData` `list`, collection `outbox`, query `{"limit": 1000}`, out_dir
        `$W/db`. It is often empty.
   b. Run `python tools/dashboard_sync.py plan --work scoring/work/dash --meta-version <N>`,
      with N from step a. The script:
      - waits, up to 15 minutes, until main has applied this run's answers;
      - posts the queued Mark applied rows to Job Search Tracker;
      - copies the marks to Firebase;
      - reads the tracker's contacts and outreach list;
      - writes the new board copy and `scoring/work/dash/plan.json`.
      It needs `TRACKER_TOKEN` from the environment. Never print the token.
   c. Make every batch in `plan.json`, in order: one `ArtifactData` `batch` call per entry
      of `batches`, with that entry as `writes`, exactly as written. Don't open the piece
      files; the batches point at them by path. If a batch is refused, stop this step and
      report it. The page keeps showing the previous copy, and the next run rebuilds
      everything.
9. Finish with a four-line summary:
   - roles done this run
   - roles still waiting
   - the dashboard step: done, or where it stopped
   - anything that went wrong, including every line `dashboard_sync.py` printed as
     WARNING or "not added"
