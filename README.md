# RevOps Radar

Finds new RevOps / GTM / Sales Ops / CS Ops / Senior CSM roles across your four target
markets, cheaply screens out the obvious no-fits, deep-scores the survivors against your
real profile with Claude, checks each UK/NL company against the official visa-sponsor
registers, and shows you the good ones on a dashboard. Hide/Applied state syncs across
your devices via Firebase.

**Markets:** Netherlands (anywhere), Belgium (anywhere), UK (London area only), Ireland
(Dublin). Germany, Spain, and remote-from-anywhere/EMEA roles are deliberately excluded.

**Runs:** 09:15, 14:00, 19:00 UTC on weekdays, 09:15 UTC only on weekends — i.e.
10:15am/3pm/8pm CET, and 10:15am CET on weekends. GitHub's cron is UTC-only, so during
CEST (late March to late October) each run lands an hour later in local time —
11:15am/4pm/9pm. Every slot stays after the 10am revopsroles.com email either way.
See `.github/workflows/scan.yml`.

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
7. **revopsroles.com** — parsed from Tom's own daily digest email via Gmail IMAP.
   Direct scraping of the site's location pages broke on 2026-07-31 when the site put
   Vercel's bot/attack-challenge in front of every request, including from GitHub
   Actions' IPs — same failure mode as hiring.cafe's direct API above. Requires
   `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` (a Gmail app password); skipped if unset.

**Filtering:**
8. A free **title + location filter** drops anything off-function or off-market before a token is spent.
9. An **age filter** drops anything older than 7 days when the source gives a posted date.
10. **Cross-source, cross-run dedupe** collapses the same role found via multiple sources
    or on different days into one dashboard entry.
11. Every UK/NL company is matched against the **official sponsor registers** (gov.uk daily
    CSV, IND monthly register) and badged: sponsor / sponsor (likely) / not on register / n/a.

**Reading the actual ad (everything downstream is only as good as this):**
12. Job boards routinely hand back a career page's marketing copy instead of the posting, so
    any description shorter than `MIN_DESC_CHARS` is treated as missing and **re-fetched from
    the source** — the company's own ATS API, Workday's public CxS JSON API (Workday renders
    in JavaScript, so scraping the page returns furniture and no JSON-LD), or schema.org
    `JobPosting` markup, whichever returns most. Adzuna's own 400-character summaries get
    upgraded to the full ad this way too.
13. Two **hard disqualifiers are then read off the full text in code, before either model
    call**: an ad that rules out visa sponsorship, and an ad that requires fluency in a
    language other than English. Both drop the job and log the ad's own sentence to
    `docs/excluded.json`, so a wrong drop is visible rather than silent. They are matched in
    code rather than asked of the model because they are absolute and because the model only
    ever sees a sampled copy of the posting.

**Scoring (two stages, so the expensive model only sees real candidates):**
14. **Stage 1 — Claude Haiku** cheaply screens each survivor (keep / kill).
15. **Stage 2 — Claude Opus** scores the keepers on the weighted rubric from
    `profile.md` (Experience 25% / Skills 20% / Seniority 15% / Domain 15% / Location+Visa 15% /
    Trajectory 10%) and reports the facts it can only get by reading the posting: stated
    salary, whether another language is a hard requirement, whether the function is on
    target, whether the employer is a standout.
16. **Two more hard disqualifiers, this time from what Opus reports** rather than a text
    match: a stated salary below the market's visa floor, and a posting that makes a
    non-English language a hard requirement. These catch what the regex checks in step 13
    miss — an oddly-worded requirement, a salary buried in prose — by actually reading the
    posting instead of matching a sentence. `deep_score_disqualifier()` drops the role the
    same way steps 12–13 do: not scored, not shown, logged to `docs/excluded.json` under
    `language-required` or `below-visa-floor`. The below-floor check only ever fires on pay
    actually stated in the ad, in the market's own currency — **Adzuna's predicted salaries
    are discarded** (`salary_is_predicted`), because they are modelled from the title and
    location rather than published by the employer, and a GBP figure is never compared
    against a EUR floor.
