# Hikvision (HCNetSDK) support — design

**Date:** 2026-07-13
**Status:** Approved (pending implementation plan)

## Goal

Let this proxy bridge a **Hikvision** DVR/NVR (via its proprietary **HCNetSDK**
"Device Network SDK") to standard RTSP, no transcoding, exactly as it already does
for Dahua NetSDK. A deployment targets **one vendor**, chosen by a `VENDOR` env var.

```
Hikvision device ──HCNetSDK──▶ relay ──raw stream──▶ ffmpeg -c copy ──▶ MediaMTX ──RTSP──▶ VLC
Dahua device     ──NetSDK────▶ relay ──raw DHAV───▶ ffmpeg -c copy ──▶ MediaMTX ──RTSP──▶ VLC
```

The generic pipeline (bounded queue → `FfmpegPublisher` → MediaMTX → M3U8 playlist,
plus on-demand mode) is unchanged. Only the vendor-specific seam is new.

## Constraints & context

- **No Dahua device available for testing.** The Dahua path can no longer be
  validated against hardware. Dahua code must therefore be **moved, not rewritten** —
  behavior-preserving — with safety resting on the existing unit tests.
- **One vendor per deployment**, selected by `VENDOR=dahua|hikvision` (default
  `dahua` for backward compatibility). The relay stays single-device / single-login.
- **HCNetSDK ships only C `.so` libraries** (plus `HCNetSDK.h`) — no Python bindings.
  Therefore *we* author the ctypes bindings; they live in `relay/` under source
  control. `hcnetsdk/` holds only the proprietary, git-ignored `.so` files.
- **Proven so far** (from a prior throwaway spike, low-context): `NET_DVR_Init` +
  `NET_DVR_Login_V40` log in successfully and return valid device metadata. No stream
  has been captured yet. `NET_DVR_Login_V30` failed (error 17) — do not use it.
- SDK build we target: **`EN-HCNetSDKV6.1.9.48_build20230410_linux64`** (the zip the
  user placed in `hcnetsdk/`). It bundles `incEn/HCNetSDK.h` — the ABI source of truth.

## Section 1 — `hcnetsdk/` SDK packaging

Mirror the `NetSDK/` convention: only `README.md` + `setup.sh` are committed; the SDK
itself is git-ignored.

Zip layout (top-level dir `EN-HCNetSDKV6.1.9.48_build20230410_linux64/`):

```
lib/
  libhcnetsdk.so, libHCCore.so, libhpr.so, libNPQos.so, libz.so,
  libcrypto.so.1.1, libssl.so.1.1, libPlayCtrl.so, libSuperRender.so,
  libAudioRender.so, libopenal.so.1, HCNetSDK_Log_Switch.xml
  HCNetSDKCom/    (libHCPreview.so, libStreamTransClient.so, libSystemTransform.so, …)
incEn/HCNetSDK.h   ← ABI source of truth
doc/, consoleDemo/, QtDemo/, …   ← ignored
```

**`hcnetsdk/setup.sh`** (committed) — same structure as `NetSDK/setup.sh`:
1. Find exactly one `EN-HCNetSDK*linux64.zip` in `hcnetsdk/` (error if zero or many).
2. Extract to a temp dir (Python stdlib `zipfile`, no `unzip` dependency).
3. Copy `<top>/lib/.` up into `hcnetsdk/` → yields `hcnetsdk/libhcnetsdk.so`,
   `hcnetsdk/HCNetSDKCom/…`, `hcnetsdk/HCNetSDK_Log_Switch.xml`.
4. Copy `<top>/incEn/HCNetSDK.h` into `hcnetsdk/` (reference for our ctypes bindings).
5. Verify `hcnetsdk/libhcnetsdk.so` and `hcnetsdk/HCNetSDKCom/` exist; report `✓`/`✗`.

**`hcnetsdk/README.md`** (committed) — states it's proprietary and not redistributable
(Hangzhou Hikvision Digital Technology Co., Ltd.), that you accept Hikvision's license
on download, the version built against, the expected post-`setup.sh` layout, and two
download links:
- Primary: <https://www.hikvision.com/en/support/tools/hitools/?type=IP> → choose
  **Device Network SDK_Linux64** (newer; ships as the `.zip` we build against).
- Backup: <https://www.hikvision.com/mena-en/support/download/sdk/device-network-sdk--for-linux-64-bit-/>

**`.gitignore`** — add:
```
hcnetsdk/*
!hcnetsdk/README.md
!hcnetsdk/setup.sh
```

**`THIRD_PARTY_NOTICES.md`** — add a Hikvision HCNetSDK entry alongside Dahua NetSDK.

## Section 2 — Vendor abstraction (`relay/backends/`)

