#!/usr/bin/env python3
"""
sponsors.py - load UK and NL visa-sponsor registers and match company names.

UK: gov.uk publishes a daily CSV of all licensed Worker sponsors (legal names +
    rating). We scrape the publication page for the current CSV link and load it.
NL: IND publishes a monthly register of recognised sponsors. Harder to parse
    reliably, so it's best-effort + a manual override file (nl_sponsors_extra.txt).

Registers use legal names ("Adyen N.V.", "IVXS UK Limited") while job posts use trading
names ("Adyen", "ComplyAdvantage"), so a row's status comes from resolve(), which tries
every legal name known for the employer (alias file, the scorer's reading, the ad, the
employer's own website) before the trading name. One of:
  on_register  - an exact match, on a legal name or the trading name
  likely       - the trading name plus corporate filler ("Pleo" -> "Pleo Technologies Ltd")
  possible     - a loose word match on one of a few entries; shown with the entry it hit
  not_found    - no match, after the legal names known were tried
  agency       - a recruiter's post that does not name the employer
  unknown      - register failed to load this run
"""

import csv
import html
import io
import json
import os
import re
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

import requests

UA = {"User-Agent": "Mozilla/5.0 (sponsor-check; personal use)"}

# legal-entity suffixes and filler words to strip before matching
_STRIP = {
    "ltd", "limited", "plc", "llp", "llc", "inc", "incorporated", "corp",
    "corporation", "co", "company", "group", "holding", "holdings", "bv", "nv",
    "gmbh", "ag", "sa", "sas", "srl", "oy", "ab", "as", "ltda", "the",
    "uk", "europe", "emea", "international", "global", "services", "solutions",
    # Added Nov 2026, after measuring 133 "not on register" rows: web suffixes in trading
    # names ("Aircall.io", "Checkout.com", "Thuisbezorgd.nl"), connectors ("Kubota in
    # Europe"), and the country words Dutch subsidiaries carry ("o9 Solutions Netherlands
    # B.V."). Each was the whole difference between a trading name and its register entry.
    "com", "io", "ai", "nl", "app", "hq", "in", "of", "and",
    "netherlands", "nederland", "benelux", "sarl", "se", "aps", "branch",
}
# Words a register entry may carry beyond the trading name and still be the same company
# ("Pleo" -> "Pleo Technologies Limited", "Navan" -> "Navan Labs UK Limited"). Anything else
# is a different business that happens to share a word: "Clay" is not "Clay Oven Dagenham",
# "Hadrian" is not "Hadrian Health Centre". Those were 20 of 93 "likely" rows.
_FILLER = {
    "technology", "technologie", "tech", "software", "system", "lab", "labs",
    "digital", "payment", "distribution", "intelligence", "operation", "development",
    "consulting", "wholesale", "finance", "financial", "bank", "network", "platform",
    "online", "cloud", "information", "business", "compliance", "media", "data",
    "communication", "product", "research", "trading", "ventures", "studio",
}
_TRADE = re.compile(r"\s(?:t/a|t/as|ta|trading as)\s", re.I)


def _stem(t):
    return t[:-1] if len(t) >= 5 and t.endswith("s") else t


