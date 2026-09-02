#!/usr/bin/env python3
"""The cover letter: Step 5 of the job-application-workflow, in code.

Same division of labour as cvbuild.py, and for the same reason. A model chooses the WORDS
-- the hook, the paragraphs, the voice. This file chooses everything else: the letterhead,
the date, the recipient block, the subject line, the salutation, the sign-off, the order
the paragraphs print in, and whether the thing fits on one page.

Three rules from Step 5c are enforced here rather than asked for in a prompt, because two
rules shipped broken this week that were only ever asked for in a prompt:

  - **One page.** Measured off the rendered PDF, not estimated from a character count.
    Over the line, the last body paragraph is dropped and it renders again.
  - **No listicles.** The renderer has no bullet in it, and clean_para() strips list
    markers before the text reaches it. A rule the renderer cannot break is a rule.
  - **No em dashes.** Normalised on the way in, exactly as the CV does it.

And the honesty screen, which matters more here than anywhere. A letter is prose, so it
can assert something about Tom in a sentence that reads like nothing in particular, and
unlike a bullet there is no obvious hole left behind when it does. Two screens run:

  1. Every claim the letter makes about Tom's own experience goes through the same
     cvbuild.screen_bullets() the CV uses, against the same sources -- the bank, the
     skeleton, his own answers, and the page that already shipped. A claim that fails
     takes its paragraph with it.
  2. Every number in the finished letter is checked against the sources the sentence
     carrying it is entitled to. A sentence about the company may use the company's own
     numbers; a sentence about Tom may not. The posting is never evidence for what he did.
"""

import os
import re

import cvbuild

HERE = os.path.dirname(os.path.abspath(__file__))
LETTER_JS = os.path.join(HERE, "cv", "build-letter.js")

# The rule from Step 5c, and the only one of these limits that is not negotiable: a cover
# letter is one page. Two is a letter nobody finishes.
MAX_PAGES = 1
# How many times a letter over the limit gets a paragraph dropped before it is called
# broken. Two, matching the summary trim: a letter is an opening, two or three body
# paragraphs and a close, and dropping past two is not a trim any more.
PARA_TRIM_ATTEMPTS = 2

OPENING, BODY, CLOSING = "opening", "body", "closing"
ROLES = (OPENING, BODY, CLOSING)

# Step 5c, verbatim, plus the openers the skill names. Warnings rather than blocks: these
# are not mechanically fixable -- a blocking check would just withhold the letter, and a
# deterministic retry would fail identically -- and Tom can send /cover again in a second.
BANNED_PHRASES = [
    "synergy", "synergies", "deep dive", "touch base", "circle back",
    "move the needle", "spearheaded", "leveraged", "leverage", "leveraging",
    "orchestrated", "in today's rapidly evolving", "rapidly evolving landscape",
    "i am writing to express", "i am writing to apply", "to whom it may concern",
    "excited to apply", "perfect fit", "dream job",
]
# Hedging adverbs. Kept short on purpose: a list long enough to include "just" and "very"
# fires on every letter and stops being read.
HEDGES = ["arguably", "hopefully", "perhaps", "somewhat", "presumably", "possibly",
          "fairly", "quite"]

# A listicle, in every form a model reaches for when it has been told not to write one.
LIST_MARKER_RE = re.compile(r"^\s*(?:[-*•–—]|\d+[.)]|[a-z][.)])\s+", re.I)

# "you", "your", "their" -- the tell that a sentence is about the company rather than
# about Tom. Which one it is decides which sources its numbers are allowed to come from.
COMPANY_REF_RE = re.compile(r"(?<![\w'])(you|your|yours|they|them|their|its)(?![\w'])",
                            re.I)


# ---------------------------------------------------------------- cleaning

def clean_para(text):
    """One paragraph, in house style, whatever shape the model returned it in.

    Line by line, because that is where a listicle hides: a model that has been told not to
    write bullets will return a paragraph whose lines start with dashes. The markers come
    off and the lines are joined into prose, so nothing that reaches the renderer can print
    as a list."""
    lines = [LIST_MARKER_RE.sub("", l) for l in str(text or "").splitlines()]
    return cvbuild.clean_copy(" ".join(l.strip() for l in lines if l.strip()))


