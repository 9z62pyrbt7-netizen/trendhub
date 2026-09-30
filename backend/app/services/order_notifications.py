"""Web siparişi bildirimleri: e-posta (SMTP) ve SMS (Netgsm) — `notification_outbox` kuyruğu.

Kapalı-varsayılan: sağlayıcı bilgisi sunucu ortam değişkenlerinde yoksa (SMTP_HOST / SMS_PROVIDER) veya
panelde ilgili kanal kapalıysa (storefront.notify_email / notify_sms) kuyruğa HİÇBİR kayıt eklenmez ve
hiçbir dış servise istek yapılmaz. Sağlayıcı şifreleri yalnızca .env'de tutulur.

Olaylar (sipariş başına her kanal için bir kez; `dedupe_key` ile tekrar gönderim engellenir):
  order_received     sipariş alındı (kapıda ödeme / havale bekleniyor / kartla ödendi)
  payment_confirmed  havale/EFT ödemesi onaylandı
  shipped            TrendHub'da sipariş 'kargoya verildi' oldu (bakım işi algılar)
  cancelled          ödeme alınmadığı için iptal / süresi doldu
  password_reset     müşteri hesabı şifre sıfırlama (yalnızca e-posta)
Mağaza sahibine yeni sipariş e-postası: STOREFRONT_ORDER_ALERT_EMAILS.

Gönderim worker'da yapılır (`storefront.notify` işi); başarısız gönderim en fazla MAX_ATTEMPTS kez denenir.
"""
from __future__ import annotations

import json
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import get_settings
from . import app_settings, jobs

log = logging.getLogger("trendhub.order_notifications")
JOB_TYPE = "storefront.notify"
MAX_ATTEMPTS = 5
BRAND = "Trendçantanız"


# ------------------------------------------------------------------ sağlayıcılar
def email_configured(settings=None) -> bool:
    s = settings or get_settings()
    return bool(s.smtp_host.strip() and s.smtp_from.strip())


def sms_configured(settings=None) -> bool:
    s = settings or get_settings()
    if s.sms_provider.strip().lower() == "netgsm":
        return bool(s.netgsm_usercode and s.netgsm_password and s.netgsm_header)
    return False


def channels_enabled(conn: Connection) -> dict:
    """Panel ayarı + sağlayıcı yapılandırması birlikte açıksa kanal etkindir."""
    return {
        "email": email_configured() and bool(app_settings.get(conn, "storefront.notify_email", True)),
        "sms": sms_configured() and bool(app_settings.get(conn, "storefront.notify_sms", False)),
    }


