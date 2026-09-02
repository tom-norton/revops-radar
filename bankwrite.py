#!/usr/bin/env python3
"""
Step 9 of the skill: writing the bullet bank back.

The decision of WHAT to write back is a judgement call and belongs to a model (the four
promotion tests in 9a). Actually editing the file is not, and belongs here. The bank is
parsed by every future run, so an entry written in a slightly different shape is a bullet
that quietly stops being found -- which is the worst kind of bug this system can have,
because nothing fails and the CVs just get a little worse.

Entry format, read off the live bank rather than off the skill's illustration -- the two
differ, and the file is the one that has to parse:

    ### NAVEX-01 - Renewal risk forecasting for leadership
    Status: CANONICAL | Tracks: ALL | Competencies: renewal forecasting, risk management

    Text: Owned renewal risk forecasting for a $5.2M ARR enterprise book...

    Evidence: Base CV, LinkedIn, STAR bank, gap interview 2026-08-05

    Notes: scope guards, cut history, anything a future run needs

No square brackets around the ID, an em dash after it, and a blank line between fields.
Bullets are grouped under `# EMPLOYER` headings, so a new bullet joins its family rather
than landing at the end of the file.

Two edits go into every pass, no exceptions: the `Last updated:` date at the top, and one
CHANGE LOG row per change.
"""

import re

CHANGE_LOG_RE = re.compile(r"^#{1,3}\s*change\s*log\b", re.I | re.M)
# The ID runs to the first dash or end of line. Square brackets are tolerated because the
# skill's own example uses them, but the live bank does not.
ENTRY_RE = re.compile(
    r"^###\s*\[?([^\s\]]+)\]?(?:\s+[-\u2013\u2014]\s|\s*$)", re.M)
LAST_UPDATED_RE = re.compile(r"^(Last updated:).*$", re.I | re.M)
# The `# EMPLOYER` / `# PROJECTS` group headings. An entry's text stops at the next one of
# these as well as at the next entry, so a bullet added to the end of the NAVEX family
# lands inside it rather than one line into the section after it.
GROUP_RE = re.compile(r"^#{1,2}\s+\S", re.M)

# What a change record looks like coming in. `kind` is the skill's own vocabulary.
KINDS = ("PROMOTE", "ADD", "VARIANT", "RETIRE")


def entry_spans(md):
    """{id: (start, end)} over the markdown. `end` runs to the next entry, the CHANGE LOG,
    or the end of the file."""
    md = md or ""
    hits = list(ENTRY_RE.finditer(md))
    log = CHANGE_LOG_RE.search(md)
    hard_end = log.start() if log else len(md)
    groups = [g.start() for g in GROUP_RE.finditer(md)]
    spans = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else hard_end
        after = [g for g in groups if g > m.start()]
        if after:
            end = min(end, after[0])
        spans[m.group(1).strip()] = (m.start(), min(end, hard_end))
    return spans


def entry_text(md, bank_id):
    """The `Text:` line of one entry, or ''."""
    span = entry_spans(md).get(bank_id)
    if not span:
        return ""
    m = re.search(r"^Text:\s*(.+)$", md[span[0]:span[1]], re.M)
    return m.group(1).strip() if m else ""


def _replace_field(block, field, value):
    """Set `field:` inside one entry block, adding the line if it isn't there."""
    pattern = re.compile(rf"^{re.escape(field)}:\s*.*$", re.M)
    if pattern.search(block):
        return pattern.sub(f"{field}: {value}", block, count=1)
    return block.rstrip("\n") + f"\n{field}: {value}\n"


def _set_status(block, status, reason):
    def sub(m):
        rest = m.group(0)
        # Status shares its line with Tracks and Competencies, separated by pipes. Only the
        # first field changes; the rest of the line is left exactly as it was.
        parts = rest.split("|")
        parts[0] = f"Status: {status} "
        return "|".join(parts).rstrip()
    block = re.sub(r"^Status:.*$", sub, block, count=1, flags=re.M)
    status = status.upper()
    if reason:
        note = re.search(r"^Notes:\s*(.*)$", block, re.M)
        existing = (note.group(1).strip() if note else "")
        # Prefixed and capitalised so it reads as its own sentence. Appending a lowercase
        # clause to whatever the note happened to end with produces "...not work Tom did.
        # cut on three roles", which nobody wants to read in a year.
        added = f"{status} \u2014 {reason[0].upper()}{reason[1:]}".rstrip(".") + "."
        joined = (existing + " " if existing else "") + added
        block = _replace_field(block, "Notes", joined.strip())
    return block


# Every bank entry carries a Notes line, and a new bullet's honest note is that nobody has
# checked its scope yet. The bank's most valuable content is its SCOPE GUARDs; a blank
# Notes on a machine-written bullet quietly claims there is nothing to guard.
DEFAULT_NOTES = ("Written by the apply queue from a gap interview answer. No scope guard "
                 "recorded yet: check the wording against what Tom actually did before "
                 "this bullet carries a strong verb.")


def new_entry(bank_id, title, text, tracks="ALL", competencies="", evidence="", notes=""):
    """Matched to the live bank's shape, blank lines and all. Uniformity matters more than
    elegance here, because every future run parses this file."""
    return (f"### {bank_id} \u2014 {title}\n"
            f"Status: CANONICAL | Tracks: {tracks} | Competencies: {competencies}\n\n"
            f"Text: {text}\n\n"
            f"Evidence: {evidence}\n\n"
            f"Notes: {notes or DEFAULT_NOTES}\n")