def _company_words(company):
    return {w for w in cvbuild._words(company) if w}


def about_company(sentence, company=""):
    """True when a sentence is talking about the company rather than about Tom.

    Second person, third person, or the company's own name. Everything else is treated as a
    claim about Tom, which is the strict direction: the cost of being wrong here is a good
    sentence dropped, which he can see, rather than an invented number shipped, which he
    cannot."""
    if COMPANY_REF_RE.search(sentence or ""):
        return True
    return bool(_company_words(company) & set(cvbuild._words(sentence)))


def sentences(text):
    return [s for s in cvbuild._SENTENCE_END.split((text or "").strip()) if s.strip()]


# ---------------------------------------------------------------- the honesty screen

def screen_claims(paragraphs, own_corpus):
    """(kept, rejected). A paragraph whose claim fails the screen does not print.

    The claims are what the model declared it was asserting about Tom, each one restated as
    a standalone sentence, and they go through the CV's own screen unchanged. Dropping the
    whole paragraph rather than the claim is the only honest option: a paragraph with its
    load-bearing sentence quietly removed still reads as a finished paragraph, which is
    exactly the failure mode this screen exists to prevent."""
    kept, rejected = [], []
    for p in paragraphs:
        claims = [c for c in (p.get("claims") or []) if str(c or "").strip()]
        _ok, bad = cvbuild.screen_bullets(claims, own_corpus)
        if bad:
            rejected.append((p, "; ".join(f"{why}: {text[:80]}" for text, why in bad)))
        else:
            kept.append(p)
    return kept, rejected


def screen_numbers(text, own_corpus, company_corpus, company=""):
    """(text, dropped). Sentence by sentence, each against the sources it is entitled to.

    A sentence about the company may use the company's own numbers -- its funding round,
    its markets, the years in the posting. A sentence about Tom may not: the posting says
    what to look for, never what he did, and a number he cannot source is a question he
    cannot answer in the interview.

    Dropping the sentence rather than the paragraph, because unlike a claim that failed the
    screen above, this is a local fault: one figure that came from nowhere in an otherwise
    sourced paragraph."""
    kept, dropped = [], []
    for s in sentences(text):
        corpus = (own_corpus + "\n" + company_corpus) if about_company(s, company) \
            else own_corpus
        bad = cvbuild.invented_numbers(s, corpus)
        if bad:
            dropped.append((s.strip(), f"numbers not in any source: {', '.join(bad)}"))
        else:
            kept.append(s.strip())
    return " ".join(kept), dropped


def screen(paragraphs, own_corpus, company_corpus, company=""):
    """The whole screen: claims, then numbers, then the shape of what survived.

    Returns (kept, dropped_paragraphs, dropped_sentences, fatal) where `kept` is
    [{role, text}] in printing order and `fatal` is the reason the letter cannot be sent at
    all -- an opening or a closing that failed the screen, or nothing left in the body. A
    letter missing its middle paragraph is a thinner letter; a letter missing its opening is
    not a letter."""
    cleaned = []
    for p in paragraphs or []:
        role = (p.get("role") or BODY).strip().lower()
        cleaned.append({"role": role if role in ROLES else BODY,
                        "text": clean_para(p.get("text")),
                        "claims": p.get("claims") or []})
    cleaned = [p for p in cleaned if p["text"]]

    kept, rejected = screen_claims(cleaned, own_corpus)
    dropped_paragraphs = [(p["text"], why) for p, why in rejected]

    dropped_sentences = []
    survived = []
    for p in kept:
        text, dropped = screen_numbers(p["text"], own_corpus, company_corpus, company)
        dropped_sentences += dropped
        if text.strip():
            survived.append({"role": p["role"], "text": text})
        else:
            dropped_paragraphs.append((p["text"], "every sentence carried an unsourced "
                                                  "number"))

    ordered = ([p for p in survived if p["role"] == OPENING]
               + [p for p in survived if p["role"] == BODY]
               + [p for p in survived if p["role"] == CLOSING])

    fatal = ""
    if not any(p["role"] == OPENING for p in survived):
        fatal = ("the opening paragraph did not survive the honesty screen, and a letter "
                 "without its hook is not a letter")
    elif not any(p["role"] == BODY for p in survived):
        fatal = "nothing was left in the body once the honesty screen had run"
    elif not any(p["role"] == CLOSING for p in survived):
        fatal = "the closing paragraph did not survive the honesty screen"
    return ordered, dropped_paragraphs, dropped_sentences, fatal


