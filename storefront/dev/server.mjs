// Trendçantanız temasını test etmek için yerel Shopify vitrin emülatörü (yalnızca geliştirme).
// Local Shopify storefront emulator for testing the Trendçantanız theme.
// Renders the real theme files with liquidjs + Shopify-like objects, filters and tags,
// and implements the AJAX Cart API / Section Rendering API used by theme.js.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { Liquid, Drop } from 'liquidjs';

const HERE = path.dirname(new URL(import.meta.url).pathname);
const THEME = process.env.THEME || path.resolve(HERE, '../theme');
const FONTS = path.join(HERE, 'node_modules', '@fontsource');
const IMG = path.join(HERE, '.cache', 'img');
const PORT = Number(process.env.PORT || 9292);
const EXTRA = process.env.EXTRA === '1';
const ORIGIN = `http://localhost:${PORT}`;
const errors = [];

/* ---------------- data ---------------- */
let nextId = 1000;
class Color extends Drop {
  constructor(hex) { super(); this.hex = hex; const n = parseInt(hex.slice(1), 16); this.red = (n >> 16) & 255; this.green = (n >> 8) & 255; this.blue = n & 255; }
  valueOf() { return this.hex; }
  toString() { return this.hex; }
}
const FONTMAP = {
  playfair_display_n4: { family: '"Playfair Display"', fallback_families: 'serif', weight: 400, style: 'normal', dir: 'playfair-display', file: 'playfair-display-latin-ext-400-normal.woff2' },
  jost_n4: { family: 'Jost', fallback_families: 'sans-serif', weight: 400, style: 'normal', dir: 'jost', file: 'jost-latin-ext-400-normal.woff2' }
};
class Font extends Drop {
  constructor(o) { super(); Object.assign(this, o); this['system?'] = false; }
}

const sizes = [[1500,1422],[1500,1500],[1500,1500],[1451,1500],[1386,1500],[1500,1448],[1466,1424],[1393,1380],[1500,1247]];
const mkImage = (name, i, alt) => {
  const [w, h] = sizes[i];
  const img = { id: nextId++, src: `/img/${name}-${i}.jpg`, width: w, height: h, alt, aspect_ratio: w / h, media_type: 'image', __img: true };
  img.preview_image = img;
  return img;
};

const mkProduct = ({ title, handle, desc, color, variants, media: mediaName, mediaCount = 9, extraMedia }) => {
  const id = nextId++;
  const media = [];
  for (let i = 0; i < mediaCount; i++) media.push(mkImage(mediaName, i, title));
  if (extraMedia) for (let i = 0; i < 4; i++) media.push(mkImage(extraMedia, i, title));
  const url = `/products/${handle}`;
  const vs = variants.map((v, idx) => {
    const vid = nextId++;
    return {
      id: vid, title: v.options.join(' / '), options: v.options, option1: v.options[0], price: v.price, compare_at_price: v.compare_at || null,
      available: v.qty > 0, inventory_quantity: v.qty, inventory_management: 'shopify', inventory_policy: 'deny',
      sku: `SKU-${vid}`, barcode: '', url: `${url}?variant=${vid}`,
      featured_media: v.mediaIndex !== undefined ? media[v.mediaIndex] : null
    };
  });
  const optionNames = variants[0].optionNames || ['Color'];
  const p = {
    id, title, handle, url, description: desc, vendor: 'My Store 2', type: '',
    media, featured_media: media[0], images: media, variants: vs,
    options: optionNames,
    tags: [], available: vs.some((v) => v.available),
    price: Math.min(...vs.map((v) => v.price)), price_min: Math.min(...vs.map((v) => v.price)), price_max: Math.max(...vs.map((v) => v.price)),
    compare_at_price: Math.min(...vs.map((v) => v.compare_at_price || 0)) || null,
    has_only_default_variant: vs.length === 1 && vs[0].title === 'Default Title',
    created_at: new Date().toISOString()
  };
  p.price_varies = p.price_min !== p.price_max;
  p.compare_at_price = vs.some((v) => v.compare_at_price) ? Math.max(...vs.map((v) => v.compare_at_price || 0)) : null;
  p.selected_or_first_available_variant = vs.find((v) => v.available) || vs[0];
  p.options_with_values = optionNames.map((name, i) => ({
    name, position: i + 1, values: [...new Set(vs.map((v) => v.options[i]))], selected_value: p.selected_or_first_available_variant.options[i]
  }));
  return p;
};

