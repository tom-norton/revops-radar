#!/usr/bin/env python3
"""
RevOps Radar - daily broad job scanner for Tom Norton.

Primary source is Adzuna (aggregates thousands of job sites) across NL, UK, IE,
DE, ES. Optional supplements: company ATS feeds (clean names + full JDs),
revopsroles.com, hiring.cafe (best effort). Every UK/NL job is checked against
the official visa-sponsor registers. Claude Haiku scores the survivors.

Flow: fetch -> title/location pre-filter (free) -> dedupe -> sponsor check
      -> Claude scores (cheap, capped) -> write docs/jobs.json + docs/status.json

Usage:
  python scan.py            normal run
  python scan.py --dry      everything except Claude scoring
  python scan.py --verify   test optional ATS company slugs, no scoring
"""

import json, os, re, sys, time
from datetime import datetime, timezone, timedelta
import requests
import sponsors as spon

# ---------------------------------------------------------------- config

# Adzuna: broad backbone. country code -> human label. Free key: developer.adzuna.com
ADZUNA_COUNTRIES = {"nl": "Netherlands", "gb": "United Kingdom", "ie": "Ireland",
                    "de": "Germany", "es": "Spain"}
# broad OR-recall query; the title regex below does the real filtering
ADZUNA_WHAT_OR = ("revenue operations sales operations gtm go-to-market customer "
                  "success revenue strategy enablement business operations")
ADZUNA_MAX_DAYS = 3      # only fresh postings -> low volume, low cost
ADZUNA_PAGES = 2         # 2 x 50 = up to 100 per country before filtering

INCLUDE_TITLE = re.compile(
    r"revenue operations|revops|rev ops|sales operations|sales ops"
    r"|gtm|go[- ]to[- ]market|growth operations|marketing operations"
    r"|cs operations|customer success operations"
    r"|strategy (and|&) operations|business operations|commercial operations"
    r"|sales strategy|revenue strategy|revenue enablement|sales enablement"
    r"|(senior|principal|lead|enterprise|strategic).{0,20}customer success", re.I)

EXCLUDE_TITLE = re.compile(
    r"deal desk|quote[- ]to[- ]cash|order management|billing specialist"
    r"|intern\b|internship|working student|apprentice|graduate scheme"
    r"|\bvp\b|vice president|chief |\bsvp\b|\bevp\b", re.I)

INCLUDE_LOCATION = re.compile(
    r"netherlands|amsterdam|rotterdam|utrecht|eindhoven|hague"
    r"|london|united kingdom|england|scotland|\buk\b|manchester|edinburgh"
    r"|dublin|ireland|cork"
    r"|berlin|germany|munich|hamburg"
    r"|spain|barcelona|madrid"
    r"|\bemea\b|europe|remote", re.I)

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
API_URL = "https://api.anthropic.com/v1/messages"
KEEP_DAYS = 60
DESC_CHAR_CAP = 2000
MAX_SCORED_PER_RUN = 45
SPONSOR_REQUIRED = False   # if True, drop UK/NL jobs whose company isn't on a register

PROFILE = """Candidate: 11 yrs enterprise B2B SaaS (Customer Success + Account Management, GRC/compliance domain: NAVEX, LexisNexis). ESADE MBA finishing Jul 2026. Pivoting to RevOps / GTM Strategy / Sales Ops / CS Ops at Manager or senior IC level. Parallel track: Senior/Principal CSM at established companies. US citizen; needs employer visa sponsorship. Skills: Salesforce (cert in progress), HubSpot, SQL (basic), funnel modeling, CAC/NRR analytics, QBR systems.

Score the job 1-10 for fit. Rules:
- Location tiers: Netherlands 9; London/UK 7-8; Dublin/Ireland 7-8; Berlin 7; remote-from-Spain or Spain 6-7; other EU 4-5; outside EU 2.
- Penalize hard: deal desk / quote-to-cash, heavy quota-carrying commercial roles, heavy travel, solo first-RevOps-hire builds, junior Analyst titles.
- Title band: Manager or senior IC = good. Analyst/Specialist = comp risk vs visa floor, cap at 5. Director+ = stretch, cap at 6.
- If salary is stated and below EUR 71,300 base for a Netherlands role, flag "below HSM floor" and cap at 4.
- Senior CSM roles at established companies score as primary targets, not fallbacks.
- SPONSOR SIGNAL: a "sponsor" field may be provided. "not on register" is a caution flag (visa may be impossible) but registers use legal names that miss trading names, so treat it as -1 to -2, not an auto-zero. "sponsor" or "sponsor (likely)" is a plus for UK/NL roles. Ignore for other countries.
- RevOps/GTM roles score higher than pure lateral CS moves, all else equal.

Reply with ONLY this JSON, nothing else:
{"score": <1-10>, "tier": "<location tier label>", "flags": ["<risk flags, [] if none>"], "verdict": "<one blunt sentence, max 20 words>"}"""

