---
name: job-application-workflow
description: "Produce the full application for a role Tom has already decided to pursue: company strategic brief, bullet audit (new bullets via structured interview), a tailored resume with scored variations, and a hiring manager outreach email to send right after applying. Does NOT score the role and does NOT research salary; the RevOps Radar scores and Tom requests comp separately. Reads the radar repo and writes the run back to the bullet bank. Cover letter only on explicit request. Use whenever the user pastes a job description, a job listing, a job URL, or a RADAR ROLE block from the radar dashboard. Also use when the user says 'run the workflow', 'full application', 'tailor my resume for this', or anything involving building an application for a specific role. Trigger even if the user just pastes a URL or raw text that looks like a job posting without explicitly asking. Do NOT use for general career advice, networking outreach unrelated to a role, or interview prep unless a job posting is provided."
---

# Job Application Workflow

Produces six deliverables by default: company strategic brief, bullet audit, new-bullet
drafts (if gaps exist), tailored resume (.docx) with scored variations, hiring manager
outreach email to send the same day as applying, and change log. Cover letter (Step 5) is
skipped by default; run it only if Tom explicitly asks.

**This skill does not score roles and does not research comp.** The RevOps Radar already
scores every role it finds, in Python, on a weighted rubric, and only surfaces the ones
worth looking at. Re-scoring here would be a worse copy of that (Step 0). Salary research
is Tom's to request: report what the radar already states about comp in one line and stop
there (Step 1).

**This skill is the only thing that builds applications now.** The radar's apply half
(`applyq.py`, `cvbuild.py`, `coverletter.py`) is mothballed, so nothing else is writing to
the bullet bank or the apply state any more. If this skill does not record a run, the
record does not exist. That is what Step 9 is for.

## Before You Start

Read ALL of these project files before beginning:

- `Tom_Norton_CV.docx` - Current resume (note: this is a markdown text file despite the extension)
- `STAR_Story_Bank_Tom_Norton.docx` - Interview stories and experience evidence
- `about-me.md` - Writing style guide (hard_refusals and decision_rules sections are critical)
- `anti-ai.md` - AI writing patterns to avoid
- `LinkedIn Profile.pdf` - LinkedIn profile for additional context

Also read the docx skill at `/mnt/skills/public/docx/SKILL.md` before creating any .docx files.

### Also read the Bullet Bank and the Answer Bank (required)

Git repo: **`github.com/tom-norton/tom-bullet-bank`** (private). Two files at repo root:
`bullet-bank.md` and `answer-bank.md`.

Token (fine-grained PAT, scoped to that one repo).

**The live token is in the copy of this file in the Claude project, and it stays there.**
It is not in this repo's copy and must never be: `revops-radar` is public, GitHub's push
protection blocks it, and a credential in a public repo is scraped within minutes. When you
re-upload this file to the project, paste the token back into the two places below, which
is the only editing step a re-upload needs.

```
<BULLET_BANK_PAT>
```

First use in a session, clone with the token in the URL:

```bash
git clone https://<BULLET_BANK_PAT>@github.com/tom-norton/tom-bullet-bank.git /home/claude/bank
```

If the repo is already cloned this session, pull instead:

```bash
cd /home/claude/bank && git pull
```

**The clone path is `/home/claude/bank` and nothing else.** Steps 3b, 9d and the commit
block all read from that path; cloning to `/home/claude/bullet-bank` and then reading
`/home/claude/bank` is how the cut history came back empty and the write-back failed.

Then read both files before Step 3. If the clone or pull fails, say so plainly and continue
from the project files alone. Do not silently skip it, because the bank holds better
versions of bullets than the base CV does, and the answer bank is the only record of what
Tom has already told previous runs.

If the token above has expired or been rotated, ask Tom for the current one and use it for
this session rather than stopping the workflow.

**`bullet-bank.md`** is the master library of reusable bullets; the base CV is only one
assembled instance of it. Where the bank and the base CV disagree on a bullet, **the bank's
CANONICAL version wins**.

Bank conventions:
- **Status:** `CANONICAL` (default pull) / `VARIANT` (job- or track-specific angle) / `RETIRED` (do not use without reason)
- **Tracks:** `ANALYTICS` / `BUILDER` / `CS` / `ALL`
- Bullets marked `[DRAFT]` have not been confirmed by Tom. Do not put a `[DRAFT]` bullet on a CV until he confirms the wording and any numbers in it.

**`answer-bank.md`** holds Tom's verbatim answers from every previous gap interview. It
exists so the same question is never asked twice. It is read in Step 3e and appended to in
Step 9. Entries look like this:

```
### [A-20260914-01] - pipeline hygiene
Asked: Any pipeline hygiene or CRM cleanup at NAVEX?
Answer: Not really, the data side went through an admin team.
Role: Vanta - Revenue Operations Manager (hc-ashby___vanta___abc123)
Date: 2026-09-14
```

### Also clone the radar repo (required)

Public repo, no token: **`github.com/tom-norton/revops-radar`**. Clone it rather than
curling one file, because two things in it are load-bearing here and they have to agree
with each other:

```bash
git clone --depth 1 https://github.com/tom-norton/revops-radar.git /home/claude/radar
```

**`profile.md`** holds the facts about Tom's markets, visa floors, seniority band and
claimable skills. The radar's own scoring reads it, which is why it stays current and why
this skill must never keep a second copy of those facts:

| Section | Used by |
|---------|---------|
| `## Location & visa` | Step 1 (the floor the radar's comp line is judged against), Step 2 (whether sponsorship is even a question), Step 4a (which market's framing the CV takes) |
| `## Skills` | Step 4e (the pool to build the skills line from, **and its "Deliberately NOT claimed" list, which is a hard do-not-add**) |
| `## Seniority fit` | Step 3b and Step 4 (judge the level from the posting, never from the title noun) |

**`bankwrite.py`** is the bullet bank's writer, used in Step 9d. It exists because the bank
is parsed by every future run, so an entry written in a slightly different shape is a
bullet that quietly stops being found. Do not hand-write bank entries when this is sitting
in the repo.

If the clone fails, fall back to
`curl -sS https://raw.githubusercontent.com/tom-norton/revops-radar/main/profile.md -o /home/claude/profile.md`
and say plainly that Step 9d will need hand edits this session. If that fails too, ask Tom
for the current floors rather than working from remembered numbers. Stale visa thresholds
are the kind of error that looks fine and wastes a week.

---

## Workflow Overview

Two user touchpoints:

1. **New-bullet interview** (Step 3e, only if gaps exist that the answer bank cannot already
   answer) - use `ask_user_input_v0` to surface real material; never invent experience.
2. **Batch review** (Step 7) - brief, audit, variations, and the post-application outreach
   email presented together. Picks collected via `ask_user_input_v0`. Cover letter is NOT
   included by default.

