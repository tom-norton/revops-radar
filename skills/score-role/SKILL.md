---
name: score-role
description: "Score a single job posting against Tom's profile using the RevOps Radar's own rubric, gates and hard disqualifiers, and report the number the radar would have given it. For roles the radar never saw, or saw and could not read. Use this skill ONLY when Tom explicitly asks for a score or a verdict: 'score this', 'score this role', 'what would the radar give this', 'should I apply', 'is this worth applying to', 'rescore this', 'the radar got this wrong', 'run the numbers on this'. Do NOT use it for a job description pasted without that ask, and do NOT use it to build an application: a bare pasted JD, a job URL, or a RADAR ROLE block with no scoring question attached belongs to job-application-workflow, which builds the CV, the brief and the outreach email. This skill produces a score and stops. It never tailors a CV, never writes a bullet, and never drafts an email."
---

# Score a Role

Reproduces the RevOps Radar's deep-scoring stage in chat, for the two cases the pipeline
misses:

1. **The radar never saw the role.** Tom found it himself, or it came from somewhere the
   radar does not cover.
2. **The radar saw it and could not read it.** A revopsroles.com row arrives as about fifty
   characters of metadata whose link refuses datacenter IPs, so the scorer honestly gives it
   5s across the board and it lands below the floor. The People Data Labs Revenue Operations
   Specialist role is the worked example: 57 characters of posting, scored 5.2, while the ad
   itself states $140k-$155k and the title is core RevOps.

**This skill produces a score and stops.** It does not tailor a CV, audit bullets, research a
company or draft an email. When the score clears the gate and Tom wants the application,
that is `job-application-workflow`, invoked separately.

---

## Before you start

Fetch both of these. The radar repo is public, so neither needs a token.

```bash
curl -sS https://raw.githubusercontent.com/tom-norton/revops-radar/main/profile.md \
  -o /tmp/profile.md
curl -sS https://raw.githubusercontent.com/tom-norton/revops-radar/main/docs/status.json \
  -o /tmp/status.json
curl -sS https://raw.githubusercontent.com/tom-norton/revops-radar/main/scan.py \
  | sed -n '/^RUBRIC = \[/,/^RUBRIC_KEYS/p' > /tmp/rubric.py
```

**`profile.md` is the contract.** It is the identical file the live scorer interpolates into
its system prompt, so reading it means scoring against the same candidate the radar scores
against. Everything about Tom, the markets, the salary floors, the seniority guidance, the
per-market Location and Visa score bands and the CSM track weighting lives there. Do not work
from memory, and do not restate its numbers from this file: read them.

**`status.json` carries the live bands.** Use its `gate` and `floor` (6.5 and 6.0 at the time
of writing, but read them rather than assuming) to say which section of the dashboard this
score would land in.

**`RUBRIC` is the scorer's actual dimension guidance**, and it lives in `scan.py`, not in
`profile.md`. `score_system()` interpolates those six guidance strings verbatim into the
system prompt the live scorer runs on. `profile.md` mirrors most of it but not all: the
numeric anchors for Domain (B2B SaaS 8-10, non-tech 4-6), the instruction to judge tools and
functional skills separately under Skills, and the hard-prerequisite versus soft-gap
distinction under Experience exist only in `RUBRIC`. Read `/tmp/rubric.py` and score against
its text. It is Python, so read it as text and ignore the quoting.

If the profile or status fetch fails, say so plainly and stop. A score built on remembered
visa thresholds is worse than no score, because it looks authoritative. If only the rubric
extraction fails, say so in one line and fall back to `profile.md`'s sections, which are
close but not identical.

---

## Step 0: Get the real posting

**A score built on a stub is worthless. That is the entire reason this skill exists.**

- Tom pastes a URL: fetch it and read the actual ad.
- Tom pastes text under about 900 characters: that is the radar's own threshold for "this is
  not really a posting". Say so and ask for the full ad. Do not score it.
- A posting is never inferred. If the ad does not say what the scope is, the scope is unknown,
  not "what a role with that title usually involves". Titles mislead, which is the whole
  reason the pipeline reads postings at all.

**The radar agrees with this now.** It used to score a thin row honestly at 5 to 6 with the
missing evidence named in the verdict; it no longer scores one at all. A row whose posting
could not be retrieved through any route is SET ASIDE — unscored, not rejected, in its own
section of the dashboard, carrying `Radar score: NOT SCORED` in the Copy-for-Claude block —
because a number built from a title and a location is not a worse score, it is a different
kind of object, and putting it in the same ranked list makes the list lie.