def trim_one(paragraphs):
    """The letter, one body paragraph shorter, for when it will not fit on a page.

    The LAST body paragraph, on the same reasoning as the summary's last sentence: the
    opening carries the hook and the close carries the ask, so the last of the middle is
    the least load-bearing thing in the letter. Returns (paragraphs, dropped) and drops
    nothing when only one body paragraph is left."""
    body = [i for i, p in enumerate(paragraphs) if p["role"] == BODY]
    if len(body) < 2:
        return paragraphs, None
    cut = body[-1]
    return paragraphs[:cut] + paragraphs[cut + 1:], paragraphs[cut]["text"]


# ---------------------------------------------------------------- the document

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def letter_date(d):
    """`2 September 2026`, from a date object. Written out, because a letter dated 09/02
    means two different days depending on which side of the Atlantic reads it."""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def recipient_block(job):
    """Who the letter is addressed to, from the posting and nothing else.

    No hiring manager name. Step 6 -- the outreach email, which is where a named person
    would come from -- is out of scope, and a guessed name on a cover letter is worse than
    no name at all."""
    out = [str(job.get("company") or "").strip()]
    loc = str(job.get("location") or "").strip()
    if loc and loc.lower() not in out[0].lower():
        out.append(loc)
    return [x for x in out if x]


def salutation(job):
    company = str(job.get("company") or "").strip()
    return f"Dear {company} Hiring Team," if company else "Dear Hiring Team,"


def subject_line(job, cv_title=""):
    title = str(job.get("title") or "").strip() or str(cv_title or "").strip().title()
    return f"Re: {title}" if title else "Re: Application"


def signature_name(base):
    """The name as it goes under "Sincerely,".

    The CV prints it in caps because that is the header style Tom's real base CVs use. A
    signature in caps reads as shouting, so an all-caps name is title-cased here and
    anything already mixed-case is left exactly as he wrote it."""
    name = (base.get("name") or "").strip()
    return name.title() if name and name == name.upper() else name


def assemble_spec(base, job, paragraphs, date, cv_title=""):
    """The doc spec handed to build-letter.js.

    Everything except the paragraphs is decided here. The letterhead is lifted straight off
    the CV skeleton, so the name, the contact line and the LinkedIn hyperlink are the same
    on both documents by construction rather than by being kept in step by hand."""
    return {
        "theme": base.get("theme") or {},
        "name": base.get("name") or "",
        "contact": base.get("contact") or [],
        "contact_separator": base.get("contact_separator") or "  |  ",
        "date": letter_date(date),
        "recipient": recipient_block(job),
        "subject": subject_line(job, cv_title),
        "salutation": salutation(job),
        "paragraphs": [p["text"] for p in paragraphs if p.get("text")],
        "closing": "Sincerely,",
        "signature": signature_name(base),
    }


def letter_text(spec):
    """The letter as plain text, for the packet.

    Half the application forms Tom will meet want the letter pasted into a textarea, not
    uploaded, and the PDF is no use for that."""
    L = [spec.get("date", ""), ""]
    L += [r for r in (spec.get("recipient") or [])]
    L += ["", spec.get("subject", ""), "", spec.get("salutation", ""), ""]
    for p in spec.get("paragraphs") or []:
        L += [p, ""]
    L += [spec.get("closing", ""), spec.get("signature", "")]
    return "\n".join(L).strip() + "\n"


