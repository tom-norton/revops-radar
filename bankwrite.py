#!/usr/bin/env python3
"""
Step 9 of the skill: writing the bullet bank back.

The decision of WHAT to write back is a judgement call and belongs to a model (the four
promotion tests in 9a). Actually editing the file is not, and belongs here. The bank is
parsed by every future run, so an entry written in a slightly different shape is a bullet
that quietly stops being found -- which is the worst kind of bug this system can have,
because nothing fails and the CVs just get a little worse.

Entry format, from the skill and matched exactly:

    ### [ID] - [short title]
    Status: CANONICAL | Tracks: ALL | Competencies: a, b
    Text: the bullet
    Evidence: where it came from
    Notes: scope guards, cut history

Two edits go into every pass, no exceptions: the `Last updated:` date at the top, and one
CHANGE LOG row per change.
"""

import re

CHANGE_LOG_RE = re.compile(r"^#{1,3}\s*change\s*log\b", re.I | re.M)
ENTRY_RE = re.compile(r"^###\s*\[([^\]]+)\]", re.M)
LAST_UPDATED_RE = re.compile(r"^(Last updated:).*$", re.I | re.M)

# What a change record looks like coming in. `kind` is the skill's own vocabulary.
KINDS = ("PROMOTE", "ADD", "VARIANT", "RETIRE")


def entry_spans(md):
    """{id: (start, end)} over the markdown. `end` runs to the next entry, the CHANGE LOG,
    or the end of the file."""
    hits = list(ENTRY_RE.finditer(md or ""))
    log = CHANGE_LOG_RE.search(md or "")
    hard_end = log.start() if log else len(md or "")
    spans = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else hard_end
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
    if reason:
        note = re.search(r"^Notes:\s*(.*)$", block, re.M)
        existing = (note.group(1).strip() if note else "")
        joined = (existing + " " if existing else "") + reason
        block = _replace_field(block, "Notes", joined.strip())
    return block


def new_entry(bank_id, title, text, tracks="ALL", competencies="", evidence="", notes=""):
    return (f"### [{bank_id}] - {title}\n"
            f"Status: CANONICAL | Tracks: {tracks} | Competencies: {competencies}\n"
            f"Text: {text}\n"
            f"Evidence: {evidence}\n"
            f"Notes: {notes}\n")


def next_id(md, prefix):
    """`prefix` plus the next free two-digit number, e.g. NAVEX-04. Collisions here would
    silently overwrite an existing bullet on the next promotion."""
    used = [int(n) for n in re.findall(rf"^###\s*\[{re.escape(prefix)}-(\d+)\]", md or "",
                                       re.M)]
    return f"{prefix}-{(max(used) + 1) if used else 1:02d}"


def _insert_point(md):
    """Where a new entry goes: after the last one, before the CHANGE LOG."""
    log = CHANGE_LOG_RE.search(md)
    if log:
        return log.start()
    return len(md)


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
            at = _insert_point(md)
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
            label = f"Job-specific variant ({company or 'this role'}): {text}"
            block = block.rstrip("\n") + f"\n{label}\n"
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