There is no score gate. A role that reaches this skill is one Tom has already decided to
pursue.

Do NOT generate .docx files until the user has made their picks.

---

## Step 0: Role Intake

Tom arrives with one of two things.

### A RADAR ROLE block

The dashboard's **Copy for Claude** button produces the block. It is generated by
`packetFor()` in the radar's `docs/index.html`, so the lines below are a contract, not a
guideline. Lines appear in this order and only the ones that apply are emitted:

```
WARNING: the radar never retrieved the real posting for this role.   (thin rows only)
RADAR ROLE
Company: / Title: / Location: / Market:
Radar score: 8.4  (Exp 9, Skills 8, Sen 8, Domain 9, Loc/Visa 8, Traj 8)
   or: Radar score: NOT SCORED (no posting text was ever retrieved; not screened either)
Shot 5.6 (experience, skills, seniority — can you get the interview)
Want 8.6 (domain, location, trajectory — do you want the job)
Hard gaps (stated must-haves you do not meet — address these first):
  - [one per line, quoted from the ad]
Radar verdict: ...
Sponsor: ...                          (UK and NL rows only)
Comp stated in the posting: EUR 90,000-110,000 base   or   ...: no
LOCATION CONFLICT: ...                (only when the posting and the radar disagree)
Flags:
  - [one per line]
Posting: [url]
Application form: [url]               (only when it differs from Posting)
JD (12,431 chars, complete):          or   JD: SAMPLE ONLY -- N of M characters...
[the ad]
```

**Six markets, in the radar's preference order:** Netherlands, Ireland, UK (London area
only), **Belgium**, Canada, US (remote only, senior or core RevOps titles only, and the
posting must state a salary). Belgium is a real target market; a BE row is not an error.
Germany, Spain, on-site US and remote-EMEA roles are excluded at source, so one reaching
this skill means something upstream went wrong and is worth one line.

Open with a two-line acknowledgement: the radar's score, the two halves it splits into,
and what it is made of. Then move on.

```
Radar has this at 8.4 — Shot 5.6, Want 8.6 (Exp 9, Skills 8, Sen 8, Domain 9, Loc/Visa 8,
Traj 8), market NL.
Verdict: [its verdict, quoted].
```

**Shot and Want are the same six dimensions split in two, and the split is the whole point
of this run.** Shot is experience, skills and seniority: can he get past the screen. Want is
domain, location and trajectory: does he want the job. A high Want with a low Shot is a role
the CV has to fight for, and the hard gaps are the list of what it is fighting -- Step 3
starts there. A high Shot with a low Want is a role to apply to quickly and not agonise
over. Neither is a reason to stop; Tom has already chosen the role.

If the line reads `NOT SCORED`, say that instead and do not substitute a number. That row
was set aside before scoring because no posting text was ever retrieved, which is a
different thing from a low score, and it means Step 3 has nothing to tailor against until
Tom pastes the real ad.

If a line in the block is not in the list above, the dashboard has changed and this skill
has not. Say so in ONE line, once, and carry on with what you can read -- do not infer
meaning from a line you don't recognize, and do not repeat the notice for every unfamiliar
flag. The radar's own risk notes are free text by design: a flag whose wording is not in
the table below is usually the scorer saying something new about this posting, which is
worth reading rather than reporting as a version mismatch. Reserve the notice for a
structural line (a new `Field:` heading, a missing `RADAR ROLE` header).

**Rules, in order of how easy they are to break:**

- **Do not produce a fit score.** Not a number, not a breakdown, not a "my own read is a 7."
  The radar computed it in Python from the same rubric and the arithmetic is reproducible;
  a second opinion here is noise dressed as diligence.
- **There is no gate and no apply/do-not-apply recommendation.** Tom chose this role. If
  something in the posting genuinely changes the picture, say that one thing in one line and
  keep working.
- **Take `Market` as given.** It sets the CV contact block, the floor referenced in Step 1
  and the visa framing in Step 2. The only thing that overrides it is a LOCATION CONFLICT line (below).

**Act on what the block tells you:**

| Line in the block | What it means here |
|---|---|
| `WARNING: the radar never retrieved the real posting` | **Stop.** The radar scored this on a title and a location. Ask Tom to open the posting and paste the real ad before anything else. Do not audit bullets against a stub, and do not treat a silent ad as a clean one: the sponsorship and language checks never ran on this row either. |
| `Radar score: NOT SCORED` | The row was set aside, not rated badly. Same handling as the WARNING above: get the real ad first. Never print `0.0` or invent a number for it. |
| `JD: SAMPLE ONLY -- N of M characters` | The ad was retrieved but only a head-and-tail sample was stored, so the middle is missing. That middle is usually the responsibilities and requirements, which is exactly what bullet tailoring keys off. Ask for the full ad before Step 3. |
| `LOCATION CONFLICT: the posting says X, the radar has it as Y` | **Stop and ask which is right.** Market picks the CV contact block and the visa floor; getting it wrong is wrong twice over. |
| `Application form: [url]` | The employer's real form, resolved separately from the posting link. This is the link Tom applies through, so carry it into Step 7 and name it there. When it is absent, the posting link is the form. |
| `Flags: thin evidence: no real JD retrieved...` | The flag version of the WARNING. Treat it the same way. |
| `Flags: title band: [band] -- check the JD for actual scope and comp` | Read the JD for actual scope. Analyst, Specialist, Associate and Director titles are a prompt to read carefully, not a verdict. See `profile.md` `## Seniority fit`. |
| `Flags: title band: off-target function` | The title reads outside RevOps/CS entirely. Read the responsibilities before building anything; if the body confirms it, say so in one line in the brief. |
| `Hard gaps: ...` | The posting's own stated must-haves that Tom's profile does not meet, read off the ad by the scorer. **Step 3 starts here**: audit the bullets against these before anything else, since they are what a recruiter stops on. They are not a reason to stop, and not a thing to argue with. |
| `Shot` / `Want` | The two halves of the score. See above. Neither changes what gets built; Shot decides how hard Step 3 has to work. |
| `Flags: no pay stated in the ad or the feed, verify vs floor` | Nobody published a number anywhere. Carry it into Step 1's one-liner. Do not research it. |
| `Flags: pay not confirmed in the ad; [band] came from the feed` | A different fact: a job board carried a band and the ad the scorer read did not repeat it. Say which in Step 1, because it is a question for the recruiter screen rather than a gap in the record. Do not research it. |
| `Flags: comp not listed, verify vs floor` | The older wording of the first of those two, still on rows scored before the radar split them. Same handling. |
| `Flags: band X-Y [cur] starts below your ...` | The stated band's bottom is under the market floor. One line in Step 1, flagged as a screen question for Tom. Do not research it, and do not re-derive the floor. |
| `Flags: model read the function as off-target` | Note it in the brief in one line. Do not re-argue the score. |
| `Flags: CSM in X at a non-standout company` | Note it in the brief. Relevant to how hard the summary leans on the pivot. |
| `Sponsor: ...` | Present for UK and NL rows only. Ireland and Belgium run permits rather than a register, and neither North American market needs sponsorship, so its absence there is correct, not missing data. |

