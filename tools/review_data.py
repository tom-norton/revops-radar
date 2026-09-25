#!/usr/bin/env python3
"""The numbers behind the weekly review, computed in code so the review only has to read
them. Arithmetic is not a judgement: the same week's data should always produce the same
counts, and a model asked to tally 700 rows is a model asked to get some of them wrong.

    python tools/review_data.py              -> JSON on stdout
    python tools/review_data.py --days 7     (window length; default 7)

Reads docs/jobs.json, docs/excluded.json and the dashboard's shared state (Hidden,
Applied, and the optional "why" notes) from Firebase. Writes nothing. If Firebase cannot
be reached the report still runs; the sections that need it say so.
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIREBASE_STATE_URL = ("https://revops-radar-2822a-default-rtdb.europe-west1."
                      "firebasedatabase.app/revops-radar-state.json")
STRONG = 7.5
GATE = 6.5
BASELINE_WEEKS = 4


def load(path, default):
    try:
        with open(os.path.join(ROOT, path)) as f:
            return json.load(f)
    except Exception:
        return default


def state():
    try:
        r = requests.get(FIREBASE_STATE_URL, timeout=20)
        r.raise_for_status()
        return r.json() or {}, None
    except Exception as e:
        return {}, f"Firebase unreachable: {str(e)[:120]}"


def ids_of(j):
    return [str(j.get("id") or "")] + [str(x) for x in (j.get("dupe_ids") or [])]


def summarise(rows):
    scored = [j for j in rows if isinstance(j.get("score"), (int, float))]
    sc = [j["score"] for j in scored]
    dims = defaultdict(list)
    for j in scored:
        for k, v in (j.get("dimensions") or {}).items():
            dims[k].append(v)
    return {
        "scored": len(scored),
        "set_aside_unreadable": sum(1 for j in rows if j.get("evidence") == "thin"),
        "avg_score": round(sum(sc) / len(sc), 2) if sc else None,
        "strong_7_5_plus": sum(s >= STRONG for s in sc),
        "at_or_above_gate_6_5": sum(s >= GATE for s in sc),
        "by_market": dict(Counter(j.get("market") or "?" for j in scored).most_common()),
        "by_track": dict(Counter(j.get("track") or "?" for j in scored)),
        "by_source": dict(Counter(j.get("source") or "?" for j in scored).most_common()),
        "strong_by_source": dict(Counter(j.get("source") or "?" for j in scored
                                         if j["score"] >= STRONG).most_common()),
        "strong_by_market": dict(Counter(j.get("market") or "?" for j in scored
                                         if j["score"] >= STRONG).most_common()),
        "avg_dimension": {k: round(sum(v) / len(v), 2) for k, v in dims.items()},
        "apply_link": dict(Counter(j.get("apply_link") or "?" for j in scored)),
    }


def main():
    days = 7
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).isoformat()
    base_start = (now - timedelta(days=days * (BASELINE_WEEKS + 1))).isoformat()

    jobs = load("docs/jobs.json", [])
    excluded = load("docs/excluded.json", {}).get("rows", [])
    status = load("docs/status.json", {})
    st, st_err = state()

    week = [j for j in jobs if (j.get("found_at") or "") >= start]
    base = [j for j in jobs if base_start <= (j.get("found_at") or "") < start]

    # Hidden / applied, this window and all time, joined back to the rows they were on.
    by_id = {}
    for j in jobs:
        for i in ids_of(j):
            by_id.setdefault(i, j)
    hidden = set(st.get("hidden") or [])
    applied = set(st.get("applied") or [])
    hidden_at = {r["id"]: r["at"] for r in (st.get("hidden_at") or []) if r and r.get("id")}
    applied_at = {r["id"]: r["at"] for r in (st.get("applied_at") or []) if r and r.get("id")}
    notes = [n for n in (st.get("hide_notes") or []) if n and n.get("id")]

    def row_brief(i, extra=None):
        j = by_id.get(i, {})
        out = {"id": i, "title": j.get("title"), "company": j.get("company"),
               "score": j.get("score"), "shot": j.get("shot"), "want": j.get("want"),
               "market": j.get("market"), "track": j.get("track"),
               "verdict": j.get("verdict"), "hard_gaps": j.get("hard_gaps")}
        out.update(extra or {})
        return out

    def marked_rows(marks):
        """Each row once, however many of its ids carry the mark (a row that absorbed a
        duplicate can be hidden under both), with the id that was actually marked."""
        seen, out = set(), []
        for i in marks:
            j = by_id.get(i)
            if j is not None and id(j) not in seen:
                seen.add(id(j))
                out.append((i, j))
        return out

    applied_scores = [by_id[i]["score"] for i in applied
                      if i in by_id and isinstance(by_id[i].get("score"), (int, float))]
    hidden_scores = [by_id[i]["score"] for i in hidden
                     if i in by_id and isinstance(by_id[i].get("score"), (int, float))]

    reason_counts = Counter(r for n in notes for r in (n.get("reasons") or []))
    reason_by_market = defaultdict(Counter)
    reason_scores = defaultdict(list)
    for n in notes:
        for r in n.get("reasons") or []:
            reason_by_market[r][n.get("market") or "?"] += 1
            if isinstance(n.get("score"), (int, float)):
                reason_scores[r].append(n["score"])

    out = {
        "generated_at": now.isoformat(),
        "window_days": days,
        "last_scan": status.get("last_run"),
        "score_model": status.get("score_model"),
        "last_scan_sources": status.get("sources"),
        "this_window": summarise(week),
        # TOTALS over the four weeks before this window: divide counts by `weeks` to
        # compare with this_window. Averages (avg_score, avg_dimension) compare as they are.
        "baseline": {"weeks": BASELINE_WEEKS, **summarise(base)},
        "drops_this_window": dict(Counter(
            r.get("stage") for r in excluded if (r.get("dropped_at") or "") >= start)),
        "drops_all_retained": dict(Counter(r.get("stage") for r in excluded)),
        # Every stage-1 kill still on file with its reason: the review picks out the ones
        # that look like roles Tom would have wanted.
        "stage1_kills": [{"title": r.get("title"), "company": r.get("company"),
                          "reason": r.get("reason"), "dropped_at": r.get("dropped_at")}
                         for r in excluded if r.get("stage") == "stage1-kill"][:80],
        "top_hard_gaps_this_window": Counter(
            g.strip().lower()[:80] for j in week for g in (j.get("hard_gaps") or [])
        ).most_common(12),
        "tom": None if st_err else {
            "hidden_total": len(hidden), "applied_total": len(applied),
            "hidden_this_window": sum(1 for i, a in hidden_at.items() if a >= start),
            "applied_this_window": sum(1 for i, a in applied_at.items() if a >= start),
            "avg_score_applied": round(sum(applied_scores) / len(applied_scores), 2)
                                 if applied_scores else None,
            "avg_score_hidden": round(sum(hidden_scores) / len(hidden_scores), 2)
                                if hidden_scores else None,
            # The calibration cases: where Tom and the score disagree.
            "hidden_but_scored_strong": [row_brief(i, {"hidden_at": hidden_at.get(i)})
                                         for i, j in marked_rows(hidden)
                                         if (j.get("score") or 0) >= STRONG][:25],
            "applied_but_scored_below_gate": [row_brief(i, {"applied_at": applied_at.get(i)})
                                              for i, j in marked_rows(applied)
                                              if isinstance(j.get("score"), (int, float))
                                              and j["score"] < GATE][:25],
            # Strong rows still sitting untouched: neither hidden nor applied.
            "strong_untouched": [row_brief(ids_of(j)[0]) for j in jobs
                                 if (j.get("score") or 0) >= STRONG
                                 and not any(i in hidden or i in applied for i in ids_of(j))][:25],
            "hide_notes_total": len(notes),
            "hide_notes_this_window": [n for n in notes if (n.get("at") or "") >= start],
            "hide_reasons_all_time": dict(reason_counts.most_common()),
            "hide_reasons_by_market": {r: dict(c) for r, c in reason_by_market.items()},
            "hide_reason_avg_score": {r: round(sum(v) / len(v), 2)
                                      for r, v in reason_scores.items()},
        },
        "tom_error": st_err,
    }
    json.dump(out, sys.stdout, indent=1, default=str)
    print()


if __name__ == "__main__":
    main()
