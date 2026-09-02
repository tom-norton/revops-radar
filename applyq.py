#!/usr/bin/env python3
"""
RevOps Radar - apply queue poller.

Phase 1 of the automation: everything that happens after "this role looks good".
scan.py finds and scores roles; this runs the front half of the
job-application-workflow skill against the ones Tom taps Apply on, and asks him
the questions it genuinely cannot answer over Telegram.

Flow, one role at a time:

  Firebase `queued` <- dashboard Apply button, or Telegram /apply <id>
    -> bullet audit     (track selection, per-bullet decisions, gap analysis;
                         needs nothing from Tom, so it runs before anything is asked)
    -> ask              (ONE message: the comp-risk question if there is one, plus
                         every gap the answer bank can't already answer)
    -> packet           (salary research if he asked for it; new bullets drafted
                         strictly from his answers)
    -> cv               (company brief, then the tailoring pass: title, summary
                         variations, bullet revisions, skills line)
    -> pick             (ONE message: A/B/C summary variations, but only on a role
                         scoring VARIATION_REVIEW_MIN_SCORE or better; below that the
                         strongest is picked here and the diff is sent for information)
                        then render -> verify -> PDF to Telegram -> bank write-back

Why a state machine and not a script: a gap interview has no timeout. Tom answers
when he answers, which may be tomorrow. A GitHub Actions job cannot sit and wait
indefinitely, so each tick loads the run state, drains Telegram, advances as far as
it can, persists, and exits. A tick with nothing to do costs nothing.

Why ONE round trip and not one question at a time: cron is the bottleneck, not the
model. Over the 15 hours after this first went live GitHub delivered 4 of an expected
60 scheduled runs, with gaps up to 5h45m. Every wait for Tom costs one of those, so
a five-turn conversation is a day per application. Asking everything at once makes it
one turn. After asking, the run also holds open for HOLD_OPEN_SECONDS: an answer given
while the runner is still alive finishes the whole role in that same run.

Serial by design. One `current` role at a time. Two interleaved gap interviews are
unusable on a phone, which is the only place Tom answers them.

State and every private artifact live in the private bullet-bank repo, never in
this one. This repo is public and docs/ is served by Pages: gap answers, bullet
text and packets do not belong anywhere near it.

Usage:
  python applyq.py            one tick
  python applyq.py --dry      no Claude calls, no Telegram sends, no commits
  python applyq.py --status   print queue + current state and exit
  python applyq.py --selftest offline checks of the pure functions

Environment:
  ANTHROPIC_API_KEY     scoring/audit calls
  TELEGRAM_BOT_TOKEN    @BotFather
  TELEGRAM_CHAT_ID      Tom's chat with the bot
  BULLET_BANK_PAT       fine-grained PAT, read/write on tom-bullet-bank only
  APPLYQ_HOLD_OPEN      seconds to wait for a reply before exiting (default 600)
  APPLYQ_INBOUND        Tom's message, when the relay pushed it in rather than this
                        polling for it. Set by the workflow from its `message` input.
"""

import json, os, re, shutil, subprocess, sys, time
from datetime import datetime, timezone

import requests

import scan
import bankwrite
import coverletter
import cvbuild

# ---------------------------------------------------------------- config

FIREBASE_STATE_URL = ("https://revops-radar-2822a-default-rtdb.europe-west1"
                      ".firebasedatabase.app/revops-radar-state.json")

BANK_REPO = "github.com/tom-norton/tom-bullet-bank"
BANK_DIR = os.environ.get("BULLET_BANK_DIR", "/tmp/bullet-bank")
BANK_FILE = "bullet-bank.md"
ANSWER_FILE = "answer-bank.md"
STATE_FILE = "state/apply-state.json"
PACKET_DIR = "packets"
# Commits to the bank are made as Tom, matching the skill's convention -- the bank is his
# document, and a bot identity in its history makes `git log` useless for spotting what he
# wrote himself.
BANK_GIT_NAME = "Tom Norton"
BANK_GIT_EMAIL = "tp.norton@pm.me"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Per-phase models. Split out as named constants because the budget lever, if this runs
# hot, is to route the audit and the interview to Sonnet and keep Opus for the judgement
# calls. One edit each, no logic change.
SALARY_MODEL = "claude-opus-5"
AUDIT_MODEL = "claude-opus-5"
DRAFT_MODEL = "claude-opus-5"
AUDIT_MAX_TOKENS = 8000
SALARY_MAX_TOKENS = 6000
DRAFT_MAX_TOKENS = 6000
AUDIT_EFFORT = "medium"
# Splitting one reply into per-question answers is mechanical, so it runs on the cheap
# model. It is never allowed to write anything -- see split_is_sane().
SPLIT_MODEL = "claude-haiku-4-5"
SPLIT_MAX_TOKENS = 2000
# Phase 2. The brief and the tailoring pass are both judgement calls -- what a company is
# actually trying to do next year, and which of Tom's bullets earns its line on the page --
# so both stay on Opus. The bank write-back decision is the four-part promotion test, which
# is likewise a judgement, but over a much smaller input.
BRIEF_MODEL = "claude-opus-5"
BRIEF_MAX_TOKENS = 4000
BRIEF_MAX_SEARCHES = 6
TAILOR_MODEL = "claude-opus-5"
TAILOR_MAX_TOKENS = 12000
BANKWRITE_MODEL = "claude-opus-5"
BANKWRITE_MAX_TOKENS = 4000
# The cover letter. Opus, because this is the one deliverable that is entirely prose and
# entirely Tom's voice -- there is no bank canonical underneath it to fall back on.
COVER_MODEL = "claude-opus-5"
COVER_MAX_TOKENS = 4000
# Telegram allows getUpdates OR a webhook, never both: once a webhook is registered,
# getUpdates returns 409 and polling is dead. So the message can arrive two ways.
#
#   poll mode  (no webhook)  - getUpdates, as below
#   push mode  (webhook set) - the relay hands Tom's message to the workflow as an input,
#                              which arrives here as APPLYQ_INBOUND
#
# Push mode is the one that makes this usable: cron delivered 4 of an expected 60 runs in
# the 15 hours after launch, and a relayed message starts a run in about a second.
INBOUND_ENV = "APPLYQ_INBOUND"
# The chat the relayed message came from. Checked against TELEGRAM_CHAT_ID before the text
# is believed -- see the intake in tick().
INBOUND_CHAT_ENV = "APPLYQ_INBOUND_CHAT"
# After asking, hold the run open this long waiting for a reply. GitHub's cron is the
# bottleneck, so an answer given while the runner is still alive saves hours. Telegram caps
# one long poll at 50s, so this is several polls back to back.
HOLD_OPEN_SECONDS = int(os.environ.get("APPLYQ_HOLD_OPEN", "600"))
# Web search is metered per use. The skill time-boxes salary research to 3-4 searches;
# past that the comparables get worse, not better.
SALARY_MAX_SEARCHES = 4
# Server-tool turns can pause and resume. Bounded so a pathological loop can't run the
# bill up unattended.
SERVER_TOOL_MAX_TURNS = 4

# A stated band this close to the market's visa floor is comp risk, not comfort: the floor
# is the legal minimum for the visa, and a role sitting on it leaves nothing for the gap
# between the advertised bottom and the actual offer.
COMP_RISK_MARGIN = 0.10
# A stage that keeps failing is dropped rather than retried into the ground. Three ticks
# is 45 minutes of transient trouble tolerated; past that it is not transient.
MAX_STAGE_RETRIES = 3
# The variation review gate. At or above this radar score Tom picks the summary himself
# from A/B/C; below it the highest-scoring variation is taken and he is shown what changed,
# for information only. It is a named constant because it is a volume dial, not a rule:
# every review is another round trip, and a round trip costs a cron firing. Move it up when
# the queue is busy, down when he wants a say on more of them.
VARIATION_REVIEW_MIN_SCORE = 7.5
# Summary variations, per the skill's Step 4c: canonical-tight, role-forward,
# company-forward.
VARIATION_LABELS = ["A", "B", "C"]
# How many times a too-long summary gets a sentence removed before the CV ships anyway.
# Two is enough to take a five-sentence summary down to three; past that the variation was
# never within shouting distance of the limit and a warning is the honest outcome.
SUMMARY_TRIM_ATTEMPTS = 2
# The pick message carries three summaries in full. This cap is a guard against a model
# that ignores "3-4 sentences", not a formatting choice: a real summary runs 350-550
# characters, so 900 should never bite. It was 460, which truncated real summaries with an
# ellipsis and left Tom picking between three things he could not finish reading -- on the
# one message in the whole run where he is being asked to decide.
SUMMARY_CHARS = 900
# The one-line note on what a variation changed from the canonical.
CHANGED_CHARS = 130

# At most this many gaps go to interview per role. The skill's own cap. More than three
# questions and the phone stops being the right place to answer them.
MAX_GAP_QUESTIONS = 3
# Hard caps on anything a model wrote before it reaches a phone. The prompts ask for this
# length; these are what make it true. A question over ~90 characters wraps to three lines
# on a phone, and three of those is a wall of text between Tom and the one thing he has to
# do.
Q_CHARS = 110
OPT_CHARS = 34

# Answers that mean "I have nothing here". A gap that gets one of these is dropped
# silently -- no bullet, no follow-up, no inference.
NO_MATERIAL = re.compile(r"^\s*(no|none|nope|nothing|n/?a|skip|no meaningful experience)\b",
                         re.I)

# One round trip per role, not five. The audit needs nothing from Tom, so it runs first;
# then every question this role will ever ask goes out in a single message and comes back
# as a single reply. GitHub's cron is the bottleneck (it delivered 4 of an expected 60 runs
# in the 15 hours after launch, with gaps up to 5h45m), and each extra turn costs another
# one of those. Five turns at that rate is a day per application.
STAGES = ["audit", "ask", "packet", "cv", "pick", "revise", "cover", "done"]

# Stages where the run is waiting on Tom and there is no point advancing without him. The
# hold-open loop in tick() watches these, and only these.
WAITING_STAGES = ("ask", "pick")

HELP = (
    "RevOps Radar apply queue\n\n"
    "/apply <id> - queue a role for the full workflow\n"
    "/queue - what's waiting\n"
    "/status - what the poller is working on right now\n"
    "/cancel - drop the role in flight and move on\n"
    "/phone &lt;number&gt; - put your number on the CV (or /phone off)\n"
    "/redo &lt;what to change&gt; - rebuild the last CV with your feedback\n"
    "/cover &lt;anything to steer it&gt; - write the cover letter for the last CV\n"
    "/help - this\n\n"
    "When I ask a question, just reply to it. Numbered options accept the number."
)

