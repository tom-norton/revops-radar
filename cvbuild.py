#!/usr/bin/env python3
"""
CV assembly, rendering and verification.

Split out of applyq.py so the two halves of a CV build stay apart:

  - applyq.py decides the WORDS. Which track, which bullets, which summary, what the
    role title says. Those are model calls.
  - this file decides everything else. Section order, project order, the skeleton, the
    render, and the checks that run before the PDF is allowed near a recruiter.

Nothing here calls Claude. That is deliberate: the whole point of `verify()` is to be a
second opinion on what a model produced, and a second opinion from the same kind of thing
that produced it is not one.

The verification exists because the failure mode is silent. A broken tab stop produces a
CV with the dates in the wrong place, and it looks completely fine to the code that wrote
it. So the PDF gets rendered to JPEG and measured -- actual glyph positions out of the
actual PDF, not a belief about what the .docx said.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_SEED = os.path.join(HERE, "cv-base.json")
BASE_FILE = "cv-base.json"          # same name inside the bullet bank, which wins
NODE_DIR = os.path.join(HERE, "cv")
BUILD_JS = os.path.join(NODE_DIR, "build-cv.js")

# The bank's copy is edited here when Tom wants a different phone number, a location on a
# role, or his own bullets in the skeleton. One file, one web form, no terminal.
BASE_EDIT_URL = "https://github.com/tom-norton/tom-bullet-bank/edit/main/cv-base.json"

# ---------------------------------------------------------------- policy from the skill

# Step 4a: used when the JD title is missing, internal jargon, or too vague for a CV.
STANDING_TITLES = {
    "ANALYTICS": "REVENUE OPERATIONS & GTM STRATEGY",
    "BUILDER": "REVENUE OPERATIONS & GTM SYSTEMS",
    "CS": "ENTERPRISE CUSTOMER SUCCESS",
}

# Step 4a.5. Analytics and Builder keep projects above experience so a recruiter meets the
# RevOps work before the CSM title; CS moves them below, because there the CS experience is
# the lead credential and the projects read as a bonus.
PROJECT_ORDER = {
    "ANALYTICS": ["factorial", "debic", "gtm-health"],
    "BUILDER": ["gtm-health", "handoff", "debic", "factorial"],
    "CS": ["handoff", "gtm-health", "factorial"],
}
PROJECTS_TITLE = {
    "ANALYTICS": "REVOPS & GTM PROJECTS",
    "BUILDER": "REVOPS & GTM PROJECTS",
    "CS": "PROJECTS & OTHER EXPERIENCE",
}
EXPERIENCE_TITLE = "PROFESSIONAL EXPERIENCE"
EDUCATION_TITLE = "EDUCATION"

# Page geometry, repeated here only so the checks can be written against numbers rather
# than against build-cv.js. If these two ever disagree, verify() fails, which is the
# behaviour you want from a duplicated constant.
PAGE_WIDTH_PT = 612.0            # US Letter
MARGIN_PT = 54.0                 # 0.75"
RIGHT_EDGE_PT = PAGE_WIDTH_PT - MARGIN_PT
# A right-aligned tab lands the last glyph on the margin. Anything further in than this is
# not a tab stop, it is a space bar.
TAB_TOLERANCE_PT = 8.0

MAX_PAGES = 3                    # 2 is the target; 3 is a trim job, 4 is broken
JPEG_DPI = 90

# anti-ai.md, and Tom's own style rules.
#
# Dashes are FIXED rather than failed. A blocking check on an em dash sounds strict until
# the em dash is in Tom's own bank text: the render is deterministic, so the same build
# fails identically on every retry and he never gets a CV at all. Normalising costs nothing
# and cannot be wrong -- " - " is what the house style wants anyway.
DASH_FIXES = [("\u2014", " - "), ("\u2013 ", " - "), (" \u2013", " - ")]
# Kept as a check anyway, as a backstop on anything the normaliser missed. If this ever
# fires, the normaliser has a hole in it.
BANNED_SUBSTRINGS = ["\u2014"]
# These cannot be normalised -- swapping "robust" for something else changes the claim -- so
# they are a warning on the packet rather than a block on the send.
BANNED_WORDS = ["leverage", "leveraged", "leveraging", "spearheaded", "utilised",
                "utilized", "robust", "seamless", "orchestrated"]
# A placeholder metric that reached the PDF is the worst outcome this build has, so the
# shapes they arrive in are matched directly.
PLACEHOLDER_RE = re.compile(r"\[[^\]]{0,40}\]|\bX+%|\bTBD\b|\bN/A\b", re.I)


# ---------------------------------------------------------------- the skeleton

def load_base(bank=None):
    """The CV skeleton. The bank's copy wins when it exists; the repo's is the seed.

    Returns (base, from_bank)."""
    if bank is not None:
        raw = bank.read(BASE_FILE, "")
        if raw.strip():
            try:
                return json.loads(raw), True
            except json.JSONDecodeError as e:
                # Do not fall back silently: a base that failed to parse means the CV would
                # quietly be built from the seed with none of Tom's edits in it.
                raise RuntimeError(f"{BASE_FILE} in the bullet bank is not valid JSON ({e})")
    with open(BASE_SEED, encoding="utf-8") as f:
        return json.load(f), False


def seed_base(bank):
    """Copy the repo seed into the bank. Done once, on the first CV build."""
    with open(BASE_SEED, encoding="utf-8") as f:
        bank.write(BASE_FILE, f.read())


def entry_ids(base):
    """Every slot a bullet can be attached to, in a stable order."""
    out = [e["id"] for e in base.get("education") or []]
    out += [e["id"] for e in base.get("experience") or []]
    out += list((base.get("projects") or {}).keys())
    return out


def entry_index(base):
    idx = {}
    for e in (base.get("education") or []) + (base.get("experience") or []):
        idx[e["id"]] = e
    for k, v in (base.get("projects") or {}).items():
        idx[v.get("id") or k] = v
    return idx


def is_unedited_seed(base):
    """True when nothing has been filled in yet. Drives a one-off nudge, not a failure --
    a CV with no location on a role is thin, not wrong, and blocking on it would mean the
    first role never produces anything."""
    roles = base.get("experience") or []
    return (not any((r.get("right") or "").strip() for r in roles)
            and not any(r.get("bullets") for r in roles))


# ---------------------------------------------------------------- assembly

def role_title(jd_title, track):
    """Step 4a. The JD's own title, unless it is unusable on a CV."""
    t = " ".join((jd_title or "").split())
    # Strip the noise employers append: "(m/f/d)", "- Amsterdam", "| Remote", req numbers.
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", " ", t)
    t = re.split(r"\s+[-|/–—]\s+", t)[0]
    t = re.sub(r"\b(req|job|vacancy)\s*#?\s*\d+\b", " ", t, flags=re.I)
    t = " ".join(t.split()).strip(" -,|")
    usable = (2 <= len(t) <= 60 and re.search(r"[a-z]", t, re.I)
              and not re.fullmatch(r"[\W\d_]+", t))
    return (t if usable else STANDING_TITLES.get(track, STANDING_TITLES["ANALYTICS"])).upper()