17. **The score itself is computed in Python**, not by the model, for every role that
    survives to be scored — `weighted_total()` does the arithmetic, so the number is
    reproducible from the six dimension scores instead of being whatever total the model
    reported (on an early corpus, 21 of 51 model-reported totals were more than 0.6 off the
    weighted sum of their own dimensions). **Nothing clamps that total.** There used to be
    an `apply_caps()` with score ceilings for title band, salary floor, language,
    off-target function and the CSM track; it is gone. It disagreed with the rubric it was
    supposed to enforce — a RevOps Specialist role that scored 6.5 on the dimensions landed
    at 4.0 because of one word in its title — and the `job-application-workflow` skill this
    rubric comes from has no such mechanism. What's left of it travels as **flags**
    (`score_flags()`): off-target function, junior/senior title band, CSM outside NL.
    They're shown on the row so you can judge them; they don't move the number. Salary and
    language don't appear here at all — a role either clears them and gets scored clean, or
    it doesn't and step 16 drops it before this function ever runs.
18. Results commit to `docs/jobs.json`; the dashboard shows **6.0+ to apply**, tucks
    **5.0–5.9 into a collapsed "borderline"** section, and puts everything below 5 plus
    every row dropped earlier in the pipeline into a collapsed **"excluded"** section.

## Reviewing what got thrown away

Nothing disappears silently. Every rejected row is logged to `docs/excluded.json` with the
stage and the reason, and shown in the dashboard's collapsed "excluded" section grouped by
stage — title/location filter, age, dedupe, no-sponsorship, language-required,
below-visa-floor, Haiku kill, scoring error. The disqualifier stages quote the sentence or
the figure that caused the drop, so you can tell a correct filter from an over-eager one at
a glance. The status footer carries exact counts per stage plus `raw -> kept` per source, so
an over-tight regex or a silently-broken source looks different from a quiet week.

Two commands act on what you find there:

```
python scan.py --unkill                    # free every stage-1 Haiku kill still in the file
python scan.py --unkill-history --days 14  # same, but walks git history for the ones the
                                           # file's per-stage sample no longer holds
python scan.py --ignore-age                # one run with the 7-day age filter relaxed
python scan.py --rescore                   # clear all scored rows and rescore from scratch
```

`docs/excluded.json` keeps only a bounded sample per stage, so a week of scans can produce
twice as many stage-1 kills as the committed file holds. `--unkill-history` reads every
commit of that file to recover the rest. Pair it with one `--ignore-age` run — anything it
frees is older than the 7-day cutoff by definition, so the age filter would otherwise drop
it again immediately. Neither command can resurrect a posting that has since come down:
`excluded.json` stores no URL, so a job only returns if it's still live in a source feed.

The easiest way to run a backfill is from GitHub, where the API key already is: **Actions →
"Daily job scan" → Run workflow**, tick `backfill` and `ignore_age`. That does the history
walk and the relaxed-age scan in one go. `MAX_SCORED_PER_RUN` still caps the run at 30 deep
scores, so a large backfill takes a few runs to drain. Do this after any change that
loosens the screen or the scoring rules — otherwise the change only ever applies to jobs
posted from that day on.

## Your profile lives in `profile.md`

The deep score reads `profile.md`. Edit that file whenever your background, targets, comp
floors, or market list change. Keep it factual.

Two things are **not** driven by `profile.md` alone, because code reads them directly: the
salary floors in `VISA_FLOORS` (used by `deep_score_disqualifier()` to drop a stated salary
below the floor) and the market list in `market_of()` (which gates the pipeline). Change a
comp floor or add a market and you need to update both, or the prose and the code will
disagree.

`profile.md` diverges from the `job-application-workflow` skill in exactly two places, both
deliberate: **markets** — it rejects Germany, Spain, and remote-EMEA outright (the skill
still scores Berlin 4-6 and remote-Spain 6-7) and adds Belgium — and the **CSM-track
weighting**, which the skill has no equivalent for. That narrower stance is current; don't
"fix" it back toward the skill.

Everything else is meant to match the skill's Step 1 rubric, and did not for a long time.
The scoring model here is the skill's: six weighted dimensions, judged on the skill's own
guidance, with no ceilings and no title-based exclusions. If you find the two disagreeing
anywhere other than markets and the CSM track, the radar is the one that's wrong.

## Tests

```
python tests/test_scoring.py    # pure functions: weighted total, flags, title gate, location, dedupe
python scan.py --selftest       # replay stored dimension scores through the engine, offline
python scan.py --dry            # full pipeline, no Claude calls
```

