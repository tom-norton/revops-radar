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
  - revopsroles.com  : parsed from Tom's own daily digest email (direct scraping
                       started hitting Vercel's bot-challenge on 2026-07-31, same
                       failure mode as hiring.cafe's direct API above) - reads via
                       Gmail IMAP, GMAIL_ADDRESS/GMAIL_APP_PASSWORD required

Pipeline:
  fetch -> title/location prefilter (free) -> age filter -> dedupe
        -> UK/NL sponsor-register check
        -> hard disqualifiers on the raw text (no sponsorship / non-English required)
        -> STAGE 1 cheap screen (Claude Haiku, kill/keep)
        -> STAGE 2 deep score (Claude Opus, dimension scores vs profile.md)
        -> hard disqualifiers from what Opus read (below-floor salary / hard language)
        -> weighted total, computed here in Python, for whatever survives
        -> write docs/jobs.json + docs/status.json + docs/excluded.json

Scoring split: the model scores the six rubric dimensions and reports facts it can only get
by reading the posting (stated salary, hard language requirement, function match, whether
the employer is a standout). Two of those facts -- a stated salary below the market's visa
floor, and a hard non-English language requirement -- are absolute, so deep_score_
disqualifier() drops the role before it is ever scored, the same policy as the pre-model
says_no_sponsorship() / requires_other_language() checks, just decided from a real reading
of the posting instead of a text match. Everything else lands in the six dimensions;
weighted_total() does the arithmetic on those, so the score is reproducible rather than
whatever number the model felt like reporting, and nothing clamps it afterwards. What
remains of the old ceilings (off-target function, title band, CSM outside NL) travels as a
flag via score_flags() instead -- shown, not scored -- matching how the
job-application-workflow skill works.

Every row the pipeline rejects is logged to docs/excluded.json with the stage and reason,
and shown in a collapsed section on the dashboard.

Usage:
  python scan.py             normal run
  python scan.py --dry       everything except the Claude calls
  python scan.py --verify    test the optional ATS company slugs, no scoring
  python scan.py --selftest  replay stored dimension scores through the scoring engine, offline
  python scan.py --unkill    free stage-1 kills from seen.json so they get re-evaluated
  python scan.py --unkill-history [--days N]
                             same, but walks git history of docs/excluded.json (default 14
                             days) to reach kills the committed sample no longer holds
  python scan.py --ignore-age   one run with the 7-day age filter relaxed; pair it with
                             --unkill-history, whose rows are older than the cutoff by now
  python scan.py --rescore   clear scored rows so the corpus re-runs under the current engine
