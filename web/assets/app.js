/* TrendHub panel — derleme adımı gerektirmeyen tek sayfa uygulama.
 * Tüm dinamik metinler html`` şablonunda otomatik olarak escape edilir (XSS koruması).
 * Canlı veri yoksa boş durum gösterilir; hiçbir yerde örnek/sahte veri üretilmez. */
'use strict';

// ------------------------------------------------------------------ yardımcılar
class Raw { constructor(v) { this.v = v; } toString() { return this.v; } }
const raw = (v) => new Raw(v);
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
function renderVal(v) {
  if (v instanceof Raw) return v.v;
  if (Array.isArray(v)) return v.map(renderVal).join('');
  if (v === false || v === null || v === undefined) return '';
  return esc(v);
}
function html(strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => { out += s + (i < vals.length ? renderVal(vals[i]) : ''); });
  return raw(out);
}
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const nf = new Intl.NumberFormat('tr-TR');
const mf = new Intl.NumberFormat('tr-TR', { style: 'currency', currency: 'TRY', maximumFractionDigits: 2 });
const mf0 = new Intl.NumberFormat('tr-TR', { style: 'currency', currency: 'TRY', maximumFractionDigits: 0 });
const pf = new Intl.NumberFormat('tr-TR', { style: 'percent', maximumFractionDigits: 1 });
const df = new Intl.DateTimeFormat('tr-TR', { dateStyle: 'medium', timeZone: 'Europe/Istanbul' });
const dtf = new Intl.DateTimeFormat('tr-TR', { dateStyle: 'short', timeStyle: 'short', timeZone: 'Europe/Istanbul' });
const money = (n) => (n === null || n === undefined ? '—' : mf.format(Number(n)));
const money0 = (n) => (n === null || n === undefined ? '—' : mf0.format(Number(n)));
const num = (n) => (n === null || n === undefined ? '—' : nf.format(Number(n)));
const pct = (n) => (n === null || n === undefined ? '—' : pf.format(Number(n)));
const date = (s) => (s ? df.format(new Date(s)) : '—');
const dateTime = (s) => (s ? dtf.format(new Date(s)) : '—');
const signClass = (n) => (Number(n) < 0 ? 'neg' : Number(n) > 0 ? 'pos' : '');
const todayIso = () => new Date().toLocaleDateString('sv-SE', { timeZone: 'Europe/Istanbul' });

const ROLE_LABELS = { admin: 'Yönetici', operator: 'Operatör', viewer: 'İzleyici', accountant: 'Muhasebe' };
const ROLE_RANK = { viewer: 0, accountant: 0, operator: 1, admin: 2 };
const canFinance = () => state.user && ['admin', 'operator', 'accountant'].includes(state.user.role);
const state = { user: null, meta: null, period: (localGet('th.period') !== 'custom' && localGet('th.period')) || '30d', range: null };
const can = (role) => state.user && ROLE_RANK[state.user.role] >= ROLE_RANK[role];

function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { /* yok say */ } }

// ------------------------------------------------------------------------ API
class ApiError extends Error {
  constructor(status, message, technical = null) { super(message); this.status = status; this.technical = technical; }
}
// Sade mesaj + (varsa) teknik ayrıntı "Gelişmiş detay" altında
const errorView = (e) => html`${e.message || 'Beklenmeyen hata'}${e.technical ? html`<details class="adv"><summary>Gelişmiş detay</summary><code>${e.technical}</code></details>` : ''}`;
async function api(path, { method = 'GET', body, query, rawBody } = {}) {
  let url = path;
  if (query) {
    const qs = new URLSearchParams();
    Object.entries(query).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '' && v !== false) qs.set(k, v); });
    const s = qs.toString();
    if (s) url += (url.includes('?') ? '&' : '?') + s;
  }
  const headers = { 'X-Requested-With': 'TrendHub' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  // rawBody: dosya yükleme (XML/CSV/JSON) — gövde olduğu gibi gönderilir
  if (rawBody !== undefined) headers['Content-Type'] = 'application/octet-stream';
  let res;
  try {
    res = await fetch(url, { method, headers, credentials: 'same-origin', body: rawBody !== undefined ? rawBody : body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, 'Sunucuya ulaşılamadı. Bağlantınızı kontrol edin.');
  }
  if (res.status === 401 && !path.startsWith('/api/auth/login')) {
    showLogin();
    throw new ApiError(401, 'Oturum açmanız gerekiyor');
  }
  const data = res.headers.get('content-type')?.includes('application/json') ? await res.json() : await res.text();
  if (!res.ok) {
    let msg = typeof data === 'object' && data ? data.detail : data;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
    const friendly = { 403: 'Bu işlem için yetkiniz yok.', 404: 'Kayıt bulunamadı.', 413: 'Dosya çok büyük.', 429: 'Çok fazla deneme. Biraz bekleyip tekrar deneyin.', 502: 'Sunucuya şu an ulaşılamıyor.', 503: 'Servis geçici olarak kullanılamıyor.', 504: 'Sunucu zamanında yanıt vermedi.' };
    if (typeof msg === 'string' && /^\s*</.test(msg)) msg = null;   // HTML hata sayfası kullanıcıya gösterilmez
    const technical = (typeof data === 'object' && data && data.technical) || (msg ? null : `HTTP ${res.status}`);
    throw new ApiError(res.status, msg || friendly[res.status] || `İşlem tamamlanamadı (hata ${res.status}).`, technical);
  }
  return data;
}

// --------------------------------------------------------------- arayüz öğeleri
function toast(message, bad = false, technical = null) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  if (technical) el.innerHTML = renderVal(errorView({ message, technical })); else el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), bad ? (technical ? 12000 : 6000) : 3500);
}
const fail = (e) => { if (e.status !== 401) toast(e.message || 'Beklenmeyen hata', true, e.technical); };

let lastFocus = null;
function openLayer(id, content) {
  lastFocus = document.activeElement;
  const layer = $(`#${id}`);
  $(`#${id}-body`).innerHTML = renderVal(content);
  layer.hidden = false;
  const f = layer.querySelector('input, select, textarea, button:not([data-close])');
  // Odak klavye erişimi için verilir ama panel en üstten açılır (mobilde üstteki bilgi görünür kalsın)
  (f || layer.querySelector('[data-close]')).focus({ preventScroll: true });
  layer.querySelector('.drawer-panel, .modal-panel')?.scrollTo(0, 0);
  return $(`#${id}-body`);
}
function closeLayer(id) {
  $(`#${id}`).hidden = true;
  $(`#${id}-body`).innerHTML = '';
  if (lastFocus) lastFocus.focus();
}
const openDrawer = (c) => openLayer('drawer', c);
const openModal = (c) => openLayer('modal', c);
['drawer', 'modal'].forEach((id) => {
  document.addEventListener('click', (e) => {
    const layer = $(`#${id}`);
    if (layer.hidden) return;
    if (e.target === layer || (e.target.closest('[data-close]') && layer.contains(e.target))) closeLayer(id);
  });
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!$('#modal').hidden) closeLayer('modal');
  else if (!$('#drawer').hidden) closeLayer('drawer');
  else document.body.classList.remove('nav-open');
});

const statusBadge = (code, label) => html`<span class="badge st-${code}">${label || statusLabel(code)}</span>`;
const statusLabel = (code) => state.meta?.statuses.find((s) => s.code === code)?.label || code;
const empty = (title, text) => html`<div class="empty"><b>${title}</b>${text}</div>`;
// KDV hesabında eksik bilgi varsa (komisyon/gider KDV durumu belirtilmemiş, kalemsiz sipariş) açıkça göster
const vatNote = (v) => {
  if (!v) return '';
  const complete = v.complete ?? v.vat_complete;
  if (complete) return '';
  const notes = (v.notes || []).join(' ');
  return html` <span class="badge plain tone-warn est" title="${notes}">KDV bilgisi eksik</span>`;
};
// Kâr hücresi: KDV biliniyorsa tahmini KDV sonrası kâr; değilse KDV öncesi kâr açıkça etiketli
const profitCell = (o) => (o.vat_known === false || o.profit_after_vat === null || o.profit_after_vat === undefined
  ? html`${money(o.net_profit)} <span class="badge plain tone-warn est" title="KDV hesaplanamadı (kalem bilgisi yok)">KDV öncesi</span>`
  : html`${money(o.profit_after_vat)}${estimateBadge(true)}`);
const estimateBadge = (flag) => (flag ? html` <span class="badge plain tone-warn est" title="Komisyon/kargo/hizmet bedeli veya ürün maliyeti tahmini ya da eksik">TAHMİNİ</span>` : '');

function pager(p, onGo) {
  if (!p.total) return '';
  const id = 'pg' + Math.random().toString(36).slice(2, 8);
  setTimeout(() => {
    $$(`[data-pager="${id}"]`).forEach((b) => b.addEventListener('click', () => onGo(Number(b.dataset.page))));
  });
  return html`<div class="pager"><span>${num(p.total)} kayıt · Sayfa ${p.page}/${p.pages}</span>
    <span class="row"><button class="btn btn-sm" data-pager="${id}" data-page="${p.page - 1}" ${raw(p.page <= 1 ? 'disabled' : '')}>‹ Önceki</button>
    <button class="btn btn-sm" data-pager="${id}" data-page="${p.page + 1}" ${raw(p.page >= p.pages ? 'disabled' : '')}>Sonraki ›</button></span></div>`;
}

function periodQuery() {
  if (state.period === 'custom' && state.range?.date_from && state.range?.date_to) return { date_from: state.range.date_from, date_to: state.range.date_to };
  return { period: state.period === 'custom' ? '30d' : state.period };
}
const periodQs = () => new URLSearchParams(periodQuery()).toString();
function periodSeg(current, onChange) {
  const opts = [['today', 'Bugün'], ['7d', '7 gün'], ['30d', '30 gün'], ['this_month', 'Bu ay'], ['last_month', 'Geçen ay'], ['custom', 'Özel']];
  setTimeout(() => {
    $$('[data-period]').forEach((b) => b.addEventListener('click', () => {
      state.period = b.dataset.period; localSet('th.period', state.period);
      if (state.period === 'custom' && !(state.range?.date_from && state.range?.date_to)) {
        const to = todayIso(); const from = new Date(Date.now() - 29 * 864e5).toLocaleDateString('sv-SE', { timeZone: 'Europe/Istanbul' });
        state.range = { date_from: from, date_to: to };
      }
      onChange(state.period);
    }));
    $$('[data-range]').forEach((inp) => inp.addEventListener('change', () => {
      state.range = { ...(state.range || {}), [inp.dataset.range]: inp.value };
      if (state.range.date_from && state.range.date_to && state.range.date_from <= state.range.date_to) onChange('custom');
    }));
  });
  return html`<div class="seg" role="group" aria-label="Dönem">${opts.map(([v, l]) =>
    html`<button data-period="${v}" class="${v === current ? 'on' : ''}" aria-pressed="${v === current}">${l}</button>`)}</div>
    ${current === 'custom' ? html`<span class="range-in"><input type="date" data-range="date_from" value="${state.range?.date_from || ''}" aria-label="Başlangıç"><input type="date" data-range="date_to" value="${state.range?.date_to || ''}" aria-label="Bitiş"></span>` : ''}`;
}

function formData(form) {
  const out = {};
  new FormData(form).forEach((v, k) => { out[k] = typeof v === 'string' ? v.trim() : v; });
  return out;
}

async function submitting(form, fn) {
  const btn = form.querySelector('[type="submit"]');
  const err = form.querySelector('.form-error');
  if (err) err.textContent = '';
  if (btn) btn.disabled = true;
  try { await fn(); } catch (e) { if (err) err.innerHTML = renderVal(errorView(e)); else fail(e); } finally { if (btn) btn.disabled = false; }
}

// ---------------------------------------------------------------- grafik (SVG)
function lineChart(points, series) {
  if (!points.length || points.every((p) => series.every((sr) => !Number(p[sr.key])))) return empty('Veri yok', 'Seçilen dönemde satış kaydı bulunmuyor.');
  const W = 720, H = 240, L = 64, R = 12, T = 12, B = 28;
  const vals = points.flatMap((p) => series.map((s) => Number(p[s.key]) || 0));
  let min = Math.min(0, ...vals), max = Math.max(0, ...vals);
  if (max === min) max = min + 1;
  const step = niceStep((max - min) / 4);
  min = Math.floor(min / step) * step; max = Math.ceil(max / step) * step;
  const x = (i) => L + (points.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (points.length - 1));
  const y = (v) => T + ((max - v) * (H - T - B)) / (max - min);
  const ticks = []; for (let v = min; v <= max + step / 2; v += step) ticks.push(v);
  const every = Math.max(1, Math.ceil(points.length / 8));
  const id = 'c' + Math.random().toString(36).slice(2, 8);
  const svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Günlük ciro ve net kâr">
    ${ticks.map((v) => `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke-width="${v === 0 ? 1.5 : 1}"/>
      <g class="axis"><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${esc(compactMoney(v))}</text></g>`).join('')}
    ${points.map((p, i) => (i % every === 0 ? `<g class="axis"><text x="${x(i)}" y="${H - 8}" text-anchor="middle">${esc(p.day.slice(8, 10) + '.' + p.day.slice(5, 7))}</text></g>` : '')).join('')}
    ${series.map((s) => `<polyline fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"
      points="${points.map((p, i) => `${x(i)},${y(Number(p[s.key]) || 0)}`).join(' ')}"/>`).join('')}
    <line id="${id}-cross" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="var(--text-3)" stroke-dasharray="3 3" visibility="hidden"/>
    ${series.map((s, k) => `<circle id="${id}-dot${k}" r="4" fill="${s.color}" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>`).join('')}
    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" id="${id}-hit"/>
  </svg>`;
  setTimeout(() => {
    const hit = document.getElementById(`${id}-hit`);
    if (!hit) return;
    const box = hit.closest('.chart'), tip = box.querySelector('.tip'), svgEl = hit.ownerSVGElement;
    const move = (ev) => {
      const r = svgEl.getBoundingClientRect();
      const px = ((ev.clientX - r.left) / r.width) * W;
      const i = Math.max(0, Math.min(points.length - 1, Math.round(((px - L) / (W - L - R)) * (points.length - 1))));
      const p = points[i];
      const cross = document.getElementById(`${id}-cross`);
      cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i)); cross.setAttribute('visibility', 'visible');
      series.forEach((s, k) => {
        const d = document.getElementById(`${id}-dot${k}`);
        d.setAttribute('cx', x(i)); d.setAttribute('cy', y(Number(p[s.key]) || 0)); d.setAttribute('visibility', 'visible');
      });
      tip.hidden = false;
      tip.innerHTML = renderVal(html`<b>${date(p.day)}</b>${series.map((s) => html`<div><span>${s.label}</span><b class="num">${money(p[s.key])}</b></div>`)}<div><span>Sipariş</span><b class="num">${num(p.orders)}</b></div>`);
      const left = (x(i) / W) * r.width;
      tip.style.left = Math.min(Math.max(0, left + 12), r.width - tip.offsetWidth) + 'px';
      tip.style.top = '28px';
    };
    hit.addEventListener('pointermove', move);
    hit.addEventListener('pointerleave', () => {
      tip.hidden = true;
      svgEl.querySelectorAll(`#${id}-cross, [id^="${id}-dot"]`).forEach((n) => n.setAttribute('visibility', 'hidden'));
    });
  });
  return html`<div class="chart"><div class="legend">${series.map((s) => html`<span><i style="background:${s.color}"></i>${s.label}</span>`)}</div>${raw(svg)}<div class="tip" hidden></div></div>`;
}
function niceStep(raw0) {
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(raw0, 1e-9))));
  const f = raw0 / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}
function compactMoney(v) {
  const a = Math.abs(v);
  if (a >= 1e6) return (v / 1e6).toLocaleString('tr-TR', { maximumFractionDigits: 1 }) + ' Mn ₺';
  if (a >= 1e3) return (v / 1e3).toLocaleString('tr-TR', { maximumFractionDigits: 1 }) + ' B ₺';
  return v.toLocaleString('tr-TR') + ' ₺';
}

const COMPONENTS = [
  ['product_cost', 'Ürün maliyeti'], ['commission', 'Komisyon'], ['service_fee', 'Hizmet bedeli'],
  ['shipping', 'Kargo'], ['advertising', 'Reklam'], ['refund', 'İade'], ['other', 'Diğer'],
];
const TAHMINI = html`<span class="badge plain tone-warn est" title="Gerçek pazaryeri hakediş verisi bağlanmadı; komisyon, kargo, hizmet bedeli ve vergi tahmini hesaplanır.">TAHMİNİ</span>`;
// Net değer arka uçta Decimal ile hesaplanır; burada yeniden toplanmaz (float hatası olmasın).
function profitBars(t, { periodExpenses, net, tax, netAfterTax } = {}) {
  const revenue = Number(t.revenue) || 0;
  const rows = COMPONENTS.map(([k, l]) => [l, Number(t[k]) || 0]);
  if (periodExpenses !== undefined) rows.push(['Dönem giderleri', Number(periodExpenses) || 0]);
  const scale = Math.max(revenue, 1);
  const bar = (v, cls = '') => html`<div class="bar-track"><div class="bar-fill ${cls}" style="width:${Math.min(100, (Math.abs(Number(v) || 0) / scale) * 100).toFixed(2)}%"></div></div>`;
  return html`<div class="bars">
    <div class="bar-row"><span>Ciro</span>${bar(revenue, 'rev')}<span class="r num">${money(t.revenue)}</span></div>
    ${rows.map(([l, v]) => html`<div class="bar-row"><span>− ${l}</span>${bar(v)}<span class="r num">${money(v)}</span></div>`)}
    <div class="bar-row total"><span>Net kâr</span>${bar(net, 'profit')}<span class="r num ${signClass(net)}">${money(net)}</span></div>
    ${tax !== undefined ? html`<div class="bar-row"><span>− Tahmini KDV</span>${bar(tax)}<span class="r num">${money(tax)}</span></div>
      <div class="bar-row total"><span>Net kâr (KDV sonrası)</span>${bar(netAfterTax, 'profit')}<span class="r num ${signClass(netAfterTax)}">${money(netAfterTax)}</span></div>` : ''}
    ${t.discount !== undefined && Number(t.discount) ? html`<p class="small muted">Satıcı indirimi ${money(t.discount)} ciroya zaten yansımıştır; ayrıca düşülmez.</p>` : ''}
  </div>`;
}

// Kâr/zarar sütun grafiği: pozitif ve negatif günler ayrı renk + etiket (yalnızca renge dayanmaz)
function barChart(points, key, { label }) {
  if (!points.length || points.every((p) => !Number(p[key]))) return empty('Veri yok', 'Seçilen dönemde kâr/zarar kaydı bulunmuyor.');
  const W = 720, H = 220, L = 64, R = 12, T = 12, B = 28;
  const vals = points.map((p) => Number(p[key]) || 0);
  let min = Math.min(0, ...vals), max = Math.max(0, ...vals);
  if (max === min) max = min + 1;
  const step = niceStep((max - min) / 4);
  min = Math.floor(min / step) * step; max = Math.ceil(max / step) * step;
  const y = (v) => T + ((max - v) * (H - T - B)) / (max - min);
  const slot = (W - L - R) / points.length, bw = Math.max(2, Math.min(24, slot - 2));
  const ticks = []; for (let v = min; v <= max + step / 2; v += step) ticks.push(v);
  const every = Math.max(1, Math.ceil(points.length / 8));
  const id = 'b' + Math.random().toString(36).slice(2, 8);
  const svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
    ${ticks.map((v) => `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke-width="${v === 0 ? 1.5 : 1}"/>
      <g class="axis"><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${esc(compactMoney(v))}</text></g>`).join('')}
    ${points.map((p, i) => {
      const v = vals[i], x = L + i * slot + (slot - bw) / 2, top = Math.min(y(v), y(0)), h = Math.max(1, Math.abs(y(v) - y(0)));
      return `<rect x="${x}" y="${top}" width="${bw}" height="${h}" rx="3" fill="${v < 0 ? 'var(--bad)' : 'var(--series-3)'}"/>`;
    }).join('')}
    ${points.map((p, i) => (i % every === 0 ? `<g class="axis"><text x="${L + i * slot + slot / 2}" y="${H - 8}" text-anchor="middle">${esc(p.day.slice(8, 10) + '.' + p.day.slice(5, 7))}</text></g>` : '')).join('')}
    <rect id="${id}-hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
  </svg>`;
  setTimeout(() => {
    const hit = document.getElementById(`${id}-hit`);
    if (!hit) return;
    const box = hit.closest('.chart'), tip = box.querySelector('.tip'), svgEl = hit.ownerSVGElement;
    hit.addEventListener('pointermove', (ev) => {
      const r = svgEl.getBoundingClientRect();
      const i = Math.max(0, Math.min(points.length - 1, Math.floor(((((ev.clientX - r.left) / r.width) * W) - L) / slot)));
      const p = points[i];
      tip.hidden = false;
      tip.innerHTML = renderVal(html`<b>${date(p.day)}</b><div><span>${label}</span><b class="num ${signClass(p[key])}">${money(p[key])}</b></div><div><span>Sipariş</span><b class="num">${num(p.orders)}</b></div>`);
      tip.style.left = Math.min(Math.max(0, ((L + i * slot) / W) * r.width + 12), r.width - tip.offsetWidth) + 'px';
      tip.style.top = '28px';
    });
    hit.addEventListener('pointerleave', () => { tip.hidden = true; });
  });
  return html`<div class="chart"><div class="legend"><span><i style="background:var(--series-3)"></i>Kâr</span><span><i style="background:var(--bad)"></i>Zarar</span></div>${raw(svg)}<div class="tip" hidden></div></div>`;
}

function shareBars(items, valueKey, labelFn, fmt = money) {
  const total = items.reduce((a, it) => a + (Number(it[valueKey]) || 0), 0);
  const max = Math.max(...items.map((it) => Number(it[valueKey]) || 0), 1);
  return html`<div class="bars">${items.map((it) => html`<div class="bar-row">
    <span class="ellipsis" title="${labelFn(it)}">${labelFn(it)}</span>
    <div class="bar-track"><div class="bar-fill rev" style="width:${(((Number(it[valueKey]) || 0) / max) * 100).toFixed(2)}%"></div></div>
    <span class="r num">${fmt(it[valueKey])}${total ? html` <span class="muted small">${pct((Number(it[valueKey]) || 0) / total)}</span>` : ''}</span></div>`)}</div>`;
}

// ------------------------------------------------------------------ sayfalar
const PAGES = {};
const view = () => $('#view');
function setHeader(title, subtitle, actions = '') {
  $('#page-title').textContent = title;
  $('#page-subtitle').textContent = subtitle || '';
  $('#page-actions').innerHTML = renderVal(actions);
  document.title = `${title} · TrendHub`;
}
const loading = () => { view().innerHTML = '<div class="skeleton">Yükleniyor…</div>'; };
const kpi = (label, value, sub = '', cls = '', extra = '') => html`<div class="card kpi"><div class="label">${label} ${extra}</div><div class="value ${cls}">${value}</div>${sub ? html`<div class="sub">${sub}</div>` : ''}</div>`;

// ---- Dashboard
PAGES.dashboard = {
  title: 'Genel Bakış', icon: 'dashboard', nav: 'Dashboard',
  async render() {
    const draw = async () => {
      setHeader('Genel Bakış', 'Tüm pazaryerlerinin merkezi özeti', periodSeg(state.period, draw));
      loading();
      const [d, al] = await Promise.all([api('/api/dashboard', { query: periodQuery() }),
        api('/api/alerts', { query: { status: 'open', page_size: 5 } }).catch(() => null)]);
      const s = d.summary, t = s.orders;
      const connected = d.integrations.filter((i) => i.state === 'connected').length;
      const hasMarketplaceOrders = d.by_marketplace.some((m) => Number(m.orders));
      view().innerHTML = renderVal(html`
        <div class="chips" style="margin-bottom:16px">${d.integrations.map((i) => html`
          <a class="chip" href="#/integrations"><span aria-hidden="true">${i.state === 'connected' ? '🟢' : i.state === 'error' ? '🔴' : '⚪'}</span>${i.name}
          <span class="muted small">${i.state_label}</span></a>`)}</div>
        ${al && al.total ? html`<a class="attention-box ${al.items.some((a) => a.severity === 'critical') ? 'bad' : 'warn'}" href="#/alerts">
          <span class="big num">${num(al.total)}</span><span><b>konu ilgilenmeni bekliyor</b>
          <span class="small">${al.items.slice(0, 3).map((a) => `${SEV_ICON[a.severity]} ${a.title}: ${a.description || ''}`).join(' · ')}</span></span><span class="go">Uyarılar →</span></a>` : ''}
        ${connected === 0 ? html`<div class="notice info" style="margin-bottom:16px">Henüz bağlı bir pazaryeri yok. <a href="#/integrations">Entegrasyonlar → Mağaza Ekle</a> ile mağazanızı bağladığınızda siparişler otomatik gelir. Aşağıdaki değerler gerçek kayıtlardan hesaplanır; veri yoksa 0 gösterilir.</div>` : ''}
        <div class="grid grid-4">
          ${kpi('Bugünkü satış', money0(d.today.revenue), `${num(d.today.orders)} sipariş`)}
          ${kpi('Toplam ciro', money0(t.revenue), `${date(d.range.from)} – ${date(d.range.to)} · iptaller hariç`)}
          ${kpi('Tahmini KDV sonrası kâr', money0(s.net_profit_after_tax), html`Net marj ${pct(s.margin_after_tax)} · KDV öncesi ${money0(s.net_profit_after_expenses)}${vatNote(s.vat)}`, signClass(s.net_profit_after_tax), TAHMINI)}
          ${kpi('Sipariş sayısı', num(t.orders), 'Seçili dönem, iptaller hariç')}
        </div>
        <div class="grid grid-4 mt">
          ${kpi('Bekleyen sipariş', num(d.pending_orders), 'Yeni · hazırlanıyor · tedarikçide · kargo bekliyor', d.pending_orders ? 'warn-text' : '')}
          ${kpi('İade', num(d.returns.period), `Dönem içi · toplam ${num(d.returns.open_total)}`, d.returns.period ? 'neg' : '')}
          ${kpi('Ortalama sipariş tutarı', t.average_order_value === null ? '—' : money(t.average_order_value), 'Ciro / sipariş')}
          ${kpi('Tahmini KDV', money0(s.tax_estimate), 'Satış KDV − maliyet, komisyon ve gider KDV\'si', '', TAHMINI)}
        </div>
        <div class="grid grid-4 mt">
          ${kpi('Bugünkü reklam harcaması', money0(d.today.ad_spend), html`<a href="#/ads">Reklamlar →</a>`)}
          ${kpi('Dönem reklam harcaması', money0(d.ad_spend), 'Elle girilen harcamalar')}
          ${kpi('🔴 Kritik uyarı', num(d.alert_summary.critical), html`<a href="#/alerts?severity=critical">Uyarılar →</a>`, d.alert_summary.critical ? 'neg' : '')}
          ${kpi('Açık uyarı', num(d.alert_summary.total), `🟠 ${num(d.alert_summary.warning)} · 🔵 ${num(d.alert_summary.info)}`, d.alert_summary.total ? 'warn-text' : '')}
        </div>
        <div class="grid grid-2 mt">
          <div class="card"><div class="card-head"><h2>En kârlı ürünler ${TAHMINI}</h2><a href="#/reports" class="small">Raporlar →</a></div>
            ${d.most_profitable.length ? html`<div class="table-wrap"><table><tbody>${d.most_profitable.map((p) => html`<tr><td><b>${p.sku}</b><span class="muted small ellipsis">${p.product_name}</span></td><td class="r num">${num(p.quantity)} adet</td><td class="r num ${signClass(p.profit)}">${money(p.profit)}</td></tr>`)}</tbody></table></div>` : empty('Veri yok', 'Maliyeti girilmiş satış olduğunda burada görünür.')}</div>
          <div class="card"><div class="card-head"><h2>Kritik stok</h2><span class="small muted">İlandaki ürünler · eşik ${num(d.critical_stock_threshold)}</span></div>
            ${d.critical_stock.length ? html`<div class="table-wrap"><table><tbody>${d.critical_stock.map((p) => html`<tr class="click" data-life="${p.id}"><td><b>${p.sku || '—'}</b><span class="muted small ellipsis">${p.name}</span></td><td class="r num ${Number(p.stock) > 0 ? 'warn-text' : 'neg'}">${num(p.stock)}</td></tr>`)}</tbody></table></div>` : empty('Kritik stok yok', 'İlandaki ürünlerin stoğu eşiğin üzerinde.')}</div>
        </div>
        <div class="grid grid-2 mt">
          <div class="card"><div class="card-head"><div><h2>Satış grafiği</h2><p>Günlük ciro · ${date(d.range.from)} – ${date(d.range.to)}</p></div></div>
            ${lineChart(d.daily, [{ key: 'revenue', label: 'Ciro', color: 'var(--series-1)' }])}</div>
          <div class="card"><div class="card-head"><div><h2>Kâr grafiği ${TAHMINI}</h2><p>Günlük net kâr / zarar</p></div></div>
            ${barChart(d.daily, 'net_profit', { label: 'Net kâr' })}</div>
        </div>
        <div class="grid grid-3 mt">
          <div class="card"><div class="card-head"><h2>En çok satan ürünler</h2><a href="#/reports" class="small">Raporlar →</a></div>
            ${d.top_products.length ? shareBars(d.top_products, 'revenue', (p) => `${p.sku} · ${num(p.quantity)} adet`) : empty('Satış yok', 'Seçilen dönemde satılan ürün bulunmuyor.')}</div>
          <div class="card"><div class="card-head"><h2>Pazaryeri dağılımı</h2></div>
            ${hasMarketplaceOrders ? shareBars(d.by_marketplace, 'revenue', (m) => `${m.name} · ${num(m.orders)} sip.`) : empty('Veri yok', 'Pazaryeri siparişi geldiğinde ciro dağılımı burada görünür.')}</div>
          <div class="card"><div class="card-head"><h2>Kâr dökümü ${TAHMINI}</h2><a href="#/finance" class="small">Finans →</a></div>
            ${profitBars(t, { periodExpenses: s.expenses.total, net: s.net_profit_after_expenses })}</div>
        </div>
        <div class="card mt"><div class="card-head"><div><h2>Tedarikçiler</h2><p>Ürün sayısı, stok durumu, son senkronizasyon ve hata durumu</p></div><a href="#/suppliers" class="small">Tümü →</a></div>
          ${d.suppliers.length ? supplierHealthTable(d.suppliers, { compact: true }) : empty('Tedarikçi yok', 'Tedarikçiler sayfasından XML, API, CSV veya manuel tedarikçi ekleyin.')}</div>
        <div class="card mt"><div class="card-head"><div><h2>Sipariş durumları</h2><p>Tüm kayıtlar · parantez içinde seçili dönem</p></div></div>
          <div class="status-grid">${d.statuses.map((st) => html`
            <button class="status-tile ${st.code === 'needs_review' && st.count ? 'alert' : ''}" data-status="${st.code}">
              ${statusBadge(st.code, st.label)}<b>${num(st.count)}</b><span class="muted small">(${num(st.period_count)})</span></button>`)}</div></div>
        <div class="grid grid-main mt">
          <div class="card"><div class="card-head"><h2>Son siparişler</h2><a href="#/orders" class="small">Tümü →</a></div>
            ${d.recent_orders.length ? html`<div class="table-wrap"><table><thead><tr><th>Sipariş</th><th>Pazaryeri</th><th>Tarih</th><th>Durum</th><th class="r">Ciro</th><th class="r" title="Tahmini KDV sonrası kâr">KDV sonrası kâr</th></tr></thead><tbody>
            ${d.recent_orders.map((o) => html`<tr class="click" data-order="${o.id}"><td>${o.external_order_id}</td><td>${o.marketplace_name || '—'}</td><td>${dateTime(o.order_date)}</td><td>${statusBadge(o.internal_status, o.status_label)}</td><td class="r num">${money(o.gross_revenue)}</td><td class="r num ${signClass(o.net_profit_after_tax ?? o.net_profit)}">${profitCell({ ...o, profit_after_vat: o.net_profit_after_tax, vat_known: o.net_profit_after_tax !== null })}</td></tr>`)}
            </tbody></table></div>` : empty('Henüz sipariş yok', 'Pazaryeri senkronizasyonu çalıştığında siparişler burada listelenir.')}</div>
          <div class="card"><div class="card-head"><h2>Uyarılar</h2></div><div class="stack">
            ${alertRow(d.alerts.needs_review, 'inceleme bekleyen sipariş', '#/orders?status=needs_review', 'bad')}
            ${alertRow(d.alerts.missing_cost_orders, 'siparişte ürün maliyeti eksik', '#/products?missing_cost=1', 'warn')}
            ${alertRow(d.alerts.open_errors, 'açık sistem hatası', '#/system', 'bad')}
            ${alertRow(d.queue.failed_24h, 'başarısız iş (24 saat)', '#/system', 'warn')}
            <div class="row nowrap"><span class="dot ${d.alerts.live_workers ? 'good' : 'bad'}"></span>${d.alerts.live_workers ? 'Worker çalışıyor' : 'Worker çalışmıyor — senkronizasyon yapılamaz'}</div>
            ${!d.alerts.needs_review && !d.alerts.missing_cost_orders && !d.alerts.open_errors && !d.queue.failed_24h ? html`<p class="muted small">Dikkat gerektiren bir durum yok.</p>` : ''}
          </div></div>
        </div>`);
      $$('[data-status]').forEach((b) => b.addEventListener('click', () => { location.hash = `#/orders?status=${b.dataset.status}`; }));
      $$('[data-order]').forEach((r) => r.addEventListener('click', () => showOrder(r.dataset.order)));
      $$('[data-life]').forEach((r) => r.addEventListener('click', () => showLifecycle(Number(r.dataset.life))));
      bindSupplierRows(view());
    };
    await draw();
  },
};
function alertRow(n, text, href, tone) {
  if (!n) return '';
  return html`<a class="notice ${tone}" href="${href}"><b class="num">${num(n)}</b> ${text} →</a>`;
}

