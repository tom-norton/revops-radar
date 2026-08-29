# RevOps Radar

Finds new RevOps / GTM / Sales Ops / CS Ops / Senior CSM roles across your four target
markets, cheaply screens out the obvious no-fits, deep-scores the survivors against your
real profile with Claude, checks each UK/NL company against the official visa-sponsor
registers, and shows you the good ones on a dashboard. Tap **Apply** and it runs the front
half of the job-application-workflow skill for you and asks whatever it genuinely needs
over Telegram. Hide/Apply/Applied state syncs across your devices via Firebase.

**Markets:** Netherlands (anywhere), Belgium (anywhere), UK (London area only), Ireland
(Dublin). Germany, Spain, and remote-from-anywhere/EMEA roles are deliberately excluded.

**Runs:** 10:15am, 3pm and 8pm local on weekdays, 10:15am only on weekends —
08:15, 13:00 and 18:00 UTC. GitHub's cron is UTC-only with no DST awareness, so these
need shifting back an hour when the clocks change on 25 Oct 2026, otherwise the morning
run drifts to 9:15am and lands ahead of the 10am revopsroles.com email.
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
    or on different days into one dashboard entry — before it is screened or scored, so a
    duplicate costs nothing. Sources disagree about how to write both halves of a job's
    identity, so this compares normalised forms rather than exact strings: legal form,
    region and feed provenance come off the employer name ("Heidi" = "Heidi Health",
    "Semrush" = "Semrush UK Ltd.", "Rubrik" = "Rubrik Job Board"), and titles are
    abbreviation-expanded and compared as word sets, so "Rev Ops Manager" = "Revenue
    Operations Manager" and "Manager, Sales Operations" = "Sales Operations Manager". One
    title may be a shortening of the other, which is what catches an aggregator's truncated
    version of a posting. A bare acronym also matches the initials of the full name it's
    short for ("LSEG" = "London Stock Exchange Group"), which a subset-of-words check alone
    can't catch since the two share no actual word. See `same_role()` for the guards that
    stop that from merging jobs that only look alike — the seniority band has to match, and
    a word that appears in one title and not the other must not be the kind of word that
    makes two postings different jobs. "Renewals Manager" and "Renewals Manager - French
    Speaker" stay separate, as do "Senior Renewals Manager", the fixed-term version of a
    role, and the 1-3 and 3-6 YoE variants of the same title.

    Matching itself is pairwise, but grouping isn't: `group_duplicates()` takes the full
    transitive closure of every match in a run rather than stopping at the first one a row
    finds, because two partial names for the same company routinely don't match *each
    other* even though both match a third, fuller one — Adzuna's "AWS" and hiring.cafe's
    "Amazon" share no word and aren't acronym-related, but both match LinkedIn's "Amazon
    Web Services (AWS)". A pass that stops at the first hit pairs the full name with
    whichever partial one it meets first and leaves the other sitting on the dashboard
    next to the row it's actually a duplicate of — which is exactly what happened before
    this closure existed. **The dashboard itself is collapsed on every run too**, so
    duplicates that landed before a matching rule existed clear themselves rather than
    sitting there; `python scan.py --dedupe` does that alone, without a scan. The surviving
    row is the best copy — a scored row over an unscored one, then the employer's own ATS
    feed over an aggregator — and it carries the ids of the rows it absorbed, so a Hide or
    Mark applied recorded against a duplicate still holds. It lists the other sources that
    had the same posting as "also on" links.
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

## The apply queue

Everything after "this role looks good". `applyq.py` runs on a 15-minute cron
(`.github/workflows/apply.yml`) and works **one role at a time** — a gap interview that
interleaved questions from two roles would be unusable on a phone, which is the only place
they get answered.

**Queueing.** Tap **Apply** on a dashboard card, or send the bot `/apply <id>`. Both write
the same `queued` array on the Firebase node, next to `hidden` and `applied`. Queued is not
applied: the role stays in its section with a tag until the application actually goes out.

**What a tick does.** Loads the run state, drains Telegram, pushes the role in flight as far
as it can go, persists, exits. A tick with nothing to do costs nothing and commits nothing.
The stages:

1. **Salary gate** — runs *only* when the deep score flagged comp risk: no stated salary, a
   figure that can't be compared to the market floor, or a stated floor within 10% of it.
   The bot names the risk and offers to research; you decide. **Roles with a band clearly
   clear of the floor are never researched.** At 30+ scored roles a run, blanket salary
   research is the fastest way to spend the month's budget on comparables nobody reads.
   When it does run it obeys the skill's comparable-title rules — a RevOps role benchmarked
   against "Operations Analyst" pulls in logistics coordinators and returns a garbage median.
