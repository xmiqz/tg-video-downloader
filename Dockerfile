FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements-server.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements-server.txt

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HOME=/data \
    TGDL_DATA_DIR=/data

RUN groupadd --gid 10001 tgdl \
    && useradd --uid 10001 --gid 10001 --home-dir /data --no-create-home \
       --shell /usr/sbin/nologin tgdl \
    && mkdir -p /app /data /tmp \
    && chown 10001:10001 /data

COPY --from=builder /opt/venv /opt/venv
COPY server_app.py server_auth.py server_config.py gui.py host.py \
     tg_video_dl.py docker-entrypoint.py LICENSE /app/
COPY web /app/web

WORKDIR /app
EXPOSE 8787
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).read()"]

ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
CMD ["python", "-m", "server_app", "--host", "0.0.0.0", "--port", "8787", "--data-dir", "/data"]