// ---- Uyarılar / Sorunlar merkezi
const SEV_ICON = { critical: '🔴', warning: '🟠', info: '🔵' };
const SEV_LABEL = { critical: 'Kritik', warning: 'Uyarı', info: 'Bilgi' };
const ALERT_CATS = [['', 'Tüm kaynaklar'], ['supplier', 'Tedarikçi'], ['marketplace', 'Pazaryeri'], ['order', 'Sipariş'], ['shipping', 'Kargo'], ['product', 'Ürün'], ['system', 'Sistem']];
function alertCard(a) {
  const refs = [a.product_name && ['Ürün', a.product_name], a.external_order_id && ['Sipariş', a.external_order_id],
    a.marketplace_name && ['Mağaza', a.marketplace_name], a.supplier_name && ['Tedarikçi', a.supplier_name]].filter(Boolean);
  return html`<article class="alert-card sev-${a.severity} ${a.status === 'resolved' ? 'resolved' : ''}">
    <div class="alert-head"><span class="badge sev sev-${a.severity}">${SEV_ICON[a.severity]} ${SEV_LABEL[a.severity]}</span>
      <span class="badge plain">${a.category_label}</span>${a.occurrences > 1 ? html`<span class="muted small">${num(a.occurrences)} kez görüldü</span>` : ''}</div>
    <h3>${a.title}</h3><p>${a.description || ''}</p>
    ${refs.length ? html`<dl class="kv small">${refs.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>` : ''}
    <div class="alert-foot small muted"><span>Tespit: ${dateTime(a.first_detected_at)}</span><span>Son kontrol: ${dateTime(a.last_checked_at)}</span>
      ${a.status === 'resolved' ? html`<span>Çözüldü: ${dateTime(a.resolved_at)} · ${a.resolution === 'system' ? 'sorun ortadan kalktı (otomatik)' : (a.resolved_by_name || 'kullanıcı')}</span>` : ''}</div>
    <div class="row">${a.link ? html`<a class="btn btn-sm" href="${a.link}">İlgili kayda git →</a>` : ''}
      ${a.status === 'open' && can('operator') ? html`<button class="btn btn-sm btn-primary" data-resolve="${a.id}">Çözüldü olarak işaretle</button>` : ''}</div>
  </article>`;
}
PAGES.alerts = {
  title: 'Uyarılar', icon: 'alerts',
  async render(params) {
    const f = { status: params.get('status') || 'open', severity: params.get('severity') || '', category: params.get('category') || '', q: params.get('q') || '' };
    setHeader('Uyarılar', 'Tedarikçi, pazaryeri ve kargo sorunları — yalnızca gerçek veriden',
      can('operator') ? html`<button class="btn" id="scan-now">Şimdi tara</button>` : '');
    const sum = await api('/api/alerts/summary');
    const go = (patch) => {
      const q = new URLSearchParams(Object.entries({ ...f, ...patch }).filter(([k, v]) => v && !(k === 'status' && v === 'open')));
      location.hash = '#/alerts' + (q.toString() ? '?' + q : '');
    };
    view().innerHTML = renderVal(html`
      <div class="grid grid-4">
        <button class="card kpi sev-tile ${f.severity === 'critical' ? 'on' : ''}" data-sev="critical"><div class="label">🔴 Kritik</div><div class="value ${sum.critical ? 'neg' : ''}">${num(sum.critical)}</div></button>
        <button class="card kpi sev-tile ${f.severity === 'warning' ? 'on' : ''}" data-sev="warning"><div class="label">🟠 Uyarı</div><div class="value ${sum.warning ? 'warn-text' : ''}">${num(sum.warning)}</div></button>
        <button class="card kpi sev-tile ${f.severity === 'info' ? 'on' : ''}" data-sev="info"><div class="label">🔵 Bilgi</div><div class="value">${num(sum.info)}</div></button>
        <div class="card kpi"><div class="label">Son kontrol</div><div class="value small">${dateTime(sum.last_checked_at)}</div><div class="sub">Otomatik tarama 15 dk'da bir ve her senkrondan sonra</div></div>
      </div>
      <div class="card mt"><form class="filters" id="alert-filters">
        <input type="search" name="q" placeholder="Ürün, sipariş, açıklama…" value="${f.q}" aria-label="Ara">
        <select name="category" aria-label="Kaynak">${ALERT_CATS.map(([v, l]) => html`<option value="${v}" ${raw(v === f.category ? 'selected' : '')}>${l}${v && sum.by_category[v] ? ` (${sum.by_category[v]})` : ''}</option>`)}</select>
        <select name="severity" aria-label="Seviye"><option value="">Tüm seviyeler</option>${Object.entries(SEV_LABEL).map(([v, l]) => html`<option value="${v}" ${raw(v === f.severity ? 'selected' : '')}>${l}</option>`)}</select>
        <select name="status" aria-label="Durum">${[['open', 'Açık'], ['resolved', 'Çözülenler'], ['all', 'Tümü']].map(([v, l]) => html`<option value="${v}" ${raw(v === f.status ? 'selected' : '')}>${l}</option>`)}</select>
        <button class="btn btn-primary" type="submit">Filtrele</button></form>
        <div id="alert-list"><div class="skeleton">Yükleniyor…</div></div></div>`);
    $$('[data-sev]').forEach((b) => b.addEventListener('click', () => go({ severity: f.severity === b.dataset.sev ? '' : b.dataset.sev })));
    $('#alert-filters').addEventListener('submit', (e) => { e.preventDefault(); go(formData(e.target)); });
    $('#scan-now')?.addEventListener('click', async (e) => {
      e.target.disabled = true;
      try { const r = await api('/api/alerts/scan', { method: 'POST' }); toast(`Tarama tamamlandı: ${r.detected} açık sorun, ${r.new} yeni, ${r.auto_resolved} çözüldü`); refresh(); } catch (ex) { fail(ex); e.target.disabled = false; }
    });
    const load = async (page) => {
      const d = await api('/api/alerts', { query: { ...f, page, page_size: 20 } });
      $('#alert-list').innerHTML = renderVal(d.items.length ? html`<div class="alert-list">${d.items.map(alertCard)}</div>${pager(d, load)}`
        : empty(f.status === 'open' ? 'Açık sorun yok' : 'Kayıt yok', f.status === 'open' ? 'Tedarikçi, pazaryeri ve kargo tarafında ilgilenmeni bekleyen bir konu bulunmuyor.' : 'Seçilen filtrelere uyan uyarı yok.'));
      $$('[data-resolve]').forEach((b) => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await api(`/api/alerts/${b.dataset.resolve}/resolve`, { method: 'POST', body: {} }); toast('Çözüldü olarak işaretlendi'); refreshBell(); load(page); } catch (ex) { fail(ex); b.disabled = false; }
      }));
    };
    await load(1);
  },
};

// ---- Siparişler
PAGES.orders = {
  title: 'Siparişler', icon: 'orders',
  async render(params) {
    const f = { status: params.get('status') || '', marketplace: params.get('marketplace') || '', q: params.get('q') || '',
      date_from: params.get('date_from') || '', date_to: params.get('date_to') || '', sort: params.get('sort') || 'date_desc',
      loss_only: params.get('loss_only') === '1', page: Number(params.get('page') || 1) };
    if (params.get('open')) setTimeout(() => showOrder(params.get('open')));
    setHeader('Siparişler', 'Tüm pazaryerlerinden gelen siparişler');
    const mps = await api('/api/marketplaces');
    view().innerHTML = renderVal(html`<div class="card">
      <form class="filters" id="order-filters">
        <input type="search" name="q" placeholder="Sipariş no, müşteri, SKU, barkod…" value="${f.q}" aria-label="Ara">
        <select name="status" aria-label="Durum"><option value="">Tüm durumlar</option>${state.meta.statuses.map((s) => html`<option value="${s.code}" ${raw(s.code === f.status ? 'selected' : '')}>${s.label}</option>`)}</select>
        <select name="marketplace" aria-label="Pazaryeri"><option value="">Tüm pazaryerleri</option>${mps.map((m) => html`<option value="${m.code}" ${raw(m.code === f.marketplace ? 'selected' : '')}>${m.name}</option>`)}</select>
        <input type="date" name="date_from" value="${f.date_from}" aria-label="Başlangıç">
        <input type="date" name="date_to" value="${f.date_to}" aria-label="Bitiş">
        <select name="sort" aria-label="Sıralama">${[['date_desc', 'En yeni'], ['date_asc', 'En eski'], ['profit_desc', 'En kârlı'], ['profit_asc', 'En az kârlı'], ['revenue_desc', 'En yüksek ciro']].map(([v, l]) => html`<option value="${v}" ${raw(v === f.sort ? 'selected' : '')}>${l}</option>`)}</select>
        <label class="check"><input type="checkbox" name="loss_only" value="1" ${raw(f.loss_only ? 'checked' : '')}>Zarar edenler</label>
        <button class="btn btn-primary" type="submit">Filtrele</button>
      </form><div id="order-list"><div class="skeleton">Yükleniyor…</div></div></div>`);
    $('#order-filters').addEventListener('submit', (e) => {
      e.preventDefault();
      const q = new URLSearchParams(Object.entries(formData(e.target)).filter(([, v]) => v));
      location.hash = '#/orders' + (q.toString() ? '?' + q : '');
    });
    const load = async (page) => {
      const d = await api('/api/orders', { query: { ...f, loss_only: f.loss_only ? 'true' : '', page, page_size: 25 } });
      $('#order-list').innerHTML = renderVal(d.items.length ? html`<div class="table-wrap"><table><thead><tr>
        <th>Sipariş</th><th>Pazaryeri</th><th>Tarih</th><th>Müşteri</th><th>Durum</th><th>Kargo Planı</th><th class="r">Adet</th><th class="r">Ciro</th><th class="r" title="Tahmini KDV sonrası kâr">KDV sonrası kâr</th><th class="r" title="Net marj = kâr ÷ net satış">Net marj</th></tr></thead><tbody>
        ${d.items.map((o) => html`<tr class="click" data-order="${o.id}"><td><b>${o.external_order_id}</b>${o.review_reason ? html`<span class="ellipsis small neg" title="${o.review_reason}">${o.review_reason}</span>` : ''}</td>
          <td>${o.marketplace_name || '—'}</td><td>${dateTime(o.order_date)}</td><td><span class="ellipsis">${o.customer_name || '—'}</span><span class="muted small">${o.customer_city || ''}</span></td>
          <td>${statusBadge(o.internal_status, o.status_label)}</td><td>${shipPlanBadge(o.shipping_plan)}</td><td class="r num">${num(o.item_count)}</td><td class="r num">${money(o.gross_revenue)}</td>
          <td class="r num ${signClass(o.profit_after_vat ?? o.net_profit)}">${profitCell(o)}</td><td class="r num">${pct(o.margin_after_vat ?? o.margin_before_vat)}</td></tr>`)}
        </tbody></table></div>${pager(d, load)}` : empty('Sipariş bulunamadı', 'Filtreleri değiştirin veya pazaryeri senkronizasyonunu bekleyin.'));
      $$('#order-list [data-order]').forEach((r) => r.addEventListener('click', () => showOrder(r.dataset.order)));
    };
    await load(f.page);
  },
};

// Kargo planı: backend'de Türkiye saatiyle hesaplanan TAHMİN (sipariş durumunu değiştirmez)
const SHIP_TONE = { today: 'tone-good', tomorrow: 'tone-info', later: 'tone-info', past: 'tone-warn', unknown: 'tone-warn', no_rule: '', no_date: '' };
function shipPlanBadge(sp) {
  if (!sp || sp.code === 'not_applicable') return html`<span class="muted">—</span>`;
  return html`<span class="badge ship-plan ${SHIP_TONE[sp.code] || ''}" title="${[sp.rule_note, sp.weekend_note, 'TrendHub tahmini'].filter(Boolean).join(' · ')}">${sp.label}</span>`;
}
function shipPlanCard(sp) {
  if (!sp || sp.code === 'not_applicable') return '';
  const tone = { today: 'info', tomorrow: 'info', later: 'info', past: 'warn', unknown: 'warn' }[sp.code] || '';
  return html`<div class="notice ${tone} ship-plan-card" style="margin-bottom:16px">
    <div class="row"><b class="ship-plan-title">Kargo planı: ${sp.label}</b> ${TAHMINI}</div>
    <span class="small">${sp.order_time_tr ? `Sipariş saati (TR): ${sp.order_time_tr}. ` : ''}${sp.rule_note || ''}</span>
    ${sp.code === 'unknown' ? html`<span class="small" style="display:block"><b>11:00–11:59 arası</b> için doğrulanmış Çanta Bayim kuralı olmadığından kargo günü belirlenmedi.</span>` : ''}
    ${sp.weekend_note ? html`<span class="small" style="display:block">${sp.weekend_note}</span>` : ''}
    <span class="small muted" style="display:block">TrendHub'ın kendi tahminidir; sipariş durumunu değiştirmez, pazaryerine veya tedarikçiye bildirilmez.</span></div>`;
}
async function showOrder(id) {
  let o;
  try { o = await api(`/api/orders/${id}`); } catch (e) { fail(e); return; }
  const kinds = [['commission', 'Komisyon'], ['service_fee', 'Hizmet bedeli'], ['shipping', 'Kargo'], ['advertising', 'Reklam'], ['refund', 'İade'], ['other', 'Diğer']];
  const t = { revenue: o.gross_revenue, product_cost: o.product_cost, commission: o.commission, service_fee: o.service_fee,
    shipping: o.shipping_cost, advertising: o.advertising_cost, refund: o.refund_cost, other: o.other_cost };
  const body = openDrawer(html`
    <h2>Sipariş ${o.external_order_id}</h2>
    <div class="row" style="margin-bottom:16px">${statusBadge(o.internal_status, o.status_label)}<span class="badge plain">${o.marketplace_name || '—'}</span>
      ${o.status ? html`<span class="muted small">Pazaryeri durumu: ${o.status}</span>` : ''}</div>
    ${o.review_reason ? html`<div class="notice bad" style="margin-bottom:16px">${o.review_reason}</div>` : ''}
    ${o.storefront ? html`<div class="card" style="margin-bottom:16px"><h3>Web siparişi ${o.storefront.public_code}</h3><dl class="kv mt">
      <dt>Ödeme</dt><dd>${SF_PAY[o.storefront.payment_method] || o.storefront.payment_method} · ${o.storefront.status_label}${o.storefront.paid_at ? ` (${dateTime(o.storefront.paid_at)})` : ''}</dd>
      <dt>Müşteriden alınan</dt><dd>${money(o.storefront.total)} (kargo/hizmet ${money(o.storefront.shipping_fee)})</dd>
      ${o.storefront.address ? html`<dt>Teslimat</dt><dd>${o.storefront.full_name}<br>${o.storefront.address}<br>${o.storefront.district} / ${o.storefront.city} ${o.storefront.postal_code || ''}</dd>
        <dt>İletişim</dt><dd>${o.storefront.phone} · ${o.storefront.email}</dd>
        <dt>Fatura</dt><dd>${o.storefront.billing?.type === 'corporate' ? `${o.storefront.billing.company} · ${o.storefront.billing.tax_office} / ${o.storefront.billing.tax_number}` : 'Bireysel'}</dd>` : ''}
      ${o.storefront.customer_note ? html`<dt>Müşteri notu</dt><dd>${o.storefront.customer_note}</dd>` : ''}</dl>
      <p class="small muted mt">Bu sipariş Trendçantanız web sitesinden geldi. Tedarik ve kargo süreci TrendHub'dan yönetilir; pazaryerine gönderilmez.</p>
      ${o.storefront.supplier_orders ? html`<h3 class="mt">Tedarikçiye aktarım</h3>${o.storefront.supplier_orders.length
        ? html`<ul class="timeline mt">${o.storefront.supplier_orders.map((x) => html`<li><b>${x.supplier_name || 'Tedarikçi'}</b> · ${x.status_label}${x.external_supplier_order_id ? ` · ${x.external_supplier_order_id}` : ''}${x.last_error ? html`<br><span class="small neg">${x.last_error}</span>` : ''}</li>`)}</ul>
          <a class="small" href="#/storefront?tab=supplier">Aktarım listesine git →</a>`
        : html`<p class="small muted">Henüz tedarikçi taslağı yok.</p>${can('operator') ? html`<button class="btn btn-sm mt" data-sfprep="${o.id}">Tedarikçi siparişi taslağı hazırla</button>` : ''}`}` : ''}</div>` : ''}
    ${shipPlanCard(o.shipping_plan)}
    <div class="grid grid-2">
      <div class="card"><h3>Bilgiler</h3><dl class="kv mt">
        <dt>Sipariş tarihi</dt><dd>${dateTime(o.order_date)}</dd><dt>Müşteri</dt><dd>${o.customer_name || '—'} ${o.customer_city ? `(${o.customer_city})` : ''}</dd>
        <dt>Mağaza</dt><dd>${o.store_name || '—'}</dd><dt>Son senkron</dt><dd>${dateTime(o.last_synced_at)}</dd><dt>Kaynak</dt><dd>${o.source || '—'}</dd></dl></div>
      <div class="card"><h3>Kârlılık ${TAHMINI}</h3><div class="mt">${profitBars({ ...t, discount: o.discount }, { net: o.net_profit, tax: o.tax_estimate, netAfterTax: o.net_profit_after_tax })}</div>
        <dl class="kv small mt"><dt>Net satış</dt><dd>${money(o.net_sales)}</dd><dt>KDV öncesi kâr</dt><dd>${money(o.profit_before_vat)}</dd>
          <dt>Tahmini KDV</dt><dd>${o.vat_known ? money(o.vat_estimate) : 'hesaplanamadı'}</dd><dt><b>Tahmini KDV sonrası kâr</b></dt><dd><b>${o.vat_known ? money(o.profit_after_vat) : '—'}</b></dd>
          <dt title="kâr ÷ net satış">Net marj</dt><dd>${pct(o.margin_after_vat ?? o.margin_before_vat)}</dd>
          <dt title="kâr ÷ ürün maliyeti">Maliyet üzeri kâr (Markup)</dt><dd>${pct(o.markup_after_vat ?? o.markup_before_vat)}</dd></dl>
        ${vatNote(o.finance_config)}${o.discount && Number(o.discount) ? html`<p class="small muted">Satıcı indirimi ${money(o.discount)} ciroya yansımıştır; ayrıca düşülmez.</p>` : ''}</div>
    </div>
    <div class="card mt"><h3>Ürünler</h3><div class="table-wrap mt"><table><thead><tr><th>SKU / Ürün</th><th class="r">Adet</th><th class="r">Birim fiyat</th><th class="r">Birim maliyet</th><th class="r">Komisyon</th><th class="r">Kargo</th><th class="r" title="Tahmini KDV sonrası">KDV sonrası kâr</th><th class="r">Net marj</th></tr></thead><tbody>
      ${o.items.map((i) => html`<tr><td><b>${i.sku || i.barcode || '—'}</b><span class="ellipsis small muted">${i.product_name}</span></td><td class="r num">${num(i.quantity)}</td>
        <td class="r num">${money(i.unit_price)}</td><td class="r num">${Number(i.unit_cost) ? money(i.unit_cost) : html`<span class="badge plain tone-warn">Eksik</span>`}</td>
        <td class="r num">${money(i.commission)}</td><td class="r num">${money(i.shipping_cost)}</td><td class="r num ${signClass(i.profit_after_vat)}">${money(i.profit_after_vat)}</td><td class="r num">${pct(i.margin_after_vat)}</td></tr>`)}
      </tbody></table></div>${o.items.length ? '' : html`<p class="muted small">Bu kayıt eski sistemden geldi; kalem bilgisi yok. Tutarlar olduğu gibi korunuyor.</p>`}</div>
    <div class="grid grid-2 mt">
      <div class="card"><h3>Kargo</h3>${o.shipments.length ? html`<ul class="timeline mt">${o.shipments.map((s) => html`<li><b>${s.carrier || 'Kargo firması yok'}</b> · ${s.tracking_url ? html`<a href="${s.tracking_url}" target="_blank" rel="noopener noreferrer">${s.tracking_number || 'Takip'}</a>` : (s.tracking_number || '—')}<br><span class="muted small">${[`Paket ${s.external_package_id || s.id}`, s.marketplace_status || s.status, `Ücret: ${s.cost === null ? 'bilinmiyor' : money(s.cost)}`].filter(Boolean).join(' · ')}</span></li>`)}</ul>` : html`<p class="muted small mt">Sevkiyat kaydı yok.</p>`}</div>
      <div class="card"><h3>Durum geçmişi</h3>${o.history.length ? html`<ul class="timeline mt">${o.history.map((h) => html`<li><b>${h.to_label}</b> <span class="muted small">${dateTime(h.changed_at)} · ${h.source === 'sync' ? 'Senkronizasyon' : h.username || 'Manuel'}</span>${h.note ? html`<br><span class="small">${h.note}</span>` : ''}</li>`)}</ul>` : html`<p class="muted small mt">Geçmiş yok.</p>`}</div>
    </div>
    ${can('operator') ? html`<div class="grid grid-2 mt">
      <form class="card" id="status-form"><h3>Durumu değiştir</h3><div class="stack mt">
        ${o.allowed_transitions.length ? html`<select name="status" required><option value="">Yeni durum seçin…</option>${o.allowed_transitions.map((s) => html`<option value="${s.code}">${s.label}</option>`)}</select>
        <input name="note" placeholder="Not (isteğe bağlı)" maxlength="500"><p class="form-error"></p><button class="btn btn-primary" type="submit">Kaydet</button>` : html`<p class="muted small">Bu durumdan manuel geçiş yok.</p>`}
        <p class="small muted">Bu değişiklik yalnızca TrendHub'da kaydedilir; pazaryerine veya tedarikçiye gönderilmez.</p></div></form>
      <form class="card" id="adj-form"><h3>Gerçek gider gir</h3><div class="stack mt">
        <select name="kind">${kinds.map(([v, l]) => html`<option value="${v}">${l}</option>`)}</select>
        <select name="order_item_id"><option value="">Siparişin tamamı</option>${o.items.map((i) => html`<option value="${i.id}">${i.sku || i.product_name}</option>`)}</select>
        <input name="amount" type="number" step="0.01" placeholder="Tutar (₺)" required>
        <input name="description" placeholder="Açıklama (ör. hakediş ekstresi)" maxlength="300"><p class="form-error"></p>
        <button class="btn" type="submit">Ekle ve yeniden hesapla</button></div></form></div>` : ''}
    ${o.transactions.length ? html`<div class="card mt"><h3>Finans kayıtları</h3><div class="table-wrap mt"><table><thead><tr><th>Tarih</th><th>Tür</th><th>Kaynak</th><th>Açıklama</th><th class="r">Tutar</th></tr></thead><tbody>
      ${o.transactions.map((x) => html`<tr><td>${dateTime(x.occurred_at)}</td><td>${state.meta.finance_components[x.kind] || x.kind}</td><td>${x.source}</td><td>${x.description || ''}</td><td class="r num">${money(x.amount)}</td></tr>`)}</tbody></table></div></div>` : ''}
  `);
  $('#status-form select', body) && $('#status-form', body).addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(e.target, async () => {
      const r = await api(`/api/orders/${id}/status`, { method: 'POST', body: formData(e.target) });
      toast(`Durum güncellendi: ${r.label}`); showOrder(id); refresh();
    });
  });
  $('#adj-form', body)?.addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(e.target, async () => {
      const d = formData(e.target);
      await api(`/api/orders/${id}/adjustments`, { method: 'POST', body: { ...d, order_item_id: d.order_item_id ? Number(d.order_item_id) : null } });
      toast('Gider eklendi, kâr yeniden hesaplandı'); showOrder(id);
    });
  });
  $('[data-sfprep]', body)?.addEventListener('click', async () => {
    try { await api(`/api/storefront/orders/${id}/supplier/prepare`, { method: 'POST' }); toast('Tedarikçi taslağı hazırlandı'); showOrder(id); } catch (e) { fail(e); }
  });
}

// ---- Ürün & Stok
const productTabs = (active) => html`<div class="seg" role="tablist" style="margin-bottom:16px">
  <button role="tab" class="${active === 'catalog' ? 'on' : ''}" aria-selected="${active === 'catalog'}" data-ptab="#/products">Ürün kataloğu</button>
  <button role="tab" class="${active === 'listings' ? 'on' : ''}" aria-selected="${active === 'listings'}" data-ptab="#/products?tab=listings">Pazaryeri ilanları</button></div>`;
const bindProductTabs = () => $$('[data-ptab]').forEach((b) => b.addEventListener('click', () => { location.hash = b.dataset.ptab; }));

PAGES.products = {
  title: 'Ürün & Stok', icon: 'products',
  async render(params) {
    if (params.get('tab') === 'listings') return renderListings(params);
    if (params.get('id')) setTimeout(() => showLifecycle(Number(params.get('id'))));
    const f = { q: params.get('q') || '', low_stock: params.get('low_stock') === '1', missing_cost: params.get('missing_cost') === '1' };
    setHeader('Ürün & Stok', 'Ürün kataloğu, maliyet geçmişi ve stok', can('operator') ? html`<button class="btn btn-primary" id="add-product">+ Ürün ekle</button>` : '');
    view().innerHTML = renderVal(html`${productTabs('catalog')}<div class="grid grid-4" id="prod-summary"></div><div class="card mt">
      <form class="filters" id="prod-filters"><input type="search" name="q" placeholder="SKU, barkod veya ürün adı" value="${f.q}" aria-label="Ara">
      <label class="check"><input type="checkbox" name="low_stock" value="1" ${raw(f.low_stock ? 'checked' : '')}>Düşük stok</label>
      <label class="check"><input type="checkbox" name="missing_cost" value="1" ${raw(f.missing_cost ? 'checked' : '')}>Maliyeti eksik</label>
      <button class="btn btn-primary" type="submit">Filtrele</button></form><div id="prod-list"></div></div>
      <p class="small muted">Stok değişiklikleri yalnızca TrendHub'da tutulur; pazaryerlerine stok gönderimi kapalıdır.</p>`);
    $('#prod-filters').addEventListener('submit', (e) => {
      e.preventDefault();
      const q = new URLSearchParams(Object.entries(formData(e.target)).filter(([, v]) => v));
      location.hash = '#/products' + (q.toString() ? '?' + q : '');
    });
    bindProductTabs();
    $('#add-product')?.addEventListener('click', () => productForm());
    const load = async (page) => {
      const d = await api('/api/products', { query: { q: f.q, low_stock: f.low_stock ? 'true' : '', missing_cost: f.missing_cost ? 'true' : '', page, page_size: 25 } });
      $('#prod-summary').innerHTML = renderVal(html`
        <div class="card kpi"><div class="label">Ürün</div><div class="value">${num(d.summary.total)}</div></div>
        <div class="card kpi"><div class="label">Düşük stok (≤ ${d.low_stock_threshold})</div><div class="value ${d.summary.low_stock ? 'neg' : ''}">${num(d.summary.low_stock)}</div></div>
        <div class="card kpi"><div class="label">Maliyeti eksik</div><div class="value ${d.summary.missing_cost ? 'neg' : ''}">${num(d.summary.missing_cost)}</div><div class="sub">Kâr hesabı eksik kalır</div></div>
        <div class="card kpi"><div class="label">Stok değeri (maliyet)</div><div class="value">${money0(d.summary.stock_value)}</div></div>`);
      $('#prod-list').innerHTML = renderVal(d.items.length ? html`<div class="table-wrap"><table><thead><tr><th>SKU</th><th>Ürün</th><th>Barkod</th><th class="r">Stok</th><th class="r">Satış (30g)</th><th class="r">Maliyet</th><th class="r">Satış fiyatı</th><th></th></tr></thead><tbody>
        ${d.items.map((p) => html`<tr><td><b>${p.sku || '—'}</b></td><td><span class="ellipsis">${p.name}</span>${p.is_active === false ? html`<span class="badge plain">Pasif</span>` : ''}</td><td>${p.barcode || '—'}</td>
          <td class="r num ${p.is_low_stock ? 'neg' : ''}">${num(p.stock)}</td><td class="r num">${num(p.sold_30d)}</td>
          <td class="r num">${Number(p.cost) ? money(p.cost) : html`<span class="badge plain tone-warn">Eksik</span>`}</td><td class="r num">${money(p.sale_price)}</td>
          <td class="r nowrap-cell"><button class="btn btn-sm" data-life="${p.id}">Detay</button> <button class="btn btn-sm" data-offers="${p.id}">Tedarikçiler</button> ${can('operator') ? html`<button class="btn btn-sm" data-cost="${p.id}">Maliyet</button> <button class="btn btn-sm" data-edit="${p.id}">Düzenle</button>` : html`<button class="btn btn-sm" data-cost="${p.id}">Geçmiş</button>`}</td></tr>`)}
        </tbody></table></div>${pager(d, load)}` : empty('Ürün yok', can('operator') ? 'Ürün ekleyerek maliyet takibine başlayın. Sipariş kalemleri SKU/barkod ile otomatik eşlenir.' : 'Henüz ürün tanımlanmamış.'));
      $$('[data-cost]').forEach((b) => b.addEventListener('click', () => costForm(d.items.find((p) => String(p.id) === b.dataset.cost))));
      $$('[data-edit]').forEach((b) => b.addEventListener('click', () => productForm(d.items.find((p) => String(p.id) === b.dataset.edit))));
      $$('[data-offers]').forEach((b) => b.addEventListener('click', () => showOffers(Number(b.dataset.offers))));
      $$('[data-life]').forEach((b) => b.addEventListener('click', () => showLifecycle(Number(b.dataset.life))));
    };
    await load(1);
  },
};
async function renderListings(params) {
  const f = { q: params.get('q') || '', marketplace: params.get('marketplace') || '', unlinked: params.get('unlinked') === '1',
    stock_mismatch: params.get('stock_mismatch') === '1' };
  setHeader('Ürün & Stok', 'Pazaryerlerindeki ilanlar (salt okunur)', can('operator') ? html`<button class="btn" id="import-listings">Bağlanmamış ilanlardan ürün oluştur</button>` : '');
  const mps = await api('/api/marketplaces');
  view().innerHTML = renderVal(html`${productTabs('listings')}
    <div class="notice info" style="margin-bottom:16px">Bu liste pazaryerinden <b>yalnızca okunur</b>. TrendHub pazaryerine stok veya fiyat göndermez; yerel stok da bu senkronla değişmez.</div>
    <div class="grid grid-4" id="lst-summary"></div>
    <div class="card mt"><form class="filters" id="lst-filters">
      <input type="search" name="q" placeholder="SKU, barkod veya başlık" value="${f.q}" aria-label="Ara">
      <select name="marketplace" aria-label="Pazaryeri"><option value="">Tüm pazaryerleri</option>${mps.map((m) => html`<option value="${m.code}" ${raw(m.code === f.marketplace ? 'selected' : '')}>${m.name}</option>`)}</select>
      <label class="check"><input type="checkbox" name="unlinked" value="1" ${raw(f.unlinked ? 'checked' : '')}>Ürüne bağlanmamış</label>
      <label class="check"><input type="checkbox" name="stock_mismatch" value="1" ${raw(f.stock_mismatch ? 'checked' : '')}>Stok farkı olan</label>
      <input type="hidden" name="tab" value="listings">
      <button class="btn btn-primary" type="submit">Filtrele</button></form><div id="lst-list"><div class="skeleton">Yükleniyor…</div></div></div>`);
  bindProductTabs();
  $('#lst-filters').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = new URLSearchParams(Object.entries(formData(e.target)).filter(([, v]) => v));
    location.hash = '#/products?' + q;
  });
  $('#import-listings')?.addEventListener('click', async (e) => {
    if (!confirm('SKU\'su olan ve yerel ürüne bağlanmamış ilanlar için TrendHub\'da ürün kartı oluşturulsun mu? (Maliyet boş başlar; pazaryerine hiçbir şey gönderilmez.)')) return;
    e.target.disabled = true;
    try { const r = await api('/api/listings/import-products', { method: 'POST' }); toast(`${r.created} ürün kartı oluşturuldu`); refresh(); } catch (ex) { fail(ex); } finally { e.target.disabled = false; }
  });
  const load = async (page) => {
    const d = await api('/api/listings', { query: { q: f.q, marketplace: f.marketplace, unlinked: f.unlinked ? 'true' : '', stock_mismatch: f.stock_mismatch ? 'true' : '', page, page_size: 25 } });
    $('#lst-summary').innerHTML = renderVal(html`
      <div class="card kpi"><div class="label">İlan</div><div class="value">${num(d.summary.total)}</div></div>
      <div class="card kpi"><div class="label">Satışta</div><div class="value">${num(d.summary.on_sale)}</div></div>
      <div class="card kpi"><div class="label">Ürüne bağlanmamış</div><div class="value ${d.summary.unlinked ? 'neg' : ''}">${num(d.summary.unlinked)}</div><div class="sub">Maliyet ve kâr takibi için bağlayın</div></div>
      <div class="card kpi"><div class="label">Son senkron</div><div class="value small">${dateTime(d.summary.last_synced_at)}</div></div>`);
    const tone = { on_sale: 'tone-good', not_on_sale: '', archived: '', pending: 'tone-warn', rejected: 'tone-bad' };
    $('#lst-list').innerHTML = renderVal(d.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Pazaryeri</th><th>SKU / Barkod</th><th>Başlık</th><th>Durum</th><th class="r">Fiyat</th><th class="r">Pazaryeri stoğu</th><th class="r">Yerel stok</th><th class="r">Maliyet</th><th class="r">Brüt marj*</th></tr></thead><tbody>
      ${d.items.map((l) => html`<tr><td>${l.marketplace_name}</td><td><b>${l.sku || '—'}</b><span class="muted small ellipsis">${l.barcode || ''}</span></td>
        <td><span class="ellipsis">${l.title}</span>${l.product_id ? '' : html`<span class="badge plain tone-warn">Bağlanmamış</span>`}</td><td><span class="badge ${tone[l.status] || ''}">${l.status_label || '—'}</span></td>
        <td class="r num">${money(l.listed_price)}</td><td class="r num">${num(l.listed_stock)}</td>
        <td class="r num ${l.product_id && Number(l.local_stock) !== Number(l.listed_stock) ? 'neg' : ''}">${l.product_id ? num(l.local_stock) : '—'}</td>
        <td class="r num">${l.local_cost && Number(l.local_cost) ? money(l.local_cost) : '—'}</td><td class="r num">${pct(l.gross_margin_hint)}</td></tr>`)}
      </tbody></table></div>${pager(d, load)}<p class="small muted">* Brüt marj = (fiyat − maliyet) / fiyat; komisyon, kargo ve diğer giderler hariç kaba göstergedir.</p>`
      : empty('İlan yok', 'Entegrasyonlar sayfasından “İlanları senkronize et” ile pazaryeri ilanları okunur. Pazaryeri bağlı değilse liste boş kalır.'));
  };
  await load(1);
}

function productForm(p) {
  const body = openModal(html`<h2>${p ? 'Ürünü düzenle' : 'Yeni ürün'}</h2><form class="form-grid" id="pform">
    ${p ? '' : html`<label>SKU<input name="sku" required maxlength="100"></label>`}
    <label class="${p ? 'full' : ''}">Ürün adı<input name="name" required maxlength="300" value="${p?.name || ''}"></label>
    <label>Barkod<input name="barcode" maxlength="100" value="${p?.barcode || ''}"></label>
    <label>Marka<input name="brand" value="${p?.brand || ''}"></label>
    <label>Stok<input name="stock" type="number" step="1" value="${p?.stock ?? 0}"></label>
    <label>Satış fiyatı (₺)<input name="sale_price" type="number" step="0.01" min="0" value="${p?.sale_price ?? 0}"></label>
    <label>KDV (%)<input name="vat_rate" type="number" step="1" min="0" max="100" value="${p?.vat_rate ?? 20}"></label>
    ${p ? html`<label class="check"><input type="checkbox" name="is_active" value="1" ${raw(p.is_active !== false ? 'checked' : '')}>Aktif</label>` : html`<label>Birim maliyet (₺)<input name="cost" type="number" step="0.01" min="0" value="0"></label>`}
    <p class="form-error full"></p><div class="full row"><span class="spacer"></span><button class="btn" type="button" data-close>Vazgeç</button><button class="btn btn-primary" type="submit">Kaydet</button></div></form>`);
  $('#pform', body).addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(e.target, async () => {
      const d = formData(e.target);
      const payload = { name: d.name, barcode: d.barcode || null, brand: d.brand || null, stock: Number(d.stock || 0), sale_price: d.sale_price || '0', vat_rate: d.vat_rate || '20' };
      if (p) await api(`/api/products/${p.id}`, { method: 'PATCH', body: { ...payload, is_active: d.is_active === '1' } });
      else await api('/api/products', { method: 'POST', body: { ...payload, sku: d.sku, cost: d.cost || '0' } });
      closeLayer('modal'); toast('Ürün kaydedildi'); refresh();
    });
  });
}
async function costForm(p) {
  const hist = await api(`/api/products/${p.id}/costs`).catch(() => []);
  const body = openModal(html`<h2>Maliyet · ${p.sku}</h2>
    ${can('operator') ? html`<form class="stack" id="cform"><label>Yeni birim maliyet (₺)<input name="cost" type="number" step="0.01" min="0" required></label>
      <label>Geçerlilik başlangıcı<input name="valid_from" type="date"></label><label>Not<input name="note" maxlength="300"></label>
      <p class="small muted">Geçmiş siparişlerin maliyeti değişmez; yalnızca maliyeti hiç girilmemiş kalemler yeniden hesaplanır.</p>
      <p class="form-error"></p><button class="btn btn-primary" type="submit">Kaydet</button></form>` : ''}
    <h3 class="mt">Geçmiş</h3>${hist.length ? html`<div class="table-wrap"><table><tbody>${hist.map((h) => html`<tr><td>${dateTime(h.valid_from)}</td><td class="r num">${money(h.cost)}</td><td class="muted small">${h.username || h.source}${h.note ? ' · ' + h.note : ''}</td></tr>`)}</tbody></table></div>` : html`<p class="muted small">Kayıt yok.</p>`}`);
  $('#cform', body)?.addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(e.target, async () => {
      const d = formData(e.target);
      const r = await api(`/api/products/${p.id}/cost`, { method: 'POST', body: { cost: d.cost, note: d.note || null, valid_from: d.valid_from ? d.valid_from + 'T00:00:00+03:00' : null } });
      closeLayer('modal'); toast(`Maliyet kaydedildi · ${r.recalculated_orders} sipariş yeniden hesaplandı`); refresh();
    });
  });
}

// ---- Kargo
PAGES.shipping = {
  title: 'Kargo', icon: 'shipping',
  async render(params) {
    const f = { status: params.get('status') || '', carrier: params.get('carrier') || '', q: params.get('q') || '' };
    setHeader('Kargo', 'Paketler, takip numaraları ve kargo maliyetleri');
    view().innerHTML = '<div class="grid grid-4" id="ship-summary"></div><div class="card mt" id="ship-card"><div class="skeleton">Yükleniyor…</div></div>';
    const load = async (page) => {
      const d = await api('/api/shipments', { query: { ...f, page, page_size: 25 } });
      $('#ship-summary').innerHTML = renderVal(['awaiting_shipment', 'shipped', 'delivered', 'returned'].map((k) => html`
        <a class="card kpi" href="#/shipping?status=${k}"><div class="label">${statusLabel(k)}</div><div class="value">${num(d.summary[k] || 0)}</div></a>`));
      $('#ship-card').innerHTML = renderVal(html`<form class="filters" id="ship-filters">
        <input type="search" name="q" placeholder="Takip no veya sipariş no" value="${f.q}" aria-label="Ara">
        <select name="status" aria-label="Durum"><option value="">Tüm durumlar</option>${state.meta.statuses.map((s) => html`<option value="${s.code}" ${raw(s.code === f.status ? 'selected' : '')}>${s.label}</option>`)}</select>
        <select name="carrier" aria-label="Kargo firması"><option value="">Tüm firmalar</option>${d.carriers.map((c) => html`<option ${raw(c === f.carrier ? 'selected' : '')}>${c}</option>`)}</select>
        <button class="btn btn-primary" type="submit">Filtrele</button></form>
        ${d.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Sipariş</th><th>Pazaryeri</th><th>Kargo</th><th>Takip no</th><th>Durum</th><th class="r">Desi</th><th class="r">Ücret</th><th>Güncelleme</th></tr></thead><tbody>
        ${d.items.map((s) => html`<tr><td><a href="#" data-order="${s.order_id}">${s.external_order_id}</a></td><td>${s.marketplace_name || '—'}</td><td>${s.carrier || '—'}</td>
          <td>${s.tracking_url ? html`<a href="${s.tracking_url}" target="_blank" rel="noopener noreferrer">${s.tracking_number || 'Takip'}</a>` : (s.tracking_number || '—')}</td>
          <td>${statusBadge(s.internal_status, s.status_label)}</td><td class="r num">${s.desi ?? '—'}</td>
          <td class="r num">${s.cost === null ? html`<span class="muted">—</span>` : money(s.cost)} ${can('operator') ? html`<button class="btn btn-sm" data-shipcost="${s.id}" data-current="${s.cost ?? ''}">✎</button>` : ''}</td><td>${dateTime(s.updated_at)}</td></tr>`)}
        </tbody></table></div>${pager(d, load)}` : empty('Sevkiyat yok', 'Sipariş senkronizasyonu ile paketler burada görünür.')}
        <p class="small muted">Kargo ücreti bilinmeyen siparişlerde Ayarlar'daki varsayılan kargo ücreti tahmini olarak kullanılır.</p>`);
      $('#ship-filters').addEventListener('submit', (e) => {
        e.preventDefault();
        const q = new URLSearchParams(Object.entries(formData(e.target)).filter(([, v]) => v));
        location.hash = '#/shipping' + (q.toString() ? '?' + q : '');
      });
      $$('[data-order]').forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); showOrder(a.dataset.order); }));
      $$('[data-shipcost]').forEach((b) => b.addEventListener('click', () => {
        const body = openModal(html`<h2>Kargo ücreti</h2><form class="stack" id="scf"><label>Ücret (₺)<input name="cost" type="number" step="0.01" min="0" value="${b.dataset.current}" required></label><p class="form-error"></p><button class="btn btn-primary" type="submit">Kaydet ve yeniden hesapla</button></form>`);
        $('#scf', body).addEventListener('submit', (e) => {
          e.preventDefault();
          submitting(e.target, async () => { await api(`/api/shipments/${b.dataset.shipcost}`, { method: 'PATCH', body: formData(e.target) }); closeLayer('modal'); toast('Kargo ücreti kaydedildi'); load(page); });
        });
      }));
    };
    await load(1);
  },
};

