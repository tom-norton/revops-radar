# RevOps Radar

Finds new RevOps / GTM / Sales Ops / CS Ops / Senior CSM roles across your six target
markets, goes and gets the real ad when a source hands back a stub, cheaply screens out the
obvious no-fits, deep-scores the survivors against your
real profile with Claude, checks each UK/NL company against the official visa-sponsor
registers, and shows you the good ones on a dashboard. Tap **Copy for Claude** and the role
goes to your clipboard -- score, dimensions, flags, sponsor, stated comp and the ad itself --
ready to paste into the Claude project, where `job-application-workflow` does the rest.
Hide/Applied state syncs across your devices via Firebase.

**The apply half is mothballed.** The Telegram bot, the gap interview, the CV build and the
automated submission are switched off, not deleted: `apply.yml` has lost its cron and the
relay's two apply routes are behind `APPLY_RELAY_ENABLED = false`. Applications are written
by hand again, in a Claude project, because the automation was not earning what it cost in
Opus calls. Everything below about the apply queue still describes working code; it just
does not run on its own any more. The scan and the scoring are untouched.

**Markets, in preference order:** Netherlands (anywhere) · Ireland (anywhere) · UK (London
area only) · Belgium (anywhere) · Canada (anywhere, remote or on-site) · US (**remote only**,
anywhere, core RevOps or **senior** CS titles only, and **the posting must state a salary**).
Germany, Spain, on-site US roles, and remote-from-anywhere/EMEA roles are deliberately
excluded.

That ordering is real, not cosmetic. It sets the `location_visa` score band and who gets
first claim on the deep-scoring budget. The US is a
financial-runway backstop rather than a destination, and a US employer that also hires in
NL/IE/UK/CA scores higher than a US-only one, because an internal transfer later is a route
abroad without changing employer.

**Runs:** 8am, 12:30pm and 8pm Eastern on weekdays, 9am only on weekends. The dashboard
lists roles by overall score, highest first. The clock is the Cloudflare Worker's
(`worker/wrangler.toml`, `scanDueAt()` in `worker/telegram-relay.js`), which reads
America/New_York at firing time and so needs no
touching when the clocks change. GitHub's own cron in `.github/workflows/scan.yml` is kept
as a backstop only — it is hours late in practice, see **Why the schedule lives in
Cloudflare** below.

## How it works

**Data layer (several sources so no single one can break the run):**
1. **Adzuna API** — Netherlands, UK, Canada and the US. (No Ireland coverage, hence the
   others.) North America gets a narrower phrase set: the US title gate would drop the rest
   on arrival, and the free tier's nominal 1,000 calls/month is already well exceeded.
2. **Reed API** — extra UK/London depth (free key).
3. **JobSpy** — Indeed for the **Netherlands**, **Belgium** and Ireland, plus Indeed and
   **Google Jobs** for the Netherlands, Belgium, Canada and the US. Google Jobs is the
   closest free substitute for hiring.cafe's long-tail reach, since it indexes
   Greenhouse/Lever/Ashby posting pages directly. NL and BE were added after counting the
   board: 307 London rows against 102 Dutch ones over 45 days, with Indeed, Reed, Adzuna-UK
   and LinkedIn all pointed at London while a single Adzuna feed was the whole of the Dutch
   coverage. That is a sourcing fact, not a market fact.
4. **Company ATS feeds** — **43** named companies (`companies.json`), on **any board
   `findform.BOARDS` can read**: Greenhouse, Lever and Ashby have their own fetchers here,
   while SmartRecruiters, Recruitee, Workable, Personio and Teamtailor are read through
   `findform.board_jobs()` by `fetch_board()`. The old greenhouse/lever/ashby-only rule was
   quietly deciding which *employers* could be watched — Recruitee, Workable, Personio and
   Teamtailor are the Dutch scaleup norm, so the goal market was the one the watchlist
   could not reach. The NL/IE/BE half of the list was picked by counting how many roles each
   board actually had in those markets on the day it was added (DataSnipper, Mollie, Bynder,
   Miro, Mendix, Nebius, Databricks, bunq, Channable, Sana Commerce; Klaviyo, Squarespace,
   Qualio, Tines, Flipdish, Vanta, Wayflyer, Workhuman, Sitecore, CarTrawler; Showpad,
   Deliverect, Alan, Lansweeper). Clean company names, full descriptions where the board
   carries them, and a big part of the Ireland coverage since many US firms hire there this
   way. Optional — the rest of the pipeline works without it; it exists to guarantee
   coverage of specific companies Tom wants watched regardless of whether they show up via
   the other sources. `python scan.py --verify` tests every slug.
5. **hiring.cafe** — via the Apify actor `memo23/apify-hiring-cafe-scraper`, run against
   Tom's searches. The direct API is no longer an option at all: `hiring.cafe/api/search-jobs`
   returns 401 and `hiringcafe.com` sits behind a Cloudflare challenge. Worth the $19/mo —
   of 478 dashboard rows it contributed 99, **55 of them unique to it**, across 55 distinct
   companies with no overlap with `companies.json`, including the two highest-scoring rows
   the radar has found. Its value is long-tail discovery of companies not on the watchlist,
   which is why a watchlist cannot replace it.
   The **five** searches are **structured dicts** in `scan.py`, not the percent-encoded
   `searchState` blobs they used to be: `revops` (NL/IE/BE/CA + London 50mi),
   `cs-eu-ca`, `cs-nl`, `us-revops` and `us-cs`. `hiringcafe_url()` rebuilds the URLs, and
   `tests/fixtures/hiringcafe-searchstate.json` is a capture of the five URLs Tom's own
   browser produced — the round-trip test asserts the code asks for precisely what he
   asked for, because a silent change in targeting shows up as a source going thin rather
   than as an error.
   `workplace_types` is per-location and load-bearing: it is how the European searches keep
   remote-EMEA rows out at source and how the US ones ask for remote only. Both US searches
   also set `restrictJobsToTransparentSalaries`, which is why the code can require a stated
   salary on every US row. There is deliberately no whole-US or whole-UK location: the US
   searches are Grand Rapids + 50 miles with `flexible_regions` opening outward, and the UK
   is the London locality, both so Apify is not paid for rows the location gate then drops.
6. **LinkedIn** — mirrors "Jobs based on your preferences" via LinkedIn's public,
   unauthenticated guest job-search endpoint. No login or session cookie. Seven geoIds:
   London Area, Belgium, Netherlands, Amsterdam, Ireland, United States, Canada.
7. **revopsroles.com** — parsed from Tom's own digest emails via Gmail IMAP.
   Direct scraping of the site's location pages broke on 2026-07-31 when the site put
   Vercel's bot/attack-challenge in front of every request, including from GitHub
   Actions' IPs — same failure mode as hiring.cafe's direct API above. Requires
   `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` (a Gmail app password); skipped if unset.
   **Two alerts now** (Europe and the US) arriving as separate emails. The fetcher already
   loops every matching message and dedupes by job id, so that needed no change, but the
   IMAP filter is the whole **domain** rather than one mailbox — a new alert sending from a
   different address would otherwise be missed and the status line would look normal. The
   footer reports a per-email kept count plus how many rows carried a salary.
   **The salary selector was stale and is fixed.** It matched `color:#16a34a`, the site
   restyled to `#9e4d00`, and nothing failed loudly — every row simply arrived with an empty
   salary, 0 of 32, and it stayed invisible because European postings mostly state no pay
   anyway. It only became load-bearing when US rows started requiring one. The selector is
   now the **shape of the text** (a currency symbol next to digits) rather than the colour
   of the span around it, so the next restyle cannot repeat it, and
   `tests/fixtures/revopsroles-digest.html` pins it against a real email.
   **The digest is a pointer, not a source.** Its own links still cannot be fetched (429 to
   datacenter IPs), so a row arrives carrying a ~50-character synthesized summary where a
   posting should be. Scoring that is close to worthless — a stub says nothing a title does
   not, and measurably scored *higher* than real postings (6.03 vs 5.78 mean, 22 of 59
   clearing the gate) because a silent ad has nothing in it to count against. So the title
   and company are used to go and find the real ad elsewhere; see steps 12a–12c below.
   Whatever survives that unfound reaches the scorer labelled as a stub rather than dressed
   up as an ad.
   **What was actually broken was the plumbing, not the source.** All 34 revopsroles rows on
   the dashboard were stubs, and the reason was one line in `_row_rank`: dedupe runs *before*
   any description is fetched and picks a winner on source rank, where revopsroles outranks
   hiring.cafe and LinkedIn. So the stub won and the row it folded away — carrying the
   employer's own Ashby, Greenhouse, SmartRecruiters or Workable link — was discarded. The
   JD was never missing from the internet; it was thrown away at the moment of dedupe, and
   every rescue downstream then paid board probes and web searches hunting for a link the
   row had already been handed. 19 of the 34 carried one; asking it recovers the full ad for
   18 of them, 2,371–12,255 characters against the 36–57 they had. Step 12a.
   The digest carries work mode as a *tag*, not in the location, and that tag is folded
   into what the location gate sees **for US rows only** — remote is the requirement there,
   whereas in Europe remote wording next to a bare country is how remote-EMEA reqs get
   rejected, so doing it everywhere would drop genuine Irish and Dutch roles.

