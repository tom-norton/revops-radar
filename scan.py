#!/usr/bin/env python3
"""
RevOps Radar - daily job scanner for Tom Norton.

Markets: Netherlands (anywhere), UK (London area only), Ireland (Dublin).
Germany, Spain, and remote-anywhere/EMEA are deliberately excluded.

Data layer (multi-source so no single source can break the run):
  - Adzuna API      : NL + UK (no Ireland coverage in the Adzuna API)
  - Reed API        : UK depth (free key, https://www.reed.co.uk/developers)
  - JobSpy / Indeed : Dublin/Ireland coverage (the Adzuna gap)
  - Company ATS      : Greenhouse / Lever / Ashby for named companies (clean names,
                       full descriptions, strong sponsor matching) - companies.json
  - hiring.cafe      : best-effort only (its API blocks datacenter IPs)

Pipeline:
  fetch -> title/location prefilter (free) -> dedupe -> UK/NL sponsor-register check
        -> STAGE 1 cheap screen (Claude Haiku, kill/keep)
        -> STAGE 2 deep score (Claude Sonnet, full weighted rubric vs profile.md)
        -> deterministic caps -> write docs/jobs.json + docs/status.json

Usage:
  python scan.py            normal run
  python scan.py --dry      everything except the Claude calls
  python scan.py --verify   test the optional ATS company slugs, no scoring
"""

import json, os, re, sys, time
from datetime import datetime, timezone, timedelta
import requests
import sponsors as spon

# ---------------------------------------------------------------- config

# Adzuna covers NL + UK only (its API has no Ireland). Ireland comes from JobSpy + ATS.
ADZUNA_COUNTRIES = {"nl": "Netherlands", "gb": "United Kingdom"}
ADZUNA_WHAT_OR = ("revenue operations sales operations gtm go-to-market customer "
                  "success revenue strategy enablement business operations")
ADZUNA_MAX_DAYS = 4      # only fresh postings -> low volume, low cost
ADZUNA_PAGES = 2         # 2 x 50 = up to 100 per country before filtering

# Reed (UK). Free key acts as the HTTP basic-auth username, blank password.
REED_KEYWORDS = ("revenue operations OR sales operations OR gtm OR go-to-market OR "
                 "revenue strategy OR sales strategy OR revenue enablement OR "
                 "customer success operations OR business operations")

# JobSpy / Indeed for Ireland (Dublin). Best-effort: never breaks the run.
JOBSPY_TERMS = ["revenue operations", "sales operations", "gtm strategy",
                "revenue strategy", "customer success operations"]

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

# London + commuter belt only for the UK. Other UK cities are rejected below.
UK_LONDON = re.compile(
    r"london|greater london|city of london|canary wharf|shoreditch|croydon"
    r"|watford|reading|slough|staines|uxbridge|richmond|kingston|bromley"
    r"|ilford|romford|enfield|barnet|harrow|wembley|hounslow|home counties"
    r"|surrey|hertfordshire|\bessex\b|\bkent\b", re.I)
UK_OTHER_CITY = re.compile(
    r"manchester|edinburgh|glasgow|birmingham|leeds|bristol|liverpool|sheffield"
    r"|newcastle|cardiff|belfast|nottingham|leicester|coventry|brighton"
    r"|cambridge|oxford|aberdeen|dundee|reading berkshire", re.I)
NL_LOC = re.compile(
    r"netherlands|nederland|amsterdam|rotterdam|utrecht|eindhoven|the hague"
    r"|den haag|hague|haarlem|delft|groningen|amersfoort|nijmegen|arnhem"
    r"|leiden|almere|breda|tilburg|zwolle|randstad|noord-holland|zuid-holland", re.I)
IE_LOC = re.compile(r"dublin|ireland|ierland", re.I)
IE_OTHER_CITY = re.compile(r"cork|galway|limerick|waterford", re.I)
# reject pure-remote and EMEA-wide postings that aren't anchored to a target city
REMOTE_ONLY = re.compile(r"\b(remote|anywhere|work from home|wfh|emea|europe)\b", re.I)

CLAUDE_SCREEN_MODEL = "claude-haiku-4-5"        # stage 1: cheap kill/keep
CLAUDE_SCORE_MODEL = "claude-sonnet-5"          # stage 2: deep weighted rubric
API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_VERSION = "2023-06-01"
KEEP_DAYS = 45
DESC_CHAR_CAP = 2200
MAX_SCREENED_PER_RUN = 80     # cap stage-1 Haiku calls
MAX_SCORED_PER_RUN = 30       # cap stage-2 Sonnet calls (survivors only)
SPONSOR_REQUIRED = False      # if True, drop UK/NL jobs whose company isn't on a register

