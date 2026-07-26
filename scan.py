#!/usr/bin/env python3
"""
RevOps Radar - daily job scanner for Tom Norton.

Markets: Netherlands (anywhere), Belgium (anywhere), UK (London area only), Ireland (Dublin).
Germany, Spain, and remote-anywhere/EMEA are deliberately excluded.

Data layer (multi-source so no single source can break the run):
  - Adzuna API      : NL + UK (no Ireland coverage in the Adzuna API)
  - Reed API        : UK depth (free key, https://www.reed.co.uk/developers)
  - JobSpy / Indeed : Dublin/Ireland coverage (the Adzuna gap)
  - Company ATS      : Greenhouse / Lever / Ashby for named companies (clean names,
                       full descriptions, strong sponsor matching) - companies.json
  - hiring.cafe      : via the Apify actor memo23/apify-hiring-cafe-scraper, run
                       against Tom's saved hiring.cafe searches (the direct API
                       blocks datacenter IPs, so this replaced that attempt)
  - LinkedIn         : mirrors Tom's "Jobs based on your preferences" search via
                       LinkedIn's public, unauthenticated guest job-search endpoint
                       (same keywords/geoId/24h filter that page itself uses) -
                       no login or session cookie involved
  - revopsroles.com  : per-country location pages (robots.txt allows these plain
                       paths but disallows the ?location_country= query form)

Pipeline:
  fetch -> title/location prefilter (free) -> age filter -> dedupe
        -> UK/NL sponsor-register check
        -> STAGE 1 cheap screen (Claude Haiku, kill/keep)
        -> STAGE 2 deep score (Claude Opus, dimension scores vs profile.md)
        -> weighted total + deterministic caps, computed here in Python
        -> write docs/jobs.json + docs/status.json + docs/excluded.json

Scoring split: the model scores the six rubric dimensions and reports facts it can only get
by reading the posting (stated salary, hard language requirement, function match, whether
the employer is a standout). Every piece of arithmetic and every cap is computed in
apply_caps() below, so the score is reproducible and the reason for it is recorded.

Every row the pipeline rejects is logged to docs/excluded.json with the stage and reason,
and shown in a collapsed section on the dashboard.

Usage:
  python scan.py             normal run
  python scan.py --dry       everything except the Claude calls
  python scan.py --verify    test the optional ATS company slugs, no scoring
  python scan.py --selftest  replay stored dimension scores through the cap engine, offline
  python scan.py --unkill    free stage-1 kills from seen.json so they get re-evaluated
  python scan.py --rescore   clear scored rows so the corpus re-runs under the current engine
"""

import html, json, os, re, sys, time
from datetime import datetime, timezone, timedelta
import requests
import sponsors as spon

# ---------------------------------------------------------------- config

# Adzuna covers NL + UK only (its API has no Ireland). Ireland comes from JobSpy + ATS.
ADZUNA_COUNTRIES = {"nl": "Netherlands", "gb": "United Kingdom"}
# Targeted phrase queries. A broad word-OR query sorted by date just surfaces the
# freshest generic "operations/customer/revenue" noise, none of which passes the title
# filter (that was the original "Adzuna returned nothing" bug). Precise phrases return
# on-target roles. Each phrase is sent as Adzuna `what` (matches the words together).
ADZUNA_PHRASES = [
    "revenue operations", "sales operations", "revenue strategy", "sales strategy",
    "go to market strategy", "revenue enablement", "customer success operations",
    "business operations manager", "commercial operations",
]
ADZUNA_MAX_DAYS = 7      # Tom doesn't want postings older than a week
ADZUNA_PER_PAGE = 30     # per phrase
ADZUNA_PAGES = 2         # pages per phrase; page 1 alone capped the feed at 540 results/run

# The literal OR-keyword search string. Reed and LinkedIn both take a single free-text
# query, and both want the same terms -- one constant so they can't drift apart.
OR_KEYWORDS = ("revenue operations OR sales operations OR gtm OR go-to-market OR "
               "revenue strategy OR sales strategy OR revenue enablement OR "
               "customer success operations OR business operations")

# Reed (UK). Free key acts as the HTTP basic-auth username, blank password.
REED_KEYWORDS = OR_KEYWORDS

# JobSpy / Indeed for Ireland (Dublin). Best-effort: never breaks the run.
JOBSPY_TERMS = ["revenue operations", "sales operations", "gtm strategy",
                "revenue strategy", "customer success operations"]

# LinkedIn "Jobs based on your preferences" is personalized off account-level preference
# data, not a literal text search -- replaying that natural-language phrase as a keyword
# query against the guest endpoint matched nothing (verified). REED_KEYWORDS-style OR
# terms work as an actual literal search, so that's what's used here instead.
# geoIds captured from Tom's preferences page URL: London Area UK, Belgium, Netherlands,
# Amsterdam, Ireland. Queried per-geoId: the guest endpoint doesn't paginate correctly
# when multiple geoIds are combined into one request, but works fine one market at a
# time. Belgium/Amsterdam results still pass through the same location_ok() gate as
# every other source, so anything outside NL/UK-London/Dublin gets filtered downstream.
LINKEDIN_KEYWORDS = OR_KEYWORDS
LINKEDIN_GEO_IDS = ["90009496", "100565514", "102890719", "103100785", "104738515"]
LINKEDIN_PAGES = 3       # 10 results/page per market

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
# City/region signals are kept apart from bare country signals. A named city anchors a
# posting to a market even when the text also says "remote" ("Amsterdam, remote-friendly"
# is a real Amsterdam job); a bare country name next to "remote" or "EMEA" does not
# ("Ireland or Europe" on an EMEA req is the remote-EMEA posting profile.md rejects).
NL_CITY = re.compile(
    r"amsterdam|rotterdam|utrecht|eindhoven|the hague"
    r"|den haag|hague|haarlem|delft|groningen|amersfoort|nijmegen|arnhem"
    r"|leiden|almere|breda|tilburg|zwolle|randstad|noord-holland|zuid-holland", re.I)
NL_COUNTRY = re.compile(r"netherlands|nederland", re.I)
BE_CITY = re.compile(
    r"brussels|bruxelles|brussel|antwerp|antwerpen|ghent|gent"
    r"|bruges|brugge|leuven|louvain|liege|luik|namur|mechelen|kortrijk|flemish|wallonia"
    r"|flanders", re.I)
BE_COUNTRY = re.compile(r"belgium|belgie|belgique", re.I)
IE_CITY = re.compile(r"dublin", re.I)
IE_COUNTRY = re.compile(r"ireland|ierland", re.I)
IE_OTHER_CITY = re.compile(r"cork|galway|limerick|waterford", re.I)
# reject pure-remote and EMEA-wide postings that aren't anchored to a target city
REMOTE_ONLY = re.compile(r"\b(remote|anywhere|work from home|wfh|emea|europe)\b", re.I)

CLAUDE_SCREEN_MODEL = "claude-haiku-4-5"        # stage 1: cheap kill/keep
CLAUDE_SCORE_MODEL = "claude-opus-5"            # stage 2: deep weighted rubric
API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_VERSION = "2023-06-01"
# Opus 5 thinks by default and max_tokens caps thinking + response text together, so this
# needs real headroom -- the old 900 would truncate mid-answer. Effort is the cost dial.
SCORE_MAX_TOKENS = 4000
SCORE_EFFORT = "medium"       # low | medium | high | xhigh | max
CLAUDE_ATTEMPTS = 3           # per call, with exponential backoff on 429/5xx/timeout

NTFY_TOPIC = "tom-revops-radar-c16aabb2"   # push notifications for strong matches (ntfy.sh)
NTFY_SCORE_THRESHOLD = 7.5
KEEP_DAYS = 45
MAX_POST_AGE_DAYS = 7    # drop postings older than this when the source gives us a date
DESC_CHAR_CAP = 2200
MAX_SCREENED_PER_RUN = 80     # cap stage-1 Haiku calls
MAX_SCORED_PER_RUN = 30       # cap stage-2 Opus calls (survivors only)
SPONSOR_REQUIRED = False      # if True, drop UK/NL jobs whose company isn't on a register

# Dashboard bands. Mirrored in docs/index.html and written into docs/status.json each run
# so the page reads them from here rather than keeping its own copy.
GATE = 6.0     # 6.0+ -> "apply" section
FLOOR = 5.0    # 5.0-5.9 -> collapsed "borderline"; below -> collapsed "excluded"

# Annual base-salary floors per market, local currency (2026). The prose version lives in
# profile.md for the model to reason with; this is the copy the below-floor cap uses.
# Belgium takes the lowest of the three regional Blue Card floors (Brussels) so the cap
# only fires when the salary is below every Belgian threshold.
VISA_FLOORS = {
    "NL": (71304, "EUR"),          # HSM, 30+ bracket
    "BE": (56976, "EUR"),          # EU Blue Card, Brussels
    "UK-London": (70000, "GBP"),   # Skilled Worker
    "IE-Dublin": (68911, "EUR"),   # Critical Skills / General permit
}

