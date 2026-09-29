// Tüm şablonları emülatörde render eder; HTTP durumunu, Liquid hatalarını ve eksik çevirileri denetler.
const base = process.env.BASE || 'http://localhost:9292';
const P1 = '/products/top-handle-bag-leather-satchel-bags-for-women-luxury-designer-business-crossbody-work-purse-with-multiple-internal-pockets';
const urls = [['/', 200], [P1, 200], [`${P1}?variant=1`, 200], ['/collections/all', 200], ['/collections/all?sort_by=best-selling', 200],
  ['/collections/frontpage', 200], ['/collections', 200], ['/cart', 200], ['/search', 200], ['/search?q=bag', 200], ['/does-not-exist', 404],
  ['/collections/all?section_id=product-rail', 200], ['/search/suggest?q=bag&section_id=predictive-search', 200]];
let failed = 0;
await fetch(`${base}/__reset`);
for (const [u, expected] of urls) {
  const res = await fetch(base + u);
  const html = await res.text();
  const bad = res.status !== expected || /translation missing|<pre>|Liquid error/i.test(html);
  if (bad) failed++;
  console.log(`${bad ? 'FAIL' : 'ok  '} ${res.status} ${u}`);
}
const errs = await (await fetch(`${base}/__errors`)).json();
if (errs.length) { failed++; console.log('render errors:', errs); }
process.exit(failed ? 1 : 0);