# ---------------------------------------------------------------- small helpers

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# Telegram's HTML mode needs exactly these three escaped. Everything interpolated into a
# message is escaped: job titles and company names come off scraped pages, and the
# questions come from a model, so an unescaped "&" or "<" would make Telegram reject the
# whole message -- and a question that never arrives leaves the run waiting on an answer
# to something Tom never saw.
def esc(t):
    return (str(t if t is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def strip_tags(t):
    """The plain-text fallback when a formatted send is rejected."""
    t = re.sub(r"<[^>]+>", "", t or "")
    return (t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


def clip(t, n):
    """Bound a string that a model wrote before it reaches a phone screen. The prompts ask
    for short; this is what makes it true."""
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[:n - 1].rstrip(" ,.;:-") + "\u2026"


def slugify(s, cap=40):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s[:cap].rstrip("-") or "role")


def run_git(args, cwd, check=True, redact=None):
    """git with the PAT kept out of anything that gets printed. A failed clone prints its
    stderr, and the token is in the remote URL."""
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        if redact:
            err = err.replace(redact, "***")
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {err[:300]}")
    return p.stdout.strip()


# ---------------------------------------------------------------- Telegram

class Telegram:
    """Thin wrapper. Every send is best-effort in the sense that it never raises into the
    state machine, but a failure is printed -- a silently dropped question would leave the
    run waiting forever on an answer to something Tom never saw."""

    def __init__(self, token, chat_id, dry=False):
        self.token = token
        self.chat_id = chat_id
        self.dry = dry
        self.enabled = bool(token and chat_id)

    def _call(self, method, _timeout=30, **params):
        if not self.enabled:
            return None
        try:
            r = requests.post(TELEGRAM_API.format(token=self.token, method=method),
                              json=params, timeout=_timeout)
            data = r.json()
            if not data.get("ok"):
                print(f"  telegram {method} not ok: {str(data)[:200]}")
                return None
            return data.get("result")
        except Exception as e:
            print(f"  telegram {method} failed: {e}")
            return None

    def send(self, text, html=True):
        """Send one message, formatted unless told otherwise.

        Formatting is worth the risk because these are read on a phone, but the risk is
        real: if the markup is malformed Telegram rejects the whole message rather than
        degrading, and a question that never arrives strands the run. So a rejected
        formatted send is retried once as plain text -- worse looking, still delivered."""
        text = (text or "").strip()
        if not text:
            return
        if self.dry or not self.enabled:
            print(f"  [telegram]\n{strip_tags(text)}\n")
            return
        # Telegram hard-caps a message at 4096 characters and rejects anything longer
        # outright, so long ones are split rather than lost.
        for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
            kw = {"chat_id": self.chat_id, "disable_web_page_preview": True}
            sent = None
            if html:
                sent = self._call("sendMessage", text=chunk, parse_mode="HTML", **kw)
            if sent is None:
                if html:
                    print("  formatted send rejected; retrying as plain text")
                self._call("sendMessage", text=strip_tags(chunk), **kw)

    def send_document(self, path, caption=""):
        """Upload a file to the chat. This is how the finished CV reaches Tom: he reads
        everything on a phone, and a link into a private repo is a login and two taps before
        he can see whether the dates are in the right place.

        Returns True when Telegram accepted it. A failure here is not fatal -- the PDF is
        already committed to the bank -- but it is loud, because a CV he never received is
        indistinguishable from one that was never built."""
        if not self.enabled:
            print(f"  [telegram document] {path}")
            return False
        if self.dry:
            print(f"  [telegram document] {path}")
            return True
        try:
            with open(path, "rb") as f:
                r = requests.post(
                    TELEGRAM_API.format(token=self.token, method="sendDocument"),
                    data={"chat_id": self.chat_id, "caption": (caption or "")[:1000],
                          "parse_mode": "HTML"},
                    files={"document": (os.path.basename(path), f, "application/pdf")},
                    timeout=120)
            data = r.json()
            if not data.get("ok"):
                print(f"  telegram sendDocument not ok: {str(data)[:200]}")
                return False
            return True
        except Exception as e:
            print(f"  telegram sendDocument failed: {e}")
            return False

    def webhook_active(self):
        """True when a webhook is registered on this bot.

        Detected rather than configured, because the two are mutually exclusive and a
        stale config flag is worse than an extra API call: with a webhook registered
        getUpdates returns 409, and a scheduled tick that kept polling would log a
        confusing failure every firing forever."""
        info = self._call("getWebhookInfo") or {}
        return bool((info or {}).get("url"))

    def updates(self, offset, wait=0):
        """Everything since `offset`, and the new offset.

        `wait` holds the connection open server-side for up to that many seconds (Telegram
        caps a single long poll at 50). A tick that has just asked a question uses this so
        an answer given straight away is handled in the same run instead of waiting for the
        next cron firing, which may be hours off."""
        if self.dry or not self.enabled:
            return [], offset
        res = self._call("getUpdates", offset=offset, timeout=min(int(wait), 50),
                         limit=100, _timeout=min(int(wait), 50) + 25) or []
        for u in res:
            offset = max(offset, u.get("update_id", 0) + 1)
        return res, offset


def message_texts(updates, chat_id):
    """(update_id, text) for ordinary messages in Tom's chat, oldest first. Anything from
    another chat is ignored outright -- the bot token is a bearer credential and the chat
    id is the only thing establishing that a message is actually from Tom."""
    out = []
    for u in updates:
        m = u.get("message") or u.get("edited_message") or {}
        text = (m.get("text") or "").strip()
        chat = str(((m.get("chat") or {}).get("id")) or "")
        if text and chat and chat == str(chat_id):
            out.append((u.get("update_id", 0), text))
    out.sort()
    return out


# ---------------------------------------------------------------- Firebase queue

def firebase_state(url=FIREBASE_STATE_URL):
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.json() or {}
    except Exception as e:
        print(f"  firebase read failed: {e}")
        return None


def firebase_patch(fields, url=FIREBASE_STATE_URL, dry=False):
    """PATCH, not PUT. The dashboard owns `hidden`/`applied` and this owns `queued`; a PUT
    from either side would delete whatever the other had just written."""
    if dry:
        print(f"  [firebase patch] {json.dumps(fields)[:200]}")
        return True
    try:
        r = requests.patch(url, json=fields, timeout=30)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  firebase write failed: {e}")
        return False


def as_id_list(v):
    """Firebase omits empty arrays entirely and turns sparse arrays into objects keyed by
    index. Both come back here as a clean list of id strings."""
    if isinstance(v, list):
        items = v
    elif isinstance(v, dict):
        items = [v[k] for k in sorted(v, key=lambda k: str(k))]
    else:
        return []
    return [str(x) for x in items if x not in (None, "")]


# ---------------------------------------------------------------- bullet bank repo

class Bank:
    """The private repo: bullet bank, answer bank, run state, and the packets this
    produces. A hard dependency -- without it there is nowhere to persist an interview
    that spans days, and nowhere private to put what comes out."""

    def __init__(self, pat, dry=False, path=BANK_DIR):
        self.pat = pat
        self.dry = dry
        self.path = path
        self.url = f"https://{pat}@{BANK_REPO}.git" if pat else None

    def clone(self):
        if not self.pat:
            if self.dry:
                # --dry is for exercising the state machine locally. A scratch directory
                # stands in for the bank so it can run with no credentials at all.
                os.makedirs(self.path, exist_ok=True)
                print(f"  [dry] no PAT; using {self.path} as a stand-in bank")
                return
            raise RuntimeError("BULLET_BANK_PAT is not set; cannot reach the bullet bank")
        if os.path.isdir(os.path.join(self.path, ".git")):
            run_git(["remote", "set-url", "origin", self.url], self.path, redact=self.pat)
            run_git(["fetch", "--depth", "1", "origin", "HEAD"], self.path, redact=self.pat)
            run_git(["reset", "--hard", "FETCH_HEAD"], self.path, redact=self.pat)
            return
        if os.path.exists(self.path):
            shutil.rmtree(self.path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        run_git(["clone", "--depth", "1", self.url, self.path], ".", redact=self.pat)

    def read(self, rel, default=""):
        p = os.path.join(self.path, rel)
        if not os.path.exists(p):
            return default
        with open(p, encoding="utf-8") as f:
            return f.read()

    def write(self, rel, text):
        p = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def append(self, rel, text):
        cur = self.read(rel, "")
        if cur and not cur.endswith("\n"):
            cur += "\n"
        self.write(rel, cur + text)

    def read_bytes(self, rel, default=b""):
        p = os.path.join(self.path, rel)
        if not os.path.exists(p):
            return default
        with open(p, "rb") as f:
            return f.read()

    def write_bytes(self, rel, data):
        """For the CV PDF. The bank is where the artifacts live, and a PDF is the one
        artifact here that is not text."""
        p = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def load_state(self):
        try:
            return json.loads(self.read(STATE_FILE, "") or "{}")
        except json.JSONDecodeError as e:
            # Never guess at a half-written state file: that would silently restart an
            # interview Tom has already answered half of.
            raise RuntimeError(f"{STATE_FILE} is not valid JSON ({e}); fix it by hand")

    def save_state(self, state):
        self.write(STATE_FILE, json.dumps(state, indent=1, sort_keys=True) + "\n")

    def commit(self, message):
        if self.dry:
            print(f"  [bank commit] {message}")
            return None
        run_git(["config", "user.name", BANK_GIT_NAME], self.path)
        run_git(["config", "user.email", BANK_GIT_EMAIL], self.path)
        run_git(["add", "-A"], self.path)
        if not run_git(["status", "--porcelain"], self.path):
            return None
        run_git(["commit", "-m", message], self.path)
        for attempt in range(4):
            p = subprocess.run(["git", "push", "origin", "HEAD"], cwd=self.path,
                               capture_output=True, text=True)
            if p.returncode == 0:
                break
            if attempt == 3:
                err = (p.stderr or "").replace(self.pat or "\0", "***")
                raise RuntimeError(f"bank push failed after 4 tries: {err[:300]}")
            time.sleep(2 ** (attempt + 1))
        return run_git(["rev-parse", "--short", "HEAD"], self.path)


# ---------------------------------------------------------------- salary gate

def comp_risk(job):
    """(risky, reason). Risk is the only thing that opens salary research: at 30+ scored
    roles a run, researching comp on every one of them is the fastest way to spend the
    month's budget on comparables nobody reads.

    Two triggers, both from what the deep scorer already read off the posting:
      - no salary stated at all
      - a stated floor within COMP_RISK_MARGIN of the market's visa floor

    A row scored before scan.py started persisting `comp` has no data, which reads as not
    stated. That asks a question that may not be needed; the alternative is assuming the
    money is fine on a role Tom might take, which is worse."""
    comp = job.get("comp") or {}
    if not comp.get("stated"):
        return True, "no salary stated in the posting"
    try:
        low = float(comp.get("min_base") or 0)
    except (TypeError, ValueError):
        low = 0.0
    if low <= 0:
        return True, "salary mentioned but no usable base figure"
    market = job.get("market") or ""
    floor, cur = scan.VISA_FLOORS.get(market, (0, ""))
    stated_cur = (comp.get("currency") or "").upper()
    if not floor:
        return False, ""
    if stated_cur and stated_cur != cur:
        # No FX guessing, same rule as scan.salary_floor_flag(). A figure that can't be
        # compared to the floor is a figure that can't clear it either.
        return True, f"stated in {stated_cur}, floor is {cur} - not directly comparable"
    if low < floor * (1 + COMP_RISK_MARGIN):
        return True, (f"stated floor {int(low)} {cur} is within "
                      f"{int(COMP_RISK_MARGIN * 100)}% of the {market} visa floor "
                      f"({floor} {cur})")
    return False, ""


SALARY_SYSTEM = """You are researching the pay band for one job posting, for a candidate \
who needs the role to clear a visa salary floor. Use web search. You have at most \
{max_searches} searches -- spend them well and stop.

Benchmark hygiene, which decides whether the answer is worth anything:
- The comparable title has to match the actual job, not just share a word with it. NEVER \
benchmark a RevOps role against "Operations Analyst": in the Netherlands that title sweeps \
in logistics coordinators, supply-chain admin and back-office ops, and the median it \
returns is garbage.
- Preferred comparables, in order: the exact title at the exact company; the exact title in \
that city; "Revenue Analyst" / "Sales Operations Analyst" / "Data Analyst" in that city; \
the function's Manager-level band as a ceiling reference.
- Report the spread, not just the average. A EUR 74K average over a EUR 53K-90K range means \
the data does not resolve the question, and you say so.
- Quote at least two independent sources and show both. If they disagree by more than about \
EUR 15K, the honest verdict is borderline, not a confident call either way.
- Never lead with the lowest number you found because it makes the cleanest argument. Give \
the range, then reason about where this specific role sits in it.

Tom's floors: Netherlands EUR 75,000 base (ideal 83,000+); Ireland EUR 75,000 (ideal \
85,000+); London GBP 70,000 (ideal 80,000+). Belgium: treat EUR 75,000 as the working floor.

30% ruling (Netherlands only): if the role clears the HSM threshold of EUR 71,304/yr \
contractual fixed base for age 30+ AND the employer is an IND-recognised sponsor, Tom \
qualifies, which makes an EUR 80K base roughly comparable to EUR 95K without it. The \
threshold counts contractual base only -- not bonus, commission, RSUs or holiday allowance.

When you have finished searching, answer with ONLY a JSON object and nothing else:
{{"sources": ["<source - what it said>"],
  "low": <number>, "high": <number>, "currency": "<ISO code>",
  "thirty_percent_ruling": "yes" | "no" | "unknown" | "n/a",
  "verdict": "above_floor" | "borderline" | "below_floor",
  "notes": "<caveats: sparse data, ambiguous level, heavy variable comp, etc>"}}

`verdict` is borderline whenever the data does not actually resolve the question. Do not \
round a wide or thin result up to above_floor."""


def claude_server_tool_call(api_key, model, system, user, max_tokens, tools,
                            effort="medium"):
    """One request that may use server-side tools, resumed across pause_turn.

    Raw HTTP rather than the SDK because that is what this repo already speaks -- scan.py's
    _claude_call is the same shape, and mixing an SDK client in beside it would mean two
    retry policies and two places to change a header.

    Returns the concatenated text of the final turn. Server-tool failures do NOT raise:
    they arrive as HTTP 200 with an error object in the result block, so a search that
    fails just means less evidence, and the model says so in its answer."""
    messages = [{"role": "user", "content": user}]
    for turn in range(SERVER_TOOL_MAX_TURNS):
        body = {
            "model": model, "max_tokens": max_tokens,
            "system": system, "messages": messages, "tools": tools,
            "output_config": {"effort": effort},
        }
        last = None
        payload = None
        for attempt in range(scan.CLAUDE_ATTEMPTS):
            if attempt:
                time.sleep(2 ** attempt)
            try:
                r = requests.post(scan.API_URL, timeout=300, headers={
                    "x-api-key": api_key,
                    "anthropic-version": scan.API_HEADERS_VERSION,
                    "content-type": "application/json"}, json=body)
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                continue
            if r.status_code in (408, 409, 429) or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code}: {r.text[:140]}")
                continue
            r.raise_for_status()
            payload = r.json()
            break
        if payload is None:
            raise last or RuntimeError("claude call failed")

        scan.note_usage(payload.get("usage") or {})
        stop = payload.get("stop_reason")
        if stop == "refusal":
            raise RuntimeError("model declined this request (stop_reason=refusal)")
        if stop == "max_tokens":
            raise RuntimeError(f"hit max_tokens ({max_tokens}) before finishing")
        if stop == "pause_turn":
            # The server-side tool loop hit its iteration limit. Hand the assistant turn
            # straight back with no extra user message -- the API sees the trailing
            # server_tool_use block and resumes.
            messages.append({"role": "assistant", "content": payload.get("content", [])})
            continue
        return "".join(b.get("text", "") for b in payload.get("content", [])
                       if b.get("type") == "text")
    raise RuntimeError(f"server-tool turn did not finish in {SERVER_TOOL_MAX_TURNS} turns")


def research_salary(api_key, job):
    system = SALARY_SYSTEM.format(max_searches=SALARY_MAX_SEARCHES)
    user = (f"Title: {job.get('title')}\n"
            f"Company: {job.get('company') or 'not named in the posting'}\n"
            f"Location: {job.get('location')}\n"
            f"Market: {job.get('market')}\n"
            f"Stated salary on the posting: {job.get('salary') or 'none'}\n\n"
            f"Posting:\n{scan.sample_desc(job.get('description'), 4000)}")
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "max_uses": SALARY_MAX_SEARCHES}]
    text = claude_server_tool_call(api_key, SALARY_MODEL, system, user,
                                   SALARY_MAX_TOKENS, tools)
    return scan._extract_json(text)


def format_salary(res):
    cur = res.get("currency") or ""
    low, high = res.get("low"), res.get("high")
    band = (f"{cur} {int(low or 0):,}\u2013{int(high or 0):,}" if (low or high)
            else "no usable band")
    verdict = {"above_floor": "above floor", "borderline": "borderline",
               "below_floor": "BELOW FLOOR"}.get(res.get("verdict"),
                                                 res.get("verdict") or "?")
    second = [verdict]
    ruling = res.get("thirty_percent_ruling")
    if ruling and ruling not in ("n/a", "unknown"):
        second.append(f"30% ruling: {ruling}")
    lines = [f"<b>Salary  {esc(band)}</b>", esc(" · ".join(second))]
    # Two sources, clipped. The full list is in the packet; on a phone a third quote is
    # scrolling, not evidence.
    srcs = [x for x in (res.get("sources") or []) if str(x).strip()][:2]
    if srcs:
        lines.append("")
        lines += [f"\u2022 {esc(clip(x, 90))}" for x in srcs]
    if res.get("notes"):
        lines += ["", f"<i>{esc(clip(res['notes'], 160))}</i>"]
    return "\n".join(lines)


# ---------------------------------------------------------------- bullet audit

AUDIT_SYSTEM = """You are running Steps 3a-3d of Tom Norton's job-application-workflow \
against one posting: sub-track selection, bullet inventory, a decision per bullet, metric \
gap flags, and the gap analysis that decides what he gets asked.

SUB-TRACK SELECTION (do this first, it drives everything after it)
The bank labels bullets ANALYTICS / BUILDER / CS / ALL. Analytics and Builder are \
sub-tracks of RevOps, so a RevOps role still needs one picked.
- Analytics signals: revenue or capacity modelling, forecasting, territory and quota \
planning, SQL or BI in the requirements, business partnering with sales leadership. Larger \
established employers.
- Builder signals: systems ownership, automation, integrations, CRM administration, owning \
the GTM tool stack, first or early RevOps hire, Python or API mentions. Scaleups.
- Both fire: pick the one matching the top third of the posting's responsibilities. What a \
posting lists first is what the role does; what it lists last is a wish.
- Neither fires clearly: default to ANALYTICS.
- CS-family roles (CSM, Senior/Principal CSM, CS Ops, CS leadership) take CS and skip \
this check entirely.

INVENTORY
Candidates come from the bank first, then the base profile. Pull every bank bullet whose \
Tracks field matches the chosen track, plus everything marked ALL. Include VARIANT bullets \
only when the role genuinely matches the variant's angle. Skip RETIRED unless the posting \
makes one newly relevant, and say why if you resurrect it. Reference each candidate by its \
bank ID; anything with no bank ID is marked new_to_bank.

DECISION PER BULLET
One of: KEEP, KEEP+KEY, REVISE, REVISE+KEY, CUT, PROMOTE. Target 2-3 KEY bullets, the ones \
closest to the core responsibility of this role. CUT is about relevance, not about hiding \
weak work. A REVISE may not add a claim that is not already defensible from the bullet, the \
profile, or the bank's own evidence notes.

METRIC GAPS
Up to 3 KEEP or REVISE bullets that lack a concrete metric. For each, suggest 2-3 angles a \
real number could come from. NEVER propose a placeholder like "[X]%".

GAP ANALYSIS
Keywords the posting treats as important, that Tom could credibly address, that no existing \
bullet covers even after revision. For each one, check the ANSWER BANK first: if a previous \
interview already answered it, set answered_by to that answer's ID and do not ask again. \
Only genuinely uncovered gaps become questions. At most {max_gaps} questions, highest value \
first.

Each question must name the gap, ask whether he has specific experience that fits, and give \
2-4 concrete options -- never open-ended. One of the options is always a plain "no \
meaningful experience" wording.

WRITE THESE FOR A PHONE SCREEN. Tom reads them in a chat, standing up, and answers in one \
reply. Every question is ONE line under 90 characters, and every option is under 30. Ask \
the question directly: no preamble about what the posting says or emphasises, no restating \
the role, no "I noticed that". He knows which job this is. "Any pipeline hygiene or CRM \
cleanup at NAVEX?" is right; "The posting leans on pipeline hygiene and data quality, so \
did you do any of that work at NAVEX that isn't already on your CV?" is three times the \
length and says no more.

HARD RULES
- Never invent experience, and never treat the posting's own language as evidence Tom has \
done something. The posting tells you what to probe. Only Tom's answers say what he did.
- Be direct about a bad fit. If the bank has little that fits this role, say so in \
track_rationale rather than stretching bullets to cover it.

`answered_by` carries an answer-bank ID when the bank already answers the gap, and an empty \
string when it does not. Every gap you report needs a question written, even one the bank \
already answers -- the empty `answered_by` is what decides whether it gets asked."""

# Schemas rather than "return only JSON" and hope. The audit result is nested enough that a
# stray sentence before the object would cost a whole Opus call to find out.
AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "track": {"type": "string", "enum": ["ANALYTICS", "BUILDER", "CS"]},
        "track_rationale": {"type": "string"},
        "bullets": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "bank_id": {"type": "string"},
                "text_start": {"type": "string"},
                "decision": {"type": "string", "enum": ["KEEP", "KEEP+KEY", "REVISE",
                                                        "REVISE+KEY", "CUT", "PROMOTE"]},
                "rationale": {"type": "string"},
                "new_to_bank": {"type": "boolean"},
            },
            "required": ["bank_id", "text_start", "decision", "rationale", "new_to_bank"],
            "additionalProperties": False}},
        "metric_gaps": {"type": "array", "items": {
            "type": "object",
            "properties": {"bank_id": {"type": "string"}, "summary": {"type": "string"},
                           "angles": {"type": "array", "items": {"type": "string"}}},
            "required": ["bank_id", "summary", "angles"],
            "additionalProperties": False}},
        "gaps": {"type": "array", "items": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}, "adjacent": {"type": "string"},
                           "answered_by": {"type": "string"},
                           "question": {"type": "string"},
                           "options": {"type": "array", "items": {"type": "string"}}},
            "required": ["keyword", "adjacent", "answered_by", "question", "options"],
            "additionalProperties": False}},
    },
    "required": ["track", "track_rationale", "bullets", "metric_gaps", "gaps"],
    "additionalProperties": False,
}


def run_audit(api_key, job, profile, bank_md, answers_md):
    system = AUDIT_SYSTEM.format(max_gaps=MAX_GAP_QUESTIONS)
    user = (
        f"=== POSTING ===\n"
        f"Title: {job.get('title')}\nCompany: {job.get('company') or '?'}\n"
        f"Location: {job.get('location')}  Market: {job.get('market')}\n"
        f"Radar score: {job.get('score')} ({job.get('verdict') or ''})\n\n"
        f"{scan.sample_desc(job.get('description'), 6000)}\n\n"
        f"=== TOM'S PROFILE ===\n{profile}\n\n"
        f"=== BULLET BANK ===\n{bank_md or '(empty)'}\n\n"
        f"=== ANSWER BANK (previous gap interviews) ===\n{answers_md or '(empty)'}")
    text = scan._claude_call(
        api_key, AUDIT_MODEL, system, user, AUDIT_MAX_TOKENS,
        extra={"output_config": {"effort": AUDIT_EFFORT,
                                 "format": {"type": "json_schema", "schema": AUDIT_SCHEMA}}})
    return scan._extract_json(text)


def pending_gaps(audit):
    """Gaps that still need Tom. A gap the answer bank already covers never becomes a
    question -- that is the whole point of the bank, and it is why the questions thin out
    as it grows."""
    out = []
    for g in (audit.get("gaps") or []):
        if (g.get("answered_by") or "").strip():
            continue
        if not (g.get("question") or "").strip():
            continue
        out.append(g)
    return out[:MAX_GAP_QUESTIONS]


def build_questions(audit, job):
    """Every question this role will ask, in one list. The salary gate rides along as
    question 1 when the deep score flagged comp risk, because it needs an answer from Tom
    exactly like a gap does and there is no reason to spend a separate round trip on it.

    Returns [{kind, keyword, question, options}]."""
    qs = []
    risky, reason = comp_risk(job)
    if risky:
        # Short form of the risk, because the long form reads as an explanation Tom has
        # to parse before he can answer a yes/no. The full reason is in the packet.
        short = {"no salary stated in the posting": "No salary stated"}.get(
            reason, reason.split(" - ")[0].split(",")[0])
        qs.append({"kind": "salary", "keyword": "salary", "reason": reason,
                   "question": f"{clip(short, 70)}. Research the market band?",
                   "options": ["Yes, research it", "No, skip it"]})
    for g in pending_gaps(audit):
        qs.append({"kind": "gap", "keyword": g.get("keyword"),
                   "question": g.get("question"),
                   "options": [o for o in (g.get("options") or []) if str(o).strip()][:4],
                   "adjacent": g.get("adjacent")})
    return qs


def job_line(job, audit=None):
    """The one-line header every message about a role starts with. Company, market and
    score on a second dim line rather than a sentence, so the eye lands on the title."""
    bits = [job.get("company") or "?", job.get("market") or "",
            f"{job.get('score')}" if job.get("score") is not None else ""]
    if audit and audit.get("track"):
        bits.append(audit["track"])
    return (f"<b>{esc(clip(job.get('title'), 70))}</b>\n"
            + esc(" · ".join(b for b in bits if b)))