"""

import email, html, imaplib, json, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
import requests
import sponsors as spon

# ---------------------------------------------------------------- config

# Adzuna covers NL + UK only (its API has no Ireland). Ireland comes from JobSpy + ATS.
# Mapped to the ISO currency of each country's salary figures: Adzuna reports pay with no
# currency at all, so the code has to supply it. The old format put the country's display
# name in the currency slot ("44231-44231 United Kingdom local"), leaving the scoring model
# to infer "GBP" from the words -- and salary_floor_flag() checks that inference against the
# market's currency before deep_score_disqualifier() drops the role on it.
ADZUNA_COUNTRIES = {"nl": "EUR", "gb": "GBP"}
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
    # Strategy & Operations, in every wording the market actually uses. "op(eration)?s" so
    # the abbreviated "Strategy & Ops" isn't missed; the connector class covers "Strategy,
    # Planning & Operations" and "Strategy and Business Operations", which the old
    # "strategy (and|&) ops" could not match. Wolters Kluwer's "Business Strategy &
    # Analytics Manager" was the one genuinely relevant role this gate lost in a week.
    r"|strategy[ ,&]{1,3}(and[ ,&]{1,3})?(planning|business|revenue|sales|commercial)?[ ,&]{0,3}"
    r"(op(eration)?s|planning|analytics)\b"
    r"|strategic operations|\bs ?& ?o\b|business strategy"
    r"|business operations|commercial operations|biz ?ops"
    r"|sales strategy|revenue strategy|revenue enablement|sales enablement"
    # Comp, quota and territory design are core RevOps work the filter had no words for --
    # 5 of 5 sales-compensation roles on the watched boards were being dropped. "territory"
    # is deliberately narrow so quota-carrying "Territory Sales Director" titles stay out.
    r"|sales compensation|incentive compensation|quota"
    r"|territory (planning|design|management|operations)"
    r"|revenue analytics|revenue systems|revenue technology"
    # Renewals: adjacent to the RevOps pivot, but a direct match for the renewal-forecasting
    # and NRR record in profile.md.
    r"|renewals?\b"
    # Both word orders. "Enterprise Customer Success Manager" used to pass while
    # "Customer Success Manager, Enterprise" -- the same job -- was dropped.
    r"|(senior|principal|lead|enterprise|strategic).{0,20}customer success"
    r"|customer success.{0,30}(senior|principal|lead|enterprise|strategic)"
    # CS team-lead roles ("Manager, Customer Success"), which are the Manager band profile.md
    # actually targets. Qualifier-before-noun only, so this never matches a plain
    # "Customer Success Manager" -- that individual-contributor title is handled by the
    # market-conditional rule in prefilter() instead.
    r"|(manager|head of),?\s+(of\s+)?customer success", re.I)

# A plain "Customer Success Manager" with no seniority wording is a target in the Netherlands
# only -- profile.md's CSM track weighting makes NL Senior/Principal CSM a primary target and
# keeps the same role modest elsewhere. Admitting it everywhere would put ~25 extra rows per
# run on the dashboard; restricting it to NL admitted one. profile.md carries the same
# asymmetry into the deep score, and score_flags() notes it on the row.
CSM_ANY = re.compile(r"customer success", re.I)

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
# How much of the posting the scoring model sees. The old 2200 cut a typical 5,000-char ad
# roughly in half, and the half it threw away was the bottom -- which is exactly where the
# disqualifiers live. An Edenred ad reading "You are fluent in French, Dutch and English" at
# character 4,200 was scored 6.7 with language_hard_requirement: false. Over the cap the
# description is sampled head + tail rather than truncated, so the closing requirements
# block always arrives.
DESC_CHAR_CAP = 6000
DESC_HEAD_SHARE = 0.7         # of DESC_CHAR_CAP; the remainder is taken from the end
# Below this, a description is treated as missing and the detail fetchers are tried. Job
# boards routinely hand back a page's marketing furniture instead of the posting.
MIN_DESC_CHARS = 900
DESC_STORE_CAP = 1200         # how much is kept in docs/jobs.json, for auditing a score
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

# ---------------------------------------------------------------- hard disqualifiers
# Two facts that end an application before it starts, both stated in plain English in the
# ad and neither previously looked for anywhere in the pipeline. They are read in code
# rather than asked of the model because they are absolute: the model gets a truncated
# copy of the posting and, on the first run, missed both. Kantar scored 7.3 with "We're
# not able to offer visa sponsorship ... for this role" in its ad, and Edenred scored 6.7
# with "You are fluent in French, Dutch and English" in its requirements.
#
# Both are matched a sentence at a time. A posting that says "German is a plus" in one
# sentence and "fluent Dutch required" in another must still be caught, and a softener
# must only excuse the sentence it appears in.
_SENTENCE = re.compile(r"[^.;!?\n•|]+")
# Some sources return the ad with its punctuation flattened, so a whole requirements list
# arrives as one 900-character "sentence". Past this length the context is narrowed to a
# window around the match instead, both for the quote and for the softener check.
_CONTEXT_WINDOW = 110
_MAX_SENTENCE = 240

# Negation bound TIGHTLY to a sponsorship word: the two may only be separated by words from
# this filler list, which is what makes a guard clause unnecessary. "We have no restrictions
# on visa sponsorship" does not match ("restrictions" is not filler) and neither does "we
# are happy to sponsor" (no negator), while "not able to offer visa sponsorship" does.
_NEG = (r"(?:\bnot\b|\bno\b|\bunable\b|\bunwilling\b|\bcan ?not\b|\bcan't\b|\bwon't\b"
        r"|\bnever\b|\bnor\b)")
_FILLER = (r"(?:\s+(?:able|willing|eligible|prepared|position|currently|presently|at|this|"
           r"present|time|to|be|being|can|will|do|does|offer|offers|offering|provide|"
           r"provides|providing|support|supports|supporting|consider|considering|accept|"
           r"accepting|seek|seeking|in|a|an|any|the|for|of|with|new|further|additional|"
           r"applicants|candidates|require|requiring|visa|visas|work|employment|"
           r"immigration|relocation|uk|us|eu)){0,8}")
NO_SPONSOR_RX = re.compile(
    _NEG + _FILLER + r"\s+sponsor(?:ship|ing|s|ed)?\b"
    r"|\bsponsorship\b[^,]{0,25}\b(?:not available|unavailable|not offered|not provided"
    r"|not possible|not an option|not on offer)\b"
    r"|\bwithout\b[^,]{0,30}\bsponsor(?:ship)?\b", re.I)

# English is deliberately absent -- Tom is a native speaker, so an English requirement is
# never a disqualifier.
_OTHER_LANGS = (r"dutch|nederlands|flemish|french|français|german|deutsch|spanish"
                r"|italian|portuguese|polish|danish|swedish|norwegian|finnish|czech")
# `\brequire[ds]?\b` and not `required?` on purpose: the latter also matches the word
# "Requirements", so a "Requirements:" heading followed by any mention of the Dutch market
# would read as a Dutch-language requirement.
_REQUIRED = (r"\bfluent\b|\bfluency\b|\bnative\b|mother ?tongue|business[- ]level"
             r"|professional (?:working )?proficiency|\bproficient\b|\bproficiency\b"
             r"|must speak|\brequire[ds]?\b|\bmandatory\b|\bessential\b|\bmust have\b")
LANG_HARD_RX = re.compile(
    rf"(?:{_REQUIRED})[^,]{{0,80}}\b(?:{_OTHER_LANGS})\b"
    rf"|\b(?:{_OTHER_LANGS})\b[^,]{{0,60}}"
    rf"(?:\bfluen\w+|\bnative\b|\brequire[ds]?\b|is a must|\bmandatory\b|\bessential\b"
    rf"|non-negotiable)", re.I)
# A requirement worded as a preference is not a requirement.
LANG_SOFT_RX = re.compile(
    r"\ba plus\b|nice to have|advantage|preferr?ed|bonus|desirable|an asset|beneficial"
    r"|would be (?:great|good|nice)|welcome|ideally|helpful|not required|optional", re.I)

# Postings put optional skills under their own heading, so the qualifier sits on a
# different line from the requirement it qualifies. A Stripe ad listing "Proficiency in
# Italian" under "Preferred qualifications" -- and stating outright that "the preferred
# qualifications are a bonus, not a requirement" -- read as a hard Italian requirement
# until this existed. Whichever heading is nearest above the match decides.
_SOFT_SECTION = re.compile(
    r"\b(?:preferr?ed|nice[- ]to[- ]have|bonus|desirable|good to have|advantageous"
    r"|optional|pluses|extra credit|even better)\b", re.I)
_HARD_SECTION = re.compile(
    r"\b(?:minimum|required|requirements|must[- ]have|essential|basic qualifications"
    r"|what you(?:'ll| will)? need|who you are|about you)\b", re.I)
_SECTION_LOOKBACK = 700

def _in_soft_section(text, pos):
    """True when the nearest preceding section heading marks optional criteria."""
    before = text[max(0, pos - _SECTION_LOOKBACK):pos]
    soft = max((m.start() for m in _SOFT_SECTION.finditer(before)), default=-1)
    hard = max((m.start() for m in _HARD_SECTION.finditer(before)), default=-1)
    return soft > hard

def _first_matching_sentence(text, hard, soft=None, section_aware=False):
    """The words that trip `hard` without a softener beside them, quoted for the drop log.
    Returned rather than a bare True so every drop can be reviewed against the ad's own
    wording on the dashboard -- a wrong drop has to be visible to be fixable."""
    text = text or ""
    for sent in _SENTENCE.finditer(text):
        s = sent.group(0)
        if len(s.strip()) < 12:
            continue
        m = hard.search(s)
        if not m:
            continue
        if section_aware and _in_soft_section(text, sent.start() + m.start()):
            continue
        # Normally the sentence is the right unit of context. When punctuation has been
        # flattened it is not, and quoting from its start would point at wording hundreds
        # of characters from the phrase that actually matched.
        if len(s) > _MAX_SENTENCE:
            s = s[max(0, m.start() - _CONTEXT_WINDOW):m.end() + _CONTEXT_WINDOW]
        if soft and soft.search(s):
            continue
        return re.sub(r"\s+", " ", s).strip()[:200]
    return ""

def says_no_sponsorship(desc):
    # No softener list: NO_SPONSOR_RX only fires on a negator bound tightly to the
    # sponsorship word, so a sentence advertising that the company DOES sponsor cannot
    # reach it in the first place.
    return _first_matching_sentence(desc, NO_SPONSOR_RX)

def requires_other_language(desc):
    # section_aware: a language listed under "Preferred qualifications" is a preference even
    # though its own bullet reads like a requirement. Sponsorship terms never appear under
    # such a heading, so says_no_sponsorship() deliberately does not use this.
    return _first_matching_sentence(desc, LANG_HARD_RX, LANG_SOFT_RX, section_aware=True)

# Title intelligence from the job-application-workflow skill, as code. First match wins,
# so the most disqualifying bands are checked first.
#
# A band is a SIGNAL, not a verdict. It used to drive a set of hard score ceilings; those
# are gone (see the note above apply-time flags below). The band is now passed to the
# scoring model as context and surfaced as a dashboard flag, so a title that reads junior
# gets looked at rather than silently buried. An "Analyst" at a tier-1 employer routinely
# carries manager-level scope and a band well clear of the visa floor, and the skill's own
# title intelligence says exactly that about Senior Associate / Strategy & Ops Associate
# roles at Stripe, Booking and Uber.
TITLE_BANDS = [
    ("wrong_function", re.compile(r"deal desk|quote[- ]to[- ]cash|order management"
                                 r"|billing|accounts (payable|receivable)", re.I)),
    ("director_plus", re.compile(r"\bdirector\b|head of|\bvp\b|vice president|chief "
                                 r"|\bsvp\b|\bevp\b|\bcro\b|\bcoo\b", re.I)),
    ("analyst", re.compile(r"\banalyst\b", re.I)),
    ("specialist", re.compile(r"\bspecialist\b|\bcoordinator\b", re.I)),
]

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
     "of dedicated RevOps required) penalises hard. Senior Manager fits when the posting "
     "welcomes adjacent backgrounds. Judge the level from the POSTING, not the title noun: "
     "read the scope, the reporting line, whether it owns a team or a system end to end, "
     "the years-of-experience band, and any stated salary. An Analyst, Specialist, "
     "Associate or Coordinator title is a prompt to check, not an automatic penalty -- at a "
     "tier-1 employer these routinely carry manager-level scope and a band well clear of "
     "the visa floor, and the market uses 'Senior Analyst, Sales Strategy & Operations' for "
     "work that is Manager-grade elsewhere. Penalise the level only when the posting itself "
     "reads junior: 0-3 years wanted, execution-only or admin duties, reporting into a "
     "Manager with no ownership, or a stated band below the market's visa floor."),
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
# The model supplies the six dimension scores and a handful of facts it can only get by
# reading the posting; the arithmetic happens here. score_job() does not trust a total the
# model reports -- on the 51 stored rows from before this existed, 21 of those totals were
# more than 0.6 off the weighted sum of their own dimensions, the worst by 3.8.
#
# What is deliberately NOT here any more: score ceilings. There used to be a CAPS table and
# an apply_caps() that clamped the weighted total for a title band, a below-floor salary, a
# language requirement, an off-target function reading, or a CSM role outside NL. The
# job-application-workflow skill has no such mechanism -- it weights six dimensions, gates
# at 5, and says the raw fit score is never adjusted. The caps also produced results the
# rubric disagreed with: a RevOps Specialist role that scored 6.5 on the dimensions landed
# at 4.0 purely because "Specialist" was in the title, and roles in Strategy & Operations
# were being pushed under the dashboard gate before anyone read the JD.
#
# Of those five considerations, three now travel as a FLAG (see score_flags below):
# off-target function, title band, and CSM outside NL. They still get reported and still
# show on the dashboard; they inform rather than overwrite. Where one of these should
# genuinely move the number, it belongs inside a dimension score -- that is what the rubric
# guidance tells the model to do.
#
# The other two -- a below-floor stated salary and a hard non-English language requirement
# -- are not flags at all. Both are absolute the same way says_no_sponsorship() and
# requires_other_language() already are: a role Tom cannot legally take, or cannot actually
# do, is not worth a fit score. deep_score_disqualifier() drops these before scoring runs,
# using the same facts the model reports (see SCORE_SCHEMA), rather than clamping a score
# the role was never going to keep.

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

def salary_floor_flag(market, obs):
    """A note when a salary is actually stated, in the market's own currency, and below its
    visa floor; "" otherwise. No FX guessing: a GBP figure is never compared to a EUR floor.

    Despite the name this is no longer a dashboard flag -- deep_score_disqualifier() below
    uses it to drop the job outright, the same way says_no_sponsorship() and
    requires_other_language() drop on a regex match before the model ever runs. A role that
    cannot clear the visa floor on its own stated salary is not one Tom can take, so there
    is nothing to show a flag on; the name stays because the function itself -- find the
    note, or don't -- hasn't changed. Most EU postings state no salary at all, and the ones
    that do often state a range whose bottom is a negotiating position rather than the
    offer, which is exactly why this only fires on a real, market-matched, stated figure."""
    if not market or not obs.get("salary_stated"):
        return ""
    try:
        low = float(obs.get("salary_min_base") or 0)
    except (TypeError, ValueError):
        return ""
    floor, cur = VISA_FLOORS.get(market, (0, ""))
    if low <= 0 or not floor:
        return ""
    stated = (obs.get("salary_currency") or "").upper()
    if stated and stated != cur:
        return ""
    if low < floor:
        return f"stated salary {int(low)} {cur} below the {market} visa floor ({floor} {cur})"
    return ""

def deep_score_disqualifier(job, obs):
    """Hard disqualifiers that only the deep scorer can catch, because they need a real
    reading of the full JD rather than a sentence-level regex match. Returns (stage, reason)
    to pass straight to record_drop(), or (None, None) when the role is not disqualified.

    This is policy-identical to says_no_sponsorship() / requires_other_language() -- an
    absolute fact ends the application, so the job is dropped before it reaches the
    dashboard rather than scored and flagged. Those two run on the raw text before either
    model call; this runs on what the model actually understood after reading the posting,
    catching the hard requirements the regex wording missed -- an oddly-phrased language
    requirement, a salary band buried in prose -- not a second, softer judgement on the
    same two facts. Language is checked first only because a role that fails both checks
    can only be logged with one reason."""
    if obs.get("language_hard_requirement"):
        return "language-required", ("deep score: posting requires non-English fluency "
                                      "(missed by the wording-based check)")
    note = salary_floor_flag(job.get("market"), obs)
    if note:
        return "below-visa-floor", note
    return None, None

def score_flags(job, obs):
    """Risk notes shown on a scored row. Language and below-floor salary are NOT here --
    both are hard disqualifiers now (deep_score_disqualifier()) and a job carrying either
    never reaches this function, because it never reaches the dashboard at all."""
    flags = []
    market = job.get("market")
    title = job.get("title")

    if obs.get("function_match") == "off_target":
        flags.append("model read the function as off-target")

    band = title_band(title)
    if band == "wrong_function":
        flags.append("title band: off-target function")
    elif band in ("analyst", "specialist", "director_plus"):
        # Deliberately worded as an instruction to look, not as a judgement. These are the
        # bands that were being auto-buried; the whole point of the change is that the JD
        # decides, not the noun in the title.
        flags.append(f"title band: {band} -- check the JD for actual scope and comp")

    # CSM track: a primary target in NL, a weaker one elsewhere unless the company is a
    # genuine standout (see the CSM track weighting section of profile.md). The model is
    # told this in the profile and reflects it in the dimensions; this is just the note.
    if market in ("UK-London", "IE-Dublin", "BE") and is_csm_title(title) \
            and not obs.get("company_standout"):
        flags.append(f"CSM in {market} at a non-standout company")

    return flags

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

You may kill a role for exactly TWO reasons. Nothing else is grounds for a kill.

1. LOCATION - the role is not in one of the target markets above.
2. FUNCTION - the role is unambiguously outside the candidate's target functions. That means: engineering or data engineering, product management, finance or accounting, quota-carrying sales (AE, SDR, BDR, account executive, business development), deal desk / quote-to-cash / billing / order management, HR or People Ops, procurement, legal, and operations that are not commercial in nature (retail store ops, restaurant ops, manufacturing, supply chain, logistics, facilities, clinical, NGO programme delivery).

NEVER KILL ON SENIORITY. This is the single most important rule here, and getting it wrong is expensive. Analyst, Senior Analyst, Specialist, Coordinator, Associate, Senior Associate, Business Partner, Lead, Manager, Senior Manager, Principal, Director and Head of are ALL keeps. Do not kill something for being "too junior", "entry level", "below Manager", "Director+ exceeds target", or any variant of that reasoning. A title is not a seniority: an "Analyst" or "Associate" at a strong employer routinely carries manager-level scope and pay well above a junior band, and a "Head of" at a 40-person startup is often a hands-on Manager role. The deep scorer reads the full posting - the scope, the reporting line, the years-of-experience band, the stated salary - and weighs seniority properly there. You cannot see enough to make that call. The ONLY seniority-shaped exception: genuine internships, working-student roles, apprenticeships and graduate schemes may be killed.

IN SCOPE - never kill these on function. A title filter in code has already decided they are on target, and the deep scorer weights them properly:
- Revenue operations, sales operations, CS operations, business operations, commercial operations
- Sales strategy, revenue strategy, GTM strategy, business strategy, commercial strategy
- ANY "Strategy & Operations" or "Strategy and Operations" or "Strategy, Planning & Operations" or "S&O" role, including partner-scoped, field-scoped, segment-scoped, regional and international variants. These are core target roles, not generalist strategy jobs. Do not kill one because it sits inside a partner, product, marketing or regional org - the deep scorer takes the domain into account.
- GTM systems, RevOps systems, revenue systems, revenue technology, RevOps architecture, CRM and GTM tool-stack ownership. Systems ownership is a target track, not "tool administration".
- Revenue analytics, sales/incentive compensation, quota and territory design
- Renewals and renewal management, sales enablement, revenue enablement
- Senior / Principal / Enterprise / Strategic Customer Success

Renewals is NOT quota-carrying sales for this purpose. Enablement is NOT marketing-ops admin. Systems ownership is NOT engineering. Kill one of these only when the location is wrong or the employer is plainly outside B2B tech and the work is plainly not commercial (a supermarket's "Sales Operations Manager" running store rotas, for instance).

Be lenient - when unsure, KEEP. A wrong keep costs one cheap scoring call. A wrong kill loses a job Tom would have applied to, and he never sees it.

Reply with ONLY this JSON: {{"keep": true or false, "reason": "<max 12 words>"}}"""


