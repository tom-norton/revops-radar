# Subscription trial routine

The instructions the scheduled trial session follows. The scheduled task's own prompt only
says "follow shadow/ROUTINE.md". Edit this file to change what a run does.

## What it is for

For one week, every role the radar screens is also screened and scored here, on Tom's
Claude subscription instead of the API, using the exact same prompts. The two sets of
results are compared in `shadow/compare.md`. Tom wants to answer two questions with it:
does the scoring fit inside his Pro usage limit, and does it agree with the API closely
enough to switch.

You are the person running the experiment, not the scorer. **Do not score any role
yourself.** Every answer comes from a fresh subagent that sees only that role's prompt
file. That is how a fresh API call works, and it keeps your context from building up.

## Hard rules

- **Never commit or push to `main`.** Each scan ends with a bare `git push` to main, and a
  trial commit landing there first would make that push fail and lose the scan. Everything
  you write goes to `claude/shadow-results`.
- Never edit `scan.py`, `profile.md`, the prompts, or any other file outside `shadow/`.
- **No cap.** Work through every pending role, best first, until none are left or the usage
  limit stops you. Whatever is left waits for the next run. Nothing is ever skipped.
- **Model.** Screens use the `haiku` model and deep scores use the `opus` model with effort
  `medium`. That matches the API side: Haiku for the screen, and Opus 5.5 with
  `SCORE_EFFORT = "medium"` for the deep score. If `opus` is refused or unavailable, **stop.**
  Do not fall back to another model, because a different model's scores would make the
  comparison meaningless. Write what happened in `shadow/runs.md` and finish the run.

## Steps

1. Get the branch:
   ```
   git config user.name "radar-trial"; git config user.email "bot@users.noreply.github.com"
   git fetch origin main
   git fetch origin claude/shadow-results && git checkout -B claude/shadow-results origin/claude/shadow-results \
     || git checkout -B claude/shadow-results origin/main
   git merge --no-edit origin/main
   ```
   The merge is always clean, because main never touches `shadow/results.json`,
   `shadow/compare.md` or `shadow/runs.md`.
2. `python shadow.py prepare`. This writes one screen prompt and one score prompt per pending
   role into `shadow/work/`, which is git-ignored.
3. `python shadow.py todo` prints JSON listing `screen` prompt files and `score` prompt files.
   - For each **screen** file, launch a subagent with model `haiku` and this prompt:
     `Read <file> and do exactly what it says.`
   - Run up to 4 subagents at once. Each one writes its JSON answer to the path the file
     names and replies "done". Do not read the answers yourself.
4. `python shadow.py ingest`. Killed roles are now recorded.
5. `python shadow.py todo` again. For each **score** file, launch a subagent with model
   `opus` and effort `medium`, with the same one-line prompt. Again, up to 4 at once.
6. `python shadow.py ingest`.
7. After every batch of about 8 roles, commit and push, so a run cut off by the usage limit
   keeps what it did:
   ```
   git add shadow/results.json && git commit -qm "trial: progress" ; git push -u origin claude/shadow-results
   ```
8. Repeat steps 3–7 until `todo` returns two empty lists, or a subagent fails because the
   usage limit is reached.
   - On the limit: stop launching subagents, run `python shadow.py ingest`, and go to step 9.
   - If a single subagent errors for any other reason, skip it. Its role stays pending and is
     retried next run.
9. Run `python shadow.py compare`. Then add one line to `shadow/runs.md` with:
   - date and time (UTC)
   - roles screened this run
   - roles deep-scored this run
   - whether it finished, or was stopped by the usage limit or a model refusal
   - roles still pending (from `python shadow.py status`)

   Then:
   ```
   git add shadow/results.json shadow/compare.md shadow/runs.md
   git commit -m "trial: $(date -u +%F)"; git push -u origin claude/shadow-results
   ```
   If the push fails because the branch moved, run `git pull --rebase origin claude/shadow-results`
   and push again.
10. Finish with a three-line summary: roles done this run, roles still pending, and the
    headline numbers from `compare.md`.
