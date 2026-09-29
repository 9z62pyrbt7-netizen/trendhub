"""TrendHub tedarikçi ürün alanları, eşanlamlılar ve hazır şablonlar.

Kod hiçbir tedarikçinin alan adına bağlı değildir. Her tedarikçi için
`supplier_field_mappings` tablosunda "TrendHub alanı <- kaynak yol" eşleşmesi
tutulur. Burada yalnızca:
  * TrendHub tarafındaki hedef alanların listesi,
  * önizlemede otomatik ÖNERİ üretmek için yaygın alan adları (eşanlamlılar),
  * hazır şablonlar (ör. Çanta Bayim) bulunur.
Öneriler kullanıcı onaylayana kadar kaydedilmez.
"""
from __future__ import annotations

import re
import unicodedata

# (alan, Türkçe etiket, tip, zorunlu)
TARGET_FIELDS: list[tuple[str, str, str, bool]] = [
    ("supplier_sku", "Tedarikçi ürün kodu (SKU)", "text", True),
    ("barcode", "Barkod (GTIN/EAN)", "text", False),
    ("model_code", "Model kodu", "text", False),
    ("name", "Ürün adı", "text", True),
    ("category", "Kategori", "text", False),
    ("brand", "Marka", "text", False),
    ("color", "Renk", "text", False),
    ("size", "Beden", "text", False),
    ("variant", "Varyant", "text", False),
    ("purchase_price", "Alış fiyatı", "decimal", False),
    ("sale_price", "Satış fiyatı (tavsiye)", "decimal", False),
    ("currency", "Para birimi", "text", False),
    ("stock", "Stok", "int", False),
    ("vat_rate", "KDV oranı (%)", "decimal", False),
    ("desi", "Desi", "decimal", False),
    ("description", "Açıklama", "text", False),
    ("images", "Görseller", "list", False),
]
FIELD_NAMES = [f[0] for f in TARGET_FIELDS]
FIELD_TYPES = {f[0]: f[2] for f in TARGET_FIELDS}
REQUIRED_FIELDS = [f[0] for f in TARGET_FIELDS if f[3]]

# Yaygın XML/CSV/JSON alan adları (normalize edilmiş: küçük harf, Türkçe karakter ve ayraçsız).
# GÜÇLÜ eşanlamlı → yüksek güven. ZAYIF eşanlamlı (ör. code/id) → düşük güven, kullanıcı kontrol etmeli.
SYNONYMS: dict[str, list[str]] = {
    "supplier_sku": ["urunkodu", "stokkodu", "productcode", "sku", "stockcode", "merchantsku", "urunstokkodu",
                     "variantsku", "varyantkodu", "itemcode"],
    "barcode": ["barkod", "barcode", "ean", "gtin", "upc", "barkodno", "ean13"],
    "model_code": ["modelkodu", "model", "modelcode", "modelno", "productmaincode", "anaurunkodu", "parentcode"],
    "name": ["urunadi", "urunismi", "productname", "name", "title", "baslik", "isim", "adi", "ad"],
    "category": ["kategori", "kategoriadi", "category", "categoryname", "kategoriyolu", "categorypath",
                 "categorytree"],
    "brand": ["marka", "markaadi", "brand", "brandname", "manufacturer", "uretici"],
    "color": ["renk", "renkadi", "color", "colour", "colorname"],
    "size": ["beden", "size", "olcu", "bedenadi", "sizename"],
    "variant": ["varyant", "variant", "variants", "varyasyon", "secenek", "option", "variantname"],
    "purchase_price": ["alisfiyati", "bayifiyati", "bayifiyat", "maliyet", "purchaseprice", "cost", "costprice",
                       "dealerprice", "buyingprice", "alis", "tedarikfiyati", "wholesaleprice", "toptanfiyat",
                       "toptanfiyati"],
    "sale_price": ["satisfiyati", "piyasafiyati", "tavsiyefiyat", "listefiyati", "saleprice", "listprice",
                   "retailprice", "psf", "msrp", "marketprice", "perakendefiyat"],
    "currency": ["parabirimi", "doviz", "currency", "dovizcinsi", "currencycode", "paracinsi"],
    "stock": ["stok", "stokadedi", "stockquantity", "stock", "quantity", "miktar", "adet", "qty", "inventory",
              "stokmiktari"],
    "vat_rate": ["kdv", "kdvorani", "vat", "vatrate", "taxrate", "vergiorani"],
    "desi": ["desi", "kargodesi", "dimensionalweight", "volumetricweight"],
    "description": ["aciklama", "urunaciklamasi", "description", "detay", "details", "icerik", "longdescription"],
    "images": ["resim", "resimler", "gorsel", "gorseller", "image", "images", "picture", "pictures",
               "imageurl", "resimurl", "foto", "fotograf"],
}
# Düşük güvenli eşanlamlılar: öneride gösterilir ama "kontrol edin" uyarısıyla
WEAK_SYNONYMS: dict[str, list[str]] = {
    "supplier_sku": ["kod", "code", "urunid", "productid", "urunkartiid", "id"],
    "vat_rate": ["tax", "vergi"],
}
# Anlamı belirsiz: ALIŞ fiyatı olduğu kullanıcı doğrulamadan kabul edilmez
CONFIRM_SYNONYMS: dict[str, list[str]] = {
    "purchase_price": ["price", "fiyat", "birimfiyat", "unitprice", "fiyati"],
}
NUMBERED_IMAGE = re.compile(r"^(image|images|resim|gorsel|picture|foto|img|imageurl|resimurl)(\d+)$")

