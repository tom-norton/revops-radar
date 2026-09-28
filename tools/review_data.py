#!/usr/bin/env python3
"""The numbers behind the weekly review, computed in code so the review only has to read
them. Arithmetic is not a judgement: the same week's data should always produce the same
counts, and a model asked to tally 700 rows is a model asked to get some of them wrong.

    python tools/review_data.py              -> JSON on stdout
    python tools/review_data.py --days 7     (window length; default 7)
    python tools/review_data.py --tracker /tmp/tracker.csv

Reads docs/jobs.json, docs/excluded.json and the dashboard's shared state (Hidden,
Applied, and the optional "why" notes) from Firebase. Writes nothing. If Firebase cannot
be reached the report still runs; the sections that need it say so.

--tracker is a CSV export of the Applications tab of Tom's Job Search Tracker sheet
(Company, Title, Location, Score, Date Applied, Status, Link, Referral?, Interview Date).
It is the only record of what happened AFTER he applied, and the only one that goes back
further than the dashboard's 45 days, so the coaching half of the review is built on it.
Without it the "applications" block says so and the rest still runs.
"""

import csv
import json
import os
import re
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
# Tom's own rule: an application with no answer after three weeks is a no-response, whether
# or not he has got round to changing it in the sheet.
NO_RESPONSE_DAYS = 21
CS_TITLE = re.compile(r"customer success|\bcsm\b|customer business|customer experience"
                      r"|customer enablement", re.I)
TRACKER_MARKETS = {"netherlands": "NL", "ireland": "IE", "uk": "UK-London",
                   "united kingdom": "UK-London", "belgium": "BE", "canada": "CA",
                   "us-remote": "US-Remote", "us": "US-Remote"}


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


def tracker_date(s, today):
    """The sheet stores "9/25" with no year. The latest such date not after today (plus a
    day of timezone slack), so a January review reads "12/30" as last December."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*$", s or "")
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if y:
            y = int(y)
            return datetime(y + 2000 if y < 100 else y, mo, d, tzinfo=timezone.utc)
        dt = datetime(today.year, mo, d, tzinfo=timezone.utc)
        return dt if dt <= today + timedelta(days=1) else dt.replace(year=today.year - 1)
    except ValueError:
        return None


def outcome_of(row, applied_on, today):
    """One of interview / rejected / no_response / pending, from the sheet's Status and
    Interview Date columns, with Tom's three-week rule applied to a stale "Applied"."""
    status = (row.get("Status") or "").strip().lower()
    if (row.get("Interview Date") or "").strip() or re.search(r"interview|offer|screen", status):
        return "interview"
    if "reject" in status or "declin" in status:
        return "rejected"
    if "no response" in status or "ghost" in status:
        return "no_response"
    if applied_on and (today - applied_on).days >= NO_RESPONSE_DAYS:
        return "no_response"
    return "pending"