```
relay/backends/
  base.py       # SdkClient Protocol + Backend descriptor + ProbeResult + get_backend()
  dahua.py      # DahuaClient (moved verbatim from sdk_client.py) + DHAV-based probe
  hikvision.py  # HikvisionClient (new) + ffprobe-based probe
```

`SdkClient` **Protocol** (the interface both clients satisfy; matches today's
`DahuaClient`): `init()`, `login(host, port, username, password) -> int`,
`channel_count` (property), `start_realplay(channel, play_type, on_raw) -> handle`,
`stop_realplay(handle)`, `logout()`, `cleanup()`.

`Backend` (frozen dataclass) bundles everything vendor-specific:

| field | Dahua | Hikvision |
|---|---|---|
| `client_class` | `DahuaClient` | `HikvisionClient` |
| `stream_types` | `{main:0, sub:3, sub2:4}` | `{main:0, sub:1}` |
| `ffmpeg_input_args` | `["-f","dhav"]` | *set by spike* (expected `["-f","mpeg"]`) |
| `default_port` | `37777` | `8000` |
| `channels(client)` | `range(0, client.channel_count)` | analog + IP ranges from device info |
| `probe(client, pairs, seconds, concurrency)` | DHAV parser (existing) | ffprobe on captured bytes |

`get_backend(vendor: str) -> Backend` resolves the registry; unknown vendor raises a
clear error listing valid values.

**Consumer changes (minimal):**
- `config.py`: add `Config.vendor` (from `VENDOR`, default `dahua`, lower-cased).
  The module-level `STREAM_TYPES` constant and the hardcoded `port` default move to
  the backend. `Config.port` default handling: if `HOST_PORT` is unset, the effective
  port comes from `backend.default_port` (resolved where the backend is known —
  `__main__`/`manager`), so `Config` keeps a neutral sentinel rather than 37777.
  Everything else in `Config` (URLs, auth, on-demand) is unchanged.
- `publisher.py`: `FfmpegPublisher(rtsp_url, input_args)` replaces the hardcoded
  `-f dhav`. `_args()` becomes `["ffmpeg","-hide_banner","-loglevel","warning",
  *input_args, "-i","pipe:0", "-c","copy", "-f","rtsp","-rtsp_transport","tcp", url]`.
  Restart / process-group / write logic unchanged.
- `manager.py`, `__main__.py`, `probe.py`: obtain client, `stream_types`, `channels`,
  `ffmpeg_input_args`, and `probe` via `get_backend(cfg.vendor)` instead of importing
  `DahuaClient` / `STREAM_TYPES` directly. `StreamPipeline`/`_serve_one` pass
  `input_args` through to the publisher.
- `probe.py`: the DHAV-parsing `probe_streams` body moves into `dahua.probe`;
  `ProbeResult` moves to `backends/base.py` (imported back by `streams_config.merge`,
  whose duck-typed contract is unchanged). `dhav.py` is untouched, imported by
  `dahua.py`.

**Dahua safety:** `DahuaClient` moves byte-for-byte into `backends/dahua.py`.
`sdk_client.py` becomes a thin re-export (`from relay.backends.dahua import DahuaClient`)
so any stale import still resolves, or is deleted with imports updated — decided in the
plan. No Dahua logic changes.

## Section 3 — Hikvision backend (`relay/backends/hikvision.py`)

`HikvisionClient` implements `SdkClient`, authored from `hcnetsdk/HCNetSDK.h`:

- **Discovery/init:** load `libhcnetsdk.so` via ctypes; before `NET_DVR_Init`, call
  `NET_DVR_SetSDKInitCfg` to point at the `HCNetSDKCom` directory and the bundled
  `libcrypto.so.1.1` / `libssl.so.1.1` (a Linux HCNetSDK requirement — otherwise
  component load fails). Then `NET_DVR_Init`. `LD_LIBRARY_PATH` still covers the
  base libs (see Section 5).
- **Login:** `NET_DVR_Login_V40` with `NET_DVR_USER_LOGIN_INFO` /
  `NET_DVR_DEVICEINFO_V40` (the proven path). Never V30. `channel_count` and the
  channel-layout fields are read from the returned `DEVICEINFO_V30`.
- **Channels:** `channels()` computes valid numbers from device info —
  analog `[byStartChan .. byStartChan+byChanNum-1]` plus IP
  `[byStartDChan .. byStartDChan+byIPChanNum-1]`. No hardcoded ranges. (Handoff
  device observed analog 1–16, IP start 33, 18 IP channels → 33–50; treat only as a
  sanity check, not a constant.)