# Title intelligence from the job-application-workflow skill, as code. First match wins,
# so the most disqualifying bands are checked first.
TITLE_BANDS = [
    ("wrong_function", re.compile(r"deal desk|quote[- ]to[- ]cash|order management"
                                 r"|billing|accounts (payable|receivable)", re.I)),
    ("director_plus", re.compile(r"\bdirector\b|head of|\bvp\b|vice president|chief "
                                 r"|\bsvp\b|\bevp\b|\bcro\b|\bcoo\b", re.I)),
    ("analyst", re.compile(r"\banalyst\b", re.I)),
    ("specialist", re.compile(r"\bspecialist\b|\bcoordinator\b", re.I)),
]

# Deterministic score ceilings. Lowest applicable cap wins.
CAPS = {
    "location": 2.0,               # outside NL / BE / London / Dublin
    "analyst": 5.0,                # comp risk vs visa salary floor
    "specialist": 5.0,
    "director_plus": 6.0,          # stretch for a Manager/senior-IC target
    "below_floor": 4.0,            # stated salary under the market's visa floor
    "wrong_function": 4.0,         # deal desk / billing / quota-carrying sales
    "csm_secondary_market": 6.0,   # CSM outside NL at a non-standout company
    "language": 3.0,               # hard non-English fluency requirement
}

# The full candidate profile (profile.md) drives the deep score. Loaded at runtime.
def load_profile():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "profile.md"), "profile.md"):
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
    return "Profile file missing."

# Weighted rubric from the job-application-workflow skill. The fourth element is that
# skill's "Evaluate" guidance, passed to the model so each dimension is judged on the same
# criteria the skill uses rather than on the label alone.
RUBRIC = [
    ("experience", "Experience Alignment", 25,
     "11 years B2B SaaS (CSM + AM) vs the requirements. Distinguish hard prerequisite gaps "
     "from soft gaps that the adjacent experience plus the MBA cover."),
    ("skills", "Skills Match", 20,
     "Judge required tools/certs (Salesforce, SQL, BI) and functional skills (territory "
     "planning, quota modelling, pipeline analysis) separately."),
    ("seniority", "Seniority Fit", 15,
     "Manager is the target zone for this pivot. Too senior (Director/Head of, or 10+ years "
     "of dedicated RevOps required) penalises hard. Analyst/Specialist is a double penalty: "
     "overqualification plus comp below the visa floor. Senior Manager fits only when the "
     "posting explicitly welcomes adjacent backgrounds. Associate/IC framing at a tier-1 "
     "employer is viable if comp clears the floor."),
    ("domain", "Domain / Industry Fit", 15,
     "B2B SaaS or tech is strong (8-10). GRC/compliance/legal/regulatory adds a familiarity "
     "bonus but is not required for a high score. Non-tech, non-SaaS (manufacturing, retail, "
     "government) is weaker (4-6)."),
    ("location_visa", "Location & Visa", 15,
     "The market has already been resolved in code and is given to you; score sponsorship "
     "realism and comp certainty within it, not whether the location qualifies. Netherlands "
     "is the primary market; Dublin is structurally safest on salary thresholds; London is a "
     "deep RevOps market with strong comp."),
    ("trajectory", "Career Trajectory", 10,
     "Does the role advance the RevOps/GTM pivot? Pure CS maintenance is a penalty -- except "
     "that a strong Senior/Principal CSM role in the Netherlands is a primary target and is "
     "not penalised as lateral."),
]
RUBRIC_KEYS = [k for k, _, _, _ in RUBRIC]

# ---------------------------------------------------------------- deterministic scoring
# Everything below is computed in code. The model supplies the six dimension scores and a
# handful of facts it can only get by reading the posting; the arithmetic and every cap
# happen here. Previously score_job() just trusted whatever total the model reported, and
# on the 51 stored rows 21 of those totals were more than 0.6 off the weighted sum of
# their own dimensions -- the worst by 3.8.

def weighted_total(dims):
    """sum(dimension * weight) / 100, on a 0-10 scale."""
    total = sum(float(dims.get(k, 0) or 0) * w for k, _, w, _ in RUBRIC) / 100.0
    return max(0.0, min(10.0, total))

def title_band(title):
    for band, rx in TITLE_BANDS:
        if rx.search(title or ""):
            return band
    return "normal"

def is_csm_title(title):
    return bool(re.search(r"customer success", title or "", re.I))

def below_visa_floor(market, obs):
    """(True, note) only when a salary is actually stated, in the market's own currency,
    and below its floor. No FX guessing: a GBP figure is never compared to a EUR floor."""
    if not market or not obs.get("salary_stated"):
        return False, ""
    try:
        low = float(obs.get("salary_min_base") or 0)
    except (TypeError, ValueError):
        return False, ""
    floor, cur = VISA_FLOORS.get(market, (0, ""))
    if low <= 0 or not floor:
        return False, ""
    stated = (obs.get("salary_currency") or "").upper()
    if stated and stated != cur:
        return False, ""
    if low < floor:
        return True, f"stated salary {int(low)} {cur} below the {market} visa floor ({floor} {cur})"
    return False, ""

def apply_caps(weighted, job, obs):
    """Apply the rubric's deterministic ceilings; the lowest applicable cap wins.
    Returns (score, caps_applied). Each cap is decided from a regex over the title, the
    market the location gate already resolved, or one factual observation the model
    reported -- never from the model's own arithmetic."""
    caps = []
    market = job.get("market")
    title = job.get("title")

    if market is None:
        caps.append(("location outside target markets", CAPS["location"]))

    band = title_band(title)
    if band in ("analyst", "specialist"):
        caps.append((f"{band} title (comp risk vs visa floor)", CAPS[band]))
    elif band == "director_plus":
        caps.append(("Director+/Head-of title (stretch vs Manager target)", CAPS["director_plus"]))
    elif band == "wrong_function":
        caps.append(("off-target function (title)", CAPS["wrong_function"]))

    if obs.get("function_match") == "off_target":
        caps.append(("off-target function (description)", CAPS["wrong_function"]))

    if obs.get("language_hard_requirement"):
        caps.append(("requires non-English fluency", CAPS["language"]))

    low, note = below_visa_floor(market, obs)
    if low:
        caps.append((note, CAPS["below_floor"]))

    # CSM track: a primary target in NL, a modest one elsewhere unless the company is a
    # genuine standout (see the CSM track weighting section of profile.md).
    if market in ("UK-London", "IE-Dublin", "BE") and is_csm_title(title) \
            and not obs.get("company_standout"):
        caps.append((f"CSM in {market} at a non-standout company", CAPS["csm_secondary_market"]))

    if not caps:
        return round(weighted, 1), []
    return round(min(weighted, min(c[1] for c in caps)), 1), [c[0] for c in caps]

# JSON schema for the deep score. Replaces "reply with ONLY this JSON" plus a regex that
# scraped {.*} out of the response -- one row in the corpus is permanently stuck at score 0
# because that salvage failed on a stray delimiter.
SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "dimensions": {
            "type": "object",
            "properties": {k: {"type": "number"} for k in RUBRIC_KEYS},
            "required": RUBRIC_KEYS,
            "additionalProperties": False,
        },
        "function_match": {"type": "string", "enum": ["core", "adjacent", "off_target"]},
        "company_standout": {"type": "boolean"},
        "language_hard_requirement": {"type": "boolean"},
        "salary_stated": {"type": "boolean"},
        "salary_min_base": {"type": "number"},
        "salary_currency": {"type": "string"},
        "flags": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string"},
    },
    "required": ["dimensions", "function_match", "company_standout",
                 "language_hard_requirement", "salary_stated", "salary_min_base",
                 "salary_currency", "flags", "verdict"],
    "additionalProperties": False,
}

# The one market list, used by both prompts. Kept next to VISA_FLOORS so adding a market
# updates the floors, the caps, and both prompts together -- the old hardcoded copy in the
# screen prompt had already drifted (it listed Belgium as a target but omitted it from the
# reject clause).
MARKETS_SENTENCE = ("Netherlands (anywhere), Belgium (anywhere), UK London area and commuter "
                    "belt only, Ireland/Dublin")
REJECT_SENTENCE = ("Germany, Spain, other UK cities, non-Dublin Ireland, remote-from-anywhere, "
                   "and remote-EMEA roles")

SCREEN_SYSTEM = f"""You are a fast pre-screen for a job-search pipeline. Decide if a role is worth a full evaluation for this candidate:

11 years B2B SaaS (Customer Success + Account Management), pivoting into Revenue Operations / GTM Strategy / Sales Ops / CS Ops at Manager or senior-IC level. Also open to Senior/Principal Customer Success Manager roles. US citizen needing EU visa sponsorship.

Target markets ONLY: {MARKETS_SENTENCE}. Reject {REJECT_SENTENCE}.

KEEP if the role plausibly fits function AND market. KILL obvious no-fits: wrong function (deal desk, quote-to-cash, billing, pure marketing-ops admin, engineering, finance, quota-carrying AE/SDR), wrong seniority (intern, VP+, C-level), or wrong location (outside the target markets above).

Be lenient at this stage - when unsure, keep it. The next stage does the real scoring.

Reply with ONLY this JSON: {{"keep": true or false, "reason": "<max 12 words>"}}"""