// ---- Tedarikçiler
// ---- Tedarikçiler (genel tedarikçi mimarisi: XML / API / CSV / manuel)
const INTEGRATION_LABELS = { xml: 'XML', api: 'API (JSON)', csv: 'CSV', manual: 'Manuel / dosya', external: 'Harici sistem' };
const HEALTH = { ok: ['good', 'Senkron başarılı'], warning: ['warn', 'Uyarılı'], error: ['bad', 'Hata'], never: ['', 'Henüz senkron yok'], inactive: ['', 'Pasif'] };
const RUN_STATUS = { running: ['info', 'Çalışıyor'], success: ['good', 'Başarılı'], partial: ['warn', 'Kısmi'], failed: ['bad', 'Başarısız'] };
const INTERVALS = [[0, 'Kapalı (yalnızca elle)'], [15, '15 dakika'], [30, '30 dakika'], [60, '1 saat'], [180, '3 saat'], [360, '6 saat'], [720, '12 saat'], [1440, '24 saat']];
const healthBadge = (h) => { const [tone, label] = HEALTH[h] || ['', h]; return html`<span class="badge ${tone ? 'tone-' + tone : ''}">${label}</span>`; };
const runBadge = (s) => { const [tone, label] = RUN_STATUS[s] || ['', s || '—']; return html`<span class="badge tone-${tone}">${label}</span>`; };
async function supplierMeta() {
  if (!state.supplierMeta) state.supplierMeta = await api('/api/supplier-meta');
  return state.supplierMeta;
}
function supplierHealthTable(items, { compact = false } = {}) {
  return html`<div class="table-wrap"><table><thead><tr><th>Tedarikçi</th>${compact ? '' : html`<th>Entegrasyon</th>`}<th class="r">Ürün</th><th class="r">Stokta</th><th class="r">Stoksuz</th><th class="r">Kaynağında yok</th><th>Son senkron</th><th>Durum</th></tr></thead><tbody>
    ${items.map((s) => html`<tr class="click" data-supplier="${s.id}"><td><b>${s.name}</b>${compact ? '' : html`<span class="muted small">${s.code}</span>`}</td>
      ${compact ? '' : html`<td>${INTEGRATION_LABELS[s.integration_type] || s.integration_type}</td>`}
      <td class="r num">${num(s.active_count ?? s.product_count)}</td><td class="r num">${num(s.in_stock_count)}</td>
      <td class="r num ${Number(s.out_of_stock_count) ? 'warn-text' : ''}">${num(s.out_of_stock_count)}</td>
      <td class="r num ${Number(s.missing_count) ? 'neg' : ''}">${num(s.missing_count)}</td>
      <td>${dateTime(s.last_sync_at)}</td>
      <td>${healthBadge(s.health)}${s.last_sync_error ? html`<span class="ellipsis small neg" title="${s.last_sync_error}">${s.last_sync_error}</span>` : ''}</td></tr>`)}
    </tbody></table></div>`;
}
const bindSupplierRows = (root = document) => $$('[data-supplier]', root).forEach((r) => r.addEventListener('click', () => { location.hash = `#/suppliers?id=${r.dataset.supplier}`; }));

PAGES.suppliers = {
  title: 'Tedarikçiler', icon: 'suppliers',
  async render(params) {
    if (params.get('id')) return renderSupplierDetail(Number(params.get('id')), params.get('tab') || 'overview');
    const sview = params.get('view') || 'list';
    setHeader('Tedarikçiler', 'Tüm tedarikçiler, ürün havuzu ve senkronizasyon durumu', html`
      <a class="btn" href="#/transfer">Ürün aktarımı →</a>${can('admin') ? html` <button class="btn btn-primary" id="add-sup">+ Yeni Tedarikçi Ekle</button>` : ''}`);
    const tabs = html`<div class="seg" role="tablist" style="margin-bottom:16px">${[['list', 'Tedarikçiler'], ['pool', 'Ürün havuzu'], ['compare', 'Tedarikçi karşılaştırma']].map(([k, l]) =>
      html`<button role="tab" class="${k === sview ? 'on' : ''}" aria-selected="${k === sview}" data-sview="${k}">${l}</button>`)}</div>`;
    const bindTabs = () => {
      $$('[data-sview]').forEach((b) => b.addEventListener('click', () => { location.hash = b.dataset.sview === 'list' ? '#/suppliers' : `#/suppliers?view=${b.dataset.sview}`; }));
      $('#add-sup')?.addEventListener('click', () => supplierForm());
    };
    if (sview === 'pool') {
      view().innerHTML = renderVal(html`${tabs}<div id="sup-pool"></div>`);
      bindTabs();
      return renderPool($('#sup-pool'), {});
    }
    if (sview === 'compare') {
      view().innerHTML = renderVal(html`${tabs}<div id="sup-cmp"></div>`);
      bindTabs();
      return renderComparison($('#sup-cmp'));
    }
    const [sups, sos] = await Promise.all([api('/api/suppliers'), api('/api/supplier-orders', { query: { page_size: 25 } })]);
    const sum = (k) => sups.reduce((a, s) => a + (Number(s[k]) || 0), 0);
    view().innerHTML = renderVal(html`${tabs}
      <div class="grid grid-4">
        ${kpi('Tedarikçi', `${num(sups.filter((s) => s.is_active).length)} / ${num(sups.length)}`, 'Aktif / toplam')}
        ${kpi('Havuzdaki ürün', num(sum('active_count')), `${num(sum('linked_count'))} ürün kataloğa bağlı`)}
        ${kpi('Stoksuz ürün', num(sum('out_of_stock_count')), 'Aktif tedarikçi ürünleri', sum('out_of_stock_count') ? 'warn-text' : '')}
        ${kpi('Kaynağında bulunamadı', num(sum('missing_count')), 'Silinmez; pasif işaretlenir', sum('missing_count') ? 'neg' : '')}
      </div>
      <div class="card mt"><div class="card-head"><div><h2>Tedarikçiler</h2><p>Satıra tıklayarak bağlantı, alan eşleştirme ve ürünleri yönetin.</p></div></div>
        ${sups.length ? supplierHealthTable(sups) : empty('Henüz tedarikçi yok', can('admin') ? '“Yeni Tedarikçi Ekle” ile XML, API, CSV veya manuel tedarikçi ekleyin. Çanta Bayim için hazır şablon vardır.' : 'Yönetici bir tedarikçi eklediğinde burada görünür.')}</div>
      <div class="card mt"><div class="card-head"><h2>Tedarikçi siparişleri</h2></div>
        <div class="notice info" style="margin-bottom:12px">Trendyol → Çanta Bayim sipariş otomasyonu ayrı production sunucusunda çalışır ve TrendHub tarafından <b>yönetilmez veya tetiklenmez</b>. TrendHub tedarikçilerden yalnızca ürün/fiyat/stok <b>okur</b>.</div>
        ${sos.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Tarih</th><th>Sipariş</th><th>Tedarikçi</th><th>Tedarikçi sipariş no</th><th>Durum</th><th class="r">Maliyet</th><th>Hata</th></tr></thead><tbody>
        ${sos.items.map((x) => html`<tr><td>${dateTime(x.created_at)}</td><td>${x.external_order_id || '—'}</td><td>${x.supplier_name || '—'}</td><td>${x.external_supplier_order_id || '—'}</td><td>${x.status || '—'}</td><td class="r num">${money(x.cost)}</td><td class="neg small">${x.last_error || ''}</td></tr>`)}</tbody></table></div>`
          : empty('Kayıt yok', 'TrendHub tedarikçiye sipariş aktarmaz.')}</div>`);
    bindSupplierRows();
    bindTabs();
  },
};

async function renderComparison(box) {
  let q = '', onlySavings = false;
  const load = async (page) => {
    const d = await api('/api/supplier-comparison', { query: { q, only_savings: onlySavings ? 'true' : '', page, page_size: 25 } });
    box.innerHTML = renderVal(html`
      <div class="grid grid-3">
        ${kpi('Birden çok tedarikçili ürün', num(d.summary.products), 'Aynı barkod, farklı tedarikçiler')}
        ${kpi('Daha ucuz teklif var', num(d.summary.with_saving), 'Seçili tedarikçiden ucuz, stokta', d.summary.with_saving ? 'warn-text' : '')}
        ${kpi('Uygun teklif yok', num(d.summary.no_usable_offer), 'Hiçbir tedarikçide stok/fiyat yok', d.summary.no_usable_offer ? 'neg' : '')}
      </div>
      <div class="card mt"><form class="filters" id="cmp-f"><input type="search" name="q" value="${q}" placeholder="SKU, barkod veya ürün adı" aria-label="Ara">
        <label class="check"><input type="checkbox" name="only_savings" value="1" ${raw(onlySavings ? 'checked' : '')}>Yalnızca daha ucuz teklifi olanlar</label>
        <button class="btn btn-primary" type="submit">Filtrele</button></form>
      ${d.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Ürün</th><th>Teklifler (alış · stok)</th><th>Seçili tedarikçi</th><th>En ucuz</th><th>En yüksek stok</th><th class="r">Adet başı fark</th><th></th></tr></thead><tbody>
        ${d.items.map((x) => html`<tr><td><b class="ellipsis" title="${x.name}">${x.name}</b><span class="muted small">${x.sku} · ${x.barcode || 'barkod yok'}</span></td>
          <td>${x.offers.map((o) => html`<span class="small ${o.usable ? '' : 'muted'}" style="display:block">${o.supplier_name}: ${o.cost === null ? '—' : money(o.cost)} · ${num(o.stock)} adet</span>`)}</td>
          <td>${x.selected_supplier || html`<span class="muted">seçilmedi</span>`}<span class="muted small">${x.strategy_label}</span></td>
          <td>${x.cheapest_supplier || '—'}<span class="muted small">${x.cheapest_cost === null ? '' : money(x.cheapest_cost)}</span></td>
          <td>${x.highest_stock_supplier || '—'}<span class="muted small">${x.highest_stock === null ? '' : num(x.highest_stock) + ' adet'}</span></td>
          <td class="r num ${Number(x.saving_per_unit) > 0 ? 'warn-text' : ''}">${x.saving_per_unit === null ? '—' : money(x.saving_per_unit)}</td>
          <td class="r"><button class="btn btn-sm" data-offers="${x.id}">Tedarikçi seç</button></td></tr>`)}
        </tbody></table></div>${pager(d, load)}` : empty('Karşılaştırılacak ürün yok', 'Aynı barkodlu ürün birden fazla tedarikçide bulunduğunda burada fiyat ve stok karşılaştırması görünür.')}</div>`);
    $('#cmp-f', box).addEventListener('submit', (e) => { e.preventDefault(); const v = formData(e.target); q = v.q || ''; onlySavings = v.only_savings === '1'; load(1); });
    $$('[data-offers]', box).forEach((b) => b.addEventListener('click', () => showOffers(Number(b.dataset.offers))));
  };
  await load(1);
}

async function supplierForm(detail) {
  const meta = await supplierMeta();
  const s = detail?.supplier, c = detail?.connection || {};
  const rules = s?.stock_rules || {};
  const opt = (pairs, cur) => pairs.map(([v, l]) => html`<option value="${v}" ${raw(String(cur) === String(v) ? 'selected' : '')}>${l}</option>`);
  const body = openDrawer(html`<h2>${s ? 'Tedarikçiyi düzenle' : 'Yeni tedarikçi ekle'}</h2>
    ${meta.secret_storage_ready ? '' : html`<div class="notice warn" style="margin-bottom:12px">Sunucuda şifreleme anahtarı tanımlı değil; kaynak adresi ve şifreler kaydedilemez. Sistem yöneticinize başvurun.<details class="adv"><summary>Gelişmiş detay</summary><code>APP_SECRET ayarı boş (kurulum betiği boşsa otomatik üretir).</code></details></div>`}
    <form class="form-grid" id="supform" autocomplete="off">
      ${s ? '' : html`<label class="full">Şablon<select name="preset"><option value="">Boş tedarikçi</option>${meta.presets.map((p) => html`<option value="${p.key}">${p.name} (${INTEGRATION_LABELS[p.integration_type]})</option>`)}</select>
        <span class="small muted" id="preset-note">Alan eşleştirmesi kaydettikten sonra kaynağı önizleyerek yapılır.</span></label>`}
      <label>Tedarikçi adı<input name="name" required maxlength="200" value="${s?.name || ''}"></label>
      <label>Kod<input name="code" maxlength="50" pattern="[a-z0-9_\\-]*" value="${s?.code || ''}" ${raw(s ? 'readonly' : '')} placeholder="otomatik"></label>
      <label>Entegrasyon türü<select name="integration_type">${opt(Object.entries(meta.integration_types), c.integration_type || 'xml')}</select>
        <span class="small muted" id="conn-desc"></span></label>
      <label>Senkronizasyon sıklığı<select name="sync_interval_minutes">${opt(INTERVALS, s?.sync_interval_minutes ?? 60)}</select></label>
      <label class="full">Tedarikçi fiyatları (alış fiyatı)<select name="price_vat_mode">${opt([['', 'Belirtilmedi — KDV dahil varsayılır (uyarı gösterilir)'], ['included', 'KDV DAHİL'], ['excluded', 'KDV HARİÇ — ürünün KDV oranı eklenerek maliyet hesaplanır']], s?.price_vat_mode || '')}</select>
        <span class="small muted">Finanstaki ürün maliyeti KDV dahil tutardır. Döviz fiyatlar yalnızca Ayarlar'da kur tanımlıysa kullanılır.</span></label>
      <fieldset class="full fieldset" data-remote>
        <legend>Kaynak</legend>
        <label>Kaynak adresi (XML / API / CSV URL)<input name="source_url" type="url" inputmode="url" maxlength="2000" placeholder="${c.has_source_url ? 'Kayıtlı: ' + (c.source_url_display || '') + ' — değiştirmek için yeni adres girin' : 'https://…'}"></label>
        ${c.has_source_url ? html`<label class="check"><input type="checkbox" name="clear_url" value="1">Kayıtlı adresi sil</label>` : ''}
        <p class="small muted">Adres token içerebileceği için şifreli saklanır; ekranda yalnızca maskeli hâli görünür.</p>
        <div class="form-grid">
          <label>Kimlik doğrulama<select name="auth_type">${opt(Object.entries(meta.auth_types), c.auth_type || 'none')}</select></label>
          <label data-auth="basic">Kullanıcı adı<input name="auth_username" maxlength="200" value="${c.auth_username || ''}"></label>
          <label data-auth="header query">Başlık / parametre adı<input name="auth_param_name" maxlength="100" value="${c.auth_param_name || ''}" placeholder="ör. X-Api-Key veya apikey"></label>
          <label data-auth="basic bearer header query">Şifre / token<input name="secret" type="password" autocomplete="new-password" maxlength="4000" placeholder="${c.has_secret ? 'Kayıtlı ✓ — değiştirmek için yazın' : ''}"></label>
          <details class="full adv" data-auth="basic bearer header query"><summary>Gelişmiş: şifreyi sunucu ayarından oku</summary><label>Sunucu ayar adı<input name="secret_env" maxlength="80" pattern="SUPPLIER_[A-Z0-9_]+" value="${c.secret_env || ''}" placeholder="SUPPLIER_ACME_TOKEN"></label></details>
          ${c.has_secret ? html`<label class="check" data-auth="basic bearer header query"><input type="checkbox" name="clear_secret" value="1">Kayıtlı şifreyi sil</label>` : ''}
          <label>Kayıt yolu (isteğe bağlı)<input name="record_path" maxlength="300" value="${c.record_path || ''}" placeholder="ör. Urunler/Urun — boşsa otomatik"></label>
          <label data-api>Sayfa parametresi (isteğe bağlı)<input name="page_param" maxlength="50" value="${c.options?.page_param || ''}" placeholder="ör. page — boşsa tek istek"></label>
          <label data-api>Sayfa boyutu parametresi<input name="page_size_param" maxlength="50" value="${c.options?.page_size_param || ''}" placeholder="ör. limit"></label>
          <label data-api>Sayfa boyutu<input name="page_size" type="number" min="1" max="5000" value="${c.options?.page_size ?? ''}" placeholder="100"></label>
          <label data-api>En fazla sayfa<input name="max_pages" type="number" min="1" max="500" value="${c.options?.max_pages ?? ''}" placeholder="100"></label>
          <label data-csv>CSV ayırıcı<select name="delimiter">${opt([['', 'Otomatik'], [';', 'Noktalı virgül (;)'], [',', 'Virgül (,)'], ['\t', 'Sekme'], ['|', 'Dikey çizgi (|)']], c.options?.delimiter || '')}</select></label>
        </div>
      </fieldset>
      <fieldset class="full fieldset"><legend>Stok kuralları</legend><div class="form-grid">
        <label>Güvenlik payı (adet düşülür)<input name="buffer" type="number" min="0" value="${rules.buffer ?? 0}"></label>
        <label>Minimum stok (altı 0 sayılır)<input name="min_stock" type="number" min="0" value="${rules.min_stock ?? 0}"></label>
        <label>Maksimum stok (üst sınır)<input name="max_stock" type="number" min="0" value="${rules.max_stock ?? ''}" placeholder="sınırsız"></label>
        <label>Öncelik (1 = en yüksek)<input name="priority" type="number" min="1" max="10000" value="${s?.priority ?? 100}"></label>
      </div></fieldset>
      <details class="full"><summary>İletişim ve notlar</summary><div class="form-grid mt">
        <label>Yetkili<input name="contact_name" maxlength="200" value="${s?.contact_name || ''}"></label><label>Telefon<input name="phone" maxlength="50" value="${s?.phone || ''}"></label>
        <label>E-posta<input name="email" type="email" maxlength="200" value="${s?.email || ''}"></label><label>Tedarik süresi (gün)<input name="lead_time_days" type="number" min="0" max="365" value="${s?.lead_time_days ?? ''}"></label>
        <label class="full">Notlar<textarea name="notes" rows="3" maxlength="2000">${s?.notes || ''}</textarea></label></div></details>
      <label class="check full"><input type="checkbox" name="is_active" value="1" ${raw(!s || s.is_active ? 'checked' : '')}>Aktif (pasif tedarikçi otomatik senkronize edilmez)</label>
      <p class="form-error full"></p>
      <div class="full row"><span class="spacer"></span><button class="btn" type="button" data-close>Vazgeç</button><button class="btn btn-primary" type="submit">${s ? 'Kaydet' : 'Tedarikçiyi ekle'}</button></div>
    </form>`);
  const form = $('#supform', body);
  const sync = () => {
    const t = form.integration_type.value, a = form.auth_type.value;
    $('[data-remote]', form).hidden = t === 'manual';
    $('#conn-desc', form).textContent = (meta.connectors || []).find((x) => x.type === t)?.description || '';
    $$('[data-auth]', form).forEach((el) => { el.hidden = !el.dataset.auth.split(' ').includes(a); });
    $$('[data-csv]', form).forEach((el) => { el.hidden = t !== 'csv'; });
    $$('[data-api]', form).forEach((el) => { el.hidden = t !== 'api'; });
  };
  form.integration_type.addEventListener('change', sync);
  form.auth_type.addEventListener('change', sync);
  form.preset?.addEventListener('change', () => {
    const p = meta.presets.find((x) => x.key === form.preset.value);
    if (!p) return;
    const nm = form.elements.namedItem('name');
    if (!nm.value) nm.value = p.name;
    form.integration_type.value = p.integration_type;
    form.sync_interval_minutes.value = p.sync_interval_minutes;
    $('#preset-note', body).textContent = p.note;
    sync();
  });
  sync();
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(form, async () => {
      const d = formData(form);
      const connection = {
        integration_type: d.integration_type, auth_type: d.integration_type === 'manual' ? 'none' : d.auth_type,
        auth_username: d.auth_username || null, auth_param_name: d.auth_param_name || null, secret_env: d.secret_env || null,
        record_path: d.record_path || null, delimiter: d.delimiter || null,
        page_param: d.page_param || null, page_size_param: d.page_size_param || null,
        page_size: d.page_size ? Number(d.page_size) : null, max_pages: d.max_pages ? Number(d.max_pages) : null,
        source_url: d.clear_url ? '' : (d.source_url || null), secret: d.clear_secret ? '' : (d.secret || null),
      };
      const payload = {
        name: d.name, code: d.code || null, contact_name: d.contact_name || null, phone: d.phone || null, email: d.email || null,
        lead_time_days: d.lead_time_days ? Number(d.lead_time_days) : null, notes: d.notes || null, is_active: d.is_active === '1',
        priority: Number(d.priority || 100), sync_interval_minutes: Number(d.sync_interval_minutes || 0),
        stock_rules: { buffer: Number(d.buffer || 0), min_stock: Number(d.min_stock || 0), max_stock: d.max_stock === '' ? null : Number(d.max_stock) },
        connection, preset: d.preset || null, price_vat_mode: d.price_vat_mode || null,
      };
      if (s) {
        await api(`/api/suppliers/${s.id}`, { method: 'PUT', body: payload });
        closeLayer('drawer'); toast('Tedarikçi kaydedildi'); refresh();
      } else {
        const r = await api('/api/suppliers', { method: 'POST', body: payload });
        closeLayer('drawer'); toast('Tedarikçi eklendi — kurulum adımlarını tamamlayın');
        state.supSetup = r.id;
        location.hash = `#/suppliers?id=${r.id}&tab=overview`;
      }
    });
  });
}

const SUP_TABS = [['overview', 'Genel'], ['import', 'İçe aktarma sihirbazı'], ['mapping', 'Alan eşleştirme'], ['products', 'Ürünler'], ['runs', 'Senkron geçmişi'], ['changes', 'Değişiklikler']];
async function renderSupplierDetail(id, tab) {
  const [d, meta] = await Promise.all([api(`/api/suppliers/${id}`), supplierMeta()]);
  const s = d.supplier, c = d.connection;
  const remote = c.integration_type !== 'manual' && c.has_source_url;
  setHeader(s.name, `${INTEGRATION_LABELS[c.integration_type] || c.integration_type} tedarikçi · ${s.code}`, html`
    <a class="btn" href="#/suppliers">← Tedarikçiler</a>
    ${can('operator') && remote ? html`<button class="btn" id="sup-test">Bağlantıyı test et</button> <button class="btn" id="sup-sync">Şimdi senkronize et</button>` : ''}
    ${can('operator') ? html`<label class="btn" for="sup-file">Dosya yükle</label><input type="file" id="sup-file" accept=".xml,.csv,.json,.txt,text/xml,text/csv,application/json" hidden>` : ''}
    ${can('admin') ? html`<button class="btn btn-primary" id="sup-edit">Düzenle</button>` : ''}`);
  view().innerHTML = renderVal(html`
    ${s.last_sync_status === 'failed' ? html`<div class="notice bad" style="margin-bottom:12px"><b>Son senkron başarısız:</b> ${s.last_sync_error}</div>`
      : s.last_sync_status === 'partial' ? html`<div class="notice warn" style="margin-bottom:12px"><b>Son senkron kısmi:</b> ${s.last_sync_error || 'bazı kayıtlar işlenemedi'}</div>` : ''}
    ${Number(s.product_count) === 0 || state.supSetup === id ? html`<div class="card" style="margin-bottom:12px"><div class="card-head"><div><h2>Tedarikçi kurulumu</h2><p>Adımları sırayla tamamlayın. Gelen ürünler önce Ürün Havuzu'na düşer; pazaryerine otomatik gönderilmez.</p></div></div>
      <ol class="steps setup-steps">
        <li class="done">1. Bilgiler</li>
        <li class="${state.supTested?.[id] ? 'done' : 'on'}">2. Bağlantı testi ${c.integration_type === 'manual' ? html`<span class="small muted">(manuel: dosya yükleyeceksiniz)</span>` : can('operator') && remote ? html`<button class="btn btn-sm" id="setup-test">Test et</button>` : html`<span class="small muted">(kaynak adresi yok)</span>`}</li>
        <li class="${remote ? (s.mapping_approved_at ? 'done' : '') : Number(s.mapped_fields) ? 'done' : ''}">3. ${remote ? html`Node seçimi, alan eşleştirme, önizleme ve onay <a class="btn btn-sm" href="#/suppliers?id=${id}&tab=import">İçe aktarma sihirbazı</a>` : html`Alan eşleştirme <a class="btn btn-sm" href="#/suppliers?id=${id}&tab=mapping">Eşleştir</a>`}</li>
        <li class="${Number(s.product_count) ? 'done' : ''}">4. Ürünleri getir ${can('operator') ? (remote ? html`<button class="btn btn-sm btn-primary" id="setup-fetch" ${raw(s.mapping_approved_at ? '' : 'disabled')} title="${s.mapping_approved_at ? '' : 'Önce sihirbazda eşleştirmeyi onaylayın'}">Ürünleri Getir</button>` : html`<label class="btn btn-sm btn-primary" for="sup-file">Dosya seç ve getir</label>`) : ''}</li>
        <li class="${Number(s.product_count) ? 'done' : ''}">5. Ürün havuzu <a class="btn btn-sm" href="#/transfer">Havuzda gör →</a></li>
      </ol></div>` : ''}
    ${remote && !s.mapping_approved_at ? html`<div class="notice warn" style="margin-bottom:12px">Eşleştirme onaylanmadı: kaynaktan aktarım ve zamanlanmış senkron çalışmaz. <a href="#/suppliers?id=${id}&tab=import">İçe aktarma sihirbazını</a> tamamlayın.</div>` : ''}
    ${!remote && !Number(s.mapped_fields) ? html`<div class="notice warn" style="margin-bottom:12px">Alan eşleştirmesi yapılmadı. <b>Alan eşleştirme</b> sekmesinde kaynağı önizleyip en az “Tedarikçi ürün kodu” ve “Ürün adı” alanlarını eşleştirin.</div>` : ''}
    <div class="grid grid-4">
      ${kpi('Ürün (aktif)', num(s.active_count), `${num(s.linked_count)} ürün kataloğa bağlı`)}
      ${kpi('Stokta', num(s.in_stock_count), `Stoksuz: ${num(s.out_of_stock_count)}`)}
      ${kpi('Kaynağında bulunamadı', num(s.missing_count), 'Silinmez, pasif işaretlenir', Number(s.missing_count) ? 'neg' : '')}
      ${kpi('Son senkron', dateTime(s.last_sync_at), '', 'small', healthBadge(s.health))}
    </div>
    <div class="seg mt" role="tablist">${SUP_TABS.map(([k, l]) => html`<button role="tab" class="${k === tab ? 'on' : ''}" aria-selected="${k === tab}" data-stab="${k}">${l}</button>`)}</div>
    <div id="sup-tab" class="mt"></div>`);
  $$('[data-stab]').forEach((b) => b.addEventListener('click', () => { location.hash = `#/suppliers?id=${id}&tab=${b.dataset.stab}`; }));
  $('#sup-edit')?.addEventListener('click', () => supplierForm(d));
  $('#setup-test')?.addEventListener('click', async (e) => {
    e.target.disabled = true;
    try { const r = await api(`/api/suppliers/${id}/test`, { method: 'POST' }); toast(r.ok ? `🟢 ${r.message}` : `🔴 ${r.message}`, !r.ok); if (r.ok) { (state.supTested ||= {})[id] = true; refresh(); } } catch (ex) { fail(ex); } finally { e.target.disabled = false; }
  });
  $('#setup-fetch')?.addEventListener('click', async (e) => {
    e.target.disabled = true;
    try { const r = await api(`/api/suppliers/${id}/sync`, { method: 'POST' }); toast(`${r.message}. Birkaç saniye sonra ürünler havuzda görünecek.`); setTimeout(refresh, 4000); } catch (ex) { fail(ex); e.target.disabled = false; }
  });
  $('#sup-test')?.addEventListener('click', async (e) => {
    e.target.disabled = true;
    try { const r = await api(`/api/suppliers/${id}/test`, { method: 'POST' }); toast(r.message, !r.ok); } catch (ex) { fail(ex); } finally { e.target.disabled = false; }
  });
  $('#sup-sync')?.addEventListener('click', async (e) => {
    e.target.disabled = true;
    try { const r = await api(`/api/suppliers/${id}/sync`, { method: 'POST' }); toast(r.message); } catch (ex) { fail(ex); } finally { e.target.disabled = false; }
  });
  $('#sup-file')?.addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    toast('Dosya işleniyor…');
    try {
      const r = await api(`/api/suppliers/${id}/upload`, { method: 'POST', rawBody: f });
      toast(`Senkron: ${r.created_count} yeni, ${r.price_changed} fiyat, ${r.stock_changed} stok, ${r.missing_count} kayıp${r.error_count ? `, ${r.error_count} hata` : ''}`, r.status === 'failed');
      refresh();
    } catch (ex) { fail(ex); } finally { e.target.value = ''; }
  });
  const box = $('#sup-tab');
  if (tab === 'import') return renderImportWizard(box, id, d);
  if (tab === 'mapping') return renderMapping(box, id, d, meta);
  if (tab === 'products') return renderPool(box, { supplierId: id, embedded: true });
  if (tab === 'runs') return renderRuns(box, id);
  if (tab === 'changes') return renderChanges(box, id);
  const rules = s.stock_rules || {};
  box.innerHTML = renderVal(html`<div class="grid grid-2">
    <div class="card"><div class="card-head"><h2>Bağlantı</h2></div><dl class="kv">
      <dt>Entegrasyon</dt><dd>${meta.integration_types[c.integration_type] || c.integration_type}</dd>
      ${c.integration_type !== 'manual' ? html`<dt>Kaynak adresi</dt><dd>${c.has_source_url ? html`<code>${c.source_url_display}</code>` : html`<span class="neg">Tanımlı değil</span>`}</dd>
      <dt>Kimlik doğrulama</dt><dd>${meta.auth_types[c.auth_type] || c.auth_type}${c.auth_type !== 'none' ? html` · ${c.has_secret ? 'şifre kayıtlı (şifreli)' : c.secret_env ? html`ortam değişkeni <code>${c.secret_env}</code>` : html`<span class="neg">şifre yok</span>`}` : ''}</dd>
      <dt>Kayıt yolu</dt><dd>${c.record_path || 'otomatik'}</dd>` : ''}
      <dt>Senkron sıklığı</dt><dd>${(INTERVALS.find(([v]) => v === Number(s.sync_interval_minutes)) || [0, s.sync_interval_minutes + ' dk'])[1]}</dd>
      <dt>Öncelik</dt><dd>${s.priority}</dd><dt>Durum</dt><dd>${s.is_active ? 'Aktif' : 'Pasif'}</dd></dl></div>
    <div class="card"><div class="card-head"><h2>Stok kuralları</h2></div><dl class="kv">
      <dt>Güvenlik payı</dt><dd>${num(rules.buffer || 0)} adet</dd><dt>Minimum stok</dt><dd>${num(rules.min_stock || 0)} (altı 0 sayılır)</dd>
      <dt>Maksimum stok</dt><dd>${rules.max_stock ?? 'sınırsız'}</dd></dl>
      <p class="small muted">Kurallar tedarikçi stoğuna uygulanır; sonra her pazaryerinin kendi stok kuralı uygulanır.</p>
      ${s.contact_name || s.phone || s.email ? html`<h3 class="mt">İletişim</h3><p>${s.contact_name || ''} ${s.phone || ''} ${s.email || ''}</p>` : ''}</div>
  </div>
  <div class="card mt"><div class="card-head"><h2>Son senkronizasyonlar</h2><a class="small" href="#/suppliers?id=${id}&tab=runs">Tümü →</a></div>${runsTable(d.runs)}</div>`);
}

