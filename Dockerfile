FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_PORT=8321 \
    DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-opencv \
       python3-pil \
       opencv-data \
       libimage-exiftool-perl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY photo_auto_rotate.py /app/photo_auto_rotate.py
COPY server.py /app/server.py
COPY web /app/web
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8321
ENTRYPOINT ["/app/docker-entrypoint.sh"]
