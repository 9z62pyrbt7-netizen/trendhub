"""TrendHub API uygulaması.

Şema değişiklikleri API açılışında YAPILMAZ; `alembic upgrade head`
(docker-compose'daki `migrate` servisi) ile uygulanır.
"""
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import (ads, ai, alerts, analytics, auth, catalog, integrations, orders, storefront_admin, suppliers, system,
                  transfer)
from .config import get_settings
from .db import transaction
from .logging_setup import configure_logging
from .security import bootstrap_admin

log = logging.getLogger("trendhub.api")
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "TrendHub"


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    settings = get_settings()
    try:
        with transaction() as conn:
            result = bootstrap_admin(conn, settings.admin_user, settings.admin_password)
        if result == "created":
            log.warning("İlk admin kullanıcısı oluşturuldu: %s", settings.admin_user)
        elif result in ("missing", "weak"):
            log.warning("Kullanıcı yok ve ADMIN_USER/ADMIN_PASSWORD geçersiz (%s). "
                        "Parola en az 12 karakter olmalı.", result)
    except Exception:  # noqa: BLE001 - migration henüz çalışmamış olabilir
        log.exception("Admin bootstrap atlandı (migration uygulandı mı?)")
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="TrendHub API", version="2.0.0", lifespan=lifespan,
                  docs_url="/api/docs" if settings.app_env != "production" else None,
                  redoc_url=None, openapi_url="/api/openapi.json" if settings.app_env != "production" else None)

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Content-Type", CSRF_HEADER, "Authorization"])

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        # Çerezle kimliği doğrulanan değiştirici isteklerde özel başlık zorunlu:
        # başka bir siteden gelen form/istek bu başlığı ekleyemez.
        if (request.method in MUTATING and request.url.path.startswith("/api/")
                and not request.headers.get("Authorization", "").lower().startswith("bearer ")
                and request.headers.get(CSRF_HEADER) != CSRF_VALUE):
            return JSONResponse({"detail": "CSRF doğrulaması başarısız"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        # Kullanıcıya sade Türkçe mesaj; teknik ayrıntı "Gelişmiş detay" için ayrı alanda.
        # Girilen değerler (parola/secret olabilir) yanıta KONMAZ.
        errors = [{"field": ".".join(str(x) for x in e.get("loc", [])[1:]), "message": _tr_validation(e)}
                  for e in exc.errors()]
        fields = ", ".join(sorted({e["field"] for e in errors if e["field"]}))
        return JSONResponse(status_code=422, content={
            "detail": "Girilen bilgilerde hata var" + (f": {fields}" if fields else "") + ".",
            "errors": errors,
            "technical": "; ".join(f"{e['field'] or 'istek'}: {e['message']}" for e in errors)})

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        ref = uuid.uuid4().hex[:10]
        log.exception("Beklenmeyen hata (ref %s) %s %s", ref, request.method, request.url.path)
        return JSONResponse(status_code=500, content={
            "detail": "Beklenmeyen bir hata oluştu. İşlem tamamlanmadı; lütfen tekrar deneyin.",
            "technical": f"Hata referansı: {ref} ({exc.__class__.__name__}). Ayrıntı sunucu kayıtlarında."})

    for r in (auth.router, analytics.router, orders.router, catalog.router, suppliers.router,
              transfer.router, integrations.router, system.router, alerts.router, ads.router, storefront_admin.router, ai.router):
        app.include_router(r)
    return app


_TR_MESSAGES = {
    "missing": "zorunlu alan", "string_too_short": "çok kısa", "string_too_long": "çok uzun",
    "string_pattern_mismatch": "geçersiz biçim", "int_parsing": "tam sayı olmalı", "float_parsing": "sayı olmalı",
    "decimal_parsing": "sayı olmalı", "greater_than": "daha büyük olmalı", "greater_than_equal": "çok küçük",
    "less_than_equal": "çok büyük", "less_than": "çok büyük", "date_from_datetime_parsing": "geçersiz tarih",
    "date_parsing": "geçersiz tarih", "bool_parsing": "evet/hayır olmalı", "too_short": "en az bir değer seçin",
    "too_long": "çok fazla değer", "json_invalid": "geçersiz istek", "value_error": "geçersiz değer",
    "enum": "geçersiz seçim", "list_type": "liste olmalı",
}


def _tr_validation(e: dict) -> str:
    if e.get("type") == "value_error":
        return str(e.get("msg", "")).removeprefix("Value error, ") or "geçersiz değer"
    return _TR_MESSAGES.get(e.get("type", ""), "geçersiz değer")


app = create_app()