function runsTable(runs) {
  if (!runs.length) return empty('Henüz senkron yok', 'Kaynak adresi varsa “Şimdi senkronize et”, yoksa “Dosya yükle” ile başlayın.');
  return html`<div class="table-wrap"><table><thead><tr><th>Başlangıç</th><th>Tetik</th><th>Durum</th><th class="r">Kayıt</th><th class="r">Yeni</th><th class="r">Fiyat</th><th class="r">Stok</th><th class="r">Kayıp</th><th class="r">Hata</th><th>Mesaj</th></tr></thead><tbody>
    ${runs.map((r) => html`<tr><td>${dateTime(r.started_at)}</td><td>${{ schedule: 'Zamanlanmış', manual: 'Elle', upload: 'Dosya', approval: 'Onay' }[r.trigger] || r.trigger}</td><td>${runBadge(r.status)}</td>
      <td class="r num">${num(r.records_total)}</td><td class="r num">${num(r.created_count)}</td><td class="r num">${num(r.price_changed)}</td><td class="r num">${num(r.stock_changed)}</td>
      <td class="r num ${r.missing_count ? 'neg' : ''}">${num(r.missing_count)}</td><td class="r num ${r.error_count ? 'neg' : ''}">${num(r.error_count)}</td>
      <td><span class="ellipsis small" title="${[r.message, ...(r.errors || [])].filter(Boolean).join('\n')}">${r.message || (r.errors || [])[0] || ''}</span></td></tr>`)}</tbody></table></div>`;
}
async function renderRuns(box, id) {
  const load = async (page) => {
    const d = await api(`/api/suppliers/${id}/runs`, { query: { page, page_size: 25 } });
    box.innerHTML = renderVal(html`<div class="card">${runsTable(d.items)}${pager(d, load)}</div>`);
  };
  await load(1);
}
async function renderChanges(box, id) {
  let kind = '';
  const load = async (page) => {
    const d = await api(`/api/suppliers/${id}/changes`, { query: { page, page_size: 50, kind } });
    box.innerHTML = renderVal(html`<div class="card"><div class="filters"><select id="chg-kind" aria-label="Değişiklik türü">${[['', 'Tüm değişiklikler'], ['new', 'Yeni ürün'], ['price', 'Fiyat değişti'], ['stock', 'Stok değişti'], ['missing', 'Kaynağında bulunamadı'], ['reactivated', 'Yeniden göründü']].map(([v, l]) => html`<option value="${v}" ${raw(v === kind ? 'selected' : '')}>${l}</option>`)}</select></div>
      ${d.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Tarih</th><th>Değişiklik</th><th>Tedarikçi SKU</th><th>Ürün</th><th class="r">Önceki</th><th class="r">Yeni</th></tr></thead><tbody>
      ${d.items.map((x) => html`<tr><td>${dateTime(x.created_at)}</td><td><span class="badge ${{ new: 'tone-info', price: 'tone-warn', stock: '', missing: 'tone-bad', reactivated: 'tone-good' }[x.kind] || ''}">${x.kind_label}</span></td>
        <td><b>${x.supplier_sku}</b></td><td><span class="ellipsis">${x.name || '—'}</span></td><td class="r num">${x.old_value ?? '—'}</td><td class="r num">${x.new_value ?? '—'}</td></tr>`)}</tbody></table></div>${pager(d, load)}`
        : empty('Değişiklik yok', 'Senkronizasyonlarda tespit edilen yeni ürün, fiyat, stok ve kaybolan ürün değişiklikleri burada listelenir.')}</div>`);
    $('#chg-kind', box).addEventListener('change', (e) => { kind = e.target.value; load(1); });
  };
  await load(1);
}

async function renderMapping(box, id, d, meta) {
  const c = d.connection;
  const remote = c.integration_type !== 'manual' && c.has_source_url;
  let preview = null;
  const current = Object.fromEntries(d.mappings.map((m) => [m.target_field, { source_path: m.source_path || '', default_value: m.default_value || '' }]));
  const draw = () => {
    const paths = preview ? preview.fields : [];
    const sample = (field) => {
      if (!preview) return '';
      const v = preview.samples[0]?.values?.[field];
      return Array.isArray(v) ? v.join(', ') : v ?? '';
    };
    const opts = (cur) => html`<option value="">— eşleştirilmedi —</option>${cur && !paths.some((p) => p.path === cur) ? html`<option value="${cur}" selected>${cur}</option>` : ''}
      ${paths.map((p) => html`<option value="${p.path}" ${raw(p.path === cur ? 'selected' : '')}>${p.path}  (${pct(p.fill_rate)} dolu · ör. ${p.sample.slice(0, 30)})</option>`)}`;
    box.innerHTML = renderVal(html`<div class="card"><div class="card-head"><div><h2>Alan eşleştirme</h2>
      <p>Tedarikçinin alan adlarını TrendHub alanlarına bağlayın. Kod hiçbir tedarikçinin alan adına bağlı değildir; her tedarikçinin XML/CSV yapısı farklı olabilir.</p></div></div>
      <div class="row" style="margin-bottom:12px">
        ${remote ? html`<button class="btn" id="pv-remote">Kaynaktan önizle</button>` : ''}
        <label class="btn" for="pv-file">Dosyadan önizle</label><input type="file" id="pv-file" accept=".xml,.csv,.json,.txt" hidden>
        ${preview ? html`<span class="muted small">${num(preview.total)} kayıt okundu · kayıt yolu <code>${preview.record_path || '—'}</code> · ${preview.using === 'suggestion' ? 'otomatik öneri gösteriliyor' : 'kayıtlı eşleştirme gösteriliyor'}</span>
          <button class="btn btn-sm" id="pv-apply">Öneriyi uygula</button>` : html`<span class="muted small">Önizleme veritabanına ürün yazmaz.</span>`}
      </div>
      <form id="mapform"><div class="table-wrap"><table><thead><tr><th>TrendHub alanı</th><th>Tedarikçi alanı (kaynak yol)</th><th>Varsayılan değer</th>${preview ? html`<th>Örnek (1. kayıt)</th>` : ''}</tr></thead><tbody>
        ${d.mappings.map((m) => html`<tr><td><b>${m.label}</b>${m.required ? html` <span class="badge plain tone-warn">zorunlu</span>` : ''}<span class="muted small">${m.target_field}</span></td>
          <td>${preview ? html`<select name="src_${m.target_field}" aria-label="${m.label} kaynak alanı">${opts(current[m.target_field].source_path)}</select>`
            : html`<input name="src_${m.target_field}" value="${current[m.target_field].source_path}" maxlength="300" placeholder="ör. UrunKodu veya Resimler/Resim" aria-label="${m.label} kaynak alanı">`}</td>
          <td><input name="def_${m.target_field}" value="${current[m.target_field].default_value}" maxlength="500" aria-label="${m.label} varsayılan" placeholder="${m.target_field === 'currency' ? 'TRY' : m.target_field === 'vat_rate' ? '20' : ''}"></td>
          ${preview ? html`<td><span class="ellipsis small" title="${sample(m.target_field)}">${sample(m.target_field)}</span></td>` : ''}</tr>`)}
      </tbody></table></div>
      ${can('operator') ? html`<div class="row mt"><span class="spacer"></span><button class="btn btn-primary" type="submit">Eşleştirmeyi kaydet</button></div>` : ''}</form></div>
      ${preview ? html`<div class="card mt"><div class="card-head"><h2>Örnek kayıtlar (eşleştirilmiş)</h2></div><div class="table-wrap"><table><thead><tr><th>SKU</th><th>Barkod</th><th>Ürün adı</th><th>Marka</th><th>Kategori</th><th class="r">Alış</th><th class="r">Stok</th><th class="r">Görsel</th><th>Sorun</th></tr></thead><tbody>
        ${preview.samples.map((x) => html`<tr><td>${x.values.supplier_sku || '—'}</td><td>${x.values.barcode || '—'}</td><td><span class="ellipsis">${x.values.name || '—'}</span></td><td>${x.values.brand || '—'}</td><td><span class="ellipsis">${x.values.category || '—'}</span></td>
          <td class="r num">${x.values.purchase_price ? money(x.values.purchase_price) : '—'}</td><td class="r num">${num(x.values.stock)}</td><td class="r num">${num((x.values.images || []).length)}</td>
          <td class="small ${x.errors.length ? 'neg' : 'pos'}">${x.errors.length ? x.errors.join('; ') : 'Uygun'}</td></tr>`)}</tbody></table></div></div>` : ''}`);
    const read = () => {
      const f = $('#mapform', box);
      d.mappings.forEach((m) => { current[m.target_field] = { source_path: f[`src_${m.target_field}`].value.trim(), default_value: f[`def_${m.target_field}`].value.trim() }; });
    };
    const run = async (content) => {
      read();
      try {
        preview = await api(`/api/suppliers/${id}/preview`, { method: 'POST', rawBody: content });
        if (preview.using === 'suggestion') Object.entries(preview.suggestion).forEach(([k, v]) => { if (!current[k].source_path) current[k].source_path = v; });
        draw();
      } catch (ex) { fail(ex); }
    };
    $('#pv-remote', box)?.addEventListener('click', (e) => { e.target.disabled = true; toast('Kaynak okunuyor…'); run(undefined); });
    $('#pv-file', box).addEventListener('change', (e) => { if (e.target.files[0]) run(e.target.files[0]); });
    $('#pv-apply', box)?.addEventListener('click', () => { read(); Object.entries(preview.suggestion).forEach(([k, v]) => { current[k].source_path = v; }); draw(); });
    $('#mapform', box).addEventListener('submit', (e) => {
      e.preventDefault();
      read();
      submitting(e.target, async () => {
        await api(`/api/suppliers/${id}/mappings`, { method: 'PUT', body: { mappings: Object.entries(current).map(([k, v]) => ({ target_field: k, source_path: v.source_path || null, default_value: v.default_value || null })) } });
        toast('Eşleştirme kaydedildi');
      });
    });
  };
  draw();
}

// ---- İçe aktarma sihirbazı: test → node → eşleştirme → canlı önizleme → uyarılar → ONAY → Ürün Havuzu
const CONF_BADGE = { high: ['tone-good', 'Güçlü öneri'], low: ['tone-warn', 'Düşük güven'], confirm: ['tone-warn', 'Doğrulayın'], manual: ['', 'Elle'] };
const NODE_KIND = { product: ['tone-good', 'Önerilen ürün'], variant: ['tone-info', 'Varyant listesi'], nested: ['', 'Ürün içi liste'], other: ['', 'Diğer'] };
async function renderImportWizard(box, id, d) {
  const c = d.connection, s = d.supplier;
  const remote = c.integration_type !== 'manual' && c.has_source_url;
  if (!remote) {
    box.innerHTML = renderVal(html`<div class="card">${empty('Kaynak adresi yok', 'Sihirbaz kaynak adresinden (URL) okur. Manuel tedarikçide “Alan eşleştirme” sekmesinden dosyayla önizleyip “Dosya yükle” ile aktarın.')}</div>`);
    return;
  }
  const st = { a: null, tested: null, record_path: null, variant_path: null, mappings: null, confirm: false, vat: s.price_vat_mode || '', busy: false };
  const analyze = async () => {
    st.busy = true; draw();
    try {
      st.a = await api(`/api/suppliers/${id}/import/analyze`, { method: 'POST', body: { record_path: st.record_path, variant_path: st.variant_path, mappings: st.mappings, limit: 10 } });
      st.record_path = st.a.record_path; st.variant_path = st.a.variant_path || '';
      st.mappings = st.a.mapping.map((m) => ({ target_field: m.target_field, source_path: m.source_path || null, default_value: m.default_value || null }));
    } catch (ex) { fail(ex); } finally { st.busy = false; draw(); }
  };
  const readForm = () => {
    const f = $('#wz-map', box);
    if (!f || !st.mappings) return;
    st.mappings = st.mappings.map((m) => ({ ...m, source_path: f[`src_${m.target_field}`]?.value || null }));
    st.confirm = !!$('#wz-confirm', box)?.checked;
    st.vat = $('#wz-vat', box)?.value || st.vat;
  };
  const draw = () => {
    const a = st.a;
    const step = (n, title, body, done) => html`<div class="card mt"><div class="card-head"><h2>${done ? '✓ ' : ''}${n}. ${title}</h2></div>${body}</div>`;
    const fields = a ? a.fields : [];
    const opts = (cur) => html`<option value="">— eşleştirilmedi —</option>${cur && !fields.some((p) => p.path === cur) ? html`<option value="${cur}" selected>${cur}</option>` : ''}
      ${fields.map((p) => html`<option value="${p.path}" ${raw(p.path === cur ? 'selected' : '')}>${p.path} (${pct(p.fill_rate)} dolu${p.all_same ? ' · hepsi aynı' : ''} · ör. ${String(p.sample || '').slice(0, 30)})</option>`)}`;
    const priceRow = a?.mapping.find((m) => m.target_field === 'purchase_price');
    box.innerHTML = renderVal(html`
      <div class="notice" style="margin-bottom:4px">Sihirbaz kaynağı yalnızca <b>okur</b>. Onay verene kadar Ürün Havuzu'na ürün yazılmaz ve zamanlanmış senkron çalışmaz. Onaydan sonra da pazaryerine hiçbir şey gönderilmez.
        ${s.mapping_approved_at ? html`<br><span class="pos">Mevcut eşleştirme ${dateTime(s.mapping_approved_at)} tarihinde onaylandı.</span>` : html`<br><span class="neg">Bu tedarikçinin eşleştirmesi henüz onaylanmadı.</span>`}</div>
      ${step(1, 'Bağlantıyı test et', html`<div class="row"><code>${c.source_url_display}</code><span class="spacer"></span>
        <button class="btn" id="wz-test">Bağlantıyı test et</button> <button class="btn btn-primary" id="wz-analyze" ${raw(st.busy ? 'disabled' : '')}>${st.busy ? 'Okunuyor…' : a ? 'Yeniden analiz et' : 'Kaynağı analiz et'}</button></div>
        ${st.tested ? html`<p class="small ${st.tested.ok ? 'pos' : 'neg'}">${st.tested.message}</p>` : ''}`, !!a)}
      ${a ? html`
      ${step(2, 'Ürün node seçimi', html`<p class="small muted">Tekrar eden elemanlar. Öneri en çok tekrar eden değil, ürüne en çok benzeyen elemandır (ad, SKU, fiyat, stok, barkod, görsel).</p>
        ${a.nodes ? html`<div class="table-wrap"><table><thead><tr><th></th><th>Eleman yolu</th><th>Tür</th><th class="r">Adet</th><th>Alanlar</th></tr></thead><tbody>
          ${a.nodes.candidates.map((n) => html`<tr><td><input type="radio" name="wz-node" value="${n.path}" ${raw(n.path === st.record_path ? 'checked' : '')} ${raw(n.kind === 'variant' || n.kind === 'nested' ? 'disabled' : '')} aria-label="${n.path}"></td>
            <td><code>${n.path}</code></td><td><span class="badge ${NODE_KIND[n.kind][0]}">${NODE_KIND[n.kind][1]}</span></td><td class="r num">${num(n.count)}</td>
            <td><span class="ellipsis small" title="${n.fields.join(', ')}">${n.fields.slice(0, 8).join(', ')}</span></td></tr>`)}</tbody></table></div>
          <label class="mt" style="display:block">Varyant listesi (her varyant ayrı ürün olur, ana ürün alanlarını miras alır)
            <select id="wz-variant" style="max-width:100%"><option value="">Varyant yok (her ürün tek kayıt)</option>
              ${a.nodes.variant_candidates.map((v) => html`<option value="${v.path}" ${raw(v.path === st.variant_path ? 'selected' : '')}>${v.path} (${num(v.count)} adet · ${v.fields.slice(0, 5).join(', ')})</option>`)}
              ${st.variant_path && !a.nodes.variant_candidates.some((v) => v.path === st.variant_path) ? html`<option value="${st.variant_path}" selected>${st.variant_path}</option>` : ''}</select></label>`
          : html`<p class="muted small">Bu kaynak XML değil; kayıt yolu: <code>${a.record_path || '—'}</code></p>`}
        <p class="small">Seçili: <code>${a.record_path || '—'}</code>${a.variant_path ? html` · varyant <code>${a.variant_path}</code>` : ''} → <b>${num(a.total)}</b> kayıt</p>`, true)}
      ${step(3, 'Alan eşleştirme', html`<form id="wz-map"><div class="table-wrap"><table><thead><tr><th>TrendHub alanı</th><th>Tedarikçi alanı</th><th>Güven</th><th>Örnek (1. ürün)</th></tr></thead><tbody>
        ${a.mapping.map((m) => {
          const cur = (st.mappings.find((x) => x.target_field === m.target_field) || {}).source_path;
          const conf = cur ? (cur === m.source_path ? (m.confidence || 'manual') : 'manual') : null;
          const v = a.samples[0]?.values?.[m.target_field];
          return html`<tr><td><b>${m.label}</b>${m.required ? html` <span class="badge plain tone-warn">zorunlu</span>` : ''}</td>
            <td><select name="src_${m.target_field}" aria-label="${m.label} kaynak alanı">${opts(cur)}</select></td>
            <td>${conf ? html`<span class="badge ${CONF_BADGE[conf]?.[0] || ''}">${CONF_BADGE[conf]?.[1] || conf}</span>` : ''}</td>
            <td><span class="ellipsis small">${Array.isArray(v) ? `${v.length} görsel` : v ?? ''}</span></td></tr>`;
        })}</tbody></table></div>
        ${priceRow?.source_path ? html`<div class="notice ${a.price_needs_confirmation ? 'warn' : ''} mt"><label class="wz-check"><input type="checkbox" id="wz-confirm" ${raw(st.confirm || priceRow.confirmed ? 'checked' : '')}>
          <b>'${priceRow.source_path}'</b> alanı tedarikçi <b>ALIŞ fiyatı</b> mı? (Satış fiyatı olarak kullanılmaz; pazaryeri fiyatı fiyat kurallarıyla hesaplanır.)</label></div>` : ''}
        <div class="row mt"><span class="spacer"></span><button class="btn" type="submit">Önizlemeyi güncelle</button></div></form>`, true)}
      ${step(4, `Canlı önizleme (${a.samples.length} ürün)`, html`<div class="table-wrap"><table><thead><tr><th>SKU</th><th>Barkod</th><th>Ürün adı</th><th>Renk/Beden</th><th>Kategori</th><th class="r">XML fiyatı</th><th>Para</th><th class="r">KDV</th><th class="r">Maliyet (TL, KDV dahil)</th><th class="r">Stok</th><th class="r">Görsel</th><th>Durum</th></tr></thead><tbody>
        ${a.samples.map((x) => html`<tr><td>${x.values.supplier_sku || '—'}</td><td>${x.values.barcode || '—'}</td><td><span class="ellipsis">${x.values.name || '—'}</span></td>
          <td class="small">${[x.values.color, x.values.size].filter(Boolean).join(' / ') || '—'}</td><td><span class="ellipsis small">${x.values.category || '—'}</span></td>
          <td class="r num">${x.values.purchase_price ?? '—'}</td><td>${x.values.currency || 'TRY?'}</td><td class="r num">${x.values.vat_rate ?? '—'}</td>
          <td class="r num" title="${x.cost_note || ''}">${x.effective_cost === null ? html`<span class="neg">—</span>` : money(x.effective_cost)}${x.cost_note ? html`<span class="muted small" style="display:block">${x.cost_note}</span>` : ''}</td>
          <td class="r num ${Number(x.values.stock) <= 0 ? 'neg' : ''}">${num(x.values.stock)}</td><td class="r num">${num((x.values.images || []).length)}</td>
          <td class="small ${x.errors.length ? 'neg' : 'pos'}">${x.errors.length ? x.errors.join('; ') : 'Uygun'}</td></tr>`)}</tbody></table></div>
        <p class="small muted">Özet: ${num(a.summary.importable)} aktarılabilir · ${num(a.summary.invalid)} hatalı · ${num(a.summary.zero_stock)} stoksuz · ${num(a.summary.duplicate_skus)} tekrar eden SKU</p>`, true)}
      ${step(5, 'Eksik alan uyarıları', a.warnings.length ? html`<ul class="small">${a.warnings.map((w) => html`<li>${w}</li>`)}</ul>` : html`<p class="pos small">Uyarı yok.</p>`, !a.warnings.length)}
      ${step(6, 'Onay ve Ürün Havuzu\'na aktarım', html`<div class="row"><label>XML fiyatları<select id="wz-vat"><option value="">— seçin —</option>
          <option value="included" ${raw(st.vat === 'included' ? 'selected' : '')}>KDV dahil</option><option value="excluded" ${raw(st.vat === 'excluded' ? 'selected' : '')}>KDV hariç (ürün KDV oranı eklenir)</option></select></label>
        <span class="spacer"></span>${can('operator') ? html`<button class="btn btn-primary" id="wz-approve" ${raw(a.can_approve ? '' : 'disabled')}>Onayla ve Ürün Havuzu'na aktar</button>` : ''}</div>
        <p class="small muted">Onay eşleştirmeyi kaydeder ve ${num(a.summary.importable)} kaydı Ürün Havuzu'na alır. Mevcut ürün/sipariş, pazaryeri stok ve fiyatı değişmez. Sonraki zamanlanmış senkronlar bu onaylı eşleştirmeyi kullanır; eşleştirme değişirse onay yeniden istenir.</p>`, false)}` : ''}`);
    $('#wz-test', box)?.addEventListener('click', async (e) => {
      e.target.disabled = true;
      try { st.tested = await api(`/api/suppliers/${id}/test`, { method: 'POST' }); draw(); } catch (ex) { fail(ex); e.target.disabled = false; }
    });
    $('#wz-analyze', box)?.addEventListener('click', () => { readForm(); analyze(); });
    $$('[name="wz-node"]', box).forEach((r) => r.addEventListener('change', () => { st.record_path = r.value; st.variant_path = null; st.mappings = null; analyze(); }));
    $('#wz-variant', box)?.addEventListener('change', (e) => { st.variant_path = e.target.value; st.mappings = null; analyze(); });
    $('#wz-map', box)?.addEventListener('submit', (e) => { e.preventDefault(); readForm(); analyze(); });
    $('#wz-confirm', box)?.addEventListener('change', (e) => { st.confirm = e.target.checked; });
    $('#wz-vat', box)?.addEventListener('change', (e) => { st.vat = e.target.value; });
    $('#wz-approve', box)?.addEventListener('click', async (e) => {
      readForm();
      if (!st.vat) { toast('XML fiyatlarının KDV dahil mi hariç mi olduğunu seçin.', true); return; }
      const price = st.mappings.find((m) => m.target_field === 'purchase_price')?.source_path;
      if (price && !st.confirm && !a.mapping.find((m) => m.target_field === 'purchase_price')?.confirmed) {
        if (a.price_needs_confirmation || !confirm(`'${price}' alanı tedarikçi alış fiyatı olarak kullanılacak. Onaylıyor musunuz?`)) {
          if (a.price_needs_confirmation) toast(`'${price}' alanının tedarikçi alış fiyatı olduğunu işaretleyin.`, true);
          return;
        }
        st.confirm = true;
      }
      e.target.disabled = true;
      try {
        const r = await api(`/api/suppliers/${id}/import/approve`, { method: 'POST', body: { record_path: st.record_path, variant_path: st.variant_path || null, mappings: st.mappings, confirm_price: st.confirm, price_vat_mode: st.vat } });
        const imp = r.import;
        toast(imp ? `${r.message} ${imp.created_count} yeni, ${imp.updated_count} güncellendi${imp.error_count ? `, ${imp.error_count} hata` : ''}.` : r.message);
        location.hash = `#/suppliers?id=${id}&tab=products`;
      } catch (ex) { fail(ex); e.target.disabled = false; }
    });
  };
  draw();
}

// ---- Ürün havuzu (tedarikçi ürünleri) — Tedarikçi detayında ve Ürün Aktarımı 1. adımında kullanılır
async function renderPool(box, { supplierId = null, embedded = false, selectable = false, onSelect } = {}) {
  const sups = embedded ? [] : await api('/api/suppliers');
  const f = { supplier_id: supplierId || '', q: '', status: 'active', in_catalog: '', stock: '', category: '', price_min: '', price_max: '', in_store: '', problematic: false, multi_supplier: false };
  const sel = state.transfer.selected;
  const kv = (k, v) => (v === null || v === undefined || v === '' ? '' : html`<dt>${k}</dt><dd>${v}</dd>`);
  const load = async (page) => {
    const d = await api('/api/supplier-products', { query: { ...f, problematic: f.problematic ? 'true' : '', multi_supplier: f.multi_supplier ? 'true' : '', page, page_size: 48 } });
    const opt = (pairs, cur) => pairs.map(([v, l]) => html`<option value="${v}" ${raw(v === cur ? 'selected' : '')}>${l}</option>`);
    box.innerHTML = renderVal(html`<div class="card">
      <form class="filters" id="pool-f">
        <input type="search" name="q" placeholder="SKU, barkod, model veya ürün adı" value="${f.q}" aria-label="Ara">
        ${embedded ? '' : html`<select name="supplier_id" aria-label="Tedarikçi"><option value="">Tüm tedarikçiler</option>${sups.map((s) => html`<option value="${s.id}" ${raw(String(s.id) === String(f.supplier_id) ? 'selected' : '')}>${s.name}</option>`)}</select>`}
        <input name="category" placeholder="Kategori" value="${f.category}" aria-label="Kategori">
        <select name="stock" aria-label="Stok">${opt([['', 'Stok: tümü'], ['in', 'Stokta var'], ['out', 'Stokta yok']], f.stock)}</select>
        <select name="in_store" aria-label="Mağaza">${opt([['', 'Mağaza: tümü'], ['yes', 'Mağazada olan'], ['no', 'Mağazada olmayan']], f.in_store)}</select>
        <select name="in_catalog" aria-label="Katalog">${opt([['', 'Katalog: tümü'], ['no', 'Kataloğa alınmamış'], ['yes', 'Katalogda']], f.in_catalog)}</select>
        <select name="status" aria-label="Durum">${opt([['active', 'Aktif'], ['missing', 'Kaynağında bulunamadı'], ['', 'Tümü']], f.status)}</select>
        <input type="number" name="price_min" min="0" step="0.01" placeholder="Min alış ₺" value="${f.price_min}" aria-label="En düşük alış fiyatı">
        <input type="number" name="price_max" min="0" step="0.01" placeholder="Maks alış ₺" value="${f.price_max}" aria-label="En yüksek alış fiyatı">
        <label class="check"><input type="checkbox" name="problematic" value="1" ${raw(f.problematic ? 'checked' : '')}>Sorunlu</label>
        <label class="check"><input type="checkbox" name="multi_supplier" value="1" ${raw(f.multi_supplier ? 'checked' : '')}>Birden çok tedarikçide</label>
        <button class="btn btn-primary" type="submit">Filtrele</button></form>
      <p class="small muted">${num(d.summary.total)} tedarikçi ürünü · ${num(d.summary.missing)} kaynağında bulunamadı · ${num(d.summary.not_in_catalog)} kataloğa alınmamış · ${num(d.summary.catalog_products)} katalog ürünü. Havuzdaki ürünler pazaryerine otomatik GÖNDERİLMEZ.</p>
      ${selectable && d.items.length ? html`<label class="check" style="margin:6px 0"><input type="checkbox" id="pool-all">Bu sayfadakilerin tümünü seç (${num(d.items.length)})</label>` : ''}
      ${d.items.length ? html`<div class="pool-grid">${d.items.map((x) => html`<article class="pool-card ${sel.has(x.id) ? 'picked' : ''} ${x.problems.length ? 'has-problem' : ''}">
          <div class="pool-img">${x.image ? html`<img src="${x.image}" alt="" loading="lazy" referrerpolicy="no-referrer">` : html`<span class="muted small">Görsel yok</span>`}
            ${selectable ? html`<label class="pool-pick"><input type="checkbox" data-pick="${x.id}" ${raw(sel.has(x.id) ? 'checked' : '')} aria-label="Seç: ${x.name || x.supplier_sku}"></label>` : ''}</div>
          <div class="pool-body">
            <b class="pool-title" title="${x.name || ''}">${x.name || x.supplier_sku}</b>
            <div class="row" style="gap:4px">${embedded ? '' : html`<span class="badge plain">${x.supplier_name}</span>`}<span class="badge ${x.status === 'missing' ? 'tone-bad' : 'tone-good'}">${x.status_label}</span>
              ${(x.stores || []).map((m) => html`<span class="badge tone-info">${m}</span>`)}${(x.draft_marketplaces || []).length ? html`<span class="badge plain">taslak: ${x.draft_marketplaces.join(', ')}</span>` : ''}</div>
            <div class="pool-prices"><span title="${x.cost_note || 'Finansta kullanılan maliyet (TL, KDV dahil)'}"><small>Alış${x.currency && x.currency !== 'TRY' ? ` (${x.currency})` : ''}</small><b>${x.cost ? (x.currency && x.currency !== 'TRY' ? `${Number(x.cost).toLocaleString('tr-TR')} ${x.currency}` : money(x.cost)) : '—'}</b>${x.effective_cost && Number(x.effective_cost) !== Number(x.cost) ? html`<small>maliyet ${money(x.effective_cost)}</small>` : ''}</span><span><small>Satış</small><b>${x.sale_price ? money(x.sale_price) : '—'}</b></span><span><small>Stok</small><b class="${Number(x.stock) > 0 ? '' : 'neg'}">${num(x.stock)}</b></span></div>
            <dl class="kv small">${kv('Tedarikçi ürün ID', x.supplier_sku)}${kv('SKU', x.product_sku)}${kv('Barkod', x.barcode || '—')}${kv('Model', x.model_code)}${kv('Marka', x.brand)}${kv('Kategori', x.category)}${kv('Renk', x.color)}${kv('Varyant', x.variant)}${kv('KDV', x.vat_rate === null ? null : `%${x.vat_rate}`)}${kv('Desi', x.desi)}${kv('Güncelleme', dateTime(x.updated_at || x.last_seen_at))}</dl>
            ${x.problems.length ? html`<p class="small neg">⚠ ${x.problems.join(' · ')}</p>` : ''}
            <div class="row">${x.product_id ? html`<button class="btn btn-sm" data-life="${x.product_id}">Ürün detayı</button>${Number(x.offer_count) > 1 ? html`<button class="btn btn-sm" data-offers="${x.product_id}">${x.offer_count} teklif</button>` : ''}` : html`<span class="muted small">Havuzda (kataloğa alınmadı)</span>`}</div>
          </div></article>`)}</div>${pager(d, load)}` : empty('Ürün yok', embedded ? 'Bu tedarikçiden henüz ürün okunmadı. Alan eşleştirmesini yapıp senkronize edin veya dosya yükleyin.' : 'Filtreye uyan tedarikçi ürünü yok.')}</div>`);
    $('#pool-f', box).addEventListener('submit', (e) => {
      e.preventDefault();
      const v = formData(e.target);
      Object.assign(f, { q: v.q || '', status: v.status || '', in_catalog: v.in_catalog || '', stock: v.stock || '', category: v.category || '', price_min: v.price_min || '', price_max: v.price_max || '', in_store: v.in_store || '', problematic: v.problematic === '1', multi_supplier: v.multi_supplier === '1' });
      if (!embedded) f.supplier_id = v.supplier_id || '';
      load(1);
    });
    $$('img', box).forEach((img) => img.addEventListener('error', () => { img.replaceWith(Object.assign(document.createElement('span'), { className: 'muted small', textContent: 'Görsel yüklenemedi' })); }));
    $$('[data-offers]', box).forEach((b) => b.addEventListener('click', () => showOffers(Number(b.dataset.offers))));
    $$('[data-life]', box).forEach((b) => b.addEventListener('click', () => showLifecycle(Number(b.dataset.life))));
    if (selectable) {
      $$('[data-pick]', box).forEach((cb) => cb.addEventListener('change', () => { const i = Number(cb.dataset.pick); if (cb.checked) sel.add(i); else sel.delete(i); cb.closest('.pool-card').classList.toggle('picked', cb.checked); onSelect?.(); }));
      $('#pool-all', box)?.addEventListener('change', (e) => { $$('[data-pick]', box).forEach((cb) => { cb.checked = e.target.checked; cb.dispatchEvent(new Event('change')); }); });
    }
  };
  await load(1);
}

// ---- Ürün yaşam döngüsü: tedarikçi → eşleştirme → ilan → sipariş → kargo → iade → finans → reklam → uyarı
async function showLifecycle(productId, tab = 'supplier') {
  let d;
  try { d = await api(`/api/products/${productId}/lifecycle`); } catch (e) { fail(e); return; }
  const p = d.product, fz = d.finance;
  const TABS = [['supplier', 'Tedarikçi', d.suppliers.length], ['mapping', 'Eşleştirme', d.drafts.length], ['listings', 'İlanlar', d.listings.length], ['orders', 'Siparişler', d.orders.length],
    ['shipping', 'Kargo', d.shipments.length], ['returns', 'İadeler', d.returns.length], ['finance', 'Finans', null], ['ads', 'Reklam', d.ads.length], ['alerts', 'Uyarılar', d.alerts.filter((a) => a.status === 'open').length]];
  const tbl = (head, rowsHtml, emptyText) => (rowsHtml.length ? html`<div class="table-wrap"><table><thead><tr>${head.map((h) => html`<th>${h}</th>`)}</tr></thead><tbody>${rowsHtml}</tbody></table></div>` : empty('Kayıt yok', emptyText));
  const content = {
    supplier: () => tbl(['Tedarikçi', 'Ürün ID', 'Alış', 'Stok', 'Durum', 'Son görülme'], d.suppliers.map((s) => html`<tr><td><b>${s.supplier_name}</b>${s.is_preferred ? html` <span class="badge tone-info">Tercih edilen</span>` : ''}</td><td>${s.supplier_sku}</td><td class="num">${money(s.cost)}</td><td class="num">${num(s.stock)}</td><td>${s.status === 'missing' ? 'Kaynakta yok' : 'Aktif'}</td><td>${dateTime(s.last_seen_at)}</td></tr>`), 'Bu ürün bir tedarikçiye bağlı değil.'),
    mapping: () => tbl(['Pazaryeri', 'Durum', 'Kategori', 'Fiyat', 'Eksikler'], d.drafts.map((x) => html`<tr><td>${x.marketplace_name}</td><td><span class="badge ${DRAFT_TONE[x.status]}">${{ draft: 'Taslak', invalid: 'Hatalı', ready: 'Yayına hazır', cancelled: 'İptal' }[x.status]}</span></td><td>${x.category_name || x.category_id || '—'}</td><td class="num">${money(x.price)}</td><td>${(x.errors || []).length ? html`<span class="small neg">Bu ürün yayınlanmaya hazır değil: ${x.errors.join('; ')}</span>` : '—'}</td></tr>`), 'Bu ürün için pazaryeri taslağı yok.'),
    listings: () => html`${tbl(['Mağaza', 'İlan no', 'Fiyat', 'Stok', 'Durum', 'Son senkron'], d.listings.map((l) => html`<tr><td>${l.marketplace_name}</td><td>${l.external_product_id || '—'}</td><td class="num">${money(l.listed_price)}</td><td class="num">${num(l.listed_stock)}</td><td>${l.status || '—'}</td><td>${dateTime(l.last_synced_at)}</td></tr>`), 'Ürün hiçbir mağazada ilanda değil.')}
      ${d.publications.length ? html`<h3 class="mt">Yayın denemeleri</h3>${tbl(['Pazaryeri', 'Durum', 'Mesaj', 'Tarih'], d.publications.map((x) => html`<tr><td>${x.marketplace_name}</td><td>${x.status}</td><td class="small">${x.message || ''}</td><td>${dateTime(x.created_at)}</td></tr>`), '')}` : ''}`,
    orders: () => tbl(['Sipariş', 'Pazaryeri', 'Tarih', 'Adet', 'Fiyat', 'Durum'], d.orders.map((o) => html`<tr class="click" data-lorder="${o.id}"><td>${o.external_order_id}</td><td>${o.marketplace_name || '—'}</td><td>${dateTime(o.order_date)}</td><td class="num">${num(o.quantity)}</td><td class="num">${money(o.unit_price)}</td><td>${statusBadge(o.internal_status)}</td></tr>`), 'Bu ürün için sipariş yok.'),
    shipping: () => tbl(['Sipariş', 'Kargo', 'Takip', 'Durum', 'Kargoya verildi', 'Teslim'], d.shipments.map((s) => html`<tr><td>${s.external_order_id}</td><td>${s.carrier || '—'}</td><td>${s.tracking_number || '—'}</td><td>${s.status || '—'}</td><td>${dateTime(s.shipped_at)}</td><td>${dateTime(s.delivered_at)}</td></tr>`), 'Kargo kaydı yok.'),
    returns: () => tbl(['Sipariş', 'Tarih', 'Adet', 'İade tutarı', 'Durum'], d.returns.map((o) => html`<tr><td>${o.external_order_id}</td><td>${dateTime(o.order_date)}</td><td class="num">${num(o.quantity)}</td><td class="num">${money(o.refund_amount)}</td><td>${statusBadge(o.internal_status)}</td></tr>`), 'İade yok.'),
    finance: () => html`<dl class="kv"><dt>Satılan adet</dt><dd>${num(fz.quantity)}</dd><dt>Ciro</dt><dd>${money(fz.revenue)}</dd><dt>Ürün maliyeti</dt><dd>${money(fz.product_cost)}</dd><dt>Komisyon</dt><dd>${money(fz.commission)}</dd><dt>Kargo</dt><dd>${money(fz.shipping)}</dd><dt>İadeler</dt><dd>${money(fz.refunds)}</dd>
      <dt>Kâr ${TAHMINI}</dt><dd class="${signClass(fz.profit)}">${money(fz.profit)}</dd><dt>Reklam payı</dt><dd>${money(fz.ad_spend_share)}</dd><dt>Reklam sonrası kâr ${TAHMINI}</dt><dd class="${signClass(fz.profit_after_ads)}">${money(fz.profit_after_ads)}</dd></dl>`,
    ads: () => tbl(['Kampanya', 'Kanal', 'Durum', 'Toplam harcama'], d.ads.map((a) => html`<tr><td>${a.name}</td><td>${a.channel}</td><td>${a.status}</td><td class="num">${money(a.spend)}</td></tr>`), 'Bu ürün bir reklam kampanyasına bağlı değil.'),
    alerts: () => (d.alerts.length ? html`<div class="alert-list">${d.alerts.map((a) => html`<div class="alert-card sev-${a.severity} ${a.status === 'resolved' ? 'resolved' : ''}"><b>${SEV_ICON[a.severity]} ${a.title}</b><span class="small">${a.description}</span><span class="small muted">${dateTime(a.last_detected_at)} · ${a.status === 'open' ? 'Açık' : 'Çözüldü'}</span></div>`)}</div>` : empty('Uyarı yok', 'Bu ürünle ilgili uyarı yok.')),
  };
  const body = openDrawer(html`<div class="life-head">${(p.images || [])[0] ? html`<img src="${p.images[0]}" alt="" referrerpolicy="no-referrer">` : ''}<div><h2>${p.name}</h2>
      <p class="muted small">SKU ${p.sku || '—'} · barkod ${p.barcode || '—'} · stok ${num(p.stock)} · ${p.preferred_supplier_name ? `tercih: ${p.preferred_supplier_name}` : 'tercih edilen tedarikçi yok'}</p>
      <div class="row" style="gap:4px">${d.stores.length ? d.stores.map((m) => html`<span class="badge tone-info">${m}</span>`) : html`<span class="badge plain">Hiçbir mağazada değil</span>`}</div></div></div>
    <div class="seg life-tabs" role="tablist">${TABS.map(([k, l, n]) => html`<button role="tab" class="${k === tab ? 'on' : ''}" aria-selected="${k === tab}" data-ltab="${k}">${l}${n ? html` <small>${n}</small>` : ''}</button>`)}</div>
    <div class="mt">${content[tab]()}</div>`);
  $$('[data-ltab]', body).forEach((b) => b.addEventListener('click', () => showLifecycle(productId, b.dataset.ltab)));
  $$('[data-lorder]', body).forEach((r) => r.addEventListener('click', () => showOrder(r.dataset.lorder)));
}

// ---- Teklif karşılaştırma (aynı katalog ürünü, birden çok tedarikçi)
async function showOffers(productId) {
  const d = await api(`/api/products/${productId}/offers`);
  const p = d.product;
  const body = openDrawer(html`<h2>Tedarikçi teklifleri</h2>
    <p><b>${p.name}</b><br><span class="muted small">${p.sku || ''} · barkod ${p.barcode || '—'}</span></p>
    <div class="grid grid-2"><div class="card kpi"><div class="label">Katalog stoğu</div><div class="value">${num(p.stock)}</div></div>
      <div class="card kpi"><div class="label">Katalog maliyeti</div><div class="value">${money(p.cost)}</div></div></div>
    ${d.offers.length ? html`<div class="table-wrap mt"><table><thead><tr><th>Tedarikçi</th><th class="r">Alış</th><th class="r">Kullanılabilir stok</th><th class="r">Öncelik</th><th>Durum</th><th>Kullanılan</th></tr></thead><tbody>
      ${d.offers.map((o) => html`<tr><td><b>${o.supplier_name}</b><span class="muted small">${o.supplier_sku || ''}</span></td><td class="r num">${o.cost === null ? '—' : money(o.cost)}</td>
        <td class="r num">${num(o.stock)}${Number(o.raw_stock) !== Number(o.stock) ? html`<span class="muted small">kaynak: ${num(o.raw_stock)}</span>` : ''}</td><td class="r num">${o.priority}</td>
        <td>${o.usable ? html`<span class="badge tone-good">Uygun</span>` : html`<span class="badge tone-bad">${o.available ? 'Stok/fiyat yok' : o.status_label || 'Pasif'}</span>`}</td>
        <td>${o.supplier_id === d.selected_supplier_id ? html`<span class="badge tone-info">Seçili</span>` : ''}</td></tr>`)}</tbody></table></div>` : empty('Teklif yok', 'Bu ürün henüz hiçbir tedarikçi ürününe bağlı değil.')}
    <h3 class="mt">Tedarikçi seçimi</h3>
    <div class="table-wrap"><table><tbody>${Object.entries(d.strategy_labels).map(([k, l]) => html`<tr><td>${l}</td><td>${d.offers.find((o) => o.supplier_id === d.by_strategy[k])?.supplier_name || html`<span class="muted">—</span>`}</td></tr>`)}</tbody></table></div>
    ${can('operator') && d.offers.length ? html`<form class="form-grid mt" id="srcform">
      <label>Strateji<select name="strategy">${Object.entries(d.strategy_labels).map(([k, l]) => html`<option value="${k}" ${raw(k === p.supplier_strategy ? 'selected' : '')}>${l}</option>`)}</select></label>
      <label>Tercih edilen tedarikçi<select name="preferred_supplier_id"><option value="">—</option>${d.offers.map((o) => html`<option value="${o.supplier_id}" ${raw(o.supplier_id === p.preferred_supplier_id ? 'selected' : '')}>${o.supplier_name}</option>`)}</select></label>
      <p class="small muted full">Seçilen teklif katalog ürününün stok ve maliyetini belirler (yalnızca TrendHub'da). Maliyet değişiklikleri maliyet geçmişine yazılır; geçmiş siparişler değişmez.</p>
      <p class="form-error full"></p><div class="full row"><span class="spacer"></span><button class="btn btn-primary" type="submit">Kaydet</button></div></form>` : ''}`);
  $('#srcform', body)?.addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(e.target, async () => {
      const v = formData(e.target);
      await api(`/api/products/${productId}/sourcing`, { method: 'PUT', body: { strategy: v.strategy, preferred_supplier_id: v.preferred_supplier_id ? Number(v.preferred_supplier_id) : null } });
      toast('Tedarikçi seçimi kaydedildi'); showOffers(productId);
    });
  });
}

