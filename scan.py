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
  python scan.py --dedupe    collapse duplicates already on the dashboard, without scanning
  python scan.py --backfill-jd [--days N]
                             re-fetch the full ad for rows already on the dashboard that
                             never got one: cut short by the old 1200-char DESC_STORE_CAP,
                             or a stub still holding a folded-away duplicate's link
                             (default: the 7 days the dashboard renders). Free; a row that
                             crosses from stub to real posting is then RESCORED, which is
                             the one part that needs ANTHROPIC_API_KEY -- without it the
                             text is recovered and the stale scores are named.
"""

import email, html, imaplib, json, os, re, subprocess, sys, time, urllib.parse
from datetime import datetime, timezone, timedelta
import requests
import sponsors as spon
import submit

# ---------------------------------------------------------------- config

# Adzuna covers NL + UK only (its API has no Ireland). Ireland comes from JobSpy + ATS.
# Mapped to the ISO currency of each country's salary figures: Adzuna reports pay with no
# currency at all, so the code has to supply it. The old format put the country's display
# name in the currency slot ("44231-44231 United Kingdom local"), leaving the scoring model
# to infer "GBP" from the words -- and salary_floor_flag() checks that inference against the
# market's currency before deep_score_disqualifier() drops the role on it.
ADZUNA_COUNTRIES = {"nl": "EUR", "gb": "GBP", "ca": "CAD", "us": "USD"}
# Which phrase set each country gets. Europe takes the full list; North America takes the
# RevOps-proper subset, for two reasons. The narrow one is that the US prefilter gate
# (REVOPS_CORE) would drop the rest on arrival anyway, so fetching them is paid-for volume
# thrown away. The wider one is the call budget: Adzuna's nominal free tier is 1,000 calls
# a month and 2 countries x 9 phrases x 2 pages x 4 runs a day already runs well past it,
# so doubling the country count on the full phrase list is not something to do casually.
ADZUNA_CORE_COUNTRIES = ("us", "ca")
# Targeted phrase queries. A broad word-OR query sorted by date just surfaces the
# freshest generic "operations/customer/revenue" noise, none of which passes the title
# filter (that was the original "Adzuna returned nothing" bug). Precise phrases return
# on-target roles. Each phrase is sent as Adzuna `what` (matches the words together).
ADZUNA_PHRASES = [
    "revenue operations", "sales operations", "revenue strategy", "sales strategy",
    "go to market strategy", "revenue enablement", "customer success operations",
    "business operations manager", "commercial operations",
]
# The North American subset of ADZUNA_PHRASES. Deliberately a filter over that list rather
# than a second literal, so a phrase added above cannot be silently missing here.
ADZUNA_CORE_PHRASES = ["revenue operations", "sales operations", "revenue strategy",
                       "sales strategy", "go to market strategy",
                       "customer success operations"]
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
# The US subset, kept as a filter over the list above so a term added there cannot be
# silently missing here. "customer success operations" survives because it IS core RevOps;
# plain CSM was never in this list.
JOBSPY_CORE_TERMS = [t for t in JOBSPY_TERMS if t != "customer success operations"]

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
# London Area UK, Belgium, Netherlands, Amsterdam, Ireland, then the two new ones:
# United States and Canada. Queried one geoId at a time (the guest endpoint paginates
# wrongly when they are combined), and every row still passes through the same
# prefilter()/market_of() gate, so a US row that is not remote is dropped downstream.
LINKEDIN_GEO_IDS = ["90009496", "100565514", "102890719", "103100785", "104738515",
                    "103644278", "101174742"]
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

# Customer success WITH a seniority qualifier. These are the same clauses INCLUDE_TITLE
# already carries; named separately so the US gate can admit senior CS without admitting
# the plain title, and so the two cannot silently diverge -- a test asserts everything
# SENIOR_CS matches is also matched by INCLUDE_TITLE.
#
# The qualifier requirement is what keeps a bare "Customer Success Manager" out. That title
# is a primary target in the Netherlands and nowhere else (CSM_ANY, above), and widening it
# by accident is the exact failure this guards.
SENIOR_CS = re.compile(
    r"(senior|principal|lead|enterprise|strategic).{0,20}customer success"
    r"|customer success.{0,30}(senior|principal|lead|enterprise|strategic)"
    r"|(manager|head of),?\s+(of\s+)?customer success", re.I)

# The narrow gate for US roles, one of two (see SENIOR_CS below). The US is a
# financial-runway backstop rather than a place Tom wants to move to, so it is only worth
# the radar's attention for the pivot proper -- RevOps, not the adjacent-and-arguable end
# of INCLUDE_TITLE. What it still drops outright is renewals and generic "business
# operations", both legitimate European targets.
#
# It used to drop customer success in every band too. That changed: US senior CS is wanted
# now, on the grounds that the US searches confirm remote and the salary band, so
# prefilter() admits SENIOR_CS alongside this. Plain "Customer Success Manager" is still
# Netherlands-only, which falls out of SENIOR_CS requiring a seniority qualifier.
#
# This regex does most of the cost control for the whole US expansion. The US is a far
# bigger market than the four European ones combined, and this runs before any model call,
# so the volume dies for free rather than at $0.001 a row in stage one. Every phrase in
# HC_REVOPS_TITLES has to match here or the search pays for rows this then throws away --
# which is how "sales enablement" and "commercial operations" came to be missing. A test
# pins the pairing in both directions.
REVOPS_CORE = re.compile(
    r"revenue operations|revops|rev ops|sales operations|sales ops"
    r"|cs operations|customer success operations"
    r"|revenue strategy|sales strategy|revenue analytics|revenue systems"
    r"|revenue technology|revenue enablement|sales enablement|commercial operations"
    # GTM needs an operations/strategy noun beside it, in either word order. A bare \bgtm\b
    # admitted "GTM Recruiter, AMER" and "Staff, Analytics Engineer, GTM Data Science" on
    # the first US run -- a recruiting role and an engineering role. Both would have died
    # at the Haiku screen, but the whole point of this gate is that US volume dies for
    # free, before anything is spent on it.
    r"|(gtm|go[- ]to[- ]market)[ ,&-]{1,3}(strategy|operations|ops|enablement|analytics"
    r"|systems|planning)"
    r"|(strategy|operations|ops|enablement|analytics|systems)[ ,&-]{1,3}(gtm|go[- ]to[- ]market)"
    r"|sales compensation|incentive compensation"
    r"|territory (planning|design|management|operations)"
    r"|strategy[ ,&]{1,3}(and[ ,&]{1,3})?(planning|business|revenue|sales|commercial)?[ ,&]{0,3}"
    r"(op(eration)?s)\b"
    r"|\bs ?& ?o\b", re.I)

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
# All of Ireland, not just Dublin. Dublin dominates the RevOps market, but Dublin is also
# where the cost of living eats the salary advantage, so a role in Cork or Galway is more
# attractive rather than less. There used to be an IE_OTHER_CITY here that rejected Cork,
# Galway, Limerick and Waterford outright, and the stage-1 prompt said "Ireland/Dublin" to
# match, so Haiku killed the rest: "Burnfoot, County Donegal" and "Dunshaughlin, County
# Meath" were both dropped as "not Dublin commuter". Both the regex and both prompts now
# treat the whole country as in scope.
#
# The county forms matter as much as the city names: the feeds emit "County Donegal",
# "Dublin 8, County Dublin" and "Dublin, Leinster, Ireland", so bare city names alone miss
# a real share of Irish rows.
IE_CITY = re.compile(
    r"\b(?:dublin|cork|galway|limerick|waterford|kilkenny|sligo|drogheda|dundalk"
    r"|athlone|wexford|tralee|maynooth|letterkenny|killarney|ennistymon|dun laoghaire"
    r"|leinster|munster|connacht)\b", re.I)
# Irish counties, but only where the text says so. "Louth", "Clare", "Bray" and "Meath" are
# British place names as well (Louth in Lincolnshire, Clare in Suffolk, Bray in Berkshire),
# and a bare match on those would route a UK row to Ireland AND bypass the London-only
# rule, because UK_OTHER_CITY does not list them. Requiring the "County"/"Co." prefix makes
# the match safe; the far more common "<town>, County X, Ireland" shape is already caught by
# IE_COUNTRY on the country half of the string.
IE_COUNTY = re.compile(
    r"\b(?:county|co\.?)\s+(?:dublin|cork|galway|limerick|waterford|kilkenny|sligo"
    r"|wexford|kildare|wicklow|louth|meath|donegal|mayo|kerry|clare|tipperary|westmeath"
    r"|laois|offaly|cavan|monaghan|leitrim|roscommon|longford|carlow)\b", re.I)
IE_COUNTRY = re.compile(r"ireland|ierland|\beire\b", re.I)
# reject pure-remote and EMEA-wide postings that aren't anchored to a target city
REMOTE_ONLY = re.compile(r"\b(remote|anywhere|work from home|wfh|emea|europe)\b", re.I)

# ---------------------------------------------------------------- North America
# Canada and the US are in scope on different terms from Europe, and they bring three name
# collisions that have to be resolved before the European city anchors run, not after:
#
#   "London, ON"     is Canada, and UK_LONDON would otherwise claim it.
#   "Ontario, CA"    is California, so CA_PROVINCE must NOT contain a bare "CA".
#   "Amsterdam, NY"  is not the Netherlands, and NL_CITY would otherwise claim it.
#
# So market_of() resolves a North American context first and only falls through to Europe
# when there is none. Full province and state names are matched anywhere in the string;
# the two-letter codes only count after a comma, which is the "City, ST" shape the feeds
# actually emit and which stops "ON"/"IN"/"OR"/"ME" matching ordinary English.
CA_PROVINCE = re.compile(
    r"\b(?:ontario|quebec|qu\u00e9bec|british columbia|alberta|manitoba|saskatchewan"
    r"|nova scotia|new brunswick|newfoundland|labrador|prince edward island|yukon"
    r"|nunavut|northwest territories)\b"
    r"|,\s*(?:ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)\b", re.I)
CA_CITY = re.compile(
    r"\b(?:toronto|vancouver|montreal|montr\u00e9al|calgary|edmonton|ottawa|winnipeg"
    r"|quebec city|hamilton|kitchener|waterloo|mississauga|brampton|burnaby|richmond hill"
    r"|markham|vaughan|oakville|burlington ontario|halifax|victoria bc|saskatoon|regina"
    r"|windsor ontario|kelowna|gatineau|laval|longueuil|oshawa|barrie|guelph|kingston "
    r"ontario|sherbrooke|moncton|st\. john\u2019s|st\. johns|saint john)\b", re.I)
CA_COUNTRY = re.compile(r"\bcanada\b|\bcanadian\b", re.I)

US_STATE = re.compile(
    r"\b(?:alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware"
    r"|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana"
    r"|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana"
    r"|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina"
    r"|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina"
    r"|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia"
    r"|wisconsin|wyoming|district of columbia)\b"
    r"|,\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|MD|MA|MI|MN|MS"
    r"|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
    r"|ME|OR)\b", re.I)
# "united states" and "u.s." are safe case-insensitively. A bare "US"/"USA" is not -- "us"
# is an ordinary English word -- so that alternative is matched case-sensitively.
US_COUNTRY = re.compile(r"united states|u\.s\.a?\.?|\bstateside\b", re.I)
US_COUNTRY_ABBR = re.compile(r"\b(?:US|USA)\b")
# What counts as remote for a US row. Deliberately separate from REMOTE_ONLY, which exists
# to REJECT unanchored European postings; here remote is the requirement, so this is a
# whitelist rather than a blacklist. "anywhere" alone is not enough: "anywhere in EMEA" is
# not a US remote role.
US_REMOTE = re.compile(
    r"\bremote\b|\bwork from home\b|\bwfh\b|\bdistributed\b|\btelecommute\b"
    r"|\bhome[- ]based\b|\banywhere in the u", re.I)
# A remote scope that is explicitly somewhere else, so a "Remote - EMEA" req carrying a US
# country code is not read as a US remote role.
REMOTE_ELSEWHERE = re.compile(r"\bemea\b|\beurope\b|\bapac\b|\blatam\b|\bglobal\b", re.I)


def north_america_of(loc, cc):
    """'CA' / 'US-Remote' / None for a row that looks North American, before Europe is
    considered. Returns None both for "not North America" and for a US row that fails the
    remote requirement -- the caller cannot act differently on those two, because a US
    onsite role is as out of scope as a German one."""
    ca_ctx = bool(CA_COUNTRY.search(loc) or CA_PROVINCE.search(loc) or CA_CITY.search(loc)
                  or cc == "ca")
    us_ctx = bool(US_COUNTRY.search(loc) or US_COUNTRY_ABBR.search(loc)
                  or US_STATE.search(loc) or cc == "us")

    # Canada wins a tie: "Vancouver, WA" and "Ontario, CA" both carry a US state code, and
    # the state code is the more specific signal, so only treat the row as Canadian when
    # there is no US context alongside it.
    if ca_ctx and not us_ctx:
        return "CA"                      # anywhere in Canada, remote or not
    if us_ctx:
        # Remote is a hard requirement, and it is the only US requirement about place: a
        # remote role anchored to another state is fine, an onsite role is not.
        if US_REMOTE.search(loc) and not REMOTE_ELSEWHERE.search(loc):
            return "US-Remote"
        return None
    return None



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
# Only the markets Tom actually wants buzz his phone. A US role can score 8 on the strength
# of the role itself while still being a place he does not want to move to, and a push
# notification is a claim on his attention right now rather than an entry on a list he
# reads when he chooses. Canada and the US stay on the dashboard; they just do not ring.
NTFY_MAX_TIER = 2
KEEP_DAYS = 45
# How far back a row is worth a board lookup. It used to match MAX_POST_AGE_DAYS exactly,
# on the reasoning that a row he cannot see is a row whose board nobody is waiting to
# know -- but the dashboard keeps rows for KEEP_DAYS (45), not 7, so that was leaving
# three quarters of the visible board permanently unlooked-up. Two weeks is a compromise:
# it covers the rows he is realistically still deciding on, and the per-company cache
# means the extra reach costs each employer once, not once a run.
BOARD_LOOKUP_DAYS = 14
# New companies looked up per run. The cache makes each one a one-off cost, so this is
# only about keeping any single scan short.
BOARD_LOOKUPS_PER_RUN = 60
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
# How much of the ad is kept in docs/jobs.json. This was 1200 for a long time, sized for
# auditing a score after the fact, which was all that file was for. It now feeds the
# application workflow -- the dashboard's copy button hands the stored text to the
# job-application-workflow skill, and applyq.py reads the same field into seven prompts that
# each ask sample_desc() for 3000-6000 characters and were silently getting 1200. A
# head-and-tail sample is fine for checking a score and useless for tailoring a bullet,
# because the part it drops is the middle, where the responsibilities are.
#
# 30000 is not a sample size, it is a sanity guard: the longest ad on record is 23,994
# characters, so every real posting stores whole and only something pathological gets cut.
# The cost of going from 1200 to here is about 2.2MB on a file that keeps 45 days of rows
# (KEEP_DAYS), roughly 0.5MB gzipped over the wire, which the dashboard pays on load.
DESC_STORE_CAP = 30000        # the whole ad, not a sample
MAX_SCREENED_PER_RUN = 80     # cap stage-1 Haiku calls
MAX_SCORED_PER_RUN = 30       # cap stage-2 Opus calls (survivors only)
SPONSOR_REQUIRED = False      # if True, drop UK/NL jobs whose company isn't on a register

# Dashboard bands. Mirrored in docs/index.html and written into docs/status.json each run
# so the page reads them from here rather than keeping its own copy.
# Raised from 6.0/5.0 once the map went from four markets to six. The 478 scored rows of
# the previous 45 days sat 233 above 6.0, 156 above 6.5 and 88 above 7.0, so 6.5 is about
# 3.5 "apply" roles a day, which is what Tom's own time actually allows. Worth knowing: the
# score distribution peaks at 5.5-6.5, so the bar lands on the mode and the borderline band
# is doing real work rather than catching strays.
GATE = 6.5     # 6.5+ -> "apply" section
FLOOR = 6.0    # 6.0-6.4 -> collapsed "borderline"; below -> collapsed "excluded"

# Annual base-salary floors per market, local currency (2026). The prose version lives in
# profile.md for the model to reason with; this is the copy the below-floor cap uses.
# Belgium takes the lowest of the three regional Blue Card floors (Brussels) so the cap
# only fires when the salary is below every Belgian threshold.
VISA_FLOORS = {
    "NL": (71304, "EUR"),          # HSM, 30+ bracket
    "BE": (56976, "EUR"),          # EU Blue Card, Brussels
    "UK-London": (70000, "GBP"),   # Skilled Worker
    "IE": (68911, "EUR"),          # Critical Skills / General permit, nationwide
}

# Tom's own floor, which is a different kind of thing from a visa floor and so lives in its
# own table rather than being smuggled into that one. There is no legal minimum for a US
# role -- he is a citizen -- but the US is only worth taking if it pays enough to make
# staying somewhere he does not want to live comfortable for his family. Below this it is
# not a trade worth making, so the role is dropped rather than scored.
#
# Canada has no entry, deliberately: no visa floor applies (citizen) and he has set no comp
# floor for it.
COMP_FLOORS = {
    "US-Remote": (130000, "USD"),
}


def floor_for(market):
    """(amount, currency, kind) for a market's salary floor, or (0, "", "") if it has none.

    Two tables, one lookup, because salary_floor_flag() needs to word the drop differently:
    a visa floor is a legal fact about what Tom can be hired at, a comp floor is his own
    decision about what is worth taking."""
    if market in VISA_FLOORS:
        amount, cur = VISA_FLOORS[market]
        return amount, cur, "visa"
    if market in COMP_FLOORS:
        amount, cur = COMP_FLOORS[market]
        return amount, cur, "comp"
    return 0, "", ""

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
     "Manager is the target zone for this pivot (MBA + 11 yrs adjacent SaaS). Too senior "
     "(Director/Head of, or 10+ years of dedicated RevOps required) penalises hard. Senior "
     "Manager fits only with a referral or when the posting explicitly welcomes adjacent "
     "backgrounds. Judge the level from the POSTING, not the title noun: read the scope, the "
     "reporting line, whether it owns a team or a system end to end, the years-of-experience "
     "band, and any stated salary. At a tier-1 employer an Analyst, Specialist, Associate or "
     "Coordinator title routinely carries manager-level scope, and the market uses 'Senior "
     "Analyst, Sales Strategy & Operations' for work that is Manager-grade elsewhere. But a "
     "2-4 year experience ceiling screened against 11 years plus an MBA is a real filter risk, "
     "so score these titles 4-6 depending on how hard the posting's experience ceiling reads. "
     "Apply that as a single penalty for overqualification risk only. Do NOT also penalise "
     "them on compensation here -- comp is assessed separately against actual salary data, "
     "never inferred from the title noun. Penalise the level further only when the posting "
     "itself reads junior: 0-3 years wanted, execution-only or admin duties, or reporting into "
     "a Manager with no ownership."),
    ("domain", "Domain / Industry Fit", 15,
     "B2B SaaS or tech is strong (8-10). GRC/compliance/legal/regulatory adds a familiarity "
     "bonus but is not required for a high score. Non-tech, non-SaaS (manufacturing, retail, "
     "government) is weaker (4-6)."),
    ("location_visa", "Location & Visa", 15,
     "This dimension is Tom's PREFERENCE ordering over places to live, not a measure of how "
     "easy a market is to get hired in. Those two come apart hardest on the US, where he has "
     "no visa problem at all and still does not want to live there. Do not score a market up "
     "for being administratively easy. The market is resolved in code and given to you; "
     "score where it sits in the ordering below, adjusted for sponsorship realism and comp "
     "certainty within it. Never re-litigate whether the location qualifies.\n"
     "  * Netherlands, company shown as a sponsor or likely sponsor: 9-10. This is the goal, "
     "where Tom and his wife want to live.\n"
     "  * Netherlands, sponsor status not confirmed: 7. Same preference, real execution risk.\n"
     "  * Ireland: 8. Strong market and the safest permit salary thresholds. Cost of living "
     "is the real drag and is lower outside Dublin, so a role in Cork or Galway is not a "
     "worse outcome than a Dublin one.\n"
     "  * UK-London: 7-8. More expensive than Ireland and they would rather live in Ireland, "
     "but London comp is strong enough to make it worth more in practice.\n"
     "  * Belgium: 6. A backup that keeps them in the EU.\n"
     "  * Canada: 4-5. Tom is a Canadian citizen by descent but holds no certificate yet, so "
     "the employer lift is a letter rather than visa sponsorship -- genuinely lighter than "
     "Europe -- against an unproven timeline and a discretionary expedite with no fallback "
     "if it is refused. Use 5 when the employer is large enough to absorb a delayed start or "
     "the posting signals a flexible start date; 4 when it signals urgency (immediate start, "
     "backfill, ASAP).\n"
     "  * US: 2-3. A financial-runway backstop, not a destination. Scoring it higher for "
     "being frictionless is exactly the error to avoid.\n"
     "  * US where the job details show a Transfer field naming NL, IE, UK or CA: 4-5. An "
     "internal move later is a route abroad without changing employer, and sponsorship is a "
     "lighter ask once they know you. Still hard, but strictly better than a US-only "
     "employer."),
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

def _as_float(v):
    """Tolerant float(): the model reports salary figures as numbers, but a null or a
    stray string shouldn't take down the whole parse."""
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0

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
    """A note when a salary is actually stated, in the market's own currency, and below that
    market's floor; "" otherwise. No FX guessing: a GBP figure is never compared to a EUR
    floor.

    Despite the name this is no longer a dashboard flag -- deep_score_disqualifier() below
    uses it to drop the job outright, the same way says_no_sponsorship() and
    requires_other_language() drop on a regex match before the model ever runs. A role that
    cannot clear the floor on its own stated salary is not one Tom can take, so there is
    nothing to show a flag on; the name stays because the function itself -- find the note,
    or don't -- hasn't changed. Most EU postings state no salary at all, and the ones that
    do often state a range whose bottom is a negotiating position rather than the offer,
    which is exactly why this only fires on a real, market-matched, stated figure.

    "Stated" means stated in the posting. The figure reaching this function comes from the
    deep scorer reading the JD, which is the only salary source in the pipeline that can be
    trusted to gate on: Adzuna reports an ESTIMATE for most rows, and adzuna_salary()
    already discards anything flagged salary_is_predicted precisely so a guessed number can
    never reach here and drop a real role."""
    if not market or not obs.get("salary_stated"):
        return ""
    try:
        low = float(obs.get("salary_min_base") or 0)
    except (TypeError, ValueError):
        return ""
    floor, cur, kind = floor_for(market)
    if low <= 0 or not floor:
        return ""
    stated = (obs.get("salary_currency") or "").upper()
    if stated and stated != cur:
        return ""
    if low < floor:
        if kind == "visa":
            return (f"stated salary {int(low)} {cur} below the {market} visa floor "
                    f"({floor} {cur})")
        return (f"stated salary {int(low)} {cur} below the {floor} {cur} floor for taking "
                f"a {market} role at all")
    return ""