**On the card:** pay is shown as a tag — solid when the scorer read it off the posting,
outlined and marked `(feed)` when it came from the job feed and the ad did not confirm it.
The dashboard had never rendered `salary` at all, so a Versapay row whose feed said
`110000-130000 USD` displayed "comp not listed" and nothing else. The flag now names which
fact is missing rather than flattening both into one sentence.

**Filtering:**
8. A free **title + location filter** drops anything off-function or off-market before a token is spent.
8b. **A source that states the work mode in a field gets to use it** (`gate_location()`).
    `market_of()` can only look inside the location string, and US remote is the one market
    where that matters: remote is the *requirement* there, and almost no source writes it
    into the location. hiring.cafe sends `Grand Rapids, MI` for a role its own search
    already filtered to Remote. The result was a run taking 360 raw Adzuna US rows, 80
    Indeed, ~70 hiring.cafe and LinkedIn's entire US geoId and keeping **one** — from
    revopsroles, the only source whose work-mode tag had been wired in. One source had the
    plumbing, one source produced output.
    So the work mode is appended to what the **gate** sees, never to the location stored on
    the row. **US only**, and that restriction is the whole point: in Europe remote wording
    next to a bare country is how remote-EMEA reqs get *rejected*, so tagging an "Ireland"
    row would drop a real Irish role, and Canada accepts remote and onsite alike so the tag
    could only do harm.
    For hiring.cafe the fact comes from the **search itself** — `us-revops` and `us-cs` set
    `workplace_types: ["Remote"]`, so their rows are remote by construction and
    `hc_search_work_mode()` reads that off the query rather than guessing. A row-level field
    refines it where hiring.cafe provides one, but nothing depends on that key existing.
    **LinkedIn, Adzuna and Indeed cannot be fixed this way**, and it is worth being plain
    about it: LinkedIn's guest cards carry no work-mode field at all and its `f_WT` filter
    is ignored by that endpoint (remote-only and onsite-only return the identical ten job
    IDs — verified), while Adzuna and JobSpy have no such field either. Matching on the word
    "remote" in the title would be a poor substitute, since titles rarely carry it. Those
    three need the work mode read off the **posting**, which is a separate change.
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
12a. When that comes back thin, the row is asked **what its own duplicates know** before
    anything is searched for. Dedupe collapses every copy of a role into one row *before*
    any description is fetched and picks the survivor on source rank, not on what it can
    tell us about the job — so the winner is routinely a stub while the copy it folded away
    was pointing at the employer's own Ashby/Greenhouse/SmartRecruiters/Workable posting.
    `absorb_duplicate()` already keeps those links on the winner as `also_seen`, so
    `fill_from_duplicates()` costs nothing to run and needs no lookup: on the 34 revopsroles
    rows it was written for, 19 carried a link and **18 came back with the full ad**. This
    is the same `also_seen` field the apply-link resolver reads (see *The record itself is
    checked before any of that*) — it was answering "where do I apply" and not being asked
    "what does it say".
12b. When *that* still comes back thin — a stub with no duplicate, or a feed whose link is
    dead — the **company's own ATS board is searched by name**: `rescue_description()` probes the
    eight boards in `findform.BOARDS` for the employer, matches the posting by title, and
    takes the text off the board directly. Free, so it runs in stage 1 before anything is
    screened, capped at `MAX_JD_RESCUES_PER_RUN = 12` rows a run. It is **stricter about
    location than an application is**: a board row in the wrong market is refused outright,
    because a title match alone is good enough to *apply* with (you see the role before you
    send anything) but not good enough to *score* on — the wrong city's copy of a role would
    silently answer the location question. Worth being honest about the yield: this only
    helps a company that has a public board, which is **3 of the 34** revopsroles stubs —
    and all three are ones 12a had already recovered for free, so on the current dashboard
    this step adds nothing the duplicates had not already given. It earns its place on the
    rows that arrive with no duplicate at all.
12c. For the rest, the posting can be **web-searched, and the result verified in code** —
    the only place in the pipeline that spends tokens to get *evidence* rather than to form
    a judgement. **It is switched off** (`MAX_JD_SEARCHES_PER_RUN = 0`) because it never
    once paid: over 12 sampled runs it fired 11 times and `verify_jd()` confirmed the page
    **zero** times, at a Sonnet call plus 20–40k tokens of search results each. The failure
    is in the confirmation rather than the search — `verify_jd()` wants schema.org
    JobPosting JSON-LD and most ATS pages do not publish it — so the code stays, and
    loosening that check is what would earn the money back. When it is on it is priced like
    a last resort: a few rows a run, `search_jd_url()` asks Sonnet for one line, a URL, and nothing else;
    it is never asked to read or judge the role. `verify_jd()` then decides whether the page
    is really that job off the page's own schema.org metadata — the title through
    `findform.title_score` at the same threshold an application uses, the employer through
    `findform.cache_key` so "Omnea" and "Omnea Ltd" are one company. **Nothing is accepted
    without both.** A search that finds the wrong posting costs a refused match and a row
    that stays thin, which is the state it was already in, rather than a confident score
    built on another role's requirements.
12d. Anything still thin is **set aside, not scored and not dropped** — the one outcome
    that says what is actually true about it. It used to be scored under an `EVIDENCE: THIN`
    warning, and that was the wrong shape of answer: a number produced from a title, a
    company and a location is not a *worse* score, it is a different kind of object, and
    ranking it against scores built on real postings makes the list lie in both directions.
    A stub scored 8.2 sat above real 7s it should not outrank; a stub scored 5.2 was a role
    that never got looked at on evidence that could not support the judgement. It was also
    paying a Haiku screen and an Opus call per row to say nothing.
    Not *dropped* either, and that half matters as much. A drop means "ruled out", and
    nothing about these rows has been ruled out — the two hard disqualifiers in step 13
    need posting text to fire, so on a stub neither ran. The role is **unexamined, not
    rejected**. It goes to the dashboard's own **"unscored"** section carrying everything
    that is known (title, company, location, salary, sponsor badge, and an apply link the
    board lookup goes and finds for it), with a dash where the score would be rather than a
    `0.0`, for Tom to judge by eye. The status footer counts them on their own line, because
    this number climbing is the signal that 12a–12c have stopped working.
    Rows already on the dashboard that were scored this way are converted by
    `scan.py --backfill-jd`, after it has tried to recover their ad. On the current corpus
    that is **62 rows across 45 days** — 34 revopsroles, 16 Adzuna and 12 hiring.cafe, so
    this was never only a revopsroles problem.
