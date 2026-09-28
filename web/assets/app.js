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
      const [d, al] = await Promise.all([api('/api/dashboard', { query: { period: state.period } }),
        api('/api/alerts', { query: { status: 'open', page_size: 5 } }).catch(() => null)]);
      const s = d.summary, t = s.orders;
      const connected = d.integrations.filter((i) => i.state === 'connected').length;
      const hasMarketplaceOrders = d.by_marketplace.some((m) => Number(m.orders));
      view().innerHTML = renderVal(html`
        <div class="chips" style="margin-bottom:16px">${d.integrations.map((i) => html`
          <a class="chip" href="#/integrations"><span class="dot ${i.state === 'connected' ? 'good' : i.state === 'error' ? 'bad' : i.state === 'not_connected' ? '' : 'warn'}"></span>${i.name}
          <span class="muted small">${i.state_label}</span></a>`)}</div>
        ${al && al.total ? html`<a class="attention-box ${al.items.some((a) => a.severity === 'critical') ? 'bad' : 'warn'}" href="#/alerts">
          <span class="big num">${num(al.total)}</span><span><b>konu ilgilenmeni bekliyor</b>
          <span class="small">${al.items.slice(0, 3).map((a) => `${SEV_ICON[a.severity]} ${a.title}: ${a.description || ''}`).join(' · ')}</span></span><span class="go">Uyarılar →</span></a>` : ''}
        ${connected === 0 ? html`<div class="notice info" style="margin-bottom:16px">Henüz bağlı bir pazaryeri yok. Sipariş verisi, <a href="#/integrations">Entegrasyonlar</a> sayfasındaki API bilgileri sunucuya tanımlanıp senkronizasyon çalıştığında görünecek. Aşağıdaki değerler gerçek kayıtlardan hesaplanır; veri yoksa 0 gösterilir.</div>` : ''}
        <div class="grid grid-4">
          ${kpi('Bugünkü satış', money0(d.today.revenue), `${num(d.today.orders)} sipariş`)}
          ${kpi('Toplam ciro', money0(t.revenue), `${date(d.range.from)} – ${date(d.range.to)} · iptaller hariç`)}
          ${kpi('Tahmini net kâr', money0(s.net_profit_after_expenses), `Marj ${pct(s.margin_after_expenses)} · KDV sonrası ${money0(s.net_profit_after_tax)}`, signClass(s.net_profit_after_expenses), TAHMINI)}
          ${kpi('Sipariş sayısı', num(t.orders), 'Seçili dönem, iptaller hariç')}
        </div>
        <div class="grid grid-4 mt">
          ${kpi('Bekleyen sipariş', num(d.pending_orders), 'Yeni · hazırlanıyor · tedarikçide · kargo bekliyor', d.pending_orders ? 'warn-text' : '')}
          ${kpi('İade', num(d.returns.period), `Dönem içi · toplam ${num(d.returns.open_total)}`, d.returns.period ? 'neg' : '')}
          ${kpi('Ortalama sipariş tutarı', t.average_order_value === null ? '—' : money(t.average_order_value), 'Ciro / sipariş')}
          ${kpi('Tahmini KDV', money0(s.tax_estimate), 'Satış KDV − maliyet KDV', '', TAHMINI)}
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
            ${d.recent_orders.length ? html`<div class="table-wrap"><table><thead><tr><th>Sipariş</th><th>Pazaryeri</th><th>Tarih</th><th>Durum</th><th class="r">Ciro</th><th class="r">Net kâr</th></tr></thead><tbody>
            ${d.recent_orders.map((o) => html`<tr class="click" data-order="${o.id}"><td>${o.external_order_id}</td><td>${o.marketplace_name || '—'}</td><td>${dateTime(o.order_date)}</td><td>${statusBadge(o.internal_status, o.status_label)}</td><td class="r num">${money(o.gross_revenue)}</td><td class="r num ${signClass(o.net_profit)}">${money(o.net_profit)}</td></tr>`)}
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
        <th>Sipariş</th><th>Pazaryeri</th><th>Tarih</th><th>Müşteri</th><th>Durum</th><th>Kargo Planı</th><th class="r">Adet</th><th class="r">Ciro</th><th class="r">Net kâr</th><th class="r">Marj</th></tr></thead><tbody>
        ${d.items.map((o) => html`<tr class="click" data-order="${o.id}"><td><b>${o.external_order_id}</b>${o.review_reason ? html`<span class="ellipsis small neg" title="${o.review_reason}">${o.review_reason}</span>` : ''}</td>
          <td>${o.marketplace_name || '—'}</td><td>${dateTime(o.order_date)}</td><td><span class="ellipsis">${o.customer_name || '—'}</span><span class="muted small">${o.customer_city || ''}</span></td>
          <td>${statusBadge(o.internal_status, o.status_label)}</td><td>${shipPlanBadge(o.shipping_plan)}</td><td class="r num">${num(o.item_count)}</td><td class="r num">${money(o.gross_revenue)}</td>
          <td class="r num ${signClass(o.net_profit)}">${money(o.net_profit)}${estimateBadge(o.finance_is_estimate)}</td><td class="r num">${pct(o.margin)}</td></tr>`)}
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
    ${shipPlanCard(o.shipping_plan)}
    <div class="grid grid-2">
      <div class="card"><h3>Bilgiler</h3><dl class="kv mt">
        <dt>Sipariş tarihi</dt><dd>${dateTime(o.order_date)}</dd><dt>Müşteri</dt><dd>${o.customer_name || '—'} ${o.customer_city ? `(${o.customer_city})` : ''}</dd>
        <dt>Mağaza</dt><dd>${o.store_name || '—'}</dd><dt>Son senkron</dt><dd>${dateTime(o.last_synced_at)}</dd><dt>Kaynak</dt><dd>${o.source || '—'}</dd></dl></div>
      <div class="card"><h3>Kârlılık ${TAHMINI}</h3><div class="mt">${profitBars({ ...t, discount: o.discount }, { net: o.net_profit, tax: o.tax_estimate, netAfterTax: o.net_profit_after_tax })}</div>
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
const productTabs = (active) => html`<div class="seg" role="tablist" style="margin-bottom:16px">
  <button role="tab" class="${active === 'catalog' ? 'on' : ''}" aria-selected="${active === 'catalog'}" data-ptab="#/products">Ürün kataloğu</button>
  <button role="tab" class="${active === 'listings' ? 'on' : ''}" aria-selected="${active === 'listings'}" data-ptab="#/products?tab=listings">Pazaryeri ilanları</button></div>`;
const bindProductTabs = () => $$('[data-ptab]').forEach((b) => b.addEventListener('click', () => { location.hash = b.dataset.ptab; }));

PAGES.products = {
  title: 'Ürün & Stok', icon: 'products',
  async render(params) {
    if (params.get('tab') === 'listings') return renderListings(params);
    const f = { q: params.get('q') || '', low_stock: params.get('low_stock') === '1', missing_cost: params.get('missing_cost') === '1' };
    setHeader('Ürün & Stok', 'Ürün kataloğu, maliyet geçmişi ve stok', can('operator') ? html`<button class="btn btn-primary" id="add-product">+ Ürün ekle</button>` : '');
    view().innerHTML = renderVal(html`${productTabs('catalog')}<div class="grid grid-4" id="prod-summary"></div><div class="card mt">
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
          <td class="r nowrap-cell"><button class="btn btn-sm" data-offers="${p.id}">Tedarikçiler</button> ${can('operator') ? html`<button class="btn btn-sm" data-cost="${p.id}">Maliyet</button> <button class="btn btn-sm" data-edit="${p.id}">Düzenle</button>` : html`<button class="btn btn-sm" data-cost="${p.id}">Geçmiş</button>`}</td></tr>`)}
        </tbody></table></div>${pager(d, load)}` : empty('Ürün yok', can('operator') ? 'Ürün ekleyerek maliyet takibine başlayın. Sipariş kalemleri SKU/barkod ile otomatik eşlenir.' : 'Henüz ürün tanımlanmamış.'));
      $$('[data-cost]').forEach((b) => b.addEventListener('click', () => costForm(d.items.find((p) => String(p.id) === b.dataset.cost))));
      $$('[data-edit]').forEach((b) => b.addEventListener('click', () => productForm(d.items.find((p) => String(p.id) === b.dataset.edit))));
      $$('[data-offers]').forEach((b) => b.addEventListener('click', () => showOffers(Number(b.dataset.offers))));
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
    ${meta.secret_storage_ready ? '' : html`<div class="notice warn" style="margin-bottom:12px">Sunucuda <code>APP_SECRET</code> tanımlı değil. Kaynak adresi ve şifreler şifreli saklandığı için önce sunucu .env dosyasında APP_SECRET belirlenmeli (deploy betiği boşsa otomatik üretir).</div>`}
    <form class="form-grid" id="supform" autocomplete="off">
      ${s ? '' : html`<label class="full">Şablon<select name="preset"><option value="">Boş tedarikçi</option>${meta.presets.map((p) => html`<option value="${p.key}">${p.name} (${INTEGRATION_LABELS[p.integration_type]})</option>`)}</select>
        <span class="small muted" id="preset-note">Alan eşleştirmesi kaydettikten sonra kaynağı önizleyerek yapılır.</span></label>`}
      <label>Tedarikçi adı<input name="name" required maxlength="200" value="${s?.name || ''}"></label>
      <label>Kod<input name="code" maxlength="50" pattern="[a-z0-9_\\-]*" value="${s?.code || ''}" ${raw(s ? 'readonly' : '')} placeholder="otomatik"></label>
      <label>Entegrasyon türü<select name="integration_type">${opt(Object.entries(meta.integration_types), c.integration_type || 'xml')}</select>
        <span class="small muted" id="conn-desc"></span></label>
      <label>Senkronizasyon sıklığı<select name="sync_interval_minutes">${opt(INTERVALS, s?.sync_interval_minutes ?? 60)}</select></label>
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
          <label data-auth="basic bearer header query">veya ortam değişkeni<input name="secret_env" maxlength="80" pattern="SUPPLIER_[A-Z0-9_]+" value="${c.secret_env || ''}" placeholder="SUPPLIER_ACME_TOKEN"></label>
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
        connection, preset: d.preset || null,
      };
      if (s) {
        await api(`/api/suppliers/${s.id}`, { method: 'PUT', body: payload });
        closeLayer('drawer'); toast('Tedarikçi kaydedildi'); refresh();
      } else {
        const r = await api('/api/suppliers', { method: 'POST', body: payload });
        closeLayer('drawer'); toast('Tedarikçi eklendi — şimdi alan eşleştirmesini yapın');
        location.hash = `#/suppliers?id=${r.id}&tab=mapping`;
      }
    });
  });
}

