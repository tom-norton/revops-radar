/**
 * GitHub dispatcher for revops-radar. Cloudflare Worker.
 *
 * Why this exists: GitHub's scheduled workflows are best-effort and get dropped or delayed
 * under load, and this repo gets the bad end of it. Measured twice now -- a
 * once-every-15-minutes cron delivered 4 of an expected 60 runs over 15 hours, and over the
 * six days to 4 Sep 2026 apply.yml's every-fifteen-minutes cron produced 35 firings of an
 * expected ~576. The daily scan is on the same scheduler and lands 2 to 5 hours after its
 * cron, drifting further the longer the repo runs. Nothing in a workflow file can fix
 * that: the cron expressions in scan.yml are correct and GitHub does not honour them.
 *
 * So the work is pushed in rather than waited for, and this Worker is the thing that
 * pushes. Cloudflare's cron triggers fire on time, so the schedule lives here and asks
 * GitHub to start the run at the minute it is due. Telegram messages arrive the same way,
 * within about a second, so applyq.py never has to poll for one either.
 *
 * Two routes and a schedule:
 *   POST /telegram   Telegram's webhook. Authenticated by the secret token header.
 *   POST /queue      The dashboard's Apply button. Starts a run; carries no message.
 *   scheduled()      Cloudflare cron. Starts the daily scan at its three local times.
 *
 * Deploy: README, "Why the schedule lives in Cloudflare". `wrangler deploy` is what
 * registers the cron triggers, so a schedule change here does nothing until you redeploy.
 *
 * Secrets (wrangler secret put <NAME>, never in this file):
 *   GH_TOKEN          fine-grained PAT, Actions: read and write on revops-radar only
 *   TELEGRAM_SECRET   any random string; also given to Telegram in setWebhook
 */

const OWNER = "tom-norton";
const REPO = "revops-radar";
const APPLY_WORKFLOW = "apply.yml";
const SCAN_WORKFLOW = "scan.yml";
const REF = "main";
const FIREBASE_STATE_URL =
  "https://revops-radar-2822a-default-rtdb.europe-west1.firebasedatabase.app/revops-radar-state.json";
// GitHub caps how much a workflow input can carry, and Telegram allows 4096 characters.
// An answer that long is not a real answer, so it is cut rather than risking a rejected
// dispatch that would look to Tom like the bot ignoring him.
const MAX_MESSAGE = 3000;

// When the scan should run, in Tom's own time. A UTC cron cannot express this without
// being rewritten twice a year, which is the trap scan.yml sat in. Here the local time is
// worked out at firing time, so the clocks changing moves which UTC trigger matches rather
// than requiring an edit to this list.
const TZ = "Europe/Amsterdam";
// 10:15 is deliberately after the 10am revopsroles.com email, which is the whole reason
// the morning run's punctuality matters: fire it early and that source is not there yet.
const SCAN_TIMES = [
  { hour: 10, minute: 15, weekdaysOnly: false },
  { hour: 15, minute: 0, weekdaysOnly: true },
  { hour: 20, minute: 0, weekdaysOnly: true },
];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"];

/** Ask GitHub to start one of this repo's workflows now. */
async function dispatch(env, workflow, inputs) {
  const r = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        // GitHub rejects requests with no User-Agent.
        "User-Agent": "revops-radar-relay",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: REF, inputs }),
    },
  );
  // 204 is success. Anything else is worth seeing in `wrangler tail`, because a silent
  // relay failure looks exactly like the slow cron this was built to replace.
  if (r.status !== 204) {
    console.log("dispatch failed", workflow, r.status, (await r.text()).slice(0, 300));
    return false;
  }
  return true;
}

/**
 * Is `at` one of the three moments the scan is due, read in Tom's timezone?
 *
 * Exported for tests: the whole point of moving the schedule here is that it stays right
 * across a DST change, and the only way to know that is to run both sides of one.
 */
export function scanDueAt(at) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: TZ,
    hour12: false,
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(at);
  const get = (type) => (parts.find((p) => p.type === type) || {}).value;
  // hourCycle h23 still renders midnight as "24" in some ICU builds.
  const hour = Number(get("hour")) % 24;
  const minute = Number(get("minute"));
  const weekday = get("weekday");
  return SCAN_TIMES.some(
    (t) =>
      t.hour === hour &&
      t.minute === minute &&
      (!t.weekdaysOnly || WEEKDAYS.includes(weekday)),
  );
}