That is exactly the case this skill is for. Tom came back with the real ad; score that. What
does not change is the rule above: if what he pasted is still under about 900 characters,
ask for the ad rather than producing the hedged number the radar deliberately stopped
producing.

---

## Step 1: Resolve the market

Do this first, because the title gate in Step 2 branches on the answer. Resolve to exactly one
of `NL`, `BE`, `UK-London`, `IE`, `CA`, `US-Remote`, or to nothing.

Apply these in order. The order is load-bearing:

1. **Any North American signal wins outright.** A US state, a US country name, a Canadian
   province or city. This is what stops "Amsterdam, NY" reading as the Netherlands. On a tie,
   Canada wins: "Ontario, CA" and "Vancouver, WA" both carry a US state code, and the more
   specific Canadian signal takes it.
2. **London includes the commuter belt.** `UK_LONDON` in `scan.py` is wider than the word
   "London" and the difference costs real roles: Greater London, City of London, Canary
   Wharf, Shoreditch, Croydon, Watford, Reading, Slough, Staines, Uxbridge, Richmond,
   Kingston, Bromley, Ilford, Romford, Enfield, Barnet, Harrow, Wembley, Hounslow, home
   counties, Surrey, Hertfordshire, Essex and Kent all resolve to `UK-London`. A Watford
   role is in, not out.
3. **A named non-London UK city, with no London signal beside it, is out.** Manchester,
   Edinburgh, Glasgow, Birmingham, Leeds, Bristol, Liverpool, Sheffield, Newcastle, Cardiff,
   Belfast, Nottingham, Leicester, Coventry, Brighton, Cambridge, Oxford, Aberdeen, Dundee.
   "Reading, Berkshire" is the one that goes the other way: written that way it is out,
   while a bare "Reading" is commuter belt and in.
4. **A named target city anchors the posting, even when the ad also says remote.** "Amsterdam,
   remote-friendly" is NL. This runs before the remote check on purpose.
5. **Only now, remote-anywhere or EMEA-wide wording with no city named is out.** So
   "Netherlands (Remote)" is out while "Amsterdam (Remote)" is in. That asymmetry is
   deliberate: a bare country next to remote wording is the remote-EMEA posting profile.md
   rejects.
6. **Then a bare country name** (Netherlands, Belgium, Ireland) resolves on its own, since
   plenty of real Amsterdam and Dublin postings list only the country.

Resolving to nothing is a **drop**, not a low score. That is the reject list in `profile.md`:
Germany, Spain, other UK cities, on-site or hybrid US roles, remote-from-anywhere, remote-EMEA.

---

## Step 2: The gates

The radar drops roles before spending anything on scoring them. Reproduce that, or this skill
will hand back a number for a role Tom cannot take. Every drop quotes the posting's own words.

### 2a: Title

