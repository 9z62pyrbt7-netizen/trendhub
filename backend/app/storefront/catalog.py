"""Web mağazası kataloğu: TrendHub'ın merkezi ürün kataloğundan (products) okur.

Kaynak zinciri (değiştirilmez, yalnızca okunur):
  tedarikçi beslemesi (supplier_products) ─► products (stok, maliyet, görseller, açıklama)
  pazaryeri ilan içe aktarımı ─────────────┘
Web'e özel ayarlar (yayında mı, başlık, üstü çizili fiyat, öne çıkan) `storefront_products`'tadır.

Görünürlük: aktif, satış fiyatı > 0 ve en az bir görseli olan ürün; `storefront_products.published`
TRUE ise veya (ayar kaydı yoksa) `storefront.auto_publish` açıksa yayındadır.

Varyantlar: aynı model kodu (products.model_code veya tedarikçi ana ürün kodu parent_code) taşıyan
ürünler tek ürün sayfasında renk/beden seçenekleri olarak gösterilir. Renk/beden tedarikçi
teklifinden (supplier_products.color/size) okunur.
"""
from __future__ import annotations

import html
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..services import stock_availability
from . import store_config

SNAPSHOT_TTL = 30.0
BESTSELLER_DAYS = 90

_TR = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "İ": "i", "ö": "o", "ş": "s", "ü": "u",
                     "Ç": "c", "Ğ": "g", "I": "i", "Ö": "o", "Ş": "s", "Ü": "u", "â": "a", "î": "i", "û": "u"})


def slugify(s: str | None) -> str:
    s = (s or "").translate(_TR)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80].strip("-") or "urun"


def fold(s: str | None) -> str:
    """Arama için Türkçe duyarlı küçük harfe indirgeme."""
    return slugify(s).replace("-", " ")


def product_path(pid: int, title: str) -> str:
    return f"/urun/{slugify(title)}-p{pid}"


PATH_ID = re.compile(r"-p(\d+)$")


def parse_product_slug(slug: str) -> int | None:
    m = PATH_ID.search(slug)
    return int(m.group(1)) if m else None


def category_label(cat: str | None) -> str | None:
    if not cat or not cat.strip():
        return None
    parts = [p.strip() for p in re.split(r"\s*(?:>|/|\||»)\s*", cat) if p.strip()]
    return parts[-1] if parts else None


COLOR_HEX = {
    "siyah": "#151311", "black": "#151311", "beyaz": "#F4F0E8", "white": "#F4F0E8", "ekru": "#EFE7D8",
    "krem": "#EDE3D1", "cream": "#EDE3D1", "bej": "#D8C3A5", "beige": "#D8C3A5", "nude": "#D9B99B",
    "ten": "#D9B99B", "kahverengi": "#5E3B28", "kahve": "#5E3B28", "brown": "#5E3B28", "taba": "#A8703D",
    "camel": "#B88A5A", "konyak": "#9A5B2E", "vizon": "#9C8B7A", "gri": "#8A8580", "gray": "#8A8580",
    "grey": "#8A8580", "fume": "#5B5A58", "antrasit": "#3E3D3B", "lacivert": "#1F2A44", "navy": "#1F2A44",
    "mavi": "#4A6D9B", "blue": "#4A6D9B", "bebek mavisi": "#A9C4DE", "kirmizi": "#A1271F", "red": "#A1271F",
    "bordo": "#5E1A22", "pembe": "#E4B7B7", "pink": "#E4B7B7", "pudra": "#E6C4BC", "yesil": "#4D5B3C",
    "green": "#4D5B3C", "haki": "#6F6A48", "zumrut": "#1F5E4A", "sari": "#D1A53C", "yellow": "#D1A53C",
    "hardal": "#C0902E", "turuncu": "#CF6D33", "orange": "#CF6D33", "mor": "#6C4E7C", "lila": "#B8A2C8",
    "gumus": "#C4C4C4", "silver": "#C4C4C4", "altin": "#C9A45C", "gold": "#C9A45C", "leopar": "#B07A3F",
}


