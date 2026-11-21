#!/usr/bin/env python3
"""Keep the dashboard (a private Claude page) and its outside world in step.

The dashboard moved off GitHub Pages in Nov 2026 to a private Claude page,
dashboard/index.html. A Claude page cannot reach other websites, so everything that used
to travel between the page and the outside now goes through this script, run by the
scoring session (scoring/ROUTINE.md, "Update the dashboard") after every scoring run:

  - the board: docs/jobs.json, status.json and excluded.json as they stand on main once
    the run's scores are applied, plus the tracker's contacts and outreach list, gzipped
    into one bundle and cut into pieces small enough for the page's storage (256 KiB a
    document). The page reads board/meta to find the current copy.
  - the marks: Hide / Mark applied live in the page's storage now (state/marks). They are
    copied to Firebase as they stand, because scan.py (dealt.json, the re-post memory) and
    tools/review_data.py still read them there.
  - Job Search Tracker: a Mark applied queues its sheet row in outbox/. This posts each
    one to tools/tracker-append.gs with TRACKER_TOKEN (an environment variable of the
    scoring environment) and lists the ones that landed, for deletion.

This script makes no Claude tool calls; only the session can read and write the page's
storage. So it works on files the session saved with ArtifactData (out_dir), and writes a
plan of ArtifactData batch calls for the session to make, in order, verbatim.

    python tools/dashboard_sync.py plan --work scoring/work/dash \\
        [--meta-version N] [--no-wait] [--offline]

Expects, under --work, what the session saved with ArtifactData out_dir:
    db/board/meta.json        (absent on the very first run)
    db/state/marks.json       (absent until the marks have been migrated)
    db/outbox/*.json          (one per queued sheet row; absent when there are none)
Writes --work/out/ (the pieces) and --work/plan.json, and prints what it did.

Every board piece and every outbox document is written exactly once and never changed,
so its version is always 1 and the plan can pin its deletion without a read. board/meta is
the only document rewritten, hence --meta-version.
"""

import argparse
import base64
import glob
import gzip
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

# The dashboard, for the scoring session. A private page: only Tom can open it.
DASHBOARD_URL = "https://claude.ai/artifact/5bdcqQY39voaY3CZWt7TFr"

FIREBASE_STATE_URL = ("https://revops-radar-2822a-default-rtdb.europe-west1."
                      "firebasedatabase.app/revops-radar-state.json")
TRACKER_URL = ("https://script.google.com/macros/s/AKfycbzzhUGnXD92Gs_LB9h3qf360-cHnJyKPx"
               "NXHVCjYb00qh2TxByYrGMh5HdHqsaMzasn/exec")
MARK_KEYS = ("hidden", "applied", "hidden_at", "applied_at", "hide_notes")

# Under the page storage's 256 KiB a document, with room for the JSON around it. Four
# pieces to a batch call keeps each under the tool's 1 MiB.
PIECE_CHARS = 200_000
PIECES_PER_BATCH = 4
DELETES_PER_BATCH = 50
WAIT_SECONDS = 15 * 60


def log(msg):
    print(msg, flush=True)


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8")


def fetch_refs():
    git("fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main",
        "+refs/heads/claude/scores:refs/remotes/origin/claude/scores")


def show(ref_path, default):
    r = git("show", ref_path)
    if r.returncode != 0:
        return default
    try:
        return json.loads(r.stdout)
    except ValueError:
        return default


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- waiting for the apply

def unapplied():
    """Roles answered on claude/scores that main has not applied yet."""
    import scan
    queue = show("origin/main:" + scan.SCORE_QUEUE_FILE, {})
    results = show("origin/claude/scores:" + scan.SCORE_RESULTS_FILE, {})
    return [k for k, e in queue.items()
            if k in results and scan.subscription_outcome(e, results[k]) is not None]