2. **Bullet audit** — pulls `bullet-bank.md` from the private bank, picks the sub-track
   (ANALYTICS / BUILDER / CS), decides per bullet, flags metric gaps, and works out which
   posting keywords no bullet covers.
3. **Gap interview** — checks `answer-bank.md` first and only escalates what the bank can't
   answer. One question at a time over Telegram, at most three. **No timeout.** The workflow
   persists and waits, however long that takes. Answers commit to `answer-bank.md`, so the
   bank grows with every application and the questions thin out.
4. **Packet** — new bullets drafted strictly from your own answers, written to
   `packets/` in the private repo with the audit, the answers and the per-phase token cost.

**The honesty rule, which is the point of the whole thing:** a gap you didn't answer, or
answered "no meaningful experience" to, produces no bullet. The drafting call is never even
shown it. Nothing gets filled in by inference, and the posting's own language is never
treated as evidence you did something. `tests/test_apply.py` asserts this directly.

**Bot commands:** `/apply <id>`, `/queue`, `/status`, `/cancel`, `/help`. When it asks a
numbered question, reply with the number or just describe it — both work.

**Where things live.** State, answers, bullets and packets all live in the private
`tom-bullet-bank` repo. This repo is public and `docs/` is served by Pages, so nothing from
the bank is ever written into it. That makes `BULLET_BANK_PAT` a hard dependency: without
it there is nowhere to persist an interview that spans days, and the poller fails loudly
rather than proceeding.

**Not in this phase.** The CV build, the variation review, cover letters and autonomous
submission are Phases 2-4. So is the Step 2 company strategic brief — it feeds the summary
variations, which the CV build owns. A packet is the input to that, not a finished
application.

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
python scan.py --dedupe                    # collapse duplicates already on the dashboard,
                                           # without running a scan
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
python tests/test_apply.py      # comp gate, answer handling, the apply state machine across ticks
python scan.py --selftest       # replay stored dimension scores through the engine, offline
python scan.py --dry            # full pipeline, no Claude calls
python applyq.py --selftest     # apply-queue pure functions, offline
python applyq.py --dry          # one tick, no Claude calls, no Telegram, no commits
python applyq.py --status       # what's queued and what's in flight
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
- `TELEGRAM_BOT_TOKEN` — from @BotFather, `/newbot`
- `TELEGRAM_CHAT_ID` — message the bot once, then read it off
  `api.telegram.org/bot<TOKEN>/getUpdates`
- `BULLET_BANK_PAT` — fine-grained PAT, read/write contents on `tom-bullet-bank` only

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
- **What counts as the same posting:** `same_role()`, and the four lists it reads —
  `COMPANY_SUFFIX` / `COMPANY_REGION` (noise on an employer name), `TITLE_ALIASES` (the
  abbreviations the market uses interchangeably), `DISTINGUISHING` (words that make two
  postings different jobs even when the rest of the title matches) and `DEDUPE_CITY` /
  `PLACE_NOISE` (the city bucket two rows must share before they are compared at all).
  `company_initials()` is the acronym fallback when neither name's words contain the
  other's. **How matches get grouped, not just compared, is `group_duplicates()`** — it
  takes the transitive closure of every pairwise match in a run instead of stopping at the
  first one each row finds, because `same_role()` is not itself transitive (two partial
  company names routinely don't match each other even though both match a fuller third
  one). Loosening any of these is only safe while the must-not-merge half of
  `test_same_role_keeps_genuinely_different_postings_apart` still passes — every pair in it
  is two real postings the feeds carried at the same time. Preview a change with
  `python scan.py --dedupe`, which prints each keep/drop pair before writing anything.
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

- **hiring.cafe (Apify)** was silently returning a flat ~30 raw items per run, every run, for
  six-plus weeks, regardless of how the market moved — one combined actor call across all four
  saved searches, apparently starving each other. An Atlassian Amsterdam CSM role Tom found
  browsing hiring.cafe directly never once appeared, even though a live fetch of just one of
  the four saved searches returned it near the top. Fixed by calling the actor once per search
  instead of once for all four `startUrls` together; the status footer now shows a raw count
  per search (`hiringcafe:cs-nl=raw 15`, etc.) so a search going quiet again is visible instead
  of hiding inside one opaque total.
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
