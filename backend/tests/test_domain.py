from decimal import Decimal

import pytest

from app.domain import order_status as S
from app.domain.finance import Breakdown, LineInput, OrderCosts, allocate, compute_order


def test_all_required_statuses_have_turkish_labels():
    assert [S.LABELS_TR[s] for s in S.ALL_STATUSES] == [
        "Yeni", "Hazırlanıyor", "Tedarikçiye Aktarıldı", "Kargoya Verilmeyi Bekliyor", "Kargoda",
        "Teslim Edildi", "İptal", "İade", "Hata / İnceleme Gerekiyor"]


@pytest.mark.parametrize("current,incoming,expected,changed", [
    (None, S.NEW, S.NEW, True),
    (S.NEW, S.NEW, S.NEW, False),
    (S.NEW, S.SHIPPED, S.SHIPPED, True),
    (S.SENT_TO_SUPPLIER, S.PREPARING, S.SENT_TO_SUPPLIER, False),   # geri gitmez
    (S.SENT_TO_SUPPLIER, S.AWAITING_SHIPMENT, S.AWAITING_SHIPMENT, True),
    (S.SHIPPED, S.CANCELLED, S.CANCELLED, True),                     # iptal her zaman uygulanır
    (S.DELIVERED, S.RETURNED, S.RETURNED, True),
    (S.NEEDS_REVIEW, S.SHIPPED, S.NEEDS_REVIEW, False),              # manuel çözülene kadar kalır
    (S.CANCELLED, S.SHIPPED, S.NEEDS_REVIEW, True),                  # olağan dışı -> inceleme
])
def test_sync_status_resolution(current, incoming, expected, changed):
    d = S.resolve_sync_status(current, incoming)
    assert (d.status, d.changed) == (expected, changed)


def test_manual_transitions():
    S.check_manual_transition(S.NEW, S.SENT_TO_SUPPLIER)
    with pytest.raises(S.InvalidTransition):
        S.check_manual_transition(S.DELIVERED, S.NEW)
    with pytest.raises(S.InvalidTransition):
        S.check_manual_transition(S.NEW, "yok")


def test_allocate_never_loses_cents():
    parts = allocate(Decimal("10.00"), [Decimal("1"), Decimal("1"), Decimal("1")])
    assert sum(parts) == Decimal("10.00")
    assert parts == [Decimal("3.33"), Decimal("3.33"), Decimal("3.34")]
    assert allocate(Decimal("5"), [Decimal("0"), Decimal("0")]) == [Decimal("2.50"), Decimal("2.50")]


def test_compute_order_breakdown_and_margin():
    lines = [
        LineInput(quantity=2, unit_price=Decimal("150"), unit_cost=Decimal("60")),              # ciro 300
        LineInput(quantity=1, unit_price=Decimal("100"), unit_cost=Decimal("40"),
                  commission=Decimal("12.50")),                                                  # ciro 100
    ]
    of = compute_order(lines, OrderCosts(shipping=Decimal("40"), service_fee=Decimal("8"),
                                         advertising=Decimal("20")), Decimal("0.20"))
    a, b = of.lines
    assert a.revenue == Decimal("300.00") and a.product_cost == Decimal("120.00")
    assert a.commission == Decimal("60.00")          # tahmin: %20
    assert b.commission == Decimal("12.50")          # gerçek değer
    assert a.shipping == Decimal("30.00") and b.shipping == Decimal("10.00")   # ciro payına göre
    assert a.service_fee + b.service_fee == Decimal("8.00")
    assert a.advertising + b.advertising == Decimal("20.00")
    t = of.total
    assert t.revenue == Decimal("400.00")
    assert t.net_profit == Decimal("400") - (Decimal("160") + Decimal("72.50") + 8 + 40 + 20)
    assert t.margin == (t.net_profit / t.revenue).quantize(Decimal("0.0001"))
    assert of.is_estimate is True


def test_refund_reduces_profit_and_zero_revenue_margin_is_none():
    of = compute_order([LineInput(quantity=1, unit_price=Decimal("100"), unit_cost=Decimal("50"),
                                  commission=Decimal("0"), refund_amount=Decimal("100"))], OrderCosts())
    assert of.total.net_profit == Decimal("-50.00")
    assert Breakdown().margin is None


def test_estimated_vat_payable_uses_decimal_and_vat_inclusive_amounts():
    from app.domain.finance import estimated_vat_payable, vat_portion
    assert vat_portion(Decimal("120"), 20) == Decimal("20.00")
    assert vat_portion(Decimal("110"), 10) == Decimal("10.00")
    assert vat_portion(Decimal("100"), 0) == Decimal("0")
    # satış 240 (KDV 40), maliyet 120 (KDV 20) -> ödenecek tahmini KDV 20
    assert estimated_vat_payable(Decimal("240"), Decimal("120"), Decimal("0"), 20) == Decimal("20.00")
    # iade edilen satışın KDV'si düşülür
    assert estimated_vat_payable(Decimal("240"), Decimal("0"), Decimal("240"), 20) == Decimal("0.00")
    # float birikimi yok: 0.1 + 0.2 gibi tutarlar kuruş hassasiyetinde kalır
    assert estimated_vat_payable(Decimal("0.30"), Decimal("0"), Decimal("0"), 20) == Decimal("0.05")
