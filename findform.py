#!/usr/bin/env python3
"""Finding the real application form when the link on the dashboard is not one.

Half the roles on the radar arrive through LinkedIn, Adzuna or an aggregator, and none of
those links is a form. LinkedIn's own page will not give one up either: the "apply on
company website" URL is behind a login for anyone not signed in, and what is left on the
guest page is Easy Apply, which needs an account and is the last resort anyway.

So this does not chase the link. It goes to the company instead: finds their own job board
through the public APIs Greenhouse, Ashby and Lever publish, and looks for the same role on
it. Nine of the first ten companies tried this way were found from the company name alone,
with no scraping and no key.

The whole difficulty is in the second half, and it is a matching problem with a sharp
downside. Three real cases from the current scan:

  - Okta, "Customer Success Operations Manager, EMEA": one exact title on the board, in
    Dublin. Resolved.
  - Braze, "Senior Customer Success Manager, Industry": FOUR exact titles, in San
    Francisco, New York, Chicago and Austin, and the posting was London. Not resolved --
    a form filled for the wrong city is a wrong application, not a near miss.
  - Vanta, "Revenue Operations Manager, Post Sales (EMEA)": nothing above 0.33 on a board
    of 109 roles, because the role has been taken down. Not resolved, and emphatically not
    "Strategic Channel Manager - EMEA", which was the closest thing to it.

Hence the two rules below. A title has to match nearly exactly, not merely well, and where
several roles share a title the market has to pick exactly one of them. Anything else is
handed back as candidates for Tom to choose between, which costs him one tap and cannot
put a CV in front of the wrong hiring manager.
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import scan
import submit


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

# How close a board title has to be to the posting's title. Word overlap, so 1.0 is the
# same words in any order.
#
# Deliberately strict. At 0.80 Okta's "Customer Success Operations Manager" (Toronto)
# matches a search for "Customer Success Operations Manager, EMEA", and it is a different
# job in a different hemisphere. The cost of being too strict is Tom applying by hand,
# which he was going to do anyway; the cost of being too loose is an application to a role
# he never chose.
TITLE_MATCH_MIN = 0.85
# Seconds per board probe.
PROBE_TIMEOUT = 12
# Company-name suffixes that are never in a board slug.
SUFFIX_RE = re.compile(r"\b(inc|llc|ltd|limited|gmbh|bv|nv|sa|ag|plc|corp|corporation|"
                       r"technologies|technology|software|labs|group|holdings|company|"
                       r"the)\b", re.I)

# Where an application actually lives, as opposed to where a job was advertised, and how
# far that can be known WITHOUT a network call -- is_apply_host(), known_links() and
# application_status() all live in submit.py now, because scan.py needs them too (to show
# which board a role is on, before /submit has ever run) and scan.py cannot import this
# file: this file already imports scan. submit.py imports neither, so it is the one place
# both scan.py and findform.py can reach.
#
# The distinction is the whole problem findform.py exists to solve. Half the radar's rows
# are aggregator links -- LinkedIn, Adzuna, revopsroles, hiring.cafe -- and none of those is
# a form. Worse, the dedupe step picks ONE url per role by source rank, so a role seen on
# both revopsroles and hiring.cafe keeps the revopsroles link and files the real one under
# `also_seen`, where nothing was looking. Triptease's Revenue Operations Manager was
# exactly that: the workable.com application was already in the record, one field away,
# while /submit was reporting there was no form to fill.
is_apply_host = submit.is_apply_host
known_links = submit.known_links


BOARDS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    # Added after measuring what the first three were missing. ServiceNow's three London
    # and Dublin roles were all sitting on the dashboard as "no board found" purely
    # because nothing here had ever asked SmartRecruiters.
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                       "?limit=100",
    "recruitee": "https://{slug}.recruitee.com/api/offers/",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
}
# Where the form actually lives, as opposed to wherever the employer chose to link it.
# Greenhouse's `absolute_url` is often a careers page on the company's own domain with the
# board embedded in it; the board-hosted URL is the same application and is the one with a
# driver behind it.
GREENHOUSE_FORM = "https://job-boards.greenhouse.io/{slug}/jobs/{id}"


def _words(text):
    return [w for w in re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split() if w]


def title_score(a, b):
    """How much two job titles agree, 0 to 1. Word overlap over the longer of the two, so
    a title with extra words in it scores lower rather than free."""
    A, B = set(_words(a)), set(_words(b))
    if not A or not B:
        return 0.0
    return len(A & B) / max(len(A), len(B))


def board_slugs(company):
    """Slugs a company's board is plausibly at, best guess first.

    "Kong Inc." -> kong, "commercetools" -> commercetools, "Cube Software" -> cube. The
    first word alone is last, because it is the guess most likely to land on somebody
    else's board -- and a wrong board is caught by the title match rather than here."""
    n = SUFFIX_RE.sub(" ", re.sub(r"[^\w\s-]", " ", (company or "").lower()))
    words = n.split()
    if not words:
        return []
    out = ["".join(words), "-".join(words)]
    if len(words) > 1:
        out.append(words[0])
    return list(dict.fromkeys(s for s in out if len(s) > 1))


