"""V2.1 — gerçek Trendyol verisi: finans (gerçek/tahmin), hakediş ≠ nakit, iade, soru, webhook, iç olaylar, hata durumları.

Trendyol API'si sahte HTTP katmanıyla (httpx.MockTransport) taklit edilir; yanıt alanları resmî dokümantasyondaki
şemadır. Connector, eşleme, veritabanı, olay kuyruğu, risk motoru, sermaye motoru ve CEO sohbeti GERÇEK kod yollarıdır.
"""
import base64
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from tests.test_ai import H, campaign, cash, product, sell

pytestmark = pytest.mark.usefixtures("engine")
NOW = datetime.now(timezone.utc)
SELLER = "1"


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture
def conn(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        yield c


@pytest.fixture
def api(client_factory):
    c, login = client_factory
    login("admin", "Admin-Password-123")
    return c


class FakeTrendyol:
    """Resmî uç nokta yollarıyla sahte Trendyol. routes: yol sonu → yanıt (dict/list) | (status, body, headers) | callable."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.calls.append((request.method, path, params))
        for suffix, resp in self.routes.items():
            if path.endswith(suffix):
                if callable(resp):
                    resp = resp(request, params)
                if isinstance(resp, tuple):
                    status, body, headers = (resp + ({},))[:3]
                    return httpx.Response(status, json=body, headers=headers)
                return httpx.Response(200, json=resp)
        return httpx.Response(404, json={"errors": [{"message": "not found"}]})

    def connector(self, **extra):
        from app.config import Settings
        from app.connectors.trendyol import TrendyolConnector
        s = Settings(database_url="postgresql://x@y/z", trendyol_seller_id=SELLER, trendyol_api_key="k",
                     trendyol_api_secret="s", trendyol_extended_read=True, trendyol_rate_per_minute=100000, **extra)
        return TrendyolConnector(s, transport=httpx.MockTransport(self.handler), sleep=self.sleeps.append)


@pytest.fixture
def ty(monkeypatch):
    fake = FakeTrendyol()
    from app.services.platform import smoke, sync
    monkeypatch.setattr(sync, "get_connector", lambda code, settings=None: fake.connector())
    monkeypatch.setattr(smoke, "get_connector", lambda code, settings=None: fake.connector())
    return fake


def run(engine, job):
    from app.services.platform import sync
    return sync.run(engine, job)


def store(c) -> int:
    return c.execute(text("SELECT s.id FROM stores s JOIN marketplaces m ON m.id = s.marketplace_id WHERE m.code = 'trendyol' LIMIT 1")).scalar() \
        or c.execute(text("INSERT INTO stores(marketplace_id, name, external_id) SELECT id, 'TY', :e FROM marketplaces WHERE code = 'trendyol' RETURNING id"),
                     {"e": SELLER}).scalar()


def order(c, number, pid, *, price="1000", qty=1, days_ago=1, status="delivered", customer=None):
    from app.services.finance_service import recalculate_order
    oid = c.execute(text("""INSERT INTO orders(store_id, external_order_id, status, internal_status, order_date, customer_name)
                            VALUES (:s, :n, 'Delivered', :st, NOW() - make_interval(days => :d), :cn) RETURNING id"""),
                    {"s": store(c), "n": number, "st": status, "d": days_ago, "cn": customer}).scalar()
    c.execute(text("""INSERT INTO order_items(order_id, product_id, external_line_id, sku, barcode, product_name, quantity, unit_price, vat_rate)
                      SELECT :o, p.id, :l, p.sku, p.barcode, p.name, :q, :pr, 20 FROM products p WHERE p.id = :p"""),
              {"o": oid, "p": pid, "l": f"L-{number}", "q": qty, "pr": price})
    recalculate_order(c, oid)
    return oid


def settlement(id_, ttype, number, barcode, *, credit=0, debt=0, commission=None, paid=None, pay_in_days=5, days_ago=1):
    return {"id": id_, "transactionDate": ms(NOW - timedelta(days=days_ago)), "barcode": barcode, "transactionType": ttype,
            "receiptId": None, "description": ttype, "debt": debt, "credit": credit, "paymentPeriod": 7,
            "commissionRate": 15, "commissionAmount": commission,
            "sellerRevenue": None if commission is None else (credit - debt - commission if credit else -(debt - commission)),
            "orderNumber": number, "paymentOrderId": paid, "paymentDate": ms(NOW + timedelta(days=pay_in_days)),
            "sellerId": 1, "storeName": "Mağaza", "storeAddress": "Satıcı adresi", "shipmentPackageId": 55}


def finance_routes(fake, sales: list[dict], others: list[dict] | None = None):
    def pick(src, params):   # Trendyol gibi: tür + transactionDate aralığı (startDate ≤ t < endDate)
        return [r for r in src if r["transactionType"] == params["transactionType"]
                and int(params["startDate"]) <= r["transactionDate"] < int(params["endDate"])]

    def st(req, params):
        rows_ = pick(sales, params)
        return {"content": rows_, "totalPages": 1, "totalElements": len(rows_)}

    def of(req, params):
        return {"content": pick(others or [], params), "totalPages": 1}
    fake.routes["/settlements"] = st
    fake.routes["/otherfinancials"] = of


# ======================================================================== FINANS: gerçek vs tahmin
def test_finance_actual_commission_replaces_estimate(engine, conn, ty):
    from app.services.ai.chat import provenance_lines
    from app.services.platform import finance, sync
    p1 = product(conn, "FIN-1", cost="200", price="1000")
    p2 = product(conn, "FIN-2", cost="200", price="1000")
    o1 = order(conn, "TY-1001", p1)
    o2 = order(conn, "TY-1002", p2)
    est = conn.execute(text("SELECT commission, net_profit, finance_is_estimate FROM orders WHERE id = :i"), {"i": o1}).one()
    assert est.commission == Decimal("200.00") and est.finance_is_estimate          # %20 oran TAHMİNİ
    pv = finance.profit_provenance(conn, NOW - timedelta(days=3), NOW + timedelta(days=1))
    assert pv["status"] == "ESTIMATED" and pv["actual_commission_items"] == 0

    finance_routes(ty, [settlement("S1", "Sale", "TY-1001", "FIN-1", credit=1000, commission=150)])
    out = run(engine, sync.FINANCE_SYNC)
    assert out["settlements"] == 1 and out["events"]["processed"] >= 1
    act = conn.execute(text("SELECT commission, net_profit FROM orders WHERE id = :i"), {"i": o1}).one()
    assert act.commission == Decimal("150.00")                                       # GERÇEK Trendyol kesintisi
    assert act.net_profit == est.net_profit + Decimal("50.00")
    tx = conn.execute(text("SELECT source, kind, amount FROM financial_transactions WHERE order_id = :i"), {"i": o1}).all()
    assert [(t.source, t.kind, t.amount) for t in tx] == [("trendyol_finance", "commission", Decimal("150.00"))]
    # Finans kaydı olmayan sipariş tahminde kalır
    assert conn.execute(text("SELECT commission FROM orders WHERE id = :i"), {"i": o2}).scalar() == Decimal("200.00")
    pv = finance.profit_provenance(conn, NOW - timedelta(days=3), NOW + timedelta(days=1))
    assert pv["status"] == "PARTIAL" and pv["actual_commission_items"] == 1 and pv["estimated_commission_items"] == 1
    lines = "\n".join(provenance_lines(conn, NOW - timedelta(days=3), NOW + timedelta(days=1)))
    assert "satış: Trendyol Orders" in lines and "1/2 kalem gerçek, kalanı TAHMİN" in lines and "reklam: elle girilen" in lines
    ev = conn.execute(text("SELECT event_type, status, source FROM platform_events")).all()
    assert [(e.event_type, e.status, e.source) for e in ev] == [("FINANCE_TRANSACTION_CREATED", "done", "polling")]
    st = conn.execute(text("SELECT status, record_count, last_success_at FROM sync_state WHERE resource = 'finance'")).one()
    assert st.status == "CONNECTED" and st.record_count == 1 and st.last_success_at is not None
    assert all(m == "GET" for m, _p, _q in ty.calls)                                # yalnızca okuma


def test_event_idempotency_finance_sync_twice(engine, conn, ty):
    from app.services.platform import events, sync
    pid = product(conn, "IDEM-1", cost="100", price="500")
    oid = order(conn, "TY-3001", pid, price="500")
    finance_routes(ty, [settlement("S9", "Sale", "TY-3001", "IDEM-1", credit=500, commission=75)])
    run(engine, sync.FINANCE_SYNC)
    first = conn.execute(text("SELECT net_profit FROM orders WHERE id = :i"), {"i": oid}).scalar()
    run(engine, sync.FINANCE_SYNC)
    assert conn.execute(text("SELECT COUNT(*) FROM financial_transactions")).scalar() == 1
    assert conn.execute(text("SELECT COUNT(*) FROM marketplace_finance_entries")).scalar() == 1
    assert conn.execute(text("SELECT COUNT(*) FROM platform_events")).scalar() == 1
    assert conn.execute(text("SELECT net_profit FROM orders WHERE id = :i"), {"i": oid}).scalar() == first
    with engine.begin() as c:
        assert events.emit(c, source="polling", marketplace="trendyol", event_type="QUESTION_CREATED", dedupe_key="q:1") is not None
        assert events.emit(c, source="polling", marketplace="trendyol", event_type="QUESTION_CREATED", dedupe_key="q:1") is None
    # Aynı olayın ikinci kez işlenmesi de sonucu değiştirmez (işleyiciler idempotent)
    conn.execute(text("UPDATE platform_events SET status = 'pending', next_attempt_at = NOW() WHERE event_type = 'FINANCE_TRANSACTION_CREATED'"))
    events.process_pending(engine)
    assert conn.execute(text("SELECT net_profit FROM orders WHERE id = :i"), {"i": oid}).scalar() == first
    assert conn.execute(text("SELECT COUNT(*) FROM financial_transactions")).scalar() == 1


# ======================================================================== HAKEDİŞ ≠ NAKİT
def test_pending_payout_is_not_available_cash(engine, conn, ty, api):
    from app.services.ai import proposals
    from app.services.ai.chat import rules_answer
    from app.services.platform import sync
    cash(conn, "8000")
    finance_routes(ty, [settlement("P1", "Sale", "TY-X", "B-X", credit=20000, commission=3000, pay_in_days=3)])
    run(engine, sync.FINANCE_SYNC)
    cap = api.get("/api/ai/capital").json()
    cs = cap["cash_structure"]
    assert Decimal(cs["available_cash"]["amount"]) == Decimal("8000.00")
    assert Decimal(cs["pending_marketplace_payout"]["amount"]) == Decimal("17000.00")
    assert cs["pending_marketplace_payout"]["kind"] == "ACTUAL"
    assert Decimal(cs["total_money"]["amount"]) == Decimal("25000.00")
    assert Decimal(cap["usable"]) == Decimal("8000.00") == Decimal(cs["deployable_capital"]["amount"])
    ans = rules_answer(conn, "Şu anda ne kadar sermaye kullanabilirim?")[0]
    assert "8.000,00 ₺" in ans and "17.000,00 ₺" in ans and "harcanabilir sayılmadı" in ans
    assert "25.000" not in ans                                       # "25.000 TL harcanabilir" DEMEZ
    pay = rules_answer(conn, "Trendyol hakediş ne zaman yatacak?")[0]
    assert "17.000,00 ₺" in pay and "harcanabilir sermaye sayılmaz" in pay and "Trendyol Finance" in pay
    # 10.000 TL gerektiren öneri: kasada 8.000 var → sermaye aşımı (hakediş sayılmaz)
    pid = product(conn, "CAP-P", cost="100", price="500")
    cid = campaign(conn, "Cap", [pid], budget="300", spend_per_day="300", revenue_per_day="3000", clicks=150)
    with engine.begin() as c:
        prop = proposals.propose(c, agent="advertising", action_type="ads.increase_budget", entity_type="campaign", entity_id=cid,
                                 title="artır", reason="x", evidence={},
                                 params={"current_daily_budget": "300", "new_daily_budget": "390", "delta_per_day": "90"},
                                 required_capital=10000, capital_category="advertising")
    checks = conn.execute(text("SELECT risk_checks FROM ai_proposals WHERE id = :i"), {"i": prop}).scalar()
    assert "capital_exceeded" in {x["code"] for x in checks}
    k = api.get("/api/ai/overview").json()["kpis"]
    assert Decimal(k["pending_payout"]) == Decimal("17000.00") and Decimal(k["cash_usable"]) == Decimal("8000.00")


def test_brief_priority_profit_then_cash(engine, conn, ty, api):
    from app.services.ai.ceo import build_brief
    from app.services.platform import sync
    cash(conn, "8000")
    pid = product(conn, "BR-1", cost="100", price="500")
    order(conn, "TY-B1", pid, price="500", days_ago=1)
    finance_routes(ty, [settlement("BR1", "Sale", "TY-B1", "BR-1", credit=500, commission=75, pay_in_days=2)])
    run(engine, sync.FINANCE_SYNC)
    with engine.begin() as c:
        items = build_brief(c)["items"]
    assert [i["kind"] for i in items][:2] == ["yesterday", "cash"]
    assert "Trendyol Orders" in items[0]["source"] and "bekleyen hakediş 425,00 ₺" in items[1]["text"]
    assert "harcanabilir sermaye sayılmaz" in items[1]["text"]
    assert len(items) <= 7


# ======================================================================== İADE → kâr yeniden hesaplama
def claim_payload(number, barcode, status="Accepted", price=500, reason=("UNFIT", "Ürün beden/ölçü olarak uymadı")):
    return {"content": [{
        "id": "c-uuid-1", "orderNumber": number, "orderDate": ms(NOW - timedelta(days=5)), "claimDate": ms(NOW - timedelta(days=2)),
        "customerFirstName": "Zeynep", "customerLastName": "Arslan", "cargoTrackingNumber": 123, "orderShipmentPackageId": 777,
        "lastModifiedDate": ms(NOW - timedelta(hours=3)),
        "items": [{"orderLine": {"id": 991, "productName": "Çanta", "barcode": barcode, "merchantSku": barcode, "price": price},
                   "claimItems": [{"id": "ci-1", "orderLineItemId": 5,
                                   "customerClaimItemReason": {"name": reason[1], "externalReasonId": 1, "code": reason[0]},
                                   "trendyolClaimItemReason": {"name": reason[1], "externalReasonId": 1, "code": reason[0]},
                                   "claimItemStatus": {"name": status}, "customerNote": "Beni 0532 123 45 67'den arayın",
                                   "acceptedBySeller": status == "Accepted"}]}]}], "totalPages": 1}


def test_return_recalculates_profit_without_double_count(engine, conn, ty):
    from app.services.platform import sync
    pid = product(conn, "RET-1", cost="200", price="500")
    oid = order(conn, "TY-2001", pid, price="500")
    before = conn.execute(text("SELECT net_profit, refund_cost FROM orders WHERE id = :i"), {"i": oid}).one()
    assert before.refund_cost == 0
    ty.routes["/claims"] = claim_payload("TY-2001", "RET-1")
    out = run(engine, sync.RETURNS_SYNC)
    assert out["created"] == 1
    r = conn.execute(text("SELECT * FROM marketplace_returns")).mappings().one()
    assert (r["order_id"], r["product_id"], r["status"], r["reason_code"]) == (oid, pid, "Accepted", "UNFIT")
    assert r["shipment_package_id"] == "777" and r["amount"] == Decimal("500.00")
    assert "0532" not in r["customer_note"] and "[telefon]" in r["customer_note"]   # kişisel veri maskeli
    after = conn.execute(text("SELECT net_profit, refund_cost FROM orders WHERE id = :i"), {"i": oid}).one()
    assert after.refund_cost == Decimal("500.00") and after.net_profit < before.net_profit
    assert conn.execute(text("SELECT status FROM platform_events WHERE event_type = 'RETURN_CREATED'")).scalar() == "done"
    # Cari hesapta gerçek iade kaydı gelir → claim kaynaklı satır kalkar, iade iki kez düşülmez
    finance_routes(ty, [settlement("S-SALE", "Sale", "TY-2001", "RET-1", credit=500, commission=75),
                        settlement("S-RET", "Return", "TY-2001", "RET-1", debt=500, commission=75)])
    run(engine, sync.FINANCE_SYNC)
    refunds = conn.execute(text("SELECT source, amount FROM financial_transactions WHERE order_id = :i AND kind = 'refund'"), {"i": oid}).all()
    assert [(x.source, x.amount) for x in refunds] == [("trendyol_finance", Decimal("500.00"))]
    final = conn.execute(text("SELECT refund_cost, commission FROM orders WHERE id = :i"), {"i": oid}).one()
    assert final.refund_cost == Decimal("500.00") and final.commission == Decimal("0.00")   # komisyon iade edildi


def test_rejected_claim_has_no_profit_effect(engine, conn, ty):
    from app.services.platform import sync
    pid = product(conn, "RET-2", cost="200", price="500")
    oid = order(conn, "TY-2101", pid, price="500")
    ty.routes["/claims"] = claim_payload("TY-2101", "RET-2", status="Accepted")
    run(engine, sync.RETURNS_SYNC)
    assert conn.execute(text("SELECT refund_cost FROM orders WHERE id = :i"), {"i": oid}).scalar() == Decimal("500.00")
    ty.routes["/claims"] = claim_payload("TY-2101", "RET-2", status="Rejected")
    run(engine, sync.RETURNS_SYNC)
    assert conn.execute(text("SELECT refund_cost FROM orders WHERE id = :i"), {"i": oid}).scalar() == Decimal("0.00")
    assert conn.execute(text("SELECT COUNT(*) FROM platform_events WHERE event_type = 'RETURN_UPDATED'")).scalar() == 1


# ======================================================================== SORU-CEVAP + CX
def q(i, text_, main="MODEL-1", name="Siyah Omuz Çantası", answered=False):
    return {"id": 1000 + i, "text": text_, "customerId": 555, "userName": "Ayşe Kaya", "showUserName": True,
            "status": "ANSWERED" if answered else "WAITING_FOR_ANSWER", "creationDate": ms(NOW - timedelta(days=1, minutes=i)),
            "public": True, "imageUrl": "https://img", "productName": name, "productMainId": main, "webUrl": "https://ty/p",
            "answer": {"text": "Cevap", "creationDate": ms(NOW)} if answered else None}


def test_question_ingestion_classification_and_cx(engine, conn, ty, api):
    from app.services.platform import sync
    from app.services.platform.cx import analyze
    p1 = product(conn, "Q-1", name="Siyah Omuz Çantası")
    conn.execute(text("UPDATE products SET model_code = 'MODEL-1' WHERE id = :i"), {"i": p1})
    p2 = product(conn, "Q-2", name="Bej Sırt Çantası")
    conn.execute(text("UPDATE products SET model_code = 'MODEL-2' WHERE id = :i"), {"i": p2})
    texts = ["Boyutu nedir?", "Kaç cm?", "A4 sığar mı?", "Ölçüleri nedir?", "Ebatı ne kadar?", "Laptop sığar mı?",
             "Genişliği kaç cm?", "Boyutu nedir acaba?", "Askısı çıkıyor mu?", "Bu renk mevcut mu?",
             "Ne zaman kargoya verilir?", "Beni 0532 123 45 67 numaradan arayın, mail: ayse@example.com"]
    qs = [q(i, t) for i, t in enumerate(texts)] + [q(50 + i, t, main="MODEL-2", name="Bej Sırt Çantası")
                                                  for i, t in enumerate(["Boyutu nedir?", "Kaç cm?", "Ölçüsü?", "Renk?"])]
    ty.routes["/questions/filter"] = {"content": qs, "totalPages": 1, "totalElements": len(qs)}
    out = run(engine, sync.QUESTIONS_SYNC)
    assert out["created"] == 16
    stored = conn.execute(text("SELECT * FROM customer_questions")).mappings().all()
    blob = json.dumps([dict(r) for r in stored], default=str)
    assert "Ayşe Kaya" not in blob and "555" not in blob.replace("1555", "") and "0532" not in blob and "ayse@example.com" not in blob
    assert "[telefon]" in blob and "[e-posta]" in blob
    assert all(r["product_id"] in (p1, p2) for r in stored)
    cats = {r["question_text"]: r["category"] for r in stored}
    assert cats["Boyutu nedir?"] == "size" and cats["Askısı çıkıyor mu?"] == "strap"
    assert cats["Bu renk mevcut mu?"] == "color" and cats["Ne zaman kargoya verilir?"] == "shipping"
    assert all(r["suggested_answer"] for r in stored if r["answer_text"] is None and r["category"] != "other")
    r = analyze(conn, 30)
    prod1 = next(x for x in r["products"] if x["product_id"] == p1)
    size1 = next(c for c in prod1["categories"] if c["category"] == "size")
    assert prod1["questions"] == 12 and size1["count"] == 8 and size1["share"] == Decimal("0.667") and size1["status"] == "OK"
    prod2 = next(x for x in r["products"] if x["product_id"] == p2)
    size2 = next(c for c in prod2["categories"] if c["category"] == "size")
    assert size2["count"] == 3 and size2["share"] is None and size2["status"] == "INSUFFICIENT_DATA"   # yüzde UYDURULMAZ
    gap2 = next(g for g in r["info_gaps"] if g["product_id"] == p2)
    assert "yüzde için örnek yetersiz" in gap2["text"] and "%" not in gap2["text"]
    gap1 = next(g for g in r["info_gaps"] if g["product_id"] == p1)
    assert "%67" in gap1["text"]
    assert api.get("/api/ai/cx").status_code == 200
    # Cevap HİÇ gönderilmez: yalnızca GET
    assert all(m == "GET" for m, _p, _q in ty.calls)
    assert not any("answers" in p for _m, p, _q in ty.calls)


# ======================================================================== WEBHOOK
def webhook_settings(monkeypatch, **kw):
    from app.api import webhooks
    from app.config import Settings
    s = Settings(database_url="postgresql://x@y/z", **kw)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)


def package(pid=9001, number="TY-W1", status="Created", lm=1000, barcode="WH-1"):
    return {"shipmentPackageId": pid, "orderNumber": number, "status": status, "shipmentPackageStatus": status,
            "lastModifiedDate": lm, "orderDate": ms(NOW), "currencyCode": "TRY",
            "customerFirstName": "Zeynep", "customerLastName": "Arslan", "customerEmail": "z@example.com",
            "shipmentAddress": {"fullName": "Zeynep Arslan", "address1": "Gizli sokak 1", "city": "İstanbul", "phone": "05321234567"},
            "invoiceAddress": {"fullName": "Zeynep Arslan"},
            "lines": [{"lineId": 1, "stockCode": barcode, "barcode": barcode, "productName": "Çanta", "quantity": 1,
                       "lineUnitPrice": 750, "lineGrossAmount": 750, "vatRate": 20}]}


def test_webhook_duplicate_delivery_and_pii(engine, conn, client_factory, monkeypatch):
    from app.services.platform import events
    webhook_settings(monkeypatch, trendyol_webhook_api_key="wh-secret-123")
    store(conn)
    c, _login = client_factory
    body = package()
    r1 = c.post("/api/webhooks/trendyol", json=body, headers={"x-api-key": "wh-secret-123"})
    r2 = c.post("/api/webhooks/trendyol", json=body, headers={"x-api-key": "wh-secret-123"})
    assert r1.status_code == 200 and r1.json() == {"accepted": 1, "duplicates": 0, "invalid": 0}
    assert r2.status_code == 200 and r2.json()["duplicates"] == 1
    assert conn.execute(text("SELECT COUNT(*) FROM platform_events")).scalar() == 1
    payload = json.dumps(conn.execute(text("SELECT payload FROM platform_events")).scalar(), ensure_ascii=False)
    assert "Zeynep" not in payload and "Gizli sokak" not in payload and "z@example.com" not in payload and "0532" not in payload
    # HTTP isteğinde iş yapılmadı: sipariş henüz yok, olay kuyrukta, işleme işi kuyrukta
    assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() == 0
    assert conn.execute(text("SELECT COUNT(*) FROM sync_jobs WHERE job_type = 'events.process'")).scalar() == 1
    assert events.process_pending(engine)["processed"] == 1
    o = conn.execute(text("SELECT internal_status, customer_name, gross_revenue FROM orders WHERE external_order_id = 'TY-W1'")).one()
    assert o.internal_status == "new" and o.customer_name is None and o.gross_revenue == Decimal("750.00")
    assert conn.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action = 'webhook.received'")).scalar() == 2


def test_webhook_invalid_authentication(engine, conn, client_factory, monkeypatch):
    c, _login = client_factory
    webhook_settings(monkeypatch)                                         # yapılandırılmamış → kapalı
    assert c.post("/api/webhooks/trendyol", json=package(), headers={"x-api-key": "x"}).status_code == 503
    webhook_settings(monkeypatch, trendyol_webhook_api_key="wh-secret-123", trendyol_webhook_username="ty",
                     trendyol_webhook_password="pw-123456")
    assert c.post("/api/webhooks/trendyol", json=package(), headers={"x-api-key": "wrong"}).status_code == 401
    assert c.post("/api/webhooks/trendyol", json=package()).status_code == 401
    bad = base64.b64encode(b"ty:wrong").decode()
    assert c.post("/api/webhooks/trendyol", json=package(), headers={"Authorization": f"Basic {bad}"}).status_code == 401
    assert conn.execute(text("SELECT COUNT(*) FROM platform_events")).scalar() == 0
    ev = conn.execute(text("SELECT occurrences FROM system_events WHERE fingerprint = 'webhook:trendyol:auth'")).scalar()
    assert ev == 4
    good = base64.b64encode(b"ty:pw-123456").decode()
    assert c.post("/api/webhooks/trendyol", json=package(), headers={"Authorization": f"Basic {good}"}).status_code == 200
    assert c.post("/api/webhooks/trendyol", content=b"{bozuk", headers={"x-api-key": "wh-secret-123"}).status_code == 400
    assert c.post("/api/webhooks/trendyol", json={"foo": 1}, headers={"x-api-key": "wh-secret-123"}).status_code == 400


def test_webhook_worker_retry_and_restart_recovery(engine, conn, client_factory, monkeypatch):
    from app.services.platform import events
    webhook_settings(monkeypatch, trendyol_webhook_api_key="wh-secret-123")
    store(conn)
    c, _login = client_factory
    c.post("/api/webhooks/trendyol", json=package(pid=9101, number="TY-R1"), headers={"x-api-key": "wh-secret-123"})
    real = events._webhook_order
    monkeypatch.setattr(events, "_webhook_order", lambda conn_, ev: (_ for _ in ()).throw(RuntimeError("DB koptu")))
    assert events.process_pending(engine)["failed"] == 1
    e = conn.execute(text("SELECT status, attempts, last_error, next_attempt_at > NOW() AS later FROM platform_events")).one()
    assert e.status == "failed" and e.attempts == 1 and "DB koptu" in e.last_error and e.later
    assert events.process_pending(engine)["processed"] == 0                         # backoff süresi dolmadı
    assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() == 0           # yarım iş yok (transaction geri alındı)
    monkeypatch.setattr(events, "_webhook_order", real)
    conn.execute(text("UPDATE platform_events SET next_attempt_at = NOW() - INTERVAL '1 second'"))
    assert events.process_pending(engine)["processed"] == 1
    assert conn.execute(text("SELECT status, attempts FROM platform_events")).one() == ("done", 2)
    assert conn.execute(text("SELECT COUNT(*) FROM orders WHERE external_order_id = 'TY-R1'")).scalar() == 1
    # Worker işlerken yeniden başladı: 'processing'de kalan olay 10 dk sonra tekrar alınır
    c.post("/api/webhooks/trendyol", json=package(pid=9102, number="TY-R2"), headers={"x-api-key": "wh-secret-123"})
    conn.execute(text("UPDATE platform_events SET status = 'processing', attempts = 1, next_attempt_at = NOW() - INTERVAL '11 minutes' WHERE entity_ref = '9102'"))
    assert events.process_pending(engine)["processed"] == 1
    assert conn.execute(text("SELECT COUNT(*) FROM orders WHERE external_order_id = 'TY-R2'")).scalar() == 1


def test_webhook_replayed_old_event_is_ignored(engine, conn, client_factory, monkeypatch):
    from app.services.platform import events
    webhook_settings(monkeypatch, trendyol_webhook_api_key="wh-secret-123")
    store(conn)
    c, _login = client_factory
    hdr = {"x-api-key": "wh-secret-123"}
    c.post("/api/webhooks/trendyol", json=package(pid=9201, number="TY-P1", status="Shipped", lm=2000), headers=hdr)
    events.process_pending(engine)
    c.post("/api/webhooks/trendyol", json=package(pid=9201, number="TY-P1", status="Created", lm=1000), headers=hdr)
    assert events.process_pending(engine)["ignored"] == 1
    assert conn.execute(text("SELECT internal_status FROM orders WHERE external_order_id = 'TY-P1'")).scalar() == "shipped"


# ======================================================================== TAZELİK / HATA DURUMLARI
def _profitable_campaign(conn):
    pid = product(conn, "FS-1", cost="100", price="600")
    sell(conn, pid, price="600", n=10, days_ago=2, tag="fs")
    return campaign(conn, "Kârlı", [pid], budget="300", spend_per_day="300", revenue_per_day="6000", clicks=150)


def _propose_increase(engine, cid):
    from app.services.ai import proposals
    with engine.begin() as c:
        pid = proposals.propose(c, agent="advertising", action_type="ads.increase_budget", entity_type="campaign", entity_id=cid,
                                title="artır", reason="x", evidence={},
                                params={"current_daily_budget": "300", "new_daily_budget": "390", "delta_per_day": "90"},
                                required_capital=1260, capital_category="advertising")
        return c.execute(text("SELECT status, risk_checks FROM ai_proposals WHERE id = :i"), {"i": pid}).one()


def test_stale_finance_blocks_spending(engine, conn, api):
    cash(conn)
    cid = _profitable_campaign(conn)
    sid = store(conn)
    conn.execute(text("""INSERT INTO sync_state(store_id, resource, last_attempt_at, last_success_at, error_count, status)
                         VALUES (:s, 'finance', NOW() - INTERVAL '40 hours', NOW() - INTERVAL '40 hours', 0, 'CONNECTED')"""), {"s": sid})
    st = _propose_increase(engine, cid)
    fin = next(x for x in st.risk_checks if x["code"] == "finance_stale")
    assert st.status == "blocked" and fin["severity"] == "block"
    assert "Finans verisi güncel olmadığı için ek reklam bütçesi" in fin["message"]
    src = {s["code"]: s for s in api.get("/api/ai/data-sources").json()["sources"]}
    assert src["trendyol.finance"]["freshness"] == "STALE"
    # Güncel ama son deneme hatalı → DEGRADED: blok değil uyarı
    conn.execute(text("UPDATE sync_state SET last_success_at = NOW() - INTERVAL '2 hours', error_count = 1 WHERE resource = 'finance'"))
    st = _propose_increase(engine, cid)
    codes = {x["code"]: x["severity"] for x in st.risk_checks}
    assert "finance_stale" not in codes and codes.get("finance_degraded") == "warning"


def test_api_permission_denied_is_reported_not_retried(engine, conn, ty, monkeypatch):
    from app.connectors.base import AuthError
    from app.services import jobs
    from app.services.platform import sync
    from app.services.platform.sources import data_sources
    from app.worker import Worker
    ty.routes["/claims"] = (403, {"errors": [{"message": "forbidden"}]})
    with pytest.raises(AuthError):
        run(engine, sync.RETURNS_SYNC)
    st = conn.execute(text("SELECT status, error_count, last_success_at FROM sync_state WHERE resource = 'returns'")).one()
    assert st.status == "PERMISSION_DENIED" and st.error_count == 1 and st.last_success_at is None
    from app.connectors import registry
    monkeypatch.setattr(registry, "get_connector", lambda code, settings=None: ty.connector())
    with engine.begin() as c:
        src = {s["code"]: s for s in data_sources(c)}
    assert src["trendyol.returns"]["connection"] == "PERMISSION_DENIED" and src["trendyol.returns"]["freshness"] == "ERROR"
    assert conn.execute(text("SELECT COUNT(*) FROM system_events WHERE fingerprint = 'platform:returns'")).scalar() == 1
    # Worker: yetki hatası tekrar denenmez (FAILED, kuyruğa geri alınmaz)
    with engine.begin() as c:
        jobs.enqueue(c, sync.RETURNS_SYNC, marketplace="trendyol", idempotency_key="r1")
    Worker(engine).run_once()
    assert conn.execute(text("SELECT status FROM sync_jobs WHERE idempotency_key = 'r1'")).scalar() == "failed"


def test_api_rate_limit_is_retried_with_retry_after(engine, conn, ty):
    from app.services.platform import sync
    calls = {"n": 0}

    def claims(req, params):
        calls["n"] += 1
        if calls["n"] == 1:
            return (429, {"errors": [{"message": "too.many.requests"}]}, {"Retry-After": "2"})
        return {"content": [], "totalPages": 0}
    ty.routes["/claims"] = claims
    out = run(engine, sync.RETURNS_SYNC)
    assert out["claim_items"] == 0 and 2.0 in ty.sleeps
    assert conn.execute(text("SELECT status FROM sync_state WHERE resource = 'returns'")).scalar() == "NO_DATA"


def test_trendyol_5xx_degrades_without_losing_data(engine, conn, ty):
    from app.connectors.base import RetryableError
    from app.services.platform import sources, sync
    ty.routes["/questions/filter"] = {"content": [q(1, "Boyutu nedir?")], "totalPages": 1}
    run(engine, sync.QUESTIONS_SYNC)
    ty.routes["/questions/filter"] = (503, {"error": "unavailable"})
    with pytest.raises(RetryableError):
        run(engine, sync.QUESTIONS_SYNC)
    assert conn.execute(text("SELECT COUNT(*) FROM customer_questions")).scalar() == 1       # eski veri silinmedi
    with engine.begin() as c:
        assert sources.freshness(sources.trendyol_state(c, "questions"), 12) == "DEGRADED"
        c.execute(text("UPDATE sync_state SET last_success_at = NOW() - INTERVAL '20 hours' WHERE resource = 'questions'"))
        assert sources.freshness(sources.trendyol_state(c, "questions"), 12) == "ERROR"
    assert conn.execute(text("SELECT status FROM sync_state WHERE resource = 'questions'")).scalar() == "ERROR"


def test_partial_finance_outage_keeps_data_but_is_not_fresh(engine, conn, ty, api):
    from app.connectors.base import ConnectorError
    from app.services.platform import sync
    cash(conn)
    cid = _profitable_campaign(conn)
    finance_routes(ty, [settlement("PO1", "Sale", "TY-Z", "B-Z", credit=1000, commission=150)])
    ty.routes["/otherfinancials"] = (500, {"error": "boom"})
    with pytest.raises(ConnectorError, match="kısmi"):
        run(engine, sync.FINANCE_SYNC)
    assert conn.execute(text("SELECT COUNT(*) FROM marketplace_finance_entries")).scalar() == 1   # alınan veri yazıldı
    src = {s["code"]: s for s in api.get("/api/ai/data-sources").json()["sources"]}
    assert src["trendyol.finance"]["freshness"] != "FRESH"
    st = _propose_increase(engine, cid)
    assert "finance_stale" in {x["code"] for x in st.risk_checks}     # kısmi/başarısız finansla para harcanmaz


# ======================================================================== PII → LLM
def test_pii_is_not_sent_to_llm(engine, conn, ty, client_factory, monkeypatch):
    from types import SimpleNamespace as NS

    import anthropic

    from app.config import get_settings
    from app.services.ai import chat
    from app.services.platform import pii, sync
    pid = product(conn, "PII-1", cost="100", price="500")
    conn.execute(text("UPDATE products SET model_code = 'PII-M' WHERE id = :i"), {"i": pid})
    order(conn, "TY-PII", pid, price="500", customer="Zeynep Arslan Demir")
    ty.routes["/questions/filter"] = {"content": [q(1, "Ölçüsü nedir? 0532 765 43 21", main="PII-M", name="Çanta PII-1")], "totalPages": 1}
    run(engine, sync.QUESTIONS_SYNC)
    with engine.begin() as c:
        for name in chat.TOOLS:
            payload = chat.tool_payload_for_llm(c, name, {"amount": 1000} if name == "get_ad_budget_plan" else {})
            assert "Zeynep" not in payload and "0532" not in payload and "765 43 21" not in payload, name
    with pytest.raises(pii.PIILeak):
        pii.assert_no_pii("Müşteri: Zeynep Arslan Demir", ["Zeynep Arslan Demir"])
    with pytest.raises(pii.PIILeak):
        pii.assert_no_pii("tel 0532 111 22 33")
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    sent = []

    class FakeMessages:
        def create(self, **kw):
            sent.append(json.dumps(kw["messages"], default=str, ensure_ascii=False))
            usage = NS(input_tokens=1, output_tokens=1)
            if len(sent) == 1:
                return NS(stop_reason="tool_use", usage=usage, content=[
                    NS(type="tool_use", id="t1", name="get_customer_signals", input={}),
                    NS(type="tool_use", id="t2", name="get_today_summary", input={})])
            return NS(stop_reason="end_turn", usage=usage, content=[NS(type="text", text="Tamam")])

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: NS(beta=NS(messages=FakeMessages())))
    c, login = client_factory
    login("admin", "Admin-Password-123")
    r = c.post("/api/ai/chat", json={"message": "Müşteriler ne soruyor?"}, headers=H).json()
    assert r["engine"] == "claude"
    everything = "\n".join(sent)
    assert "Ölçü / ebat" in everything or "size" in everything                    # veri gitti
    assert "Zeynep" not in everything and "0532" not in everything                # kişisel veri gitmedi


# ======================================================================== SMOKE (salt okunur, PII yok)
def test_smoke_report_statuses_without_pii(engine, conn, ty):
    from app.services.platform import smoke
    ty.routes["/v2/orders"] = {"content": [package(pid=1, number="TY-S1", status="Delivered", lm=ms(NOW))], "totalPages": 1,
                               "totalElements": 1}
    ty.routes["/claims"] = (403, {"errors": [{"message": "forbidden"}]})
    finance_routes(ty, [settlement("SM1", "Sale", "TY-S1", "WH-1", credit=750, commission=112.5)])
    ty.routes["/questions/filter"] = {"content": [q(1, "Askısı çıkıyor mu?")], "totalPages": 1}
    ty.routes["/addresses"] = {"supplierAddresses": [{"addressType": "Shipment", "city": "İstanbul", "address": "Gizli"},
                                                     {"addressType": "Returning", "city": "İstanbul", "address": "Gizli"}]}
    ty.routes["/webhooks"] = [{"id": "w1", "url": "https://panel.example.com/api/webhooks/trendyol", "status": "ACTIVE",
                               "authenticationType": "API_KEY", "subscribedStatuses": ["CREATED"]}]
    rep = smoke.run()
    st = {r["service"].split(" ")[0] + ("-" + r["service"].split("— ")[1][:5] if "—" in r["service"] else ""): r for r in rep["results"]}
    assert st["Orders"]["status"] == "CONNECTED" and st["Orders"]["record_count"] == 1
    assert st["Products"]["status"] == "UNSUPPORTED" and st["Products"]["http"] == "404"
    assert st["Returns"]["status"] == "PERMISSION_DENIED" and st["Returns"]["http"] == "403"
    assert st["Finance-settl"]["status"] == "CONNECTED" and st["Finance-settl"]["mapping"]["commission_amount"] == "1/1"
    assert st["Finance-other"]["status"] == "NO_DATA"
    assert st["Questions"]["mapping"]["categories"] == {"strap": 1}
    assert st["Seller"]["mapping"]["address_types"] == ["Returning", "Shipment"]
    assert st["Webhooks"]["mapping"]["active"] == 1 and st["Webhooks"]["mapping"]["webhooks"][0]["host"] == "panel.example.com"
    dump = json.dumps(rep, ensure_ascii=False, default=str) + smoke.format_table(rep)
    for secret in ("Zeynep", "Gizli", "z@example.com", "05321234567", "Ayşe"):
        assert secret not in dump
    assert all(m == "GET" for m, _p, _q in ty.calls)
    assert conn.execute(text("SELECT COUNT(*) FROM marketplace_finance_entries")).scalar() == 0     # smoke DB'ye yazmaz


def test_next_payment_date_is_never_in_the_past(engine, conn, ty):
    from app.services.platform import finance, sync
    finance_routes(ty, [settlement("N1", "Sale", "TY-N1", "B-N", credit=1000, commission=100, pay_in_days=-3),
                        settlement("N2", "Sale", "TY-N2", "B-N", credit=500, commission=50, pay_in_days=4)])
    run(engine, sync.FINANCE_SYNC)
    p = finance.payout_summary(conn)
    assert p["pending_payout"] == Decimal("1350.00")               # vadesi 3 gün geçmiş ama ödenmemiş kayıt da beklemede
    assert p["next_payment_date"].date() >= datetime.now(timezone.utc).date() - timedelta(days=1)
    assert p["next_payment_date"] > NOW + timedelta(days=3)        # geçmiş tarih "sonraki ödeme" olarak gösterilmez


def test_unknown_pending_payout_is_not_zero(engine, conn, api):
    """Regresyon (Docker doğrulamasında bulundu): finans verisi yok + elle hakediş girilmemiş → hakediş 0 DEĞİL, BİLİNMİYOR."""
    from app.services.ai.chat import rules_answer
    cash(conn, "8000")
    cap = api.get("/api/ai/capital").json()
    pend = cap["cash_structure"]["pending_marketplace_payout"]
    assert pend["amount"] is None and pend["kind"] == "UNKNOWN"
    assert cap["pending_payout_not_counted"] is None
    assert "Eksik" in cap["cash_structure"]["total_money"]["note"]
    assert Decimal(cap["usable"]) == Decimal("8000.00")
    ans = rules_answer(conn, "Ne kadar sermaye kullanabilirim?")[0]
    assert "hakedişini bilmiyorum" in ans and "Bekleyen hakediş 0" not in ans
    assert "Bunu söylemek için yeterli verim yok" in rules_answer(conn, "Trendyol hakediş ne zaman yatacak?")[0]
    assert api.get("/api/ai/overview").json()["kpis"]["pending_payout_kind"] == "UNKNOWN"
    # Elle girilirse MANUAL olarak, kaynağıyla birlikte gösterilir
    conn.execute(text("INSERT INTO ai_capital_accounts(kind, name, amount) VALUES ('pending_payout', 'Trendyol', 0)"))
    pend = api.get("/api/ai/capital").json()["cash_structure"]["pending_marketplace_payout"]
    assert pend["kind"] == "MANUAL" and Decimal(pend["amount"]) == Decimal("0.00")


def test_no_data_is_not_reported_as_zero_or_estimate(engine, conn, api):
    """Regresyon (boş veri ekran kontrolünde bulundu): sipariş/reklam verisi yokken 'Tahmin' veya ₺0 iddiası yapılmaz."""
    from app.services.platform import finance
    pv = finance.profit_provenance(conn, NOW - timedelta(days=30), NOW + timedelta(days=1))
    assert pv["items"] == 0 and pv["status"] == "NO_DATA"
    k = api.get("/api/ai/overview").json()["kpis"]
    assert k["orders_7d"] == 0 and k["has_ad_data"] is False          # arayüz bu bayraklarla "—  Veri yok" gösterir
    pid = product(conn, "ND-1", cost="100", price="500")
    order(conn, "TY-ND1", pid, price="500")
    assert finance.profit_provenance(conn, NOW - timedelta(days=30), NOW + timedelta(days=1))["status"] == "ESTIMATED"
    cid = campaign(conn, "ND", [pid], days=1)
    assert cid and api.get("/api/ai/overview").json()["kpis"]["has_ad_data"] is True
