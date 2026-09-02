#!/usr/bin/env python3
"""The cover-letter render, for real: spec -> .docx -> LibreOffice -> PDF -> JPEG -> measured.

    python tests/test_letter_render.py            skips if the toolchain isn't installed
    python tests/test_letter_render.py --install   installs it first (this is what CI does)

Separate from test_cover.py for the same reason test_cv_render.py is separate from
test_cv.py: this needs LibreOffice, poppler and a Calibri metric-compatible font, which is
two minutes of apt, and test_cover.py runs on every 15-minute tick.

Why it exists at all: the one-page rule is the whole point of Step 5c and it is enforced off
the rendered page count. A stub that reports one page proves nothing about the letter, and
the failure it is guarding against is silent -- a letter that spills onto a second page
looks perfectly fine to the code that produced it. So a deliberately over-long letter is
rendered here and the trim is watched doing its job on a real page count.
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coverletter  # noqa: E402
import cvbuild  # noqa: E402

OUT = os.environ.get("APPLYQ_CV_OUT", "cv-out")
BASE = cvbuild.load_base()[0]

JOB = {"title": "Revenue Operations Manager", "company": "Northwind Tax",
       "location": "Amsterdam, Netherlands", "market": "NL"}

# A letter of the shape the writer is asked for: a hook, two body paragraphs and a close.
# The content is invented for this file, so it is safe for a public Actions log -- nothing
# here comes out of the private bank.
LETTER = [
    {"role": "opening",
     "text": "Expanding indirect tax coverage into six new markets is a data problem "
             "before it is a tax problem, and it is the one I have spent the last four "
             "years inside. Your June engineering post on rebuilding the rates pipeline "
             "read like the projects I have run."},
    {"role": "body",
     "text": "At NAVEX I managed a 5.2M ARR book across 22 enterprise accounts and ran a "
             "rolling six-month renewal risk forecast that CS and sales leadership used to "
             "decide where to spend their quarter. The work was less about the model than "
             "about getting three teams to agree on what a number meant."},
    {"role": "body",
     "text": "The MBA has been a deliberate pivot rather than a pause. I built the funnel "
             "model, team sizing and payback scenarios behind a live market-entry case for "
             "a 100M ARR HR-tech company and presented it to their VP of CX, which is the "
             "closest thing to this role I could have engineered for myself."},
    {"role": "closing",
     "text": "I would welcome the chance to talk about where the rates pipeline goes next "
             "and what the first ninety days would need to look like."},
]

# The same letter with two extra fat paragraphs, to push it past a page on purpose.
FILLER = ("Beyond the systems work, the part of revenue operations I keep coming back to "
          "is the negotiation: a forecast is only useful once the people it describes "
          "believe it, and that belief is won in the review meeting rather than in the "
          "warehouse. I have spent a lot of time in those meetings and I am comfortable "
          "being the person who says the number is wrong and here is why, without making "
          "an enemy of anyone in the room. That is the habit I would bring first.")


def build(paragraphs, stem):
    spec = coverletter.assemble_spec(BASE, JOB, paragraphs, date(2026, 9, 2),
                                     "REVENUE OPERATIONS MANAGER")
    paths = coverletter.render(spec, OUT, stem)
    problems, warnings, facts = coverletter.verify(paths, spec)
    # Verbose here, and only here: this renders invented content from this file.
    coverletter.log_render(paths, problems, warnings, facts, spec=spec, verbose=True)
    return spec, paths, problems, warnings, facts


def main():
    if "--install" in sys.argv:
        cvbuild.ensure_toolchain()
    for binary in ("node", "soffice", "pdftoppm", "pdftotext"):
        if not cvbuild._have(binary):
            print(f"SKIP: {binary} is not installed. Run with --install.")
            return 0
    if not os.path.isdir(os.path.join(cvbuild.NODE_DIR, "node_modules", "docx")):
        print("SKIP: cv/node_modules is missing. Run with --install, or `cd cv && npm ci`.")
        return 0

    fails = []

    def ok(name, cond, detail=""):
        print(("  pass  " if cond else "  FAIL  ") + name + (f": {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    # ---- the letter as it is meant to be
    spec, paths, problems, warnings, facts = build(LETTER, "letter-smoke")
    ok("a four-paragraph letter renders", bool(paths.get("pdf")))
    ok("it is one page", facts.get("pages") == 1, f"{facts.get('pages')} pages")
    ok("it passed its checks", not problems, "; ".join(problems))
    ok("a page image was produced", bool(paths.get("jpegs")))
    text = cvbuild.pdf_text(paths["pdf"])
    ok("the letterhead is on it", "TOM NORTON" in text.upper())
    ok("the date is written out", "2 September 2026" in text)
    ok("it is addressed to the company", "Northwind Tax" in text)
    ok("there is a subject line", "Re: Revenue Operations Manager" in text)
    ok("it is signed", "Sincerely," in text and "Tom Norton" in text)
    ok("nothing printed as a list",
       not [l for l in text.splitlines() if coverletter.LIST_MARKER_RE.match(l)])
    ok("the LinkedIn hyperlink survived", cvbuild.docx_has_link(paths["docx"], "linkedin.com"))
    ok("it rendered in Calibri or Carlito",
       any(f.lower().startswith(("calibri", "carlito")) for f in cvbuild.pdf_fonts(paths["pdf"])))

    # ---- a letter that does not fit, and the trim that makes it fit
    # Five filler paragraphs, because a page holds more than it looks like it does: the
    # four-paragraph letter above uses about half of one. The point of this case is a real
    # page count of two, and it took this much text to get one.
    long_letter = (LETTER[:3]
                   + [{"role": "body", "text": FILLER.replace("negotiation", w)}
                      for w in ("negotiation", "handover", "forecast", "hiring plan",
                                "renewal review")]
                   + [LETTER[3]])
    _s, _p, long_problems, _w, long_facts = build(long_letter, "letter-smoke-long")
    ok("an over-long letter is caught, not shipped",
       long_facts.get("pages", 0) > 1 and any("one" in p for p in long_problems),
       f"{long_facts.get('pages')} pages, problems={long_problems}")

    # The trim, twice, which is what build_and_ship_cover() does before it gives up.
    trimmed, cut = coverletter.trim_one(long_letter)
    ok("the trim takes a body paragraph", cut is not None)
    trimmed, cut2 = coverletter.trim_one(trimmed)
    ok("the trim takes a second one", cut2 is not None)
    trimmed = coverletter.trim_one(coverletter.trim_one(trimmed)[0])[0]
    _s, _p, trim_problems, _w, trim_facts = build(trimmed, "letter-smoke-trimmed")
    ok("the trimmed letter fits on a page", trim_facts.get("pages") == 1,
       f"{trim_facts.get('pages')} pages")
    ok("and passes its checks", not trim_problems, "; ".join(trim_problems))
    ok("the opening and the close survived the trim",
       [p["role"] for p in trimmed][0] == "opening"
       and [p["role"] for p in trimmed][-1] == "closing")

    print(f"\n{len(fails)} failed" if fails else "\nall passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
