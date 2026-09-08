FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="ValetTracker" \
      org.opencontainers.image.description="Valet ticket tracking for a single stand" \
      org.opencontainers.image.source="https://github.com/OWNER/valettracker" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DB_PATH=/data/valettracker.db

WORKDIR /app
COPY app/ /app/

# No pip install anywhere in this file — the app is standard library only, so
# the image builds in seconds and cannot break on a dependency resolve.
RUN useradd --system --uid 10001 --create-home --home-dir /home/valet valet \
    && mkdir -p /data \
    && chown -R valet:valet /data /app

USER valet
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "-u", "server.py"]