def format_questions(qs, job, audit):
    """One message carrying the whole conversation.

    Read on a phone, so it is built to be scanned rather than read: title, one dim context
    line, then the questions with nothing between them. The track rationale, the bullet
    decisions and the metric gaps are all deliberately absent -- they are in the packet,
    and on a phone they are wall-of-text between Tom and the thing he actually has to do.
    Everything the model wrote is clipped, because a model asked for one sentence will
    sometimes write four."""
    out = [job_line(job, audit), ""]
    for i, q in enumerate(qs, 1):
        out.append(f"<b>{i}</b>  {esc(clip(q.get('question'), Q_CHARS))}")
        for j, o in enumerate(q.get("options") or [], 1):
            out.append(f"    <code>{chr(96 + j)}</code>  {esc(clip(o, OPT_CHARS))}")
        out.append("")
    # No instructions about what to do if he has nothing -- every question carries a
    # "nothing here" option, so saying it again is a line of prose that earns nothing.
    out.append("<i>Reply once \u2014 " + ("\"1a\"" if len(qs) == 1 else "\"1a 2b\"")
               + ", or just write it.</i>")
    return "\n".join(out)


SPLIT_SYSTEM = """Tom was asked several numbered questions in one message and has replied \
in one message. Split his reply into one answer per question.

You are SPLITTING, not interpreting and not writing. For each question return Tom's own \
words, as close to verbatim as the split allows. Where he answered by picking a lettered \
option, return that option's text. Where he did not address a question at all, return an \
empty string for it -- do not infer an answer from his tone, from the other answers, or \
from what the question was hoping to hear. An unanswered question is a normal outcome and \
returning "" for it is the correct result.

Return ONLY a JSON object: {"answers": ["<for q1>", "<for q2>", ...]} with exactly one \
entry per question, in order."""


# One answer token: an optional question number, then the option letter. Covers "1a",
# "1. a", "a)", and a bare "b". Four options is the cap, hence a-d.
_KEY_TOKEN = r"(?:([1-9])\s*[.):]?\s*)?([a-dA-D])[.):,]?"
KEY_TOKEN_RE = re.compile(_KEY_TOKEN)
# The whole reply, and nothing but tokens. Separators between them are REQUIRED, so "bad"
# reads as a word rather than as picks b, a and d.
KEY_REPLY_RE = re.compile(rf"^{_KEY_TOKEN}(?:[\s,;/|]+{_KEY_TOKEN})*$")


def parse_answer_key(reply, qs):
    """A reply that is nothing but letters, resolved in code. Returns one answer per
    question, or None when the reply is prose and the model has to read it.

    This exists because the most common reply is the cheapest one to type on a phone:
    "1a 2b 3c", or just "a b c". Sending that to a model to be split is both a waste and a
    risk -- it was the risk that bit. Anything this parses is exact by construction."""
    r = " ".join((reply or "").split())
    if not r or not KEY_REPLY_RE.fullmatch(r):
        return None                        # a real word in there: prose, not a key
    picks = [(int(m.group(1)) - 1 if m.group(1) else None, m.group(2).lower())
             for m in KEY_TOKEN_RE.finditer(r)]
    if not picks or len(picks) > len(qs):
        return None

    out = [""] * len(qs)
    for i, (numbered, letter) in enumerate(picks):
        target = numbered if numbered is not None else i
        if not 0 <= target < len(qs):
            return None
        opts = [str(o) for o in (qs[target].get("options") or []) if str(o).strip()][:4]
        j = ord(letter) - 97
        if not 0 <= j < len(opts):
            return None
        out[target] = opts[j]
    return out


def offered_options(qs):
    """Every option string Tom was shown, normalised. An answer matching one of these was
    written by us, not invented by the splitter, so it is trustworthy by definition."""
    seen = set()
    for q in qs or []:
        for o in (q.get("options") or []):
            norm = " ".join(str(o or "").lower().split())
            if norm:
                seen.add(norm)
    return seen


def split_reply(api_key, qs, reply):
    """One cheap call to map a single human reply onto N questions.

    Deliberately a model call and not a regex: people answer "yeah did that one for a
    couple of quarters, nothing on the second, skip the salary thing" and no pattern
    survives that. The prompt and the schema both constrain it to splitting rather than
    writing, and split_is_sane() below rejects a result that invented text."""
    numbered = "\n".join(
        f"{i}. {q['question']}"
        + ("".join(f"\n     {chr(96 + j)}) {o}" for j, o in enumerate(q.get('options') or [], 1)))
        for i, q in enumerate(qs, 1))
    schema = {"type": "object",
              "properties": {"answers": {"type": "array", "items": {"type": "string"}}},
              "required": ["answers"], "additionalProperties": False}
    text = scan._claude_call(
        api_key, SPLIT_MODEL, SPLIT_SYSTEM,
        f"QUESTIONS:\n{numbered}\n\nTOM'S REPLY:\n{reply}", SPLIT_MAX_TOKENS,
        extra={"output_config": {"effort": "low",
                                 "format": {"type": "json_schema", "schema": schema}}})
    out = (scan._extract_json(text).get("answers") or [])
    out = [str(a or "").strip() for a in out][:len(qs)]
    return out + [""] * (len(qs) - len(out))


def split_is_sane(answers, reply, qs=None):
    """Cheap guard against the splitter writing rather than splitting. Every answer it
    returns has to be traceable to something in Tom's reply, or be one of the options he
    was actually offered; an answer that is neither is discarded, and a discarded answer
    is an unanswered question, which produces no bullet.

    This exists because the whole build rests on bullets coming from Tom's words. A
    splitter that paraphrases him into something more useful would break that quietly.

    The options half of that rule was in this docstring from the start and was never in the
    code, which broke every lettered reply. The split prompt asks for an option's TEXT when
    Tom picks a letter, and "Yes, pipeline cleanup" shares no words with a reply of
    "1a 2b 3c" -- so the guard threw away every answer after the first one he happened to
    write out in full. An option we put in front of him is not something a model invented."""
    hay = re.sub(r"[^a-z0-9 ]+", " ", (reply or "").lower())
    hay_words = set(hay.split())
    offered = offered_options(qs)
    clean = []
    for a in answers:
        norm = " ".join(str(a or "").lower().split())
        words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", a.lower()).split() if len(w) > 3]
        if not a:
            clean.append("")
        elif norm in offered:
            clean.append(a)
        elif not words or sum(w in hay_words for w in words) / len(words) >= 0.5:
            clean.append(a)
        else:
            print(f"  split discarded (not traceable to the reply): {a[:60]!r}")
            clean.append("")
    return clean


def is_yes(text):
    return bool(re.match(r"^\s*(a\)?|1\.?|y|yes|yep|yeah|sure|go|do it|please|ok)\b",
                         (text or "").strip(), re.I))


def resolve_answer(text, gap):
    """A bare number or letter picks an option; anything else is taken verbatim. Tom
    answering "b" and Tom answering a paragraph both have to work, because which one he
    sends depends on whether he is walking."""
    text = (text or "").strip()
    opts = [str(o) for o in (gap.get("options") or []) if str(o).strip()][:4]
    m = re.fullmatch(r"([1-9])\.?", text) or re.fullmatch(r"([a-d])\)?", text, re.I)
    if m and opts:
        tok = m.group(1)
        i = (int(tok) - 1) if tok.isdigit() else (ord(tok.lower()) - 97)
        if 0 <= i < len(opts):
            return opts[i]
    return text


def has_material(answer):
    """The honesty hard stop, in one place. An empty answer or a 'nothing here' answer
    yields no bullet, ever. No inference fills a gap."""
    a = (answer or "").strip()
    return bool(a) and not NO_MATERIAL.match(a)


# ---------------------------------------------------------------- new bullets

DRAFT_SYSTEM = """Draft resume bullets for Tom Norton from his own interview answers.

THE ONLY SOURCE OF FACT IS WHAT TOM SAID. The job posting told us which gap to probe. It is \
not evidence that he did anything. Do not import a responsibility, a tool, a scale or a \
number from the posting, the company, or the general shape of the role. If his answer does \
not support a bullet that would survive an interviewer asking "tell me more about this", \
return no bullet for that gap and say why in `dropped`.

Style, from Tom's own rules:
- No first person. Bullets start with a past-tense verb.
- No em dashes. No "leverage", "spearheaded", "utilised", "robust", "seamless".
- No negative parallelism ("not X, but Y").
- Plain and short beats impressive. If his answer has no number, write the bullet without \
one rather than reaching for a vague quantifier.
- Never write a placeholder metric.

Every gap you are given ends up in exactly one of `bullets` or `dropped`. Dropping one is a \
correct outcome, not a failure."""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "bullets": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "gap": {"type": "string"}, "text": {"type": "string"},
                "tracks": {"type": "string",
                           "enum": ["ANALYTICS", "BUILDER", "CS", "ALL"]},
                "competencies": {"type": "string"}, "evidence": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["gap", "text", "tracks", "competencies", "evidence", "notes"],
            "additionalProperties": False}},
        "dropped": {"type": "array", "items": {
            "type": "object",
            "properties": {"gap": {"type": "string"}, "why": {"type": "string"}},
            "required": ["gap", "why"], "additionalProperties": False}},
    },
    "required": ["bullets", "dropped"],
    "additionalProperties": False,
}


def draft_bullets(api_key, job, track, answered):
    """answered is [(gap, answer)] and has already been filtered to answers with material
    in them. Nothing else reaches this call: a gap Tom did not answer is not in the input,
    so there is no path by which it becomes a bullet."""
    if not answered:
        return {"bullets": [], "dropped": []}
    blocks = []
    for gap, answer in answered:
        blocks.append(f"GAP: {gap.get('keyword')}\n"
                      f"ASKED: {gap.get('question')}\n"
                      f"TOM'S ANSWER (verbatim, the only evidence): {answer}")
    system = DRAFT_SYSTEM + f"\n\nToday is {today()}; use it in `evidence`."
    user = (f"Role being applied to (context only, NOT evidence): "
            f"{job.get('title')} at {job.get('company') or '?'}\n"
            f"Track: {track}\n\n" + "\n\n".join(blocks))
    text = scan._claude_call(
        api_key, DRAFT_MODEL, system, user, DRAFT_MAX_TOKENS,
        extra={"output_config": {"effort": AUDIT_EFFORT,
                                 "format": {"type": "json_schema", "schema": DRAFT_SCHEMA}}})
    return scan._extract_json(text)


# ---------------------------------------------------------------- company brief (Step 2)

BRIEF_SYSTEM = """You are writing Step 2 of Tom Norton's job-application-workflow: a short \
strategic brief on one company. Its only job is to make the CV summary sharper, so it is \
about where the company is going, not what it does.

Use web search, at most {max_searches} searches, then stop. Priority order: company blog, \
press releases and announcements from the last six months; the careers page and the "why \
we're hiring" language in the posting itself; leadership posts; funding or earnings news; \
product launches, new markets, new pricing; culture signals from team pages.

Rules that decide whether this is worth anything:
- Every priority and every challenge carries the evidence it came from. A priority you \
cannot point at is a guess, and a guess in a CV summary is a question Tom cannot answer in \
the interview.
- Cultural signals are specific or they are omitted. "They publish a customer-facing \
postmortem after every outage" is a signal. "They value innovation" is filler.
- If the search turns up little, say so. A thin brief is a fact about the company's \
public footprint, and inventing a strategy to fill the space is worse than an empty field.

When you have finished searching, answer with ONLY a JSON object and nothing else:
{{"priorities": [{{"priority": "<one line>", "evidence": "<source and what it said>"}}],
  "challenges": [{{"challenge": "<one line>", "evidence": "<source>"}}],
  "why_hiring": "<2-3 sentences connecting the role to those priorities>",
  "culture": ["<specific signal>"],
  "visa_note": "<sponsorship track record, or 'unknown'>",
  "thin": true | false}}

At most three priorities, two challenges, three culture signals. `thin` is true when the \
search did not find enough to say anything useful."""


def research_brief(api_key, job):
    user = (f"Company: {job.get('company') or 'not named in the posting'}\n"
            f"Role: {job.get('title')}\n"
            f"Location: {job.get('location')}  Market: {job.get('market')}\n\n"
            f"Posting:\n{scan.sample_desc(job.get('description'), 4000)}")
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "max_uses": BRIEF_MAX_SEARCHES}]
    text = claude_server_tool_call(api_key, BRIEF_MODEL,
                                   BRIEF_SYSTEM.format(max_searches=BRIEF_MAX_SEARCHES),
                                   user, BRIEF_MAX_TOKENS, tools)
    return scan._extract_json(text)


def brief_block(brief):
    """The brief as the tailoring call sees it. Empty when there is no brief, rather than a
    heading over nothing -- a section header with nothing under it reads to a model as
    something it should fill in."""
    if not brief or brief.get("thin"):
        return ""
    L = []
    for p in (brief.get("priorities") or [])[:3]:
        L.append(f"- PRIORITY: {p.get('priority')} (evidence: {p.get('evidence')})")
    for c in (brief.get("challenges") or [])[:2]:
        L.append(f"- CHALLENGE: {c.get('challenge')} (evidence: {c.get('evidence')})")
    if brief.get("why_hiring"):
        L.append(f"- WHY THIS ROLE EXISTS: {brief['why_hiring']}")
    for c in (brief.get("culture") or [])[:3]:
        L.append(f"- CULTURE: {c}")
    return "\n".join(L)


# ---------------------------------------------------------------- tailoring (Step 4)

TAILOR_SYSTEM = """You are running Steps 4b-4e of Tom Norton's job-application-workflow: \
turning an already-completed bullet audit into the actual contents of one tailored CV.

You choose the WORDS. You do not choose the layout: section order, project order, page \
setup, fonts and the role-title fallback are all decided in code from the track, and \
nothing you return can change them.

WHAT YOU ARE GIVEN
The audit's per-bullet decisions (KEEP / KEEP+KEY / REVISE / REVISE+KEY / CUT / PROMOTE), \
the bullet bank those IDs refer to, the CV skeleton with its entry IDs, any bullets already \
drafted from Tom's own interview answers, and a company brief if one was findable.

BULLETS
Place every surviving bullet against the skeleton entry it belongs to, using entry_id. An \
entry_id is one JOB, not one employer: LexisNexis has two of them, and a bullet from the \
Corporate Legal years does not belong under Print & Digital. KEEP means the bank's text, \
unchanged. REVISE means keyword polish and re-angling of the bank's text, never a new \
claim. CUT means it does not appear. New bullets from the interview go on the role or \
project they actually happened at.

A PROJECT entry_id takes exactly one bullet and it is the body only. Its lead-in (the \
project's name and what it was) is fixed and is added in code. Return no bullet for a \
project and its base text is kept, which is usually the right answer -- the projects are \
already tuned and there is rarely anything a posting can teach them.

An entry_id you return NOTHING for keeps the base CV's own bullets. That is a real choice \
and often the correct one. Return an entry only when this posting changes what should be \
on it.

The bank's Notes carry SCOPE GUARDs and METRIC GUARDs written after real interviews: what \
Tom did and did not do, and which numbers do not exist. They are binding. A guard saying \
never write "led" or "ran" on a bullet means exactly that, and a METRIC GUARD saying no \
number attaches means you do not attach one.

THE HARD LIMIT ON REVISION: every fact, tool, scale and number in a bullet you return must \
already be in the bank's text for that bullet, in the skeleton's own text, or in a drafted \
bullet. The job posting is not evidence. It tells you what to look for; only Tom's history \
says what he did. A revision that adds a number the source did not have is discarded in \
code before it reaches the page, so writing one costs the bullet its place on the CV.

Aim for 2-3 KEY bullets, first in their entry, closest to this role's core responsibility.

AT MOST SIX BULLETS ON ANY ONE JOB, and order them so the six that survive are the six
that matter to THIS posting. When there is more good material than that, do not just stop
at six and let the rest fall off the end: decide which are least relevant and cut them, or
merge two related ones into a single bullet that carries both facts. Anything past the
sixth is dropped in code, so an unordered list loses whatever happened to be last.

SUMMARY -- three variations, scored
Start from the bank's canonical summary for this track (SUM-ANALYTICS / SUM-BUILDER / \
SUM-CS). Do not write from a blank page: those are tuned and rebuilding them loses the \
tuning.

HARD LIMIT: four printed lines on the CV. That is 11pt Calibri across a seven-inch \
measure, which is roughly 105 characters a line, so keep every variation UNDER 400 \
CHARACTERS. Count them. Three or four short sentences, not four long ones. A summary over \
the limit gets its last sentence deleted in code before it prints, so a fifth sentence is \
not extra content, it is a sentence you wrote and nobody reads.

Three variations, all within that limit:
  A. Canonical-tight -- the bank summary with keyword swaps and light phrasing edits. The \
floor, and often the right answer.
  B. Role-forward -- resequenced to lead with this JD's core function.
  C. Company-forward -- leads with alignment to a priority from the company brief. If \
there is no brief, make C lead with the JD's stated mission instead and say so in `changed`.
Score each out of 10 for recruiter impact, with a one-line reason and a one-line note on \
what it changed from the canonical. SCORE HONESTLY AND MAKE THEM DIFFER. Three 8/10s is \
not a review, it is a shrug.

SKILLS
5-7 skills, most relevant first, separated by " | ". Only skills Tom actually has, and only \
from the bank's SKILLS POOL, which also records what is ruled out and why. Start from the \
standing line for this track and change it only where the posting gives a reason.

STYLE, non-negotiable
No first person. Bullets start with a past-tense verb. No em dashes. No "leverage", \
"spearheaded", "utilised", "robust", "seamless", "orchestrated". No negative parallelism \
("not X, but Y"). No placeholder metrics, ever. Plain and short beats impressive: where an \
answer has no number, write the bullet without one rather than reaching for a vague \
quantifier.

`role_title_usable` is false only when the posting's title would be embarrassing or \
meaningless on a CV -- internal jargon, a made-up seniority ladder, or nothing at all. A \
plain, ordinary title is usable even if it is not the exact wording Tom would have picked."""

