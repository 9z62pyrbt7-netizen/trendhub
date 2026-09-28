"""TrendHub API uygulaması.

Şema değişiklikleri API açılışında YAPILMAZ; `alembic upgrade head`
(docker-compose'daki `migrate` servisi) ile uygulanır.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import analytics, auth, catalog, integrations, orders, system
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

    for r in (auth.router, analytics.router, orders.router, catalog.router, integrations.router, system.router):
        app.include_router(r)
    return app


app = create_app()