def us_comp_unstated(job):
    """A reason string when a US row carries no salary at all, "" otherwise.

    The third code-level hard disqualifier, alongside says_no_sponsorship() and
    requires_other_language(), and it runs in the same place: before either model call, so
    it costs nothing.

    Tom only wants US roles that are CONFIRMED to pay well. Both US hiring.cafe searches
    set restrictJobsToTransparentSalaries, so this is the code agreeing with the search
    rather than second-guessing it: a US row that reached here without a number on it came
    from a source that does not publish comp, and there is no way to establish it is worth
    leaving Europe on the table for.

    Absence, not value. The value check is downstream and better placed: COMP_FLOORS via
    salary_floor_flag() drops a US role whose JD-STATED base is under the floor, reading
    the figure the deep scorer took off the posting. The feed's salary string is not the
    posting, so it is trusted for "is there a number" and nothing more.

    How this lands per source: hiring.cafe supplies yearly_min_compensation, JobSpy
    supplies min_amount when it has one, LinkedIn supplies nothing, and Adzuna's estimates
    are already discarded by adzuna_salary() for being salary_is_predicted -- so an Adzuna
    US row has no salary and is dropped, which is the right answer for a filter whose whole
    point is "confirmed"."""
    if job.get("market") != "US-Remote":
        return ""
    if (job.get("salary") or "").strip():
        return ""
    return ("no salary on the posting; a US role is only worth taking at confirmed pay "
            f"(floor {COMP_FLOORS['US-Remote'][0]} {COMP_FLOORS['US-Remote'][1]})")


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
        # Two stage names for one check, because they mean different things and both are
        # worth being able to read separately in the drop log: a visa floor is a legal
        # fact about what Tom can be hired at in Europe, a comp floor is his own decision
        # that a US role below it is not worth leaving Europe on the table for. The old
        # name is kept for the visa case so historical rows keep rendering.
        _amt, _cur, kind = floor_for(job.get("market"))
        return ("below-visa-floor" if kind == "visa" else "below-comp-floor"), note
    return None, None

def market_conflict(job, obs):
    """{"stated": ..., "resolves_to": ...} when the posting's own location contradicts the
    market resolved in code, else None.

    This exists because a feed can simply be wrong about where a job is -- hiring.cafe put
    a Canadian role in Dublin -- and the market is not a cosmetic label downstream. It picks
    the CV's contact block, and it decides how submit.work_status() answers the visa
    questions ON THE FORM: an Irish-tagged Canadian role tells the employer Tom needs
    sponsorship and is not authorised to work there, which is wrong twice over for a
    Canadian citizen. A wrong phone number is an inconvenience; that is a self-inflicted
    rejection.

    The deep scorer is the only component that reads the whole posting, and it is
    deliberately told to TRUST the resolved market rather than argue with it. So it reports
    the location verbatim instead, and the comparison happens here, in code, through
    market_of() -- the same single source of truth the location gate and the dashboard use,
    so there is no third opinion about what a place name means. (findform.same_market() is
    this comparison as a one-liner, but it calls market_of() itself and we need the
    resolved value to report, so that would be the same lookup twice.)

    A stated location that resolves to no target market is NOT a conflict. "Remote - EMEA",
    "Global" and a bare country next to remote wording all land there, and treating them as
    disagreements would fire on most rows while telling us nothing."""
    stated = (obs.get("posting_location") or "").strip()
    market = job.get("market") or ""
    if not stated or not market:
        return None
    resolves = market_of("", stated)
    if not resolves or resolves == market:
        return None
    return {"stated": stated[:120], "resolves_to": resolves}

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
    if market in ("UK-London", "IE", "BE", "CA", "US-Remote") and is_csm_title(title) \
            and not obs.get("company_standout"):
        flags.append(f"CSM in {market} at a non-standout company")

    # Thin evidence, said out loud on the row as well as in the prompt. A score built on a
    # title and a location is not the same kind of number as one built on a posting, and
    # the difference has to be visible on the card rather than only in the transcript --
    # especially because the two hard disqualifiers cannot fire on text this short, so a
    # role that rules out sponsorship in its JD passes them silently.
    if is_thin(job.get("description")):
        flags.append("thin evidence: no real JD retrieved, sponsorship and language "
                     "checks could not run")

    conflict = market_conflict(job, obs)
    if conflict:
        flags.append(f"location conflict: the posting says {conflict['stated']} "
                     f"({conflict['resolves_to']}), not {market}")

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
        "posting_location": {"type": "string"},
        "flags": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string"},
    },
    "required": ["dimensions", "function_match", "company_standout",
                 "language_hard_requirement", "salary_stated", "salary_min_base",
                 "salary_currency", "posting_location", "flags", "verdict"],
    "additionalProperties": False,
}

# The one market list, used by both prompts. Kept next to VISA_FLOORS so adding a market
# updates the floors, the caps, and both prompts together -- the old hardcoded copy in the
# screen prompt had already drifted (it listed Belgium as a target but omitted it from the
# reject clause).
MARKETS_SENTENCE = ("Netherlands (anywhere), Belgium (anywhere), UK London area and commuter "
                    "belt only, Ireland (anywhere in the country, not just Dublin), Canada "
                    "(anywhere, remote or on-site), and the US (REMOTE ONLY, anywhere in "
                    "the country, core RevOps or senior customer success only, and the "
                    "posting must state a salary)")
REJECT_SENTENCE = ("Germany, Spain, other UK cities, on-site or hybrid US roles, "
                   "remote-from-anywhere, and remote-EMEA roles")

