#!/usr/bin/env python3
"""
sponsors.py - load UK and NL visa-sponsor registers and match company names.

UK: gov.uk publishes a daily CSV of all licensed Worker sponsors (legal names +
    rating). We scrape the publication page for the current CSV link and load it.
NL: IND publishes a monthly register of recognised sponsors. Harder to parse
    reliably, so it's best-effort + a manual override file (nl_sponsors_extra.txt).

Matching is fuzzy because registers use legal names ("Adyen N.V.") while job
posts use trading names ("Adyen"). We return one of:
  on_register  - exact normalized match
  likely       - token-subset match (name variant)
  not_found    - no match (real signal, but could be a legal/trading mismatch)
  unknown      - register failed to load this run
  n/a          - job is outside UK/NL
"""

import csv
import io
import re
import os
import requests

UA = {"User-Agent": "Mozilla/5.0 (sponsor-check; personal use)"}

# legal-entity suffixes and filler words to strip before matching
_STRIP = {
    "ltd", "limited", "plc", "llp", "llc", "inc", "incorporated", "corp",
    "corporation", "co", "company", "group", "holding", "holdings", "bv", "nv",
    "gmbh", "ag", "sa", "sas", "srl", "oy", "ab", "as", "ltda", "the",
    "uk", "europe", "emea", "international", "global", "services", "solutions",
}

def normalize(name):
    n = (name or "").lower()
    n = n.replace("&", " and ").replace(".", " ").replace(",", " ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    toks = [t for t in n.split() if len(t) > 1 and t not in _STRIP]
    return toks

def _key(name):
    return " ".join(normalize(name))

class Register:
    def __init__(self, label):
        self.label = label
        self.ok = False
        self.note = "not loaded"
        self._exact = set()          # normalized full-name keys
        self._token_index = {}       # distinctive token -> list of token-sets

    def add(self, name, meta=""):
        toks = normalize(name)
        if not toks:
            return
        self._exact.add(" ".join(toks))
        tset = frozenset(toks)
        for t in toks:
            if len(t) >= 4:
                self._token_index.setdefault(t, []).append(tset)

    def match(self, company):
        toks = normalize(company)
        if not toks:
            return "not_found"
        key = " ".join(toks)
        if key in self._exact:
            return "on_register"
        cset = set(toks)
        distinctive = [t for t in toks if len(t) >= 4]
        for t in distinctive:
            for tset in self._token_index.get(t, []):
                # job company's tokens are a subset of a register entry's tokens
                if cset <= set(tset):
                    return "likely"
        return "not_found"

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

def load_nl():
    """Best effort. IND register is monthly and awkward to parse; we try the
    public page and merge a manual override file. Degrades to 'unknown'."""
    reg = Register("NL")
    names = set()
    # manual override: one company name per line
    if os.path.exists("nl_sponsors_extra.txt"):
        with open("nl_sponsors_extra.txt", encoding="utf-8") as f:
            names.update(l.strip() for l in f if l.strip() and not l.startswith("#"))
    try:
        r = requests.get(
            "https://ind.nl/en/public-register-recognised-sponsors/public-register-work",
            headers=UA, timeout=45,
        )
        r.raise_for_status()
        # the register renders org names in table cells / list items; grab plausible
        # company-looking strings. This is deliberately loose and may under-match.
        for m in re.finditer(r'>\s*([A-Z][^<>\n]{2,80}?(?:B\.?V\.?|N\.?V\.?|Holding|Group|GmbH|Ltd)?)\s*<', r.text):
            cand = m.group(1).strip()
            if len(cand.split()) <= 8 and any(c.isalpha() for c in cand):
                names.add(cand)
    except Exception as e:
        reg.note = f"IND fetch failed: {e}"
    for n in names:
        reg.add(n)
    if names:
        reg.ok = True
        src = "IND + override" if os.path.exists("nl_sponsors_extra.txt") else "IND"
        reg.note = f"{len(names)} names ({src})"
    elif reg.note == "not loaded":
        reg.note = "no names parsed"
    return reg

# map job country/location -> which register applies
def which_register(location, country_code=""):
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
        "not_found": f"not on {reg_label} register",
        "unknown": f"{reg_label} register unavailable",
    }.get(raw, "")
