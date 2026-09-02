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

**Bot commands:** `/apply <id>`, `/queue`, `/status`, `/cancel`, `/phone`, `/redo`,
`/cover`, `/submit`, `/send`, `/help`.

**Answer questions however you like.** A reply that is nothing but letters is read in
code, exactly, with no model involved: `1a 2b 3c`, `a b c`, `C, c, b`, `1. a  2. c` all
work. Prose goes to a cheap model to be split per question, and anything that model
returns which is neither traceable to your own words nor one of the options you were
offered gets thrown away.

**Known limit: GitHub's cron is unreliable.** `*/15` is a request, not a promise, and in
practice it lands every few hours. One round trip per role keeps that to a single wait, but
the only real fix is pushing work in rather than polling for it — a Telegram webhook into
`workflow_dispatch`, which drops latency to seconds.

**Where things live.** State, answers, bullets and packets all live in the private
`tom-bullet-bank` repo. This repo is public and `docs/` is served by Pages, so nothing from
the bank is ever written into it. That makes `BULLET_BANK_PAT` a hard dependency: without
it there is nowhere to persist an interview that spans days, and the poller fails loudly
rather than proceeding.

## Applying

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
set with `/phone` reaches the form the moment it reaches the CV. Work authorisation comes
off your nationality and the market the role is in: a US citizen applying to a Dublin role
is not authorised there and does need sponsorship, and that is legal status rather than a
judgement call. The model is never shown those fields, so it cannot put a wrong answer in
one. What it does answer is the rest: "what excites you most about this opportunity", "how
did you hear about this job", and whatever else that particular form asks.

**Demographic questions are never answered.** Gender, race, ethnicity, veteran status,
disability: the model never sees them, and the code does not answer them either, beyond
taking the form's own "prefer not to say" when a required field will not accept a blank.

**Blank beats invented, and a blank stops the send.** Every written answer goes through the
same honesty screen as the CV and the letter, against the same sources; one whose claim
cannot be traced, or that carries a number from nowhere, is dropped and its field left
empty. A salary expectation you have not given is never guessed at. Anything required and
still empty is listed back to you, numbered, and `/send` refuses until you answer it —
reply `1 Dublin`, `2 three months` and it fills them in and prints the form again. Your own
answers go in as your words, unscreened, because the screen exists to stop a model
inventing your experience and you cannot invent your own.

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
Greenhouse, Ashby and Lever publish, and looks for the same role on it. Nine of the first
ten companies tried this way were found from the company name alone.

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

**What it actually resolves**, measured over 30 distinct LinkedIn-sourced companies in the
current scan: 12 found, of which 8 are Greenhouse and fill automatically and 4 are Ashby
and come back as a direct apply link. 4 more have a board with no matching title on it. The
remaining 14 are companies running their own careers stack (Google, Salesforce, Canva, Box,
Deel) where there is nothing public to find. So roughly a quarter of LinkedIn roles become
one-tap, and another eighth become a direct link instead of a LinkedIn page.

**Both boards run an invisible reCAPTCHA on submission, and nothing here tries to get past
it.** Greenhouse loads reCAPTCHA Enterprise into every application page; Ashby loads an
invisible reCAPTCHA v2 with a hidden response field on the form. That check exists to put a
person behind a submission, and a job application is exactly the kind of submission an
employer is entitled to want a person behind. So the check is detected and named instead:
in the preview, while you are still deciding whether to send, and again if a submission
goes through unconfirmed, where the message tells you the check may simply have refused an
automated send and points you at the packet, which already has every answer written down to
paste in by hand. Whether a real send clears the check is not knowable by reading, and your
first live `/send` on a Greenhouse role is what settles it.

Boards it can fill: **Greenhouse**, including the employers whose board URL redirects to
their own careers site with the form embedded in it. Ashby and Lever are found and handed
to you as a direct link, which still beats a LinkedIn page you have to search from. There
is no Ashby driver, and the reason is not that its form is hard: it was probed and mapped
(`tools/notes/ashby-form.md`) and it is more tractable than Greenhouse's. It is that a
driver whose submissions the reCAPTCHA refuses would produce a filled form nobody can send,
so it waits on the answer above.
Workday and LinkedIn Easy Apply are left alone entirely: both want an account and a
logged-in session, which is a credential sitting in a runner and a different conversation.
Either way the CV and the letter are already in the bank and the letter's text is already
in the packet for pasting into a box.

**Bot commands:** `/apply <id>`, `/queue`, `/status`, `/cancel`, `/phone`, `/redo`,
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
/phone +34 700 000 000     put it on the CV
/phone off                 take it off
/phone                     what's on there now
```

The bot rewrites the bank's copy and commits. The CV renders fine without a number, so
this is a nudge on the first build and never a blocker.

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

## Tests

```
python tests/test_scoring.py    # pure functions: weighted total, flags, title gate, location, dedupe
python tests/test_apply.py      # comp gate, answer handling, the apply state machine across ticks
python tests/test_cv.py         # layout policy, the honesty screen, the review gate, bank write-back
python tests/test_cover.py      # the letter: the two honesty screens, the one-page trim, /cover
python tests/test_submit.py     # the form: what code fills, what nobody fills, and that nothing sends
node tests/test_worker.mjs      # the Cloudflare relay's two guards
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