- **US-Remote** uses a deliberately narrow gate. The title must be **core RevOps** (revenue
  operations, sales operations, CS operations, revenue or sales strategy, revenue analytics,
  revenue systems or technology, sales or revenue enablement, commercial operations, GTM with
  an operations/strategy/analytics noun beside it, sales or incentive compensation, territory
  planning or design, Strategy and Operations in any wording) **or** customer success **with
  a seniority qualifier** (Senior, Principal, Lead, Enterprise, Strategic, or a "Manager of /
  Head of Customer Success" construction). A plain "Customer Success Manager" in the US is
  dropped.
- **NL** is the one market that admits a plain "Customer Success Manager", because
  `profile.md`'s CSM track weighting makes an NL Senior or Principal CSM a primary target.
- **Everywhere else** uses the broad target-function gate: all of the core list above, plus
  business operations, marketing operations, growth operations, business strategy, renewals,
  and senior or enterprise-qualified customer success.
- **Excluded in every market**, whatever else the title says: deal desk, quote-to-cash, order
  management, billing specialist, internship, working student, apprentice, graduate scheme,
  and VP / SVP / EVP / Vice President / Chief.

### 2b: The US salary rule

**A US posting that states no salary is dropped before scoring.** Not scored low, dropped.
The reasoning in `profile.md` is that the US is a financial-runway backstop rather than
somewhere Tom wants to live, so it is only worth taking at pay that is confirmed rather than
hoped for.

Only a figure the **posting itself** states counts. A job board's estimate is never a stated
salary. Report the annual **base**, excluding bonus, commission, equity and holiday
allowance.

**Report both ends of the range, not just the bottom.** A posting is not an offer, and the
whole negotiation happens inside the band, so the radar's floor check reads the top. A bare
bottom figure is what made `$120,000 - $150,000` look like a $120,000 role and threw it away
against a $130,000 floor. A single figure rather than a range goes in both ends. See Step 6.

### 2c: Sponsorship ruled out

Drop when the ad says it will not sponsor. Be strict about what counts: the negation has to be
bound tightly to the sponsorship word. "We are unable to offer visa sponsorship" fires. "We
have no restrictions on visa sponsorship" and "we are happy to sponsor" must not, and getting
this wrong throws away a role Tom would have wanted.

### 2d: Another language required

Drop when the ad makes a language other than English a hard requirement to do the job:
fluent, fluency, native, mother tongue, business-level, professional proficiency, must speak,
required, mandatory, essential.

**English is never a disqualifier.** Neither is a language that is preferred, a plus, nice to
have, an advantage, an asset, desirable, beneficial, welcome, ideally, helpful or optional.
Neither is one that sits under a "Preferred qualifications" or "Nice to have" heading, even
when its own bullet reads like a requirement. Check the nearest heading above the mention
before deciding.

### 2e: Age

The radar drops postings older than seven days when the source gives it a date, and keeps
anything undated. Here, **report the age rather than enforcing it**. Tom is looking at this
role deliberately, and a nine-day-old posting he found himself is still worth a number.

---

## Step 3: Sponsor status

**Do not try to check the registers, and do not guess from a web search.**

The radar loads the gov.uk daily CSV (over 143,000 sponsors on the last run) and the IND
register (about 12,700 names). Those are multi-megabyte files, and the gov.uk file's URL is
rediscovered each run because its name carries the publication date. This is not something to
do from chat.

So report sponsor status as **unknown** for UK and NL roles and hand Tom the links:

- UK: https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers
- NL: https://ind.nl/en/public-register-recognised-sponsors/public-register-work

Tell him the register lists the **legal** name, so search for "Adyen N.V." rather than
"Adyen", and that a company being absent is genuinely weak evidence because registers miss
trading names.

**Ignore sponsor status entirely for Ireland, Canada and the US.** Ireland runs employment
permits rather than a register, and Tom needs no sponsorship in either North American market.
Producing a sponsorship paragraph for those is noise.

Two consequences for the score, and say both out loud:

- **An NL role sits at the 7 band rather than the 9-10 band** until the sponsor is confirmed.
  Do not quietly pick one.
- **"Not on register" is a 1 to 2 point caution on Location and Visa, never an auto-zero.**
  The registers list legal names and miss trading names, so absence is weak evidence. This
  matters here mostly as a rule about what Tom finds when he checks the link himself: tell
  him an absent company is not a no.

---

## Step 4: Score the six dimensions

Score each 0-10 against **`/tmp/rubric.py`**, which is the scorer's own guidance text, with
`profile.md` supplying the candidate behind it. Be rigorous and honest; this decides whether
Tom spends a day applying.

| Dimension | Weight | Rubric key | Candidate facts in profile.md |
|---|---|---|---|
| Experience Alignment | 25% | `experience` | `## Experience` |
| Skills Match | 20% | `skills` | `## Skills` |
| Seniority Fit | 15% | `seniority` | `## Seniority fit` |
| Domain / Industry Fit | 15% | `domain` | `## Domain / industry fit` |
| Location & Visa | 15% | `location_visa` | `## Location & visa` |
| Career Trajectory | 10% | `trajectory` | `## Career trajectory` and `## CSM track weighting` |

**Calibration**, so the numbers land on the same scale the radar uses: 8-10 is a bullseye
worth applying to immediately, 7 a strong fit with manageable gaps, 6 borderline and worth it
only when the pipeline is thin, 5 barely at the bar, 4 and below not worth applying to.
**Do not inflate to be encouraging.**

Three rules that are easy to get wrong:

- **Every consideration lands inside a dimension.** If the posting reads junior, that is
  Seniority Fit. If the function is off-target, that is Domain and Career Trajectory. There is
  no separate penalty, no cap and no ceiling applied afterwards.
- **Location and Visa is a preference ordering over places to live, not a measure of how easy
  a market is to hire into.** Those come apart hardest on the US, where there is no visa
  problem at all and Tom still does not want to live there. Never score a market up for being
  administratively easy. The per-market bands are in `profile.md`; use them.
- **On-site and hybrid are normal in Europe and Canada.** Tom is relocating for the role and
  needs an employer with an office there, so an on-site requirement, a named-office
  requirement or the absence of remote flexibility is not a risk in NL, BE, UK-London, IE or
  CA. Do not score it down and do not flag it. **The US inverts this**: a US role only
  qualifies as remote in the first place, so a US posting that turns out on reading to
  require regular office attendance is worth a flag.
- **A posting that states no salary is not penalised on salary.** Judge comp risk from the
  seniority and the company instead, and note it. Silence about pay is the normal case in
  Europe, not a warning sign.
- **A location disagreement goes in `posting_location` and nowhere else.** Record what the ad
  says, score the market resolved in Step 1, and mention the disagreement once in the report.
  It never moves a dimension score and never becomes a flag reason of its own.
- **Judge seniority from the posting, not the title noun.** Analyst, Specialist, Associate and
  Coordinator titles routinely carry manager-level scope at a strong employer. Mark the level
  down only when the posting itself reads junior: 0-3 years wanted, execution-only or admin
  duties, no ownership of a system or process, reporting into a Manager. A 2-4 year experience
  ceiling screened against 11 years plus an MBA is a real filter risk, and that is worth 4-6
  on this dimension, but it is a single penalty for overqualification risk only. Do not also
  dock the comp on the strength of the title.

### Also report these observations

The live scorer is constrained to produce exactly these, so produce them too:

- **function_match**: `core` for RevOps, GTM strategy, sales ops, CS ops, revenue or sales
  strategy, or a Senior/Principal CSM role. `adjacent` for a related commercial-ops role that
  is not quite one of those. `off_target` for deal desk, quote-to-cash, billing, pure
  marketing-ops admin, quota-carrying sales, engineering or finance.
- **company_standout**: true only for a genuine tier-1 SaaS or strong-brand tech company.
  This is what decides whether a CSM role outside the Netherlands gets flagged.
- **language_hard_requirement**: true only on a hard requirement, per Step 2d.
- **salary_stated / salary_min_base / salary_max_base / salary_currency**: the bottom and the
  top of the stated annual base range, as numbers, with the ISO currency code. A single
  figure goes in both. Zero, zero and empty when the posting states nothing. **Leaving the
  top at zero is the specific error to avoid**, because Step 6 reads it.
- **posting_location**: the work location the posting states, copied as written. A
  transcription, not an opinion. Empty when the ad genuinely does not say.

---

## Step 5: Do the arithmetic, and show it

```
total = (experience*25 + skills*20 + seniority*15 + domain*15 + location_visa*15 + trajectory*10) / 100
```

Clamp each dimension to 0-10 first, then round the total to one decimal. **Show the
multiplication**, so Tom can check it rather than trust it. Nothing clamps the total
afterwards.

Then the same six numbers split in two, which is what the radar now stores on every row and
shows on every card (`shot_want()` in `scan.py`). Same weights, renormalised within each
half:

```
shot = (experience*25 + skills*20 + seniority*15) / 60     can he get the interview
want = (domain*15 + location_visa*15 + trajectory*10) / 40  does he want the job
```

Report both alongside the total. They are the difference between a role worth fighting for
on the CV and a role worth applying to in ten minutes, and the blended number cannot tell
them apart: a 6.8 that is Shot 5.6 / Want 8.6 and a 6.5 that is Shot 7.4 / Want 5.1 are
opposite problems.

**Hard gaps** go with them: the posting's own stated must-haves that the profile does not
meet, quoted in under 12 words each. The radar asks its scorer for these as a list now, so
report them as one — they are what a recruiter screening the CV stops on, and they are what
`job-application-workflow` starts its bullet audit from. "None" is a real answer.

---

## Step 6: The post-scoring disqualifiers

Three facts drop a role outright rather than lowering its score. Check them in this order,
and apply them after the dimensions are scored, exactly as `deep_score_disqualifier()` does.
Order matters only because a role failing more than one can be logged under a single reason.

1. **A hard non-English language requirement.**

2. **A US role where the full read found no stated salary.** Step 2b catches most of these
   up front, but a US ad that mentions money somewhere (an ARR figure, a stipend, an equity
   note) can pass that gate and still state no salary for the role itself. Once the posting
   has been read properly and no base is stated, drop it.

3. **A stated band entirely below the market's floor.** The floors are in `profile.md` under
   `## Location & visa` and `## Salary handling`. In Europe the floor is a visa threshold, so
   a role below it is one Tom cannot legally take. In the US it is his own floor. **Canada
   has no floor at all**, so a Canadian role can never be dropped on salary.

**The top of the band decides, not the bottom.** This is the rule most likely to be got
wrong, and getting it wrong throws away good roles:

- Band entirely below the floor (`max < floor`): **dropped.** No amount of negotiating gets
  there.
- Band straddling the floor (`min < floor <= max`): **kept and flagged.** Most of the range
  clears, and the negotiation happens inside it. The flag reads like `band 120000-150000 USD
  starts below your 130000 USD floor, only the top half clears it`. Tom decides.
- No salary stated: nothing to check. Not a drop, not a penalty.

**Never convert currencies.** If the posting states a figure in a currency that is not the
floor's currency, the comparison does not happen and the role is not dropped on salary. A
GBP number is never measured against a EUR floor.

None of these gets folded into a dimension. Do not soften the reading of any of them because
the rest of the role looks good. A wrong "no" here puts a role in front of Tom that he cannot
take; a wrong "yes" throws away one that was fine.

---

## Step 7: Report

```
## [Company]: [Title]

**Score: X.X / 10**  (radar gate 6.5, floor 6.0; read the live values from status.json)
Would land in: Apply / Borderline / Excluded

**Market:** [NL / BE / UK-London / IE / CA / US-Remote], [how it resolved]
**Gates:** passed, or [which one dropped it and the ad's own words]

| Dimension | Score | Weight | Why |
|---|---|---|---|
| Experience Alignment | X | 25% | one line |
| Skills Match | X | 20% | one line |
| Seniority Fit | X | 15% | one line |
| Domain / Industry Fit | X | 15% | one line |
| Location & Visa | X | 15% | one line |
| Career Trajectory | X | 10% | one line |

**Arithmetic:** (X*25 + X*20 + X*15 + X*15 + X*15 + X*10) / 100 = X.X
**Shot X.X** (experience, skills, seniority — can he get the interview)  ·  **Want X.X** (domain, location, trajectory — does he want it)
**Hard gaps:** [the ad's own stated must-haves he does not meet, each under 12 words, or "none"]

**Comp:** [stated base range and currency, both ends, or "not stated"], [top of band against the market floor]
**Sponsor:** [unknown, with the link, for UK/NL only]
**Flags:** [risk notes, or none]

**Verdict:** [one blunt sentence, 22 words maximum]
```

**Flags** are short risk notes, reproducing what `score_flags()` adds in code. The pipeline
tells its scorer not to write these because code appends them; here nothing else will, so
write them yourself. Add one for:

- an off-target function, or a title band (analyst, specialist, director-plus) worth a second look
- a CSM role outside the Netherlands at a non-standout company
- **a stated band that straddles the market floor**, in the wording from Step 6
- a US role that turns out to require regular office attendance
- anything else genuinely noticed in the ad

Do **not** flag a language requirement, a band entirely below the floor, or a missing salary.
The first two drop the role and the third is the normal European case.

**When the radar already scored this role**, say both numbers and what changed. For the
People Data Labs case that is: the radar scored 57 characters of metadata, this is the real
ad. A disagreement is the point of running this, not an embarrassment.

**When a gate dropped the role**, stop at the gate. Name it, quote the posting, and do not
produce dimension scores. A drop is not a low score.

---

## Non-negotiables

- **Never score a stub.** Under about 900 characters, ask for the real ad.
- **Never invent scope.** What the posting does not state is unknown, not typical.
- **Never guess sponsor status.** Unknown, with the link.
- **Never convert currencies** to compare against a floor.
- **Never drop on the bottom of a band.** Only a band entirely below the floor disqualifies;
  a straddle is a flag.
- **Never flag on-site or hybrid in Europe or Canada.** He is relocating for the role.
- **Never inflate.** A 5 is a 5. Tom asked for this number to decide with, and a generous one
  costs him a day.
- **Read `profile.md`, `status.json` and the extracted `RUBRIC` every run.** The floors,
  markets, bands and dimension guidance all move. This file does not carry copies of them on
  purpose, and the one place it does quote numbers (the gate and the floor in Step 7) is
  labelled as read-it-yourself.
- **Score and stop.** The CV, the brief, the bullets and the outreach email are
  `job-application-workflow`, and only when Tom asks for them.