def clean_copy(text):
    """House style, applied rather than requested. Dashes normalised, whitespace collapsed.

    Every prompt in this build asks for no em dashes. This is what makes it true, the same
    way clip() is what makes "keep it short" true for the Telegram messages."""
    out = str(text or "")
    for bad, good in DASH_FIXES:
        out = out.replace(bad, good)
    return " ".join(out.split())


def _entry_spec(entry, bullets):
    return {"left": clean_copy(entry.get("left")), "right": clean_copy(entry.get("right")),
            "sub_left": clean_copy(entry.get("sub_left")),
            "sub_right": clean_copy(entry.get("sub_right")),
            "bullets": [clean_copy(b) for b in (bullets or []) if str(b).strip()]}


def assemble_spec(base, track, title, summary, bullets_by_entry, skills):
    """The doc spec handed to build-cv.js.

    Section placement and project order are decided here from the track, never by the
    model. They are the two things the skill is most specific about and the two a model is
    most likely to quietly reorder."""
    track = (track or "ANALYTICS").upper()
    if track not in PROJECT_ORDER:
        track = "ANALYTICS"
    idx = entry_index(base)
    got = {k: v for k, v in (bullets_by_entry or {}).items()}

    education = {"heading": EDUCATION_TITLE,
                 "entries": [_entry_spec(e, got.get(e["id"]))
                             for e in (base.get("education") or [])]}
    experience = {"heading": EXPERIENCE_TITLE,
                  "entries": [_entry_spec(e, got.get(e["id"]))
                              for e in (base.get("experience") or [])]}
    projects = {"heading": PROJECTS_TITLE[track],
                "entries": [_entry_spec(idx[pid], got.get(pid))
                            for pid in PROJECT_ORDER[track] if pid in idx]}

    sections = ([education, experience, projects] if track == "CS"
                else [education, projects, experience])
    return {
        "theme": base.get("theme") or {"font": "Calibri", "accent": "1F6F78"},
        "name": base.get("name") or "",
        "contact": base.get("contact") or [],
        "role_title": clean_copy(title),
        "summary": clean_copy(summary),
        "sections": sections,
        "skills": clean_copy(skills),
    }


