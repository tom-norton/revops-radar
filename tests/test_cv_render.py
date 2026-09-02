#!/usr/bin/env python3
"""The render, for real: spec -> .docx -> LibreOffice -> PDF -> JPEG -> measured.

    python tests/test_cv_render.py            skips if the toolchain isn't installed
    python tests/test_cv_render.py --install   installs it first (this is what CI does)

Why this is a separate file from test_cv.py: it needs LibreOffice, poppler and a Calibri
metric-compatible font, which is two minutes of apt. test_cv.py runs on every 15-minute
tick and must stay instant. This runs on push, in the CV smoke workflow, which is where a
change to build-cv.js gets caught.

Why it exists at all: "a silently broken tab stop produces a CV with dates in the wrong
place and nobody notices until it's been sent to eight companies." Everything here is
measured off the rendered PDF -- glyph positions, page count, embedded fonts -- because
reading the spec back only proves the spec was written down, which was never in doubt.
The page images it leaves behind go up as a run artifact, so the last check is a person
looking at it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cvbuild  # noqa: E402

OUT = os.environ.get("APPLYQ_CV_OUT", "cv-out")

# The base CV's own content, rendered as-is. Not invented sample text: the point of this
# test is that the renderer reproduces Tom's real page, so any drift shows up as a
# difference from a document he already has on disk.
BASE = cvbuild.load_base()[0]
SUMMARY = ("MBA candidate moving into revenue operations after 11 years in enterprise B2B "
           "SaaS. Built the funnel model, team sizing, CAC, and payback scenarios behind a "
           "live market-entry case for a $100M ARR HR-tech company, presented to their VP "
           "of CX. Ran a monthly six-month renewal risk forecast across $5.2M ARR, briefing "
           "CS and sales leadership on exposure.")


def all_base_bullets():
    """{entry_id: [bullets]} straight off the skeleton, as the tailoring pass would hand
    them over. Used to build the oversized cases below."""
    out = {}
    for _entry, role in cvbuild.all_roles(BASE):
        if role.get("id"):
            out[role["id"]] = list(role.get("bullets") or [])
    return out


def build(track="BUILDER", title="Revenue Operations Manager (m/f/d)", summary=SUMMARY,
          bullets=None, stem="cv-smoke", skills=None):
    spec = cvbuild.assemble_spec(BASE, track, cvbuild.role_title(title, track), summary,
                                 bullets or {}, skills)
    paths = cvbuild.render(spec, OUT, stem)
    problems, warnings, facts = cvbuild.verify(paths, spec)
    # Verbose here, and only here: this renders invented content from this file, not
    # anything out of the private bank.
    cvbuild.log_render(paths, problems, warnings, facts, spec=spec, verbose=True)
    return spec, paths, problems, warnings, facts


def main():
    if "--install" in sys.argv:
        cvbuild.ensure_toolchain()
    for binary in ("node", "soffice", "pdftoppm", "pdftotext"):
        if not cvbuild._have(binary):
            print(f"SKIP: {binary} is not installed. Run with --install, or "
                  f"`cd cv && npm install` plus apt libreoffice-writer poppler-utils "
                  f"fonts-crosextra-carlito.")
            return 0
    if not os.path.isdir(os.path.join(cvbuild.NODE_DIR, "node_modules", "docx")):
        print("SKIP: cv/node_modules is missing. Run with --install.")
        return 0

    fails = []

    def ok(name, cond, detail=""):
        print(("  pass  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    print("\n=== a realistic CV, BUILDER track ===")
    spec, paths, problems, warnings, facts = build()
    ok("renders with no problems", not problems, "; ".join(problems))
    ok("lands on two pages", facts.get("pages") == 2, f"pages={facts.get('pages')}")
    ok("every date and location is on the right margin",
       facts.get("right_aligned", "").split("/")[0]
       == facts.get("right_aligned", "/x").split("/")[1], facts.get("right_aligned"))
    ok("uses Calibri or its metric-compatible stand-in",
       any(f.lower().startswith(("calibri", "carlito"))
           for f in cvbuild.pdf_fonts(paths["pdf"])), facts.get("fonts"))
    ok("the LinkedIn line is a real hyperlink",
       cvbuild.docx_has_link(paths["docx"], "linkedin.com/in/tom-p-norton"))
    ok("page images were produced to look at", len(paths["jpegs"]) == facts.get("pages"),
       ", ".join(os.path.basename(j) for j in paths["jpegs"]))

    text = cvbuild.pdf_text(paths["pdf"])
    ok("the role title came off the posting", "REVENUE OPERATIONS MANAGER" in text)
    ok("projects sit above experience on a RevOps track",
       text.index("REVOPS & GTM PROJECTS") < text.index("PROFESSIONAL EXPERIENCE"))
    ok("the skills line is the track's standing one, on one line",
       sum(1 for line in text.split("\n") if "Python | HubSpot | Zapier" in line) == 1)
    ok("LexisNexis appears once, with both titles under it",
       text.count("LexisNexis") == 1
       and "Account Manager (Corporate Legal)" in text
       and "Account Manager (Print & Digital Solutions)" in text)
    ok("the second degree is on the page", "University of Dayton" in text)
    ok("the contact line keeps its nationality note",
       "Nationality: United States" in text)
    ok("and the seed ships without a phone number, which the poller reports",
       any("phone" in g for g in cvbuild.skeleton_gaps(BASE)))

    print("\n=== the CS track moves the projects section ===")
    _spec, cs_paths, cs_problems, _w, _f = build(track="CS", title="Senior Customer "
                                                 "Success Manager", stem="cv-smoke-cs")
    cs_text = cvbuild.pdf_text(cs_paths["pdf"])
    ok("CS renders clean", not cs_problems, "; ".join(cs_problems))
    ok("projects move below experience and get renamed",
       cs_text.index("PROFESSIONAL EXPERIENCE") < cs_text.index("PROJECTS & OTHER "
                                                               "EXPERIENCE"))
    ok("Debic is omitted on the CS track", "Debic" not in cs_text)

    print("\n=== the checks actually catch a bad page ===")
    # A verification that has never failed on anything is not known to work. The two things
    # most likely to survive a prompt and reach paper are an em dash and a placeholder
    # metric, so both are pushed through on purpose. The em dash should be normalised away
    # before it renders; the placeholder should block the send.
    bad = {"navex": ["Managed a $5.2M ARR portfolio \u2014 across 22 accounts, "
                     "lifting NRR by [X]%."]}
    _spec, bad_paths, bad_problems, _w, _f = build(bullets=bad, stem="cv-smoke-bad")
    ok("a placeholder metric on the page is caught",
       any("placeholder" in p for p in bad_problems), "; ".join(bad_problems))
    ok("an em dash is normalised rather than blocking the build forever",
       "\u2014" not in cvbuild.pdf_text(bad_paths["pdf"])
       and not any("banned character" in p for p in bad_problems))

    print("\n=== Tom's two standing rules, on the page ===")
    # The first real CV shipped with eight bullets on NAVEX and a six-line summary. Both
    # rules were in the prompt and neither was in the code.
    nine = {"navex": [f"Ran a defined renewal process across a $2M ARR corporate "
                      f"portfolio, variation {i}." for i in range(9)]}
    _spec, cap_paths, cap_problems, _w, _f = build(bullets=nine, stem="cv-smoke-cap")
    cap_text = cvbuild.pdf_text(cap_paths["pdf"])
    ok("nine bullets on a job print as six", cap_text.count("variation") == 6,
       f"{cap_text.count('variation')} printed")
    ok("and that is clean, not a blocked build", not cap_problems, "; ".join(cap_problems))

    long_summary = (SUMMARY + " Built the Excel tracking model covering renewal dates, "
                    "ARR at risk, monthly upsell against goal, and progress toward annual "
                    "NRR targets, because the official numbers landed weeks after month "
                    "close.")
    _spec, long_paths, _p, long_warnings, long_facts = build(
        summary=long_summary, stem="cv-smoke-summary")
    ok("an over-long summary is measured, not assumed",
       long_facts.get("summary_lines", 0) > cvbuild.SUMMARY_MAX_LINES,
       f"{long_facts.get('summary_lines')} lines before trimming")
    trimmed = cvbuild.drop_last_sentence(long_summary)
    _spec, _p2, _pr, _w2, fixed = build(summary=trimmed, stem="cv-smoke-summary-fixed")
    ok("dropping its last sentence brings it inside four lines",
       fixed.get("summary_lines", 0) <= cvbuild.SUMMARY_MAX_LINES,
       f"{fixed.get('summary_lines')} lines after")

    print("\n=== too much content for two pages ===")
    # The skill's rule: a slight third-page spillover is fixed by trimming a line of bullet
    # text, not by inserting a page break, so it must not block the send. Four pages is a
    # broken build and must.
    fat = {k: v * 3 for k, v in all_base_bullets().items()}
    _spec, _p, fat_problems, fat_warnings, fat_facts = build(bullets=fat,
                                                             stem="cv-smoke-long")
    ok("a spillover page warns rather than refusing to send",
       fat_facts.get("pages") == 3 and not fat_problems,
       f"pages={fat_facts.get('pages')}; {'; '.join(fat_problems)}")
    ok("and says what to do about it", any("trim" in w for w in fat_warnings),
       "; ".join(fat_warnings))

    # Longer bullets, not more of them: the six-per-job cap now means piling on copies
    # cannot push the page count past three, which is itself the cap working.
    fatter = {k: [(" ".join(all_base_bullets()[k]) + " ") * 3 for _ in range(6)]
              for k in all_base_bullets() if all_base_bullets()[k]}
    _spec, _p, fatter_problems, _w, fatter_facts = build(bullets=fatter,
                                                         stem="cv-smoke-longer")
    ok("a genuinely oversized CV is blocked",
       any("meant to be 2" in p for p in fatter_problems),
       f"pages={fatter_facts.get('pages')}")

    print(f"\n{len(fails)} failed" if fails else "\nall passed")
    print(f"page images left in {os.path.abspath(OUT)} - look at them.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