The unit tests also run in CI before the scan, so a broken filter or a mis-weighted rubric
can't spend tokens producing wrong scores. Two of them exist specifically to stop old
mistakes coming back: one asserts that no title band changes a score (the cap engine stays
dead) and one asserts the title gate still admits the Strategy & Operations wordings the
market actually uses.

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

- **Markets / keywords:** `ADZUNA_COUNTRIES`, `ADZUNA_PHRASES`, `OR_KEYWORDS` (shared by Reed
  and LinkedIn), `JOBSPY_TERMS`, and the location regexes in `scan.py`. Adding a market means
  touching `market_of()`, `VISA_FLOORS`, and `MARKETS_SENTENCE` — they sit together.
- **Which titles get through:** `INCLUDE_TITLE` / `EXCLUDE_TITLE`. This gate is **market-aware,
  not purely textual** — a plain "Customer Success Manager" is admitted in the Netherlands and
  nowhere else (`CSM_ANY`, applied in `prefilter()`), mirroring the CSM track weighting in
  `profile.md`. Widening it is now cheap: anything wrong gets screened, capped, and shown in
  the excluded section rather than sitting on the dashboard, so prefer erring wide. Re-run the
  live audit before and after a change — a widened alternative can silently break an existing
  one, which is what `test_widening_did_not_lose_anything_previously_kept` guards.
- **Watched companies:** `companies.json` (run `python scan.py --verify` to test slugs).
- **Scoring:** what the model judges is in `profile.md` and `score_system()`; what the code
  decides is `RUBRIC` (the weights) and `weighted_total()`. `TITLE_BANDS` feeds
  `score_flags()`, which annotates a row without changing its score. `VISA_FLOORS` feeds
  `deep_score_disqualifier()` instead, which drops the row outright rather than flagging it
  — see the next bullet.
- **What the deep scorer may drop outright, not just flag:** `deep_score_disqualifier()`.
  Only two things: a stated salary below the market's visa floor, and a hard non-English
  language requirement. Both are absolute — a role Tom can't legally take, or can't
  actually do — so they're policy-identical to the pre-model `says_no_sponsorship()` /
  `requires_other_language()` regex checks, just decided from the model's reading instead
  of a text match. Everything else the model reports (off-target function, title band, CSM
  outside NL) is a flag on a scored row, not a drop.
- **What the cheap screen may throw away:** `SCREEN_SYSTEM`. It can kill on exactly two
  grounds, location and unambiguously wrong function, and is explicitly forbidden from
  killing on seniority. It used to do the latter, and quietly lost a week's worth of
  Analyst-, Specialist-, Associate- and Director-titled RevOps roles plus most of the
  Strategy & Operations market before anything read the description. Re-check that list of
  in-scope functions before narrowing anything here.
- **Model + cost:** `CLAUDE_SCORE_MODEL`, `SCORE_EFFORT` (`low`–`max`; the main cost dial),
  `MAX_SCREENED_PER_RUN` (Haiku) and `MAX_SCORED_PER_RUN` (Opus). The deep-score system
  prompt is cached, so the per-run token cost is dominated by job descriptions, not the
  profile — check the `tokens` line in the status footer.
- **Dashboard bands:** `GATE` / `FLOOR` in `scan.py`. The page reads them from
  `status.json` rather than keeping its own copy.
- **Require sponsorship:** `SPONSOR_REQUIRED = True` drops UK/NL non-matches entirely (not recommended, given the matching caveat).

## Known limits

- **JobSpy/Indeed** may be blocked from GitHub's datacenter IPs some days; it's marked "skipped"
  in the status footer and the other sources carry the run. Ireland still comes through the ATS feeds.
- **LinkedIn's guest search endpoint** is unofficial and could change or get rate-limited without
  notice; it's wrapped so a failure there never breaks the run, just shows "skipped" for that source.
- **Belgium/Amsterdam location coverage** for hiring.cafe and LinkedIn depends on `location_ok()`'s
  regexes staying in sync with how those cities/regions actually appear in postings.
- **revopsroles.com** depends on Tom's Gmail subscription to the site's daily digest staying
  active and the email's HTML layout not changing; it parses that email rather than the site
  itself (see above). If the digest stops arriving or its markup changes, this source goes
  quiet the same way the others do — check the status footer.
- The status footer at the bottom of the dashboard shows exactly what each source did each run —
  check it if results look thin.
