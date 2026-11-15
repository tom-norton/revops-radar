"""Weekly hiring signals for the outreach list (the LAMP list in the Job Search Tracker).

Reads a list of companies, checks the free sources, and prints one JSON row per company.
It never writes anywhere: the weekly outreach session (tools/lamp_signals.md) reads the
output and writes the Signals and Companies tabs of the tracker itself.

    python tools/lamp_signals.py companies.json [--previous prev.json] > signals.json

companies.json is a list of {"company", "aliases": [...], "domain", "board"} where board is
an optional override: "ats:slug" to pin a board, or "none" when the guessed board belongs to
a different company with the same name. prev.json is last week's output (or the same shape, read
back from the Signals tab), used for the posting-burst and newly-on-register comparisons.

Free sources only:
  1. The company's own job board (findform's readers): sales and CS postings now against
     last week, and roles that pass the radar's title and location gate (a lead to check,
     not proof: the gate is the cheap first screen).
  2a. The radar's current dashboard (docs/jobs.json): roles at this company the radar scored
     at or above the gate (6.5), found in the last 30 days. This is what makes a company Hot.
     In-domain rows below the gate are listed separately, for information.
  2. The UK and NL sponsor registers (sponsors.py): status now, so a change from last week
     shows a company newly on the register.
  3. The radar's own history (git history of docs/jobs.json): on-target roles the radar
     found at this company 3 to 12 months ago.

Company-level data only. This repo is public: no contact names ever go in or out of here.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# A posting counts toward a burst when its title is a sales or customer-facing role. These
# are the roles a company hires ahead of the RevOps role it has not posted yet.
SALES_CS = re.compile(
    r"\b(account executive|account manager|account director|sales|business development|"
    r"bdr|sdr|customer success|csm|client success|solutions? consultant|solutions? engineer|"
    r"pre-?sales|partnerships?|channel)\b", re.I)

# Burst: at least this many more sales/CS postings than last week, with no relevant role open.
BURST_MIN_INCREASE = 3

# Radar history window, in days before today.
HISTORY_FROM_DAYS = 365
HISTORY_TO_DAYS = 90

# Same normaliser as companyKey() in docs/index.html, so names match the way the dashboard's
# Referral tag matches them.
COMPANY_NOISE = re.compile(
    r"\b(the|inc|incorporated|ltd|limited|llc|plc|corp|corporation|co|gmbh|ag|bv|b v|nv|n v|"
    r"sa|sas|srl|ulc|holdings?|group|uk|ireland|netherlands|nederland|belgium|canada|usa|us|"
    r"europe|emea|international|global)\b")


def company_key(name):
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\([^)]*\)", " ", s).replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = COMPANY_NOISE.sub(" ", s)
    return " ".join(s.split())


def names_of(c):
    """Every spelling a company goes by: its name with any bracketed part removed, plus aliases."""
    base = re.sub(r"\s*\(.*\)", "", c.get("company", "")).strip()
    out = [base] + [a for a in c.get("aliases", []) if a]
    return [n for n in dict.fromkeys(out) if n]


def keys_of(c):
    return {company_key(n) for n in names_of(c)} - {""}


def domain_stem(domain):
    d = (domain or "").lower().strip()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    d = re.sub(r"^www\.", "", d)
    return d.split(".")[0] if d else ""


def relevant(title, location):
    """True when the radar's own title and location gate would admit this posting."""
    import scan
    return scan.prefilter(title, location, "") is None


def count_board(jobs):
    sales = [j for j in jobs if SALES_CS.search(j.get("title", ""))]
    rel = [j for j in jobs if relevant(j.get("title", ""), j.get("location", ""))]
    return {"open_total": len(jobs), "sales_cs_open": len(sales), "relevant_open": len(rel),
            "relevant_titles": [f"{j['title'].strip()} ({j.get('location') or '?'})"
                                for j in rel][:5]}


def slug_guesses(c):
    """Whole-name slugs for every spelling, then the domain stem.

    Deliberately NOT the first-word fallback findform.board_slugs() ends with: on this list
    it lands on somebody else's board ("Privacy Company" -> privacy, "Be Informed" -> be)."""
    out = []
    for n in names_of(c):
        words = re.sub(r"[^\w\s-]", " ", n.lower()).split()
        if words:
            out += ["".join(words), "-".join(words)]
    stem = domain_stem(c.get("domain"))
    if stem:
        out.append(stem)
    return [s for s in dict.fromkeys(out) if len(s) > 2]


def board_for(c):
    """(ats, slug, jobs): the override, a companies.json entry, or the first slug that answers."""
    import findform
    b = (c.get("board") or "").strip()
    if b.lower() == "none":
        return "", "", []
    if ":" in b:
        ats, slug = (x.strip() for x in b.split(":", 1))
        return ats, slug, findform.board_jobs(ats, slug)
    for n in names_of(c):
        ats, slug = findform.known_board(n, findform.load_companies())
        if ats and slug:
            jobs = findform.board_jobs(ats, slug)
            if jobs:
                return ats, slug, jobs
    for slug in slug_guesses(c):
        ats, jobs = findform.probe_slug(slug)
        if jobs:
            return ats, slug, jobs
    return "", "", []


