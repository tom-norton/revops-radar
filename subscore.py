#!/usr/bin/env python3
"""
Subscription scoring: the scheduled Claude Code session's half of the radar's scoring.

scan.py puts every role that clears its free checks on scoring/queue.json (on main). A
scheduled session on Tom's Claude subscription follows scoring/ROUTINE.md:

    python subscore.py prepare    queue -> one prompt file per question still to ask
    python subscore.py todo       which prompt files to hand to subagents next (JSON)
    (subagents answer the prompt files, writing JSON next to them)
    python subscore.py ingest     answers -> scoring/results.json
    python subscore.py prune      forget answers main has already applied
    python subscore.py status     how much is left

Nothing here talks to a model or the network, and nothing here decides anything. The
prompts are scan.py's own (SCREEN_SYSTEM_SUBSCRIPTION, score_system(), and the user turn
the scan stored on the queue entry), and results.json holds the model's raw JSON. Turning
that into a kill, a drop or a score is `scan.py --apply-scores`, run in GitHub Actions on
every push of the results branch.

Results live on the `claude/scores` branch, never on main: every scan ends with a push to
main, and a commit from here landing there in between would race it.

This replaced the side-by-side trial in shadow.py, which it borrows its plumbing from.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan  # noqa: E402
import shadow  # noqa: E402

WORK_DIR = "scoring/work"
RESULTS_FILE = scan.SCORE_RESULTS_FILE
# An answer main has applied is forgotten after this long. Kept a little while so a scan
# that raced the apply step can still find it when it re-applies.
KEEP_APPLIED_DAYS = 3


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def main_queue():
    return shadow.from_main(scan.SCORE_QUEUE_FILE, {})


def paths(key, work=WORK_DIR):
    return shadow.paths(key, work)


def screen_prompt(entry, answer_path):
    return (f"{shadow.ANSWER_RULE}\n\nANSWER FILE: {answer_path}\n\n"
            f"===== SYSTEM =====\n{scan.SCREEN_SYSTEM_SUBSCRIPTION}\n\n"
            f"===== USER =====\n{entry['message']}\n")


def score_prompt(entry, answer_path, system):
    schema = json.dumps(scan.SCORE_SCHEMA, indent=1)
    return (f"{shadow.ANSWER_RULE}\n\nANSWER FILE: {answer_path}\n\n"
            f"The JSON object must match this JSON schema exactly (every required key "
            f"present, no others):\n{schema}\n\n"
            f"===== SYSTEM =====\n{system}\n\n"
            f"===== USER =====\n{entry['message']}\n")


def pending(queue, results):
    todo = [e for i, e in queue.items() if i not in results]
    todo.sort(key=lambda e: scan.priority(e["job"]), reverse=True)
    return todo


def cmd_prepare(queue=None, results=None, work=WORK_DIR):
    queue = main_queue() if queue is None else queue
    results = shadow.load_json(RESULTS_FILE, {}) if results is None else results
    os.makedirs(work, exist_ok=True)
    todo = pending(queue, results)
    system = scan.score_system() if todo else ""
    manifest = {}
    for e in todo:
        key = shadow.key_for(e["id"])
        p = paths(key, work)
        manifest[key] = e["id"]
        if not e.get("skip_screen"):
            with open(p["screen"], "w", encoding="utf-8") as f:
                f.write(screen_prompt(e, p["screen_answer"]))
        with open(p["score"], "w", encoding="utf-8") as f:
            f.write(score_prompt(e, p["score_answer"], system))
    shadow.save_json(f"{work}/manifest.json", manifest)
    print(f"{len(todo)} role(s) to do, best first. {len(queue)} waiting on main, "
          f"{len(results)} answered.")
    return todo


def _screen_keeps(entry, p):
    """True / False once the screen has answered, None while it has not."""
    if entry.get("skip_screen"):
        return True
    if not os.path.exists(p["screen_answer"]):
        return None
    data = shadow.read_answer(p["screen_answer"])
    keep = bool(data.get("keep", True))
    return keep or bool(scan.overrule_kill(entry["job"].get("title"),
                                           str(data.get("reason", ""))[:80]))


def cmd_todo(queue=None, work=WORK_DIR):
    """Two lists of prompt files: screens to ask, then deep scores for roles the screen
    kept. A score is only asked once its screen has said keep."""
    queue = main_queue() if queue is None else queue
    manifest = shadow.load_json(f"{work}/manifest.json", {})
    results = shadow.load_json(RESULTS_FILE, {})
    screen, score = [], []
    for key, job_id in manifest.items():
        if job_id in results or job_id not in queue:
            continue
        entry, p = queue[job_id], paths(key, work)
        try:
            keeps = _screen_keeps(entry, p)
        except Exception:
            os.remove(p["screen_answer"])      # unreadable: ask again
            keeps = None
        if keeps is None:
            screen.append(p["screen"])
        elif keeps and not os.path.exists(p["score_answer"]):
            score.append(p["score"])
    return screen, score


def cmd_ingest(queue=None, work=WORK_DIR):
    """Every role whose answers are complete -> results.json, as the raw JSON the model
    gave. Each answer is checked with scan.py's own parser first, so one that would not
    apply is deleted and asked again rather than stored."""
    queue = main_queue() if queue is None else queue
    manifest = shadow.load_json(f"{work}/manifest.json", {})
    results = shadow.load_json(RESULTS_FILE, {})
    done = 0
    for key, job_id in manifest.items():
        if job_id in results or job_id not in queue:
            continue
        entry, p = queue[job_id], paths(key, work)
        row = {"at": now_iso(), "kind": entry.get("kind")}
        if not entry.get("skip_screen"):
            if not os.path.exists(p["screen_answer"]):
                continue
            try:
                row["screen"] = shadow.read_answer(p["screen_answer"])
            except Exception as e:
                print(f"  bad screen answer for {job_id} ({e}); will ask again")
                os.remove(p["screen_answer"])
                continue
            out = scan.subscription_outcome(entry, {"screen": row["screen"]})
            if out and out["outcome"] == "killed":
                results[job_id] = row; done += 1
                continue
        if not os.path.exists(p["score_answer"]):
            continue
        try:
            row["score"] = shadow.read_answer(p["score_answer"])
            if scan.subscription_outcome(entry, row) is None:
                raise ValueError("answer incomplete")
        except Exception as e:
            print(f"  bad score answer for {job_id} ({e}); will ask again")
            os.remove(p["score_answer"])
            continue
        results[job_id] = row; done += 1
    shadow.save_json(RESULTS_FILE, results)
    print(f"ingested {done} role(s); {len(results)} answers held")
    return results


def cmd_prune(queue=None, now=None):
    """Forget answers main has applied: no longer on main's queue, and older than
    KEEP_APPLIED_DAYS."""
    queue = main_queue() if queue is None else queue
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=KEEP_APPLIED_DAYS)).isoformat()
    results = shadow.load_json(RESULTS_FILE, {})
    kept = {i: r for i, r in results.items() if i in queue or (r.get("at") or "") >= cutoff}
    shadow.save_json(RESULTS_FILE, kept)
    print(f"pruned {len(results) - len(kept)} applied answer(s); {len(kept)} held")
    return kept


def cmd_status():
    queue = main_queue()
    results = shadow.load_json(RESULTS_FILE, {})
    print(f"{len(queue)} waiting on main, {len(pending(queue, results))} not yet answered")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "prepare":
        cmd_prepare()
    elif cmd == "todo":
        screen, score = cmd_todo()
        print(json.dumps({"screen": screen, "score": score}, indent=1))
    elif cmd == "ingest":
        cmd_ingest()
    elif cmd == "prune":
        cmd_prune()
    elif cmd == "status":
        cmd_status()
    else:
        sys.exit(f"unknown command {cmd!r}: prepare | todo | ingest | prune | status")


if __name__ == "__main__":
    main(sys.argv)