13. Two **hard disqualifiers are then read off the full text in code, before either model
    call**: an ad that rules out visa sponsorship, and an ad that requires fluency in a
    language other than English. Both drop the job and log the ad's own sentence to
    `docs/excluded.json`, so a wrong drop is visible rather than silent. They are matched in
    code rather than asked of the model because they are absolute and because the model only
    ever sees a sampled copy of the posting. They also **re-run on any text recovered by 12b
    or 12c**, since a stub is too short for either to fire — without that, a role the
    pipeline would have refused had its ad arrived by the ordinary route would get scored
    just because the ad turned up late. (They read whatever 12a–12c recovered because all
    three now run *before* this step, rather than 12c running after the screen and needing
    its own second pass.)
12e. **The sample the scorer reads can no longer drop the pay.** `sample_desc()` fits an ad
    into `DESC_CHAR_CAP` (6,000) by keeping head and tail, because sponsorship and language
    terms live in the closing block. That is still not enough: Samsara's ad runs to 9,163
    characters and states `Annual OTE Salary $111,562.50 — $168,750 USD` in the **middle
    third**, exactly what the 70/30 split threw away, so the scorer reported no salary for a
    role whose pay is written on the page. Across the corpus **10 of the 43 ads that state
    pay lost the figure this way — a 23% miss rate on the one fact that gates the US market
    outright**, while the tail landed on fraud-warning boilerplate. A pay figure in the
    dropped middle is now carved out and kept as a third segment, paid for out of the head,
    and the *last* match wins because ads mention money early for other reasons (Versapay
    opens with "$257B processed annually"). 43 of 43 now survive, within the same cap.
12f. **An aggregator's ad is a summary, and the part it drops first is the pay.** Versapay's
    Indeed copy runs to 5,364 characters — nowhere near thin — and states no salary, while
    the Lever posting that same row links to states `$110,000 - $130,000 a year`. So
    `needs_better_ad()` triggers the step 12b board lookup on the *missing fact* rather than
    on length: a **US** row whose ad states no pay is not a usable ad however long it is,
    since the row can neither be scored nor disqualified on it. The board's copy is only
    taken when it actually states the pay the summary dropped. **Europe is excluded on
    purpose** — most European ads state no salary and an unstated one costs the role
    nothing there, so chasing it would be 254 of 529 rows of work with no decision attached.
    The test is `jd_pay_figures()` (windowed 40k–1M), not the looser `PAY_MENTION` used for
    sampling, precisely so "$257B annually" does not read as a salary and stop the search.
13c. **The US "confirmed pay" rule asks the ad, not the feed.** A US role is only worth
    taking at pay Tom can see, so a US row with no salary is dropped — but reading "no
    salary *field*" as "no salary" made that a filter on which source found the role.
    LinkedIn publishes no salary column at all and Adzuna's figures are mostly modelled
    estimates that `adzuna_salary()` discards, so a US ad that states its band in the
    posting — which US ads increasingly must, under pay-transparency laws — was dropped
    unread. `us_comp_unstated()` now scans the description for a pay figure first
    (`jd_pay_figures()`, windowed to 40k–1M so an ARR number, an hourly rate or a $2,500
    stipend is not mistaken for a salary) and lets the row through if it finds one. The
    rule itself is unchanged: the deep scorer reads the real numbers, and
    `deep_score_disqualifier()` drops the row there if it turns out the posting states no
    salary after all. A false positive in the regex costs one step; a false negative cost a
    real role, silently.

13b. **A feed can be wrong about where a job is**, and hiring.cafe was: it put a Canadian
    role in Dublin. That is not a cosmetic label, because the market picks the CV's contact
    block, sets the floor pay is measured against, and decides how the application **form**
    answers the visa questions — an Irish-tagged Canadian role tells the employer Tom needs
    sponsorship and is not authorised to work there, which is wrong twice over for a
    Canadian citizen. So the deep scorer, the one component that reads the whole ad, reports
    the location the posting itself states (`posting_location`) and code compares it through
    `market_of()`, the same single source of truth the location gate uses. The scorer is
    deliberately told to **trust** the resolved market for scoring and put any disagreement
    in that field instead, because a model arguing with the location gate is what the
    "trust it" instruction was added to stop. A stated location that resolves to no target
    market is not a conflict — "Remote - EMEA" and "Global" land there and would otherwise
    fire on most rows. A real disagreement badges the row and, in the apply queue, stops the
    run to ask before a CV is built or a form is filled (see `/market` below).

**Scoring (two stages, so the expensive model only sees real candidates):**
14. **Stage 1 — Claude Haiku** cheaply screens each survivor (keep / kill), and it may
    kill for **one** reason: the role is unambiguously outside Tom's target functions.
    Location is not a kill ground — it has already been decided in code, and the resolved
    market is handed to the screen as settled — and neither are the industry, the employer,
    the pay or the seniority. That rule is not decorative: two thirds of screened rows were
    being killed, on stored reasons like "Location not specified; likely US-based",
    "Cleantech, not B2B SaaS" and "JetBrains is based in Czech Republic", all of them
    re-deciding something the pipeline had already settled or marking down a domain the deep
    scorer scores properly. A wrong keep costs one cheap call; a wrong kill loses a role Tom
    never sees.
15. **Stage 2 — Claude Opus** scores the keepers on the weighted rubric from
    `profile.md` (Experience 25% / Skills 20% / Seniority 15% / Domain 15% / Location+Visa 15% /
    Trajectory 10%) and reports the facts it can only get by reading the posting: stated
    salary, whether another language is a hard requirement, whether the function is on
    target, whether the employer is a standout.
16. **Two more hard disqualifiers, this time from what Opus reports** rather than a text
    match: a stated salary band entirely below the market's floor, and a posting that makes
    a non-English language a hard requirement. These catch what the regex checks in step 13
    miss — an oddly-worded requirement, a salary buried in prose — by actually reading the
    posting instead of matching a sentence. `deep_score_disqualifier()` drops the role the
    same way steps 12–13 do: not scored, not shown, logged to `docs/excluded.json` under
    `language-required` or `below-visa-floor`. The below-floor check only ever fires on pay
    actually stated in the ad, in the market's own currency — **Adzuna's predicted salaries
    are discarded** (`salary_is_predicted`), because they are modelled from the title and
    location rather than published by the employer, and a GBP figure is never compared
    against a EUR floor.
    **The top of the band decides, not the bottom.** This read the bottom, and a posting is
    not an offer: `$120,000 - $150,000` against the $130,000 US floor was dropped outright
    on the 120, even though most of that band clears the floor and the entire negotiation
    happens inside it. A band is disqualifying only when **all** of it is below the floor —
    then no amount of negotiating reaches it, and that is the fact worth acting on. A band
    that straddles the floor is kept and **flagged** on the card (`band 120000-150000 USD
    starts below your 130000 USD floor`), so Tom sees the risk instead of never seeing the
    role. Opus now reports `salary_max_base` alongside `salary_min_base` for this; a row
    scored before that field existed falls back to its bottom figure, which reproduces the
    old answer exactly rather than reading a missing top as zero and disqualifying the
    whole stored corpus in one pass.
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
    (`score_flags()`): off-target function, junior/senior title band, CSM outside
    NL, thin evidence, and a **location conflict** (step 13b).
    They're shown on the row so you can judge them; they don't move the number. Salary and
    language don't appear here at all — a role either clears them and gets scored clean, or
    it doesn't and step 16 drops it before this function ever runs.
18. Results commit to `docs/jobs.json`; the dashboard shows **6.5+ to apply**, tucks
    **6.0–6.4 into a collapsed "borderline"** section, and puts everything below 6 plus
    every row dropped earlier in the pipeline into a collapsed **"excluded"** section. The
    bands live in `scan.py` (`GATE` / `FLOOR`) and are written into `docs/status.json` each
    run, so the page reads them from there rather than keeping its own copy — the 6.0/5.0
    pair still in `docs/index.html` is the fallback for stale data, not the live value.

## The apply queue

> **Mothballed.** Kept as the record of how this worked and as working code behind `workflow_dispatch`. The live path is the dashboard's **Copy for Claude** button into the `job-application-workflow` skill.

Everything after "this role looks good". `applyq.py` runs on a 15-minute cron
(`.github/workflows/apply.yml`) and works **one role at a time** — a gap interview that
interleaved questions from two roles would be unusable on a phone, which is the only place
they get answered.