### A raw job description, URL or pasted ad

Perfectly normal. Say in one line that there is no radar score for this one, resolve the
market yourself from the posting's stated location against `profile.md`'s target list, and
start at Step 1. **Still do not invent a score.** With no block there is no comp line
either, so Step 1 is one clause about what the ad itself states, or nothing at all.

If the posting's location is not in `profile.md`'s target markets at all, say so once and
ask whether Tom wants to proceed anyway. That is a question about the market, not a score.

---

## Step 1: Comp Line (report only, never research)

**Tom requests salary research manually. This skill does not do it.** No web searches for
comp, no comparables, no estimated range, no verdict. If the role's pay is worth digging
into, he will ask for it in a separate message, and that request is a different task from
this workflow.

What this step is: one line passing through what the radar already established, because
the block has it for free and it costs nothing to say.

- The block states a band -> `Comp stated: EUR 90,000-110,000 base (NL).`
- The block says `no` -> `Comp not stated in the posting.`
- A comp flag is present -> add the flag's own words, once: `Radar flagged: band starts below the NL floor.`

Then one closing clause, only when a flag fired or nothing is stated: `say the word if you
want the comp research before you apply.` Offer it once. Do not repeat the offer later in
the run and do not let it gate anything.

Two facts from `profile.md` that keep this line honest:

- **Only a figure the posting itself states ever reaches the block.** The radar discards
  board estimates on purpose. Never present one as a stated salary, and never fill the gap
  with a remembered number.
- **In Europe the floor is a visa threshold**, not a preference, so a below-floor flag is a
  legal problem rather than a negotiation problem. Say which one it is in the flag line, in
  four words, and leave it there.

**Nothing here stops the workflow.** Tom chose this role; a comp flag is a question for the
recruiter screen, not a reason to stop building.

---

## Step 2: Company Strategic Brief

Research forward-looking priorities. Feeds Steps 3, 4, 5 and 6.

**Short brief by default.** Three searches, five lines out, about two minutes: what the
company sells, the one thing it is visibly pushing right now, and one specific cultural or
product signal. That is enough to aim a summary and an outreach email, and it is all that
reaches the CV. The full brief below is roughly ten minutes of searching on a Pro plan's
quota, spent before Tom knows whether this application goes anywhere, and most of what it
produces is never used.

Run the full version when Tom asks for it, or when he is already past a screen with this
employer and the conversation is the deliverable. Say which one you ran, in four words, so
he can ask for the other.

Short-brief shape:

```
## [Company] — short brief
Sells: [what, to whom]
Pushing now: [the one visible priority, with the evidence]
Why this role exists: [one sentence]
Signal: [one specific cultural or product detail, not a value statement]
Visa: [UK/NL only — sponsorship track record, or "unknown". Omit for IE, BE, CA, US.]
```

### 2a: Research (full brief)

Use web search. Priority order:
1. Recent company blog posts, press releases, announcements (last 6 months)
2. Careers page and "why we're hiring" language on the JD
3. LinkedIn posts by the hiring manager or their leadership
4. Funding/earnings news (investor updates for public, Crunchbase/TechCrunch for private)
5. Product launches, new markets, new pricing models
6. Glassdoor or team pages for culture signals
7. **Visa sponsorship track record, for UK and NL roles only.** Ireland runs employment
   permits rather than a register, and neither Canada nor the US needs sponsorship at all,
   so skip this entirely for those three rather than producing a paragraph about nothing.

Time-box to ~10 minutes of tool calls. Not a consulting deck.

### 2b: Output the brief

```
## Company Strategic Brief: [Company Name]

**Forward-looking priorities (next 12 months):**
1. [Priority with evidence]
2. [Priority with evidence]
3. [Priority with evidence]

**Key challenges:**
- [Challenge with evidence]
- [Challenge with evidence]

**How this role likely contributes:**
[2-3 sentences connecting role responsibilities to priorities above. The "why they're
hiring" thesis.]

**Cultural signals (specific, not generic):**
- [Specific: "they ship a customer-facing postmortem after every outage", not "they value innovation"]
- [Specific]
- [Specific]

**Visa note:** [UK/NL only. Sponsorship track record if findable; flag as unknown if not.
Omit the heading entirely for IE, CA and US roles.]

**Radar flags worth carrying forward:** [one line each, only if the intake block had any]
```

---

## Step 3: Bullet Audit

### 3a.0: The hard gaps first (when the block has them)

If the intake block carried a `Hard gaps:` list, audit against it before anything else.
These are the posting's own stated must-haves that the radar read as unmet, and they are
what a recruiter screening the CV stops on. For each one, answer in a line:

```
Hard gap: "5+ years in a dedicated Sales Ops role"
  Covered by: [bank id or CV bullet, and how]  |  Partly: [what is adjacent]  |  Not covered
```

Three honest outcomes, and the third is common:
- **Covered** — the gap is a reading failure rather than a real gap; make sure the bullet
  that covers it survives the audit and lands high on the page.
- **Partly** — adjacent experience exists. This is what Step 3e's interview is for, and a
  hard gap outranks a keyword gap for one of its three slots.
- **Not covered** — say so plainly, once. Do not invent a bullet to close it and do not
  soften the CV around it. A real gap that Tom applies into anyway is a decision he is
  allowed to make; a CV that implies experience he does not have is not.

Nothing here gates the run. It decides where the effort goes.

### 3a: Inventory

Build the candidate pool from the **bullet bank first**, then the base CV.

1. Pull every bank bullet whose `Tracks` field matches this role's track (plus everything marked `ALL`).
2. Add any base CV bullet that has no bank equivalent, and flag it; it belongs in the bank and should be written back in Step 9.
3. Include `VARIANT` bullets only when the role genuinely matches the variant's angle. Skip `RETIRED` unless the JD makes it newly relevant, and say why if you resurrect one.

#### Sub-track selection

The bank has four track labels: `ANALYTICS`, `BUILDER`, `CS`, `ALL`. Analytics and Builder
are sub-tracks of the RevOps track, so a RevOps role still needs one of the two picked before
bullets get pulled. Do this first, in one line, before listing candidates.

**Analytics signals:** revenue or capacity modeling, forecasting, territory and quota
planning, SQL or BI in the requirements, business partnering with sales leadership. Larger
established employers (Google, LinkedIn, Salesforce, Stripe, Datadog, AWS, Verkada).

**Builder signals:** systems ownership, automation, integrations, CRM administration, owning
the GTM tool stack, early or first RevOps hire, Python or API mentions. Scaleups (Vanta,
Causaly, Fonoa, Synthesia, Apron, Bunch).

