// Uçtan uca satın alma akışı testi (EXTRA=1 ile çalışan emülatöre karşı).
import { chromium, devices } from 'playwright';
const base = process.env.BASE || 'http://localhost:9292';
const SHOTS = process.env.SHOTS || '';
const browser = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {});
const results = []; const problems = [];
const ok = (name, cond, extra = '') => results.push(`${cond ? 'PASS' : 'FAIL'} ${name} ${extra}`);
for (const [vp, opts] of Object.entries({ desktop: { viewport: { width: 1440, height: 900 } }, mobile: devices['iPhone 13'] })) {
  await fetch(base + '/__reset');
  const ctx = await browser.newContext(opts);
  const page = await ctx.newPage();
  page.on('pageerror', (e) => problems.push(`[${vp}] pageerror ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error' && !m.text().includes('422')) problems.push(`[${vp}] console ${m.text()}`); });
  const T = (n) => `[${vp}] ${n}`;
  const count = () => page.locator('[data-cart-count]').first().textContent();

  // 1. home quick add from card
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  await page.evaluate(() => document.querySelector('.rail').scrollIntoView());
  await page.waitForTimeout(800);
  const card = page.locator('.rail .card').first();
  await card.hover();
  await card.locator('button.card__quick').click();
  await page.waitForSelector('#CartDrawer.is-open', { timeout: 5000 }).catch(() => {});
  ok(T('quick add opens drawer'), await page.locator('#CartDrawer.is-open').count() === 1);
  ok(T('cart count 1'), (await count()).trim() === '1', await count());
  ok(T('drawer shows line'), await page.locator('#CartDrawer .line').count() === 1);
  // 2. qty plus in drawer
  await page.locator('#CartDrawer .line [data-line-change]').nth(1).click();
  await page.waitForTimeout(700);
  ok(T('drawer qty 2'), (await page.locator('#CartDrawer [data-line-input]').inputValue()) === '2');
  ok(T('count 2'), (await count()).trim() === '2');
  await page.locator('#CartDrawer .line [data-line-change]').first().click();
  await page.waitForTimeout(700);
  ok(T('drawer qty back 1'), (await page.locator('#CartDrawer [data-line-input]').inputValue()) === '1');
  // close via Escape
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  ok(T('drawer closes with Esc'), await page.locator('#CartDrawer.is-open').count() === 0);

  // 3. variant product
  await page.goto(base + '/products/test-omuz-cantasi', { waitUntil: 'networkidle' });
  ok(T('variant pickers rendered'), await page.locator('.variants__group').count() === 2);
  const lowText = await page.locator('[data-stock]').textContent();
  ok(T('low stock real qty'), /son 2/i.test(lowText), lowText.trim());
  ok(T('sale badge real'), await page.locator('.product__price-row .price__badge:not([hidden])').count() === 1);
  await page.locator('label[title="Siyah"]').click();
  await page.waitForTimeout(300);
  ok(T('variant change updates url'), page.url().includes('variant='), page.url());
  ok(T('stock updates to in stock'), /Stokta$/.test((await page.locator('[data-stock]').textContent()).trim()));
  await page.locator('label:has-text("Büyük")').click();
  await page.waitForTimeout(300);
  ok(T('unavailable combo disables add'), await page.locator('[data-main-add]').isDisabled());
  await page.locator('label[title="Bej"]').click();
  await page.waitForTimeout(300);
  ok(T('sold out variant disabled'), await page.locator('[data-main-add]').isDisabled(), await page.locator('[data-main-add]').textContent());
  await page.locator('label:has-text("Standart")').click();
  await page.waitForTimeout(300);
  ok(T('available variant enabled'), !(await page.locator('[data-main-add]').isDisabled()));
  await page.locator('[data-qty-plus]').click();
  await page.locator('[data-main-add]').click();
  await page.waitForSelector('#CartDrawer.is-open', { timeout: 5000 }).catch(() => {});
  ok(T('pdp add opens drawer, count 3'), (await count()).trim() === '3', await count());
  // exceeding stock error
  await page.keyboard.press('Escape');
  await page.locator('[data-qty-input]').fill('5');
  await page.locator('[data-main-add]').click();
  await page.waitForTimeout(800);
  ok(T('stock limit error shown'), await page.locator('[data-form-error]:not([hidden])').count() === 1, await page.locator('[data-form-error]').textContent());
  if (vp === 'mobile') {
    await page.locator('[data-qty-input]').fill('1');
    await page.locator('label[title="Siyah"]').click();
    await page.evaluate(() => window.scrollTo(0, 1600));
    await page.waitForTimeout(700);
    ok(T('sticky ATC visible after scroll'), await page.locator('[data-sticky-buy].is-visible').count() === 1);
    
    await page.locator('[data-sticky-add]').click();
    await page.waitForTimeout(1200);
    ok(T('sticky add works, count 4'), (await count()).trim() === '4', await count());
    await page.keyboard.press('Escape');
  }
  // gallery shot
  await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(300);

  // 4. sold out product
  await page.goto(base + '/products/top-handle-bag-fashion-leather-satchel-bag-for-ladies-zipper-pocket-metal-leather-tote-bag-for-women', { waitUntil: 'networkidle' });
  ok(T('sold out product disabled'), await page.locator('[data-main-add]').isDisabled());

  // 5. cart page
  await page.goto(base + '/cart', { waitUntil: 'networkidle' });
  const linesBefore = await page.locator('.cart-page .line').count();
  await page.locator('.cart-page .line').first().locator('.line__remove').click();
  await page.waitForTimeout(900);
  ok(T('remove line on cart page'), (await page.locator('.cart-page .line').count()) === linesBefore - 1, `${linesBefore}`);
  await page.locator('.cart-page button[name="checkout"]').click();
  await page.waitForLoadState('networkidle');
  ok(T('checkout redirect'), page.url().includes('/checkouts/'), page.url());

  // 6. menu + search
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  if (vp === 'mobile') {
    await page.locator('.header__burger').click();
    await page.waitForTimeout(700);
    ok(T('mobile menu opens'), await page.locator('#MenuDrawer.is-open').count() === 1);
    
    await page.locator('#MenuDrawer [data-drawer-close]').last().click();
    await page.waitForTimeout(500);
    ok(T('mobile menu closes'), await page.locator('#MenuDrawer.is-open').count() === 0);
  }
  await page.locator('[data-search-open]').click();
  await page.locator('#HeaderSearch').fill('çanta');
  await page.waitForTimeout(900);
  ok(T('predictive search results'), await page.locator('.predictive__item').count() > 0);
  await page.keyboard.press('Escape');

  // 7. hero detail add
  await page.evaluate(() => { const t = document.querySelector('[data-hero-track]'); window.scrollTo(0, (t.offsetHeight - innerHeight) * 0.8); });
  await page.waitForTimeout(800);
  await page.locator('.hero__detail button[data-add-button]').click();
  await page.waitForSelector('#CartDrawer.is-open', { timeout: 5000 }).catch(() => {});
  ok(T('hero add to cart'), await page.locator('#CartDrawer.is-open .line').count() >= 1);

  // 8. collection page sort
  await page.goto(base + '/collections/all', { waitUntil: 'networkidle' });
  await Promise.all([page.waitForURL(/sort_by=created-descending/, { timeout: 8000 }).catch(() => {}), page.selectOption('#SortBy', 'created-descending')]);
  ok(T('sort submit'), page.url().includes('sort_by=created-descending'));
  await page.locator('.toolbar__btn').click();
  await page.waitForTimeout(600);
  ok(T('filter drawer opens'), await page.locator('#FilterDrawer.is-open').count() === 1);
  await ctx.close();
}
await browser.close();
console.log(results.join('\n'));
console.log(problems.length ? problems.join('\n') : 'NO CONSOLE PROBLEMS');
if (problems.length || results.some((r) => r.startsWith('FAIL'))) process.exit(1);