**Queueing.** Tap **Apply** on a dashboard card, or send the bot `/apply <id>`. Both write
the same `queued` array on the Firebase node, next to `hidden` and `applied`. Queued is not
applied: the role stays in its section with a tag until the application actually goes out.

**One round trip per phase, and never more.** This is the design constraint, and it comes
from measurement: over the 15 hours after launch, GitHub delivered **4 of an expected 60**
scheduled runs, with gaps up to 5h45m. Scheduled workflows are best-effort and get dropped
under load. So every wait for you costs hours. A role asks once to get its packet, and at
most once more to pick a summary — and that second one only fires above the review score.
Everything else, including the company research and the whole tailoring pass, is arranged
to run on the near side of a wait:

1. **Bullet audit** — needs nothing from you, so it runs first. Pulls `bullet-bank.md` from
   the private bank, picks the sub-track (ANALYTICS / BUILDER / CS), decides per bullet,
   flags metric gaps, and works out which posting keywords no bullet covers. Checks
   `answer-bank.md` so a gap a previous interview already answered is never asked again.
2. **Ask** — one message carrying every question this role will ever ask: the comp-risk
   question if there is one, plus up to three gaps. You reply once, however you like —
   numbers, letters, or prose. A cheap model splits your reply per question, and
   `split_is_sane()` discards anything it returns that isn't traceable to your own words.
   Anything you leave out gets one nudge, then it proceeds without it. **No timeout.**
3. **Packet** — salary research if you asked for it, new bullets drafted strictly from your
   answers, written to `packets/` in the private repo with the audit, the answers and the
   per-phase token cost.

After asking, the run stays alive for ten minutes (`APPLYQ_HOLD_OPEN`). Answer while it's
still up and the entire application finishes in that one run.

**Salary research is on-demand, never blanket.** It only asks when the deep score flagged
comp risk: no stated salary, a figure that can't be compared to the market floor, or a
stated floor within 10% of it. **Roles with a band clearly clear of the floor are never
researched.** At 30+ scored roles a run, blanket research is the fastest way to spend the
month's budget on comparables nobody reads. When it does run it obeys the skill's
comparable-title rules — a RevOps role benchmarked against "Operations Analyst" pulls in
logistics coordinators and returns a garbage median.

**The honesty rule, which is the point of the whole thing:** a gap you didn't answer, or
answered "no meaningful experience" to, produces no bullet. The drafting call is never even
shown it. Nothing gets filled in by inference, and the posting's own language is never
treated as evidence you did something. `tests/test_apply.py` asserts this directly.

## Telling it the CV is wrong

> **Mothballed.** Kept as the record of how this worked and as working code behind `workflow_dispatch`. The live path is the dashboard's **Copy for Claude** button into the `job-application-workflow` skill.

```
/redo cut the LexisNexis training bullet, it's the weakest
/redo lead the summary with the forecasting, not the MBA
/redo the NAVEX section is too long
```

It rebuilds the last CV with that change and nothing else, and sends the new PDF. No round
trip: you spent it by sending the feedback. The revision edits **the page you actually
read**, not the tailoring output behind it, and the posting travels with the finished role
so "the CV you sent me yesterday" is still revisable after the role ages off the dashboard.

It works on a CV built before `/redo` existed, too. The run state is a cache; the packet
and the PDF in the bank are the record, so a `/redo` with nothing in state recovers the
role from those and revises the page you actually read. Saying "no CV to revise yet" with
the PDF sitting in the same repo is not an answer.

Your feedback is an instruction about the page, not a new source of fact. Ask for a number
that exists nowhere and it will not write it: it says so, in the message, and hands you the
page without it. The same honesty screen runs on a revision as on a first build. The bullet
bank is **not** written a second time, because those bullets already went through the
promotion test on the first build.

## The cover letter

> **Mothballed.** Kept as the record of how this worked and as working code behind `workflow_dispatch`. The live path is the dashboard's **Copy for Claude** button into the `job-application-workflow` skill.

Opt-in, never automatic:

```
/cover
/cover lead on the forecasting rebuild, not the MBA
/cover keep it short, they asked for brevity in the posting
```

It writes the letter for the CV that just went out and sends the PDF. No round trip and no
new research: the company brief, the audit, your answers and the page as it shipped are all
still on the finished role, so `/cover` is one model call and a render. Run it again to
rewrite it; there is nothing to undo. The letter is named after the CV, so the two sit
next to each other in the bank, and its **full text goes in the packet** because half the
application forms want it pasted into a box rather than uploaded.

The layout is not the model's to choose. The letterhead is the CV's letterhead, read off
the same skeleton, so the number you set with `/phone` is on the letter the moment it is on
the CV. The date, the recipient block, the subject line, the salutation and the sign-off
are written in code, and **no hiring manager is ever invented** — it is addressed to the
company. The renderer has no bullet in it, which is how the no-listicle rule holds.

**One page, measured off the rendered PDF.** Over the line, the last body paragraph is
dropped and it renders again, twice; still over, it is not sent, and you are told to ask
for it shorter. A rule asked for in a prompt is a request.

**The same honesty screen as the CV, applied twice.** Every claim the letter makes about
your experience goes through the screen the bullets go through, against the bank, the base
CV, your own answers and the page that already shipped; a claim that fails takes its
paragraph off the page. Then every number is checked sentence by sentence against the
sources that sentence is entitled to: a sentence about the company may use the company's
numbers, a sentence about you may not, and the posting is evidence for neither. If the
opening or the close is what failed, nothing is sent at all — a letter without its hook is
not a letter — and you get told why.

**Bot commands:** `/apply <id>`, `/queue`, `/status`, `/cancel`, `/phone`, `/market`, `/redo`,
`/cover`, `/submit`, `/send`, `/help`.

**Answer questions however you like.** A reply that is nothing but letters is read in
code, exactly, with no model involved: `1a 2b 3c`, `a b c`, `C, c, b`, `1. a  2. c` all
work. Prose goes to a cheap model to be split per question, and anything that model
returns which is neither traceable to your own words nor one of the options you were
offered gets thrown away.

**Known limit: GitHub's cron is unreliable.** `*/15` is a request, not a promise, and in
practice it lands every few hours — 35 firings of an expected ~576 over the six days to
4 Sep 2026. One round trip per role keeps that to a single wait, and the Telegram webhook
into `workflow_dispatch` removes the wait entirely by pushing work in rather than polling
for it. The cron stays as the fallback for when the Worker is down.

**Where things live.** State, answers, bullets and packets all live in the private
`tom-bullet-bank` repo. This repo is public and `docs/` is served by Pages, so nothing from
the bank is ever written into it. That makes `BULLET_BANK_PAT` a hard dependency: without
it there is nowhere to persist an interview that spans days, and the poller fails loudly
rather than proceeding.

## Applying

> **Mothballed.** Kept as the record of how this worked and as working code behind `workflow_dispatch`. The live path is the dashboard's **Copy for Claude** button into the `job-application-workflow` skill.

Opt-in, and it never sends anything on its own:

```
/submit     fill the form for the CV that just went out, and show it to you
/send       submit the form you have just read
```

`/submit` opens the application form in a browser, fills it, prints the filled page to a
PDF and sends you that PDF. **Nothing is submitted.** The fill stage has no route to a
submit button at all — pressing it lives in one function, and the only thing that reaches
that function is a `/send` from you. That split is the whole design: a CV that ships wrong
gets a `/redo`, a letter that reads thin gets rewritten, and an application that goes in
has gone in, on a real company's record, under your name.

**Code fills the facts; a model only answers the questions.** Your name, email, phone,
location, LinkedIn, the CV, the cover letter, work authorisation and sponsorship are all
filled from what is on file — the same skeleton the letterhead is built from, so a number
set with `/phone` reaches the form the moment it reaches the CV. The model is never shown
those fields, so it cannot put a wrong answer in one.

