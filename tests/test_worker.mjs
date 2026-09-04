// Exercises the Cloudflare Worker's routing, guards and schedule with the network stubbed.
//
//   node tests/test_worker.mjs
//
// The Worker is the only part of this system that is publicly reachable and holds a token
// that can start CI, so its two guards are the ones worth pinning: /telegram must reject
// anything without the shared secret, because whatever it accepts becomes Tom's answer;
// and /queue, which is called from a public page and therefore cannot hold a secret at
// all, must refuse to reach GitHub unless something is genuinely queued.
//
// The schedule is pinned for a different reason. It moved here because GitHub's cron does
// not keep time, and the one thing that could quietly reintroduce the same symptom is this
// Worker firing at the wrong hour -- which is exactly what happens if the DST handling is
// wrong, and would not show up until 25 Oct. So both sides of that change are tested.
import worker, { scanDueAt } from '../worker/telegram-relay.js';

let calls = [];
let firebaseQueue = ["az-nl-1"];

globalThis.fetch = async (url, opts) => {
  calls.push({ url: String(url), opts });
  if (String(url).includes('firebasedatabase')) {
    return { json: async () => ({ queued: firebaseQueue }) };
  }
  return { status: 204, text: async () => '' };
};

const env = { GH_TOKEN: 'gh_fake', TELEGRAM_SECRET: 's3cret' };
const post = (path, body, headers = {}) =>
  new Request('https://relay.example.com' + path, {
    method: 'POST', headers, body: JSON.stringify(body),
  });

let failures = [];
const check = (name, cond) => {
  console.log((cond ? '  pass  ' : '  FAIL  ') + name);
  if (!cond) failures.push(name);
};
const dispatches = () => calls.filter(c => c.url.includes('api.github.com'));

// --- telegram route rejects a request without the secret header
calls = [];
let r = await worker.fetch(post('/telegram', { message: { text: 'hi', chat: { id: 42 } } }), env);
check('no secret header is rejected', r.status === 403);
check('and nothing is dispatched', dispatches().length === 0);

// --- telegram route with the right secret dispatches the message
calls = [];
r = await worker.fetch(
  post('/telegram', { message: { text: 'did the cleanup', chat: { id: 42 } } },
       { 'X-Telegram-Bot-Api-Secret-Token': 's3cret' }), env);
check('correct secret is accepted', r.status === 200);
const d = dispatches()[0];
check('dispatch carries the message', JSON.parse(d.opts.body).inputs.message === 'did the cleanup');
check('dispatch carries the chat id', JSON.parse(d.opts.body).inputs.chat_id === '42');
check('dispatch targets main', JSON.parse(d.opts.body).ref === 'main');
check('dispatch sends a User-Agent', !!d.opts.headers['User-Agent']);

// --- a non-text update is acknowledged, not dispatched (Telegram retries non-200s)
calls = [];
r = await worker.fetch(
  post('/telegram', { message: { photo: [{}], chat: { id: 42 } } },
       { 'X-Telegram-Bot-Api-Secret-Token': 's3cret' }), env);
check('a photo is acknowledged', r.status === 200);
check('but not dispatched', dispatches().length === 0);

// --- oversized message is truncated rather than rejected by GitHub
calls = [];
await worker.fetch(
  post('/telegram', { message: { text: 'x'.repeat(5000), chat: { id: 42 } } },
       { 'X-Telegram-Bot-Api-Secret-Token': 's3cret' }), env);
check('a 5000-char message is cut to 3000',
      JSON.parse(dispatches()[0].opts.body).inputs.message.length === 3000);

// --- queue route dispatches when something is actually queued
calls = []; firebaseQueue = ["az-nl-1"];
r = await worker.fetch(post('/queue', {}), env);
check('a non-empty queue dispatches', r.status === 200 && dispatches().length === 1);
check('and passes no message', Object.keys(JSON.parse(dispatches()[0].opts.body).inputs).length === 0);

