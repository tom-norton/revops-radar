// The outreach list: how the dashboard tags a role at a company on the Companies tab, and
// what the tracker web app's three outreach reads answer (tools/tracker-append.gs, run here
// against a fake spreadsheet, since it is pasted into the sheet by hand and nothing else
// would catch a mistake before Tom deploys it).
//
//   node tests/test_outreach.mjs
import { readFileSync } from 'node:fs';

let failures = [];
const check = (name, cond) => {
  console.log((cond ? '  pass  ' : '  FAIL  ') + name);
  if (!cond) failures.push(name);
};

// ---------------------------------------------------------------- the dashboard
const html = readFileSync(new URL('../docs/index.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const keySrc = script.match(/const COMPANY_NOISE = [\s\S]*?\nfunction companyKey[\s\S]*?\n}\n/)[0];
const outSrc = script.match(/function indexOutreach[\s\S]*?\nfunction refreshOutreach/)[0]
  .replace(/\nfunction refreshOutreach$/, '')
  .replace(/\nlet outreachMap[\s\S]*?\nfunction outreachText/, '\nfunction outreachText');
const dash = new Function(keySrc + outSrc + 'return { companyKey, indexOutreach, outreachText, outreachLabel };')();

const idx = dash.indexOutreach([
  { company: 'BWise (SAI360)', keys: ['bwise', 'sai360'], active: true, signal: 'Warm',
    contact: 'Neil Example', stage: 'emailed 16 Nov 2026', contacts: 3 },
  { company: 'Workiva', keys: ['workiva'], active: false, signal: 'Hot', contacts: 0 },
]);
const of = c => idx.get(dash.companyKey(c)) || null;
check('a role at "SAI360 Inc." finds BWise (SAI360) through its alias', of('SAI360 Inc.')?.company === 'BWise (SAI360)');
check('a role at "BWise B.V." finds it by its own name', of('BWise B.V.')?.company === 'BWise (SAI360)');
check('a company not on the list finds nothing', of('Salesforce') === null);
check('an active company with a contact labels the first name and stage',
  dash.outreachLabel(of('SAI360')) === 'Outreach: Neil, emailed 16 Nov 2026');
check('a listed company with no contact labels its signal',
  dash.outreachLabel(of('Workiva')) === 'On list: Hot');
check('the hover text carries the full name, status and signal',
  dash.outreachText(of('SAI360')) === 'Active · Warm · Neil Example (emailed 16 Nov 2026)');
check('a missing list is an empty index', dash.indexOutreach(null).size === 0);

// ---------------------------------------------------------------- the web app
const gs = readFileSync(new URL('../tools/tracker-append.gs', import.meta.url), 'utf8');
const sheet = (rows, values) => ({
  getDataRange: () => ({ getDisplayValues: () => rows.map(r => r.slice()),
                         getValues: () => (values || rows).map(r => r.slice()) }),
  getLastRow: () => rows.length,
  getRange: (r, c, nr, nc) => ({ getDisplayValues: () =>
    rows.slice(r - 1, r - 1 + nr).map(x => x.slice(c - 1, c - 1 + nc)) }),
});
const day = d => new Date(Date.now() - d * 864e5);
const tabs = {
  Companies: sheet([
    ['Tier', 'Company', 'Aliases', 'Names to match', 'Active', 'Hiring signal', 'Silent contacts', 'Drop (2 silent)'],
    ['A', 'BWise (SAI360)', 'SAI360', 'BWise;SAI360', 'Yes', 'Warm', '0', ''],
    ['A', 'Fenergo', '', 'Fenergo', 'Yes', 'Warm', '2', 'Drop: next company moves up'],
    ['B', 'Workiva', '', 'Workiva', '', 'Hot', '0', ''],
  ]),
  Contacts: sheet([
    ['Name', 'Company', 'First Outreach', 'Follow-up Date', 'Chat Date', 'Notes', 'Contact ID', 'Stage',
     'Next action', 'Title', 'First email sent', 'Last reply', 'Role (First or Backup)'],
    ['Old Contact', 'SAI360', '5/1/26', '', '', '', 'C-077', 'Messaged', '', '', '', '', ''],
    ['Neil Example', 'SAI360', '', '', '', '', 'C-143', 'To contact', '', '', '16 Nov 2026', '18 Nov 2026', 'First'],
    ['Older Example', 'Fenergo', '4/7/26', '', '', '', 'C-050', 'Messaged', '', '', '', '', ''],
    ['Ann Example', 'Fenergo', '', '', '', '', 'C-145', 'To contact', '', '', '', '', 'First'],
    ['Nadira Example', 'Fenergo', '', '', '', '', 'C-146', 'To contact', '', '', '', '', 'Backup'],
    ['Wk Example', 'Workiva', '3/9/26', '', '', '', 'C-020', 'Messaged', '', '', '', '', ''],
  ]),
  Touches: sheet(
    [['Contact ID', 'Type', 'Channel', 'Sent date', 'Reply date', 'Status'],
     ['C-143', 'Outreach', 'Email', 'x', 'x', 'Replied'],
     ['C-145', 'Outreach', 'Email', 'x', '', 'Sent'],
     ['C-145', 'Follow-up', 'Email', 'x', '', 'Bounced'],
     ['C-001', 'Outreach', 'Email', 'x', '', 'Sent']],
    [['Contact ID', 'Type', 'Channel', 'Sent date', 'Reply date', 'Status'],
     ['C-143', 'Outreach', 'Email', day(3), day(1), 'Replied'],
     ['C-145', 'Outreach', 'Email', day(2), '', 'Sent'],
     ['C-145', 'Follow-up', 'Email', day(1), '', 'Bounced'],
     ['C-001', 'Outreach', 'Email', day(30), '', 'Sent']]),
  'Keep checks': sheet([
    ['Keep checks'], [''], ['Phase', 'Measure', 'Now', 'Target', 'Status'],
    ['1', 'Outreach emails sent since baseline', '2', '30', 'Keep sending'],
  ]),
};
const ss = { getSheetByName: n => tabs[n] || null };
const stubs = {
  ContentService: { createTextOutput: t => ({ text: t, setMimeType() { return this; } }),
                    MimeType: { JSON: 'json', CSV: 'csv' } },
  SpreadsheetApp: { getActiveSpreadsheet: () => ss },
  PropertiesService: { getScriptProperties: () => ({ getProperty: () => 'tok' }) },
  LockService: {}, Utilities: {},
};
const app = new Function(...Object.keys(stubs), gs + '\nreturn { doGet };')(...Object.values(stubs));
const get = params => JSON.parse(app.doGet({ parameter: { token: 'tok', ...params } }).text);

check('a wrong token is refused', get({ token: 'nope', action: 'outreach' }).error === 'bad token');
const o = get({ action: 'outreach' });
check('outreach answers ok with every listed company', o.ok && o.companies.length === 3);
const bw = o.companies.find(c => c.company === 'BWise (SAI360)');
check('BWise carries its keys, active flag and signal',
  JSON.stringify(bw.keys) === '["bwise","sai360"]' && bw.active === true && bw.signal === 'Warm');
check('BWise counts both contacts found through the SAI360 alias', bw.contacts === 2);
check('the most advanced contact wins (replied beats an old message)',
  bw.contact === 'Neil Example' && bw.stage === 'replied 18 Nov 2026');
const fe = o.companies.find(c => c.company === 'Fenergo');
check('a contact queued this round outranks an email from the earlier round',
  fe.stage === 'to contact' && fe.contact !== 'Older Example');
check('before anyone is emailed, the First contact shows rather than the backup', fe.contact === 'Ann Example');
check('a company with only an earlier round says so',
  o.companies.find(c => c.company === 'Workiva').stage === 'emailed 3/9/26 (earlier round)');
const oc = get({ action: 'outreach_companies' });
check('outreach_companies carries no people',
  oc.ok && oc.companies.every(c => !('contact' in c) && !('stage' in c) && !('contacts' in c)));
const f = get({ action: 'funnel' });
check('funnel reads the Keep checks rows', f.keep_checks.length === 1 && f.keep_checks[0].now === '2');
check('funnel counts the last seven days only',
  f.last_7_days.outreach_sent === 2 && f.last_7_days.follow_ups_sent === 1 && f.last_7_days.replies === 1);
check('funnel counts a bounce', f.last_7_days.bounced === 1);
const fen = f.active.find(a => a.company === 'Fenergo');
check('funnel lists only Active companies', f.active.length === 2 && !f.active.some(a => a.company === 'Workiva'));
check('funnel per company: emailed (this round only), replied, drop flag',
  fen.emailed === 0 && fen.replied === 0 && fen.drop === true &&
  f.active.find(a => a.company === 'BWise (SAI360)').replied === 1);
check('funnel carries no names of people', !JSON.stringify(f).includes('Example'));
check('an unknown action is refused', get({ action: 'nope' }).error === 'unknown action');

console.log(failures.length ? `\n${failures.length} FAILED` : '\nall passed');
process.exit(failures.length ? 1 : 0);
