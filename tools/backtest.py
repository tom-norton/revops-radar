#!/usr/bin/env python3
"""Where would each application have ranked? Joins the tracker's Applications tab to the
radar rows the dashboard actually held, read back out of git history, and reports the
rank each one had on the day Tom applied -- under the stored score and under any other
ordering passed in -- for the screens and for every group the conversion diagnosis named.

    python tools/backtest.py --tracker tracker.csv            -> markdown on stdout
    TRACKER_TOKEN=... python tools/backtest.py                 (reads the sheet's web app)
    python tools/backtest.py --tracker t.csv --json            (machine-readable)

Reads git history (`git log -- docs/jobs.json`, one snapshot per day), so the clone needs
full history: `git fetch --unshallow` on a shallow checkout. Calls no model and costs
nothing. Writes nothing: the report holds tracker data, so it is printed, not committed.

What it can and cannot say. Four screens can disprove an ordering -- a ranking that puts
all four at the bottom is wrong -- but they cannot fit one. And every row scored before
13 Nov 2026 was scored without its title, company, market or sponsor line (the header bug
fixed in 71c8250), so the stored score is the old scorer's, not today's.
"""

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import findform  # noqa: E402
import scan  # noqa: E402
import review_data  # noqa: E402

# The groups the Nov 2026 conversion diagnosis named. Each is a predicate over an
# application joined to its radar row.
BIG_GRC = re.compile(r"onetrust|workiva|wolters kluwer|diligent", re.I)
MANAGER_PLUS = re.compile(r"\b(manager|head|director|lead|vp|principal)\b", re.I)
GROUPS = [
    ("screens", lambda a: a["outcome"] == "interview"),
    ("home domain", lambda a: a["in_domain"]),
    ("large GRC vendors", lambda a: bool(BIG_GRC.search(a["company"]))),
    ("ops, manager+, outside domain",
     lambda a: a["track"] == "revops" and not a["in_domain"] and bool(MANAGER_PLUS.search(a["title"]))),
    ("UK", lambda a: a["market"] == "UK-London"),
    ("NL", lambda a: a["market"] == "NL"),
    ("CS track", lambda a: a["track"] == "cs"),
    ("ops track", lambda a: a["track"] == "revops"),
]
# How long a row counts as "on the board" for ranking: the dashboard hides rows older than
# MAX_AGE_DAYS, so the competition on a given day is what was found in the week before.
BOARD_DAYS = 7


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True,
                          check=True).stdout


def snapshots():
    """{date: rows} -- the last committed docs/jobs.json of each day."""
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        print("warning: shallow clone; run `git fetch --unshallow` for full history",
              file=sys.stderr)
    by_day = {}
    for line in git("log", "--format=%H %cs", "--", "docs/jobs.json").splitlines():
        sha, day = line.split()
        by_day.setdefault(day, sha)          # log is newest first: first seen is the day's last
    out = {}
    for day, sha in sorted(by_day.items()):
        try:
            rows = json.loads(git("show", f"{sha}:docs/jobs.json"))
        except (subprocess.CalledProcessError, ValueError):
            continue
        if isinstance(rows, list):
            out[day] = rows
    return out


def norm_url(u):
    u = (u or "").strip().lower().split("?")[0].split("#")[0].rstrip("/)")
    return re.sub(r"^https?://(www\.)?", "", u)


def urls_of(row):
    out = {norm_url(row.get("url")), norm_url(row.get("apply_url"))}
    for s in row.get("also_seen") or []:
        out.add(norm_url(s.get("url") if isinstance(s, dict) else s))
    return {u for u in out if u}


def latest_rows(snaps):
    """Every row id ever on the board, at its last stored version, with the first day it
    appeared."""
    rows, first = {}, {}
    for day, snap in sorted(snaps.items()):
        for r in snap:
            rid = str(r.get("id") or "")
            if not rid:
                continue
            first.setdefault(rid, day)
            rows[rid] = r
    return rows, first


def join(app, rows, first, applied_day):
    """The radar row this application was for, or None. A link match first; then the same
    employer with a close title, found no later than three days after applying."""
    link = norm_url(app.get("Link"))
    limit = (applied_day + timedelta(days=3)).isoformat()
    if link:
        for rid, r in rows.items():
            if first[rid] <= limit and link in urls_of(r):
                return r
    want = scan.company_tokens(app.get("Company"))
    best, best_s = None, 0.0
    for rid, r in rows.items():
        if first[rid] > limit or scan.company_tokens(r.get("company")) != want:
            continue
        s = findform.title_score(app.get("Title"), r.get("title"))
        if s > best_s:
            best, best_s = r, s
    return best if best_s >= 0.6 else None


def board_on(snaps, day):
    """The rows a ranking competed against on `day`: the nearest snapshot at or before it,
    scored, and found within BOARD_DAYS."""
    days = [d for d in snaps if d <= day]
    if not days:
        return []
    snap = snaps[max(days)]
    cutoff = (datetime.fromisoformat(day) - timedelta(days=BOARD_DAYS)).date().isoformat()
    return [r for r in snap if isinstance(r.get("score"), (int, float))
            and r.get("evidence") != "thin" and (r.get("found_at") or "")[:10] >= cutoff]


# Orderings to compare, highest first.
def _num(v):
    return v if isinstance(v, (int, float)) else 0.0