SCREEN_SYSTEM = f"""You are a fast pre-screen for a job-search pipeline. Decide if a role is worth a full evaluation for this candidate:

11 years B2B SaaS (Customer Success + Account Management), pivoting into Revenue Operations / GTM Strategy / Sales Ops / CS Ops at Manager or senior-IC level. Also open to Senior/Principal Customer Success Manager roles. US citizen, so any European role needs employer visa sponsorship; also a Canadian citizen by descent, so Canadian roles need no sponsorship.

Target markets ONLY: {MARKETS_SENTENCE}. Reject {REJECT_SENTENCE}.

You may kill a role for exactly TWO reasons. Nothing else is grounds for a kill.

1. LOCATION - the role is not in one of the target markets above. Europe is the priority and the US is a distant last resort, but that is a question of SCORE, not of keep/kill: a US or Canadian role that reached you has already passed both a remote check and a narrow RevOps title gate in code, so never kill one for being North American, or for being ranked low. The deep scorer handles the ranking.
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

Two facts are handled differently: a stated salary below the market's floor (a visa floor in Europe, and in the US Tom's own floor for whether the role is worth taking at all), and a posting that makes another language (other than English) a hard requirement to do the job. Neither gets scored at all -- code drops the role outright the moment you report either one true, the same way it already drops a role whose ad rules out sponsorship. Do NOT fold either into a dimension score, and do NOT soften your reading of either one because you like the rest of the role -- report salary_stated / salary_min_base / salary_currency and language_hard_requirement exactly as the posting states them. A wrong "false" here puts a role in front of Tom that he cannot actually take; a wrong "true" throws away a role that was fine.

Thin evidence: some rows arrive with no retrievable posting text at all, and those say "EVIDENCE: THIN" where the description would be. Treat that as a real constraint on how high you can score, not as a neutral absence. Do not fill the gap with what a role of that title usually involves -- the whole point of reading the posting is that titles mislead, and on these rows you have not read one. Score each dimension on what is actually stated and no further, cap the total's optimism accordingly, and note the missing evidence in your verdict. A thin row that looks like a 7 is a 7 you cannot support; 5 to 6 is the honest range unless the title and market alone genuinely settle it. Both hard disqualifier checks are also unrun on these rows, so do not treat a silent posting as a clean one.

A title band (analyst / specialist / director_plus / normal) is given to you in the job details. It is a signal to read the posting carefully, not a verdict. Do not mark a role down merely because its title contains "Analyst", "Specialist", "Associate" or "Coordinator" -- score what the posting actually describes.

Alongside the dimensions, report these observations from the posting:
- function_match: "core" for RevOps / GTM strategy / sales ops / CS ops / revenue or sales strategy, or a Senior/Principal CSM role. "adjacent" for a related commercial-ops role that isn't quite one of those. "off_target" for deal desk, quote-to-cash, billing, pure marketing-ops admin, quota-carrying sales, engineering, or finance.
- company_standout: true only if the employer is a genuine tier-1 SaaS or strong-brand technology company. This decides whether a CSM role outside the Netherlands gets a flag.
- language_hard_requirement: true only when the posting makes another language (Dutch, German, French, ...) a hard requirement to do the job -- "fluency required", "must speak", "native/business-level X required". False when it is merely preferred, a plus, advantageous, or nice to have. This one DROPS the role -- see above.
- salary_stated / salary_min_base / salary_currency: the annual base-salary floor of any stated range, as a number, with its ISO currency code. Report the base only -- exclude bonus, commission, equity, and holiday allowance. If no salary is stated, set salary_stated false, salary_min_base 0, salary_currency "". Report only a figure the POSTING states; never carry over an estimate from a job board. A stated figure below the market's floor DROPS the role -- see above.
- posting_location: the work location the POSTING itself states, copied as it is written ("Toronto, ON", "Amsterdam, Netherlands", "Remote - US"). This is a transcription, not an opinion: report what the ad says even when it disagrees with the market you were given, and especially then. Give the location of the job, not the company's headquarters, and where several offices are listed give the one the role is actually based in. Use "" when the posting genuinely does not say, which is common -- do not infer one from the company, the currency or the language of the ad.

The market (NL / BE / UK-London / IE / CA / US-Remote) has already been resolved in code and is given to you in the job details. Trust it for SCORING. Do not second-guess whether the location qualifies, and do not penalise a location that has been accepted. If the posting's own location contradicts that market, posting_location is where that goes and the only place it goes: report what the ad says there, score the market you were given, and let code reconcile the two. Do not mention the disagreement in a dimension score, in flags or in the verdict. Where it sits in Tom's preference ordering is the whole of the Location & Visa dimension -- see that dimension's guidance, and note that the ordering is about where he wants to live, not about which market is easiest to get hired in.

Sponsor handling: a "sponsor" field may be given. "not on register" is a -1 to -2 caution on Location & Visa (registers use legal names and miss trading names), NOT an auto-zero. "sponsor" or "sponsor (likely)" is a plus for UK/NL roles, and for the Netherlands specifically it is what separates the 9-10 band from the 7 band. Ignore sponsor entirely for Ireland, Canada and the US: Ireland runs employment permits rather than a register, and Tom needs no sponsorship in either North American market.

Salary: if not stated, do NOT penalise on salary; judge comp risk from the seniority and the company.

Working pattern: in Europe and Canada, on-site or hybrid in the resolved market is normal and expected -- the candidate is relocating for the role and needs an employer with an office there. Do NOT treat an on-site or hybrid requirement, a named-office requirement, or the absence of remote flexibility as a risk in those markets, and do not raise a flag about it. The US is the exception and inverts this: a US role only reaches you at all if code read it as remote, so remote is the baseline there rather than a bonus, and a US posting that turns out on reading to require regular office attendance IS worth a flag.

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
                       "below-visa-floor": 60, "below-comp-floor": 60,
                       "us-comp-unstated": 40}
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

def trim_drop_rows(rows):
    """Bound the committed drop log: newest first, capped per stage. Retained per stage
    rather than as one flat list, so thousands of routine title rejects can't push the
    handful of Haiku kills and scoring errors -- the rows actually worth reviewing -- out
    of the file."""
    out, per_group = [], {}
    for d in rows:
        g = str(d.get("stage", "")).split(":")[0]
        if per_group.get(g, 0) >= DROP_KEEP_PER_STAGE.get(g, DROP_KEEP_DEFAULT):
            continue
        per_group[g] = per_group.get(g, 0) + 1
        out.append(d)
    return out[:DROP_MAX_ROWS]

def bump_raw(source, n):
    """Count rows a source returned before filtering, so a regex change that silently
    zeroes a source looks different from a genuinely quiet week."""
    RAW_COUNTS[source] = RAW_COUNTS.get(source, 0) + n

def src_line(source, kept):
    raw = RAW_COUNTS.get(source)
    return f"ok (raw {raw} -> kept {kept})" if raw is not None else f"ok ({kept})"

# Everything in the status footer is written to docs/status.json, which is committed and
# pushed on every run -- so anything a fetcher's exception text drags in is pushed too.
# requests puts the full effective URL, query string included, into the text of every
# exception it raises, which is how an Apify token reached a commit on 6 Sep 2026 and
# GitHub push protection began rejecting the push, silently stopping every scan after it.
# Two guards, because the call site alone did not hold: credentials go in headers, and
# every value on its way into the footer goes through redact().
SECRET_ENV = ("ANTHROPIC_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "REED_API_KEY",
              "APIFY_API_TOKEN", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")

# Credential-bearing query parameters, whatever the host: ?token=, &app_key=, &api_key=.
_SECRET_PARAM = re.compile(
    r"([?&](?:token|api[-_]?key|app[-_]?key|apikey|app[-_]?id|key|secret|password|passwd|"
    r"pwd|access[-_]?token|auth)=)[^&\s\"\'<>]+", re.I)

def redact(text):
    """Scrub credentials out of a status string. Removes the run's own secret values
    verbatim, then any credential-shaped query parameter -- the second catches a token
    this process never held, such as one embedded in a URL a source handed back."""
    out = str(text)
    for name in SECRET_ENV:
        value = os.environ.get(name, "")
        # An 8-char floor: a short or empty secret would otherwise blank out ordinary
        # words, and a blanked-out status line is its own kind of broken.
        if len(value) >= 8:
            out = out.replace(value, f"<{name}>")
    return _SECRET_PARAM.sub(r"\1<redacted>", out)

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
    strong = [j for j in jobs
              if (j.get("score") or 0) >= NTFY_SCORE_THRESHOLD
              and market_tier(j.get("market")) <= NTFY_MAX_TIER]
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
    "us": "us", "usa": "us", "united states": "us", "united states of america": "us",
    "ca": "ca", "canada": "ca",
}

def country_code(v):
    return COUNTRY_CODES.get((v or "").strip().lower(), "")

# ---------------------------------------------------------------- cross-source dedupe
# The same posting reaches this pipeline from up to seven sources, and each one writes the
# employer and the title its own way. An exact (company, title, city) triple therefore
# missed most real duplicates -- "Heidi" vs "Heidi Health", "Rev Ops Manager" vs "Revenue
# Operations Manager", "Semrush" vs "Semrush UK Ltd." -- and every miss is a second full
# Haiku + Opus scoring run on a job that is already on the dashboard, plus a row Tom has to
# read past twice.
#
# So both halves of the identity are normalised hard, and the comparison itself allows one
# name to be a shortening of the other. What stops that from collapsing roles that merely
# look alike lives in same_role(): the seniority band must match, and the words that are
# only in the longer title must not be the words that make two postings different jobs
# (a language requirement, a fixed term, a years-of-experience band).

COMPANY_SUFFIX = re.compile(
    r"\b(inc|incorporated|ltd|limited|llc|plc|corp|corporation|company|co|holdings?|group"
    r"|international|bv|nv|gmbh|ag|sa|sarl|srl|spa|oy|ab|aps|pte|pty)\b\.?", re.I)
# Provenance the feeds tack onto the employer name: "Rubrik Job Board", "SevenRooms, a
# DoorDash company". Stripped before the suffix list, since it carries its own "company".
COMPANY_NOISE = re.compile(r",?\s+(a|an)\s+[\w'&.-]+(\s+[\w'&.-]+)?\s+(company|brand|business)\b"
                           r"|\bjob board\b|\bcareers?\b", re.I)
# A country or region in the employer name is provenance too ("Semrush UK Ltd.",
# "SevenRooms - United Kingdom"), never the thing that distinguishes two employers.
COMPANY_REGION = {"uk", "gb", "gbr", "britain", "british", "england", "ireland", "irish",
                  "netherlands", "nederland", "holland", "dutch", "belgium", "emea",
                  "europe", "european", "eu", "usa", "us", "america", "american",
                  "united", "kingdom", "states", "apac", "dach", "benelux", "global"}

# One bucket per city. These used to share a single bucket per regex alternation group,
# because the code took a slice of the *pattern* string rather than the matched text -- so
# "amster" stood in for every Dutch city and the same role in Amsterdam and in Rotterdam
# collapsed into one dashboard entry.
DEDUPE_CITY = re.compile(r"amsterdam|rotterdam|utrecht|eindhoven|den haag|the hague|hague"
                         r"|dublin|london|brussels|antwerp|ghent", re.I)
CITY_ALIAS = {"the hague": "denhaag", "den haag": "denhaag", "hague": "denhaag"}
# Words in a location string that name no particular place. A location made only of these
# leaves the key incomplete, which means the row is never deduped -- "Netherlands" on its
# own must not become a bucket that two different Dutch cities fall into.
PLACE_NOISE = {"remote", "hybrid", "onsite", "on", "site", "office", "based", "area",
               "region", "greater", "county", "city", "metro", "north", "south", "east",
               "west", "central", "and", "or", "of", "the", "nl", "be", "ie", "gb", "uk",
               "netherlands", "nederland", "holland", "belgium", "belgie", "belgique",
               "ireland", "eire", "irl", "nld", "bel", "united", "kingdom", "great",
               "britain", "england", "scotland", "wales", "europe", "emea", "eu",
               "anywhere", "worldwide", "global", "flanders", "wallonia"}

# Abbreviations the market uses interchangeably. Expanded on both sides before comparison,
# so "Rev Ops Manager" and "Revenue Operations Manager" are one role rather than two.
# Order matters: the compound forms run before the bare "ops" rule.
TITLE_ALIASES = [
    (re.compile(r"\brev(enue)?\s*[-/]?\s*ops\b", re.I), " revenue operations "),
    (re.compile(r"\bsales\s*[-/]?\s*ops\b", re.I), " sales operations "),
    (re.compile(r"\bcs\s*[-/]?\s*ops\b", re.I), " customer success operations "),
    (re.compile(r"\b(biz|business)\s*[-/]?\s*ops\b", re.I), " business operations "),
    (re.compile(r"\b(marketing|mktg)\s*[-/]?\s*ops\b", re.I), " marketing operations "),
    (re.compile(r"\bops\b", re.I), " operations "),
    (re.compile(r"\bgtm\b|\bg2m\b|\bgo[- ]to[- ]market\b", re.I), " go to market "),
    (re.compile(r"\bcsm\b", re.I), " customer success manager "),
    (re.compile(r"\bsr\.?\b|\bsnr\.?\b", re.I), " senior "),
    (re.compile(r"\bjr\.?\b", re.I), " junior "),
    (re.compile(r"\bmgr\.?\b", re.I), " manager "),
    (re.compile(r"\bmgmt\b", re.I), " management "),
    (re.compile(r"\bvp\b|\bv\.p\.", re.I), " vice president "),
    (re.compile(r"\bexec\.?\b", re.I), " executive "),
    (re.compile(r"\bintl\b", re.I), " international "),
    (re.compile(r"\bacct\b", re.I), " account "),
]
TITLE_STOPWORDS = {"of", "the", "and", "for", "a", "an", "to", "in", "at", "on", "with",
                   "or", "de", "het", "een", "van"}
# Words that, when they appear in one title and not the other, mean the two postings are
# different jobs rather than the same job written twice. Without this list "Renewals
# Manager" would swallow "Renewals Manager - French Speaker" and "Sales Operations
# Associate" would swallow the fixed-term version of itself.
DISTINGUISHING = {
    "fixed", "term", "temporary", "temp", "contract", "contractor", "interim", "intern",
    "internship", "graduate", "placement", "apprentice", "maternity", "paternity",
    "parental", "cover", "secondment", "part", "time", "yoe", "year",
    "french", "german", "dutch", "flemish", "spanish", "italian", "portuguese", "polish",
    "swedish", "danish", "norwegian", "finnish", "turkish", "arabic", "hebrew", "japanese",
    "korean", "mandarin", "chinese", "russian", "czech", "hungarian", "romanian", "greek",
    "nordic", "speaking", "speaker", "native", "bilingual", "fluent", "language",
}
# Beyond this many extra words, the longer title is describing a different job, not the
# same one with a product or region suffix.
DEDUPE_MAX_EXTRA_TITLE_WORDS = 4

def company_tokens(s):
    """The words that actually identify an employer, as a set. Legal form, provenance and
    region are all stripped, so "Heidi", "Heidi Health" and "Semrush UK Ltd." reduce to
    something a subset test can compare."""
    t = COMPANY_NOISE.sub(" ", (s or "").lower())
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = COMPANY_SUFFIX.sub(" ", t)
    toks = [w for w in t.split() if w not in COMPANY_REGION]
    # Single letters are the debris of a punctuated legal form ("S.a.r.l." -> s a r l), not
    # part of the name -- unless they are all that is left, since X is a real employer.
    return frozenset([w for w in toks if len(w) > 1] or toks)

def norm_company(s):
    """Order-insensitive string form of company_tokens(), kept because dkey() is a hashable
    key and a frozenset doesn't read well in a drop reason."""
    return "".join(sorted(company_tokens(s)))

def company_initials(s):
    """First letter of each word in a company name, as a lowercase string -- so "London
    Stock Exchange Group" reduces to "lseg". Unlike company_tokens() this keeps a word like
    "Group" that COMPANY_SUFFIX treats as noise for the subset check, because it's exactly
    the letter a real acronym is built from; stripping it there would turn "lseg" into
    "lse" and miss the match it exists to catch."""
    words = [w for w in re.findall(r"[a-z]+", (s or "").lower()) if w not in {"and", "of", "the"}]
    return "".join(w[0] for w in words)

def title_tokens(s):
    """The words that identify a role, as a set: abbreviations expanded, punctuation and
    filler dropped, plurals folded. A set rather than a sequence, so "Manager, Sales
    Operations" and "Sales Operations Manager" are the same role."""
    t = " " + (s or "").lower() + " "
    for rx, rep in TITLE_ALIASES:
        t = rx.sub(rep, t)
    out = set()
    for w in re.sub(r"[^a-z0-9]+", " ", t).split():
        if w in TITLE_STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            w = w[:-1]          # renewals/renewal, operations/operation
        out.add(w)
    return frozenset(out)

# Seniority is the one thing a shorter title is never allowed to be shortened out of:
# "Renewals Manager" and "Senior Renewals Manager" are two postings, not one.
SENIORITY_BANDS = [({"chief", "ceo", "cro", "coo", "cfo", "cto", "cmo"}, 6),
                   ({"vice", "president", "svp", "evp"}, 5),
                   ({"director", "head"}, 4),
                   ({"principal", "staff"}, 3),
                   ({"senior", "lead"}, 2),
                   ({"junior", "graduate", "intern", "internship", "entry", "trainee"}, 0)]

def title_seniority(tokens):
    """Rank of the most senior band named in a title; 1 when none is."""
    return max((rank for words, rank in SENIORITY_BANDS if tokens & words), default=1)

def dedupe_city(location):
    """The city bucket two rows must share before they can be compared at all. A named
    target city wins; failing that, the first word of the location that names a place, so
    Staines rows can dedupe against each other without Staines and Slough merging. A
    location that names only a country or a region yields "", which leaves the key
    incomplete and the row un-dedupable."""
    loc = location or ""
    m = DEDUPE_CITY.search(loc)
    if m:
        return CITY_ALIAS.get(m.group(0).lower(), m.group(0).lower())
    for w in re.split(r"[^a-z]+", loc.lower()):
        if len(w) > 2 and w not in PLACE_NOISE:
            return w
    return ""

def role_key(j):
    """(company words, title words, city, seniority, company initials) identity of a
    posting. Not hashable as an equality key -- same_role() compares two of these --
    because the whole point is that two rows can name the same job with different numbers
    of words."""
    tt = title_tokens(j.get("title"))
    return (company_tokens(j.get("company")), tt, dedupe_city(j.get("location")),
            title_seniority(tt), company_initials(j.get("company")))

def role_key_complete(k):
    """Company, title and city all present. A row missing any of them is never deduped, so
    a blank company can't swallow unrelated rows."""
    return bool(k[0] and k[1] and k[2])

def _same_company(ka, kb):
    """One name's words contain the other's ("Heidi" inside "Heidi Health"), or one side is
    a bare acronym matching the other's initials ("LSEG" / "London Stock Exchange Group",
    "AWS" / "Amazon Web Services (AWS)"). The acronym check only fires when the shorter
    side is a single token -- "Amazon" alone is not an acronym of anything -- and it's safe
    on its own only because the title and city have to agree too; a subset match still
    covers most of the real world so it's tried first."""
    ca, cb = ka[0], kb[0]
    if ca <= cb or cb <= ca:
        return True
    if len(ca) == 1 and len(next(iter(ca))) >= 2 and next(iter(ca)) == kb[4]:
        return True
    if len(cb) == 1 and len(next(iter(cb))) >= 2 and next(iter(cb)) == ka[4]:
        return True
    return False

