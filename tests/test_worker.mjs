// Exercises the Cloudflare Worker's routing, guards and schedule with the network stubbed.
//
//   node tests/test_worker.mjs
//
// The Worker is the only part of this system that is publicly reachable and holds a token
// that can start CI, so its guards are the ones worth pinning. /telegram must reject
// anything without the shared secret, because whatever it accepts becomes Tom's answer.
// That check is unconditional and still runs first even now that both apply routes are
// mothballed (APPLY_RELAY_ENABLED), which is why the tests below assert "answers, reaches
// nothing" rather than being deleted -- a switched-off route that quietly started
// dispatching again would otherwise be invisible.
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

// --- the apply routes are mothballed: they answer, and they reach nothing
//
// Both routes still run every guard above them before they stop, which is the point of
// testing them rather than deleting the tests. When APPLY_RELAY_ENABLED goes back to true
// these become dispatch assertions again.

// A correct secret gets a 200 and no dispatch. The 200 matters on its own: Telegram retries
// anything else, so a route that is switched off has to say so politely or Tom's messages
// turn into a retry loop against a bot nobody is listening to.
calls = [];
r = await worker.fetch(
  post('/telegram', { message: { text: 'did the cleanup', chat: { id: 42 } } },
       { 'X-Telegram-Bot-Api-Secret-Token': 's3cret' }), env);
check('correct secret still answers 200', r.status === 200);
check('but the apply queue is not started', dispatches().length === 0);

// --- a non-text update is acknowledged, not dispatched (Telegram retries non-200s)
calls = [];
r = await worker.fetch(
  post('/telegram', { message: { photo: [{}], chat: { id: 42 } } },
       { 'X-Telegram-Bot-Api-Secret-Token': 's3cret' }), env);
check('a photo is acknowledged', r.status === 200);
check('but not dispatched', dispatches().length === 0);

// The oversized-message truncation test is gone with the dispatch it asserted on: the slice
// still happens in the route, but with nothing dispatched there is no way to observe it.
// Restore it alongside APPLY_RELAY_ENABLED.

// --- /queue reaches nothing, whatever the queue says
//
// Both cases now have the same answer, and the pair is kept deliberately: a non-empty queue
// was the case that used to dispatch, so it is the one worth pinning as silent.
calls = []; firebaseQueue = ["az-nl-1"];
r = await worker.fetch(post('/queue', {}), env);
check('a non-empty queue does not reach GitHub', dispatches().length === 0);
check('and still answers 200', r.status === 200);

calls = []; firebaseQueue = [];
r = await worker.fetch(post('/queue', {}), env);
check('an empty queue does not reach GitHub either', dispatches().length === 0);
check('and also answers 200', r.status === 200);

// Firebase is not even read while the route is off, which is the cheapest possible answer
// to a page that still has the old button cached.
calls = []; firebaseQueue = { 0: 'az-nl-1', 1: 'az-nl-2' };
await worker.fetch(post('/queue', {}), env);
check('and Firebase is not consulted', calls.length === 0);

// --- unknown path
calls = [];
r = await worker.fetch(post('/nope', {}), env);
check('unknown path is 404', r.status === 404);

// --- GET is a friendly liveness page
r = await worker.fetch(new Request('https://relay.example.com/', { method: 'GET' }), env);
check('GET returns a liveness page', r.status === 200);

// --- the scan schedule, read in America/New_York
//
// Every instant below is written in UTC, which is what a cron trigger hands over, and the
// assertion is about what that instant is in Tom's local time. Two Fridays: 4 Sep 2026 is
// EDT (UTC-4), 6 Nov 2026 is EST (UTC-5). The same local times therefore sit an hour
// apart in UTC across the two, and both have to fire -- that is the whole DST claim.
const due = (iso) => scanDueAt(new Date(iso));

check('summer: 12:00Z is 08:00 local, due',      due('2026-09-04T12:00:00Z'));
check('summer: 16:30Z is 12:30 local, due',      due('2026-09-04T16:30:00Z'));
// 8pm Friday is already Saturday in UTC; the weekday has to be read locally.
check('summer: Sat 00:00Z is Fri 20:00 local, due', due('2026-09-05T00:00:00Z'));
check('summer: 13:00Z is 09:00 local, not due on a weekday', !due('2026-09-04T13:00:00Z'));
check('summer: 17:30Z is 13:30 local, not due', !due('2026-09-04T17:30:00Z'));
check('summer: 01:00Z is 21:00 local, not due', !due('2026-09-05T01:00:00Z'));

check('winter: 13:00Z is 08:00 local, due',      due('2026-11-06T13:00:00Z'));
check('winter: 17:30Z is 12:30 local, due',      due('2026-11-06T17:30:00Z'));
check('winter: Sat 01:00Z is Fri 20:00 local, due', due('2026-11-07T01:00:00Z'));
check('winter: 12:00Z is 07:00 local, not due', !due('2026-11-06T12:00:00Z'));
check('winter: 16:30Z is 11:30 local, not due', !due('2026-11-06T16:30:00Z'));

// Weekends are one run at 9am, and none of the weekday times.
check('Saturday 09:00 local is due',       due('2026-09-05T13:00:00Z'));
check('Sunday 09:00 local is due',         due('2026-09-06T13:00:00Z'));
check('Sunday 08:00 local is not due',    !due('2026-09-06T12:00:00Z'));
check('Sunday 12:30 local is not due',    !due('2026-09-06T16:30:00Z'));
check('Sunday 20:00 local is not due',    !due('2026-09-07T00:00:00Z'));
check('Saturday 12:30 local is not due',  !due('2026-09-05T16:30:00Z'));
check('Monday 20:00 local is due',         due('2026-09-08T00:00:00Z'));

// The day the clocks go back, 1 Nov 2026, a Sunday. 13:00Z is 08:00 local by then and must
// not fire; 14:00Z is 09:00 and does.
check('1 Nov: 13:00Z no longer fires', !due('2026-11-01T13:00:00Z'));
check('1 Nov: 14:00Z fires instead',    due('2026-11-01T14:00:00Z'));

// --- scheduled() dispatches the scan, and only when something is due
calls = [];
await worker.scheduled({ scheduledTime: Date.parse('2026-09-04T12:00:00Z') }, env);
check('a due tick dispatches once', dispatches().length === 1);
check('and it targets scan.yml', dispatches()[0].url.includes('/workflows/scan.yml/'));
check('and passes no inputs', Object.keys(JSON.parse(dispatches()[0].opts.body).inputs).length === 0);

calls = [];
await worker.scheduled({ scheduledTime: Date.parse('2026-09-04T13:00:00Z') }, env);
check('a tick that is not due reaches nothing', dispatches().length === 0);

console.log(failures.length ? `\n${failures.length} FAILED` : '\nall passed');
process.exit(failures.length ? 1 : 0);