**When both fire:** pick the one matching the top third of the JD's responsibilities. What a
JD lists first is what the role actually does; what it lists last is a wish.

**When neither fires clearly:** default to Analytics.

CS-family roles (CSM, Senior/Principal CSM, CS Ops, CS leadership) take the CS track and skip
this check.

The determined track drives five downstream things: bullet pull (3a), role title (4a),
project order and section placement (4a.5), summary source (4c), and the skills line (4e).
State it explicitly so the rest of the run is traceable.

List every candidate bullet. Number them (1.1, 1.2, ... 2.1, 2.2, ...) and note the bank ID
alongside each (e.g. `1.1 [NAVEX-01]`). Bullets with no bank ID get marked `[new to bank]`.

### 3b: Decision per bullet

| # | Bullet (first 8 words...) | Decision | Rationale |
|---|------------------------|----------|-----------|

Decisions:
- **KEEP** - well-aligned; no change
- **KEEP + KEY** - well-aligned AND strategically important; candidate for variation scoring
- **REVISE** - relevant but could better reflect JD keywords or angle
- **REVISE + KEY** - same, AND strategically important; variations in Step 4d
- **CUT** - not relevant enough to justify resume real estate
- **PROMOTE** - buried in secondary role, should be elevated (rare)

Target 2-3 KEY bullets, the ones closest to the core responsibility of the target role.

Honesty constraint: CUT is about relevance, not hiding weak work. REVISE cannot add claims
not defensible against the CV, the STAR stories, LinkedIn, or `profile.md`. `profile.md`'s
Experience section states scope boundaries explicitly where a gap interview established one;
where it does, that boundary is the ceiling on what a bullet may claim.

#### Cut history (check before you decide)

`state/apply-state.json` in the bullet bank repo holds `cut_counts`, a running tally of how
many roles each bank ID has been cut on. Read it at the top of 3b:

```bash
python3 -c "import json;print(json.load(open('/home/claude/bank/state/apply-state.json'))['cut_counts'])"
```

Use it two ways, and only these two:

1. **As information, not instruction.** A bullet cut on four previous roles is a bullet that
   keeps losing, which is worth knowing before you spend a line on it. It is not a reason to
   cut it here if this JD makes it relevant. The CS-track roles regularly resurrect bullets
   that RevOps roles cut.
2. **As the trigger for retirement.** Anything that crosses three cuts with this run goes
   into Step 9's proposals as a RETIRE. Step 9d computes this properly; you just need to
   have looked.

If the file is missing or the clone failed, say so in one line and carry on. The audit is
still valid without it, it is just blinder.

### 3c: Metric gap flags

For up to 3 KEEP or REVISE bullets lacking concrete metrics:

```
**Metric gaps to close:**
- Bullet X.Y: "[summary]..."
  Suggested angles for a real metric: (a) [angle], (b) [angle], (c) [angle]
  -> Do you have actual numbers? If not, the bullet stays as is.
```

NEVER insert placeholder metrics like "[X]%" into the resume.

### 3d: Gap analysis - addressable keywords without a home

Run keyword analysis (see 4b) now. Identify keywords that are Addressable but NOT covered by
any existing bullet's language, even with revision.

Examples: "pipeline hygiene," "dashboard building," "quote-to-cash navigation," "onboarding
enablement content," "cross-functional project leadership."

Output:
```
**Unaddressed addressable gaps:**
1. [Keyword/theme] - [adjacent experience that hints at it]
2. [Keyword/theme] - [...]
```

### 3e: New-bullet interview (only if gaps remain after the banks are checked)

**Check both banks before asking Tom anything.**

1. **`answer-bank.md` first.** If a previous interview already covers this gap, use the
   stored answer and do not ask again. Cite the entry ID in the audit so the provenance is
   visible: `gap: pipeline hygiene - answered [A-20260914-01], no material`. This is the
   whole point of the answer bank and it is why the questions thin out over time.
2. **Then `bullet-bank.md`.** If a `VARIANT` or `RETIRED` bullet already covers the gap, pull
   it instead of interviewing Tom again for material he has already given.

For up to 3 highest-value gaps that survive both checks, use `ask_user_input_v0`.

Each question:
- Names the gap
- Asks if Tom has specific experience that fits
- Gives 2-4 concrete options (not open-ended)
- Is short. He knows which job this is. "Any pipeline hygiene or CRM cleanup at NAVEX?" is
  right; "The posting leans on pipeline hygiene and data quality, so did you do any of that
  work at NAVEX that isn't already on your CV?" is three times the length and says no more.

Example:
```
Q: "JD emphasizes pipeline hygiene and data cleanup. Did you do any of this at NAVEX or LexisNexis that isn't on your resume?"
Options: ["Yes, pipeline data cleanup", "Yes, forecast accuracy projects", "Yes, but informal / hard to claim", "No meaningful experience"]
```

If Tom answers positively, follow up in freetext to extract:
- What exactly did he do?
- Scope (accounts, time period)?
- Any measurable outcome?

Then draft a NEW bullet from his answers, constrained to what he said. If "no meaningful
experience," drop that gap silently. Do not invent.

**Hard rule:** new bullets are built from Tom's interview answers, never from JD language
itself. The JD tells you what gap to probe; Tom's words tell you what to write.

**Every answer goes into the answer bank in Step 9, including the negatives.** A "no
meaningful experience" is exactly as worth recording as a yes, because it is what stops the
same question coming back on the next role.

---

## Step 4: Resume Tailoring

### 4a: Title replacement

Use the exact target role title from the JD as the role title header. When the JD title is
missing, internal jargon, or too vague to be useful on a CV, fall back to the standing
default for the track determined in Step 3a:

| Track | Standing default |
|-------|------------------|
| Analytics | REVENUE OPERATIONS & GTM STRATEGY |
| Builder | REVENUE OPERATIONS & GTM SYSTEMS |
| CS | ENTERPRISE CUSTOMER SUCCESS |

In the .docx, render this as ALL CAPS, left-justified, with a bottom border, matching the
style of section headers like EDUCATION and PROFESSIONAL EXPERIENCE.

### 4a.5: Section placement and project order by track

Three tracks: **Analytics**, **Builder**, **CS**. Analytics and Builder are sub-tracks of the
RevOps track (selection rule in Step 3a) and are identical on section placement. Only the CS
track moves the Projects section.

**Section placement**

**RevOps track, Analytics and Builder** (Revenue Operations, Sales Operations, GTM Strategy &
Operations, BizOps, Sales Strategy & Operations): Projects section stays **above**
Professional Experience, titled "REVOPS & GTM PROJECTS." Purpose: surface RevOps-relevant
work before a recruiter reaches the CSM title and writes Tom off as "just another CSM
applying."