def spec_bullets(spec):
    out = []
    for s in spec.get("sections") or []:
        for e in s.get("entries") or []:
            out += [b for b in (e.get("bullets") or []) if str(b).strip()]
    return out


# ---------------------------------------------------------------- the honesty guards

_WORD_RE = re.compile(r"[a-z0-9]+")
# Numbers as they appear on a CV: 5.2, 22, 108, 150k, 1.5m, 70,000.
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _words(text):
    return [w for w in _WORD_RE.findall((text or "").lower()) if len(w) > 3]


def _numbers(text):
    out = set()
    for raw in _NUM_RE.findall(text or ""):
        n = raw.replace(",", "").rstrip(".")
        if not n:
            continue
        # 5.20 and 5.2 are the same claim.
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n or "0")
    return out


def source_corpus(*parts):
    """Everything a CV bullet is allowed to be made of: the bullet bank, the base CV's own
    bullets, and the new bullets drafted from Tom's interview answers. Nothing else. The
    job posting is deliberately NOT in here -- it says what to look for, never what he
    did."""
    return "\n".join(str(p or "") for p in parts)


def invented_numbers(text, corpus):
    """Numbers in a bullet that appear nowhere in its sources.

    This is the sharpest honesty check available and it is nearly free. A model that
    rewrites "beat the target three years running" into "beat the target by 12%" has
    invented the only part of the sentence a recruiter will ask about."""
    return sorted(_numbers(text) - _numbers(corpus))


def untraceable(text, corpus, threshold=0.5):
    """True when a bullet's wording cannot be traced back to its sources.

    Deliberately the same shape as split_is_sane() in applyq.py, and for the same reason:
    a tailoring pass is allowed to reword and re-angle, not to write new experience. A
    genuine revision keeps most of the original's content words; a fabrication does not."""
    words = _words(text)
    if not words:
        return False
    have = set(_words(corpus))
    return (sum(w in have for w in words) / len(words)) < threshold


def screen_bullets(bullets, corpus):
    """(kept, rejected). Rejected bullets are dropped from the CV rather than fixed: a
    bullet missing from the PDF is visible to Tom the moment he opens it, and an invented
    one is not."""
    kept, rejected = [], []
    for b in bullets:
        b = " ".join(str(b or "").split())
        if not b:
            continue
        bad = invented_numbers(b, corpus)
        if bad:
            rejected.append((b, f"numbers not in any source: {', '.join(bad)}"))
        elif untraceable(b, corpus):
            rejected.append((b, "wording not traceable to the bank, the base CV or Tom's "
                                "answers"))
        else:
            kept.append(b)
    return kept, rejected


# ---------------------------------------------------------------- toolchain

def _have(binary):
    return shutil.which(binary) is not None


def _run(cmd, cwd=None, timeout=600):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({p.returncode}): "
                           f"{(p.stderr or p.stdout or '')[:400]}")
    return p.stdout