RANKINGS = {
    "radar score": lambda r: _num(r.get("score")),
    "domain first, then score": lambda r: (1 if scan.in_home_domain(r) else 0, _num(r.get("score"))),
    # Recomputed from the stored row with today's rank_axes(), so old rows are ranked the
    # way the dashboard ranks them now (Shot, Want and their even-balance rank).
    "two-axis rank": lambda r: _num(scan.rank_axes(r).get("rank")),
    "shot": lambda r: _num(scan.rank_axes(r).get("shot")),
    "want": lambda r: _num(scan.rank_axes(r).get("want")),
}


def percentile(row, board, key):
    """Where the row sat on that day's board: 1.0 is the top, 0.0 the bottom."""
    others = [r for r in board if str(r.get("id")) != str(row.get("id"))]
    if not others:
        return None
    k = key(row)
    below = sum(1 for r in others if key(r) < k)
    ties = sum(1 for r in others if key(r) == k)
    return round((below + ties / 2) / len(others), 3)


def run(tracker_text, snaps, rankings=RANKINGS, today=None):
    today = today or datetime.now(timezone.utc)
    rows, first = latest_rows(snaps)
    apps = []
    for t in csv.DictReader(io.StringIO(tracker_text)):
        applied = review_data.tracker_date(t.get("Date Applied"), today)
        if not applied:
            continue
        day = applied.date()
        r = join(t, rows, first, day)
        a = {"company": t.get("Company") or "", "title": t.get("Title") or "",
             "applied": day.isoformat(),
             "outcome": review_data.outcome_of(t, applied, today),
             "matched": r is not None}
        if r is not None:
            board = board_on(snaps, day.isoformat())
            a.update({"id": r.get("id"), "market": r.get("market") or "",
                      "track": "cs" if scan.is_csm_title(r.get("title")) else "revops",
                      "in_domain": scan.in_home_domain(r), "score": r.get("score"),
                      "shot": r.get("shot"), "want": r.get("want"),
                      "sponsor": r.get("sponsor") or "", "board": len(board),
                      "pct": {name: percentile(r, board, key) if board else None
                              for name, key in rankings.items()}})
        else:
            a.update({"market": review_data.TRACKER_MARKETS.get(
                (t.get("Location") or "").strip().lower(), ""),
                "track": "cs" if scan.is_csm_title(a["title"]) else "revops",
                "in_domain": scan.home_domain_company(a["company"])})
        apps.append(a)
    return apps


def summary(apps, rankings=RANKINGS):
    out = []
    for name, pred in GROUPS:
        g = [a for a in apps if pred(a)]
        m = [a for a in g if a["matched"] and a.get("board")]
        decided = [a for a in g if a["outcome"] != "pending"]
        line = {"group": name, "applied": len(g), "decided": len(decided),
                "screens": sum(1 for a in g if a["outcome"] == "interview"),
                "on_radar": len(m)}
        for rk in rankings:
            vals = [a["pct"][rk] for a in m if a["pct"].get(rk) is not None]
            line[rk] = round(median(vals), 2) if vals else None
        out.append(line)
    return out


def markdown(apps, rankings=RANKINGS):
    names = list(rankings)
    L = ["# Backtest: where each application ranked on the day it was applied for", "",
         f"{len(apps)} applications; {sum(a['matched'] for a in apps)} matched to a radar row "
         f"(the radar started 11 Jul 2026, so earlier applications cannot match). Ranks are "
         f"percentiles on that day's board (1.0 = top), over scored rows found in the "
         f"previous {BOARD_DAYS} days.", "",
         "Scores on rows before 13 Nov 2026 come from the scorer that never saw the title, "
         "company, market or sponsor line, so read the score column as the old scorer's.", "",
         "## By group (median percentile)", "",
         "| group | applied | decided | screens | on radar | " + " | ".join(names) + " |",
         "|---|---|---|---|---|" + "---|" * len(names)]
    for s in summary(apps, rankings):
        L.append(f"| {s['group']} | {s['applied']} | {s['decided']} | {s['screens']} | "
                 f"{s['on_radar']} | " + " | ".join(
                     "" if s[n] is None else f"{s[n]:.2f}" for n in names) + " |")
    L += ["", "## Screens", "",
          "| applied | company | title | market | domain | score | " + " | ".join(names) + " |",
          "|---|---|---|---|---|---|" + "---|" * len(names)]
    for a in apps:
        if a["outcome"] != "interview":
            continue
        pct = a.get("pct") or {}
        L.append(f"| {a['applied']} | {a['company']} | {a['title']} | {a.get('market','')} | "
                 f"{'yes' if a['in_domain'] else ''} | {a.get('score', '') if a['matched'] else 'not on radar'} | "
                 + " | ".join("" if pct.get(n) is None else f"{pct[n]:.2f}" for n in names) + " |")
    return "\n".join(L) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tracker", help="CSV export of the Applications tab")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.tracker:
        text = open(args.tracker, encoding="utf-8-sig").read()
    else:
        token = os.environ.get("TRACKER_TOKEN", "")
        if not token:
            p.error("pass --tracker or set TRACKER_TOKEN")
        text = review_data.fetch_tracker(review_data.tracker_url(), token)
    apps = run(text, snapshots())
    if args.json:
        print(json.dumps({"applications": apps, "summary": summary(apps)}, indent=1))
    else:
        print(markdown(apps))


if __name__ == "__main__":
    main()