// ---- Ürün Aktarımı: Tedarikçiler → Ürün Havuzu → Ürünleri Seç → Pazaryerini Seç → Fiyatlandır → Validate → Yayına Hazırla
state.transfer = { selected: new Set(), marketplaces: new Set(['trendyol']), productIds: [], draftIds: [] };
const TRANSFER_STEPS = [['pool', '1. Ürünler'], ['marketplaces', '2. Mağazalar'], ['pricing', '3. Kategori · 4. Özellikler · 5. Fiyat · 6. Stok'], ['ready', '7. Önizleme & doğrulama · 8. Yayın onayı']];
const DRAFT_TONE = { draft: 'tone-info', invalid: 'tone-bad', ready: 'tone-good', cancelled: '' };
PAGES.transfer = {
  title: 'Ürün Aktarımı', icon: 'transfer', nav: 'Ürün Aktarımı',
  async render(params) {
    const step = params.get('step') || 'pool';
    const tab = params.get('tab') || 'wizard';
    setHeader('Ürün Aktarımı', 'Tedarikçi ürünlerini seçip pazaryerleri için fiyatlandırın ve doğrulayın');
    view().innerHTML = renderVal(html`
      <div class="notice info" style="margin-bottom:12px">Pazaryerine <b>otomatik gönderim yoktur</b>. Gönderim yalnızca yöneticinin önizleme + onayıyla ve tüm güvenlik kontrolleri açıkken yapılır; pazaryerinin yayın API'si doğrulanmadığı sürece hiçbir ürün gönderilmez. “Yayına hazır” taslaklar CSV olarak indirilebilir.</div>
      <div class="seg" role="tablist">${[['wizard', 'Aktarım sihirbazı'], ['drafts', 'Tüm taslaklar'], ['rules', 'Pazaryeri kuralları']].map(([k, l]) => html`<button role="tab" class="${k === tab ? 'on' : ''}" aria-selected="${k === tab}" data-ttab="${k}">${l}</button>`)}</div>
      ${tab === 'wizard' ? html`<ol class="steps mt">${TRANSFER_STEPS.map(([k, l], i) => html`<li class="${k === step ? 'on' : TRANSFER_STEPS.findIndex(([x]) => x === step) > i ? 'done' : ''}">${l}</li>`)}</ol>` : ''}
      <div id="tr-body" class="mt"></div>`);
    $$('[data-ttab]').forEach((b) => b.addEventListener('click', () => { location.hash = `#/transfer?tab=${b.dataset.ttab}`; }));
    const box = $('#tr-body');
    if (tab === 'drafts') return renderDrafts(box, {});
    if (tab === 'rules') return renderRules(box);
    const T = state.transfer;
    if (step === 'pool') {
      const bar = document.createElement('div');
      const updateBar = () => {
        bar.innerHTML = renderVal(html`<div class="card sticky-bar row"><b>${num(T.selected.size)}</b> ürün seçildi<span class="spacer"></span>
          ${T.selected.size ? html`<button class="btn" id="tr-clear">Seçimi temizle</button>` : ''}
          <button class="btn btn-primary" id="tr-next" ${raw(T.selected.size ? '' : 'disabled')}>Pazaryerini seç →</button></div>`);
        $('#tr-clear', bar)?.addEventListener('click', () => { T.selected.clear(); refresh(); });
        $('#tr-next', bar).addEventListener('click', () => { location.hash = '#/transfer?step=marketplaces'; });
      };
      const pool = document.createElement('div');
      box.append(pool, bar);
      updateBar();
      return renderPool(pool, { selectable: true, onSelect: updateBar });
    }
    if (step === 'marketplaces') {
      if (!T.selected.size) { location.hash = '#/transfer'; return; }
      const [mps, rules] = await Promise.all([api('/api/marketplaces'), api('/api/marketplace-rules')]);
      const rmap = Object.fromEntries(rules.items.map((r) => [r.code, r]));
      box.innerHTML = renderVal(html`<div class="card"><div class="card-head"><div><h2>Hedef pazaryerleri</h2><p>${num(T.selected.size)} ürün seçildi. Her pazaryeri kendi fiyat, komisyon, kategori ve stok kuralıyla ayrı değerlendirilir.</p></div></div>
        <div class="grid grid-3">${mps.map((m) => { const r = rmap[m.code] || {}; return html`<label class="card mp-pick"><span class="row nowrap"><input type="checkbox" name="mp" value="${m.code}" ${raw(T.marketplaces.has(m.code) ? 'checked' : '')}><b>${m.name}</b></span>
          <span class="small muted">Komisyon ${pct(r.effective_commission_rate)} · maliyet üzeri kâr (markup) ${pct(r.markup_rate)} · min. net marj ${pct(r.min_margin_rate)}</span>
          <span class="small muted">Zorunlu: ${(r.required_fields || []).join(', ') || '—'}</span>
          <span class="small"><span class="dot ${r.connector?.connected ? 'good' : ''}"></span> ${r.connector?.exists ? (r.connector.connected ? 'API bağlı (salt okunur)' : 'Bağlı değil') : 'Connector yok'} · otomatik yayın kapalı</span></label>`; })}</div>
        <p class="small muted mt">Seçilen havuz ürünleri önce kataloğa alınır: barkodu katalogda olan ürün MEVCUT ürüne bağlanır (aynı ürün iki tedarikçide olsa bile tek katalog ürünü ve pazaryeri başına tek ilan).</p>
        <div class="row mt"><a class="btn" href="#/transfer">← Ürün seçimi</a><span class="spacer"></span><button class="btn btn-primary" id="tr-build">Kataloğa al ve fiyatlandır →</button></div></div>`);
      $('#tr-build', box).addEventListener('click', async (e) => {
        const chosen = $$('[name=mp]:checked', box).map((i) => i.value);
        if (!chosen.length) { toast('En az bir pazaryeri seçin', true); return; }
        T.marketplaces = new Set(chosen);
        e.target.disabled = true;
        try {
          const imp = await api('/api/transfer/import', { method: 'POST', body: { supplier_product_ids: [...T.selected] } });
          T.productIds = imp.product_ids;
          const r = await api('/api/transfer/drafts', { method: 'POST', body: { product_ids: T.productIds, marketplaces: chosen } });
          T.draftIds = r.draft_ids;
          toast(`${imp.created} yeni katalog ürünü, ${imp.linked} mevcut ürüne bağlandı · ${r.valid} taslak uygun, ${r.invalid} hatalı`);
          location.hash = '#/transfer?step=pricing';
        } catch (ex) { fail(ex); e.target.disabled = false; }
      });
      return;
    }
    if (step === 'pricing') {
      if (!T.draftIds.length) { location.hash = '#/transfer'; return; }
      return renderDrafts(box, { ids: T.draftIds, wizard: true });
    }
    if (step === 'ready') {
      const d = await api('/api/listing-drafts', { query: { ids: T.draftIds.join(','), page_size: 200 } });
      const byMp = {};
      d.items.forEach((x) => { (byMp[x.marketplace] ||= { name: x.marketplace_name, ready: 0, invalid: 0 })[x.status === 'ready' ? 'ready' : 'invalid'] += 1; });
      box.innerHTML = renderVal(html`<div class="card"><div class="card-head"><h2>Yayına hazır</h2></div>
        <div class="grid grid-3">${Object.entries(byMp).map(([code, v]) => html`<div class="card"><h3>${v.name}</h3><p><b class="pos">${num(v.ready)}</b> hazır · <b class="${v.invalid ? 'neg' : ''}">${num(v.invalid)}</b> eksik/hatalı</p>
          ${v.ready ? html`<div class="row"><a class="btn btn-sm" href="/api/listing-drafts/export.csv?marketplace=${encodeURIComponent(code)}" download>CSV indir</a>
            ${can('admin') ? html`<button class="btn btn-sm btn-primary" data-publish="${code}">Pazaryerine gönder…</button>` : ''}</div>` : ''}</div>`)}</div>
        <p class="small muted mt">Pazaryerine otomatik yükleme kapalıdır. CSV dosyasını pazaryerinin satıcı panelinden toplu ürün yükleme ile kullanabilirsiniz; hatalı taslakları “Tüm taslaklar” sekmesinden düzeltebilirsiniz.</p>
        <div class="row mt"><a class="btn" href="#/transfer?step=pricing">← Fiyat & doğrulama</a><span class="spacer"></span><button class="btn btn-primary" id="tr-new">Yeni aktarım</button></div></div>`);
      $('#tr-new', box).addEventListener('click', () => { T.selected.clear(); T.draftIds = []; T.productIds = []; location.hash = '#/transfer'; });
      $$('[data-publish]', box).forEach((b) => b.addEventListener('click', () => publishFlow(b.dataset.publish, d.items.filter((x) => x.marketplace === b.dataset.publish).map((x) => x.id))));
    }
  },
};

// ---- Kontrollü yayın: önizleme -> kapılar -> tek kullanımlık onay (yalnızca yönetici)
async function publishFlow(code, draftIds) {
  if (!draftIds.length) { toast('Taslak seçin', true); return; }
  let pv;
  try { pv = await api('/api/publish/preview', { method: 'POST', body: { marketplace: code, draft_ids: draftIds } }); } catch (e) { fail(e); return; }
  const body = openModal(html`<h2>Yayın onayı · ${pv.marketplace_name}</h2>
    <div class="notice ${pv.sendable.length ? 'info' : 'warn'}"><b>${pv.summary}</b>${pv.blocked_summary ? html` · ${pv.blocked_summary}` : ''}</div>
    <h3 class="mt">Güvenlik kontrolleri</h3>
    <ul class="gate-list">${pv.gates.map((g) => html`<li class="${g.ok ? 'ok' : 'no'}"><span aria-hidden="true">${g.ok ? '✅' : '⛔'}</span><span><b>${g.label}</b>${g.hint ? html`<span class="small muted" style="display:block">${g.hint}</span>` : ''}</span></li>`)}</ul>
    ${pv.sendable.length ? html`<details class="mt" open><summary>Gönderilecek ürünler (${pv.sendable.length})</summary><ul class="small">${pv.sendable.map((x) => html`<li>${x.title} · ${x.barcode || '—'} · ${money(x.price)} · stok ${num(x.stock)}</li>`)}</ul></details>` : ''}
    ${pv.blocked.length ? html`<details class="mt"><summary>Gönderilmeyecek ürünler (${pv.blocked.length})</summary><ul class="small">${pv.blocked.map((x) => html`<li><b>${x.title || `Taslak #${x.draft_id}`}</b>${x.reasons.map((r) => html`<span class="neg" style="display:block">• ${r}</span>`)}</li>`)}</ul></details>` : ''}
    ${pv.notice ? html`<div class="notice warn mt">${pv.notice}</div>` : ''}
    <div class="row mt"><span class="spacer"></span><button class="btn" data-close>Vazgeç</button>
      <button class="btn btn-primary" id="pub-confirm" ${raw(pv.can_confirm ? '' : 'disabled')}>${pv.can_confirm ? `Onayla ve ${pv.sendable.length} ürünü gönder` : 'Gönderim şu an mümkün değil'}</button></div>`);
  $('#pub-confirm', body)?.addEventListener('click', async (e) => {
    if (!confirm(`${pv.summary}. Onaylıyor musunuz?`)) return;
    e.target.disabled = true;
    try { const r = await api('/api/publish/confirm', { method: 'POST', body: { token: pv.token, confirm: true } }); closeLayer('modal'); toast(r.message); } catch (ex) { fail(ex); }
  });
}

async function renderDrafts(box, { ids = null, wizard = false }) {
  const mps = await api('/api/marketplaces');
  const f = { marketplace: '', status: '', q: '' };
  const load = async (page) => {
    const d = await api('/api/listing-drafts', { query: { ...f, ids: ids ? ids.join(',') : '', page, page_size: wizard ? 200 : 50 } });
    box.innerHTML = renderVal(html`<div class="card">
      ${wizard ? '' : html`<form class="filters" id="dr-f"><input type="search" name="q" value="${f.q}" placeholder="SKU, barkod, ürün adı" aria-label="Ara">
        <select name="marketplace" aria-label="Pazaryeri"><option value="">Tüm pazaryerleri</option>${mps.map((m) => html`<option value="${m.code}" ${raw(m.code === f.marketplace ? 'selected' : '')}>${m.name}</option>`)}</select>
        <select name="status" aria-label="Durum"><option value="">Aktif taslaklar</option>${Object.entries({ draft: 'Taslak', invalid: 'Hatalı', ready: 'Yayına hazır', cancelled: 'İptal' }).map(([v, l]) => html`<option value="${v}" ${raw(v === f.status ? 'selected' : '')}>${l}</option>`)}</select>
        <button class="btn btn-primary" type="submit">Filtrele</button></form>`}
      <div class="row" style="margin-bottom:10px">${Object.entries({ draft: 'Taslak', invalid: 'Hatalı', ready: 'Yayına hazır' }).map(([k, l]) => html`<span class="badge ${DRAFT_TONE[k]}">${l}: ${num(d.summary[k] || 0)}</span>`)}</div>
      ${d.items.length ? html`<div class="table-wrap"><table><thead><tr>${wizard ? '' : html`<th><input type="checkbox" id="dr-all" aria-label="Tümünü seç"></th>`}<th>Ürün</th><th>Pazaryeri</th><th>Tedarikçi</th><th class="r">Maliyet</th><th class="r">Fiyat</th><th class="r">Stok</th><th class="r">Tahmini kâr</th><th>Kategori</th><th>Durum</th></tr></thead><tbody>
        ${d.items.map((x) => html`<tr>${wizard ? '' : html`<td><input type="checkbox" data-dsel="${x.id}" aria-label="Seç"></td>`}
          <td><b class="ellipsis" title="${x.name}">${x.name}</b><span class="muted small">${x.sku} · ${x.barcode || 'barkod yok'}</span></td>
          <td>${x.marketplace_name}</td><td>${x.supplier_name || '—'}</td><td class="r num">${x.cost_basis ? money(x.cost_basis) : '—'}</td>
          <td class="r">${can('operator') && x.status !== 'cancelled' ? html`<input class="price-in" type="number" step="0.01" min="0" value="${x.price ?? ''}" data-price="${x.id}" aria-label="Fiyat">
            <span class="muted small">${x.price_is_manual ? html`elle · <a href="#" data-auto="${x.id}">otomatik</a>` : 'otomatik'}</span>` : money(x.price)}</td>
          <td class="r num">${num(x.stock)}</td>
          <td class="r num ${signClass(x.estimated_profit)}">${x.estimated_profit === null ? '—' : money(x.estimated_profit)}<span class="muted small">${pct(x.estimated_margin)}</span></td>
          <td>${x.category_id ? html`<span class="small">${x.category_name || x.category_id}</span>` : can('operator') ? html`<input class="cat-in" data-cat="${x.id}" data-mp="${x.marketplace}" data-src="${x.category || ''}" placeholder="kategori ID" aria-label="Pazaryeri kategori ID"><span class="muted small ellipsis">${x.category || 'kaynak kategori yok'}</span>` : '—'}</td>
          <td><span class="badge ${DRAFT_TONE[x.status]}">${x.status_label}</span> <button class="btn btn-sm" type="button" data-preview="${x.id}">Önizle</button>
            ${(x.errors || []).map((e) => html`<span class="small neg" style="display:block">• ${e}</span>`)}${(x.warnings || []).map((w) => html`<span class="small warn-text" style="display:block">• ${w}</span>`)}</td></tr>`)}
        </tbody></table></div>${wizard ? '' : pager(d, load)}` : empty('Taslak yok', 'Ürün Aktarımı sihirbazıyla tedarikçi ürünlerini seçip pazaryeri taslakları oluşturun.')}
      ${can('operator') && d.items.length ? html`<div class="row mt">${wizard ? html`<a class="btn" href="#/transfer?step=marketplaces">← Pazaryeri</a>` : html`<button class="btn" id="dr-cancel">Seçilenleri iptal et</button>`}<span class="spacer"></span>
        <button class="btn" id="dr-validate">Yeniden doğrula</button><button class="btn btn-primary" id="dr-prepare">${wizard ? 'Yayına hazırla →' : 'Seçilenleri yayına hazırla'}</button>
        ${!wizard && can('admin') ? html`<button class="btn" id="dr-publish" title="Önizleme ve onay ile">Seçilenleri yayınla…</button>` : ''}</div>` : ''}</div>`);
    const targetIds = () => (wizard ? d.items.map((x) => x.id) : $$('[data-dsel]:checked', box).map((c) => Number(c.dataset.dsel)));
    $$('[data-preview]', box).forEach((b) => b.addEventListener('click', () => showDraftPreview(Number(b.dataset.preview), () => load(page))));
    $('#dr-f', box)?.addEventListener('submit', (e) => { e.preventDefault(); Object.assign(f, formData(e.target)); load(1); });
    $('#dr-all', box)?.addEventListener('change', (e) => $$('[data-dsel]', box).forEach((c) => { c.checked = e.target.checked; }));
    $$('[data-price]', box).forEach((inp) => inp.addEventListener('change', async () => {
      try { await api(`/api/listing-drafts/${inp.dataset.price}`, { method: 'PATCH', body: { price: inp.value || null } }); load(page); } catch (ex) { fail(ex); }
    }));
    $$('[data-auto]', box).forEach((a) => a.addEventListener('click', async (e) => {
      e.preventDefault();
      try { await api(`/api/listing-drafts/${a.dataset.auto}`, { method: 'PATCH', body: { auto_price: true } }); load(page); } catch (ex) { fail(ex); }
    }));
    $$('[data-cat]', box).forEach((inp) => inp.addEventListener('change', async () => {
      if (!inp.value.trim()) return;
      try {
        if (inp.dataset.src) {
          await api('/api/category-mappings', { method: 'PUT', body: { marketplace: inp.dataset.mp, source_category: inp.dataset.src, target_category_id: inp.value.trim() } });
          await api('/api/listing-drafts/validate', { method: 'POST', body: { ids: d.items.map((x) => x.id) } });
          toast('Kategori eşleştirildi; aynı kaynak kategorideki ürünlere de uygulandı');
        } else {
          await api(`/api/listing-drafts/${inp.dataset.cat}`, { method: 'PATCH', body: { category_id: inp.value.trim() } });
        }
        load(page);
      } catch (ex) { fail(ex); }
    }));
    $('#dr-validate', box)?.addEventListener('click', async () => {
      const t = targetIds(); if (!t.length) { toast('Taslak seçin', true); return; }
      try { const r = await api('/api/listing-drafts/validate', { method: 'POST', body: { ids: t } }); toast(`${r.valid} uygun, ${r.invalid} hatalı`); load(page); } catch (ex) { fail(ex); }
    });
    $('#dr-prepare', box)?.addEventListener('click', async () => {
      const t = targetIds(); if (!t.length) { toast('Taslak seçin', true); return; }
      try {
        const r = await api('/api/listing-drafts/prepare', { method: 'POST', body: { ids: t } });
        toast(`${r.ready} taslak yayına hazır${r.invalid ? `, ${r.invalid} hatalı` : ''}`);
        if (wizard) location.hash = '#/transfer?step=ready'; else load(page);
      } catch (ex) { fail(ex); }
    });
    $('#dr-publish', box)?.addEventListener('click', () => {
      const chosen = d.items.filter((x) => targetIds().includes(x.id));
      const codes = [...new Set(chosen.map((x) => x.marketplace))];
      if (codes.length !== 1) { toast(chosen.length ? 'Tek seferde tek pazaryeri seçin' : 'Taslak seçin', true); return; }
      publishFlow(codes[0], chosen.map((x) => x.id));
    });
    $('#dr-cancel', box)?.addEventListener('click', async () => {
      const t = targetIds(); if (!t.length) { toast('Taslak seçin', true); return; }
      if (!confirm(`${t.length} taslak iptal edilsin mi? (Pazaryerinde hiçbir şey değişmez.)`)) return;
      try { await api('/api/listing-drafts/cancel', { method: 'POST', body: { ids: t } }); load(page); } catch (ex) { fail(ex); }
    });
  };
  await load(1);
}

async function showDraftPreview(id, onChange) {
  const p = await api(`/api/listing-drafts/${id}/preview`);
  const pl = p.payload, pr = p.pricing, req = new Set(p.required_attributes || []);
  const attrKeys = Array.from(new Set([...req, ...Object.keys(pl.attributes || {})]));
  const body = openDrawer(html`<h2>Yayın önizleme · ${p.draft.marketplace_name}</h2>
    <div class="notice ${p.publish.can_publish ? 'info' : 'warn'}" style="margin-bottom:12px"><b>Pazaryerine gönderilmeyecek.</b> ${p.publish.message}</div>
    <div class="row" style="margin-bottom:12px"><span class="badge ${DRAFT_TONE[p.draft.status]}">${p.draft.status_label}</span>
      ${p.valid ? html`<span class="badge tone-good">Doğrulama geçti</span>` : html`<span class="badge tone-bad">${p.errors.length} hata</span>`}
      <span class="badge plain">${p.publish.connector ? (p.publish.connected ? 'API bağlı (salt okunur)' : 'Bağlı değil') : 'Connector yok'}</span></div>
    ${p.errors.length || p.warnings.length ? html`<div class="stack" style="gap:4px;margin-bottom:12px">${p.errors.map((e) => html`<span class="small neg">• ${e}</span>`)}${p.warnings.map((w) => html`<span class="small warn-text">• ${w}</span>`)}</div>` : ''}
    <h3>Fiyat ve kâr ${TAHMINI}</h3>
    ${pr ? html`<div class="table-wrap"><table><tbody>
      <tr><td>Satış fiyatı</td><td class="r num"><b>${money(pr.price)}</b></td></tr>
      <tr><td>− Komisyon (${pct(pr.commission_rate)})</td><td class="r num">${money(pr.commission)}</td></tr>
      <tr><td>− Kargo</td><td class="r num">${money(pr.shipping)}</td></tr>
      <tr><td>− Sabit gider</td><td class="r num">${money(pr.fixed)}</td></tr>
      <tr><td>− Ürün maliyeti (${pl.supplier || '—'})</td><td class="r num">${money(pr.cost)}</td></tr>
      <tr><td>− Tahmini KDV farkı</td><td class="r num">${money(pr.vat)}</td></tr>
      <tr><td><b>Tahmini kâr</b></td><td class="r num ${signClass(pr.profit)}"><b>${money(pr.profit)}</b> <span class="muted small">net marj ${pct(pr.margin)} · markup ${pct(pr.markup)} · min. marj ${pct(pr.min_margin_rate)}</span></td></tr>
    </tbody></table></div>` : html`<p class="muted small">Maliyet veya fiyat olmadığı için hesaplanamadı.</p>`}
    <h3 class="mt">Kategori ve özellikler</h3>
    ${can('operator') ? html`<form class="stack" id="pv-form">
      <label>Pazaryeri kategori ID<input name="category_id" value="${pl.category_id || ''}" maxlength="100" placeholder="boş bırakılırsa kategori eşleştirmesi kullanılır"></label>
      <div class="table-wrap"><table><thead><tr><th>Özellik</th><th>Değer</th></tr></thead><tbody>
        ${attrKeys.map((k) => html`<tr><td>${k}${req.has(k) ? html` <span class="badge plain tone-warn">zorunlu</span>` : ''}</td><td><input data-attr="${k}" value="${(pl.attributes || {})[k] || ''}" maxlength="300" aria-label="${k}"></td></tr>`)}
        <tr><td><input id="pv-newk" placeholder="yeni özellik adı" maxlength="100" aria-label="Yeni özellik adı"></td><td><input id="pv-newv" placeholder="değer" maxlength="300" aria-label="Yeni özellik değeri"></td></tr>
      </tbody></table></div>
      <p class="form-error"></p><div class="row"><button class="btn" type="button" id="pv-reset">Elle girilenleri sıfırla</button><span class="spacer"></span><button class="btn btn-primary" type="submit">Kaydet ve doğrula</button></div></form>`
      : html`<p>${pl.category_name || pl.category_id || '—'}</p>`}
    <h3 class="mt">Gönderilecek alanlar (önizleme)</h3>
    <p class="small muted">${p.payload_note}</p>
    <dl class="kv"><dt>Barkod</dt><dd>${pl.barcode || '—'}</dd><dt>Stok kodu</dt><dd>${pl.sku || '—'}</dd><dt>Model kodu</dt><dd>${pl.model_code || '—'}</dd>
      <dt>Başlık</dt><dd>${pl.title}</dd><dt>Marka</dt><dd>${pl.brand || '—'}</dd><dt>Kategori</dt><dd>${pl.category_name || pl.category_id || '—'}</dd>
      <dt>Fiyat</dt><dd>${money(pl.price)}</dd><dt>Stok</dt><dd>${num(pl.stock)}</dd><dt>KDV</dt><dd>%${pl.vat_rate ?? '—'}</dd><dt>Desi</dt><dd>${pl.desi ?? '—'}</dd>
      <dt>Görseller</dt><dd>${num((pl.images || []).length)} adet</dd><dt>Açıklama</dt><dd><span class="ellipsis" title="${pl.description || ''}">${pl.description || '—'}</span></dd></dl>`);
  const form = $('#pv-form', body);
  form?.addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(form, async () => {
      const attributes = {};
      $$('[data-attr]', form).forEach((i) => { attributes[i.dataset.attr] = i.value.trim(); });
      const nk = $('#pv-newk', form).value.trim();
      if (nk) attributes[nk] = $('#pv-newv', form).value.trim();
      await api(`/api/listing-drafts/${id}`, { method: 'PATCH', body: { category_id: form.category_id.value.trim(), attributes } });
      toast('Taslak güncellendi'); onChange?.(); showDraftPreview(id, onChange);
    });
  });
  $('#pv-reset', body)?.addEventListener('click', async () => {
    try { await api(`/api/listing-drafts/${id}`, { method: 'PATCH', body: { reset_attributes: true, category_id: '' } }); toast('Eşleştirme varsayılanlarına dönüldü'); onChange?.(); showDraftPreview(id, onChange); } catch (ex) { fail(ex); }
  });
}

async function renderRules(box) {
  const [rules, mps] = await Promise.all([api('/api/marketplace-rules'), api('/api/marketplaces')]);
  let mp = mps[0]?.code || 'trendyol';
  const drawCats = async () => {
    const d = await api('/api/category-mappings', { query: { marketplace: mp } });
    $('#cat-box', box).innerHTML = renderVal(d.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Kaynak kategori</th><th class="r">Katalog ürünü</th><th>Pazaryeri kategori ID</th><th>Kategori adı</th><th>Varsayılan özellikler (ad=değer; …)</th><th>Zorunlu özellikler (virgülle)</th></tr></thead><tbody>
      ${d.items.map((c) => html`<tr><td>${c.source_category}</td><td class="r num">${num(c.product_count)}</td>
        <td>${can('operator') ? html`<input data-cid="${c.source_category}" value="${c.target_category_id || ''}" placeholder="eşleştirilmedi" aria-label="Kategori ID">` : c.target_category_id || '—'}</td>
        <td>${can('operator') ? html`<input data-cname="${c.source_category}" value="${c.target_category_name || ''}" aria-label="Kategori adı">` : c.target_category_name || ''}</td>
        <td>${can('operator') ? html`<input data-cattr="${c.source_category}" value="${Object.entries(c.attributes || {}).map(([k, v]) => `${k}=${v}`).join('; ')}" placeholder="Renk=Kahverengi; Materyal=Deri" aria-label="Kategori özellikleri">` : Object.entries(c.attributes || {}).map(([k, v]) => `${k}=${v}`).join('; ')}</td>
        <td>${can('operator') ? html`<input data-creq="${c.source_category}" value="${(c.required_attributes || []).join(', ')}" placeholder="Renk, Materyal" aria-label="Zorunlu özellikler">` : (c.required_attributes || []).join(', ')}</td></tr>`)}</tbody></table></div>`
      : empty('Kategori yok', 'Tedarikçi ürünleri senkronize edildiğinde kaynak kategoriler burada listelenir.'));
    $$('[data-cid]', box).forEach((inp) => {
      const save = async () => {
        const id = inp.value.trim(); if (!id) return;
        const name = $$('[data-cname]', box).find((x) => x.dataset.cname === inp.dataset.cid)?.value || null;
        const rawAttrs = $$('[data-cattr]', box).find((x) => x.dataset.cattr === inp.dataset.cid)?.value || '';
        const attributes = Object.fromEntries(rawAttrs.split(';').map((x) => x.split('=')).filter((kv) => kv.length === 2 && kv[0].trim()).map(([k, v]) => [k.trim(), v.trim()]));
        const rawReq = $$('[data-creq]', box).find((x) => x.dataset.creq === inp.dataset.cid)?.value || '';
        const required_attributes = rawReq.split(',').map((x) => x.trim()).filter(Boolean);
        try { await api('/api/category-mappings', { method: 'PUT', body: { marketplace: mp, source_category: inp.dataset.cid, target_category_id: id, target_category_name: name, attributes, required_attributes } }); toast('Kategori eşleştirmesi kaydedildi'); } catch (ex) { fail(ex); }
      };
      inp.addEventListener('change', save);
      $$('[data-cname]', box).find((x) => x.dataset.cname === inp.dataset.cid)?.addEventListener('change', save);
      $$('[data-cattr]', box).find((x) => x.dataset.cattr === inp.dataset.cid)?.addEventListener('change', save);
      $$('[data-creq]', box).find((x) => x.dataset.creq === inp.dataset.cid)?.addEventListener('change', save);
    });
  };
  box.innerHTML = renderVal(html`${can('admin') ? html`<div class="row" style="margin-bottom:12px"><span class="spacer"></span><button class="btn" id="add-mp">+ Yeni pazaryeri</button></div>` : ''}
    <div class="grid grid-3">${rules.items.map((r) => html`<form class="card rule-form" data-rule="${r.code}"><div class="card-head"><h2>${r.name}</h2>
      <span class="badge ${r.connector.connected ? 'tone-good' : ''}">${r.connector.exists ? (r.connector.connected ? 'Bağlı' : 'Bağlı değil') : 'Connector yok'}</span></div>
      <p class="small muted" style="margin-top:0">${r.connector.reason}</p>
      <div class="form-grid">
        <label>Komisyon oranı<input name="commission_rate" type="number" step="0.001" min="0" max="0.99" value="${r.commission_rate ?? ''}" placeholder="${r.effective_commission_rate} (Ayarlar)"></label>
        <label title="Fiyat hesaplanırken maliyetin üzerine eklenen oran (markup); marjla aynı şey değildir">Maliyet üzeri kâr (Markup)<input name="markup_rate" type="number" step="0.01" min="0" value="${r.markup_rate}"></label>
        <label>Kargo (₺)<input name="shipping_cost" type="number" step="0.01" min="0" value="${r.shipping_cost}"></label>
        <label>Sabit gider (₺)<input name="fixed_cost" type="number" step="0.01" min="0" value="${r.fixed_cost}"></label>
        <label title="Tahmini KDV sonrası kâr ÷ satış fiyatı">Minimum net marj<input name="min_margin_rate" type="number" step="0.01" min="-1" max="1" value="${r.min_margin_rate}"></label>
        <label>Yuvarlama<select name="rounding">${[['x.90', 'x,90'], ['x.99', 'x,99'], ['integer', 'Tam sayı'], ['none', 'Yok']].map(([v, l]) => html`<option value="${v}" ${raw(v === r.rounding ? 'selected' : '')}>${l}</option>`)}</select></label>
        <label>Stok güvenlik payı<input name="stock_buffer" type="number" min="0" value="${r.stock_buffer}"></label>
        <label>Minimum stok<input name="min_stock" type="number" min="0" value="${r.min_stock}"></label>
        <label>Maksimum stok<input name="max_stock" type="number" min="0" value="${r.max_stock ?? ''}" placeholder="sınırsız"></label>
        <label>Başlık uzunluğu sınırı<input name="title_max_length" type="number" min="10" value="${r.title_max_length ?? ''}" placeholder="yok"></label>
        <fieldset class="full fieldset"><legend>Zorunlu alanlar</legend><div class="row">${rules.requirable_fields.map((fld) => html`<label class="check"><input type="checkbox" name="rf" value="${fld}" ${raw((r.required_fields || []).includes(fld) ? 'checked' : '')}>${{ barcode: 'Barkod', brand: 'Marka', category: 'Kategori', images: 'Görsel', description: 'Açıklama', model_code: 'Model kodu', desi: 'Desi', vat_rate: 'KDV' }[fld] || fld}</label>`)}</div></fieldset>
      </div>
      ${can('admin') ? html`<p class="form-error"></p><div class="row mt"><span class="spacer"></span><button class="btn btn-primary btn-sm" type="submit">Kaydet</button></div>` : ''}</form>`)}</div>
    <p class="small muted">Fiyat = (maliyet × (1 + markup) + kargo + sabit gider) ÷ (1 − komisyon), sonra yuvarlanır. Markup maliyete göre, net marj satış fiyatına göre hesaplanır. Değerler varsayılandır; pazaryerinin güncel komisyon ve zorunlu alan kurallarına göre düzenleyin. Tahmini kâr TAHMİNİDİR.</p>
    <div class="card mt"><div class="card-head"><div><h2>Kategori eşleştirme</h2><p>Kaynak (tedarikçi/katalog) kategorisini pazaryeri kategori ID'sine bağlayın.</p></div>
      <select id="cat-mp" aria-label="Pazaryeri">${mps.map((m) => html`<option value="${m.code}">${m.name}</option>`)}</select></div><div id="cat-box"></div></div>`);
  $$('[data-rule]', box).forEach((form) => form.addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(form, async () => {
      const v = formData(form);
      await api(`/api/marketplace-rules/${form.dataset.rule}`, { method: 'PUT', body: {
        commission_rate: v.commission_rate === '' ? null : v.commission_rate, markup_rate: v.markup_rate, shipping_cost: v.shipping_cost,
        fixed_cost: v.fixed_cost, min_margin_rate: v.min_margin_rate, rounding: v.rounding, stock_buffer: Number(v.stock_buffer || 0),
        min_stock: Number(v.min_stock || 0), max_stock: v.max_stock === '' ? null : Number(v.max_stock),
        title_max_length: v.title_max_length === '' ? null : Number(v.title_max_length), required_fields: $$('[name=rf]:checked', form).map((c) => c.value) } });
      toast('Kural kaydedildi · taslakları “Yeniden doğrula” ile güncelleyin');
    });
  }));
  $('#cat-mp', box).addEventListener('change', (e) => { mp = e.target.value; drawCats(); });
  $('#add-mp', box)?.addEventListener('click', () => {
    const body = openModal(html`<h2>Yeni pazaryeri</h2><form class="stack" id="mpform">
      <label>Kod<input name="code" required pattern="[a-z0-9_]+" maxlength="40" placeholder="ör. n11"></label>
      <label>Ad<input name="name" required minlength="2" maxlength="100" placeholder="ör. N11"></label>
      <p class="small muted">Yeni pazaryeri kendi fiyat, stok, zorunlu alan ve kategori kurallarıyla taslak hazırlamada kullanılır. API connector'ı olmadığı için “Bağlı değil” görünür; sipariş/ilan verisi üretilmez.</p>
      <p class="form-error"></p><div class="row"><span class="spacer"></span><button class="btn" type="button" data-close>Vazgeç</button><button class="btn btn-primary" type="submit">Ekle</button></div></form>`);
    $('#mpform', body).addEventListener('submit', (e) => {
      e.preventDefault();
      submitting(e.target, async () => { await api('/api/marketplaces', { method: 'POST', body: formData(e.target) }); closeLayer('modal'); toast('Pazaryeri eklendi'); refresh(); });
    });
  });
  await drawCats();
}

