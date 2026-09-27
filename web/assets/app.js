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

const ROLE_LABELS = { admin: 'Yönetici', operator: 'Operatör', viewer: 'İzleyici' };
const ROLE_RANK = { viewer: 0, operator: 1, admin: 2 };
const state = { user: null, meta: null, period: localGet('th.period') || '30d' };
const can = (role) => state.user && ROLE_RANK[state.user.role] >= ROLE_RANK[role];

function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { /* yok say */ } }

// ------------------------------------------------------------------------ API
class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
async function api(path, { method = 'GET', body, query } = {}) {
  let url = path;
  if (query) {
    const qs = new URLSearchParams();
    Object.entries(query).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '' && v !== false) qs.set(k, v); });
    const s = qs.toString();
    if (s) url += (url.includes('?') ? '&' : '?') + s;
  }
  const headers = { 'X-Requested-With': 'TrendHub' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(url, { method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body) });
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
    throw new ApiError(res.status, msg || `Hata (${res.status})`);
  }
  return data;
}

// --------------------------------------------------------------- arayüz öğeleri
function toast(message, bad = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), bad ? 6000 : 3500);
}
const fail = (e) => { if (e.status !== 401) toast(e.message || 'Beklenmeyen hata', true); };

let lastFocus = null;
function openLayer(id, content) {
  lastFocus = document.activeElement;
  const layer = $(`#${id}`);
  $(`#${id}-body`).innerHTML = renderVal(content);
  layer.hidden = false;
  const f = layer.querySelector('input, select, textarea, button:not([data-close])');
  (f || layer.querySelector('[data-close]')).focus();
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
const estimateBadge = (flag) => (flag ? html` <span class="badge plain tone-warn" title="Komisyon/kargo/hizmet bedeli veya ürün maliyeti tahmini ya da eksik">Tahmini</span>` : '');

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

function periodSeg(current, onChange) {
  const opts = [['today', 'Bugün'], ['7d', '7 gün'], ['30d', '30 gün'], ['90d', '90 gün']];
  setTimeout(() => $$('[data-period]').forEach((b) => b.addEventListener('click', () => {
    state.period = b.dataset.period; localSet('th.period', state.period); onChange(state.period);
  })));
  return html`<div class="seg" role="group" aria-label="Dönem">${opts.map(([v, l]) =>
    html`<button data-period="${v}" class="${v === current ? 'on' : ''}" aria-pressed="${v === current}">${l}</button>`)}</div>`;
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
  try { await fn(); } catch (e) { if (err) err.textContent = e.message; else fail(e); } finally { if (btn) btn.disabled = false; }
}

// ---------------------------------------------------------------- grafik (SVG)
function lineChart(points, series) {
  if (!points.length) return empty('Veri yok', 'Seçilen dönemde sipariş bulunmuyor.');
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
function profitBars(t, periodExpenses) {
  const revenue = Number(t.revenue) || 0;
  const rows = COMPONENTS.map(([k, l]) => [l, Number(t[k]) || 0]);
  if (periodExpenses !== undefined) rows.push(['Dönem giderleri', Number(periodExpenses) || 0]);
  const net = revenue - rows.reduce((a, [, v]) => a + v, 0);
  const scale = Math.max(revenue, 1);
  const bar = (v, cls = '') => html`<div class="bar-track"><div class="bar-fill ${cls}" style="width:${Math.min(100, (Math.abs(v) / scale) * 100).toFixed(2)}%"></div></div>`;
  return html`<div class="bars">
    <div class="bar-row"><span>Ciro</span>${bar(revenue, 'rev')}<span class="r num">${money(revenue)}</span></div>
    ${rows.map(([l, v]) => html`<div class="bar-row"><span>− ${l}</span>${bar(v)}<span class="r num">${money(v)}</span></div>`)}
    <div class="bar-row total"><span>Net kâr</span>${bar(net, 'profit')}<span class="r num ${signClass(net)}">${money(net)}</span></div>
  </div>`;
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

// ---- Dashboard
PAGES.dashboard = {
  title: 'Genel Bakış', icon: '▦', nav: 'Dashboard',
  async render() {
    const draw = async () => {
      setHeader('Genel Bakış', 'Tüm pazaryerlerinin merkezi özeti', periodSeg(state.period, draw));
      loading();
      const d = await api('/api/dashboard', { query: { period: state.period } });
      const s = d.summary, t = s.orders;
      const connected = d.integrations.filter((i) => i.state === 'connected').length;
      view().innerHTML = renderVal(html`
        <div class="chips" style="margin-bottom:16px">${d.integrations.map((i) => html`
          <a class="chip" href="#/integrations"><span class="dot ${i.state === 'connected' ? 'good' : i.state === 'error' ? 'bad' : i.state === 'not_connected' ? '' : 'warn'}"></span>${i.name}
          <span class="muted small">${i.state_label}</span></a>`)}</div>
        ${connected === 0 ? html`<div class="notice info" style="margin-bottom:16px">Henüz bağlı bir pazaryeri yok. Sipariş verisi, <a href="#/integrations">Entegrasyonlar</a> sayfasındaki API bilgileri sunucu ortamına tanımlanıp senkronizasyon çalıştığında görünecek.</div>` : ''}
        <div class="grid grid-4">
          <div class="card kpi"><div class="label">Ciro</div><div class="value">${money0(t.revenue)}</div><div class="sub">İptaller hariç</div></div>
          <div class="card kpi"><div class="label">Sipariş</div><div class="value">${num(t.orders)}</div><div class="sub">${t.orders ? `Sepet ort. ${money0(t.revenue / t.orders)}` : 'Dönemde sipariş yok'}</div></div>
          <div class="card kpi"><div class="label">Net kâr</div><div class="value ${signClass(s.net_profit_after_expenses)}">${money0(s.net_profit_after_expenses)}</div><div class="sub">Dönem giderleri (${money0(s.expenses.total)}) düşülmüş${estimateBadge(t.estimated_orders > 0)}</div></div>
          <div class="card kpi"><div class="label">Kâr marjı</div><div class="value">${pct(s.margin_after_expenses)}</div><div class="sub">Sipariş marjı: ${pct(s.margin)}</div></div>
        </div>
        <div class="grid grid-main mt">
          <div class="card"><div class="card-head"><div><h2>Günlük ciro ve net kâr</h2><p>${date(d.range.from)} – ${date(d.range.to)}</p></div></div>
            ${lineChart(d.daily, [{ key: 'revenue', label: 'Ciro', color: 'var(--series-1)' }, { key: 'net_profit', label: 'Net kâr', color: 'var(--series-2)' }])}</div>
          <div class="card"><div class="card-head"><h2>Kâr dökümü</h2><a href="#/finance" class="small">Finans →</a></div>${profitBars(t, s.expenses.total)}</div>
        </div>
        <div class="card mt"><div class="card-head"><div><h2>Sipariş durumları</h2><p>Tüm açık kayıtlar · parantez içinde seçili dönem</p></div></div>
          <div class="status-grid">${d.statuses.map((st) => html`
            <button class="status-tile ${st.code === 'needs_review' && st.count ? 'alert' : ''}" data-status="${st.code}">
              ${statusBadge(st.code, st.label)}<b>${num(st.count)}</b><span class="muted small">(${num(st.period_count)})</span></button>`)}</div></div>
        <div class="grid grid-main mt">
          <div class="card"><div class="card-head"><h2>Son siparişler</h2><a href="#/orders" class="small">Tümü →</a></div>
            ${d.recent_orders.length ? html`<div class="table-wrap"><table><thead><tr><th>Sipariş</th><th>Pazaryeri</th><th>Tarih</th><th>Durum</th><th class="r">Ciro</th><th class="r">Net kâr</th></tr></thead><tbody>
            ${d.recent_orders.map((o) => html`<tr class="click" data-order="${o.id}"><td>${o.external_order_id}</td><td>${o.marketplace_name || '—'}</td><td>${dateTime(o.order_date)}</td><td>${statusBadge(o.internal_status, o.status_label)}</td><td class="r num">${money(o.gross_revenue)}</td><td class="r num ${signClass(o.net_profit)}">${money(o.net_profit)}</td></tr>`)}
            </tbody></table></div>` : empty('Henüz sipariş yok', 'Pazaryeri senkronizasyonu çalıştığında siparişler burada listelenir.')}</div>
          <div class="card"><div class="card-head"><h2>Uyarılar</h2></div><div class="stack">
            ${alertRow(d.alerts.needs_review, 'inceleme bekleyen sipariş', '#/orders?status=needs_review', 'bad')}
            ${alertRow(d.alerts.missing_cost_orders, 'siparişte ürün maliyeti eksik', '#/products?missing_cost=1', 'warn')}
            ${alertRow(d.alerts.open_errors, 'açık sistem hatası', '#/system', 'bad')}
            ${alertRow(d.queue.failed_24h, 'başarısız iş (24 saat)', '#/system', 'warn')}
            <div class="row nowrap"><span class="dot ${d.alerts.live_workers ? 'good' : 'bad'}"></span>${d.alerts.live_workers ? 'Worker çalışıyor' : 'Worker çalışmıyor — senkronizasyon yapılamaz'}</div>
          </div></div>
        </div>`);
      $$('[data-status]').forEach((b) => b.addEventListener('click', () => { location.hash = `#/orders?status=${b.dataset.status}`; }));
      $$('[data-order]').forEach((r) => r.addEventListener('click', () => showOrder(r.dataset.order)));
    };
    await draw();
  },
};
function alertRow(n, text, href, tone) {
  if (!n) return '';
  return html`<a class="notice ${tone}" href="${href}"><b class="num">${num(n)}</b> ${text} →</a>`;
}

// ---- Siparişler
PAGES.orders = {
  title: 'Siparişler', icon: '🛒',
  async render(params) {
    const f = { status: params.get('status') || '', marketplace: params.get('marketplace') || '', q: params.get('q') || '',
      date_from: params.get('date_from') || '', date_to: params.get('date_to') || '', sort: params.get('sort') || 'date_desc',
      loss_only: params.get('loss_only') === '1', page: Number(params.get('page') || 1) };
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
        <th>Sipariş</th><th>Pazaryeri</th><th>Tarih</th><th>Müşteri</th><th>Durum</th><th class="r">Adet</th><th class="r">Ciro</th><th class="r">Net kâr</th><th class="r">Marj</th></tr></thead><tbody>
        ${d.items.map((o) => html`<tr class="click" data-order="${o.id}"><td><b>${o.external_order_id}</b>${o.review_reason ? html`<span class="ellipsis small neg" title="${o.review_reason}">${o.review_reason}</span>` : ''}</td>
          <td>${o.marketplace_name || '—'}</td><td>${dateTime(o.order_date)}</td><td><span class="ellipsis">${o.customer_name || '—'}</span><span class="muted small">${o.customer_city || ''}</span></td>
          <td>${statusBadge(o.internal_status, o.status_label)}</td><td class="r num">${num(o.item_count)}</td><td class="r num">${money(o.gross_revenue)}</td>
          <td class="r num ${signClass(o.net_profit)}">${money(o.net_profit)}${estimateBadge(o.finance_is_estimate)}</td><td class="r num">${pct(o.margin)}</td></tr>`)}
        </tbody></table></div>${pager(d, load)}` : empty('Sipariş bulunamadı', 'Filtreleri değiştirin veya pazaryeri senkronizasyonunu bekleyin.'));
      $$('#order-list [data-order]').forEach((r) => r.addEventListener('click', () => showOrder(r.dataset.order)));
    };
    await load(f.page);
  },
};

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
    <div class="grid grid-2">
      <div class="card"><h3>Bilgiler</h3><dl class="kv mt">
        <dt>Sipariş tarihi</dt><dd>${dateTime(o.order_date)}</dd><dt>Müşteri</dt><dd>${o.customer_name || '—'} ${o.customer_city ? `(${o.customer_city})` : ''}</dd>
        <dt>Mağaza</dt><dd>${o.store_name || '—'}</dd><dt>Son senkron</dt><dd>${dateTime(o.last_synced_at)}</dd><dt>Kaynak</dt><dd>${o.source || '—'}</dd></dl></div>
      <div class="card"><h3>Kârlılık ${estimateBadge(o.finance_is_estimate)}</h3><div class="mt">${profitBars(t)}</div>
        <p class="small muted">Marj: <b>${pct(o.margin)}</b></p></div>
    </div>
    <div class="card mt"><h3>Ürünler</h3><div class="table-wrap mt"><table><thead><tr><th>SKU / Ürün</th><th class="r">Adet</th><th class="r">Birim fiyat</th><th class="r">Birim maliyet</th><th class="r">Komisyon</th><th class="r">Kargo</th><th class="r">Net</th><th class="r">Marj</th></tr></thead><tbody>
      ${o.items.map((i) => html`<tr><td><b>${i.sku || i.barcode || '—'}</b><span class="ellipsis small muted">${i.product_name}</span></td><td class="r num">${num(i.quantity)}</td>
        <td class="r num">${money(i.unit_price)}</td><td class="r num">${Number(i.unit_cost) ? money(i.unit_cost) : html`<span class="badge plain tone-warn">Eksik</span>`}</td>
        <td class="r num">${money(i.commission)}</td><td class="r num">${money(i.shipping_cost)}</td><td class="r num ${signClass(i.net_profit)}">${money(i.net_profit)}</td><td class="r num">${pct(i.margin)}</td></tr>`)}
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
}

// ---- Ürün & Stok
PAGES.products = {
  title: 'Ürün & Stok', icon: '📦',
  async render(params) {
    const f = { q: params.get('q') || '', low_stock: params.get('low_stock') === '1', missing_cost: params.get('missing_cost') === '1' };
    setHeader('Ürün & Stok', 'Ürün kataloğu, maliyet geçmişi ve stok', can('operator') ? html`<button class="btn btn-primary" id="add-product">+ Ürün ekle</button>` : '');
    view().innerHTML = renderVal(html`<div class="grid grid-4" id="prod-summary"></div><div class="card mt">
      <form class="filters" id="prod-filters"><input type="search" name="q" placeholder="SKU, barkod veya ürün adı" value="${f.q}" aria-label="Ara">
      <label class="check"><input type="checkbox" name="low_stock" value="1" ${raw(f.low_stock ? 'checked' : '')}>Düşük stok</label>
      <label class="check"><input type="checkbox" name="missing_cost" value="1" ${raw(f.missing_cost ? 'checked' : '')}>Maliyeti eksik</label>
      <button class="btn btn-primary" type="submit">Filtrele</button></form><div id="prod-list"></div></div>
      <p class="small muted">Stok değişiklikleri yalnızca TrendHub'da tutulur; pazaryerlerine stok gönderimi kapalıdır (CONNECTOR_WRITE_ENABLED=false).</p>`);
    $('#prod-filters').addEventListener('submit', (e) => {
      e.preventDefault();
      const q = new URLSearchParams(Object.entries(formData(e.target)).filter(([, v]) => v));
      location.hash = '#/products' + (q.toString() ? '?' + q : '');
    });
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
          <td class="r">${can('operator') ? html`<button class="btn btn-sm" data-cost="${p.id}">Maliyet</button> <button class="btn btn-sm" data-edit="${p.id}">Düzenle</button>` : html`<button class="btn btn-sm" data-cost="${p.id}">Geçmiş</button>`}</td></tr>`)}
        </tbody></table></div>${pager(d, load)}` : empty('Ürün yok', can('operator') ? 'Ürün ekleyerek maliyet takibine başlayın. Sipariş kalemleri SKU/barkod ile otomatik eşlenir.' : 'Henüz ürün tanımlanmamış.'));
      $$('[data-cost]').forEach((b) => b.addEventListener('click', () => costForm(d.items.find((p) => String(p.id) === b.dataset.cost))));
      $$('[data-edit]').forEach((b) => b.addEventListener('click', () => productForm(d.items.find((p) => String(p.id) === b.dataset.edit))));
    };
    await load(1);
  },
};
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
  title: 'Kargo', icon: '🚚',
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
PAGES.suppliers = {
  title: 'Tedarikçiler', icon: '🏭',
  async render() {
    setHeader('Tedarikçiler', 'Tedarikçi kartları ve tedarikçi siparişleri', can('operator') ? html`<button class="btn btn-primary" id="add-sup">+ Tedarikçi ekle</button>` : '');
    const [sups, sos] = await Promise.all([api('/api/suppliers'), api('/api/supplier-orders', { query: { page_size: 25 } })]);
    view().innerHTML = renderVal(html`
      <div class="notice info" style="margin-bottom:16px">Trendyol → Çanta Bayim sipariş otomasyonu ayrı production sunucusunda çalışır ve TrendHub tarafından <b>yönetilmez veya tetiklenmez</b>. Bu ekran tedarikçi kayıtlarını ve (varsa) aktarım kayıtlarını görüntülemek içindir.</div>
      <div class="card"><div class="card-head"><h2>Tedarikçiler</h2></div>
        ${sups.length ? html`<div class="table-wrap"><table><thead><tr><th>Kod</th><th>Ad</th><th>İletişim</th><th>Entegrasyon</th><th class="r">Tedarik süresi</th><th class="r">Sipariş</th><th>Durum</th><th></th></tr></thead><tbody>
        ${sups.map((s) => html`<tr><td><code>${s.code}</code></td><td><b>${s.name}</b></td><td>${s.contact_name || ''} <span class="muted small">${s.phone || ''} ${s.email || ''}</span></td>
          <td>${{ manual: 'Manuel', external: 'Harici sistem', api: 'API' }[s.integration_type] || s.integration_type}</td><td class="r">${s.lead_time_days ?? '—'} gün</td>
          <td class="r num">${num(s.order_count)}${s.error_count ? html` <span class="badge plain tone-bad">${s.error_count} hata</span>` : ''}</td>
          <td>${s.is_active ? html`<span class="badge tone-good">Aktif</span>` : html`<span class="badge">Pasif</span>`}</td>
          <td class="r">${can('operator') ? html`<button class="btn btn-sm" data-sup="${s.id}">Düzenle</button>` : ''}</td></tr>`)}</tbody></table></div>`
          : empty('Tedarikçi yok', 'Tedarikçi ekleyerek ürün-tedarikçi eşleşmesini ve maliyetleri takip edebilirsiniz.')}</div>
      <div class="card mt"><div class="card-head"><h2>Tedarikçi siparişleri</h2></div>
        ${sos.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Tarih</th><th>Sipariş</th><th>Tedarikçi</th><th>Tedarikçi sipariş no</th><th>Durum</th><th class="r">Maliyet</th><th>Hata</th></tr></thead><tbody>
        ${sos.items.map((x) => html`<tr><td>${dateTime(x.created_at)}</td><td>${x.external_order_id || '—'}</td><td>${x.supplier_name || '—'}</td><td>${x.external_supplier_order_id || '—'}</td><td>${x.status || '—'}</td><td class="r num">${money(x.cost)}</td><td class="neg small">${x.last_error || ''}</td></tr>`)}</tbody></table></div>`
          : empty('Kayıt yok', 'TrendHub henüz tedarikçiye sipariş aktarmıyor; aktarım production otomasyonunda yapılıyor.')}</div>`);
    const form = (s) => {
      const body = openModal(html`<h2>${s ? 'Tedarikçiyi düzenle' : 'Yeni tedarikçi'}</h2><form class="form-grid" id="sform">
        <label>Kod<input name="code" required pattern="[a-z0-9_\\-]+" maxlength="50" value="${s?.code || ''}" ${raw(s ? 'readonly' : '')} placeholder="canta_bayim"></label>
        <label>Ad<input name="name" required maxlength="200" value="${s?.name || ''}"></label>
        <label>Yetkili<input name="contact_name" value="${s?.contact_name || ''}"></label><label>Telefon<input name="phone" value="${s?.phone || ''}"></label>
        <label>E-posta<input name="email" type="email" value="${s?.email || ''}"></label><label>Tedarik süresi (gün)<input name="lead_time_days" type="number" min="0" value="${s?.lead_time_days ?? ''}"></label>
        <label>Entegrasyon<select name="integration_type">${[['manual', 'Manuel'], ['external', 'Harici sistem'], ['api', 'API']].map(([v, l]) => html`<option value="${v}" ${raw(s?.integration_type === v ? 'selected' : '')}>${l}</option>`)}</select></label>
        <label class="check"><input type="checkbox" name="is_active" value="1" ${raw(!s || s.is_active ? 'checked' : '')}>Aktif</label>
        <label class="full">Notlar<textarea name="notes" rows="3" maxlength="2000">${s?.notes || ''}</textarea></label>
        <p class="form-error full"></p><div class="full row"><span class="spacer"></span><button class="btn" type="button" data-close>Vazgeç</button><button class="btn btn-primary" type="submit">Kaydet</button></div></form>`);
      $('#sform', body).addEventListener('submit', (e) => {
        e.preventDefault();
        submitting(e.target, async () => {
          const d = formData(e.target);
          const payload = { ...d, is_active: d.is_active === '1', lead_time_days: d.lead_time_days ? Number(d.lead_time_days) : null };
          await api(s ? `/api/suppliers/${s.id}` : '/api/suppliers', { method: s ? 'PUT' : 'POST', body: payload });
          closeLayer('modal'); toast('Tedarikçi kaydedildi'); refresh();
        });
      });
    };
    $('#add-sup')?.addEventListener('click', () => form());
    $$('[data-sup]').forEach((b) => b.addEventListener('click', () => form(sups.find((s) => String(s.id) === b.dataset.sup))));
  },
};

// ---- Finans
const EXPENSE_LABELS = { advertising: 'Reklam', shipping: 'Kargo', packaging: 'Ambalaj', personnel: 'Personel', rent: 'Kira', software: 'Yazılım', other: 'Diğer' };
PAGES.finance = {
  title: 'Finans', icon: '₺',
  async render() {
    const draw = async () => {
      setHeader('Finans', 'Sipariş ve SKU seviyesinde kârlılık', html`${periodSeg(state.period, draw)}${can('operator') ? html`<button class="btn btn-primary" id="add-exp">+ Gider ekle</button>` : ''}`);
      loading();
      const [f, ex] = await Promise.all([api('/api/finance/summary', { query: { period: state.period } }),
        api('/api/finance/expenses', { query: { period: state.period, page_size: 50 } })]);
      const t = f.orders;
      view().innerHTML = renderVal(html`
        ${t.estimated_orders ? html`<div class="notice warn" style="margin-bottom:16px"><b>${num(t.estimated_orders)}</b> siparişte komisyon, kargo, hizmet bedeli veya ürün maliyeti tahmini ya da eksik. Gerçek tutarları sipariş detayından veya ürün maliyetinden girerek netleştirebilirsiniz.</div>` : ''}
        <div class="grid grid-4">
          <div class="card kpi"><div class="label">Ciro</div><div class="value">${money0(t.revenue)}</div><div class="sub">${num(t.orders)} sipariş</div></div>
          <div class="card kpi"><div class="label">Sipariş giderleri</div><div class="value">${money0(t.total_cost)}</div><div class="sub">Maliyet + kesintiler</div></div>
          <div class="card kpi"><div class="label">Dönem giderleri</div><div class="value">${money0(f.expenses.total)}</div><div class="sub">Siparişe bağlı olmayan</div></div>
          <div class="card kpi"><div class="label">Net kâr</div><div class="value ${signClass(f.net_profit_after_expenses)}">${money0(f.net_profit_after_expenses)}</div><div class="sub">Marj ${pct(f.margin_after_expenses)}</div></div>
        </div>
        <div class="grid grid-2 mt">
          <div class="card"><div class="card-head"><div><h2>Kâr / zarar dökümü</h2><p>Ciro − ürün maliyeti − komisyon − hizmet bedeli − kargo − reklam − iade − diğer</p></div></div>${profitBars(t, f.expenses.total)}</div>
          <div class="card"><div class="card-head"><h2>Günlük</h2></div>${lineChart(f.daily, [{ key: 'revenue', label: 'Ciro', color: 'var(--series-1)' }, { key: 'net_profit', label: 'Net kâr', color: 'var(--series-2)' }])}</div>
        </div>
        <div class="card mt"><div class="card-head"><h2>Pazaryerine göre</h2></div><div class="table-wrap"><table><thead><tr><th>Pazaryeri</th><th class="r">Sipariş</th><th class="r">Ciro</th><th class="r">Ürün maliyeti</th><th class="r">Komisyon</th><th class="r">Hizmet</th><th class="r">Kargo</th><th class="r">Reklam</th><th class="r">İade</th><th class="r">Net kâr</th><th class="r">Marj</th></tr></thead><tbody>
          ${f.by_marketplace.map((m) => html`<tr><td><b>${m.name}</b></td><td class="r num">${num(m.orders)}</td><td class="r num">${money(m.revenue)}</td><td class="r num">${money(m.product_cost)}</td><td class="r num">${money(m.commission)}</td><td class="r num">${money(m.service_fee)}</td><td class="r num">${money(m.shipping)}</td><td class="r num">${money(m.advertising)}</td><td class="r num">${money(m.refund)}</td><td class="r num ${signClass(m.net_profit)}">${money(m.net_profit)}</td><td class="r num">${pct(m.margin)}</td></tr>`)}
        </tbody></table></div></div>
        <div class="card mt"><div class="card-head"><div><h2>Dönem giderleri</h2><p>Reklam, ambalaj, personel gibi siparişe bağlı olmayan giderler. SKU girilen reklam giderleri SKU raporuna yansır.</p></div></div>
          ${ex.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Tarih</th><th>Kategori</th><th>Açıklama</th><th>Pazaryeri</th><th>SKU</th><th class="r">Tutar</th><th></th></tr></thead><tbody>
          ${ex.items.map((e) => html`<tr><td>${date(e.expense_date)}</td><td>${e.category_label}</td><td>${e.description || ''}</td><td>${e.marketplace_name || '—'}</td><td>${e.sku || '—'}</td><td class="r num">${money(e.amount)}</td>
            <td class="r">${can('operator') && e.source === 'manual' ? html`<button class="btn btn-sm btn-danger" data-delexp="${e.id}">Sil</button>` : ''}</td></tr>`)}</tbody></table></div>` : empty('Gider yok', 'Bu dönem için gider girilmemiş.')}</div>`);
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

// ---- Raporlar
PAGES.reports = {
  title: 'Raporlar', icon: '📊',
  async render() {
    let sortKey = 'net_profit', dir = -1;
    const draw = async () => {
      const csvHref = `/api/reports/sku.csv?period=${encodeURIComponent(state.period)}`;
      setHeader('Raporlar', 'SKU kârlılığı, iade oranı ve zarar eden siparişler', html`${periodSeg(state.period, draw)}<a class="btn" href="${csvHref}" download>CSV indir</a>`);
      loading();
      const [sku, loss] = await Promise.all([api('/api/reports/sku', { query: { period: state.period } }), api('/api/reports/top-loss', { query: { period: state.period } })]);
      const items = sku.items;
      const table = () => {
        const sorted = [...items].sort((a, b) => ((a[sortKey] ?? -Infinity) > (b[sortKey] ?? -Infinity) ? 1 : -1) * dir);
        const th = (k, l, r = true) => html`<th class="${r ? 'r' : ''}"><a href="#" data-sort="${k}">${l}${sortKey === k ? (dir < 0 ? ' ↓' : ' ↑') : ''}</a></th>`;
        $('#sku-table').innerHTML = renderVal(items.length ? html`<div class="table-wrap"><table><thead><tr>${th('sku', 'SKU', false)}${th('quantity', 'Adet')}${th('revenue', 'Ciro')}${th('product_cost', 'Maliyet')}${th('commission', 'Komisyon')}${th('shipping', 'Kargo')}${th('advertising', 'Reklam')}${th('refund', 'İade')}${th('return_rate', 'İade oranı')}${th('net_profit', 'Net kâr')}${th('margin', 'Marj')}</tr></thead><tbody>
          ${sorted.map((r) => html`<tr><td><b>${r.sku}</b><span class="ellipsis small muted">${r.product_name || ''}</span>${r.missing_cost ? html`<span class="badge plain tone-warn">Maliyet eksik</span>` : ''}</td>
            <td class="r num">${num(r.quantity)}</td><td class="r num">${money(r.revenue)}</td><td class="r num">${money(r.product_cost)}</td><td class="r num">${money(r.commission)}</td><td class="r num">${money(r.shipping)}</td><td class="r num">${money(r.advertising)}</td><td class="r num">${money(r.refund)}</td><td class="r num">${pct(r.return_rate)}</td>
            <td class="r num ${signClass(r.net_profit)}">${money(r.net_profit)}${estimateBadge(r.is_estimate)}</td><td class="r num">${pct(r.margin)}</td></tr>`)}</tbody></table></div>` : empty('Veri yok', 'Seçilen dönemde satılan ürün yok.'));
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
  title: 'Entegrasyonlar', icon: '⚙',
  async render() {
    setHeader('Entegrasyonlar', 'Pazaryeri bağlantıları ve senkronizasyon');
    const d = await api('/api/integrations');
    const tone = { connected: 'tone-good', error: 'tone-bad', not_connected: '', configured: 'tone-warn', not_implemented: 'tone-warn' };
    view().innerHTML = renderVal(html`
      <div class="notice info" style="margin-bottom:16px">API anahtarları panelden girilmez ve veritabanında saklanmaz; sunucudaki <code>.env</code> dosyasında tanımlanır ve servis yeniden başlatılır. Bu ekran yalnızca hangi değişkenin tanımlı olduğunu gösterir. Senkronizasyon her ${d.sync_interval_minutes} dakikada bir otomatik çalışır ve <b>salt okunurdur</b>; pazaryerinde hiçbir değişiklik yapılmaz.</div>
      <div class="grid grid-3">${d.items.map((i) => html`<div class="card integration-card">
        <div class="card-head"><h2>${i.name}</h2><span class="badge ${tone[i.state]}">${i.state_label}</span></div>
        ${i.implementation_note ? html`<p class="notice warn small">${i.implementation_note}</p>` : ''}
        <ul class="cred-list">${i.credentials.map((c) => html`<li><span>${c.label}<br><code>${c.env}</code></span><span class="badge plain ${c.is_set ? 'tone-good' : ''}">${c.is_set ? 'Tanımlı' : 'Eksik'}</span></li>`)}</ul>
        <dl class="kv"><dt>Son test</dt><dd>${dateTime(i.last_check_at)}</dd><dt>Sonuç</dt><dd>${i.last_check_message || '—'}</dd><dt>Son senkron</dt><dd>${dateTime(i.last_sync_at)}</dd>
          <dt>Yazma</dt><dd>${i.write_enabled ? html`<span class="badge tone-warn">Açık</span>` : 'Kapalı (salt okunur)'}</dd></dl>
        ${can('operator') ? html`<div class="row mt"><button class="btn btn-sm" data-check="${i.code}">Bağlantıyı test et</button>
          <button class="btn btn-sm btn-primary" data-sync="${i.code}" ${raw(i.capabilities.includes('orders.read') && i.state !== 'not_connected' ? '' : 'disabled')}>Şimdi senkronize et</button></div>` : ''}
        <h3 class="mt">Son işler</h3>${i.recent_jobs.length ? html`<ul class="timeline">${i.recent_jobs.map((j) => html`<li><b>${j.job_type}</b> · ${jobBadge(j.status)} <span class="muted small">${dateTime(j.created_at)} · deneme ${j.attempts}</span>${j.message ? html`<br><span class="small ${j.status === 'succeeded' ? '' : 'neg'}">${j.message}</span>` : ''}</li>`)}</ul>` : html`<p class="muted small">Henüz iş yok.</p>`}
      </div>`)}</div>`);
    $$('[data-check]').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true;
      try { const r = await api(`/api/integrations/${b.dataset.check}/check`, { method: 'POST' }); toast(r.message, !r.ok); refresh(); } catch (e) { fail(e); } finally { b.disabled = false; }
    }));
    $$('[data-sync]').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true;
      try { const r = await api(`/api/integrations/${b.dataset.sync}/sync`, { method: 'POST', body: {} }); toast(r.message); refresh(); } catch (e) { fail(e); b.disabled = false; }
    }));
  },
};
const JOB_LABELS = { queued: ['Kuyrukta', 'tone-info'], running: ['Çalışıyor', 'tone-warn'], succeeded: ['Başarılı', 'tone-good'], failed: ['Başarısız', 'tone-bad'], dead: ['Deneme bitti', 'tone-bad'] };
const jobBadge = (s) => { const [l, t] = JOB_LABELS[s] || [s || '—', '']; return html`<span class="badge ${t}">${l}</span>`; };