def wait_for_apply(timeout=WAIT_SECONDS):
    """apply-scores.yml puts the session's answers on main a minute or two after each push.
    The board copy must be taken after that, or the page shows this run's roles one run
    late. Gives up after `timeout` and copies what main has: late is better than never."""
    start = time.time()
    while True:
        fetch_refs()
        left = unapplied()
        if not left:
            log("main has applied every answer")
            return True
        if time.time() - start > timeout:
            log(f"WARNING: {len(left)} answers still not applied on main after "
                f"{timeout // 60} min; copying the board as it stands")
            return False
        time.sleep(30)


# ---------------------------------------------------------------- the outside world

def post_outbox(outbox, token, post=None):
    """Each queued row to the tracker script. Returns (done doc ids, problems)."""
    import requests
    post = post or requests.post
    done, problems = [], []
    for doc_id, doc in sorted(outbox.items()):
        row = (doc or {}).get("row") or {}
        if not row.get("company") and not row.get("title"):
            done.append(doc_id)              # nothing in it worth a row; clear it
            continue
        try:
            r = post(TRACKER_URL, data=json.dumps(dict(row, token=token)),
                     headers={"Content-Type": "text/plain;charset=utf-8"}, timeout=60)
            res = r.json()
        except Exception as e:
            # No readable reply is not proof nothing was written (the script can fail after
            # the row is in). Leave it queued: a second post of the same link is skipped by
            # the script, so retrying costs nothing.
            problems.append(f"{row.get('company')} / {row.get('title')}: no answer ({str(e)[:80]})")
            continue
        if res.get("ok"):
            done.append(doc_id)
        else:
            problems.append(f"{row.get('company')} / {row.get('title')}: {res.get('error')}")
    return done, problems


