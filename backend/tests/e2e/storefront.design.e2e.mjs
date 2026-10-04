// Vitrin tasarımı uç durum testleri (gerçek tarayıcı). seed_dev.py ile hazırlanmış GELİŞTİRME veritabanında çalıştırın.
// Kapsam: yatay taşma (320/390/1440), kaydırma hikâyesi, hareket azaltma, mobil menü, tükenen ürün, tek görselli kart,
// uzun ürün adı, yavaş görsel yükleme (yerleşim kayması), sepetten çıkarma, sipariş takibi (Hesabım), klavye erişimi.
import { chromium, devices } from 'playwright';
const base = process.env.BASE || 'http://127.0.0.1:8090';
const b = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {});
const results = []; const problems = [];
const ok = (n, c, x = '') => results.push(`${c ? 'PASS' : 'FAIL'} ${n} ${x}`);
const json = (p, url, body) => p.evaluate(async ([u, bd]) => {
  const r = await fetch(u, bd ? { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Storefront' }, body: JSON.stringify(bd) } : {});
  return { status: r.status, data: await r.json().catch(() => ({})) };
}, [url, body]);
const overflowX = (p) => p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

const pages = ['/', '/urunler', '/kategori/omuz-cantasi', '/ara?q=canta', '/sepet', '/hesabim', '/iletisim'];

for (const [vp, opts] of Object.entries({ desktop: { viewport: { width: 1440, height: 900 } }, mobile: devices['iPhone 13'], small: { viewport: { width: 320, height: 640 }, isMobile: true, hasTouch: true } })) {
  const ctx = await b.newContext({ ...opts, locale: 'tr-TR' });
  const p = await ctx.newPage();
  p.on('pageerror', (e) => problems.push(`[${vp}] pageerror ${e.message}`));
  p.on('console', (m) => { if (m.type() === 'error' && !/404|422/.test(m.text())) problems.push(`[${vp}] console ${m.text()} @ ${p.url()}`); });
  const T = (n) => `[${vp}] ${n}`;

  // Yatay taşma: hiçbir sayfada olmamalı
  await p.goto(base + '/', { waitUntil: 'domcontentloaded' });
  const longUrl = (await json(p, base + '/api/store/search?q=ekstra uzun')).data.products[0].url;
  const outUrl = (await json(p, base + '/api/store/search?q=tukenmis test')).data.products[0].url;
  for (const path of [...pages, longUrl, outUrl]) {
    await p.goto(base + path, { waitUntil: 'networkidle' });
    const ox = await overflowX(p);
    ok(T(`no horizontal overflow ${path.slice(0, 40)}`), ox <= 0, String(ox));
  }

  // Uzun ürün adı: kartta 2 satıra kırpılır, ürün sayfasında taşmaz
  await p.goto(base + '/kategori/omuz-cantasi', { waitUntil: 'networkidle' });
  const clamp = await p.evaluate(() => {
    const t = [...document.querySelectorAll('.card__title')].find((x) => x.textContent.includes('Ekstra Uzun'));
    const lh = parseFloat(getComputedStyle(t).lineHeight);
    return t.getBoundingClientRect().height <= lh * 2 + 2;
  });
  ok(T('long title clamped to 2 lines on card'), clamp);
  // Tek görselli kart: ikinci görsel yok, kart yine tam boy
  const single = await p.evaluate(() => {
    const card = [...document.querySelectorAll('[data-product-card]')].find((x) => x.textContent.includes('Ekstra Uzun'));
    const m = card.querySelector('.card__media').getBoundingClientRect();
    return { alt: !!card.querySelector('.card__img--secondary'), ratio: m.height / m.width };
  });
  ok(T('single-image card has no hover image and keeps 4:5'), !single.alt && Math.abs(single.ratio - 1.25) < 0.02, JSON.stringify(single));

  // Tükenen ürün: sepete ekle kapalı, "Tükendi" görünür, kartta hızlı ekleme yok
  await p.goto(base + outUrl, { waitUntil: 'networkidle' });
  ok(T('sold-out PDP button disabled'), await p.locator('[data-main-add][disabled]').count() === 1);
  ok(T('sold-out PDP stock label'), (await p.locator('[data-stock]').textContent()).includes('stokta yok'));
  await p.goto(base + '/ara?q=tukenmis', { waitUntil: 'networkidle' });
  ok(T('sold-out card flagged, no quick add'), await p.locator('.card--soldout .card__flag').count() === 1 && await p.locator('.card--soldout [data-quick-add]').count() === 0);

  // Yavaş görsel: yer tutucu en-boy oranı korunur (CLS yok)
  await p.route('**/img/**', async (route) => { await new Promise((r) => setTimeout(r, 1500)); route.continue(); });
  await p.goto(base + '/urunler', { waitUntil: 'domcontentloaded' });
  const before = await p.evaluate(() => document.querySelector('.grid .card__media').getBoundingClientRect().height);
  await p.waitForLoadState('networkidle');
  const after = await p.evaluate(() => document.querySelector('.grid .card__media').getBoundingClientRect().height);
  ok(T('slow images: no layout shift'), before > 100 && Math.abs(before - after) < 1, `${before}/${after}`);
  await p.unroute('**/img/**');

  // Ana sayfa: hikâye bölümü kaydırmayla bölüm değiştirir; header kayınca zemin alır
  await p.goto(base + '/', { waitUntil: 'networkidle' });
  ok(T('header transparent at top'), await p.locator('.header.is-scrolled').count() === 0);
  const track = await p.evaluate(() => { const r = document.querySelector('[data-story-track]').getBoundingClientRect(); return { top: r.top + scrollY, h: r.height }; });
  const vh = await p.evaluate(() => innerHeight);
  const chapterAt = async (f) => {
    await p.evaluate((y) => scrollTo(0, y), track.top + (track.h - vh) * f);
    await p.waitForTimeout(500);
    return p.evaluate(() => document.querySelector('.story__chapter.is-active').dataset.storyChapter);
  };
  ok(T('header gets background after scroll'), (await chapterAt(0.1)) === '0' && await p.locator('.header.is-scrolled').count() === 1);
  ok(T('story chapter 2 mid-scroll'), (await chapterAt(0.5)) === '1');
  ok(T('story chapter 3 shows product CTA'), (await chapterAt(0.9)) === '2' && await p.locator('.story__chapter.is-active a[href^="/urun/"]').count() >= 1);

  // Sepet: ekle, adet artır, kaldır (yan sepette)
  const pid = Number(longUrl.match(/-p(\d+)$/)[1]);
  await json(p, base + '/api/store/cart/add', { product_id: pid, quantity: 1 });
  await p.goto(base + '/urunler', { waitUntil: 'networkidle' });
  await p.locator('[data-cart-open]').click();
  await p.waitForSelector('#CartDrawer.is-open .line'); await p.waitForTimeout(700);
  await p.locator('#CartDrawer .line__remove').first().click();
  await p.waitForSelector('#CartDrawer .cart-empty', { timeout: 5000 }).catch(() => {});
  ok(T('remove from drawer empties cart'), await p.locator('#CartDrawer .cart-empty').count() === 1);
  await p.keyboard.press('Escape');

  // Mobil menü: kategoriler ve Hesabım bağlantısı
  if (vp !== 'desktop') {
    await p.locator('.header__burger').click(); await p.waitForTimeout(700);
    ok(T('menu lists real categories'), await p.locator('#MenuDrawer .mobile-nav__sub a').count() >= 2);
    ok(T('menu has account link'), await p.locator('#MenuDrawer a[href="/hesabim"]').count() === 1);
    const menuOx = await overflowX(p);
    ok(T('menu no overflow'), menuOx <= 0, String(menuOx));
    await p.keyboard.press('Escape');
  }

  // Sipariş takibi: doğru e-posta ile sonuç, yanlışla genel hata
  await json(p, base + '/api/store/cart/add', { product_id: pid, quantity: 1 });
  const order = (await json(p, base + '/api/store/checkout', { full_name: 'Elif Demir', email: `elif.${vp}@example.com`, phone: '05321112233', city: 'İzmir', district: 'Konak', address: 'Alsancak Mah. Kıbrıs Şehitleri Cad. No:5', payment_method: 'cash_on_delivery', accept_terms: true, accept_kvkk: true })).data;
  await p.goto(base + '/hesabim', { waitUntil: 'networkidle' });
  await p.fill('input[name=code]', order.public_code.toLowerCase());
  await p.fill('input[name=email]', `ELIF.${vp}@example.com`);
  await p.locator('[data-lookup-submit]').click();
  await p.waitForSelector('.lookup-card', { timeout: 5000 }).catch(() => {});
  const card = await p.locator('.lookup-card').textContent().catch(() => '');
  ok(T('order lookup shows order'), card.includes(order.public_code) && card.includes('Ekstra Uzun'));
  ok(T('order lookup hides address/phone'), !card.includes('Alsancak') && !card.includes('0532'));
  await p.fill('input[name=email]', 'baskasi@example.com');
  await p.locator('[data-lookup-submit]').click(); await p.waitForTimeout(700);
  ok(T('order lookup wrong email refused'), await p.locator('[data-lookup-error]:not([hidden])').count() === 1 && await p.locator('.lookup-card').count() === 0);
  await ctx.close();
}

// Hareket azaltma: hikâye sabit sahne yerine sade dizilim; belirme animasyonu yok
{
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  const p = await ctx.newPage();
  await p.goto(base + '/', { waitUntil: 'networkidle' });
  ok('[reduced] story static', await p.locator('.story.story--static').count() === 1);
  ok('[reduced] reveals visible', await p.evaluate(() => [...document.querySelectorAll('[data-reveal]')].every((e) => e.classList.contains('is-in'))));
  await ctx.close();
}
// Klavye: hikâyedeki ürün bağlantısına sekmeyle ulaşılır ve odaklanınca görünür olur
{
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } });
  const p = await ctx.newPage();
  await p.goto(base + '/', { waitUntil: 'networkidle' });
  let reached = false;
  for (let i = 0; i < 60 && !reached; i++) {
    await p.keyboard.press('Tab');
    reached = await p.evaluate(() => !!document.activeElement.closest('.story__chapter--product'));
  }
  await p.waitForTimeout(900);
  const visible = reached && await p.evaluate(() => getComputedStyle(document.activeElement.closest('.story__chapter')).opacity === '1');
  ok('[keyboard] story product CTA reachable and visible on focus', visible);
  await ctx.close();
}

await b.close();
console.log(results.join('\n'));
console.log(problems.length ? problems.join('\n') : 'NO CONSOLE PROBLEMS');
if (problems.length || results.some((x) => x.startsWith('FAIL'))) process.exit(1);
