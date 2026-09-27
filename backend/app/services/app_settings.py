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
}


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