def score_band(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "unscored"
    return "7.5+" if v >= STRONG else ("6.5-7.4" if v >= GATE else "under 6.5")


def outcome_table(apps, key):
    """{group: {applied, interview, rejected, no_response, pending, interview_rate}}.
    The rate is over DECIDED applications only (pending excluded), so a burst of fresh
    applications does not read as a falling hit rate."""
    out = defaultdict(Counter)
    for a in apps:
        out[key(a)][a["outcome"]] += 1
    table = {}
    for g, c in sorted(out.items(), key=lambda kv: -sum(kv[1].values())):
        decided = c["interview"] + c["rejected"] + c["no_response"]
        table[g] = {"applied": sum(c.values()), **{k: c[k] for k in
                    ("interview", "rejected", "no_response", "pending")},
                    "interview_rate": round(c["interview"] / decided, 3) if decided else None}
    return table


def applications(path, jobs, now, start):
    """The coaching block: outcomes from the tracker, joined to the radar's own reading of
    each role where the dashboard still has it."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("Company") or "").strip()]
    by_link, by_name = {}, {}
    for j in jobs:
        for u in [j.get("url"), j.get("apply_url")] + [a.get("url") for a in j.get("also_seen") or []]:
            if u:
                by_link.setdefault(u.split("?")[0].rstrip("/)"), j)
        by_name.setdefault(((j.get("company") or "").lower(), (j.get("title") or "").lower()), j)
    apps = []
    for r in rows:
        on = tracker_date(r.get("Date Applied"), now)
        link = (r.get("Link") or "").strip()
        j = by_link.get(link.split("?")[0].rstrip("/)")) or by_name.get(
            ((r.get("Company") or "").strip().lower(), (r.get("Title") or "").strip().lower()))
        loc = (r.get("Location") or "").strip()
        a = {"company": r.get("Company", "").strip(), "title": r.get("Title", "").strip(),
             "market": TRACKER_MARKETS.get(loc.lower(), loc or "?"),
             "track": "cs" if CS_TITLE.search(r.get("Title") or "") else "revops",
             "score": r.get("Score", "").strip(), "band": score_band(r.get("Score")),
             "applied_on": on.date().isoformat() if on else None,
             "status_in_sheet": (r.get("Status") or "").strip(),
             "referral": bool((r.get("Referral?") or "").strip()),
             "interview_date": (r.get("Interview Date") or "").strip() or None}
        a["outcome"] = outcome_of(r, on, now)
        a["stale_in_sheet"] = (a["outcome"] == "no_response"
                               and a["status_in_sheet"].lower() == "applied")
        if j:
            a["radar"] = {"verdict": j.get("verdict"), "hard_gaps": j.get("hard_gaps"),
                          "dimensions": j.get("dimensions"), "flags": j.get("flags"),
                          "sponsor": j.get("sponsor")}
        apps.append(a)
    month = lambda a: (a["applied_on"] or "?")[:7]
    return {
        "total": len(apps),
        "outcomes_all_time": dict(Counter(a["outcome"] for a in apps)),
        "interviews": [a for a in apps if a["outcome"] == "interview"],
        "applied_this_window": [a for a in apps if (a["applied_on"] or "") >= start[:10]],
        # Rows still reading "Applied" in the sheet after three weeks: counted here as no
        # response, and listed so Tom can update the sheet.
        "stale_in_sheet": [{k: a[k] for k in ("company", "title", "applied_on")}
                           for a in apps if a["stale_in_sheet"]],
        "by_market": outcome_table(apps, lambda a: a["market"]),
        "by_track": outcome_table(apps, lambda a: a["track"]),
        "by_score_band": outcome_table(apps, lambda a: a["band"]),
        "by_referral": outcome_table(apps, lambda a: "referral" if a["referral"] else "cold"),
        "by_month": dict(sorted(outcome_table(apps, month).items())),
        # Hard gaps on the applications the radar still holds a reading for, rejected vs
        # not: the gap a screener keeps stopping on is the one worth closing first.
        "hard_gaps_on_rejected": Counter(
            g.strip().lower()[:80] for a in apps if a["outcome"] == "rejected"
            for g in (a.get("radar") or {}).get("hard_gaps") or []).most_common(10),
        "rows_with_radar_detail": sum(1 for a in apps if a.get("radar")),
    }


def main():
    days = 7
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    tracker = None
    if "--tracker" in sys.argv:
        tracker = sys.argv[sys.argv.index("--tracker") + 1]
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
    # Every role marked applied on the dashboard in this window, with the radar's reading
    # of it -- not just the ones under the gate -- so the review can say what he is
    # choosing, not only where he overrode the score.
    if out["tom"] is not None:
        out["tom"]["applied_rows_this_window"] = [
            row_brief(i, {"applied_at": applied_at.get(i), "dimensions": j.get("dimensions"),
                          "flags": j.get("flags")})
            for i, j in marked_rows(applied) if (applied_at.get(i) or "") >= start]
    out["applications"], out["applications_error"] = None, None
    if not tracker:
        out["applications_error"] = ("no --tracker CSV given: export the Applications tab "
                                     "of Job Search Tracker and pass it")
    else:
        try:
            out["applications"] = applications(tracker, jobs, now, start)
        except Exception as e:
            out["applications_error"] = f"tracker unreadable: {str(e)[:160]}"
    json.dump(out, sys.stdout, indent=1, default=str)
    print()


if __name__ == "__main__":
    main()