# The full candidate profile (profile.md) drives the deep score. Loaded at runtime.
def load_profile():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "profile.md"), "profile.md"):
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
    return "Profile file missing."

# Weighted rubric from the job-application-workflow skill.
RUBRIC = [
    ("experience", "Experience Alignment", 25),
    ("skills", "Skills Match", 20),
    ("seniority", "Seniority Fit", 15),
    ("domain", "Domain / Industry Fit", 15),
    ("location_visa", "Location & Visa", 15),
    ("trajectory", "Career Trajectory", 10),
]

SCREEN_SYSTEM = """You are a fast pre-screen for a job-search pipeline. Decide if a role is worth a full evaluation for this candidate:

11 years B2B SaaS (Customer Success + Account Management), pivoting into Revenue Operations / GTM Strategy / Sales Ops / CS Ops at Manager or senior-IC level. Also open to Senior/Principal Customer Success Manager roles. US citizen needing EU visa sponsorship.

Target markets ONLY: Netherlands (anywhere), UK London area only, Ireland/Dublin. Reject Germany, Spain, remote-from-anywhere, and remote-EMEA roles.

KEEP if the role plausibly fits function AND market. KILL obvious no-fits: wrong function (deal desk, quote-to-cash, billing, pure marketing-ops admin, engineering, finance, quota-carrying AE/SDR), wrong seniority (intern, VP+, C-level), or wrong location (outside NL/London/Dublin, or remote-anywhere).

Be lenient at this stage - when unsure, keep it. The next stage does the real scoring.

Reply with ONLY this JSON: {"keep": true or false, "reason": "<max 12 words>"}"""


def score_system():
    dims = "\n".join(f"- {label}: {w}%" for _, label, w in RUBRIC)
    return f"""You deeply score a job posting against this candidate's real profile. Be rigorous and honest; this gates whether the candidate spends time applying.

CANDIDATE PROFILE:
{load_profile()}

SCORING RUBRIC - score each dimension 0-10, then a weighted total:
{dims}

Weighted total = sum(dimension_score * weight) / 100, on a 0-10 scale.

After the weighted total, apply these deterministic CAPS to the final score (take the lowest that applies):
- Location outside NL / London / Dublin, or remote-anywhere/EMEA: cap 2.
- Analyst or Specialist title: cap 5 (comp risk vs visa salary floor).
- Director+ or "Head of" title: cap 6 (stretch for a Manager/senior-IC target).
- Salary stated and below the market's visa floor: cap 4, and add a "below visa floor" flag.
- Wrong function (deal desk, quote-to-cash, billing, marketing-ops admin, quota-carrying sales): cap 4.
- CSM role in UK or Ireland at a non-standout company: cap 6 (see CSM track weighting in the profile).

Sponsor handling: a "sponsor" field may be given. "not on register" is a -1 to -2 caution (registers use legal names and miss trading names), NOT an auto-zero. "sponsor" or "sponsor (likely)" is a plus for UK/NL roles. Ignore sponsor for Ireland.

Salary: if not stated, do NOT penalize on salary; estimate comp risk from seniority/company and add a "comp not listed, verify vs floor" flag.

Calibration for the final score: 8-10 apply immediately, 7 strong fit, 6 borderline/volume play, 5 or below do not apply.

Reply with ONLY this JSON, nothing else:
{{"dimensions": {{"experience": <0-10>, "skills": <0-10>, "seniority": <0-10>, "domain": <0-10>, "location_visa": <0-10>, "trajectory": <0-10>}}, "score": <final 0-10, one decimal, after caps>, "tier": "<location tier label>", "flags": ["<risk flags, [] if none>"], "verdict": "<one blunt sentence, max 22 words>"}}"""

# ---------------------------------------------------------------- helpers

def now_iso(): return datetime.now(timezone.utc).isoformat()

def get(url, **kw):
    kw.setdefault("timeout", 30)
    kw.setdefault("headers", {"User-Agent": "Mozilla/5.0 (job-radar; personal use)"})
    return requests.get(url, **kw)

def strip_html(t):
    return re.sub(r"<[^>]+>", " ", t or "").replace("&amp;", "&").replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")

