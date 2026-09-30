import { chromium, devices } from 'playwright';
const base = process.env.BASE || 'http://127.0.0.1:8090';
const b = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {});
const results = []; const problems = [];
const ok = (n, c, x = '') => results.push(`${c ? 'PASS' : 'FAIL'} ${n} ${x}`);
const shots = process.env.SHOTS;
for (const [vp, opts] of Object.entries({ desktop: { viewport: { width: 1440, height: 900 } }, mobile: devices['iPhone 13'] })) {
  const ctx = await b.newContext({ ...opts, locale: 'tr-TR' });
  const p = await ctx.newPage();
  p.on('pageerror', (e) => problems.push(`[${vp}] pageerror ${e.message}`));
  p.on('console', (m) => { if (m.type() === 'error' && !/422|409|403/.test(m.text())) problems.push(`[${vp}] console ${m.text()} @ ${p.url()}`); });
  const T = (n) => `[${vp}] ${n}`;
  const count = async () => (await p.locator('[data-cart-count]').first().textContent()).trim();

  // 1) Ana sayfa: hızlı sepete ekle
  await p.goto(base + '/', { waitUntil: 'networkidle' });
  ok(T('hero rendered with product'), await p.locator('.hero__img').count() === 1);
  await p.locator('.rail').first().scrollIntoViewIfNeeded(); await p.waitForTimeout(800);
  const quick = p.locator('.rail button[data-quick-add]').first();
  ok(T('rail has quick add'), await quick.count() === 1);
  await quick.scrollIntoViewIfNeeded(); await quick.hover(); await quick.click();
  await p.waitForSelector('#CartDrawer.is-open .line', { timeout: 6000 }).catch(() => {});
  ok(T('quick add opens drawer with line'), await p.locator('#CartDrawer.is-open .line').count() === 1);
  ok(T('count 1'), await count() === '1', await count());
  await p.locator('#CartDrawer [data-set-qty]').nth(1).click(); await p.waitForTimeout(700);
  ok(T('drawer qty +'), await count() === '2', await count());
  await p.locator('#CartDrawer [data-set-qty]').first().click(); await p.waitForTimeout(700);
  ok(T('drawer qty -'), await count() === '1');
  await p.keyboard.press('Escape'); await p.waitForTimeout(400);
  ok(T('drawer closes Esc'), await p.locator('#CartDrawer.is-open').count() === 0);

  // 2) Ürün sayfası
  const url = await p.evaluate(async () => (await (await fetch('/api/store/search?q=hobo')).json()).products[0].url);
  await p.goto(base + url, { waitUntil: 'networkidle' });
  ok(T('pdp title'), (await p.locator('h1.product__title').textContent()).includes('Hobo'));
  await p.locator('[data-qty-plus]').click();
  await p.locator('[data-main-add]').click();
  await p.waitForSelector('#CartDrawer.is-open .line', { timeout: 6000 }).catch(() => {});
  ok(T('pdp add qty 2 → count 3'), await count() === '3', await count());
  await p.keyboard.press('Escape');
  if (vp === 'mobile') {
    await p.evaluate(() => scrollTo(0, 1500)); await p.waitForTimeout(600);
    ok(T('sticky ATC visible'), await p.locator('[data-sticky-buy].is-visible').count() === 1);
    await p.evaluate(() => scrollTo(0, 0)); await p.waitForTimeout(600);
    ok(T('sticky ATC hidden at top'), await p.locator('[data-sticky-buy].is-visible').count() === 0);
  }
  if (shots) await p.screenshot({ path: `${shots}/${vp}_pdp.png` });

  // 3) Tükenen ürün
  const soldOut = await p.evaluate(async () => { const r = await fetch('/urunler?sirala=fiyat-artan'); return r.ok; });
  ok(T('listing ok'), soldOut);

  // 4) Sepet sayfası
  await p.goto(base + '/sepet', { waitUntil: 'networkidle' });
  ok(T('cart page 2 lines'), await p.locator('.cart-page .line').count() === 2);
  await Promise.all([p.waitForLoadState('load'), p.locator('.cart-page [data-line-set]').nth(1).click()]);
  await p.waitForTimeout(800);
  if (shots) await p.screenshot({ path: `${shots}/${vp}_cart.png`, fullPage: true });

  // 5) Ödeme: boş form hataları
  await p.goto(base + '/odeme', { waitUntil: 'networkidle' });
  await p.locator('[data-checkout-submit]').click(); await p.waitForTimeout(800);
  ok(T('checkout validation errors'), await p.locator('.field.is-invalid').count() >= 4, String(await p.locator('.field.is-invalid').count()));
  await p.fill('input[name=full_name]', 'Ayşe Yılmaz');
  await p.fill('input[name=email]', 'ayse@example.com');
  await p.fill('input[name=phone]', '0532 111 22 33');
  await p.selectOption('select[name=city]', 'İstanbul');
  await p.fill('input[name=district]', 'Kadıköy');
  await p.fill('textarea[name=address]', 'Caferağa Mah. Moda Cad. No:10 D:3');
  await p.locator('input[value=bank_transfer]').check();
  await p.locator('input[name=accept_kvkk]').check({ force: true });
  await p.locator('[data-checkout-submit]').click(); await p.waitForTimeout(800);
  ok(T('terms consent required'), await p.locator('.check.is-invalid').count() === 1);
  await p.locator('input[name=accept_terms]').check({ force: true });
  if (shots) await p.screenshot({ path: `${shots}/${vp}_checkout.png`, fullPage: true });
  await Promise.all([p.waitForURL(/\/siparis\//, { timeout: 10000 }).catch(() => {}), p.locator('[data-checkout-submit]').click()]);
  ok(T('order placed → order page'), /\/siparis\/TC\d{6}/.test(p.url()), p.url());
  ok(T('bank transfer IBAN shown'), (await p.locator('.order-page').textContent()).includes('TR33'));
  if (shots) await p.screenshot({ path: `${shots}/${vp}_order.png`, fullPage: true });
  await p.goto(base + '/', { waitUntil: 'networkidle' });
  ok(T('cart emptied after order'), await count() === '0', await count());

  // 6) Kapıda ödeme siparişi
  await p.evaluate(async (u) => {
    const pid = Number(u.match(/-p(\d+)$/)[1]);
    await fetch('/api/store/cart/add', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Storefront' }, body: JSON.stringify({ product_id: pid, quantity: 1 }) });
  }, url);
  const r = await p.evaluate(async () => (await fetch('/api/store/checkout', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Storefront' },
    body: JSON.stringify({ full_name: 'Zeynep Kaya', email: 'z@example.com', phone: '05321112233', city: 'Ankara', district: 'Çankaya', address: 'Atatürk Blv. No:1 Daire 2', payment_method: 'cash_on_delivery', accept_terms: true, accept_kvkk: true }) })).json());
  ok(T('COD order placed'), r.ok === true && r.status === 'cash_on_delivery', JSON.stringify(r).slice(0, 120));

  // 7) CSRF: özel başlıksız istek reddedilir
  const csrf = await p.evaluate(async () => (await fetch('/api/store/cart/add', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"product_id":1}' })).status);
  ok(T('mutation without header rejected'), csrf === 403, String(csrf));

  // 8) Menü + arama
  if (vp === 'mobile') {
    await p.locator('.header__burger').click(); await p.waitForTimeout(600);
    ok(T('mobile menu opens'), await p.locator('#MenuDrawer.is-open').count() === 1);
    if (shots) await p.screenshot({ path: `${shots}/${vp}_menu.png` });
    await p.locator('#MenuDrawer [data-drawer-close]').last().click(); await p.waitForTimeout(500);
    ok(T('mobile menu closes'), await p.locator('#MenuDrawer.is-open').count() === 0);
  }
  await p.locator('[data-search-open]').click();
  await p.fill('#HeaderSearch', 'çapraz');
  await p.waitForTimeout(900);
  ok(T('predictive search'), await p.locator('.predictive__item').count() > 0);
  if (shots) await p.screenshot({ path: `${shots}/${vp}_search.png` });
  await p.keyboard.press('Escape');

  // 9) Kategori + sıralama
  await p.goto(base + '/urunler', { waitUntil: 'networkidle' });
  await Promise.all([p.waitForURL(/sirala=fiyat-artan/, { timeout: 8000 }).catch(() => {}), p.selectOption('#SortBy', 'fiyat-artan')]);
  ok(T('sort'), p.url().includes('sirala=fiyat-artan'));
  if (shots) await p.screenshot({ path: `${shots}/${vp}_collection.png` });

  // 10) Müşteri hesabı: üye ol → Hesabım → adres ekle → çıkış
  await p.goto(base + '/hesap/giris', { waitUntil: 'networkidle' });
  const reg = p.locator('form[data-api-form^="/api/store/account/register"]');
  await reg.locator('[name=full_name]').fill('Deniz Aksoy');
  await reg.locator('[name=email]').fill(`deniz.${vp}.${Date.now()}@example.com`);
  await reg.locator('[name=password]').fill('kisa');
  await reg.locator('button[type=submit]').click(); await p.waitForTimeout(700);
  ok(T('register validation errors shown'), await reg.locator('.field__error').count() >= 1);
  await reg.locator('[name=password]').fill('guvenliSifre1');
  await reg.locator('[name=accept_kvkk]').check({ force: true });
  await Promise.all([p.waitForURL(/\/hesabim/, { timeout: 8000 }).catch(() => {}), reg.locator('button[type=submit]').click()]);
  ok(T('register → Hesabım'), p.url().endsWith('/hesabim') && (await p.locator('h1').innerText()).includes('Deniz'));
  const addr = p.locator('form[data-api-form="/api/store/account/addresses"]');
  await addr.locator('[name=phone]').fill('05321112233');
  await addr.locator('[name=city]').selectOption('İzmir');
  await addr.locator('[name=district]').fill('Bornova');
  await addr.locator('[name=address]').fill('Kazımdirik Mah. 372 Sok. No:4');
  await Promise.all([p.waitForLoadState('load'), addr.locator('button[type=submit]').click()]); await p.waitForTimeout(900);
  ok(T('address saved'), (await p.locator('.account-address summary').first().innerText()).includes('Adresim'));
  if (shots) await p.screenshot({ path: `${shots}/${vp}_account.png`, fullPage: true });
  await Promise.all([p.waitForURL((u) => u.pathname === '/', { timeout: 8000 }).catch(() => {}), p.locator('[data-api-action="/api/store/account/logout"]').click()]);
  const back = await p.goto(base + '/hesabim', { waitUntil: 'load' });
  ok(T('logout ends session'), p.url().includes('/hesap/giris'), String(back && back.status()));
  await ctx.close();
}
await b.close();
console.log(results.join('\n'));
console.log(problems.length ? problems.join('\n') : 'NO CONSOLE PROBLEMS');
if (problems.length || results.some((x) => x.startsWith('FAIL'))) process.exit(1);
