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
SYNONYMS: dict[str, list[str]] = {
    "supplier_sku": ["urunkodu", "stokkodu", "productcode", "sku", "kod", "code", "urunid", "productid",
                     "urunkartiid", "stockcode", "itemcode", "id"],
    "barcode": ["barkod", "barcode", "ean", "gtin", "upc", "barkodno"],
    "model_code": ["modelkodu", "model", "modelcode", "modelno", "productmaincode", "anaurunkodu"],
    "name": ["urunadi", "urunismi", "ad", "adi", "isim", "name", "title", "productname", "baslik"],
    "category": ["kategori", "kategoriadi", "category", "categoryname", "kategoriyolu", "categorypath"],
    "brand": ["marka", "markaadi", "brand", "brandname", "manufacturer", "uretici"],
    "purchase_price": ["alisfiyati", "bayifiyati", "maliyet", "purchaseprice", "cost", "dealerprice",
                       "bayifiyat", "alis", "tedarikfiyati", "fiyat", "price"],
    "sale_price": ["satisfiyati", "piyasafiyati", "tavsiyefiyat", "listefiyati", "saleprice", "listprice",
                   "retailprice", "psf", "msrp"],
    "currency": ["parabirimi", "doviz", "currency", "dovizcinsi"],
    "stock": ["stok", "stokadedi", "stockquantity", "stock", "quantity", "miktar", "adet", "qty", "inventory"],
    "vat_rate": ["kdv", "kdvorani", "vat", "vatrate", "tax", "taxrate", "vergi"],
    "desi": ["desi", "kargodesi", "dimensionalweight", "volumetricweight"],
    "description": ["aciklama", "urunaciklamasi", "description", "detay", "details", "icerik"],
    "images": ["resim", "resimler", "gorsel", "gorseller", "image", "images", "picture", "pictures",
               "imageurl", "resimurl", "foto", "fotograf"],
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


def suggest_mapping(paths: list[str]) -> dict[str, str]:
    """Önizlemede bulunan alan yollarından eşleştirme ÖNERİSİ üretir.

    Her hedef alan için eşanlamlı listesindeki ilk (en güçlü) eşleşme seçilir; bir kaynak
    yol iki hedefe atanmaz. Sonuç yalnızca öneridir, kullanıcı onayı olmadan kaydedilmez."""
    by_norm: dict[str, list[str]] = {}
    for p in paths:
        by_norm.setdefault(normalize_key(_leaf(p)), []).append(p)
    used: set[str] = set()
    out: dict[str, str] = {}
    for field in FIELD_NAMES:
        for syn in SYNONYMS.get(field, []):
            candidates = [p for p in by_norm.get(syn, []) if p not in used]
            if candidates:
                best = min(candidates, key=lambda p: (p.count("/"), len(p)))
                out[field] = best
                used.add(best)
                break
    return out


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