**Two contact blocks, picked by market.** `contact` is the European CV (Barcelona, the
European number, a nationality line). `contact_na` is the Canada/US one: Grand Rapids, the
US number, and **no nationality line** — stating US citizenship on a US application is
noise, and on a Canadian one it invites a sponsorship question that does not apply to a
citizen. `cvbuild.load_base(bank, market)` picks between them, and the market is an
argument to *that* rather than something applied afterwards, because the same skeleton
feeds the CV, the letterhead and the form: a swap applied in one place and forgotten in
another would put a Barcelona address on a Grand Rapids CV's application form.

Nationality is its own field rather than read back off the printed contact line. It is a
*fact* the form filler needs and separately a *line* the European CV happens to print, and
while it was being scraped out of the contact array, dropping that line from the North
American block silently emptied nationality for every Canada and US application.

**Work authorisation comes off the citizenships on file and the market the role is in.** A
US citizen applying to a Dublin role is not authorised there and does need sponsorship.
Canada answers authorised **yes** and sponsorship **no**, because Canadian citizenship by
descent is automatic at birth — the certificate is proof of it, not the grant of it. What
the pending certificate affects is the start date, which an employer needs to know rather
than have buried, so the answer carries that caveat into the plan you approve. What it does answer is the rest: "what excites you most about this opportunity", "how
did you hear about this job", and whatever else that particular form asks.

**Demographic questions are declined, never answered.** Gender, race, ethnicity, veteran
status, disability: the model never sees them, and the code does not answer them either.
Where the form offers a way to decline it takes it, required or not, so the form says so
rather than just sitting blank. Where it offers none, it stays empty. They are never put to
you in the interview below either.

**Blank beats invented, and a blank stops the send.** Every written answer goes through the
same honesty screen as the CV and the letter, against the same sources; one whose claim
cannot be traced, or that carries a number from nowhere, is dropped and its field left
empty. A salary expectation you have not given is never guessed at.

**Anything it couldn't answer, it asks you.** Right after the filled form comes a numbered
interview, the same shape as the gap interview earlier in the run, covering everything
nobody could answer: a salary expectation nothing could source, an answer the honesty
screen dropped, a phone number that isn't on file. Required ones are marked, because only
those hold up a send. Reply `1 …`, `2 …` and it fills them in and prints the form again.
Your own answers go in as your words, unscreened, because the screen exists to stop a model
inventing your experience and you cannot invent your own.

One list does the asking, the reply-mapping and the "still open" listing, because the
numbering is the contract: ask off one list and map the reply against another and your
answer to question two lands in question three's box. A field whose question couldn't be
read off the page isn't in it — there's no way to ask a question nobody can state — but it
still counts as missing and still stops the send; you read it on the printed form.

**What you approved is what gets sent.** The runner that filled the form is destroyed long
before you read the PDF, so `/send` opens the form again and replays the plan onto it. If
the employer has edited the form in the meantime the shape no longer matches, nothing is
submitted, and it re-reads and re-prints for you instead. After the click the page is read
back: if it does not confirm, you are told it was sent but not confirmed, never that you
have applied. **The one thing you are never told is that an application went in when the
page did not say so.**

**A LinkedIn link is not a form, so it goes looking for the real one.** Half the roles on
the radar arrive through LinkedIn or an aggregator, and LinkedIn will not give up the
"apply on company website" URL to anyone who is not logged in. So it does not chase the
link: it goes to the company instead, finds their own job board through the public APIs
Greenhouse, Ashby, Lever, SmartRecruiters, Recruitee and Workable publish, and looks for
the same role on it. Nine of the first ten companies tried this way were found from the
company name alone.

The matching is deliberately strict, because the downside is not a near miss. Three real
cases from the current scan:

| Company | What happened |
|---|---|
| Okta | One exact title on their board, in Dublin. Filled it. |
| Braze | Four identical titles across US cities, posting was London. Refused, and sent you the four links. |
| Vanta | 109 roles on the board, nothing above 0.33. You are told the title is not on their board, with a link to it, rather than told the role is dead: a slug guessed from a company name can land on somebody else's board, and a retitled role looks the same from here as a closed one. |

A title has to match nearly exactly rather than merely closely, and where several roles
share one title the market has to pick exactly one of them. Anything else comes back to you
as links to choose between.

**And it now runs at scan time, not only when you queue a role.** 373 of the 492 rows had
no known application host, across 281 distinct companies, and the dashboard could say
nothing but "unknown" about every one of them — while a board existed for a good share the
moment `/submit` was pointed at them. So the lookup moved forward: each scan resolves the
recent, above-floor rows whose board is unknown, and writes the answer onto the card.

**Every row now carries an `apply_link` state**, and the scan reports the split, because
"can I actually apply to this" was invisible. The only signal used to be `ats_fillable`,
which was false on 432 of 478 rows and conflates two different problems: *no driver for
this ATS* and *no form was ever found*. The four states are `auto-fillable`,
`company ATS`, `aggregator only` and `unresolved`; a recent run read
`aggregator only 353, auto-fillable 44, company ATS 87`.

Following an aggregator link to the employer was tried and **does not work**, so the code
does not attempt it:

- **Adzuna's** API `redirect_url` is not a redirect — it answers 200 with an Adzuna
  details page. The apply button on that page goes to `/land/ad/<id>?aztt=<JWT>`, a second
  Adzuna interstitial that returns **403 from CloudFront** to a datacenter IP and carries
  an `exp` claim, so the URL would expire regardless. Measured on six real rows: 0 resolved.
- **LinkedIn's** "apply on company website" URL is behind a login, and the guest page only
  offers Easy Apply.

So the route for those rows is the one this module already takes: go to the company
instead. What the `apply_link` state buys is that the board-lookup budget is now spent
**aggregator-only rows first** — a row already pointing at a company ATS has somewhere to
apply even if the lookup never runs, and an aggregator-only row has nowhere. Raising
`BOARD_LOOKUP_DAYS` from 7 to 14 (the dashboard keeps rows for 45 days, so 7 left three
quarters of the visible board permanently unlooked-up) took one run from *26 rows / 7
resolved* to *66 rows / 12 resolved*.

The cost of that is bounded three ways, because this is the one part of a scan that talks
to eight board APIs:

- **Cached per company, negatives included** (`board-cache.json`, committed by the scan
  workflow — a cache that isn't committed doesn't survive a fresh runner checkout). The
  ~200 employers running their own careers stack are asked once a month, not every fifteen
  minutes.
- **The eight boards are probed together**, not one after another. A typical miss costs a couple of seconds
  for all of them; a company whose board host simply *hangs* costs a timeout eight
  times over. Measured on a 35-company backfill: **11 minutes serial, 69 seconds
  concurrent.** The winner is still the first board in list order that answered, so the
  answer doesn't depend on which request came back first.
- **A wall-clock budget and a company cap per run.** A scan fires every fifteen minutes and
  ends by pushing to main, so two overlapping runs race on that push. Companies that don't
  fit wait for the next run, which costs nothing — the cache means each is only ever paid
  for once.

**What that first pass actually found**, over the 40 unresolved rows on the current
dashboard (35 companies): 10 resolved — 6 Ashby, 1 Greenhouse, 2 Lever, all of which now
fill automatically, plus 1 SmartRecruiters that comes back as a direct link. 13 more have a
board with no matching title on it, and you're told that rather than told the role is dead.
The remaining 17 are companies with nothing public to find. Across the whole file that took
rows with a known board from 119 to 129, and auto-fillable rows to 59.

**The record itself is checked before any of that, and it's free.** Dedupe keeps one URL
per role by source rank and files the rest under `also_seen` — so a role seen on both
revopsroles and hiring.cafe keeps the revopsroles advert as its `url` and puts the real
`workable.com` application in a field nothing was reading. `/submit` reads every URL a row
carries before it looks anything up: a link a driver can fill wins, then a link to a real
application system (even one without a driver — still worth handing you), then the advert
it was found on. No network call, and it alone resolves 84 of the 433 rows in the current
scan whose own link isn't a form. And when none of that lands: **`/submit <link>`** points
it straight at whatever form you found by hand — a link you paste beats anything a lookup
can infer, so it's used outright and nothing is searched for.