# ---------------------------------------------------------------- helpers

def now_iso(): return datetime.now(timezone.utc).isoformat()
def get(url, **kw):
    kw.setdefault("timeout", 30)
    kw.setdefault("headers", {"User-Agent": "Mozilla/5.0 (job-radar; personal use)"})
    return requests.get(url, **kw)
def strip_html(t): return re.sub(r"<[^>]+>", " ", t or "").replace("&amp;", "&").replace("&nbsp;", " ")

def prefilter(title, location):
    if not INCLUDE_TITLE.search(title or ""): return False
    if EXCLUDE_TITLE.search(title or ""): return False
    if location and not INCLUDE_LOCATION.search(location): return False
    return True

# ---------------------------------------------------------------- Adzuna (primary)

def fetch_adzuna(app_id, app_key):
    out = []
    for cc, label in ADZUNA_COUNTRIES.items():
        for page in range(1, ADZUNA_PAGES + 1):
            try:
                r = get(f"https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}", params={
                    "app_id": app_id, "app_key": app_key,
                    "what_or": ADZUNA_WHAT_OR, "results_per_page": 50,
                    "max_days_old": ADZUNA_MAX_DAYS, "sort_by": "date",
                    "content-type": "application/json",
                })
                if r.status_code != 200:
                    break
                results = r.json().get("results", [])
                if not results:
                    break
                for j in results:
                    title = j.get("title", "")
                    loc = (j.get("location") or {}).get("display_name", "")
                    if not prefilter(title, loc):
                        continue
                    sal = ""
                    if j.get("salary_min"):
                        sal = f"{int(j['salary_min'])}-{int(j.get('salary_max') or j['salary_min'])} {label} local"
                    out.append({
                        "id": f"az-{cc}-{j.get('id')}",
                        "company": (j.get("company") or {}).get("display_name", ""),
                        "title": title, "location": loc, "country": cc,
                        "url": j.get("redirect_url", ""), "source": "adzuna",
                        "description": strip_html(j.get("description", "")),
                        "salary": sal,
                    })
                time.sleep(0.3)
            except Exception:
                break
    return out

# ---------------------------------------------------------------- ATS supplements (optional)