def radar_now(c, rows, today):
    """(at_gate, in_domain_below_gate): roles on the dashboard at this company found in the
    last 30 days. Only the first list makes a company Hot; the second is for information."""
    import scan
    ks = keys_of(c)
    since = (today - timedelta(days=30)).isoformat()
    hot, other = [], []
    for r in rows:
        if company_key(r.get("company")) not in ks or (r.get("found_at") or "")[:10] < since:
            continue
        label = f"{r.get('title', '').strip()} ({r.get('market') or '?'}, {r.get('score')})"
        if (r.get("score") or 0) >= scan.GATE:
            hot.append(label)
        elif r.get("in_domain"):
            other.append(label)
    return hot, other


def burst(now, prev):
    """True when sales/CS postings jumped and no relevant role is open yet."""
    if prev is None or now.get("relevant_open"):
        return False
    return now.get("sales_cs_open", 0) - prev.get("sales_cs_open", 0) >= BURST_MIN_INCREASE


def sponsor_status(c, uk, nl):
    out = {}
    for label, reg in (("uk", uk), ("nl", nl)):
        if reg is None or not reg.ok:
            out[label] = "unknown"
            continue
        best = "not_found"
        for n in names_of(c):
            m = reg.match(n)
            if m == "on_register":
                best = m
                break
            if m == "likely" and best == "not_found":
                best = m
        out[label] = best
    return out


def newly_on(now, prev):
    if not prev:
        return []
    return [k for k in ("uk", "nl")
            if now.get(k) == "on_register" and prev.get(k) in ("not_found", "likely")]


def history_rows(today, git=None):
    """On-target radar rows found 3 to 12 months ago, from weekly samples of docs/jobs.json."""
    import scan
    git = git or (lambda *a: subprocess.run(["git", "-C", ROOT, *a], capture_output=True,
                                            text=True, check=False).stdout)
    lo = today - timedelta(days=HISTORY_FROM_DAYS)
    hi = today - timedelta(days=HISTORY_TO_DAYS)
    seen, rows = set(), []
    d = hi
    while d >= lo:
        sha = git("rev-list", "-1", f"--before={d.isoformat()}", "origin/main", "--",
                  "docs/jobs.json").strip()
        d -= timedelta(days=7)
        if not sha or sha in seen:
            continue
        seen.add(sha)
        try:
            data = json.loads(git("show", f"{sha}:docs/jobs.json") or "[]")
        except ValueError:
            continue
        for r in data if isinstance(data, list) else []:
            found = (r.get("found_at") or "")[:10]
            if not found or not (lo.isoformat() <= found <= hi.isoformat()):
                continue
            score = r.get("score") or 0
            if score >= scan.GATE or r.get("in_domain"):
                rows.append(r)
    out, ids = [], set()
    for r in rows:
        if r.get("id") in ids:
            continue
        ids.add(r.get("id"))
        out.append(r)
    return out


def past_roles(c, rows):
    ks = keys_of(c)
    hits = [r for r in rows if company_key(r.get("company")) in ks]
    return [f"{r.get('title', '').strip()} ({r.get('market') or '?'}, {(r.get('found_at') or '')[:10]})"
            for r in hits]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("companies")
    ap.add_argument("--previous")
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--no-sponsors", action="store_true")
    a = ap.parse_args(argv)

    import sponsors

    companies = json.load(open(a.companies, encoding="utf-8"))
    prev = {}
    if a.previous and os.path.exists(a.previous):
        prev = {company_key(p.get("company")): p for p in json.load(open(a.previous, encoding="utf-8"))}

    today = datetime.now(timezone.utc).date()
    uk = nl = None
    if not a.no_sponsors:
        uk, nl = sponsors.load_uk(), sponsors.load_nl()
    hist = [] if a.no_history else history_rows(today)
    try:
        current = json.load(open(os.path.join(ROOT, "docs", "jobs.json"), encoding="utf-8"))
    except (OSError, ValueError):
        current = []

    out = []
    for c in companies:
        if not c.get("company"):
            continue
        p = prev.get(company_key(c["company"]))
        ats, slug, jobs = board_for(c)
        row = {"company": c["company"], "week": today.isoformat(),
               "board": f"{ats}:{slug}" if ats else ""}
        row.update(count_board(jobs) if jobs else
                   {"open_total": None, "sales_cs_open": None, "relevant_open": None,
                    "relevant_titles": []})
        row["sales_cs_last_week"] = p.get("sales_cs_open") if p else None
        has_prev = p is not None and p.get("sales_cs_open") is not None
        row["burst"] = bool(jobs) and burst(row, p if has_prev else None)
        row["sponsor"] = sponsor_status(c, uk, nl) if uk is not None else {}
        row["newly_on_register"] = newly_on(row["sponsor"], (p or {}).get("sponsor"))
        row["radar_open_now"], row["radar_in_domain_below_gate"] = radar_now(c, current, today)
        row["past_on_target_roles"] = past_roles(c, hist)
        out.append(row)
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write("\n")
    print(f"{len(out)} companies; {sum(1 for r in out if r['board'])} with a readable board; "
          f"history rows {len(hist)}; uk {getattr(uk, 'note', 'skipped')}; "
          f"nl {getattr(nl, 'note', 'skipped')}", file=sys.stderr)


if __name__ == "__main__":
    main()