# Ürün node'u tespiti için sinyaller (normalize edilmiş yaprak adları)
SIGNALS: dict[str, set[str]] = {
    "name": {"urunadi", "urunismi", "productname", "name", "title", "baslik", "isim"},
    "sku": set(SYNONYMS["supplier_sku"]) | set(WEAK_SYNONYMS["supplier_sku"]),
    "price": set(SYNONYMS["purchase_price"]) | set(SYNONYMS["sale_price"]) | set(CONFIRM_SYNONYMS["purchase_price"]),
    "stock": set(SYNONYMS["stock"]),
    "barcode": set(SYNONYMS["barcode"]),
    "image": set(SYNONYMS["images"]) | {f"image{i}" for i in range(1, 11)} | {f"resim{i}" for i in range(1, 11)},
    "variant": set(SYNONYMS["color"]) | set(SYNONYMS["size"]) | set(SYNONYMS["variant"]),
}

_TR = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g", "ü": "u", "Ü": "u",
                     "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})


def normalize_key(key: str) -> str:
    k = key.translate(_TR)
    k = unicodedata.normalize("NFKD", k).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", k.lower())


def _leaf(path: str) -> str:
    leaf = path.rsplit("/", 1)[-1]
    return leaf.lstrip("@").split("[", 1)[0]


def _depth_key(p: str):
    return (p.count("/"), len(p))


def suggest_mapping_detailed(fields: list[dict]) -> dict[str, dict]:
    """Önizleme alanlarından (field_paths çıktısı) eşleştirme ÖNERİSİ + güven düzeyi.

    confidence: high (güçlü eşanlamlı) · low (genel ad: code/id vb.) · confirm (ör. tek 'Price':
    alış fiyatı olduğu kullanıcı doğrulamadan kabul edilmez). Tüm kayıtlarda aynı değere sahip alan
    SKU olarak önerilmez. Bir kaynak yol iki hedefe atanmaz. Öneri kullanıcı onaylayana kadar kaydedilmez."""
    info = {f["path"]: f for f in fields}
    by_norm: dict[str, list[str]] = {}
    for p in info:
        by_norm.setdefault(normalize_key(_leaf(p)), []).append(p)
    used: set[str] = set()
    out: dict[str, dict] = {}

    def ok(field, p):
        f = info.get(p, {})
        if p in used:
            return False
        if field == "supplier_sku" and (f.get("all_same") or (f.get("fill_rate") is not None and f["fill_rate"] < 0.5)):
            return False
        return True

    def pick(field, syns, confidence, reason):
        for syn in syns:
            cands = [p for p in by_norm.get(syn, []) if ok(field, p)]
            if cands:
                best = min(cands, key=_depth_key)
                used.add(best)
                out[field] = {"path": best, "confidence": confidence, "reason": reason,
                              "needs_confirmation": confidence == "confirm"}
                return True
        return False

    numbered = sorted(((int(m.group(2)), p) for p in info
                       if (m := NUMBERED_IMAGE.match(normalize_key(_leaf(p))))), key=lambda x: x[0])
    for field in FIELD_NAMES:
        if field == "images" and numbered:
            paths = [p for _, p in numbered]
            used.update(paths)
            out["images"] = {"path": ",".join(paths), "confidence": "high", "needs_confirmation": False,
                             "reason": f"Numaralı görsel alanları birleştirildi ({len(paths)} alan)"}
            continue
        if pick(field, SYNONYMS.get(field, []), "high", "Alan adı güçlü eşleşme"):
            continue
        if pick(field, WEAK_SYNONYMS.get(field, []), "low", "Genel alan adı; doğru alan olduğunu kontrol edin"):
            continue
        pick(field, CONFIRM_SYNONYMS.get(field, []), "confirm",
             "Anlamı belirsiz fiyat alanı: bu alanın TEDARİKÇİ ALIŞ FİYATI olduğunu doğrulayın")
    rejected = [p for p in by_norm.get("code", []) + by_norm.get("kod", []) + by_norm.get("id", [])
                if info.get(p, {}).get("all_same")]
    if rejected and "supplier_sku" not in out:
        out["_notes"] = {"sku": "Tüm ürünlerde aynı değere sahip alanlar SKU olarak önerilmedi: " + ", ".join(rejected)}
    return out


def suggest_mapping(paths: list[str]) -> dict[str, str]:
    """Geriye uyumlu basit öneri: yalnızca {alan: yol}."""
    d = suggest_mapping_detailed([{"path": p} for p in paths])
    return {k: v["path"] for k, v in d.items() if not k.startswith("_")}


# Hazır şablonlar. Alan eşleştirmesi şablonda SABİT DEĞİLDİR: tedarikçinin gerçek
# XML'i önizlemede okunur ve eşanlamlılardan öneri üretilir; kullanıcı onaylar.
# Böylece doğrulanmamış bir alan adı "doğru" varsayılmaz.
PRESETS: dict[str, dict] = {
    "canta_bayim": {
        "code": "canta_bayim",
        "name": "Çanta Bayim",
        "integration_type": "xml",
        "sync_interval_minutes": 60,
        "stock_rules": {"buffer": 0, "min_stock": 1},
        "note": "İlk tedarikçi şablonu. XML alan adları önizleme ile okunup otomatik önerilir; "
                "kaydetmeden önce eşleştirmeyi kontrol edin.",
    },
}