def normalize(name):
    n = html.unescape(name or "").lower()
    n = re.sub(r"(?<=\w)-(?=\w)", "", n)          # "V-Silicon" is one word, not "v silicon"
    # "S&P", "SS&C", "AT&T" are one word; split on "&" they were single letters, which are
    # dropped, and "S&P Global" normalised to nothing at all.
    n = re.sub(r"\b(\w{1,3})&(\w{1,3})\b", r"\1\2", n)
    n = n.replace("&", " and ").replace(".", " ").replace(",", " ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return [_stem(t) for t in n.split() if len(t) > 1 and t not in _STRIP]


def brand(name):
    """The trading name a job board shows, without the tagline or parenthetical some feeds
    append: "Orbisk | Zero Food Waste" -> "Orbisk", "Amazon Web Services (AWS)" -> "Amazon
    Web Services"."""
    return re.split(r"\s+[|–—-]\s+|\s*\(", html.unescape(name or ""))[0]


def _key(name):
    return " ".join(normalize(name))


# How an indexed key was derived from its register entry, best first. A lookup takes the
# best kind that hits, so "Checkout" lands on "CHECKOUT LTD" rather than on "CHECKOUT
# (WIMBLEDON) LIMITED", and "Amazon" on "Amazon UK Services Ltd" rather than on a shop
# trading as "The Amazon".
FULL, BRAND, TRADE = 0, 1, 2


def _variants(name):
    """The forms of one register entry worth indexing: the full name, the name before any
    parenthetical, and each trade name after "t/a" ("aventis pharma Ltd t/a Sanofi")."""
    raw = html.unescape(name or "").strip()
    parts = _TRADE.split(raw)
    out = [(parts[0], FULL), (brand(parts[0]), BRAND)]
    out += [(p, TRADE) for p in parts[1:]]
    return out


class Register:
    """A set of sponsor names, plus two independent trust flags.

    ok               - the register loaded well enough to be consulted at all.
    trust_negatives  - it is complete enough that "absent" means "not a sponsor". When a
                       partial register loads (e.g. the NL override list without the IND
                       parse behind it) a hit is still meaningful but a miss is not, so
                       match() returns "unknown" rather than "not_found".
    """

    def __init__(self, label):
        self.label = label
        self.ok = False
        self.trust_negatives = True
        self.note = "not loaded"
        # normalized key (and the same with spaces removed) -> {kind: register names}
        self._exact = {}
        self._concat = {}
        self._token_index = {}       # distinctive token -> list of (token-set, name)

    def add(self, name, meta=""):
        for variant, kind in _variants(name):
            toks = normalize(variant)
            if not toks:
                continue
            for index, key in ((self._exact, " ".join(toks)), (self._concat, "".join(toks))):
                index.setdefault(key, {}).setdefault(kind, set()).add(name)
            if kind == TRADE:
                continue
            tset = frozenset(toks)
            for t in toks:
                if len(t) >= 4:
                    self._token_index.setdefault(t, []).append((tset, name))

    def _hit(self, toks):
        """(kind, register names) for the best-kind exact hit on these tokens, or None."""
        for index, key in ((self._exact, " ".join(toks)), (self._concat, "".join(toks))):
            kinds = index.get(key)
            if kinds:
                kind = min(kinds)
                return kind, kinds[kind]
        return None

    def exact_entry(self, name):
        """The register entry a LEGAL name matches exactly, or None. Used for names found
        elsewhere -- an alias, the ad, the scorer, the employer's website -- where a loose
        match would turn a wrong guess into a confident "sponsor"."""
        for variant in (name, brand(name)):
            toks = normalize(variant)
            hit = toks and self._hit(toks)
            if hit:
                return sorted(hit[1])[0]
        return None

    def lookup(self, company):
        """(status, register entry) for a trading name. Status is one of on_register,
        likely, possible, not_found, unknown."""
        toks = normalize(brand(company)) or normalize(company)
        if not toks:
            return "unknown", None   # nothing left to match on after stripping suffixes
        hit = self._hit(toks)
        if hit:
            kind, names = hit
            # A trade name several unrelated licensees carry (Costa Coffee franchisees) is
            # not a match on the brand's own entity.
            if kind == TRADE and len(names) > 1:
                return "likely", sorted(names)[0]
            return "on_register", sorted(names)[0]
        cset = set(toks)
        strict, loose = {}, {}
        for t in toks:
            if len(t) < 4:
                continue
            for tset, name in self._token_index.get(t, []):
                if cset <= tset:
                    extra = tset - cset
                    if all(x in _FILLER or re.fullmatch(r"(?:19|20)\d\d", x) for x in extra):
                        strict[name] = sorted(tset)
                    loose[name] = sorted(tset)
        if strict and len({normalize(n)[0] for n in strict}) == 1:
            return "likely", sorted(strict)[0]
        # A loose word match is shown, with the entry it matched, so it can be judged at a
        # glance -- but only when it is one of a few. "Factory" against 50 register entries
        # is no evidence of anything.
        if loose and len(loose) <= 3:
            return "possible", sorted(loose)[0]
        return ("not_found" if self.trust_negatives else "unknown"), None

    def match(self, company):
        return self.lookup(company)[0]

def load_uk():
    reg = Register("UK")
    try:
        page = requests.get(
            "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers",
            headers=UA, timeout=45,
        )
        page.raise_for_status()
        m = re.search(r'https://assets\.publishing\.service\.gov\.uk/media/[^"\s]+\.csv', page.text)
        if not m:
            reg.note = "CSV link not found on gov.uk page"
            return reg
        csv_url = m.group(0)
        raw = requests.get(csv_url, headers=UA, timeout=90)
        raw.raise_for_status()
        text = raw.content.decode("utf-8-sig", errors="replace")
        rdr = csv.reader(io.StringIO(text))
        rows = list(rdr)
        if not rows:
            reg.note = "empty CSV"
            return reg
        header = [h.strip().lower() for h in rows[0]]
        # find the organisation-name column (usually first)
        name_i = 0
        for i, h in enumerate(header):
            if "organisation" in h or "name" in h:
                name_i = i
                break
        rating_i = next((i for i, h in enumerate(header) if "rating" in h or "type" in h), None)
        count = 0
        for row in rows[1:]:
            if len(row) <= name_i or not row[name_i].strip():
                continue
            reg.add(row[name_i], row[rating_i] if rating_i is not None and len(row) > rating_i else "")
            count += 1
        reg.ok = count > 0
        reg.note = f"{count} sponsors from {csv_url.rsplit('/', 1)[-1]}"
    except Exception as e:
        reg.note = f"load failed: {e}"
    return reg

# The IND page has to yield at least this many names before we'll treat the register as
# usable. Without the floor, a failed parse left the handful of manual overrides standing in
# for the whole register, and every other Dutch company came back "not on NL register" --
# a confident-looking wrong answer that then cost the role points in the deep score. Below
# the floor we report "unknown" instead, which scoring treats as no signal either way.
NL_MIN_NAMES = 200

def load_nl():
    """Best effort. IND register is monthly and awkward to parse; we try the
    public page and merge a manual override file. Degrades to 'unknown'."""
    reg = Register("NL")
    names = set()
    overrides = set()
    # manual override: one company name per line
    if os.path.exists("nl_sponsors_extra.txt"):
        with open("nl_sponsors_extra.txt", encoding="utf-8") as f:
            overrides.update(l.strip() for l in f if l.strip() and not l.startswith("#"))
        names.update(overrides)
    try:
        r = requests.get(
            "https://ind.nl/en/public-register-recognised-sponsors/public-register-work",
            headers=UA, timeout=45,
        )
        r.raise_for_status()
        # The register is a table of organisation name and KvK number, one row per sponsor
        # ("<th scope="row">Adyen N.V.</th><td>34259528</td>"): 13,085 rows in Nov 2026.
        # Names are HTML-escaped ("Smith &amp; Nephew"), which the old loose scrape kept as
        # a literal "amp" word.
        rows = re.findall(r'<th scope="row">([^<]+)</th>\s*<td>(\d+)</td>', r.text)
        names.update(html.unescape(n).strip() for n, _ in rows)
        if len(rows) < NL_MIN_NAMES:
            # Layout changed: fall back to grabbing company-looking strings. Deliberately
            # loose; the floor below decides whether its misses can be trusted.
            for m in re.finditer(r'>\s*([A-Z][^<>\n]{2,80}?(?:B\.?V\.?|N\.?V\.?|Holding|Group|GmbH|Ltd)?)\s*<', r.text):
                cand = html.unescape(m.group(1)).strip()
                if len(cand.split()) <= 8 and any(c.isalpha() for c in cand):
                    names.add(cand)
    except Exception as e:
        reg.note = f"IND fetch failed: {e}"
    for n in names:
        reg.add(n)
    ind_count = len(names - overrides)
    reg.ok = bool(names)
    reg.trust_negatives = ind_count >= NL_MIN_NAMES
    if reg.trust_negatives:
        reg.note = f"{len(names)} names ({'IND + override' if overrides else 'IND'})"
    elif names:
        # Positive matches from the override list still stand; misses report "unknown".
        reg.note = (f"partial: {ind_count} from IND (need {NL_MIN_NAMES}) + "
                    f"{len(overrides)} override; hits count, misses report unknown")
    elif reg.note == "not loaded":
        reg.note = "no names parsed"
    return reg

# Which register applies. By MARKET, not by location text: the old text match sent
# "London, Ontario, Canada", "Dublin or London" and "Amsterdam or London" rows to the UK
# register (13 rows on the board in Nov 2026) and Canadian and Irish roles came back "not on
# UK register". A row's market is already the one place its location is decided.
MARKET_REGISTER = {"UK-London": "UK", "NL": "NL"}


def which_register(location="", country_code="", market=None):
    if market is not None:
        return MARKET_REGISTER.get(market)
    loc = (location or "").lower()
    if country_code == "gb" or re.search(r"united kingdom|england|scotland|wales|london|manchester|\buk\b", loc):
        return "UK"
    if country_code == "nl" or re.search(r"netherlands|amsterdam|rotterdam|utrecht|holland|eindhoven|hague", loc):
        return "NL"
    return None


def status_label(raw, reg_label):
    return {
        "on_register": f"{reg_label} sponsor",
        "likely": f"{reg_label} sponsor (likely)",
        "possible": f"{reg_label} sponsor (possible)",
        "not_found": f"not on {reg_label} register",
        "agency": f"{reg_label} agency post, employer not named",
        "unknown": f"{reg_label} register unavailable",
    }.get(raw, "")


# Rows whose sponsor result moves them out of the main list into the collapsed "No sponsor
# on register" lane. Tom's call, Nov 2026: UK and NL rows not on the register leave the main
# list and stop notifying -- after the legal-name lookup below, because a trading-name miss
# is not a non-sponsor -- and so do agency posts that do not name the employer.
LANE_RAWS = ("not_found", "agency")


def in_lane(raw, region):
    return region in ("UK", "NL") and raw in LANE_RAWS


# ---------------------------------------------------------------- legal entity names
# Registers list legal entities; job posts carry trading names. Measured on the board in Nov
# 2026, 37 of 66 direct-employer UK "not on register" rows and 7 of 21 Dutch ones were on
# the register under another name: ComplyAdvantage is IVXS UK Limited, Qualtrics is QUL
# Technologies Limited, Tellent is Recruitee B.V. The sources below find that name, cheapest
# first. Every name found elsewhere has to match a register entry EXACTLY to count; a
# wrong guess can only fail to help, never manufacture a sponsor.

_SUFFIX = (r"(?:Ltd\.?|Limited|LIMITED|LTD|plc|PLC|LLP|B\.\s?V\.?|BV|N\.\s?V\.?|Inc\.?|GmbH"
           r"|S\.?A\.?R\.?L\.?|ApS|AB|SE)")
_ENTITY = re.compile(
    r"((?:[A-Z0-9][\w&'.\-]*|&)(?:\s+(?:[A-Z0-9(][\w&'.\-)]*|&|and|of))*?\s+" + _SUFFIX + r")"
    r"(?=[\s,.;:)<]|$)")
# Words that introduce a legal name, on a website footer or in an ad's closing lines.
_ENTITY_CONTEXT = re.compile(
    r"©|&copy;|copyright|operated by|registered (?:in|office|company|number)|trading as"
    r"|t/a|a trading name of|impressum|imprint|kvk", re.I)


def _trim_entity(e):
    """Drop the leading words a regex over page text picks up: years, section numbers and
    headings ("2026 Pineapple Technology Ltd", "1. Introduction Landy Tech Limited")."""
    e = re.sub(r"\s+", " ", e).strip()
    e = re.sub(r"^(?:[\d.()]+\s+)+", "", e)
    return e.lstrip("(.,;: ")


def legal_names_in(text, company="", domain="", limit=8):
    """Legal entity names in a page or an ad that plausibly belong to this employer.

    A page names plenty of other companies (its processors, its cookie vendors, its parent's
    other brands), so a candidate is kept only when it shares a word with the company or
    the domain, or follows a word that introduces the site's own legal name (the copyright
    line, "operated by", an imprint). Without that filter, four of 32 sites tested gave a
    false hit: Wiz -> Google UK, Scrive -> Dun & Bradstreet, ML6 -> Skyhaus, Moss -> Airwallex.
    """
    text = html.unescape(text or "")
    # What counts as "this employer": the head word of its trading name ("Rise" in "Rise
    # Technical Recruitment", not the generic "Recruitment"), the whole name run together,
    # and the domain's stem.
    head = normalize(brand(company))[:1]
    own = set(head) | set(normalize(re.sub(r"\.[a-z.]+$", "", domain or "").split(".")[-1]))
    own_concat = "".join(normalize(brand(company)))
    out = []
    for m in _ENTITY.finditer(text):
        e = _trim_entity(" ".join(m.group(1).split()[-8:]))
        if len(e) <= 3:
            continue
        before = text[max(0, m.start() - 40):m.start()] + " " + e
        ctx = _ENTITY_CONTEXT.search(before)
        if ctx and ctx.end() > len(before) - len(e):
            # the introducing word was captured inside the entity ("Impressum Recruitee B.V.")
            e = _trim_entity(before[ctx.end():])
        # Page text runs headings and sentences into the name ("Moss Credit (Germany)
        # GetMoss NL B.V.", "30th October 2026 EPX Limited"), so every tail of the match that
        # still names this employer is a candidate. A tail that does not is dropped, which is
        # what stops "Moss Credit Airwallex (UK) Limited" from offering "Airwallex (UK)
        # Limited".
        words = e.split()
        tails = [" ".join(words[i:]).lstrip("(.,;: ") for i in range(len(words) - 1)] or [e]
        for i, tail in enumerate(tails):
            if _related(tail, own, own_concat) or (i == 0 and ctx):
                if tail not in out:
                    out.append(tail)
    # Shortest first: "EPX Limited" is the name to show for a miss, not the sentence it
    # was found in.
    return sorted(out, key=len)[:limit]


def _related(name, own, own_concat):
    toks = set(normalize(name))
    return bool(toks & {t for t in own if len(t) >= 3}) or bool(
        own_concat and len(own_concat) >= 3 and own_concat in "".join(normalize(name)))


_LEGAL_LINK = re.compile(r"privacy|legal|terms|imprint|impressum|disclaimer|cookie", re.I)
UA_SITE = {"User-Agent": "Mozilla/5.0 (compatible; revops-radar legal-entity check)"}


def _page_text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h or "")
    return html.unescape(re.sub(r"<[^>]+>", " ", h))