def send_email(to: str, subject: str, body: str) -> str:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = formataddr((BRAND, s.smtp_from.strip()))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=s.smtp_from.split("@")[-1])
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if s.smtp_ssl:
        server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
    try:
        if s.smtp_starttls and not s.smtp_ssl:
            server.starttls(context=ctx)
        if s.smtp_username:
            server.login(s.smtp_username, s.smtp_password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass
    return "smtp"


def sms_number(phone: str) -> str | None:
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 10 and d[-10:].startswith("5") else None


def send_sms(phone: str, message: str) -> str:
    """Netgsm GET API. Yanıt '00 <id>' / '01 <id>' / '02 <id>' başarılıdır; diğer kodlar hatadır."""
    s = get_settings()
    number = sms_number(phone)
    if not number:
        raise ValueError("Geçersiz cep telefonu")
    resp = httpx.get(s.netgsm_api_url, params={"usercode": s.netgsm_usercode, "password": s.netgsm_password,
                                               "gsmno": number, "message": message, "msgheader": s.netgsm_header,
                                               "dil": "TR"}, timeout=20)
    code = resp.text.strip().split(" ")[0]
    if resp.status_code != 200 or code not in ("00", "01", "02"):
        raise RuntimeError(f"Netgsm hata kodu: {code[:10]}")
    return "netgsm"


# ------------------------------------------------------------------ içerik
def _money(v) -> str:
    from ..storefront.store_config import fmt_try
    return fmt_try(v)


def _base() -> str:
    return get_settings().storefront_base_url.strip().rstrip("/")


def render(event: str, sfo: dict, *, order_url: str | None = None) -> dict:
    """Olay için e-posta konusu/gövdesi ve SMS metni (düz metin, Türkçe)."""
    code, name = sfo["public_code"], (sfo.get("full_name") or "").split(" ")[0]
    lines = "\n".join(f"  • {ln['title']}{' · ' + ln['color'] if ln.get('color') else ''} × {ln['quantity']} — {_money(ln['line_total'])}"
                      for ln in (sfo.get("lines") or []))
    link = f"\nSiparişinizi takip edin: {order_url}\n" if order_url else ""
    footer = f"\n\n{BRAND}\n{_base() or ''}".rstrip()
    if event == "order_received":
        pm = sfo.get("payment_method")
        if sfo.get("status") == "awaiting_payment":
            extra = ("Siparişiniz alındı. Havale/EFT ödemeniz hesabımıza ulaştığında hazırlanmaya başlanacak. "
                     "Ödeme bilgileri sipariş sayfanızda yer alıyor.")
        elif pm == "cash_on_delivery":
            extra = "Siparişiniz alındı. Ödemeyi teslimatta yapacaksınız."
        else:
            extra = "Ödemeniz alındı, siparişiniz hazırlanıyor."
        subject = f"Siparişiniz alındı — {code}"
        body = f"Merhaba {name},\n\n{extra}\n\nSipariş numarası: {code}\n{lines}\n\nToplam: {_money(sfo['total'])}\n{link}"
        sms = f"{BRAND}: {code} numaralı siparişiniz alındı. Toplam {_money(sfo['total'])}. Teşekkür ederiz."
    elif event == "payment_confirmed":
        subject = f"Ödemeniz onaylandı — {code}"
        body = f"Merhaba {name},\n\n{code} numaralı siparişinizin ödemesi onaylandı; siparişiniz hazırlanıyor.\n"
        sms = f"{BRAND}: {code} numaralı siparişinizin ödemesi onaylandı. Siparişiniz hazırlanıyor."
    elif event == "shipped":
        subject = f"Siparişiniz kargoya verildi — {code}"
        body = f"Merhaba {name},\n\n{code} numaralı siparişiniz kargoya verildi.\n{lines}\n"
        sms = f"{BRAND}: {code} numaralı siparişiniz kargoya verildi."
    elif event == "cancelled":
        subject = f"Siparişiniz iptal edildi — {code}"
        body = (f"Merhaba {name},\n\n{code} numaralı siparişiniz ödeme alınamadığı için iptal edildi. "
                "Kartınızdan tutar çekildiyse bizimle iletişime geçin.\n")
        sms = f"{BRAND}: {code} numaralı siparişiniz ödeme alınamadığı için iptal edildi."
    else:
        raise ValueError(f"Bilinmeyen bildirim: {event}")
    return {"subject": subject, "body": body + footer, "sms": sms}


# ------------------------------------------------------------------ kuyruk
def _queue(conn: Connection, *, channel: str, recipient: str, template: str, subject: str | None, body: str,
           sfo_id: int | None, dedupe_key: str | None) -> bool:
    new_id = conn.execute(text("""
        INSERT INTO notification_outbox(channel, recipient, template, subject, body, storefront_order_id, dedupe_key)
        VALUES (:ch, :to, :tpl, :subj, :body, :o, :k)
        ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING RETURNING id"""),
        {"ch": channel, "to": recipient, "tpl": template, "subj": subject, "body": body, "o": sfo_id, "k": dedupe_key}).scalar()
    if new_id:
        jobs.enqueue(conn, JOB_TYPE, payload={}, idempotency_key=JOB_TYPE, max_attempts=3)
    return bool(new_id)


def notify_order(conn: Connection, sfo_id: int, event: str, *, order_url: str | None = None) -> list[str]:
    """Web siparişi olayı için etkin kanallara bildirim kuyruğa ekler. Hiçbir kanal etkin değilse hiçbir şey yapmaz."""
    enabled = channels_enabled(conn)
    alerts = [a.strip() for a in get_settings().storefront_order_alert_emails.split(",") if a.strip()] \
        if email_configured() and event == "order_received" else []
    if not (enabled["email"] or enabled["sms"] or alerts):
        return []
    sfo = conn.execute(text("SELECT * FROM storefront_orders WHERE id = :id"), {"id": sfo_id}).mappings().first()
    if sfo is None:
        return []
    msg = render(event, dict(sfo), order_url=order_url)
    queued = []
    if enabled["email"] and sfo["email"]:
        if _queue(conn, channel="email", recipient=sfo["email"], template=event, subject=msg["subject"], body=msg["body"],
                  sfo_id=sfo_id, dedupe_key=f"{event}:{sfo_id}:email"):
            queued.append("email")
    if enabled["sms"] and sms_number(sfo["phone"]):
        if _queue(conn, channel="sms", recipient=sfo["phone"], template=event, subject=None, body=msg["sms"],
                  sfo_id=sfo_id, dedupe_key=f"{event}:{sfo_id}:sms"):
            queued.append("sms")
    for addr in alerts:
        body = (f"Yeni web siparişi: {sfo['public_code']}\nMüşteri: {sfo['full_name']} ({sfo['city']})\n"
                f"Ödeme: {sfo['payment_method']} · {sfo['status']}\nToplam: {_money(sfo['total'])}\n\n"
                "Ayrıntılar TrendHub panelinde: Web Sitesi → Web siparişleri.")
        _queue(conn, channel="email", recipient=addr, template="owner_alert", subject=f"Yeni web siparişi {sfo['public_code']}",
               body=body, sfo_id=sfo_id, dedupe_key=f"owner_alert:{sfo_id}:{addr.lower()}")
    if queued:
        conn.execute(text("""UPDATE storefront_orders SET notified = notified || CAST(:n AS JSONB) WHERE id = :id"""),
                     {"n": json.dumps({event: queued}), "id": sfo_id})
    return queued


def send_password_reset(conn: Connection, customer: dict, reset_url: str) -> bool:
    if not (email_configured() and app_settings.get(conn, "storefront.notify_email", True)):
        return False
    body = (f"Merhaba {customer['full_name'].split(' ')[0]},\n\nŞifrenizi yenilemek için aşağıdaki bağlantıyı kullanın. "
            f"Bağlantı 1 saat geçerlidir ve yalnızca bir kez kullanılabilir.\n\n{reset_url}\n\n"
            f"Bu isteği siz yapmadıysanız bu e-postayı dikkate almayın.\n\n{BRAND}")
    return _queue(conn, channel="email", recipient=customer["email"], template="password_reset",
                  subject="Şifre yenileme", body=body, sfo_id=None, dedupe_key=None)


def scan_shipped(conn: Connection) -> int:
    """TrendHub'da kargoya verilen web siparişleri için 'shipped' bildirimi (bakım işi çağırır)."""
    enabled = channels_enabled(conn)
    if not (enabled["email"] or enabled["sms"]):
        return 0
    ids = [r.id for r in conn.execute(text("""
        SELECT so.id FROM storefront_orders so JOIN orders o ON o.id = so.order_id
         WHERE o.internal_status IN ('shipped', 'delivered') AND NOT (so.notified ? 'shipped')
           AND so.created_at > NOW() - INTERVAL '30 days'
         LIMIT 200"""))]
    n = 0
    for sfo_id in ids:
        notify_order(conn, sfo_id, "shipped")
        # Kanal kuyruğa eklenmese de (ör. telefon geçersiz) tekrar taranmasın
        conn.execute(text("""UPDATE storefront_orders SET notified = notified || '{"shipped": []}'::jsonb
                             WHERE id = :id AND NOT (notified ? 'shipped')"""), {"id": sfo_id})
        n += 1
    return n


def deliver_pending(conn_factory, limit: int = 50) -> dict:
    """Kuyruktaki bildirimleri gönderir. `conn_factory`: engine.begin benzeri bağlam yöneticisi üreticisi."""
    sent = failed = skipped = 0
    done: list[int] = [0]
    for _ in range(5):  # iş çalışırken eklenen yeni kayıtlar da aynı işte gönderilir
        with conn_factory() as conn:
            batch = [dict(r) for r in conn.execute(text("""
                SELECT id, channel, recipient, subject, body, attempts FROM notification_outbox
                 WHERE status = 'queued' AND id <> ALL(:done) ORDER BY id LIMIT :l FOR UPDATE SKIP LOCKED"""),
                {"l": limit, "done": done}).mappings()]
            for item in batch:
                conn.execute(text("UPDATE notification_outbox SET attempts = attempts + 1 WHERE id = :id"), {"id": item["id"]})
        if not batch:
            break
        done += [item["id"] for item in batch]
        s, f, k = _deliver_batch(conn_factory, batch)
        sent, failed, skipped = sent + s, failed + f, skipped + k
    return {"sent": sent, "retry_or_failed": failed, "skipped": skipped}


def _deliver_batch(conn_factory, batch: list[dict]) -> tuple[int, int, int]:
    sent = failed = skipped = 0
    for item in batch:
        status, provider, error = "sent", None, None
        try:
            if item["channel"] == "email":
                if not email_configured():
                    status = "skipped"
                else:
                    provider = send_email(item["recipient"], item["subject"] or BRAND, item["body"])
            else:
                if not sms_configured():
                    status = "skipped"
                else:
                    provider = send_sms(item["recipient"], item["body"])
        except Exception as exc:  # noqa: BLE001 - sağlayıcı hatası kuyruğu durdurmamalı
            error = f"{exc.__class__.__name__}: {str(exc)[:300]}"
            status = "failed" if item["attempts"] + 1 >= MAX_ATTEMPTS else "queued"
            log.warning("Bildirim #%s gönderilemedi: %s", item["id"], error)
        with conn_factory() as conn:
            conn.execute(text("""UPDATE notification_outbox SET status = :s, provider = COALESCE(:p, provider), last_error = :e,
                                 sent_at = CASE WHEN :s = 'sent' THEN NOW() ELSE sent_at END WHERE id = :id"""),
                         {"s": status, "p": provider, "e": error, "id": item["id"]})
        sent += status == "sent"
        failed += status in ("failed", "queued")
        skipped += status == "skipped"
    return sent, failed, skipped


def schedule_retry(conn: Connection) -> int | None:
    """Bakım işi: geçici hata nedeniyle kuyrukta kalan bildirimler için gönderim işi açar."""
    if conn.execute(text("SELECT 1 FROM notification_outbox WHERE status = 'queued' LIMIT 1")).first():
        return jobs.enqueue(conn, JOB_TYPE, payload={}, idempotency_key=JOB_TYPE, max_attempts=3)
    return None