TAILOR_SCHEMA = {
    "type": "object",
    "properties": {
        "role_title_usable": {"type": "boolean"},
        "entries": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
                "bullets": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"text": {"type": "string"},
                                   "source": {"type": "string"},
                                   "key": {"type": "boolean"}},
                    "required": ["text", "source", "key"],
                    "additionalProperties": False}},
            },
            "required": ["entry_id", "bullets"], "additionalProperties": False}},
        "summaries": {"type": "array", "items": {
            "type": "object",
            "properties": {"label": {"type": "string", "enum": ["A", "B", "C"]},
                           "angle": {"type": "string"},
                           "score": {"type": "number"},
                           "why": {"type": "string"},
                           "changed": {"type": "string"},
                           "text": {"type": "string"}},
            "required": ["label", "angle", "score", "why", "changed", "text"],
            "additionalProperties": False}},
        "skills": {"type": "string"},
        "keywords": {"type": "array", "items": {
            "type": "object",
            "properties": {"keyword": {"type": "string"},
                           "status": {"type": "string",
                                      "enum": ["Present", "Addressable",
                                               "Not addressable"]}},
            "required": ["keyword", "status"], "additionalProperties": False}},
        "changes": {"type": "array", "items": {
            "type": "object",
            "properties": {"section": {"type": "string"}, "original": {"type": "string"},
                           "revised": {"type": "string"}, "keywords": {"type": "string"},
                           "evidence": {"type": "string"}},
            "required": ["section", "original", "revised", "keywords", "evidence"],
            "additionalProperties": False}},
    },
    "required": ["role_title_usable", "entries", "summaries", "skills", "keywords",
                 "changes"],
    "additionalProperties": False,
}


def skeleton_block(base):
    """The CV skeleton as the tailoring call sees it: every entry_id, what it is, and the
    base CV's own bullets sitting on it.

    The base bullets travel because they are part of what a revision is allowed to be made
    of, and because they are the floor -- a role the tailoring pass says nothing about
    keeps them rather than going blank."""
    idx = cvbuild.entry_index(base)
    L = []
    for eid in cvbuild.entry_ids(base):
        slot = idx.get(eid) or {}
        if slot.get("kind") == "project":
            p = slot["project"]
            L.append(f"- entry_id `{eid}` (PROJECT, one bullet): lead-in "
                     f"\"{p.get('lead')}\" is fixed and must not change; return only the "
                     f"body that follows it")
            L.append(f"    (already on the base CV) {p.get('text')}")
            continue
        entry, role = slot.get("entry") or {}, slot.get("role") or {}
        L.append(f"- entry_id `{eid}`: {entry.get('left')} - {role.get('sub_left')} "
                 f"({role.get('sub_right')})")
        for b in (role.get("bullets") or []):
            L.append(f"    (already on the base CV) {cvbuild.bullet_text(b)}")
    return "\n".join(L)


def tailor_cv(api_key, job, audit, brief, base, bank_md, drafts):
    drafted = [b.get("text") for b in ((drafts or {}).get("bullets") or [])
               if (b.get("text") or "").strip()]
    decisions = "\n".join(
        f"- {b.get('bank_id') or '(new)'}: {b.get('decision')} -- {b.get('text_start')}"
        for b in (audit.get("bullets") or []))
    parts = [
        f"=== POSTING ===\nTitle: {job.get('title')}\n"
        f"Company: {job.get('company') or '?'}\nMarket: {job.get('market')}\n\n"
        f"{scan.sample_desc(job.get('description'), 6000)}",
        f"=== TRACK (decided already, do not revisit) ===\n{audit.get('track')} -- "
        f"{audit.get('track_rationale', '')}",
        f"=== AUDIT DECISIONS ===\n{decisions or '(none)'}",
        f"=== BULLET BANK ===\n{bank_md or '(empty)'}",
        f"=== CV SKELETON (place bullets against these entry IDs) ===\n"
        f"{skeleton_block(base)}",
    ]
    if drafted:
        parts.append("=== NEW BULLETS FROM TOM'S OWN INTERVIEW ANSWERS ===\n"
                     + "\n".join(f"- {d}" for d in drafted))
    block = brief_block(brief)
    parts.append(f"=== COMPANY BRIEF ===\n{block}" if block
                 else "=== COMPANY BRIEF ===\n(none found; variation C leads with the "
                      "posting's own stated mission)")
    text = scan._claude_call(
        api_key, TAILOR_MODEL, TAILOR_SYSTEM, "\n\n".join(parts), TAILOR_MAX_TOKENS,
        extra={"output_config": {"effort": AUDIT_EFFORT,
                                 "format": {"type": "json_schema",
                                            "schema": TAILOR_SCHEMA}}})
    return scan._extract_json(text)


def screened_entries(tailored, base, corpus):
    """(bullets_by_entry, rejected). Every bullet the tailoring call produced goes through
    cvbuild's honesty screen before it can reach the page, and an unknown entry_id is
    dropped rather than guessed at."""
    known = set(cvbuild.entry_ids(base))
    by_entry, rejected = {}, []
    for e in (tailored.get("entries") or []):
        eid = (e.get("entry_id") or "").strip()
        if eid not in known:
            for b in (e.get("bullets") or []):
                rejected.append((b.get("text", ""), f"unknown entry_id {eid!r}"))
            continue
        texts = [b.get("text", "") for b in (e.get("bullets") or [])]
        kept, bad = cvbuild.screen_bullets(texts, corpus)
        # Six per job. Screened first, so a bullet the honesty check threw out does not use
        # up one of the six.
        kept, over = cvbuild.cap_bullets(kept)
        rejected += bad + [(b, f"over the {cvbuild.MAX_BULLETS_PER_ROLE}-bullet limit on "
                               f"this job") for b in over]
        if kept:
            by_entry.setdefault(eid, []).extend(kept)
    return by_entry, rejected


def summaries_of(tailored):
    """Variations in label order, highest score first among equals. Anything without text
    is dropped: an empty variation on a phone is a letter Tom can pick that does nothing."""
    out = [s for s in (tailored.get("summaries") or []) if (s.get("text") or "").strip()]
    order = {l: i for i, l in enumerate(VARIATION_LABELS)}
    out.sort(key=lambda s: order.get(s.get("label"), 9))
    return out


def best_summary(summaries):
    """The one the agent picks when the score is below the review gate. Ties break towards
    A, because A is the bank canonical with light edits and the canonical is the tuned
    version -- a tie is not a reason to move away from it."""
    if not summaries:
        return None
    order = {l: i for i, l in enumerate(VARIATION_LABELS)}
    return sorted(summaries, key=lambda s: (-float(s.get("score") or 0),
                                            order.get(s.get("label"), 9)))[0]


def format_variations(summaries, job, audit):
    """The one round trip Phase 2 gets. Three summaries is already a lot of text for a
    phone, so everything except the summaries themselves is a single dim line."""
    out = [job_line(job, audit), "", "<i>Pick a summary. Everything else is decided.</i>", ""]
    for s in summaries:
        score = float(s.get("score") or 0)
        out.append(f"<b>{esc(s.get('label'))}</b>  {esc(clip(s.get('angle'), 34))} "
                   f"· <b>{score:g}/10</b>")
        out.append(esc(clip(s.get("text"), SUMMARY_CHARS)))
        if s.get("changed"):
            out.append(f"<i>{esc(clip(s.get('changed'), CHANGED_CHARS))}</i>")
        out.append("")
    out.append("<i>Reply with a letter.</i>")
    return "\n".join(out)


def format_auto_pick(chosen, summaries, job, audit):
    """Below the gate, no question is asked. The message still has to say which one was
    taken and what the alternatives scored, because a pick nobody sees is a pick nobody can
    disagree with."""
    others = " · ".join(f"{s.get('label')} {float(s.get('score') or 0):g}"
                        for s in summaries if s.get("label") != chosen.get("label"))
    out = [job_line(job, audit), "",
           f"<b>Summary {esc(chosen.get('label'))}</b> "
           f"· {float(chosen.get('score') or 0):g}/10 · "
           f"{esc(clip(chosen.get('angle'), 30))}", "",
           esc(clip(chosen.get("text"), SUMMARY_CHARS))]
    if others:
        out += ["", f"<i>Picked for you, below the {VARIATION_REVIEW_MIN_SCORE} review "
                    f"line. Also scored: {esc(others)}.</i>"]
    return "\n".join(out)


def resolve_pick(text, summaries):
    """A letter, a number, or the word. Anything unrecognised means the highest score,
    because a role does not stall on an ambiguous reply."""
    t = (text or "").strip().lower()
    m = re.match(r"^\s*([abc])\b", t) or re.match(r"^\s*([123])\b", t)
    if m:
        tok = m.group(1)
        label = VARIATION_LABELS[int(tok) - 1] if tok.isdigit() else tok.upper()
        for s in summaries:
            if s.get("label") == label:
                return s, True
    return best_summary(summaries), False


# ---------------------------------------------------------------- revision (/redo)

REVISE_SYSTEM = """A tailored CV was built for Tom Norton and sent to him. He has read it \
and asked for a change. Rebuild the page with that change made, and change nothing he did \
not ask about.

You are given the CV exactly as it went out, his feedback, the bullet bank, the CV \
skeleton, and any bullets drafted from his own interview answers. Return the whole page \
again: the summary, the bullets per entry, and the skills line. Anything he did not \
mention comes back identical, character for character. A revision that quietly improves \
three other bullets is not a revision, it is a second draft, and he has already read and \
approved the first one.

WHAT HIS FEEDBACK CANNOT DO. It is an instruction about the page, not a new source of \
fact. If he asks for something the bank, the skeleton and his own answers cannot support \
-- a number that exists nowhere, a responsibility he has not described, a stronger verb \
than a SCOPE GUARD allows -- do not write it. Say so in `notes`, plainly, and return the \
page without it. He would rather be told than find out in an interview. Every claim you \
return still has to trace back to those sources; a bullet that does not is discarded in \
code before it prints, so writing one costs the bullet its place.

The limits still hold: at most six bullets on any one job, ordered so the six that survive \
are the six that matter; the summary under 400 characters so it fits four printed lines; \
no first person; no em dashes; no placeholder metrics.

`notes` is one or two sentences for Tom: what you changed, and anything you would not do \
and why. Write it to be read on a phone."""

REVISE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "entries": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
                "bullets": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"text": {"type": "string"},
                                   "source": {"type": "string"},
                                   "key": {"type": "boolean"}},
                    "required": ["text", "source", "key"],
                    "additionalProperties": False}},
            },
            "required": ["entry_id", "bullets"], "additionalProperties": False}},
        "skills": {"type": "string"},
        "changes": {"type": "array", "items": {
            "type": "object",
            "properties": {"section": {"type": "string"}, "original": {"type": "string"},
                           "revised": {"type": "string"}, "keywords": {"type": "string"},
                           "evidence": {"type": "string"}},
            "required": ["section", "original", "revised", "keywords", "evidence"],
            "additionalProperties": False}},
        "notes": {"type": "string"},
    },
    "required": ["summary", "entries", "skills", "changes", "notes"],
    "additionalProperties": False,
}


def as_built_block(spec):
    """The CV as it actually printed, by entry_id, so the revision edits the page Tom read
    rather than the tailoring output he never saw."""
    L = [f"ROLE TITLE: {spec.get('role_title')}", "",
         f"SUMMARY: {spec.get('summary')}", "",
         f"SKILLS: {spec.get('skills')}", ""]
    for section in spec.get("sections") or []:
        L.append(f"[{section.get('heading')}]")
        for b in section.get("bullets") or []:
            L.append(f"  - {cvbuild.bullet_text(b)}")
        for e in section.get("entries") or []:
            for role in e.get("roles") or []:
                L.append(f"  {e.get('left')} - {role.get('sub_left')}")
                for b in (role.get("bullets") or []):
                    L.append(f"    - {cvbuild.bullet_text(b)}")
    return "\n".join(L)


PACKET_TRACK_RE = re.compile(r"^- Track:\s*\*\*(\w+)\*\*", re.M)
PACKET_CV_TITLE_RE = re.compile(r"^- Role title on the CV:\s*\*\*(.+?)\*\*", re.M)


def recoverable_cv(state):
    """The most recent finished role that actually produced a PDF, or None."""
    return next((h for h in reversed(state.get("history") or []) if h.get("cv")), None)


def recover_last_cv(bank, state):
    """Rebuild a revisable role from the BANK when the run state does not carry one.

    The state is a cache; the packet and the PDF are the durable record. Without this, a
    CV built before /redo existed -- or after any state reset -- answers "no CV to revise
    yet" while the PDF sits in the same repo, which is both wrong and infuriating.

    The page comes back as the text of the PDF Tom actually read, which is a better input
    to a revision than a reconstruction of it would be."""
    done = recoverable_cv(state)
    if not done:
        return None
    pdf = bank.read_bytes(done["cv"])
    if not pdf:
        return None
    cvbuild.ensure_toolchain()
    tmp = os.path.join(CV_OUT_DIR, "recovered.pdf")
    os.makedirs(CV_OUT_DIR, exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(pdf)
    as_built = cvbuild.pdf_text(tmp)

    packet = bank.read(f"{PACKET_DIR}/{done['packet']}", "") if done.get("packet") else ""
    track = (PACKET_TRACK_RE.search(packet) or [None, ""])[1] if packet else ""
    cv_title = (PACKET_CV_TITLE_RE.search(packet) or [None, ""])[1] if packet else ""
    return {
        "id": done.get("id"), "title": done.get("title"),
        "company": done.get("company"), "packet_file": done.get("packet"),
        "cv_title": cv_title or done.get("title"),
        "cv_stem": os.path.splitext(os.path.basename(done.get("cv") or ""))[0],
        "audit": {"track": track or "ANALYTICS"},
        # The title on the page came off the posting, and a recovery must not silently
        # swap it for the track's standing default.
        "tailored": {"role_title_usable": True},
        "as_built": as_built,
        "recovered": True,
        "usage": {},
    }


def revise_cv(api_key, job, audit, as_built, feedback, base, bank_md, drafts):
    drafted = [b.get("text") for b in ((drafts or {}).get("bullets") or [])
               if (b.get("text") or "").strip()]
    parts = [
        f"=== TOM'S FEEDBACK ===\n{feedback}",
        f"=== THE CV AS IT WENT OUT ===\n{as_built}",
        f"=== THE POSTING ===\n{job.get('title')} at {job.get('company') or '?'}\n"
        f"Track: {audit.get('track')}\n\n"
        f"{scan.sample_desc(job.get('description'), 4000)}",
        f"=== BULLET BANK ===\n{bank_md or '(empty)'}",
        f"=== CV SKELETON ===\n{skeleton_block(base)}",
    ]
    if drafted:
        parts.append("=== BULLETS FROM TOM'S OWN INTERVIEW ANSWERS ===\n"
                     + "\n".join(f"- {d}" for d in drafted))
    text = scan._claude_call(
        api_key, TAILOR_MODEL, REVISE_SYSTEM, "\n\n".join(parts), TAILOR_MAX_TOKENS,
        extra={"output_config": {"effort": AUDIT_EFFORT,
                                 "format": {"type": "json_schema",
                                            "schema": REVISE_SCHEMA}}})
    return scan._extract_json(text)


# ---------------------------------------------------------------- cover letter (Step 5)

COVER_SYSTEM = """You are writing Step 5 of Tom Norton's job-application-workflow: one \
cover letter for a role whose tailored CV has already been built and sent to him.

You choose the WORDS. You do not choose the layout. The letterhead, the date, the recipient \
block, the subject line, the salutation and the sign-off are all written in code and are \
already on the page. Write the paragraphs and nothing else -- no greeting, no date, no \
address, no "Sincerely", no name. Anything of that kind you return prints twice.

5a THE HOOK. Pick ONE, from the company brief: a named forward priority, a challenge Tom \
has solved before, domain overlap, or the MBA's relevance to the phase the company is in. \
One. A letter that opens on three things opens on nothing. Name the one you took, and why, \
in `hook`.

5b HOW IT OPENS. The first sentence IS the hook. Never "I am writing to express my \
interest". Mirror the posting's own language for the work without parroting its sentences \
back at it. Reference one specific cultural signal from the brief -- the concrete kind \
("they publish a customer-facing postmortem after every outage"), never the generic kind.

5c HOW IT READS. One page: an opening, two body paragraphs, a close. Warm, polished, \
assertive, diplomatic. The register of somebody who expects to be taken seriously and is \
easy to talk to. Flowing prose with the value front-loaded. No em dashes. No "synergy", \
"deep dive", "touch base", "circle back", "move the needle". No "spearheaded", "leveraged", \
"orchestrated". No "In today's rapidly evolving landscape". No "it is not X, it is Y". No \
lists, no bullets, no headings: it is a letter. No hedging adverbs.

WHAT YOU MAY SAY HE HAS DONE. Only what is already on the CV as it went out, in his own \
interview answers, or in the bullet bank behind them. This letter re-angles that material \
for this company; it does not add to it. THE POSTING IS NOT EVIDENCE. It says what they are \
looking for, never what he did, and the moment it counts as evidence the whole honesty rule \
is gone. A number that is not in his sources does not exist. Neither does a responsibility \
he has not described, nor a stronger verb than a SCOPE GUARD allows.

`claims` is how that gets checked. For every paragraph, list each assertion it makes about \
Tom's own experience, restated as a standalone sentence in that paragraph's own words. Each \
one is screened in code against his sources, and a claim that fails takes its whole \
paragraph off the page with it. So: a paragraph whose claims you cannot source is a \
paragraph not to write, and one that quietly asserts something it did not declare is the \
thing this rule exists to stop. What you say about the COMPANY has to come from the brief or \
the posting on the same terms.

Say nothing about visas, relocation or work authorisation unless the posting or the brief \
raises it first, and even then only what those two actually say.

`notes` is one or two sentences for Tom: the angle you took, and anything you decided not to \
say and why. Written to be read on a phone."""

COVER_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string"},
        "paragraphs": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": ["opening", "body", "closing"]},
                "text": {"type": "string"},
                "claims": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["role", "text", "claims"], "additionalProperties": False}},
        "notes": {"type": "string"},
    },
    "required": ["hook", "paragraphs", "notes"],
    "additionalProperties": False,
}