def location_ok(country, location):
    """Country-aware location gate. country is 'nl'/'gb'/'ie' when known, else ''."""
    loc = location or ""
    cc = (country or "").lower()
    if cc == "nl" or NL_LOC.search(loc):
        return True
    if cc == "ie" or IE_LOC.search(loc):
        # a non-Dublin Irish city (Cork/Galway/...) is out even if "Ireland" also appears
        if IE_OTHER_CITY.search(loc) and not re.search(r"dublin", loc, re.I):
            return False
        return True
    if cc == "gb" or UK_LONDON.search(loc) or UK_OTHER_CITY.search(loc):
        if UK_OTHER_CITY.search(loc) and not UK_LONDON.search(loc):
            return False        # a UK city that isn't London
        return bool(UK_LONDON.search(loc)) or (cc == "gb" and not loc)
    # ATS/community rows with no country: accept only if a target location shows,
    # and reject pure-remote / EMEA-wide with no target city.
    if NL_LOC.search(loc) or IE_LOC.search(loc) or UK_LONDON.search(loc):
        return not (IE_OTHER_CITY.search(loc) or UK_OTHER_CITY.search(loc))
    if REMOTE_ONLY.search(loc):
        return False
    return False

def prefilter(title, location, country=""):
    if not INCLUDE_TITLE.search(title or ""):
        return False
    if EXCLUDE_TITLE.search(title or ""):
        return False
    return location_ok(country, location)

# ---------------------------------------------------------------- Adzuna (NL + UK)

def fetch_adzuna(app_id, app_key, diag):
    out = []
    for cc, label in ADZUNA_COUNTRIES.items():
        got = 0
        for page in range(1, ADZUNA_PAGES + 1):
            try:
                r = get(f"https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}", params={
                    "app_id": app_id, "app_key": app_key,
                    "what_or": ADZUNA_WHAT_OR, "results_per_page": 50,
                    "max_days_old": ADZUNA_MAX_DAYS, "sort_by": "date",
                })
                if r.status_code != 200:
                    diag[f"adzuna:{cc}"] = f"HTTP {r.status_code}: {r.text[:120]}"
                    break
                results = r.json().get("results", [])
                if not results:
                    break
                for j in results:
                    title = j.get("title", "")
                    loc = (j.get("location") or {}).get("display_name", "")
                    if not prefilter(title, loc, cc):
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
                    got += 1
                time.sleep(0.3)
            except Exception as e:
                diag[f"adzuna:{cc}"] = f"error: {e}"
                break
        diag.setdefault(f"adzuna:{cc}", f"ok, {got} kept")
    return out

# ---------------------------------------------------------------- Reed (UK)

def fetch_reed(api_key):
    out = []
    r = requests.get("https://www.reed.co.uk/api/1.0/search",
                     params={"keywords": REED_KEYWORDS, "locationName": "London",
                             "distanceFromLocation": 25, "resultsToTake": 100},
                     auth=(api_key, ""), timeout=30,
                     headers={"User-Agent": "Mozilla/5.0 (job-radar; personal use)"})
    r.raise_for_status()
    for j in r.json().get("results", []):
        title = j.get("jobTitle", "")
        loc = j.get("locationName", "")
        if not prefilter(title, loc, "gb"):
            continue
        sal = ""
        if j.get("minimumSalary"):
            sal = f"{int(j['minimumSalary'])}-{int(j.get('maximumSalary') or j['minimumSalary'])} GBP"
        out.append({
            "id": f"reed-{j.get('jobId')}", "company": j.get("employerName", ""),
            "title": title, "location": loc or "London", "country": "gb",
            "url": j.get("jobUrl", ""), "source": "reed",
            "description": strip_html(j.get("jobDescription", "")), "salary": sal,
        })
    return out

# ---------------------------------------------------------------- JobSpy / Indeed (Ireland)

def fetch_jobspy_ireland():
    """Indeed via JobSpy for Dublin. Best-effort: import + scrape may fail on CI IPs."""
    from jobspy import scrape_jobs   # imported lazily so a missing dep can't break the run
    out, seen = [], set()
    for term in JOBSPY_TERMS:
        try:
            df = scrape_jobs(site_name=["indeed"], search_term=term,
                             location="Dublin, Ireland", results_wanted=20,
                             country_indeed="Ireland", hours_old=96)
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            title = str(row.get("title") or "")
            loc = str(row.get("location") or "Dublin")
            if not prefilter(title, loc, "ie"):
                continue
            jid = "js-" + re.sub(r"\W+", "-", str(row.get("job_url") or title))[-70:]
            if jid in seen:
                continue
            seen.add(jid)
            sal = ""
            if row.get("min_amount"):
                sal = f"{int(row['min_amount'])}-{int(row.get('max_amount') or row['min_amount'])} {row.get('currency') or 'EUR'}"
            out.append({
                "id": jid, "company": str(row.get("company") or ""),
                "title": title, "location": loc, "country": "ie",
                "url": str(row.get("job_url") or ""), "source": "indeed",
                "description": strip_html(str(row.get("description") or "")), "salary": sal,
            })
    return out