const products = [
  mkProduct({
    title: 'Top Handle Bag Leather Satchel Bags for Women Luxury Designer Business Crossbody Work Purse with Multiple Internal Pockets',
    handle: 'top-handle-bag-leather-satchel-bags-for-women-luxury-designer-business-crossbody-work-purse-with-multiple-internal-pockets',
    desc: '<div> <div> <h2>Product description</h2> </div> </div>', media: 'black',
    variants: [{ options: ['Black'], price: 11808, qty: 10 }]
  }),
  mkProduct({
    title: 'Top Handle Bag Fashion Leather Satchel Bag for Ladies, Zipper Pocket, Metal，Leather Tote Bag for Women.',
    handle: 'top-handle-bag-fashion-leather-satchel-bag-for-ladies-zipper-pocket-metal-leather-tote-bag-for-women',
    desc: '<div><p><span>top handle bag,handbag,handbags for women,handbags women,handbags white handbags,the tote bag,tote bag</span></p></div>', media: 'brown',
    variants: [{ options: ['Brown'], price: 9720, qty: 0 }]
  })
];
if (EXTRA) {
  products.push(mkProduct({
    title: 'Test Omuz Çantası (yalnızca test)', handle: 'test-omuz-cantasi', desc: '<p>Test ürünü.</p>', media: 'beige', mediaCount: 5, extraMedia: 'black',
    variants: [
      { options: ['Bej', 'Standart'], optionNames: ['Renk', 'Boyut'], price: 7990, compare_at: 9990, qty: 2, mediaIndex: 0 },
      { options: ['Siyah', 'Standart'], price: 7990, compare_at: 9990, qty: 6, mediaIndex: 5 },
      { options: ['Bej', 'Büyük'], price: 8990, qty: 0, mediaIndex: 1 }
    ].map((v, i, arr) => ({ ...v, optionNames: arr[0].optionNames }))
  }));
  for (let i = 0; i < 5; i++) products.push(mkProduct({ title: `Test Çanta ${i + 1}`, handle: `test-canta-${i + 1}`, desc: '', media: ['black', 'brown', 'beige'][i % 3], mediaCount: 3, variants: [{ options: ['Default Title'], optionNames: ['Title'], price: 5000 + i * 1000, qty: 5 }] }));
}
const byHandle = Object.fromEntries(products.map((p) => [p.handle, p]));
const variantById = {};
products.forEach((p) => p.variants.forEach((v) => { variantById[v.id] = { v, p }; }));

const SORTS = [
  { value: 'manual', name: 'Öne çıkanlar' }, { value: 'best-selling', name: 'En çok satanlar' },
  { value: 'title-ascending', name: 'A-Z' }, { value: 'price-ascending', name: 'Fiyat, düşükten yükseğe' },
  { value: 'price-descending', name: 'Fiyat, yüksekten düşüğe' }, { value: 'created-descending', name: 'En yeni' }
];
const mkCollection = (handle, title, prods, sort) => {
  let list = prods.slice();
  if (sort === 'created-descending') list.reverse();
  if (sort === 'price-ascending') list.sort((a, b) => a.price - b.price);
  if (sort === 'price-descending') list.sort((a, b) => b.price - a.price);
  const colorValues = [...new Set(prods.flatMap((p) => p.variants.map((v) => v.options[0])))];
  return {
    id: nextId++, handle, title, url: `/collections/${handle}`, products: list, products_count: prods.length, all_products_count: prods.length,
    image: null, description: '', sort_by: sort || '', default_sort_by: 'manual', sort_options: SORTS,
    filters: handle === 'all' ? [
      { label: 'Stok durumu', type: 'list', active_values: [], values: [{ label: 'Stokta', value: '1', param_name: 'filter.v.availability', count: prods.filter((p) => p.available).length, active: false }, { label: 'Stokta yok', value: '0', param_name: 'filter.v.availability', count: prods.filter((p) => !p.available).length, active: false }] },
      { label: 'Renk', type: 'list', active_values: [], values: colorValues.map((c) => ({ label: c, value: c, param_name: 'filter.v.option.color', count: 1, active: false })) },
      { label: 'Fiyat', type: 'price_range', active_values: [], min_value: { param_name: 'filter.v.price.gte', value: null }, max_value: { param_name: 'filter.v.price.lte', value: null }, range_max: 11808, url_to_remove: '/collections/all' }
    ] : []
  };
};
const collectionsFor = (sort) => {
  const all = mkCollection('all', 'Products', products, sort);
  const list = [
    mkCollection('frontpage', 'Home page', [products[0]]),
    mkCollection('asset-pack-126820089858-example-products', 'Default example products', []),
    mkCollection('face-eye-care', 'Face & Eye Care', []),
    mkCollection('lip-oral-care', 'Lip & Oral Care', [])
  ];
  if (EXTRA) list.push(mkCollection('omuz-cantalari', 'Omuz Çantaları', products.slice(2, 6)), mkCollection('capraz-cantalar', 'Çapraz Çantalar', products.slice(4)));
  const arr = list.slice();
  arr.all = all;
  list.forEach((c) => { arr[c.handle] = c; });
  return arr;
};