def render(spec, outdir, stem):
    return cvbuild.render(spec, outdir, stem, build_js=LETTER_JS)


# ---------------------------------------------------------------- verification

def verify(paths, spec):
    """(problems, warnings, facts). Problems block the send; warnings ride along with it.

    Measured off the rendered PDF, like the CV's. The one that matters is the page count:
    it is the whole reason this runs at all, because a letter that runs to two pages looks
    completely fine to the code that produced it."""
    problems, warnings, facts = [], [], {}
    pdf, docx = paths["pdf"], paths["docx"]

    pages = cvbuild.pdf_pages(pdf)
    facts["pages"] = pages
    if pages == 0:
        problems.append("could not read a page count out of the PDF")
    elif pages > MAX_PAGES:
        problems.append(f"{pages} pages; a cover letter is one")

    text = cvbuild.pdf_text(pdf)
    facts["chars"] = len(text)
    facts["paragraphs"] = len(spec.get("paragraphs") or [])
    facts["words"] = len(text.split())

    if spec.get("name") and spec["name"].split()[0] not in text:
        problems.append("the name is not on the rendered page")
    for field in ("date", "salutation", "signature"):
        value = (spec.get(field) or "").strip()
        if value and value.split()[0] not in text:
            warnings.append(f"the {field} is not on the rendered page")

    flat = re.sub(r"\s+", " ", text)
    missing = [p for p in (spec.get("paragraphs") or [])
               if re.sub(r"\s+", " ", p)[:40] not in flat]
    if missing:
        problems.append(f"{len(missing)} paragraph(s) never reached the page, first: "
                        f"{missing[0][:60]!r}")

    fonts = cvbuild.pdf_fonts(pdf)
    facts["fonts"] = ", ".join(sorted(set(fonts))) or "(none reported)"
    if fonts and not any(f.lower().startswith(("calibri", "carlito")) for f in fonts):
        problems.append(f"rendered in {facts['fonts']}, not Calibri/Carlito")

    if not cvbuild.docx_has_link(docx, "linkedin.com"):
        warnings.append("no LinkedIn hyperlink in the letterhead")

    lower = text.lower()
    for bad in cvbuild.BANNED_SUBSTRINGS:
        if bad in text:
            problems.append(f"banned character on the page: {bad!r}")
    hits = [w for w in BANNED_PHRASES if w in lower]
    if hits:
        warnings.append("Step 5c banned wording on the page: " + ", ".join(hits))
    hedges = [w for w in HEDGES if re.search(rf"\b{re.escape(w)}\b", lower)]
    if hedges:
        warnings.append("hedging adverbs on the page: " + ", ".join(hedges))
    listy = [l for l in text.splitlines() if LIST_MARKER_RE.match(l)]
    if listy:
        problems.append(f"the letter printed as a list: {listy[0].strip()[:60]!r}")
    ph = cvbuild.PLACEHOLDER_RE.findall(text)
    if ph:
        problems.append("placeholder text on the page: " + ", ".join(sorted(set(ph))[:4]))

    if not paths.get("jpegs"):
        warnings.append("no page image was rendered, so nothing was actually looked at")
    return problems, warnings, facts


def outline(spec):
    """The letter's shape without its words. The Actions log is public and this letter is
    not: the log gets the structure, and the page goes to Tom's phone."""
    L = [f"date: {spec.get('date')}",
         f"to: {' / '.join(spec.get('recipient') or [])}",
         f"subject: {spec.get('subject')}",
         f"salutation: {spec.get('salutation')}"]
    for i, p in enumerate(spec.get("paragraphs") or [], 1):
        L.append(f"  paragraph {i}: {len(p)} chars, {len(sentences(p))} sentences")
    L.append(f"closing: {spec.get('closing')}")
    return "\n".join(L)


def log_render(paths, problems, warnings, facts, spec=None, verbose=False):
    cvbuild.log_render(paths, problems, warnings, facts, spec=spec, verbose=verbose,
                       label="cover letter", outline_fn=outline)
