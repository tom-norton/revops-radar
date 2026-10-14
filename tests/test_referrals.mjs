// Pins how the dashboard matches a job's company to the Contacts tab of Job Search Tracker
// (the referral flag). A miss means a referral role goes unflagged; a false hit puts a
// stranger's company at the top of the page.
//
//   node tests/test_referrals.mjs
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../docs/index.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
new Function(script); // whole dashboard script parses

const src = script.match(/const COMPANY_NOISE = [\s\S]*?\nfunction indexContacts[\s\S]*?\n}\n/)[0];
const { companyKey, indexContacts } =
  new Function(src + 'return { companyKey, indexContacts };')();

let failures = [];
const check = (name, cond) => {
  console.log((cond ? '  pass  ' : '  FAIL  ') + name);
  if (!cond) failures.push(name);
};
const same = (a, b) => check(`"${a}" matches "${b}"`, companyKey(a) === companyKey(b));
const differ = (a, b) => check(`"${a}" does not match "${b}"`, companyKey(a) !== companyKey(b));

same('Salesforce', 'salesforce');
same('Salesforce', 'Salesforce Ireland Ltd');
same('Salesforce', 'Salesforce, Inc.');
same('Personio', 'Personio GmbH');
same('Backbase', 'Backbase B.V.');
same('Wolters Kluwer', 'Wolters Kluwer N.V.');
same('HubSpot', 'HubSpot (Dublin)');
same('Palo Alto Networks', 'Palo Alto Networks UK');
same('Miro', 'Miró');
differ('Salesforce', 'Salesforce Partner Canada');
differ('Miro', 'Mirova');
differ('Stripe', 'Stripes Group');
check('a name that is all noise matches nothing', companyKey('The Group Ltd') === '');

const idx = indexContacts([
  { name: 'A', company: 'Salesforce', chat: '10/14/25' },
  { name: 'B', company: 'Salesforce Ireland', chat: '12/5/25' },
  { name: 'C', company: '', chat: '1/1/26' },
]);
check('two contacts at one company share an entry', (idx.get('salesforce') || []).length === 2);
check('a row with no company is dropped', idx.size === 1);
check('a missing list is an empty index', indexContacts(undefined).size === 0);

if (failures.length) { console.log(`\n${failures.length} failed`); process.exit(1); }
console.log('\nall passed');