/* ---------------- cart ---------------- */
let cart = { items: [], note: '' };
const cartObject = () => {
  const items = cart.items.map((it, i) => {
    const { v, p } = variantById[it.id];
    return {
      key: `${it.id}:k`, id: v.id, variant_id: v.id, product_id: p.id, quantity: it.quantity, product: p, variant: v,
      title: `${p.title} - ${v.title}`, url: v.url, image: v.featured_media || p.featured_media,
      options_with_values: p.options.map((n, j) => ({ name: n, value: v.options[j] })),
      price: v.price, final_price: v.price, original_price: v.price, final_line_price: v.price * it.quantity,
      original_line_price: v.price * it.quantity, line_price: v.price * it.quantity,
      properties: {}, url_to_remove: `/cart/change?line=${i + 1}&quantity=0`, line_level_discount_allocations: []
    };
  });
  const total = items.reduce((s, it) => s + it.final_line_price, 0);
  return {
    items, item_count: items.reduce((s, it) => s + it.quantity, 0), total_price: total, items_subtotal_price: total, original_total_price: total,
    note: cart.note, currency: { iso_code: 'USD' }, taxes_included: false, cart_level_discount_applications: [], token: 'mock'
  };
};
const cartJSON = () => {
  const c = cartObject();
  return { ...c, items: c.items.map((it) => ({ id: it.id, key: it.key, quantity: it.quantity, title: it.title, price: it.price, line_price: it.final_line_price, url: it.url, product_title: it.product.title })) };
};
const addToCart = (id, qty) => {
  const entry = variantById[id];
  if (!entry) return { status: 404, message: 'Cart Error', description: 'Ürün bulunamadı' };
  if (!entry.v.available) return { status: 422, message: 'Cart Error', description: `${entry.p.title} tükendi.` };
  const line = cart.items.find((i) => i.id === Number(id));
  const newQty = (line ? line.quantity : 0) + qty;
  if (newQty > entry.v.inventory_quantity) return { status: 422, message: 'Cart Error', description: `Stokta yalnızca ${entry.v.inventory_quantity} adet var.` };
  if (line) line.quantity = newQty; else cart.items.push({ id: Number(id), quantity: qty });
  return null;
};

/* ---------------- liquid ---------------- */
const locale = JSON.parse(fs.readFileSync(path.join(THEME, 'locales/tr.default.json'), 'utf8'));
const settingsData = JSON.parse(fs.readFileSync(path.join(THEME, 'config/settings_data.json'), 'utf8'));
const settingsSchema = JSON.parse(fs.readFileSync(path.join(THEME, 'config/settings_schema.json'), 'utf8'));

