"""Panelden düzenlenebilir iş ayarları (app_settings tablosu).

Secret'lar burada TUTULMAZ; yalnızca iş kuralları (komisyon oranı tahmini,
varsayılan kargo ücreti vb.).
"""
import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

# anahtar -> (tip, Türkçe açıklama)
EDITABLE = {
    "finance.commission_rate.trendyol": ("rate", "Trendyol tahmini komisyon oranı"),
    "finance.commission_rate.hepsiburada": ("rate", "Hepsiburada tahmini komisyon oranı"),
    "finance.commission_rate.amazon_tr": ("rate", "Amazon.com.tr tahmini komisyon oranı"),
    "finance.commission_rate.storefront": ("rate", "Trendçantanız Web: ödeme sağlayıcısı (sanal POS) komisyon oranı"),
    "finance.service_fee_per_order": ("money", "Sipariş başı hizmet bedeli (₺)"),
    "finance.default_shipping_cost": ("money", "Kargo ücreti bilinmiyorsa varsayılan (₺)"),
    "finance.include_vat": ("bool", "Tutarlar KDV dahil"),
    "finance.commission_vat_mode": ("choice", "Komisyon oranı ve hakediş komisyonu KDV durumu"),
    "finance.commission_vat_rate": ("percent", "Komisyon faturası KDV oranı (%)"),
    "finance.expense_vat_mode": ("choice", "Kargo / hizmet bedeli / reklam giderleri KDV durumu"),
    "finance.expense_vat_rate": ("percent", "Gider KDV oranı (%)"),
    "finance.fx_rates": ("fx", "Döviz kurları (1 birim = ? TL) — tanımsız döviz maliyeti finansa girmez"),
    "finance.return_product_cost_is_loss": ("bool", "İade edilen ürünün maliyeti zarar sayılsın (stoğa geri dönmüyorsa)"),
    "stock.low_stock_threshold": ("int", "Düşük stok eşiği"),
    "shipping.same_day_before": ("time", "Kargo kesim saati: bu saatten ÖNCE gelen sipariş bugün kargoya verilir"),
    "shipping.next_day_from": ("time", "Bu saat ve SONRASI gelen sipariş yarın kargoya verilir"),
    "shipping_plan.default_supplier_code": ("code", "Kargo planı için varsayılan tedarikçi kodu (boş = kapalı)"),
    "alerts.critical_stock_threshold": ("int", "Kritik stok uyarısı: tedarikçi stoğu bu değer ve altındaysa"),
    "alerts.price_change_pct": ("percent", "Fiyat değişim uyarısı: alış fiyatı bu oranın üzerinde değişirse (%)"),
    "alerts.shipping_overdue_hours": ("int", "Kargo gecikme uyarısı: planlanan günden bu kadar saat sonra"),
    "notifications.in_app": ("bool", "Panel içi bildirimler (zil)"),
}

# Seçimli ayarların geçerli değerleri (arayüzde açılır liste)
CHOICES = {
    "finance.commission_vat_mode": {"unset": "Belirtilmedi (komisyon KDV'si hesaba katılmaz)",
                                    "included": "Komisyon tutarı KDV dahil",
                                    "excluded": "Komisyon KDV hariç (faturada ayrıca KDV eklenir)"},
    "finance.expense_vat_mode": {"unset": "Belirtilmedi (gider KDV'si indirilmez)",
                                 "included": "Giderler KDV dahil (KDV'si indirilir)"},
}

# Ayarlar ekranındaki bölümler (anahtar önekine göre)
GROUPS = {"finance": "Finans varsayılanları", "stock": "Stok", "shipping": "Kargo planı",
          "shipping_plan": "Kargo planı", "alerts": "Uyarılar", "notifications": "Bildirimler"}


def group_of(key: str) -> str:
    return GROUPS.get(key.split(".", 1)[0], "Diğer")


def get_all(conn: Connection) -> dict:
    return {r.key: r.value for r in conn.execute(text("SELECT key, value FROM app_settings"))}


def get(conn: Connection, key: str, default=None):
    v = conn.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": key}).scalar()
    return default if v is None else v


def get_decimal(conn: Connection, key: str, default: str = "0") -> Decimal:
    return Decimal(str(get(conn, key, default)))


def validate(key: str, value):
    if key not in EDITABLE:
        raise ValueError(f"Düzenlenemeyen ayar: {key}")
    kind = EDITABLE[key][0]
    if kind == "rate":
        v = float(value)
        if not 0 <= v <= 1:
            raise ValueError("Oran 0 ile 1 arasında olmalı (ör. 0.2 = %20)")
        return v
    if kind == "money":
        v = float(value)
        if v < 0:
            raise ValueError("Tutar negatif olamaz")
        return v
    if kind == "int":
        v = int(value)
        if v < 0:
            raise ValueError("Değer negatif olamaz")
        return v
    if kind == "time":
        import re
        if not isinstance(value, str) or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
            raise ValueError("Saat SS:DD biçiminde olmalı (ör. 12:00)")
        return value
    if kind == "percent":
        v = float(value)
        if not 0 <= v <= 1000:
            raise ValueError("Yüzde 0 ile 1000 arasında olmalı")
        return v
    if kind == "code":
        import re
        v = str(value or "").strip()
        if v and not re.match(r"^[a-z0-9_\-]{1,50}$", v):
            raise ValueError("Tedarikçi kodu küçük harf, rakam, _ veya - içermeli")
        return v
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("true/false olmalı")
    if kind == "choice":
        if value not in CHOICES[key]:
            raise ValueError("Geçersiz seçim: " + ", ".join(CHOICES[key].values()))
        return value
    if kind == "fx":
        return validate_fx(value)
    return value


def validate_fx(value) -> dict:
    """{"USD": "34.10", "EUR": "37.2"} — 3 harfli para birimi kodu, pozitif kur. TRY tanımlanamaz (her zaman 1)."""
    import re
    if isinstance(value, str):
        value = json.loads(value or "{}")
    if not isinstance(value, dict):
        raise ValueError("Kurlar {\"USD\": 34.1} biçiminde olmalı")
    out = {}
    for k, v in value.items():
        code = str(k).strip().upper()
        if not re.match(r"^[A-Z]{3}$", code) or code == "TRY":
            raise ValueError(f"Geçersiz para birimi: {k}")
        rate = Decimal(str(v))
        if rate <= 0 or rate > 100000:
            raise ValueError(f"{code} kuru pozitif olmalı")
        out[code] = str(rate)
    return out


def fx_rates(conn: Connection) -> dict[str, Decimal]:
    """TRY = 1 ve tanımlı kurlar. Tanımsız döviz için kur YOKTUR (sessizce TL sayılmaz)."""
    raw = get(conn, "finance.fx_rates", {}) or {}
    out = {"TRY": Decimal("1")}
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        try:
            if Decimal(str(v)) > 0:
                out[str(k).upper()] = Decimal(str(v))
        except Exception:  # noqa: BLE001
            continue
    return out


def set_value(conn: Connection, key: str, value, user_id: int | None) -> None:
    conn.execute(text("""
        INSERT INTO app_settings(key, value, updated_by, updated_at)
        VALUES (:k, CAST(:v AS JSONB), :u, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                                        updated_at = NOW()
    """), {"k": key, "v": json.dumps(value), "u": user_id})
