/**
 * Telegram -> GitHub relay. Cloudflare Worker.
 *
 * Why this exists: GitHub's scheduled workflows are best-effort and get dropped under
 * load. Measured on this repo, a once-every-15-minutes cron delivered 4 of an expected 60
 * runs over 15 hours, with gaps of 5h45m. That is fine for a nightly scan and useless for
 * a conversation, because every message Tom sends has to wait for the next firing.
 *
 * So instead of the queue polling for work, the work pushes itself in. Telegram delivers
 * each message here within about a second; this asks GitHub to start a run immediately and
 * hands the message text along as an input, so applyq.py never has to poll for it.
 *
 * Two routes:
 *   POST /telegram   Telegram's webhook. Authenticated by the secret token header.
 *   POST /queue      The dashboard's Apply button. Starts a run; carries no message.
 *
 * Deploy: see the setup steps in the repo README ("Making it instant").
 *
 * Secrets (wrangler secret put <NAME>, never in this file):
 *   GH_TOKEN          fine-grained PAT, Actions: read and write on revops-radar only
 *   TELEGRAM_SECRET   any random string; also given to Telegram in setWebhook
 */

const OWNER = "tom-norton";
const REPO = "revops-radar";
const WORKFLOW = "apply.yml";
const REF = "main";
const FIREBASE_STATE_URL =
  "https://revops-radar-2822a-default-rtdb.europe-west1.firebasedatabase.app/revops-radar-state.json";
// GitHub caps how much a workflow input can carry, and Telegram allows 4096 characters.
// An answer that long is not a real answer, so it is cut rather than risking a rejected
// dispatch that would look to Tom like the bot ignoring him.
const MAX_MESSAGE = 3000;

/** Ask GitHub to start the apply-queue workflow now. */
async function dispatch(env, inputs) {
  const r = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
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
    console.log("dispatch failed", r.status, (await r.text()).slice(0, 300));
    return false;
  }
  return true;
}

export default {
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

      await dispatch(env, { message: text, chat_id: chat });
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
      await dispatch(env, {});
      return new Response("ok", { status: 200, headers: cors });
    }

    return new Response("not found", { status: 404 });
  },
};
