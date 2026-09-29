/* Trendçantanız — tema davranışları (bağımlılıksız, ~defer ile yüklenir) */
(() => {
  'use strict';

  const theme = window.theme || { routes: {}, strings: {} };
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const motionOK = () => theme.motion !== false && !reduceMotion.matches;
  const isDesktop = window.matchMedia('(min-width: 990px)');
  const root = (theme.routes.root || '/').replace(/\/?$/, '/');

  /* ---------- Yardımcılar ---------- */
  const fetchJSON = async (url, options = {}) => {
    const res = await fetch(url, {
      credentials: 'same-origin',
      ...options,
      headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest', ...(options.headers || {}) }
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.status) {
      const err = new Error(data.description || data.message || theme.strings.error || 'Error');
      err.data = data;
      throw err;
    }
    return data;
  };
  const parseHTML = (html) => new DOMParser().parseFromString(html, 'text/html');

  let toastTimer;
  const toast = (message, ok = true) => {
    const el = $('[data-toast]');
    if (!el) return;
    el.innerHTML = (ok
      ? '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'
      : '') + '<span></span>';
    el.lastChild.textContent = message;
    el.hidden = false;
    requestAnimationFrame(() => el.classList.add('is-visible'));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.classList.remove('is-visible');
      setTimeout(() => { el.hidden = true; }, 400);
    }, 2600);
  };

  /* ---------- Header ---------- */
  const header = $('[data-header]');
  if (header) {
    let lastY = window.scrollY;
    let ticking = false;
    const update = () => {
      const y = window.scrollY;
      header.classList.toggle('is-scrolled', y > 8);
      const openUI = header.classList.contains('is-open') || document.body.classList.contains('is-locked');
      const hide = !openUI && y > 480 && y > lastY + 4;
      const show = y < lastY - 4 || y <= 480;
      if (hide) header.classList.add('is-hidden');
      else if (show) header.classList.remove('is-hidden');
      document.documentElement.classList.toggle('header-hidden', header.classList.contains('is-hidden'));
      lastY = y;
      ticking = false;
    };
    window.addEventListener('scroll', () => {
      if (!ticking) { ticking = true; requestAnimationFrame(update); }
    }, { passive: true });
    update();
  }

  /* ---------- Çekmeceler (menü, sepet, filtre) ---------- */
  const drawers = {};
  let lastFocus = null;
  const focusables = (el) => $$('a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"])', el)
    .filter((n) => n.offsetParent !== null);

  const openDrawer = (name) => {
    const drawer = $(`[data-drawer="${name}"]`);
    if (!drawer) return false;
    Object.keys(drawers).forEach((k) => k !== name && closeDrawer(k, false));
    lastFocus = document.activeElement;
    drawer.classList.add('is-open');
    drawer.setAttribute('aria-hidden', 'false');
    document.body.classList.add('is-locked');
    $$(`[data-drawer-open="${name}"]`).forEach((b) => b.setAttribute('aria-expanded', 'true'));
    drawers[name] = drawer;
    const panel = $('.drawer__panel', drawer);
    setTimeout(() => (focusables(panel)[0] || panel).focus({ preventScroll: true }), 60);
    return true;
  };
  const closeDrawer = (name, restore = true) => {
    const drawer = drawers[name] || $(`[data-drawer="${name}"]`);
    if (!drawer || !drawer.classList.contains('is-open')) return;
    drawer.classList.remove('is-open');
    drawer.setAttribute('aria-hidden', 'true');
    delete drawers[name];
    $$(`[data-drawer-open="${name}"]`).forEach((b) => b.setAttribute('aria-expanded', 'false'));
    if (!Object.keys(drawers).length) document.body.classList.remove('is-locked');
    if (restore && lastFocus) lastFocus.focus({ preventScroll: true });
  };

  document.addEventListener('click', (e) => {
    const opener = e.target.closest('[data-drawer-open]');
    if (opener) { e.preventDefault(); openDrawer(opener.dataset.drawerOpen); return; }
    const closer = e.target.closest('[data-drawer-close]');
    if (closer) {
      const drawer = closer.closest('[data-drawer]');
      if (drawer) { e.preventDefault(); closeDrawer(drawer.dataset.drawer); }
      return;
    }
    const cartLink = e.target.closest('[data-cart-open]');
    if (cartLink && theme.cartType === 'drawer' && $('[data-drawer="cart"]')) {
      e.preventDefault();
      openDrawer('cart');
    }
  });

  document.addEventListener('keydown', (e) => {
    const names = Object.keys(drawers);
    if (e.key === 'Escape') {
      if (names.length) closeDrawer(names[names.length - 1]);
      closeSearch();
      closeZoom();
      return;
    }
    if (e.key === 'Tab' && names.length) {
      const panel = $('.drawer__panel', drawers[names[names.length - 1]]);
      const items = focusables(panel);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  // Menü içindeki bağlantıya tıklanınca çekmeceyi kapat (aynı sayfa filtre linkleri için)
  $$('[data-drawer="menu"] a').forEach((a) => a.addEventListener('click', () => closeDrawer('menu', false)));

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

    let timer;
    let controller;
    if (input && results && theme.routes.predictiveSearch) {
      input.addEventListener('input', () => {
        clearTimeout(timer);
        const q = input.value.trim();
        if (q.length < 2) { results.innerHTML = ''; return; }
        timer = setTimeout(async () => {
          controller && controller.abort();
          controller = new AbortController();
          try {
            const url = `${theme.routes.predictiveSearch}?q=${encodeURIComponent(q)}&section_id=predictive-search&resources[type]=product,collection&resources[limit]=6&resources[options][unavailable_products]=last`;
            const res = await fetch(url, { signal: controller.signal });
            if (!res.ok) return;
            const doc = parseHTML(await res.text());
            const inner = $('[data-predictive-inner]', doc);
            results.innerHTML = inner ? inner.outerHTML : '';
          } catch (err) { /* iptal edilen istek */ }
        }, 220);
      });
    }
  }

  /* ---------- Sepet ---------- */
  const updateCount = (count) => {
    if (theme.strings.cartCount) $$('[data-cart-count-label]').forEach((el) => { el.textContent = theme.strings.cartCount.replace('__COUNT__', count); });
    $$('[data-cart-count]').forEach((el) => {
      el.textContent = count;
      el.classList.toggle('is-empty', count === 0);
      el.classList.remove('is-bump');
      void el.offsetWidth;
      el.classList.add('is-bump');
    });
  };

  const renderDrawer = (html) => {
    const drawerInner = $('[data-cart-drawer-inner]');
    if (!drawerInner || !html) return;
    const next = $('[data-cart-drawer-inner]', parseHTML(html));
    if (next) drawerInner.innerHTML = next.innerHTML;
  };

  const cartPage = $('[data-cart-page]');
  const cartPageSection = cartPage && cartPage.closest('[id^="shopify-section-"]');
  const cartPageSectionId = cartPageSection ? cartPageSection.id.replace('shopify-section-', '') : null;

  const renderCartPage = (html) => {
    const inner = $('[data-cart-page-inner]');
    if (!inner || !html) return;
    const next = $('[data-cart-page-inner]', parseHTML(html));
    if (next) inner.innerHTML = next.innerHTML;
  };

  const sectionsToRender = () => {
    const list = [];
    if ($('[data-cart-drawer-inner]')) list.push('cart-drawer');
    if (cartPageSectionId) list.push(cartPageSectionId);
    return list;
  };

  const setButtonState = (btn, state) => {
    if (!btn) return;
    btn.classList.remove('is-loading', 'is-added');
    if (state) btn.classList.add(state);
    btn.setAttribute('aria-busy', state === 'is-loading' ? 'true' : 'false');
  };

  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('form[data-product-form]');
    if (!form) return;
    if (!window.fetch) return;
    e.preventDefault();

    const submitter = e.submitter && e.submitter.matches('[data-add-button]') ? e.submitter : $('[data-add-button]', form);
    const allButtons = form.id ? $$(`[form="${form.id}"][data-add-button]`).concat($$('[data-add-button]', form)) : $$('[data-add-button]', form);
    const errorEl = $('[data-form-error]', form);
    if (errorEl) errorEl.hidden = true;
    if (allButtons.some((b) => b.disabled)) return;

    allButtons.forEach((b) => setButtonState(b, b === submitter ? 'is-loading' : null));

    const body = new FormData(form);
    const sections = sectionsToRender();
    if (sections.length) {
      body.append('sections', sections.join(','));
      body.append('sections_url', window.location.pathname);
    }

    try {
      const data = await fetchJSON(`${theme.routes.cartAdd || '/cart/add'}.js`, { method: 'POST', body });
      if (theme.cartType === 'page' || !$('[data-drawer="cart"]')) {
        window.location.href = theme.routes.cart || '/cart';
        return;
      }
      if (data.sections) {
        renderDrawer(data.sections['cart-drawer']);
        if (cartPageSectionId) renderCartPage(data.sections[cartPageSectionId]);
      }
      const cart = await fetchJSON(`${theme.routes.cart || '/cart'}.js`);
      updateCount(cart.item_count);
      allButtons.forEach((b) => setButtonState(b, null));
      if (submitter) {
        setButtonState(submitter, 'is-added');
        setTimeout(() => setButtonState(submitter, null), 1600);
      }
      openDrawer('cart');
      document.dispatchEvent(new CustomEvent('cart:added', { detail: { item: data } }));
    } catch (err) {
      allButtons.forEach((b) => setButtonState(b, null));
      const message = (err.data && (err.data.description || err.data.message)) || theme.strings.error;
      if (errorEl) { errorEl.textContent = message; errorEl.hidden = false; }
      else toast(message, false);
    }
  });

  let changeQueue = Promise.resolve();
  const changeLine = (line, quantity, lineEl) => {
    changeQueue = changeQueue.then(async () => {
      lineEl && lineEl.classList.add('is-updating');
      const sections = sectionsToRender();
      try {
        const cart = await fetchJSON(`${theme.routes.cartChange || '/cart/change'}.js`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ line, quantity, sections, sections_url: window.location.pathname })
        });
        if (cart.sections) {
          renderDrawer(cart.sections['cart-drawer']);
          if (cartPageSectionId) renderCartPage(cart.sections[cartPageSectionId]);
        }
        updateCount(cart.item_count);
      } catch (err) {
        lineEl && lineEl.classList.remove('is-updating');
        toast((err.data && err.data.description) || theme.strings.error, false);
      }
    });
    return changeQueue;
  };

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-line-change]');
    if (!btn) return;
    const lineEl = btn.closest('[data-line]');
    if (!lineEl) return;
    e.preventDefault();
    changeLine(Number(lineEl.dataset.line), Math.max(0, Number(btn.dataset.lineChange)), lineEl);
  });
  document.addEventListener('change', (e) => {
    const input = e.target.closest('[data-line-input]');
    if (input) {
      const lineEl = input.closest('[data-line]');
      const qty = Math.max(0, parseInt(input.value, 10) || 0);
      changeLine(Number(lineEl.dataset.line), qty, lineEl);
      return;
    }
    const note = e.target.closest('[data-cart-note]');
    if (note) {
      fetchJSON(`${theme.routes.cartUpdate || '/cart/update'}.js`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: note.value })
      }).catch(() => {});
    }
  });
  // Enter tuşu sepet formunu göndermesin (adet alanı)
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.matches && e.target.matches('[data-line-input]')) {
      e.preventDefault();
      e.target.blur();
    }
  });

  /* ---------- Kart renk seçenekleri ---------- */
  document.addEventListener('click', (e) => {
    const sw = e.target.closest('[data-card-swatch]');
    if (!sw) return;
    e.preventDefault();
    const card = sw.closest('[data-product-card]');
    $$('[data-card-swatch]', card).forEach((s) => s.classList.toggle('is-active', s === sw));
    const img = $('.card__img--primary', card);
    if (img && sw.dataset.image) {
      img.srcset = '';
      img.src = sw.dataset.image;
    }
    if (sw.dataset.url) $$('[data-card-link]', card).forEach((a) => { a.href = sw.dataset.url; });
  });

  /* ---------- Ürün sayfası ---------- */
  $$('[data-product-section]').forEach((section) => {
    const jsonEl = $('[data-product-json]', section);
    if (!jsonEl) return;
    let product;
    try { product = JSON.parse(jsonEl.textContent); } catch (err) { return; }

    const form = $('[data-main-form]', section);
    const idInput = $('[data-variant-id]', section);
    const picker = $('[data-variant-picker]', section);
    const stock = $('[data-stock]', section);
    const addButtons = $$('[data-add-button]', section);
    const priceEl = $('.product__price-row [data-price]', section);
    const stickyPrice = $('[data-sticky-price]', section);
    const track = $('[data-gallery-track]', section);

    const selectedOptions = () => {
      const groups = $$('.variants__group', section);
      return groups.map((g) => {
        const checked = $('input:checked', g);
        return checked ? checked.value : null;
      });
    };

    const scrollToMedia = (mediaId) => {
      if (!track || !mediaId) return;
      const item = $(`[data-media-id="${mediaId}"]`, track);
      if (!item) return;
      if (isDesktop.matches) {
        const idx = $$('.gallery__item', track).indexOf(item);
        if (idx > 0) track.prepend(item);
        window.scrollTo({ top: Math.max(0, section.getBoundingClientRect().top + window.scrollY - 90), behavior: motionOK() ? 'smooth' : 'auto' });
      } else {
        track.scrollTo({ left: item.offsetLeft, behavior: motionOK() ? 'smooth' : 'auto' });
      }
    };

    const updateAvailability = () => {
      if (!picker) return;
      const current = selectedOptions();
      $$('.variants__group', section).forEach((g, index) => {
        $$('input', g).forEach((input) => {
          const candidate = current.slice();
          candidate[index] = input.value;
          const match = product.variants.find((v) => v.options.every((o, i) => o === candidate[i]));
          const label = input.nextElementSibling;
          if (label) label.classList.toggle('is-unavailable', !match || !match.available);
        });
      });
    };

    const applyVariant = (variant) => {
      addButtons.forEach((btn) => {
        const label = $('[data-add-label]', btn);
        if (!variant) {
          btn.disabled = true;
          if (label) label.textContent = theme.strings.unavailable;
        } else {
          btn.disabled = !variant.available;
          if (label) label.textContent = variant.available ? theme.strings.addToCart : theme.strings.soldOut;
        }
      });
      if (!variant) {
        if (stock) stock.innerHTML = `<span class="stock__dot stock__dot--out"></span>${theme.strings.unavailable}`;
        return;
      }
      idInput.value = variant.id;
      if (priceEl) {
        const cur = $('[data-price-current]', priceEl);
        const cmpWrap = $('.price__compare', priceEl);
        const cmp = $('[data-price-compare]', priceEl);
        const badge = $('[data-price-badge]', priceEl);
        if (cur) cur.textContent = variant.price;
        priceEl.classList.toggle('price--sale', !!variant.compare_at);
        if (cmpWrap) cmpWrap.hidden = !variant.compare_at;
        if (cmp) cmp.textContent = variant.compare_at || '';
        if (badge) { badge.hidden = !variant.saving; badge.textContent = variant.saving ? `-%${variant.saving}` : ''; }
      }
      if (stickyPrice) stickyPrice.textContent = variant.price;
      if (stock) {
        if (!variant.available) stock.innerHTML = `<span class="stock__dot stock__dot--out"></span>${theme.strings.outOfStock}`;
        else if (variant.low > 0) stock.innerHTML = `<span class="stock__dot stock__dot--low"></span>${theme.strings.lowStock.replace('__COUNT__', variant.low)}`;
        else stock.innerHTML = `<span class="stock__dot"></span>${theme.strings.inStock}`;
      }
      const url = new URL(window.location.href);
      url.searchParams.set('variant', variant.id);
      window.history.replaceState({}, '', url.toString());
      if (variant.media_id) scrollToMedia(variant.media_id);
    };

    if (picker) {
      picker.addEventListener('change', (e) => {
        const input = e.target;
        const index = Number(input.dataset.optionIndex);
        const out = $(`[data-option-selected="${index}"]`, section);
        if (out) out.textContent = input.value;
        const opts = selectedOptions();
        const variant = product.variants.find((v) => v.options.every((o, i) => o === opts[i]));
        applyVariant(variant);
        updateAvailability();
      });
      updateAvailability();
    }

    // Adet
    const qtyInput = $('[data-qty-input]', section);
    $$('[data-qty-minus], [data-qty-plus]', section).forEach((btn) => btn.addEventListener('click', () => {
      const step = btn.hasAttribute('data-qty-plus') ? 1 : -1;
      qtyInput.value = Math.max(1, (parseInt(qtyInput.value, 10) || 1) + step);
    }));

    // Galeri sayacı (mobil kaydırma)
    if (track) {
      const indexEl = $('[data-gallery-index]', section);
      const dots = $$('.gallery__dot', section);
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

    // Tam ekran görsel
    const zoom = $('[data-zoom]', section);
    if (zoom && track) {
      const zoomTrack = $('[data-zoom-track]', zoom);
      $$('[data-zoom-open]', section).forEach((btn) => btn.addEventListener('click', () => {
        $$('img[data-src]', zoomTrack).forEach((img) => { img.src = img.dataset.src; img.removeAttribute('data-src'); });
        zoom.hidden = false;
        document.body.classList.add('is-locked');
        const items = $$('li', zoomTrack);
        const mediaItems = $$('.gallery__item', track);
        const clickedItem = btn.closest('.gallery__item');
        const originalIndex = Number(btn.dataset.zoomOpen);
        const target = items[originalIndex] || items[mediaItems.indexOf(clickedItem)] || items[0];
        requestAnimationFrame(() => { if (target) zoomTrack.scrollLeft = target.offsetLeft; });
        $('[data-zoom-close]', zoom).focus();
      }));
      $('[data-zoom-close]', zoom).addEventListener('click', closeZoom);
    }

    // Mobil yapışkan sepete ekle
    const sticky = $('[data-sticky-buy]', section);
    const mainAdd = $('[data-main-add]', section);
    if (sticky && mainAdd && 'IntersectionObserver' in window) {
      const stickyBtn = $('[data-sticky-add]', sticky);
      const footer = $('.footer');
      let mainVisible = true;
      let footerVisible = false;
      const sync = () => {
        const show = !mainVisible && !footerVisible && mainAdd.getBoundingClientRect().top < 0;
        sticky.classList.toggle('is-visible', show);
        sticky.setAttribute('aria-hidden', show ? 'false' : 'true');
        if (stickyBtn) stickyBtn.tabIndex = show ? 0 : -1;
      };
      new IntersectionObserver(([entry]) => { mainVisible = entry.isIntersecting; sync(); }).observe(mainAdd);
      let stickyRaf = null;
      window.addEventListener('scroll', () => {
        if (!stickyRaf) stickyRaf = requestAnimationFrame(() => { stickyRaf = null; sync(); });
      }, { passive: true });
      if (footer) new IntersectionObserver(([entry]) => { footerVisible = entry.isIntersecting; sync(); }).observe(footer);
    }

    if (form && !form.id) form.id = `ProductForm-${section.dataset.sectionId}`;
  });

  function closeZoom() {
    const zoom = $('[data-zoom]:not([hidden])');
    if (!zoom) return;
    zoom.hidden = true;
    if (!Object.keys(drawers).length) document.body.classList.remove('is-locked');
  }

  /* ---------- Koleksiyon sıralama ---------- */
  $$('[data-sort]').forEach((select) => select.addEventListener('change', () => select.form.submit()));

  /* ---------- Ürün şeridi (oklar + gerçek satış sıralaması) ---------- */
  $$('[data-rail]').forEach((rail) => {
    const viewport = $('[data-rail-viewport]', rail);
    const arrows = $('[data-rail-arrows]', rail);
    const prev = $('[data-rail-prev]', rail);
    const next = $('[data-rail-next]', rail);
    const syncArrows = () => {
      if (!viewport || !arrows) return;
      const overflow = viewport.scrollWidth > viewport.clientWidth + 4;
      arrows.hidden = !overflow || !isDesktop.matches;
      if (prev) prev.disabled = viewport.scrollLeft <= 4;
      if (next) next.disabled = viewport.scrollLeft + viewport.clientWidth >= viewport.scrollWidth - 4;
    };
    const step = () => {
      const item = $('.rail__item', viewport);
      return item ? item.getBoundingClientRect().width + 24 : viewport.clientWidth * 0.8;
    };
    prev && prev.addEventListener('click', () => viewport.scrollBy({ left: -step(), behavior: 'smooth' }));
    next && next.addEventListener('click', () => viewport.scrollBy({ left: step(), behavior: 'smooth' }));
    viewport && viewport.addEventListener('scroll', syncArrows, { passive: true });
    window.addEventListener('resize', syncArrows);
    syncArrows();

    // Koleksiyon seçilmemiş şeritlerde Shopify'ın gerçek sıralamasını (en çok satan / en yeni) getir
    const sort = rail.dataset.railSort;
    const list = $('[data-rail-items]', rail);
    if (sort && list && theme.routes.root !== undefined) {
      const limit = Number(rail.dataset.railLimit) || 8;
      const url = `${root}collections/all?sort_by=${encodeURIComponent(sort)}&section_id=product-rail`;
      fetch(url)
        .then((r) => (r.ok ? r.text() : null))
        .then((html) => {
          if (!html) return;
          const fresh = $('[data-rail-items]', parseHTML(html));
          if (!fresh) return;
          const items = $$('.rail__item', fresh).slice(0, limit);
          if (!items.length) return;
          const currentIds = $$('.rail__item a[data-card-link]', list).map((a) => a.getAttribute('href')).join('|');
          const freshIds = items.map((li) => { const a = $('a[data-card-link]', li); return a ? a.getAttribute('href') : ''; }).join('|');
          if (currentIds === freshIds) return;
          items.forEach((li) => { li.removeAttribute('data-reveal'); });
          list.replaceChildren(...items.map((li) => document.importNode(li, true)));
          list.dataset.count = items.length;
          syncArrows();
        })
        .catch(() => {});
    }
  });

  /* ---------- Ürün önerileri ---------- */
  $$('[data-recommendations]').forEach((el) => {
    const load = () => {
      fetch(el.dataset.url)
        .then((r) => (r.ok ? r.text() : null))
        .then((html) => {
          if (!html) return;
          const fresh = $('[data-recommendations]', parseHTML(html));
          if (fresh && fresh.innerHTML.trim()) el.innerHTML = fresh.innerHTML;
        })
        .catch(() => {});
    };
    if ('IntersectionObserver' in window) {
      const io = new IntersectionObserver((entries) => {
        if (entries[0].isIntersecting) { io.disconnect(); load(); }
      }, { rootMargin: '0px 0px 400px 0px' });
      io.observe(el);
    } else load();
  });

  /* ---------- Görünür olunca beliren öğeler ---------- */
  const revealAll = () => $$('[data-reveal]').forEach((el) => el.classList.add('is-in'));
  if (!motionOK() || !('IntersectionObserver' in window)) {
    revealAll();
  } else {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-in');
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    const observe = () => $$('[data-reveal]:not(.is-in)').forEach((el) => io.observe(el));
    observe();
    document.addEventListener('shopify:section:load', observe);
  }

  /* ---------- Hero: kaydırmaya tepki veren ürün ---------- */
  $$('[data-hero]').forEach((hero) => {
    const stage = $('[data-hero-stage]', hero);
    const trackEl = $('[data-hero-track]', hero);
    if (!stage || !trackEl || !hero.classList.contains('hero--motion')) return;
    if (!motionOK()) { hero.classList.remove('hero--motion'); return; }

    let active = true;
    let raf = null;
    let lastP = -1;
    const render = () => {
      raf = null;
      const rect = trackEl.getBoundingClientRect();
      const total = rect.height - window.innerHeight;
      const p = total > 0 ? Math.min(1, Math.max(0, -rect.top / total)) : 0;
      if (Math.abs(p - lastP) < 0.001) return;
      lastP = p;
      stage.style.setProperty('--p', p.toFixed(4));
      hero.classList.toggle('is-detail', p > 0.5);
    };
    const onScroll = () => { if (active && !raf) raf = requestAnimationFrame(render); };
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(([entry]) => { active = entry.isIntersecting; if (active) onScroll(); }).observe(trackEl);
    }
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', () => { lastP = -1; onScroll(); });
    render();
  });

  /* ---------- Paralaks (yalnızca masaüstü) ---------- */
  const parallaxEls = $$('[data-parallax]');
  if (parallaxEls.length && motionOK()) {
    let raf = null;
    const visible = new Set();
    const tick = () => {
      raf = null;
      if (!isDesktop.matches) { parallaxEls.forEach((el) => { el.style.transform = ''; }); return; }
      const vh = window.innerHeight;
      visible.forEach((el) => {
        const r = el.getBoundingClientRect();
        const offset = (r.top + r.height / 2 - vh / 2) * Number(el.dataset.parallax || 0);
        el.style.transform = `translate3d(0, ${offset.toFixed(1)}px, 0)`;
      });
    };
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => (e.isIntersecting ? visible.add(e.target) : visible.delete(e.target)));
      if (!raf) raf = requestAnimationFrame(tick);
    });
    parallaxEls.forEach((el) => io.observe(el));
    window.addEventListener('scroll', () => { if (!raf) raf = requestAnimationFrame(tick); }, { passive: true });
  }

  /* ---------- Tema düzenleyici ---------- */
  document.addEventListener('shopify:section:load', () => {
    if (!motionOK()) revealAll();
  });
})();