def same_role(ka, kb):
    """True when two role keys are the same posting seen twice.

    Company: see _same_company().
    Title: identical word sets, or the shorter contained in the longer with the guards
    above -- same seniority band, at most a few extra words, and none of those words a
    DISTINGUISHING one."""
    if not (role_key_complete(ka) and role_key_complete(kb)):
        return False
    if ka[2] != kb[2] or ka[3] != kb[3]:
        return False
    if not _same_company(ka, kb):
        return False
    ta, tb = ka[1], kb[1]
    if ta == tb:
        return True
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) < 2 or not short <= long_:
        return False
    extra = long_ - short
    if len(extra) > DEDUPE_MAX_EXTRA_TITLE_WORDS:
        return False
    return not any(w in DISTINGUISHING or w.isdigit() for w in extra)

# Which copy of a duplicate to keep. A scored row always beats an unscored one, then the
# source: an employer's own ATS feed carries the clean company name, the full description
# and the real location, while the big aggregators are the ones that shorten a title to
# "Rev Ops Manager" and file a London role under "Somers Town".
DEDUPE_SOURCE_RANK = {"greenhouse": 0, "lever": 0, "ashby": 0, "workday": 1,
                      "revopsroles": 2, "hiring.cafe": 3, "linkedin": 4, "reed": 5,
                      "adzuna": 6, "indeed": 7}

def _row_rank(j):
    return (-(j.get("score") or 0), DEDUPE_SOURCE_RANK.get(j.get("source"), 9),
            -len(j.get("description") or ""), -len(j.get("title") or ""))

def prefer_row(a, b):
    """The better of two rows for the same posting."""
    return a if _row_rank(a) <= _row_rank(b) else b

DEDUPE_ALSO_CAP = 4

def absorb_duplicate(winner, loser):
    """Fold a duplicate into the row that survives it. The loser's id is carried on the
    winner so the dashboard can treat a Hide or Mark-applied recorded against either id as
    applying to the surviving row -- otherwise collapsing a duplicate would silently
    un-apply a job Tom had already applied to."""
    ids = set(winner.get("dupe_ids") or []) | set(loser.get("dupe_ids") or [])
    ids.add(str(loser.get("id", "")))
    ids.discard(str(winner.get("id", "")))
    winner["dupe_ids"] = sorted(i for i in ids if i)
    also = {(a.get("source"), a.get("url")) for a in (winner.get("also_seen") or [])}
    also |= {(a.get("source"), a.get("url")) for a in (loser.get("also_seen") or [])}
    also.add((loser.get("source", ""), loser.get("url", "")))
    also.discard((winner.get("source"), winner.get("url")))
    winner["also_seen"] = [{"source": s, "url": u} for s, u in sorted(also) if s][:DEDUPE_ALSO_CAP]
    return winner

def group_duplicates(rows):
    """Partition row indices into duplicate groups (singletons included), using the full
    transitive closure of same_role() rather than a single first-match pass.

    same_role() is not transitive: Adzuna's "AWS" and hiring.cafe's "Amazon" don't match
    each other, but both match LinkedIn's "Amazon Web Services (AWS)". A pass that stops
    at the first match a row finds pairs the LinkedIn row with whichever partial name it
    meets first and never revisits the other one, so "Amazon" stayed on the dashboard next
    to the row it was really a duplicate of. Union-find closes that: as long as AWS~full
    and Amazon~full are each discovered somewhere in the bucket, both unions land on the
    same root regardless of which row triggered them or in what order, so the group ends
    up {AWS, Amazon, full} even though AWS and Amazon were never compared directly.

    Bucketed by city, same as the callers used to do their own bucketing, so this stays
    linear in practice rather than comparing every row in the run to every other."""
    parent = list(range(len(rows)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    keys = [role_key(r) for r in rows]
    buckets = {}
    for i, k in enumerate(keys):
        if role_key_complete(k):
            for j in buckets.get(k[2], ()):
                if same_role(k, keys[j]):
                    union(i, j)
            buckets.setdefault(k[2], []).append(i)

    groups = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())

def fold_group(rows, idxs):
    """Reduce one duplicate group to its best row, folding every other member's ids and
    sources into it. `idxs` order controls nothing about correctness -- prefer_row is
    applied pairwise as a running tournament, so the final winner is the group's overall
    best regardless of how many members there are or what order they arrive in."""
    winner = rows[idxs[0]]
    for i in idxs[1:]:
        other = rows[i]
        new_winner = prefer_row(winner, other)
        loser = other if new_winner is winner else winner
        winner = new_winner
        absorb_duplicate(winner, loser)
    return winner

def collapse_duplicates(rows):
    """Reduce a list of rows to one per posting, keeping the best copy of each. Returns
    (kept, dropped_with_their_winner). Used on the dashboard itself as well as on a run's
    fetches, so duplicates that landed before this logic existed heal on the next scan
    instead of sitting there forever."""
    kept, dropped = [], []
    for idxs in group_duplicates(rows):
        if len(idxs) == 1:
            kept.append(rows[idxs[0]])
            continue
        winner = fold_group(rows, idxs)
        kept.append(winner)
        dropped.extend((rows[i], winner) for i in idxs if rows[i] is not winner)
    return kept, dropped

def dupe_reason(winner):
    """The drop reason written into the excluded log, naming what it collapsed into."""
    where = f"{winner.get('company') or '?'} - {winner.get('title') or '?'}"
    return f"same posting as {where} ({winner.get('source') or 'dashboard'}), already kept"

def merge_found_into_dashboard(existing, found):
    """Fold one run's fetches into the dashboard, one row per posting -- covers both this
    run's cross-source duplicates and any still sitting on the dashboard from before this
    matching existed, in a single pass.

    Grouped with the rest of `existing`, so a group made only of existing rows heals the
    dashboard exactly as a standalone self-heal pass would. But a group that mixes an
    existing row with a fresh fetch can never let the fetch become the winner, whatever
    prefer_row's score/source ranking would otherwise say: an already-scored dashboard row
    always keeps its identity, since re-scoring a job Tom has already been shown is the one
    cost this whole stage exists to avoid.

    Returns (existing, found, drops): existing and found have one row per posting apiece,
    every winner mutated in place with the ids and sources it absorbed; drops pairs every
    folded-away row with what it folded into, for record_drop()."""
    combined = existing + found
    n_existing = len(existing)
    kept_existing, kept_found, drops = [], [], []
    for idxs in group_duplicates(combined):
        existing_idxs = [i for i in idxs if i < n_existing]
        found_idxs = [i for i in idxs if i >= n_existing]
        if existing_idxs:
            winner = fold_group(combined, existing_idxs)
            kept_existing.append(winner)
            drops.extend((combined[i], winner) for i in existing_idxs if combined[i] is not winner)
            for i in found_idxs:
                loser = combined[i]
                absorb_duplicate(winner, loser)
                drops.append((loser, winner))
            continue
        winner = fold_group(combined, found_idxs)
        kept_found.append(winner)
        drops.extend((combined[i], winner) for i in found_idxs if combined[i] is not winner)
    return kept_existing, kept_found, drops

def market_of(country, location):
    """Which target market a row belongs to ('NL' / 'BE' / 'UK-London' / 'IE' / 'CA' /
    'US-Remote'), or None if it's outside all of them. This is the single source of truth
    for location: location_ok() and the deep score's location cap both read it, so the code
    and the rubric can no longer disagree (a Staines role used to pass the gate here and
    then get capped at 2 by the model for being 'outside the London commuter belt').

    North America is resolved FIRST, before the European city anchors, because the two
    halves disagree about remote and because the place names collide. Europe rejects an
    unanchored remote posting; the US requires one. And "London, ON" is Canadian while
    UK_LONDON would happily claim it. See north_america_of()."""
    loc = location or ""
    cc = (country or "").lower()

    # North America first. A row with a Canadian or US signal never falls through to the
    # European branches, so a US onsite role is out here rather than being misread as a
    # European city match ("Amsterdam, NY" is not the Netherlands).
    if (CA_COUNTRY.search(loc) or CA_PROVINCE.search(loc) or CA_CITY.search(loc)
            or US_COUNTRY.search(loc) or US_COUNTRY_ABBR.search(loc) or US_STATE.search(loc)
            or cc in ("ca", "us")):
        return north_america_of(loc, cc)

    # An explicitly named city we don't want, with no target city alongside it, is out --
    # checked first so "Cambridge, UK" can't slip through on the strength of the country
    # half of the string. Ireland has no such list any more: the whole country is in scope.
    if UK_OTHER_CITY.search(loc) and not UK_LONDON.search(loc):
        return None

    # A named city or region anchors the posting, remote wording notwithstanding.
    if NL_CITY.search(loc):
        return "NL"
    if BE_CITY.search(loc):
        return "BE"
    if IE_CITY.search(loc) or IE_COUNTY.search(loc):
        return "IE"
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
        return "IE"

    # Fall back to the source's own country field. The NL/BE/IE feeds are country-scoped so
    # the code alone is enough. A GB feed spans the whole UK, so an unrecognised UK location
    # is not assumed to be London -- only a bare/empty one is.
    if cc in ("nl", "be", "ie"):
        return {"nl": "NL", "be": "BE", "ie": "IE"}[cc]
    if cc == "gb" and not loc:
        return "UK-London"
    return None


# Where each market sits in Tom's own ordering, which is not the same thing as how feasible
# it is. The Netherlands is the goal and gets a tier to itself. Ireland and London are the
# realistic strong options. Belgium and Canada are the two backups that keep a door open --
# Belgium into the EU, Canada via a citizenship certificate that is still unproven. The US
# is a financial-runway backstop Tom does not want to move to, so it sits alone at the
# bottom. Tiers decide dashboard order and who gets first claim on the deep-scoring budget;
# the fit score itself is untouched by them.
MARKET_TIER = {"NL": 1, "IE": 2, "UK-London": 2, "BE": 3, "CA": 3, "US-Remote": 4}
TIER_UNKNOWN = 9        # a market the table forgot sorts last rather than first


def market_tier(market):
    return MARKET_TIER.get(market or "", TIER_UNKNOWN)


# ---------------------------------------------------------------- the scoring queue
#
# MAX_SCORED_PER_RUN caps what the deep scorer costs. What it did NOT do, until this
# existed, is decide WHICH roles get those slots: the loop walked new_jobs in fetcher order
# (Adzuna first, LinkedIn and hiring.cafe last) and broke at the cap. With only the four
# European markets the cap never bound -- 478 scored rows over 45 days is about 2.7 a run
# against a cap of 30 -- so the arbitrary order never showed. Adding the US, which is
# bigger than all four European markets combined, is exactly the change that makes it bind,
# and then Adzuna would win every run and hiring.cafe would lose every run. hiring.cafe is
# the source that found the two highest-scoring roles the radar has ever seen.
#
# So the survivors of stage one are sorted before the budget is spent. Every term here is
# free to compute: no model call, no network.
#
# Nothing is discarded by losing. A job past the cap is deliberately not added to seen.json
# (that predates this change and is the reason it works), so it comes back next run -- at
# four runs a day inside a 7-day age window, up to 28 more chances. The queue decides
# latency, not inclusion.
#
# The one way latency could turn into loss is a job waiting until MAX_POST_AGE_DAYS expires
# it unscored. That is what DEFER_FILE and the first term of the sort key are for: a job
# that has been passed over climbs every run, and after DEFER_ESCALATES_AFTER runs it
# outranks fresh arrivals regardless of market. "Maybe never" becomes "within a few runs".
DEFER_FILE = "deferred.json"
# Deferrals above this count as equal, so a job that has waited a long time cannot keep
# climbing past one that has waited slightly less and starve IT instead. Past this point
# they tie and the rest of the key decides.
DEFER_CAP = 6
# After this many deferrals a job is promoted ahead of every never-deferred row, whatever
# its market. Three runs is under a day at the current schedule, well inside the 7-day age
# window, so a US role cannot sit behind European arrivals until it expires.
DEFER_ESCALATES_AFTER = 3


def load_deferrals(path=DEFER_FILE):
    """{job id: runs it has been passed over}. Missing or corrupt reads as empty -- a lost
    counter costs some ordering fairness for a run, and is not worth failing a scan over."""
    raw = load_json(path, {})
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def save_deferrals(deferrals, ids_still_pending, path=DEFER_FILE):
    """Write back only the counters for jobs still waiting.

    Pruning against the live pending set is what stops this growing without bound: a job
    that got scored, got dropped, or aged out has no counter worth keeping, and seen.json
    is already the record that it is done with."""
    keep = {k: min(v, DEFER_CAP) for k, v in deferrals.items()
            if k in ids_still_pending and v > 0}
    json.dump(dict(sorted(keep.items())), open(path, "w"), indent=0)
    return keep


def revops_core_title(title):
    """True for the RevOps-proper end of the funnel, as opposed to the adjacent-and-arguable
    end (CSM, renewals, enablement, generic business operations). Same regex the US gate
    uses, reused here as a quality signal for every market rather than a filter."""
    return bool(REVOPS_CORE.search(title or ""))


def priority(job, deferrals=None):
    """Sort key for the deep-scoring budget, highest first. Free to compute.

    Returned as a tuple of descending-sorted numbers, so `sorted(jobs, key=priority,
    reverse=True)` reads in the same order as the terms are described here."""
    deferrals = deferrals or {}
    waited = min(int(deferrals.get(job.get("id"), 0)), DEFER_CAP)
    market = job.get("market") or ""
    title = job.get("title") or ""

    # 1. Starvation guard, and deliberately the first term. A job that has been passed over
    #    DEFER_ESCALATES_AFTER times jumps every fresh row; below that it is a tiebreak.
    escalated = 1 if waited >= DEFER_ESCALATES_AFTER else 0

    # 2. Tom's market ordering. Negated because lower tier means more wanted.
    tier = -market_tier(market)

    # 3. Is this the pivot proper, or the adjacent end of the funnel?
    core = 1 if revops_core_title(title) else 0

    # 4. Title band. The bands that used to be auto-buried are not penalised here either,
    #    but a plainly off-function title sorts below everything else.
    band = {"wrong_function": -2, "director_plus": -1}.get(title_band(title), 0)

    # 5. A company we can already see can sponsor is materially more actionable than one we
    #    cannot. Only meaningful for NL and UK; the others have no register and score 0.
    sponsor = {"sponsor": 2, "sponsor (likely)": 1}.get(job.get("sponsor") or "", 0)

    # 6. A US employer with a route to a market Tom actually wants.
    transfer = 1 if job.get("transfer_markets") else 0

    # 7. A stated salary is a real signal in a feed where most rows have none. Adzuna's
    #    estimates never reach here -- adzuna_salary() discards predicted figures -- so a
    #    salary string on a row means someone published a number.
    stated_salary = 1 if job.get("salary") else 0

    # 8. A real posting rather than a page of marketing furniture. A stub scores badly for
    #    reasons that are not the role's fault, so it should not consume a slot ahead of a
    #    row the scorer can actually read.
    real_desc = 1 if len(job.get("description") or "") >= MIN_DESC_CHARS else 0

    # 9. On the watchlist Tom curated by hand.
    watched = 1 if job.get("_watched") else 0

    # 10. Freshness, as the tiebreak.
    posted = parse_date_loose(job.get("posted_at"))
    fresh = posted.timestamp() if posted else 0.0

    return (escalated, tier, core, band, sponsor, transfer, stated_salary, real_desc,
            watched, waited, fresh)


def location_ok(country, location):
    return market_of(country, location) is not None

def prefilter(title, location, country=""):
    """None if the row passes the free filters; otherwise a short reason for the drop log.

    The title half of this gate is market-aware, not purely textual, in both directions: a
    plain "Customer Success Manager" is admitted in the Netherlands and nowhere else (see
    CSM_ANY), and a US role has to clear the much narrower REVOPS_CORE instead of
    INCLUDE_TITLE. The market is resolved once here and reused for the location check
    below."""
    t = title or ""
    market = market_of(country, location)
    if market == "US-Remote":
        # The US gets the two narrow gates, not the broad one: core RevOps, or customer
        # success with a seniority qualifier. Everything else INCLUDE_TITLE would admit --
        # renewals, generic business operations, plain CSM -- stays out.
        if not (REVOPS_CORE.search(t) or SENIOR_CS.search(t)):
            return "title: not core RevOps or senior CS (US uses the narrow gates)"
    elif not (INCLUDE_TITLE.search(t) or (market == "NL" and CSM_ANY.search(t))):
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
        phrases = (ADZUNA_CORE_PHRASES if cc in ADZUNA_CORE_COUNTRIES
                   else ADZUNA_PHRASES)
        for phrase in phrases:
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

# JobSpy targets, one per market it covers. Ireland is Adzuna's gap (that API has no
# Ireland endpoint), so Indeed is the only broad feed there. The North American entries add
# Google Jobs, which is the closest free thing to hiring.cafe's long-tail discovery: Google
# indexes Greenhouse, Lever and Ashby posting pages directly, so it surfaces the
# small-company ATS rows the big aggregators miss. Indeed rides along for the US and Canada
# because the call is already being made.
JOBSPY_TARGETS = [
    {"cc": "ie", "location": "Ireland", "country_indeed": "Ireland",
     "sites": ["indeed"], "currency": "EUR", "terms": None},
    {"cc": "ca", "location": "Canada", "country_indeed": "Canada",
     "sites": ["indeed", "google"], "currency": "CAD", "terms": None},
    # "remote" in the search string does the same job at the source that market_of() does
    # downstream: a US row that is not remote is dropped, so asking for onsite rows is
    # paid-for volume thrown away.
    {"cc": "us", "location": "United States", "country_indeed": "USA",
     "sites": ["indeed", "google"], "currency": "USD",
     "terms": [f"{t} remote" for t in JOBSPY_CORE_TERMS]},
]


def fetch_jobspy(diag=None):
    """Indeed (plus Google Jobs in North America) via JobSpy. Best-effort: import and
    scrape may both fail on CI IPs, and neither is allowed to break the run.

    Searches whole countries, never a single city. Scoping Ireland to "Dublin, Ireland"
    made Indeed's own location filter do the Dublin-only narrowing the location gate used
    to do, and hid Cork and Galway roles before anything could score them."""
    from jobspy import scrape_jobs   # imported lazily so a missing dep can't break the run
    out, seen = [], set()
    for target in JOBSPY_TARGETS:
        cc = target["cc"]
        raw = kept = 0
        err = None
        for term in (target["terms"] or JOBSPY_TERMS):
            try:
                df = scrape_jobs(site_name=target["sites"], search_term=term,
                                 location=target["location"], results_wanted=20,
                                 country_indeed=target["country_indeed"],
                                 hours_old=MAX_POST_AGE_DAYS * 24)
            except Exception as e:
                err = f"error: {e}"
                continue
            if df is None or len(df) == 0:
                continue
            raw += len(df)
            bump_raw("indeed", len(df))
            for _, row in df.iterrows():
                title = str(row.get("title") or "")
                loc = str(row.get("location") or target["location"])
                jid = "js-" + re.sub(r"\W+", "-", str(row.get("job_url") or title))[-70:]
                reason = prefilter(title, loc, cc)
                if reason:
                    record_drop({"id": jid, "title": title, "location": loc,
                                 "company": str(row.get("company") or ""),
                                 "source": "indeed"}, "prefilter", reason)
                    continue
                if jid in seen:
                    continue
                seen.add(jid)
                sal = ""
                if row.get("min_amount"):
                    sal = (f"{int(row['min_amount'])}-"
                           f"{int(row.get('max_amount') or row['min_amount'])} "
                           f"{row.get('currency') or target['currency']}")
                out.append({
                    "id": jid, "company": str(row.get("company") or ""),
                    "title": title, "location": loc, "country": cc,
                    "market": market_of(cc, loc),
                    "url": str(row.get("job_url") or ""), "source": "indeed",
                    "description": strip_html(str(row.get("description") or "")),
                    "salary": sal,
                    "posted_at": str(row.get("date_posted") or ""),
                })
                kept += 1
        if diag is not None:
            diag[f"jobspy:{cc}"] = err or f"raw {raw}, kept {kept}"
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

SMARTRECRUITERS_URL = re.compile(
    r"^https?://jobs\.smartrecruiters\.com/([^/]+)/(\d+)", re.I)


def smartrecruiters_desc(url):
    """The JD off a jobs.smartrecruiters.com posting URL, via their posting API.

    Added because the board lookup was resolving SmartRecruiters URLs and then recovering
    nothing from them: the visible page renders client-side, so the generic JSON-LD
    extractor finds no JobPosting. The API has the whole thing in jobAd.sections -- a QIMA
    RevOps posting came back as 6,736 characters across four sections where the page gave
    zero.

    Sections are concatenated in reading order rather than cherry-picked, because
    qualifications is where the language requirements and the years-of-experience ceiling
    live, and those are exactly what the disqualifier checks and the seniority dimension
    need to see."""
    m = SMARTRECRUITERS_URL.match((url or "").split("?")[0])
    if not m:
        return ""
    try:
        r = get(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}"
                f"/postings/{m.group(2)}", headers={"Accept": "application/json"})
        if r.status_code != 200:
            return ""
        secs = ((r.json().get("jobAd") or {}).get("sections") or {})
    except Exception:
        return ""
    parts = []
    for key in ("jobDescription", "qualifications", "companyDescription",
                "additionalInformation"):
        text = strip_html(((secs.get(key) or {}).get("text")) or "")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


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