const engine = new Liquid({
  root: [path.join(THEME, 'snippets')], partials: path.join(THEME, 'snippets'), extname: '.liquid',
  relativeReference: false, strictFilters: true, cache: false, jsTruthy: false, lenientIf: true, orderedFilterParameters: true
});
const money = (c) => `$${(Number(c || 0) / 100).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',')}`;
const lookup = (key) => key.split('.').reduce((o, k) => (o ? o[k] : undefined), locale);
engine.registerFilter('t', function (key, ...args) {
  const params = {};
  for (let i = 0; i < args.length; i++) { const a = args[i]; if (Array.isArray(a)) params[a[0]] = a[1]; }
  let v = lookup(key);
  if (v === undefined) { errors.push(`missing translation ${key}`); return `translation missing: ${key}`; }
  if (typeof v === 'object') v = params.count === 1 ? v.one : v.other;
  return String(v).replace(/\{\{\s*(\w+)\s*\}\}/g, (_, k) => (params[k] !== undefined ? params[k] : ''));
});
engine.registerFilter('money', money);
engine.registerFilter('money_with_currency', (c) => `${money(c)} USD`);
engine.registerFilter('money_without_currency', (c) => (Number(c || 0) / 100).toFixed(2));
engine.registerFilter('image_url', (img, ...args) => {
  if (!img) { errors.push('image_url on nil'); return ''; }
  const src = typeof img === 'string' ? img : img.src;
  const w = (args.find((a) => Array.isArray(a) && a[0] === 'width') || [])[1];
  return `${src}${w ? `?width=${w}` : ''}`;
});
engine.registerFilter('asset_url', (n) => `/assets/${n}`);
engine.registerFilter('stylesheet_tag', (u) => `<link href="${u}" rel="stylesheet" type="text/css" media="all">`);
engine.registerFilter('font_face', (f) => (f && f.file ? [f.file, f.file.replace('latin-ext-', 'latin-')].map((file) => `@font-face{font-family:${f.family};font-weight:${f.weight};font-style:${f.style};font-display:swap;src:url(/fonts/${f.dir}/${file}) format("woff2");${file.includes('ext') ? 'unicode-range:U+0100-02AF,U+1E00-1EFF;' : ''}}`).join('') : ''));
engine.registerFilter('font_url', (f) => (f ? `/fonts/${f.dir}/${f.file}` : ''));
engine.registerFilter('font_modify', (f, prop, val) => {
  if (!f) return f;
  const o = new Font({ ...f });
  if (prop === 'weight') { o.weight = Number(val); if (f.dir === 'jost') o.file = 'jost-latin-ext-500-normal.woff2'; }
  if (prop === 'style') { o.style = val; if (f.dir === 'playfair-display') o.file = 'playfair-display-latin-ext-400-italic.woff2'; }
  return o;
});
engine.registerFilter('placeholder_svg_tag', (n, cls) => `<svg class="${cls || ''}" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>`);
engine.registerFilter('payment_type_svg_tag', (t) => `<svg class="payment-icon" viewBox="0 0 38 24"><rect width="38" height="24" rx="3" fill="#fff"/><text x="4" y="16" font-size="9">${t}</text></svg>`);
engine.registerFilter('handleize', (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''));
engine.registerFilter('link_to', (t, u) => `<a href="${u}">${t}</a>`);
engine.registerFilter('time_tag', (d) => `<time>${d}</time>`);
engine.registerFilter('default_errors', (e) => String(e || ''));
engine.registerFilter('format_address', () => '<p>Adres</p>');
engine.registerFilter('media_tag', () => '<video></video>');
engine.registerFilter('payment_button', () => '<div class="shopify-payment-button"><button type="button" class="shopify-payment-button__button shopify-payment-button__button--unbranded">Hemen Al</button></div>');
engine.registerFilter('structured_data', () => '');

const sectionSchemas = {};
const readSchema = (name) => {
  if (sectionSchemas[name]) return sectionSchemas[name];
  const src = fs.readFileSync(path.join(THEME, 'sections', `${name}.liquid`), 'utf8');
  const m = src.match(/\{%\s*schema\s*%\}([\s\S]*?)\{%\s*endschema\s*%\}/);
  const schema = m ? JSON.parse(m[1]) : {};
  sectionSchemas[name] = schema;
  return schema;
};

const menus = {
  'main-menu': { title: 'Main menu', links: [
    { title: 'Home', url: '/', links: [] },
    { title: 'Catalog', url: '/collections/all', links: [] },
    { title: 'Contact', url: '/pages/contact', links: [] }
  ] },
  footer: { title: 'Footer menu', links: [{ title: 'Search', url: '/search', links: [] }] }
};

