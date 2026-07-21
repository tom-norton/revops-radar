// Cloudflare Pages Function — cross-device Hide/Applied state for the dashboard.
// Requires a KV namespace bound to this Pages project as "STATE_KV", added via
// the project's Bindings tab in the dashboard (not wrangler.toml -- that file
// causes the Workers Builds CI system to mishandle the deploy command here).
//
// GET  /api/state           -> { hidden: [...ids], applied: [...ids] }
// POST /api/state           body: { act: "hide"|"applied"|"restore", id: "<job id>" }
//                           -> returns the updated { hidden, applied }

const KEY = "state";
const ACTIONS = new Set(["hide", "applied", "restore"]);

async function readState(kv) {
  const raw = await kv.get(KEY);
  if (!raw) return { hidden: [], applied: [] };
  try {
    const parsed = JSON.parse(raw);
    return {
      hidden: Array.isArray(parsed.hidden) ? parsed.hidden : [],
      applied: Array.isArray(parsed.applied) ? parsed.applied : [],
    };
  } catch {
    return { hidden: [], applied: [] };
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export async function onRequestGet({ env }) {
  const state = await readState(env.STATE_KV);
  return json(state);
}

export async function onRequestPost({ request, env }) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid JSON body" }, 400);
  }
  const { act, id } = body || {};
  if (typeof id !== "string" || !id || !ACTIONS.has(act)) {
    return json({ error: "expected { act: hide|applied|restore, id: string }" }, 400);
  }

  const state = await readState(env.STATE_KV);
  const hidden = new Set(state.hidden);
  const applied = new Set(state.applied);

  if (act === "hide") { hidden.add(id); applied.delete(id); }
  else if (act === "applied") { applied.add(id); hidden.delete(id); }
  else { hidden.delete(id); applied.delete(id); } // restore

  const next = { hidden: [...hidden], applied: [...applied] };
  await env.STATE_KV.put(KEY, JSON.stringify(next));
  return json(next);
}