def cover_answers_block(answers):
    """Tom's own words from the gap interview, as the letter writer sees them.

    Included because they are the one source that is his voice rather than a bullet's, and
    a letter written only off the finished page reads like a page read aloud."""
    said = [a for a in (answers or []) if (a.get("answer") or "").strip()
            and a.get("has_material") is not False]
    return "\n".join(f"- {a.get('question')}\n  {a.get('answer')}" for a in said)


def write_cover(api_key, job, audit, brief, as_built, answers, steer=""):
    parts = []
    if (steer or "").strip():
        # First, and labelled as his, because everything under it is context and this is an
        # instruction.
        parts.append(f"=== WHAT TOM ASKED FOR ===\n{steer.strip()}")
    parts.append(f"=== THE POSTING ===\n{job.get('title')} at {job.get('company') or '?'}\n"
                 f"Location: {job.get('location')}  Market: {job.get('market')}\n"
                 f"Track: {audit.get('track')}\n\n"
                 f"{scan.sample_desc(job.get('description'), 4000)}")
    parts.append("=== COMPANY BRIEF ===\n" + (brief_block(brief) or
                 "(nothing usable: the research came back thin or was never run. Take the "
                 "hook from the posting's own language instead, and say in `notes` that the "
                 "letter is thinner for it.)"))
    parts.append(f"=== THE CV AS IT WENT OUT ===\n{as_built}")
    said = cover_answers_block(answers)
    if said:
        parts.append(f"=== TOM'S OWN WORDS, FROM THE GAP INTERVIEW ===\n{said}")
    text = scan._claude_call(
        api_key, COVER_MODEL, COVER_SYSTEM, "\n\n".join(parts), COVER_MAX_TOKENS,
        extra={"output_config": {"effort": AUDIT_EFFORT,
                                 "format": {"type": "json_schema",
                                            "schema": COVER_SCHEMA}}})
    return scan._extract_json(text)


# ---------------------------------------------------------------- bank write-back (Step 9)

BANKWRITE_SYSTEM = """You are running Step 9a of Tom Norton's job-application-workflow: \
deciding what, if anything, from this application belongs back in the permanent bullet bank.

THE DEFAULT IS NO. Most tailored rewrites are job-specific and must not touch the bank. A \
revision is promoted only if it would improve the bullet for MOST FUTURE ROLES.

All four tests, and failing any one means it is not promoted:
1. Portability. Strip every reference to this company, this posting's vocabulary and this \
role's title. Is what remains still better than the current canonical? If the improvement \
evaporates, it was tailoring.
2. New substance. Does it add a real fact, metric, scope detail or outcome the canonical \
lacks? A keyword swap, a synonym or a reorder is not substance.
3. Cross-track. Would it still be the version to reach for on a role in a different track? \
If it now reads worse on another track, it is a VARIANT, not a replacement.
4. Defensibility. Same interview-defensibility bar as the canonical, against the CV, the \
STAR bank or Tom's own confirmed words.

What always goes back regardless: NEW bullets built from Tom's interview answers that were \
used on this CV. Those are real experience that existed nowhere before this session.

Anything that fails the test but is still worth having is a VARIANT logged on the existing \
bullet. Anything that is merely a keyword reshuffle is DISCARDED, not logged -- the bank is \
a library, not an archive.

Return ONLY JSON:
{"changes": [{"kind": "PROMOTE" | "ADD" | "VARIANT",
              "bank_id": "<existing ID, or the ID prefix a new bullet should follow>",
              "title": "<short title, ADD only>",
              "text": "<the bullet text>",
              "tracks": "ANALYTICS" | "BUILDER" | "CS" | "ALL",
              "competencies": "<comma list>",
              "why": "<one clause>"}],
 "didnt_qualify": ["<one clause each for rewrites that read better here but failed>"]}

An empty `changes` list is a normal and correct outcome. Say so rather than finding \
something."""


def decide_bank_changes(api_key, job, audit, used_bullets, drafts, bank_md):
    drafted = [b.get("text") for b in ((drafts or {}).get("bullets") or [])
               if (b.get("text") or "").strip()]
    if not used_bullets and not drafted:
        return {"changes": [], "didnt_qualify": []}
    user = (f"Role just applied to: {job.get('title')} at {job.get('company') or '?'}\n"
            f"Track: {audit.get('track')}\n\n"
            f"=== BULLETS THAT WENT ON THIS CV ===\n"
            + "\n".join(f"- {b}" for b in used_bullets)
            + "\n\n=== NEW BULLETS BUILT FROM TOM'S INTERVIEW ANSWERS ===\n"
            + ("\n".join(f"- {d}" for d in drafted) or "(none)")
            + f"\n\n=== THE BANK AS IT STANDS ===\n{bank_md or '(empty)'}")
    schema = {
        "type": "object",
        "properties": {
            "changes": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["PROMOTE", "ADD", "VARIANT"]},
                    "bank_id": {"type": "string"}, "title": {"type": "string"},
                    "text": {"type": "string"}, "tracks": {"type": "string"},
                    "competencies": {"type": "string"}, "why": {"type": "string"}},
                "required": ["kind", "bank_id", "title", "text", "tracks",
                             "competencies", "why"],
                "additionalProperties": False}},
            "didnt_qualify": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["changes", "didnt_qualify"], "additionalProperties": False}
    text = scan._claude_call(
        api_key, BANKWRITE_MODEL, BANKWRITE_SYSTEM, user, BANKWRITE_MAX_TOKENS,
        extra={"output_config": {"effort": AUDIT_EFFORT,
                                 "format": {"type": "json_schema", "schema": schema}}})
    return scan._extract_json(text)


def write_back(bank, state, job, audit, proposed, corpus):
    """Apply Step 9 to bullet-bank.md. Autonomous: no approval, one commit per role.

    Every proposed change goes through the same honesty screen the CV bullets did. A
    fabricated bullet on one CV is one bad application; the same bullet promoted into the
    bank is every application after it."""
    changes = list((proposed or {}).get("changes") or [])
    screened, blocked = [], []
    for ch in changes:
        text = " ".join(str(ch.get("text") or "").split())
        bad = cvbuild.invented_numbers(text, corpus)
        if bad:
            blocked.append((ch.get("bank_id"), f"numbers not in any source: "
                                               f"{', '.join(bad)}"))
        elif cvbuild.untraceable(text, corpus):
            blocked.append((ch.get("bank_id"), "wording not traceable to a source"))
        else:
            screened.append(dict(ch, text=text))

    # Repeated CUTs are a fact this system holds across roles, so the retirement rule is
    # arithmetic here rather than something a model is asked to remember.
    counts = state.setdefault("cut_counts", {})
    for bid in bankwrite.bump_cut_counts(counts, audit):
        screened.append({"kind": "RETIRE", "bank_id": bid,
                         "why": "cut on three roles"})

    md = bank.read(BANK_FILE, "")
    if not md.strip() or not screened:
        return [], blocked, (proposed or {}).get("didnt_qualify") or []
    md, applied, skipped = bankwrite.apply_changes(
        md, screened, today(), company=job.get("company") or "")
    if applied:
        bank.write(BANK_FILE, md)
    for bid, why in skipped:
        print(f"  bank change skipped [{bid}]: {why}")
    return applied, blocked, (proposed or {}).get("didnt_qualify") or []


# ---------------------------------------------------------------- artifacts

def answer_entries(job, answered, start_n=1):
    """Answer-bank entries, one per answered gap. Written verbatim: the value of the bank
    is that it holds what Tom actually said, so a future run can reuse it instead of asking
    again."""
    out = []
    for i, (gap, answer) in enumerate(answered, start=start_n):
        aid = f"A-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{i:02d}"
        out.append(
            f"### [{aid}] - {gap.get('keyword') or 'gap'}\n"
            f"Asked: {gap.get('question', '').strip()}\n"
            f"Answer: {answer.strip()}\n"
            f"Role: {job.get('company') or '?'} - {job.get('title')} ({job.get('id')})\n"
            f"Date: {today()}\n")
    return out


def next_answer_index(answers_md):
    """Continue today's numbering rather than colliding with it when two roles are
    processed on the same day."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    used = [int(m) for m in re.findall(rf"\[A-{stamp}-(\d+)\]", answers_md or "")]
    return (max(used) + 1) if used else 1


def packet_markdown(job, state, audit, drafts, salary):
    L = [f"# {job.get('title')} - {job.get('company') or '?'}", "",
         f"- Job id: `{job.get('id')}`",
         f"- Market: {job.get('market')}  |  Radar score: {job.get('score')}",
         f"- Posting: {job.get('url')}",
         f"- Queued: {state.get('started_at')}  |  Completed: {now_iso()}",
         f"- Track: **{audit.get('track')}** - {audit.get('track_rationale', '')}", ""]

    L += ["## Salary", ""]
    if not salary:
        L += ["Not researched. The deep score showed no comp risk on this posting.", ""]
    elif salary.get("skipped"):
        L += [f"Research offered and declined. Risk noted: {salary.get('reason', '')}", ""]
    else:
        L += ["```", format_salary(salary), "```", ""]

    L += ["## Bullet audit", "", "| Bank ID | Bullet | Decision | Rationale |",
          "|---|---|---|---|"]
    for b in (audit.get("bullets") or []):
        bid = b.get("bank_id") or ("_new_" if b.get("new_to_bank") else "")
        L.append(f"| {bid} | {b.get('text_start', '')} | **{b.get('decision', '')}** | "
                 f"{b.get('rationale', '')} |")
    L.append("")

    gaps = audit.get("metric_gaps") or []
    if gaps:
        L += ["## Metric gaps to close", ""]
        for g in gaps:
            L.append(f"- **{g.get('bank_id', '')}** {g.get('summary', '')}")
            for a in (g.get("angles") or []):
                L.append(f"  - {a}")
        L.append("")

    answered = state.get("answers") or []
    if answered:
        L += ["## Gap interview", ""]
        for a in answered:
            L += [f"**{a.get('keyword')}** - {a.get('question')}", "",
                  f"> {a.get('answer')}", ""]

    bullets = (drafts or {}).get("bullets") or []
    if bullets:
        L += ["## New bullets drafted from those answers", ""]
        for b in bullets:
            L += [f"- {b.get('text')}",
                  f"  - Tracks: {b.get('tracks')} | Competencies: {b.get('competencies')}",
                  f"  - Evidence: {b.get('evidence')}"]
            if b.get("notes"):
                L.append(f"  - Notes: {b['notes']}")
        L.append("")
    dropped = (drafts or {}).get("dropped") or []
    if dropped:
        L += ["## Gaps left open", "",
              "No bullet was written for these. Nothing here gets filled by inference.", ""]
        for d in dropped:
            L.append(f"- **{d.get('gap')}** - {d.get('why')}")
        L.append("")

    L += ["## Cost", "", "```", json.dumps(state.get("usage") or {}, indent=1), "```", "",
          "---", "",
          "Everything above is the input to the CV build. A `## CV` section is appended "
          "below once that has run; until it appears, nothing here is on a CV or in the "
          "bank.", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- the CV build

# Where the render lands in the workspace. The workflow uploads the page images from here
# as a run artifact, which is the only way to actually look at a CV that was built on a
# machine nobody is sitting at.
CV_OUT_DIR = os.environ.get("APPLYQ_CV_OUT", "cv-out")
CV_DIR = "cv"          # inside the bullet bank
# This repo is public and so is its Actions log. The CV's text comes out of the private
# bullet bank, so it is NOT printed by default -- the log gets the measurements and the
# page structure, which is what catches a layout break, and the pages themselves go to
# Tom's phone with the PDF. Set on a single run from the workflow's cv_debug input when
# something needs diagnosing and the text is worth the exposure.
CV_LOG_TEXT = os.environ.get("APPLYQ_CV_LOG_TEXT", "") == "1"


def cv_corpus(bank, base, cur):
    """Everything a bullet on this CV is allowed to be made of.

    The bank, the skeleton's own bullets, the bullets drafted from Tom's answers, and his
    answers themselves. Not the posting. The posting says what to look for; it never says
    what he did, and the moment it counts as evidence the whole honesty rule is gone."""
    skeleton = " ".join(cvbuild.base_bullets(base))
    drafted = " ".join(b.get("text", "")
                       for b in ((cur.get("drafts") or {}).get("bullets") or []))
    answers = " ".join(a.get("answer", "") for a in (cur.get("answers") or []))
    # On a revision, the page as it went out counts too. Everything on it already passed
    # this same screen on the way to the printer, so letting the revision keep a bullet it
    # was not asked to change is not a loophole -- refusing would be, because it would
    # quietly delete good bullets every time Tom asked for one small edit.
    as_built = cur.get("as_built") or ""
    return cvbuild.source_corpus(bank.read(BANK_FILE, ""), skeleton, drafted, answers,
                                 as_built)


def cv_name(job):
    return (f"{today()}-{slugify(job.get('company') or 'unknown')}-"
            f"{slugify(job.get('title'))}")


def cv_section_markdown(job, cur, chosen, summaries, tailored, rejected, facts, warnings,
                        bank_applied, blocked, didnt_qualify, pdf_rel, picked_by):
    L = ["", "---", "", "## CV", "",
         f"- Built: {now_iso()}",
         f"- Role title on the CV: **{cur.get('cv_title')}**",
         f"- PDF: `{pdf_rel}`",
         f"- Summary: **{chosen.get('label')}** ({float(chosen.get('score') or 0):g}/10, "
         f"{chosen.get('angle')}) - picked by {picked_by}", ""]

    L += ["### Summary variations", ""]
    for s in summaries:
        mark = " **<- used**" if s.get("label") == chosen.get("label") else ""
        L += [f"**{s.get('label')}. {s.get('angle')} "
              f"({float(s.get('score') or 0):g}/10)**{mark} - {s.get('why', '')}",
              f"> {s.get('text', '')}",
              f"_Changed from canonical: {s.get('changed', '')}_", ""]

    kws = tailored.get("keywords") or []
    if kws:
        L += ["### Keyword analysis", "", "| Keyword | Status |", "|---|---|"]
        L += [f"| {k.get('keyword', '')} | {k.get('status', '')} |" for k in kws]
        L.append("")

    changes = tailored.get("changes") or []
    if changes:
        L += ["### Change log", "",
              "| # | Section | Original | Revised | Keyword(s) | Evidence source |",
              "|---|---|---|---|---|---|"]
        for i, c in enumerate(changes, 1):
            L.append(f"| {i} | {c.get('section', '')} | {c.get('original', '')} | "
                     f"{c.get('revised', '')} | {c.get('keywords', '')} | "
                     f"{c.get('evidence', '')} |")
        L.append("")

    if rejected:
        L += ["### Bullets the honesty screen rejected", "",
              "These did not go on the CV. Each one carried a claim that could not be "
              "traced back to the bank, the base CV or Tom's own answers.", ""]
        L += [f"- {why}\n  > {text}" for text, why in rejected]
        L.append("")

    if cur.get("feedback"):
        L += ["### Revision", "",
              f"**Tom asked for:** {cur['feedback']}", ""]
        if cur.get("revision_notes"):
            L += [f"**What changed:** {cur['revision_notes']}", ""]
    if cur.get("cv_trimmed"):
        L += ["### Summary trimmed to four lines", "",
              "The chosen summary ran past the four-line rule, so its last sentence was "
              "dropped and the page re-rendered. Removed:", ""]
        L += [f"> {t}" for t in cur["cv_trimmed"]]
        L.append("")

    L += ["### Render check", "", "```",
          "\n".join(f"{k}: {v}" for k, v in (facts or {}).items()), "```", ""]
    if warnings:
        # Warnings ride along rather than blocking the send. An anti-ai.md word that came
        # out of the bank's own text is worth knowing about and is not worth withholding a
        # CV over.
        L += ["Went out with these noted:", ""]
        L += [f"- {w}" for w in warnings]
        L.append("")

    L += ["### Bullet bank write-back", ""]
    if bank_applied:
        L += [f"- **{kind}** `{bid}`" for bid, kind in bank_applied]
    else:
        L.append("Nothing qualified. A session with no bank changes is the normal outcome.")
    if blocked:
        L += ["", "Blocked by the honesty screen before they could reach the bank:", ""]
        L += [f"- `{bid}`: {why}" for bid, why in blocked]
    if didnt_qualify:
        L += ["", "Read better here but failed the promotion test:", ""]
        L += [f"- {x}" for x in didnt_qualify]
    L.append("")
    return "\n".join(L)


def try_build(state, job, bank, tg, api_key, chosen, picked_by):
    """build_and_ship, with the same bounded retry the audit gets.

    Rendering shells out to apt, npm, LibreOffice and poppler, so it can fail for reasons
    that have nothing to do with this role. A blip deserves another tick. A genuinely
    broken toolchain does not deserve a LibreOffice install every firing forever, with
    nobody watching, so the retries are counted and the role is dropped loudly."""
    try:
        return build_and_ship(state, job, bank, tg, api_key, chosen, picked_by)
    except Exception as e:
        cur = state.get("current") or {}
        n = cur.get("render_failures", 0) + 1
        cur["render_failures"] = n
        if n < MAX_STAGE_RETRIES:
            print(f"  CV build failed ({n}/{MAX_STAGE_RETRIES}): {e}")
            return False
        tg.send(f"Gave up building the CV for {job.get('title')}: it failed {n} times.\n"
                f"Last error: {str(e)[:250]}\n"
                f"The packet is still in the bank. /apply {cur.get('id')} to try again.")
        state.setdefault("history", []).append(
            {"id": cur.get("id"), "title": cur.get("title"),
             "company": cur.get("company"), "outcome": "render-failed", "at": now_iso(),
             "packet": cur.get("packet_file"), "error": str(e)[:200]})
        state["current"] = None
        return False


def build_and_ship(state, job, bank, tg, api_key, chosen, picked_by):
    """Assemble, render, check, and only then send. Returns True when the role is finished.

    The order matters and is the whole point of the phase: nothing reaches Tom's phone
    before the PDF has been rendered to images and measured. A CV with a broken tab stop
    looks perfect to the code that produced it."""
    cur = state["current"]
    tailored = cur.get("tailored") or {}
    audit = cur.get("audit") or {}

    base, from_bank = cvbuild.load_base(bank)
    if not from_bank:
        # First CV ever. Put the skeleton where Tom can edit it and tell him once.
        cvbuild.seed_base(bank)
    gaps = cvbuild.skeleton_gaps(base)
    if gaps and not state.get("cv_base_nudged"):
        # Said once, not every role. None of these stop a CV being built, they just make it
        # slightly worse than it needs to be, and a nag on every application is how a
        # message stops being read.
        state["cv_base_nudged"] = True
        tg.send("<b>Two things on the CV skeleton</b>\n\n"
                + "\n".join(f"\u2022 {esc(g)}" for g in gaps)
                + "\n\n<i>Send</i>  <code>/phone +34 700 000 000</code>  <i>and I'll put "
                  "it on. Everything else is in</i> "
                  f'<a href="{cvbuild.BASE_EDIT_URL}">cv-base.json</a>.')

    corpus = cv_corpus(bank, base, cur)
    by_entry, rejected = screened_entries(tailored, base, corpus)
    for text, why in rejected:
        print(f"  bullet rejected ({why}): {text[:70]!r}")

    title = cvbuild.role_title(
        job.get("title") if tailored.get("role_title_usable") else "",
        audit.get("track"))
    cur["cv_title"] = title
    stem = cv_name(job)
    cvbuild.ensure_toolchain()

    # Four printed lines, measured off the page rather than guessed at from a character
    # count: whether a sentence wraps is a question about Calibri's metrics. Over the
    # limit, the last sentence goes and it renders again. A summary is three or four
    # sentences and the last one is the least load-bearing by construction, so dropping
    # beats rewriting -- a rewrite would be new text arriving after the honesty screen has
    # already passed on it.
    summary = chosen.get("text") or ""
    trimmed = []
    for attempt in range(SUMMARY_TRIM_ATTEMPTS + 1):
        spec = cvbuild.assemble_spec(base, audit.get("track"), title, summary, by_entry,
                                     tailored.get("skills"))
        paths = cvbuild.render(spec, CV_OUT_DIR, stem)
        problems, warnings, facts = cvbuild.verify(paths, spec)
        lines = facts.get("summary_lines", 0)
        if lines <= cvbuild.SUMMARY_MAX_LINES or attempt == SUMMARY_TRIM_ATTEMPTS:
            break
        shorter = cvbuild.drop_last_sentence(summary)
        if not shorter:
            break
        trimmed.append(summary[len(shorter):].strip())
        print(f"  summary ran to {lines} lines; dropping its last sentence and "
              f"re-rendering")
        summary = shorter
    cur["cv_summary"] = summary
    cur["cv_trimmed"] = trimmed
    cvbuild.log_render(paths, problems, warnings, facts, spec=spec, verbose=CV_LOG_TEXT)

    # The PDF is committed either way. A CV that failed its checks is still the fastest way
    # to see what went wrong, and losing it would mean rebuilding to find out.
    pdf_rel = f"{CV_DIR}/{stem}.pdf"
    with open(paths["pdf"], "rb") as f:
        bank.write_bytes(pdf_rel, f.read())

    summaries = cur.get("summaries") or []
    bank_applied, blocked, didnt_qualify = [], [], []
    # A revision is a redraft of a page whose bullets already went through Step 9 on the
    # first build. Running it again would promote the same material twice.
    if not problems and not cur.get("revised"):
        # Step 9 runs only on a CV that actually shipped. Promoting a bullet off a build
        # that failed its checks would put it in the bank on the strength of a page nobody
        # ever saw.
        before = usage_snapshot()
        used = cvbuild.spec_bullets(spec)
        try:
            proposed = decide_bank_changes(api_key, job, audit, used,
                                           cur.get("drafts"), bank.read(BANK_FILE, ""))
            bank_applied, blocked, didnt_qualify = write_back(
                bank, state, job, audit, proposed, corpus)
        except Exception as e:
            print(f"  bank write-back failed: {e}")
            blocked = [("(all)", f"write-back call failed: {str(e)[:150]}")]
        record_usage(cur, "bankwrite", before)

    packet = cur.get("packet_file")
    if packet:
        bank.append(f"{PACKET_DIR}/{packet}",
                    cv_section_markdown(job, cur, chosen, summaries, tailored, rejected,
                                        facts, warnings, bank_applied, blocked,
                                        didnt_qualify, pdf_rel, picked_by))

    state.setdefault("history", []).append(
        {"id": cur["id"], "title": cur.get("title"), "company": cur.get("company"),
         "outcome": "done" if not problems else "cv-failed", "at": now_iso(),
         "packet": packet, "cv": pdf_rel, "usage": cur.get("usage") or {}})
    # Kept so /redo has a page to edit rather than a tailoring output Tom never saw. The
    # posting travels with it: a role can drop off the dashboard, and "the CV you sent me
    # yesterday" should still be revisable today.
    cur["spec"] = spec
    # The letter is named after the CV, not after the day it was written: /cover a week
    # later should still put the two files next to each other in the bank.
    cur["cv_stem"] = stem
    cur["job_snapshot"] = {k: job.get(k) for k in
                           ("id", "title", "company", "market", "score", "url",
                            "description")}
    state["last_cv"] = cur
    state["current"] = None
    state["last_tick"] = now_iso()
    bank.save_state(state)
    sha = bank.commit(f"CV: {job.get('title')} at {job.get('company') or '?'}")

    if problems:
        tg.send("\n".join(
            [f"⚠ <b>{esc(clip(job.get('title'), 70))}</b>",
             esc(f"{job.get('company') or '?'} · CV built but did not pass its checks, so "
                 f"I have not sent it."), ""]
            + [f"• {esc(clip(p, 150))}" for p in problems[:4]]
            + ["", f"<i>The PDF and the page images are in the run log and at "
                   f"<code>{esc(pdf_rel)}</code>.</i>"]))
        return True

    tally = [f"{len(cvbuild.spec_bullets(spec))} bullets"]
    if rejected:
        tally.append(f"{len(rejected)} cut")
    if bank_applied:
        tally.append(f"bank +{len(bank_applied)}")
    lines = [f"✓ <b>{esc(clip(title, 70))}</b>",
             esc(" · ".join([job.get("company") or "?", audit.get("track") or "",
                                  f"{facts.get('pages', '?')} pages"] + tally)
                 .strip(" ·"))]
    if cur.get("revised"):
        lines.append("<i>Rebuilt from your feedback.</i>")
    else:
        lines.append(f"<i>Summary {esc(chosen.get('label'))} · picked by "
                     f"{esc(picked_by)}</i>")
    if cur.get("revision_notes"):
        lines += ["", esc(clip(cur["revision_notes"], 300))]
    if trimmed:
        lines += ["", "<i>Summary was over four lines, so I dropped: "
                      f"{esc(clip(' '.join(trimmed), 160))}</i>"]
    caption = "\n".join(lines)
    if not tg.send_document(paths["pdf"], caption):
        tg.send(caption + f"\n\n<i>Telegram would not take the file. It is at "
                          f"<code>{esc(pdf_rel)}</code> in the bank.</i>")
    for w in warnings:
        print(f"  warn: {w}")
    if sha:
        what = "packet and CV" if cur.get("revised") else "packet, CV and bank changes"
        tg.send(f'<a href="https://{BANK_REPO}/commit/{sha}">{what}</a>')
    return True



# ---------------------------------------------------------------- the cover letter

def company_corpus(cur, job):
    """What a sentence about the COMPANY is allowed to be made of.

    Deliberately separate from cv_corpus(). The posting and the brief are the sources for
    what the company is doing and are not sources for anything about Tom -- that separation
    is the whole reason the letter can talk about their Series B without the posting quietly
    becoming evidence for his own numbers."""
    return cvbuild.source_corpus(brief_block(cur.get("brief")),
                                 json.dumps(cur.get("brief") or {}),
                                 job.get("description"), job.get("title"),
                                 job.get("company"))


def cover_stem(cur, job):
    return cur.get("cv_stem") or cv_name(job)


def cover_section_markdown(cur, letter, spec, dropped_paras, dropped_sentences, trimmed,
                           facts, warnings, pdf_rel):
    L = ["", "---", "", "## Cover letter", "",
         f"- Written: {now_iso()}",
         f"- PDF: `{pdf_rel}`",
         f"- Hook: {letter.get('hook', '')}"]
    if cur.get("cover_steer"):
        L.append(f"- Tom asked for: {cur['cover_steer']}")
    if letter.get("notes"):
        L.append(f"- Notes: {letter['notes']}")
    # The full text, because half the application forms Tom will meet want a letter pasted
    # into a box rather than uploaded, and a PDF is no use for that.
    L += ["", "### The letter", "", "```", coverletter.letter_text(spec).rstrip(), "```", ""]

    if dropped_paras:
        L += ["### Paragraphs the honesty screen rejected", "",
              "These did not print. Each made a claim about Tom that could not be traced "
              "back to the bank, the base CV, his own answers or the CV as it shipped.", ""]
        L += [f"- {why}\n  > {text}" for text, why in dropped_paras]
        L.append("")
    if dropped_sentences:
        L += ["### Sentences dropped for an unsourced number", ""]
        L += [f"- {why}\n  > {text}" for text, why in dropped_sentences]
        L.append("")
    if trimmed:
        L += ["### Trimmed to one page", "",
              "The letter ran past a page, so the last body paragraph was dropped and it "
              "was rendered again. Removed:", ""]
        L += [f"> {t}" for t in trimmed]
        L.append("")
    L += ["### Render check", "", "```",
          "\n".join(f"{k}: {v}" for k, v in (facts or {}).items()), "```", ""]
    if warnings:
        L += ["Went out with these noted:", ""] + [f"- {w}" for w in warnings] + [""]
    return "\n".join(L)


def try_cover(state, job, bank, tg, letter):
    """build_and_ship_cover with the same bounded retry the CV render gets. The render
    shells out to node, LibreOffice and poppler, none of which are this role's fault."""
    try:
        return build_and_ship_cover(state, job, bank, tg, letter)
    except Exception as e:
        cur = state.get("current") or {}
        n = cur.get("render_failures", 0) + 1
        cur["render_failures"] = n
        if n < MAX_STAGE_RETRIES:
            print(f"  cover render failed ({n}/{MAX_STAGE_RETRIES}): {e}")
            return False
        tg.send(f"Gave up rendering the cover letter for {job.get('title')}: it failed "
                f"{n} times.\nLast error: {str(e)[:250]}\n"
                f"The CV is untouched. /cover to try again.")
        finish_cover(state, cur, "cover-render-failed", None)
        return True


