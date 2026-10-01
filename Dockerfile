FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PA_SC_DATA_ROOT=/app/data

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations

RUN pip install --no-cache-dir . \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

FROM runtime AS test

USER root

COPY tests ./tests

RUN pip install --no-cache-dir ".[dev]" \
    && chown -R app:app /app

USER app

CMD ["pytest", "-q", "--basetemp", "/tmp/pytest"]