// --- queue route refuses when the queue is empty (the anti-abuse guard)
calls = []; firebaseQueue = [];
r = await worker.fetch(post('/queue', {}), env);
check('an empty queue does not reach GitHub', dispatches().length === 0);
check('and still answers 200', r.status === 200);

// --- Firebase's object form for a sparse array still counts as a queue
calls = []; firebaseQueue = { 0: 'az-nl-1', 1: 'az-nl-2' };
await worker.fetch(post('/queue', {}), env);
check('object-form queue counts', dispatches().length === 1);

// --- unknown path
calls = [];
r = await worker.fetch(post('/nope', {}), env);
check('unknown path is 404', r.status === 404);

// --- GET is a friendly liveness page
r = await worker.fetch(new Request('https://relay.example.com/', { method: 'GET' }), env);
check('GET returns a liveness page', r.status === 200);

// --- the scan schedule, read in Europe/Amsterdam
//
// Every instant below is written in UTC, which is what a cron trigger hands over, and the
// assertion is about what that instant is in Tom's local time. Two Fridays: 4 Sep 2026 is
// CEST (UTC+2), 6 Nov 2026 is CET (UTC+1). The same three local times therefore sit an
// hour apart in UTC across the two, and both have to fire -- that is the whole DST claim.
const due = (iso) => scanDueAt(new Date(iso));

check('summer: 08:15Z is 10:15 local, due',      due('2026-09-04T08:15:00Z'));
check('summer: 13:00Z is 15:00 local, due',      due('2026-09-04T13:00:00Z'));
check('summer: 18:00Z is 20:00 local, due',      due('2026-09-04T18:00:00Z'));
check('summer: 09:15Z is 11:15 local, not due', !due('2026-09-04T09:15:00Z'));
check('summer: 14:00Z is 16:00 local, not due', !due('2026-09-04T14:00:00Z'));

check('winter: 09:15Z is 10:15 local, due',      due('2026-11-06T09:15:00Z'));
check('winter: 14:00Z is 15:00 local, due',      due('2026-11-06T14:00:00Z'));
check('winter: 19:00Z is 20:00 local, due',      due('2026-11-06T19:00:00Z'));
check('winter: 08:15Z is 09:15 local, not due', !due('2026-11-06T08:15:00Z'));

// The morning run is daily; the other two are weekdays only, because the boards Tom is
// waiting on do not post at 3pm on a Sunday and a run that finds nothing still costs money.
check('Sunday 10:15 local is due',        due('2026-09-06T08:15:00Z'));
check('Sunday 15:00 local is not due',   !due('2026-09-06T13:00:00Z'));
check('Sunday 20:00 local is not due',   !due('2026-09-06T18:00:00Z'));
check('Saturday 15:00 local is not due', !due('2026-09-05T13:00:00Z'));

// The day the clocks go back. 08:15Z is 09:15 local by then, and must not fire; the run
// has to land after the 10am revopsroles.com email, and 09:15Z does.
check('25 Oct: 08:15Z no longer fires', !due('2026-10-25T08:15:00Z'));
check('25 Oct: 09:15Z fires instead',    due('2026-10-25T09:15:00Z'));

// --- scheduled() dispatches the scan, and only when something is due
calls = [];
await worker.scheduled({ scheduledTime: Date.parse('2026-09-04T08:15:00Z') }, env);
check('a due tick dispatches once', dispatches().length === 1);
check('and it targets scan.yml', dispatches()[0].url.includes('/workflows/scan.yml/'));
check('and passes no inputs', Object.keys(JSON.parse(dispatches()[0].opts.body).inputs).length === 0);

calls = [];
await worker.scheduled({ scheduledTime: Date.parse('2026-09-04T09:15:00Z') }, env);
check('a tick that is not due reaches nothing', dispatches().length === 0);

console.log(failures.length ? `\n${failures.length} FAILED` : '\nall passed');
process.exit(failures.length ? 1 : 0);