**The dashboard shows the last 7 days.** A role you've sat on for a week isn't news, and
471 rows is not a list anybody reads. It's a view filter only: `docs/jobs.json` keeps its
45 days, because the scan dedupes each run against everything already on the dashboard and
a row deleted from that file is a role that comes back tomorrow looking new. The header
line says how many are hidden, so nothing disappears quietly. The excluded log gets the
same cut.

**Where the application actually is, on the card.** Where the scan's board lookup found
the form itself, the card carries an **Apply →** link straight to it, next to the "View
posting" link that often opens nothing but an advert. The **ATS badge that used to sit
beside it is gone**, along with the "Auto-fillable only" filter: both answered "could the
bot fill this form", and the bot is mothballed — applications are made by hand through the
`job-application-workflow` skill now. `scan.py` still writes `ats` / `ats_fillable` on every
row (the apply-link state is derived from them, and the mothballed code reads them); they
are simply not something worth reading on a card any more.

**Shot and Want, under the score.** The six dimensions answer two different questions, and
the weighted total blends them into one number that cannot tell them apart. Stripe's GTM
S&O Analyst scored 6.8 and Qualio's Senior CSM 6.5 — opposite problems: the Stripe role is
one Tom badly wants and would probably not be screened for (advanced SQL and BI required),
the Qualio one he would likely be screened for and does not much want. Split by the
rubric's own weights they read **5.6 / 8.6** and **7.4 / 5.1**.

```
Shot = (experience*25 + skills*20 + seniority*15) / 60     can I get the interview
Want = (domain*15 + location*15 + trajectory*10) / 40      do I want the job
```

No extra model call: it is arithmetic over dimension scores already stored, so every row
already scored has both (`shot_want()` in `scan.py`, written onto each row, with the weights
also published in `status.json` so the page can derive them for a row stored before the
split existed). Next to them, **hard gaps** — the posting's own stated must-haves the
profile does not meet, quoted in under 12 words, asked for as a list in `SCORE_SCHEMA`
rather than left as prose in the flags. They render first in the tag row, travel in the
Copy-for-Claude block, and are where the skill's bullet audit now starts.

**Filters, and what's new.** A sticky bar at the top: market chips (All · NL · IE · London ·
BE · CA · US) and track chips (All · RevOps · CS), each with a live count of Apply and
Borderline roles only (below-floor and unscored rows are not counted) taken *before* the
other filter applies, plus a **Shot 6.5+** toggle that never hides an unscored row. `track`
is written onto every row by `scan.py` from the same CSM title test the flags use, so the
page keeps no second copy of it. Every role found since the last visit carries a dot, and
the header says how many and since when — with three scans a day, that is the question
every visit starts with. **Hidden** and **applied** are two sections now, each newest-marked
first with the date on the card; Hide and Mark applied record a timestamp and sync it
alongside the id lists.

**Risk notes read as sentences.** The scorer's flags were pills cut at 70 characters, and a
pill cannot wrap — 104 of 137 cards were wider than a 375px screen. Short facts (market,
pay, hard gaps, title band, the location conflict, the sponsor badge) stay pills and now
wrap; the scorer's notes are a **Watch-outs** list under the verdict, at full length, with
the stored cap raised from 70 to 160 characters.

**Every board it can fill runs an invisible bot check on submission, and nothing here
tries to get past one.** Greenhouse loads reCAPTCHA Enterprise into every application page;
Ashby loads an invisible reCAPTCHA v2 with a hidden response field on the form; Lever runs
hCaptcha. Different vendors, one rule. That check exists to put a
person behind a submission, and a job application is exactly the kind of submission an
employer is entitled to want a person behind. So the check is detected and named instead:
in the preview, while you are still deciding whether to send, and again if a submission
goes through unconfirmed, where the message tells you the check may simply have refused an
automated send and points you at the packet, which already has every answer written down to
paste in by hand. Whether a real send clears the check is not knowable by reading, and your
first live `/send` on a Greenhouse role is what settles it.

Boards it can fill: **Greenhouse**, **Ashby** and **Lever**. Each needed its own handling,
all of it mapped in `tools/notes/boards.md`:

- **Greenhouse**, including regional boards like `job-boards.eu.greenhouse.io` and
  employers whose board URL redirects to their own careers site with the form embedded in
  it.
- **Ashby**: the form is a page on from the posting (`/application`), one field takes the
  whole name, a yes/no question is two buttons over a hidden checkbox rather than a
  dropdown, and a second id-less file input sits above the real one feeding Ashby's resume
  parser, so the CV goes to `#_systemfield_resume` by id and never to `input[type=file]`
  generally.
- **Lever**: the plainest markup of the three and the one that exposed two bugs in the
  shared half. Its inputs have a `name` and no `id` at all, so a fill that only ever
  addressed fields by id matched nothing on every Lever form; and its radio groups have no
  ids either, so a four-option question read as one option. Both are fixed for every board.
  On top of that, a Lever custom question is written once *above* the radios that answer it
  (so the question is read off the page, not off the field), and "current location" is a
  typeahead where typing the city is not choosing it — the form submits a hidden
  `selectedLocation` that stays empty until a suggestion is clicked.

**SmartRecruiters is read but not driven, and that is deliberate.** Its board API is in the
lookup above — it is how ServiceNow's London and Dublin roles stopped showing as "no board
found" — but its apply form lives on a separate host behind DataDome bot detection: a probe
of it returned zero controls and a captcha iframe, so there is nothing to read, let alone
fill. Workday and LinkedIn Easy Apply are left alone for a different reason: both want an
account and a logged-in session, which is a credential sitting in a runner and a different
conversation. All of these are still found and handed to you as a direct link, which beats
a LinkedIn page you have to search from, and the CV and the letter are already in the bank
with the letter's text in the packet for pasting into a box.

**A caution about counting boards.** SmartRecruiters answers `200` with `totalFound: 0` for
a slug that does not exist rather than `404`, and `google.recruitee.com` resolves to a demo
account with one sample posting on it. Reading status lines alone, it is easy to conclude a
board was found for nearly every company; the code treats an empty board as no board for
exactly that reason.

**A question nobody can read is never sent to a model.** Some boards put the question in
text above the control rather than in a label. Where that text can't be found, the honest
answer is that nothing here knows what is being asked, so the field goes to you as a blank
on the printed form, where you can read it yourself, rather than to a model that would
answer it off the option list alone.

**Bot commands:** `/apply <id>`, `/queue`, `/status`, `/cancel`, `/phone`, `/market`, `/redo`,
`/cover`, `/submit`, `/send`, `/help`.

## The CV build

The back half of the same run. Once the packet exists, the role carries straight on into
two more stages, and only one of them ever asks anything.

4. **CV** — a short **company strategic brief** (web search: what the company is trying to
   do in the next twelve months, with the evidence for each claim), then one tailoring pass
   that turns the audit into the actual contents of a page: which bullet goes where, how a
   REVISE is worded, three summary variations scored out of 10, and the skills line.
5. **Pick** — **only on roles scoring 7.5 or better** (`VARIATION_REVIEW_MIN_SCORE`, a
   named constant because it is a volume dial, not a rule). Above the line you get the
   three summaries with their scores and reply with a letter. Below it the highest-scoring
   one is taken, and you are told which and what the others scored. Either way it is at
   most one more wait.

Then the CV is rendered, checked, and only then sent. **PDF only** — the .docx is an
intermediate and is never delivered. It arrives in Telegram as a file, and lands in the
private bank at `cv/YYYY-MM-DD-company-title.pdf`.

**A model chooses the words; code chooses the layout.** Section placement, project order
per track, the role-title fallbacks, US Letter, 0.75in margins, Calibri, the teal accents
and the right-aligned tab stop at 10080 twips all live in `cv/build-cv.js` and `cvbuild.py`,
where nothing a model returns can move them. `docx` builds the file, LibreOffice headless
converts it, and `pdftoppm` renders the pages. The cover letter has its own renderer,
`cv/build-letter.js` with `coverletter.py` beside it, on the same terms and sharing that
same docx to PDF to JPEG path.