export default {
  /**
   * Cloudflare cron. wrangler.toml registers every UTC minute that could be one of the
   * three local times under either of Amsterdam's offsets, so on any given day half of
   * them return here without doing anything. That slack is the point: it is what lets the
   * DST decision live in code, where it can be tested, rather than in a cron expression.
   *
   * event.scheduledTime is the minute the trigger was *due*, not when it ran, so a firing
   * Cloudflare delivers a few seconds late still matches. Delivery is at-least-once, so a
   * duplicate is possible; scan.yml's concurrency group serialises it and the second run
   * finds nothing new in seen.json to score.
   */
  async scheduled(event, env, ctx) {
    const at = new Date((event && event.scheduledTime) || Date.now());
    if (!scanDueAt(at)) return;
    const ok = await dispatch(env, SCAN_WORKFLOW, {});
    // Visible in `wrangler tail`. A silent failure here looks exactly like the late cron
    // this was built to replace, which is the one thing worth being loud about.
    console.log("scan dispatch", at.toISOString(), ok ? "ok" : "FAILED");
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method !== "POST") {
      // A GET is almost always someone checking the Worker is alive.
      return new Response("revops-radar relay: POST /telegram or POST /queue\n", {
        status: 200,
      });
    }

    if (url.pathname === "/telegram") {
      // Telegram echoes the secret set via setWebhook. Without this check anyone who
      // learns the URL could start runs and put words in Tom's mouth, because the message
      // text is passed straight into the workflow as his answer.
      //
      // Note what this does and does not prove. It authenticates TELEGRAM, not Tom. A bot
      // is findable by its username, so anyone can message it, and Telegram relays every
      // one of those here with this same valid secret. The chat id is forwarded below and
      // checked against Tom's in applyq.py, which is the only place that holds it. Do not
      // treat arriving here as evidence the sender is Tom.
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_SECRET) {
        return new Response("no", { status: 403 });
      }
      let update = {};
      try {
        update = await request.json();
      } catch (e) {
        return new Response("ok", { status: 200 });
      }
      const msg = update.message || update.edited_message || {};
      const text = (msg.text || "").trim().slice(0, MAX_MESSAGE);
      const chat = String((msg.chat || {}).id || "");
      // Telegram retries anything that is not a 200, so unwanted updates are acknowledged
      // rather than rejected: a non-text message is nothing to act on, not an error.
      if (!text || !chat) return new Response("ok", { status: 200 });

      await dispatch(env, APPLY_WORKFLOW, { message: text, chat_id: chat });
      return new Response("ok", { status: 200 });
    }

    if (url.pathname === "/queue") {
      // Called by the public dashboard, so it carries no secret and cannot -- anything
      // shipped to the page is readable by anyone who opens it. Two things keep that from
      // mattering. This route passes no message, so it can never put words in Tom's mouth
      // the way /telegram could. And it refuses to start a run unless the queue actually
      // has something in it, so hammering this URL with an empty queue costs nothing and
      // never reaches GitHub. Filling the queue first means writing to Firebase, which has
      // been world-writable since long before this existed and is the real exposure, not
      // this. Past that, the workflow's concurrency group caps a flood at one running run
      // plus one queued.
      const cors = { "Access-Control-Allow-Origin": "*" };
      let queued = [];
      try {
        const state = await fetch(FIREBASE_STATE_URL).then((r) => r.json());
        const q = (state || {}).queued;
        // Firebase drops empty arrays entirely and returns sparse ones as index-keyed
        // objects, so both shapes have to count as a queue.
        queued = Array.isArray(q) ? q : q && typeof q === "object" ? Object.values(q) : [];
      } catch (e) {
        // If Firebase cannot be read, fall through and dispatch: a missed run is worse
        // than a wasted one, and the run itself re-reads the queue anyway.
        queued = ["unknown"];
      }
      if (!queued.filter(Boolean).length) {
        return new Response("queue empty; not dispatching\n", { status: 200, headers: cors });
      }
      await dispatch(env, APPLY_WORKFLOW, {});
      return new Response("ok", { status: 200, headers: cors });
    }

    return new Response("not found", { status: 404 });
  },
};