def color_hex(name: str | None) -> str:
    key = fold(name)
    if key in COLOR_HEX:
        return COLOR_HEX[key]
    for word in key.split():
        if word in COLOR_HEX:
            return COLOR_HEX[word]
    return "#D8CBB8"


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "tr", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def paragraphs(desc: str | None) -> list[str]:
    """Tedarikçi açıklamasındaki HTML'i güvenli düz metin paragraflarına çevirir (HTML çalıştırılmaz)."""
    if not desc:
        return []
    p = _TextExtractor()
    try:
        p.feed(desc)
        p.close()
    except Exception:  # noqa: BLE001
        return [html.unescape(re.sub(r"<[^>]+>", " ", desc)).strip()]
    lines = [re.sub(r"[ \t\r\f\v]+", " ", x).strip() for x in "".join(p.parts).split("\n")]
    return [x for x in lines if x][:40]


def plain(desc: str | None, limit: int = 160) -> str:
    s = " ".join(paragraphs(desc))
    return (s[: limit - 1].rsplit(" ", 1)[0] + "…") if len(s) > limit else s


# ------------------------------------------------------------------ modeller
@dataclass
class Variant:
    id: int
    sku: str | None
    barcode: str | None
    title: str
    price: Decimal
    compare_at: Decimal | None
    images: list[str]
    color: str | None
    size: str | None
    available: int
    brand: str | None
    category: str | None
    description: str | None
    created_at: datetime | None
    featured: bool
    sort_order: int
    group_key: str

    @property
    def url(self) -> str:
        return product_path(self.id, self.title)

    @property
    def in_stock(self) -> bool:
        return self.available > 0

    @property
    def discount_pct(self) -> int | None:
        if self.compare_at and self.compare_at > self.price:
            return int(round((self.compare_at - self.price) * 100 / self.compare_at))
        return None


@dataclass
class Group:
    key: str
    variants: list[Variant]
    sold: int = 0

    @property
    def rep(self) -> Variant:
        # Temsilci: stoktaki ilk varyant (öne çıkan / id sırası), yoksa ilk varyant
        in_stock = [v for v in self.variants if v.in_stock]
        return (in_stock or self.variants)[0]

    @property
    def id(self) -> int:
        return self.rep.id

    @property
    def title(self) -> str:
        return self.rep.title

    @property
    def url(self) -> str:
        return self.rep.url

    @property
    def price(self) -> Decimal:
        return min(v.price for v in self.variants)

    @property
    def price_varies(self) -> bool:
        return len({v.price for v in self.variants}) > 1

    @property
    def images(self) -> list[str]:
        return self.rep.images

    @property
    def available(self) -> int:
        return sum(v.available for v in self.variants)

    @property
    def in_stock(self) -> bool:
        return self.available > 0

    @property
    def category(self) -> str | None:
        return category_label(self.rep.category)

    @property
    def created_at(self) -> datetime | None:
        dates = [v.created_at for v in self.variants if v.created_at]
        return max(dates) if dates else None

    @property
    def featured(self) -> bool:
        return any(v.featured for v in self.variants)

    @property
    def colors(self) -> list[dict]:
        seen, out = set(), []
        for v in self.variants:
            if v.color and fold(v.color) not in seen:
                seen.add(fold(v.color))
                out.append({"name": v.color, "hex": color_hex(v.color), "url": v.url,
                            "image": v.images[0] if v.images else None, "in_stock": v.in_stock})
        return out

    @property
    def search_text(self) -> str:
        parts = []
        for v in self.variants:
            parts += [v.title, v.category or "", v.color or "", v.size or "", v.brand or "", v.sku or "", v.barcode or ""]
        return fold(" ".join(parts))


# ------------------------------------------------------------------ sorgular
def _visible_sql() -> str:
    return """COALESCE(p.is_active, TRUE) AND COALESCE(p.sale_price, 0) > 0
          AND (jsonb_array_length(COALESCE(p.images, '[]'::jsonb)) > 0 OR COALESCE(p.image_url, '') <> '')
          AND (sfp.published IS TRUE OR (sfp.product_id IS NULL AND :auto_publish))"""


