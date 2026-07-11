# RevOps Radar

Scans **all** new RevOps / GTM / Sales Ops / CS Ops / Senior CSM jobs across the Netherlands, UK, Ireland, Germany, and Spain every morning, checks each UK/NL company against the official visa-sponsor registers, scores everything against your profile with Claude, and shows the results on a dashboard you open on your phone.

## How it works

1. **Adzuna API** (the backbone) aggregates thousands of job boards. One free key covers all five countries. This replaces any hand-maintained company list.
2. A free **keyword + location filter** drops anything irrelevant before it costs a token.
3. Every UK/NL job's company is matched against the **official sponsor registers** (gov.uk daily CSV, IND monthly register). Each job gets a badge: sponsor / sponsor (likely) / not on register / n/a.
4. **Claude Haiku** scores the survivors 1-10 against your rules (location tiers, HSM floor, title band, sponsor signal). Capped at 45/run so cost stays near-zero.
5. Results commit to `docs/jobs.json`; the dashboard renders them, gate line at 5.0.

Optional extras: `companies.json` force-watches a few named companies via their ATS (cleaner data), and the scanner also tries revopsroles.com and hiring.cafe (best effort).

## Setup (about 20 minutes, one time)

1. **Adzuna key (free).** Register at https://developer.adzuna.com/ → you get an **App ID** and **App Key**. Takes 2 minutes.

2. **Create the repo.** New GitHub repo `revops-radar` (public, for free Pages). Upload all files, keeping folder structure (`.github/workflows/scan.yml` stays in that path).

3. **Add three secrets.** Repo → Settings → Secrets and variables → Actions → New repository secret, three times:
   - `ANTHROPIC_API_KEY` — your Claude key
   - `ADZUNA_APP_ID` — from step 1
   - `ADZUNA_APP_KEY` — from step 1

4. **Enable Pages.** Settings → Pages → Deploy from a branch → `main` / `/docs`. Dashboard lives at `https://YOURUSERNAME.github.io/revops-radar/`.

5. **Allow the bot to commit.** Settings → Actions → General → Workflow permissions → "Read and write permissions".

6. **First run.** Actions → "Daily job scan" → Run workflow. Then it runs daily at ~8am Barcelona.

## Daily use

Open the dashboard. Jobs at 5.0+ sit above the gate, sorted by score, each with a sponsor badge. Three toggles: show below-gate, hide non-sponsors, show dismissed. Tap a title to open the posting. When one looks real, paste it into Claude and run the full job-application-workflow.

## The sponsor check: read this

Registers list **legal** names ("Adyen N.V."); job posts show **trading** names ("Adyen"). Matching handles the common cases but isn't perfect:
- **"not on register"** means the name didn't match. Usually true, but sometimes it's a legal-vs-trading mismatch. Verify before you rule a company out. That's why the scanner flags rather than deletes, and `SPONSOR_REQUIRED` is off by default.
- **UK** is reliable (daily CSV, ~126k sponsors).
- **NL** is best-effort (the IND register is monthly and awkward to parse). For companies you care about, add them to `nl_sponsors_extra.txt` (one per line) to make NL matching solid. It's pre-seeded with a few.
- **Ireland, Germany, Spain** have no register check here, so those jobs show no badge. Verify sponsorship directly.

## Tuning

- **Countries / keywords:** `ADZUNA_COUNTRIES` and `ADZUNA_WHAT_OR` at the top of `scan.py`.
- **Filters:** the include/exclude title regexes in `scan.py`.
- **Scoring rules:** the `PROFILE` string in `scan.py`.
- **Require sponsorship:** set `SPONSOR_REQUIRED = True` to drop UK/NL non-matches entirely (not recommended given the matching caveat).
- **Cost ceiling:** `MAX_SCORED_PER_RUN`.

## Known limits

- hiring.cafe usually blocks GitHub's IPs (Cloudflare); it's marked "skipped" in the status footer, expected. Run `python scan.py` from your laptop if you want it.
- LinkedIn is excluded on purpose (blocks scraping). Adzuna already pulls a lot of what LinkedIn would show.
- Adzuna free tier has rate limits; this uses under 15 calls/day, well inside them.