def score_system():
    dims = "\n\n".join(f"- {label} ({w}%): {guide}" for _, label, w, guide in RUBRIC)
    return f"""You deeply score a job posting against this candidate's real profile. Be rigorous and honest; this gates whether the candidate spends time applying.

CANDIDATE PROFILE:
{load_profile()}

SCORING RUBRIC - score each dimension 0-10 against the guidance given:

{dims}

Calibration, so the dimension scores land on a consistent scale: 8-10 is a bullseye worth applying to immediately, 7 a strong fit with manageable gaps, 6 borderline and worth it only when the pipeline is thin, 5 barely at the bar, 4 and below not worth applying to. Do not inflate to be encouraging.

Do NOT compute a total. The weighted total is computed in code from the six dimension scores you give, and for every role that actually gets scored nothing overrides it afterwards -- there are no caps or ceilings. Every consideration that should move the score has to land inside a dimension: if the posting reads junior, that belongs in Seniority Fit; if the function is off-target, that belongs in Domain and Career Trajectory.

Two facts are handled differently: a stated salary below the market's visa floor, and a posting that makes another language (other than English) a hard requirement to do the job. Neither gets scored at all -- code drops the role outright the moment you report either one true, the same way it already drops a role whose ad rules out sponsorship. Do NOT fold either into a dimension score, and do NOT soften your reading of either one because you like the rest of the role -- report salary_stated / salary_min_base / salary_currency and language_hard_requirement exactly as the posting states them. A wrong "false" here puts a role in front of Tom that he cannot actually take; a wrong "true" throws away a role that was fine.

A title band (analyst / specialist / director_plus / normal) is given to you in the job details. It is a signal to read the posting carefully, not a verdict. Do not mark a role down merely because its title contains "Analyst", "Specialist", "Associate" or "Coordinator" -- score what the posting actually describes.

Alongside the dimensions, report these observations from the posting:
- function_match: "core" for RevOps / GTM strategy / sales ops / CS ops / revenue or sales strategy, or a Senior/Principal CSM role. "adjacent" for a related commercial-ops role that isn't quite one of those. "off_target" for deal desk, quote-to-cash, billing, pure marketing-ops admin, quota-carrying sales, engineering, or finance.
- company_standout: true only if the employer is a genuine tier-1 SaaS or strong-brand technology company. This decides whether a CSM role outside the Netherlands gets a flag.
- language_hard_requirement: true only when the posting makes another language (Dutch, German, French, ...) a hard requirement to do the job -- "fluency required", "must speak", "native/business-level X required". False when it is merely preferred, a plus, advantageous, or nice to have. This one DROPS the role -- see above.
- salary_stated / salary_min_base / salary_currency: the annual base-salary floor of any stated range, as a number, with its ISO currency code. Report the base only -- exclude bonus, commission, equity, and holiday allowance. If no salary is stated, set salary_stated false, salary_min_base 0, salary_currency "". A stated figure below the market's visa floor DROPS the role -- see above.

The market (Netherlands / Belgium / UK-London / Ireland-Dublin) has already been resolved in code and is given to you in the job details. Trust it. Do not second-guess whether the location qualifies, and do not penalise a location that has been accepted.

Sponsor handling: a "sponsor" field may be given. "not on register" is a -1 to -2 caution on Location & Visa (registers use legal names and miss trading names), NOT an auto-zero. "sponsor" or "sponsor (likely)" is a plus for UK/NL roles. Ignore sponsor for Ireland.

Salary: if not stated, do NOT penalise on salary; judge comp risk from the seniority and the company.

Working pattern: on-site or hybrid in the resolved market is normal and expected -- the candidate is relocating for the role and needs an employer with an office there. Do NOT treat an on-site or hybrid requirement, a named-office requirement, or the absence of remote flexibility as a risk, and do not raise a flag about it.

flags: short risk notes, [] if none. Do not add a flag for missing comp, a title band, or the CSM-outside-NL case -- those are added in code from the observations above, and duplicating them crowds out anything genuinely new you noticed. Never add a flag for language or salary either way -- report them accurately in the fields above and say nothing more; code decides whether the role is dropped.
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
# sample is stored. Counts stay exact for every stage regardless. Keyed on the FULL stage
# name: keyed on the "prefilter" group instead, the 60-row budget was spent entirely on
# title rejects before the first location reject was reached, so run 1 counted 174 location
# drops and retained none of them to look at.
DROP_SAMPLE_CAP = {"prefilter:title": 60, "prefilter:location": 40}
DROP_MAX_ROWS = 400
# How many rows of each stage to keep in the committed file, newest first. Stage-1 kills,
# scoring errors and the hard disqualifiers are the ones worth actually reading, so they
# get the most room.
DROP_KEEP_PER_STAGE = {"prefilter": 80, "age": 40, "dedupe": 40,
                       "stage1-kill": 120, "score-error": 40, "sponsor-required": 40,
                       "no-sponsorship": 60, "language-required": 60,
                       "below-visa-floor": 60}
DROP_KEEP_DEFAULT = 40

_DROP_SEEN = set()

def record_drop(job, stage, reason):
    # A prefilter drop is counted as title-vs-location, so the footer says which half of the
    # gate is doing the work rather than lumping thousands of rows under one number.
    if stage == "prefilter":
        stage = "prefilter:" + reason.split(":", 1)[0]
    DROP_COUNTS[stage] = DROP_COUNTS.get(stage, 0) + 1
    cap = DROP_SAMPLE_CAP.get(stage)
    if cap is not None:
        # Sample for variety, not volume: 60 rows all reading "Account Manager / no
        # target-function keyword" tell you nothing, so only the first of each
        # title+reason pair is stored.
        fingerprint = (stage, (job.get("title") or "").lower(), reason)
        if fingerprint in _DROP_SEEN:
            return
        if sum(1 for d in DROPS if d["stage"] == stage) >= cap:
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

_BLOCK_TAG = re.compile(r"</?(?:p|br|div|li|ul|ol|tr|h[1-6]|section|table)\b[^>]*>", re.I)

def strip_html(t):
    """Unescape entities FIRST, then drop tags. The old order did it backwards, so a source
    that returns escaped markup -- Greenhouse's `content` is "&lt;p&gt;As a Customer Success
    Manager..." -- had nothing tag-shaped to strip, and the unescape step then turned the
    entities into live <p> tags that went to the model and the dashboard as-is.

    Block-level tags become newlines rather than spaces. Postings state their requirements
    as bullets with no trailing punctuation, so without a line break per item the whole
    list collapses into one run-on "sentence" and the disqualifier checks lose the ability
    to tell "Dutch is a plus" from a genuine fluency requirement two bullets away."""
    text = _BLOCK_TAG.sub("\n", html.unescape(t or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()

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
    """None if the row passes the free filters; otherwise a short reason for the drop log.

    The title half of this gate is market-aware, not purely textual: a plain "Customer Success
    Manager" is admitted in the Netherlands and nowhere else (see CSM_ANY). The market is
    resolved once here and reused for the location check below."""
    t = title or ""
    market = market_of(country, location)
    if not (INCLUDE_TITLE.search(t) or (market == "NL" and CSM_ANY.search(t))):
        return "title: no target-function keyword"
    m = EXCLUDE_TITLE.search(t)
    if m:
        return f"title: excluded term '{m.group(0).strip()}'"
    if market is None:
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

def recent_enough(posted_raw, max_days=None):
    """True if within max_days old. Fails open (keeps the job) when the source gives no
    usable date at all, so a missing field never silently wipes out a whole source.

    max_days resolves at call time rather than in the signature, so --ignore-age can raise
    MAX_POST_AGE_DAYS for one run and have it actually take effect here."""
    dt = parse_date_loose(posted_raw)
    if dt is None:
        return True
    return (datetime.now(timezone.utc) - dt) <= timedelta(
        days=MAX_POST_AGE_DAYS if max_days is None else max_days)

# ---------------------------------------------------------------- Adzuna (NL + UK)

def adzuna_salary(j, cc):
    """The pay to forward for one Adzuna result, or "" when there is none worth trusting.

    salary_is_predicted="1" means Adzuna MODELLED the figure from the title and location --
    it is not in the ad, and it comes back with salary_min == salary_max. Passing one on
    made the scoring model report salary_stated: true and fired the -4.0 below-floor cap on
    a number the employer never published: on run 1 that cost LogicMonitor 6.1 -> 4.0 and
    Windward 5.8 -> 4.0. Only pay actually listed in the ad is forwarded."""
    if not j.get("salary_min") or str(j.get("salary_is_predicted", "0")) == "1":
        return ""
    low = int(j["salary_min"])
    high = int(j.get("salary_max") or j["salary_min"])
    return f"{low}-{high} {ADZUNA_COUNTRIES.get(cc, '')}".strip()

def fetch_adzuna(app_id, app_key, diag):
    out, ids = [], set()
    for cc in ADZUNA_COUNTRIES:
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
                        out.append({
                            "id": jid,
                            "company": (j.get("company") or {}).get("display_name", ""),
                            "title": title, "location": loc, "country": cc,
                            "market": market_of(cc, loc),
                            "url": j.get("redirect_url", ""), "source": "adzuna",
                            "description": strip_html(j.get("description", "")),
                            "salary": adzuna_salary(j, cc), "posted_at": j.get("created", ""),
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

# Greenhouse's public board pages render client-side and carry no JobPosting JSON-LD, so a
# posting reached by its board URL rather than through the ATS feed (revopsroles links out
# this way) came back empty and fell through to a one-line synthesized summary. The board
# URL maps straight onto the same API greenhouse_desc already reads.
GREENHOUSE_BOARD_URL = re.compile(
    r"^https://(?:job-)?boards\.greenhouse\.io/([\w-]+)/jobs/(\d+)")

def greenhouse_board_desc(url):
    m = GREENHOUSE_BOARD_URL.match((url or "").split("?")[0])
    if not m:
        return ""
    return greenhouse_desc(f"https://boards-api.greenhouse.io/v1/boards/"
                           f"{m.group(1)}/jobs/{m.group(2)}")

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

# Workday job URLs come in two shapes, both of which render the posting in JavaScript --
# so the HTML a plain GET returns holds only the page furniture ("We go beyond the
# obvious...", "Job Details"), never the requirements, and there is no JobPosting JSON-LD
# for jsonld_job_description() to find either. That is how a Kantar role whose ad ends
# "We're not able to offer visa sponsorship" reached the scorer as 300 characters of
# marketing copy. Both shapes expose the same posting through Workday's public CxS JSON
# API, which needs no key.
WORKDAY_URL = re.compile(
    r"^(?P<base>https://(?:(?P<tenant>[\w-]+)\.)?(?:wd\d+\.myworkdayjobs|"
    r"wd\d+\.myworkdaysite)\.com)/(?:recruiting/(?P<tenant2>[\w-]+)/)?(?P<site>[\w-]+)"
    r"/(?:(?P<locale>[a-z]{2}-[A-Z]{2})/)?job/(?P<path>.+)$")

def workday_cxs_url(url):
    """Rewrite a Workday job URL to its CxS JSON endpoint, or return "" if it isn't one.

    tenant.wdN.myworkdayjobs.com/site/job/PATH  -> .../wday/cxs/tenant/site/job/PATH
    wdN.myworkdaysite.com/recruiting/tenant/site/job/PATH -> .../wday/cxs/tenant/site/job/PATH
    An optional /xx-XX/ locale segment sits before /job/ and is dropped."""
    m = WORKDAY_URL.match((url or "").split("?")[0].rstrip("/"))
    if not m:
        return ""
    tenant = m.group("tenant2") or m.group("tenant")
    if not tenant:
        return ""
    return f"{m.group('base')}/wday/cxs/{tenant}/{m.group('site')}/job/{m.group('path')}"

def workday_desc(url):
    api = workday_cxs_url(url)
    if not api:
        return ""
    try:
        r = get(api, headers={"User-Agent": "Mozilla/5.0 (job-radar; personal use)",
                              "Accept": "application/json"})
        r.raise_for_status()
        return strip_html((r.json().get("jobPostingInfo") or {}).get("jobDescription", ""))
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

# Source-specific description fetchers, tried before the generic ones below.
DETAIL_FETCHERS = {"greenhouse": greenhouse_desc, "linkedin": linkedin_desc,
                   "revopsroles": jsonld_job_description}
# Tried for any source once the specific one has been exhausted. The two URL-rewriting
# fetchers cost nothing when the URL isn't theirs -- they return "" without a request.
GENERIC_FETCHERS = (workday_desc, greenhouse_board_desc, jsonld_job_description)
# Fetchers that make no request unless the URL matches their host, so trying them is free.
_FREE_IF_NO_MATCH = {workday_desc: workday_cxs_url,
                     greenhouse_board_desc: lambda u: GREENHOUSE_BOARD_URL.match(
                         (u or "").split("?")[0])}
MAX_DESC_FETCHES = 3     # network calls per job, so a board of thin ads can't stall a run

def fill_description(job):
    """Return the fullest description obtainable for one job, fetching if need be.

    A stored description is only believed when it is long enough to be a real posting.
    The old gate fetched only when the field was empty, so a board that returns a career
    page's marketing copy instead of the ad silently won: Kantar was scored 7.3 off 300
    characters of Workday page furniture while its actual 4,800-character ad ended
    "We're not able to offer visa sponsorship ... for this role"."""
    best = job.get("description") or ""
    if len(best) >= MIN_DESC_CHARS:
        return best
    specific = DETAIL_FETCHERS.get(job.get("source"))
    targets = []
    for t in (job.get("_detail"), job.get("url")):
        if t and t not in targets:
            targets.append(t)

    attempts = []
    for i, target in enumerate(targets):
        # An API that serves this exact URL goes first: it returns the canonical text and
        # keeps the ad's punctuation, where scraping the same posting flattens the
        # requirements list into one run-on line and costs the softener checks their
        # sentence boundaries. The source's own fetcher only understands its own _detail
        # URL, so it is tried against the first target only.
        exact = tuple(f for f, matches in _FREE_IF_NO_MATCH.items() if matches(target))
        for fetcher in exact + ((specific,) if specific and i == 0 else ()) + GENERIC_FETCHERS:
            # Don't spend budget on a URL-rewriting fetcher that can't handle this host.
            if fetcher in _FREE_IF_NO_MATCH and not _FREE_IF_NO_MATCH[fetcher](target):
                continue
            if (fetcher, target) not in attempts:
                attempts.append((fetcher, target))

    for fetcher, target in attempts[:MAX_DESC_FETCHES]:
        text = fetcher(target) or ""          # each fetcher swallows its own errors
        if len(text) > len(best):
            best = text
        if len(best) >= MIN_DESC_CHARS:
            break
    return best or job.get("_fallback_desc") or ""

# ---------------------------------------------------------------- revopsroles.com

# Direct scraping (the old approach: regex-extracting the JSON blob embedded in
# revopsroles.com/locations/{country} pages) started hitting Vercel's bot/attack-
# challenge on 2026-07-31 -- every request, even with a real browser UA, comes back
# as a "Vercel Security Checkpoint" interstitial (x-vercel-mitigated: challenge),
# most likely keyed on datacenter/proxy IP reputation, which also describes GitHub
# Actions runners. Same failure mode as hiring.cafe's direct API above, so this
# takes the same fix: read the data through a channel the site isn't blocking --
# here, Tom's own daily digest email, which he's subscribed his Gmail address to
# specifically for this.
GMAIL_IMAP_HOST = "imap.gmail.com"
REVOPSROLES_SENDER = "hello@mail.revopsroles.com"
REVOPSROLES_LOOKBACK_DAYS = 4   # covers a missed run (e.g. a quiet weekend) without
                                 # re-scanning the whole mailbox; reprocessing an
                                 # already-seen job is harmless, seen.json dedupes it

def fetch_revopsroles(gmail_address, gmail_app_password):
    """Parses Tom's revopsroles.com daily digest email (read via Gmail IMAP) instead of
    scraping the site directly. No full description field is present in the digest, so
    the real JD is lazy-fetched from the job's revopsroles.com page via
    jsonld_job_description() for survivors, same pattern as Greenhouse/LinkedIn --
    though that fetch is itself likely to hit the same bot-challenge, so a short
    synthesized summary (category/seniority/work mode) is kept as a fallback."""
    out, seen_ids = [], set()
    imap = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST)
    try:
        imap.login(gmail_address, gmail_app_password)
        imap.select("INBOX", readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=REVOPSROLES_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        typ, data = imap.search(None, f'(FROM "{REVOPSROLES_SENDER}" SINCE {since})')
        if typ != "OK":
            return out
        for mid in data[0].split():
            typ, msg_data = imap.fetch(mid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            posted = ""
            try:
                posted = email.utils.parsedate_to_datetime(msg["Date"]).timestamp()
            except Exception:
                pass
            body = ""
            for part in (msg.walk() if msg.is_multipart() else [msg]):
                if part.get_content_type() == "text/html":
                    charset = part.get_content_charset() or "utf-8"
                    body = (part.get_payload(decode=True) or b"").decode(charset, errors="replace")
                    break
            if not body:
                continue
            for chunk in body.split('<a href="https://revopsroles.com/jobs/')[1:]:
                m = re.match(r'([0-9a-fA-F-]+)"[^>]*>(.*?)</a>(.*)', chunk, re.S)
                if not m:
                    continue
                jid, title_raw, rest = m.groups()
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                bump_raw("revopsroles", 1)
                title = clean_text(title_raw)
                region = rest[:1500]   # bounds the field search to this job's own block
                cl = re.search(r'>([^<]+)<!--\s*-->\s*·\s*([^<]+)</span>', region)
                company, loc = (clean_text(cl.group(1)), clean_text(cl.group(2))) if cl else ("", "")
                sal_m = re.search(r'color:#16a34a[^"]*">([^<]+)</span>', region)
                salary = clean_text(sal_m.group(1)) if sal_m else ""
                tags_m = re.search(r'margin-top:8px">(.*?)</div>', region, re.S)
                tags = re.findall(r'>([^<]+)</span>', tags_m.group(1)) if tags_m else []
                category = tags[0] if len(tags) > 0 else ""
                seniority = tags[1] if len(tags) > 1 else ""
                work_mode = tags[2] if len(tags) > 2 else ""
                cc = country_code(loc.rsplit(",", 1)[-1]) if "," in loc else ""
                reason = prefilter(title, loc, cc)
                if reason:
                    record_drop({"id": f"rr-{jid}", "title": title, "location": loc,
                                 "company": company, "source": "revopsroles"},
                                "prefilter", reason)
                    continue
                summary = "; ".join(f"{label}: {v}" for label, v in (
                    ("Category", category), ("Seniority", seniority), ("Work mode", work_mode),
                ) if v)
                src_url = f"https://revopsroles.com/jobs/{jid}"
                out.append({
                    "id": f"rr-{jid}", "company": company,
                    "title": title, "location": loc, "country": cc,
                    "market": market_of(cc, loc),
                    "url": src_url, "source": "revopsroles", "salary": salary,
                    "posted_at": posted,
                    "_detail": src_url, "_fallback_desc": summary,
                })
    finally:
        try:
            imap.logout()
        except Exception:
            pass
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
# Short labels for the searches above, same order, used only for the per-search status line.
APIFY_SEARCH_LABELS = ["revops-broad", "cs-nl", "cs-senior", "cs-grc"]
APIFY_MAX_ITEMS = 200   # across all four searches combined; ~$0.25/run at $1.25/1000 results

def fetch_apify_hiringcafe(token, diag=None):
    """Runs Tom's saved hiring.cafe searches through the Apify actor
    memo23/apify-hiring-cafe-scraper. Each search already encodes its own
    location/title/language filters; dateFetchedPastNDays=21 in the searches is wider
    than our own MAX_POST_AGE_DAYS, so the age filter downstream still applies.

    One actor call per search, not one call for all four startUrls together. A combined
    call was silently returning a flat ~30 items total run after run for six-plus weeks
    regardless of how the market moved -- an Atlassian Amsterdam CSM req that Tom found
    browsing hiring.cafe directly, and that a live fetch of just the cs-nl search alone
    returns near the top of 15 results, never once appeared in that combined feed. The
    actor's own docs only document a global maxItems, not a per-URL cap, so the most
    likely explanation is the four searches starving each other (or the actor only
    paginating the first of them) inside one run -- calling separately, each with its own
    budget, is the direct fix and also gives a raw count per search (diag) instead of one
    opaque total, so a search silently going quiet again is visible in the status footer."""
    out = []
    per_url_budget = max(1, APIFY_MAX_ITEMS // len(APIFY_HIRINGCAFE_SEARCHES))
    for label, url in zip(APIFY_SEARCH_LABELS, APIFY_HIRINGCAFE_SEARCHES):
        try:
            r = requests.post(
                f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
                params={"token": token},
                json={"startUrls": [url], "maxItems": per_url_budget,
                      "enrichDescription": True},
                timeout=280)
            r.raise_for_status()
            items = r.json()
        except Exception as e:
            if diag is not None:
                diag[f"hiringcafe:{label}"] = f"FAIL: {e}"
            continue
        if diag is not None:
            diag[f"hiringcafe:{label}"] = f"raw {len(items)}"
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

def sample_desc(desc, cap=None):
    """Fit a description into `cap` characters keeping both ends. A plain head slice drops
    the closing block, and that is where sponsorship terms, language requirements and comp
    are stated -- the two rows this pipeline got wrong were both decided by a sentence in
    the last fifth of the ad."""
    cap = cap or DESC_CHAR_CAP
    desc = desc or ""
    if len(desc) <= cap:
        return desc
    marker = "\n[...]\n"
    head = int((cap - len(marker)) * DESC_HEAD_SHARE)
    tail = cap - len(marker) - head
    return desc[:head] + marker + desc[-tail:]

def job_message(job):
    desc = sample_desc(job.get("description"))
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

def parse_score_result(job, data):
    """Turn the model's parsed JSON into either a disqualification or a scored result. Split
    out from score_job() so the decision can be unit-tested against a synthetic `data` dict
    with no API call.

    A disqualified result is {"disqualified": True, "stage": ..., "reason": ...} -- ready to
    hand straight to record_drop(), same shape the caller already uses for the pre-model
    hard disqualifiers. A scored result has no "disqualified" key at all, so
    `result.get("disqualified")` is the one check the caller needs."""
    stage, reason = deep_score_disqualifier(job, data)
    if stage:
        return {"disqualified": True, "stage": stage, "reason": reason}

    dims = {k: max(0.0, min(10.0, float((data.get("dimensions") or {}).get(k, 0) or 0)))
            for k in RUBRIC_KEYS}
    raw = weighted_total(dims)
    # The weighted sum IS the score. Nothing clamps it.
    score = round(raw, 1)
    # Code-derived flags first: they are the ones that used to be caps, so they matter most
    # and must not be pushed off the end of the list by the model's own commentary.
    flags = score_flags(job, data)
    if not data.get("salary_stated"):
        flags.append("comp not listed, verify vs floor")
    flags += [str(f)[:70] for f in (data.get("flags") or [])]
    return {
        # score_raw and caps_applied are still written so rows scored under the old cap
        # engine keep rendering alongside new ones. caps_applied is always empty now.
        "score": score, "score_raw": score, "caps_applied": [],
        "dimensions": dims,
        "tier": job.get("market") or "outside target markets",
        "flags": flags[:10], "verdict": str(data.get("verdict", ""))[:180],
    }

def score_job(api_key, system, job):
    """The model scores the six dimensions and reports what it read off the posting; the
    total is computed here, and so is the decision to drop the role outright rather than
    score it (see deep_score_disqualifier()). Opus 5 thinks by default -- do not disable it,
    which on this model can leak reasoning into the visible answer."""
    text = _claude_call(
        api_key, CLAUDE_SCORE_MODEL, system, job_message(job), SCORE_MAX_TOKENS,
        cache_system=True,
        extra={"output_config": {"effort": SCORE_EFFORT,
                                 "format": {"type": "json_schema", "schema": SCORE_SCHEMA}}})
    data = _extract_json(text)
    return parse_score_result(job, data)

# ---------------------------------------------------------------- main

def load_json(path, default):
    try:
        return json.load(open(path)) if os.path.exists(path) else default
    except Exception:
        return default

def cmd_selftest():
    """Replay the stored dimension scores through the current engine and report every row
    whose score moves. No network, no API key.

    The score is now just the weighted sum of the six dimensions, so anything that moves is
    a row the old cap engine had clamped. Each one should name the cap that did it, which
    makes this the check that the caps are genuinely gone rather than merely unreferenced.

    A row capped for language or a below-visa-floor salary is a special case worth calling
    out separately: those two are hard disqualifiers now (deep_score_disqualifier()), not
    scored roles with a caveat. This replay can only show the score moving up, because it
    has no way to re-run the disqualifier check on stored data -- on an actual rescan that
    row disappears from the dashboard instead."""
    jobs = load_json("docs/jobs.json", [])
    moved = up = would_now_drop = 0
    print(f"Replaying {len(jobs)} stored rows through weighted_total (no caps)\n")
    for j in jobs:
        dims = j.get("dimensions") or {}
        if not dims:
            continue
        new = round(weighted_total(dims), 1)
        old = j.get("score", 0)
        if abs(new - old) > 0.05:
            moved += 1
            up += new > old
            caps = j.get("caps_applied") or []
            was = "; ".join(caps) or "no cap recorded"
            hard_drop = any("visa floor" in c or "non-English fluency" in c for c in caps)
            would_now_drop += hard_drop
            marker = "  [now a hard drop, not a score]" if hard_drop else ""
            print(f"  {old:>4} -> {new:<4}  {str(j.get('title'))[:42]:<44}"
                  f" {str(j.get('company',''))[:18]:<20} {was[:60]}{marker}")
    print(f"\n{moved} of {len(jobs)} rows move, {up} of them upward.\n"
          "Every mover should name the cap that used to hold it down. A row that moves with "
          "'no cap recorded'\nmeans its stored total disagreed with its own dimensions -- "
          "worth inspecting.\n"
          f"{would_now_drop} of the movers are marked [now a hard drop, not a score]: on an "
          "actual rescan those\ndisappear from the dashboard entirely rather than landing at "
          "the higher number shown here.\n"
          "These are replays, not rescores: the rows keep their old dimension scores, which "
          "were produced\nunder the previous rubric wording. Re-running the scan will move "
          "some of them again.")

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

def cmd_unkill_history(days=14):
    """Like --unkill, but reaches back through git history instead of only the file on disk.

    docs/excluded.json keeps a bounded sample per stage (DROP_KEEP_PER_STAGE), so the
    committed copy holds ~120 stage-1 kills while a week of scans actually produced closer
    to 240. Every one of them was committed at the time, so the history has them all. This
    walks the commits, unions the kills, and frees their ids from seen.json.

    Written for the switch away from the cap engine: the old stage-1 prompt killed
    Analyst/Specialist/Associate/Director titles and Strategy & Operations roles outright,
    and those need re-evaluating under the new rules rather than staying lost."""
    try:
        shas = subprocess.check_output(
            ["git", "log", f"--since={days} days ago", "--format=%H", "--", "docs/excluded.json"],
            text=True, stderr=subprocess.DEVNULL).split()
    except Exception as e:
        print(f"git log failed ({e}); falling back to the committed file only.")
        shas = []

    killed, titles = {}, {}
    def absorb(rows):
        for r in rows:
            if r.get("stage") in ("stage1-kill", "score-error") and r.get("id"):
                killed[str(r["id"])] = r.get("stage")
                titles[str(r["id"])] = f"{r.get('title','?')} - {r.get('company','?')}"

    absorb(load_json("docs/excluded.json", {}).get("rows", []))
    for sha in shas:
        try:
            blob = subprocess.check_output(["git", "show", f"{sha}:docs/excluded.json"],
                                           text=True, stderr=subprocess.DEVNULL)
            absorb(json.loads(blob).get("rows", []))
        except Exception:
            continue   # a commit that predates the file, or a bad blob; skip it

    seen = set(load_json("seen.json", []))
    freed = set(killed) & seen
    json.dump(sorted(seen - freed), open("seen.json", "w"))

    print(f"Walked {len(shas)} commits of docs/excluded.json over the last {days} days.")
    print(f"Found {len(killed)} distinct stage-1 kills / scoring errors; freed {len(freed)} "
          f"from seen.json ({len(killed) - len(freed)} were already absent).\n")
    for jid in sorted(freed, key=lambda i: titles.get(i, "")):
        print(f"  {titles[jid]}")
    print("\nThese only come back if they are STILL LIVE in a source feed -- excluded.json\n"
          "stores no URL, so there is nothing to re-fetch a dead posting from. Most will\n"
          "also be older than MAX_POST_AGE_DAYS by now, so run the next scan with\n"
          "--ignore-age or the age filter will drop them again immediately:\n"
          "    python scan.py --ignore-age")

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
    if "--unkill-history" in sys.argv:
        days = 14
        if "--days" in sys.argv:
            try:
                days = int(sys.argv[sys.argv.index("--days") + 1])
            except (IndexError, ValueError):
                print("--days needs a number; using 14.")
        return cmd_unkill_history(days)
    if "--unkill" in sys.argv:
        return cmd_unkill()
    if "--rescore" in sys.argv:
        return cmd_rescore()

    if "--ignore-age" in sys.argv:
        # One-run escape hatch for the backfill: rows freed by --unkill-history are older
        # than the 7-day cutoff by definition, so without this the age filter drops every
        # one of them again before they reach the scorer.
        global MAX_POST_AGE_DAYS
        MAX_POST_AGE_DAYS = 3650
        print("--ignore-age: age filter relaxed for this run.")

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
            jobs = fetch_apify_hiringcafe(apify_token, diag); found += jobs
            src_status["hiring.cafe (Apify)"] = f"{src_line('hiring.cafe', len(jobs))} | " + "; ".join(f"{k.split(':')[1]}={v}" for k, v in diag.items() if k.startswith("hiringcafe:"))
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

    # 7. revopsroles.com (parsed from Tom's daily digest email via Gmail IMAP; direct
    # scraping is blocked by Vercel's bot-challenge since 2026-07-31)
    gmail_addr, gmail_pw = os.environ.get("GMAIL_ADDRESS", ""), os.environ.get("GMAIL_APP_PASSWORD", "")
    if gmail_addr and gmail_pw:
        try:
            jobs = fetch_revopsroles(gmail_addr, gmail_pw); found += jobs
            src_status["revopsroles.com"] = src_line("revopsroles", len(jobs))
        except Exception as e:
            src_status["revopsroles.com"] = f"FAIL: {e}"
    else:
        src_status["revopsroles.com"] = "skipped: no GMAIL_ADDRESS/GMAIL_APP_PASSWORD set"

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

    for j in new_jobs[:MAX_SCREENED_PER_RUN]:
        # Get the real posting text before anything reads it -- only for survivors of the
        # title/location prefilter, so the fetches stay cheap. Every downstream decision
        # (the two disqualifier checks below, both model calls) is only as good as this.
        j["description"] = fill_description(j)
        j["desc_chars"] = len(j["description"])   # kept so a score can be audited later
        j.pop("_detail", None); j.pop("_fallback_desc", None)

        # Hard disqualifiers, read off the full description before either model sees it.
        # These are absolute -- no score is worth computing for a role that has ruled Tom
        # out -- so they drop the job and save the Haiku and Opus calls.
        quote = says_no_sponsorship(j["description"])
        if quote:
            record_drop(j, "no-sponsorship", f'JD: "{quote}"')
            seen.add(j["id"]); continue
        quote = requires_other_language(j["description"])
        if quote:
            record_drop(j, "language-required", f'JD: "{quote}"')
            seen.add(j["id"]); continue

        which, raw, label = sponsor_for(j)
        j["sponsor_region"], j["sponsor_raw"], j["sponsor"] = which or "", raw, label
        if SPONSOR_REQUIRED and raw == "not_found":
            record_drop(j, "sponsor-required", f"company not on the {which} sponsor register")
            seen.add(j["id"]); continue

        if dry or not api_key:
            j.update({"score": 0, "score_raw": 0, "caps_applied": [], "dimensions": {},
                      "tier": j.get("market") or "", "flags": [], "verdict": "(not scored)"})
            j["found_at"] = now_iso()
            j["description"] = sample_desc(j.get("description"), DESC_STORE_CAP)
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
            result = score_job(api_key, system_score, j)
        except Exception as e:
            # Deliberately NOT added to seen: a transient failure used to write score 0 and
            # mark the job seen forever, which needed a manual commit to undo. Now it just
            # retries on the next run.
            record_drop(j, "score-error", str(e)[:160])
            print(f"  ERR   {j['title']} @ {j.get('company') or j['source']} ({e})")
            continue
        if result.get("disqualified"):
            # A hard disqualifier the deep scorer alone caught -- non-English fluency or a
            # below-visa-floor stated salary, read from the full posting rather than matched
            # by the pre-model regex. Same policy as says_no_sponsorship() /
            # requires_other_language() above: dropped outright, not scored, not shown.
            record_drop(j, result["stage"], result["reason"])
            seen.add(j["id"])
            print(f"  drop  {j['title']} @ {j.get('company') or j['source']} "
                  f"({result['stage']}: {result['reason']})")
            continue
        j.update(result)
        # Stored head + tail, matching what the scorer read, so a surprising score can be
        # checked against the part of the ad that decided it.
        j["description"] = sample_desc(j.get("description"), DESC_STORE_CAP)
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