def known_board(company, companies):
    """(ats, slug) from companies.json, which is curated and verified, so it is tried
    before anything is guessed at."""
    want = " ".join(_words(company))
    for c in companies or []:
        if " ".join(_words(c.get("name"))) == want and c.get("slug"):
            return c.get("ats"), c.get("slug")
    return None, None


def load_companies(path="companies.json"):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("companies", [])
    except Exception:
        return []


# ---------------------------------------------------------------- reading a board

def _greenhouse(slug, data):
    for j in data.get("jobs", []):
        yield {"title": j.get("title", ""),
               "location": (j.get("location") or {}).get("name", ""),
               "url": GREENHOUSE_FORM.format(slug=slug, id=j.get("id")),
               "linked_from": j.get("absolute_url", "")}


def _ashby(slug, data):
    for j in data.get("jobs", []):
        yield {"title": j.get("title", ""), "location": j.get("location", ""),
               "url": j.get("jobUrl") or j.get("applyUrl", ""), "linked_from": ""}


def _lever(slug, data):
    for j in data if isinstance(data, list) else []:
        yield {"title": j.get("text", ""),
               "location": (j.get("categories") or {}).get("location", ""),
               "url": j.get("hostedUrl") or j.get("applyUrl", ""), "linked_from": ""}


def _smartrecruiters(slug, data):
    # A slug that does not exist answers 200 with totalFound 0 rather than 404, so an
    # empty board and a wrong guess look identical from the status line alone. board_jobs()
    # below treats "no postings" as "no board" for exactly that reason.
    for j in data.get("content", []):
        ident = ((j.get("company") or {}).get("identifier") or slug)
        loc = j.get("location") or {}
        yield {"title": j.get("name", ""),
               "location": loc.get("fullLocation") or ", ".join(
                   x for x in (loc.get("city"), loc.get("country")) if x),
               "url": f"https://jobs.smartrecruiters.com/{ident}/{j.get('id')}",
               "linked_from": ""}


def _recruitee(slug, data):
    for j in data.get("offers", []):
        yield {"title": j.get("title", ""), "location": j.get("location", ""),
               # careers_apply_url is the form itself, not the advert in front of it.
               "url": j.get("careers_apply_url") or j.get("careers_url", ""),
               "linked_from": j.get("careers_url", "")}


def _workable(slug, data):
    for j in data.get("jobs", []):
        yield {"title": j.get("title", ""),
               "location": ", ".join(x for x in (j.get("city"), j.get("country")) if x),
               # application_url is apply.workable.com/j/<code>/apply -- the real form.
               # It is NOT jobs.workable.com/view/..., which is Workable's own board page
               # with no fields on it at all; see tools/notes/boards.md.
               "url": j.get("application_url") or j.get("url", ""),
               "linked_from": j.get("url", "")}


READERS = {"greenhouse": _greenhouse, "ashby": _ashby, "lever": _lever,
           "smartrecruiters": _smartrecruiters, "recruitee": _recruitee,
           "workable": _workable}