def _variants(conn: Connection, where: str = "TRUE", **params) -> list[Variant]:
    cfg = store_config.load(conn)
    sql = f"""
        SELECT p.id, p.sku, p.barcode, COALESCE(NULLIF(sfp.title, ''), p.name) AS title, p.sale_price AS price,
               sfp.compare_at_price, p.images, p.image_url, sp.color, sp.size, p.brand, p.category,
               COALESCE(NULLIF(sfp.description, ''), p.description) AS description, p.created_at,
               COALESCE(sfp.featured, FALSE) AS featured, COALESCE(sfp.sort_order, 0) AS sort_order,
               COALESCE(NULLIF(p.model_code, ''), NULLIF(sp.parent_code, ''), 'p' || p.id) AS group_key,
               {stock_availability.available_sql('p')} AS available
          FROM products p
          LEFT JOIN storefront_products sfp ON sfp.product_id = p.id
          LEFT JOIN LATERAL (
              SELECT x.color, x.size, x.parent_code FROM supplier_products x WHERE x.product_id = p.id
               ORDER BY (x.supplier_id = p.preferred_supplier_id) DESC NULLS LAST, x.id LIMIT 1) sp ON TRUE
         WHERE {_visible_sql()} AND {where}
         ORDER BY COALESCE(sfp.featured, FALSE) DESC, COALESCE(sfp.sort_order, 0), p.id
    """
    out = []
    for r in conn.execute(text(sql), {"auto_publish": cfg.auto_publish, **stock_availability.params(conn), **params}).mappings():
        imgs = [u for u in (r["images"] or []) if isinstance(u, str) and u.startswith(("http://", "https://", "/"))]
        if not imgs and r["image_url"]:
            imgs = [r["image_url"]]
        price = Decimal(r["price"])
        cmp_ = Decimal(r["compare_at_price"]) if r["compare_at_price"] is not None else None
        out.append(Variant(id=r["id"], sku=r["sku"], barcode=r["barcode"], title=(r["title"] or "").strip() or f"Ürün {r['id']}",
                           price=price, compare_at=cmp_ if cmp_ and cmp_ > price else None, images=imgs,
                           color=(r["color"] or "").strip() or None, size=(r["size"] or "").strip() or None,
                           available=int(r["available"]), brand=r["brand"], category=r["category"],
                           description=r["description"], created_at=r["created_at"], featured=r["featured"],
                           sort_order=r["sort_order"], group_key=r["group_key"]))
    return out


def _sales(conn: Connection, days: int = BESTSELLER_DAYS) -> dict[int, int]:
    """Tüm kanallardaki (Trendyol, HB, Amazon, web) gerçek satış adedi, ürün başına."""
    return {int(r.product_id): int(r.qty) for r in conn.execute(text("""
        SELECT i.product_id, SUM(i.quantity) AS qty FROM order_items i JOIN orders o ON o.id = i.order_id
         WHERE i.product_id IS NOT NULL AND o.internal_status NOT IN ('cancelled', 'returned')
           AND o.order_date > NOW() - make_interval(days => :d)
         GROUP BY i.product_id"""), {"d": days})}


def group_variants(variants: list[Variant], sales: dict[int, int] | None = None) -> list[Group]:
    groups: dict[str, Group] = {}
    for v in variants:
        groups.setdefault(v.group_key, Group(key=v.group_key, variants=[])).variants.append(v)
    sales = sales or {}
    for g in groups.values():
        g.sold = sum(sales.get(v.id, 0) for v in g.variants)
    return list(groups.values())


@dataclass
class Snapshot:
    groups: list[Group]
    has_sales: bool
    loaded_at: float = field(default_factory=time.monotonic)

    def by_category(self) -> dict[str, dict]:
        cats: dict[str, dict] = {}
        for g in self.groups:
            label = g.category
            if not label:
                continue
            c = cats.setdefault(slugify(label), {"slug": slugify(label), "label": label, "count": 0, "groups": []})
            c["count"] += 1
            c["groups"].append(g)
        return dict(sorted(cats.items(), key=lambda kv: -kv[1]["count"]))


_cache: dict[str, Snapshot] = {}
_lock = threading.Lock()


def snapshot(conn: Connection, *, fresh: bool = False) -> Snapshot:
    with _lock:
        snap = _cache.get("s")
        if snap and not fresh and time.monotonic() - snap.loaded_at < SNAPSHOT_TTL:
            return snap
    sales = _sales(conn)
    groups = group_variants(_variants(conn), sales)
    snap = Snapshot(groups=groups, has_sales=any(g.sold for g in groups))
    with _lock:
        _cache["s"] = snap
    return snap


def invalidate() -> None:
    with _lock:
        _cache.clear()


# ------------------------------------------------------------------ listeler
SORTS = {
    "onerilen": "Önerilen",
    "cok-satan": "En çok satan",
    "yeni": "En yeni",
    "fiyat-artan": "Fiyat: düşükten yükseğe",
    "fiyat-azalan": "Fiyat: yüksekten düşüğe",
}


