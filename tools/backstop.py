#!/usr/bin/env python3
"""Whether a late GitHub backstop run of scan.yml should stand down.

    python tools/backstop.py "<the cron that fired>" <unix time of the last scan commit>
    -> prints why, and "skip=true" if the run should stand down

The Worker dispatches each scan on time; GitHub delivers the matching backstop cron 20
minutes to 5 hours late. The old rule -- stand down if a scan committed in the last two
hours -- let every backstop that arrived more than two hours late run a second, duplicate
scan, and GitHub was arriving that late most days: 5-6 scans a day where 3 were asked
for, each paying Apify for ~175 rows, until hiring.cafe ran Tom's plan dry on 1 Oct 2026.

So the question is not "how long ago was the last scan" but "did a scan happen for the
slot this cron stands in for". The slot is the cron's own time, which is fixed and known;
the delivery time is not. Any scan committed since shortly before the slot covers it.

The backstop crons are set to the EST (UTC-5) instant of each slot, the LATER of the two
offsets. Under EDT the Worker fires an hour before that and its commit lands inside
SLOT_LEAD; under EST it fires at the slot itself, and a backstop that arrives while the
Worker's run is still going queues behind it in the `scan` concurrency group and reads
the commit when it starts.
"""
import sys
from datetime import datetime, timedelta, timezone

# How far before the slot a scan may have committed and still count for it: the hour the
# EDT Worker fires early by, plus slack. Well short of the 4.5 hours between slots, so a
# scan for the previous slot never counts for this one.
SLOT_LEAD = timedelta(minutes=90)


def slot_of(cron, now):
    """The most recent instant at or before `now` matching the cron's minute and hour
    (UTC). Day fields are ignored: GitHub only delivers a cron on a day it matches, and
    it is never a day late."""
    minute, hour = (int(x) for x in cron.split()[:2])
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot if slot <= now else slot - timedelta(days=1)


def should_stand_down(cron, now, last_scan):
    """True when a scan already committed for the slot this backstop stands in for."""
    return last_scan is not None and last_scan >= slot_of(cron, now) - SLOT_LEAD


def main(argv):
    cron = argv[1]
    last = (datetime.fromtimestamp(int(argv[2]), timezone.utc)
            if len(argv) > 2 and argv[2] else None)
    now = datetime.now(timezone.utc)
    slot = slot_of(cron, now)
    if should_stand_down(cron, now, last):
        print(f"skipped: the {slot:%d %b %H:%M}Z slot already has a scan ({last:%d %b %H:%M}Z)")
        print("skip=true")
    else:
        print(f"no scan for the {slot:%d %b %H:%M}Z slot yet; running")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