**CS track** (Customer Success Manager, Senior/Principal CSM, CS Operations, CS leadership):
Projects section moves **below** Professional Experience, retitled "PROJECTS & OTHER
EXPERIENCE." Section order becomes Education, Experience, Projects, Skills. Purpose: for CS
roles the CS experience itself is the lead credential; Projects read as a bonus
differentiator, not the headline.

**Project order within the section**

| Track | Order |
|-------|-------|
| Analytics | Factorial, Debic, GTM Health Diagnostic |
| Builder | GTM Health Diagnostic, Sales-to-CS Handoff, Debic, Factorial |
| CS | Sales-to-CS Handoff, GTM Health Diagnostic, Factorial (Debic omitted) |

This is the bank's standing order. Deviate only when the JD gives a specific reason to, and
say what the reason was in the change log.

Apply both the placement and the order in Step 8a when assembling the .docx. Note the track,
placement, and project order explicitly in the change log (8d).

### 4b: Keyword analysis

Identify the 15 most important skill keywords. Prioritize:
1. Hard skills/tools mentioned multiple times
2. Functional competencies in requirements or responsibilities
3. Industry-specific terminology
4. Language echoing Step 2's forward priorities

Mark each: **Present** / **Addressable** / **Not addressable**. Never fabricate.

A keyword naming something on `profile.md`'s "Deliberately NOT claimed" list is **Not
addressable**, full stop, however central the JD makes it. That is a skills miss, not a
stretch, and it is better to know now.

### 4c: Summary - start from the bank canonical, then 3 scored variations

The bank holds a canonical summary per track: `SUM-ANALYTICS`, `SUM-BUILDER`, `SUM-CS`. Pull
the one matching the track determined in Step 3a and use it as the starting point for all
three variations. Don't write from a blank page; these are already tuned, and rebuilding them
every run loses that tuning.

`SUM-CS-LEAD` is a VARIANT, not a default. Use it as the starting point only when a CS
leadership JD explicitly welcomes adjacent or non-manager backgrounds. It claims team
enablement and leadership exposure, never direct reports, and does not paper over a hard
people-management requirement.

Produce three variations that tailor the canonical to this JD. Each:
- Incorporates keywords that don't fit in bullets or skills
- Mirrors JD language
- Reflects Step 2 brief where natural
- 3-4 sentences max; must fit within 4 printed lines in the final .docx (11pt Calibri, standard margins)
- Defensible against CV + STAR stories + `profile.md`
- Follows about-me.md: direct, warm, no throat-clearing, no AI-isms

Variations differ in how far they move from the canonical, and in angle:
- **A. Canonical-tight** - the bank summary with keyword swaps and light phrasing edits only. The floor, and often the right answer.
- **B. Role-forward** - resequenced to lead with the JD's core function. On RevOps tracks this is also where the pivot framing lands if the JD invites it.
- **C. Company-forward** - leads with alignment to a Step 2 forward priority.

**Score each X/10 for recruiter impact** with one-line rationale. Criteria: JD language match,
signal of target function, quantification strength, voice alignment. Scores should
differentiate; don't hand Tom three 8/10s. Say in one line what each variation changed from
the canonical, so Tom can see whether the tailoring earned its keep.

If a variation would read better than the canonical on most future roles in that track, flag
it for the Step 9 promotion test rather than quietly leaving the improvement in this one
application.

Format:
```
**A. Canonical-tight (7/10)** - [one-line why]
[Summary text]

**B. Role-forward (8/10)** - [one-line why]
[Summary text]

**C. Company-forward (6/10)** - [one-line why]
[Summary text]
```

### 4d: Bullet revisions

For each KEY bullet from Step 3b:

**If the existing bullet is already strong** (clean metric, clear target-skill signal, voice
match) **produce ONE committed revision** with keyword polish only. Note: "already strong,
single revision."

**Otherwise, produce 3 variations (A, B, C)** differing in angle:
- Tool-forward vs. outcome-forward vs. cross-functional-forward
- Process-forward vs. data-forward vs. enablement-forward
- Pick angles that fit the JD's language

**Score each X/10** with one-line rationale.

Format:
```
**Key Bullet X - [topic]:**

A. *[Angle]* (8/10) - [why]
> [Bullet text]

B. *[Angle]* (7/10) - [why]
> [Bullet text]

C. *[Angle]* (6/10) - [why]
> [Bullet text]
```

**For NEW bullets from Step 3e:** one committed draft each, no variations. Flag as "NEW -
built from interview answers."

**For other REVISE bullets (non-KEY):** one committed revision each.

Rules for all revisions:
- Defensible against CV, STAR stories, LinkedIn, and `profile.md`'s stated scope boundaries
- No exaggeration
- Match about-me.md
- No anti-ai.md patterns (no em dashes, no "spearheaded/leveraged/orchestrated")
- Keep roughly the same length as original
- Each revision carries at least one addressable keyword from 4b

### 4e: Skills section

Rebuild to 5-7 most relevant skills. Rules:
- Single line, skills separated by " | " (matches base CV format, e.g. "Salesforce | HubSpot | Gainsight | SQL | Revenue Operations")
- Prioritize JD-relevant skills

**Build the pool from `profile.md`'s `## Skills` section, not from memory.** It carries the
qualifiers that matter (which tools Tom is a power user of versus an administrator of, which
certifications are in progress rather than held) and those qualifiers are what make the line
defensible in a screen.

**Its "Deliberately NOT claimed" list is a hard do-not-add.** Those entries are ruled out on
purpose, with reasons and dates, because they are drilling risks in an interview. A JD asking
for one of them does not change that.

---

## Step 5: Cover Letter Draft (ON REQUEST ONLY)

Skip by default to save tokens. Only run if Tom explicitly asks for a cover letter on this
application.

When requested:

**5a - Hook:** From Step 2 brief, pick ONE strongest hook: direct alignment with a named
forward priority, overlap with a challenge Tom has solved before, industry/domain overlap, or
MBA relevance to the company's current phase.