def patch_firebase(marks, patch=None):
    import requests
    patch = patch or requests.patch
    body = {k: marks.get(k) or [] for k in MARK_KEYS}
    r = patch(FIREBASE_STATE_URL, data=json.dumps(body),
              headers={"Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    return {k: len(body[k]) for k in MARK_KEYS}


def read_sheet(token, get=None):
    """(contacts, outreach, error). Either list is None when it could not be read, and the
    page then keeps the one it already has."""
    import requests
    get = get or requests.get
    out, errors = {}, []
    for action, key in (("contacts", "contacts"), ("outreach", "companies")):
        try:
            res = get(TRACKER_URL, params={"action": action, "token": token}, timeout=60).json()
            if res.get("ok") and isinstance(res.get(key), list):
                out[action] = res[key]
            else:
                errors.append(f"{action}: {res.get('error') or 'bad reply'}")
        except Exception as e:
            errors.append(f"{action}: {str(e)[:80]}")
    return out.get("contacts"), out.get("outreach"), "; ".join(errors)


# ---------------------------------------------------------------- the board copy

def bundle_board(contacts, outreach, sheet_error):
    return {"jobs": show("origin/main:docs/jobs.json", []),
            "status": show("origin/main:docs/status.json", {}),
            "excluded": show("origin/main:docs/excluded.json", {}),
            "contacts": contacts, "outreach": outreach, "sheet_error": sheet_error}


def pieces(bundle, size=PIECE_CHARS):
    raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
    return [b64[i:i + size] for i in range(0, len(b64), size)], len(raw)


def piece_id(gen, i):
    return f"{gen}-{i:02d}"


def build_plan(work, gen, chunks, raw_bytes, now, meta_version, old_meta, done_outbox):
    out = os.path.join(work, "out")
    os.makedirs(out, exist_ok=True)
    for f in glob.glob(os.path.join(out, "*.json")):
        os.remove(f)
    sets = []
    for i, c in enumerate(chunks):
        path = os.path.join(out, piece_id(gen, i) + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"gen": gen, "i": i, "n": len(chunks), "data": c}, f)
        sets.append({"op": "set", "collection": "board", "doc_id": piece_id(gen, i),
                     "file_path": os.path.abspath(path)})
    meta = {"gen": gen, "n": len(chunks), "built_at": now.isoformat(), "bytes": raw_bytes}
    meta_path = os.path.join(out, "meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    meta_write = {"op": "set", "collection": "board", "doc_id": "meta",
                  "file_path": os.path.abspath(meta_path)}
    if meta_version:
        meta_write["if_version"] = meta_version

    deletes = []
    if old_meta and old_meta.get("gen") and old_meta.get("gen") != gen:
        deletes += [{"op": "delete", "collection": "board",
                     "doc_id": piece_id(old_meta["gen"], i), "if_version": 1}
                    for i in range(int(old_meta.get("n") or 0))]
    deletes += [{"op": "delete", "collection": "outbox", "doc_id": d, "if_version": 1}
                for d in done_outbox]

    batches = [sets[i:i + PIECES_PER_BATCH] for i in range(0, len(sets), PIECES_PER_BATCH)]
    batches.append([meta_write])
    batches += [deletes[i:i + DELETES_PER_BATCH] for i in range(0, len(deletes), DELETES_PER_BATCH)]
    plan = {"url": DASHBOARD_URL, "gen": gen, "batches": batches}
    with open(os.path.join(work, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=1)
    return plan


def cmd_plan(work, meta_version=0, wait=True, offline=False, now=None):
    now = now or datetime.now(timezone.utc)
    db = os.path.join(work, "db")
    old_meta = load(os.path.join(db, "board", "meta.json"))
    marks = load(os.path.join(db, "state", "marks.json"))
    outbox = {os.path.splitext(os.path.basename(p))[0]: load(p, {})
              for p in glob.glob(os.path.join(db, "outbox", "*.json"))}
    report = []

    if wait:
        wait_for_apply()
    else:
        fetch_refs()

    token = os.environ.get("TRACKER_TOKEN", "")
    done_outbox, contacts, outreach, sheet_error = [], None, None, ""
    if offline:
        sheet_error = "not read (offline run)"
        report.append("offline: tracker and Firebase left alone")
    else:
        if not token:
            sheet_error = "TRACKER_TOKEN not set"
            report.append("WARNING: TRACKER_TOKEN not set; tracker skipped")
        else:
            done_outbox, problems = post_outbox(outbox, token)
            report.append(f"tracker: {len(done_outbox)} of {len(outbox)} queued rows added")
            report += [f"  not added: {p}" for p in problems]
            contacts, outreach, sheet_error = read_sheet(token)
            report.append(f"tracker read: contacts {len(contacts) if contacts is not None else 'FAILED'}, "
                          f"outreach {len(outreach) if outreach is not None else 'FAILED'}"
                          + (f" ({sheet_error})" if sheet_error else ""))
        if marks is None:
            report.append("marks: none on the page yet, Firebase left alone")
        else:
            try:
                counts = patch_firebase(marks)
                report.append("Firebase: marks copied (" + ", ".join(f"{k} {v}" for k, v in counts.items()) + ")")
            except Exception as e:
                report.append(f"WARNING: Firebase not updated ({str(e)[:100]})")

    gen = now.strftime("%Y%m%dT%H%M%SZ")
    chunks, raw = pieces(bundle_board(contacts, outreach, sheet_error))
    plan = build_plan(work, gen, chunks, raw, now, meta_version, old_meta, done_outbox)
    report.append(f"board: {raw:,} bytes -> {len(chunks)} pieces, copy {gen}")
    report.append(f"plan: {len(plan['batches'])} ArtifactData batch calls in {os.path.join(work, 'plan.json')}")
    for line in report:
        log(line)
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--work", default="scoring/work/dash")
    p.add_argument("--meta-version", type=int, default=0,
                   help="board/meta's current version (0: it does not exist yet)")
    p.add_argument("--no-wait", action="store_true", help="do not wait for apply-scores")
    p.add_argument("--offline", action="store_true",
                   help="build the board copy only: no tracker, no Firebase")
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        cmd_plan(a.work, a.meta_version, wait=not a.no_wait, offline=a.offline)


if __name__ == "__main__":
    main()