def sort_groups(groups: list[Group], sort: str) -> list[Group]:
    def ts(g: Group) -> float:
        return g.created_at.timestamp() if g.created_at else 0.0

    if sort == "cok-satan":
        return sorted(groups, key=lambda g: (not g.in_stock, -g.sold, -ts(g)))
    if sort == "yeni":
        return sorted(groups, key=lambda g: (not g.in_stock, -ts(g), -g.id))
    if sort == "fiyat-artan":
        return sorted(groups, key=lambda g: (not g.in_stock, g.price))
    if sort == "fiyat-azalan":
        return sorted(groups, key=lambda g: (not g.in_stock, -g.price))
    # önerilen: öne çıkan → satış → stok → yeni
    return sorted(groups, key=lambda g: (not g.in_stock, not g.featured, min(v.sort_order for v in g.variants),
                                         -g.sold, -ts(g)))


def bestsellers(snap: Snapshot, limit: int = 8) -> list[Group]:
    """Gerçek satış verisine göre; satış yoksa boş liste (sahte 'çok satan' yok)."""
    sold = [g for g in snap.groups if g.sold > 0 and g.in_stock]
    return sorted(sold, key=lambda g: -g.sold)[:limit]


def new_arrivals(snap: Snapshot, limit: int = 8) -> list[Group]:
    return sort_groups([g for g in snap.groups if g.in_stock], "yeni")[:limit]


def featured(snap: Snapshot, limit: int = 8) -> list[Group]:
    return sort_groups([g for g in snap.groups if g.in_stock], "onerilen")[:limit]


def search(snap: Snapshot, q: str, limit: int | None = None) -> list[Group]:
    tokens = [t for t in fold(q).split() if t]
    if not tokens:
        return []
    hits = [g for g in snap.groups if all(t in g.search_text for t in tokens)]
    hits = sort_groups(hits, "onerilen")
    return hits[:limit] if limit else hits


def hero(conn: Connection, snap: Snapshot) -> tuple[Group | None, str]:
    """Hero ürünü ve seçim gerekçesi.

    Öncelik: 1) panelden seçilen ürün  2) son 90 günde tüm kanallarda en çok satan (stokta)
    3) satış verisi yoksa stokta olup en çok görseli olan (görsel kalitesi/premium algı için vekil ölçü),
    eşitlikte daha yüksek fiyatlı  4) herhangi bir ürün."""
    cfg = store_config.load(conn)
    in_stock = [g for g in snap.groups if g.in_stock]
    if cfg.hero_product_id:
        for g in snap.groups:
            if any(v.id == cfg.hero_product_id for v in g.variants) and g.in_stock:
                return g, "Panelden seçildi"
    best = bestsellers(snap, 1)
    if best:
        return best[0], f"Son {BESTSELLER_DAYS} günde tüm kanallarda en çok satan ({best[0].sold} adet)"
    if in_stock:
        g = sorted(in_stock, key=lambda g: (-len(g.images), -g.price, g.id))[0]
        return g, "Satış verisi yok: stokta olup en çok görseli olan ürün"
    if snap.groups:
        return snap.groups[0], "Stokta ürün yok"
    return None, "Yayında ürün yok"


def product_group(conn: Connection, product_id: int) -> Group | None:
    """Ürün sayfası: taze (önbelleksiz) stokla ürün ve aynı modelin diğer varyantları."""
    key = conn.execute(text("""
        SELECT COALESCE(NULLIF(p.model_code, ''), NULLIF(sp.parent_code, ''), 'p' || p.id)
          FROM products p LEFT JOIN LATERAL (
              SELECT x.parent_code FROM supplier_products x WHERE x.product_id = p.id
               ORDER BY (x.supplier_id = p.preferred_supplier_id) DESC NULLS LAST, x.id LIMIT 1) sp ON TRUE
         WHERE p.id = :id"""), {"id": product_id}).scalar()
    if key is None:
        return None
    variants = _variants(conn, """COALESCE(NULLIF(p.model_code, ''), NULLIF(sp.parent_code, ''), 'p' || p.id) = :key""", key=key)
    if not any(v.id == product_id for v in variants):
        return None
    g = group_variants(variants, _sales(conn))[0]
    return g


def variants_by_ids(conn: Connection, ids: list[int]) -> dict[int, Variant]:
    if not ids:
        return {}
    return {v.id: v for v in _variants(conn, "p.id = ANY(:ids)", ids=[int(i) for i in ids])}