# ---------------------------------------------------------------- ATS supplements (companies.json)

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

def fetch_hiringcafe():
    """Best-effort. hiring.cafe's internal API blocks datacenter IPs and its shape
    shifts; wrapped so it can never break the run."""
    out = []
    payload = {"size": 40, "page": 0, "searchState": {
        "searchQuery": "revenue operations OR sales operations OR gtm OR customer success operations",
        "locations": [{"formatted_address": c, "types": ["country"]} for c in
                      ["Netherlands", "United Kingdom", "Ireland"]],
        "sortBy": "date"}}
    r = requests.post("https://hiring.cafe/api/search-jobs", json=payload, timeout=30,
                      headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
                               "Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"blocked ({r.status_code})")
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

def _extract_json(text):
    text = re.sub(r"```json|```", "", text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)

def _claude_call(api_key, model, system, user, max_tokens, extra=None):
    body = {"model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    if extra:
        body.update(extra)
    r = requests.post(API_URL, timeout=90, headers={
        "x-api-key": api_key, "anthropic-version": API_HEADERS_VERSION,
        "content-type": "application/json"}, json=body)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")

def job_message(job):
    desc = (job.get("description") or "")[:DESC_CHAR_CAP]
    return (f"Title: {job['title']}\nCompany: {job.get('company','?')}\n"
            f"Location: {job.get('location','?')}\n"
            + (f"Salary: {job['salary']}\n" if job.get("salary") else "")
            + (f"Sponsor: {job['sponsor']}\n" if job.get("sponsor") else "")
            + (f"Description: {desc}" if desc else "No description; judge on title/location/sponsor only."))

def screen_job(api_key, job):
    text = _claude_call(api_key, CLAUDE_SCREEN_MODEL, SCREEN_SYSTEM, job_message(job), 120)
    data = _extract_json(text)
    return bool(data.get("keep", True)), str(data.get("reason", ""))[:80]

def score_job(api_key, system, job):
    # Sonnet 5: thinking disabled keeps it a fast, deterministic structured scorer.
    text = _claude_call(api_key, CLAUDE_SCORE_MODEL, system, job_message(job), 900,
                        extra={"thinking": {"type": "disabled"}})
    data = _extract_json(text)
    dims = data.get("dimensions", {}) or {}
    return {
        "score": round(float(data.get("score", 0)), 1),
        "dimensions": {k: float(dims.get(k, 0)) for k, _, _ in RUBRIC},
        "tier": str(data.get("tier", ""))[:40],
        "flags": [str(f)[:70] for f in data.get("flags", [])][:6],
        "verdict": str(data.get("verdict", ""))[:180],
    }

# ---------------------------------------------------------------- main

def main():
    verify, dry = "--verify" in sys.argv, "--dry" in sys.argv
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)
    companies = json.load(open("companies.json")).get("companies", []) if os.path.exists("companies.json") else []

    if verify:
        print("Verifying optional ATS slugs...")
        for c in companies:
            try:
                n = len(ATS[c["ats"]](c["name"], c["slug"]))
                print(f"  OK   {c['name']:<20} matched {n}")
            except Exception as e:
                print(f"  FAIL {c['name']:<20} {e}")
        return

    os.makedirs("docs", exist_ok=True)
    seen = set(json.load(open("seen.json"))) if os.path.exists("seen.json") else set()
    existing = [j for j in (json.load(open("docs/jobs.json")) if os.path.exists("docs/jobs.json") else [])
                if not str(j.get("id", "")).startswith("demo-")]
    src_status, diag, found = {}, {}, []

    # 1. Adzuna (NL + UK)
    aid, akey = os.environ.get("ADZUNA_APP_ID", ""), os.environ.get("ADZUNA_APP_KEY", "")
    if aid and akey:
        try:
            jobs = fetch_adzuna(aid, akey, diag); found += jobs
            src_status["Adzuna (NL+UK)"] = f"ok ({len(jobs)}) | " + "; ".join(f"{k.split(':')[1]}={v}" for k, v in diag.items() if k.startswith("adzuna:"))
        except Exception as e:
            src_status["Adzuna (NL+UK)"] = f"FAIL: {e}"
    else:
        src_status["Adzuna (NL+UK)"] = "skipped: no ADZUNA_APP_ID/KEY set"

    # 2. Reed (UK)
    reed_key = os.environ.get("REED_API_KEY", "")
    if reed_key:
        try:
            jobs = fetch_reed(reed_key); found += jobs
            src_status["Reed (UK)"] = f"ok ({len(jobs)})"
        except Exception as e:
            src_status["Reed (UK)"] = f"FAIL: {e}"
    else:
        src_status["Reed (UK)"] = "skipped: no REED_API_KEY set"

    # 3. JobSpy / Indeed (Ireland)
    try:
        jobs = fetch_jobspy_ireland(); found += jobs
        src_status["Indeed/JobSpy (Dublin)"] = f"ok ({len(jobs)})"
    except Exception as e:
        src_status["Indeed/JobSpy (Dublin)"] = f"skipped: {e}"

    # 4. Company ATS feeds (Greenhouse/Lever/Ashby)
    ats_n = 0
    for c in companies:
        try:
            jobs = ATS[c["ats"]](c["name"], c["slug"]); found += jobs; ats_n += len(jobs)
        except Exception:
            pass
    src_status[f"Company ATS ({len(companies)} watched)"] = f"ok ({ats_n})"

    # 5. hiring.cafe (best-effort)
    try:
        jobs = fetch_hiringcafe(); found += jobs; src_status["hiring.cafe"] = f"ok ({len(jobs)})"
    except Exception as e:
        src_status["hiring.cafe"] = f"skipped: {e}"

    new_jobs = [j for j in found if j["id"] not in seen]
    print(f"Fetched {len(found)} relevant, {len(new_jobs)} new.")

    # 6. sponsor registers (load once)
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

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    system_score = score_system()
    scored, kept, killed = [], 0, 0

    for j in new_jobs[:MAX_SCREENED_PER_RUN]:
        # fill ATS descriptions before any scoring
        if j.get("_detail") and not j.get("description"):
            j["description"] = greenhouse_desc(j.pop("_detail"))
        j.pop("_detail", None)

        which, raw, label = sponsor_for(j)
        j["sponsor_region"], j["sponsor_raw"], j["sponsor"] = which or "", raw, label
        if SPONSOR_REQUIRED and raw == "not_found":
            seen.add(j["id"]); continue

        if dry or not api_key:
            j.update({"score": 0, "dimensions": {}, "tier": "", "flags": [], "verdict": "(not scored)"})
            j["found_at"] = now_iso(); j["description"] = (j.get("description") or "")[:400]
            scored.append(j); seen.add(j["id"]); continue

        # STAGE 1: cheap screen
        try:
            keep, reason = screen_job(api_key, j)
        except Exception:
            keep, reason = True, "screen error, passed through"
        if not keep:
            killed += 1; seen.add(j["id"])
            print(f"  kill  {j['title']} @ {j.get('company') or j['source']} ({reason})")
            continue
        kept += 1

        # STAGE 2: deep score (only survivors, capped)
        if len(scored) >= MAX_SCORED_PER_RUN:
            break
        try:
            j.update(score_job(api_key, system_score, j))
        except Exception as e:
            j.update({"score": 0, "dimensions": {}, "tier": "", "flags": [], "verdict": f"scoring failed: {e}"})
        j["description"] = (j.get("description") or "")[:400]
        j["found_at"] = now_iso()
        scored.append(j); seen.add(j["id"])
        print(f"  [{j.get('score','-')}] {j['title']} @ {j.get('company') or j['source']} | {j['sponsor'] or 'n/a'}")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    merged = scored + [j for j in existing if j.get("found_at", "") >= cutoff]
    merged.sort(key=lambda j: (j.get("score", 0), j.get("found_at", "")), reverse=True)

    src_status["screening"] = f"stage1 kept {kept}, killed {killed}; stage2 scored {len(scored)}"

    json.dump(sorted(seen), open("seen.json", "w"))
    json.dump(merged, open("docs/jobs.json", "w"), indent=1)
    json.dump({"last_run": now_iso(), "new_this_run": len(scored), "sources": src_status},
              open("docs/status.json", "w"), indent=1)
    print(f"Done. {len(scored)} new on dashboard, {len(merged)} total.")

if __name__ == "__main__":
    main()
