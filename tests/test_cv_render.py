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

# A representative role: filled-in skeleton, four employers' worth of bullets, projects.
# Deliberately near the two-page boundary, because that is where the layout bugs live.
BULLETS = {
    "edu-esade": ["Concentration in strategy and analytics. Graduating July 2026."],
    "navex": [
        "Managed a $5.2M ARR portfolio across 22 enterprise accounts, beating net revenue "
        "retention targets three straight years at 108 to 110 percent.",
        "Built account-level renewal risk assessments that fed the regional forecast, "
        "giving sales leadership visibility a quarter ahead of renewal dates.",
        "Designed a QBR format pairing product usage data with recommendations, later "
        "adopted across the CS and AE teams.",
        "Operationalized a three-stage review, comment and publish workflow with Product "
        "and Support that retained $70K ARR.",
    ],
    "lexisnexis-corp": [
        "Owned a $2M ARR portfolio across the full customer journey, from onboarding "
        "through renewal and expansion.",
        "Ran QBRs mapping product usage to customer time savings, surfacing roughly $150K "
        "in annual expansion.",
        "Trained new account managers and cut ramp time 25 percent in a quarter.",
    ],
    "lexisnexis-print": [
        "Held a $1.2M ARR book across retention and cross-sell in print and digital legal "
        "research.",
    ],
    "gtm-health": [
        "Built a Python and Streamlit dashboard analysing funnel conversion and velocity "
        "across the customer lifecycle, with a Claude API advisor layer.",
    ],
    "handoff": [
        "Built a HubSpot and Zapier workflow triggering structured onboarding tasks on "
        "deal close.",
    ],
    "debic": [
        "Designed lead scoring and routing logic plus the CRM data foundation behind a EUR "
        "1.5M demand generation roadmap.",
    ],
    "factorial": [
        "Built a GTM funnel model, sized the sales team and ran CAC payback scenarios. "
        "Only team selected to present to Factorial's VP of CX.",
    ],
}
SUMMARY = ("Revenue operations and customer success operator with 11 years in B2B SaaS, "
           "most recently managing $5.2M ARR at NAVEX. ESADE MBA finishing 2026, with GTM "
           "funnel modelling, CRM architecture and lead routing work behind it. Strongest "
           "where post-sale data meets forecasting: renewal risk, customer health and the "
           "handoffs between sales and CS.")
SKILLS = ("Salesforce | HubSpot | Gainsight | SQL | Revenue Operations | GTM Strategy | "
          "Sales Forecasting")


def filled_base():
    base, _ = cvbuild.load_base()
    # Stand in for the edits Tom makes in the bank's copy, so the render is measured
    # against a realistic page rather than a half-empty one.
    for role, where in zip(base["experience"],
                           ["Remote, United States", "New York, NY", "New York, NY"]):
        role["right"] = where
    return base


def build(track="BUILDER", title="Revenue Operations Manager (m/f/d)", summary=SUMMARY,
          bullets=None, stem="cv-smoke"):
    base = filled_base()
    spec = cvbuild.assemble_spec(base, track, cvbuild.role_title(title, track), summary,
                                 BULLETS if bullets is None else bullets, SKILLS)
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
    ok("the skills line is one line",
       sum(1 for line in text.split("\n") if "Salesforce | HubSpot" in line) == 1)

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
    bad = dict(BULLETS, navex=["Managed a $5.2M ARR portfolio \u2014 across 22 accounts, "
                               "lifting NRR by [X]%."])
    _spec, bad_paths, bad_problems, _w, _f = build(bullets=bad, stem="cv-smoke-bad")
    ok("a placeholder metric on the page is caught",
       any("placeholder" in p for p in bad_problems), "; ".join(bad_problems))
    ok("an em dash is normalised rather than blocking the build forever",
       "\u2014" not in cvbuild.pdf_text(bad_paths["pdf"])
       and not any("banned character" in p for p in bad_problems))

    print("\n=== too much content for two pages ===")
    # The skill's rule: a slight third-page spillover is fixed by trimming a line of bullet
    # text, not by inserting a page break, so it must not block the send. Four pages is a
    # broken build and must.
    fat = {k: v * 3 for k, v in BULLETS.items()}
    _spec, _p, fat_problems, fat_warnings, fat_facts = build(bullets=fat,
                                                             stem="cv-smoke-long")
    ok("a spillover page warns rather than refusing to send",
       fat_facts.get("pages") == 3 and not fat_problems,
       f"pages={fat_facts.get('pages')}; {'; '.join(fat_problems)}")
    ok("and says what to do about it", any("trim" in w for w in fat_warnings),
       "; ".join(fat_warnings))

    fatter = {k: v * 8 for k, v in BULLETS.items()}
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
