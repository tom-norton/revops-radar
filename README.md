# RevOps Radar

Finds new RevOps / GTM / Sales Ops / CS Ops / Senior CSM roles across your four target
markets, cheaply screens out the obvious no-fits, deep-scores the survivors against your
real profile with Claude, checks each UK/NL company against the official visa-sponsor
registers, and shows you the good ones on a dashboard. Hide/Applied state syncs across
your devices via Firebase.

**Markets:** Netherlands (anywhere), Belgium (anywhere), UK (London area only), Ireland
(Dublin). Germany, Spain, and remote-from-anywhere/EMEA roles are deliberately excluded.

**Runs:** 06:00, 13:00, 19:00 UTC on weekdays, 06:00 UTC only on weekends (~8am/3pm/9pm
and ~8am Barcelona respectively) — see `.github/workflows/scan.yml`.

## How it works

**Data layer (several sources so no single one can break the run):**
1. **Adzuna API** — Netherlands + UK. (Adzuna's API has no Ireland coverage, hence the others.)
2. **Reed API** — extra UK/London depth (free key).
3. **JobSpy / Indeed** — Dublin coverage, the Adzuna gap.
4. **Company ATS feeds** — Greenhouse / Lever / Ashby boards for ~19 named SaaS companies
   that hire in NL/Belgium/London/Dublin (`companies.json`). Clean company names, full
   descriptions, and this is a big part of the Ireland coverage since many US firms hire
   in Dublin this way. Optional — the rest of the pipeline works without it; it exists to
   guarantee coverage of specific companies Tom wants watched regardless of whether they
   show up via the other sources.
5. **hiring.cafe** — via the Apify actor `memo23/apify-hiring-cafe-scraper`, run against
   Tom's saved hiring.cafe searches (the direct API blocks datacenter IPs).
6. **LinkedIn** — mirrors "Jobs based on your preferences" via LinkedIn's public,
   unauthenticated guest job-search endpoint. No login or session cookie.
7. **revopsroles.com** — per-country location pages (robots.txt allows these plain
   paths but disallows the `?location_country=` query-string filter form).

**Filtering:**
8. A free **title + location filter** drops anything off-function or off-market before a token is spent.
9. An **age filter** drops anything older than 7 days when the source gives a posted date.
10. **Cross-source, cross-run dedupe** collapses the same role found via multiple sources
    or on different days into one dashboard entry.
11. Every UK/NL company is matched against the **official sponsor registers** (gov.uk daily
    CSV, IND monthly register) and badged: sponsor / sponsor (likely) / not on register / n/a.

**Scoring (two stages, so the expensive model only sees real candidates):**
12. **Stage 1 — Claude Haiku** cheaply screens each survivor (keep / kill).
13. **Stage 2 — Claude Sonnet** deep-scores the keepers against your full weighted rubric in
    `profile.md` (Experience 25% / Skills 20% / Seniority 15% / Domain 15% / Location+Visa 15% /
    Trajectory 10%), applies deterministic caps (including a cap for postings that hard-require
    a non-English language), and returns per-dimension scores + a verdict.
14. Results commit to `docs/jobs.json`; the dashboard shows **6.0+ to apply**, tucks
    **5.0–5.9 into a collapsed "borderline"** section, and hides everything below 5.

## Your profile lives in `profile.md`

The deep score reads `profile.md`. Edit that file whenever your background, targets, comp
floors, or market list change — no code changes needed. Keep it factual.

## Setup / secrets

Repository secrets (Settings → Secrets and variables → Actions):
- `ANTHROPIC_API_KEY` — your Claude key (already set)
- `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` — free, https://developer.adzuna.com/ (already set)
- `REED_API_KEY` — free, https://www.reed.co.uk/developers/jobseeker
- `APIFY_API_TOKEN` — from https://console.apify.com/settings/integrations, needed for hiring.cafe

Pages: Settings → Pages → Deploy from a branch → `main` / `/docs`.
Dashboard: `https://tom-norton.github.io/revops-radar/`.
Bot commits: Settings → Actions → General → Workflow permissions → "Read and write".
Run it manually any time: Actions → "Daily job scan" → Run workflow.

## The sponsor check: read this

Registers list **legal** names ("Adyen N.V."); postings show **trading** names ("Adyen").
- **"not on register"** means the name didn't match — usually true, sometimes a legal-vs-trading
  mismatch. It's a caution flag, not a delete, and the deep score treats it as a small penalty.
- **UK** is reliable (daily CSV, ~126k sponsors). **NL** is best-effort (IND monthly register);
  add companies you care about to `nl_sponsors_extra.txt` (one per line) to firm it up.
- **Ireland** has no sponsor register (it uses employment permits), so Dublin roles show no
  badge — verify sponsorship directly.

## Tuning

- **Markets / keywords:** `ADZUNA_COUNTRIES`, `ADZUNA_WHAT_OR`, and the location regexes in `scan.py`.
- **Watched companies:** `companies.json` (run `python scan.py --verify` to test slugs).
- **Scoring:** everything the model sees is in `profile.md` and the rubric/caps in `scan.py`'s `score_system()`.
- **Cost ceilings:** `MAX_SCREENED_PER_RUN` (Haiku) and `MAX_SCORED_PER_RUN` (Sonnet).
- **Require sponsorship:** `SPONSOR_REQUIRED = True` drops UK/NL non-matches entirely (not recommended, given the matching caveat).

## Known limits

- **JobSpy/Indeed** may be blocked from GitHub's datacenter IPs some days; it's marked "skipped"
  in the status footer and the other sources carry the run. Ireland still comes through the ATS feeds.
- **LinkedIn's guest search endpoint** is unofficial and could change or get rate-limited without
  notice; it's wrapped so a failure there never breaks the run, just shows "skipped" for that source.
- **Belgium/Amsterdam location coverage** for hiring.cafe and LinkedIn depends on `location_ok()`'s
  regexes staying in sync with how those cities/regions actually appear in postings.
- The status footer at the bottom of the dashboard shows exactly what each source did each run —
  check it if results look thin.
