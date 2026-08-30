/**
 * Telegram -> GitHub relay. Cloudflare Worker.
 *
 * Why this exists: GitHub's scheduled workflows are best-effort and get dropped under
 * load. Measured on this repo, `*/15 * * * *` delivered 4 of an expected 60 runs over 15
 * hours, with gaps of 5h45m. That is fine for a nightly scan and useless for a
 * conversation, because every message Tom sends has to wait for the next firing.
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
      const text = (msg.text || "").trim();
      const chat = String((msg.chat || {}).id || "");
      // Telegram retries anything that is not a 200, so unwanted updates are acknowledged
      // rather than rejected: a non-text message is nothing to act on, not an error.
      if (!text || !chat) return new Response("ok", { status: 200 });

      await dispatch(env, { message: text, chat_id: chat });
      return new Response("ok", { status: 200 });
    }

    if (url.pathname === "/queue") {
      // Called by the public dashboard, so it carries no secret and cannot. The blast
      // radius is small by construction: this route passes no message, the run only ever
      // acts on what is already in the Firebase queue (which is world-writable anyway, and
      // has been since long before this existed), and the workflow's concurrency group
      // means a flood of requests still produces at most one running and one queued run.
      await dispatch(env, {});
      return new Response("ok", {
        status: 200,
        headers: { "Access-Control-Allow-Origin": "*" },
      });
    }

    return new Response("not found", { status: 404 });
  },
};