def fetch_greenhouse(name, slug):
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"); r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        if not prefilter(j.get("title", ""), loc): continue
        out.append({"id": f"gh-{slug}-{j['id']}", "company": name, "title": j["title"],
                    "location": loc, "country": "", "url": j.get("absolute_url", ""),
                    "source": "greenhouse",
                    "_detail": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{j['id']}"})
    return out

def greenhouse_desc(url):
    try:
        r = get(url); r.raise_for_status(); return strip_html(r.json().get("content", ""))
    except Exception:
        return ""

def fetch_lever(name, slug):
    r = get(f"https://api.lever.co/v0/postings/{slug}?mode=json"); r.raise_for_status()
    out = []
    for j in r.json():
        loc = (j.get("categories") or {}).get("location", "") or ""
        if not prefilter(j.get("text", ""), loc): continue
        out.append({"id": f"lv-{slug}-{j['id']}", "company": name, "title": j["text"],
                    "location": loc, "country": "", "url": j.get("hostedUrl", ""),
                    "source": "lever",
                    "description": strip_html(j.get("descriptionPlain") or j.get("description", ""))})
    return out

def fetch_ashby(name, slug):
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"); r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = j.get("location", "") or ""
        if not prefilter(j.get("title", ""), loc): continue
        out.append({"id": f"as-{slug}-{j.get('id')}", "company": name, "title": j["title"],
                    "location": loc, "country": "", "url": j.get("jobUrl") or j.get("applyUrl", ""),
                    "source": "ashby", "description": strip_html(j.get("descriptionPlain") or "")})
    return out

ATS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}

def fetch_revopsroles():
    out = []
    r = get("https://revopsroles.com/"); r.raise_for_status()
    for m in re.finditer(r'<a[^>]+href="([^"]*job[^"]*)"[^>]*>(.*?)</a>', r.text, re.I | re.S):
        href, inner = m.group(1), strip_html(m.group(2)).strip()
        if not inner or len(inner) < 8 or not INCLUDE_TITLE.search(inner): continue
        if EXCLUDE_TITLE.search(inner): continue
        url = href if href.startswith("http") else "https://revopsroles.com" + href
        out.append({"id": "rr-" + re.sub(r"\W+", "-", url)[-80:], "company": "",
                    "title": inner[:140], "location": "", "country": "",
                    "url": url, "source": "revopsroles"})
    return out

def fetch_hiringcafe():
    out = []
    payload = {"size": 40, "page": 0, "searchState": {
        "searchQuery": "revenue operations OR sales operations OR gtm OR customer success operations",
        "locations": [{"formatted_address": c, "types": ["country"]} for c in
                      ["Netherlands", "United Kingdom", "Ireland", "Germany", "Spain"]],
        "sortBy": "date"}}
    r = requests.post("https://hiring.cafe/api/search-jobs", json=payload, timeout=30,
                      headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
    if r.status_code in (403, 503): raise RuntimeError(f"blocked ({r.status_code}, likely Cloudflare)")
    r.raise_for_status()
    for j in r.json().get("results", []):
        info = j.get("job_information", {}) or {}; proc = j.get("v5_processed_job_data", {}) or {}
        title = info.get("title") or proc.get("core_job_title", "")
        loc = proc.get("formatted_workplace_location", "")
        if not prefilter(title, loc): continue
        out.append({"id": "hc-" + str(j.get("id", ""))[:60], "company": proc.get("company_name", ""),
                    "title": title, "location": loc, "country": "",
                    "url": j.get("apply_url") or info.get("url", ""), "source": "hiring.cafe",
                    "description": strip_html(info.get("description", ""))})
    return out

# ---------------------------------------------------------------- Claude scoring

def score_job(api_key, job):
    desc = (job.get("description") or "")[:DESC_CHAR_CAP]
    msg = (f"Title: {job['title']}\nCompany: {job.get('company','?')}\n"
           f"Location: {job.get('location','?')}\n"
           + (f"Salary: {job['salary']}\n" if job.get("salary") else "")
           + (f"Sponsor: {job['sponsor']}\n" if job.get("sponsor") else "")
           + (f"Description: {desc}" if desc else "No description; score on title/location/sponsor only."))
    r = requests.post(API_URL, timeout=60, headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": CLAUDE_MODEL, "max_tokens": 200, "system": PROFILE,
              "messages": [{"role": "user", "content": msg}]})
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    data = json.loads(re.sub(r"```json|```", "", text).strip())
    return {"score": float(data.get("score", 0)), "tier": str(data.get("tier", ""))[:40],
            "flags": [str(f)[:60] for f in data.get("flags", [])][:5],
            "verdict": str(data.get("verdict", ""))[:160]}

# ---------------------------------------------------------------- main

def main():
    verify, dry = "--verify" in sys.argv, "--dry" in sys.argv
    companies = json.load(open("companies.json")).get("companies", []) if os.path.exists("companies.json") else []

    if verify:
        print("Verifying optional ATS slugs...")
        for c in companies:
            try:
                n = len(ATS[c["ats"]](c["name"], c["slug"]))
                print(f"  OK   {c['name']:<18} matched {n}")
            except Exception as e:
                print(f"  FAIL {c['name']:<18} {e}")
        return

    os.makedirs("docs", exist_ok=True)
    seen = set(json.load(open("seen.json"))) if os.path.exists("seen.json") else set()
    existing = json.load(open("docs/jobs.json")) if os.path.exists("docs/jobs.json") else []
    src_status, found = {}, []

    # 1. Adzuna (primary)
    aid, akey = os.environ.get("ADZUNA_APP_ID", ""), os.environ.get("ADZUNA_APP_KEY", "")
    if aid and akey:
        try:
            jobs = fetch_adzuna(aid, akey); found += jobs
            src_status["adzuna (all boards)"] = f"ok ({len(jobs)})"
        except Exception as e:
            src_status["adzuna (all boards)"] = f"FAIL: {e}"
    else:
        src_status["adzuna (all boards)"] = "skipped: no ADZUNA_APP_ID/KEY set"

    # 2. optional ATS + community sources
    for c in companies:
        try:
            found += ATS[c["ats"]](c["name"], c["slug"])
        except Exception:
            pass
    for label, fn in [("revopsroles.com", fetch_revopsroles), ("hiring.cafe", fetch_hiringcafe)]:
        try:
            jobs = fn(); found += jobs; src_status[label] = f"ok ({len(jobs)})"
        except Exception as e:
            src_status[label] = f"skipped: {e}"

    new_jobs = [j for j in found if j["id"] not in seen]
    print(f"Fetched {len(found)} relevant, {len(new_jobs)} new.")

    # 3. sponsor registers (load once)
    print("Loading sponsor registers...")
    uk_reg, nl_reg = spon.load_uk(), spon.load_nl()
    src_status["UK sponsor register"] = ("ok - " + uk_reg.note) if uk_reg.ok else ("FAIL - " + uk_reg.note)
    src_status["NL sponsor register"] = ("ok - " + nl_reg.note) if nl_reg.ok else ("degraded - " + nl_reg.note)

    def sponsor_for(job):
        which = spon.which_register(job.get("location", ""), job.get("country", ""))
        if which == "UK":
            raw = uk_reg.match(job.get("company", "")) if uk_reg.ok else "unknown"
            return which, raw, spon.status_label(raw, "UK")
        if which == "NL":
            raw = nl_reg.match(job.get("company", "")) if nl_reg.ok else "unknown"
            return which, raw, spon.status_label(raw, "NL")
        return None, "n/a", ""

    # 4. score survivors
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    scored = []
    for j in new_jobs[:MAX_SCORED_PER_RUN]:
        if j.get("_detail") and not j.get("description"):
            j["description"] = greenhouse_desc(j.pop("_detail"))
        j.pop("_detail", None)

        which, raw, label = sponsor_for(j)
        j["sponsor_region"], j["sponsor_raw"], j["sponsor"] = which or "", raw, label
        if SPONSOR_REQUIRED and raw == "not_found":
            seen.add(j["id"]); continue

        if dry or not api_key:
            j.update({"score": 0, "tier": "", "flags": [], "verdict": "(not scored)"})
        else:
            try:
                j.update(score_job(api_key, j))
            except Exception as e:
                j.update({"score": 0, "tier": "", "flags": [], "verdict": f"scoring failed: {e}"})
        j["description"] = (j.get("description") or "")[:400]
        j["found_at"] = now_iso()
        scored.append(j); seen.add(j["id"])
        print(f"  [{j.get('score','-')}] {j['title']} @ {j.get('company') or j['source']} | {j['sponsor'] or 'n/a'}")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    merged = scored + [j for j in existing
                     if j.get("found_at", "") >= cutoff and not str(j.get("id", "")).startswith("demo-")]
    merged.sort(key=lambda j: j.get("found_at", ""), reverse=True)

    json.dump(sorted(seen), open("seen.json", "w"))
    json.dump(merged, open("docs/jobs.json", "w"), indent=1)
    json.dump({"last_run": now_iso(), "new_this_run": len(scored), "sources": src_status},
              open("docs/status.json", "w"), indent=1)
    print(f"Done. {len(scored)} new scored, {len(merged)} on dashboard.")

if __name__ == "__main__":
    main()