// ---- Finans
const EXPENSE_LABELS = { advertising: 'Reklam', shipping: 'Kargo', packaging: 'Ambalaj', personnel: 'Personel', rent: 'Kira', software: 'Yazılım', other: 'Diğer' };
PAGES.finance = {
  title: 'Finans', icon: 'finance',
  async render() {
    const draw = async () => {
      setHeader('Finans', 'Gerçek ve tahmini tutarlar ayrı gösterilir', html`${periodSeg(state.period, draw)}${canFinance() ? html`<button class="btn btn-primary" id="add-exp">+ Gider ekle</button>` : ''}`);
      loading();
      const [f, ex, st] = await Promise.all([api('/api/finance/summary', { query: periodQuery() }),
        api('/api/finance/expenses', { query: { ...periodQuery(), page_size: 50 } }), api('/api/finance/statement', { query: periodQuery() })]);
      const t = f.orders;
      const qs = periodQs();
      const BASIS_TONE = { actual: 'tone-good', estimate: 'tone-warn', entered: 'tone-info', mixed: 'tone-warn' };
      const TOTALS = new Set(['net_sales', 'contribution', 'net_after_vat']);
      view().innerHTML = renderVal(html`
        <div class="card" style="margin-bottom:16px"><div class="card-head"><div><h2>Finans tablosu</h2><p>${date(st.range.from)} – ${date(st.range.to)} · ${num(st.orders)} sipariş · <b>Gerçek</b> = pazaryeri verisi, <b>Tahmini</b> = TrendHub hesabı, <b>Girilen</b> = kullanıcı kaydı</p></div>
          <div class="row"><a class="btn btn-sm" href="/api/finance/statement.csv?${qs}" download>Tablo (CSV)</a><a class="btn btn-sm" href="/api/finance/orders.csv?${qs}" download>Siparişler (CSV)</a><a class="btn btn-sm" href="/api/finance/expenses.csv?${qs}" download>Giderler (CSV)</a><a class="btn btn-sm" href="/api/ads/spend.csv?${qs}" download>Reklam (CSV)</a></div></div>
          <div class="table-wrap"><table class="statement"><tbody>${st.lines.map((l) => html`<tr class="${TOTALS.has(l.key) ? 'total' : ''} ${l.in_total === false ? 'info-line' : ''}"><td>${l.label}</td><td><span class="badge plain ${BASIS_TONE[l.basis]}">${l.basis_label}</span></td><td class="r num ${signClass(l.amount)}">${money(l.amount)}</td></tr>`)}
            <tr class="total"><td title="Tahmini KDV sonrası kâr ÷ net satış">Net marj (KDV sonrası)</td><td></td><td class="r num">${pct(st.margin_after_tax)}</td></tr>
            <tr><td title="Tahmini KDV sonrası kâr ÷ ürün maliyeti">Maliyet üzeri kâr (Markup)</td><td></td><td class="r num">${pct(st.markup_after_tax)}</td></tr></tbody></table></div>
          ${st.warnings.map((w) => html`<p class="small warn-text">• ${w}</p>`)}</div>
        <div class="notice warn" style="margin-bottom:16px"><b>TAHMİNİ:</b> Pazaryeri hakediş (settlement) verisi henüz bağlı değil. Komisyon ve hizmet bedeli Ayarlar'daki oranlarla tahmin edilir. KDV = satış KDV'si − maliyet KDV'si − (Ayarlar'da belirtildiyse) komisyon ve gider KDV'si. Satıcı indirimi ciroya zaten yansımıştır, tekrar düşülmez; marjların paydası net satıştır (ciro − iade). Gerçek tutarları sipariş detayından “Gerçek gider gir” ile ekleyebilirsiniz.${t.estimated_orders ? html` Bu dönemde <b>${num(t.estimated_orders)}</b> siparişte tahmini veya eksik değer var.` : ''}</div>
        <div class="grid grid-4">
          ${kpi('Ciro', money0(t.revenue), `${num(t.orders)} sipariş · ort. ${t.average_order_value === null ? '—' : money(t.average_order_value)}`)}
          ${kpi('Sipariş giderleri', money0(t.total_cost), 'Maliyet + komisyon + kargo + hizmet + reklam + iade + diğer', '', TAHMINI)}
          ${kpi('Dönem giderleri', money0(f.expenses.total), 'Siparişe bağlı olmayan')}
          ${kpi('Tahmini KDV sonrası kâr', money0(f.net_profit_after_tax), html`Net marj ${pct(f.margin_after_tax)} · Markup ${pct(f.markup_after_tax)}${vatNote(f.vat)}`, signClass(f.net_profit_after_tax), TAHMINI)}
        </div>
        <div class="grid grid-4 mt">
          ${kpi('Tahmini KDV', money0(f.tax_estimate), 'Satış KDV − maliyet, komisyon ve gider KDV\'si', '', TAHMINI)}
          ${kpi('KDV öncesi kâr', money0(f.net_profit_after_expenses), `Net satış ${money0(f.net_sales)} · marj ${pct(f.margin_after_expenses)}`, signClass(f.net_profit_after_expenses), TAHMINI)}
          ${kpi('Satıcı indirimi', money0(t.discount), 'Ciroya yansımış; ayrıca düşülmez')}
          ${kpi('İade tutarı', money0(t.refund), 'İade edilen siparişlerin cirosu')}
        </div>
        <div class="grid grid-2 mt">
          <div class="card"><div class="card-head"><div><h2>Kâr / zarar dökümü ${TAHMINI}</h2><p>Ciro − ürün maliyeti − komisyon − hizmet bedeli − kargo − reklam − iade − diğer</p></div></div>${profitBars(t, { periodExpenses: f.expenses.total, net: f.net_profit_after_expenses, tax: f.tax_estimate, netAfterTax: f.net_profit_after_tax })}</div>
          <div class="card"><div class="card-head"><h2>Günlük</h2></div>${lineChart(f.daily, [{ key: 'revenue', label: 'Ciro', color: 'var(--series-1)' }, { key: 'net_profit', label: 'Net kâr', color: 'var(--series-2)' }])}</div>
        </div>
        <div class="card mt"><div class="card-head"><h2>Pazaryerine göre</h2></div><div class="table-wrap"><table><thead><tr><th>Pazaryeri</th><th class="r">Sipariş</th><th class="r">Ciro</th><th class="r">Ürün maliyeti</th><th class="r">Komisyon</th><th class="r">Hizmet</th><th class="r">Kargo</th><th class="r">Reklam</th><th class="r">İade</th><th class="r">Net kâr</th><th class="r">Tahmini KDV</th><th class="r">Marj</th></tr></thead><tbody>
          ${f.by_marketplace.map((m) => html`<tr><td><b>${m.name}</b></td><td class="r num">${num(m.orders)}</td><td class="r num">${money(m.revenue)}</td><td class="r num">${money(m.product_cost)}</td><td class="r num">${money(m.commission)}</td><td class="r num">${money(m.service_fee)}</td><td class="r num">${money(m.shipping)}</td><td class="r num">${money(m.advertising)}</td><td class="r num">${money(m.refund)}</td><td class="r num ${signClass(m.net_profit)}">${money(m.net_profit)}</td><td class="r num">${money(m.tax_estimate)}</td><td class="r num">${pct(m.margin_after_vat ?? m.margin_before_vat)}</td></tr>`)}
        </tbody></table></div></div>
        <div class="card mt"><div class="card-head"><div><h2>Dönem giderleri</h2><p>Reklam, ambalaj, personel gibi siparişe bağlı olmayan giderler. SKU girilen reklam giderleri SKU raporuna yansır.</p></div></div>
          ${ex.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Tarih</th><th>Kategori</th><th>Açıklama</th><th>Pazaryeri</th><th>SKU</th><th class="r">Tutar</th><th></th></tr></thead><tbody>
          ${ex.items.map((e) => html`<tr><td>${date(e.expense_date)}</td><td>${e.category_label}</td><td>${e.description || ''}</td><td>${e.marketplace_name || '—'}</td><td>${e.sku || '—'}</td><td class="r num">${money(e.amount)}</td>
            <td class="r">${canFinance() && e.source === 'manual' ? html`<button class="btn btn-sm btn-danger" data-delexp="${e.id}">Sil</button>` : ''}</td></tr>`)}</tbody></table></div>` : empty('Gider yok', 'Bu dönem için gider girilmemiş.')}</div>`);
      $('#add-exp')?.addEventListener('click', expenseForm);
      $$('[data-delexp]').forEach((b) => b.addEventListener('click', async () => {
        if (!confirm('Bu gider kaydı silinsin mi? (İşlem denetim kaydına yazılır.)')) return;
        try { await api(`/api/finance/expenses/${b.dataset.delexp}`, { method: 'DELETE' }); toast('Gider silindi'); draw(); } catch (e) { fail(e); }
      }));
    };
    await draw();
  },
};
async function expenseForm() {
  const mps = await api('/api/marketplaces');
  const body = openModal(html`<h2>Gider ekle</h2><form class="form-grid" id="eform">
    <label>Kategori<select name="category">${Object.entries(EXPENSE_LABELS).map(([v, l]) => html`<option value="${v}">${l}</option>`)}</select></label>
    <label>Tutar (₺)<input name="amount" type="number" step="0.01" min="0.01" required></label>
    <label>Tarih<input name="expense_date" type="date" value="${todayIso()}" required></label>
    <label>Pazaryeri<select name="marketplace"><option value="">Genel</option>${mps.map((m) => html`<option value="${m.code}">${m.name}</option>`)}</select></label>
    <label class="full">SKU (reklam giderini ürüne bağlamak için)<input name="sku" maxlength="100"></label>
    <label class="full">Açıklama<input name="description" maxlength="500"></label>
    <p class="form-error full"></p><div class="full row"><span class="spacer"></span><button class="btn" type="button" data-close>Vazgeç</button><button class="btn btn-primary" type="submit">Kaydet</button></div></form>`);
  $('#eform', body).addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(e.target, async () => {
      const d = formData(e.target);
      await api('/api/finance/expenses', { method: 'POST', body: { ...d, marketplace: d.marketplace || null, sku: d.sku || null, description: d.description || null } });
      closeLayer('modal'); toast('Gider kaydedildi'); refresh();
    });
  });
}

// ---- Reklamlar
const optDash = (v, f) => (v === null || v === undefined ? html`<span class="muted" title="Veri yok">—</span>` : f(v));
const adRatio = (v) => optDash(v, (x) => `${Number(x).toLocaleString('tr-TR', { maximumFractionDigits: 2 })}x`);
PAGES.ads = {
  title: 'Reklamlar', icon: 'ads',
  async render() {
    const draw = async () => {
      setHeader('Reklamlar', 'Trendyol Reklam, Meta, Google — harcama ve performans', html`${periodSeg(state.period, draw)}${canFinance() ? html`<button class="btn" id="ad-acc">+ Hesap</button><button class="btn" id="ad-camp">+ Kampanya</button><button class="btn" id="ad-perf">+ Performans</button><button class="btn btn-primary" id="ad-spend">+ Harcama</button>` : ''}`);
      loading();
      const [s, sp, accounts, camps, chans] = await Promise.all([api('/api/ads/summary', { query: periodQuery() }),
        api('/api/ads/spend', { query: { ...periodQuery(), page_size: 50 } }), api('/api/ads/accounts'), api('/api/ads/campaigns'), api('/api/ads/channels')]);
      const d = s.direct, g = s.general;
      const metricRow = (x) => html`<td class="r num">${money(x.spend)}</td><td class="r num">${optDash(x.impressions, num)}</td><td class="r num">${optDash(x.clicks, num)}</td>
        <td class="r num">${optDash(x.attributed_orders, num)}</td><td class="r num">${optDash(x.attributed_revenue, money)}</td><td class="r num">${adRatio(x.roas)}</td><td class="r num">${optDash(x.cpa, money)}</td><td class="r num">${optDash(x.conversion_rate, pct)}</td>`;
      const metricHead = html`<th class="r">Harcama</th><th class="r">Gösterim</th><th class="r">Tıklama</th><th class="r">Reklam siparişi</th><th class="r">Reklam cirosu</th><th class="r">ROAS</th><th class="r">CPA</th><th class="r">Dönüşüm</th>`;
      view().innerHTML = renderVal(html`
        <div class="notice info" style="margin-bottom:16px">Reklam platformu API'leri henüz bağlı değil: harcama ve performans <b>elle</b> girilir. TrendHub reklam ilişkilendirmesi <b>uydurmaz</b>; veri yoksa “—” gösterilir.</div>
        <div class="grid grid-4">
          ${kpi('Reklam harcaması', money0(d.spend), `${date(s.range.from)} – ${date(s.range.to)}`)}
          ${kpi('Doğrudan ilişkilendirilmiş ciro', d.has_attribution ? money0(d.attributed_revenue) : '—', d.has_attribution ? `${num(d.attributed_orders)} sipariş` : 'Platform verisi girilmedi')}
          ${kpi('ROAS', d.roas === null ? '—' : adRatio(d.roas), 'Reklam cirosu / harcama')}
          ${kpi('CPA', d.cpa === null ? '—' : money(d.cpa), 'Harcama / reklam siparişi')}
        </div>
        <div class="grid grid-2 mt">
          <div class="card"><h3>Doğrudan ilişkilendirilmiş</h3><p class="small muted">${d.note}</p>
            <dl class="kv mt"><dt>Gösterim</dt><dd>${optDash(d.impressions, num)}</dd><dt>Tıklama</dt><dd>${optDash(d.clicks, num)}</dd><dt>TO (CTR)</dt><dd>${optDash(d.ctr, pct)}</dd><dt>Dönüşüm oranı</dt><dd>${optDash(d.conversion_rate, pct)}</dd></dl></div>
          <div class="card"><h3>Genel dönem analizi</h3><p class="small muted">${g.note}</p>
            <dl class="kv mt"><dt>Dönem cirosu</dt><dd>${money(g.revenue)}</dd><dt>Sipariş</dt><dd>${num(g.orders)}</dd><dt>Harcama / ciro</dt><dd>${optDash(g.spend_share_of_revenue, pct)}</dd><dt>Sipariş başı harcama</dt><dd>${optDash(g.cost_per_order, money)}</dd></dl></div>
        </div>
        <div class="card mt"><div class="card-head"><h2>Kanallar</h2></div>${s.by_channel.length ? html`<div class="table-wrap"><table><thead><tr><th>Kanal</th>${metricHead}</tr></thead><tbody>
          ${s.by_channel.map((c) => html`<tr><td><b>${c.channel_label}</b></td>${metricRow(c)}</tr>`)}</tbody></table></div>` : empty('Kanal yok', 'Önce bir reklam hesabı ve kampanya ekleyin.')}</div>
        <div class="card mt"><div class="card-head"><h2>Kampanyalar</h2></div>${s.by_campaign.length ? html`<div class="table-wrap"><table><thead><tr><th>Kampanya</th><th>Kanal</th>${metricHead}</tr></thead><tbody>
          ${s.by_campaign.map((c) => html`<tr><td><b>${c.name}</b><span class="muted small">${{ active: 'Aktif', paused: 'Duraklatıldı', ended: 'Bitti' }[c.status] || c.status}</span></td><td>${c.channel_label}</td>${metricRow(c)}</tr>`)}</tbody></table></div>` : empty('Kampanya yok', 'Harcama girmek için önce kampanya oluşturun.')}</div>
        <div class="card mt"><div class="card-head"><div><h2>Reklam sonrası ürün kârı ${TAHMINI}</h2><p>Kampanya harcaması, kampanyaya bağlı ürünlere eşit bölünür.</p></div></div>
          ${s.products.length ? html`<div class="table-wrap"><table><thead><tr><th>Ürün</th><th class="r">Adet</th><th class="r">Ciro</th><th class="r">Reklam öncesi kâr</th><th class="r">Reklam payı</th><th class="r">Reklam sonrası kâr</th></tr></thead><tbody>
          ${s.products.map((p) => html`<tr><td><b class="ellipsis">${p.name}</b><span class="muted small">${p.sku}</span></td><td class="r num">${num(p.quantity)}</td><td class="r num">${money(p.revenue)}</td><td class="r num">${money(p.profit_before_ads)}</td><td class="r num">${money(p.ad_spend)}</td><td class="r num ${signClass(p.profit_after_ads)}">${money(p.profit_after_ads)}</td></tr>`)}</tbody></table></div>` : empty('Ürün bağlı kampanya yok', 'Kampanyaya ürün bağladığınızda reklam sonrası kâr burada görünür.')}</div>
        <div class="card mt"><div class="card-head"><h2>Harcama kayıtları</h2><a class="btn btn-sm" href="/api/ads/spend.csv?${periodQs()}" download>CSV indir</a></div>
          ${sp.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Tarih</th><th>Kanal</th><th>Kampanya</th><th>Not</th><th>Giren</th><th class="r">Tutar</th><th></th></tr></thead><tbody>
          ${sp.items.map((x) => html`<tr><td>${date(x.spend_date)}</td><td>${x.channel_label}</td><td>${x.campaign_name}</td><td>${x.note || ''}</td><td>${x.created_by_name || '—'}</td><td class="r num">${money(x.amount)}</td>
            <td class="r">${canFinance() && x.source === 'manual' ? html`<button class="btn btn-sm btn-danger" data-delspend="${x.id}">Sil</button>` : ''}</td></tr>`)}</tbody></table></div>` : empty('Harcama yok', 'Bu dönem için reklam harcaması girilmemiş.')}</div>`);
      const saved = (m) => { closeLayer('modal'); toast(m); draw(); };
      const form = (title, inner, submit) => {
        const body = openModal(html`<h2>${title}</h2><form class="form-grid" id="adform">${inner}<p class="form-error full"></p><div class="full row"><span class="spacer"></span><button class="btn" type="button" data-close>Vazgeç</button><button class="btn btn-primary" type="submit">Kaydet</button></div></form>`);
        $('#adform', body).addEventListener('submit', (e) => { e.preventDefault(); submitting(e.target, () => submit(formData(e.target), e.target)); });
        return body;
      };
      const campSelect = () => camps.length ? html`<label class="full">Kampanya<select name="campaign_id" required>${camps.map((c) => html`<option value="${c.id}">${c.name} · ${c.channel_label}</option>`)}</select></label>` : html`<p class="full notice warn">Önce kampanya ekleyin.</p>`;
      $('#ad-acc')?.addEventListener('click', () => form('Reklam hesabı', html`<label>Kanal<select name="channel">${chans.map((c) => html`<option value="${c.code}">${c.label}</option>`)}</select></label><label>Hesap adı<input name="name" required maxlength="120"></label>`,
        async (d) => { await api('/api/ads/accounts', { method: 'POST', body: d }); saved('Hesap eklendi'); }));
      $('#ad-camp')?.addEventListener('click', async () => {
        if (!accounts.length) { toast('Önce reklam hesabı ekleyin', true); return; }
        const [mps, prods] = await Promise.all([api('/api/marketplaces'), api('/api/products', { query: { page_size: 200 } })]);
        const body = form('Kampanya', html`<label>Hesap<select name="account_id">${accounts.map((a) => html`<option value="${a.id}">${a.name} · ${a.channel_label}</option>`)}</select></label>
          <label>Kampanya adı<input name="name" required maxlength="200"></label>
          <label>Pazaryeri<select name="marketplace"><option value="">—</option>${mps.map((m) => html`<option value="${m.code}">${m.name}</option>`)}</select></label>
          <label>Platform kampanya ID (isteğe bağlı)<input name="external_id" maxlength="100"></label>
          <label>Başlangıç<input type="date" name="start_date"></label><label>Bitiş<input type="date" name="end_date"></label>
          <label class="full">Kampanyadaki ürünler (reklam sonrası kâr için)<input type="search" id="prod-filter" placeholder="Ürün ara…"><select multiple name="product_ids" id="prod-sel" size="6">${prods.items.map((p) => html`<option value="${p.id}">${p.sku} · ${p.name}</option>`)}</select><span class="small muted">Ctrl/Cmd ile birden fazla seçebilirsiniz.</span></label>`,
          async (d, f) => {
            const ids = $$('#prod-sel option:checked', f).map((o) => Number(o.value));
            await api('/api/ads/campaigns', { method: 'POST', body: { account_id: Number(d.account_id), name: d.name, marketplace: d.marketplace || null, external_id: d.external_id || null, start_date: d.start_date || null, end_date: d.end_date || null, product_ids: ids } });
            saved('Kampanya eklendi');
          });
        $('#prod-filter', body).addEventListener('input', (e) => { const q = e.target.value.toLocaleLowerCase('tr'); $$('#prod-sel option', body).forEach((o) => { o.hidden = q && !o.textContent.toLocaleLowerCase('tr').includes(q); }); });
      });
      $('#ad-spend')?.addEventListener('click', () => form('Reklam harcaması', html`${campSelect()}<label>Tarih<input type="date" name="spend_date" value="${todayIso()}" required></label><label>Tutar (₺)<input type="number" name="amount" step="0.01" min="0" required></label><label class="full">Not<input name="note" maxlength="500"></label>`,
        async (d) => { await api('/api/ads/spend', { method: 'POST', body: { campaign_id: Number(d.campaign_id), spend_date: d.spend_date, amount: d.amount, note: d.note || null } }); saved('Harcama kaydedildi'); }));
      $('#ad-perf')?.addEventListener('click', () => form('Platform performansı (elle)', html`${campSelect()}<label>Tarih<input type="date" name="perf_date" value="${todayIso()}" required></label>
          <label>Gösterim<input type="number" name="impressions" min="0"></label><label>Tıklama<input type="number" name="clicks" min="0"></label>
          <label>Reklam kaynaklı sipariş<input type="number" name="attributed_orders" min="0"></label><label>Reklam kaynaklı ciro (₺)<input type="number" step="0.01" name="attributed_revenue" min="0"></label>
          <p class="full small muted">Yalnızca reklam platformunun raporladığı değerleri girin. Boş bırakılan alanlar hesaplamaya katılmaz.</p>`,
        async (d) => {
          const n = (v) => (v === '' || v === undefined ? null : Number(v));
          await api('/api/ads/performance', { method: 'POST', body: { campaign_id: Number(d.campaign_id), perf_date: d.perf_date, impressions: n(d.impressions), clicks: n(d.clicks), attributed_orders: n(d.attributed_orders), attributed_revenue: d.attributed_revenue || null } });
          saved('Performans kaydedildi');
        }));
      $$('[data-delspend]').forEach((b) => b.addEventListener('click', async () => {
        if (!confirm('Bu harcama kaydı silinsin mi? (Denetim kaydına yazılır.)')) return;
        try { await api(`/api/ads/spend/${b.dataset.delspend}`, { method: 'DELETE' }); toast('Silindi'); draw(); } catch (e) { fail(e); }
      }));
    };
    await draw();
  },
};

// ---- Raporlar
PAGES.reports = {
  title: 'Raporlar', icon: 'reports',
  async render() {
    let sortKey = 'net_profit', dir = -1;
    const draw = async () => {
      const csvHref = `/api/reports/sku.csv?${periodQs()}`;
      setHeader('Raporlar', 'SKU kârlılığı, iade oranı ve zarar eden siparişler', html`${periodSeg(state.period, draw)}<a class="btn" href="${csvHref}" download>CSV indir</a>`);
      loading();
      const [sku, loss] = await Promise.all([api('/api/reports/sku', { query: periodQuery() }), api('/api/reports/top-loss', { query: periodQuery() })]);
      const items = sku.items;
      const table = () => {
        const sorted = [...items].sort((a, b) => ((a[sortKey] ?? -Infinity) > (b[sortKey] ?? -Infinity) ? 1 : -1) * dir);
        const th = (k, l, r = true) => html`<th class="${r ? 'r' : ''}"><a href="#" data-sort="${k}">${l}${sortKey === k ? (dir < 0 ? ' ↓' : ' ↑') : ''}</a></th>`;
        $('#sku-table').innerHTML = renderVal(items.length ? html`<div class="table-wrap"><table><thead><tr>${th('sku', 'SKU', false)}${th('quantity', 'Adet')}${th('revenue', 'Ciro')}${th('product_cost', 'Maliyet')}${th('commission', 'Komisyon')}${th('shipping', 'Kargo')}${th('advertising', 'Reklam')}${th('refund', 'İade')}${th('return_rate', 'İade oranı')}${th('net_profit', 'KDV öncesi kâr')}${th('tax_estimate', 'Tahmini KDV')}${th('net_profit_after_tax', 'KDV sonrası kâr')}${th('margin_after_vat', 'Net marj')}${th('markup_after_vat', 'Markup')}</tr></thead><tbody>
          ${sorted.map((r) => html`<tr><td><b>${r.sku}</b><span class="ellipsis small muted">${r.product_name || ''}</span>${r.missing_cost ? html`<span class="badge plain tone-warn">Maliyet eksik</span>` : ''}</td>
            <td class="r num">${num(r.quantity)}</td><td class="r num">${money(r.revenue)}</td><td class="r num">${money(r.product_cost)}</td><td class="r num">${money(r.commission)}</td><td class="r num">${money(r.shipping)}</td><td class="r num">${money(r.advertising)}</td><td class="r num">${money(r.refund)}</td><td class="r num">${pct(r.return_rate)}</td>
            <td class="r num ${signClass(r.net_profit)}">${money(r.net_profit)}${estimateBadge(r.is_estimate)}</td><td class="r num">${money(r.tax_estimate)}</td><td class="r num ${signClass(r.net_profit_after_tax)}">${money(r.net_profit_after_tax)}</td><td class="r num">${pct(r.margin_after_vat)}</td><td class="r num">${pct(r.markup_after_vat)}</td></tr>`)}</tbody></table></div>` : empty('Veri yok', 'Seçilen dönemde satılan ürün yok.'));
        $$('[data-sort]').forEach((a) => a.addEventListener('click', (e) => {
          e.preventDefault(); if (sortKey === a.dataset.sort) dir = -dir; else { sortKey = a.dataset.sort; dir = -1; } table();
        }));
      };
      view().innerHTML = renderVal(html`<div class="card"><div class="card-head"><div><h2>SKU kârlılığı</h2><p>İptal edilen siparişler hariç · dönem reklam giderleri SKU'ya bağlıysa dahil</p></div></div><div id="sku-table"></div></div>
        <div class="card mt"><div class="card-head"><h2>Zarar eden siparişler</h2></div>${loss.length ? html`<div class="table-wrap"><table><thead><tr><th>Sipariş</th><th>Tarih</th><th>Durum</th><th class="r">Ciro</th><th class="r">Net kâr</th></tr></thead><tbody>
          ${loss.map((o) => html`<tr class="click" data-order="${o.id}"><td>${o.external_order_id}</td><td>${dateTime(o.order_date)}</td><td>${statusBadge(o.internal_status)}</td><td class="r num">${money(o.gross_revenue)}</td><td class="r num neg">${money(o.net_profit)}</td></tr>`)}</tbody></table></div>` : empty('Zarar eden sipariş yok', 'Seçilen dönemde net kârı negatif sipariş bulunmuyor.')}</div>`);
      table();
      $$('[data-order]').forEach((r) => r.addEventListener('click', () => showOrder(r.dataset.order)));
    };
    await draw();
  },
};