def website_legal_names(domain, company, get=None, max_pages=3):
    """Legal names the employer's own website states, from its home page footer and up to
    three privacy, terms or imprint pages. UK companies must show their registered name on
    their website, and Dutch ones their KvK entity, so this is usually the most reliable
    free source there is. Network errors return [] -- the caller caches the attempt either
    way, so one bad site costs one try."""
    get = get or (lambda u: requests.get(u, headers=UA_SITE, timeout=8))
    base = "https://" + domain.strip("/")
    def fetch(u):
        try:
            r = get(u)
            return r.text if getattr(r, "ok", False) else ""
        except Exception:
            return ""
    home = fetch(base)
    if not home:
        return []
    links = re.findall(r'href=["\']([^"\'#]+)["\']', home)
    legal = [urljoin(base + "/", l) for l in links if _LEGAL_LINK.search(l)]
    legal = [u for u in dict.fromkeys(legal) if urlparse(u).netloc.endswith(domain.split("/")[0].removeprefix("www."))]
    found = legal_names_in(_page_text(home), company, domain)
    for u in legal[:max_pages]:
        for e in legal_names_in(_page_text(fetch(u)), company, domain):
            if e not in found:
                found.append(e)
    return found[:12]


# Hosts that are job boards or ATSs, never the employer's own site.
_NOT_EMPLOYER_HOSTS = re.compile(
    r"linkedin|indeed|adzuna|reed\.co|glassdoor|hiring\.cafe|revopsroles|greenhouse|lever\.co"
    r"|ashbyhq|workable|recruitee|personio|myworkdayjobs|workday|smartrecruiters|teamtailor"
    r"|bamboohr|jobvite|icims|successfactors|taleo|jazzhr|breezy|join\.com|welcometothejungle"
    r"|google|jobs\.|totaljobs|cv-library|monster|ziprecruiter|otta|wellfound|builtin", re.I)