def finish_cover(state, cur, outcome, pdf_rel):
    """Put the role back where /redo and a later /cover can find it, and record the run.

    `cv` is deliberately NOT set on this history row: recoverable_cv() looks for the last
    row carrying one, and a cover letter must never make the CV behind it unrevisable."""
    state.setdefault("history", []).append(
        {"id": cur.get("id"), "title": cur.get("title"), "company": cur.get("company"),
         "outcome": outcome, "at": now_iso(), "packet": cur.get("packet_file"),
         "cover": pdf_rel, "usage": cur.get("usage") or {}})
    cur["stage"] = "done"
    for k in ("cover_letter", "cover_failures", "render_failures"):
        cur.pop(k, None)
    state["last_cv"] = cur
    state["current"] = None
    state["last_tick"] = now_iso()


def build_and_ship_cover(state, job, bank, tg, letter):
    """Screen it, render it, measure it, and only then send it. Returns True when done.

    The order is the point, exactly as it is for the CV: nothing reaches Tom's phone before
    the page has been rendered and looked at. A letter that runs to two pages looks
    perfectly fine to the code that produced it."""
    cur = state["current"]
    base, _from_bank = cvbuild.load_base(bank)

    own = cv_corpus(bank, base, cur)
    company = company_corpus(cur, job)
    kept, dropped_paras, dropped_sentences, fatal = coverletter.screen(
        letter.get("paragraphs"), own, company, job.get("company") or "")
    for text, why in dropped_paras:
        print(f"  paragraph rejected ({why[:60]}): {text[:70]!r}")
    for text, why in dropped_sentences:
        print(f"  sentence dropped ({why}): {text[:70]!r}")

    if fatal:
        # Not shipped, and not retried on its own either: the letter asserted something it
        # could not source in the one paragraph that carries the whole letter, and the fix
        # for that is a different angle, which is Tom's call and costs him one message.
        tg.send("\n".join(
            [f"⚠ <b>Cover letter not sent</b>  {esc(clip(job.get('company') or '?', 40))}",
             esc(fatal), "",
             "<i>Nothing was invented onto a page: it was caught before it printed. "
             "Send /cover again, with an angle, and I'll write it a different way.</i>"]))
        finish_cover(state, cur, "cover-screened-out", None)
        return True

    stem = cover_stem(cur, job)
    cvbuild.ensure_toolchain()

    # One page, measured off the rendered PDF rather than guessed at from a word count.
    # Over the line, the last body paragraph goes and it renders again -- the same trade the
    # summary's last sentence gets, and for the same reason: dropping is honest, rewriting
    # would be new text arriving after the honesty screen has already passed on it.
    trimmed = []
    for attempt in range(coverletter.PARA_TRIM_ATTEMPTS + 1):
        spec = coverletter.assemble_spec(base, job, kept,
                                         datetime.now(timezone.utc).date(),
                                         cur.get("cv_title"))
        paths = coverletter.render(spec, CV_OUT_DIR, stem)
        problems, warnings, facts = coverletter.verify(paths, spec)
        if facts.get("pages", 0) <= coverletter.MAX_PAGES \
                or attempt == coverletter.PARA_TRIM_ATTEMPTS:
            break
        kept, cut = coverletter.trim_one(kept)
        if not cut:
            break
        trimmed.append(cut)
        print(f"  letter ran to {facts.get('pages')} pages; dropping a body paragraph and "
              f"re-rendering")
    coverletter.log_render(paths, problems, warnings, facts, spec=spec, verbose=CV_LOG_TEXT)

    pdf_rel = f"{CV_DIR}/{stem}-cover.pdf"
    with open(paths["pdf"], "rb") as f:
        bank.write_bytes(pdf_rel, f.read())
    if cur.get("packet_file"):
        bank.append(f"{PACKET_DIR}/{cur['packet_file']}",
                    cover_section_markdown(cur, letter, spec, dropped_paras,
                                           dropped_sentences, trimmed, facts, warnings,
                                           pdf_rel))
    cur["cover_file"] = pdf_rel
    finish_cover(state, cur, "cover" if not problems else "cover-failed", pdf_rel)
    bank.save_state(state)
    sha = bank.commit(f"Cover letter: {job.get('title')} at {job.get('company') or '?'}")

    if problems:
        tg.send("\n".join(
            [f"⚠ <b>Cover letter</b>  {esc(clip(job.get('company') or '?', 40))}",
             esc("Built but did not pass its checks, so I have not sent it."), ""]
            + [f"• {esc(clip(p, 150))}" for p in problems[:4]]
            + ["", f"<i>It is at <code>{esc(pdf_rel)}</code> in the bank, with its text in "
                   f"the packet. /cover again and I'll write it shorter.</i>"]))
        return True

    lines = [f"✓ <b>Cover letter</b>  {esc(clip(job.get('company') or '?', 40))}",
             esc(f"{facts.get('words', '?')} words · one page"), ""]
    if letter.get("hook"):
        lines.append(f"<i>{esc(clip(letter['hook'], 220))}</i>")
    if letter.get("notes"):
        lines += ["", esc(clip(letter["notes"], 300))]
    if trimmed:
        lines += ["", "<i>It ran over a page, so I dropped a paragraph: "
                      f"{esc(clip(' '.join(trimmed), 160))}</i>"]
    if dropped_paras:
        lines += ["", f"<i>{len(dropped_paras)} paragraph"
                      f"{'' if len(dropped_paras) == 1 else 's'} cut by the honesty screen "
                      f"- the packet says which.</i>"]
    lines += ["", "<i>The text is in the packet too, for forms that want it pasted in.</i>"]
    caption = "\n".join(lines)
    if not tg.send_document(paths["pdf"], caption):
        tg.send(caption + f"\n\n<i>Telegram would not take the file. It is at "
                          f"<code>{esc(pdf_rel)}</code> in the bank.</i>")
    for w in warnings:
        print(f"  warn: {w}")
    if sha:
        tg.send(f'<a href="https://{BANK_REPO}/commit/{sha}">cover letter in the bank</a>')
    return True


# ---------------------------------------------------------------- usage accounting

def usage_snapshot():
    return dict(scan.USAGE)


def usage_delta(before):
    return {k: scan.USAGE.get(k, 0) - before.get(k, 0) for k in scan.USAGE}


def record_usage(state, phase, before):
    d = usage_delta(before)
    if not any(d.values()):
        return
    state.setdefault("usage", {})[phase] = d
    print(f"  usage[{phase}]: {d}")


# ---------------------------------------------------------------- the tick

def load_job(job_id):
    rows = scan.load_json("docs/jobs.json", [])
    for j in rows:
        ids = [str(j.get("id") or "")] + [str(x) for x in (j.get("dupe_ids") or [])]
        if str(job_id) in ids:
            return j
    return None


def set_phone(bank, number):
    """Put a phone number on the CV, or take it off. Returns the new contact line.

    Exists because the alternative was Tom hand-editing JSON in a private repo, which is
    not a thing to ask of someone who has said plainly he is not a developer. The number
    stays out of this public repo and goes in the bank's copy of the skeleton, which is
    exactly where it belongs -- he just never has to see that."""
    base, _from_bank = cvbuild.load_base(bank)
    contact = [c for c in (base.get("contact") or [])
               if not cvbuild.PHONE_RE.match((c.get("text") or "").strip())]
    if number:
        # Second, right after the location. That is where it sits on his base CV.
        contact.insert(1 if contact else 0, {"text": number})
    base["contact"] = contact
    bank.write(cvbuild.BASE_FILE,
               json.dumps(base, indent=2, ensure_ascii=False) + "\n")
    bank.commit(f"cv-base: {'set' if number else 'remove'} phone number")
    return contact


