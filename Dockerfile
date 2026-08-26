FROM python:3.12.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10001 openreviewer \
    && useradd --system --uid 10001 --gid openreviewer \
        --home-dir /nonexistent --shell /usr/sbin/nologin openreviewer

WORKDIR /app

COPY --chown=openreviewer:openreviewer pyproject.toml README.md ./
COPY --chown=openreviewer:openreviewer apps ./apps
COPY --chown=openreviewer:openreviewer domain ./domain
COPY --chown=openreviewer:openreviewer persistence ./persistence
COPY --chown=openreviewer:openreviewer services ./services
COPY --chown=openreviewer:openreviewer knowledge ./knowledge
COPY --chown=openreviewer:openreviewer alembic.ini ./alembic.ini
COPY --chown=openreviewer:openreviewer migrations ./migrations

RUN python -m pip install --no-cache-dir .

USER openreviewer

EXPOSE 18090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; response = urlopen('http://127.0.0.1:18090/healthz', timeout=3); assert response.status == 200"

CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "18090", "--no-access-log"]