**5b - Draft principles:** Primacy Effect (open with the hook, no "I am writing to express my
interest..."); Linguistic Mirroring (weave JD language, don't parrot); Affinity Bias
(reference a specific cultural signal from Step 2, no template feel).

**5c - Writing rules:** No em dashes, no "synergy/deep dive/touch base/circle back/move the
needle," no listicle formatting, front-load the value proposition. Concise, flowing prose,
warmth underneath directness, evidence-based claims. No "In today's rapidly evolving
landscape," no "it's not X, it's Y," no "spearheaded/leveraged/orchestrated," no hedging
adverbs. One page max. Professional Tom voice: warm, polished, assertive, diplomatic.

Save to `/mnt/user-data/outputs/Tom_Norton_Cover_Letter_[CompanyName].docx` using the docx
skill, standard business letter format.

---

## Step 6: Post-Application Hiring Manager Outreach Email

This email is drafted to send right after the application has been submitted. It's not a cold
pitch sent before or during applying, and it's not a "checking in after silence" follow-up.
It's an immediate signal to the hiring manager that Tom applied and wants to be on their radar
before the application sits in an ATS queue.

### 6a: Predict likely hiring manager titles

From the JD (who would this role report to?) and Step 2 research, propose 2-3 likely titles
with % probability. This identifies who to address the email to, or who to search for on
LinkedIn if no name is listed.

Format:
```
**Likely hiring manager titles (search LinkedIn at [Company]):**
1. [Title] - [%] - [one-line why]
2. [Title] - [%] - [one-line why]
3. [Title] - [%] - [one-line why]
```

Example for a Sales Ops Partner, EMEA North:
- "Head of Sales Operations, EMEA" - 45% - direct functional manager, regional scope matches
- "VP Sales Operations, International" - 30% - one level up, possible direct report
- "Director, Revenue Operations, EMEA" - 25% - alternate title convention

### 6b: Recipient

- If a hiring manager or recruiter name is findable (LinkedIn search using 6a's titles, or a
  name listed on the JD/portal), address them directly.
- If the application went through a generic portal/ATS with no findable contact, default to
  "Hiring Team" and note that a generic address may go unread. That is expected for portal
  applications, not a sign anything went wrong.

### 6c: Timing rule

- Send the same day Tom applies, ideally within a few hours.
- Best send window within that day: Tuesday to Thursday, 8-10am or 2-4pm in the company's
  local time zone. If Tom applies outside that window, note it but still recommend sending
  promptly rather than holding it for the next ideal slot. Speed matters more than hitting
  the perfect hour.
- This is a single send, not a follow-up sequence. Don't generate a second message for this
  step; a true non-response follow-up is a separate, later decision Tom makes on his own.

### 6d: Identify the one specific reason to reference

Pull one specific, real reason from Step 2's brief (a forward priority, a challenge Tom has
solved before), not generic enthusiasm. This is what makes the follow-up read as genuine
interest rather than a template.

### 6e: Draft the email

```
Subject: Application: [Job Title]

Hi [Name / Hiring Team],

I just submitted my application for the [Job Title] role and wanted to flag it directly, particularly given [the one specific reason from 6d].

Happy to share anything else that's useful, my resume is attached/linked in the application.

Thanks,
Tom Norton
```

Subject line: specific and short, includes the exact job title, signals this is a direct
heads-up (not a follow-up on silence), under ~50 characters.

Voice constraints:
- One short paragraph. No restating the whole resume.
- State interest with the one specific reason, not generic "very excited" language.
- No framing that implies time has passed or assumes silence ("checking in," "wanted to follow up," "haven't heard back"). This goes out the same day as the application.
- End with a low-pressure offer (sending more info) rather than a question demanding a timeline; the application is too fresh to ask about status.
- "Thanks" not "Sincerely." No em dashes. No "circle back," "touch base," "per my last email."
- Under 100 words.

### 6f: When to skip this step entirely

- Tom already has an interview scheduled or has heard back. This email is for the moment right
  after applying, not for any later stage.
- No findable contact and a generic "Hiring Team" address would clearly go to an unmonitored
  inbox. In that case, still draft it (it costs nothing to send), but note plainly that
  response odds are low for portal-only applications.

---

## Step 7: Batch Review - STOP HERE

Present everything in one message, in this order:

1. Comp line (Step 1), one line
2. Company Strategic Brief (Step 2)
3. Bullet Audit table (Step 3b)
4. Metric Gap flags (Step 3c)
5. New bullet drafts from interview (Step 3e), if any, plus any gap answered from the answer bank
6. Summary variations A / B / C with scores (Step 4c)
7. Key bullet variations with scores, OR committed single revisions if already strong (Step 4d)
8. Skills section preview (Step 4e)
9. Hiring manager outreach email, to send the same day as applying (Step 6)

Cover letter (Step 5) is NOT included unless Tom has explicitly asked for one. If he has,
insert it as item 9 and shift the outreach email to item 10.

Then use `ask_user_input_v0` for the top picks. The tool allows max 3 questions per call, so
prioritize variation picks:

```
ask_user_input_v0:
  Q1: Summary - A / B / C?
  Q2: Key Bullet [most impactful] - A / B / C?
  Q3: Key Bullet [next most impactful] - A / B / C?
```

If more than 3 variation picks are needed, ask the remaining inline as freetext:
> "Also, for Key Bullet 3, which do you want: A / B / C? And any metric numbers to add, CUT overrides, or edits to the outreach email?"

Do NOT generate .docx files until picks are in.

---

## Step 8: Generate Deliverables

### 8a: Tailored CV

1. Copy CV to `/home/claude/cv_working.md`
2. Apply section placement and project order per Step 4a.5. Analytics and Builder keep Projects ("REVOPS & GTM PROJECTS") above Professional Experience; CS moves Projects below Professional Experience and retitles it "PROJECTS & OTHER EXPERIENCE." Order the projects within the section per the track table (Analytics: Factorial, Debic, GTM Health Diagnostic. Builder: GTM Health Diagnostic, Sales-to-CS Handoff, Debic, Factorial. CS: Sales-to-CS Handoff, GTM Health Diagnostic, Factorial, Debic omitted).
3. Apply all edits using str_replace:
   - Title change
   - Picked summary variation.
   - **The contact block follows `Market:`, and nothing else.** NL, IE, UK-London and BE take the European block — "Barcelona, Spain", the European number, and the `Nationality: United States` line. CA and US-Remote take the North American block — "Grand Rapids, MI", the US number, and **no nationality line**, because on a North American application it answers a question nobody asked and invites one nobody needs. This is the same rule `cvbuild.contact_for()` applies in the radar (`NA_MARKETS = ("CA", "US-Remote")`), and the two must not disagree: the contact details on the application form are read off the same skeleton. If a LOCATION CONFLICT line was in the block, resolve it with Tom before this step rather than picking a block from a market the posting contradicts.
   - Picked variations for each KEY bullet (or committed single revisions)
   - Committed revisions for non-KEY REVISE bullets
   - NEW bullets inserted in most relevant role section
   - Remove CUT bullets (unless overridden)
   - New skills line
   - Any user-supplied metrics