// ---- Entegrasyonlar
PAGES.integrations = {
  title: 'Entegrasyonlar', icon: 'integrations',
  async render() {
    setHeader('Entegrasyonlar', 'Pazaryeri mağaza bağlantıları ve senkronizasyon', can('admin') ? html`<button class="btn btn-primary" id="add-store">+ Mağaza Ekle</button>` : '');
    const d = await api('/api/integrations');
    state.jobLabels = d.job_labels || {};
    const pill = (i) => i.state === 'connected' ? html`<span class="badge tone-good">🟢 Bağlı</span>`
      : i.state === 'error' ? html`<span class="badge tone-bad">🔴 Bağlantı başarısız</span>`
      : i.state === 'not_connected' ? html`<span class="badge">⚪ Bağlı değil</span>` : html`<span class="badge tone-warn">${i.state_label}</span>`;
    const src = { panel: 'Panelden eklendi', server: 'Sunucu ayarından (eski kurulum)', removed: 'Bağlantı kaldırıldı', none: 'Eklenmedi' };
    view().innerHTML = renderVal(html`
      <div class="notice info" style="margin-bottom:16px">Mağaza bağlantıları bu ekrandan eklenir; API bilgileri <b>şifreli</b> saklanır ve bir daha açık gösterilmez. Senkronizasyon her ${d.sync_interval_minutes} dakikada bir otomatik çalışır ve <b>salt okunurdur</b>; pazaryerinde hiçbir değişiklik yapılmaz.</div>
      <div class="grid grid-3">${d.items.map((i) => html`<div class="card integration-card">
        <div class="card-head"><h2>${i.name}</h2>${pill(i)}</div>
        <p class="small muted" style="margin-top:0">${src[i.source] || ''}${i.last_check_message ? html` · ${i.last_check_message}` : ''}</p>
        ${i.implementation_note ? html`<p class="notice warn small">${i.implementation_note}</p>` : ''}
        <ul class="cred-list">${i.credentials.map((c) => html`<li><span>${c.label}</span><span class="badge plain ${c.is_set ? 'tone-good' : ''}">${c.is_set ? 'Tanımlı' : 'Eksik'}</span></li>`)}</ul>
        ${i.optional_settings?.length ? html`<ul class="cred-list">${i.optional_settings.map((o) => html`<li><span>${o.label}</span><span class="badge plain">${o.value}</span></li>`)}</ul>` : ''}
        <div class="counts"><div><b class="num">${num(i.counts?.orders)}</b><span class="muted small">sipariş</span></div><div><b class="num">${num(i.counts?.listings)}</b><span class="muted small">ilan</span></div><div><b class="num">${num(i.counts?.ready_drafts)}</b><span class="muted small">yayına hazır taslak</span></div></div>
        <p class="small"><b>Ürün yayını:</b> <span class="badge plain tone-warn">Yayınlama henüz doğrulanmadı</span> <span class="muted">${i.publish?.reason || ''}</span></p>
        <dl class="kv"><dt>Son test</dt><dd>${dateTime(i.last_check_at)}</dd><dt>Son senkron</dt><dd>${dateTime(i.last_sync_at)}</dd>
          <dt>Mod</dt><dd>${i.write_enabled ? html`<span class="badge tone-warn">Yazma açık</span>` : 'Salt okunur'}</dd></dl>
        <div class="row mt">
          ${can('admin') ? html`<button class="btn btn-sm btn-primary" data-edit-store="${i.code}">${i.source === 'panel' ? 'Bilgileri düzenle' : 'Bağla'}</button>` : ''}
          ${can('operator') ? html`<button class="btn btn-sm" data-check="${i.code}" ${raw(i.state !== 'not_connected' ? '' : 'disabled')}>Bağlantıyı test et</button>
          <button class="btn btn-sm" data-sync="${i.code}" data-kind="orders" ${raw(i.capabilities.includes('orders.read') && i.state !== 'not_connected' ? '' : 'disabled')}>Siparişleri çek</button>
          ${i.capabilities.includes('products.read') ? html`<button class="btn btn-sm" data-sync="${i.code}" data-kind="listings" ${raw(i.state !== 'not_connected' ? '' : 'disabled')}>İlanları çek</button>` : ''}` : ''}
          ${can('admin') && i.source === 'panel' ? html`<button class="btn btn-sm btn-danger-text" data-remove-store="${i.code}" data-name="${i.name}">Bağlantıyı kaldır</button>` : ''}</div>
        <details class="mt"><summary class="small">Son işler</summary>${i.recent_jobs.length ? html`<ul class="timeline">${i.recent_jobs.map((j) => html`<li><b>${jobLabel(j.job_type)}</b> · ${jobBadge(j.status)} <span class="muted small">${dateTime(j.created_at)} · deneme ${j.attempts}</span>${j.message ? html`<br><span class="small ${j.status === 'succeeded' ? '' : 'neg'}">${j.message}</span>` : ''}</li>`)}</ul>` : html`<p class="muted small">Henüz iş yok.</p>`}</details>
      </div>`)}</div>`);
    $('#add-store')?.addEventListener('click', () => storeWizard(d.items));
    $$('[data-edit-store]').forEach((b) => b.addEventListener('click', () => storeWizard(d.items, b.dataset.editStore)));
    $$('[data-remove-store]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm(`${b.dataset.name} bağlantısı kaldırılsın mı?\n\nKayıtlı API bilgileri silinir; siparişler, ilanlar ve finans kayıtları korunur. Pazaryerinde hiçbir şey değişmez.`)) return;
      try { await api(`/api/integrations/${b.dataset.removeStore}/connection`, { method: 'DELETE' }); toast('Bağlantı kaldırıldı'); refresh(); } catch (e) { fail(e); }
    }));
    $$('[data-check]').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true;
      try { const r = await api(`/api/integrations/${b.dataset.check}/check`, { method: 'POST' }); toast(r.ok ? '🟢 Bağlantı doğrulandı' : `🔴 ${r.message}`, !r.ok); refresh(); } catch (e) { fail(e); } finally { b.disabled = false; }
    }));
    $$('[data-sync]').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true;
      try { const r = await api(`/api/integrations/${b.dataset.sync}/sync`, { method: 'POST', body: { kind: b.dataset.kind } }); toast(r.message); refresh(); } catch (e) { fail(e); b.disabled = false; }
    }));
  },
};

// Mağaza Ekle sihirbazı: 1 Pazaryeri · 2 Bağlantı · 3 Doğrulama/Tamamlandı
async function storeWizard(items, code) {
  const steps = (n) => html`<ol class="steps" style="margin-bottom:14px">${['Pazaryeri', 'Bağlantı', 'Tamamlandı'].map((l, i) => html`<li class="${i + 1 === n ? 'on' : i + 1 < n ? 'done' : ''}">${i + 1}. ${l}</li>`)}</ol>`;
  if (!code) {
    const body = openModal(html`<h2>Mağaza Ekle</h2>${steps(1)}<p class="muted small">Bağlamak istediğiniz pazaryerini seçin.</p>
      <div class="stack" style="gap:8px">${items.map((i) => html`<button class="btn mp-choice" data-pick-mp="${i.code}"><b>${i.name}</b><span class="muted small">${i.state === 'connected' ? 'Bağlı — bilgileri güncelle' : 'Bağla'}</span></button>`)}</div>`);
    $$('[data-pick-mp]', body).forEach((b) => b.addEventListener('click', () => storeWizard(items, b.dataset.pickMp)));
    return;
  }
  const name = items.find((i) => i.code === code)?.name || code;
  const v = await api(`/api/integrations/${code}/connection`);
  const field = (f) => html`<label>${f.label}${f.required ? '' : html` <span class="muted small">(isteğe bağlı)</span>`}
    ${f.secret ? html`<input name="${f.key}" type="password" autocomplete="new-password" maxlength="4000" placeholder="${f.is_set ? '•••••••• (kayıtlı — değiştirmek için yeni değer girin)' : ''}" ${raw(f.required && !f.is_set ? 'required' : '')}>`
      : html`<input name="${f.key}" maxlength="500" value="${f.value || ''}" placeholder="${f.default || ''}" ${raw(f.required ? 'required' : '')}>`}
    ${f.help ? html`<span class="small muted">${f.help}</span>` : ''}</label>`;
  const basic = v.fields.filter((f) => !f.advanced), adv = v.fields.filter((f) => f.advanced);
  const body = openModal(html`<h2>${name} bağlantısı</h2>${steps(2)}
    ${v.source === 'server' ? html`<div class="notice info small" style="margin-bottom:10px">Bu mağaza şu an sunucu ayarından çalışıyor. Buradan kaydederseniz panel bilgileri geçerli olur.</div>` : ''}
    <form class="stack" id="storeform" autocomplete="off">${basic.map(field)}
      ${adv.length ? html`<details><summary class="small">Gelişmiş ayarlar</summary><div class="stack mt">${adv.map(field)}</div></details>` : ''}
      <p class="small muted">Bilgiler şifreli saklanır; kaydettikten sonra açık gösterilmez. Test yalnızca okuma isteği yapar, mağazanızda hiçbir şey değiştirmez.</p>
      <div id="store-test" class="small"></div>
      <p class="form-error"></p>
      <div class="row"><button class="btn" type="button" data-close>Vazgeç</button><span class="spacer"></span>
        <button class="btn" type="button" id="store-test-btn">Bağlantıyı Test Et</button><button class="btn btn-primary" type="submit">Kaydet</button></div></form>`);
  const form = $('#storeform', body);
  const values = () => Object.fromEntries(v.fields.map((f) => [f.key, form.elements.namedItem(f.key).value.trim() || null]));
  const show = (r) => { $('#store-test', body).innerHTML = renderVal(html`<div class="notice ${r.ok ? 'info' : 'bad'}">${r.ok ? '🟢 Bağlı' : '🔴 Bağlantı başarısız'} — ${r.message}</div>`); };
  $('#store-test-btn', body).addEventListener('click', async (e) => {
    e.target.disabled = true; $('#store-test', body).textContent = 'Test ediliyor…';
    try { show(await api(`/api/integrations/${code}/connection/test`, { method: 'POST', body: { values: values() } })); }
    catch (ex) { $('#store-test', body).innerHTML = renderVal(html`<div class="notice bad">🔴 ${ex.message}</div>`); }
    finally { e.target.disabled = false; }
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(form, async () => {
      const r = await api(`/api/integrations/${code}/connection`, { method: 'PUT', body: { values: values() } });
      const done = openModal(html`<h2>${name} bağlantısı</h2>${steps(3)}
        <div class="notice ${r.ok ? 'info' : 'warn'}">${r.ok ? html`🟢 <b>Bağlı.</b>` : html`🔴 <b>Kaydedildi ancak bağlantı doğrulanamadı.</b>`} ${r.message}</div>
        <p class="small muted mt">Senkronizasyon salt okunurdur. Ürün yayını bu pazaryeri için henüz doğrulanmadığından kapalıdır.</p>
        <div class="row mt"><span class="spacer"></span><button class="btn btn-primary" data-close>Tamam</button></div>`);
      done.querySelector('[data-close]').addEventListener('click', refresh);
      refresh();
    });
  });
}
const JOB_LABELS = { queued: ['Kuyrukta', 'tone-info'], running: ['Çalışıyor', 'tone-warn'], succeeded: ['Başarılı', 'tone-good'], failed: ['Başarısız', 'tone-bad'], dead: ['Deneme bitti', 'tone-bad'] };
const CAPABILITY_LABELS = { 'orders.read': 'sipariş okuma', 'products.read': 'ilan okuma' };
const DEFAULT_JOB_LABELS = { 'orders.sync': 'Sipariş senkronizasyonu', 'orders.deep_sync': 'Derin sipariş senkronizasyonu (30 gün)', 'listings.sync': 'Ürün/ilan senkronizasyonu', 'integration.check': 'Bağlantı testi' };
const jobLabel = (t) => (state.jobLabels && state.jobLabels[t]) || DEFAULT_JOB_LABELS[t] || t || '—';
const jobBadge = (s) => { const [l, t] = JOB_LABELS[s] || [s || '—', '']; return html`<span class="badge ${t}">${l}</span>`; };

// ---- Sistem / Hatalar
PAGES.system = {
  title: 'Sistem / Hatalar', icon: 'system', nav: 'Sistem / Hatalar',
  async render() {
    setHeader('Sistem / Hatalar', 'Sağlık durumu, iş kuyruğu, hatalar ve denetim kaydı');
    const [h, jobs, events] = await Promise.all([api('/api/system/health'), api('/api/system/jobs', { query: { page_size: 20 } }), api('/api/system/events', { query: { page_size: 50 } })]);
    const audit = can('admin') ? await api('/api/system/audit', { query: { page_size: 30 } }) : null;
    view().innerHTML = renderVal(html`
      <div class="grid grid-4">
        <div class="card kpi"><div class="label">Genel durum</div><div class="value">${h.status === 'healthy' ? html`<span class="pos">Sağlıklı</span>` : html`<span class="neg">Dikkat</span>`}</div><div class="sub">Şema: <code>${h.database.migration || 'bilinmiyor'}</code></div></div>
        <div class="card kpi"><div class="label">Worker</div><div class="value ${h.worker_alive ? 'pos' : 'neg'}">${h.worker_alive ? 'Çalışıyor' : 'Yok'}</div><div class="sub">${h.last_heartbeat_at ? 'Son heartbeat ' + dateTime(h.last_heartbeat_at) : 'Hiç heartbeat alınmadı'}</div></div>
        <div class="card kpi"><div class="label">Kuyruk</div><div class="value">${num(h.queue.queued)} <span class="small muted">bekliyor</span></div><div class="sub">${num(h.queue.running)} çalışıyor · ${num(h.queue.succeeded_24h)} başarılı (24s)</div></div>
        <div class="card kpi"><div class="label">Açık hata</div><div class="value ${h.open_events.errors ? 'neg' : ''}">${num(h.open_events.errors)}</div><div class="sub">${num(h.open_events.warnings)} uyarı · ${num(h.queue.failed_24h)} başarısız iş (24s)</div></div>
      </div>
      <div class="card mt"><div class="card-head"><div><h2>Pazaryeri bağlantıları</h2><p>Salt okunur · pazaryeri yazma: ${h.config.connector_write_enabled ? 'AÇIK' : 'kapalı'} · senkron aralığı ${h.config.sync_interval_minutes} dk</p></div><a class="small" href="#/integrations">Entegrasyonlar →</a></div>
        <div class="table-wrap"><table><thead><tr><th>Pazaryeri</th><th>Durum</th><th>Son senkron</th><th>Son başarılı iş</th><th>Son hata</th><th class="r">Başarısız (24s)</th></tr></thead><tbody>
        ${h.connectors.map((c) => html`<tr><td><b>${c.name}</b></td><td><span class="badge ${{ connected: 'tone-good', error: 'tone-bad', not_connected: '' }[c.state] ?? 'tone-warn'}">${c.state_label}</span></td>
          <td>${dateTime(c.last_sync_at)}</td><td>${dateTime(c.last_success_at)}</td><td>${dateTime(c.last_failure_at)}</td><td class="r num ${c.failed_24h ? 'neg' : ''}">${num(c.failed_24h)}</td></tr>`)}</tbody></table></div></div>
      <div class="card mt"><div class="card-head"><h2>Worker'lar</h2></div>
        ${h.workers.length ? html`<div class="table-wrap"><table><thead><tr><th>Worker</th><th>Başlangıç</th><th>Son heartbeat</th><th>Durum</th><th>Çalışan iş</th></tr></thead><tbody>
        ${h.workers.map((w) => html`<tr><td><code>${w.worker_id}</code></td><td>${dateTime(w.started_at)}</td><td>${dateTime(w.last_seen_at)}</td><td>${w.alive ? html`<span class="badge tone-good">Canlı</span>` : html`<span class="badge">Sinyal yok</span>`}</td><td>${w.current_job_id ? '#' + w.current_job_id : '—'}</td></tr>`)}</tbody></table></div>`
          : empty('Worker yok', 'Worker servisi çalıştığında burada heartbeat görünür. Senkronizasyon worker olmadan çalışmaz.')}</div>
      <div class="card mt"><div class="card-head"><h2>Hatalar ve uyarılar</h2></div>
        ${events.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Son</th><th>Seviye</th><th>Kaynak</th><th>Mesaj</th><th class="r">Tekrar</th><th></th></tr></thead><tbody>
        ${events.items.map((e) => html`<tr><td>${dateTime(e.last_occurred_at)}</td><td><span class="badge ${e.level === 'warning' ? 'tone-warn' : e.level === 'info' ? 'tone-info' : 'tone-bad'}">${{ info: 'Bilgi', warning: 'Uyarı', error: 'Hata', critical: 'Kritik' }[e.level]}</span></td>
          <td><code>${e.source}</code></td><td>${e.message}</td><td class="r num">${num(e.occurrences)}</td><td class="r">${can('operator') ? html`<button class="btn btn-sm" data-resolve="${e.id}">Çözüldü</button>` : ''}</td></tr>`)}</tbody></table></div>` : empty('Açık hata yok', 'Sistem olayları burada listelenir.')}</div>
      <div class="card mt"><div class="card-head"><h2>Senkronizasyon işleri</h2></div>
        ${jobs.items.length ? html`<div class="table-wrap"><table><thead><tr><th>#</th><th>Tür</th><th>Pazaryeri</th><th>Durum</th><th class="r">Deneme</th><th>Oluşturma</th><th>Bitiş</th><th>Mesaj</th><th></th></tr></thead><tbody>
        ${jobs.items.map((j) => html`<tr><td>${j.id}</td><td>${jobLabel(j.job_type)}</td><td>${j.marketplace || '—'}</td><td>${jobBadge(j.status)}</td><td class="r">${j.attempts ?? 0}/${j.max_attempts ?? '—'}</td>
          <td>${dateTime(j.created_at || j.started_at)}</td><td>${dateTime(j.finished_at)}</td><td><span class="ellipsis small" title="${j.last_error || j.message || ''}">${j.last_error || j.message || ''}</span></td>
          <td class="r">${can('operator') && ['failed', 'dead'].includes(j.status) ? html`<button class="btn btn-sm" data-retry="${j.id}">Tekrar dene</button>` : ''}</td></tr>`)}</tbody></table></div>` : empty('İş yok', 'Pazaryeri bağlandığında senkronizasyon işleri burada görünür.')}</div>
      ${audit ? html`<div class="card mt"><div class="card-head"><h2>Denetim kaydı</h2><p>Son 30 işlem</p></div><div class="table-wrap"><table><thead><tr><th>Zaman</th><th>Kullanıcı</th><th>İşlem</th><th>Kayıt</th><th>IP</th></tr></thead><tbody>
        ${audit.items.map((a) => html`<tr><td>${dateTime(a.occurred_at)}</td><td>${a.actor}</td><td><code>${a.action}</code></td><td>${a.entity_type ? `${a.entity_type} #${a.entity_id}` : '—'}</td><td class="muted small">${a.ip || ''}</td></tr>`)}</tbody></table></div></div>` : ''}`);
    $$('[data-resolve]').forEach((b) => b.addEventListener('click', async () => { try { await api(`/api/system/events/${b.dataset.resolve}/resolve`, { method: 'POST' }); refresh(); } catch (e) { fail(e); } }));
    $$('[data-retry]').forEach((b) => b.addEventListener('click', async () => { try { await api(`/api/system/jobs/${b.dataset.retry}/retry`, { method: 'POST' }); toast('İş yeniden kuyruğa alındı'); refresh(); } catch (e) { fail(e); } }));
  },
};

// ---- Ayarlar
PAGES.settings = {
  title: 'Ayarlar', icon: 'settings',
  async render() {
    setHeader('Ayarlar', 'Finans varsayılanları ve hesap');
    const s = await api('/api/settings');
    const input = (it) => {
      const dis = can('admin') ? '' : 'disabled';
      if (it.type === 'bool') return html`<label class="check"><input type="checkbox" name="${it.key}" ${raw(it.value ? 'checked' : '')} ${raw(dis)}>${it.label}</label>`;
      if (it.type === 'rate') return html`<label>${it.label} (%)<input name="${it.key}" type="number" step="0.1" min="0" max="100" value="${it.value === null ? '' : +(Number(it.value) * 100).toFixed(2)}" ${raw(dis)}></label>`;
      if (it.type === 'time') return html`<label>${it.label}<input name="${it.key}" type="time" step="60" value="${it.value || ''}" ${raw(dis)}></label>`;
      if (it.type === 'code') return html`<label>${it.label}<input name="${it.key}" maxlength="50" pattern="[a-z0-9_\\-]*" value="${it.value || ''}" ${raw(dis)}></label>`;
      if (it.type === 'percent') return html`<label>${it.label}<input name="${it.key}" type="number" step="1" min="0" max="1000" value="${it.value ?? ''}" ${raw(dis)}></label>`;
      if (it.type === 'choice') return html`<label>${it.label}<select name="${it.key}" ${raw(dis)}>${Object.entries(it.choices || {}).map(([v, l]) => html`<option value="${v}" ${raw((it.value || 'unset') === v ? 'selected' : '')}>${l}</option>`)}</select></label>`;
      if (it.type === 'fx') return html`<label>${it.label}<input name="${it.key}" value="${Object.entries(it.value || {}).map(([k, v]) => `${k}=${v}`).join(', ')}" placeholder="ör. USD=34.10, EUR=37.20 (boş = döviz kullanılmaz)" ${raw(dis)}></label>`;
      return html`<label>${it.label}<input name="${it.key}" type="number" step="${it.type === 'int' ? 1 : 0.01}" min="0" value="${it.value ?? ''}" ${raw(dis)}></label>`;
    };
    view().innerHTML = renderVal(html`
      <div class="grid grid-2">
        <form class="card" id="set-form"><div class="card-head"><div><h2>İşletme ayarları</h2><p>Tüm ayarlar buradan yönetilir; sunucu dosyası düzenlemek gerekmez. Finans varsayılanlarıyla hesaplanan tutarlar raporlarda “TAHMİNİ” olarak işaretlenir.</p></div></div>
          ${[...new Set(s.items.map((it) => it.group))].map((g) => html`<fieldset class="fieldset mt"><legend>${g}</legend><div class="form-grid">${s.items.filter((it) => it.group === g).map((it) => html`<div class="${it.type === 'bool' || it.type === 'time' || it.type === 'choice' || it.type === 'fx' ? 'full' : ''}">${input(it)}</div>`)}</div>
            ${g === 'Kargo planı' ? html`<p class="small muted">Varsayılan: 11:00 öncesi bugün, 12:00 ve sonrası yarın; aradaki saatler “Kargo günü belirsiz” gösterilir. İki saati aynı yaparsanız belirsiz aralık kalmaz. Hafta sonu/tatil takvimi tanımlı değildir; tarih tahminidir.</p>` : ''}</fieldset>`)}
          ${can('admin') ? html`<p class="form-error"></p><button class="btn btn-primary mt" type="submit">Kaydet</button>` : html`<p class="muted small mt">Ayarları yalnızca yöneticiler değiştirebilir.</p>`}</form>
        <div class="stack">
          <form class="card" id="pw-form"><div class="card-head"><h2>Parola değiştir</h2></div><div class="stack">
            <label>Mevcut parola<input type="password" name="current_password" autocomplete="current-password" required></label>
            <label>Yeni parola (en az 12 karakter)<input type="password" name="new_password" autocomplete="new-password" minlength="12" required></label>
            <p class="form-error"></p><button class="btn btn-primary" type="submit">Parolayı değiştir</button><p class="small muted">Diğer cihazlardaki oturumlar kapatılır.</p></div></form>
          <div class="card"><div class="card-head"><h2>Hesap</h2></div><dl class="kv">
            <dt>Kullanıcı</dt><dd>${state.user.username}</dd><dt>Rol</dt><dd>${ROLE_LABELS[state.user.role]}</dd>
            <dt>Pazaryeri yazma</dt><dd>Kapalı — TrendHub pazaryerlerine yalnızca okuma yapar</dd></dl>
            ${can('admin') ? html`<a class="btn btn-sm mt" href="#/users">Kullanıcıları yönet →</a>` : ''}</div>
        </div>
      </div>`);
    $('#set-form').addEventListener('submit', (e) => {
      e.preventDefault();
      submitting(e.target, async () => {
        const values = {};
        s.items.forEach((it) => {
          const el = e.target.elements[it.key];
          if (it.type === 'bool') values[it.key] = el.checked;
          else if (it.type === 'time' || it.type === 'code' || it.type === 'choice') values[it.key] = el.value.trim();
          else if (it.type === 'fx') values[it.key] = Object.fromEntries(el.value.split(',').map((x) => x.trim()).filter(Boolean).map((x) => x.split('=').map((y) => y.trim())));
          else if (el.value !== '') values[it.key] = it.type === 'rate' ? Number(el.value) / 100 : it.type === 'int' ? parseInt(el.value, 10) : Number(el.value);
        });
        const r = await api('/api/settings', { method: 'PUT', body: { values } });
        toast(r.changed.length ? 'Ayarlar kaydedildi. Yeni hesaplamalarda kullanılacak.' : 'Değişiklik yok');
      });
    });
    $('#pw-form').addEventListener('submit', (e) => {
      e.preventDefault();
      submitting(e.target, async () => { await api('/api/auth/change-password', { method: 'POST', body: formData(e.target) }); e.target.reset(); toast('Parola değiştirildi'); });
    });
  },
};

// ---- Trendçantanız web mağazası (satış kanalı: "Trendçantanız Web")
const SF_STATUS_TONE = { awaiting_payment: 'tone-warn', pending_payment: 'tone-warn', paid: 'tone-good', cash_on_delivery: 'tone-info', cancelled: '', expired: '', payment_failed: 'tone-bad' };
const SF_PAY = { bank_transfer: 'Havale / EFT', cash_on_delivery: 'Kapıda ödeme', card: 'Kart' };
PAGES.storefront = {
  title: 'Web Sitesi', icon: 'storefront',
  async render(params) {
    const tab = params.get('tab') || 'orders';
    const ov = await api('/api/storefront/overview');
    setHeader('Web Sitesi', 'Trendçantanız web mağazası: yayındaki ürünler, ödeme bekleyen siparişler ve mağaza ayarları',
      ov.base_url ? html`<a class="btn" href="${ov.base_url}" target="_blank" rel="noopener">Mağazayı aç ↗</a>` : '');
    const tabs = [['orders', `Ödeme bekleyenler${ov.pending_payment ? ` (${ov.pending_payment})` : ''}`], ['products', 'Ürün yayını'],
      ...(can('operator') ? [['supplier', 'Tedarikçiye aktarım']] : []), ['integrations', 'Entegrasyonlar'], ...(can('admin') ? [['settings', 'Mağaza ayarları']] : [])];
    view().innerHTML = renderVal(html`
      ${ov.checkout_ready ? '' : html`<div class="notice bad" style="margin-bottom:16px"><b>Web mağazası şu anda sipariş kabul etmiyor.</b><ul class="small mt">${ov.blockers.map((b) => html`<li>${b}</li>`)}</ul></div>`}
      <div class="grid grid-4">
        ${kpi('Yayındaki model', num(ov.published_models), `${num(ov.published_products)} ürün/varyant · ${num(ov.categories)} kategori`)}
        ${kpi('Stokta model', num(ov.in_stock_models), 'Kanallar arası kullanılabilir stoğa göre')}
        ${kpi('Web siparişi (30 gün)', num(ov.orders_30d), money(ov.revenue_30d))}
        ${kpi('Ödeme bekleyen', num(ov.pending_payment), 'Havale/EFT veya kart', ov.pending_payment ? 'neg' : '')}
      </div>
      <div class="card mt"><div class="card-head"><div><h2>Ana sayfa hero ürünü</h2>
        <p>${ov.hero ? html`<b>${ov.hero.title}</b> — ${ov.hero.reason}` : 'Yayında ürün yok.'}</p></div>
        ${ov.hero && ov.base_url ? html`<a class="btn btn-sm" href="${ov.base_url}${ov.hero.url}" target="_blank" rel="noopener">Ürünü gör ↗</a>` : ''}</div>
        <p class="small muted">Ödeme yöntemleri: ${ov.payment_methods.length ? ov.payment_methods.map((m) => m.label).join(', ') : 'yok'}.
          Kartla ödeme ${ov.card_provider_configured ? 'etkin (PayTR / iyzico, sunucu ayarı)' : 'kapalı: PayTR veya iyzico anahtarları sunucu .env dosyasına girilince açılır (Entegrasyonlar sekmesi)'}.</p></div>
      <div class="seg mt" role="tablist" style="margin-bottom:16px">${tabs.map(([k, l]) => html`<button role="tab" class="${k === tab ? 'on' : ''}" aria-selected="${k === tab}" data-sftab="${k}">${l}</button>`)}</div>
      <div id="sf-body"></div>`);
    $$('[data-sftab]').forEach((b) => b.addEventListener('click', () => { location.hash = `#/storefront?tab=${b.dataset.sftab}`; }));
    const box = $('#sf-body');
    if (tab === 'products') return sfProducts(box);
    if (tab === 'supplier' && can('operator')) return sfSupplier(box);
    if (tab === 'integrations') return sfIntegrations(box);
    if (tab === 'settings' && can('admin')) return sfSettings(box);
    return sfOrders(box);
  },
};

