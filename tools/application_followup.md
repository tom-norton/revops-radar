# Application follow-ups (phase 5)

When Tom applies for a role, a note to the hiring manager is the one message that can make
a cold application get read. Two Claude skills (on Tom's account, not in this repo) split
the work:

- **job-application-workflow** builds the application. Tom often runs it from his phone.
  Its Step 6 now writes only a **follow-up brief**: the JD line, the proof point and its
  bullet-bank ID, one question, and the likely hiring-manager titles. It saves the brief to
  the tracker's Briefs tab. No names, no drafts, no credits.
- **application-followup** is what Tom runs later, at his laptop, for the roles he really
  wants (radar score 7.5 and up, Settings B31; any role when he asks). It finds the people,
  writes the notes, queues them in the tracker and leaves the email as a Proton draft.

Everything about people lives in the tracker (a Google Sheet) and in the chat. This repo is
public: no names, emails, titles or LinkedIn URLs of people go in it.

## How an application-followup run goes

1. **The role and the brief.** It finds the Applications row (Date Applied, Score, Status,
   referral) and the Briefs row for the posting link. With no brief, it rebuilds one from
   the posting and `profile.md`, and says so.
2. **Skip, hold and timing.** These are the workflow's old Step 6 rules: no note when the
   posting says no contact, a referral is in play, Tom has already heard back, or nobody
   can be named. Because Tom follows up after applying, the note is worded for the day.
   After 5 business days there is no Day 0 note, only the Day 7-10 style message.
3. **Find candidates for free.** It checks the tracker, the LinkedIn job page's "Meet the
   hiring team" box and the likely titles, then runs Apollo's people search (no credits),
   for the recruiter too.
4. **Read a few profiles** in Tom's logged-in Chrome (Claude in Chrome): at most 4 per
   application (Settings B30), opened directly from Apollo's links, read only. Tom accepted
   the LinkedIn risk on 16 Nov 2026 on these terms: no messages, connection requests,
   follows, reactions or InMail, and stop at the first warning. Without Claude in Chrome
   (a cloud session or the phone), the skill gives Tom a prompt to paste into the extension.
5. **Estimate.** Up to three named people with a probability each, a "someone not found"
   share and one line of evidence for each. It also names the recruiter if found, and
   quotes the credit cost.
6. **Tom picks** the hiring manager, the recruiter, both or none. That is the only yes the
   spend needs: one Apollo credit per person (none if a Leads tab has the email).
7. **Write.** The model writes the Day 0 note and the Day 7-10 follow-up, under the
   workflow's old Step 6 voice rules. The Dalton outreach, by contrast, is filled word for
   word from the Lookups tab. With no hiring manager, the recruiter gets the Day 0 note. An
   agency role goes to the agency recruiter only. Tom approves the text.
8. **Queue.** People go on Contacts and messages on Touches (channel "Proton").
9. **Proton draft.** The Day 0 email is saved as a draft in Proton Mail from the
   tp.norton@pm.me alias (the address on Tom's CV and applications), never sent. If the
   alias can't be selected, there is no draft. The Day 7-10 message waits until it is due.
   Then "draft my due follow-ups" drafts every due one: as a reply in the Day 0 thread when
   it goes to the same person, as a new message to the recruiter otherwise.
10. **Tom sends** each one from Proton. That's all: since 20 Nov 2026 the skill's
    "Logging sends and replies" step runs first on every later run (or on "log my
    touches"). Through Claude in Chrome it searches Proton's Sent folder for each queued
    subject and fills Sent date (S), then searches for mail from each contact's address or
    domain and fills Reply date and Status (Replied, Chat booked or Bounced; auto-replies
    don't count). The searches are read-only and scoped to Touches contacts; no other
    Proton mail is opened. The weekly outreach session does the same for the Outlook rows.
    Tom can still type these cells; a filled cell is never overwritten.

The Touches tab hides its formula and helper columns (D-L, O-Q, V) so Tom sees Contact ID,
Type, Channel, Subject, Text, Send, Sent date, Reply date and Status. Two saved filter views
narrow it further: "To send" (no Sent date) and "Awaiting reply" (sent, no reply yet). The
scripts find columns by header, so hiding them changes nothing for code.

## In the tracker

| Where | What |
|---|---|
| Briefs | One row per posting from the workflow: Link, Company, Title, Written, Track, JD line, Proof point, Proof source, Question, Ties to, Likely hiring-manager titles. |
| Contacts | One row per person. Role (column N) is "Hiring manager" or "Recruiter". Radar role id (column Y) is the posting link, exactly as in the Applications Link column: it ties the person to the application. |
| Touches | Type "HM note" (Day 0) or "App follow-up" (Day 7-10), channel "Proton". The skill types the Subject and Text; the template formulas don't fill them. Send (R) reads "Draft in Proton (application-followup)". |
| Touches V | "Send after (App follow-up)": 7 business days after the Applications row's Date Applied (Settings B28). Until then Send shows "Due [date]", or "Mark applied first" if the Applications row doesn't exist yet. |
| Today, section 1 | Lists HM notes straight away, and follow-ups only once they are due. |
| Today, section 8 | Roles scored 7.5+ (Settings B31), applied in the last 7 days (B29) since phase 5 began (B27), with no referral and nobody logged yet. Lower-scored roles stay off it, so it lists only the notes Tom means to write. |
| Keep checks, phase 5 | Roles scored 7.5+ since the start date, with a note sent and without one, interviews from each group, the two interview rates, and Apollo credits spent on hiring managers and recruiters. It reads "Pass: notes help" once 10 notes have gone out and the rate with a note beats the rate without. Both groups are limited to 7.5+ so the comparison isn't flattered by Tom writing notes only for his best-fit roles. |

Hiring managers and recruiters stay out of the Dalton machinery. "First email sent", the
backup and follow-up due dates, the "drop after two silent contacts" rule, and the
5-a-day and 12-a-week caps all count only Touches rows of type "Outreach".

## Why Proton, not Outlook

The Dalton outreach goes from the ESADE Outlook. Application notes go from tp.norton@pm.me
because that is the address on the CV and the application, so a hiring manager who looks
Tom up in their ATS finds the same person. Proton has no API or connector, so drafts are
made in the browser. The Microsoft 365 connector couldn't make Outlook drafts anyway: on
16 Nov 2026 Microsoft Entra had granted it only read permissions on the ESADE tenant.

## Why no Apps Script or Python change

The Applications tab is a Sheets table, and the web app inserts each new application at
row 2. Formulas inside it would break, and formulas elsewhere that start a range at row 2
shift down one row with every insert. So nothing new lives in Applications, the phase 5
formulas use whole-column ranges (`Applications!$E:$E`), and the person-to-application
link sits in Contacts. The funnel endpoint already returns every Keep checks row, so the
Monday review picks up phase 5 with no code change.