def board_jobs(ats, slug, fetch=None):
    """Everything on one board, or [] when there is no board there.

    Never raises: this runs on a best-effort path, and a board that 404s is the normal
    outcome of a guessed slug rather than an error worth stopping for."""
    if ats not in BOARDS:
        return []
    try:
        r = (fetch or scan.get)(BOARDS[ats].format(slug=slug),
                                headers={"User-Agent": "Mozilla/5.0 (job-radar; personal "
                                                       "use)",
                                         "Accept": "application/json"},
                                # Short, because a miss is the normal outcome of a guessed
                                # slug and there are up to nine of these behind one
                                # /submit. The default 30s would make a wrong guess cost
                                # more than a right one.
                                timeout=PROBE_TIMEOUT)
        if getattr(r, "status_code", 0) != 200:
            return []
        data = r.json()
    except Exception:
        return []
    out = [j for j in READERS[ats](slug, data) if j.get("title") and j.get("url")]
    for j in out:
        j["ats"] = ats
        j["slug"] = slug
    return out


def probe_slug(slug, fetch=None):
    """(ats, jobs) for the first board in BOARDS order that answers for one slug.

    The six probes run together rather than one after another, and the reason is a
    measurement rather than a preference. A typical miss costs 2.8s for all twelve probes,
    which is nothing -- but a company whose board host simply hangs costs PROBE_TIMEOUT six
    times over per slug, and a backfill of 35 companies that should have taken two minutes
    took eleven. A scan runs every fifteen minutes and ends by pushing to main; two
    overlapping ones race on that push. So the tail is the thing worth bounding, and
    concurrency bounds it at one timeout per slug instead of six.

    What does NOT change is the answer. The winner is still the first board in BOARDS
    order that came back with something, not whichever request happened to return first, so
    a company on two boards resolves the same way every time."""
    results = {}
    with ThreadPoolExecutor(max_workers=len(BOARDS)) as pool:
        futures = {pool.submit(board_jobs, ats, slug, fetch): ats for ats in BOARDS}
        for fut in futures:
            try:
                results[futures[fut]] = fut.result()
            except Exception:
                results[futures[fut]] = []
    for ats in BOARDS:
        if results.get(ats):
            return ats, results[ats]
    return "", []


# ---------------------------------------------------------------- the company cache
#
# 281 distinct companies sit behind the 373 rows whose application host nothing knew. The
# expensive half of finding one is guessing the slug -- up to two spellings against six
# board APIs before anything is known -- and the answer is a property of the COMPANY, not
# of the row, so it is worth writing down. A negative is worth writing down too: without
# one, every scan re-probes the same 200 companies that run their own careers stack and
# will never be on a public board.

CACHE_FILE = "board-cache.json"
# How long an answer stands before it is asked again. Companies do migrate boards, and a
# negative that never expires is a company this can never discover; a month is short
# enough to catch a move and long enough that the probes stay rare.
CACHE_TTL_DAYS = 30


def cache_key(company):
    """The company name, normalised the same way board_slugs() normalises it.

    Suffixes stripped, and stripped HERE rather than only in board_slugs(), because the
    cache answers a question about a company and "Acme", "Acme Ltd" and "Acme, Inc." have
    one board between them. Keyed on the raw name, the same lookup is cached three times
    and a company is re-probed the moment one feed spells it differently from another --
    which is the normal case, since these rows arrive from six sources that each write a
    company name their own way."""
    n = SUFFIX_RE.sub(" ", re.sub(r"[^\w\s-]", " ", (company or "").lower()))
    return " ".join(n.split()) or (company or "").strip().lower()


def load_cache(path=CACHE_FILE):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("boards", {})
    except Exception:
        return {}