def score_system():
    dims = "\n\n".join(f"- {label} ({w}%): {guide}" for _, label, w, guide in RUBRIC)
    return f"""You deeply score a job posting against this candidate's real profile. Be rigorous and honest; this gates whether the candidate spends time applying.

CANDIDATE PROFILE:
{load_profile()}

SCORING RUBRIC - score each dimension 0-10 against the guidance given:

{dims}

Calibration, so the dimension scores land on a consistent scale: 8-10 is a bullseye worth applying to immediately, 7 a strong fit with manageable gaps, 6 borderline and worth it only when the pipeline is thin, 5 barely at the bar, 4 and below not worth applying to. Do not inflate to be encouraging.

Do NOT compute a total, and do NOT apply any caps or ceilings. The weighted total and every deterministic cap (title band, location, salary floor, language requirement, CSM track) are computed in code from the facts you report below. Score each dimension on its own merits and report the facts accurately; adjusting a dimension downward to "pre-apply" a cap would double-count it.

Alongside the dimensions, report these observations from the posting:
- function_match: "core" for RevOps / GTM strategy / sales ops / CS ops / revenue or sales strategy, or a Senior/Principal CSM role. "adjacent" for a related commercial-ops role that isn't quite one of those. "off_target" for deal desk, quote-to-cash, billing, pure marketing-ops admin, quota-carrying sales, engineering, or finance.
- company_standout: true only if the employer is a genuine tier-1 SaaS or strong-brand technology company. This decides whether a CSM role outside the Netherlands is capped.
- language_hard_requirement: true only when the posting makes another language (Dutch, German, French, ...) a hard requirement to do the job -- "fluency required", "must speak", "native/business-level X required". False when it is merely preferred, a plus, advantageous, or nice to have.
- salary_stated / salary_min_base / salary_currency: the annual base-salary floor of any stated range, as a number, with its ISO currency code. Report the base only -- exclude bonus, commission, equity, and holiday allowance. If no salary is stated, set salary_stated false, salary_min_base 0, salary_currency "".

The market (Netherlands / Belgium / UK-London / Ireland-Dublin) has already been resolved in code and is given to you in the job details. Trust it. Do not second-guess whether the location qualifies, and do not penalise a location that has been accepted.

Sponsor handling: a "sponsor" field may be given. "not on register" is a -1 to -2 caution on Location & Visa (registers use legal names and miss trading names), NOT an auto-zero. "sponsor" or "sponsor (likely)" is a plus for UK/NL roles. Ignore sponsor for Ireland.

Salary: if not stated, do NOT penalise on salary; judge comp risk from the seniority and the company.

flags: short risk notes, [] if none. Do not add a flag for missing comp or for a cap -- those are added in code.
verdict: one blunt sentence, max 22 words."""

# ---------------------------------------------------------------- helpers

def now_iso(): return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------- drop log
# Every row this run rejects, so the dashboard can show what was thrown away and why.
# Before this existed, prefilter/location/age/dedupe rejects vanished in memory and a
# stage-1 kill left nothing behind but a line in the Actions log.
DROPS = []
DROP_COUNTS = {}
RAW_COUNTS = {}
# Title rejects are the highest-volume, lowest-signal stage (hundreds per run), so only a
# sample is stored. Counts stay exact for every stage regardless.
DROP_SAMPLE_CAP = {"prefilter": 60}
DROP_MAX_ROWS = 400
# How many rows of each stage to keep in the committed file, newest first. Stage-1 kills and
# scoring errors are the ones worth actually reading, so they get the most room.
DROP_KEEP_PER_STAGE = {"prefilter": 80, "age": 40, "dedupe": 40,
                       "stage1-kill": 120, "score-error": 40, "sponsor-required": 40}
DROP_KEEP_DEFAULT = 40

_DROP_SEEN = set()

def record_drop(job, stage, reason):
    # A prefilter drop is counted as title-vs-location, so the footer says which half of the
    # gate is doing the work rather than lumping thousands of rows under one number.
    if stage == "prefilter":
        stage = "prefilter:" + reason.split(":", 1)[0]
    DROP_COUNTS[stage] = DROP_COUNTS.get(stage, 0) + 1
    group = stage.split(":")[0]
    cap = DROP_SAMPLE_CAP.get(group)
    if cap is not None:
        # Sample for variety, not volume: 60 rows all reading "Account Manager / no
        # target-function keyword" tell you nothing, so only the first of each
        # title+reason pair is stored.
        fingerprint = (stage, (job.get("title") or "").lower(), reason)
        if fingerprint in _DROP_SEEN:
            return
        if sum(1 for d in DROPS if d["stage"].split(":")[0] == group) >= cap:
            return
        _DROP_SEEN.add(fingerprint)
    DROPS.append({
        "id": str(job.get("id", "")), "title": job.get("title", ""),
        "company": job.get("company", ""), "location": job.get("location", ""),
        "source": job.get("source", ""), "stage": stage, "reason": reason,
        "dropped_at": now_iso(),
    })

def bump_raw(source, n):
    """Count rows a source returned before filtering, so a regex change that silently
    zeroes a source looks different from a genuinely quiet week."""
    RAW_COUNTS[source] = RAW_COUNTS.get(source, 0) + n

def src_line(source, kept):
    raw = RAW_COUNTS.get(source)
    return f"ok (raw {raw} -> kept {kept})" if raw is not None else f"ok ({kept})"

# Token usage across the run, surfaced in the status footer. cache_read staying at 0 across
# a multi-job run means something volatile is leaking into the cached system prefix.
USAGE = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}

