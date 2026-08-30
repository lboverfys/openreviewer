FROM python:3.12.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_CONSTRAINT=/app/requirements.lock

# 基础镜像发布后 Debian 安全仓库仍可能追加补丁；在构建时同步已安装的
# 运行时包，避免把已修复的系统漏洞带进最终镜像。
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 openreviewer \
    && useradd --system --uid 10001 --gid openreviewer \
        --home-dir /nonexistent --shell /usr/sbin/nologin openreviewer

WORKDIR /app

COPY --chown=openreviewer:openreviewer pyproject.toml requirements.lock README.md ./
COPY --chown=openreviewer:openreviewer apps ./apps
COPY --chown=openreviewer:openreviewer domain ./domain
COPY --chown=openreviewer:openreviewer persistence ./persistence
COPY --chown=openreviewer:openreviewer services ./services
COPY --chown=openreviewer:openreviewer knowledge ./knowledge
COPY --chown=openreviewer:openreviewer alembic.ini ./alembic.ini
COPY --chown=openreviewer:openreviewer migrations ./migrations

# pip/setuptools 只用于构建；运行时移除它们及其 vendored 组件，缩小镜像攻击面。
RUN python -m pip install --no-cache-dir "pip==26.2.1" \
    && python -m pip install --no-cache-dir . \
    && python -m pip uninstall --yes pip setuptools

USER openreviewer

EXPOSE 18090 18091

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; response = urlopen('http://127.0.0.1:18090/healthz', timeout=3); assert response.status == 200"

CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "18090", "--no-access-log"]
