FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini main.py ./
COPY migrations ./migrations
COPY app ./app

RUN useradd --system --uid 10001 --home /app trendhub \
    && mkdir -p /var/cache/trendcantaniz/img && chown -R trendhub /var/cache/trendcantaniz
USER trendhub

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
