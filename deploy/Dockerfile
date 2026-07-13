FROM python:3.11-slim

# System deps: ffmpeg + libs needed by the NetSDK .so files + curl/tar for mediamtx.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

# Install MediaMTX (RTSP server).
ARG MEDIAMTX_VERSION=1.9.3
RUN curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_amd64.tar.gz" \
      | tar -xz -C /usr/local/bin mediamtx

WORKDIR /app
# uv for dependency management.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY relay/ ./relay/
COPY NetSDK/ ./NetSDK/
COPY hcnetsdk/ ./hcnetsdk/
COPY deploy/ ./deploy/

# Both vendors' .so files reference each other; make them discoverable. Only the
# selected vendor's libs are actually loaded, so listing both dirs is harmless.
ENV LD_LIBRARY_PATH=/app/NetSDK/Libs/linux64:/app/hcnetsdk:/app/hcnetsdk/HCNetSDKCom
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

RUN chmod +x deploy/entrypoint.sh
ENTRYPOINT ["deploy/entrypoint.sh"]
CMD ["parse"]
