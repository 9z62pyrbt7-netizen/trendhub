"""Trendçantanız web mağazası (storefront) uygulaması.

TrendHub yönetim API'sinden AYRI bir süreçtir (`uvicorn app.storefront.main:app`, compose'da
`storefront` servisi). Yönetim uç noktalarını (/api/orders, /api/settings …) İÇERMEZ; yalnızca
herkese açık mağaza sayfalarını ve /api/store/* sepet uçlarını sunar. Veriyi TrendHub veritabanından
sunucu tarafında okur; tarayıcıya hiçbir secret, maliyet, tedarikçi veya pazaryeri bilgisi gönderilmez.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape as xml_escape

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import cookie_secure_for, get_settings
from ..db import get_conn, transaction
from ..logging_setup import configure_logging
from ..services import order_notifications
from . import cart as cart_svc
from . import accounts, catalog, checkout, images, payments, store_config
from .store_config import LEGAL_PAGES, fmt_try

log = logging.getLogger("trendhub.storefront")
ORDER_CODE_RE = re.compile(r"^TC\d{6}[A-Z0-9]{5}$")
HERE = Path(__file__).parent
STATIC_VERSION = str(int(max(p.stat().st_mtime for p in (HERE / "static").glob("*.*"))))
BRAND = "Trendçantanız"

env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html", "xml"]),
                  trim_blocks=True, lstrip_blocks=True)
env.globals.update(slugify=catalog.slugify, fmt_try=fmt_try, img=images.img_url, srcset=images.srcset, BRAND=BRAND, V=STATIC_VERSION,
                   LEGAL_PAGES=LEGAL_PAGES, paragraphs=catalog.paragraphs, SORTS=catalog.SORTS, CITIES=checkout.CITIES,
                   STATUS_LABELS=checkout.STATUS_LABELS, year=lambda: datetime.now().year)
env.filters["json"] = lambda v: Markup(json.dumps(v, ensure_ascii=False, default=str)
                                        .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))

def _csp(frame: tuple[str, ...] = ()) -> str:
    return ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            + (f"; frame-src {' '.join(frame)}" if frame else ""))


CSP = _csp()


def create_app() -> FastAPI:
    app = FastAPI(title="Trendçantanız", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.on_event("startup")
    def _startup():
        configure_logging()

    @app.middleware("http")
    async def head_as_get(request: Request, call_next):
        # HEAD istekleri (izleme, arama motorları) GET gibi işlenir; gövde sunucu tarafından gönderilmez.
        if request.method == "HEAD":
            request.scope["method"] = "GET"
            response = await call_next(request)
            return Response(status_code=response.status_code, headers={k: v for k, v in response.headers.items()
                                                                       if k.lower() != "content-length"})
        return await call_next(request)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/store/"):
            # Başka sitelerden istek: özel başlık zorunlu (formlar ekleyemez) + Origin kontrolü.
            if request.headers.get("X-Requested-With") != "Storefront" or not _same_origin(request):
                return JSONResponse({"detail": "Geçersiz istek"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.url.path.startswith(("/api/store/", "/sepet", "/odeme", "/siparis", "/hesap")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        with transaction() as conn:
            ctx = _ctx(request, conn, title="Sayfa bulunamadı", noindex=True)
            ctx["rail"] = catalog.featured(catalog.snapshot(conn), 8)
            return _render("404.html", ctx, status=exc.status_code if exc.status_code in (404, 410) else 404)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("Mağaza hatası %s %s", request.method, request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin."}, status_code=500)
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Trendçantanız</title>"
                            "<p style='font-family:sans-serif;padding:40px'>Beklenmeyen bir hata oluştu. "
                            "Lütfen biraz sonra tekrar deneyin.</p>", status_code=500)

    _routes(app)
    _account_routes(app)
    return app


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # aynı-origin fetch'lerin bazıları Origin göndermez; özel başlık yine zorunlu
    allowed = {h for h in (request.headers.get("x-forwarded-host"), request.headers.get("host")) if h}
    base = get_settings().storefront_base_url.strip().rstrip("/")
    if base:
        allowed.add(base.split("://", 1)[-1])
    origin_host = origin.split("://", 1)[-1]
    # Vekil sunucu Host başlığından portu düşürmüş olabilir: ana makine adı eşleşmesi yeterlidir.
    return origin_host in allowed or origin_host.split(":")[0] in {h.split(":")[0] for h in allowed}


def _render(name: str, ctx: dict, status: int = 200) -> HTMLResponse:
    return HTMLResponse(env.get_template(name).render(**ctx), status_code=status)


def _ctx(request: Request, conn: Connection, *, title: str | None = None, description: str | None = None,
         canonical: str | None = None, image: str | None = None, noindex: bool = False, og_type: str = "website") -> dict:
    cfg = store_config.load(conn)
    base = store_config.base_url(request)
    token = request.cookies.get(cart_svc.COOKIE)
    cart_id = cart_svc.find(conn, token)
    count = int(conn.execute(text("SELECT COALESCE(SUM(quantity), 0) FROM storefront_cart_items WHERE cart_id = :c"),
                             {"c": cart_id}).scalar()) if cart_id else 0
    snap = catalog.snapshot(conn)
    cats = list(snap.by_category().values())[:8]
    org = {"@context": "https://schema.org", "@type": "Organization", "name": BRAND, "url": base + "/",
           "logo": base + "/static/logo.svg"}
    same_as = [cfg.social[k] for k in ("instagram", "tiktok", "facebook", "pinterest", "youtube") if cfg.social.get(k)]
    if same_as:
        org["sameAs"] = same_as
    if cfg.seller.get("phone"):
        org["contactPoint"] = {"@type": "ContactPoint", "telephone": cfg.seller["phone"], "contactType": "customer service",
                               "areaServed": "TR", "availableLanguage": "Turkish"}
    customer = accounts.resolve(conn, request.cookies.get(accounts.COOKIE))
    return {
        "org_jsonld": org, "customer": customer,
        "request": request, "cfg": cfg, "base": base, "path": request.url.path,
        "title": f"{title} | {BRAND}" if title else f"{BRAND} — Kadın Çanta",
        "description": description or "Trendçantanız kadın çanta koleksiyonu: omuz, çapraz ve el çantaları. Güvenli ödeme, kolay sipariş.",
        "canonical": base + (canonical if canonical is not None else request.url.path),
        "og_image": image, "noindex": noindex, "og_type": og_type, "cart_count": count, "nav_categories": cats,
    }


def _cart_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(cart_svc.COOKIE, token, max_age=cart_svc.TTL_DAYS * 86400, httponly=True, samesite="lax",
                        secure=cookie_secure_for(get_settings(), request.headers.get("x-forwarded-proto") or request.url.scheme),
                        path="/")


def _ip(request: Request) -> str | None:
    return request.headers.get("X-Real-IP") or (request.client.host if request.client else None)


_hits: dict[str, deque] = defaultdict(deque)


def _invalid(exc: ValidationError) -> JSONResponse:
    fields = {}
    for e in exc.errors():
        key = str(e["loc"][0]) if e.get("loc") else "_"
        msg = str(e.get("msg", "")).removeprefix("Value error, ")
        if e.get("type") == "missing":
            msg = "Bu alan zorunlu"
        elif e.get("type") == "string_too_short":
            msg = "Çok kısa"
        elif e.get("type") == "string_too_long":
            msg = "Çok uzun"
        fields[key] = msg
    return JSONResponse({"detail": "Lütfen işaretli alanları kontrol edin.", "fields": fields}, status_code=422)


def _rate_limit(key: str, limit: int, seconds: int) -> None:
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > seconds:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Çok fazla deneme. Lütfen biraz sonra tekrar deneyin.")
    q.append(now)


def _group_json(g: catalog.Group) -> dict:
    return {"id": g.id, "title": g.title, "url": g.url, "price": fmt_try(g.price),
            "image": images.img_url(g.images[0], 160) if g.images else None, "in_stock": g.in_stock}


def _product_jsonld(base: str, g: catalog.Group, cfg) -> dict:
    offers = []
    for v in g.variants:
        offer = {"@type": "Offer", "url": base + v.url, "priceCurrency": "TRY", "price": str(v.price),
                 "availability": "https://schema.org/" + ("InStock" if v.in_stock else "OutOfStock"),
                 "itemCondition": "https://schema.org/NewCondition",
                 "seller": {"@type": "Organization", "name": cfg.seller.get("title") or BRAND}}
        if v.sku:
            offer["sku"] = v.sku
        if v.barcode and v.barcode.isdigit() and len(v.barcode) in (8, 12, 13, 14):
            offer["gtin"] = v.barcode
        offers.append(offer)
    data = {"@context": "https://schema.org", "@type": "Product", "name": g.title,
            "image": [img if img.startswith("http") else base + img for img in g.images[:6]],
            "description": catalog.plain(g.rep.description, 5000) or g.title,
            "brand": {"@type": "Brand", "name": g.rep.brand or BRAND}, "offers": offers}
    if g.rep.sku:
        data["sku"] = g.rep.sku
    if g.category:
        data["category"] = g.category
    if len(g.variants) > 1:
        data["productGroupID"] = g.key
    return data


class CartIn(BaseModel):
    product_id: int
    quantity: int = Field(1, ge=0, le=cart_svc.MAX_QTY)


def _routes(app: FastAPI) -> None:
    # ------------------------------------------------------------- sayfalar
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, conn: Connection = Depends(get_conn)):
        snap = catalog.snapshot(conn)
        hero, _reason = catalog.hero(conn, snap)
        best = catalog.bestsellers(snap, 8)
        ctx = _ctx(request, conn, canonical="/", image=hero.images[0] if hero and hero.images else None)
        exclude = {hero.key} if hero else set()
        editorial = next((g for g in (best + catalog.featured(snap, 12)) if g.key not in exclude and len(g.images) > 1), None) \
            or (hero if hero and len(hero.images) > 1 else None)
        cats = list(snap.by_category().values())
        ctx.update(hero=hero, bestsellers=best, has_sales=snap.has_sales,
                   featured=[] if best else catalog.featured(snap, 8),
                   new_arrivals=catalog.new_arrivals(snap, 8) if len(snap.groups) >= 6 else [],
                   categories=cats[:6], editorial=editorial, total_groups=len(snap.groups))
        return _render("home.html", ctx)

    @app.get("/urun/{slug}", response_class=HTMLResponse)
    def product_page(slug: str, request: Request, conn: Connection = Depends(get_conn)):
        pid = catalog.parse_product_slug(slug)
        g = catalog.product_group(conn, pid) if pid else None
        if g is None:
            raise HTTPException(404)
        v = next(x for x in g.variants if x.id == pid)
        if request.url.path != v.url:
            return RedirectResponse(v.url, status_code=301)
        ctx = _ctx(request, conn, title=v.seo_title or v.title, description=v.seo_description or catalog.plain(v.description) or
                   f"{v.title} — {fmt_try(v.price)}. {BRAND} güvencesiyle güvenli ödeme.", image=v.images[0] if v.images else None,
                   og_type="product")
        snap = catalog.snapshot(conn)
        related = [x for x in snap.groups if x.key != g.key and x.in_stock and (x.category == g.category)][:8]
        if len(related) < 4:
            related += [x for x in catalog.featured(snap, 12) if x.key != g.key and x not in related][: 8 - len(related)]
        crumbs = [("Ana sayfa", "/")]
        if g.category:
            crumbs.append((g.category, f"/kategori/{catalog.slugify(g.category)}"))
        crumbs.append((v.title, v.url))
        ctx.update(group=g, v=v, related=related[:8], jsonld=_product_jsonld(ctx["base"], g, ctx["cfg"]),
                   breadcrumb_jsonld={"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
                       {"@type": "ListItem", "position": i, "name": n, "item": ctx["base"] + u}
                       for i, (n, u) in enumerate(crumbs, start=1)]})
        return _render("product.html", ctx)

    def _listing(request: Request, conn: Connection, *, groups: list, title: str, heading: str, canonical: str,
                 intro: str | None = None, sort_default: str = "onerilen", active_cat: str | None = None, noindex=False):
        sort = request.query_params.get("sirala") or sort_default
        if sort not in catalog.SORTS:
            sort = sort_default
        groups = catalog.sort_groups(groups, sort)
        try:
            page = max(1, int(request.query_params.get("sayfa", "1")))
        except ValueError:
            page = 1
        per = 24
        pages = max(1, (len(groups) + per - 1) // per)
        page = min(page, pages)
        ctx = _ctx(request, conn, title=title, canonical=canonical + (f"?sayfa={page}" if page > 1 else ""), noindex=noindex,
                   description=intro)
        snap = catalog.snapshot(conn)
        ctx.update(groups=groups[(page - 1) * per: page * per], total=len(groups), heading=heading, intro=intro,
                   sort=sort, page=page, pages=pages, categories=list(snap.by_category().values()), active_cat=active_cat,
                   listing_path=request.url.path)
        return _render("collection.html", ctx)

    @app.get("/urunler", response_class=HTMLResponse)
    def all_products(request: Request, conn: Connection = Depends(get_conn)):
        snap = catalog.snapshot(conn)
        return _listing(request, conn, groups=snap.groups, title="Tüm Çantalar", heading="Tüm Çantalar", canonical="/urunler")

    @app.get("/cok-satanlar", response_class=HTMLResponse)
    def best_page(request: Request, conn: Connection = Depends(get_conn)):
        snap = catalog.snapshot(conn)
        groups = [g for g in snap.groups if g.sold > 0] or snap.groups
        heading = "Çok Satanlar" if snap.has_sales else "Öne Çıkanlar"
        return _listing(request, conn, groups=groups, title=heading, heading=heading, canonical="/cok-satanlar",
                        sort_default="cok-satan" if snap.has_sales else "onerilen",
                        intro="Son 90 günde en çok tercih edilen çantalar." if snap.has_sales else None)

    @app.get("/yeni-gelenler", response_class=HTMLResponse)
    def new_page(request: Request, conn: Connection = Depends(get_conn)):
        snap = catalog.snapshot(conn)
        return _listing(request, conn, groups=snap.groups, title="Yeni Gelenler", heading="Yeni Gelenler",
                        canonical="/yeni-gelenler", sort_default="yeni")

    @app.get("/kategori/{slug}", response_class=HTMLResponse)
    def category_page(slug: str, request: Request, conn: Connection = Depends(get_conn)):
        cat = catalog.snapshot(conn).by_category().get(slug)
        if cat is None:
            raise HTTPException(404)
        return _listing(request, conn, groups=cat["groups"], title=cat["label"], heading=cat["label"],
                        canonical=f"/kategori/{slug}", active_cat=slug)

    @app.get("/ara", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", conn: Connection = Depends(get_conn)):
        q = q.strip()[:100]
        groups = catalog.search(catalog.snapshot(conn), q) if q else []
        heading = f"“{q}” için sonuçlar" if q else "Ara"
        return _listing(request, conn, groups=groups, title=heading, heading=heading, canonical="/ara", noindex=True)

    @app.get("/sepet", response_class=HTMLResponse)
    def cart_page(request: Request, conn: Connection = Depends(get_conn)):
        cid = cart_svc.find(conn, request.cookies.get(cart_svc.COOKIE))
        ctx = _ctx(request, conn, title="Sepetim", noindex=True)
        ctx.update(cart=cart_svc.view(conn, cid), rail=catalog.featured(catalog.snapshot(conn), 8))
        return _render("cart.html", ctx)

    @app.get("/odeme", response_class=HTMLResponse)
    def checkout_page(request: Request, conn: Connection = Depends(get_conn)):
        cid = cart_svc.find(conn, request.cookies.get(cart_svc.COOKIE))
        cv = cart_svc.view(conn, cid)
        if not cv.lines:
            return RedirectResponse("/sepet", status_code=303)
        ctx = _ctx(request, conn, title="Ödeme", noindex=True)
        pre: dict = {}
        if ctx["customer"]:
            c = ctx["customer"]
            addrs = accounts.addresses(conn, c["id"])
            pre = {**(addrs[0] if addrs else {}), "email": c["email"]}
            pre.setdefault("full_name", c["full_name"])
            pre.setdefault("phone", c["phone"])
        ctx.update(cart=cv, methods=ctx["cfg"].payment_methods(), blockers=ctx["cfg"].checkout_blockers(), pre=pre)
        return _render("checkout.html", ctx)

    # ------------------------------------------------------------- kartla ödeme
    @app.get("/odeme/kart/{code}", response_class=HTMLResponse)
    def card_payment(code: str, request: Request, t: str | None = None):
        with transaction() as conn:
            sfo = checkout.find_public(conn, code, t)
            if sfo is None:
                raise HTTPException(404)
            order_url = f"/siparis/{code}?t={t}"
            if sfo["status"] != "pending_payment" or sfo["payment_method"] != "card":
                return RedirectResponse(order_url, status_code=303)
            provider = payments.card_provider()
            ctx = _ctx(request, conn, title="Kartla ödeme", noindex=True)
            ctx.update(o=sfo, order_url=order_url, provider=provider, start=None, error=None)
            if provider is None:
                ctx["error"] = "Kartla ödeme şu anda kullanılamıyor. Siparişiniz ödeme alınmadan oluşturulmaz."
                return _render("card_payment.html", ctx)
            base = store_config.base_url(request)
            try:
                start = provider.start(sfo, ok_url=base + order_url, fail_url=base + order_url + "&odeme=basarisiz",
                                       callback_url=f"{base}/odeme/geri-donus/{provider.code}?c={code}&t={t}",
                                       user_ip=_ip(request) or "127.0.0.1")
            except payments.PaymentNotConfigured as exc:
                ctx["error"] = str(exc)
                return _render("card_payment.html", ctx)
            conn.execute(text("""UPDATE storefront_orders SET payment_provider = :p,
                                 payment_reference = COALESCE(:r, payment_reference), updated_at = NOW() WHERE id = :id"""),
                         {"p": provider.code, "r": start.reference, "id": sfo["id"]})
            conn.execute(text("""INSERT INTO storefront_payment_events(storefront_order_id, provider, kind, verified, details)
                                 VALUES (:o, :p, 'start', FALSE, '{}'::jsonb)"""), {"o": sfo["id"], "p": provider.code})
        if start.redirect_url:
            return RedirectResponse(start.redirect_url, status_code=303)
        ctx["start"] = start
        response = _render("card_payment.html", ctx)
        response.headers["Content-Security-Policy"] = _csp(frame=provider.frame_src)
        return response

    @app.post("/odeme/geri-donus/{provider_code}")
    async def payment_callback(provider_code: str, request: Request, c: str | None = None, t: str | None = None):
        """Sağlayıcı geri bildirimi. PayTR: sunucudan sunucuya bildirim ('OK' beklenir). iyzico: tarayıcı yönlendirmesi."""
        _rate_limit(f"paycb:{_ip(request)}", 120, 60)
        provider = payments.card_provider()
        if provider is None or provider.code != provider_code:
            raise HTTPException(404)
        raw = await request.body()
        if len(raw) > 65536:
            raise HTTPException(413)
        # Sağlayıcılar application/x-www-form-urlencoded gönderir (ek bağımlılık gerektirmeden ayrıştırılır).
        form = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
        if c:
            form["_code"] = c
        code = str(provider.order_code(form) or "")
        outcome = "unknown"
        sfo = None
        with transaction() as conn:
            if ORDER_CODE_RE.match(code):
                sfo = conn.execute(text("SELECT * FROM storefront_orders WHERE public_code = :c FOR UPDATE"),
                                   {"c": code}).mappings().first()
            if sfo is None:
                log.warning("Ödeme geri bildirimi: sipariş bulunamadı (%s)", provider_code)
            else:
                result = provider.verify_callback(form, dict(sfo))
                conn.execute(text("""INSERT INTO storefront_payment_events(storefront_order_id, provider, kind, verified, details)
                                     VALUES (:o, :p, 'callback', :v, CAST(:d AS JSONB))"""),
                             {"o": sfo["id"], "p": provider.code, "v": result.verified,
                              "d": json.dumps({"paid": result.paid, "message": result.message, **result.details},
                                              ensure_ascii=False, default=str)})
                if result.verified and result.paid:
                    if sfo["status"] in ("pending_payment", "expired", "payment_failed"):
                        checkout.confirm_payment(conn, sfo["id"], reference=result.reference, provider=provider.code,
                                                 allow_expired=True)
                    outcome = "paid"
                elif result.verified:
                    outcome = "failed"
                else:
                    outcome = "invalid"
                    log.warning("Ödeme geri bildirimi doğrulanamadı (%s %s): %s", provider_code, code, result.message)
        if provider.code == "paytr":
            # PayTR, 'OK' yanıtı alana kadar bildirimi tekrarlar; doğrulanamayan istek için OK dönülmez.
            ok = outcome in ("paid", "failed")
            return PlainTextResponse("OK" if ok else "FAIL", status_code=200 if ok else 400)
        if sfo is None or not t:
            raise HTTPException(404)
        suffix = "" if outcome == "paid" else "&odeme=basarisiz"
        return RedirectResponse(f"/siparis/{code}?t={t}{suffix}", status_code=303)

    @app.get("/siparis/{code}", response_class=HTMLResponse)
    def order_page(code: str, request: Request, t: str | None = None, conn: Connection = Depends(get_conn)):
        sfo = checkout.find_public(conn, code, t)
        if sfo is None:
            raise HTTPException(404)
        ctx = _ctx(request, conn, title=f"Sipariş {code}", noindex=True)
        from ..domain.order_status import LABELS_TR
        ctx.update(o=sfo, t=t, payment_failed=request.query_params.get("odeme") == "basarisiz",
                   card_available=ctx["cfg"].card_available, fulfilment=LABELS_TR.get(sfo.get("internal_status")) if sfo.get("internal_status") else None)
        return _render("order.html", ctx)

    @app.get("/sayfa/{slug}", response_class=HTMLResponse)
    def legal_page(slug: str, request: Request, conn: Connection = Depends(get_conn)):
        if slug not in LEGAL_PAGES:
            raise HTTPException(404)
        cfg = store_config.load(conn)
        body = str(cfg.legal.get(slug) or "").strip()
        if not body:
            raise HTTPException(404)
        title = LEGAL_PAGES[slug][0]
        ctx = _ctx(request, conn, title=title)
        ctx.update(page_title=title, paragraphs_=[p for p in body.split("\n")])
        return _render("page.html", ctx)

    @app.get("/iletisim", response_class=HTMLResponse)
    def contact_page(request: Request, conn: Connection = Depends(get_conn)):
        ctx = _ctx(request, conn, title="İletişim")
        return _render("contact.html", ctx)

    # ------------------------------------------------------------- SEO
    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots(request: Request):
        base = store_config.base_url(request)
        return ("User-agent: *\nDisallow: /sepet\nDisallow: /odeme\nDisallow: /siparis/\nDisallow: /api/\n"
                "Disallow: /hesap/\nDisallow: /hesabim\n"
                f"Disallow: /ara\n\nSitemap: {base}/sitemap.xml\n")

    @app.get("/sitemap.xml")
    def sitemap(request: Request, conn: Connection = Depends(get_conn)):
        base = store_config.base_url(request)
        snap = catalog.snapshot(conn)
        cfg = store_config.load(conn)
        urls = [("/", "daily"), ("/urunler", "daily"), ("/yeni-gelenler", "daily"), ("/cok-satanlar", "daily")]
        urls += [(f"/kategori/{c['slug']}", "daily") for c in snap.by_category().values()]
        urls += [(v.url, "weekly") for g in snap.groups for v in g.variants]
        urls += [(f"/sayfa/{s}", "monthly") for s in LEGAL_PAGES if str(cfg.legal.get(s) or "").strip()]
        body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        body += [f"<url><loc>{xml_escape(base + u)}</loc><changefreq>{f}</changefreq></url>" for u, f in urls]
        body.append("</urlset>")
        return Response("\n".join(body), media_type="application/xml")

    @app.get("/img/{w}/{sig}/{b}")
    def image(w: int, sig: str, b: str):
        src = images.decode(w, sig, b)
        if src is None:
            raise HTTPException(404)
        try:
            path = images.render(src, w)
        except Exception as exc:  # noqa: BLE001 - kaynak erişilemezse özgün adrese yönlendir
            log.warning("Görsel işlenemedi (%s): %s", exc.__class__.__name__, src[:120])
            return RedirectResponse(src, status_code=302)
        return FileResponse(path, media_type="image/webp",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # ------------------------------------------------------------- JSON uçları
    @app.get("/api/store/health")
    def health(conn: Connection = Depends(get_conn)):
        conn.execute(text("SELECT 1"))
        return {"status": "healthy"}

    @app.get("/api/store/cart")
    def get_cart(request: Request, conn: Connection = Depends(get_conn)):
        cid = cart_svc.find(conn, request.cookies.get(cart_svc.COOKIE))
        return cart_svc.as_json(cart_svc.view(conn, cid))

    def _cart_change(request: Request, conn: Connection, body: CartIn, add: bool):
        _rate_limit(f"cart:{_ip(request)}", 120, 60)
        cid, token, created = cart_svc.ensure(conn, request.cookies.get(cart_svc.COOKIE))
        try:
            res = cart_svc.add(conn, cid, body.product_id, body.quantity) if add else \
                cart_svc.set_quantity(conn, cid, body.product_id, body.quantity)
        except cart_svc.CartError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        data = cart_svc.as_json(cart_svc.view(conn, cid))
        if res.get("limited"):
            data["notice"] = f"Stokta {res.get('available')} adet var; sepetiniz buna göre güncellendi."
        response = JSONResponse(data)
        if created:
            _cart_cookie(response, request, token)
        return response

    @app.post("/api/store/cart/add")
    def cart_add(body: CartIn, request: Request, conn: Connection = Depends(get_conn)):
        return _cart_change(request, conn, body, add=True)

    @app.post("/api/store/cart/update")
    def cart_update(body: CartIn, request: Request, conn: Connection = Depends(get_conn)):
        return _cart_change(request, conn, body, add=False)

    @app.get("/api/store/search")
    def search_json(q: str = "", conn: Connection = Depends(get_conn)):
        q = q.strip()[:100]
        snap = catalog.snapshot(conn)
        hits = catalog.search(snap, q, 6) if len(q) >= 2 else []
        cats = [c for c in snap.by_category().values() if catalog.fold(q) in catalog.fold(c["label"])][:4] if q else []
        return {"q": q, "products": [_group_json(g) for g in hits],
                "categories": [{"label": c["label"], "url": f"/kategori/{c['slug']}"} for c in cats]}

    @app.post("/api/store/checkout")
    async def checkout_submit(request: Request):
        _rate_limit(f"checkout:{_ip(request)}", 30, 600)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"detail": "Geçersiz istek"}, status_code=400)
        try:
            data = checkout.CheckoutIn(**(payload if isinstance(payload, dict) else {}))
        except ValidationError as exc:
            return _invalid(exc)
        token = request.cookies.get(cart_svc.COOKIE)
        try:
            with transaction() as conn:
                cid = cart_svc.find(conn, token)
                customer = accounts.resolve(conn, request.cookies.get(accounts.COOKIE))
                customer_id = customer["id"] if customer else None
                result = checkout.place_order(conn, cid, data, _ip(request), customer_id=customer_id)
                url = f"/siparis/{result['public_code']}?t={result['access_token']}"
                if result["status"] != "pending_payment":
                    order_notifications.notify_order(conn, result["id"], "order_received",
                                               order_url=store_config.base_url(request) + url)
        except checkout.CheckoutError as exc:
            return JSONResponse({"detail": str(exc), "fields": exc.fields}, status_code=422)
        if result["status"] == "pending_payment":
            url = f"/odeme/kart/{result['public_code']}?t={result['access_token']}"
        return {"ok": True, "public_code": result["public_code"], "status": result["status"], "redirect": url}



def _safe_next(v: str | None) -> str:
    """Açık yönlendirme engeli: yalnızca site içi yol."""
    return v if v and v.startswith("/") and not v.startswith("//") and "\\" not in v and len(v) < 300 else "/hesabim"


def _session_cookie(response: Response, request: Request, token: str | None) -> None:
    if token is None:
        response.delete_cookie(accounts.COOKIE, path="/")
        return
    response.set_cookie(accounts.COOKIE, token, max_age=accounts.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=cookie_secure_for(get_settings(), request.headers.get("x-forwarded-proto") or request.url.scheme),
                        path="/")


def _account_routes(app: FastAPI) -> None:
    def _me(request: Request, conn: Connection) -> dict | None:
        return accounts.resolve(conn, request.cookies.get(accounts.COOKIE))

    async def _body(request: Request, model):
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return None, JSONResponse({"detail": "Geçersiz istek"}, status_code=400)
        try:
            return model(**(payload if isinstance(payload, dict) else {})), None
        except ValidationError as exc:
            return None, _invalid(exc)

    def _err(exc: accounts.AccountError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "fields": exc.fields}, status_code=exc.status)

    # ------------------------------------------------------------- sayfalar
    @app.get("/hesap/giris", response_class=HTMLResponse)
    def login_page(request: Request, sonra: str | None = None, conn: Connection = Depends(get_conn)):
        if _me(request, conn):
            return RedirectResponse(_safe_next(sonra), status_code=303)
        ctx = _ctx(request, conn, title="Giriş yap / Üye ol", noindex=True)
        ctx.update(next=_safe_next(sonra), reset_done=request.query_params.get("yenilendi") == "1")
        return _render("account_login.html", ctx)

    @app.get("/hesap/sifremi-unuttum", response_class=HTMLResponse)
    def forgot_page(request: Request, conn: Connection = Depends(get_conn)):
        ctx = _ctx(request, conn, title="Şifremi unuttum", noindex=True)
        ctx["email_available"] = order_notifications.email_configured()
        return _render("account_forgot.html", ctx)

    @app.get("/hesap/sifre-yenile", response_class=HTMLResponse)
    def reset_page(request: Request, anahtar: str | None = None, conn: Connection = Depends(get_conn)):
        ctx = _ctx(request, conn, title="Yeni şifre", noindex=True)
        ctx["token"] = anahtar or ""
        return _render("account_reset.html", ctx)

    @app.get("/hesabim", response_class=HTMLResponse)
    def account_page(request: Request, conn: Connection = Depends(get_conn)):
        me = _me(request, conn)
        if me is None:
            return RedirectResponse("/hesap/giris?sonra=/hesabim", status_code=303)
        ctx = _ctx(request, conn, title="Hesabım", noindex=True)
        ctx.update(me=me, orders=accounts.orders(conn, me["id"]), addresses=accounts.addresses(conn, me["id"]))
        return _render("account.html", ctx)

    @app.get("/hesabim/siparis/{code}", response_class=HTMLResponse)
    def account_order(code: str, request: Request, conn: Connection = Depends(get_conn)):
        me = _me(request, conn)
        if me is None:
            return RedirectResponse(f"/hesap/giris?sonra=/hesabim/siparis/{code}", status_code=303)
        sfo = accounts.order(conn, me["id"], code)
        if sfo is None:
            raise HTTPException(404)
        from ..domain.order_status import LABELS_TR
        ctx = _ctx(request, conn, title=f"Sipariş {code}", noindex=True)
        ctx.update(o=sfo, t=None, payment_failed=False, card_available=False, from_account=True,
                   fulfilment=LABELS_TR.get(sfo.get("internal_status")) if sfo.get("internal_status") else None)
        return _render("order.html", ctx)

    # ------------------------------------------------------------- JSON uçları
    @app.post("/api/store/account/register")
    async def register(request: Request):
        _rate_limit(f"acct:{_ip(request)}", 20, 600)
        data, bad = await _body(request, accounts.RegisterIn)
        if bad:
            return bad
        try:
            with transaction() as conn:
                cid = accounts.register(conn, data)
                token = accounts.create_session(conn, cid)
        except accounts.AccountError as exc:
            return _err(exc)
        response = JSONResponse({"ok": True, "redirect": _safe_next(request.query_params.get("sonra"))})
        _session_cookie(response, request, token)
        return response

    @app.post("/api/store/account/login")
    async def login(request: Request):
        _rate_limit(f"login:{_ip(request)}", 20, 600)
        data, bad = await _body(request, accounts.LoginIn)
        if bad:
            return bad
        with transaction() as conn:
            cid, error = accounts.authenticate(conn, data.email, data.password)
            token = accounts.create_session(conn, cid) if cid else None
        if error:
            return JSONResponse({"detail": error, "fields": {}}, status_code=401)
        response = JSONResponse({"ok": True, "redirect": _safe_next(request.query_params.get("sonra"))})
        _session_cookie(response, request, token)
        return response

    @app.post("/api/store/account/logout")
    def logout(request: Request, conn: Connection = Depends(get_conn)):
        accounts.logout(conn, request.cookies.get(accounts.COOKIE))
        response = JSONResponse({"ok": True, "redirect": "/"})
        _session_cookie(response, request, None)
        return response

    @app.post("/api/store/account/password/forgot")
    async def forgot(request: Request):
        _rate_limit(f"forgot:{_ip(request)}", 5, 600)
        data, bad = await _body(request, accounts.ForgotIn)
        if bad:
            return bad
        if not order_notifications.email_configured():
            return JSONResponse({"detail": "Şifre yenileme e-postası şu anda gönderilemiyor. Lütfen iletişim sayfasındaki "
                                           "bilgilerden bize ulaşın."}, status_code=503)
        with transaction() as conn:
            res = accounts.create_reset(conn, data.email)
            if res:
                customer, token = res
                order_notifications.send_password_reset(conn, customer, f"{store_config.base_url(request)}/hesap/sifre-yenile?anahtar={token}")
        return {"ok": True, "message": "Bu e-posta adresine kayıtlı bir hesap varsa şifre yenileme bağlantısı gönderildi."}

    @app.post("/api/store/account/password/reset")
    async def reset(request: Request):
        _rate_limit(f"reset:{_ip(request)}", 10, 600)
        data, bad = await _body(request, accounts.ResetIn)
        if bad:
            return bad
        try:
            with transaction() as conn:
                accounts.reset_password(conn, data)
        except accounts.AccountError as exc:
            return _err(exc)
        response = JSONResponse({"ok": True, "redirect": "/hesap/giris?yenilendi=1"})
        _session_cookie(response, request, None)
        return response

    def _authed(request: Request, conn: Connection) -> dict:
        me = _me(request, conn)
        if me is None:
            raise HTTPException(401, "Oturumunuz sona erdi. Lütfen tekrar giriş yapın.")
        return me

    @app.post("/api/store/account/password/change")
    async def change_password(request: Request):
        _rate_limit(f"pwchange:{_ip(request)}", 10, 600)
        data, bad = await _body(request, accounts.ChangePasswordIn)
        if bad:
            return bad
        try:
            with transaction() as conn:
                me = _authed(request, conn)
                accounts.change_password(conn, me["id"], data, request.cookies.get(accounts.COOKIE))
        except accounts.AccountError as exc:
            return _err(exc)
        return {"ok": True, "message": "Şifreniz güncellendi. Diğer cihazlardaki oturumlar kapatıldı."}

    @app.post("/api/store/account/profile")
    async def profile(request: Request):
        data, bad = await _body(request, accounts.ProfileIn)
        if bad:
            return bad
        with transaction() as conn:
            accounts.update_profile(conn, _authed(request, conn)["id"], data)
        return {"ok": True, "message": "Bilgileriniz kaydedildi."}

    @app.post("/api/store/account/addresses")
    async def add_address(request: Request):
        data, bad = await _body(request, accounts.AddressIn)
        if bad:
            return bad
        try:
            with transaction() as conn:
                accounts.save_address(conn, _authed(request, conn)["id"], data)
        except accounts.AccountError as exc:
            return _err(exc)
        return {"ok": True, "message": "Adres kaydedildi."}

    @app.post("/api/store/account/addresses/{address_id}")
    async def update_address(address_id: int, request: Request):
        data, bad = await _body(request, accounts.AddressIn)
        if bad:
            return bad
        try:
            with transaction() as conn:
                accounts.save_address(conn, _authed(request, conn)["id"], data, address_id)
        except accounts.AccountError as exc:
            return _err(exc)
        return {"ok": True, "message": "Adres güncellendi."}

    @app.post("/api/store/account/addresses/{address_id}/delete")
    def delete_address(address_id: int, request: Request, conn: Connection = Depends(get_conn)):
        try:
            accounts.delete_address(conn, _authed(request, conn)["id"], address_id)
        except accounts.AccountError as exc:
            return _err(exc)
        return {"ok": True}


app = create_app()