def note_usage(u):
    USAGE["in"] += u.get("input_tokens", 0) or 0
    USAGE["out"] += u.get("output_tokens", 0) or 0
    USAGE["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
    USAGE["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0

def notify_strong_matches(jobs):
    """Push a notification via ntfy.sh (free, no signup) for anything scoring high
    enough this run. Never lets a notification failure affect the scan itself."""
    strong = [j for j in jobs if (j.get("score") or 0) >= NTFY_SCORE_THRESHOLD]
    if not strong:
        return
    strong.sort(key=lambda j: j.get("score", 0), reverse=True)
    body = "\n".join(f"{j['score']} — {j['title']} @ {j.get('company') or j['source']}"
                     for j in strong[:10])
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"), timeout=15,
                      headers={"Title": f"{len(strong)} strong RevOps Radar match" + ("es" if len(strong) != 1 else ""),
                               "Priority": "high", "Tags": "briefcase"})
    except Exception:
        pass

def get(url, **kw):
    kw.setdefault("timeout", 30)
    kw.setdefault("headers", {"User-Agent": "Mozilla/5.0 (job-radar; personal use)"})
    return requests.get(url, **kw)

def strip_html(t):
    return re.sub(r"<[^>]+>", " ", t or "").replace("&amp;", "&").replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")

def clean_text(t):
    """Decode HTML entities and collapse whitespace in a short scraped field. Titles come
    off HTML pages still carrying entities -- one stored title reads "Revenue Operations
    &amp; Systems" -- which then reach the model and the dashboard verbatim."""
    return re.sub(r"\s+", " ", html.unescape(str(t or ""))).strip()

# Sources disagree on how they name a country: Adzuna sends ISO codes, revopsroles sends
# display names. Normalise to the ISO code the rest of the pipeline expects.
COUNTRY_CODES = {
    "nl": "nl", "netherlands": "nl", "the netherlands": "nl", "holland": "nl",
    "be": "be", "belgium": "be", "belgie": "be", "belgique": "be",
    "gb": "gb", "uk": "gb", "united kingdom": "gb", "great britain": "gb", "england": "gb",
    "ie": "ie", "ireland": "ie",
}

def country_code(v):
    return COUNTRY_CODES.get((v or "").strip().lower(), "")

# ---------------------------------------------------------------- cross-source dedupe key
COMPANY_SUFFIX = re.compile(r"\b(inc|ltd|llc|bv|gmbh|corp|co|plc|nv)\b\.?", re.I)
# One bucket per city. These used to share a single bucket per regex alternation group,
# because the code took a slice of the *pattern* string rather than the matched text -- so
# "amster" stood in for every Dutch city and the same role in Amsterdam and in Rotterdam
# collapsed into one dashboard entry.
DEDUPE_CITY = re.compile(r"amsterdam|rotterdam|utrecht|eindhoven|den haag|the hague|hague"
                         r"|dublin|london|brussels|antwerp|ghent", re.I)
CITY_ALIAS = {"the hague": "denhaag", "den haag": "denhaag", "hague": "denhaag"}

def norm_company(s):
    return re.sub(r"\s+", "", COMPANY_SUFFIX.sub("", re.sub(r"[^a-z0-9 ]", "", (s or "").lower())))

def dkey(j):
    """(company, title, city) identity for collapsing the same role found twice. All three
    parts must be non-empty before it's used to dedupe, so a row with no company never
    swallows unrelated rows."""
    m = DEDUPE_CITY.search(j.get("location") or "")
    city = CITY_ALIAS.get(m.group(0).lower(), m.group(0).lower()) if m else ""
    return (norm_company(j.get("company")),
            re.sub(r"[^a-z0-9]", "", (j.get("title") or "").lower())[:40], city)

def market_of(country, location):
    """Which target market a row belongs to ('NL' / 'BE' / 'UK-London' / 'IE-Dublin'),
    or None if it's outside all of them. This is the single source of truth for location:
    location_ok() and the deep score's location cap both read it, so the code and the
    rubric can no longer disagree (a Staines role used to pass the gate here and then get
    capped at 2 by the model for being 'outside the London commuter belt')."""
    loc = location or ""
    cc = (country or "").lower()

    # An explicitly named city we don't want, with no target city alongside it, is out --
    # checked first so "Cork, Ireland" and "Cambridge, UK" can't slip through on the
    # strength of the country half of the string.
    if IE_OTHER_CITY.search(loc) and not IE_CITY.search(loc):
        return None
    if UK_OTHER_CITY.search(loc) and not UK_LONDON.search(loc):
        return None

    # A named city or region anchors the posting, remote wording notwithstanding.
    if NL_CITY.search(loc):
        return "NL"
    if BE_CITY.search(loc):
        return "BE"
    if IE_CITY.search(loc):
        return "IE-Dublin"
    if UK_LONDON.search(loc):
        return "UK-London"

    # No city named. A remote-anywhere/EMEA-wide posting isn't anchored to a target market,
    # so it's out. This check used to sit on the country-less path only, which meant an
    # Adzuna 'nl'/'gb' row or any 'be' row skipped it entirely.
    if REMOTE_ONLY.search(loc):
        return None

    # Country named but no city: acceptable, since plenty of Dublin/Amsterdam postings list
    # only the country.
    if NL_COUNTRY.search(loc):
        return "NL"
    if BE_COUNTRY.search(loc):
        return "BE"
    if IE_COUNTRY.search(loc):
        return "IE-Dublin"

    # Fall back to the source's own country field. The NL/BE/IE feeds are country-scoped so
    # the code alone is enough. A GB feed spans the whole UK, so an unrecognised UK location
    # is not assumed to be London -- only a bare/empty one is.
    if cc in ("nl", "be", "ie"):
        return {"nl": "NL", "be": "BE", "ie": "IE-Dublin"}[cc]
    if cc == "gb" and not loc:
        return "UK-London"
    return None

def location_ok(country, location):
    return market_of(country, location) is not None

def prefilter(title, location, country=""):
    """None if the row passes the free filters; otherwise a short reason for the drop log."""
    t = title or ""
    if not INCLUDE_TITLE.search(t):
        return "title: no target-function keyword"
    m = EXCLUDE_TITLE.search(t)
    if m:
        return f"title: excluded term '{m.group(0).strip()}'"
    if market_of(country, location) is None:
        return f"location: outside target markets ({location or 'unspecified'})"
    return None

def parse_date_loose(v):
    """Best-effort parse of a source's posted-date value. Returns an aware datetime or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=timezone.utc)
        except Exception:
            return None
    s = str(v).strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)   # Reed: dd/mm/yyyy
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except Exception:
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def recent_enough(posted_raw, max_days=MAX_POST_AGE_DAYS):
    """True if within max_days old. Fails open (keeps the job) when the source gives no
    usable date at all, so a missing field never silently wipes out a whole source."""
    dt = parse_date_loose(posted_raw)
    if dt is None:
        return True
    return (datetime.now(timezone.utc) - dt) <= timedelta(days=max_days)

# ---------------------------------------------------------------- Adzuna (NL + UK)

def fetch_adzuna(app_id, app_key, diag):
    out, ids = [], set()
    for cc, label in ADZUNA_COUNTRIES.items():
        raw = kept = 0
        err = None
        for phrase in ADZUNA_PHRASES:
            for page in range(1, ADZUNA_PAGES + 1):
                try:
                    r = get(f"https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}", params={
                        "app_id": app_id, "app_key": app_key,
                        "what": phrase, "results_per_page": ADZUNA_PER_PAGE,
                        "max_days_old": ADZUNA_MAX_DAYS, "sort_by": "date",
                    })
                    if r.status_code != 200:
                        err = f"HTTP {r.status_code}: {r.text[:100]}"
                        break
                    results = r.json().get("results", [])
                    raw += len(results)
                    for j in results:
                        jid = f"az-{cc}-{j.get('id')}"
                        if jid in ids:
                            continue
                        title = j.get("title", "")
                        loc = (j.get("location") or {}).get("display_name", "")
                        stub = {"id": jid, "title": title, "location": loc,
                                "company": (j.get("company") or {}).get("display_name", ""),
                                "source": "adzuna"}
                        reason = prefilter(title, loc, cc)
                        if reason:
                            record_drop(stub, "prefilter", reason)
                            continue
                        ids.add(jid)
                        sal = ""
                        if j.get("salary_min"):
                            sal = f"{int(j['salary_min'])}-{int(j.get('salary_max') or j['salary_min'])} {label} local"
                        out.append({
                            "id": jid,
                            "company": (j.get("company") or {}).get("display_name", ""),
                            "title": title, "location": loc, "country": cc,
                            "market": market_of(cc, loc),
                            "url": j.get("redirect_url", ""), "source": "adzuna",
                            "description": strip_html(j.get("description", "")),
                            "salary": sal, "posted_at": j.get("created", ""),
                        })
                        kept += 1
                    time.sleep(0.25)
                    if len(results) < ADZUNA_PER_PAGE:
                        break            # last page for this phrase
                except Exception as e:
                    err = f"error: {e}"
                    break
            if err:
                break
        bump_raw("adzuna", raw)
        diag[f"adzuna:{cc}"] = err or f"raw {raw}, kept {kept}"
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
    results = r.json().get("results", [])
    bump_raw("reed", len(results))
    for j in results:
        title = j.get("jobTitle", "")
        loc = j.get("locationName", "")
        reason = prefilter(title, loc, "gb")
        if reason:
            record_drop({"id": f"reed-{j.get('jobId')}", "title": title, "location": loc,
                         "company": j.get("employerName", ""), "source": "reed"},
                        "prefilter", reason)
            continue
        sal = ""
        if j.get("minimumSalary"):
            sal = f"{int(j['minimumSalary'])}-{int(j.get('maximumSalary') or j['minimumSalary'])} GBP"
        out.append({
            "id": f"reed-{j.get('jobId')}", "company": j.get("employerName", ""),
            "title": title, "location": loc or "London", "country": "gb",
            "market": market_of("gb", loc),
            "url": j.get("jobUrl", ""), "source": "reed",
            "description": strip_html(j.get("jobDescription", "")), "salary": sal,
            "posted_at": j.get("date", ""),
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
                             country_indeed="Ireland", hours_old=MAX_POST_AGE_DAYS * 24)
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        bump_raw("indeed", len(df))
        for _, row in df.iterrows():
            title = str(row.get("title") or "")
            loc = str(row.get("location") or "Dublin")
            jid = "js-" + re.sub(r"\W+", "-", str(row.get("job_url") or title))[-70:]
            reason = prefilter(title, loc, "ie")
            if reason:
                record_drop({"id": jid, "title": title, "location": loc,
                             "company": str(row.get("company") or ""), "source": "indeed"},
                            "prefilter", reason)
                continue
            if jid in seen:
                continue
            seen.add(jid)
            sal = ""
            if row.get("min_amount"):
                sal = f"{int(row['min_amount'])}-{int(row.get('max_amount') or row['min_amount'])} {row.get('currency') or 'EUR'}"
            out.append({
                "id": jid, "company": str(row.get("company") or ""),
                "title": title, "location": loc, "country": "ie",
                "market": market_of("ie", loc),
                "url": str(row.get("job_url") or ""), "source": "indeed",
                "description": strip_html(str(row.get("description") or "")), "salary": sal,
                "posted_at": str(row.get("date_posted") or ""),
            })
    return out

# ---------------------------------------------------------------- ATS supplements (companies.json)

def fetch_greenhouse(name, slug):
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"); r.raise_for_status()
    out = []
    jobs = r.json().get("jobs", [])
    bump_raw("ats", len(jobs))
    for j in jobs:
        loc = (j.get("location") or {}).get("name", "")
        reason = prefilter(j.get("title", ""), loc)
        if reason:
            record_drop({"id": f"gh-{slug}-{j['id']}", "title": j.get("title", ""),
                         "location": loc, "company": name, "source": "greenhouse"},
                        "prefilter", reason)
            continue
        out.append({"id": f"gh-{slug}-{j['id']}", "company": name, "title": j["title"],
                    "location": loc, "country": "", "market": market_of("", loc),
                    "url": j.get("absolute_url", ""),
                    "source": "greenhouse", "posted_at": j.get("updated_at", ""),
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
    jobs = r.json()
    bump_raw("ats", len(jobs))
    for j in jobs:
        loc = (j.get("categories") or {}).get("location", "") or ""
        reason = prefilter(j.get("text", ""), loc)
        if reason:
            record_drop({"id": f"lv-{slug}-{j['id']}", "title": j.get("text", ""),
                         "location": loc, "company": name, "source": "lever"},
                        "prefilter", reason)
            continue
        out.append({"id": f"lv-{slug}-{j['id']}", "company": name, "title": j["text"],
                    "location": loc, "country": "", "market": market_of("", loc),
                    "url": j.get("hostedUrl", ""),
                    "source": "lever", "posted_at": j.get("createdAt", ""),
                    "description": strip_html(j.get("descriptionPlain") or j.get("description", ""))})
    return out

def fetch_ashby(name, slug):
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"); r.raise_for_status()
    out = []
    jobs = r.json().get("jobs", [])
    bump_raw("ats", len(jobs))
    for j in jobs:
        loc = j.get("location", "") or ""
        reason = prefilter(j.get("title", ""), loc)
        if reason:
            record_drop({"id": f"as-{slug}-{j.get('id')}", "title": j.get("title", ""),
                         "location": loc, "company": name, "source": "ashby"},
                        "prefilter", reason)
            continue
        out.append({"id": f"as-{slug}-{j.get('id')}", "company": name, "title": j["title"],
                    "location": loc, "country": "", "market": market_of("", loc),
                    "url": j.get("jobUrl") or j.get("applyUrl", ""),
                    "source": "ashby", "posted_at": j.get("publishedAt", ""),
                    "description": strip_html(j.get("descriptionPlain") or "")})
    return out

ATS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}

# ---------------------------------------------------------------- LinkedIn (public guest search)

def fetch_linkedin():
    """Mirrors Tom's own 'Jobs based on your preferences' page via LinkedIn's public,
    unauthenticated guest job-search endpoint. No login/session cookie -- this is the
    same endpoint LinkedIn serves to logged-out visitors, so there's no account risk.
    The first call against it is unreliable cold, hence the throwaway warm-up request."""
    out, seen_ids = [], set()
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    ep = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    for geo_id in LINKEDIN_GEO_IDS:
        params = {"keywords": LINKEDIN_KEYWORDS, "geoId": geo_id, "f_TPR": "r86400"}
        s.get(ep, params={**params, "start": 0}, timeout=20)   # cold first call is unreliable
        for page in range(LINKEDIN_PAGES):
            cards = []
            for attempt in range(3):   # this endpoint is known to be flaky/throttly; retry before giving up
                time.sleep(0.5 if attempt == 0 else 2)
                r = s.get(ep, params={**params, "start": page * 10}, timeout=20)
                if r.status_code != 200:
                    continue
                cards = re.split(r"<li>", r.text)[1:]
                if cards:
                    break
            if not cards:
                break
            new_ids_this_page = 0
            for c in cards:
                m_id = re.search(r'data-entity-urn="urn:li:jobPosting:(\d+)"', c)
                m_title = re.search(r'<h3 class="base-search-card__title">\s*([^<]+?)\s*</h3>', c)
                if not (m_id and m_title):
                    continue
                jid = m_id.group(1)
                if jid in seen_ids:
                    continue
                seen_ids.add(jid); new_ids_this_page += 1
                bump_raw("linkedin", 1)
                m_company = re.search(r'<h4 class="base-search-card__subtitle">.*?>\s*([^<]+?)\s*</a>', c, re.S)
                m_loc = re.search(r'<span class="job-search-card__location">\s*([^<]+?)\s*</span>', c)
                m_date = re.search(r'<time class="job-search-card__listdate[^"]*"\s+datetime="([^"]+)"', c)
                title, loc = m_title.group(1), (m_loc.group(1) if m_loc else "")
                company = m_company.group(1) if m_company else ""
                reason = prefilter(title, loc)
                if reason:
                    record_drop({"id": f"li-{jid}", "title": title, "location": loc,
                                 "company": company, "source": "linkedin"}, "prefilter", reason)
                    continue
                detail_url = f"https://www.linkedin.com/jobs/view/{jid}"
                out.append({
                    "id": f"li-{jid}", "company": company,
                    "title": title, "location": loc, "country": "",
                    "market": market_of("", loc), "url": detail_url,
                    "source": "linkedin", "posted_at": (m_date.group(1) if m_date else ""),
                    "_detail": detail_url,
                })
            if new_ids_this_page == 0:   # exhausted this market's real results, stop paginating
                break
    return out

def linkedin_desc(url):
    try:
        r = get(url); r.raise_for_status()
        m = re.search(r'<div class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>\s*</div>', r.text, re.S)
        return strip_html(m.group(1)) if m else ""
    except Exception:
        return ""

def jsonld_job_description(url):
    """Generic schema.org JobPosting extractor. Widely used for SEO across ATS/career
    platforms (Workday, iCIMS, SmartRecruiters, custom sites) regardless of how the
    visible page itself renders, so this works across revopsroles.com's varied
    source_url domains without needing per-ATS parsing. Returns "" if absent/unparseable."""
    try:
        r = get(url); r.raise_for_status()
        for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', r.text, re.S):
            try:
                data = json.loads(m.group(1))
            except Exception:
                continue
            for d in (data if isinstance(data, list) else [data]):
                if isinstance(d, dict) and d.get("@type") == "JobPosting" and d.get("description"):
                    return strip_html(d["description"])
    except Exception:
        pass
    return ""

# ---------------------------------------------------------------- revopsroles.com

# robots.txt disallows the ?location_country= query-string filter (for every agent,
# named or "*") but NOT these plain per-country paths -- use these, never the query form.
REVOPSROLES_LOCATIONS = ["netherlands", "belgium", "united-kingdom", "ireland"]

def fetch_revopsroles():
    """Job metadata is embedded as an escaped JSON blob in the page's Next.js RSC
    payload rather than served through a documented API. No full description field is
    present in this listing data, so the real JD is lazy-fetched from source_url (the
    original posting) via jsonld_job_description() for survivors, same pattern as
    Greenhouse/LinkedIn. A short synthesized summary (category/seniority/work mode) is
    kept as a fallback only, for postings whose site doesn't expose JobPosting JSON-LD."""
    out, seen_ids = [], set()
    for slug in REVOPSROLES_LOCATIONS:
        try:
            r = get(f"https://revopsroles.com/locations/{slug}")
            r.raise_for_status()
        except Exception:
            continue
        for c in re.split(r'\\"_formatted\\":\{', r.text)[1:]:
            def field(key):
                m = re.search(r'\\"' + key + r'\\":\\"(.*?)\\"', c)
                if not m:
                    return ""
                try:
                    return json.loads('"' + m.group(1) + '"')
                except Exception:
                    return m.group(1)
            jid = field("id")
            if not jid or jid in seen_ids:
                continue
            seen_ids.add(jid)
            bump_raw("revopsroles", 1)
            title, loc = field("title"), field("location_raw")
            cc = country_code(field("location_country"))
            reason = prefilter(title, loc, cc)
            if reason:
                record_drop({"id": f"rr-{jid}", "title": title, "location": loc,
                             "company": field("company_name"), "source": "revopsroles"},
                            "prefilter", reason)
                continue
            sal = ""
            if field("salary_min"):
                sal = f"{field('salary_min')}-{field('salary_max') or field('salary_min')} {field('salary_currency')}"
            summary = "; ".join(f"{label}: {v}" for label, v in (
                ("Category", field("category")), ("Seniority", field("seniority")),
                ("Work mode", field("work_mode")), ("Visa sponsorship", field("visa_sponsorship")),
            ) if v)
            posted = field("posted_at")
            src_url = field("source_url") or f"https://revopsroles.com/jobs/{jid}"
            out.append({
                "id": f"rr-{jid}", "company": field("company_name"),
                "title": title, "location": loc, "country": cc,
                "market": market_of(cc, loc),
                "url": src_url, "source": "revopsroles", "salary": sal,
                "posted_at": float(posted) if posted else "",
                "_detail": src_url, "_fallback_desc": summary,
            })
    return out

APIFY_ACTOR = "memo23~apify-hiring-cafe-scraper"
# Tom's saved hiring.cafe searches (address-bar URLs, each encodes its own location/
# title/language filters). Add or edit searches here as his targeting evolves.
APIFY_HIRINGCAFE_SEARCHES = [
    # revops/gtm ops titles across NL, IE, UK-London, BE
    "https://hiringcafe.com/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%221BY1yZQBoEtHp_8UEq3V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22The+Netherlands%22%2C%22short_name%22%3A%22NL%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22The+Netherlands%22%2C%22population%22%3A17231017%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%5D%7D%7D%2C%7B%22id%22%3A%22kxY1yZQBoEtHp_8UEq3V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22Ireland%22%2C%22short_name%22%3A%22IE%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22Ireland%22%2C%22population%22%3A4853506%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%5D%7D%7D%2C%7B%22id%22%3A%22xRg1yZQBoEtHp_8UXQ1z%22%2C%22types%22%3A%5B%22locality%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22London%22%2C%22short_name%22%3A%22London%22%2C%22types%22%3A%5B%22locality%22%5D%7D%2C%7B%22long_name%22%3A%22England%22%2C%22short_name%22%3A%22ENG%22%2C%22types%22%3A%5B%22administrative_area_level_1%22%5D%7D%2C%7B%22long_name%22%3A%22United+Kingdom%22%2C%22short_name%22%3A%22GB%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22geometry%22%3A%7B%22location%22%3A%7B%22lat%22%3A51.50853%2C%22lon%22%3A-0.12574%7D%7D%2C%22formatted_address%22%3A%22London%2C+England%2C+GB%22%2C%22population%22%3A8961989%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22radius%22%3A25%2C%22radius_unit%22%3A%22miles%22%2C%22ignore_radius%22%3Afalse%7D%7D%2C%7B%22id%22%3A%22QRY1yZQBoEtHp_8UEq3V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22Belgium%22%2C%22short_name%22%3A%22BE%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22Belgium%22%2C%22population%22%3A11422068%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%5D%7D%7D%5D%2C%22commitmentTypes%22%3A%5B%22Full+Time%22%5D%2C%22dateFetchedPastNDays%22%3A21%2C%22excludedLanguageRequirements%22%3A%5B%22dutch%22%2C%22german%22%2C%22spanish%22%2C%22french%22%5D%2C%22sortBy%22%3A%22date%22%2C%22jobTitleQuery%22%3A%22%5C%22revenue+operations%5C%22+OR+%5C%22RevOps%5C%22+OR+%5C%22sales+operations%5C%22+OR+%5C%22sales+ops%5C%22+OR+%5C%22CS+operations%5C%22+OR+%5C%22customer+success+operations%5C%22+OR+%5C%22GTM+operations%5C%22+OR+%5C%22go-to-market+operations%5C%22+OR+%5C%22GTM+strategy%5C%22+OR+%5C%22go-to-market+strategy%5C%22+OR+%5C%22revenue+strategy%5C%22+OR+%5C%22sales+enablement%5C%22+OR+%5C%22revenue+enablement%5C%22+OR+%5C%22commercial+operations%5C%22+OR+%5C%22sales+strategy%5C%22+OR+%5C%22revenue+strategy+%26+operations%5C%22+OR+%5C%22sales+strategy+%26+operations%5C%22+OR+%5C%22GTM+strategy+%26+operations%5C%22%22%7D",
    # CS titles, Netherlands only
    "https://hiringcafe.com/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%221BY1yZQBoEtHp_8UEq3V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22The+Netherlands%22%2C%22short_name%22%3A%22NL%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22The+Netherlands%22%2C%22population%22%3A17231017%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%5D%7D%7D%5D%2C%22dateFetchedPastNDays%22%3A21%2C%22excludedLanguageRequirements%22%3A%5B%22dutch%22%2C%22german%22%5D%2C%22sortBy%22%3A%22date%22%2C%22jobTitleQuery%22%3A%22%5C%22Customer+success%5C%22%22%7D",
    # senior/principal/lead/enterprise/strategic CS titles across NL, IE, UK-London
    "https://hiringcafe.com/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%221BY1yZQBoEtHp_8UEq3V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22The+Netherlands%22%2C%22short_name%22%3A%22NL%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22The+Netherlands%22%2C%22population%22%3A17231017%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%5D%7D%7D%2C%7B%22id%22%3A%22kxY1yZQBoEtHp_8UEq3V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22Ireland%22%2C%22short_name%22%3A%22IE%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22Ireland%22%2C%22population%22%3A4853506%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%7D%2C%7B%22id%22%3A%22xRg1yZQBoEtHp_8UXQ1z%22%2C%22types%22%3A%5B%22locality%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22London%22%2C%22short_name%22%3A%22London%22%2C%22types%22%3A%5B%22locality%22%5D%7D%2C%7B%22long_name%22%3A%22England%22%2C%22short_name%22%3A%22ENG%22%2C%22types%22%3A%5B%22administrative_area_level_1%22%5D%7D%2C%7B%22long_name%22%3A%22United+Kingdom%22%2C%22short_name%22%3A%22GB%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22geometry%22%3A%7B%22location%22%3A%7B%22lat%22%3A51.50853%2C%22lon%22%3A-0.12574%7D%7D%2C%22formatted_address%22%3A%22London%2C+England%2C+GB%22%2C%22population%22%3A8961989%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22radius%22%3A50%2C%22radius_unit%22%3A%22miles%22%2C%22ignore_radius%22%3Afalse%7D%7D%5D%2C%22dateFetchedPastNDays%22%3A21%2C%22excludedLanguageRequirements%22%3A%5B%22dutch%22%2C%22german%22%2C%22french%22%5D%2C%22sortBy%22%3A%22date%22%2C%22jobTitleQuery%22%3A%22%5C%22Customer+success%5C%22+AND+%28senior+OR+principal+OR+lead+OR+enterprise+OR+strategic%29%22%7D",
    # CS titles at GRC/compliance/legaltech companies, NL/IE/UK-London
    "https://hiringcafe.com/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%221BY1yZQBoEtHp_8UEq3V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22The+Netherlands%22%2C%22short_name%22%3A%22NL%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22The+Netherlands%22%2C%22population%22%3A17231017%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%5D%7D%7D%2C%7B%22id%22%3A%22kxY1yZQBoEtHp_8UEq3V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22Ireland%22%2C%22short_name%22%3A%22IE%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22Ireland%22%2C%22population%22%3A4853506%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%5D%7D%7D%2C%7B%22id%22%3A%22xRg1yZQBoEtHp_8UXQ1z%22%2C%22types%22%3A%5B%22locality%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22London%22%2C%22short_name%22%3A%22London%22%2C%22types%22%3A%5B%22locality%22%5D%7D%2C%7B%22long_name%22%3A%22England%22%2C%22short_name%22%3A%22ENG%22%2C%22types%22%3A%5B%22administrative_area_level_1%22%5D%7D%2C%7B%22long_name%22%3A%22United+Kingdom%22%2C%22short_name%22%3A%22GB%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22geometry%22%3A%7B%22location%22%3A%7B%22lat%22%3A51.50853%2C%22lon%22%3A-0.12574%7D%7D%2C%22formatted_address%22%3A%22London%2C+England%2C+GB%22%2C%22population%22%3A8961989%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22radius%22%3A50%2C%22radius_unit%22%3A%22miles%22%2C%22ignore_radius%22%3Afalse%7D%7D%5D%2C%22dateFetchedPastNDays%22%3A21%2C%22excludedLanguageRequirements%22%3A%5B%22dutch%22%2C%22german%22%5D%2C%22sortBy%22%3A%22date%22%2C%22jobTitleQuery%22%3A%22%5C%22Customer+success%5C%22%22%2C%22jobDescriptionQuery%22%3A%22GRC+OR+compliance+OR+legaltech%22%7D",
]
APIFY_MAX_ITEMS = 200   # across all four searches combined; ~$0.25/run at $1.25/1000 results

def fetch_apify_hiringcafe(token):
    """Runs Tom's saved hiring.cafe searches through the Apify actor
    memo23/apify-hiring-cafe-scraper. Each search already encodes its own
    location/title/language filters; dateFetchedPastNDays=21 in the searches is wider
    than our own MAX_POST_AGE_DAYS, so the age filter downstream still applies."""
    out = []
    r = requests.post(
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
        params={"token": token},
        json={"startUrls": APIFY_HIRINGCAFE_SEARCHES, "maxItems": APIFY_MAX_ITEMS,
              "enrichDescription": True},
        timeout=280)
    r.raise_for_status()
    items = r.json()
    bump_raw("hiring.cafe", len(items))
    for j in items:
        info = j.get("job_information", {}) or {}; proc = j.get("v5_processed_job_data", {}) or {}
        title = info.get("title") or proc.get("core_job_title", "")
        loc = proc.get("formatted_workplace_location", "")
        reason = prefilter(title, loc)
        if reason:
            record_drop({"id": "hc-" + str(j.get("id", ""))[:60], "title": title,
                         "location": loc, "company": proc.get("company_name", ""),
                         "source": "hiring.cafe"}, "prefilter", reason)
            continue
        sal = ""
        if proc.get("yearly_min_compensation"):
            cur = proc.get("listed_compensation_currency") or ""
            sal = f"{int(proc['yearly_min_compensation'])}-{int(proc.get('yearly_max_compensation') or proc['yearly_min_compensation'])} {cur}".strip()
        out.append({"id": "hc-" + str(j.get("id", ""))[:60], "company": proc.get("company_name", ""),
                    "title": title, "location": loc, "country": "",
                    "market": market_of("", loc), "salary": sal,
                    "url": j.get("apply_url") or "", "source": "hiring.cafe",
                    "description": strip_html(info.get("description", "")),
                    "posted_at": proc.get("estimated_publish_date", "")})
    return out

# ---------------------------------------------------------------- Claude scoring

def _extract_json(text):
    text = re.sub(r"```json|```", "", text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)

def _claude_call(api_key, model, system, user, max_tokens, extra=None, cache_system=False):
    """One Messages API call, with bounded retry on the transient failures. cache_system
    puts a cache breakpoint on the system prompt: the deep-score prefix (rubric + the whole
    of profile.md) is identical for every job in a run, so without this it gets re-billed
    on all 30 calls."""
    body = {
        "model": model, "max_tokens": max_tokens,
        "system": ([{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}] if cache_system else system),
        "messages": [{"role": "user", "content": user}],
    }
    if extra:
        body.update(extra)
    last = None
    for attempt in range(CLAUDE_ATTEMPTS):
        if attempt:
            time.sleep(2 ** attempt)      # 2s, 4s
        try:
            r = requests.post(API_URL, timeout=180, headers={
                "x-api-key": api_key, "anthropic-version": API_HEADERS_VERSION,
                "content-type": "application/json"}, json=body)
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            continue
        if r.status_code in (408, 409, 429) or r.status_code >= 500:
            last = RuntimeError(f"HTTP {r.status_code}: {r.text[:140]}")
            continue
        r.raise_for_status()             # 4xx other than the above is a real bug, not a blip
        payload = r.json()
        note_usage(payload.get("usage") or {})
        stop = payload.get("stop_reason")
        # Opus 5 can decline a request outright (HTTP 200, empty content) and can run out of
        # room mid-answer. Both used to surface as an empty string and a bogus score of 0.
        if stop == "refusal":
            raise RuntimeError("model declined to score this posting (stop_reason=refusal)")
        if stop == "max_tokens":
            raise RuntimeError(f"hit max_tokens ({max_tokens}) before finishing")
        return "".join(b.get("text", "") for b in payload.get("content", [])
                       if b.get("type") == "text")
    raise last or RuntimeError("claude call failed")

def job_message(job):
    desc = (job.get("description") or "")[:DESC_CHAR_CAP]
    return (f"Title: {job['title']}\nCompany: {job.get('company','?')}\n"
            f"Location: {job.get('location','?')}\n"
            # Facts resolved in code, given so the model doesn't re-derive (and mis-derive) them.
            + (f"Market (resolved in code, trust this): {job['market']}\n" if job.get("market") else "")
            + (f"Title band (resolved in code): {title_band(job.get('title'))}\n")
            + (f"Salary: {job['salary']}\n" if job.get("salary") else "")
            + (f"Sponsor: {job['sponsor']}\n" if job.get("sponsor") else "")
            + (f"Description: {desc}" if desc else "No description; judge on title/location/sponsor only."))

def screen_job(api_key, job):
    text = _claude_call(api_key, CLAUDE_SCREEN_MODEL, SCREEN_SYSTEM, job_message(job), 120)
    data = _extract_json(text)
    return bool(data.get("keep", True)), str(data.get("reason", ""))[:80]

def score_job(api_key, system, job):
    """The model scores the six dimensions and reports what it read off the posting; the
    total and the caps are computed here. Opus 5 thinks by default -- do not disable it,
    which on this model can leak reasoning into the visible answer."""
    text = _claude_call(
        api_key, CLAUDE_SCORE_MODEL, system, job_message(job), SCORE_MAX_TOKENS,
        cache_system=True,
        extra={"output_config": {"effort": SCORE_EFFORT,
                                 "format": {"type": "json_schema", "schema": SCORE_SCHEMA}}})
    data = _extract_json(text)
    dims = {k: max(0.0, min(10.0, float((data.get("dimensions") or {}).get(k, 0) or 0)))
            for k in RUBRIC_KEYS}
    raw = weighted_total(dims)
    score, caps = apply_caps(raw, job, data)
    flags = [str(f)[:70] for f in (data.get("flags") or [])][:6]
    if not data.get("salary_stated"):
        flags.append("comp not listed, verify vs floor")
    return {
        "score": score, "score_raw": round(raw, 1), "caps_applied": caps,
        "dimensions": dims,
        "tier": job.get("market") or "outside target markets",
        "flags": flags[:8], "verdict": str(data.get("verdict", ""))[:180],
    }

# ---------------------------------------------------------------- main

def load_json(path, default):
    try:
        return json.load(open(path)) if os.path.exists(path) else default
    except Exception:
        return default

def cmd_selftest():
    """Replay the stored dimension scores through the new engine and report every row whose
    score moves. No network, no API key. The drifts here are exactly the rows where the old
    model-reported total disagreed with the weighted sum of its own dimensions."""
    jobs = load_json("docs/jobs.json", [])
    moved = 0
    print(f"Replaying {len(jobs)} stored rows through weighted_total + apply_caps\n")
    for j in jobs:
        dims = j.get("dimensions") or {}
        if not dims:
            continue
        raw = weighted_total(dims)
        # Reconstruct the observations the old rows never stored, from what they did store.
        obs = {"function_match": "core", "company_standout": True,
               "language_hard_requirement": any("language" in f.lower() or "fluency" in f.lower()
                                                for f in (j.get("flags") or [])),
               "salary_stated": False, "salary_min_base": 0, "salary_currency": ""}
        job = {"title": j.get("title"), "market": market_of(j.get("country"), j.get("location"))}
        new, caps = apply_caps(raw, job, obs)
        old = j.get("score", 0)
        if abs(new - old) > 0.05:
            moved += 1
            print(f"  {old:>4} -> {new:<4} weighted {raw:.1f}  {str(j.get('title'))[:44]:<46}"
                  f" {'; '.join(caps) or 'no caps'}")
    print(f"\n{moved} of {len(jobs)} rows move. Inspect any row whose movement you can't "
          f"explain from its caps.\n"
          "Caveat: the stored rows predate the observation fields, so the salary-floor and "
          "off-target-function\ncaps cannot be replayed and some rows will land lower than "
          "this once actually rescored.")

def cmd_unkill():
    """Clear stage-1 Haiku kills out of seen.json so they get re-evaluated next run.
    Replaces hand-editing the JSON when the cheap screen throws away something good."""
    dropped = load_json("docs/excluded.json", {}).get("rows", [])
    killed = {d["id"] for d in dropped if d.get("stage") == "stage1-kill" and d.get("id")}
    seen = set(load_json("seen.json", []))
    freed = killed & seen
    json.dump(sorted(seen - freed), open("seen.json", "w"))
    print(f"Freed {len(freed)} stage-1 kills for re-evaluation "
          f"({len(killed)} recorded, {len(killed) - len(freed)} already absent from seen.json).")

def cmd_rescore():
    """Drop every scored row so the whole corpus re-runs under the current engine."""
    jobs = load_json("docs/jobs.json", [])
    ids = {str(j.get("id")) for j in jobs}
    seen = set(load_json("seen.json", []))
    json.dump(sorted(seen - ids), open("seen.json", "w"))
    json.dump([], open("docs/jobs.json", "w"))
    print(f"Cleared {len(jobs)} scored rows; {len(ids & seen)} ids freed from seen.json. "
          f"Run scan.py to rescore.")

def main():
    verify, dry = "--verify" in sys.argv, "--dry" in sys.argv
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)
    companies = json.load(open("companies.json")).get("companies", []) if os.path.exists("companies.json") else []

    if "--selftest" in sys.argv:
        return cmd_selftest()
    if "--unkill" in sys.argv:
        return cmd_unkill()
    if "--rescore" in sys.argv:
        return cmd_rescore()

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
            src_status["Adzuna (NL+UK)"] = f"{src_line('adzuna', len(jobs))} | " + "; ".join(f"{k.split(':')[1]}={v}" for k, v in diag.items() if k.startswith("adzuna:"))
        except Exception as e:
            src_status["Adzuna (NL+UK)"] = f"FAIL: {e}"
    else:
        src_status["Adzuna (NL+UK)"] = "skipped: no ADZUNA_APP_ID/KEY set"

    # 2. Reed (UK)
    reed_key = os.environ.get("REED_API_KEY", "")
    if reed_key:
        try:
            jobs = fetch_reed(reed_key); found += jobs
            src_status["Reed (UK)"] = src_line("reed", len(jobs))
        except Exception as e:
            src_status["Reed (UK)"] = f"FAIL: {e}"
    else:
        src_status["Reed (UK)"] = "skipped: no REED_API_KEY set"

    # 3. JobSpy / Indeed (Ireland)
    try:
        jobs = fetch_jobspy_ireland(); found += jobs
        src_status["Indeed/JobSpy (Dublin)"] = src_line("indeed", len(jobs))
    except Exception as e:
        src_status["Indeed/JobSpy (Dublin)"] = f"skipped: {e}"

    # 4. Company ATS feeds (Greenhouse/Lever/Ashby)
    ats_n = 0
    for c in companies:
        try:
            jobs = ATS[c["ats"]](c["name"], c["slug"]); found += jobs; ats_n += len(jobs)
        except Exception:
            pass
    src_status[f"Company ATS ({len(companies)} watched)"] = src_line("ats", ats_n)

    # 5. hiring.cafe via Apify (direct API blocks datacenter IPs, so this runs
    # Tom's saved searches through the Apify actor instead)
    apify_token = os.environ.get("APIFY_API_TOKEN", "")
    if apify_token:
        try:
            jobs = fetch_apify_hiringcafe(apify_token); found += jobs
            src_status["hiring.cafe (Apify)"] = src_line("hiring.cafe", len(jobs))
        except Exception as e:
            src_status["hiring.cafe (Apify)"] = f"FAIL: {e}"
    else:
        src_status["hiring.cafe (Apify)"] = "skipped: no APIFY_API_TOKEN set"

    # 6. LinkedIn (public guest search, mirrors Tom's own "based on your preferences" page)
    try:
        jobs = fetch_linkedin(); found += jobs
        src_status["LinkedIn"] = src_line("linkedin", len(jobs))
    except Exception as e:
        src_status["LinkedIn"] = f"skipped: {e}"

    # 7. revopsroles.com (per-country location pages, robots.txt-compliant)
    try:
        jobs = fetch_revopsroles(); found += jobs
        src_status["revopsroles.com"] = src_line("revopsroles", len(jobs))
    except Exception as e:
        src_status["revopsroles.com"] = f"skipped: {e}"

    # Normalise the short scraped fields once, here, rather than in seven fetchers.
    for j in found:
        for k in ("title", "company", "location"):
            if j.get(k):
                j[k] = clean_text(j[k])

    # age filter: drop anything older than a week when the source told us its post date
    kept_age, no_date = [], 0
    for j in found:
        if parse_date_loose(j.get("posted_at")) is None:
            no_date += 1        # recent_enough fails open here by design; count it so a
                                # source that silently loses its date field is visible
        if recent_enough(j.get("posted_at")):
            kept_age.append(j)
        else:
            record_drop(j, "age", f"posted more than {MAX_POST_AGE_DAYS}d ago ({j.get('posted_at')})")
    src_status["age filter"] = (f"dropped {len(found) - len(kept_age)} older than "
                               f"{MAX_POST_AGE_DAYS}d; {no_date} had no usable date (kept)")
    found = kept_age

    # cross-source dedupe: the same role can arrive from Indeed + Greenhouse etc, and
    # can also resurface via a different source in a later run than the one that first
    # found it -- so seed dseen with everything already on the dashboard, not just this run.
    dseen = {dkey(j) for j in existing if all(dkey(j))}
    deduped = []
    for j in found:
        k = dkey(j)
        if k in dseen and all(k):   # only dedupe when company+title+city all present
            record_drop(j, "dedupe", f"same company+title+city already on the dashboard ({k[2]})")
            continue
        dseen.add(k); deduped.append(j)
    found = deduped

    new_jobs = [j for j in found if j["id"] not in seen]
    print(f"Fetched {len(found)} relevant, {len(new_jobs)} new.")

    # 8. sponsor registers (load once)
    print("Loading sponsor registers...")
    uk_reg, nl_reg = spon.load_uk(), spon.load_nl()
    def reg_status(reg, fail_word):
        if not reg.ok:
            return f"{fail_word} - {reg.note}"
        return ("ok - " if reg.trust_negatives else "degraded - ") + reg.note
    src_status["UK sponsor register"] = reg_status(uk_reg, "FAIL")
    src_status["NL sponsor register"] = reg_status(nl_reg, "degraded")

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

    detail_fetchers = {"greenhouse": greenhouse_desc, "linkedin": linkedin_desc,
                       "revopsroles": jsonld_job_description}
    for j in new_jobs[:MAX_SCREENED_PER_RUN]:
        # fill lazily-fetched descriptions (Greenhouse, LinkedIn, revopsroles) before any
        # scoring -- only for survivors of the title/location prefilter, same as every
        # other source. revopsroles falls back to its synthesized summary if the source
        # site has no JobPosting JSON-LD to pull a real description from.
        if j.get("_detail") and not j.get("description"):
            fetcher = detail_fetchers.get(j.get("source"))
            if fetcher:
                j["description"] = fetcher(j["_detail"])
        if not j.get("description") and j.get("_fallback_desc"):
            j["description"] = j["_fallback_desc"]
        j.pop("_detail", None); j.pop("_fallback_desc", None)

        which, raw, label = sponsor_for(j)
        j["sponsor_region"], j["sponsor_raw"], j["sponsor"] = which or "", raw, label
        if SPONSOR_REQUIRED and raw == "not_found":
            record_drop(j, "sponsor-required", f"company not on the {which} sponsor register")
            seen.add(j["id"]); continue

        if dry or not api_key:
            j.update({"score": 0, "score_raw": 0, "caps_applied": [], "dimensions": {},
                      "tier": j.get("market") or "", "flags": [], "verdict": "(not scored)"})
            j["found_at"] = now_iso(); j["description"] = (j.get("description") or "")[:400]
            scored.append(j); seen.add(j["id"]); continue

        # STAGE 1: cheap screen
        try:
            keep, reason = screen_job(api_key, j)
        except Exception:
            keep, reason = True, "screen error, passed through"
        if not keep:
            killed += 1; seen.add(j["id"])
            # Recorded, so the kill is reviewable on the dashboard and reversible with
            # `scan.py --unkill` -- it used to leave nothing behind but this print.
            record_drop(j, "stage1-kill", reason or "screened out")
            print(f"  kill  {j['title']} @ {j.get('company') or j['source']} ({reason})")
            continue
        kept += 1

        # STAGE 2: deep score (only survivors, capped)
        if len(scored) >= MAX_SCORED_PER_RUN:
            break
        try:
            j.update(score_job(api_key, system_score, j))
        except Exception as e:
            # Deliberately NOT added to seen: a transient failure used to write score 0 and
            # mark the job seen forever, which needed a manual commit to undo. Now it just
            # retries on the next run.
            record_drop(j, "score-error", str(e)[:160])
            print(f"  ERR   {j['title']} @ {j.get('company') or j['source']} ({e})")
            continue
        j["description"] = (j.get("description") or "")[:400]
        j["found_at"] = now_iso()
        scored.append(j); seen.add(j["id"])
        caps = f" | capped: {'; '.join(j['caps_applied'])}" if j.get("caps_applied") else ""
        print(f"  [{j.get('score','-')}] (raw {j.get('score_raw','-')}) {j['title']} "
              f"@ {j.get('company') or j['source']} | {j['sponsor'] or 'n/a'}{caps}")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    merged = scored + [j for j in existing if j.get("found_at", "") >= cutoff]
    merged.sort(key=lambda j: (j.get("score", 0), j.get("found_at", "")), reverse=True)

    src_status["screening"] = f"stage1 kept {kept}, killed {killed}; stage2 scored {len(scored)}"
    if USAGE["in"] or USAGE["cache_read"]:
        src_status["tokens"] = (f"in {USAGE['in']}, out {USAGE['out']}, "
                               f"cache read {USAGE['cache_read']}, written {USAGE['cache_write']}")
    if DROP_COUNTS:
        src_status["dropped"] = ", ".join(f"{k} {v}" for k, v in sorted(DROP_COUNTS.items()))

    # Rolling audit trail of what was thrown away, newest first. Retained per stage rather
    # than as one flat list, so thousands of routine title rejects can't push the handful of
    # Haiku kills and scoring errors -- the rows actually worth reviewing -- out of the file.
    prev = load_json("docs/excluded.json", {}).get("rows", [])
    rows, per_group = [], {}
    for d in DROPS + prev:
        g = str(d.get("stage", "")).split(":")[0]
        if per_group.get(g, 0) >= DROP_KEEP_PER_STAGE.get(g, DROP_KEEP_DEFAULT):
            continue
        per_group[g] = per_group.get(g, 0) + 1
        rows.append(d)
    rows = rows[:DROP_MAX_ROWS]

    json.dump(sorted(seen), open("seen.json", "w"))
    json.dump(merged, open("docs/jobs.json", "w"), indent=1)
    json.dump({"last_run": now_iso(), "counts": DROP_COUNTS, "rows": rows},
              open("docs/excluded.json", "w"), indent=1)
    json.dump({"last_run": now_iso(), "new_this_run": len(scored), "sources": src_status,
               "gate": GATE, "floor": FLOOR, "score_model": CLAUDE_SCORE_MODEL,
               "screen_model": CLAUDE_SCREEN_MODEL},
              open("docs/status.json", "w"), indent=1)
    if not dry:
        notify_strong_matches(scored)
    print(f"Done. {len(scored)} new on dashboard, {len(merged)} total, "
          f"{sum(DROP_COUNTS.values())} dropped this run.")

if __name__ == "__main__":
    main()