def employer_domain(url):
    """The employer's own domain from a job URL, when the URL is on the employer's site
    ("careers.scrive.com/jobs/..." -> "scrive.com"). None for a job board or an ATS."""
    host = (urlparse(url or "").netloc or "").lower().split(":")[0]
    if not host or _NOT_EMPLOYER_HOSTS.search(host):
        return None
    parts = host.split(".")
    while len(parts) > 2 and parts[0] in ("www", "careers", "jobs", "job", "work", "apply",
                                           "career", "talent", "join"):
        parts = parts[1:]
    return ".".join(parts)


# ---------------------------------------------------------------- agency posts
# A recruiter's post names the agency, not the employer, and the register says nothing
# about who would actually sponsor. 38 of the 133 "not on register" rows were agencies.
_AGENCY_NAME = re.compile(
    r"recruit|staffing|resourcing|headhunt|talent (?:partners|solutions|group)"
    r"|executive search|search (?:partners|solutions|group)|\bpersonnel\b|\bcareers\b"
    r"|\b(?:randstad|michael page|page group|hays|robert half|robert walters|morson|lhh"
    r"|selby jennings|harnham|referment|trust in soda|steadman brown|trevose partners"
    r"|siena partnership|x4 technology|elbrus partners|insight select|executive facilities"
    r"|zero surplus|brewer morris|cbsbutler|bisonrs|staffice|hackajob|aquent|adecco)\b", re.I)