def save_cache(boards, path=CACHE_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_readme": [
            "Which job board each company posts on, discovered by findform.py and cached",
            "here so a scan does not re-probe six board APIs for every company every run.",
            "An empty `ats` is a real answer: nothing public was found, and it will be",
            f"asked again after {CACHE_TTL_DAYS} days in case they move.",
            "A row records the board found under that company's NAME, which is not proof",
            "of identity: google.recruitee.com resolves, and is a demo account with one",
            "'Senior Marketer (Sample)' posting on it. Nothing applies on the strength of",
            "a row here -- the title still has to match at TITLE_MATCH_MIN, which is what",
            "keeps a wrong board from becoming a wrong application.",
            "Delete a row to force a re-check. Delete the file to re-check everything."],
            "boards": dict(sorted(boards.items()))}, f, indent=1, sort_keys=False)
        f.write("\n")


def cache_fresh(entry, today=None):
    """True when a cached answer is recent enough to trust without asking again."""
    at = (entry or {}).get("at") or ""
    if not at:
        return False
    try:
        seen = datetime.strptime(at[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return (( today or datetime.now(timezone.utc).date()) - seen).days < CACHE_TTL_DAYS


def find_board(company, companies=None, fetch=None, cache=None):
    """(ats, slug, jobs) for a company's own board, or ("", "", []).

    companies.json first because it is verified; then the guesses, greenhouse before ashby
    before lever, which is the order they turn up in this market."""
    ats, slug = known_board(company, companies if companies is not None
                            else load_companies())
    if ats and slug:
        jobs = board_jobs(ats, slug, fetch)
        if jobs:
            return ats, slug, jobs

    # A cached answer skips the slug guessing entirely -- including a cached "nothing
    # here", which is the whole point: most of these companies run their own careers
    # stack and asking six APIs about them again tomorrow finds the same nothing.
    key = cache_key(company)
    hit = (cache or {}).get(key)
    if cache is not None and cache_fresh(hit):
        if not hit.get("ats"):
            return "", "", []
        jobs = board_jobs(hit["ats"], hit["slug"], fetch)
        if jobs:
            return hit["ats"], hit["slug"], jobs
        # It was there and now is not. Fall through and look again rather than trusting a
        # stale answer, and let the re-probe overwrite it.

    for slug in board_slugs(company):
        ats, jobs = probe_slug(slug, fetch)
        if jobs:
            if cache is not None:
                cache[key] = {"ats": ats, "slug": slug, "at": _today()}
            return ats, slug, jobs
    if cache is not None:
        cache[key] = {"ats": "", "slug": "", "at": _today()}
    return "", "", []


# ---------------------------------------------------------------- picking the right one

def same_market(market, location):
    """True when a board's location string lands in the posting's own market.

    market_of() is scan.py's single source of truth for location, so "Dublin, Ireland" and
    "IE-Dublin" agree here for the same reason they agree in the location gate."""
    if not market:
        return False
    return scan.market_of("", location or "") == market


def rank(jobs, title, market=""):
    """(matches, best) -- every board role whose title is close enough, market-first.

    The market filter is applied only when it leaves something, so a board that states no
    locations does not silently rule out the whole company."""
    close = [dict(j, score=title_score(title, j["title"])) for j in jobs]
    close = [j for j in close if j["score"] >= TITLE_MATCH_MIN]
    close.sort(key=lambda j: -j["score"])
    if not close:
        return [], None
    in_market = [j for j in close if same_market(market, j.get("location"))]
    if in_market:
        return in_market, (in_market[0] if len(in_market) == 1 else None)
    # No location matched. One candidate is still one candidate; several are a coin flip,
    # and this does not flip coins.
    return close, (close[0] if len(close) == 1 else None)


def find_form(job, companies=None, fetch=None, fillable=None, cache=None):
    """Where this posting's application form actually is.

    Returns a dict, always, because every outcome here is worth saying out loud:

      found      the one role on the company's board that this posting is, with `url`
      ambiguous  several roles share the title and the market did not separate them
      gone       the board exists and this role is not on it any more
      no-board   no public board found under any spelling of the company name

    `candidates` carries what was seen either way, so a message to Tom can show him what
    it found rather than only what it concluded.

    `fillable` says which URLs have a driver behind them, so that a link already in the
    record can be preferred over one that would have to be looked up."""
    company = job.get("company") or ""
    # Step nought, and free: the record may already carry the real application. Dedupe
    # keeps one url per role by source rank and files the rest under `also_seen`, so the
    # workable.com link for a role advertised on revopsroles is sitting right there.
    for url in known_links(job, fillable):
        if url != (job.get("url") or "") and is_apply_host(url):
            return {"outcome": "known", "company": company, "url": url,
                    "title": job.get("title") or "", "location": job.get("location") or "",
                    "ats": "", "candidates": []}

    ats, slug, jobs = find_board(company, companies, fetch, cache)
    if not jobs:
        return {"outcome": "no-board", "company": company, "candidates": []}
    matches, best = rank(jobs, job.get("title") or "", job.get("market") or "")
    board = {"outcome": "", "company": company, "ats": ats, "slug": slug,
             "board_size": len(jobs),
             "candidates": [{k: j.get(k) for k in ("title", "location", "url", "score")}
                            for j in matches[:5]]}
    if best:
        return dict(board, outcome="found", url=best["url"], title=best["title"],
                    location=best.get("location", ""))
    if matches:
        return dict(board, outcome="ambiguous")
    return dict(board, outcome="gone")


# ---------------------------------------------------------------- resolving in bulk

# How long one scan may spend meeting new employers, in seconds. A belt to the `limit`
# braces, and it exists because `limit` bounds the number of companies rather than the
# time: a company costs 2.8s when every board answers and 12s per slug when one hangs, and
# which of those a given run gets is a property of somebody else's infrastructure. A scan
# fires every fifteen minutes and ends by pushing to main, so two overlapping runs race on
# that push -- and that is a real failure, where "some companies waited until the next
# run" is not one, because the cache means each is only ever paid for once.
LOOKUP_BUDGET_S = 120


def resolve_rows(rows, companies=None, cache=None, fetch=None, limit=None,
                 budget_s=LOOKUP_BUDGET_S):
    """Work out where a batch of postings actually apply, grouped by company.

    Returns {row id: find_form-shaped result}. One board fetch per company rather than one
    per row, because a company's board is a property of the company and half the rows on
    this dashboard share one with another row.

    `limit` caps how many companies are looked up in a single pass, so a scan that meets
    two hundred new employers at once does not spend ten minutes on them: the rest are
    picked up on the next run, and the cache means each one is only ever paid for once.
    Companies with no cached answer go first, since they are the ones that can still
    change a row from "unknown" to a link. `budget_s` stops the pass on the clock as well
    as the count -- pass None to run to the end, which is what a one-off backfill wants
    and a scan does not.
    """
    started = time.monotonic()
    by_company = {}
    for row in rows:
        by_company.setdefault(cache_key(row.get("company")), []).append(row)

    def uncached(key):
        return not cache_fresh((cache or {}).get(key))
    order = sorted(by_company, key=lambda k: (not uncached(k), k))
    if limit is not None:
        order = order[:limit]

    out = {}
    for done, key in enumerate(order):
        if budget_s is not None and time.monotonic() - started > budget_s:
            print(f"  board lookup: {budget_s}s spent after {done} companies; "
                  f"{len(order) - done} left for the next run")
            break
        group = by_company[key]
        company = group[0].get("company") or ""
        try:
            ats, slug, jobs = find_board(company, companies, fetch, cache)
        except Exception as e:
            print(f"  board lookup failed for {company}: {str(e)[:80]}")
            continue
        for row in group:
            rid = row.get("id")
            if not jobs:
                out[rid] = {"outcome": "no-board", "company": company, "candidates": []}
                continue
            matches, best = rank(jobs, row.get("title") or "", row.get("market") or "")
            base = {"company": company, "ats": ats, "slug": slug, "board_size": len(jobs),
                    "candidates": [{k: j.get(k) for k in
                                    ("title", "location", "url", "score")}
                                   for j in matches[:5]]}
            if best:
                out[rid] = dict(base, outcome="found", url=best["url"],
                                title=best["title"], location=best.get("location", ""))
            elif matches:
                out[rid] = dict(base, outcome="ambiguous")
            else:
                out[rid] = dict(base, outcome="gone")
    return out
