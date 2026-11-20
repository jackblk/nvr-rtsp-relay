# NVR RTSP relay (Dahua / Hikvision)

Re-publishes a **Dahua** or **Hikvision** NVR's live stream as
RTSP, **without transcoding** (`ffmpeg -c copy`), so CPU and latency stay low. Pick the
vendor per deployment with the `VENDOR` env var.

```
Dahua device     ──NetSDK────▶ relay ──raw DHAV───▶ ffmpeg -f dhav -c copy ──▶ MediaMTX ──RTSP──▶ VLC / ffplay / NVR
Hikvision device ──private TCP──▶ Python relay ──MPEG-PS──▶ ffmpeg -f mpeg -c copy ──▶ MediaMTX ──RTSP──▶ VLC / ffplay / NVR
```

## Quick start with uv
Requires `uv`, `ffmpeg`, and `mediamtx` on PATH.
```bash
uv sync

# Copy .env.example to .env and set VENDOR, HOST, HOST_PORT, USERNAME, and PASSWORD.

# 1. Discover which channels/streams work -> generates/updates streams.yml
uv run python -m relay parse

# 2. Start the RTSP server (separate shell), then run every enabled stream:
mkdir -p tmp
uv run --env-file .env python -m relay.mediamtx_config \
  --base deploy/mediamtx.yml --out tmp/mediamtx.yml
mediamtx tmp/mediamtx.yml
uv run python -m relay run            # reads config/streams.yml (default)

# (or serve a single ad-hoc stream without a config file:)
uv run python -m relay stream --channel 3 --stream main --name cam3-main

# 3. View — open the generated playlist, or use the RTSP URL logged by the relay:
vlc config/output.m3u8
ffplay -rtsp_transport tcp "<RTSP URL>"
```

## Why?

Because sometimes you don't have ONVIF or RTSP available, you only have the proprietary endpoints, and you want to use standard RTSP clients (VLC, ffplay, NVRs, etc.) without transcoding.

Some deployments expose only the recorder's private protocol. The Dahua backend uses NetSDK. The default Hikvision backend speaks the private TCP protocol directly from Python and publishes the live stream through the same RTSP pipeline.

## Vendor SDKs

The Dahua backend requires NetSDK. Hikvision's native SDK is available as an opt-in on Linux x86-64 via `HIKVISION_TRANSPORT=sdk`. Obtain either SDK separately from its vendor.

- **Dahua NetSDK** — download it, paste the wheel into `./NetSDK/`, run `./NetSDK/setup.sh`.
  👉 See **[NetSDK/README.md](NetSDK/README.md)**.
- **Hikvision HCNetSDK** — download the Linux64 SDK, paste the zip into `./hcnetsdk/`, run
  `./hcnetsdk/setup.sh`. 👉 See **[hcnetsdk/README.md](hcnetsdk/README.md)**.

The Python Hikvision transport currently supports IPv4, the recorder's 1024-bit RSA challenge mode, and private TCP-framed RTP. Live tests covered H.264 main and substreams on macOS ARM64. Other firmware authentication and stream modes can use the SDK option on a compatible host. [Porting notes](docs/notes/2026-11-20-hikvision-python-port.md)

## Configure
Copy [.env.example](.env.example) to `.env` (git-ignored) and fill it in:
```
VENDOR=dahua            # dahua (default) or hikvision (alias: hik)
HOST=your-device-host
HOST_PORT=37777         # private protocol port: Dahua 37777, Hikvision 8000 by default
USERNAME=...
PASSWORD=...
TARGET_HOST=0.0.0.0     # RTSP server bind/advertise host
TARGET_PORT=8554        # RTSP server port

# Optional — set BOTH to password-protect the RTSP stream (publish + view).
# Leave unset for open access.
TARGET_USERNAME=
TARGET_PASSWORD=
```

When `TARGET_USERNAME`/`TARGET_PASSWORD` are set, MediaMTX requires those
credentials to publish *and* to view, so clients must connect with
`rtsp://<user>:<pass>@<host>:<port>/<path>`. On startup the relay logs the exact
view URL (`Stream ready — view at: ...`).

## On-demand

For **on-demand** mode locally, generate the MediaMTX config so the `runOnDemand` paths
exist, then run both with `ON_DEMAND=true` (start `mediamtx` from the repo root):
```bash
ON_DEMAND=true uv run --env-file .env python -m relay.mediamtx_config \
  --base deploy/mediamtx.yml --out tmp/mediamtx.yml --streams-config config/streams.yml
mediamtx tmp/mediamtx.yml
ON_DEMAND=true uv run python -m relay run
```

## Docker
```bash
docker compose up --build        # runs `relay run` against the mounted streams.yml
# View (add user:pass@ if RTSP auth is enabled):
ffplay -rtsp_transport tcp rtsp://<host>:8554/cam3-main
```

Run `parse` mode in Docker instead (writes to the mounted ./config/streams.yml):
```bash
docker compose run --rm relay parse
```

The `./config` directory is bind-mounted into the container, so `config/streams.yml`
and `config/output.m3u8` are editable/viewable on the host. Only `config/README.md`
is committed; the generated files are git-ignored.

## Modes
- `parse [--config config/streams.yml] [--probe-concurrency 4]` — probes every
  channel × stream type briefly (concurrently, in batches), detects codec/resolution,
  and **non-destructively merges** results into `streams.yml`: adds newly-found
  streams, marks invalid ones `enable: false`, and leaves your hand-edits (renamed
  paths, manual enable/disable) intact. An unparseable `streams.yml` is regenerated.
- `run [--config config/streams.yml]` — logs in once and publishes **every
  `enable: true` stream** in the config, each at `rtsp://<host>:<TARGET_PORT>/<name>`,
  and writes `output.m3u8` (a playlist of all published streams) next to the config.
  Set `ON_DEMAND=true` to switch to **on-demand** mode: streams are not started up
  front; instead each enabled stream is pulled from the device only while a viewer is
  connected (via MediaMTX's `runOnDemand`) and is torn down ~10s after the last viewer
  disconnects. `output.m3u8` still lists every enabled stream — opening one triggers it.
- `serve <name> [--config config/streams.yml]` — serves a single stream by name
  (resolved from the config). Invoked by MediaMTX's `runOnDemand`; not usually run by hand.
- `stream --channel N --stream <type> --name <path>` — serves one ad-hoc
  channel/stream without a config file. Valid stream types are vendor-specific:
  Dahua `main|sub|sub2`, Hikvision `main|sub`.

> **Channel numbering is vendor-specific.** Dahua channels are 0-based; Hikvision uses
> the device's own layout (e.g. analog `1–16` plus IP channels starting at `33`), which
> `parse` reads from the device. On a remote/WAN device, streams can take a few seconds
> to establish — if `parse` marks a working channel disabled, raise `--probe-seconds`.

## License
This project's own code is licensed under the [MIT License](LICENSE). It does **not**
cover the Dahua NetSDK or the Hikvision HCNetSDK, which are proprietary and must be
obtained separately under their own licenses — see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