_AGENCY_AD = re.compile(
    # "Our client is a ..." -- not "our client's needs" (Octus), "our client-facing teams"
    # (quantilope) or "our client success team", which direct employers write too.
    r"\b(?:our|my) client(?=[\s,])(?!\s+(?:success|services|facing|base|relationships?"
    r"|teams?|portfolio|experience|needs)\b)|\bon behalf of (?:our|a|an) (?:client|leading)"
    r"|\bwe(?:'re|’re| are) (?:partnering|partnered|working) with (?:a|an)\b"
    r"|\bare (?:partnered|partnering|working) with (?:a|an) (?:leading|established|growing)",
    re.I)


def looks_like_agency(company, description=""):
    return bool(_AGENCY_NAME.search(company or "") or _AGENCY_AD.search(description or ""))


# ---------------------------------------------------------------- aliases and cache
ALIASES_FILE = "sponsor_aliases.json"
ENTITIES_FILE = "entities.json"
ENTITY_RECHECK_DAYS = 90


def load_aliases(path=ALIASES_FILE):
    """{normalized trading name: [legal names]} from the hand-kept alias file."""
    try:
        raw = json.load(open(path, encoding="utf-8")).get("aliases", {})
    except Exception:
        return {}
    return {_key(k): list(v) for k, v in raw.items() if _key(k)}


