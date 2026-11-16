# Hiring-manager notes (phase 5)

When Tom applies for a role, the same-day note to the hiring manager is the one message
that can make a cold application get read. The job-application-workflow skill has always
drafted it; phase 5 adds finding the person and queueing the note in the Job Search
Tracker, so the note actually goes out and the review can tell whether it helps.

Everything about people lives in the tracker (a Google Sheet) and in the chat. This repo is
public: no names, emails, titles or LinkedIn URLs of people go in it.

## How a run goes

The skill's Step 6 does this, in a Claude session Tom is in. The browser steps need the
session to run on his own computer, with Claude in Chrome.

1. **Find candidates for free.** Look in the tracker first: an earlier contact at the
   company, or a lookup already done for this posting. Then check the LinkedIn job page's
   "Meet the hiring team" box, use the JD to guess who the role reports to, and run
   Apollo's people search (no credits) for those titles at the company and location.
2. **Read a few profiles** in Tom's logged-in Chrome. At most 4 per application
   (Settings B30), opened directly from Apollo's links, read only. Tom accepted the
   LinkedIn risk on 16 Nov 2026 on these terms: no messages, connection requests, follows,
   reactions or InMail, and stop at the first warning or limit notice. A cloud session
   has no LinkedIn login, so it skips this step and says so.
3. **Estimate.** Up to three named people with a probability each, a "someone not found"
   share, and one line of evidence for each. The estimate also quotes the credit cost.
4. **Tom picks.** That is the only yes the spend needs: one Apollo credit for the work
   email (none if the person is in a Leads tab), plus one for the recruiter if the Day 7
   message goes to one.
5. **Write and queue.** The skill writes the Day 0 note and the Day 7-10 follow-up under
   its own Step 6 rules. These notes are written by a model, unlike the Dalton outreach,
   which is filled word for word from the Lookups tab. The skill then adds the people
   to Contacts and the messages to Touches, and tries an Outlook draft for the Day 0 note.
6. **Tom sends** each one and types the Sent date in Touches column S.

## In the tracker

| Where | What |
|---|---|
| Contacts | One row per person. Role (column N) is "Hiring manager" or "Recruiter". Radar role id (column Y) is the posting link, exactly as in the Applications Link column: it ties the person to the application. |
| Touches | Type "HM note" (Day 0) or "App follow-up" (Day 7-10). Subject and Text are typed by the skill, not filled by the template formulas. |
| Touches V | "Send after (App follow-up)": 7 business days after the Applications row's Date Applied (Settings B28). Until then Send shows "Due [date]", or "Mark applied first" if the Applications row doesn't exist yet. |
| Today, section 1 | Lists HM notes straight away, and follow-ups only once they are due. |
| Today, section 8 | Applications from the last 7 days (Settings B29) since phase 5 began, with no referral and no hiring manager logged yet. |
| Keep checks, phase 5 | Applications with a note sent and without one since the start date (Settings B27), interviews from each group, the two interview rates, and Apollo credits spent on hiring managers and recruiters. The keep check reads "Pass: notes help" once 10 notes have gone out and the rate with a note is higher than the rate without. |

Hiring managers stay out of the Dalton machinery. "First email sent", the backup and
follow-up due dates and the "drop after two silent contacts" rule all count only Touches
rows of type "Outreach", and the 5-a-day and 12-a-week caps apply to those only.

## Outlook drafts

The skill tries `outlook_create_draft`. On 16 Nov 2026 the Microsoft 365 connector had
read-only permissions on the ESADE tenant: Microsoft Entra had not granted
`Mail.ReadWrite`. Until it does, the Touches row's "Open in Outlook" link opens a filled-in
email instead. The skill never sends.

## Why no Apps Script or Python change

The Applications tab is a Sheets table, and the web app inserts each new application at
row 2. Formulas inside it would break, and formulas elsewhere that start a range at row 2
shift down one row with every insert. So nothing new lives in Applications, the phase 5
formulas use whole-column ranges (`Applications!$E:$E`), and the person-to-application
link sits in Contacts. The funnel endpoint already returns every Keep checks row, so the
Monday review picks up phase 5 with no code change.
