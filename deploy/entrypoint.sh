#!/usr/bin/env bash
set -euo pipefail

export VENDOR="${VENDOR:-hikvision}"
vendor="${VENDOR,,}"
transport="${HIKVISION_TRANSPORT:-python}"

if [[ "$vendor" =~ ^[[:space:]]*dahua[[:space:]]*$ ]]; then
  if [[ ! -f /app/NetSDK/NetSDK.py || ! -f /app/NetSDK/Libs/linux64/libdhnetsdk.so ]]; then
    echo 'Error: VENDOR=dahua requires Dahua NetSDK. Mount the installed SDK at /app/NetSDK.' >&2
    exit 1
  fi
elif [[ "$vendor" =~ ^[[:space:]]*(hikvision|hik)[[:space:]]*$ &&
        "${transport,,}" =~ ^[[:space:]]*sdk[[:space:]]*$ &&
        ! -f /app/hcnetsdk/libhcnetsdk.so ]]; then
  echo 'Error: HIKVISION_TRANSPORT=sdk requires Hikvision HCNetSDK. Mount the installed SDK at /app/hcnetsdk.' >&2
  exit 1
fi

# Build the runtime MediaMTX config from the committed base using PyYAML
# (rtspAddress + optional auth block from TARGET_* env). No shell string-munging.
MTX_CONFIG=/tmp/mediamtx.runtime.yml
uv run --frozen python -m relay.mediamtx_config \
  --base /app/deploy/mediamtx.yml --out "$MTX_CONFIG" \
  --streams-config /app/config/streams.yml

# Start MediaMTX (RTSP server) in the background.
/usr/local/bin/mediamtx "$MTX_CONFIG" &
MEDIAMTX_PID=$!

# Give the RTSP server a moment to bind.
sleep 1

PROXY_PID=""
# Forward signals to children for clean docker stop.
trap 'kill -TERM "$MEDIAMTX_PID" "${PROXY_PID:-}" 2>/dev/null || true' TERM INT

# Run the proxy with the provided CMD/args (default: run).
uv run --frozen python -m relay "$@" &
PROXY_PID=$!
wait "$PROXY_PID"
