// Exercises the Cloudflare relay's routing and guards with the network stubbed out.
//
//   node tests/test_worker.mjs
//
// The relay is the only part of this system that is publicly reachable and holds a token
// that can start CI, so its two guards are the ones worth pinning: /telegram must reject
// anything without the shared secret, because whatever it accepts becomes Tom's answer;
// and /queue, which is called from a public page and therefore cannot hold a secret at
// all, must refuse to reach GitHub unless something is genuinely queued.
import worker from '../worker/telegram-relay.js';

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

console.log(failures.length ? `\n${failures.length} FAILED` : '\nall passed');
process.exit(failures.length ? 1 : 0);