async function sfOrders(box, page = 1, status = 'pending') {
  if (!can('operator')) { box.innerHTML = renderVal(empty('Yetki gerekli', 'Web siparişlerinin müşteri bilgileri yalnızca operatör ve yöneticilere gösterilir.')); return; }
  const d = await api('/api/storefront/orders', { query: { status: status === 'all' ? '' : status, page, page_size: 25 } });
  box.innerHTML = renderVal(html`<div class="card">
    <div class="card-head"><div><h2>Web siparişleri</h2><p>Havale/EFT siparişi, ödeme hesaba geçtiğinde “Ödeme alındı” ile TrendHub siparişine dönüşür ve Siparişler ekranında “Trendçantanız Web” kanalıyla görünür. O zamana kadar stok ayrılır; süre dolarsa sipariş otomatik kapanır.</p></div>
      <select id="sf-status" aria-label="Durum">${[['pending', 'Ödeme bekleyenler'], ['all', 'Tümü'], ['paid', 'Ödeme alındı'], ['cash_on_delivery', 'Kapıda ödeme'], ['expired', 'Süresi dolan'], ['cancelled', 'İptal']].map(([v, l]) => html`<option value="${v}" ${raw(v === status ? 'selected' : '')}>${l}</option>`)}</select></div>
    ${d.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Sipariş</th><th>Tarih</th><th>Müşteri</th><th>Ödeme</th><th class="r">Adet</th><th class="r">Tutar</th><th>Durum</th><th></th></tr></thead><tbody>
      ${d.items.map((o) => html`<tr><td><b>${o.public_code}</b>${o.order_id ? html`<br><a href="#" class="small" data-order="${o.order_id}">TrendHub siparişi →</a>` : ''}</td><td>${dateTime(o.created_at)}</td>
        <td>${o.full_name}<span class="muted small">${o.city}</span></td><td>${SF_PAY[o.payment_method] || o.payment_method}</td><td class="r num">${num(o.item_count)}</td><td class="r num">${money(o.total)}</td>
        <td><span class="badge ${SF_STATUS_TONE[o.status] || ''}">${o.status_label}</span>${o.reserved_until ? html`<span class="muted small">Stok ayrıldı: ${dateTime(o.reserved_until)}'e kadar</span>` : ''}</td>
        <td class="r">${['awaiting_payment', 'pending_payment'].includes(o.status) ? html`<span class="row nowrap"><button class="btn btn-sm btn-primary" data-sfpay="${o.id}" data-code="${o.public_code}" data-total="${o.total}">Ödeme alındı</button><button class="btn btn-sm" data-sfcancel="${o.id}" data-code="${o.public_code}">İptal</button></span>` : ''}</td></tr>`)}
      </tbody></table></div>${pager(d, (p) => sfOrders(box, p, status))}` : empty('Kayıt yok', status === 'pending' ? 'Ödemesi beklenen web siparişi yok.' : 'Bu durumda web siparişi yok.')}</div>`);
  $('#sf-status').addEventListener('change', (e) => sfOrders(box, 1, e.target.value));
  $$('[data-order]', box).forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); showOrder(Number(a.dataset.order)); }));
  $$('[data-sfpay]', box).forEach((b) => b.addEventListener('click', () => {
    const body = openModal(html`<h2>Ödeme alındı · ${b.dataset.code}</h2><form class="stack" id="sfpay-form">
      <p>${money(b.dataset.total)} tutarındaki ödemenin hesaba geçtiğini doğruladınız mı? Onaylayınca sipariş TrendHub'da “Yeni” statüsüyle oluşur.</p>
      <label>Banka açıklaması / referans (isteğe bağlı)<input name="reference" maxlength="200"></label>
      <p class="form-error"></p><div class="row"><span class="spacer"></span><button class="btn" type="button" data-close>Vazgeç</button><button class="btn btn-primary" type="submit">Ödemeyi onayla</button></div></form>`);
    $('#sfpay-form', body).addEventListener('submit', (e) => {
      e.preventDefault();
      submitting(e.target, async () => {
        await api(`/api/storefront/orders/${b.dataset.sfpay}/confirm-payment`, { method: 'POST', body: { reference: formData(e.target).reference || null } });
        closeLayer('modal'); toast('Ödeme onaylandı, sipariş oluşturuldu'); refresh();
      });
    });
  }));
  $$('[data-sfcancel]', box).forEach((b) => b.addEventListener('click', async () => {
    if (!confirm(`${b.dataset.code} iptal edilsin mi? Ayrılan stok serbest bırakılır.`)) return;
    try { await api(`/api/storefront/orders/${b.dataset.sfcancel}/cancel`, { method: 'POST' }); toast('Web siparişi iptal edildi'); refresh(); } catch (e) { fail(e); }
  }));
}

const SFP_STATUS = [['', 'Tümü'], ['visible', 'Yayında'], ['hidden', 'Yayında değil'], ['eligible_unpublished', 'Yayına hazır (yayında değil)'], ['not_eligible', 'Yayına uygun değil (fiyat/görsel yok)']];
async function sfProducts(box, page = 1, f = { q: '', status: '' }) {
  const d = await api('/api/storefront/products', { query: { ...f, page, page_size: 25 } });
  const op = can('operator');
  const c = d.counts || {};
  box.innerHTML = renderVal(html`<div class="card">
    <div class="card-head"><div><h2>Ürün yayını</h2><p>Ürünler TrendHub kataloğundan gelir (tedarikçi beslemesi / pazaryeri ilanı): ad, fiyat, stok, görseller ve renk katalogla eşleşir. Görseli veya fiyatı olmayan ürün yayınlanmaz.
      ${d.auto_publish ? 'Aktif, fiyatı ve görseli olan ürünler otomatik yayınlanır.' : 'Otomatik yayın kapalı: yalnızca yayınlanan ürünler görünür.'}</p>
      <p class="small muted">Yayında ${num(c.visible)} · yayına hazır ${num(c.eligible_unpublished)} · uygun değil ${num(c.not_eligible)} · toplam ${num(c.total)}</p></div></div>
    <form class="filters" id="sfp-filter"><input name="q" value="${f.q}" placeholder="SKU, barkod, ad veya kategori" aria-label="Ara">
      <select name="status" aria-label="Durum">${SFP_STATUS.map(([v, l]) => html`<option value="${v}" ${raw(f.status === v ? 'selected' : '')}>${l}</option>`)}</select>
      <button class="btn btn-sm" type="submit">Filtrele</button></form>
    ${op && d.items.length ? html`<div class="row mt" id="sfp-bulk"><span class="muted small" id="sfp-count">Seçili: 0</span>
      <button class="btn btn-sm btn-primary" data-bulk="publish" disabled>Seçilenleri yayınla</button><button class="btn btn-sm" data-bulk="unpublish" disabled>Seçilenleri gizle</button>
      <button class="btn btn-sm" data-bulk="feature" disabled>Öne çıkar</button><button class="btn btn-sm" data-bulk="unfeature" disabled>Öne çıkarmayı kaldır</button>
      <span class="spacer"></span><button class="btn btn-sm" data-bulkall="publish" title="Filtreye uyan tüm ürünler (en fazla 5000)">Filtredeki tümünü yayınla (${num(d.total)})</button></div>` : ''}
    ${d.items.length ? html`<div class="table-wrap mt"><table><thead><tr>${op ? html`<th><input type="checkbox" id="sfp-all" aria-label="Sayfadakilerin tümünü seç"></th>` : ''}<th>Ürün</th><th>Renk / varyant</th><th class="r">Fiyat</th><th class="r" title="Katalog stoğu">Stok</th><th class="r" title="Son stok güncellemesinden sonraki tüm kanal siparişleri ve ödeme bekleyen web siparişleri düşülmüş">Satılabilir</th><th>Web durumu</th><th></th></tr></thead><tbody>
      ${d.items.map((x) => html`<tr>${op ? html`<td><input type="checkbox" data-sel="${x.id}" aria-label="Seç"></td>` : ''}<td><b>${x.title || x.name}</b><span class="muted small ellipsis">${x.sku || '—'} · ${x.category || 'kategori yok'} · ${num(x.image_count)} görsel</span></td>
        <td>${x.effective_color || '—'}${x.effective_size ? ` · ${x.effective_size}` : ''}<span class="muted small">${x.variant_count > 1 ? `${x.variant_count} varyantlı model` : 'tek varyant'} · ${x.group_key}</span></td>
        <td class="r num">${money(x.sale_price)}${x.compare_at_price ? html`<br><s class="muted small">${money(x.compare_at_price)}</s>` : ''}</td>
        <td class="r num">${num(x.stock)}</td><td class="r num ${Number(x.available) <= 0 ? 'neg' : ''}">${num(x.available)}</td>
        <td>${x.visible ? html`<span class="badge tone-good">Yayında</span>${x.featured ? html` <span class="badge tone-info">Öne çıkan</span>` : ''}` : html`<span class="badge">Yayında değil</span><span class="muted small">${x.hidden_reasons.join(' · ')}</span>`}</td>
        <td class="r">${op ? html`<button class="btn btn-sm" data-sfedit="${x.id}">Düzenle</button>` : ''}</td></tr>`)}
      </tbody></table></div>${pager(d, (p) => sfProducts(box, p, f))}` : empty('Ürün yok', 'Kataloğa ürün eklendiğinde (Tedarikçiler → Ürün havuzu → Kataloğa al) burada görünür.')}</div>`);
  $('#sfp-filter').addEventListener('submit', (e) => { e.preventDefault(); sfProducts(box, 1, formData(e.target)); });
  const selected = () => $$('[data-sel]', box).filter((i) => i.checked).map((i) => Number(i.dataset.sel));
  const sync = () => { const n = selected().length; const cnt = $('#sfp-count'); if (cnt) cnt.textContent = `Seçili: ${n}`; $$('[data-bulk]', box).forEach((b) => { b.disabled = !n; }); };
  $$('[data-sel]', box).forEach((i) => i.addEventListener('change', sync));
  const all = $('#sfp-all');
  if (all) all.addEventListener('change', () => { $$('[data-sel]', box).forEach((i) => { i.checked = all.checked; }); sync(); });
  const bulk = async (body, label) => {
    try {
      const r = await api('/api/storefront/products/bulk', { method: 'POST', body });
      const skipped = r.skipped_not_eligible.length;
      toast(`${label}: ${num(r.changed)} ürün${skipped ? ` · ${skipped} ürün fiyat/görsel eksik olduğu için atlandı` : ''}`);
      sfProducts(box, page, f);
    } catch (e) { fail(e); }
  };
  const labels = { publish: 'Yayınlandı', unpublish: 'Gizlendi', feature: 'Öne çıkarıldı', unfeature: 'Öne çıkarma kaldırıldı' };
  $$('[data-bulk]', box).forEach((b) => b.addEventListener('click', () => bulk({ action: b.dataset.bulk, product_ids: selected() }, labels[b.dataset.bulk])));
  $$('[data-bulkall]', box).forEach((b) => b.addEventListener('click', () => {
    if (!confirm(`Filtreye uyan ${d.total} ürünün yayına uygun olanları yayınlansın mı?`)) return;
    bulk({ action: 'publish', q: f.q || null, status: f.status || null }, labels.publish);
  }));
  $$('[data-sfedit]', box).forEach((b) => b.addEventListener('click', () => {
    const x = d.items.find((i) => String(i.id) === b.dataset.sfedit);
    const body = openModal(html`<h2>Web ayarı · ${x.name}</h2><form class="stack" id="sfp-form">
      <label class="check"><input type="checkbox" name="published" value="1" ${raw(x.published === false ? '' : 'checked')}>Web sitesinde yayınla</label>
      <label class="check"><input type="checkbox" name="featured" value="1" ${raw(x.featured ? 'checked' : '')}>Öne çıkan (listelerde önce gösterilir)</label>
      <label>Web başlığı (boş = katalog adı)<input name="title" maxlength="300" value="${x.title || ''}" placeholder="${x.name}"></label>
      <label>Web açıklaması (boş = katalog açıklaması)<textarea name="description" rows="4" maxlength="20000">${x.web_description || ''}</textarea></label>
      <div class="form-grid">
        <label>Renk (boş = tedarikçi: ${x.supplier_color || 'yok'})<input name="color" maxlength="60" value="${x.color || ''}"></label>
        <label>Beden / ölçü (boş = tedarikçi: ${x.supplier_size || 'yok'})<input name="size" maxlength="60" value="${x.size || ''}"></label>
        <label class="full">Varyant grubu kodu (aynı koda sahip ürünler tek ürün sayfasında renk seçeneği olur; boş = ${x.model_code || x.parent_code || 'tekil'})<input name="group_code" maxlength="100" value="${x.group_code || ''}"></label>
        <label>Üstü çizili fiyat (₺, boş = yok; satış fiyatından yüksek olmalı)<input name="compare_at_price" type="number" step="0.01" min="0" value="${x.compare_at_price ?? ''}"></label>
        <label>Sıra (küçük önce)<input name="sort_order" type="number" step="1" value="${x.sort_order}"></label>
        <label class="full">SEO başlığı (en fazla 70 karakter; boş = ürün adı)<input name="seo_title" maxlength="70" value="${x.seo_title || ''}"></label>
        <label class="full">SEO açıklaması (en fazla 170 karakter; boş = açıklamadan otomatik)<textarea name="seo_description" rows="2" maxlength="170">${x.seo_description || ''}</textarea></label>
      </div>
      <p class="small muted">Satış fiyatı, stok ve görseller katalogdan gelir (Ürün & Stok ekranı). İndirim rozeti yalnızca gerçek üstü çizili fiyat girilirse gösterilir.</p>
      <p class="form-error"></p><div class="row"><span class="spacer"></span><button class="btn" type="button" data-close>Vazgeç</button><button class="btn btn-primary" type="submit">Kaydet</button></div></form>`);
    $('#sfp-form', body).addEventListener('submit', (e) => {
      e.preventDefault();
      submitting(e.target, async () => {
        const v = formData(e.target);
        await api(`/api/storefront/products/${x.id}`, { method: 'PATCH', body: { published: v.published === '1', featured: v.featured === '1', title: v.title || null,
          description: v.description || null, color: v.color || null, size: v.size || null, group_code: v.group_code || null,
          seo_title: v.seo_title || null, seo_description: v.seo_description || null,
          compare_at_price: v.compare_at_price === '' ? 0 : Number(v.compare_at_price), sort_order: parseInt(v.sort_order || '0', 10) } });
        closeLayer('modal'); toast('Kaydedildi. Mağazada en geç 30 sn içinde görünür.'); sfProducts(box, page, f);
      });
    });
  }));
}

const SFS_TONE = { draft: 'tone-warn', sent: 'tone-good', manual_sent: 'tone-good', failed: 'tone-bad', cancelled: '' };
async function sfSupplier(box, page = 1, status = 'draft') {
  const d = await api('/api/storefront/supplier-orders', { query: { status: status === 'all' ? '' : status, page, page_size: 25 } });
  const modes = { off: 'Kapalı', manual: 'Panelden onayla', auto: 'Otomatik' };
  box.innerHTML = renderVal(html`<div class="card">
    <div class="card-head"><div><h2>Web siparişlerinin tedarikçiye aktarımı</h2>
      <p>Yalnızca Trendçantanız web siparişleri içindir; Trendyol → Çanta Bayim otomasyonu ayrı çalışır ve buradan etkilenmez. Ödemesi alınmış veya kapıda ödemeli web siparişleri için tedarikçi başına taslak hazırlanır (mod: <b>${modes[d.mode] || d.mode}</b>, Mağaza ayarları).
      Tedarikçinin sipariş API bağlantısı henüz tanımlı değilse siparişi tedarikçiye iletip “Manuel iletildi” olarak işaretleyin.</p></div>
      <select id="sfs-status" aria-label="Durum">${[['draft', 'Onay bekleyen'], ['all', 'Tümü'], ['sent', 'Gönderildi'], ['manual_sent', 'Manuel iletildi'], ['failed', 'Hatalı'], ['cancelled', 'İptal']].map(([v, l]) => html`<option value="${v}" ${raw(v === status ? 'selected' : '')}>${l}</option>`)}</select></div>
    ${d.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Sipariş</th><th>Tedarikçi</th><th>Ürünler</th><th>Teslimat</th><th class="r">Maliyet</th><th>Durum</th><th></th></tr></thead><tbody>
      ${d.items.map((x) => html`<tr><td><a href="#" data-order="${x.order_id}"><b>${x.order_code}</b></a><span class="muted small">${dateTime(x.created_at)}</span></td>
        <td>${x.supplier_name || '—'}</td>
        <td>${(x.payload?.lines || []).map((l) => html`<div class="small"><b>${l.sku || l.barcode || '—'}</b> × ${l.quantity}<span class="muted ellipsis">${l.name}</span></div>`)}</td>
        <td class="small">${x.payload?.ship_to ? html`${x.payload.ship_to.full_name}<br>${x.payload.ship_to.district} / ${x.payload.ship_to.city}` : '—'}</td>
        <td class="r num">${money(x.cost)}</td>
        <td><span class="badge ${SFS_TONE[x.status] || ''}">${x.status_label}</span>${x.external_supplier_order_id ? html`<span class="muted small">${x.external_supplier_order_id}</span>` : ''}${x.last_error ? html`<span class="small neg">${x.last_error}</span>` : ''}</td>
        <td class="r">${['draft', 'failed'].includes(x.status) ? html`<span class="row nowrap">${x.can_send ? html`<button class="btn btn-sm btn-primary" data-sfsend="${x.id}">Gönder</button>` : ''}
          <button class="btn btn-sm" data-sfmanual="${x.id}">Manuel iletildi</button><button class="btn btn-sm" data-sfscancel="${x.id}">İptal</button></span>` : ''}</td></tr>`)}
      </tbody></table></div>${pager(d, (p) => sfSupplier(box, p, status))}` : empty('Kayıt yok', status === 'draft' ? 'Onay bekleyen tedarikçi siparişi yok.' : 'Bu durumda kayıt yok.')}</div>`);
  $('#sfs-status').addEventListener('change', (e) => sfSupplier(box, 1, e.target.value));
  $$('[data-order]', box).forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); showOrder(Number(a.dataset.order)); }));
  $$('[data-sfsend]', box).forEach((b) => b.addEventListener('click', async () => {
    try { const r = await api(`/api/storefront/supplier-orders/${b.dataset.sfsend}/send`, { method: 'POST' }); toast(r.ok ? `Gönderildi: ${r.external_id}` : `Gönderilemedi: ${r.error}`, !r.ok); sfSupplier(box, page, status); } catch (e) { fail(e); }
  }));
  $$('[data-sfmanual]', box).forEach((b) => b.addEventListener('click', () => {
    const body = openModal(html`<h2>Tedarikçiye manuel iletildi</h2><form class="stack" id="sfm-form">
      <label>Tedarikçi sipariş no (isteğe bağlı)<input name="external_id" maxlength="100"></label>
      <p class="form-error"></p><div class="row"><span class="spacer"></span><button class="btn" type="button" data-close>Vazgeç</button><button class="btn btn-primary" type="submit">Kaydet</button></div></form>`);
    $('#sfm-form', body).addEventListener('submit', (e) => {
      e.preventDefault();
      submitting(e.target, async () => {
        await api(`/api/storefront/supplier-orders/${b.dataset.sfmanual}/mark-sent`, { method: 'POST', body: { external_id: formData(e.target).external_id || null } });
        closeLayer('modal'); toast('İşaretlendi'); sfSupplier(box, page, status);
      });
    });
  }));
  $$('[data-sfscancel]', box).forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Tedarikçi taslağı iptal edilsin mi?')) return;
    try { await api(`/api/storefront/supplier-orders/${b.dataset.sfscancel}/cancel`, { method: 'POST' }); toast('İptal edildi'); sfSupplier(box, page, status); } catch (e) { fail(e); }
  }));
}

async function sfIntegrations(box) {
  const d = await api('/api/storefront/integrations');
  const st = (ok, on, off) => (ok ? html`<span class="badge tone-good">${on}</span>` : html`<span class="badge">${off}</span>`);
  const n = d.notifications || {};
  box.innerHTML = renderVal(html`<div class="grid grid-2">
    <div class="card"><h3>Kartla ödeme</h3><p class="mt">${st(d.payment.active, `Etkin: ${d.payment.active}${d.payment.test_mode ? ' (TEST modu)' : ''}`, 'Kapalı')}</p>
      <p class="small muted mt">Sunucu .env: <code>STOREFRONT_PAYMENT_PROVIDER=paytr</code> + <code>PAYTR_MERCHANT_ID / KEY / SALT</code> veya <code>iyzico</code> + <code>IYZICO_API_KEY / SECRET_KEY</code>. PayTR panelinde bildirim adresi: <code>${d.base_url || 'https://alan-adınız'}/odeme/geri-donus/paytr</code>. Anahtarlar panele/veritabanına yazılmaz.</p></div>
    <div class="card"><h3>E-posta bildirimleri</h3><p class="mt">${st(d.email.active, 'Etkin (SMTP)', d.email.configured ? 'SMTP var, panelde kapalı' : 'Kapalı (SMTP yok)')}</p>
      <p class="small muted mt">Sipariş alındı, ödeme onayı, kargoya verildi ve iptal e-postaları; müşteri şifre sıfırlama. Sahibe yeni sipariş e-postası: ${d.email.owner_alerts ? 'açık' : 'kapalı (STOREFRONT_ORDER_ALERT_EMAILS)'}.</p>
      <p class="small">Kuyrukta ${num(n.queued)} · hatalı ${num(n.failed)} · son 7 gün gönderilen ${num(n.sent_7d)}</p></div>
    <div class="card"><h3>SMS bildirimleri</h3><p class="mt">${st(d.sms.active, `Etkin (${d.sms.provider})`, d.sms.configured ? 'Sağlayıcı var, panelde kapalı' : 'Kapalı (sağlayıcı yok)')}</p>
      <p class="small muted mt">Desteklenen: Netgsm (<code>SMS_PROVIDER=netgsm</code>, <code>NETGSM_USERCODE / PASSWORD / HEADER</code>). Onaylı SMS başlığı gerekir.</p></div>
    <div class="card"><h3>E-fatura / e-arşiv</h3><p class="mt">${st(d.einvoice.active, `Etkin: ${d.einvoice.provider}`, 'Kapalı')}</p>
      <p class="small muted mt">Entegratör bağlantısı ${d.einvoice.provider_configured ? 'tanımlı' : 'yok (EINVOICE_PROVIDER; entegratör sözleşmesi ve bağlantı kodu gerekir)'}; panel anahtarı ${d.einvoice.setting_enabled ? 'açık' : 'kapalı'}. Pazaryeri siparişlerinin faturaları bu katmandan kesilmez.</p></div>
    <div class="card"><h3>Tedarikçiye aktarım</h3><p class="mt">Mod: <b>${{ off: 'Kapalı', manual: 'Panelden onayla', auto: 'Otomatik' }[d.supplier_forwarding.mode]}</b></p>
      <p class="small muted mt">API bağlantısı olan tedarikçi: ${d.supplier_forwarding.connectors.length ? d.supplier_forwarding.connectors.join(', ') : 'yok (manuel iletim)'}.</p></div>
    <div class="card"><h3>Alan adı</h3><p class="mt">${d.base_url ? html`<a href="${d.base_url}" target="_blank" rel="noopener">${d.base_url}</a>` : html`<span class="badge tone-warn">STOREFRONT_BASE_URL tanımlı değil</span>`}</p>
      <p class="small muted mt">Canonical, sitemap, OpenGraph ve ödeme dönüş adresleri bu adresi kullanır.</p></div>
  </div>`);
}

async function sfSettings(box) {
  const s = await api('/api/storefront/settings');
  const v = s.values;
  const txt = (name, label, value, attrs = '') => html`<label>${label}<input name="${name}" value="${value ?? ''}" ${raw(attrs)}></label>`;
  box.innerHTML = renderVal(html`<form class="card" id="sf-set"><div class="card-head"><div><h2>Mağaza ayarları</h2><p>Satıcı bilgileri ve yasal metinler (Mesafeli Satış Sözleşmesi, Ön Bilgilendirme Formu, KVKK, iade koşulları) 6502 sayılı Kanun ve Mesafeli Sözleşmeler Yönetmeliği gereği zorunludur; eksikse mağaza sipariş almaz. Metinleri hukuk danışmanınızla hazırlayın.</p></div></div>
    <fieldset class="fieldset"><legend>Genel</legend><div class="form-grid">
      <label class="check full"><input type="checkbox" name="enabled" ${raw(v.enabled ? 'checked' : '')}>Web mağazası açık (kapalıyken ürünler görünür, sipariş alınmaz)</label>
      <label class="check full"><input type="checkbox" name="auto_publish" ${raw(v.auto_publish ? 'checked' : '')}>Aktif, fiyatı ve görseli olan katalog ürünlerini otomatik yayınla</label>
      ${txt('announcement', 'Duyuru çubuğu (boş = gizli; yalnızca geçerli bilgi yazın)', v.announcement, 'maxlength="200"')}
      ${txt('hero_product_id', 'Hero ürün ID (boş = en çok satan / otomatik)', v.hero_product_id, 'type="number" min="1"')}
      ${txt('stock_buffer', 'Web stok güvenlik payı (adet)', v.stock_buffer, 'type="number" min="0"')}
      ${txt('committed_window_hours', 'Tedarikçi stoğuna henüz yansımamış siparişleri düşme süresi (saat)', v.committed_window_hours, 'type="number" min="1"')}
    </div></fieldset>
    <fieldset class="fieldset mt"><legend>Kargo</legend><div class="form-grid">
      ${txt('shipping_fee', 'Kargo ücreti (₺, 0 = ücretsiz)', v.shipping_fee, 'type="number" step="0.01" min="0"')}
      ${txt('free_shipping_threshold', 'Ücretsiz kargo limiti (₺, boş = yok)', v.free_shipping_threshold, 'type="number" step="0.01" min="0"')}
    </div></fieldset>
    <fieldset class="fieldset mt"><legend>Ödeme</legend><div class="form-grid">
      <label class="check full"><input type="checkbox" name="bank_transfer_enabled" ${raw(v.bank_transfer_enabled ? 'checked' : '')}>Havale / EFT</label>
      ${txt('bank_transfer_iban', 'IBAN', v.bank_transfer_iban, 'maxlength="34" placeholder="TR00 0000 0000 0000 0000 0000 00"')}
      ${txt('bank_transfer_account_name', 'Hesap sahibi', v.bank_transfer_account_name, 'maxlength="200"')}
      ${txt('bank_transfer_bank_name', 'Banka', v.bank_transfer_bank_name, 'maxlength="200"')}
      ${txt('bank_transfer_days', 'Ödeme süresi (gün; sonra sipariş kapanır, stok serbest kalır)', v.bank_transfer_days, 'type="number" min="1"')}
      <label class="check full"><input type="checkbox" name="cash_on_delivery_enabled" ${raw(v.cash_on_delivery_enabled ? 'checked' : '')}>Kapıda ödeme (kargo firmanızın hizmeti olmalı)</label>
      ${txt('cash_on_delivery_fee', 'Kapıda ödeme hizmet bedeli (₺)', v.cash_on_delivery_fee, 'type="number" step="0.01" min="0"')}
      <p class="small muted full">Kartla ödeme: PayTR veya iyzico anahtarları sunucu .env dosyasına girilince otomatik açılır; anahtarlar yalnızca sunucu ortamında tutulur.</p>
    </div></fieldset>
    <fieldset class="fieldset mt"><legend>Sipariş sonrası</legend><div class="form-grid">
      <label>Web siparişlerini tedarikçiye aktarım<select name="supplier_forwarding_mode">${[['off', 'Kapalı'], ['manual', 'Panelden onayla (önerilen)'], ['auto', 'Ödeme alınınca otomatik (API bağlantısı olan tedarikçiler)']].map(([k, l]) => html`<option value="${k}" ${raw(v.supplier_forwarding_mode === k ? 'selected' : '')}>${l}</option>`)}</select></label>
      <label class="check full"><input type="checkbox" name="notify_email" ${raw(v.notify_email ? 'checked' : '')}>Müşteriye e-posta bildirimleri (SMTP sunucu ayarı gerekir)</label>
      <label class="check full"><input type="checkbox" name="notify_sms" ${raw(v.notify_sms ? 'checked' : '')}>Müşteriye SMS bildirimleri (SMS sağlayıcısı gerekir)</label>
      <label class="check full"><input type="checkbox" name="einvoice_enabled" ${raw(v.einvoice_enabled ? 'checked' : '')}>Web siparişleri için e-fatura/e-arşiv oluştur (entegratör bağlantısı gerekir)</label>
      <p class="small muted full">Sağlayıcı bilgileri sunucu .env dosyasındadır; tanımlı değilse bu seçenekler açık olsa bile hiçbir dış servise istek yapılmaz. Durum: Entegrasyonlar sekmesi.</p>
    </div></fieldset>
    <fieldset class="fieldset mt"><legend>Satıcı bilgileri</legend><div class="form-grid">
      ${s.seller_fields.map((f) => txt(`seller.${f.key}`, f.label + (f.required ? ' *' : ''), v.seller[f.key], 'maxlength="500"'))}
    </div></fieldset>
    <fieldset class="fieldset mt"><legend>Sosyal medya</legend><div class="form-grid">
      ${s.social_fields.map((k) => txt(`social.${k}`, k === 'whatsapp' ? 'WhatsApp (ülke koduyla, ör. 905xxxxxxxxx)' : `${k[0].toUpperCase()}${k.slice(1)} (https://…)`, v.social[k]))}
    </div></fieldset>
    <fieldset class="fieldset mt"><legend>Yasal metinler ve sayfalar</legend><div class="stack">
      ${s.legal_pages.map((p) => html`<label>${p.title}${p.required ? ' *' : ''}<textarea name="legal.${p.slug}" rows="5">${v.legal[p.slug] || ''}</textarea></label>`)}
    </div></fieldset>
    <p class="form-error"></p><button class="btn btn-primary mt" type="submit">Kaydet</button></form>`);
  $('#sf-set').addEventListener('submit', (e) => {
    e.preventDefault();
    submitting(e.target, async () => {
      const el = e.target.elements;
      const values = { seller: {}, social: {}, legal: {} };
      ['enabled', 'auto_publish', 'bank_transfer_enabled', 'cash_on_delivery_enabled', 'notify_email', 'notify_sms', 'einvoice_enabled'].forEach((k) => { values[k] = el[k].checked; });
      values.supplier_forwarding_mode = el.supplier_forwarding_mode.value;
      ['announcement', 'bank_transfer_iban', 'bank_transfer_account_name', 'bank_transfer_bank_name'].forEach((k) => { values[k] = el[k].value.trim(); });
      ['stock_buffer', 'committed_window_hours', 'bank_transfer_days'].forEach((k) => { values[k] = parseInt(el[k].value || '0', 10); });
      ['shipping_fee', 'cash_on_delivery_fee'].forEach((k) => { values[k] = el[k].value || '0'; });
      values.free_shipping_threshold = el.free_shipping_threshold.value || null;
      values.hero_product_id = el.hero_product_id.value ? parseInt(el.hero_product_id.value, 10) : null;
      s.seller_fields.forEach((f) => { values.seller[f.key] = el[`seller.${f.key}`].value; });
      s.social_fields.forEach((k) => { values.social[k] = el[`social.${k}`].value; });
      s.legal_pages.forEach((p) => { values.legal[p.slug] = el[`legal.${p.slug}`].value; });
      const r = await api('/api/storefront/settings', { method: 'PUT', body: { values } });
      toast(r.changed.length ? 'Mağaza ayarları kaydedildi' : 'Değişiklik yok');
      refresh();
    });
  });
}

// ---- Kullanıcılar (yalnızca yönetici)
PAGES.users = {
  title: 'Kullanıcılar', icon: 'users', admin: true,
  async render() {
    setHeader('Kullanıcılar', 'Panel erişimi ve roller', can('admin') ? html`<button class="btn btn-primary" id="add-user">+ Kullanıcı ekle</button>` : '');
    if (!can('admin')) { view().innerHTML = renderVal(empty('Yetkiniz yok', 'Kullanıcı yönetimi yalnızca yöneticilere açıktır.')); return; }
    const users = await api('/api/users');
    view().innerHTML = renderVal(html`<div class="card">
      <div class="table-wrap"><table><thead><tr><th>Kullanıcı</th><th>Ad</th><th>Rol</th><th>Durum</th><th>Son giriş</th><th>Oluşturma</th><th></th></tr></thead><tbody>
      ${users.map((u) => html`<tr><td><b>${u.username}</b></td><td>${u.full_name || '—'}</td><td>${ROLE_LABELS[u.role]}</td>
        <td>${u.is_active ? (u.locked ? html`<span class="badge tone-warn">Kilitli</span>` : html`<span class="badge tone-good">Aktif</span>`) : html`<span class="badge">Pasif</span>`}</td>
        <td>${dateTime(u.last_login_at)}</td><td>${date(u.created_at)}</td>
        <td class="r">${u.id !== state.user.id ? html`<button class="btn btn-sm" data-user="${u.id}">Düzenle</button>` : html`<span class="muted small">Siz</span>`}</td></tr>`)}</tbody></table></div></div>
      <div class="grid grid-3 mt">
        <div class="card"><h3>İzleyici</h3><p class="small muted">Tüm ekranları görüntüler; değişiklik yapamaz.</p></div>
        <div class="card"><h3>Operatör</h3><p class="small muted">Sipariş durumu, ürün maliyeti, gider, tedarikçi ve senkronizasyon işlemleri.</p></div>
        <div class="card"><h3>Yönetici</h3><p class="small muted">Operatör yetkileri + ayarlar, kullanıcılar ve denetim kaydı.</p></div>
      </div>
      <p class="small muted mt">Hesap 5 hatalı girişte 15 dakika kilitlenir. Parola sıfırlandığında veya kullanıcı pasifleştirildiğinde açık oturumları kapatılır.</p>`);
    const userForm = (u) => {
      const body = openModal(html`<h2>${u ? `Kullanıcı · ${u.username}` : 'Yeni kullanıcı'}</h2><form class="stack" id="uform">
        ${u ? '' : html`<label>Kullanıcı adı<input name="username" required minlength="3" maxlength="50" pattern="[A-Za-z0-9_.\\-]+" autocomplete="off"></label>`}
        <label>Ad soyad<input name="full_name" maxlength="100" value="${u?.full_name || ''}"></label>
        <label>Rol<select name="role">${Object.entries(ROLE_LABELS).map(([v, l]) => html`<option value="${v}" ${raw(u?.role === v ? 'selected' : '')}>${l}</option>`)}</select></label>
        ${u ? html`<label class="check"><input type="checkbox" name="is_active" value="1" ${raw(u.is_active ? 'checked' : '')}>Aktif</label>` : ''}
        <label>${u ? 'Yeni parola (boş bırakılırsa değişmez; kilit de kaldırılır)' : 'Parola (en az 12 karakter)'}<input type="password" name="password" autocomplete="new-password" ${raw(u ? '' : 'required minlength="12"')}></label>
        <p class="form-error"></p><div class="row"><span class="spacer"></span><button class="btn" type="button" data-close>Vazgeç</button><button class="btn btn-primary" type="submit">Kaydet</button></div></form>`);
      $('#uform', body).addEventListener('submit', (e) => {
        e.preventDefault();
        submitting(e.target, async () => {
          const d = formData(e.target);
          if (u) await api(`/api/users/${u.id}`, { method: 'PATCH', body: { full_name: d.full_name || null, role: d.role, is_active: d.is_active === '1', ...(d.password ? { new_password: d.password } : {}) } });
          else await api('/api/users', { method: 'POST', body: { username: d.username, full_name: d.full_name || null, role: d.role, password: d.password } });
          closeLayer('modal'); toast('Kullanıcı kaydedildi'); refresh();
        });
      });
    };
    $('#add-user')?.addEventListener('click', () => userForm());
    $$('[data-user]').forEach((b) => b.addEventListener('click', () => userForm(users.find((x) => String(x.id) === b.dataset.user))));
  },
};

// ------------------------------------------------------------------- yönlendirme
const NAV = ['dashboard', 'alerts', 'orders', 'products', 'storefront', 'suppliers', 'transfer', 'shipping', 'finance', 'ads', 'reports', 'integrations', 'system', 'settings', 'users'];
// Sade çizgi ikonlar (24x24, currentColor)
const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  orders: '<path d="M3 4h2l2.4 11.2a2 2 0 0 0 2 1.6h7.7a2 2 0 0 0 2-1.5L21 8H6.2"/><circle cx="10" cy="20" r="1.3"/><circle cx="18" cy="20" r="1.3"/>',
  products: '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/>',
  shipping: '<path d="M3 6h11v10H3zM14 10h4l3 3v3h-7"/><circle cx="7" cy="18" r="2"/><circle cx="17" cy="18" r="2"/>',
  suppliers: '<path d="M3 21V10l6 3V10l6 3V6l6-3v18z"/><path d="M7 17h2M12 17h2M17 17h2"/>',
  transfer: '<path d="M4 7h13l-3-3M20 17H7l3 3"/>',
  finance: '<path d="M8 4v16M8 9l7-3M8 13l7-3M5 20h9a5 5 0 0 0 5-5"/>',
  reports: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  integrations: '<path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0z"/><path d="M12 16v5"/>',
  system: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  ads: '<path d="M3 10v4h3l6 4V6L6 10z"/><path d="M16 9a4 4 0 0 1 0 6M19 6a8 8 0 0 1 0 12"/>',
  alerts: '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  storefront: '<path d="M4 9h16l-1 11H5z"/><path d="M8 9V7a4 4 0 0 1 8 0v2"/><path d="M3 5h18"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6 6 0 0 1 3.5 6"/>',
};
const icon = (name) => raw(`<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`);
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [path, qs] = h.split('?');
  return { page: PAGES[path] ? path : 'dashboard', params: new URLSearchParams(qs || '') };
}
function renderNav(active) {
  $('#nav').innerHTML = renderVal(NAV.filter((k) => !PAGES[k].admin || can('admin')).map((k) => html`<a href="#/${k}" class="${k === active ? 'active' : ''}" ${raw(k === active ? 'aria-current="page"' : '')}><span class="ico">${icon(PAGES[k].icon)}</span>${PAGES[k].nav || PAGES[k].title}${k === 'alerts' && state.alertCount ? html`<span class="nav-count">${num(state.alertCount)}</span>` : ''}</a>`));
}
// ---- Bildirim zili: açık uyarı sayısı (panel içi bildirim kanalı)
async function refreshBell() {
  try {
    const s = await api('/api/alerts/summary');
    state.alertSummary = s; state.alertCount = s.total;
    const b = $('#bell');
    b.hidden = false;
    b.classList.toggle('has-critical', !!s.critical);
    $('#bell-count').textContent = s.total ? (s.total > 99 ? '99+' : String(s.total)) : '';
    b.setAttribute('aria-label', s.total ? `${s.total} uyarı ilgilenmeni bekliyor` : 'Uyarı yok');
    b.title = s.total ? `${s.total} konu ilgilenmeni bekliyor (${s.critical} kritik)` : 'Açık uyarı yok';
    renderNav(parseHash().page);
  } catch { /* zil kritik değil */ }
}
let routeSeq = 0;
async function route() {
  if (!state.user) return;
  const { page, params } = parseHash();
  const seq = ++routeSeq;
  renderNav(page);
  document.body.classList.remove('nav-open');
  if (!$('#drawer').hidden) closeLayer('drawer');
  refreshBell();
  try {
    await PAGES[page].render(params);
  } catch (e) {
    if (seq !== routeSeq || e.status === 401) return;
    view().innerHTML = renderVal(html`<div class="notice bad">Sayfa yüklenemedi: ${errorView(e)}</div>`);
  }
}
const refresh = () => route();

// ------------------------------------------------------------------- oturum
function showLogin() {
  state.user = null;
  $('#app').hidden = true;
  $('#login').hidden = false;
  closeLayer('drawer'); closeLayer('modal');
  $('#login-form [name=username]').focus();
}
async function startApp(user) {
  state.user = user;
  state.meta = await api('/api/orders/meta');
  $('#user-name').textContent = user.full_name || user.username;
  $('#user-role').textContent = ROLE_LABELS[user.role] || user.role;
  $('#login').hidden = true;
  $('#app').hidden = false;
  route();
}
$('#login-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const err = $('#login-error');
  err.textContent = '';
  submitting(e.target, async () => {
    try {
      const r = await api('/api/auth/login', { method: 'POST', body: formData(e.target) });
      e.target.reset();
      await startApp(r.user);
    } catch (ex) { err.textContent = ex.message; }
  });
});
$('#logout').addEventListener('click', async () => { try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* yok say */ } showLogin(); });
$('#menu-toggle').addEventListener('click', () => document.body.classList.toggle('nav-open'));
$('#scrim').addEventListener('click', () => document.body.classList.remove('nav-open'));
function applyTheme(t) { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
applyTheme(localGet('th.theme'));
$('#theme-toggle').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  const next = dark ? 'light' : 'dark';
  applyTheme(next); localSet('th.theme', next);
});
window.addEventListener('hashchange', route);
$('#bell').addEventListener('click', () => { location.hash = '#/alerts'; });
setInterval(() => { if (state.user && !document.hidden) refreshBell(); }, 120000);

(async () => {
  try { await startApp(await api('/api/auth/me')); } catch (e) { if (e.status !== 401) { showLogin(); $('#login-error').textContent = e.message; } }
})();
