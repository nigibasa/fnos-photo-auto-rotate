# syntax=docker/dockerfile:1.7
FROM debian:bookworm-slim AS python-deps

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
    && python3 -m pip install --break-system-packages --no-cache-dir --target /opt/python \
       numpy==2.2.6 \
       opencv-python-headless==4.10.0.84 \
    && rm -rf /var/lib/apt/lists/*

FROM debian:bookworm-slim

ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/python \
    WEB_PORT=8321 \
    DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-pil \
       libimage-exiftool-perl \
       libglib2.0-0 \
       libgomp1 \
       ca-certificates \
    && if [ "$TARGETARCH" = "amd64" ]; then \
         apt-get install -y --no-install-recommends intel-opencl-icd; \
       fi \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-deps /opt/python /opt/python
ADD --checksum=sha256:8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 \
    https://media.githubusercontent.com/media/opencv/opencv_zoo/f12e12798e8314f7c074a6656816c048dcc95b7a/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    /app/models/face_detection_yunet_2023mar.onnx
COPY photo_auto_rotate.py /app/photo_auto_rotate.py
COPY server.py /app/server.py
COPY web /app/web
COPY third_party/yunet-LICENSE /app/models/LICENSE
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8321
ENTRYPOINT ["/app/docker-entrypoint.sh"]