def ensure_toolchain(quiet=False):
    """Install what the render needs, the first time a CV is actually built.

    Not a workflow step, on purpose. This runs at most once per role; a workflow step runs
    on all ~96 ticks a day, and two minutes of apt on every relayed message would undo the
    whole point of the webhook."""
    missing = [pkg for binary, pkg in
               (("soffice", "libreoffice-writer"), ("pdftoppm", "poppler-utils"))
               if not _have(binary)]
    # Calibri is not redistributable; Carlito is metric-compatible with it, which is what
    # makes the line breaks and the two-page target hold. Without it LibreOffice falls back
    # to something wider and the layout drifts -- verify() catches that, but installing the
    # font is how you stop it happening.
    if not _font_present("Carlito"):
        missing.append("fonts-crosextra-carlito")
    if missing:
        if not quiet:
            print(f"  installing render toolchain: {', '.join(missing)}")
        sudo = ["sudo"] if os.geteuid() != 0 and _have("sudo") else []
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        subprocess.run(sudo + ["apt-get", "update", "-qq"], env=env, check=False,
                       capture_output=True, text=True, timeout=600)
        _run(sudo + ["apt-get", "install", "-y", "-qq", "--no-install-recommends"] + missing,
             timeout=900)
    if not os.path.isdir(os.path.join(NODE_DIR, "node_modules", "docx")):
        if not quiet:
            print("  installing the docx renderer")
        # `npm ci` when there is a lockfile, which is the reproducible path.
        cmd = (["npm", "ci", "--silent", "--no-audit", "--no-fund"]
               if os.path.exists(os.path.join(NODE_DIR, "package-lock.json"))
               else ["npm", "install", "--silent", "--no-audit", "--no-fund"])
        _run(cmd, cwd=NODE_DIR, timeout=600)


def _font_present(name):
    if not _have("fc-list"):
        return False
    try:
        out = subprocess.run(["fc-list"], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return False
    return name.lower() in (out or "").lower()


# ---------------------------------------------------------------- render

def render(spec, outdir, stem):
    """spec -> .docx -> .pdf -> page JPEGs. Returns dict of paths.

    LibreOffice headless does the PDF because it is the only converter on a runner that
    honours docx tab stops, and pdftoppm does the JPEGs because the only reliable way to
    know a CV looks right is to look at it."""
    os.makedirs(outdir, exist_ok=True)
    spec_path = os.path.join(outdir, f"{stem}.spec.json")
    docx_path = os.path.join(outdir, f"{stem}.docx")
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=1)

    print(_run(["node", BUILD_JS, spec_path, docx_path]).strip())

    # LibreOffice writes into -outdir keeping the basename, and needs a private profile:
    # the default one is shared and a second concurrent run silently does nothing.
    profile = os.path.join(outdir, ".lo-profile")
    _run(["soffice", "--headless", "--norestore",
          f"-env:UserInstallation=file://{profile}",
          "--convert-to", "pdf", "--outdir", outdir, docx_path], timeout=600)
    pdf_path = os.path.join(outdir, f"{stem}.pdf")
    if not os.path.exists(pdf_path):
        raise RuntimeError("LibreOffice produced no PDF")

    jpeg_stem = os.path.join(outdir, f"{stem}-page")
    _run(["pdftoppm", "-jpeg", "-r", str(JPEG_DPI), pdf_path, jpeg_stem])
    return {"spec": spec_path, "docx": docx_path, "pdf": pdf_path,
            "jpegs": sorted(glob.glob(f"{jpeg_stem}*.jpg"))}


# ---------------------------------------------------------------- verification

def pdf_pages(pdf_path):
    out = _run(["pdfinfo", pdf_path]) if _have("pdfinfo") else ""
    m = re.search(r"^Pages:\s+(\d+)", out, re.M)
    return int(m.group(1)) if m else 0


def pdf_text(pdf_path):
    return _run(["pdftotext", "-layout", pdf_path, "-"])


