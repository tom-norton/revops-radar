# Weekly outreach session (attended)

Tom's 30 minutes a week with Claude, usually Monday after the signals push and the weekly
review. Unlike tools/lamp_signals.md this session runs with Tom present, because it is the
one place credits get spent and every spend needs his yes. Tom starts it by saying "run my
weekly outreach session".

Everything lives in the Job Search Tracker (Google Sheet). Nothing here writes to this repo,
which is public: no names, emails or titles of people go in it.

Hiring-manager notes on applications (Touches types "HM note" and "App follow-up") are
queued by the job-application-workflow skill, not by this session; see
`tools/hiring_manager.md`. This session only checks them for replies in step 6, like every
other Touches row with a Sent date, and reads out Today section 8 (applications still
waiting for a hiring-manager lookup).

## Rules

- **Ask before every spend.** State how many people and the maximum credits, wait for yes.
  Standard Apollo match only (1 credit per match, 0 if no match). Validate an email (up to 1
  more credit) only when Apollo's status is not "verified". Never waterfall, never phone.
- **Free first.** Look in the NL, UK and IE Leads tabs for a verified work email before
  spending anything. Then Apollo. Then Clay's free tier. A LinkedIn request is the last
  fallback (networking-outreach skill), and only when no work email exists.
- **Never contact anyone twice.** Check Contacts for the company (and its aliases) first.
  Past contacts stay out of new outreach; past chat contacts are on the monthly check-in
  cycle instead (Today tab, section 6).
- **No recruiters, no personal emails, no phone numbers.** Respect Do not contact.
- **Never send anything.** Tom sends every email himself from the Touches tab.

## Steps

1. **Read the week.** Today tab sections 2, 3 and 7; the Keep checks tab; the signals push.
2. **Free slots.** A slot is free when an Active company shows "Drop: next company moves up"
   on the Companies tab (two silent contacts), or Tom ends it. Set that company's Active to
   No (Tom confirms). Keep the mix at three Netherlands, one UK or Ireland, two US
   (Settings tab). Fill the slot from the top of the list for that market: Motivation, then
   Signal score, then Affinity #, skipping Hot companies (the side lane) unless Tom says
   otherwise, and skipping companies whose "Passes contact gate" is not Yes.
3. **Find two people** for each new Active company with Apollo's free people search, in the
   decided order:
   1. anyone sharing an employer or school with Tom (NAVEX, LexisNexis, ESADE, Dayton), any
      function or country. Apollo cannot search past employers; ask Tom whether he found
      anyone on LinkedIn (the company's People tab filtered by school);
   2. RevOps, sales ops, commercial ops, CS ops or CS people in the target market;
   3. the same functions at UK or US headquarters when the local office has none;
   4. under about 100 staff: head of sales, COO or a founder.
   Prefer an operations title over a CSM, and a peer over the person who interviewed or
   rejected Tom. The first person is Role First, the second Backup.
4. **Reveal.** Leads tabs first (0 credits). For the rest, tell Tom the count and the maximum
   credits and wait. Then match by Apollo id. Record on each Contacts row: Email source,
   Credits used (1 per match, plus 1 if validated), Work email, Email status, Title,
   LinkedIn URL, Market (the person's own market; for someone outside NL/UK/IE/US use the
   company's slot market and say so in Notes), Commonality, Role, a Contact ID (next C-
   number), Stage "To contact". Check employment history for a shared employer, school or
   a customer-success-to-RevOps path and set Commonality to match; otherwise None.
5. **Queue.** Add a Touches row (Contact ID, Outreach, Email) for each new First contact and
   read back the Checks column. Fix a failed check in the Lookups tables, never in the text.
6. **Replies.** With the Microsoft 365 connector (read-only; tested 15 Nov 2026 on Tom's
   ESADE mailbox), for each Touches row with a Sent date and no Reply date:
   - `outlook_email_search` with `sender` = the contact's work email and `afterDateTime` =
     the Sent date. If nothing comes back, try `sender` = the company's email domain (a
     reply can come from a colleague or an alias) and check the name in the result.
   - Bounces: `outlook_email_search` with `query` = `Undeliverable OR "Delivery has failed"
     OR "couldn't be delivered"` and `afterDateTime` = the earliest open Sent date, then
     match the bounced address.
   - Fill Reply date and Status on the Touches row (Replied, Chat booked, or Bounced). A
     calendar acceptance counts as Chat booked; put the chat date in the contact's Chat
     Date. A bounce: set the contact's Email status to "bounced" and move to the backup.
   - Read only. Never use the connector's send, draft, delete or label tools.
   Without the connector, ask Tom which replies came in.
7. **Close.** Read out the Keep checks tab (credits per contact, bounces per 20, chat rate)
   and what is due this week.