**Two standing rules are enforced, not requested.** At most **six bullets on any one
job**, and a summary that fits **four printed lines**. Both are in the skill and both were
in the tailoring prompt, and the first CV that shipped had eight bullets on NAVEX and a
six-line summary, because asking is not the same as guaranteeing. The bullet cap runs on
the way in and again on the way out; anything past the sixth is listed in the packet rather
than silently lost. The summary is measured off the rendered page (`pdftotext -layout`
preserves the real line breaks, and whether a sentence wraps is a question about Calibri's
metrics, not about character counts) and, if it runs long, loses its last sentence and
re-renders. Dropped, never rewritten: a rewrite would be new text arriving after the
honesty screen had already passed on it. You are told what came off.

**One employer, one entry.** LexisNexis is a single company Tom worked at twice, rendered
as one header line with two titles under it. Rendering it as two employers turns an
11-year career into a job-hopping one. A project is not an entry either: it is one bullet
with a bold lead-in, and a tailored rewrite replaces the body while the name stays put.

**Nothing ships unlooked-at.** Every build renders the PDF to JPEG at 90dpi and measures
the result: page count, embedded fonts, and — the one that matters — the actual glyph
position of every date and location, straight out of the PDF, checked against the right
margin to within 8 points. A silently broken tab stop produces a CV with the dates in the
wrong place, and it looks perfectly fine to the code that made it. A build that fails its
checks is **not sent**; you get the list of what failed instead, and the PDF is still kept
in the bank so you can see it.

**The CV's text is not printed into the Actions log.** This repo is public and so is its
log; the bullets come out of the private bank. The log gets the measurements and the page
*structure* — headings, entry headers, bullet counts — which is what catches a layout
break. The pages themselves come to you on Telegram with the PDF. When something needs
diagnosing, run the workflow by hand with **cv_debug** ticked: that one run prints the full
text and keeps the page images as an artifact.

**The honesty rule, extended.** Phase 1's version: a gap you didn't answer produces no
bullet. This phase's version: every number in a bullet must already appear in the bank, the
base CV, or your own answers, and the wording has to be traceable to one of them. The
posting is never evidence. A bullet that fails is dropped from the CV and listed in the
packet, because a missing bullet is visible the moment you open the PDF and an invented one
is not. The same screen runs again before anything reaches the bank — a fabrication on one
CV is one bad application, and the same fabrication promoted into the bank is every
application after it.

**Bullet bank write-back is autonomous**, one commit per role, no approval. It runs only
after a CV that passed its checks. New bullets from your answers always go back; a
tailored revision is promoted only if it clears all four of the skill's tests, and is
logged as a job-specific variant if it doesn't. A bullet cut on three separate roles is
retired automatically — that count is arithmetic this system keeps, not something a model
has to remember. Every pass bumps `Last updated:` and writes one CHANGE LOG row per change.

**The CV skeleton** is `cv-base.json`: who, where, when, the section structure, and the
base CV's own bullets. It is taken verbatim from the 2026-08-05 base CVs, so the dates,
locations, the two degrees, the euro sign in the Debic line and the fact that LexisNexis is
one employer with two titles are all exact. A rendered CV with no tailoring at all
reproduces `Tom_Norton_CV.docx` to the pixel, which is the test.

**Your phone number is not in this repo.** The seed's contact line has Barcelona, your
email, LinkedIn and nationality, and no number: this repo is public, and a phone number in
a public repo gets scraped in a way the same number on a CV sent to a named recruiter does
not. It lives in the bank's private copy of `cv-base.json` instead, and nobody edits JSON
to put it there:

```
/phone +34 700 000 000     put it on the European CV
/phone us +1 555 000 0000  put it on the Canada/US CV
/phone off                 take the European one off
/phone us off              take the US one off
/phone                     what's on there now
```

The bot rewrites the bank's copy and commits. The CV renders fine without a number, so
this is a nudge on the first build and never a blocker.

**Which of the two you get is decided by the role, not by you.** `contact_for()` returns the
Grand Rapids block with the US number for Canada and remote-US roles and the Barcelona block
for European ones, and `load_base()` takes the market as an argument rather than letting
callers swap contacts afterwards — the form's identity is read off that same skeleton by
`submit.identity()`, so one call site is what stops a Grand Rapids CV going out attached to a
form with a Barcelona address on it.

Which makes the market worth being able to correct, because a feed can get it wrong:

```
/market CA                 the role you're on right now is in Canada, whatever the feed said
/market us                 "us", "uk", "canada", "ireland" all work
/market az-nl-1 CA         correct a different role by id, not the one in flight
/market                    list the markets and what this does
```

No id needed for the common case — a bare `/market <market>` corrects whatever's in flight,
falling back to the last CV built if nothing is running. There is nowhere on the dashboard
an id is printed as visible text (it lives only in a `data-id` attribute behind the Apply
button), so requiring one for the role already in the chat would have made the command
useless for the situation it exists for. An id stays available for the other case — fixing a
role that is not the one currently in flight.

That writes a `state/market-overrides.json` in the bank, and `load_job()` applies it — the
one place every consumer reads a role from, so the CV, the letter, the form and the pay gate
all move together. Step 13b flags the ones worth correcting, and a flagged role is asked
about in the same batch as its gap questions, before any CV is built. Two things worth
knowing: the **dashboard row keeps the scan's reading**, because nothing in the apply
workflow can write to this repo, and the correction is keyed against every id the row
answers to, so one made against a duplicate survives the two being collapsed.

Those base bullets are the floor. A role the tailoring pass says nothing about keeps them
rather than going blank, and they count as a source the honesty screen will trace a
revision back to. The bank is still the master library and its CANONICAL text wins wherever
the two disagree. The copy in the private bank wins over the one in this repo, which is
only the seed.

**The bank's guards are binding.** `bullet-bank.md` carries SCOPE GUARDs and METRIC GUARDs
written after real gap interviews: what Tom did and did not do, and which numbers do not
exist. "Contributed his accounts to the renewal risk forum, did not run it" is the
difference between a defensible bullet and one that falls apart in the first interview
question. They go into the tailoring prompt as rules, not context.

**Not in this phase.** Autonomous submission is Phase 4. The hiring-manager outreach email
the skill describes is out of scope entirely.

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

## The two Claude skills

The application half runs in a Claude project and **its files live there, not in this
repo**: `job-application-workflow` builds the brief, the bullet audit, the tailored CV and
the outreach email, and `score-role` reproduces this scorer in chat for a role the radar
never saw. They stay out of here for a plain reason — the application skill carries the
bullet bank's PAT, and this repo is public.

They are still a contract with this code, and it is a one-way one: change something on this
list and the skill in the project has to be edited to match, because nothing here can check
it for you.

| Here | What the skill does with it |
|---|---|
| `packetFor()` in `docs/index.html` | Writes the RADAR ROLE block the application skill parses line by line, including the `Shot` / `Want` / `Hard gaps` lines |
| `score_flags()` and `parse_score_result()` in `scan.py` | Decide the flag wordings its Step 0 table matches on; an unmatched line makes it announce that the dashboard has changed |
| `cvbuild.contact_for()` / `NA_MARKETS` | Decides which contact block a market takes (Barcelona for NL/IE/UK/BE, Grand Rapids and no nationality line for CA/US) |
| `profile.md` and `RUBRIC` in `scan.py` | What `score-role` scores against; it fetches both at run time rather than remembering them |
| `shot_want()` in `scan.py` | The split both skills report |
| `bankwrite.py` | The bullet bank's writer, called by the application skill's Step 9d |

## Tests