def handle_commands(texts, state, queue, tg, bank=None):
    """Telegram commands, applied in order. Returns (queue, remaining_texts) where the
    remaining texts are the non-command messages -- those are answers to a pending
    question, and only the state machine knows what to do with them."""
    rest = []
    for _uid, text in texts:
        cmd = text.split()[0].lower() if text.split() else ""
        cmd = cmd.split("@")[0]
        if cmd == "/apply":
            arg = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            if not arg:
                tg.send("Give me an id: /apply <id>")
            elif arg in queue:
                tg.send(f"{arg} is already queued.")
            elif not load_job(arg):
                tg.send(f"No scored role with id {arg} on the dashboard.")
            else:
                queue = queue + [arg]
                j = load_job(arg)
                tg.send(f"<b>Queued</b>  {esc(clip(j.get('title'), 60))}\n"
                        + esc(f"{j.get('company') or '?'} · "
                              f"{len(queue)} in the queue"))
        elif cmd == "/queue":
            if not queue:
                tg.send("Queue is empty.")
            else:
                lines = []
                for q in queue[:15]:
                    j = load_job(q)
                    lines.append(
                        f"\u2022 {esc(clip(j.get('title'), 60))} "
                        f"<i>{esc(j.get('company') or '?')}</i>"
                        if j else f"\u2022 <code>{esc(q)}</code> not on the dashboard")
                tg.send(f"<b>{len(queue)} queued</b>\n\n" + "\n".join(lines))
        elif cmd == "/status":
            cur = state.get("current")
            if not cur:
                tg.send(f"Idle. {len(queue)} queued.")
            else:
                tg.send(f"<b>{esc(clip(cur.get('title'), 70))}</b>\n"
                        + esc(" · ".join(x for x in [
                            cur.get("company") or "?", cur.get("stage"),
                            f"since {cur.get('started_at', '')[:10]}"] if x))
                        + f"\n\n<i>{len(queue)} more queued.</i>")
        elif cmd == "/cancel":
            cur = state.get("current")
            if not cur:
                tg.send("Nothing in flight.")
            else:
                tg.send(f"Dropped {cur.get('title')}. It stays un-applied; "
                        f"/apply {cur.get('id')} to start it again.")
                state.setdefault("history", []).append(
                    {**{k: cur.get(k) for k in ("id", "title", "company")},
                     "outcome": "cancelled", "at": now_iso()})
                state["current"] = None
        elif cmd == "/redo":
            arg = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            last = state.get("last_cv")
            # A CV built before this command existed has no `last_cv`, but its PDF and its
            # packet are still in the bank. Recover from those rather than telling him
            # there is nothing to revise while the file sits in the same repo.
            recoverable = last or (bank is not None and recoverable_cv(state))
            if state.get("current"):
                tg.send(f"Working on {esc(clip(state['current'].get('title'), 50))} right "
                        f"now. /redo once that one's finished.")
            elif not recoverable:
                tg.send("No CV to revise yet. /apply a role and I'll build one.")
            elif not arg:
                tg.send("<b>Tell me what to change.</b>\n\n"
                        "<i>/redo cut the LexisNexis training bullet, it's the weakest</i>\n"
                        "<i>/redo lead the summary with forecasting, not the MBA</i>\n"
                        "<i>/redo the NAVEX section is too long</i>")
            else:
                base_role = last or recover_last_cv(bank, state)
                if not base_role:
                    tg.send("There's a CV in the bank but I can't read it back. "
                            "/apply the role again and I'll rebuild it from scratch.")
                    continue
                revision = json.loads(json.dumps(base_role))
                revision.update({"stage": "revise", "feedback": arg, "revised": True,
                                 "started_at": now_iso()})
                for k in ("asked_at", "render_failures", "cv_failures", "nudged"):
                    revision.pop(k, None)
                state["current"] = revision
                name = revision.get("cv_title") or revision.get("title")
                tg.send(f"<b>Rebuilding</b>  {esc(clip(name, 60))}\n\n"
                        f"<i>{esc(clip(arg, 160))}</i>")
        elif cmd == "/cover":
            # Opt-in, and only ever on the CV that already went out. Everything the letter
            # needs -- the company brief, the audit, Tom's answers and the page as built --
            # was collected on the CV run and is still on the role, so this costs one Opus
            # call and no round trip. /cover again to rewrite it; there is nothing to undo.
            arg = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            last = state.get("last_cv")
            recoverable = last or (bank is not None and recoverable_cv(state))
            if state.get("current"):
                tg.send(f"Working on {esc(clip(state['current'].get('title'), 50))} right "
                        f"now. /cover once that one's finished.")
            elif not recoverable:
                tg.send("No CV to write a letter for yet. /apply a role and I'll build "
                        "one, then /cover it.")
            else:
                base_role = last or recover_last_cv(bank, state)
                if not base_role:
                    tg.send("There's a CV in the bank but I can't read it back. "
                            "/apply the role again and I'll rebuild it from scratch.")
                    continue
                run = json.loads(json.dumps(base_role))
                run.update({"stage": "cover", "cover_steer": arg,
                            "started_at": now_iso()})
                for k in ("asked_at", "render_failures", "cv_failures", "cover_failures",
                          "nudged"):
                    run.pop(k, None)
                state["current"] = run
                name = run.get("company") or run.get("cv_title") or run.get("title")
                msg = [f"<b>Writing the cover letter</b>  {esc(clip(name, 60))}"]
                if arg:
                    msg += ["", f"<i>{esc(clip(arg, 160))}</i>"]
                if not (run.get("brief") or {}).get("priorities"):
                    # Said plainly rather than discovered later: the hook is supposed to
                    # come off the company brief, and a recovered role does not carry one.
                    msg += ["", "<i>I don't have the company research for this one, so the "
                                "hook comes off the posting itself. It'll be a thinner "
                                "letter.</i>"]
                tg.send("\n".join(msg))
        elif cmd == "/phone":
            arg = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            if bank is None:
                tg.send("Can't reach the bullet bank right now. Try again in a bit.")
            elif not arg:
                base, _ = cvbuild.load_base(bank)
                now = next((c.get("text") for c in (base.get("contact") or [])
                            if cvbuild.PHONE_RE.match((c.get("text") or "").strip())), None)
                tg.send(f"Phone on the CV: <b>{esc(now)}</b>\n\n"
                        f"<i>/phone &lt;number&gt; to change it, /phone off to remove it.</i>"
                        if now else
                        "No phone number on the CV yet.\n\n"
                        "<i>Send /phone +34 700 000 000 and I'll put it on.</i>")
            elif arg.lower() in ("off", "none", "remove", "clear"):
                set_phone(bank, "")
                tg.send("Phone number taken off the CV.")
            elif not cvbuild.PHONE_RE.match(arg):
                tg.send(f"<code>{esc(arg)}</code> doesn't look like a phone number. "
                        f"Digits, spaces and a leading + only.")
            else:
                contact = set_phone(bank, arg)
                tg.send(f"<b>Phone set.</b>  {esc(arg)}\n\n"
                        + esc(" | ".join(c.get("text", "") for c in contact))
                        + "\n\n<i>That's the contact line on every CV from now on.</i>")
        elif cmd in ("/help", "/start"):
            tg.send(HELP)
        elif cmd.startswith("/"):
            tg.send(f"Don't know {cmd}. /help for what I do know.")
        else:
            rest.append(text)
    return queue, rest


def start_next(state, queue, tg):
    """Pop the head of the queue into `current`. Removed from the queue at pick-up, not at
    completion: `current` is persisted, so a crashed tick resumes rather than losing the
    role, and a role that can't be found doesn't wedge the queue behind it."""
    while queue:
        job_id, queue = queue[0], queue[1:]
        # Only a role that got all the way to a CV is finished. A role that stopped at a
        # Phase 1 packet is re-runnable on purpose: the answer bank means the interview is
        # not repeated, so a rerun is one audit call away from the CV it never got.
        done = next((h for h in state.get("history", [])
                     if h.get("id") == job_id and h.get("outcome") == "done"
                     and h.get("cv")), None)
        if done:
            tg.send(f"{done.get('title') or job_id} already has a CV "
                    f"({done.get('cv')}). Skipped.")
            continue
        job = load_job(job_id)
        if not job:
            tg.send(f"Queued id {job_id} isn't on the dashboard any more. Skipped.")
            continue
        state["current"] = {
            "id": job_id, "title": job.get("title"), "company": job.get("company"),
            "stage": "audit", "started_at": now_iso(),
            "answers": [], "usage": {},
        }
        return state, queue, job
    return state, queue, None


# Stages that existed before the conversation was collapsed to one round trip. A role
# parked on one of them when the code changed under it has no matching branch in advance()
# and would sit there forever, so it restarts from the audit. Restarting costs one Opus
# call and asks the questions again, which is the honest outcome: the half-finished
# interview it was holding no longer maps onto the questions the new flow asks.
RETIRED_STAGES = {"salary_gate": "audit", "gap_interview": "audit"}


def migrate_stage(cur, tg):
    """Returns True if the role was moved off a retired stage."""
    old = (cur or {}).get("stage")
    if old not in RETIRED_STAGES:
        return False
    cur["stage"] = RETIRED_STAGES[old]
    for k in ("pending", "gaps", "gap_index", "questions", "questions_all",
              "asked_at", "nudged", "answers", "answered_so_far"):
        cur.pop(k, None)
    cur["answers"] = []
    tg.send(f"I changed how questions are asked - everything for a role now comes in one "
            f"message instead of one at a time, because GitHub's scheduler was making a "
            f"five-message conversation take a day.\n\n"
            f"Restarting {cur.get('title')} from the top. You'll get one message with all "
            f"of it shortly. Sorry about the earlier answer - it was for the old flow and "
            f"I can't carry it across honestly.")
    return True


def advance(state, job, bank, tg, api_key, answers_text):
    """Push `current` as far as it can go this tick. Returns True when the role finished.

    Five stages, and only two of them ever wait on Tom -- the gap questions, and the
    summary pick on roles above VARIATION_REVIEW_MIN_SCORE. That is the point: every wait
    costs a cron firing, and cron is delivering a few of those a day rather than the four
    an hour it was asked for. Everything else, the company brief and the tailoring pass
    included, is arranged to run on the near side of a wait."""
    cur = state["current"]
    if migrate_stage(cur, tg):
        answers_text = ""
    profile = scan.load_profile()

    # ---- audit: needs nothing from Tom, so it runs before anything is asked
    if cur["stage"] == "audit":
        before = usage_snapshot()
        bank_md = bank.read(BANK_FILE, "")
        answers_md = bank.read(ANSWER_FILE, "")
        if not bank_md.strip():
            tg.send("The bullet bank is empty or unreadable. Auditing against profile.md "
                    "alone, which is weaker - the bank holds better versions of these "
                    "bullets than the profile does.")
        try:
            audit = run_audit(api_key, job, profile, bank_md, answers_md)
        except Exception as e:
            # Retrying next tick is right for a blip. Retrying forever is a role that
            # silently burns an Opus call every firing, so the retries are counted and the
            # role is dropped loudly once they run out.
            n = cur.get("audit_failures", 0) + 1
            cur["audit_failures"] = n
            record_usage(cur, "audit", before)
            if n < MAX_STAGE_RETRIES:
                print(f"  audit failed ({n}/{MAX_STAGE_RETRIES}): {e}")
                return False
            tg.send(f"Gave up on {job.get('title')} @ {job.get('company') or '?'}: the "
                    f"bullet audit failed {n} times.\nLast error: {str(e)[:200]}\n"
                    f"/apply {cur['id']} to try again.")
            state.setdefault("history", []).append(
                {"id": cur["id"], "title": cur.get("title"), "company": cur.get("company"),
                 "outcome": "audit-failed", "at": now_iso(), "error": str(e)[:200]})
            state["current"] = None
            return False
        cur["audit"] = audit
        cur["audit_failures"] = 0
        record_usage(cur, "audit", before)
        cur["questions"] = build_questions(audit, job)
        # The nudge path narrows cur["questions"] to what is still open, so the full list is
        # kept separately -- the packet needs the original gap for every answer it drafts from.
        cur["questions_all"] = list(cur["questions"])
        cur["stage"] = "ask"
        answers_text = ""

    # ---- ask: the one and only round trip
    if cur["stage"] == "ask":
        qs = cur.get("questions") or []
        if not qs:
            # Nothing to ask. Say what was found and go straight to the packet.
            audit = cur.get("audit") or {}
            reused = [g for g in (audit.get("gaps") or [])
                      if (g.get("answered_by") or "").strip()]
            tg.send(job_line(job, audit) + "\n\n"
                    + ("<i>Nothing to ask"
                       + (f" - {len(reused)} already in the answer bank" if reused else "")
                       + ". Finishing it off.</i>"))
            cur["answers"] = []
            cur["stage"] = "packet"
        elif not cur.get("asked_at"):
            tg.send(format_questions(qs, job, cur.get("audit") or {}))
            cur["asked_at"] = now_iso()
            return False
        else:
            reply = (answers_text or "").strip()
            if not reply:
                return False
            # A reply that is only letters is resolved here, exactly, for nothing. The
            # model is for prose.
            split = parse_answer_key(reply, qs)
            if split is not None:
                print(f"  reply read as an answer key: {reply!r}")
            else:
                before = usage_snapshot()
                try:
                    split = split_is_sane(split_reply(api_key, qs, reply), reply, qs)
                except Exception as e:
                    # One question is the common case, and a single reply to a single
                    # question needs no splitting at all. Falling back keeps a splitter
                    # outage from blocking the role.
                    print(f"  split failed ({e}); falling back")
                    split = [reply] + [""] * (len(qs) - 1)
                record_usage(cur, "split", before)

            answers, unanswered = [], []
            for q, a in zip(qs, split):
                resolved = resolve_answer(a, q)
                answers.append({"kind": q["kind"], "keyword": q.get("keyword"),
                                "question": q.get("question"), "answer": resolved,
                                "has_material": has_material(resolved), "at": now_iso()})
                if not resolved.strip():
                    unanswered.append(q)
            cur["answers"] = answers

            # One nudge for anything he genuinely did not touch, then proceed regardless.
            # An unanswered question simply produces no bullet; it is never worth a second
            # cron firing to chase the same ground twice.
            if unanswered and not cur.get("nudged"):
                cur["nudged"] = True
                names = "\n".join(f"<b>{i}</b>  {esc(clip(q.get('question'), 160))}"
                                  for i, q in enumerate(unanswered, 1))
                tg.send(f"<b>Still open</b>\n\n{names}\n\n"
                        f"<i>Answer, or say skip and I'll leave "
                        f"{'it' if len(unanswered) == 1 else 'them'} out.</i>")
                cur["asked_at"] = now_iso()
                cur["questions"] = unanswered
                cur["answered_so_far"] = (cur.get("answered_so_far") or []) + [
                    a for a in answers if a["answer"].strip()]
                return False
            cur["answers"] = (cur.get("answered_so_far") or []) + [
                a for a in answers if a["answer"].strip()]
            cur["stage"] = "packet"
            answers_text = ""

    # ---- packet
    if cur["stage"] == "packet":
        audit = cur.get("audit") or {}
        answers = cur.get("answers") or []

        # Salary research, if he asked for it. Runs here rather than in its own stage so it
        # costs no extra round trip.
        sal = next((a for a in answers if a["kind"] == "salary"), None)
        if sal is None:
            cur["salary"] = None
        elif is_yes(sal["answer"]):
            before = usage_snapshot()
            try:
                cur["salary"] = research_salary(api_key, job)
                tg.send(format_salary(cur["salary"]))
            except Exception as e:
                cur["salary"] = {"skipped": True,
                                 "reason": f"research failed: {str(e)[:150]}"}
                tg.send(f"Salary research failed: {str(e)[:200]}\nCarrying on without it.")
            record_usage(cur, "salary", before)
        else:
            cur["salary"] = {"skipped": True, "reason": sal["answer"] or "declined"}

        gap_answers = [a for a in answers if a["kind"] == "gap"]
        before = usage_snapshot()
        gaps_by_kw = {g.get("keyword"): g for g in (cur.get("questions_all") or [])}
        answered = [(gaps_by_kw.get(a["keyword"], {"keyword": a["keyword"],
                                                   "question": a["question"]}), a["answer"])
                    for a in gap_answers if a.get("has_material")]
        try:
            drafts = draft_bullets(api_key, job, audit.get("track"), answered)
        except Exception as e:
            drafts = {"bullets": [], "dropped": [
                {"gap": "all", "why": f"drafting call failed: {str(e)[:150]}"}]}
            tg.send(f"Bullet drafting failed: {str(e)[:200]}\n"
                    f"The packet still has the audit and your answers.")
        record_usage(cur, "draft", before)

        # Answers go in the bank whether or not they produced a bullet -- a "no meaningful
        # experience" is exactly as worth remembering as a yes, because it stops the same
        # question being asked on the next role.
        answers_md = bank.read(ANSWER_FILE, "")
        entries = answer_entries(
            job, [({"keyword": a["keyword"], "question": a["question"]}, a["answer"])
                  for a in gap_answers],
            start_n=next_answer_index(answers_md))
        if entries:
            if not answers_md.strip():
                bank.write(ANSWER_FILE,
                           "# Answer bank\n\nVerbatim answers from gap interviews. Checked "
                           "before any new question is asked, so the same ground is never "
                           "covered twice.\n\n")
            bank.append(ANSWER_FILE, "\n" + "\n".join(entries))

        name = cv_name(job) + ".md"
        bank.write(f"{PACKET_DIR}/{name}",
                   packet_markdown(job, cur, audit, drafts, cur.get("salary")))
        cur["packet_file"] = name
        # Kept for the CV stage: the honesty screen has to know which bullets came out of
        # Tom's own answers, and a rerun of the drafting call to find out would be a second
        # Opus call for something already on disk.
        cur["drafts"] = drafts

        # The packet lands in its own commit rather than waiting for the CV. It is finished
        # work and it is the thing a failed CV build falls back to, so it should be in the
        # bank before anything that can fail runs.
        state["last_tick"] = now_iso()
        bank.save_state(state)
        bank.commit(f"Apply packet: {job.get('title')} at {job.get('company') or '?'}")

        n_new = len((drafts or {}).get("bullets") or [])
        n_drop = len((drafts or {}).get("dropped") or [])
        tally = [f"{n_new} new bullet" + ("" if n_new == 1 else "s")]
        if n_drop:
            tally.append(f"{n_drop} gap" + ("" if n_drop == 1 else "s") + " left open")
        tg.send("\n".join(
            [f"<b>{esc(clip(job.get('title'), 70))}</b>",
             esc(" · ".join([job.get("company") or "?", audit.get("track") or ""]
                            + tally).strip(" ·")),
             "", "<i>Packet done. Building the CV.</i>"]))
        cur["stage"] = "cv"
        answers_text = ""

    # ---- cv: the company brief, then the tailoring pass. Neither needs Tom, so both run
    # before the one question this phase asks.
    if cur["stage"] == "cv":
        before = usage_snapshot()
        audit = cur.get("audit") or {}
        if "brief" not in cur:
            # A brief that cannot be researched is a thin brief, not a stopped role. The
            # summary's company-forward variation then leads with the posting's own
            # mission, and says so.
            try:
                cur["brief"] = research_brief(api_key, job)
            except Exception as e:
                print(f"  company brief failed: {e}")
                cur["brief"] = {"thin": True, "error": str(e)[:200]}
            record_usage(cur, "brief", before)

        before = usage_snapshot()
        try:
            cur["tailored"] = tailor_cv(api_key, job, audit, cur.get("brief"),
                                        cvbuild.load_base(bank)[0],
                                        bank.read(BANK_FILE, ""), cur.get("drafts"))
        except Exception as e:
            n = cur.get("cv_failures", 0) + 1
            cur["cv_failures"] = n
            record_usage(cur, "tailor", before)
            if n < MAX_STAGE_RETRIES:
                print(f"  tailoring failed ({n}/{MAX_STAGE_RETRIES}): {e}")
                return False
            tg.send(f"Gave up on the CV for {job.get('title')}: the tailoring call failed "
                    f"{n} times.\nLast error: {str(e)[:200]}\n"
                    f"The packet is still in the bank. /apply {cur['id']} to try again.")
            state.setdefault("history", []).append(
                {"id": cur["id"], "title": cur.get("title"),
                 "company": cur.get("company"), "outcome": "tailor-failed",
                 "at": now_iso(), "packet": cur.get("packet_file"), "error": str(e)[:200]})
            state["current"] = None
            return False
        cur["cv_failures"] = 0
        record_usage(cur, "tailor", before)

        summaries = summaries_of(cur["tailored"])
        cur["summaries"] = summaries
        if not summaries:
            tg.send("The tailoring pass returned no usable summary, so the CV would go out "
                    "with none. Stopping here rather than shipping that.")
            state.setdefault("history", []).append(
                {"id": cur["id"], "title": cur.get("title"),
                 "company": cur.get("company"), "outcome": "no-summary", "at": now_iso(),
                 "packet": cur.get("packet_file")})
            state["current"] = None
            return False

        # The gate. Above the line Tom picks; below it the strongest is taken and he is
        # shown what was chosen. Either way it is at most one more round trip.
        score = job.get("score")
        gated = (score is not None and float(score) >= VARIATION_REVIEW_MIN_SCORE
                 and len(summaries) > 1)
        if gated:
            tg.send(format_variations(summaries, job, audit))
            cur["asked_at"] = now_iso()
            cur["stage"] = "pick"
            return False
        chosen = best_summary(summaries)
        tg.send(format_auto_pick(chosen, summaries, job, audit))
        return try_build(state, job, bank, tg, api_key, chosen, "agent")

    # ---- pick: the second and last round trip, and only on the roles worth one
    if cur["stage"] == "pick":
        summaries = cur.get("summaries") or []
        reply = (answers_text or "").strip()
        if not reply:
            return False
        chosen, recognised = resolve_pick(reply, summaries)
        if not recognised:
            tg.send(f"Didn't read that as a letter, so I've taken "
                    f"<b>{esc(chosen.get('label'))}</b>, the highest scored.")
        return try_build(state, job, bank, tg, api_key, chosen,
                         "Tom" if recognised else "agent")

    # ---- revise: Tom read the CV and asked for a change. No round trip -- he already
    # spent it by sending the feedback.
    if cur["stage"] == "revise":
        before = usage_snapshot()
        try:
            as_built = (as_built_block(cur["spec"]) if cur.get("spec")
                        else (cur.get("as_built") or ""))
            cur["as_built"] = as_built
            rev = revise_cv(api_key, job, cur.get("audit") or {}, as_built,
                            cur.get("feedback"), cvbuild.load_base(bank)[0],
                            bank.read(BANK_FILE, ""), cur.get("drafts"))
        except Exception as e:
            n = cur.get("cv_failures", 0) + 1
            cur["cv_failures"] = n
            record_usage(cur, "revise", before)
            if n < MAX_STAGE_RETRIES:
                print(f"  revision failed ({n}/{MAX_STAGE_RETRIES}): {e}")
                return False
            tg.send(f"Couldn't rebuild {job.get('title')}: the revision failed {n} times.\n"
                    f"Last error: {str(e)[:200]}\n"
                    f"The CV you already have is untouched.")
            state["current"] = None
            return False
        record_usage(cur, "revise", before)

        tailored = dict(cur.get("tailored") or {})
        tailored["entries"] = rev.get("entries") or tailored.get("entries")
        tailored["skills"] = rev.get("skills") or tailored.get("skills")
        tailored["changes"] = (tailored.get("changes") or []) + (rev.get("changes") or [])
        cur["tailored"] = tailored
        cur["revision_notes"] = rev.get("notes") or ""
        chosen = {"label": "revised", "score": 0, "angle": "your feedback",
                  "text": rev.get("summary") or cur.get("cv_summary") or ""}
        return try_build(state, job, bank, tg, api_key, chosen, "your feedback")

    # ---- cover: the letter, on request only. No round trip either -- /cover carried the
    # request, and the CV run already collected everything a letter needs.
    if cur["stage"] == "cover":
        before = usage_snapshot()
        try:
            # The page Tom actually read, not the tailoring output he never saw. It is both
            # what the letter re-angles and, through cv_corpus(), part of what it is allowed
            # to claim.
            as_built = (as_built_block(cur["spec"]) if cur.get("spec")
                        else (cur.get("as_built") or ""))
            cur["as_built"] = as_built
            letter = write_cover(api_key, job, cur.get("audit") or {}, cur.get("brief"),
                                 as_built, cur.get("answers"), cur.get("cover_steer"))
        except Exception as e:
            n = cur.get("cover_failures", 0) + 1
            cur["cover_failures"] = n
            record_usage(cur, "cover", before)
            if n < MAX_STAGE_RETRIES:
                print(f"  cover letter failed ({n}/{MAX_STAGE_RETRIES}): {e}")
                return False
            tg.send(f"Couldn't write the cover letter for {job.get('title')}: it failed "
                    f"{n} times.\nLast error: {str(e)[:200]}\n"
                    f"The CV you already have is untouched.")
            finish_cover(state, cur, "cover-write-failed", None)
            return True
        record_usage(cur, "cover", before)
        return try_cover(state, job, bank, tg, letter)
    return False


