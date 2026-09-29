"""Kargoya verilme günü tahmini (yalnızca bilgilendirme) — saf fonksiyonlar.

TrendHub'ın KENDİ tahminidir: sipariş statüsünü değiştirmez, pazaryerine veya tedarikçiye
hiçbir istek göndermez, canlı sipariş otomasyonunun davranışını etkilemez.

Çanta Bayim kuralı (kullanıcının verdiği iş kuralı, Türkiye saati / Europe/Istanbul):
  * sipariş saati 11:00'dan ÖNCE        -> aynı gün kargoya verilir
  * sipariş saati 12:00 ve SONRASI      -> ertesi gün kargoya verilir
  * 11:00:00 – 11:59:59 arası           -> TANIMSIZ: doğrulanmış kural yok, "Kargo günü belirsiz"
Hafta sonu / resmî tatil için doğrulanmış kural yoktur; iş günü hesabı YAPILMAZ, kural takvim
günü olarak aynen uygulanır ve sonuç "tahmini" olarak işaretlenir.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TR_TZ = ZoneInfo("Europe/Istanbul")
MONTHS_TR = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]
PENDING_STATUSES = {"new", "preparing", "sent_to_supplier", "awaiting_shipment"}


@dataclass(frozen=True)
class CutoffRule:
    supplier_code: str
    supplier_name: str
    same_day_before: time      # bu saatten ÖNCE -> aynı gün
    next_day_from: time        # bu saat ve SONRASI -> ertesi gün

    @property
    def note(self) -> str:
        a, b = self.same_day_before.strftime("%H:%M"), self.next_day_from.strftime("%H:%M")
        gap = (f" {a}–{(datetime.combine(date.min, self.next_day_from) - timedelta(seconds=1)).strftime('%H:%M')} "
               "arası için doğrulanmış kural yok.") if self.same_day_before < self.next_day_from else ""
        return f"{self.supplier_name}: {a} öncesi aynı gün, {b} ve sonrası ertesi gün.{gap}"


# Tedarikçi koduna göre kural. Yeni tedarikçi kuralı ancak doğrulanınca buraya eklenir.
RULES: dict[str, CutoffRule] = {
    "canta_bayim": CutoffRule("canta_bayim", "Çanta Bayim", time(11, 0), time(12, 0)),
}


def date_label(d: date) -> str:
    return f"{d.day} {MONTHS_TR[d.month - 1]}"


def to_tr(dt: datetime) -> datetime:
    """UTC (veya herhangi bir saat dilimli) zamanı Türkiye saatine çevirir.
    Saat dilimi olmayan değer UTC kabul edilir (veritabanı TIMESTAMPTZ -> UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TR_TZ)


def parse_hhmm(v: str | None, default: time) -> time:
    try:
        h, m = str(v).split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return default


def rule_from_settings(supplier_code: str, supplier_name: str, same_day_before: str | None,
                       next_day_from: str | None) -> CutoffRule:
    """Ayarlar ekranındaki kargo kesim saatlerinden kural (varsayılan 11:00 / 12:00)."""
    return CutoffRule(supplier_code, supplier_name, parse_hhmm(same_day_before, time(11, 0)),
                      parse_hhmm(next_day_from, time(12, 0)))


def is_shipping_day(d: date) -> bool:
    """İş takvimi kancası. Doğrulanmış bir tedarikçi/iş takvimi olmadığı için her gün kargo günü sayılır;
    ileride tatil/hafta sonu takvimi buraya bağlanır (tahmin UYDURULMAZ)."""
    return True


def plan(order_date: datetime | None, now: datetime, *, status: str | None = None,
         supplier_code: str | None = None, rule: CutoffRule | None = None) -> dict:
    """Siparişin tahmini kargoya verilme günü.

    Dönen `code`: today | tomorrow | later | past | unknown | not_applicable | no_rule | no_date
    """
    base = {"is_estimate": True, "supplier_code": supplier_code, "date": None, "date_label": None,
            "window": None, "rule_note": None, "weekend_note": None}
    if status is not None and status not in PENDING_STATUSES:
        return {**base, "code": "not_applicable", "label": "—"}
    rule = rule or RULES.get(supplier_code or "")
    if rule is None or not supplier_code:
        return {**base, "code": "no_rule", "label": "Kargo planı tanımlı değil"}
    base["rule_note"] = rule.note
    if order_date is None:
        return {**base, "code": "no_date", "label": "Sipariş saati yok"}
    local = to_tr(order_date)
    t = local.timetz().replace(tzinfo=None)
    if t < rule.same_day_before:
        ship, window = local.date(), "before_cutoff"
    elif t >= rule.next_day_from:
        ship, window = local.date() + timedelta(days=1), "after_cutoff"
    else:
        return {**base, "code": "unknown", "window": "undefined", "label": "Kargo günü belirsiz",
                "order_time_tr": local.strftime("%H:%M")}
    today = to_tr(now).date()
    if ship == today:
        code, prefix = "today", "Bugün kargoya verilecek"
    elif ship == today + timedelta(days=1):
        code, prefix = "tomorrow", "Yarın kargoya verilecek"
    elif ship < today:
        code, prefix = "past", "Planlanan kargo günü geçti"
    else:
        code, prefix = "later", "Kargoya verilecek"
    return {**base, "code": code, "window": window, "date": ship.isoformat(), "date_label": date_label(ship),
            "label": f"{prefix} · {date_label(ship)}", "order_time_tr": local.strftime("%H:%M"),
            "weekend_note": ("Hafta sonu/resmî tatil hesaba katılmadı (doğrulanmış kural yok)"
                             if ship.weekday() >= 5 else None)}