const SUP_TABS = [['overview', 'Genel'], ['mapping', 'Alan eşleştirme'], ['products', 'Ürünler'], ['runs', 'Senkron geçmişi'], ['changes', 'Değişiklikler']];
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
    ${!Number(s.mapped_fields) ? html`<div class="notice warn" style="margin-bottom:12px">Alan eşleştirmesi yapılmadı. <b>Alan eşleştirme</b> sekmesinde kaynağı önizleyip en az “Tedarikçi ürün kodu” ve “Ürün adı” alanlarını eşleştirin.</div>` : ''}
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
    ${runs.map((r) => html`<tr><td>${dateTime(r.started_at)}</td><td>${{ schedule: 'Zamanlanmış', manual: 'Elle', upload: 'Dosya' }[r.trigger] || r.trigger}</td><td>${runBadge(r.status)}</td>
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

// ---- Ürün havuzu (tedarikçi ürünleri) — Tedarikçi detayında ve Ürün Aktarımı 1. adımında kullanılır
async function renderPool(box, { supplierId = null, embedded = false, selectable = false, onSelect } = {}) {
  const sups = embedded ? [] : await api('/api/suppliers');
  const f = { supplier_id: supplierId || '', q: '', status: 'active', in_catalog: '', in_stock: false, multi_supplier: false };
  const sel = state.transfer.selected;
  const load = async (page) => {
    const d = await api('/api/supplier-products', { query: { ...f, in_stock: f.in_stock ? 'true' : '', multi_supplier: f.multi_supplier ? 'true' : '', page, page_size: 50 } });
    box.innerHTML = renderVal(html`<div class="card">
      <form class="filters" id="pool-f">
        <input type="search" name="q" placeholder="SKU, barkod, model veya ürün adı" value="${f.q}" aria-label="Ara">
        ${embedded ? '' : html`<select name="supplier_id" aria-label="Tedarikçi"><option value="">Tüm tedarikçiler</option>${sups.map((s) => html`<option value="${s.id}" ${raw(String(s.id) === String(f.supplier_id) ? 'selected' : '')}>${s.name}</option>`)}</select>`}
        <select name="status" aria-label="Durum">${[['active', 'Aktif'], ['missing', 'Kaynağında bulunamadı'], ['', 'Tümü']].map(([v, l]) => html`<option value="${v}" ${raw(v === f.status ? 'selected' : '')}>${l}</option>`)}</select>
        <select name="in_catalog" aria-label="Katalog">${[['', 'Katalog: tümü'], ['no', 'Kataloğa alınmamış'], ['yes', 'Katalogda']].map(([v, l]) => html`<option value="${v}" ${raw(v === f.in_catalog ? 'selected' : '')}>${l}</option>`)}</select>
        <label class="check"><input type="checkbox" name="in_stock" value="1" ${raw(f.in_stock ? 'checked' : '')}>Stokta</label>
        <label class="check"><input type="checkbox" name="multi_supplier" value="1" ${raw(f.multi_supplier ? 'checked' : '')}>Birden çok tedarikçide</label>
        <button class="btn btn-primary" type="submit">Filtrele</button></form>
      <p class="small muted">${num(d.summary.total)} tedarikçi ürünü · ${num(d.summary.missing)} kaynağında bulunamadı · ${num(d.summary.not_in_catalog)} kataloğa alınmamış · ${num(d.summary.catalog_products)} katalog ürünü</p>
      ${d.items.length ? html`<div class="table-wrap"><table><thead><tr>
        ${selectable ? html`<th><input type="checkbox" id="pool-all" aria-label="Sayfadakilerin tümünü seç"></th>` : ''}
        ${embedded ? '' : html`<th>Tedarikçi</th>`}<th>SKU / Barkod</th><th>Ürün</th><th>Marka / Kategori</th><th class="r">Alış</th><th class="r">Stok</th><th>Durum</th><th>Katalog</th></tr></thead><tbody>
        ${d.items.map((x) => html`<tr>
          ${selectable ? html`<td><input type="checkbox" data-pick="${x.id}" ${raw(sel.has(x.id) ? 'checked' : '')} aria-label="Seç: ${x.name || x.supplier_sku}"></td>` : ''}
          ${embedded ? '' : html`<td>${x.supplier_name}</td>`}
          <td><b>${x.supplier_sku}</b><span class="muted small">${x.barcode || 'barkod yok'}</span></td>
          <td><span class="ellipsis" title="${x.name || ''}">${x.name || '—'}</span><span class="muted small">${x.image_count ? `${x.image_count} görsel` : 'görsel yok'}</span></td>
          <td><span class="ellipsis">${x.brand || '—'}</span><span class="muted small ellipsis">${x.category || ''}</span></td>
          <td class="r num">${x.cost ? money(x.cost) : '—'}</td><td class="r num ${Number(x.stock) > 0 ? '' : 'warn-text'}">${num(x.stock)}</td>
          <td><span class="badge ${x.status === 'missing' ? 'tone-bad' : 'tone-good'}">${x.status_label}</span></td>
          <td>${x.product_id ? html`<button class="btn btn-sm" data-offers="${x.product_id}">${Number(x.offer_count) > 1 ? `${x.offer_count} teklif` : 'Katalogda'}</button>` : html`<span class="muted small">Havuzda</span>`}
            ${(x.draft_marketplaces || []).map((m) => html` <span class="badge plain">${m}</span>`)}</td></tr>`)}
        </tbody></table></div>${pager(d, load)}` : empty('Ürün yok', embedded ? 'Bu tedarikçiden henüz ürün okunmadı. Alan eşleştirmesini yapıp senkronize edin veya dosya yükleyin.' : 'Filtreye uyan tedarikçi ürünü yok.')}</div>`);
    $('#pool-f', box).addEventListener('submit', (e) => {
      e.preventDefault();
      const v = formData(e.target);
      Object.assign(f, { q: v.q || '', status: v.status || '', in_catalog: v.in_catalog || '', in_stock: v.in_stock === '1', multi_supplier: v.multi_supplier === '1' });
      if (!embedded) f.supplier_id = v.supplier_id || '';
      load(1);
    });
    $$('[data-offers]', box).forEach((b) => b.addEventListener('click', () => showOffers(Number(b.dataset.offers))));
    if (selectable) {
      $$('[data-pick]', box).forEach((cb) => cb.addEventListener('change', () => { const i = Number(cb.dataset.pick); if (cb.checked) sel.add(i); else sel.delete(i); onSelect?.(); }));
      $('#pool-all', box)?.addEventListener('change', (e) => { $$('[data-pick]', box).forEach((cb) => { cb.checked = e.target.checked; cb.dispatchEvent(new Event('change')); }); });
    }
  };
  await load(1);
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
const TRANSFER_STEPS = [['pool', '1. Ürünleri seç'], ['marketplaces', '2. Pazaryeri'], ['pricing', '3. Fiyat & doğrulama'], ['ready', '4. Yayına hazır']];
const DRAFT_TONE = { draft: 'tone-info', invalid: 'tone-bad', ready: 'tone-good', cancelled: '' };
PAGES.transfer = {
  title: 'Ürün Aktarımı', icon: 'transfer', nav: 'Ürün Aktarımı',
  async render(params) {
    const step = params.get('step') || 'pool';
    const tab = params.get('tab') || 'wizard';
    setHeader('Ürün Aktarımı', 'Tedarikçi ürünlerini seçip pazaryerleri için fiyatlandırın ve doğrulayın');
    view().innerHTML = renderVal(html`
      <div class="notice info" style="margin-bottom:12px">Pazaryerine otomatik gönderim <b>kapalıdır</b> (salt okunur mod). “Yayına hazır” taslaklar TrendHub'da tutulur ve CSV olarak indirilebilir; canlı Trendyol / Hepsiburada / Amazon hesaplarında hiçbir şey değişmez.</div>
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
          <span class="small muted">Komisyon ${pct(r.effective_commission_rate)} · kâr oranı ${pct(r.markup_rate)} · min. marj ${pct(r.min_margin_rate)}</span>
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
          ${v.ready ? html`<a class="btn btn-sm" href="/api/listing-drafts/export.csv?marketplace=${encodeURIComponent(code)}" download>CSV indir</a>` : ''}</div>`)}</div>
        <p class="small muted mt">Pazaryerine otomatik yükleme kapalıdır. CSV dosyasını pazaryerinin satıcı panelinden toplu ürün yükleme ile kullanabilirsiniz; hatalı taslakları “Tüm taslaklar” sekmesinden düzeltebilirsiniz.</p>
        <div class="row mt"><a class="btn" href="#/transfer?step=pricing">← Fiyat & doğrulama</a><span class="spacer"></span><button class="btn btn-primary" id="tr-new">Yeni aktarım</button></div></div>`);
      $('#tr-new', box).addEventListener('click', () => { T.selected.clear(); T.draftIds = []; T.productIds = []; location.hash = '#/transfer'; });
    }
  },
};

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
        <button class="btn" id="dr-validate">Yeniden doğrula</button><button class="btn btn-primary" id="dr-prepare">${wizard ? 'Yayına hazırla →' : 'Seçilenleri yayına hazırla'}</button></div>` : ''}</div>`);
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
      <tr><td><b>Tahmini kâr</b></td><td class="r num ${signClass(pr.profit)}"><b>${money(pr.profit)}</b> <span class="muted small">marj ${pct(pr.margin)} · min. ${pct(pr.min_margin_rate)}</span></td></tr>
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
        <label>Kâr oranı<input name="markup_rate" type="number" step="0.01" min="0" value="${r.markup_rate}"></label>
        <label>Kargo (₺)<input name="shipping_cost" type="number" step="0.01" min="0" value="${r.shipping_cost}"></label>
        <label>Sabit gider (₺)<input name="fixed_cost" type="number" step="0.01" min="0" value="${r.fixed_cost}"></label>
        <label>Minimum marj<input name="min_margin_rate" type="number" step="0.01" min="-1" max="1" value="${r.min_margin_rate}"></label>
        <label>Yuvarlama<select name="rounding">${[['x.90', 'x,90'], ['x.99', 'x,99'], ['integer', 'Tam sayı'], ['none', 'Yok']].map(([v, l]) => html`<option value="${v}" ${raw(v === r.rounding ? 'selected' : '')}>${l}</option>`)}</select></label>
        <label>Stok güvenlik payı<input name="stock_buffer" type="number" min="0" value="${r.stock_buffer}"></label>
        <label>Minimum stok<input name="min_stock" type="number" min="0" value="${r.min_stock}"></label>
        <label>Maksimum stok<input name="max_stock" type="number" min="0" value="${r.max_stock ?? ''}" placeholder="sınırsız"></label>
        <label>Başlık uzunluğu sınırı<input name="title_max_length" type="number" min="10" value="${r.title_max_length ?? ''}" placeholder="yok"></label>
        <fieldset class="full fieldset"><legend>Zorunlu alanlar</legend><div class="row">${rules.requirable_fields.map((fld) => html`<label class="check"><input type="checkbox" name="rf" value="${fld}" ${raw((r.required_fields || []).includes(fld) ? 'checked' : '')}>${{ barcode: 'Barkod', brand: 'Marka', category: 'Kategori', images: 'Görsel', description: 'Açıklama', model_code: 'Model kodu', desi: 'Desi', vat_rate: 'KDV' }[fld] || fld}</label>`)}</div></fieldset>
      </div>
      ${can('admin') ? html`<p class="form-error"></p><div class="row mt"><span class="spacer"></span><button class="btn btn-primary btn-sm" type="submit">Kaydet</button></div>` : ''}</form>`)}</div>
    <p class="small muted">Fiyat = (maliyet × (1 + kâr oranı) + kargo + sabit gider) ÷ (1 − komisyon), sonra yuvarlanır. Değerler varsayılandır; pazaryerinin güncel komisyon ve zorunlu alan kurallarına göre düzenleyin. Tahmini kâr TAHMİNİDİR.</p>
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
      setHeader('Finans', 'Sipariş ve SKU seviyesinde kârlılık', html`${periodSeg(state.period, draw)}${can('operator') ? html`<button class="btn btn-primary" id="add-exp">+ Gider ekle</button>` : ''}`);
      loading();
      const [f, ex] = await Promise.all([api('/api/finance/summary', { query: { period: state.period } }),
        api('/api/finance/expenses', { query: { period: state.period, page_size: 50 } })]);
      const t = f.orders;
      view().innerHTML = renderVal(html`
        <div class="notice warn" style="margin-bottom:16px"><b>TAHMİNİ:</b> Pazaryeri hakediş (settlement) verisi henüz bağlı değil. Komisyon ve hizmet bedeli Ayarlar'daki oranlarla, KDV satış − maliyet KDV'si olarak tahmin edilir. Gerçek tutarları sipariş detayından “Gerçek gider gir” ile ekleyebilirsiniz.${t.estimated_orders ? html` Bu dönemde <b>${num(t.estimated_orders)}</b> siparişte tahmini veya eksik değer var.` : ''}</div>
        <div class="grid grid-4">
          ${kpi('Ciro', money0(t.revenue), `${num(t.orders)} sipariş · ort. ${t.average_order_value === null ? '—' : money(t.average_order_value)}`)}
          ${kpi('Sipariş giderleri', money0(t.total_cost), 'Maliyet + komisyon + kargo + hizmet + reklam + iade + diğer', '', TAHMINI)}
          ${kpi('Dönem giderleri', money0(f.expenses.total), 'Siparişe bağlı olmayan')}
          ${kpi('Net kâr', money0(f.net_profit_after_expenses), `Marj ${pct(f.margin_after_expenses)}`, signClass(f.net_profit_after_expenses), TAHMINI)}
        </div>
        <div class="grid grid-4 mt">
          ${kpi('Tahmini KDV', money0(f.tax_estimate), 'Satış KDV − maliyet KDV (KDV dahil tutarlardan)', '', TAHMINI)}
          ${kpi('Net kâr (KDV sonrası)', money0(f.net_profit_after_tax), `Marj ${pct(f.margin_after_tax)}`, signClass(f.net_profit_after_tax), TAHMINI)}
          ${kpi('Satıcı indirimi', money0(t.discount), 'Ciroya yansımış; ayrıca düşülmez')}
          ${kpi('İade tutarı', money0(t.refund), 'İade edilen siparişlerin cirosu')}
        </div>
        <div class="grid grid-2 mt">
          <div class="card"><div class="card-head"><div><h2>Kâr / zarar dökümü ${TAHMINI}</h2><p>Ciro − ürün maliyeti − komisyon − hizmet bedeli − kargo − reklam − iade − diğer</p></div></div>${profitBars(t, { periodExpenses: f.expenses.total, net: f.net_profit_after_expenses, tax: f.tax_estimate, netAfterTax: f.net_profit_after_tax })}</div>
          <div class="card"><div class="card-head"><h2>Günlük</h2></div>${lineChart(f.daily, [{ key: 'revenue', label: 'Ciro', color: 'var(--series-1)' }, { key: 'net_profit', label: 'Net kâr', color: 'var(--series-2)' }])}</div>
        </div>
        <div class="card mt"><div class="card-head"><h2>Pazaryerine göre</h2></div><div class="table-wrap"><table><thead><tr><th>Pazaryeri</th><th class="r">Sipariş</th><th class="r">Ciro</th><th class="r">Ürün maliyeti</th><th class="r">Komisyon</th><th class="r">Hizmet</th><th class="r">Kargo</th><th class="r">Reklam</th><th class="r">İade</th><th class="r">Net kâr</th><th class="r">Tahmini KDV</th><th class="r">Marj</th></tr></thead><tbody>
          ${f.by_marketplace.map((m) => html`<tr><td><b>${m.name}</b></td><td class="r num">${num(m.orders)}</td><td class="r num">${money(m.revenue)}</td><td class="r num">${money(m.product_cost)}</td><td class="r num">${money(m.commission)}</td><td class="r num">${money(m.service_fee)}</td><td class="r num">${money(m.shipping)}</td><td class="r num">${money(m.advertising)}</td><td class="r num">${money(m.refund)}</td><td class="r num ${signClass(m.net_profit)}">${money(m.net_profit)}</td><td class="r num">${money(m.tax_estimate)}</td><td class="r num">${pct(m.margin)}</td></tr>`)}
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
  title: 'Raporlar', icon: 'reports',
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
        $('#sku-table').innerHTML = renderVal(items.length ? html`<div class="table-wrap"><table><thead><tr>${th('sku', 'SKU', false)}${th('quantity', 'Adet')}${th('revenue', 'Ciro')}${th('product_cost', 'Maliyet')}${th('commission', 'Komisyon')}${th('shipping', 'Kargo')}${th('advertising', 'Reklam')}${th('refund', 'İade')}${th('return_rate', 'İade oranı')}${th('net_profit', 'Net kâr')}${th('tax_estimate', 'Tahmini KDV')}${th('margin', 'Marj')}</tr></thead><tbody>
          ${sorted.map((r) => html`<tr><td><b>${r.sku}</b><span class="ellipsis small muted">${r.product_name || ''}</span>${r.missing_cost ? html`<span class="badge plain tone-warn">Maliyet eksik</span>` : ''}</td>
            <td class="r num">${num(r.quantity)}</td><td class="r num">${money(r.revenue)}</td><td class="r num">${money(r.product_cost)}</td><td class="r num">${money(r.commission)}</td><td class="r num">${money(r.shipping)}</td><td class="r num">${money(r.advertising)}</td><td class="r num">${money(r.refund)}</td><td class="r num">${pct(r.return_rate)}</td>
            <td class="r num ${signClass(r.net_profit)}">${money(r.net_profit)}${estimateBadge(r.is_estimate)}</td><td class="r num">${money(r.tax_estimate)}</td><td class="r num">${pct(r.margin)}</td></tr>`)}</tbody></table></div>` : empty('Veri yok', 'Seçilen dönemde satılan ürün yok.'));
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
      return html`<label>${it.label}<input name="${it.key}" type="number" step="${it.type === 'int' ? 1 : 0.01}" min="0" value="${it.value ?? ''}" ${raw(dis)}></label>`;
    };
    view().innerHTML = renderVal(html`
      <div class="grid grid-2">
        <form class="card" id="set-form"><div class="card-head"><div><h2>İşletme ayarları</h2><p>Tüm ayarlar buradan yönetilir; sunucu dosyası düzenlemek gerekmez. Finans varsayılanlarıyla hesaplanan tutarlar raporlarda “TAHMİNİ” olarak işaretlenir.</p></div></div>
          ${[...new Set(s.items.map((it) => it.group))].map((g) => html`<fieldset class="fieldset mt"><legend>${g}</legend><div class="form-grid">${s.items.filter((it) => it.group === g).map((it) => html`<div class="${it.type === 'bool' || it.type === 'time' ? 'full' : ''}">${input(it)}</div>`)}</div>
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
          else if (it.type === 'time' || it.type === 'code') values[it.key] = el.value.trim();
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
const NAV = ['dashboard', 'alerts', 'orders', 'products', 'suppliers', 'transfer', 'shipping', 'finance', 'reports', 'integrations', 'system', 'settings', 'users'];
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
  alerts: '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
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
$('#bell').addEventListener('click', () => { location.hash = '#/alerts'; });
setInterval(() => { if (state.user && !document.hidden) refreshBell(); }, 120000);

(async () => {
  try { await startApp(await api('/api/auth/me')); } catch (e) { if (e.status !== 401) { showLogin(); $('#login-error').textContent = e.message; } }
})();
