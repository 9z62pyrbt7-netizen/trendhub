/* Trendçantanız web mağazası — davranışlar (bağımlılıksız, defer ile yüklenir) */
(() => {
  'use strict';

  document.documentElement.classList.add('js');
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const motionOK = () => !reduceMotion.matches;
  const isDesktop = window.matchMedia('(min-width: 990px)');
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const api = async (url, body) => {
    const opts = { credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers['Content-Type'] = 'application/json';
      opts.headers['X-Requested-With'] = 'Storefront';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.detail || 'Bir sorun oluştu. Lütfen tekrar deneyin.');
      err.data = data;
      err.status = res.status;
      throw err;
    }
    return data;
  };

  let toastTimer;
  const toast = (message, ok = true) => {
    const el = $('[data-toast]');
    if (!el) return;
    el.innerHTML = (ok ? '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>' : '') + '<span></span>';
    el.lastChild.textContent = message;
    el.hidden = false;
    requestAnimationFrame(() => el.classList.add('is-visible'));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.classList.remove('is-visible'); setTimeout(() => { el.hidden = true; }, 400); }, 2800);
  };

  /* ---------- Görsel fonu tespiti ----------
     Varsayılan: beyaz fonlu tedarikçi fotoğrafı (contain + multiply ile zemine karışır, kutu görünmez).
     Köşeleri beyaz olmayan fotoğraf (lifestyle) ise çerçeveyi kaplar. */
  const detectCutout = (img) => {
    const apply = () => {
      try {
        const c = document.createElement('canvas');
        c.width = 24; c.height = 24;
        const ctx = c.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, 24, 24);
        const pts = [[1, 1], [22, 1], [1, 22], [22, 22], [12, 1], [1, 12], [22, 12]];
        let white = 0;
        for (const [x, y] of pts) {
          const [r, g, b] = ctx.getImageData(x, y, 1, 1).data;
          if (r > 236 && g > 236 && b > 236 && Math.max(r, g, b) - Math.min(r, g, b) < 14) white += 1;
        }
        if (white < 5) img.classList.add('is-photo');
      } catch (e) { /* farklı origin: varsayılan görünüm kalır */ }
    };
    if (img.complete && img.naturalWidth) apply();
    else img.addEventListener('load', apply, { once: true });
  };
  const scanCutouts = (root = document) => $$('img[data-cutout]', root).forEach((img) => { img.removeAttribute('data-cutout'); detectCutout(img); });
  scanCutouts();

  /* ---------- Header ---------- */
  const header = $('[data-header]');
  if (header) {
    let lastY = window.scrollY;
    let ticking = false;
    const update = () => {
      const y = window.scrollY;
      header.classList.toggle('is-scrolled', y > 8);
      const openUI = header.classList.contains('is-open') || document.body.classList.contains('is-locked');
      if (!openUI && y > 480 && y > lastY + 4) header.classList.add('is-hidden');
      else if (y < lastY - 4 || y <= 480) header.classList.remove('is-hidden');
      document.documentElement.classList.toggle('header-hidden', header.classList.contains('is-hidden'));
      lastY = y;
      ticking = false;
    };
    window.addEventListener('scroll', () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
    update();
  }

  /* ---------- Çekmeceler ---------- */
  const drawers = {};
  let lastFocus = null;
  const focusables = (el) => $$('a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"])', el)
    .filter((n) => n.offsetParent !== null);
  const openDrawer = (name) => {
    const drawer = $(`[data-drawer="${name}"]`);
    if (!drawer) return;
    Object.keys(drawers).forEach((k) => k !== name && closeDrawer(k, false));
    lastFocus = document.activeElement;
    drawer.classList.add('is-open');
    drawer.setAttribute('aria-hidden', 'false');
    document.body.classList.add('is-locked');
    $$(`[data-drawer-open="${name}"]`).forEach((b) => b.setAttribute('aria-expanded', 'true'));
    drawers[name] = drawer;
    const panel = $('.drawer__panel', drawer);
    setTimeout(() => (focusables(panel)[0] || panel).focus({ preventScroll: true }), 60);
  };
  function closeDrawer(name, restore = true) {
    const drawer = drawers[name] || $(`[data-drawer="${name}"]`);
    if (!drawer || !drawer.classList.contains('is-open')) return;
    drawer.classList.remove('is-open');
    drawer.setAttribute('aria-hidden', 'true');
    delete drawers[name];
    $$(`[data-drawer-open="${name}"]`).forEach((b) => b.setAttribute('aria-expanded', 'false'));
    if (!Object.keys(drawers).length) document.body.classList.remove('is-locked');
    if (restore && lastFocus) lastFocus.focus({ preventScroll: true });
  }

  document.addEventListener('click', (e) => {
    const opener = e.target.closest('[data-drawer-open]');
    if (opener) { e.preventDefault(); openDrawer(opener.dataset.drawerOpen); return; }
    const closer = e.target.closest('[data-drawer-close]');
    if (closer) { const d = closer.closest('[data-drawer]'); if (d) { e.preventDefault(); closeDrawer(d.dataset.drawer); } return; }
    const cartLink = e.target.closest('[data-cart-open]');
    if (cartLink && !document.body.classList.contains('page-cart') && !document.body.classList.contains('page-checkout')) {
      e.preventDefault();
      openDrawer('cart');
      loadCart();
    }
  });
  document.addEventListener('keydown', (e) => {
    const names = Object.keys(drawers);
    if (e.key === 'Escape') { if (names.length) closeDrawer(names[names.length - 1]); closeSearch(); closeZoom(); return; }
    if (e.key === 'Tab' && names.length) {
      const items = focusables($('.drawer__panel', drawers[names[names.length - 1]]));
      if (!items.length) return;
      const first = items[0]; const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  /* ---------- Arama ---------- */
  const searchPanel = $('[data-search-panel]');
  const searchOpeners = $$('[data-search-open]');
  function closeSearch() {
    if (!searchPanel || searchPanel.hidden) return;
    searchPanel.hidden = true;
    header && header.classList.remove('is-open');
    searchOpeners.forEach((b) => b.setAttribute('aria-expanded', 'false'));
  }
  if (searchPanel) {
    const input = $('[data-predictive-input]', searchPanel);
    const results = $('[data-predictive-results]', searchPanel);
    searchOpeners.forEach((btn) => btn.addEventListener('click', () => {
      if (!searchPanel.hidden) { closeSearch(); return; }
      searchPanel.hidden = false;
      header.classList.add('is-open');
      btn.setAttribute('aria-expanded', 'true');
      setTimeout(() => input && input.focus(), 30);
    }));
    $$('[data-search-close]', searchPanel).forEach((b) => b.addEventListener('click', closeSearch));
    document.addEventListener('click', (e) => {
      if (!searchPanel.hidden && !e.target.closest('[data-search-panel]') && !e.target.closest('[data-search-open]')) closeSearch();
    });
    let timer; let seq = 0;
    input && input.addEventListener('input', () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) { results.innerHTML = ''; return; }
      timer = setTimeout(async () => {
        const my = ++seq;
        try {
          const d = await api(`/api/store/search?q=${encodeURIComponent(q)}`);
          if (my !== seq) return;
          let h = '<div class="predictive__inner">';
          if (d.products.length) {
            h += '<p class="predictive__heading">Ürünler</p><ul class="predictive__list" role="list">';
            h += d.products.map((p) => `<li><a class="predictive__item" href="${esc(p.url)}">${p.image ? `<img src="${esc(p.image)}" alt="" width="56" height="56" loading="lazy">` : ''}<span class="predictive__text"><span class="predictive__title">${esc(p.title)}</span><span class="predictive__price">${esc(p.price)}${p.in_stock ? '' : ' · Tükendi'}</span></span></a></li>`).join('');
            h += '</ul>';
          }
          if (d.categories.length) {
            h += '<p class="predictive__heading">Kategoriler</p><ul class="predictive__links" role="list">' + d.categories.map((c) => `<li><a href="${esc(c.url)}">${esc(c.label)}</a></li>`).join('') + '</ul>';
          }
          if (!d.products.length && !d.categories.length) h += '<p class="predictive__empty">Sonuç bulunamadı.</p>';
          h += `<a class="link-arrow predictive__all" href="/ara?q=${encodeURIComponent(q)}">“${esc(q)}” için tüm sonuçlar</a></div>`;
          results.innerHTML = h;
        } catch (e) { /* sessiz */ }
      }, 200);
    });
  }

  /* ---------- Sepet ---------- */
  const updateCount = (count) => {
    $$('[data-cart-count]').forEach((el) => {
      el.textContent = count;
      el.classList.toggle('is-empty', count === 0);
      el.classList.remove('is-bump'); void el.offsetWidth; el.classList.add('is-bump');
    });
    $$('[data-cart-count-label]').forEach((el) => { el.textContent = `${count} ürün`; });
    $$('[data-cart-drawer-count]').forEach((el) => { el.textContent = `(${count})`; });
  };

  const renderDrawer = (d) => {
    const body = $('[data-cart-drawer-body]');
    const foot = $('[data-cart-drawer-foot]');
    if (!body) return;
    updateCount(d.count);
    const hint = $('[data-cart-shipping-hint]');
    if (hint) {
      hint.hidden = !(d.lines.length && d.free_shipping_remaining_fmt);
      if (!hint.hidden) hint.textContent = `Ücretsiz kargo için ${d.free_shipping_remaining_fmt} daha ekleyin.`;
    }
    if (!d.lines.length) {
      body.innerHTML = $('#CartEmptyTpl').innerHTML;
      body.classList.add('cart-empty-wrap');
      foot.hidden = true;
      return;
    }
    body.classList.remove('cart-empty-wrap');
    body.innerHTML = '<ul class="lines" role="list">' + d.lines.map((l) => `
      <li class="line${l.issue ? ' line--issue' : ''}" data-drawer-line="${l.product_id}">
        <a class="line__media" href="${esc(l.url)}" tabindex="-1" aria-hidden="true">${l.image ? `<img src="${esc(l.image)}" alt="" width="120" height="150" loading="lazy" data-cutout>` : ''}</a>
        <div class="line__info">
          <a class="line__title" href="${esc(l.url)}">${esc(l.title)}</a>
          ${l.color || l.size ? `<p class="line__variant">${esc([l.color, l.size].filter(Boolean).join(' · '))}</p>` : ''}
          <div class="line__price"><span>${esc(l.price_fmt)}</span></div>
          ${l.issue ? `<p class="line__issue">${esc(l.issue)}</p>` : ''}
          <div class="line__controls">
            <div class="qty qty--sm">
              <button type="button" class="qty__btn" data-set-qty="${l.quantity - 1}" aria-label="Adedi azalt: ${esc(l.title)}"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M5 12h14"/></svg></button>
              <span class="qty__input" aria-live="polite">${l.quantity}</span>
              <button type="button" class="qty__btn" data-set-qty="${l.quantity + 1}" aria-label="Adedi artır: ${esc(l.title)}" ${l.quantity >= l.available ? 'disabled' : ''}><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button>
            </div>
            <button type="button" class="line__remove" data-set-qty="0" aria-label="Sepetten kaldır: ${esc(l.title)}">Kaldır</button>
          </div>
        </div>
        <div class="line__total">${esc(l.total_fmt)}</div>
      </li>`).join('') + '</ul>';
    scanCutouts(body);
    foot.hidden = false;
    $('[data-cart-items-total]', foot).textContent = d.items_total_fmt;
    $('[data-cart-shipping]', foot).textContent = Number(d.shipping) > 0 ? d.shipping_fmt : 'Ücretsiz';
    $('[data-cart-total]', foot).textContent = d.total_fmt;
  };

  let cartLoaded = false;
  async function loadCart() {
    if (cartLoaded) return;
    try { renderDrawer(await api('/api/store/cart')); cartLoaded = true; } catch (e) { toast(e.message, false); }
  }

  const setBusy = (btn, busy) => {
    if (!btn) return;
    btn.classList.toggle('is-loading', busy);
    btn.setAttribute('aria-busy', busy ? 'true' : 'false');
  };
  const flashAdded = (btn) => {
    if (!btn) return;
    btn.classList.add('is-added');
    const label = $('[data-add-label]', btn);
    const prev = label && label.textContent;
    if (label) label.textContent = 'Sepete eklendi';
    setTimeout(() => { btn.classList.remove('is-added'); if (label) label.textContent = prev; }, 1600);
  };

  const addToCart = async (productId, quantity, btn) => {
    setBusy(btn, true);
    try {
      const d = await api('/api/store/cart/add', { product_id: Number(productId), quantity: Number(quantity) || 1 });
      cartLoaded = true;
      renderDrawer(d);
      flashAdded(btn);
      if (d.notice) toast(d.notice, false);
      openDrawer('cart');
      return true;
    } catch (e) {
      toast(e.message, false);
      return false;
    } finally { setBusy(btn, false); }
  };

  document.addEventListener('click', async (e) => {
    const quick = e.target.closest('[data-quick-add]');
    if (quick) { e.preventDefault(); addToCart(quick.dataset.quickAdd, 1, quick); return; }
    const setQty = e.target.closest('[data-set-qty]');
    if (setQty) {
      const line = setQty.closest('[data-drawer-line]');
      line.classList.add('is-updating');
      try { renderDrawer(await api('/api/store/cart/update', { product_id: Number(line.dataset.drawerLine), quantity: Math.max(0, Number(setQty.dataset.setQty)) })); }
      catch (err) { line.classList.remove('is-updating'); toast(err.message, false); }
      return;
    }
    const lineSet = e.target.closest('[data-line-set]');
    if (lineSet) {
      const line = lineSet.closest('[data-line]');
      line.classList.add('is-updating');
      try {
        await api('/api/store/cart/update', { product_id: Number(line.dataset.productId), quantity: Math.max(0, Number(lineSet.dataset.lineSet)) });
        window.location.reload();
      } catch (err) { line.classList.remove('is-updating'); toast(err.message, false); }
    }
  });

  /* ---------- Kartta renk önizleme ---------- */
  if (window.matchMedia('(hover: hover)').matches) {
    document.addEventListener('mouseover', (e) => {
      const sw = e.target.closest('[data-card-swatch]');
      if (!sw || !sw.dataset.image) return;
      const card = sw.closest('[data-product-card]');
      const img = $('.card__img--primary', card);
      if (img && img.dataset.current !== sw.dataset.image) {
        img.srcset = ''; img.src = sw.dataset.image; img.dataset.current = sw.dataset.image;
        $$('[data-card-swatch]', card).forEach((s) => s.classList.toggle('is-active', s === sw));
      }
    });
  }

  /* ---------- Ürün sayfası ---------- */
  const product = $('[data-product]');
  if (product) {
    const buyForm = $('[data-buy-form]', product);
    const qty = $('[data-qty-input]', product);
    const mainAdd = $('[data-main-add]', product);
    const max = () => Number(qty.max) || 10;
    $$('[data-qty-minus], [data-qty-plus]', product).forEach((b) => b.addEventListener('click', () => {
      qty.value = Math.min(max(), Math.max(1, (parseInt(qty.value, 10) || 1) + (b.hasAttribute('data-qty-plus') ? 1 : -1)));
    }));
    buyForm && buyForm.addEventListener('submit', (e) => {
      e.preventDefault();
      addToCart(product.dataset.productId, Math.min(max(), Math.max(1, parseInt(qty.value, 10) || 1)), mainAdd);
    });
    const stickyBtn = $('[data-sticky-add]', product);
    stickyBtn && stickyBtn.addEventListener('click', () => addToCart(product.dataset.productId, 1, stickyBtn));

    const track = $('[data-gallery-track]', product);
    if (track) {
      const indexEl = $('[data-gallery-index]', product);
      const dots = $$('.gallery__dot', product);
      let raf;
      track.addEventListener('scroll', () => {
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => {
          const i = Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
          if (indexEl) indexEl.textContent = i + 1;
          dots.forEach((d, n) => d.classList.toggle('is-active', n === i));
        });
      }, { passive: true });
    }
    const zoom = $('[data-zoom]', product);
    if (zoom) {
      const zt = $('[data-zoom-track]', zoom);
      $$('[data-zoom-open]', product).forEach((btn) => btn.addEventListener('click', () => {
        $$('img[data-src]', zt).forEach((img) => { img.src = img.dataset.src; img.removeAttribute('data-src'); });
        zoom.hidden = false;
        document.body.classList.add('is-locked');
        const target = $$('li', zt)[Number(btn.dataset.zoomOpen)];
        requestAnimationFrame(() => { if (target) zt.scrollLeft = target.offsetLeft; });
        $('[data-zoom-close]', zoom).focus();
      }));
      $('[data-zoom-close]', zoom).addEventListener('click', closeZoom);
    }
    const sticky = $('[data-sticky-buy]', product);
    if (sticky && mainAdd && 'IntersectionObserver' in window) {
      const footer = $('.footer');
      let mainVisible = true; let footerVisible = false;
      const sync = () => {
        const show = !mainVisible && !footerVisible && mainAdd.getBoundingClientRect().top < 0;
        sticky.classList.toggle('is-visible', show);
        sticky.setAttribute('aria-hidden', show ? 'false' : 'true');
        if (stickyBtn) stickyBtn.tabIndex = show ? 0 : -1;
      };
      new IntersectionObserver(([en]) => { mainVisible = en.isIntersecting; sync(); }).observe(mainAdd);
      if (footer) new IntersectionObserver(([en]) => { footerVisible = en.isIntersecting; sync(); }).observe(footer);
      let r = null;
      window.addEventListener('scroll', () => { if (!r) r = requestAnimationFrame(() => { r = null; sync(); }); }, { passive: true });
    }
  }
  function closeZoom() {
    const zoom = $('[data-zoom]:not([hidden])');
    if (!zoom) return;
    zoom.hidden = true;
    if (!Object.keys(drawers).length) document.body.classList.remove('is-locked');
  }

  /* ---------- Ödeme formu ---------- */
  const form = $('[data-checkout-form]');
  if (form) {
    const corp = $('[data-corporate]', form);
    form.addEventListener('change', (e) => {
      if (e.target.name === 'billing_type') corp.hidden = e.target.value !== 'corporate';
      const f = e.target.closest('.field, .check');
      if (f) { f.classList.remove('is-invalid'); const m = $('.field__error', f); if (m) m.remove(); }
    });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('[data-checkout-submit]', form);
      const errBox = $('[data-checkout-error]', form);
      errBox.hidden = true;
      $$('.is-invalid', form).forEach((n) => n.classList.remove('is-invalid'));
      $$('.field__error', form).forEach((n) => n.remove());
      const fd = new FormData(form);
      const body = {};
      for (const [k, v] of fd.entries()) body[k] = typeof v === 'string' ? v.trim() : v;
      body.accept_terms = fd.get('accept_terms') === '1';
      body.accept_kvkk = fd.get('accept_kvkk') === '1';
      ['postal_code', 'company', 'tax_office', 'tax_number', 'note'].forEach((k) => { if (!body[k]) body[k] = null; });
      setBusy(btn, true);
      btn.disabled = true;
      try {
        const d = await api('/api/store/checkout', body);
        window.location.href = d.redirect;
      } catch (err) {
        btn.disabled = false;
        setBusy(btn, false);
        errBox.textContent = err.message;
        errBox.hidden = false;
        const fields = (err.data && err.data.fields) || {};
        let firstBad = null;
        Object.entries(fields).forEach(([name, msg]) => {
          const input = form.elements[name];
          const el = input && (input.length && !input.tagName ? input[0] : input);
          const wrap = el && el.closest('.field, .check');
          if (wrap) {
            wrap.classList.add('is-invalid');
            const m = document.createElement('span'); m.className = 'field__error'; m.textContent = msg;
            wrap.appendChild(m);
            firstBad = firstBad || el;
          }
        });
        (firstBad || errBox).scrollIntoView({ behavior: motionOK() ? 'smooth' : 'auto', block: 'center' });
        if (firstBad && firstBad.focus) firstBad.focus({ preventScroll: true });
      }
    });
  }

  /* ---------- Sıralama ---------- */
  $$('[data-sort]').forEach((s) => s.addEventListener('change', () => s.form.submit()));

  /* ---------- Ürün şeridi okları ---------- */
  $$('[data-rail]').forEach((rail) => {
    const vp = $('[data-rail-viewport]', rail);
    const arrows = $('[data-rail-arrows]', rail);
    const prev = $('[data-rail-prev]', rail);
    const next = $('[data-rail-next]', rail);
    if (!vp || !arrows) return;
    const sync = () => {
      arrows.hidden = !(vp.scrollWidth > vp.clientWidth + 4) || !isDesktop.matches;
      prev.disabled = vp.scrollLeft <= 4;
      next.disabled = vp.scrollLeft + vp.clientWidth >= vp.scrollWidth - 4;
    };
    const step = () => { const it = $('.rail__item', vp); return it ? it.getBoundingClientRect().width + 24 : vp.clientWidth * 0.8; };
    prev.addEventListener('click', () => vp.scrollBy({ left: -step(), behavior: 'smooth' }));
    next.addEventListener('click', () => vp.scrollBy({ left: step(), behavior: 'smooth' }));
    vp.addEventListener('scroll', sync, { passive: true });
    window.addEventListener('resize', sync);
    sync();
  });

  /* ---------- Sipariş takibi (Hesabım) ---------- */
  const lookup = $('[data-lookup-form]');
  if (lookup) {
    lookup.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('[data-lookup-submit]', lookup);
      const err = $('[data-lookup-error]', lookup);
      const out = $('[data-lookup-result]');
      err.hidden = true;
      const code = lookup.elements.code.value.trim().toUpperCase();
      const email = lookup.elements.email.value.trim();
      if (!code || !email) { err.textContent = 'Sipariş numarası ve e-posta gerekli.'; err.hidden = false; return; }
      setBusy(btn, true);
      try {
        const d = await api('/api/store/order-lookup', { code, email });
        out.innerHTML = d.html;
        scanCutouts(out);
        out.scrollIntoView({ behavior: motionOK() ? 'smooth' : 'auto', block: 'start' });
      } catch (ex) { out.innerHTML = ''; err.textContent = ex.message; err.hidden = false; }
      finally { setBusy(btn, false); }
    });
  }

  /* ==========================================================================
     Hareket: yalnızca transform/opacity; tek rAF döngüsü, ekranda olmayan bölüm hesaplanmaz.
     prefers-reduced-motion açıksa hiçbiri çalışmaz (içerik sade ve sabit gösterilir).
     ========================================================================== */
  const clamp01 = (v) => Math.min(1, Math.max(0, v));
  const easeOut = (t) => 1 - Math.pow(1 - t, 3);

  /* Görünür olunca belirme: ilk ekrandakiler beklemeden görünür (yanıp sönme olmaz) */
  const reveals = $$('[data-reveal]');
  if (!motionOK() || !('IntersectionObserver' in window)) {
    reveals.forEach((el) => el.classList.add('is-in'));
  } else {
    const vh = window.innerHeight;
    const io = new IntersectionObserver((entries) => entries.forEach((en) => {
      if (en.isIntersecting) { en.target.classList.add('is-in'); io.unobserve(en.target); }
    }), { rootMargin: '0px 0px -6% 0px', threshold: 0.06 });
    reveals.forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.top < vh && r.bottom > 0) { el.style.transitionDelay = '0s'; el.classList.add('is-in'); } else io.observe(el);
    });
  }

  const scenes = [];
  const addScene = (el, update) => {
    const s = { el, update, visible: true };
    scenes.push(s);
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(([en]) => { s.visible = en.isIntersecting; if (s.visible) requestTick(); }, { rootMargin: '10% 0px' }).observe(el);
    }
  };
  let ticking = false;
  const frame = () => {
    ticking = false;
    const vh = window.innerHeight;
    scenes.forEach((s) => { if (s.visible) s.update(vh); });
  };
  function requestTick() { if (!ticking) { ticking = true; requestAnimationFrame(frame); } }

  /* Hero: ürün metinden yavaş hareket eder ve hafifçe büyür (yalnızca masaüstü) */
  const hero = $('[data-hero]');
  if (hero && motionOK()) {
    const img = $('.hero__img', hero);
    const copy = $('[data-hero-copy]', hero);
    addScene(hero, (vh) => {
      if (!isDesktop.matches) { if (img) img.style.transform = ''; if (copy) copy.style.transform = copy.style.opacity = ''; return; }
      const p = clamp01(window.scrollY / vh);
      if (img) img.style.transform = `translate3d(0, ${(-p * 8).toFixed(2)}%, 0) scale(${(1 + p * 0.06).toFixed(4)})`;
      if (copy) { copy.style.transform = `translate3d(0, ${(-p * 90).toFixed(1)}px, 0)`; copy.style.opacity = (1 - p * 0.9).toFixed(3); }
    });
  }

  /* Ürün hikâyesi: sahne sabit; ilerlemeye göre ürün öne gelir, görsel ve metin bölüm bölüm değişir */
  $$('[data-story]').forEach((story) => {
    if (!motionOK()) { story.classList.add('story--static'); return; }
    const track = $('[data-story-track]', story);
    const stage = $('.story__stage', story);
    const imgs = $$('[data-story-img]', story);
    const chapters = $$('[data-story-chapter]', story);
    const steps = $$('[data-story-step]', story);
    let current = -1;
    const setChapter = (i) => {
      if (i === current) return;
      current = i;
      chapters.forEach((c, n) => c.classList.toggle('is-active', n === i));
      steps.forEach((c, n) => c.classList.toggle('is-active', n === i));
      const k = Math.min(i, imgs.length - 1);
      imgs.forEach((im, n) => im.classList.toggle('is-active', n === k));
    };
    addScene(story, (vh) => {
      const r = track.getBoundingClientRect();
      const total = r.height - vh;
      const p = total > 0 ? clamp01(-r.top / total) : 0;
      stage.style.setProperty('--p', p.toFixed(4));
      stage.style.setProperty('--e', easeOut(clamp01(p / 0.4)).toFixed(4));
      setChapter(p < 0.34 ? 0 : p < 0.67 ? 1 : 2);
    });
  });

  /* İkinci editoryal: görsel ekrana girerken yavaşça yerine oturur */
  $$('[data-parallax-scale]').forEach((el) => {
    if (!motionOK()) return;
    addScene(el, (vh) => {
      const r = el.getBoundingClientRect();
      el.style.setProperty('--s', easeOut(clamp01((vh - r.top) / (vh + r.height * 0.5))).toFixed(4));
    });
  });

  if (scenes.length) {
    window.addEventListener('scroll', requestTick, { passive: true });
    window.addEventListener('resize', requestTick);
    frame();
  }
})();