REQUIRED_SECRETS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "BULLET_BANK_PAT"]


def configuration_state():
    """(state, missing). Not-yet-configured and configured-but-broken are different
    situations and deserve different noise. The cron fires every 15 minutes from the moment
    this lands on main, which may be days before the secrets exist; failing red that whole
    time would teach the Actions tab to be ignored, and a red cross that means nothing is
    worse than no cross at all. A partial setup is a genuine mistake and still fails."""
    missing = [s for s in REQUIRED_SECRETS if not os.environ.get(s)]
    if len(missing) == len(REQUIRED_SECRETS):
        return "unconfigured", missing
    return ("partial" if missing else "ready"), missing


def tick(dry=False):
    state_of_config, missing = configuration_state()
    if state_of_config == "unconfigured" and not dry:
        print("Apply queue is not set up yet: none of "
              f"{', '.join(REQUIRED_SECRETS)} are set.\n"
              "Nothing to do. Add them under Settings -> Secrets and variables -> Actions "
              "and this starts working on the next tick.")
        return 0
    if state_of_config == "partial" and not dry:
        # Half-configured is a real error. Say which half.
        raise RuntimeError(
            f"Apply queue is half set up: {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} missing. Add "
            f"{'it' if len(missing) == 1 else 'them'} under Settings -> Secrets and "
            f"variables -> Actions.")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    tg = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                  os.environ.get("TELEGRAM_CHAT_ID", ""), dry=dry)
    if not tg.enabled:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; questions have nowhere to go.")

    bank = Bank(os.environ.get("BULLET_BANK_PAT", ""), dry=dry)
    bank.clone()
    state = bank.load_state()
    state.setdefault("history", [])
    # Deep copy via round-trip: the tick-end comparison has to see nested edits (a stage
    # change inside `current`), which a shallow copy would share and hide.
    original_state = json.loads(json.dumps(state))

    server = firebase_state()
    if server is None:
        # No queue means nothing to start, but an interview already in flight is answered
        # over Telegram and does not need Firebase at all -- so carry on rather than
        # abandoning a role mid-question.
        print("  firebase unreachable; working from persisted state only")
        server = {}
    queue = as_id_list(server.get("queued"))
    applied = set(as_id_list(server.get("applied")))
    hidden = set(as_id_list(server.get("hidden")))
    # A role hidden or marked applied by hand after it was queued is no longer wanted.
    queue = [q for q in queue if q not in applied and q not in hidden]

    started_queue = list(queue)
    inbound = os.environ.get(INBOUND_ENV, "").strip()
    if inbound:
        # Push mode: the relay already has the message, so there is nothing to poll for and
        # nothing to acknowledge.
        #
        # The sender still has to be checked here. The relay authenticates Telegram, not
        # Tom: a bot is findable by its username, so anyone can message it, and Telegram
        # relays every one of those with the same valid secret. Poll mode has always
        # filtered on chat id in message_texts(); push mode needs the same gate, and this
        # is the only place that holds the real chat id. Without it a stranger's message
        # becomes Tom's answer, and lands in a resume bullet with his name on it.
        push_mode = True
        want_chat = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
        from_chat = str(os.environ.get(INBOUND_CHAT_ENV, "")).strip()
        if not want_chat or from_chat != want_chat:
            print(f"  inbound from chat {from_chat or '(none)'} is not Tom's; ignoring")
            texts = []
        else:
            texts = [(0, inbound)]
            print(f"  inbound (push): {inbound[:80]!r}")
    elif tg.enabled and not dry and tg.webhook_active():
        # A webhook is registered, so this is a scheduled tick in push mode. There is
        # nothing to read: Telegram is delivering to the relay, and getUpdates would 409.
        # The tick still runs, because it is what starts a role queued from the dashboard
        # and what carries one forward if the relay ever misses a message.
        push_mode = True
        texts = []
        print("  webhook active; not polling")
    else:
        push_mode = False
        updates, offset = tg.updates(state.get("telegram_offset", 0))
        texts = message_texts(updates, os.environ.get("TELEGRAM_CHAT_ID", ""))
        # Written only once the updates have actually been handled. Bumping the offset
        # before that would acknowledge an answer a crash then threw away, and Tom would be
        # asked the same thing again with no way to tell why.
        state["telegram_offset"] = offset
    queue, answers = handle_commands(texts, state, queue, tg, bank)
    # Only the newest free-text message answers the pending question. If Tom sent three
    # lines while thinking out loud, the last one is his answer.
    answer_text = answers[-1] if answers else ""

    # One role per tick, and one advance() per role. advance() already pushes the role as
    # far as it can go before it needs Tom, and a finished role deliberately leaves the
    # next one for the next tick -- that is what keeps two interviews out of the chat at
    # the same time.
    job = None
    if not state.get("current"):
        state, queue, job = start_next(state, queue, tg)
        if state.get("current"):
            print(f"  starting {state['current']['title']}")
    else:
        job = load_job(state["current"]["id"])
        if not job:
            # A revision carries its own copy of the posting. "The CV you sent me
            # yesterday" should still be revisable today, and a role ages off the
            # dashboard in a week.
            job = state["current"].get("job_snapshot")
        if not job:
            tg.send(f"{state['current'].get('title')} is no longer on the dashboard. "
                    f"Dropping it mid-run.")
            state.setdefault("history", []).append(
                {"id": state["current"]["id"], "outcome": "vanished", "at": now_iso()})
            state["current"] = None

    if state.get("current") and job:
        if not api_key and not dry:
            tg.send("ANTHROPIC_API_KEY is not set on the workflow. Nothing can run.")
        else:
            finished = advance(state, job, bank, tg, api_key, answer_text)
            # If that tick ended by asking Tom something, stay alive and wait for the
            # answer instead of exiting and leaving it to the next cron firing. Cron is
            # delivering a handful of firings a day, so a reply given in the next few
            # minutes would otherwise sit unread for hours. Waiting here costs nothing but
            # runner time, and the whole application finishes in this one run.
            waited = 0
            # Pointless in push mode: his reply arrives as its own run within seconds, and
            # getUpdates would return 409 against a registered webhook anyway.
            while (not finished and not push_mode and state.get("current")
                   and state["current"].get("stage") in WAITING_STAGES
                   and state["current"].get("asked_at")
                   and waited < HOLD_OPEN_SECONDS and not dry):
                chunk = min(50, HOLD_OPEN_SECONDS - waited)
                print(f"  holding open for a reply ({waited}/{HOLD_OPEN_SECONDS}s)")
                updates, offset = tg.updates(state.get("telegram_offset", 0), wait=chunk)
                waited += chunk
                if not updates:
                    continue
                texts = message_texts(updates, os.environ.get("TELEGRAM_CHAT_ID", ""))
                queue, more = handle_commands(texts, state, queue, tg, bank)
                state["telegram_offset"] = offset
                if more:
                    finished = advance(state, job, bank, tg, api_key, more[-1])

    if queue != started_queue:
        firebase_patch({"queued": queue}, dry=dry)

    # An idle tick changes nothing and commits nothing. At 96 ticks a day, a heartbeat
    # commit would bury the bank's real history under its own noise.
    if state != original_state:
        state["last_tick"] = now_iso()
        bank.save_state(state)
        bank.commit(f"apply-queue: {now_iso()[:16]}")

    cur = state.get("current")
    print(f"Tick done. queue={len(queue)} "
          f"current={cur['title'] + ' @ ' + cur['stage'] if cur else 'none'}")


def cmd_status():
    server = firebase_state() or {}
    queue = as_id_list(server.get("queued"))
    print(f"queued ({len(queue)}):")
    for q in queue:
        j = load_job(q)
        print(f"  {q}  {j.get('title') if j else '(not on dashboard)'}")
    pat = os.environ.get("BULLET_BANK_PAT", "")
    if not pat:
        print("BULLET_BANK_PAT not set; can't read run state.")
        return
    bank = Bank(pat)
    bank.clone()
    state = bank.load_state()
    cur = state.get("current")
    print(f"current: {json.dumps(cur, indent=1) if cur else 'none'}")
    print(f"history: {len(state.get('history', []))} completed")


def cmd_selftest():
    """Offline checks of the decisions that would otherwise only be observable by watching
    real money get spent."""
    fails = []

    def ok(name, cond):
        print(("  pass  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    ok("no comp data reads as risk",
       comp_risk({"market": "NL"})[0] is True)
    ok("clearly-above band is not risk",
       comp_risk({"market": "NL", "comp": {"stated": True, "min_base": 95000,
                                           "currency": "EUR"}})[0] is False)
    ok("band just over the floor is still risk",
       comp_risk({"market": "NL", "comp": {"stated": True, "min_base": 73000,
                                           "currency": "EUR"}})[0] is True)
    ok("mismatched currency is risk",
       comp_risk({"market": "NL", "comp": {"stated": True, "min_base": 90000,
                                           "currency": "GBP"}})[0] is True)
    ok("answered-from-bank gaps are not asked",
       pending_gaps({"gaps": [{"question": "q", "answered_by": "A-1"},
                              {"question": "q2", "answered_by": ""}]})
       == [{"question": "q2", "answered_by": ""}])
    ok("gap questions are capped",
       len(pending_gaps({"gaps": [{"question": f"q{i}"} for i in range(9)]}))
       == MAX_GAP_QUESTIONS)
    ok("a number picks the option",
       resolve_answer("2", {"options": ["a", "b", "c"]}) == "b")
    ok("free text is taken verbatim",
       resolve_answer("built the dashboards myself", {"options": ["a"]})
       == "built the dashboards myself")
    ok("'no meaningful experience' yields nothing",
       has_material("No meaningful experience") is False)
    ok("empty answer yields nothing", has_material("   ") is False)
    ok("a real answer has material", has_material("Cleaned up 400 accounts") is True)
    ok("firebase object-form queue reads as a list",
       as_id_list({"0": "a", "1": "b"}) == ["a", "b"])
    ok("the review gate is a named constant at 7.5",
       VARIATION_REVIEW_MIN_SCORE == 7.5)
    ok("a tie on the summary score breaks towards the bank canonical",
       best_summary([{"label": "B", "score": 8, "text": "b"},
                     {"label": "A", "score": 8, "text": "a"}])["label"] == "A")
    ok("an invented number never reaches the page",
       cvbuild.invented_numbers("lifted NRR 14 points", "beat NRR three years") == ["14"])
    ok("a real number is left alone",
       cvbuild.invented_numbers("managed 5.2M ARR", "managed a $5.2M ARR book") == [])
    ok("the CS track moves projects below experience",
       [x["heading"] for x in cvbuild.assemble_spec(
           cvbuild.load_base()[0], "CS", "T", "S", {}, "s")["sections"]]
       == ["EDUCATION", "PROFESSIONAL EXPERIENCE", "PROJECTS & OTHER EXPERIENCE"])
    ok("an unusable JD title falls back to the track default",
       cvbuild.role_title("", "BUILDER") == "REVENUE OPERATIONS & GTM SYSTEMS")
    ok("only Tom's chat is read",
       message_texts([{"update_id": 1, "message": {"text": "hi", "chat": {"id": 99}}},
                      {"update_id": 2, "message": {"text": "yes", "chat": {"id": 42}}}],
                     "42") == [(2, "yes")])
    print(f"\n{len(fails)} failed" if fails else "\nall passed")
    return 1 if fails else 0


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if "--selftest" in sys.argv:
        return cmd_selftest()
    if "--status" in sys.argv:
        return cmd_status()
    return tick(dry="--dry" in sys.argv)


if __name__ == "__main__":
    sys.exit(main() or 0)