def pdf_fonts(pdf_path):
    if not _have("pdffonts"):
        return []
    out = _run(["pdffonts", pdf_path])
    names = []
    for line in out.splitlines()[2:]:
        name = line.split()[0] if line.split() else ""
        names.append(name.split("+")[-1])
    return names


def pdf_words(pdf_path):
    """[(text, xMin, xMax, page)] straight out of the PDF.

    Glyph positions, not a guess from the .docx. This is what makes the tab-stop check
    mean anything."""
    xml = _run(["pdftotext", "-bbox", pdf_path, "-"])
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    ns = {"h": "http://www.w3.org/1999/xhtml"}
    words = []
    for pno, page in enumerate(root.iter(f"{{{ns['h']}}}page"), 1):
        for w in page.iter(f"{{{ns['h']}}}word"):
            words.append(((w.text or "").strip(), float(w.get("xMin", 0)),
                          float(w.get("xMax", 0)), pno))
    return words


def _norm(word):
    return re.sub(r"[^\w&/.-]", "", (word or "")).lower()


def right_edges(pdf_path, wanted):
    """For each right-hand string on the CV, where it actually ended on the page.

    Matched as a consecutive run of words, not just its last token. Matching one token
    would quietly pass a drifted date whose year happens to appear at the right margin
    somewhere else on the page, which is exactly the failure this check exists to catch.

    Returns [(string, xMax or None)]."""
    words = pdf_words(pdf_path)
    flat = [_norm(t) for t, _a, _b, _p in words]
    out = []
    for value in wanted:
        tokens = [_norm(t) for t in (value or "").split() if _norm(t)]
        if not tokens:
            continue
        hit = None
        for i in range(len(flat) - len(tokens) + 1):
            if flat[i:i + len(tokens)] == tokens:
                hit = max(hit or 0, words[i + len(tokens) - 1][2])
        out.append((value, hit))
    return out


def docx_has_link(docx_path, url_fragment):
    """A hyperlink is a relationship in the .docx, not blue text. Checked as one."""
    with zipfile.ZipFile(docx_path) as z:
        for name in z.namelist():
            if name.endswith(".rels"):
                if url_fragment.lower() in z.read(name).decode("utf-8", "replace").lower():
                    return True
    return False


def verify(paths, spec):
    """(problems, warnings, facts). Problems block the send; warnings ride along with it.

    Everything here is measured off the rendered PDF. A check that reads the spec back
    only proves the spec was written down, which was never in doubt."""
    problems, warnings, facts = [], [], {}
    pdf, docx = paths["pdf"], paths["docx"]

    pages = pdf_pages(pdf)
    facts["pages"] = pages
    if pages == 0:
        problems.append("could not read a page count out of the PDF")
    elif pages > MAX_PAGES:
        problems.append(f"{pages} pages; the CV is meant to be 2")
    elif pages == MAX_PAGES:
        warnings.append(f"{pages} pages; trim a line of bullet text rather than adding a "
                        "page break")
    elif pages < 2:
        warnings.append(f"{pages} page; the target is 2 and a thin CV reads as a thin "
                        "candidate")

    text = pdf_text(pdf)
    facts["chars"] = len(text)
    if spec.get("name") and spec["name"].split()[0] not in text:
        problems.append("the name is not on the rendered page")

    # Tab stops. The check this whole verification exists for.
    wanted = []
    for section in spec.get("sections") or []:
        for e in section.get("entries") or []:
            wanted += [v for v in (e.get("right"), e.get("sub_right")) if (v or "").strip()]
    drifted, unfound = [], []
    for value, xmax in right_edges(pdf, wanted):
        if xmax is None:
            unfound.append(value)
        elif abs(xmax - RIGHT_EDGE_PT) > TAB_TOLERANCE_PT:
            drifted.append(f"{value!r} ends at {xmax:.0f}pt, margin is {RIGHT_EDGE_PT:.0f}pt")
    facts["right_aligned"] = f"{len(wanted) - len(drifted) - len(unfound)}/{len(wanted)}"
    if drifted:
        problems.append("right-aligned tab stop is not holding: " + "; ".join(drifted[:4]))
    if unfound:
        warnings.append("could not find these on the page to measure: "
                        + ", ".join(repr(u) for u in unfound[:4]))

    # Fonts. Carlito is Calibri's metric-compatible stand-in and is the expected result on
    # a Linux runner; anything else means the metrics -- and so the page count -- moved.
    fonts = pdf_fonts(pdf)
    facts["fonts"] = ", ".join(sorted(set(fonts))) or "(none reported)"
    if fonts and not any(f.lower().startswith(("calibri", "carlito")) for f in fonts):
        problems.append(f"rendered in {facts['fonts']}, not Calibri/Carlito")

    if not docx_has_link(docx, "linkedin.com"):
        warnings.append("no LinkedIn hyperlink in the document")

    lower = text.lower()
    for bad in BANNED_SUBSTRINGS:
        if bad in text:
            problems.append(f"banned character on the page: {bad!r}")
    hits = [w for w in BANNED_WORDS if re.search(rf"\b{re.escape(w)}\b", lower)]
    if hits:
        warnings.append("anti-ai.md words on the page: " + ", ".join(hits))
    ph = PLACEHOLDER_RE.findall(text)
    if ph:
        problems.append("placeholder text on the page: " + ", ".join(sorted(set(ph))[:4]))

    # Nothing silently dropped between the spec and the paper.
    flat = re.sub(r"\s+", " ", text)
    missing = [b for b in spec_bullets(spec)
               if re.sub(r"\s+", " ", b)[:40] not in flat]
    if missing:
        problems.append(f"{len(missing)} bullet(s) in the spec never reached the page, "
                        f"first: {missing[0][:60]!r}")

    if not paths.get("jpegs"):
        warnings.append("no page images were rendered, so nothing was actually looked at")
    return problems, warnings, facts


