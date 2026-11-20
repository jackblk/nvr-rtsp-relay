FROM bluenviron/mediamtx:1.21.4 AS mediamtx
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=mediamtx /mediamtx /usr/local/bin/mediamtx
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY relay/ ./relay/
COPY deploy/ ./deploy/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV UV_NO_DEV=1
ENV VENDOR=hikvision
ENV LD_LIBRARY_PATH=/app/NetSDK/Libs/linux64:/app/hcnetsdk:/app/hcnetsdk/HCNetSDKCom

RUN chmod +x deploy/entrypoint.sh
ENTRYPOINT ["deploy/entrypoint.sh"]
CMD ["run"]