def jsonld_job_posting(url):
    """The schema.org JobPosting dict off a page, or {}.

    Split out from jsonld_job_description() because the JD text is not the only useful
    thing in there: `title` and `hiringOrganization` are what let a web-searched URL be
    VERIFIED as the right role in code, instead of taking the model's word for it."""
    try:
        r = get(url); r.raise_for_status()
        for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
                             r.text, re.S):
            try:
                data = json.loads(m.group(1))
            except Exception:
                continue
            for d in (data if isinstance(data, list) else [data]):
                if isinstance(d, dict) and d.get("@type") == "JobPosting":
                    return d
    except Exception:
        pass
    return {}


def jsonld_job_description(url):
    """Generic schema.org JobPosting extractor. Widely used for SEO across ATS/career
    platforms (Workday, iCIMS, SmartRecruiters, custom sites) regardless of how the
    visible page itself renders, so this works across revopsroles.com's varied
    source_url domains without needing per-ATS parsing. Returns "" if absent/unparseable."""
    return strip_html((jsonld_job_posting(url) or {}).get("description") or "")

# Source-specific description fetchers, tried before the generic ones below.
DETAIL_FETCHERS = {"greenhouse": greenhouse_desc, "linkedin": linkedin_desc,
                   "revopsroles": jsonld_job_description}
# Tried for any source once the specific one has been exhausted. The two URL-rewriting
# fetchers cost nothing when the URL isn't theirs -- they return "" without a request.
GENERIC_FETCHERS = (workday_desc, greenhouse_board_desc, smartrecruiters_desc,
                    jsonld_job_description)
# Fetchers that make no request unless the URL matches their host, so trying them is free.
_FREE_IF_NO_MATCH = {workday_desc: workday_cxs_url,
                     greenhouse_board_desc: lambda u: GREENHOUSE_BOARD_URL.match(
                         (u or "").split("?")[0]),
                     smartrecruiters_desc: lambda u: SMARTRECRUITERS_URL.match(
                         (u or "").split("?")[0])}
MAX_DESC_FETCHES = 3     # network calls per job, so a board of thin ads can't stall a run


# How many stub rows may go looking for their real posting in one run. Bounded because
# each one can cost a slug probe across eight board APIs; the per-company cache in
# board-cache.json means a given employer is only ever paid for once, so this is about
# keeping a single scan short rather than about a recurring cost.
MAX_JD_RESCUES_PER_RUN = 12


def rescue_description(job, companies, cache, fetch=None):
    """Go to the company's own board for a row whose posting text never arrived.

    Returns (description, apply_url) and ("", "") when nothing trustworthy was found.

    This is Tom's idea done the cheap way: use the source as a pointer, then get the real
    JD from the employer. findform already had every piece -- find_board() probes the eight
    public board APIs by company slug, rank() matches the posting's title -- it was just
    running AFTER scoring, on rows gated at FLOOR, so the description it could have
    recovered arrived too late to be scored on.

    The title gate is rank()'s, unchanged, and that matters more here than it does for
    applications. TITLE_MATCH_MIN plus a market match is what stops a same-titled role in
    another city being pulled in; a wrong JD does not just mis-link a row, it produces a
    confidently wrong SCORE, which is harder to notice than a wrong link."""
    import findform                   # deferred: findform imports this module
    company = (job.get("company") or "").strip()
    if not company:
        return "", ""
    try:
        ats, _slug, jobs = findform.find_board(company, companies, fetch, cache)
    except Exception:
        return "", ""
    if not jobs:
        return "", ""
    _matches, best = findform.rank(jobs, job.get("title") or "", job.get("market") or "")
    if not best or not best.get("url"):
        return "", ""
    # Stricter than rank() on purpose, and only here.
    #
    # rank() accepts a single close title match even when no candidate sits in the
    # posting's market, so that a board which omits locations does not rule out the whole
    # company. That is a considered trade-off for finding an application form -- Tom sees
    # the link and confirms it himself.
    #
    # It is the wrong trade-off for a DESCRIPTION. A board that says "Austin, TX" against
    # an Amsterdam row is not missing a location, it is stating a different one, and
    # attaching that JD would hand the scorer the wrong comp, the wrong requirements and
    # the wrong market -- a confidently wrong score, which is exactly what the
    # thin-evidence work exists to prevent. A thin row is recoverable; a plausible score
    # built on another city's posting is not.
    #
    # So: accept a stated location only when it is in this row's market. A blank location
    # still falls through, which is the case rank()'s rule was written for.
    board_loc = (best.get("location") or "").strip()
    if board_loc and not findform.same_market(job.get("market") or "", board_loc):
        return "", ""
    desc = fill_description({"url": best["url"], "source": ats})
    return (desc, best["url"]) if not is_thin(desc) else ("", best["url"])


# Web-searching for a posting the other routes could not find. Last resort, and priced
# like one: this is the only part of the pipeline that spends tokens to get EVIDENCE rather
# than to form a judgement.
#
# Sonnet 5 rather than Opus, because the task is a lookup: find the canonical posting URL.
# It is not asked to read, summarise or judge the role -- the free JSON-LD extractor pulls
# the text and code checks the identity, so a wrong answer here is caught rather than
# believed.
JD_SEARCH_MODEL = "claude-sonnet-5"
JD_SEARCH_MAX_TOKENS = 1500
JD_SEARCH_MAX_USES = 3        # web_search calls inside one request
MAX_JD_SEARCHES_PER_RUN = 3   # requests per scan; ~$0.01-0.02 each

JD_SEARCH_SYSTEM = """You find the canonical URL of one specific job posting. You do not evaluate it, summarise it, or comment on it.

Given an employer, a job title and a location, search for that exact posting and return the URL of the employer's own application page for it -- their careers site or their applicant tracking system (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Personio, Teamtailor and the like).

Rules:
- Prefer the employer's own posting over any job board. LinkedIn, Indeed, Glassdoor, ZipRecruiter and aggregator reposts are a last resort and usually not worth returning at all.
- It must be THE SAME role: same employer, same title, same location. A similar title at the same company in a different city is the wrong answer, and so is the same title at a different company.
- If you cannot find it, or you are not confident it is the same role, say so. A wrong URL is far worse than none: it would attach another role's requirements to this one.

Reply with ONLY one line, and nothing else:
URL: <the url>
or
URL: NONE"""


def search_jd_url(api_key, job):
    """A candidate URL for this posting from a web search, or "".

    Deliberately narrow: the model is asked for a URL and nothing else. It does not read
    the posting and its answer is not trusted -- verify_jd() checks the identity in code
    off the page's own schema.org metadata. That split is what makes spending tokens here
    safe: the failure mode of a bad search is a refused match, not a plausible wrong score.
    """
    user = (f"Employer: {job.get('company') or '?'}\n"
            f"Job title: {job.get('title') or '?'}\n"
            f"Location: {job.get('location') or '?'}")
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "max_uses": JD_SEARCH_MAX_USES}]
    try:
        text = _claude_call(api_key, JD_SEARCH_MODEL, JD_SEARCH_SYSTEM, user,
                            JD_SEARCH_MAX_TOKENS, tools=tools)
    except Exception:
        return ""
    m = re.search(r"URL:\s*(\S+)", text or "")
    if not m:
        return ""
    url = m.group(1).strip().rstrip(".,)")
    if url.upper() == "NONE" or not url.lower().startswith("http"):
        return ""
    return url


def verify_jd(url, job):
    """(description, url) when the page at `url` really is this job, else ("", "").

    The verification is the point, and it is done here rather than by the model. A page
    that advertises itself as a JobPosting states its own title and hiring organisation in
    schema.org metadata, so those can be checked against the row: the title with the same
    findform.title_score gate an application uses, and the employer by name.

    Nothing is accepted without both. A search that finds the wrong posting therefore costs
    a refused match and a thin row -- which is the outcome the row already had -- rather
    than a confidently wrong score built on another role's requirements."""
    import findform                   # deferred: findform imports this module
    posting = jsonld_job_posting(url)
    if not posting:
        return "", ""
    desc = strip_html(posting.get("description") or "")
    if is_thin(desc):
        return "", ""

    found_title = (posting.get("title") or "").strip()
    if not found_title:
        return "", ""
    if findform.title_score(job.get("title") or "", found_title) < findform.TITLE_MATCH_MIN:
        return "", ""

    org = posting.get("hiringOrganization")
    found_org = (org.get("name") if isinstance(org, dict) else org) or ""
    want_org = job.get("company") or ""
    if want_org and found_org:
        # Same normalisation the board cache keys on, so "Acme", "Acme Ltd" and "Acme,
        # Inc." are one employer here too.
        a, b = findform.cache_key(found_org), findform.cache_key(want_org)
        if a != b and a not in b and b not in a:
            return "", ""
    return desc, url


def is_thin(desc):
    """True when what we have is not really a posting.

    MIN_DESC_CHARS is already the threshold fill_description() uses to decide a description
    is missing; this is the same line, named, so the scorer's warning and the fetcher's
    retry can never disagree about what counts as thin."""
    return len((desc or "").strip()) < MIN_DESC_CHARS

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
    if len(best) < MIN_DESC_CHARS:
        best = fill_from_duplicates(job, best)
    return best or job.get("_fallback_desc") or ""


# How many of a row's absorbed duplicates may be asked for the posting text. Dedupe keeps
# up to DEDUPE_ALSO_CAP of them and orders them as it absorbed them rather than by how
# likely each is to answer, so this is a budget rather than a filter.
MAX_ALSO_SEEN_FETCHES = 3

# Rows this run that got their posting out of a folded-away duplicate. On the status line
# because this source has already failed silently once: if dedupe ever stops keeping
# `also_seen`, or the winners stop being stubs, this number goes to zero and nothing else
# changes -- the rows just quietly go back to being scored off fifty characters.
DUPE_RECOVERIES = [0, 0]      # [asked, recovered]


