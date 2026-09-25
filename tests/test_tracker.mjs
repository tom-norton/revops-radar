// Pins the country the dashboard writes into Networking Tracker for every market the radar
// actually produces. A market added to scan.py without a row here would otherwise land in
// the sheet as a raw code ("IE-Dublin") or a city, silently.
//
//   node tests/test_tracker.mjs
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../docs/index.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
new Function(script); // whole dashboard script parses

const src = script.match(/const TRACKER_COUNTRY = [\s\S]*?\n};\nfunction trackerCountry[\s\S]*?\n}\n/)[0];
const trackerCountry = new Function(src + 'return trackerCountry;')();

const SHEET = new Set(['Netherlands', 'Ireland', 'UK', 'Belgium', 'Canada', 'US-Remote']);
const jobs = JSON.parse(readFileSync(new URL('../docs/jobs.json', import.meta.url), 'utf8'));
const tiers = JSON.parse(readFileSync(new URL('../docs/status.json', import.meta.url), 'utf8')).market_tier || {};

let failures = [];
const check = (name, cond) => {
  console.log((cond ? '  pass  ' : '  FAIL  ') + name);
  if (!cond) failures.push(name);
};

for (const m of Object.keys(tiers))
  check(`tier ${m} -> ${trackerCountry({ tier: m })}`, SHEET.has(trackerCountry({ tier: m })));
const seen = new Set(jobs.map(j => j.tier || j.market || ''));
for (const m of seen)
  check(`jobs.json market "${m}" -> ${trackerCountry({ tier: m })}`, SHEET.has(trackerCountry({ tier: m })));
check('unknown market falls back to the last part of the location',
      trackerCountry({ market: 'de', location: 'Berlin, Germany' }) === 'Germany');

if (failures.length) { console.log(`\n${failures.length} failed`); process.exit(1); }
console.log('\nall passed');