const resolveSetting = (type, value, ctx) => {
  if (value === '' || value === undefined || value === null) return null;
  switch (type) {
    case 'product': return byHandle[value] || null;
    case 'collection': return ctx.collections[value] || null;
    case 'link_list': return menus[value] || null;
    case 'image_picker': return null;
    default: return value;
  }
};
const buildSettings = (schemaSettings = [], values = {}, ctx) => {
  const out = {};
  for (const s of schemaSettings) {
    if (!s.id) continue;
    const raw = values[s.id] !== undefined ? values[s.id] : s.default;
    out[s.id] = resolveSetting(s.type, raw, ctx);
  }
  return out;
};

class RawTag {
  parse(token, remain) { while (remain.length) { const t = remain.shift(); if (t.name === `end${token.name}`) return; } }
  * render() { return ''; }
}

engine.registerTag('style', {
  parse(token, remain) {
    this.tpls = [];
    const stream = this.liquid.parser.parseStream(remain).on('tag:endstyle', () => stream.stop()).on('template', (t) => this.tpls.push(t)).on('end', () => { throw new Error('style not closed'); });
    stream.start();
  },
  * render(ctx, emitter) { emitter.write('<style>'); yield this.liquid.renderer.renderTemplates(this.tpls, ctx, emitter); emitter.write('</style>'); }
});

const FORM_ACTIONS = { product: '/cart/add', customer: '/contact#contact_form', contact: '/contact#contact_form', storefront_password: '/password', customer_login: '/account/login', recover_customer_password: '/account/recover', create_customer: '/account', guest_login: '/account/login', customer_address: '/account/addresses', reset_customer_password: '/account/reset', activate_customer_password: '/account/activate' };
engine.registerTag('form', {
  parse(token, remain) {
    this.args = token.args;
    this.tpls = [];
    const stream = this.liquid.parser.parseStream(remain).on('tag:endform', () => stream.stop()).on('template', (t) => this.tpls.push(t)).on('end', () => { throw new Error('form not closed'); });
    stream.start();
  },
  * render(ctx, emitter) {
    const type = this.args.match(/^\s*'([^']+)'/)[1];
    const attrs = [];
    const re = /([\w-]+):\s*('([^']*)'|"([^"]*)"|([\w.\-]+))/g;
    let m;
    while ((m = re.exec(this.args))) {
      let val = m[3] ?? m[4];
      if (val === undefined) val = yield this.liquid.evalValue(m[5], ctx);
      attrs.push(`${m[1]}="${val ?? ''}"`);
    }
    emitter.write(`<form method="post" action="${FORM_ACTIONS[type] || '/'}" accept-charset="UTF-8" enctype="multipart/form-data" ${attrs.join(' ')}><input type="hidden" name="form_type" value="${type}"><input type="hidden" name="utf8" value="✓">`);
    ctx.push({ form: { errors: null, 'posted_successfully?': false, password_needed: true } });
    yield this.liquid.renderer.renderTemplates(this.tpls, ctx, emitter);
    ctx.pop();
    emitter.write('</form>');
  }
});
engine.registerTag('paginate', {
  parse(token, remain) {
    this.tpls = [];
    const stream = this.liquid.parser.parseStream(remain).on('tag:endpaginate', () => stream.stop()).on('template', (t) => this.tpls.push(t)).on('end', () => { throw new Error('paginate not closed'); });
    stream.start();
  },
  * render(ctx, emitter) {
    ctx.push({ paginate: { pages: 1, current_page: 1, parts: [], previous: null, next: null } });
    yield this.liquid.renderer.renderTemplates(this.tpls, ctx, emitter);
    ctx.pop();
  }
});

