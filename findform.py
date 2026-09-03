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

import scan
import submit

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


READERS = {"greenhouse": _greenhouse, "ashby": _ashby, "lever": _lever}


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


def find_board(company, companies=None, fetch=None):
    """(ats, slug, jobs) for a company's own board, or ("", "", []).

    companies.json first because it is verified; then the guesses, greenhouse before ashby
    before lever, which is the order they turn up in this market."""
    ats, slug = known_board(company, companies if companies is not None
                            else load_companies())
    if ats and slug:
        jobs = board_jobs(ats, slug, fetch)
        if jobs:
            return ats, slug, jobs
    for slug in board_slugs(company):
        for ats in BOARDS:
            jobs = board_jobs(ats, slug, fetch)
            if jobs:
                return ats, slug, jobs
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


def find_form(job, companies=None, fetch=None, fillable=None):
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

    ats, slug, jobs = find_board(company, companies, fetch)
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