def next_id(md, prefix):
    """`prefix` plus the next free two-digit number, e.g. NAVEX-12. Collisions here would
    silently overwrite an existing bullet on the next promotion."""
    used = [int(n) for n in re.findall(rf"^###\s*\[?{re.escape(prefix)}-(\d+)\]?\b",
                                       md or "", re.M)]
    return f"{prefix}-{(max(used) + 1) if used else 1:02d}"


def _insert_point(md, prefix=""):
    """Where a new entry goes.

    Next to its family when the family exists: the bank groups bullets under `# EMPLOYER`
    headings, and a NAVEX bullet filed after the projects section is a bullet the next run
    reads in the wrong context. Otherwise after the last entry, before the CHANGE LOG."""
    log = CHANGE_LOG_RE.search(md)
    hard_end = log.start() if log else len(md)
    if prefix:
        spans = entry_spans(md)
        family = [span for bid, span in spans.items()
                  if bid == prefix or bid.startswith(prefix + "-")]
        if family:
            return max(end for _start, end in family)
    return hard_end


def apply_changes(md, changes, today, company=""):
    """Apply approved bank changes. Returns (markdown, applied, skipped).

    `changes` are dicts: {kind, bank_id, text, why, title, tracks, competencies, evidence}.
    A change naming an ID that isn't in the file is skipped rather than guessed at -- the
    skill's rule is that an old_str you can't find means the edit is wrong."""
    md = md or ""
    applied, skipped, log_rows = [], [], []

    for ch in changes or []:
        kind = str(ch.get("kind") or "").upper()
        bank_id = str(ch.get("bank_id") or "").strip()
        text = " ".join(str(ch.get("text") or "").split())
        why = " ".join(str(ch.get("why") or "").split())
        if kind not in KINDS:
            skipped.append((bank_id or "?", f"unknown change kind {kind!r}"))
            continue

        if kind == "ADD":
            if not text:
                skipped.append((bank_id or "?", "no bullet text"))
                continue
            prefix = (bank_id.rsplit("-", 1)[0] if "-" in bank_id else bank_id) or "NEW"
            new_id = next_id(md, prefix)
            block = new_entry(
                new_id, ch.get("title") or "New bullet", text,
                tracks=ch.get("tracks") or "ALL",
                competencies=ch.get("competencies") or "",
                evidence=ch.get("evidence") or f"Gap interview {today}",
                notes=ch.get("notes") or "")
            at = _insert_point(md, prefix)
            md = md[:at].rstrip("\n") + "\n\n" + block + "\n" + md[at:]
            applied.append((new_id, "ADD"))
            log_rows.append((today, new_id, "added from gap interview", why or "new material"))
            continue

        spans = entry_spans(md)
        if bank_id not in spans:
            skipped.append((bank_id or "?", "no entry with that ID in the bank"))
            continue
        start, end = spans[bank_id]
        block = md[start:end]

        if kind == "PROMOTE":
            if not text:
                skipped.append((bank_id, "no replacement text"))
                continue
            block = _replace_field(block, "Text", text)
            was = re.search(r"^Evidence:\s*(.*)$", block, re.M)
            evidence = "; ".join(x for x in [(was.group(1).strip() if was else ""),
                                             f"promoted {today}"] if x)
            block = _replace_field(block, "Evidence", evidence)
            log_rows.append((today, bank_id, "promoted to canonical", why or "clears 9a"))
        elif kind == "VARIANT":
            if not text:
                skipped.append((bank_id, "no variant text"))
                continue
            # The bank's own variant shape: id, company and date in the parentheses, so a
            # later run can tell which posting the angle was cut for.
            label = (f"Job-specific variant ({bank_id}-VAR, "
                     f"{company or 'unknown'} {today}): {text}")
            block = block.rstrip("\n") + f"\n\n{label}\n\n"
            log_rows.append((today, bank_id, "variant logged", why or "failed portability"))
        elif kind == "RETIRE":
            block = _set_status(block, "RETIRED", why or f"Retired {today}.")
            log_rows.append((today, bank_id, "retired", why or "repeatedly cut"))

        md = md[:start] + block + md[end:]
        applied.append((bank_id, kind))

    if applied:
        md = bump_last_updated(md, today)
        md = append_change_log(md, log_rows)
    return md, applied, skipped


def bump_last_updated(md, today):
    if LAST_UPDATED_RE.search(md):
        return LAST_UPDATED_RE.sub(rf"\1 {today}", md, count=1)
    # No such line yet: put one under the title rather than at the very top, so the file
    # still opens with its heading.
    lines = (md or "").split("\n")
    for i, line in enumerate(lines):
        if line.startswith("#"):
            lines.insert(i + 1, f"\nLast updated: {today}")
            return "\n".join(lines)
    return f"Last updated: {today}\n\n{md}"


def append_change_log(md, rows):
    """One row per change: | date | id | what changed | why |."""
    if not rows:
        return md
    body = "\n".join(f"| {d} | {i} | {what} | {why} |" for d, i, what, why in rows)
    m = CHANGE_LOG_RE.search(md)
    if not m:
        return (md.rstrip("\n") + "\n\n## CHANGE LOG\n\n"
                "| Date | Bullet | What changed | Why |\n|---|---|---|---|\n" + body + "\n")
    return md.rstrip("\n") + "\n" + body + "\n"


def bump_cut_counts(counts, audit):
    """Count CUT decisions per bullet across roles, so 9b's "cut on three or more roles"
    is a fact this system holds rather than a thing a model has to remember. Returns the
    IDs that have just crossed the line."""
    newly = []
    for b in (audit.get("bullets") or []):
        if (b.get("decision") or "").upper() != "CUT":
            continue
        bid = (b.get("bank_id") or "").strip()
        if not bid:
            continue
        counts[bid] = counts.get(bid, 0) + 1
        if counts[bid] == 3:
            newly.append(bid)
    return newly