const GLOBAL_KEYS = ['settings','collections','all_products','shop','routes','request','template','cart','customer','linklists','product','collection','canonical_url','all_country_option_tags','additional_checkout_buttons','page_title','page_description','page_image','current_page','current_tags'];
const globalsOf = (c) => Object.fromEntries(GLOBAL_KEYS.map((k) => [k, c[k]]));
const renderSection = async (type, id, values, baseCtx, extraClass = '') => {
  const schema = readSchema(type);
  const settings = buildSettings(schema.settings, values.settings || {}, baseCtx);
  const blocks = (values.block_order || Object.keys(values.blocks || {})).map((bid) => {
    const b = values.blocks[bid];
    const bs = (schema.blocks || []).find((x) => x.type === b.type) || {};
    return { id: bid, type: b.type, settings: buildSettings(bs.settings, b.settings || {}, baseCtx), shopify_attributes: '' };
  });
  const section = { id, settings, blocks, shopify_attributes: '' };
  const src = fs.readFileSync(path.join(THEME, 'sections', `${type}.liquid`), 'utf8')
    .replace(/\{%-?\s*(schema|stylesheet|javascript)\s*-?%\}[\s\S]*?\{%-?\s*end\1\s*-?%\}/g, '');
  const html = await engine.parseAndRender(src, { ...baseCtx, section }, { globals: globalsOf(baseCtx) });
  return `<div id="shopify-section-${id}" class="shopify-section ${extraClass} ${schema.class || ''}">${html}</div>`;
};
engine.registerTag('section', {
  parse(token) { this.name = token.args.match(/'([^']+)'/)[1]; },
  * render(ctx, emitter) { emitter.write(yield renderSection(this.name, this.name, {}, ctx.getAll())); }
});
engine.registerTag('sections', {
  parse(token) { this.name = token.args.match(/'([^']+)'/)[1]; },
  * render(ctx, emitter) {
    const group = JSON.parse(fs.readFileSync(path.join(THEME, 'sections', `${this.name}.json`), 'utf8'));
    for (const key of group.order) {
      const s = group.sections[key];
      emitter.write(yield renderSection(s.type, `sections--2__${key}`, s, ctx.getAll(), `shopify-section-group-${this.name}`));
    }
  }
});

const baseContext = (req, page) => {
  const url = new URL(req.url, ORIGIN);
  const sort = url.searchParams.get('sort_by') || '';
  const collections = collectionsFor(sort);
  const settings = {};
  settingsSchema.forEach((g) => (g.settings || []).forEach((s) => { if (s.id) settings[s.id] = s.default ?? null; }));
  Object.assign(settings, settingsData.presets.Default);
  for (const s of settingsSchema.flatMap((g) => g.settings || [])) {
    if (s.type === 'color') settings[s.id] = new Color(settings[s.id]);
    if (s.type === 'font_picker') settings[s.id] = new Font(FONTMAP[settings[s.id]]);
    if (s.type === 'image_picker') settings[s.id] = null;
  }
  return {
    settings, collections, all_products: byHandle,
    shop: { name: 'My Store 2', money_format: '${{amount}}', customer_accounts_enabled: true, enabled_payment_types: ['visa', 'master', 'paypal'], policies: [{ title: 'İade politikası', url: '/policies/refund-policy' }], description: '', shipping_policy: { body: '' }, checkout: { guest_login: false } },
    routes: {
      root_url: '/', cart_url: '/cart', cart_add_url: '/cart/add', cart_change_url: '/cart/change', cart_update_url: '/cart/update',
      search_url: '/search', predictive_search_url: '/search/suggest', all_products_collection_url: '/collections/all', collections_url: '/collections',
      account_url: '/account', account_login_url: '/account/login', account_register_url: '/account/register', account_logout_url: '/account/logout', account_addresses_url: '/account/addresses',
      product_recommendations_url: '/recommendations/products'
    },
    request: { page_type: page.type, locale: { iso_code: 'tr' }, origin: ORIGIN, path: url.pathname },
    template: { name: page.type, suffix: null },
    canonical_url: ORIGIN + url.pathname, page_title: page.title || 'My Store 2', page_description: page.description || '', page_image: page.image || null,
    content_for_header: '', cart: cartObject(), customer: null, linklists: menus,
    product: page.product || null, collection: page.collection || null, search: page.search || null,
    current_page: 1, current_tags: null, additional_checkout_buttons: false, content_for_additional_checkout_buttons: '',
    all_country_option_tags: '<option>Turkey</option>'
  };
};

const renderTemplate = async (req, page) => {
  const ctx = baseContext(req, page);
  if (page.collectionHandle) ctx.collection = page.collectionHandle === 'all' ? ctx.collections.all : ctx.collections[page.collectionHandle];
  if (page.type === 'collection' && !ctx.collection) return null;
  const tpl = JSON.parse(fs.readFileSync(path.join(THEME, 'templates', `${page.template || page.type}.json`), 'utf8'));
  let body = '';
  for (const key of tpl.order) {
    const s = tpl.sections[key];
    body += await renderSection(s.type, `template--1__${key}`, s, ctx);
  }
  const layout = fs.readFileSync(path.join(THEME, 'layout', `${tpl.layout || 'theme'}.liquid`), 'utf8');
  return engine.parseAndRender(layout, { ...ctx, content_for_layout: body }, { globals: globalsOf(ctx) });
};

