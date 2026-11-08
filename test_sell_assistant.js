// Sell Assistant pricing contract: tiers come from dealer asking prices,
// never fall below verified metal value, and the auction reserve/start use the
// LOW-confidence ratios of auction_sell_engine.py.
const fs = require('fs');
const assert = require('assert');
const html = fs.readFileSync(__dirname + '/index.html', 'utf8');
const grab = name => {
  const m = html.match(new RegExp(`function ${name}\\([^]*?\\n}`)) || html.match(new RegExp(`function ${name}\\(.*\\}`));
  assert(m, `missing ${name}`);
  return m[0];
};
const consts = html.match(/const SELL_RESERVE_FRACTION_LOW=[^;]+;/)[0];
const src = [consts, ...['median', 'quantile', 'robustDealerSample', 'round2', 'sellPriceTiers', 'sellListingText'].map(grab)].join('\n');
const api = new Function(src + '\nreturn {sellPriceTiers, sellListingText};')();

// Ordinary coin: tiers relative to dealer asks.
let t = api.sellPriceTiers([12, 10.9, 15, 11.5], null);
assert.strictEqual(t.lowest, 10.9);
assert.strictEqual(t.quick, 9.27);
assert.strictEqual(t.competitive, 10.36);
assert.strictEqual(t.patient, 11.75);
assert.ok(t.start < t.reserve && t.reserve < t.quick);

// Silver coin whose dealer asks are below melt: every tier is floored.
t = api.sellPriceTiers([18, 19, 20], 25);
assert.deepStrictEqual([t.quick, t.competitive, t.patient], [25, 25, 25]);
assert.deepStrictEqual(t.floored.sort(), ['competitive', 'patient', 'quick']);
assert.ok(t.reserve >= 25);

// No listings: only the metal floor, or nothing at all.
assert.strictEqual(api.sellPriceTiers([], null), null);
assert.strictEqual(api.sellPriceTiers([], 30).floorOnly, true);

// Listing text keeps titles within eBay's 80-character limit and omits empty fields.
const txt = api.sellListingText(
  {country: 'Ελλάδα', denomination: '2 ευρώ', year: '2022', variant: 'Πρόγραμμα Εράσμους'.repeat(5), grade: 'UNC'},
  {countryEN: 'Greece', denomEN: '2 euro', specTitle: '', spec: null});
assert.ok(txt.titleEN.length <= 80 && txt.titleEL.length <= 80);
assert.ok(!/Weight:|Βάρος:/.test(txt.descEN + txt.descEL));

console.log('PASS: sell assistant pricing tiers, metal floor, auction reserve, listing text');