def outline(spec):
    """The CV's shape without its words: headings, entry headers, bullet counts.

    This is what goes in the log by default, because the log belongs to a PUBLIC repo and
    the bullets do not -- they come out of the private bank, which is private for a reason.
    A structural break (a section in the wrong place, an entry with no bullets, a project
    order that moved) is visible here; the sentences are not."""
    lines = [f"title: {spec.get('role_title')}",
             f"summary: {len(spec.get('summary') or '')} chars",
             f"skills: {len((spec.get('skills') or '').split('|'))} items"]
    for section in spec.get("sections") or []:
        lines.append(f"[{section.get('heading')}]")
        for e in section.get("entries") or []:
            head = " / ".join(x for x in [e.get("left"), e.get("sub_left")] if x)
            right = " / ".join(x for x in [e.get("right"), e.get("sub_right")] if x)
            bullets = [b for b in (e.get("bullets") or []) if str(b).strip()]
            lines.append(f"  {head}  ->  {right}"
                         f"  [{len(bullets)} bullets, "
                         f"{sum(len(b) for b in bullets)} chars]")
    return "\n".join(lines)


def log_render(paths, problems, warnings, facts, spec=None, verbose=False):
    """What gets printed into the Actions log.

    `verbose` prints the rendered page text, which is how you diagnose a layout problem and
    also how you publish Tom's CV to a public Actions log. So it is off unless a run is
    started with it on deliberately. The page images are the real eyeball, and they go to
    his phone with the PDF."""
    print("\n--- CV render " + "-" * 50)
    for k, v in facts.items():
        print(f"  {k}: {v}")
    print(f"  images: {', '.join(os.path.basename(j) for j in paths.get('jpegs') or []) or 'none'}")
    for w in warnings:
        print(f"  warn:  {w}")
    for p in problems:
        print(f"  FAIL:  {p}")
    if spec is not None:
        print("--- structure " + "-" * 51)
        print(outline(spec))
    if verbose:
        print("--- page text " + "-" * 51)
        try:
            print(pdf_text(paths["pdf"]))
        except Exception as e:
            print(f"  (could not extract text: {e})")
    else:
        print("  (page text withheld: this log is public. Re-run with cv_debug to see it.)")
    print("-" * 65 + "\n")
