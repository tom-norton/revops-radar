# Weekly hiring-signal refresh

The instructions the Monday outreach session follows. The scheduled task only says "follow
tools/lamp_signals.md" and gives the tracker's spreadsheet id. Edit this file to change
what the refresh does.

## What it is for

Tom's outreach follows Dalton's 2-Hour Job Search: he contacts people at companies on his
LAMP list before a role is posted. "Warm" companies (signs of hiring, no relevant role yet)
are the proactive targets; "Hot" companies (a relevant role open now) are a side lane that
gets the apply-day note instead. This refresh keeps those labels current from free sources,
once a week, and logs every week so the 8-week keep check can ask whether Warm companies
really do post relevant roles more often than Cool ones.

## Hard rules

- **No credits.** Apollo people search only (it is free). Never enrich, reveal, validate or
  call anything that says it consumes credits. No Clay.
- **No messages.** Never send an email or a LinkedIn message, never draft outreach.
- **Company-level data only** outside the tracker. This repo is public and the ntfy topic
  is readable by anyone who knows it: no person's name, title-plus-company that identifies
  a person, or email goes in either. Names live in the tracker only, and this refresh does
  not write any.
- **Write only these cells:** new rows on the Signals tab, and on the Companies tab only
  columns J (Hiring signal), AI (Fresh signals), AJ (Signal notes) and AK (Signals
  updated). Never touch Motivation, Active, Status, Notes, the board override (AH) or any
  other column. Never edit Contacts, Touches or Applications.
- **No commits.** This refresh changes nothing in the repo.

## Steps

1. Use the working branch named in the task prompt (`git fetch origin <branch>` and check
   it out) until the code is on `main`. Then make sure history is there for the radar
   lookback: `git fetch --unshallow origin main` if `git rev-parse --is-shallow-repository`
   says true, otherwise `git fetch origin main`. `pip install requests` if needed.
2. Read the Companies tab (`Companies!A1:AK`) with the Google Sheets connector. Write
   `/tmp/lamp/companies.json`, one entry per row with a company name in column B:
   `{"company": B, "aliases": C split on ";", "domain": T, "board": AH}`.
3. Read the Signals tab (`Signals!A1:R`). Take the rows of the most recent Week and write
   `/tmp/lamp/prev.json` as `[{"company": B, "sales_cs_open": D or null,
   "sponsor": {"uk": I, "nl": J}}]`. If the tab has no rows, skip `--previous`.
4. Run `python tools/lamp_signals.py /tmp/lamp/companies.json --previous /tmp/lamp/prev.json
   > /tmp/lamp/signals.json`. It takes about three minutes. Its last stderr line says how
   many boards it read and whether both sponsor registers loaded; if a register did not
   load, say so in the summary and treat that register's statuses as unknown.
5. **New GTM leader (Apollo, free search).** For companies with Motivation (column F) of 3
   or more, in batches of up to 10 domains (column T), run a people search with
   `person_titles` = chief revenue officer, vp sales, vice president of sales, head of
   sales, vp customer success, head of customer success, chief customer officer, head of
   revenue operations, director of revenue operations, vp revenue operations;
   `person_days_in_current_title_range` = {"max": 120}; `per_page` 50. Count people per
   company. A count of one or more is the signal. Record only the titles, never names. If
   Apollo is not connected, write "not checked" and carry on.
6. **New hub (web search).** For the Active companies and every company with Motivation 5,
   search for a new Amsterdam, Netherlands or Dublin office, hub or headquarters announced in
   the last 90 days. At most 15 searches. Count it only with a source dated in that window;
   put the URL in the notes.
7. **Score each company.**
   - Fresh signals = one point each for: `burst` true; `newly_on_register` non-empty;
     `past_on_target_roles` non-empty; a new GTM leader (step 5); a new hub (step 6).
   - Hiring signal after:
     - **Hot** if `radar_open_now` is non-empty (a role the radar scored 6.5+ in the last
       30 days).
     - Otherwise, if it was Hot: **Warm** (the role has closed or aged out).
     - Otherwise, if fresh signals is 1 or more and it was Cool, Cold or Not checked:
       **Warm**.
     - Otherwise unchanged. This refresh never moves a company down from Warm: the
       November evidence (growth, funding) stands until Tom changes it.
8. **Write the Signals tab.** Append one row per company after the last row, columns A to R
   in the existing header order: Week (today, ISO date), Company, Board, Sales/CS open,
   Sales/CS last week, Burst, Relevant on board, Radar roles at gate (30 days), UK sponsor,
   NL sponsor, Newly on register, On-target roles 3-12 months ago, New GTM leader (120 days,
   titles only), New hub (URL), Fresh signals, Hiring signal before, Hiring signal after,
   Notes. Never overwrite earlier weeks.
9. **Update the Companies tab**, only the four columns: J where the label changed, AI the
   fresh-signal count, AJ a short reason (under 150 characters, for example "Sales/CS
   postings 9 -> 14; new VP Sales"), AK today's date.
10. **Keep check, from the eighth weekly run on.** Using the Signals tab: take the companies
    labelled Warm and those labelled Cool eight weeks ago (column Q of that week). For each
    group, the share that had a radar role at the gate (column H non-empty) in any week
    since. Report both shares. The check passes when Warm is higher.
11. **Push a summary** with ntfy, at most three short lines and company names only:
    `curl -s -H "Title: Outreach signals" -H "Tags: mag" -d "<lines>"
    https://ntfy.sh/<NTFY_TOPIC from scan.py>`. Line 1: how many companies changed label.
    Line 2: up to three that became Warm or Hot, with the reason. Line 3: the keep-check
    result when due, otherwise boards found that have no override yet ("confirm in AH").
    Do not put the spreadsheet link in the push.
12. End the session with the same summary, plus anything that failed and why.

## What Tom does with it

Nothing, most weeks. In his weekly 30 minutes he reads the push, glances at the Signals
tab, and fills a free Active slot from the top of the list (Motivation, then Signal score,
then Affinity). When a board the script found belongs to a different company with the same
name, he types `none` in column AH; when the script missed a board, he types `ats:slug`.