- **RealPlay:** `NET_DVR_RealPlay_V40` headless (`hPlayWnd = 0`) with
  `NET_DVR_PREVIEWINFO` (`dwStreamType` from `stream_types`, `dwLinkMode = 0`
  TCP/private, blocked as appropriate), then register a real-data callback
  (`REALDATACALLBACK`). The callback forwards `SYSHEAD` + `STREAMDATA` bytes straight
  to `on_raw` (same fast-enqueue contract as Dahua; **keep ctypes callback objects
  strongly referenced** for the handle's lifetime). Exact `PREVIEWINFO`/callback ABI
  comes from `HCNetSDK.h`.
- **Stream types:** `{main: 0, sub: 1}`.
- **Probe:** `probe()` starts RealPlay per `(channel, stream)`, captures a few seconds
  of callback bytes, and runs `ffprobe` on them to detect codec + resolution, returning
  `ProbeResult`s (same shape Dahua's probe returns). No hand-written PS parser.
- **Disconnect:** register a disconnect/exception callback for logging + SDK
  auto-reconnect, mirroring `DahuaClient._on_disconnect`.

## Section 4 — Phase 0 capture spike (de-risks the stream path)

Before wiring the backend into the relay, a throwaway script in `tmp/` (per the repo's
scratch-in-`tmp/` convention) proves the stream path at the ctypes level:

1. Read `NET_DVR_PREVIEWINFO`, `REALDATACALLBACK`, `NET_DVR_RealPlay_V40` from
   `hcnetsdk/HCNetSDK.h` and reproduce them exactly in ctypes.
2. `SetSDKInitCfg` → `Init` → `Login_V40` (log serial/channels — known to work).
3. `RealPlay_V40` headless on a known-good channel + register the real-data callback.
4. Log every `dwDataType` (`NET_DVR_SYSHEAD` / `NET_DVR_STREAMDATA`) and byte count;
   dump concatenated bytes to `tmp/hik_capture.bin` for ~5s; stop / logout / cleanup.
5. `xxd -l 64 tmp/hik_capture.bin` + `ffprobe tmp/hik_capture.bin` → identify the
   container (expected MPEG-PS) and confirm `-c copy` works.

**Outcome** fixes `hikvision.ffmpeg_input_args` and confirms the no-transcode goal.
This is the **only** point where the design could branch: if ffprobe cannot demux the
callback bytes directly (Path B — Hikvision PS/private framing needing PlayM4/decode),
we stop and revisit before building the backend. Expectation is Path A (direct remux).

## Section 5 — Docker / env / docs

- **`Dockerfile`:** `COPY hcnetsdk/ ./hcnetsdk/`; append the Hikvision lib dirs to the
  library path: `ENV LD_LIBRARY_PATH=/app/NetSDK/Libs/linux64:/app/hcnetsdk:/app/hcnetsdk/HCNetSDKCom`
  (harmless for a Dahua-only deployment; only the selected vendor's libs load).
  ffmpeg is already installed.
- **`.env.example`:** add `VENDOR=dahua|hikvision`; document per-vendor default ports
  (Dahua 37777, Hikvision 8000) and that `HOST_PORT` overrides (the handoff DVR uses
  8200). No default `HOST_PORT` should be assumed for Hikvision beyond 8000.
- **`AGENTS.md` + root `README.md`:** document the two-vendor model, the `VENDOR`
  switch, `relay/backends/` layout, and `hcnetsdk/` setup. Note the "we author the
  ctypes bindings; Hikvision ships only `.so`" distinction and the no-Dahua-testing
  caveat.

## Section 6 — Testing

- **TDD (pure logic, no SDK/ffmpeg):** `get_backend` registry + unknown-vendor error;
  Hikvision channel-enumeration math from a fake device-info; `Config.vendor` parsing;
  stream-type maps; `FfmpegPublisher._args()` construction from `input_args`.
- **Live (Hikvision DVR):** the spike, then full `parse` → `run` → view in VLC against
  the real device. Validate `-c copy` (no transcode) and both main/sub.
- **Dahua:** no live test possible; correctness rests on the behavior-preserving move
  plus the existing DHAV / `streams_config` unit tests continuing to pass.

## Out of scope

- Renaming the repo or the `relay` package (internals go vendor-neutral only).
- Simultaneous multi-vendor / multi-device in one deployment (multiple logins).
- PlayM4 decode/transcode path (only revisited if the spike shows Path B).
- Playback/recording, PTZ, audio talk, alarms — live preview only.

## Implementation order

1. Section 1 packaging (`setup.sh`, `README.md`, `.gitignore`, notices) — run
   `setup.sh` to lay down the libs + header.
2. Section 4 spike — resolve container format + RealPlay/callback ABI.
3. Section 2 abstraction — introduce `relay/backends/`, move Dahua, thread `vendor`
   and `input_args` through consumers; keep tests green.
4. Section 3 Hikvision backend — build on the spike's proven ctypes.
5. Section 5 Docker/env/docs.
6. End-to-end validation against the Hikvision DVR.