```
python tests/test_scoring.py    # pure functions: weighted total, flags, title gate, location, dedupe
python tests/test_apply.py      # comp gate, answer handling, the apply state machine across ticks
python tests/test_cv.py         # layout policy, the honesty screen, the review gate, bank write-back
python tests/test_cover.py      # the letter: the two honesty screens, the one-page trim, /cover
python tests/test_submit.py     # the form: what code fills, what nobody fills, and that nothing sends
node tests/test_worker.mjs      # the Worker: its two guards, and the scan schedule incl. DST
python tests/preview_messages.py # print every bot message as Telegram renders it (no asserts)
python tests/test_cv_render.py --install  # the real render: docx -> PDF -> JPEG, measured
python tests/test_submit_form.py --install  # a real browser filling, printing and submitting a form
python tools/probe_form.py <url>  # dump a real form's structure, to write a driver against it. Reads only.
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

`test_cv_render.py` is the odd one out: it needs LibreOffice, poppler and the Carlito font,
which is two minutes of apt, so it is not in the 15-minute tick. It runs on push instead,
in the **CV render smoke** workflow, on any change that can move the layout — and it leaves
the rendered pages behind as a run artifact, because the last check on a CV is a person
looking at one. It also renders a deliberately bad page, so the checks are known to fail on
something rather than merely known to pass.

`test_submit_form.py` is the same idea for the form fill and runs in the **Form fill
smoke** workflow. It drives a real Chromium over `tests/fixtures/application-form.html`, a
stand-in built to carry the shapes a real board uses, and never over an employer's form —
a test that submits a real application is a real application. What it proves is the half a
stub cannot reach: that a dropdown opens and gives up its options, that a hidden file input
takes a path, that the printed page still has the answers on it, and that a form which has
changed since it was approved is refused rather than guessed at.

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

### Why the schedule lives in Cloudflare

GitHub does not honour the cron in `scan.yml`. Over 20 Aug – 3 Sep 2026 every scheduled
firing arrived late and the lateness grew: 19–46 minutes in the first week, 2h12m to 5h13m
by 1–3 Sep, and a stretch on 27–28 Aug where the morning run landed after 19:00. The same
scheduler gave `apply.yml`'s 15-minute cron 35 firings of an expected ~576 over six days.
The expressions are correct; scheduled workflows are best-effort and this repo is being
deprioritised. What that costs is not a missed source but a wrecked cadence: three runs a
day spread across the working day becomes three runs bunched into the afternoon and
evening, and every new role is seen hours later than it was posted. The morning slot has a
second constraint on top, which is that it must stay *after* the revopsroles.com email
(10am Amsterdam, 4am Eastern); 8am Eastern clears it with hours to spare.

So the clock moved to the Worker, which already held a GitHub PAT for the Telegram relay.
Cloudflare's cron triggers fire on time; `scheduled()` checks whether the firing minute is
one of the scan times in America/New_York and, if so, dispatches `scan.yml` immediately. Because
the local time is computed at firing time rather than baked into a UTC expression, the
clocks going back on 1 Nov needs no change — the triggers deliberately cover both offsets and only one matches per day.

**The GitHub cron stays as a backstop, and now stands down when it is not needed.** Both
schedulers were firing: the Worker on time, GitHub's cron 20 minutes to 5 hours later. On
17 Sep that was four runs — 13:00 dispatch, 13:23 schedule, 17:22 schedule, 18:00 dispatch —
where the second of each pair found nothing new and still paid for five Apify actor calls
and up to 60 Adzuna ones, with Adzuna answering 503 on the later run almost every day
(a free-tier quota already spent). So a **scheduled** run now exits before the scanner if a
scan committed inside the last two hours. A `workflow_dispatch` never stands down — asking
for a run by hand means you want one now — and if the Worker stops firing, nothing commits
and the next scheduled run goes ahead, which is the whole point of a backstop.

**Deploying is merging.** `.github/workflows/worker-deploy.yml` uploads `worker/` to
Cloudflare on any push to `main` that touches it, so a schedule change goes live when the PR
carrying it is merged. There is nothing to run by hand, which is the point: this project is
worked on from Claude Code cloud sessions and the GitHub web UI, and a deploy step needing a
laptop and a local clone is a step that would never get run. It can also be fired on demand
from Actions → "Deploy relay" → Run workflow.

That upload is the *only* way the schedule changes. Cron triggers are registered as part of
deploying the Worker, so editing `wrangler.toml` on `main` and not deploying leaves the old
schedule running with no sign anything is stale.

**One secret makes it work**, added once at Settings → Secrets and variables → Actions:

- `CLOUDFLARE_API_TOKEN` — from Cloudflare → My Profile → API Tokens → Create Token → use
  the **Edit Cloudflare Workers** template. That template's permissions are what a deploy
  needs and nothing more.
- `CLOUDFLARE_ACCOUNT_ID` — only if that Cloudflare login owns more than one account, in
  which case the deploy will say so. Otherwise skip it.

Until `CLOUDFLARE_API_TOKEN` exists the workflow explains itself and exits **green** rather
than failing on every push, on the same reasoning as `apply.yml`'s no-secrets no-op: a red
cross that means "not configured yet" teaches the Actions tab to be ignored.

**What a deploy does not touch.** `GH_TOKEN` and `TELEGRAM_SECRET` are attached to the
Worker by name rather than to the upload, so they survive and never need re-entering. The
Telegram webhook survives too, since it points at the URL and the URL does not change. The
`name` in `wrangler.toml` is what decides that URL and must stay `revops-relay` — the
webhook and the dashboard's Apply button are both pointed at
`revops-relay.tomnorton92.workers.dev`, and deploying under a different name would quietly
create a second Worker that nothing calls.

**Confirming it took:** Cloudflare → Workers → `revops-relay` → Settings → Triggers → Cron
Triggers should list `15 8,9 * * *` and `0 13,14,18,19 * * *`. The real proof is the next
morning: the "Daily job scan" run should start at 08:15 UTC rather than four hours later.

If the Worker is undeployed or broken the scan still happens, just late — that is what the
GitHub cron is left in place for. `scan.yml` has a `concurrency: scan` group so a backstop
firing that overlaps a Worker-triggered run queues behind it instead of racing it to `git
push`.

## The sponsor check: read this

Registers list **legal** names ("Adyen N.V."); postings show **trading** names ("Adyen").
- **"not on register"** means the name didn't match — usually true, sometimes a legal-vs-trading
  mismatch. It's a caution flag, not a delete, and the deep score treats it as a small penalty.
- **UK** is reliable (daily CSV, ~126k sponsors). **NL** is best-effort (IND monthly register);
  add companies you care about to `nl_sponsors_extra.txt` (one per line) to firm it up.
- **Ireland** has no sponsor register (it uses employment permits), so Irish roles show no
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

- **Secrets must never reach the status footer.** `docs/status.json` is committed and pushed
  on every run, so anything written into it is published. A failing source writes its
  exception text there, and `requests` puts the full request URL — query string included —
  into the text of every exception it raises. On 6 Sep 2026 an Apify 429 did exactly that:
  the token went into `status.json`, GitHub push protection rejected the push, and the scan
  then failed at its final step on every run for three days while the scan itself worked
  perfectly. The dashboard just stopped moving. Two guards now: credentials go in an
  `Authorization` header rather than a query parameter (`fetch_apify_hiringcafe`), and every
  status value passes through `redact()` before the file is written. Both are covered by
  `tests/test_scoring.py`. If you add a source, use header auth — a blocked push is silent
  from the dashboard's side and looks exactly like a quiet market.
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
- **revopsroles.com** depends on Tom's Gmail subscriptions staying active and the email's
  HTML layout not changing; it parses those emails rather than the site itself (see above).
  Field extraction is pinned by a fixture rather than trusted, because this source has
  already failed silently once: a restyle emptied the salary field on every row for as long
  as nobody was relying on it. If a digest stops arriving or its markup changes, this source
  goes quiet the same way the others do; the footer names each email separately so two
  alerts read as two, and reports how many rows carried a salary.
  **The site itself is closed to this pipeline and will stay closed.** Every path on
  `revopsroles.com` returns Vercel's challenge to a datacenter IP — job pages, `/api/*`,
  even `/robots.txt` — so the "apply to this job" link that leads to the real ATS cannot be
  read from a runner, and no amount of user-agent work changes that: the block is on the IP,
  not the client. That makes the digest a **pointer** permanently, and the value of the
  source is entirely in what it points at. Dropping it would cost the 15 of 34 current rows
  that no other source carried; keeping it is only defensible because 12a, 12b and 12c go
  and fetch the ad the digest cannot give.
- The status footer at the bottom of the dashboard shows exactly what each source did each run —
  check it if results look thin.
