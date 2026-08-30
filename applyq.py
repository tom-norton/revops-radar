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
"""

import json, os, re, shutil, subprocess, sys, time
from datetime import datetime, timezone

import requests

import scan

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
# At most this many gaps go to interview per role. The skill's own cap. More than three
# questions and the phone stops being the right place to answer them.
MAX_GAP_QUESTIONS = 3

# Answers that mean "I have nothing here". A gap that gets one of these is dropped
# silently -- no bullet, no follow-up, no inference.
NO_MATERIAL = re.compile(r"^\s*(no|none|nope|nothing|n/?a|skip|no meaningful experience)\b",
                         re.I)

# One round trip per role, not five. The audit needs nothing from Tom, so it runs first;
# then every question this role will ever ask goes out in a single message and comes back
# as a single reply. GitHub's cron is the bottleneck (it delivered 4 of an expected 60 runs
# in the 15 hours after launch, with gaps up to 5h45m), and each extra turn costs another
# one of those. Five turns at that rate is a day per application.
STAGES = ["audit", "ask", "packet", "done"]

HELP = (
    "RevOps Radar apply queue\n\n"
    "/apply <id> - queue a role for the full workflow\n"
    "/queue - what's waiting\n"
    "/status - what the poller is working on right now\n"
    "/cancel - drop the role in flight and move on\n"
    "/help - this\n\n"
    "When I ask a question, just reply to it. Numbered options accept the number."
)

# ---------------------------------------------------------------- small helpers

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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

    def send(self, text):
        text = (text or "").strip()
        if not text:
            return
        if self.dry or not self.enabled:
            print(f"  [telegram] {text[:500]}")
            return
        # Telegram hard-caps a message at 4096 characters. An audit summary can exceed it,
        # and the API rejects the whole message rather than truncating -- so split.
        for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
            self._call("sendMessage", chat_id=self.chat_id, text=chunk,
                       disable_web_page_preview=True)

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
    band = f"{cur} {int(low or 0):,}-{int(high or 0):,}" if (low or high) else "no usable band"
    verdict = {"above_floor": "above floor", "borderline": "borderline",
               "below_floor": "BELOW FLOOR"}.get(res.get("verdict"), res.get("verdict") or "?")
    lines = [f"Salary research: {band} base", f"Verdict: {verdict}"]
    ruling = res.get("thirty_percent_ruling")
    if ruling and ruling != "n/a":
        lines.append(f"30% ruling: {ruling}")
    for s in (res.get("sources") or [])[:4]:
        lines.append(f"- {s}")
    if res.get("notes"):
        lines.append(f"Note: {res['notes']}")
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
        qs.append({"kind": "salary", "keyword": "salary", "reason": reason,
                   "question": (f"Comp risk on this one: {reason}. "
                                f"Want me to research the market band?"),
                   "options": ["Yes, research it", "No, skip it"]})
    for g in pending_gaps(audit):
        qs.append({"kind": "gap", "keyword": g.get("keyword"),
                   "question": g.get("question"),
                   "options": [o for o in (g.get("options") or []) if str(o).strip()][:4],
                   "adjacent": g.get("adjacent")})
    return qs


def format_questions(qs, job, audit):
    """One message carrying the whole conversation. Numbered so Tom can answer them in one
    reply the way a person actually would -- "1 yes, 2 did that for two quarters, 3 no" --
    rather than being pinged once per question across a day of cron firings."""
    head = [f"{job.get('title')} @ {job.get('company') or '?'}",
            f"Track: {audit.get('track')} - {audit.get('track_rationale', '')}", ""]
    if len(qs) == 1:
        head.append("One question and I can finish this off:")
    else:
        head.append(f"{len(qs)} questions and I can finish this off:")
    head.append("")
    for i, q in enumerate(qs, 1):
        head.append(f"{i}. {q['question']}")
        for j, o in enumerate(q.get("options") or [], 1):
            head.append(f"     {chr(96 + j)}) {o}")
        head.append("")
    head.append("Answer them all in one message, however you like. Numbers, letters, or "
                "just write it out. If you have nothing for one of them, say so and I'll "
                "leave it alone.")
    return "\n".join(head)


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


def split_is_sane(answers, reply):
    """Cheap guard against the splitter writing rather than splitting. Every answer it
    returns has to be traceable to something in Tom's reply or to an option he was offered;
    an answer that is neither is discarded, and a discarded answer is an unanswered
    question, which produces no bullet.

    This exists because the whole build rests on bullets coming from Tom's words. A
    splitter that paraphrases him into something more useful would break that quietly."""
    hay = re.sub(r"[^a-z0-9 ]+", " ", (reply or "").lower())
    hay_words = set(hay.split())
    clean = []
    for a in answers:
        words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", a.lower()).split() if len(w) > 3]
        if not a:
            clean.append("")
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
          "Phase 1 output. The CV itself is built in Phase 2; these bullets are not on a "
          "CV yet and are not in the bank yet.", ""]
    return "\n".join(L)


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


def handle_commands(texts, state, queue, tg):
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
                tg.send(f"Queued: {j.get('title')} at {j.get('company') or '?'} "
                        f"({len(queue)} in the queue)")
        elif cmd == "/queue":
            if not queue:
                tg.send("Queue is empty.")
            else:
                lines = []
                for q in queue[:15]:
                    j = load_job(q)
                    lines.append(f"- {j.get('title')} @ {j.get('company') or '?'}"
                                 if j else f"- {q} (not on the dashboard)")
                tg.send(f"{len(queue)} queued:\n" + "\n".join(lines))
        elif cmd == "/status":
            cur = state.get("current")
            if not cur:
                tg.send(f"Idle. {len(queue)} queued.")
            else:
                tg.send(f"Working on {cur.get('title')} @ {cur.get('company') or '?'}\n"
                        f"Stage: {cur.get('stage')}\n"
                        f"Started {cur.get('started_at', '')[:16]}\n"
                        f"{len(queue)} more queued.")
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
        done = next((h for h in state.get("history", [])
                     if h.get("id") == job_id and h.get("outcome") == "done"), None)
        if done:
            tg.send(f"{done.get('title') or job_id} already has a packet "
                    f"({done.get('packet')}). Skipped.")
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


def advance(state, job, bank, tg, api_key, answers_text):
    """Push `current` as far as it can go this tick. Returns True when the role finished.

    Three stages, and only one of them ever waits on Tom. That is the point: every wait
    costs a cron firing, and cron is delivering a few of those a day rather than the four
    an hour it was asked for."""
    cur = state["current"]
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
            reused = [g for g in ((cur.get("audit") or {}).get("gaps") or [])
                      if (g.get("answered_by") or "").strip()]
            tg.send(f"{job.get('title')} @ {job.get('company') or '?'}\n"
                    f"Track: {(cur.get('audit') or {}).get('track')}\n"
                    + (f"{len(reused)} gap(s) already answered from the answer bank. "
                       if reused else "")
                    + "No questions needed - finishing it off.")
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
            before = usage_snapshot()
            try:
                split = split_is_sane(split_reply(api_key, qs, reply), reply)
            except Exception as e:
                # One question is the common case, and a single reply to a single question
                # needs no splitting at all. Falling back keeps a splitter outage from
                # blocking the role.
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
                names = "\n".join(f"- {q['question']}" for q in unanswered)
                tg.send(f"Got the rest. Still open:\n{names}\n\n"
                        f"Answer if you have something, or say skip and I'll leave "
                        f"{'it' if len(unanswered) == 1 else 'them'} out.")
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

        name = f"{today()}-{slugify(job.get('company') or 'unknown')}-{slugify(job.get('title'))}.md"
        bank.write(f"{PACKET_DIR}/{name}",
                   packet_markdown(job, cur, audit, drafts, cur.get("salary")))

        # State moves to done before the commit, so the packet, the answers and the fact
        # that this role is finished all land in one commit. A crash between two commits
        # would otherwise leave a packet on disk that the state file says is unfinished.
        state.setdefault("history", []).append(
            {"id": cur["id"], "title": cur.get("title"), "company": cur.get("company"),
             "outcome": "done", "at": now_iso(), "packet": name,
             "usage": cur.get("usage") or {}})
        state["current"] = None
        state["last_tick"] = now_iso()
        bank.save_state(state)
        sha = bank.commit(f"Apply packet: {job.get('title')} at {job.get('company') or '?'}")

        n_new = len((drafts or {}).get("bullets") or [])
        n_drop = len((drafts or {}).get("dropped") or [])
        msg = [f"Done: {job.get('title')} @ {job.get('company') or '?'}",
               f"Track {audit.get('track')} | {n_new} new bullet(s)"
               + (f", {n_drop} gap(s) left open" if n_drop else ""),
               f"Packet: {PACKET_DIR}/{name}"]
        if sha:
            msg.append(f"https://{BANK_REPO}/commit/{sha}")
        msg.append(job.get("url") or "")
        tg.send("\n".join(m for m in msg if m))
        return True
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
    updates, offset = tg.updates(state.get("telegram_offset", 0))
    texts = message_texts(updates, os.environ.get("TELEGRAM_CHAT_ID", ""))
    queue, answers = handle_commands(texts, state, queue, tg)
    # Only the newest free-text message answers the pending question. If Tom sent three
    # lines while thinking out loud, the last one is his answer.
    answer_text = answers[-1] if answers else ""
    # Written only once the updates have actually been handled. Bumping the offset before
    # that would acknowledge a question's answer that a crash then threw away, and Tom
    # would be asked the same thing again with no way to tell why.
    state["telegram_offset"] = offset

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
            while (not finished and state.get("current")
                   and state["current"].get("stage") == "ask"
                   and state["current"].get("asked_at")
                   and waited < HOLD_OPEN_SECONDS and not dry):
                chunk = min(50, HOLD_OPEN_SECONDS - waited)
                print(f"  holding open for a reply ({waited}/{HOLD_OPEN_SECONDS}s)")
                updates, offset = tg.updates(state.get("telegram_offset", 0), wait=chunk)
                waited += chunk
                if not updates:
                    continue
                texts = message_texts(updates, os.environ.get("TELEGRAM_CHAT_ID", ""))
                queue, more = handle_commands(texts, state, queue, tg)
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
