"""AI ayarları, eşikler, acil durdurma ve izin modeli."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Connection

from .. import app_settings

TZ = ZoneInfo("Europe/Istanbul")

DEFAULT_THRESHOLDS: dict = {
    "analysis_days": 30,            # ürün ekonomisi penceresi
    "min_units_for_data": 3,        # altında NO_DATA
    "star_margin": 0.20,            # reklam sonrası net marj
    "star_min_profit": 1000,        # pencere içi net kâr (TL)
    "profitable_margin": 0.08,
    "ads_window_days": 7,
    "ads_min_clicks": 100,          # altında reklam kararı INSUFFICIENT_DATA / TEST
    "ads_min_spend": 300,
    "ads_target_margin_after_ads": 0.10,   # bütçe artırma için reklam sonrası en az net marj
    "ads_max_budget_step": 0.30,    # tek adımda en fazla bütçe değişimi (risk motoru)
    "ads_pause_loss": 500,          # pencere içi reklam net zararı bu tutarı geçerse PAUSE
    "stockout_days": 7,             # bu kadar günden az stok = tükenme riski
    "dead_stock_days": 60,
    "reserve_months_opex": 1,       # nakit rezervi: aylık sabit gider × N
    "data_stale_hours": 24,         # sipariş senkronu bu kadar eskiyse veri bayat
    "ads_data_stale_days": 3,
    "finance_stale_hours": 36,      # pazaryeri finans (cari hesap) verisi bu kadar eskiyse para harcayan öneri onaylanmaz
    "returns_stale_hours": 12,
    "questions_stale_hours": 12,
    "cash_stale_days": 7,           # elle girilen kasa bilgisi bu kadar gün güncellenmediyse uyarı
    "payout_window_days": 7,        # vadesi bu kadar gün içinde olan ödenmemiş hakediş = "bekleyen ödeme"
    "cx_min_sample": 10,            # ürün başına yüzde hesaplamak için en az soru/iade sayısı
    # ---- para güvenliği (guardrails) ----
    "daily_ad_budget_limit": 1500,  # aktif kampanyaların toplam günlük bütçesi + önerilen artışlar bu tutarı aşamaz (TL)
    "advertising_max_capital_per_proposal": 5000,   # reklam ajanının tek öneride isteyebileceği en fazla sermaye
    "inventory_max_capital_per_proposal": 20000,    # stok ajanının tek öneride isteyebileceği en fazla sermaye
    "min_unit_profit": 20,          # ürün başına en az net kâr (TL) — fiyat/kampanya bu sınırın altına inemez
    "min_net_margin": 0.10,         # en az net marj (fiyat/kampanya tabanı)
    "max_price_change_pct": 0.10,   # tek değişiklikte en fazla fiyat değişimi
    "max_price_changes_per_cycle": 5,   # tek döngüde fiyat değişikliği önerilebilecek en fazla ürün
    "stock_safety_units": 2,        # bu adetten az kullanılabilir stokta reklam/kampanya yok
    "tracking_change_pct": 0.30,    # ürün takibi: 7 gün / önceki 7 gün değişim eşiği
    "tracking_min_units": 5,        # ürün takibi: karşılaştırma için iki dönemden birinde en az adet
    "profit_guard_unknown_max_spend": 1000,   # kârı doğrulanmamış (UNKNOWN) ürüne en fazla test harcaması (TL)
    "unprocessed_order_hours": 24,  # operasyon: bu kadar saattir işlenmeyen sipariş olay açar
    "failed_jobs_incident": 3,      # operasyon: 24 saatte bu kadar başarısız iş olay açar
}
INVENTORY_MODELS = {"dropship": "Dropshipping (stok tedarikçide; stok sermayesi gerekmez)",
                    "own_stock": "Kendi stoğu (stok sermaye bağlar)"}

# İzin modeli: mevcut rollere eşlenir. Ajanlar yalnızca READ + PROPOSE kullanır.
PERMISSIONS = {
    "read": ("viewer", "accountant", "operator", "admin"),
    "propose": ("operator", "admin"),
    "approve": ("admin",),
    "execute": ("admin",),
    "admin": ("admin",),
}


def can(role: str, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, ())


def thresholds(conn: Connection) -> dict:
    raw = app_settings.get(conn, "ai.thresholds", {}) or {}
    out = dict(DEFAULT_THRESHOLDS)
    for k, v in raw.items():
        if k in out and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    return out


def validate_thresholds(values: dict) -> dict:
    out = {}
    for k, v in values.items():
        if k not in DEFAULT_THRESHOLDS:
            raise ValueError(f"Bilinmeyen eşik: {k}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 1_000_000:
            raise ValueError(f"{k}: 0 veya pozitif sayı olmalı")
        if k in ("star_margin", "profitable_margin", "ads_target_margin_after_ads", "ads_max_budget_step", "min_net_margin",
                 "max_price_change_pct", "tracking_change_pct") and v > 1:
            raise ValueError(f"{k}: oran 0–1 arasında olmalı (ör. 0.10 = %10)")
        out[k] = v
    return out


def emergency_stop(conn: Connection) -> bool:
    return bool(app_settings.get(conn, "ai.emergency_stop", False))


def write_allowed(conn: Connection) -> bool:
    """Dış dünyaya yazan her işlem (yayın, tedarikçiye otomatik sipariş, onaylı ajan aksiyonu) bunu kontrol eder."""
    return not emergency_stop(conn)


def enabled(conn: Connection) -> bool:
    return bool(app_settings.get(conn, "ai.enabled", True))


def inventory_model(conn: Connection) -> str:
    m = app_settings.get(conn, "ai.inventory_model", "dropship")
    return m if m in INVENTORY_MODELS else "dropship"


def tl(v) -> str:
    """Türkçe para biçimi: 1.299,90 ₺ (ajan metinlerinde)."""
    q = Decimal(str(v or 0)).quantize(Decimal("0.01"))
    sign = "-" if q < 0 else ""
    whole, frac = f"{abs(q):.2f}".split(".")
    return f"{sign}{int(whole):,}".replace(",", ".") + f",{frac} ₺"


def d(v) -> Decimal:
    return Decimal(str(v or 0))


class Window:
    """Türkiye saatine göre son N günün [start, end) aralığı (DateRange ile aynı arayüz)."""

    def __init__(self, days: int, end_date: date | None = None, start_date: date | None = None):
        end_d = end_date or datetime.now(TZ).date()
        start_d = start_date or end_d - timedelta(days=max(1, int(days)) - 1)
        self.period = "custom"
        self.start_date, self.end_date = start_d, end_d
        self.start = datetime.combine(start_d, time.min, TZ).astimezone(timezone.utc)
        self.end = datetime.combine(end_d + timedelta(days=1), time.min, TZ).astimezone(timezone.utc)
        self.days = (end_d - start_d).days + 1

    def params(self) -> dict:
        return {"start": self.start, "end": self.end, "start_date": self.start_date, "end_date": self.end_date}

    def as_dict(self) -> dict:
        return {"from": self.start_date.isoformat(), "to": self.end_date.isoformat(), "days": self.days}

    def previous(self) -> "Window":
        """Aynı uzunlukta bir önceki dönem."""
        end = self.start_date - timedelta(days=1)
        return Window(self.days, end_date=end)


def today() -> date:
    return datetime.now(TZ).date()