// ---- Sistem / Hatalar
PAGES.system = {
  title: 'Sistem / Hatalar', icon: '⚠', nav: 'Sistem / Hatalar',
  async render() {
    setHeader('Sistem / Hatalar', 'Sağlık durumu, iş kuyruğu, hatalar ve denetim kaydı');
    const [h, jobs, events] = await Promise.all([api('/api/system/health'), api('/api/system/jobs', { query: { page_size: 20 } }), api('/api/system/events', { query: { page_size: 50 } })]);
    const audit = can('admin') ? await api('/api/system/audit', { query: { page_size: 30 } }) : null;
    view().innerHTML = renderVal(html`
      <div class="grid grid-4">
        <div class="card kpi"><div class="label">Genel durum</div><div class="value">${h.status === 'healthy' ? html`<span class="pos">Sağlıklı</span>` : html`<span class="neg">Dikkat</span>`}</div><div class="sub">Şema: <code>${h.database.migration || 'bilinmiyor'}</code></div></div>
        <div class="card kpi"><div class="label">Worker</div><div class="value ${h.worker_alive ? 'pos' : 'neg'}">${h.worker_alive ? 'Çalışıyor' : 'Yok'}</div><div class="sub">${h.workers[0] ? 'Son sinyal ' + dateTime(h.workers[0].last_seen_at) : 'Hiç sinyal alınmadı'}</div></div>
        <div class="card kpi"><div class="label">Kuyruk</div><div class="value">${num(h.queue.queued)} <span class="small muted">bekliyor</span></div><div class="sub">${num(h.queue.running)} çalışıyor · ${num(h.queue.succeeded_24h)} başarılı (24s)</div></div>
        <div class="card kpi"><div class="label">Açık hata</div><div class="value ${h.open_events.errors ? 'neg' : ''}">${num(h.open_events.errors)}</div><div class="sub">${num(h.open_events.warnings)} uyarı · ${num(h.queue.failed_24h)} başarısız iş (24s)</div></div>
      </div>
      <div class="card mt"><div class="card-head"><h2>Hatalar ve uyarılar</h2></div>
        ${events.items.length ? html`<div class="table-wrap"><table><thead><tr><th>Son</th><th>Seviye</th><th>Kaynak</th><th>Mesaj</th><th class="r">Tekrar</th><th></th></tr></thead><tbody>
        ${events.items.map((e) => html`<tr><td>${dateTime(e.last_occurred_at)}</td><td><span class="badge ${e.level === 'warning' ? 'tone-warn' : e.level === 'info' ? 'tone-info' : 'tone-bad'}">${{ info: 'Bilgi', warning: 'Uyarı', error: 'Hata', critical: 'Kritik' }[e.level]}</span></td>
          <td><code>${e.source}</code></td><td>${e.message}</td><td class="r num">${num(e.occurrences)}</td><td class="r">${can('operator') ? html`<button class="btn btn-sm" data-resolve="${e.id}">Çözüldü</button>` : ''}</td></tr>`)}</tbody></table></div>` : empty('Açık hata yok', 'Sistem olayları burada listelenir.')}</div>
      <div class="card mt"><div class="card-head"><h2>Senkronizasyon işleri</h2></div>
        ${jobs.items.length ? html`<div class="table-wrap"><table><thead><tr><th>#</th><th>Tür</th><th>Pazaryeri</th><th>Durum</th><th class="r">Deneme</th><th>Oluşturma</th><th>Bitiş</th><th>Mesaj</th><th></th></tr></thead><tbody>
        ${jobs.items.map((j) => html`<tr><td>${j.id}</td><td>${j.job_type || '—'}</td><td>${j.marketplace || '—'}</td><td>${jobBadge(j.status)}</td><td class="r">${j.attempts ?? 0}/${j.max_attempts ?? '—'}</td>
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
  title: 'Ayarlar', icon: '☰',
  async render() {
    setHeader('Ayarlar', 'Finans varsayılanları, kullanıcılar ve hesap');
    const s = await api('/api/settings');
    const users = can('admin') ? await api('/api/users') : null;
    const input = (it) => {
      const dis = can('admin') ? '' : 'disabled';
      if (it.type === 'bool') return html`<label class="check"><input type="checkbox" name="${it.key}" ${raw(it.value ? 'checked' : '')} ${raw(dis)}>${it.label}</label>`;
      if (it.type === 'rate') return html`<label>${it.label} (%)<input name="${it.key}" type="number" step="0.1" min="0" max="100" value="${it.value === null ? '' : +(Number(it.value) * 100).toFixed(2)}" ${raw(dis)}></label>`;
      return html`<label>${it.label}<input name="${it.key}" type="number" step="${it.type === 'int' ? 1 : 0.01}" min="0" value="${it.value ?? ''}" ${raw(dis)}></label>`;
    };
    view().innerHTML = renderVal(html`
      <div class="grid grid-2">
        <form class="card" id="set-form"><div class="card-head"><div><h2>Finans ve stok varsayılanları</h2><p>Pazaryeri gerçek tutarı bildirmediğinde kullanılan tahminler. Tahmini tutarlar raporlarda “Tahmini” olarak işaretlenir.</p></div></div>
          <div class="form-grid">${s.items.map((it) => html`<div class="${it.type === 'bool' ? 'full' : ''}">${input(it)}</div>`)}</div>
          ${can('admin') ? html`<p class="form-error"></p><button class="btn btn-primary mt" type="submit">Kaydet</button>` : html`<p class="muted small mt">Ayarları yalnızca yöneticiler değiştirebilir.</p>`}</form>
        <form class="card" id="pw-form"><div class="card-head"><h2>Parola değiştir</h2></div><div class="stack">
          <label>Mevcut parola<input type="password" name="current_password" autocomplete="current-password" required></label>
          <label>Yeni parola (en az 12 karakter)<input type="password" name="new_password" autocomplete="new-password" minlength="12" required></label>
          <p class="form-error"></p><button class="btn btn-primary" type="submit">Parolayı değiştir</button><p class="small muted">Diğer cihazlardaki oturumlar kapatılır.</p></div></form>
      </div>
      ${users ? html`<div class="card mt"><div class="card-head"><h2>Kullanıcılar</h2><button class="btn btn-sm btn-primary" id="add-user">+ Kullanıcı</button></div>
        <div class="table-wrap"><table><thead><tr><th>Kullanıcı</th><th>Ad</th><th>Rol</th><th>Durum</th><th>Son giriş</th><th></th></tr></thead><tbody>
        ${users.map((u) => html`<tr><td><b>${u.username}</b></td><td>${u.full_name || ''}</td><td>${ROLE_LABELS[u.role]}</td>
          <td>${u.is_active ? (u.locked ? html`<span class="badge tone-warn">Kilitli</span>` : html`<span class="badge tone-good">Aktif</span>`) : html`<span class="badge">Pasif</span>`}</td><td>${dateTime(u.last_login_at)}</td>
          <td class="r">${u.id !== state.user.id ? html`<button class="btn btn-sm" data-user="${u.id}">Düzenle</button>` : html`<span class="muted small">Siz</span>`}</td></tr>`)}</tbody></table></div>
        <p class="small muted">Roller: <b>İzleyici</b> yalnızca görüntüler · <b>Operatör</b> sipariş durumu, maliyet, gider ve senkronizasyon işlemleri yapar · <b>Yönetici</b> ayarları ve kullanıcıları yönetir.</p></div>` : ''}`);
    $('#set-form').addEventListener('submit', (e) => {
      e.preventDefault();
      submitting(e.target, async () => {
        const values = {};
        s.items.forEach((it) => {
          const el = e.target.elements[it.key];
          if (it.type === 'bool') values[it.key] = el.checked;
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
    const userForm = (u) => {
      const body = openModal(html`<h2>${u ? `Kullanıcı · ${u.username}` : 'Yeni kullanıcı'}</h2><form class="stack" id="uform">
        ${u ? '' : html`<label>Kullanıcı adı<input name="username" required minlength="3" maxlength="50" pattern="[A-Za-z0-9_.\\-]+"></label>`}
        <label>Ad soyad<input name="full_name" maxlength="100" value="${u?.full_name || ''}"></label>
        <label>Rol<select name="role">${Object.entries(ROLE_LABELS).map(([v, l]) => html`<option value="${v}" ${raw(u?.role === v ? 'selected' : '')}>${l}</option>`)}</select></label>
        ${u ? html`<label class="check"><input type="checkbox" name="is_active" value="1" ${raw(u.is_active ? 'checked' : '')}>Aktif</label>` : ''}
        <label>${u ? 'Yeni parola (boş bırakılırsa değişmez)' : 'Parola (en az 12 karakter)'}<input type="password" name="password" autocomplete="new-password" ${raw(u ? '' : 'required minlength="12"')}></label>
        <p class="form-error"></p><button class="btn btn-primary" type="submit">Kaydet</button></form>`);
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
    $$('[data-user]').forEach((b) => b.addEventListener('click', () => userForm(users.find((u) => String(u.id) === b.dataset.user))));
  },
};

// ------------------------------------------------------------------- yönlendirme
const NAV = ['dashboard', 'orders', 'products', 'shipping', 'suppliers', 'finance', 'reports', 'integrations', 'system', 'settings'];
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [path, qs] = h.split('?');
  return { page: PAGES[path] ? path : 'dashboard', params: new URLSearchParams(qs || '') };
}
function renderNav(active) {
  $('#nav').innerHTML = renderVal(NAV.map((k) => html`<a href="#/${k}" class="${k === active ? 'active' : ''}" ${raw(k === active ? 'aria-current="page"' : '')}><span class="ico" aria-hidden="true">${PAGES[k].icon}</span>${PAGES[k].nav || PAGES[k].title}</a>`));
}
let routeSeq = 0;
async function route() {
  if (!state.user) return;
  const { page, params } = parseHash();
  const seq = ++routeSeq;
  renderNav(page);
  document.body.classList.remove('nav-open');
  if (!$('#drawer').hidden) closeLayer('drawer');
  try {
    await PAGES[page].render(params);
  } catch (e) {
    if (seq !== routeSeq || e.status === 401) return;
    view().innerHTML = renderVal(html`<div class="notice bad">Sayfa yüklenemedi: ${e.message}</div>`);
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

(async () => {
  try { await startApp(await api('/api/auth/me')); } catch (e) { if (e.status !== 401) { showLogin(); $('#login-error').textContent = e.message; } }
})();
