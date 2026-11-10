#!/usr/bin/env python3
"""
The subscription-scoring trial: score the radar's roles a second time, on Tom's Claude
subscription instead of the API, and compare the two.

The scan (scan.py, with SHADOW_QUEUE=1) writes every role it is about to screen to
shadow/queue.json on main. A scheduled Claude Code session follows shadow/ROUTINE.md:

    python shadow.py prepare      queue -> one prompt file per role still to do
    (subagents answer the prompt files, writing JSON next to them)
    python shadow.py ingest       answers -> shadow/results.json, via scan.py's own parsers
    python shadow.py compare      results vs the API's -> shadow/compare.md
    python shadow.py status       how much is left

Nothing here talks to a model or the network. The prompts are built by scan.py's own
functions (SCREEN_SYSTEM, score_system(), job_message()) and the answers are turned into
scores by scan.py's own parse_score_result() and overrule_kill(), so the only thing that
differs between the two sides is who answered.

Results live on the `claude/shadow-results` branch, never on main: every scan ends with a bare
`git push` to main, and a trial commit landing there in between would make that push fail.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan  # noqa: E402

WORK_DIR = "shadow/work"
RESULTS_FILE = "shadow/results.json"
COMPARE_FILE = "shadow/compare.md"
MAIN_REF = "origin/main"

ANSWER_RULE = (
    "Answer with ONE JSON object and nothing else: no prose, no markdown fence. "
    "Write that JSON object, and only that, to the file named in ANSWER FILE below, "
    "using your file-writing tool. Then reply with the single word: done.")


# ---------------------------------------------------------------- io

def load_json(path, default):
    return scan.load_json(path, default)


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)


def from_main(path, default, ref=MAIN_REF):
    """A file as it stands on main. Falls back to the working tree, so the trial works on
    a checkout of main as well as on the results branch."""
    try:
        out = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True,
                             text=True, check=True).stdout
        return json.loads(out)
    except Exception:
        return load_json(path, default)


def key_for(job_id):
    """A filename-safe key for a job id. Ids are URLs and source slugs, so they carry
    slashes and colons; the hash keeps two ids that sanitise alike apart."""
    job_id = str(job_id)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)[:80]
    return f"{safe}-{hashlib.sha1(job_id.encode()).hexdigest()[:8]}"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- prompts

def screen_prompt(job, answer_path):
    return (f"{ANSWER_RULE}\n\nANSWER FILE: {answer_path}\n\n"
            f"===== SYSTEM =====\n{scan.SCREEN_SYSTEM}\n\n"
            f"===== USER =====\n{scan.job_message(job)}\n")


def score_prompt(job, answer_path, system=None):
    system = system if system is not None else scan.score_system()
    schema = json.dumps(scan.SCORE_SCHEMA, indent=1)
    return (f"{ANSWER_RULE}\n\nANSWER FILE: {answer_path}\n\n"
            f"The JSON object must match this JSON schema exactly (every required key "
            f"present, no others):\n{schema}\n\n"
            f"===== SYSTEM =====\n{system}\n\n"
            f"===== USER =====\n{scan.job_message(job)}\n")


def paths(key, work=WORK_DIR):
    return {"screen": f"{work}/{key}.screen.md", "score": f"{work}/{key}.score.md",
            "screen_answer": f"{work}/{key}.screen.answer.json",
            "score_answer": f"{work}/{key}.score.answer.json"}


# ---------------------------------------------------------------- commands

def pending_jobs(queue, results):
    todo = [j for i, j in queue.items() if i not in results]
    todo.sort(key=lambda j: scan.priority(j), reverse=True)
    return todo


def cmd_prepare(queue=None, results=None, work=WORK_DIR):
    queue = from_main(scan.SHADOW_QUEUE_FILE, {}) if queue is None else queue
    results = load_json(RESULTS_FILE, {}) if results is None else results
    os.makedirs(work, exist_ok=True)
    todo = pending_jobs(queue, results)
    system = scan.score_system() if todo else ""
    manifest = {}
    for j in todo:
        key = key_for(j["id"])
        p = paths(key, work)
        manifest[key] = j["id"]
        with open(p["screen"], "w") as f:
            f.write(screen_prompt(j, p["screen_answer"]))
        with open(p["score"], "w") as f:
            f.write(score_prompt(j, p["score_answer"], system))
    save_json(f"{work}/manifest.json", manifest)
    print(f"{len(todo)} role(s) to do, best first. {len(queue)} in queue, "
          f"{len(results)} already done.")
    return todo


def cmd_todo(work=WORK_DIR):
    """What the routine should hand to subagents next, as two lists of prompt files."""
    manifest = load_json(f"{work}/manifest.json", {})
    results = load_json(RESULTS_FILE, {})
    screen, score = [], []
    for key, job_id in manifest.items():
        if job_id in results:
            continue
        p = paths(key, work)
        if not os.path.exists(p["screen_answer"]):
            screen.append(p["screen"])
        elif not os.path.exists(p["score_answer"]):
            score.append(p["score"])
    return screen, score


def screen_decision(job, data):
    """Mirrors main()'s stage 1: the screen's keep/kill, then overrule_kill()."""
    keep = bool(data.get("keep", True))
    reason = str(data.get("reason", ""))[:80]
    overruled = "" if keep else scan.overrule_kill(job.get("title"), reason)
    return (keep or bool(overruled)), reason, overruled


def read_answer(path):
    with open(path) as f:
        return scan._extract_json(f.read())


def cmd_ingest(queue=None, work=WORK_DIR, screen_model="haiku", score_model="opus"):
    """Every answered role -> results.json. A kept role with no score answer yet is left
    for the next pass; an unreadable answer is deleted so it is asked again."""
    queue = from_main(scan.SHADOW_QUEUE_FILE, {}) if queue is None else queue
    manifest = load_json(f"{work}/manifest.json", {})
    results = load_json(RESULTS_FILE, {})
    done = 0
    for key, job_id in manifest.items():
        if job_id in results or job_id not in queue:
            continue
        job, p = queue[job_id], paths(key, work)
        if not os.path.exists(p["screen_answer"]):
            continue
        try:
            keep, reason, overruled = screen_decision(job, read_answer(p["screen_answer"]))
        except Exception as e:
            print(f"  bad screen answer for {job_id} ({e}); will ask again")
            os.remove(p["screen_answer"])
            continue
        row = {"id": job_id, "title": job.get("title"), "company": job.get("company"),
               "market": job.get("market"), "captured_at": job.get("captured_at"),
               "screen_model": screen_model, "screen_reason": reason}
        if not keep:
            row.update({"outcome": "killed", "at": now_iso()})
            results[job_id] = row; done += 1
            continue
        if overruled:
            row["overruled"] = overruled
        if not os.path.exists(p["score_answer"]):
            continue
        try:
            result = scan.parse_score_result(job, read_answer(p["score_answer"]))
        except Exception as e:
            print(f"  bad score answer for {job_id} ({e}); will ask again")
            os.remove(p["score_answer"])
            continue
        row["score_model"] = score_model
        if result.get("disqualified"):
            row.update({"outcome": "dropped", "stage": result["stage"],
                        "reason": result["reason"]})
        else:
            row.update({"outcome": "scored", "score": result["score"],
                        "dimensions": result["dimensions"], "shot": result["shot"],
                        "want": result["want"], "verdict": result["verdict"],
                        "flags": result["flags"]})
        row["at"] = now_iso()
        results[job_id] = row; done += 1
    save_json(RESULTS_FILE, results)
    print(f"ingested {done} role(s); {len(results)} done in all")
    return results


# ---------------------------------------------------------------- comparison

def api_outcomes(jobs, kills, excluded):
    """{id: (outcome, score)} for what the API side did with each role it handled."""
    out = {}
    for r in (excluded or {}).get("rows", []):
        if r.get("stage") and r.get("id"):
            st = r["stage"]
            out[str(r["id"])] = ("killed" if st == "stage1-kill" else "dropped", None, st)
    for k in kills or []:
        out[str(k.get("id"))] = ("killed", None, "stage1-kill")
    for j in jobs or []:
        if j.get("score") is not None and j.get("evidence") != "thin":
            out[str(j["id"])] = ("scored", float(j["score"]), "")
    return out


def compare(results, api, queue_size=0):
    both = []          # (id, shadow score, api score, row)
    screen = {"both kept": 0, "both killed": 0, "API kept, trial killed": 0,
              "API killed, trial kept": 0}
    unknown = 0
    for i, r in results.items():
        a = api.get(i)
        if not a:
            unknown += 1
            continue
        a_kept = a[0] != "killed"
        s_kept = r["outcome"] != "killed"
        screen[("both kept" if a_kept else "API killed, trial kept") if s_kept else
               ("API kept, trial killed" if a_kept else "both killed")] += 1
        if a[0] == "scored" and r["outcome"] == "scored":
            both.append((i, r["score"], a[1], r))
    diffs = [s - a for _, s, a, _ in both]
    n = len(diffs)
    stats = {"n": n, "unknown": unknown, "screen": screen,
             "mean_diff": round(sum(diffs) / n, 2) if n else None,
             "mean_abs": round(sum(abs(d) for d in diffs) / n, 2) if n else None,
             "within_0_5": sum(1 for d in diffs if abs(d) <= 0.5),
             "within_1": sum(1 for d in diffs if abs(d) <= 1.0),
             "pending": max(0, queue_size - len(results))}
    stats["biggest"] = sorted(both, key=lambda t: abs(t[1] - t[2]), reverse=True)[:10]
    stats["gate_flips"] = [t for t in both if (t[1] >= scan.GATE) != (t[2] >= scan.GATE)]
    stats["notify_flips"] = [t for t in both if (t[1] >= 7.5) != (t[2] >= 7.5)]
    per_day = {}
    for r in results.values():
        d = (r.get("at") or "")[:10]
        c = per_day.setdefault(d, {"screened": 0, "scored": 0})
        c["screened"] += 1
        c["scored"] += 1 if r["outcome"] in ("scored", "dropped") else 0
    stats["per_day"] = dict(sorted(per_day.items()))
    return stats


def render(stats):
    L = ["# Subscription trial: comparison with the API", "",
         f"_Updated {now_iso()[:16].replace('T', ' ')} UTC_", "",
         "## The short version", ""]
    n = stats["n"]
    if n:
        L += [f"- **{n} roles scored by both.** On average the trial scored "
              f"**{stats['mean_diff']:+.2f}** against the API, and was off by "
              f"**{stats['mean_abs']:.2f}** either way.",
              f"- {stats['within_0_5']} of {n} were within half a point; "
              f"{stats['within_1']} of {n} within one point.",
              f"- {len(stats['gate_flips'])} landed on different sides of the "
              f"{scan.GATE} apply line; {len(stats['notify_flips'])} on different sides "
              f"of 7.5."]
    else:
        L += ["- No role has been scored by both sides yet."]
    L += [f"- **{stats['pending']} role(s) still waiting** for the trial to reach them.",
          f"- {stats['unknown']} trial result(s) have no API outcome to compare against "
          f"(the API has not reached them yet, or its record has aged out).", "",
          "## Screen (keep or kill)", "", "| | Roles |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in stats["screen"].items()]

    def table(rows):
        out = ["| Role | Trial | API | Gap |", "|---|---|---|---|"]
        for _, s, a, r in rows:
            out.append(f"| {r.get('title')} @ {r.get('company')} ({r.get('market')}) | "
                       f"{s:.1f} | {a:.1f} | {s - a:+.1f} |")
        return out

    L += ["", "## Biggest disagreements", ""]
    L += table(stats["biggest"]) if stats["biggest"] else ["None yet."]
    L += ["", f"## Different side of the {scan.GATE} apply line", ""]
    L += table(stats["gate_flips"]) if stats["gate_flips"] else ["None."]
    L += ["", "## Different side of 7.5", ""]
    L += table(stats["notify_flips"]) if stats["notify_flips"] else ["None."]
    L += ["", "## Trial work per day (UTC)", "", "| Day | Screened | Deep-scored |",
          "|---|---|---|"]
    L += [f"| {d} | {c['screened']} | {c['scored']} |" for d, c in stats["per_day"].items()]
    return "\n".join(L) + "\n"


def cmd_compare():
    results = load_json(RESULTS_FILE, {})
    queue = from_main(scan.SHADOW_QUEUE_FILE, {})
    api = api_outcomes(from_main("docs/jobs.json", []), from_main(scan.KILLS_FILE, []),
                       from_main("docs/excluded.json", {}))
    stats = compare(results, api, queue_size=len(queue))
    os.makedirs(os.path.dirname(COMPARE_FILE), exist_ok=True)
    with open(COMPARE_FILE, "w") as f:
        f.write(render(stats))
    print(f"wrote {COMPARE_FILE}: {stats['n']} compared, {stats['pending']} pending")
    return stats


def cmd_status():
    queue = from_main(scan.SHADOW_QUEUE_FILE, {})
    results = load_json(RESULTS_FILE, {})
    print(f"{len(queue)} in queue, {len(results)} done, "
          f"{len(pending_jobs(queue, results))} pending")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "prepare":
        cmd_prepare()
    elif cmd == "todo":
        screen, score = cmd_todo()
        print(json.dumps({"screen": screen, "score": score}, indent=1))
    elif cmd == "ingest":
        cmd_ingest()
    elif cmd == "compare":
        cmd_compare()
    elif cmd == "status":
        cmd_status()
    else:
        sys.exit(f"unknown command {cmd!r}: prepare | todo | ingest | compare | status")


if __name__ == "__main__":
    main(sys.argv)