def fill_from_duplicates(job, best=""):
    """Ask the copies of this posting that dedupe folded away for the text the winner lacks.

    Duplicates are collapsed BEFORE any description is fetched, and the copy that survives
    is picked on source rank, not on what it can tell us about the job. revopsroles
    outranks hiring.cafe and LinkedIn, so the row that won was routinely the one carrying a
    50-character synthesized summary while the row it folded away carried the employer's
    own Ashby/Greenhouse/SmartRecruiters/Workable link. The JD was not missing from the
    internet, it was discarded at the moment of dedupe -- and every rescue downstream then
    went looking for it on the open web, paying board probes and web searches for a link
    the row had already been handed.

    absorb_duplicate() keeps those links on the winner as `also_seen`, so they cost nothing
    to find. Of the 34 revopsroles rows this was written for, 19 carried one and 18 came
    back with the full ad -- 2,371 to 12,255 characters, against the 36-57 they had.

    Recursion is bounded by construction: the probe dict carries no `also_seen` of its own,
    so the nested call takes the ordinary fetch path and stops."""
    asked = False
    for a in (job.get("also_seen") or [])[:MAX_ALSO_SEEN_FETCHES]:
        url = (a.get("url") or "") if isinstance(a, dict) else ""
        if not url or url == job.get("url"):
            continue
        asked = True
        text = fill_description({"url": url, "source": a.get("source") or ""}) or ""
        if len(text) > len(best):
            best = text
        if len(best) >= MIN_DESC_CHARS:
            break
    if asked:
        DUPE_RECOVERIES[0] += 1
        DUPE_RECOVERIES[1] += len(best) >= MIN_DESC_CHARS
    return best

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
# Matched as a substring of the From header, which is what IMAP SEARCH FROM does -- so
# this is the whole DOMAIN rather than one mailbox, deliberately. Tom now runs two alerts
# (Europe and the US) and they arrive as separate emails; if the second one ever sends from
# a different mailbox or subdomain, an exact match would miss it and the status line would
# look completely normal. A silently missing feed is the expensive failure here.
REVOPSROLES_SENDER = "revopsroles.com"
REVOPSROLES_LOOKBACK_DAYS = 4   # covers a missed run (e.g. a quiet weekend) without
                                 # re-scanning the whole mailbox; reprocessing an
                                 # already-seen job is harmless, seen.json dedupes it

# A compensation figure as this digest writes it: "$104k – $183k", "€70,000 - €90,000",
# "£85k". Matched on the SHAPE of the text rather than on the colour of the span holding
# it, and that is the whole point of the rewrite.
#
# The selector used to be color:#16a34a, and the site restyled: salary is #9e4d00 now and
# #16a34a is gone entirely (#10b981 is the category tag's border). Nothing failed loudly.
# Every row simply arrived with salary "" -- 0 of 32 on the dashboard -- and it stayed
# invisible for as long as it did because European postings mostly state no salary anyway,
# so an empty field looked normal. It only became load-bearing when US rows started
# requiring a stated salary.
#
# A colour is presentation and will change again. A currency symbol next to digits is the
# thing itself.
REVOPSROLES_SALARY = re.compile(
    r"[$£€]\s?[\d,.]+\s*[kKmM]?(?:\s*[-–—to]{1,3}\s*[$£€]?\s?[\d,.]+\s*[kKmM]?)?")


def parse_revopsroles_jobs(body):
    """Every job block in one revopsroles digest email, as dicts.

    Pure and offline, so tests/fixtures/revopsroles-digest.html can pin it. That fixture is
    the guard that matters for this source: the failure mode here is not an exception, it is
    a selector going stale after a restyle and a field quietly emptying for months.

    Returns id/title/company/location/salary/category/seniority/work_mode. Field extraction
    is per-block, bounded to the job's own region, so one row's salary can never be read
    onto another."""
    out = []
    for chunk in (body or "").split('<a href="https://revopsroles.com/jobs/')[1:]:
        m = re.match(r'([0-9a-fA-F-]+)"[^>]*>(.*?)</a>(.*)', chunk, re.S)
        if not m:
            continue
        jid, title_raw, rest = m.groups()
        region = rest[:1500]        # bounds the field search to this job's own block
        cl = re.search(r'>([^<]+)<!--\s*-->\s*·\s*([^<]+)</span>', region)
        company, loc = ((clean_text(cl.group(1)), clean_text(cl.group(2))) if cl
                        else ("", ""))
        # The first span in this block whose TEXT reads as money. Skipping the company and
        # location span matters: a location like "Palo Alto, CA 94301" would otherwise be
        # read as a figure.
        salary = ""
        for text in re.findall(r">([^<]+)</span>", region):
            text = clean_text(text)
            if text in (company, loc) or not text:
                continue
            if REVOPSROLES_SALARY.search(text):
                salary = text
                break
        tags_m = re.search(r'margin-top:8px">(.*?)</div>', region, re.S)
        tags = [clean_text(t) for t in
                (re.findall(r">([^<]+)</span>", tags_m.group(1)) if tags_m else [])]
        # Category, then seniority, then work mode -- and every one of them optional. A
        # recent digest carried only two tags (category and seniority) with no work mode at
        # all, so indexing blind would have thrown on a real email.
        category, seniority, work_mode = (tags + ["", "", ""])[:3]
        out.append({"id": jid, "title": clean_text(title_raw), "company": company,
                    "location": loc, "salary": salary, "category": category,
                    "seniority": seniority, "work_mode": work_mode})
    return out


def fetch_revopsroles(gmail_address, gmail_app_password, diag=None):
    """Parses Tom's revopsroles.com daily digest email (read via Gmail IMAP) instead of
    scraping the site directly. No full description field is present in the digest, so
    the real JD is lazy-fetched from the job's revopsroles.com page via
    jsonld_job_description() for survivors, same pattern as Greenhouse/LinkedIn --
    though that fetch is itself likely to hit the same bot-challenge, so a short
    synthesized summary (category/seniority/work mode) is kept as a fallback."""
    out, seen_ids = [], set()
    # Per-email accounting. Tom runs two alerts now (Europe and the US) and they arrive as
    # separate emails, so "one email produced everything" and "two emails each produced
    # half" have to be distinguishable in the status footer -- otherwise a feed that stops
    # arriving looks exactly like a quiet day.
    emails, with_salary = 0, 0
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
            emails += 1
            before = len(out)
            for row in parse_revopsroles_jobs(body):
                jid = row["id"]
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                bump_raw("revopsroles", 1)
                title, company, loc = row["title"], row["company"], row["location"]
                salary, work_mode = row["salary"], row["work_mode"]
                category, seniority = row["category"], row["seniority"]
                cc = country_code(loc.rsplit(",", 1)[-1]) if "," in loc else country_code(loc)
                if salary:
                    with_salary += 1
                # The work mode is a TAG in this digest, not part of the location string,
                # and market_of() only reads the location. For the US that loses real rows:
                # remote is the REQUIREMENT there, so "Austin, United States" tagged
                # "Remote" is a US remote role that matching on the location alone drops as
                # on-site. So the tag is appended to what the gate sees, while `loc` itself
                # is stored unchanged because that is what Tom reads on the card.
                #
                # US ONLY, and that restriction is the whole point. In Europe remote
                # wording is a reason to REJECT -- market_of() returns None for a bare
                # country next to "remote", which is how remote-EMEA reqs are kept out --
                # so appending "(Remote)" to an "Ireland" row would drop a genuine Irish
                # role that is currently kept. Canada needs no help either: its branch
                # accepts remote already.
                gate_loc = f"{loc} ({work_mode})" if work_mode and cc == "us" else loc
                reason = prefilter(title, gate_loc, cc)
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
                    "market": market_of(cc, gate_loc),
                    "url": src_url, "source": "revopsroles", "salary": salary,
                    "posted_at": posted,
                    "_detail": src_url, "_fallback_desc": summary,
                })
            if diag is not None:
                diag[f"revopsroles:email{emails}"] = f"kept {len(out) - before}"
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    if diag is not None:
        # with_salary is worth its own number because every US row needs a stated salary
        # (us_comp_unstated), and this field has already emptied itself once without
        # anything failing: the selector was a colour, the site restyled, and 32 rows in a
        # row arrived with nothing. If this count goes to zero again it is the first thing
        # to look at, and it should be on the status line rather than inferred from an
        # empty dashboard.
        diag["revopsroles:emails"] = (f"{emails} email{'s' if emails != 1 else ''}, "
                                      f"{with_salary} row(s) with a salary")
    return out

APIFY_ACTOR = "memo23~apify-hiring-cafe-scraper"
# Tom's hiring.cafe searches, as structured data rather than the percent-encoded
# searchState blobs that used to live here.
#
# Those blobs were 1-2KB URL-encoded JSON strings. They worked, but editing the targeting
# meant hand-editing percent-encoded JSON, which is the reason adding a market here had
# been avoided for as long as it had. hiringcafe_url() rebuilds the URLs from the
# definitions below, and tests/fixtures/hiringcafe-searchstate.json is a capture of the
# five URLs Tom's own browser produced: the round-trip test asserts every rebuilt
# searchState decodes to exactly that. A silent change in targeting here would show up as
# a source going thin rather than as an error, so the fixture is the guard.
#
# The "id" on each location is hiring.cafe's own opaque geo key, taken from those URLs.
# workplace_types is PER LOCATION and load-bearing: it is how the European searches keep
# remote-from-anywhere rows out at source, and how the US ones ask for remote only.


def _hc_country(long_name, short_name, population, hc_id, workplace_types=(),
                flexible=()):
    """A whole-country location."""
    return {"id": hc_id, "types": ["country"],
            "address_components": [{"long_name": long_name, "short_name": short_name,
                                    "types": ["country"]}],
            "formatted_address": long_name, "population": population,
            "workplace_types": list(workplace_types),
            "options": {"flexible_regions": list(flexible)}}


def _hc_london(radius_miles, workplace_types=()):
    """London with a commuter radius, which is the only UK shape in scope: market_of()
    accepts London and the commuter belt and nothing else in the UK, so a whole-UK
    search would pay Apify for Manchester and Edinburgh rows that prefilter() then
    drops on arrival."""
    return {"id": "xRg1yZQBoEtHp_8UXQ1z", "types": ["locality"],
            "address_components": [
                {"long_name": "London", "short_name": "London", "types": ["locality"]},
                {"long_name": "England", "short_name": "ENG",
                 "types": ["administrative_area_level_1"]},
                {"long_name": "United Kingdom", "short_name": "GB", "types": ["country"]}],
            "geometry": {"location": {"lat": 51.50853, "lon": -0.12574}},
            "formatted_address": "London, England, GB", "population": 8961989,
            "workplace_types": list(workplace_types),
            "options": {"radius": radius_miles, "radius_unit": "miles",
                        "ignore_radius": False}}


def _hc_grand_rapids():
    """Home, remote, flexible outward to anywhere.

    This is how the US searches are scoped, and it is a better expression of "ideally
    Michigan but anywhere remote" than a whole-country search: the radius puts local roles
    first while flexible_regions opens it to the state, the country, the continent and the
    world. There is deliberately no whole-US country location anywhere in this file."""
    return {"id": "YRk1yZQBoEtHp_8UuNQa", "types": ["locality"],
            "address_components": [
                {"long_name": "Grand Rapids", "short_name": "Grand Rapids",
                 "types": ["locality"]},
                {"long_name": "Michigan", "short_name": "MI",
                 "types": ["administrative_area_level_1"]},
                {"long_name": "United States", "short_name": "US", "types": ["country"]}],
            "geometry": {"location": {"lat": 42.96336, "lon": -85.66809}},
            "formatted_address": "Grand Rapids, MI, US", "population": 195097,
            "workplace_types": ["Remote"],
            "options": {"radius": 50, "radius_unit": "miles", "ignore_radius": False,
                        "flexible_regions": ["anywhere_in_administrative_area_level_1",
                                             "anywhere_in_country",
                                             "anywhere_in_continent",
                                             "anywhere_in_world"]}}


def _hc_nl(workplace_types=(), flexible=()):
    return _hc_country("The Netherlands", "NL", 17231017, "1BY1yZQBoEtHp_8UEq3V",
                       workplace_types, flexible)


def _hc_ie(workplace_types=(), flexible=()):
    return _hc_country("Ireland", "IE", 4853506, "kxY1yZQBoEtHp_8UEq3V",
                       workplace_types, flexible)


def _hc_be(workplace_types=(), flexible=()):
    return _hc_country("Belgium", "BE", 11422068, "QRY1yZQBoEtHp_8UEq3V",
                       workplace_types, flexible)


def _hc_ca(workplace_types=(), flexible=()):
    return _hc_country("Canada", "CA", 37058856, "UxY1yZQBoEtHp_8UEq3V",
                       workplace_types, flexible)


# The RevOps title query, shared verbatim by the European and US searches so they cannot
# drift apart on what counts as the target function. REVOPS_CORE, the code-side US gate,
# is pinned against every phrase in here by a test -- a term in the search that the regex
# does not match is a row Apify is paid for and prefilter() then throws away.
HC_REVOPS_TITLES = (
    '"revenue operations" OR "RevOps" OR "sales operations" OR "sales ops" OR '
    '"CS operations" OR "customer success operations" OR "GTM operations" OR '
    '"go-to-market operations" OR "GTM strategy" OR "go-to-market strategy" OR '
    '"revenue strategy" OR "sales enablement" OR "revenue enablement" OR '
    '"commercial operations" OR "sales strategy" OR "revenue strategy & operations" OR '
    '"sales strategy & operations" OR "GTM strategy & operations"')

# label -> searchState. dateFetchedPastNDays is wider than MAX_POST_AGE_DAYS on purpose;
# the age filter downstream still applies.
#
# roleTypes "Individual Contributor" on both RevOps searches is deliberate: Tom wants
# senior-IC RevOps, so anything hiring.cafe classifies as People Manager is excluded at
# source.
APIFY_HIRINGCAFE_SEARCHES = {
    # RevOps titles across the four European markets plus Canada. Europe is Hybrid/Onsite
    # only, which keeps remote-EMEA rows out at source; Canada adds Remote because Tom is
    # a citizen and can work anywhere in it.
    "revops": {
        "locations": [
            _hc_nl(("Hybrid", "Onsite")),
            _hc_ie(("Hybrid", "Onsite")),
            _hc_be(("Hybrid", "Onsite")),
            _hc_ca(("Hybrid", "Onsite", "Remote")),
            _hc_london(50, ("Hybrid", "Onsite", "Field")),
        ],
        "commitmentTypes": ["Full Time"],
        "dateFetchedPastNDays": 14,
        "roleTypes": ["Individual Contributor"],
        "seniorityLevel": ["Mid Level", "Senior Level"],
        "excludedLanguageRequirements": ["dutch", "german", "spanish", "french"],
        "sortBy": "date",
        "jobTitleQuery": HC_REVOPS_TITLES,
    },
    # Senior CS across NL, Ireland, London and Canada. managementYoeRange caps the people
    # management expected, which is what keeps this on the senior-IC track.
    "cs-eu-ca": {
        "locations": [
            _hc_nl(("Hybrid", "Onsite", "Field")),
            _hc_ie(("Hybrid", "Onsite", "Field")),
            _hc_london(50, ("Hybrid", "Onsite")),
            _hc_ca(),
        ],
        "dateFetchedPastNDays": 21,
        "managementYoeRange": [0, 2],
        "seniorityLevel": ["Senior Level"],
        "excludedLanguageRequirements": ["dutch", "german", "french"],
        "sortBy": "date",
        "jobTitleQuery": '"Customer success"',
    },
    # CS titles, Netherlands only and at any seniority -- profile.md's CSM track weighting
    # makes a plain NL Customer Success Manager a primary target and keeps the same role
    # modest everywhere else.
    "cs-nl": {
        "locations": [_hc_nl(("Hybrid", "Onsite", "Field"))],
        "dateFetchedPastNDays": 21,
        "excludedLanguageRequirements": ["dutch", "german"],
        "sortBy": "date",
        "jobTitleQuery": '"Customer success"',
    },
    # US RevOps: remote, transparent salaries only, and a compensation bound.
    #
    # maxCompensationLowEnd is Tom's own setting, kept verbatim at his explicit direction.
    # Reading the field name against its three siblings (minCompensationLowEnd,
    # minCompensationHighEnd, maxCompensationHighEnd) it looks like an UPPER bound on the
    # bottom of the posted range, which would invert the filter. If the US feed ever comes
    # back full of underpaid roles, this is the first thing to check.
    "us-revops": {
        "locations": [_hc_grand_rapids()],
        "commitmentTypes": ["Full Time"],
        "dateFetchedPastNDays": 14,
        "restrictJobsToTransparentSalaries": True,
        "roleTypes": ["Individual Contributor"],
        "maxCompensationLowEnd": "130000",
        "seniorityLevel": ["Mid Level", "Senior Level"],
        "excludedLanguageRequirements": ["dutch", "german", "spanish", "french"],
        "sortBy": "date",
        "jobTitleQuery": HC_REVOPS_TITLES,
    },
    # US senior CS. Wanted only because this search confirms remote and the salary band,
    # which is why the code requires a stated salary on every US row (see prefilter and
    # us_comp_unstated).
    "us-cs": {
        "locations": [_hc_grand_rapids()],
        "dateFetchedPastNDays": 21,
        "restrictJobsToTransparentSalaries": True,
        "managementYoeRange": [0, 2],
        "maxCompensationLowEnd": "130000",
        "seniorityLevel": ["Senior Level"],
        "excludedLanguageRequirements": ["dutch", "german", "french"],
        "sortBy": "date",
        "jobTitleQuery": '"Customer success"',
    },
}


