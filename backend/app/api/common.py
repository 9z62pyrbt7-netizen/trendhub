"""Router'larda ortak yardımcılar."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Query

TZ = ZoneInfo("Europe/Istanbul")
PERIODS = {"today": 0, "7d": 6, "30d": 29, "90d": 89, "365d": 364}


class DateRange:
    """Türkiye saatine göre gün sınırları; sorgularda UTC [start, end)."""

    def __init__(self, period: str = Query("30d"), date_from: date | None = Query(None),
                 date_to: date | None = Query(None)):
        today = datetime.now(TZ).date()
        if date_from or date_to:
            start_d = date_from or (date_to or today) - timedelta(days=29)
            end_d = date_to or today
            period = "custom"
        else:
            if period not in PERIODS:
                raise HTTPException(422, f"Geçersiz dönem: {period}")
            end_d = today
            start_d = today - timedelta(days=PERIODS[period])
        if start_d > end_d:
            raise HTTPException(422, "Başlangıç tarihi bitişten sonra olamaz")
        if (end_d - start_d).days > 731:
            raise HTTPException(422, "En fazla 2 yıllık aralık seçilebilir")
        self.period = period
        self.start_date, self.end_date = start_d, end_d
        self.start = datetime.combine(start_d, time.min, TZ).astimezone(timezone.utc)
        self.end = datetime.combine(end_d + timedelta(days=1), time.min, TZ).astimezone(timezone.utc)

    def params(self) -> dict:
        return {"start": self.start, "end": self.end, "start_date": self.start_date, "end_date": self.end_date}

    def as_dict(self) -> dict:
        return {"period": self.period, "from": self.start_date.isoformat(), "to": self.end_date.isoformat()}


class Page:
    def __init__(self, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200)):
        self.page, self.page_size = page, page_size
        self.offset = (page - 1) * page_size


def paged(items: list, total: int, page: Page) -> dict:
    return {"items": items, "total": total, "page": page.page, "page_size": page.page_size,
            "pages": (total + page.page_size - 1) // page.page_size if total else 0}


def not_found(what: str = "Kayıt"):
    return HTTPException(404, f"{what} bulunamadı")
