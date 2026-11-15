// Runs tools/tracker-append.gs's doPost against an in-memory sheet, so a change to the
// Apps Script is checked before Tom pastes and redeploys it.
//
//   node tests/test_tracker_gs.mjs
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('../tools/tracker-append.gs', import.meta.url), 'utf8');

function makeSheet(rows) {
  const g = rows.map(r => r.slice());
  const width = () => Math.max(...g.map(r => r.length));
  const cellAt = (r, c) => { while (g.length < r) g.push([]); const row = g[r - 1]; while (row.length < c) row.push(''); return row; };
  const range = (r, c, nr = 1, nc = 1) => ({
    getDisplayValues: () => Array.from({ length: nr }, (_, i) =>
      Array.from({ length: nc }, (_, k) => String((g[r - 1 + i] || [])[c - 1 + k] ?? ''))),
    setValues: vals => vals.forEach((row, i) => row.forEach((v, k) => { cellAt(r + i, c + k)[c - 1 + k] = v; })),
    setValue: v => { cellAt(r, c)[c - 1] = v; },
    setNumberFormat: () => {}, copyTo: () => {},
  });
  return {
    grid: g,
    getLastRow: () => g.length, getLastColumn: () => width(),
    insertRowBefore: n => g.splice(n - 1, 0, []),
    getRange: range,
  };
}

function run(body, sheet) {
  const env = {
    SpreadsheetApp: { getActiveSpreadsheet: () => ({ getSheetByName: () => sheet, getSpreadsheetTimeZone: () => 'UTC' }),
                      flush: () => {}, CopyPasteType: { PASTE_FORMAT: 1 } },
    PropertiesService: { getScriptProperties: () => ({ getProperty: () => 'tok' }) },
    LockService: { getScriptLock: () => ({ waitLock: () => {}, releaseLock: () => {} }) },
    Utilities: { formatDate: () => '2026-11-14' },
    ContentService: { createTextOutput: t => ({ setMimeType: () => t, text: t }), MimeType: { JSON: 'json', CSV: 'csv' } },
  };
  const fn = new Function(...Object.keys(env), src + '\nreturn doPost;');
  const doPost = fn(...Object.values(env));
  return JSON.parse(doPost({ postData: { contents: JSON.stringify(body) } }));
}

const failures = [];
const check = (name, cond) => { console.log((cond ? '  pass  ' : '  FAIL  ') + name); if (!cond) failures.push(name); };

const HEAD = ['Company', 'Title', 'Location', 'Score', 'Date Applied', 'Status', 'Link', 'Referral?', 'Interview Date', 'Interview 2 Date'];
const sheet = makeSheet([HEAD, ['Old Co', 'Old role', 'UK', 7, '', 'Applied', 'https://old', '', '', '']]);
let res = run({ token: 'tok', company: 'Acme', title: 'RevOps Manager', location: 'Netherlands', score: '7.1',
                link: 'https://acme/1', shot: 7.3, want: 9.4, rank: 8.3, gaps: 'years 5y; bi', domain: 'Y',
                sponsor: 'NL sponsor' }, sheet);
check('application row written', res.ok && sheet.grid[1][0] === 'Acme' && sheet.grid[1][6] === 'https://acme/1');
const head = sheet.grid[0];
for (const [name, v] of [['Shot', 7.3], ['Want', 9.4], ['Rank', 8.3], ['Gaps', 'years 5y; bi'], ['Domain', 'Y'], ['Sponsor', 'NL sponsor']]) {
  const col = head.indexOf(name);
  check(`${name} column created and filled`, col >= HEAD.length && sheet.grid[1][col] === v);
}
check('the ten original columns do not move', HEAD.every((h, i) => head[i] === h));
check('the older row is untouched', sheet.grid[2][0] === 'Old Co');
// second application reuses the columns rather than adding more
run({ token: 'tok', company: 'Beta', title: 'CSM', link: 'https://beta/1', shot: 6, want: 8 }, sheet);
check('radar columns are reused, not duplicated', sheet.grid[0].filter(h => h === 'Shot').length === 1
      && sheet.grid[1][sheet.grid[0].indexOf('Shot')] === 6);
// an old dashboard sends no radar fields: no columns are touched
const plain = makeSheet([HEAD]);
run({ token: 'tok', company: 'Gamma', title: 'X', link: 'https://g/1' }, plain);
check('no radar fields, no new columns', plain.grid[0].length === HEAD.length);
check('duplicate link is not added twice', run({ token: 'tok', company: 'Acme', link: 'https://acme/1' }, sheet).dup === true);
check('bad token refused', run({ token: 'nope', company: 'X' }, sheet).ok === false);

if (failures.length) { console.log(`\n${failures.length} failed`); process.exit(1); }
console.log('\nall passed');