def hiringcafe_url(search_state):
    """A hiring.cafe address-bar URL for one searchState dict.

    separators= matches what the browser produces (no spaces), and sort_keys is
    deliberately NOT set: key order follows the dict above, so a diff of the generated URL
    stays readable and the round-trip test compares decoded JSON rather than bytes."""
    qs = urllib.parse.urlencode({"searchState": json.dumps(search_state,
                                                           separators=(",", ":"))},
                                quote_via=urllib.parse.quote_plus)
    return f"https://hiringcafe.com/?{qs}"
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
    for label, state in APIFY_HIRINGCAFE_SEARCHES.items():
        url = hiringcafe_url(state)
        try:
            # Token goes in the header, never in the query string. requests puts the
            # full effective URL into the text of every exception it raises -- a 429 from
            # this endpoint reads "429 Client Error: ... for url: ...?token=apify_api_..."
            # -- and that text is written straight into docs/status.json, which is
            # committed and pushed. It leaked the token into a commit on 6 Sep 2026 and
            # GitHub push protection rejected the push, which stopped every scan since.
            r = requests.post(
                f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
                headers={"Authorization": f"Bearer {token}"},
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

# How many assistant turns a server-tool call may be resumed across. The API returns
# stop_reason "pause_turn" when its own tool loop hits an iteration limit, and handing the
# assistant turn straight back resumes it. Bounded so a pathological loop cannot run the
# bill up unattended -- the same reason applyq.SERVER_TOOL_MAX_TURNS exists.
SERVER_TOOL_MAX_TURNS = 3


def _claude_call(api_key, model, system, user, max_tokens, extra=None, cache_system=False,
                 tools=None):
    """A Messages API call, with bounded retry on the transient failures. cache_system
    puts a cache breakpoint on the system prompt: the deep-score prefix (rubric + the whole
    of profile.md) is identical for every job in a run, so without this it gets re-billed
    on all 30 calls.

    `tools` turns this into a server-tool call, which is more than one HTTP request: the
    API answers "pause_turn" when its own tool loop needs resuming, and the assistant turn
    goes straight back with no extra user message. Without tools the loop runs exactly
    once, so every existing caller is unaffected."""
    messages = [{"role": "user", "content": user}]
    for _turn in range(SERVER_TOOL_MAX_TURNS if tools else 1):
        body = {
            "model": model, "max_tokens": max_tokens,
            "system": ([{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}]
                       if cache_system else system),
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        if extra:
            body.update(extra)
        last = None
        payload = None
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
            r.raise_for_status()         # 4xx other than the above is a real bug, not a blip
            payload = r.json()
            break
        if payload is None:
            raise last or RuntimeError("claude call failed")
        note_usage(payload.get("usage") or {})
        stop = payload.get("stop_reason")
        # Opus 5 can decline a request outright (HTTP 200, empty content) and can run out of
        # room mid-answer. Both used to surface as an empty string and a bogus score of 0.
        if stop == "refusal":
            raise RuntimeError("model declined to score this posting (stop_reason=refusal)")
        if stop == "max_tokens":
            raise RuntimeError(f"hit max_tokens ({max_tokens}) before finishing")
        if stop == "pause_turn":
            messages.append({"role": "assistant", "content": payload.get("content", [])})
            continue
        return "".join(b.get("text", "") for b in payload.get("content", [])
                       if b.get("type") == "text")
    raise RuntimeError(f"server-tool call did not finish in {SERVER_TOOL_MAX_TURNS} turns")

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
            # Which other target markets this employer posts roles in, derived free from the
            # ATS board the apply-link lookup already fetches. Only meaningful for a US row,
            # where an internal move later is the difference between a dead end and a route
            # abroad, so it is only emitted when there is something to say.
            + (f"Transfer: this employer also posts roles in {job['transfer_markets']}\n"
               if job.get("transfer_markets") else "")
            # A stub is not a description, and saying so is the whole point of this branch.
            # revopsroles rows arrive carrying a ~50-character synthesized summary
            # ("Category: CS Ops; Seniority: Senior") and the old code emitted that under
            # "Description:" as though it were the posting. The model had no way to know,
            # and it showed: stub rows scored a 6.03 mean against 5.78 for rows with a real
            # JD, with 22 of 59 clearing the gate. Absence of evidence was reading as
            # absence of problems.
            + ("EVIDENCE: THIN. No real posting text could be retrieved for this role -- "
               "what follows is all that is known, and it is a few words of metadata "
               "rather than a job description. Score conservatively and do NOT infer "
               "scope, seniority, comp or requirements that are not stated. The two hard "
               "disqualifier checks (sponsorship ruled out, another language required) "
               "could not run on text this short, so neither has been cleared.\n"
               + (f"What is known: {desc}" if desc else "Nothing beyond the fields above.")
               ) if is_thin(desc) else f"Description: {desc}")

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
        # What the scorer read off the posting about pay, kept on the row so the apply
        # queue can decide comp risk from the same numbers rather than re-reading the ad.
        # A row scored before this existed has no "comp" key at all, and applyq.py treats
        # that as "not stated" -- the conservative direction, since that asks Tom rather
        # than assuming the money is fine.
        "comp": {"stated": bool(data.get("salary_stated")),
                 "min_base": _as_float(data.get("salary_min_base")),
                 "currency": str(data.get("salary_currency") or "").upper()[:3]},
        # Structured as well as flagged, because the apply queue has to ACT on this one
        # rather than just show it: it asks Tom which market is right before building a CV
        # or filling a form. Parsing it back out of a flag string would be the wrong kind
        # of clever. None on a row where the posting and the feed agree, which is most.
        "market_conflict": market_conflict(job, data),
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

def cmd_dedupe():
    """Collapse duplicates already on the dashboard, without running a scan.

    A normal run does this too -- see the dedupe stage in main() -- so this exists for the
    case where you want the dashboard cleaned now rather than at the next scan, and for
    seeing exactly what a change to same_role() would collapse before letting a run do it."""
    jobs = load_json("docs/jobs.json", [])
    kept, dropped = collapse_duplicates(jobs)
    for loser, winner in dropped:
        record_drop(loser, "dedupe", dupe_reason(winner))
        print(f"  keep {winner.get('score', '-'):>4}  {winner.get('company')} | "
              f"{winner.get('title')} | {winner.get('location')} [{winner.get('source')}]")
        print(f"  drop {loser.get('score', '-'):>4}  {loser.get('company')} | "
              f"{loser.get('title')} | {loser.get('location')} [{loser.get('source')}]\n")
    if not dropped:
        print("No duplicates on the dashboard.")
        return
    prev = load_json("docs/excluded.json", {})
    prev["rows"] = trim_drop_rows(DROPS + prev.get("rows", []))
    prev["counts"] = {**prev.get("counts", {}),
                      "dedupe": prev.get("counts", {}).get("dedupe", 0) + len(dropped)}
    json.dump(kept, open("docs/jobs.json", "w"), indent=1)
    json.dump(prev, open("docs/excluded.json", "w"), indent=1)
    print(f"{len(jobs)} rows -> {len(kept)}: {len(dropped)} duplicates collapsed. "
          f"The ids they were shown under are carried on the surviving row, so a Hide or "
          f"Mark applied recorded against one still holds.")

def backfill_targets(jobs, days, now=None):
    """Rows whose stored ad was cut short by the old DESC_STORE_CAP, newest first.

    `desc_chars` is written before truncation (see the scoring loop), so a row where the
    stored text is shorter than that number is exactly a row the old 1200-char cap trimmed.

    A row that was always short is a target too, but only when it carries `also_seen`. That
    qualifier is the whole difference: a stub with no folded-away copy has nowhere new to
    look and re-requesting it just buys another empty page, while a stub that absorbed a
    duplicate is holding the employer's own link and has simply never been asked for it
    (see fill_from_duplicates()). It is the second case that put 34 of 34 revopsroles rows
    on the dashboard scored off fifty characters.

    Bounded by age on purpose. The dashboard only ever renders 7 days (MAX_AGE_DAYS in
    docs/index.html) while the file keeps 45, so the default scope is the rows Tom can
    actually click. Widening it means re-requesting hundreds of postings that expired weeks
    ago, which is a lot of 404s for rows nobody will open."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    out = [j for j in jobs
           if (j.get("found_at") or "") >= cutoff
           and (len(j.get("description") or "") < (j.get("desc_chars") or 0)
                or (is_thin(j.get("description")) and j.get("also_seen")))]
    return sorted(out, key=lambda j: j.get("found_at") or "", reverse=True)

def backfill_row(job, fetch=None):
    """Re-fetch one row's full ad. Returns the text, or "" if nothing better was found.

    Two traps, both load-bearing:

    1. `description` is blanked on the copy handed to fill_description(). It returns early
       when the description it is given already clears MIN_DESC_CHARS (900), and a stored
       sample is 1200, so without this it hands the sample straight back and never fetches.
    2. `apply_url` is tried ahead of `url`. Half these rows arrived through an aggregator,
       whose link is an advert rather than the posting; `apply_url` is the employer's own
       board, which is both likelier to answer and likelier to carry the whole ad. The
       source-specific `_detail` endpoint is long gone -- it is popped before a row is
       stored -- so the generic fetchers and the JSON-LD path do this work.
    """
    fetch = fetch or fill_description
    best = ""
    for target in (job.get("apply_url"), job.get("url")):
        if not target:
            continue
        probe = dict(job, description="", url=target)
        try:
            got = fetch(probe) or ""
        except Exception:
            got = ""
        if len(got) > len(best):
            best = got
        # Back to the length the scorer saw AND long enough to be a real posting. The
        # second half is what makes this work for a stub: its desc_chars is 54, so the
        # bare "as long as before" test was satisfied by the first fetcher that returned
        # anything at all and the row stopped asking while still holding a stub.
        if len(best) >= max(job.get("desc_chars") or 0, MIN_DESC_CHARS):
            break
    return best

def cmd_backfill_jd(days, fetch=None):
    """Recover the full ad for rows stored under the old 1200-character cap.

    A carried-forward row is never re-fetched by an ordinary run -- main() merges `existing`
    through verbatim and only the new rows go near a fetcher -- so raising DESC_STORE_CAP
    fixes every future row and none of the ones already on the dashboard. This closes that
    gap once. It needs no API key: the generic fetchers and schema.org parsing do it all."""
    jobs = load_json("docs/jobs.json", [])
    targets = backfill_targets(jobs, days)
    if not targets:
        print(f"Nothing to backfill in the last {days} days.")
        return
    print(f"{len(targets)} thin or truncated rows in the last {days} days. Re-fetching.\n")
    by_id, crossed = {}, set()
    recovered = was_stub = 0
    for j in targets:
        stored, want = len(j.get("description") or ""), j.get("desc_chars") or 0
        stub = is_thin(j.get("description"))
        got = backfill_row(j, fetch)
        # Never shorten a row. A fetcher that comes back with a page's furniture instead of
        # the posting would otherwise replace a real sample with something worse.
        if len(got) > stored:
            by_id[id(j)] = got
            recovered += 1
            # A row that crossed the thin line is a different case from one that merely got
            # longer: its stored score was formed with no posting to read. Counted here and
            # rescored below.
            if stub and not is_thin(got):
                was_stub += 1
                crossed.add(id(j))
            mark = "OK  "
        else:
            mark = "--  "
        print(f"  {mark}{(j.get('source') or '?'):<12} {stored:>5} -> {max(len(got), stored):<6} "
              f"of {want:<6} {(j.get('company') or '?')[:28]}")
    for j in jobs:
        if id(j) in by_id:
            j["description"] = by_id[id(j)]
            j["desc_chars"] = len(by_id[id(j)])
    json.dump(jobs, open("docs/jobs.json", "w"), indent=1)
    missed = len(targets) - recovered
    print(f"\nRecovered {recovered} of {len(targets)}. {missed} could not be re-fetched "
          f"(expired postings, or a board that refuses datacenter IPs); those rows keep the "
          f"sample they had and say so in the dashboard's copy block.")
    if was_stub:
        rescore_recovered(jobs, [j for j in jobs if id(j) in crossed])


def rescore_recovered(jobs, stale):
    """Re-run the deep score on rows whose posting only just arrived.

    Recovering the text and leaving the score alone would be the worse half of the job. A
    row scored under EVIDENCE: THIN was told in as many words that it was looking at a few
    words of metadata and that neither hard disqualifier had been cleared -- a Mixpanel
    stub reached 8.2 on 54 characters. Once the real 6,677-character ad is in hand that
    number is not conservative, it is simply about a different thing, and it is the number
    Tom sorts the dashboard by.

    Rescoring in place rather than freeing the row for the next scan is deliberate: these
    rows are older than the revopsroles digest's 4-day lookback, so a freed row would not
    come back from the feed -- it would just be gone. The row keeps its id, so a Hide or a
    Mark-applied recorded against it still holds.

    No API key means no rescore, and that is said out loud rather than passed over: a
    dashboard where the text and the score disagree about how much was known is worse than
    one where neither moved."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    stale = [j for j in stale if not is_thin(j.get("description"))]
    if not stale:
        return
    if not api_key:
        print(f"\n{len(stale)} row(s) now have a real posting but keep a score formed "
              f"without one. Set ANTHROPIC_API_KEY and re-run to rescore them.")
        return
    print(f"\nRescoring {len(stale)} row(s) whose posting only just arrived.")
    system = score_system()
    dropped = set()
    rescored = 0
    for j in stale:
        before = j.get("score")
        # The free checks first, exactly as the scan runs them, and for the same reason:
        # these are the two questions the stub could not be asked. A role whose ad rules
        # out sponsorship was sitting on the dashboard with a score because the sentence
        # saying so had never been fetched. No Opus call is worth making for it.
        quote = says_no_sponsorship(j.get("description"))
        stage = "no-sponsorship" if quote else ""
        if not quote:
            quote = requires_other_language(j.get("description"))
            stage = "language-required" if quote else ""
        if quote:
            dropped.add(id(j))
            record_drop(j, stage, f'JD (recovered): "{quote}"')
            print(f"  drop  {j.get('title')} @ {j.get('company')} ({stage})")
            continue
        try:
            result = score_job(api_key, system, j)
        except Exception as e:
            print(f"  ERR   {j.get('title')} @ {j.get('company')} ({str(e)[:80]})")
            continue
        if result.get("disqualified"):
            # The recovered ad rules Tom out. Same policy as the scan itself: dropped
            # outright rather than shown with a caveat -- and this is exactly the row the
            # thin-evidence warning could not catch, because there was nothing to read.
            dropped.add(id(j))
            record_drop(j, result["stage"],
                        f"{result['reason']} (read off the recovered posting)")
            print(f"  drop  {j.get('title')} @ {j.get('company')} "
                  f"({result['stage']}: {result['reason']})")
            continue
        j.update(result)
        rescored += 1
        print(f"  {before} -> {j['score']:<5} {(j.get('company') or '?')[:28]} "
              f"| {(j.get('title') or '')[:40]}")
    kept = [j for j in jobs if id(j) not in dropped]
    json.dump(kept, open("docs/jobs.json", "w"), indent=1)
    if dropped:
        prev = load_json("docs/excluded.json", {})
        prev["rows"] = trim_drop_rows(DROPS + prev.get("rows", []))
        json.dump(prev, open("docs/excluded.json", "w"), indent=1)
    print(f"Rescored {rescored}, dropped {len(dropped)}, "
          f"{len(stale) - rescored - len(dropped)} left as they were.")

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
    if "--dedupe" in sys.argv:
        return cmd_dedupe()
    if "--backfill-jd" in sys.argv:
        days = 7
        if "--days" in sys.argv:
            try:
                days = int(sys.argv[sys.argv.index("--days") + 1])
            except (IndexError, ValueError):
                print("--days needs a number; using 7.")
        return cmd_backfill_jd(days)

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
            src_status["Adzuna (NL+UK+CA+US)"] = f"{src_line('adzuna', len(jobs))} | " + "; ".join(f"{k.split(':')[1]}={v}" for k, v in diag.items() if k.startswith("adzuna:"))
        except Exception as e:
            src_status["Adzuna (NL+UK+CA+US)"] = f"FAIL: {e}"
    else:
        src_status["Adzuna (NL+UK+CA+US)"] = "skipped: no ADZUNA_APP_ID/KEY set"

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

    # 3. JobSpy: Indeed for Ireland (Adzuna has no Ireland endpoint), Indeed + Google Jobs
    #    for Canada and the US.
    try:
        diag = {}
        jobs = fetch_jobspy(diag); found += jobs
        src_status["Indeed/JobSpy (IE+CA+US)"] = (
            f"{src_line('indeed', len(jobs))} | "
            + "; ".join(f"{k.split(':')[1]}={v}" for k, v in diag.items()))
    except Exception as e:
        src_status["Indeed/JobSpy (IE+CA+US)"] = f"skipped: {e}"

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
            diag = {}
            jobs = fetch_revopsroles(gmail_addr, gmail_pw, diag); found += jobs
            src_status["revopsroles.com"] = (
                f"{src_line('revopsroles', len(jobs))} | "
                + "; ".join(f"{k.split(':')[1]}={v}" for k, v in diag.items()))
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

    # cross-source dedupe: the same role can arrive from Indeed + Greenhouse etc, and can
    # also resurface via a different source in a later run than the one that first found
    # it -- so this compares against everything already on the dashboard, not just this
    # run, and heals any dashboard duplicates left over from before this matching existed
    # in the same pass. See same_role() for what counts as the same posting and
    # merge_found_into_dashboard() for why this has to be one combined pass rather than
    # dashboard-then-fetches: same_role() isn't transitive (Adzuna's "AWS" and hiring.cafe's
    # "Amazon" don't match each other, only both match LinkedIn's "Amazon Web Services
    # (AWS)"), so comparing the dashboard and the fetches as two separate passes can pair a
    # fuller name with whichever partial name it meets first and never revisit the other.
    existing, found, dedupe_drops = merge_found_into_dashboard(existing, found)
    for loser, winner in dedupe_drops:
        record_drop(loser, "dedupe", dupe_reason(winner))
    src_status["dedupe"] = f"{len(dedupe_drops)} duplicates collapsed (dashboard + this run's fetches)"

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
    deferrals = load_deferrals()
    # Reuses the list already read for the ATS fetchers above, rather than re-reading the
    # file (which is a dict with a "companies" key, not a bare list).
    watched_names = {(c.get("name") or "").strip().lower() for c in companies}

    # PASS A: screen. Everything up to and including the Haiku kill/keep, for up to
    # MAX_SCREENED_PER_RUN rows. Survivors are collected rather than scored immediately,
    # so that pass B can spend the expensive budget on the best of them instead of on
    # whichever source happened to be fetched first. See priority().
    #
    # Ordering pass A matters too, because it has its own cap: the rows most likely to
    # deserve a slot should be the ones that get screened at all. The same key is used,
    # minus the signals that only exist after the description is fetched.
    new_jobs.sort(key=lambda j: priority(j, deferrals), reverse=True)
    survivors = []

    # Board lookups spent rescuing stub descriptions this run, and the cache they share
    # with the post-score lookup further down -- so a company probed here is not probed
    # again there.
    rescues, rescued = 0, 0
    # Imported here, not at the top. findform imports this module -- it needs market_of()
    # to tell whether a board's posting is in one of Tom's markets -- so a top-level
    # import either way round is a cycle. It resolves at runtime today because both sides
    # only touch the other inside functions, which is a thing that works right up until
    # somebody adds a module-level reference and the whole pipeline stops importing. One
    # deferred import is cheaper than that failure.
    import findform
    board_cache = findform.load_cache()

    for j in new_jobs[:MAX_SCREENED_PER_RUN]:
        # Get the real posting text before anything reads it -- only for survivors of the
        # title/location prefilter, so the fetches stay cheap. Every downstream decision
        # (the two disqualifier checks below, both model calls) is only as good as this.
        j["description"] = fill_description(j)
        j.pop("_detail", None); j.pop("_fallback_desc", None)

        # Still no posting? Go to the company's own board for it.
        #
        # Deliberately BEFORE the two hard disqualifiers rather than after. They read the
        # description, so on a stub neither can fire -- a role whose JD rules out
        # sponsorship was passing both checks silently. Recovering the text first is what
        # makes them work at all on these rows, and that is worth more than the score.
        if is_thin(j["description"]) and rescues < MAX_JD_RESCUES_PER_RUN:
            rescues += 1
            desc, apply_url = rescue_description(j, companies, board_cache)
            if apply_url:
                j["apply_url"] = apply_url       # the real posting, not the advert
            if desc:
                j["description"] = desc
                rescued += 1
                print(f"  jd    recovered {len(desc)} chars for {j['title']} "
                      f"@ {j.get('company')}")

        j["desc_chars"] = len(j["description"])   # kept so a score can be audited later

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
        reason = us_comp_unstated(j)
        if reason:
            record_drop(j, "us-comp-unstated", reason)
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
        j["_watched"] = (j.get("company") or "").strip().lower() in watched_names
        survivors.append(j)

    # PASS B: deep score, best first, until the budget runs out.
    survivors.sort(key=lambda j: priority(j, deferrals), reverse=True)
    searches, searched_ok = 0, 0
    for j in survivors:
        if len(scored) >= MAX_SCORED_PER_RUN:
            # Out of budget. NOT added to seen, so this row comes back next run, and its
            # deferral counter goes up so it climbs the queue when it does.
            deferrals[j["id"]] = min(deferrals.get(j["id"], 0) + 1, DEFER_CAP)
            continue

        # Last resort for a row still carrying no posting: pay for a web search.
        #
        # Here rather than in pass A on purpose. This is the only place in the pipeline
        # that spends tokens to get EVIDENCE rather than to form a judgement, so it is
        # spent only on rows that already survived the Haiku screen and have won a
        # deep-scoring slot -- never on one about to be killed or deferred. The queue has
        # already put them in Tom's own preference order, so the budget lands on the rows
        # that matter most.
        if (api_key and not dry and is_thin(j.get("description"))
                and searches < MAX_JD_SEARCHES_PER_RUN):
            searches += 1
            url = search_jd_url(api_key, j)
            desc, confirmed = verify_jd(url, j) if url else ("", "")
            if desc:
                j["description"] = desc
                j["desc_chars"] = len(desc)
                j["apply_url"] = j.get("apply_url") or confirmed
                searched_ok += 1
                print(f"  jd    web search recovered {len(desc)} chars for {j['title']} "
                      f"@ {j.get('company')}")
                # Re-run the two hard disqualifiers now there is finally text to read.
                # Skipping this would score a role the pipeline would have refused had the
                # posting arrived by any other route.
                quote = says_no_sponsorship(desc) or requires_other_language(desc)
                if quote:
                    stage = ("no-sponsorship" if says_no_sponsorship(desc)
                             else "language-required")
                    record_drop(j, stage, f'JD (web search): "{quote}"')
                    seen.add(j["id"])
                    print(f"  drop  {j['title']} @ {j.get('company')} "
                          f"({stage}, found in the recovered JD)")
                    continue
            elif url:
                print(f"  jd    web search found a page for {j['title']} that could not be "
                      f"confirmed as the same role; left thin")
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
        j.pop("_watched", None)      # queue-only signal; never belongs in docs/jobs.json
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

    # Which board a role's application actually lives on, and whether /submit can fill it
    # without being asked -- read off the url and also_seen this run's dedupe already
    # assembled, never a network call. This is the free half of what findform.find_form()
    # does: it never guesses a company's board slug, so it costs nothing to compute for
    # every row on every scan, and it is exactly what the dashboard's checkmark can
    # promise without ever opening a browser. Applied to the whole merged list, not just
    # this run's new rows, so a row that has carried an also_seen link since before this
    # existed gets it filled in on the very next scan rather than staying blank forever.
    for j in merged:
        ats, fillable = submit.application_status(j)
        j["ats"] = ats
        j["ats_fillable"] = fillable
        # Separates two things that used to look like one problem. ats_fillable was false
        # on 432 of 478 rows, which reads as "the bot cannot fill this" -- but for most of
        # them the link is a job-board advert and the form was never found at all. One is a
        # missing driver, the other a missing lookup, and only the second is fixed below.
        j["apply_link"] = findform.apply_link_state(
            j.get("apply_url") or j.get("url"), fillable)

    # Then the half that costs a network call: for the rows that still have no known
    # application host, go and find the company's own board. 373 of 492 rows were in that
    # state and the dashboard could only say "unknown" about every one of them, while
    # /submit would have found a board for a good share the moment one was queued. The
    # answer belongs on the dashboard, before he picks.
    #
    # Bounded four ways, because this is the one part of a scan that talks to eight board
    # APIs, and a scan that overruns its fifteen-minute tick races the next one on the
    # push to main. Only rows he can actually see are looked up (recent, and at or above
    # the borderline floor); the answer is cached per COMPANY, negatives included, so the
    # ~200 employers running their own careers stack are asked once a month rather than
    # every run; a single run looks up at most BOARD_LOOKUPS_PER_RUN new companies; and
    # findform.resolve_rows stops on its own wall-clock budget regardless, since how long
    # a company costs is a property of somebody else's infrastructure rather than of
    # anything measurable here. Whatever does not fit waits for the next run, which costs
    # nothing: the cache means each company is only ever paid for once.
    cutoff_seen = (datetime.now(timezone.utc) - timedelta(days=BOARD_LOOKUP_DAYS)).isoformat()
    todo = [j for j in merged
            if not j.get("ats")
            and j.get("found_at", "") >= cutoff_seen
            and (j.get("score") or 0) >= FLOOR]
    # Best row first, by the same ordering the scoring budget uses, and within that the
    # rows whose only link is a job-board advert. A row already pointing at a company ATS
    # has somewhere to apply even if this lookup never runs; an "aggregator only" row has
    # nowhere, so it is the one the budget should be spent on.
    todo.sort(key=lambda j: (j.get("apply_link") == "aggregator only",
                             priority(j, deferrals)), reverse=True)
    if todo:
        # The same cache object pass A already populated, so a company probed while
        # rescuing a description is not probed again here.
        cache = board_cache
        before = len(cache)
        found = findform.resolve_rows(todo, companies, cache,
                                      limit=BOARD_LOOKUPS_PER_RUN)
        hits = 0
        for j in todo:
            r = found.get(j.get("id")) or {}
            # Free either way: the board was fetched to match this row's title against
            # it, and every board entry carries a location, so which target markets the
            # employer hires in is arithmetic over data already in hand. It only earns its
            # place on a US row -- profile.md and the location_visa rubric lift the US band
            # from 2-3 to 4-5 when there is a route out -- so it is only recorded there.
            if r.get("transfer_markets") and j.get("market") == "US-Remote":
                j["transfer_markets"] = ", ".join(r["transfer_markets"])
            if r.get("outcome") == "found":
                j["ats"] = submit.detect_ats(r["url"]) or submit.apply_host_name(r["url"])
                j["ats_fillable"] = bool(submit.detect_ats(r["url"]))
                # Kept so /submit does not repeat the lookup, and so the dashboard can
                # link straight at the application rather than the advert.
                j["apply_url"] = r["url"]
                j["apply_link"] = findform.apply_link_state(r["url"], j["ats_fillable"])
                hits += 1
        findform.save_cache(cache)
        src_status["board lookup"] = (
            f"{len(todo)} rows with no known board, {hits} resolved; "
            f"{len(cache) - before} companies newly cached, {len(cache)} known")

    # How much of the board actually has somewhere to apply. Worth a line of its own
    # because it was 76% unusable and nothing said so: the dashboard's only signal was
    # ats_fillable, which conflates "no driver for this ATS" with "no form found at all".
    states = {}
    for j in merged:
        states[j.get("apply_link") or "unresolved"] = (
            states.get(j.get("apply_link") or "unresolved", 0) + 1)
    src_status["apply links"] = ", ".join(f"{k} {v}" for k, v in sorted(states.items()))

    src_status["screening"] = f"stage1 kept {kept}, killed {killed}; stage2 scored {len(scored)}"
    if searches:
        src_status["jd web search"] = (
            f"{searches} searched, {searched_ok} confirmed and used"
            + (f" (cap {MAX_JD_SEARCHES_PER_RUN})"
               if searches >= MAX_JD_SEARCHES_PER_RUN else ""))
    if DUPE_RECOVERIES[0]:
        asked, got = DUPE_RECOVERIES
        src_status["jd from duplicates"] = (
            f"{asked} thin row(s) asked the copies dedupe folded away, {got} recovered")
    if rescues:
        src_status["jd rescue"] = (
            f"{rescues} stub row(s) looked for their real posting, {rescued} recovered"
            + (f" (cap {MAX_JD_RESCUES_PER_RUN})"
               if rescues >= MAX_JD_RESCUES_PER_RUN else ""))
    # Everything that cleared stage one but did not get a deep-scoring slot. Prune the
    # counters against this set so the file cannot grow without bound, then say out loud
    # how deep the queue is: a backlog that never drains is the one way this design turns
    # latency into a role Tom never sees, and it should not be silent.
    pending = {j["id"] for j in survivors if j["id"] not in seen}
    deferrals = save_deferrals(deferrals, pending)
    if deferrals:
        longest = max(deferrals.values())
        src_status["queue"] = (f"{len(deferrals)} waiting for a scoring slot, longest "
                               f"{longest} run{'s' if longest != 1 else ''}"
                               + (" (escalated past fresh rows)"
                                  if longest >= DEFER_ESCALATES_AFTER else ""))
    elif survivors:
        src_status["queue"] = "empty; every screened role was scored this run"
    if USAGE["in"] or USAGE["cache_read"]:
        src_status["tokens"] = (f"in {USAGE['in']}, out {USAGE['out']}, "
                               f"cache read {USAGE['cache_read']}, written {USAGE['cache_write']}")
    if DROP_COUNTS:
        src_status["dropped"] = ", ".join(f"{k} {v}" for k, v in sorted(DROP_COUNTS.items()))

    # Rolling audit trail of what was thrown away, newest first.
    rows = trim_drop_rows(DROPS + load_json("docs/excluded.json", {}).get("rows", []))

    json.dump(sorted(seen), open("seen.json", "w"))
    json.dump(merged, open("docs/jobs.json", "w"), indent=1)
    json.dump({"last_run": now_iso(), "counts": DROP_COUNTS, "rows": rows},
              open("docs/excluded.json", "w"), indent=1)
    # Last line of defence before this dict becomes a committed, pushed file.
    src_status = {k: redact(v) for k, v in src_status.items()}
    json.dump({"last_run": now_iso(), "new_this_run": len(scored), "sources": src_status,
               "gate": GATE, "floor": FLOOR, "score_model": CLAUDE_SCORE_MODEL,
               "screen_model": CLAUDE_SCREEN_MODEL,
               # Same reason gate/floor are here: the dashboard reads the ordering from
               # the code rather than keeping its own copy that can drift.
               "market_tier": MARKET_TIER},
              open("docs/status.json", "w"), indent=1)
    if not dry:
        notify_strong_matches(scored)
    print(f"Done. {len(scored)} new on dashboard, {len(merged)} total, "
          f"{sum(DROP_COUNTS.values())} dropped this run.")

if __name__ == "__main__":
    main()
