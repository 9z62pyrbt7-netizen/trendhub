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
    "finance.service_fee_per_order": ("money", "Sipariş başı hizmet bedeli (₺)"),
    "finance.default_shipping_cost": ("money", "Kargo ücreti bilinmiyorsa varsayılan (₺)"),
    "finance.include_vat": ("bool", "Tutarlar KDV dahil"),
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
    return value


def set_value(conn: Connection, key: str, value, user_id: int | None) -> None:
    conn.execute(text("""
        INSERT INTO app_settings(key, value, updated_by, updated_at)
        VALUES (:k, CAST(:v AS JSONB), :u, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                                        updated_at = NOW()
    """), {"k": key, "v": json.dumps(value), "u": user_id})