4. Read `/mnt/skills/public/docx/SKILL.md`
5. Create .docx preserving visual structure with these exact formatting specs:
   - **Page setup:** US Letter, 0.75" margins all sides. Target length: 2 pages (not 1). Don't force this with manual page breaks. Let content flow naturally; a slight 3rd-page spillover is fine and gets resolved by trimming a line of bullet text, not by inserting breaks.
   - **Font:** Calibri throughout, every text run in the document.
   - **Name header:** 20pt, bold, centered, same teal accent color as the section headers (EDUCATION, PROFESSIONAL EXPERIENCE, etc.)
   - **Contact info line:** 10pt, centered. The LinkedIn text (`linkedin.com/in/tom-p-norton`) must be a clickable hyperlink to `https://www.linkedin.com/in/tom-p-norton/` using `ExternalHyperlink` in docx-js.
   - **Role title header** (below contact line): 13pt, bold, ALL CAPS, left-aligned with bottom border, same style as other section headers, NOT centered. Text is the exact JD title. When no usable JD title exists, use the track standing default: Analytics = REVENUE OPERATIONS & GTM STRATEGY, Builder = REVENUE OPERATIONS & GTM SYSTEMS, CS = ENTERPRISE CUSTOMER SUCCESS.
   - **Section headers** (EDUCATION, PROFESSIONAL EXPERIENCE, etc.): 13pt, bold, teal accent color
   - **All other text** (bullets, job titles, dates, company names, summary): 11pt
   - **Skills line:** 11pt, centered, single line with " | " separators
   - **Line spacing:** 1.15 throughout
   - **Tab stop for job dates and locations:** right-aligned tab at the content-width margin (page width minus left/right margins)
   - **Layout:** centered bold name header (teal), centered contact line, left-aligned role title header, section headings, company/title/date layout with tab-aligned dates, bullets, centered single-line skills list with " | " separators (matches base CV format, no table)
6. Save to `/mnt/user-data/outputs/Tom_Norton_CV_[CompanyName].docx`

### 8b: Cover letter (ONLY if Tom requested one in Step 5)

Apply user edits. Use docx skill for standard business letter format. Save to
`/mnt/user-data/outputs/Tom_Norton_Cover_Letter_[CompanyName].docx`.

### 8c: Outreach email

Stays in chat as text (Tom will paste it into LinkedIn/email). No .docx. Display the final
version in the change log section for easy copy, and note it's meant to go out the same day
Tom applies.

### 8d: Change log

```
| # | Section | Original | Revised | Keyword(s) | Evidence Source |
|---|---------|----------|---------|------------|----------------|
```

Include: track (Analytics / Builder / CS), the resulting Projects section placement and
project order, which canonical summary was the starting point, title change, summary rewrite,
each bullet revision, each CUT, each NEW bullet with the interview answer as evidence source,
skills line changes. Where a gap was answered from `answer-bank.md` rather than by asking,
cite the entry ID as the evidence source.

### 8e: Present files

Present the CV (.docx) via `present_files`, plus the cover letter (.docx) if one was
generated. Display change log and the hiring manager outreach email inline. Ask Tom to review
before submitting.

Give him the link he actually applies through: the `Application form:` URL from the radar
block when there was one, otherwise `Posting:`. One line, at the end, so he isn't hunting
back up the dashboard for it.

---

## Step 9: Bank Write-Back

Run after the deliverables are presented, as the last thing in the session. Two files get
written, in one commit.

### 9a: The promotion test (bullet bank)

**Default is no.** Most tailored rewrites are job-specific and should not touch the bank. A
revision only gets promoted to `CANONICAL` if it would improve the bullet for **most future
roles**, not because it read better for this one job.

Apply all four. Fail any one and it does not get promoted:

1. **Portability.** Strip out every reference to this company, this JD's vocabulary, and this role's title. Is what remains still better than the current canonical version? If the improvement evaporates once the JD-specific language is removed, it was tailoring, not improvement.
2. **New substance.** Does it add a real fact, metric, scope detail, or outcome that the canonical version lacks? Keyword swaps, synonym changes, and reordering are not substance.
3. **Cross-track.** Would this version still be the one to reach for on a role in a different track? A bullet sharpened for a builder-track role that now reads worse on analytics-track roles is a `VARIANT`, not a replacement.
4. **Defensibility.** Same interview-defensibility bar as the canonical, against the CV, the STAR bank, `profile.md`, or Tom's own confirmed words. No exceptions.

Everything that fails the test but is still worth keeping gets logged on the existing bullet
as a `Job-specific variant` line with the company name, so it can be reused if a similar role
comes up. Everything that is merely a keyword reshuffle gets discarded, not logged. The bank
stays a library, not an archive.

### 9b: What always gets written back

**To `bullet-bank.md`:**
- **NEW bullets** confirmed in the Step 3e interview and used on the final CV. These are the highest-value additions; they are real experience that existed nowhere before this session.
- **Base CV bullets with no bank entry** flagged in Step 3a.
- **Repeated CUTs.** Whatever `bankwrite.bump_cut_counts` returns as newly crossing three cuts (9d computes this from `state/apply-state.json`, so it is a fact the system holds rather than something you have to remember). Mark each `RETIRED` with the count as the reason.
- **Confirmed `[DRAFT]` bullets.** Once Tom confirms wording and numbers, remove the `[DRAFT]` marker.

**To `answer-bank.md`:**
- **Every answer from the Step 3e interview**, whether or not it produced a bullet. A "no
  meaningful experience" answer is recorded too, because it is what stops the question coming
  back on the next role. This needs no approval and is not part of 9c; it is a record of what
  Tom said, not a judgement about it.

### 9c: Get approval first, one line per change

This is the decision about the **bullet** bank, not the instructions, and not the answer bank.
Keep it short; full bullet text belongs in the 9d edits, and printing it twice is what makes
this section feel like noise. One line per proposed change, no old/new text blocks:

```
**Proposed bullet bank changes ([n] total):**

1. PROMOTE - [BANK-ID]: [what the new version adds, 10 words] - clears portability + new substance
2. ADD - [new ID]: [what the bullet covers, 10 words] - from the Step 3e interview
3. VARIANT - [BANK-ID]: [Company]-specific angle, logged not promoted
4. RETIRE - [BANK-ID]: cut on [n] roles

Didn't qualify: [1-2 rewrites that read better for this job but failed portability, one clause each]

Plus [n] answers going into answer-bank.md.

Approve all, or tell me which numbers to drop.
```

Wait for Tom's answer. Only after he approves do you make the bullet edits in 9d. If nothing
qualifies, say exactly that in one line. A session producing zero bullet-bank changes is
normal and correct; the answers still get written.

### 9d: Write the changes and commit

Both files are in the local clone from "Before You Start," and the radar clone has the
writer. **Do not hand-edit bank entries with `str_replace`.** `bankwrite.py` exists because
the bank is parsed by every future run, and an entry written in a slightly different shape
is a bullet that silently stops being found. Nothing errors; the CVs just quietly get worse.

#### Before writing

```bash
cd /home/claude/bank && git pull
```

The live entry shape, for reference when you read the file (it is **not** what this skill
used to illustrate): `### NAVEX-01 — Renewal risk forecasting for leadership`, an em dash
after the ID, no square brackets, a blank line between each field, and entries grouped
under `# EMPLOYER` headings so a NAVEX bullet lands with its family. `bankwrite` produces
exactly this. Match it if you ever have to write one by hand.