/* ---------------- server ---------------- */
const MIME = { '.css': 'text/css', '.js': 'application/javascript', '.jpg': 'image/jpeg', '.woff2': 'font/woff2' };
const send = (res, code, body, type = 'text/html; charset=utf-8', headers = {}) => { res.writeHead(code, { 'Content-Type': type, ...headers }); res.end(body); };
const readBody = (req) => new Promise((resolve) => { const chunks = []; req.on('data', (c) => chunks.push(c)); req.on('end', () => resolve(Buffer.concat(chunks))); });
const parseForm = async (req) => {
  const buf = await readBody(req);
  const ct = req.headers['content-type'] || '';
  if (ct.includes('application/json')) return JSON.parse(buf.toString() || '{}');
  const fd = await new Response(buf, { headers: { 'content-type': ct } }).formData();
  const out = {};
  for (const [k, v] of fd.entries()) { if (out[k] !== undefined) out[k] = [].concat(out[k], v); else out[k] = v; }
  return out;
};
const renderSectionsMap = async (req, ids, pageUrl) => {
  const map = {};
  const fakeReq = { url: pageUrl || '/' };
  const ctx = baseContext(fakeReq, { type: 'index' });
  for (const id of ids) {
    if (id === 'cart-drawer') map[id] = await renderSection('cart-drawer', 'cart-drawer', {}, ctx);
    else if (id.startsWith('template--1__')) {
      const tpl = JSON.parse(fs.readFileSync(path.join(THEME, 'templates/cart.json'), 'utf8'));
      const key = id.replace('template--1__', '');
      map[id] = await renderSection(tpl.sections[key].type, id, tpl.sections[key], ctx);
    }
  }
  return map;
};

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, ORIGIN);
    const p = url.pathname;
    if (p.startsWith('/assets/')) { const f = path.join(THEME, p); return send(res, 200, fs.readFileSync(f), MIME[path.extname(f)]); }
    if (p.startsWith('/img/')) { const f = path.join(IMG, path.basename(p)); return send(res, 200, fs.readFileSync(f), 'image/jpeg', { 'Cache-Control': 'max-age=3600' }); }
    if (p.startsWith('/fonts/')) { const [, , dir, file] = p.split('/'); return send(res, 200, fs.readFileSync(path.join(FONTS, dir, 'files', file)), 'font/woff2'); }
    if (p === '/favicon.ico') return send(res, 204, '');

    // Section rendering API
    const sectionId = url.searchParams.get('section_id');

    if (p === '/cart.js') return send(res, 200, JSON.stringify(cartJSON()), 'application/json');
    if (p === '/cart/add.js' && req.method === 'POST') {
      const body = await parseForm(req);
      const err = addToCart(Number(body.id), Number(body.quantity || 1));
      if (err) return send(res, err.status, JSON.stringify(err), 'application/json');
      const out = { id: Number(body.id), quantity: Number(body.quantity || 1) };
      if (body.sections) out.sections = await renderSectionsMap(req, String(body.sections).split(','), body.sections_url);
      return send(res, 200, JSON.stringify(out), 'application/json');
    }
    if (p === '/cart/change.js' && req.method === 'POST') {
      const body = await parseForm(req);
      const line = Number(body.line); const q = Number(body.quantity);
      if (cart.items[line - 1]) {
        const it = cart.items[line - 1];
        const { v } = variantById[it.id];
        if (q > v.inventory_quantity) return send(res, 422, JSON.stringify({ status: 422, message: 'Cart Error', description: `Stokta yalnızca ${v.inventory_quantity} adet var.` }), 'application/json');
        if (q <= 0) cart.items.splice(line - 1, 1); else it.quantity = q;
      }
      const out = cartJSON();
      if (body.sections) out.sections = await renderSectionsMap(req, [].concat(body.sections), body.sections_url);
      return send(res, 200, JSON.stringify(out), 'application/json');
    }
    if (p === '/cart/update.js' && req.method === 'POST') { const body = await parseForm(req); cart.note = body.note || ''; return send(res, 200, JSON.stringify(cartJSON()), 'application/json'); }
    if (p === '/cart/add' && req.method === 'POST') { const body = await parseForm(req); addToCart(Number(body.id), Number(body.quantity || 1)); return send(res, 302, '', 'text/plain', { Location: '/cart' }); }
    if (p === '/cart' && req.method === 'POST') {
      const body = await parseForm(req);
      const updates = body['updates[]'] !== undefined ? [].concat(body['updates[]']) : [];
      updates.forEach((q, i) => { if (cart.items[i]) cart.items[i].quantity = Number(q); });
      cart.items = cart.items.filter((i) => i.quantity > 0);
      if (body.checkout !== undefined) return send(res, 302, '', 'text/plain', { Location: '/checkouts/mock' });
      return send(res, 302, '', 'text/plain', { Location: '/cart' });
    }
    if (p === '/checkouts/mock') return send(res, 200, `<h1 id="checkout">CHECKOUT OK</h1><pre>${JSON.stringify(cartJSON().items)}</pre>`);
    if (p === '/__reset') { cart = { items: [], note: '' }; errors.length = 0; return send(res, 200, 'ok'); }
    if (p === '/__errors') return send(res, 200, JSON.stringify(errors), 'application/json');

    if (p === '/search/suggest') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const ctx = baseContext(req, { type: 'search' });
      ctx.predictive_search = { performed: true, terms: q, resources: { products: products.filter((x) => x.title.toLowerCase().includes(q)), collections: [] } };
      return send(res, 200, await renderSection('predictive-search', 'predictive-search', {}, ctx));
    }
    if (p === '/recommendations/products') {
      const pid = Number(url.searchParams.get('product_id'));
      const ctx = baseContext(req, { type: 'product' });
      const recs = products.filter((x) => x.id !== pid).slice(0, 4);
      ctx.recommendations = { 'performed?': true, performed: true, products_count: recs.length, products: recs };
      ctx.product = products.find((x) => x.id === pid);
      const tpl = JSON.parse(fs.readFileSync(path.join(THEME, 'templates/product.json'), 'utf8'));
      const key = sectionId.replace('template--1__', '');
      return send(res, 200, await renderSection('related-products', sectionId, tpl.sections[key], ctx));
    }

    let page = null;
    let m;
    if (p === '/') page = { type: 'index', title: 'My Store 2' };
    else if ((m = p.match(/^\/products\/([^/]+)$/))) {
      const product = byHandle[m[1]];
      if (product) {
        const vid = Number(url.searchParams.get('variant'));
        const selected = product.variants.find((v) => v.id === vid);
        const prod = selected ? { ...product, selected_or_first_available_variant: selected, options_with_values: product.options_with_values.map((o, i) => ({ ...o, selected_value: selected.options[i] })) } : product;
        page = { type: 'product', product: prod, title: product.title, image: product.featured_media };
      }
    } else if ((m = p.match(/^\/collections\/([^/]+)$/))) page = { type: 'collection', collectionHandle: m[1], title: m[1] };
    else if (p === '/collections') page = { type: 'list-collections', title: 'Koleksiyonlar' };
    else if (p === '/cart') page = { type: 'cart', title: 'Sepet' };
    else if (p === '/search') {
      const q = url.searchParams.get('q');
      const results = q ? products.filter((x) => x.title.toLowerCase().includes(q.toLowerCase())).map((x) => ({ ...x, object_type: 'product' })) : [];
      page = { type: 'search', search: { performed: !!q, terms: q || '', results, results_count: results.length } };
    }

    if (sectionId && page) {
      const ctx = baseContext(req, page);
      if (page.collectionHandle) ctx.collection = page.collectionHandle === 'all' ? ctx.collections.all : ctx.collections[page.collectionHandle];
      return send(res, 200, await renderSection(sectionId, sectionId, {}, ctx));
    }
    if (!page) page = { type: '404', title: '404' };
    const html = await renderTemplate(req, page);
    if (html === null) return send(res, 404, await renderTemplate(req, { type: '404' }));
    return send(res, page.type === '404' ? 404 : 200, html);
  } catch (err) {
    console.error(err);
    errors.push(String(err && err.message));
    send(res, 500, `<pre>${String(err && err.stack)}</pre>`);
  }
});
server.listen(PORT, () => console.log(`mock store on ${ORIGIN}`));