def load_entities(path=ENTITIES_FILE):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def save_entities(cache, path=ENTITIES_FILE):
    json.dump(dict(sorted(cache.items())), open(path, "w", encoding="utf-8"), indent=1,
              ensure_ascii=False)


def entity_due(cache, company, today=None):
    """True when this company's website has not been checked, or not for ENTITY_RECHECK_DAYS."""
    rec = cache.get(_key(company))
    if not rec:
        return True
    today = today or date.today()
    try:
        return date.fromisoformat(rec.get("checked", "")) < today - timedelta(days=ENTITY_RECHECK_DAYS)
    except ValueError:
        return True


def resolve(reg, company, employer="", agency=False, legal=()):
    """Sponsor status for one row, using every legal name known for the employer.

    legal: (name, via) pairs, best source first -- the alias file, the scorer's reading,
    the ad, the employer's website. Returns a dict:
      raw      on_register / likely / possible / not_found / agency / unknown
      entity   the register entry that matched, when one did
      via      what matched: "name" for the trading name, or the legal name's source
      checked  the legal name(s) tried, so a miss can say what was looked up
    """
    out = {"raw": "unknown", "entity": "", "via": "", "checked": []}
    if reg is None or not reg.ok:
        return out
    for name, via in legal:
        hit = reg.exact_entry(name)
        if hit:
            return {"raw": "on_register", "entity": hit, "via": via, "checked": [name]}
    who = employer or ("" if agency else company)
    checked = [n for n, _ in legal]
    if who:
        st, hit = reg.lookup(who)
        if st in ("on_register", "likely"):
            return {"raw": st, "entity": hit or "", "via": "name", "checked": checked}
        # A loose word match loses to a legal name that was actually looked up and missed.
        if st == "possible" and not checked:
            return {"raw": st, "entity": hit or "", "via": "name", "checked": []}
        if st == "unknown" and not checked:
            return out
    if agency and not employer:
        return {"raw": "agency", "entity": "", "via": "", "checked": checked}
    if not reg.trust_negatives:
        return out
    return {"raw": "not_found", "entity": "", "via": "", "checked": checked}