#### Bullet bank edits

Build the approved changes from 9c as a list of dicts and run them through the writer. The
four kinds are `ADD`, `PROMOTE`, `VARIANT`, `RETIRE`, and anything else is skipped rather
than guessed at:

```python
import sys, json, datetime
sys.path.insert(0, "/home/claude/radar")
import bankwrite

today = datetime.date.today().isoformat()
bank = "/home/claude/bank/bullet-bank.md"
md = open(bank).read()

changes = [
  {"kind": "PROMOTE", "bank_id": "NAVEX-03", "text": "...", "why": "clears portability + new substance"},
  {"kind": "ADD", "bank_id": "NAVEX", "title": "...", "text": "...", "tracks": "BUILDER",
   "competencies": "...", "evidence": f"Gap interview {today}", "why": "new material"},
  {"kind": "VARIANT", "bank_id": "LN-01", "text": "...", "why": "failed portability"},
]

md, applied, skipped = bankwrite.apply_changes(md, changes, today, company="[Company]")
open(bank, "w").write(md)
print("applied:", applied, "skipped:", skipped)
```

`apply_changes` bumps `Last updated:` and appends the CHANGE LOG rows itself, so do not
also write those by hand. **Read the `skipped` list out loud.** A skip means the ID is not
in the bank, which is a real error worth one line to Tom, not something to paper over by
retrying with a guessed ID.

For `ADD`, pass the employer prefix as `bank_id` (`NAVEX`, `LN`, `PRJ`) and the writer
allocates the next free number and files it with its family. Always pass an explicit
`notes` value carrying the scope guard from Tom's interview answer. The writer's default
note says the entry came from the apply queue, which is no longer true and would mislead
the next run.

#### Answer bank edits

Plain append to `/home/claude/bank/answer-bank.md`, one entry per answered gap, in exactly
this format. It is parsed by future runs, so the field names and order are fixed:

```
### [A-YYYYMMDD-NN] - [gap keyword]
Asked: [the question, as it was put to Tom]
Answer: [Tom's words, verbatim. Do not summarize, tidy or paraphrase.]
Role: [Company] - [Title] ([job id from the radar block, or the posting URL])
Date: YYYY-MM-DD
```

`NN` continues the day's numbering: scan the file for existing `[A-<today>-NN]` ids and take
the highest plus one, so two roles worked in the same day don't collide.

**Verbatim is the whole point.** The value of this file is that it holds what Tom actually
said. A tidied paraphrase is a second-hand claim, and a future run cannot tell the difference.

#### Apply state

`state/apply-state.json` is the record of what has been applied to and what keeps getting
cut. Nothing else writes to it any more, so if this step is skipped the history just stops.
Two updates, every run, including runs with zero bank changes:

```python
state_path = "/home/claude/bank/state/apply-state.json"
state = json.load(open(state_path))

newly_retired = bankwrite.bump_cut_counts(
    state.setdefault("cut_counts", {}),
    {"bullets": [{"bank_id": "NAVEX-08", "decision": "CUT"}]},  # every CUT from Step 3b
)

state.setdefault("history", []).append({
    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "company": "[Company]",
    "title": "[Title]",
    "id": "[job id from the radar block, or the posting URL]",
    "outcome": "done",
    "cv": "[the .docx filename from Step 8a]",
})
json.dump(state, open(state_path, "w"), indent=1, sort_keys=True)
```

Run `bump_cut_counts` **before** finalizing 9c's proposal list where you can, since anything
it returns in `newly_retired` should have been proposed as a RETIRE. If it surfaces one you
did not propose, add it in a one-line follow-up rather than retiring a bullet Tom never saw.

No packet file. Packets were the mothballed pipeline's artifact, and the deliverables in
this session already live in the chat and in `/mnt/user-data/outputs/`.

#### Commit and push

One commit for the whole session, covering every file touched.

```bash
cd /home/claude/bank
git config user.name "Tom Norton"
git config user.email "tp.norton@pm.me"
git add bullet-bank.md answer-bank.md state/apply-state.json
git commit -m "[summary]"
git push
```

Message summarizes what changed, e.g. `Promote NAVEX-03, add PRJ-CLAY variant, 2 answers`.

Then report back to Tom in one line, using the SHA from the commit output:

```
Bank updated: https://github.com/tom-norton/tom-bullet-bank/commit/[sha] - [n] bullet changes, [n] answers, history + cut counts recorded
```

If the push fails, say so plainly and print the exact text you tried to write, so nothing from
the session is lost.

### 9e: Close the loop on the dashboard

Last line of the session:

```
Hit Mark applied on the radar row once you've submitted. It syncs across your devices and
keeps the role off the next scan.
```

The dashboard's Hide and Mark applied state is Tom's to set; this skill cannot write it.
Say it once, at the end, and do not chase it.

---

## Non-Negotiable Principles

- **The radar scores; this skill builds.** Never produce a fit score, a score breakdown, or an apply/don't-apply verdict. Tom already decided.
- **No comp research, ever.** Step 1 repeats what the radar already stated and offers the research once. Tom asks for it separately when he wants it. A search for salary comparables inside this workflow is out of scope, however tempting the flag.
- **The radar's block is a contract.** Its lines come from `packetFor()` in `docs/index.html`. Match on them literally, and when a line doesn't match anything in Step 0, say the dashboard has moved rather than inferring what it meant.
- **Bank writes go through `bankwrite.py`.** The bank is parsed by every future run, so shape matters more than convenience. Hand edits are the fallback for a failed clone, not the default.
- **Nothing else records the run.** The radar's apply half is off. If Step 9 doesn't write the history entry and the cut counts, no system holds them.
- **Honesty over optimization.** Never add claims Tom can't defend. No placeholder metrics. New bullets come from his interview answers, never invented from the JD. `profile.md`'s stated scope boundaries are ceilings, not suggestions.
- **Never tailor against an ad you don't have.** A stub, or a head-and-tail sample, gets the real posting fetched first. A silent ad is not a clean one.
- **Never ask a question the answer bank already answers.** Check it before every interview.
- **Relevance over preservation.** Cut ruthlessly. Resume space is zero-sum.
- **Score variations honestly.** If A is better than B, say so. Don't hand Tom three 8/10s.
- **Style consistency.** Output sounds like Tom wrote it. Simpler and shorter wins.
- **Direct about gaps.** A hard requirement sitting on the "deliberately NOT claimed" list is a miss. Say so.
- **Two touchpoints max.** New-bullet interview (only if gaps survive both banks) and batch review. No extra ceremonial check-ins. The Step 9 bank confirmation runs after deliverables are already in Tom's hands, so it doesn't block the application.
- **Nothing good gets lost.** Every new bullet earned in an interview goes into the bullet bank, and every answer goes into the answer bank. But the bullet bank only takes revisions that are better everywhere, not revisions that were better once.
