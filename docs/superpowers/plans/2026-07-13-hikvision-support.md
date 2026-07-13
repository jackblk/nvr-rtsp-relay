# Hikvision (HCNetSDK) Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the relay bridge a Hikvision DVR/NVR (HCNetSDK) to RTSP with no transcoding, exactly as it already does for Dahua, with the vendor chosen by a `VENDOR` env var.

**Architecture:** Introduce a `relay/backends/` package that isolates every vendor-specific concern (SDK client, stream-type map, ffmpeg input format, default port, channel enumeration, codec/resolution probe) behind a `Backend` descriptor resolved by `get_backend(vendor)`. The generic pipeline (bounded queue → `FfmpegPublisher` → MediaMTX → playlist, plus on-demand mode) is unchanged. The Dahua path is **moved, not rewritten** (no Dahua hardware is available to re-test it).

**Tech Stack:** Python 3.11, uv, ctypes (both SDKs), ffmpeg (`-c copy`), MediaMTX, PyYAML, pytest, python-dotenv.

## Global Constraints

- **uv only** — run Python via `uv run`; never pip/requirements.txt.
- **No Dahua device for testing** — the Dahua backend must be a behavior-preserving move; do not change its runtime logic. Correctness rests on the existing unit tests staying green.
- **One vendor per deployment**, chosen by `VENDOR=dahua|hikvision` (default `dahua`).
- **HCNetSDK ABI source of truth** = `hcnetsdk/HCNetSDK.h` (from `EN-HCNetSDKV6.1.9.48_build20230410_linux64`). Reproduce structs exactly; use `NET_DVR_Login_V40` (never V30).
- **We author the Hikvision ctypes bindings** (they live in `relay/backends/hikvision.py`, under source control). `hcnetsdk/` holds only proprietary, git-ignored `.so` files + the header.
- **No transcoding** — publisher stays `-c copy`; only the demuxer input format differs per vendor.
- **Scratch scripts go in `tmp/`** (git-ignored) — never inline shell heredocs/`python -c`.
- Backend modules must be **pure to import** (defining ctypes structs is fine; loading the `.so`/wheel must be lazy, inside client construction) so config parsing and unit tests never require an installed SDK.

---

## File Structure

**New:**
- `hcnetsdk/README.md`, `hcnetsdk/setup.sh` — SDK packaging (committed; the `.so`/zip stay git-ignored).
- `relay/backends/__init__.py` — empty package marker (keep import pure).
- `relay/backends/base.py` — `SdkClient` Protocol, `ProbeResult`, `Backend` dataclass, `get_backend()`.
- `relay/backends/dahua.py` — `DahuaClient` (moved) + Dahua probe + `BACKEND`.
- `relay/backends/hikvision.py` — ctypes bindings, `HikvisionClient`, channel math, ffprobe probe + `BACKEND`.
- `tests/test_backends.py`, `tests/test_hikvision.py` — new unit tests.
- `tmp/spike_hik_capture.py` — throwaway capture spike (git-ignored).

**Modified:**
- `relay/config.py` — add `vendor`; default port from the backend.
- `relay/publisher.py` — `FfmpegPublisher(rtsp_url, input_args)`.
- `relay/manager.py`, `relay/__main__.py` — resolve client/stream_types/channels/input_args via `get_backend`.
- `relay/sdk_client.py`, `relay/probe.py` — become thin re-export shims (or deleted with imports updated).
- `tests/test_config.py`, `tests/test_probe.py`, `tests/test_publisher.py` — follow the moves/signature changes.
- `deploy/Dockerfile`, `.env.example`, `.gitignore`, `THIRD_PARTY_NOTICES.md`, `AGENTS.md`, `README.md`.

---

## Task 1: `hcnetsdk/` SDK packaging

**Files:**
- Create: `hcnetsdk/setup.sh`, `hcnetsdk/README.md`
- Modify: `.gitignore`, `THIRD_PARTY_NOTICES.md`

**Interfaces:**
- Produces: after `hcnetsdk/setup.sh` runs, `hcnetsdk/libhcnetsdk.so`, `hcnetsdk/HCNetSDKCom/`, and `hcnetsdk/HCNetSDK.h` exist (git-ignored). Later tasks rely on these paths.

- [ ] **Step 1: Add the git-ignore rules**

Append to `.gitignore` (after the NetSDK block):

```
# Hikvision HCNetSDK — proprietary, not redistributable. Ignore the SDK files and
# the zip, but keep the setup instructions + script tracked (see hcnetsdk/README.md).
hcnetsdk/*
!hcnetsdk/README.md
!hcnetsdk/setup.sh
```

- [ ] **Step 2: Write `hcnetsdk/setup.sh`**

```bash
#!/usr/bin/env bash
# Set up the Hikvision HCNetSDK from its Linux64 zip. See README.md in this directory.
#
# Usage: paste EN-HCNetSDK*_linux64.zip into this folder, then run ./hcnetsdk/setup.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"   # ./hcnetsdk/
cd "$DIR"

# 1. Locate the zip.
shopt -s nullglob
zips=(EN-HCNetSDK*_linux64.zip)
shopt -u nullglob
if [ ${#zips[@]} -eq 0 ]; then
  echo "error: no EN-HCNetSDK*_linux64.zip found in $DIR" >&2
  echo "See ./hcnetsdk/README.md for instructions." >&2
  exit 1
fi
if [ ${#zips[@]} -gt 1 ]; then
  echo "error: multiple EN-HCNetSDK*_linux64.zip files found; keep only one:" >&2
  printf '  %s\n' "${zips[@]}" >&2
  exit 1
fi
ZIP="${zips[0]}"

# 2. Extract (a zip; use python's stdlib so no `unzip` dependency).
echo "Extracting ${ZIP} ..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
python3 -m zipfile -e "$ZIP" "$TMP"

# The zip has a single top-level dir; find the one containing lib/.
TOP="$(dirname "$(find "$TMP" -type d -name lib -print -quit)")"
if [ -z "$TOP" ] || [ ! -d "$TOP/lib" ]; then
  echo "error: could not find lib/ inside the zip (unexpected layout)" >&2
  exit 1
fi

# 3. Copy the runtime libs up into this directory, plus the header for reference.
cp -r "$TOP/lib/." "$DIR/"
if [ -f "$TOP/incEn/HCNetSDK.h" ]; then
  cp "$TOP/incEn/HCNetSDK.h" "$DIR/"
fi

# 4. Verify.
if [ -f "$DIR/libhcnetsdk.so" ] && [ -d "$DIR/HCNetSDKCom" ]; then
  n=$(find "$DIR" -maxdepth 2 -name '*.so*' | wc -l)
  echo "✓ HCNetSDK ready: libhcnetsdk.so + HCNetSDKCom/ + ${n} shared libs in place."
  echo "  You can delete the .zip now if you like (it is git-ignored either way)."
else
  echo "✗ setup incomplete — expected libhcnetsdk.so and HCNetSDKCom/" >&2
  exit 1
fi
```

- [ ] **Step 3: Write `hcnetsdk/README.md`**

```markdown
# HCNetSDK — Hikvision Device Network SDK (download separately)

This directory holds the **Hikvision HCNetSDK** ("Device Network SDK" for Linux 64-bit),
which this project depends on but **does not bundle**. The SDK is proprietary software
owned by *Hangzhou Hikvision Digital Technology Co., Ltd.* and is **not redistributable**
under its license, so you must download it yourself.

> Only `README.md` and `setup.sh` are committed to git. Everything else here
> (the `.so` files, the header, and the `.zip`) is git-ignored.
>
> Unlike the Dahua NetSDK (which ships Python bindings in a wheel), the Hikvision SDK
> ships only C `.so` libraries. This project's Python ctypes bindings for it live in
> `relay/backends/hikvision.py` (under source control).

## Setup

1. **Download** the Linux64 Device Network SDK from Hikvision (you accept their license
   agreement to download):
   - Primary: <https://www.hikvision.com/en/support/tools/hitools/?type=IP> — choose
     **Device Network SDK_Linux64** (ships as the `.zip` this project builds against).
   - Backup: <https://www.hikvision.com/mena-en/support/download/sdk/device-network-sdk--for-linux-64-bit-/>

   This project was built against
   **`EN-HCNetSDKV6.1.9.48_build20230410_linux64`** (newer builds should work too).

2. **Paste** the zip into this directory (`./hcnetsdk/`), e.g.:

   ```
   EN-HCNetSDKV6.1.9.48_build20230410_linux64.zip
   ```

3. **Run** the setup script — it extracts the libraries into place and verifies the result:

   ```bash
   ./hcnetsdk/setup.sh
   ```

## Expected layout (check at a glance)

After `setup.sh` runs, this directory should look like:

```
hcnetsdk/
├── README.md                 # this file        (committed)
├── setup.sh                  # extractor         (committed)
├── EN-HCNetSDK...linux64.zip  # you paste this    (git-ignored)
├── HCNetSDK.h                # header reference   (git-ignored)
├── libhcnetsdk.so            # ┐
├── libHCCore.so              # │
├── libcrypto.so.1.1          # │ from the zip     (git-ignored)
├── libssl.so.1.1             # │
├── libPlayCtrl.so, ...       # ┘
└── HCNetSDKCom/              # component libs     (git-ignored)
    ├── libHCPreview.so
    ├── libStreamTransClient.so
    └── ...
```

## License

The HCNetSDK is proprietary and subject to Hikvision's own license terms, which you
accept when you download it. Please read and comply with those terms (and any
applicable export-control laws) yourself. Because it is not freely redistributable, it
is not included in this repository and is not covered by this project's MIT license.
See [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
```

- [ ] **Step 4: Add a Hikvision entry to `THIRD_PARTY_NOTICES.md`**

Read the existing file first, then add a section mirroring the Dahua NetSDK one, naming
*Hangzhou Hikvision Digital Technology Co., Ltd.*, stating the SDK is proprietary/not
redistributable/not bundled, and pointing to `hcnetsdk/README.md`.

- [ ] **Step 5: Run setup and verify**

Run:
```bash
chmod +x hcnetsdk/setup.sh && ./hcnetsdk/setup.sh
ls hcnetsdk/libhcnetsdk.so hcnetsdk/HCNetSDK.h && ls hcnetsdk/HCNetSDKCom/ | head
```
Expected: `✓ HCNetSDK ready: ...`, and both `ls` commands succeed.

- [ ] **Step 6: Commit**

```bash
git add hcnetsdk/setup.sh hcnetsdk/README.md .gitignore THIRD_PARTY_NOTICES.md
git commit -m "feat: add hcnetsdk/ SDK packaging (README + setup.sh, git-ignored libs)"
```

---

## Task 2: Capture spike — confirm the stream container (live device)

This is an **exploratory** task (not TDD): it proves the HCNetSDK preview path end-to-end
at the ctypes level and determines the ffmpeg input format used by Task 8. All the ctypes
below is copied from `hcnetsdk/HCNetSDK.h`.

**Files:**
- Create: `tmp/spike_hik_capture.py` (git-ignored)

**Interfaces:**
- Produces: the confirmed ffmpeg input format for Hikvision (expected MPEG-PS →
  `("-f", "mpeg")`), recorded for Task 8's `FFMPEG_INPUT_ARGS["hikvision"]`.

- [ ] **Step 1: Write the spike script**

```python
"""Throwaway: prove HCNetSDK preview + identify the callback container format.

Run:  LD_LIBRARY_PATH=hcnetsdk:hcnetsdk/HCNetSDKCom uv run python tmp/spike_hik_capture.py --channel 1
Then: ffprobe tmp/hik_capture.bin ;  xxd -l 64 tmp/hik_capture.bin
"""
import argparse
import ctypes as C
import os
import time
from pathlib import Path

from dotenv import load_dotenv

BYTE, WORD, DWORD, LONG, BOOL = C.c_ubyte, C.c_ushort, C.c_uint32, C.c_int32, C.c_int32

SDK_DIR = Path("hcnetsdk").resolve()
NET_DVR_SYSHEAD, NET_DVR_STREAMDATA = 1, 2

class NET_DVR_DEVICEINFO_V30(C.Structure):
    _fields_ = [
        ("sSerialNumber", BYTE * 48), ("byAlarmInPortNum", BYTE), ("byAlarmOutPortNum", BYTE),
        ("byDiskNum", BYTE), ("byDVRType", BYTE), ("byChanNum", BYTE), ("byStartChan", BYTE),
        ("byAudioChanNum", BYTE), ("byIPChanNum", BYTE), ("byZeroChanNum", BYTE),
        ("byMainProto", BYTE), ("bySubProto", BYTE), ("bySupport", BYTE), ("bySupport1", BYTE),
        ("bySupport2", BYTE), ("wDevType", WORD), ("bySupport3", BYTE), ("byMultiStreamProto", BYTE),
        ("byStartDChan", BYTE), ("byStartDTalkChan", BYTE), ("byHighDChanNum", BYTE),
        ("bySupport4", BYTE), ("byLanguageType", BYTE), ("byVoiceInChanNum", BYTE),
        ("byStartVoiceInChanNo", BYTE), ("bySupport5", BYTE), ("bySupport6", BYTE),
        ("byMirrorChanNum", BYTE), ("wStartMirrorChanNo", WORD), ("bySupport7", BYTE), ("byRes2", BYTE),
    ]

class NET_DVR_DEVICEINFO_V40(C.Structure):
    _fields_ = [
        ("struDeviceV30", NET_DVR_DEVICEINFO_V30), ("bySupportLock", BYTE), ("byRetryLoginTime", BYTE),
        ("byPasswordLevel", BYTE), ("byProxyType", BYTE), ("dwSurplusLockTime", DWORD),
        ("byCharEncodeType", BYTE), ("bySupportDev5", BYTE), ("bySupport", BYTE), ("byLoginMode", BYTE),
        ("dwOEMCode", DWORD), ("iResidualValidity", C.c_int), ("byResidualValidity", BYTE),
        ("bySingleStartDTalkChan", BYTE), ("bySingleDTalkChanNums", BYTE), ("byPassWordResetLevel", BYTE),
        ("bySupportStreamEncrypt", BYTE), ("byMarketType", BYTE), ("byTLSCap", BYTE), ("byRes2", BYTE * 237),
    ]

class NET_DVR_USER_LOGIN_INFO(C.Structure):
    _fields_ = [
        ("sDeviceAddress", C.c_char * 129), ("byUseTransport", BYTE), ("wPort", WORD),
        ("sUserName", C.c_char * 64), ("sPassword", C.c_char * 64), ("cbLoginResult", C.c_void_p),
        ("pUser", C.c_void_p), ("bUseAsynLogin", BOOL), ("byProxyType", BYTE), ("byUseUTCTime", BYTE),
        ("byLoginMode", BYTE), ("byHttps", BYTE), ("iProxyID", LONG), ("byVerifyMode", BYTE),
        ("byRes3", BYTE * 119),
    ]

class NET_DVR_PREVIEWINFO(C.Structure):
    _fields_ = [
        ("lChannel", LONG), ("dwStreamType", DWORD), ("dwLinkMode", DWORD), ("hPlayWnd", C.c_void_p),
        ("bBlocked", DWORD), ("bPassbackRecord", DWORD), ("byPreviewMode", BYTE), ("byStreamID", BYTE * 32),
        ("byProtoType", BYTE), ("byRes1", BYTE), ("byVideoCodingType", BYTE), ("dwDisplayBufNum", DWORD),
        ("byNPQMode", BYTE), ("byRecvMetaData", BYTE), ("byDataType", BYTE), ("byRes", BYTE * 213),
    ]

class NET_DVR_LOCAL_SDK_PATH(C.Structure):
    _fields_ = [("sPath", C.c_char * 256), ("byRes", BYTE * 128)]

REALDATACALLBACK = C.CFUNCTYPE(None, LONG, DWORD, C.POINTER(BYTE), DWORD, C.c_void_p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--stream", type=int, default=0)  # 0 main, 1 sub
    ap.add_argument("--seconds", type=float, default=5.0)
    args = ap.parse_args()
    load_dotenv()

    sdk = C.CDLL(str(SDK_DIR / "libhcnetsdk.so"))
    sdk.NET_DVR_Login_V40.restype = LONG
    sdk.NET_DVR_RealPlay_V40.restype = LONG
    sdk.NET_DVR_GetLastError.restype = DWORD

    # Point the SDK at HCNetSDKCom + openssl so components load regardless of cwd.
    p = NET_DVR_LOCAL_SDK_PATH(); p.sPath = str(SDK_DIR).encode()
    sdk.NET_DVR_SetSDKInitCfg(2, C.byref(p))  # NET_SDK_INIT_CFG_SDK_PATH

    assert sdk.NET_DVR_Init(), "NET_DVR_Init failed"

    login = NET_DVR_USER_LOGIN_INFO()
    login.sDeviceAddress = os.environ["HOST"].encode()
    login.wPort = int(os.environ.get("HOST_PORT") or 8000)
    login.sUserName = os.environ["USERNAME"].encode()
    login.sPassword = os.environ["PASSWORD"].encode()
    login.bUseAsynLogin = 0
    dev = NET_DVR_DEVICEINFO_V40()
    uid = sdk.NET_DVR_Login_V40(C.byref(login), C.byref(dev))
    if uid < 0:
        raise SystemExit(f"login failed, err={sdk.NET_DVR_GetLastError()}")
    v30 = dev.struDeviceV30
    print(f"LOGIN OK uid={uid} chan={v30.byChanNum} startChan={v30.byStartChan} "
          f"ipChan={v30.byIPChanNum} startDChan={v30.byStartDChan}")

    out = open("tmp/hik_capture.bin", "wb")
    seen = {}

    def _cb(handle, dtype, buf, size, user):
        seen[dtype] = seen.get(dtype, 0) + 1
        if dtype in (NET_DVR_SYSHEAD, NET_DVR_STREAMDATA) and size > 0:
            out.write(bytes((C.c_ubyte * size).from_address(C.addressof(buf.contents))))
    cb = REALDATACALLBACK(_cb)   # keep referenced

    pv = NET_DVR_PREVIEWINFO()
    pv.lChannel = args.channel
    pv.dwStreamType = args.stream
    pv.dwLinkMode = 0            # TCP
    pv.hPlayWnd = None           # headless
    pv.bBlocked = 1
    handle = sdk.NET_DVR_RealPlay_V40(uid, C.byref(pv), cb, None)
    if handle < 0:
        raise SystemExit(f"RealPlay failed, err={sdk.NET_DVR_GetLastError()}")
    print(f"preview started on channel {args.channel}")
    time.sleep(args.seconds)

    sdk.NET_DVR_StopRealPlay(handle)
    sdk.NET_DVR_Logout(uid)
    sdk.NET_DVR_Cleanup()
    out.close()
    print("data-type counts:", seen, "bytes:", os.path.getsize("tmp/hik_capture.bin"))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the spike against the device**

Run:
```bash
LD_LIBRARY_PATH=hcnetsdk:hcnetsdk/HCNetSDKCom uv run python tmp/spike_hik_capture.py --channel 1
```
Expected: `LOGIN OK ...`, `preview started ...`, and `data-type counts: {1: N, 2: M}` with a non-zero byte count. If channel 1 yields nothing, retry with a channel from the printed `startDChan` (e.g. `--channel 33`).

- [ ] **Step 3: Identify the container**

Run:
```bash
xxd -l 64 tmp/hik_capture.bin
ffprobe -hide_banner tmp/hik_capture.bin
LD_LIBRARY_PATH=hcnetsdk:hcnetsdk/HCNetSDKCom uv run python tmp/spike_hik_capture.py --channel 1 --seconds 3 && \
  ffmpeg -hide_banner -f mpeg -i tmp/hik_capture.bin -c copy -f null -   # confirm -c copy demuxes
```
Expected: ffprobe reports an MPEG-PS/`mpeg` container with an h264/h265 stream; the ffmpeg `-f mpeg -c copy` run parses frames without fatal errors. **Record the working `-f` value.** If ffprobe cannot demux it, STOP and report — this is the design's only branch point (Path B in the spec).

- [ ] **Step 4: Note the result (no commit — the script is git-ignored)**

Write the confirmed format into this plan's Task 8 (`FFMPEG_INPUT_ARGS["hikvision"]`, default assumption `("-f", "mpeg")`). Nothing to commit.

---

## Task 3: `relay/backends/base.py` — abstraction seam

**Files:**
- Create: `relay/backends/__init__.py`, `relay/backends/base.py`
- Test: `tests/test_backends.py`

**Interfaces:**
- Produces:
  - `ProbeResult(channel:int, stream:str, ok:bool, codec:Optional[str], width:int, height:int)` dataclass.
  - `SdkClient` Protocol: `init()`, `login(host,port,username,password)->int`, `channel_count:int`, `start_realplay(channel:int, play_type:int, on_raw:Callable[[bytes],None])->int`, `stop_realplay(handle:int)`, `logout()`, `cleanup()`.
  - `Backend` frozen dataclass with fields: `vendor:str`, `stream_types:dict[str,int]`, `default_port:int`, `ffmpeg_input_args:tuple[str,...]`, `client_factory:Callable[[],SdkClient]`, `list_channels:Callable[[SdkClient],list[int]]`, `probe:Callable[[SdkClient,list[tuple[int,str]],float,int],list[ProbeResult]]`.
  - `get_backend(vendor:str)->Backend` — lower-cases/strips input; raises `ValueError` for unknown vendors; imports `relay.backends.<vendor>` and returns its module-level `BACKEND`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backends.py
import pytest
import relay  # noqa: F401
from relay.backends.base import get_backend, ProbeResult


def test_get_backend_unknown_vendor_raises():
    with pytest.raises(ValueError) as e:
        get_backend("acme")
    assert "acme" in str(e.value)


def test_get_backend_normalizes_case_and_whitespace():
    with pytest.raises(ValueError):
        get_backend("  DAHUAX ")   # still unknown, but must not crash on casing


def test_probe_result_shape():
    r = ProbeResult(3, "main", True, "h264", 960, 576)
    assert (r.channel, r.stream, r.ok, r.codec, r.width, r.height) == (3, "main", True, "h264", 960, 576)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backends.py -v`
Expected: FAIL (`ModuleNotFoundError: relay.backends.base`).

- [ ] **Step 3: Create the package + base module**

`relay/backends/__init__.py`:
```python
"""Vendor backends: each submodule defines a module-level BACKEND. Keep imports pure."""
```

`relay/backends/base.py`:
```python
"""Vendor abstraction seam: Protocol + Backend descriptor + registry. No SDK imports."""
import importlib
from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Tuple, runtime_checkable


@dataclass
class ProbeResult:
    channel: int
    stream: str
    ok: bool
    codec: Optional[str]
    width: int
    height: int


@runtime_checkable
class SdkClient(Protocol):
    channel_count: int
    def init(self) -> None: ...
    def login(self, host: str, port: int, username: str, password: str) -> int: ...
    def start_realplay(self, channel: int, play_type: int,
                       on_raw: Callable[[bytes], None]) -> int: ...
    def stop_realplay(self, handle: int) -> None: ...
    def logout(self) -> None: ...
    def cleanup(self) -> None: ...


@dataclass(frozen=True)
class Backend:
    vendor: str
    stream_types: dict
    default_port: int
    ffmpeg_input_args: Tuple[str, ...]
    client_factory: Callable[[], SdkClient]
    list_channels: Callable[[SdkClient], List[int]]
    probe: Callable[[SdkClient, List[Tuple[int, str]], float, int], List[ProbeResult]]


_VENDORS = ("dahua", "hikvision")


def get_backend(vendor: str) -> Backend:
    v = (vendor or "").strip().lower()
    if v not in _VENDORS:
        raise ValueError(f"unknown VENDOR {vendor!r}; valid values: {list(_VENDORS)}")
    return importlib.import_module(f"relay.backends.{v}").BACKEND
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_backends.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add relay/backends/__init__.py relay/backends/base.py tests/test_backends.py
git commit -m "feat: add relay/backends abstraction seam (base.py)"
```

---

## Task 4: Move Dahua into `relay/backends/dahua.py`

Behavior-preserving move of `DahuaClient` + the DHAV probe. The only change to Dahua
logic is making the `NetSDK` import **lazy** (inside `__init__`) so the module imports
purely; the SDK still loads exactly when a client is constructed (i.e. when streaming
starts), so runtime behavior is unchanged.

**Files:**
- Create: `relay/backends/dahua.py`
- Modify: `relay/sdk_client.py` (→ shim), `relay/probe.py` (delete), `tests/test_probe.py`, `tests/test_config.py`
- Test: `tests/test_backends.py` (extend)

**Interfaces:**
- Consumes: `Backend`, `ProbeResult`, `SdkClient` from `relay.backends.base`.
- Produces:
  - `relay.backends.dahua.DahuaClient` — same public API as today.
  - `relay.backends.dahua.probe_streams(client, pairs, seconds, concurrency=4)->list[ProbeResult]` — the moved DHAV probe.
  - `relay.backends.dahua.BACKEND` — `Backend(vendor="dahua", stream_types={"main":0,"sub":3,"sub2":4}, default_port=37777, ffmpeg_input_args=("-f","dhav"), client_factory=DahuaClient, list_channels=lambda c: list(range(c.channel_count)), probe=probe_streams)`.

- [ ] **Step 1: Write the failing test (extend `tests/test_backends.py`)**

```python
def test_dahua_backend_metadata():
    b = get_backend("dahua")
    assert b.vendor == "dahua"
    assert b.stream_types == {"main": 0, "sub": 3, "sub2": 4}
    assert b.default_port == 37777
    assert b.ffmpeg_input_args == ("-f", "dhav")


class _FakeDevInfoClient:
    channel_count = 4

def test_dahua_list_channels_is_zero_based_range():
    b = get_backend("dahua")
    assert b.list_channels(_FakeDevInfoClient()) == [0, 1, 2, 3]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_backends.py -k dahua -v`
Expected: FAIL (`ModuleNotFoundError: relay.backends.dahua`).

- [ ] **Step 3: Create `relay/backends/dahua.py`**

Move the current `relay/sdk_client.py` body verbatim, with the SDK import made lazy, then
append the moved probe and `BACKEND`. Full file:

```python
"""Dahua NetSDK backend: DahuaClient + DHAV probe. Moved from relay/sdk_client.py.

No Dahua hardware is available to re-test this; keep the client logic unchanged. The
NetSDK import is lazy (in __init__) so importing this module stays pure.
"""
import ctypes
import logging
import time
from typing import Callable, List, Tuple

import relay  # noqa: F401  (sys.path bootstrap so NetSDK imports resolve)
from relay.backends.base import Backend, ProbeResult
from relay.dhav import DhavParser

log = logging.getLogger(__name__)

RawDataHandler = Callable[[bytes], None]


def _load_sdk():
    try:
        from NetSDK.NetSDK import NetClient
        from NetSDK.SDK_Struct import (
            NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY,
            NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY,
        )
        from NetSDK.SDK_Enum import EM_LOGIN_SPAC_CAP_TYPE, EM_REALDATA_FLAG
        from NetSDK.SDK_Callback import fRealDataCallBackEx2, fDisConnect
    except ImportError as e:
        raise ImportError(
            "The Dahua NetSDK was not found. It is proprietary and not bundled with "
            "this project. See NetSDK/README.md for how to download it and run "
            "./NetSDK/setup.sh."
        ) from e
    return (NetClient, NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY,
            NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY, EM_LOGIN_SPAC_CAP_TYPE,
            EM_REALDATA_FLAG, fRealDataCallBackEx2, fDisConnect)


class DahuaClient:
    def __init__(self):
        (self._NetClient, self._IN_LOGIN, self._OUT_LOGIN, self._SPAC_CAP,
         self._REALDATA_FLAG, fRealDataCallBackEx2, fDisConnect) = _load_sdk()
        self._sdk = self._NetClient()     # singleton; loads .so libs
        self._login_id = 0
        self._channel_count = 0
        self._disconnect_cb = fDisConnect(self._on_disconnect)
        self._realdata_cb = fRealDataCallBackEx2(self._raw_trampoline)
        self._handlers: dict[int, RawDataHandler] = {}

    def init(self):
        if self._sdk.InitEx(self._disconnect_cb) != 1:
            raise RuntimeError("NetSDK InitEx failed")
        log.info("NetSDK initialized")

    def login(self, host: str, port: int, username: str, password: str) -> int:
        stu_in = self._IN_LOGIN()
        stu_in.dwSize = ctypes.sizeof(self._IN_LOGIN)
        stu_in.szIP = host.encode()
        stu_in.nPort = int(port)
        stu_in.szUserName = username.encode()
        stu_in.szPassword = password.encode()
        stu_in.emSpecCap = self._SPAC_CAP.TCP
        stu_out = self._OUT_LOGIN()
        stu_out.dwSize = ctypes.sizeof(self._OUT_LOGIN)
        login_id, device_info, err = self._sdk.LoginWithHighLevelSecurity(stu_in, stu_out)
        if login_id == 0:
            raise RuntimeError(f"Login failed: {err}")
        self._login_id = login_id
        self._channel_count = device_info.nChanNum
        log.info("Logged in: login_id=%s channels=%s", login_id, self._channel_count)
        return login_id

    @property
    def channel_count(self) -> int:
        return self._channel_count

    def start_realplay(self, channel: int, play_type: int, on_raw: RawDataHandler) -> int:
        handle = self._sdk.RealPlayEx(self._login_id, channel, 0, play_type)
        if handle == 0:
            raise RuntimeError(f"RealPlayEx failed for channel={channel} play_type={play_type}")
        self._handlers[handle] = on_raw
        ok = self._sdk.SetRealDataCallBackEx2(
            handle, self._realdata_cb, 0, self._REALDATA_FLAG.RAW_DATA
        )
        if not ok:
            self._sdk.StopRealPlayEx(handle)
            del self._handlers[handle]
            raise RuntimeError("SetRealDataCallBackEx2 failed")
        log.info("RealPlay started: handle=%s channel=%s play_type=%s", handle, channel, play_type)
        return handle

    def stop_realplay(self, handle: int):
        if handle and handle in self._handlers:
            self._sdk.StopRealPlayEx(handle)
            del self._handlers[handle]

    def logout(self):
        if self._login_id:
            self._sdk.Logout(self._login_id)
            self._login_id = 0

    def cleanup(self):
        for handle in list(self._handlers):
            self._sdk.StopRealPlayEx(handle)
        self._handlers.clear()
        self.logout()
        self._sdk.Cleanup()

    def _raw_trampoline(self, lRealHandle, dwDataType, pBuffer, dwBufSize, param, dwUser):
        if dwDataType != 0 or dwBufSize <= 0:
            return
        handler = self._handlers.get(lRealHandle)
        if handler is None:
            return
        data = ctypes.string_at(pBuffer, dwBufSize)
        try:
            handler(data)
        except Exception:
            log.exception("raw data handler error")

    def _on_disconnect(self, lLoginID, pchDVRIP, nDVRPort, dwUser):
        ip = pchDVRIP.decode(errors="replace") if pchDVRIP else "?"
        log.warning("Device disconnected: %s:%s (SDK will auto-reconnect)", ip, nDVRPort)


_STREAM_TYPES = {"main": 0, "sub": 3, "sub2": 4}


def _detected(parser: DhavParser) -> bool:
    return parser.codec is not None and parser.resolution != (0, 0)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def probe_streams(client, pairs: List[Tuple[int, str]], seconds: float,
                  concurrency: int = 4) -> List[ProbeResult]:
    """Probe (channel, stream) pairs in concurrent batches; preserve input order."""
    results = {}
    for batch in _chunks(pairs, max(1, concurrency)):
        active = []
        for channel, stream in batch:
            parser = DhavParser()

            def on_raw(data, _p=parser):
                for _ in _p.feed(data):
                    pass

            try:
                handle = client.start_realplay(channel, _STREAM_TYPES[stream], on_raw)
            except Exception as e:
                log.info("ch%s %s: cannot start (%s)", channel, stream, e)
                results[(channel, stream)] = ProbeResult(channel, stream, False, None, 0, 0)
                continue
            active.append((channel, stream, parser, handle))

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if all(_detected(p) for _, _, p, _ in active):
                break
            time.sleep(0.1)

        for channel, stream, parser, handle in active:
            client.stop_realplay(handle)
            w, h = parser.resolution
            results[(channel, stream)] = ProbeResult(
                channel, stream, parser.codec is not None, parser.codec, w, h)

    return [results[pair] for pair in pairs]


BACKEND = Backend(
    vendor="dahua",
    stream_types=dict(_STREAM_TYPES),
    default_port=37777,
    ffmpeg_input_args=("-f", "dhav"),
    client_factory=DahuaClient,
    list_channels=lambda c: list(range(c.channel_count)),
    probe=probe_streams,
)
```

- [ ] **Step 4: Replace `relay/sdk_client.py` with a shim**

```python
"""Backwards-compatible shim. DahuaClient now lives in relay.backends.dahua."""
from relay.backends.dahua import DahuaClient  # noqa: F401
```

- [ ] **Step 5: Delete `relay/probe.py` and update its consumers' imports**

```bash
git rm relay/probe.py
```
Update `tests/test_probe.py` line 4:
```python
from relay.backends.dahua import probe_streams
```
(`relay/__main__.py` still imports `from relay.probe import probe_streams` — it is rewired in Task 7. To keep the suite green until then, also update that import now:)
```python
from relay.backends.dahua import probe_streams
```

- [ ] **Step 6: Update `tests/test_config.py` stream-type assertions**

Replace the body of `test_stream_type_play_values` (lines 104-107) — stream types now come from the backend:
```python
def test_stream_type_play_values():
    from relay.backends.dahua import BACKEND
    assert BACKEND.stream_types == {"main": 0, "sub": 3, "sub2": 4}
```
Remove `STREAM_TYPES` from the `test_config.py` import on line 4 (leave `Config`).

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (including `tests/test_probe.py` and `tests/test_backends.py`). The DHAV
probe still works against the fixture through its new home.

- [ ] **Step 8: Commit**

```bash
git add relay/backends/dahua.py relay/sdk_client.py tests/ relay/__main__.py
git rm --cached relay/probe.py 2>/dev/null; git add -A relay/probe.py 2>/dev/null || true
git commit -m "refactor: move Dahua client + DHAV probe into relay/backends/dahua.py"
```

---

## Task 5: `Config.vendor` + backend-derived default port

**Files:**
- Modify: `relay/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `get_backend` from `relay.backends.base`.
- Produces: `Config.vendor:str` (from `VENDOR`, default `"dahua"`, lower-cased). `Config.port` defaults to the backend's `default_port` when `HOST_PORT` is unset.

- [ ] **Step 1: Write the failing tests (add to `tests/test_config.py`)**

```python
def test_vendor_defaults_to_dahua():
    cfg = Config.from_env(BASE)
    assert cfg.vendor == "dahua"


def test_vendor_is_lowercased():
    cfg = Config.from_env({**BASE, "VENDOR": "Hikvision"})
    assert cfg.vendor == "hikvision"


def test_default_port_follows_vendor():
    assert Config.from_env(BASE).port == 37777
    assert Config.from_env({**BASE, "VENDOR": "hikvision"}).port == 8000


def test_host_port_overrides_vendor_default():
    cfg = Config.from_env({**BASE, "VENDOR": "hikvision", "HOST_PORT": "8200"})
    assert cfg.port == 8200
```

(`BASE` already exists at the top of `tests/test_config.py` with HOST/USERNAME/PASSWORD.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_config.py -k "vendor or port" -v`
Expected: FAIL (`Config` has no `vendor`; default port is 37777 regardless of vendor).

- [ ] **Step 3: Update `relay/config.py`**

Remove the module-level `STREAM_TYPES` constant (now backend-owned). Add `vendor` and
vendor-aware default port:

```python
from relay.backends.base import get_backend
```
Add field to the dataclass (after `on_demand`):
```python
    vendor: str = "dahua"
```
Rewrite `from_env` port + vendor handling:
```python
    @classmethod
    def from_env(cls, env: dict) -> "Config":
        missing = [k for k in ("HOST", "USERNAME", "PASSWORD") if not env.get(k)]
        if missing:
            raise ValueError(f"Missing required env var(s): {', '.join(missing)}")
        vendor = (env.get("VENDOR") or "dahua").strip().lower()
        default_port = get_backend(vendor).default_port
        return cls(
            host=env["HOST"],
            username=env["USERNAME"],
            password=env["PASSWORD"],
            port=int(env["HOST_PORT"]) if env.get("HOST_PORT") else default_port,
            target_host=env.get("TARGET_HOST") or "0.0.0.0",
            target_port=int(env.get("TARGET_PORT") or 8554),
            target_username=env.get("TARGET_USERNAME") or "",
            target_password=env.get("TARGET_PASSWORD") or "",
            on_demand=_is_truthy(env.get("ON_DEMAND", "")),
            vendor=vendor,
        )
```
(`get_backend("dahua")` imports `relay.backends.dahua`, which is now pure to import.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS (all config tests, including the new ones).

- [ ] **Step 5: Commit**

```bash
git add relay/config.py tests/test_config.py
git commit -m "feat: add Config.vendor and backend-derived default port"
```

---

## Task 6: `FfmpegPublisher` per-vendor input args

**Files:**
- Modify: `relay/publisher.py`
- Test: `tests/test_publisher.py`

**Interfaces:**
- Produces: `FfmpegPublisher(rtsp_url:str, input_args:tuple[str,...])`. `_args()` returns
  `["ffmpeg","-hide_banner","-loglevel","warning", *input_args, "-i","pipe:0", "-c","copy", "-f","rtsp","-rtsp_transport","tcp", rtsp_url]`.

- [ ] **Step 1: Write the failing test (add to `tests/test_publisher.py`)**

```python
def test_input_args_are_placed_before_input():
    pub = FfmpegPublisher("rtsp://x/y", ("-f", "mpeg"))
    args = pub._args()
    assert args[:2] == ["ffmpeg", "-hide_banner"]
    assert "-f" in args and args[args.index("-f") + 1] == "mpeg"
    assert args.index("-f") < args.index("pipe:0")     # demuxer flag precedes -i input
    assert args[-4:] == ["-f", "rtsp", "-rtsp_transport", "tcp"] or args[-1] == "rtsp://x/y"
    assert args[-1] == "rtsp://x/y"
```

Update the existing `test_ffmpeg_started_in_own_session` to pass `input_args`:
```python
    pub = FfmpegPublisher("rtsp://x/y", ("-f", "dhav"))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_publisher.py -v`
Expected: FAIL (`__init__` takes 1 positional arg; `_args` hardcodes `-f dhav`).

- [ ] **Step 3: Update `relay/publisher.py`**

Change the constructor + `_args` (rest of the file unchanged):
```python
    def __init__(self, rtsp_url: str, input_args):
        self.rtsp_url = rtsp_url
        self._input_args = list(input_args)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def _args(self):
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            *self._input_args, "-i", "pipe:0",
            "-c", "copy",
            "-f", "rtsp", "-rtsp_transport", "tcp",
            self.rtsp_url,
        ]
```
Update the module docstring's `-f dhav` mention to note the input format is vendor-supplied.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_publisher.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add relay/publisher.py tests/test_publisher.py
git commit -m "feat: FfmpegPublisher takes per-vendor demuxer input args"
```

---

## Task 7: Wire consumers to the backend (`pipeline`, `manager`, `__main__`)

Thread `input_args` into `StreamPipeline`, and resolve client/stream_types/channels via
`get_backend(cfg.vendor)`. After this task the Dahua path runs end-to-end through the new
seam (behavior identical), and everything is ready for the Hikvision backend.

**Files:**
- Modify: `relay/pipeline.py`, `relay/manager.py`, `relay/__main__.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `Backend` (`client_factory`, `stream_types`, `ffmpeg_input_args`, `list_channels`, `probe`) via `get_backend(cfg.vendor)`.
- Produces: `StreamPipeline(rtsp_url, input_args, max_queue=256, publisher=None)`.

- [ ] **Step 1: Update `StreamPipeline` to pass input args to its default publisher**

Read `tests/test_pipeline.py` first. Then change `relay/pipeline.py` `__init__`:
```python
    def __init__(self, rtsp_url: str, input_args=("-f", "dhav"), max_queue: int = 256, publisher=None):
        self.rtsp_url = rtsp_url
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=max_queue)
        self._publisher = publisher or FfmpegPublisher(rtsp_url, input_args)
        self._worker = None
        self._stop = threading.Event()
        self._dropped = 0
```
If `tests/test_pipeline.py` constructs `StreamPipeline(...)` with a real (default)
publisher, ensure it passes a fake `publisher=` or an `input_args`; update the test call
if needed so no real ffmpeg spawns. Run `uv run pytest tests/test_pipeline.py -v` →
Expected: PASS.

- [ ] **Step 2: Rewire `relay/manager.py`**

```python
"""Shared-login daemon: one SDK client login, many StreamPipelines (always-on)."""
import logging
from typing import List

from relay.backends.base import Backend
from relay.config import Config
from relay.pipeline import StreamPipeline
from relay.streams_config import StreamEntry

log = logging.getLogger(__name__)


class StreamManager:
    def __init__(self, cfg: Config, backend: Backend, entries: List[StreamEntry]):
        self.cfg = cfg
        self.backend = backend
        self._entries = [e for e in entries if e.enable]
        self._client = backend.client_factory()
        self._sessions = []  # list[(handle, StreamPipeline, StreamEntry)]

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    @property
    def started(self) -> List[StreamEntry]:
        return [entry for _, _, entry in self._sessions]

    def start(self):
        self._client.init()
        self._client.login(self.cfg.host, self.cfg.port,
                           self.cfg.username, self.cfg.password)
        for e in self._entries:
            pipeline = StreamPipeline(self.cfg.publish_url(e.name),
                                      self.backend.ffmpeg_input_args)
            pipeline.start()
            try:
                handle = self._client.start_realplay(
                    e.channel, self.backend.stream_types[e.stream], pipeline.on_raw)
            except Exception:
                log.exception("ch%s %s: failed to start; skipping", e.channel, e.stream)
                pipeline.stop()
                continue
            self._sessions.append((handle, pipeline, e))
            log.info("streaming ch%s %s -> %s", e.channel, e.stream,
                     self.cfg.viewer_url(e.name))

    def stop(self):
        for handle, pipeline, _ in self._sessions:
            self._client.stop_realplay(handle)
            pipeline.stop()
        self._sessions.clear()
        self._client.cleanup()
```

- [ ] **Step 3: Rewire `relay/__main__.py`**

Replace the Dahua-specific imports and usages. Key edits:

Imports (replace lines 13-19):
```python
from relay import streams_config
from relay.backends.base import get_backend
from relay.config import Config
from relay.manager import StreamManager
from relay.pipeline import StreamPipeline
from relay.playlist import build_m3u8
```

`_serve_one` — take `backend`, use it for the client, stream type, and input args:
```python
def _serve_one(cfg: Config, backend, channel: int, stream: str, name: str):
    pipeline = StreamPipeline(cfg.publish_url(name), backend.ffmpeg_input_args)
    client = backend.client_factory()
    client.init()
    client.login(cfg.host, cfg.port, cfg.username, cfg.password)
    pipeline.start()
    client.start_realplay(channel, backend.stream_types[stream], pipeline.on_raw)
    log.info("Streaming ch%s %s (RTSP auth %s)", channel, stream,
             "enabled" if cfg.rtsp_auth_enabled else "disabled")
    log.info("Stream ready — view at: %s", cfg.viewer_url(name))

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        time.sleep(0.5)
    log.info("Shutting down...")
    pipeline.stop()
    client.cleanup()
```

`cmd_stream`, `cmd_serve` pass the backend through:
```python
def cmd_stream(cfg: Config, backend, args):
    _serve_one(cfg, backend, args.channel, args.stream, args.name)


def cmd_serve(cfg: Config, backend, args) -> int:
    entries = streams_config.load(args.config)
    try:
        entry = streams_config.find_enabled(entries, args.name)
    except LookupError as e:
        log.error("serve: %s (in %s)", e, args.config)
        return 1
    _serve_one(cfg, backend, entry.channel, entry.stream, entry.name)
    return 0
```

`cmd_run` — pass `backend` to `StreamManager`:
```python
    manager = StreamManager(cfg, backend, entries)
```
(the `_run_on_demand` branch is unchanged — it writes the playlist only.)

`cmd_parse` — use `backend.client_factory`, `backend.list_channels`, `backend.probe`:
```python
def cmd_parse(cfg: Config, backend, args) -> int:
    client = backend.client_factory()
    client.init()
    client.login(cfg.host, cfg.port, cfg.username, cfg.password)
    channels = range(args.channels) if args.channels else backend.list_channels(client)
    stream_names = args.streams.split(",")
    pairs = [(ch, st) for ch in channels for st in stream_names]
    log.info("Probing %d stream(s) at concurrency %d...", len(pairs), args.probe_concurrency)
    results = backend.probe(client, pairs, args.probe_seconds, args.probe_concurrency)
    client.cleanup()
    existing = streams_config.load(args.config)
    merged = streams_config.merge(existing, results)
    streams_config.save(args.config, merged)
    enabled = sum(1 for e in merged if e.enable)
    print(f"Wrote {args.config}: {len(merged)} stream(s), {enabled} enabled.")
    return 0
```

`main()` — build the backend once and thread it in; make `--stream` choices vendor-aware:
```python
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    cfg = Config.from_env(os.environ)
    backend = get_backend(cfg.vendor)

    ap = argparse.ArgumentParser(prog="relay")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run all enabled streams from a config file")
    r.add_argument("--config", default=DEFAULT_CONFIG)

    s = sub.add_parser("stream", help="stream one channel/stream to RTSP")
    s.add_argument("--channel", type=int, required=True)
    s.add_argument("--stream", choices=list(backend.stream_types), default="main")
    s.add_argument("--name", required=True, help="RTSP path name")

    sv = sub.add_parser("serve", help="serve one stream by name (used by MediaMTX runOnDemand)")
    sv.add_argument("name", help="RTSP path name (matches a stream in the config)")
    sv.add_argument("--config", default=DEFAULT_CONFIG)

    p = sub.add_parser("parse", help="probe channels/streams -> merge into streams.yml")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--streams", default="main,sub")
    p.add_argument("--channels", type=int, default=0, help="0 = use device channel list")
    p.add_argument("--probe-seconds", type=float, default=3.0)
    p.add_argument("--probe-concurrency", type=int, default=4,
                   help="how many streams to probe at once (default 4)")

    args = ap.parse_args()
    if args.cmd == "stream":
        cmd_stream(cfg, backend, args)
        rc = 0
    elif args.cmd == "serve":
        rc = cmd_serve(cfg, backend, args)
    elif args.cmd == "run":
        rc = cmd_run(cfg, backend, args)
    else:
        rc = cmd_parse(cfg, backend, args)
    sys.exit(rc)
```
`cmd_run(cfg, args)` signature becomes `cmd_run(cfg, backend, args)` — update its `def` and
the `_run_on_demand(cfg, args, entries)` call inside stays as is (it doesn't need backend).

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Smoke-check the CLI wiring (no device needed)**

Run:
```bash
uv run python -m relay --help && VENDOR=hikvision uv run python -m relay stream --help
```
Expected: help prints; under `VENDOR=hikvision` the `stream --stream` choices show
`{main,sub}` (proves vendor-aware choices resolve). No SDK is loaded for `--help`.

- [ ] **Step 6: Commit**

```bash
git add relay/pipeline.py relay/manager.py relay/__main__.py tests/test_pipeline.py
git commit -m "refactor: resolve SDK client/stream-types/probe via get_backend(vendor)"
```

---

## Task 8: `relay/backends/hikvision.py` — Hikvision backend

Uses the ABI copied from `hcnetsdk/HCNetSDK.h` (verified in Task 2) and the format the
spike confirmed. `ctypes.CDLL` load is lazy (in `init()`), so the module imports purely.

**Files:**
- Create: `relay/backends/hikvision.py`
- Test: `tests/test_hikvision.py`

**Interfaces:**
- Consumes: `Backend`, `ProbeResult` from `relay.backends.base`.
- Produces:
  - `relay.backends.hikvision.enumerate_channels(chan_num, start_chan, ip_chan_num, start_dchan, high_dchan_num=0)->list[int]` — pure channel math.
  - `HikvisionClient` implementing `SdkClient`.
  - `relay.backends.hikvision.BACKEND` — `Backend(vendor="hikvision", stream_types={"main":0,"sub":1}, default_port=8000, ffmpeg_input_args=("-f","mpeg"), client_factory=HikvisionClient, list_channels=<reads client device info>, probe=probe_streams)`.
  - **NOTE:** set `ffmpeg_input_args` to the value Task 2's spike confirmed (default assumption `("-f", "mpeg")`).

- [ ] **Step 1: Write the failing test (pure channel math + backend metadata)**

```python
# tests/test_hikvision.py
import relay  # noqa: F401
from relay.backends.hikvision import enumerate_channels, BACKEND


def test_enumerate_analog_plus_ip():
    # handoff device: 16 analog from 1, 18 IP from 33
    assert enumerate_channels(16, 1, 18, 33) == list(range(1, 17)) + list(range(33, 51))


def test_enumerate_analog_only():
    assert enumerate_channels(4, 1, 0, 0) == [1, 2, 3, 4]


def test_enumerate_ip_only():
    assert enumerate_channels(0, 1, 4, 33) == [33, 34, 35, 36]


def test_enumerate_high_ip_count():
    # high byte extends the IP channel count beyond 255
    assert enumerate_channels(0, 1, 0, 1, high_dchan_num=1) == list(range(1, 257))


def test_hikvision_backend_metadata():
    assert BACKEND.vendor == "hikvision"
    assert BACKEND.stream_types == {"main": 0, "sub": 1}
    assert BACKEND.default_port == 8000
    assert BACKEND.ffmpeg_input_args[0] == "-f"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_hikvision.py -v`
Expected: FAIL (`ModuleNotFoundError: relay.backends.hikvision`).

- [ ] **Step 3: Create `relay/backends/hikvision.py`**

```python
"""Hikvision HCNetSDK backend. ctypes bindings authored from hcnetsdk/HCNetSDK.h.

Importing this module is pure (only defines structs/callbacks). The .so is loaded
lazily in HikvisionClient.init().
"""
import ctypes as C
import logging
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Tuple

import relay  # noqa: F401
from relay.backends.base import Backend, ProbeResult

log = logging.getLogger(__name__)

BYTE, WORD, DWORD, LONG, BOOL = C.c_ubyte, C.c_ushort, C.c_uint32, C.c_int32, C.c_int32
SDK_DIR = Path(relay.__file__).resolve().parent.parent / "hcnetsdk"

NET_DVR_SYSHEAD = 1
NET_DVR_STREAMDATA = 2
NET_SDK_INIT_CFG_SDK_PATH = 2

RawDataHandler = Callable[[bytes], None]


# --- ctypes structs (exact layout from HCNetSDK.h) ---
class NET_DVR_DEVICEINFO_V30(C.Structure):
    _fields_ = [
        ("sSerialNumber", BYTE * 48), ("byAlarmInPortNum", BYTE), ("byAlarmOutPortNum", BYTE),
        ("byDiskNum", BYTE), ("byDVRType", BYTE), ("byChanNum", BYTE), ("byStartChan", BYTE),
        ("byAudioChanNum", BYTE), ("byIPChanNum", BYTE), ("byZeroChanNum", BYTE),
        ("byMainProto", BYTE), ("bySubProto", BYTE), ("bySupport", BYTE), ("bySupport1", BYTE),
        ("bySupport2", BYTE), ("wDevType", WORD), ("bySupport3", BYTE), ("byMultiStreamProto", BYTE),
        ("byStartDChan", BYTE), ("byStartDTalkChan", BYTE), ("byHighDChanNum", BYTE),
        ("bySupport4", BYTE), ("byLanguageType", BYTE), ("byVoiceInChanNum", BYTE),
        ("byStartVoiceInChanNo", BYTE), ("bySupport5", BYTE), ("bySupport6", BYTE),
        ("byMirrorChanNum", BYTE), ("wStartMirrorChanNo", WORD), ("bySupport7", BYTE), ("byRes2", BYTE),
    ]


class NET_DVR_DEVICEINFO_V40(C.Structure):
    _fields_ = [
        ("struDeviceV30", NET_DVR_DEVICEINFO_V30), ("bySupportLock", BYTE), ("byRetryLoginTime", BYTE),
        ("byPasswordLevel", BYTE), ("byProxyType", BYTE), ("dwSurplusLockTime", DWORD),
        ("byCharEncodeType", BYTE), ("bySupportDev5", BYTE), ("bySupport", BYTE), ("byLoginMode", BYTE),
        ("dwOEMCode", DWORD), ("iResidualValidity", C.c_int), ("byResidualValidity", BYTE),
        ("bySingleStartDTalkChan", BYTE), ("bySingleDTalkChanNums", BYTE), ("byPassWordResetLevel", BYTE),
        ("bySupportStreamEncrypt", BYTE), ("byMarketType", BYTE), ("byTLSCap", BYTE), ("byRes2", BYTE * 237),
    ]


class NET_DVR_USER_LOGIN_INFO(C.Structure):
    _fields_ = [
        ("sDeviceAddress", C.c_char * 129), ("byUseTransport", BYTE), ("wPort", WORD),
        ("sUserName", C.c_char * 64), ("sPassword", C.c_char * 64), ("cbLoginResult", C.c_void_p),
        ("pUser", C.c_void_p), ("bUseAsynLogin", BOOL), ("byProxyType", BYTE), ("byUseUTCTime", BYTE),
        ("byLoginMode", BYTE), ("byHttps", BYTE), ("iProxyID", LONG), ("byVerifyMode", BYTE),
        ("byRes3", BYTE * 119),
    ]


class NET_DVR_PREVIEWINFO(C.Structure):
    _fields_ = [
        ("lChannel", LONG), ("dwStreamType", DWORD), ("dwLinkMode", DWORD), ("hPlayWnd", C.c_void_p),
        ("bBlocked", DWORD), ("bPassbackRecord", DWORD), ("byPreviewMode", BYTE), ("byStreamID", BYTE * 32),
        ("byProtoType", BYTE), ("byRes1", BYTE), ("byVideoCodingType", BYTE), ("dwDisplayBufNum", DWORD),
        ("byNPQMode", BYTE), ("byRecvMetaData", BYTE), ("byDataType", BYTE), ("byRes", BYTE * 213),
    ]


class NET_DVR_LOCAL_SDK_PATH(C.Structure):
    _fields_ = [("sPath", C.c_char * 256), ("byRes", BYTE * 128)]


REALDATACALLBACK = C.CFUNCTYPE(None, LONG, DWORD, C.POINTER(BYTE), DWORD, C.c_void_p)


def enumerate_channels(chan_num, start_chan, ip_chan_num, start_dchan, high_dchan_num=0):
    """Valid channel numbers: analog block + IP block, from device-info fields."""
    analog = list(range(start_chan, start_chan + chan_num)) if chan_num else []
    ip_count = ip_chan_num + (high_dchan_num << 8)
    ip = list(range(start_dchan, start_dchan + ip_count)) if (ip_count and start_dchan) else []
    return analog + ip


class HikvisionClient:
    def __init__(self):
        self._sdk = None
        self._login_id = -1
        self._devinfo = None
        self._realdata_cb = REALDATACALLBACK(self._raw_trampoline)  # keep referenced
        self._handlers: dict[int, RawDataHandler] = {}

    def init(self):
        self._sdk = C.CDLL(str(SDK_DIR / "libhcnetsdk.so"))
        self._sdk.NET_DVR_Login_V40.restype = LONG
        self._sdk.NET_DVR_RealPlay_V40.restype = LONG
        self._sdk.NET_DVR_GetLastError.restype = DWORD
        p = NET_DVR_LOCAL_SDK_PATH()
        p.sPath = str(SDK_DIR).encode()
        self._sdk.NET_DVR_SetSDKInitCfg(NET_SDK_INIT_CFG_SDK_PATH, C.byref(p))
        if not self._sdk.NET_DVR_Init():
            raise RuntimeError("NET_DVR_Init failed")
        # Confirmed in the spike: a remote WAN device needs a generous connect timeout,
        # otherwise the (non-blocking) preview never establishes and no data flows.
        self._sdk.NET_DVR_SetConnectTime(8000, 2)
        self._sdk.NET_DVR_SetReconnect(10000, 1)
        log.info("HCNetSDK initialized")

    def login(self, host: str, port: int, username: str, password: str) -> int:
        info = NET_DVR_USER_LOGIN_INFO()
        info.sDeviceAddress = host.encode()
        info.wPort = int(port)
        info.sUserName = username.encode()
        info.sPassword = password.encode()
        info.bUseAsynLogin = 0
        dev = NET_DVR_DEVICEINFO_V40()
        uid = self._sdk.NET_DVR_Login_V40(C.byref(info), C.byref(dev))
        if uid < 0:
            raise RuntimeError(f"Login failed: HCNetSDK error {self._sdk.NET_DVR_GetLastError()}")
        self._login_id = uid
        self._devinfo = dev.struDeviceV30
        log.info("Logged in: uid=%s analog=%s ip=%s", uid,
                 self._devinfo.byChanNum, self._devinfo.byIPChanNum)
        return uid

    @property
    def channel_count(self) -> int:
        d = self._devinfo
        if not d:
            return 0
        return d.byChanNum + d.byIPChanNum + (d.byHighDChanNum << 8)

    def channels(self) -> List[int]:
        d = self._devinfo
        return enumerate_channels(d.byChanNum, d.byStartChan, d.byIPChanNum,
                                  d.byStartDChan, d.byHighDChanNum)

    def start_realplay(self, channel: int, play_type: int, on_raw: RawDataHandler) -> int:
        pv = NET_DVR_PREVIEWINFO()
        pv.lChannel = channel
        pv.dwStreamType = play_type
        pv.dwLinkMode = 0        # TCP/private
        pv.hPlayWnd = None       # headless
        pv.bBlocked = 0          # non-blocking: required for data to flow (spike-confirmed)
        handle = self._sdk.NET_DVR_RealPlay_V40(self._login_id, C.byref(pv),
                                                self._realdata_cb, None)
        if handle < 0:
            raise RuntimeError(
                f"RealPlay_V40 failed for channel={channel} play_type={play_type}: "
                f"HCNetSDK error {self._sdk.NET_DVR_GetLastError()}")
        self._handlers[handle] = on_raw
        log.info("RealPlay started: handle=%s channel=%s play_type=%s", handle, channel, play_type)
        return handle

    def stop_realplay(self, handle: int):
        if handle in self._handlers:
            self._sdk.NET_DVR_StopRealPlay(handle)
            del self._handlers[handle]

    def logout(self):
        if self._login_id >= 0:
            self._sdk.NET_DVR_Logout(self._login_id)
            self._login_id = -1

    def cleanup(self):
        for handle in list(self._handlers):
            self._sdk.NET_DVR_StopRealPlay(handle)
        self._handlers.clear()
        self.logout()
        if self._sdk:
            self._sdk.NET_DVR_Cleanup()

    def _raw_trampoline(self, handle, dtype, buf, size, user):
        if dtype not in (NET_DVR_SYSHEAD, NET_DVR_STREAMDATA) or size <= 0:
            return
        handler = self._handlers.get(handle)
        if handler is None:
            return
        data = bytes((C.c_ubyte * size).from_address(C.addressof(buf.contents)))
        try:
            handler(data)
        except Exception:
            log.exception("raw data handler error")


def _ffprobe_codec_resolution(sample: bytes):
    """Return (codec, width, height) from a raw sample, or (None, 0, 0)."""
    proc = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error",
         "-probesize", "5000000", "-analyzeduration", "5000000", "-f", "mpeg",
         "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
         "-of", "default=noprint_wrappers=1:nokey=0", "-i", "pipe:0"],
        input=sample, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    codec, w, h = None, 0, 0
    for line in proc.stdout.decode(errors="replace").splitlines():
        k, _, v = line.partition("=")
        if k == "codec_name" and v:
            codec = v
        elif k == "width" and v.isdigit():
            w = int(v)
        elif k == "height" and v.isdigit():
            h = int(v)
    return codec, w, h


def probe_streams(client, pairs: List[Tuple[int, str]], seconds: float,
                  concurrency: int = 4) -> List[ProbeResult]:
    """Probe each (channel, stream): capture a few seconds, detect codec via ffprobe."""
    results = {}
    for channel, stream in pairs:
        buf = bytearray()

        def on_raw(data, _b=buf):
            if len(_b) < 2_000_000:      # cap the sample size
                _b.extend(data)

        try:
            handle = client.start_realplay(channel, BACKEND.stream_types[stream], on_raw)
        except Exception as e:
            log.info("ch%s %s: cannot start (%s)", channel, stream, e)
            results[(channel, stream)] = ProbeResult(channel, stream, False, None, 0, 0)
            continue
        time.sleep(seconds)
        client.stop_realplay(handle)
        codec, w, h = _ffprobe_codec_resolution(bytes(buf)) if buf else (None, 0, 0)
        results[(channel, stream)] = ProbeResult(channel, stream, codec is not None, codec, w, h)
    return [results[pair] for pair in pairs]


BACKEND = Backend(
    vendor="hikvision",
    stream_types={"main": 0, "sub": 1},
    default_port=8000,
    # Spike-confirmed: MPEG-PS (IMKH SYSHEAD + PS packets), h264, -c copy works.
    # Bigger probe window + genpts tame the PS's non-monotonic DTS over a live pipe.
    ffmpeg_input_args=("-fflags", "+genpts", "-probesize", "5000000",
                       "-analyzeduration", "5000000", "-f", "mpeg"),
    client_factory=HikvisionClient,
    list_channels=lambda c: c.channels(),
    probe=probe_streams,
)
```

- [ ] **Step 4: Run to verify the pure tests pass**

Run: `uv run pytest tests/test_hikvision.py -v`
Expected: PASS (channel math + metadata; no device or `.so` load involved).

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add relay/backends/hikvision.py tests/test_hikvision.py
git commit -m "feat: add Hikvision HCNetSDK backend"
```

---

## Task 9: Docker, env, and docs

**Files:**
- Modify: `deploy/Dockerfile`, `.env.example`, `AGENTS.md`, `README.md`

**Interfaces:**
- Produces: a runnable container for either vendor, and updated operator docs.

- [ ] **Step 1: Update the Dockerfile**

In `deploy/Dockerfile`, add the Hikvision copy + library path. After the existing
`COPY NetSDK/ ./NetSDK/` line add:
```dockerfile
COPY hcnetsdk/ ./hcnetsdk/
```
Replace the `LD_LIBRARY_PATH` line with:
```dockerfile
ENV LD_LIBRARY_PATH=/app/NetSDK/Libs/linux64:/app/hcnetsdk:/app/hcnetsdk/HCNetSDKCom
```
(Both vendors' lib dirs are always present; only the selected vendor's libs are loaded.)

- [ ] **Step 2: Update `.env.example`**

Read it first, then add (near the device settings):
```
# Which camera vendor SDK to use: dahua (default) or hikvision.
VENDOR=dahua
# Device SDK port. Default depends on vendor (Dahua 37777, Hikvision 8000).
# Set explicitly to override — e.g. some Hikvision devices expose 8200.
# HOST_PORT=8000
```

- [ ] **Step 3: Update `AGENTS.md`**

Add to the layout/overview: the `relay/backends/` package (base + dahua + hikvision), the
`VENDOR` switch, the "we author the Hikvision ctypes bindings; `hcnetsdk/` holds only
`.so`" distinction, and the no-Dahua-testing caveat. Update the top diagram to show both
vendors feeding `ffmpeg -c copy` (Dahua via `-f dhav`, Hikvision via the confirmed `-f`).

- [ ] **Step 4: Update root `README.md`**

Document choosing a vendor via `VENDOR`, the `hcnetsdk/setup.sh` step (mirroring the
NetSDK step), and the per-vendor default ports.

- [ ] **Step 5: Verify the image builds**

Run: `docker compose build`
Expected: build succeeds (both SDK dirs copy; `LD_LIBRARY_PATH` set).

- [ ] **Step 6: Commit**

```bash
git add deploy/Dockerfile .env.example AGENTS.md README.md
git commit -m "docs: document Hikvision vendor + wire hcnetsdk into the image"
```

---

## Task 10: End-to-end validation (live Hikvision DVR)

Not TDD — a manual acceptance run against the real device.

**Files:** none (validation only).

- [ ] **Step 1: Configure for Hikvision**

Set in `.env`: `VENDOR=hikvision`, `HOST`, `HOST_PORT` (e.g. 8200), `USERNAME`, `PASSWORD`.

- [ ] **Step 2: Probe channels**

Run: `LD_LIBRARY_PATH=hcnetsdk:hcnetsdk/HCNetSDKCom uv run python -m relay parse`
Expected: `config/streams.yml` is written; enabled entries have `metadata.codec` +
`metadata.resolution` for reachable channels (analog + IP per the device layout).

- [ ] **Step 3: Run and view**

Run: `LD_LIBRARY_PATH=hcnetsdk:hcnetsdk/HCNetSDKCom uv run python -m relay run` (or
`docker compose up --build`). Open `config/output.m3u8` (or an individual
`rtsp://localhost:8554/<name>`) in VLC/ffplay.
Expected: video plays; `ffprobe rtsp://localhost:8554/<name>` shows the native codec with
no transcode. Verify both a `main` and a `sub` stream.

- [ ] **Step 4: Confirm no regression in the abstraction**

Run: `uv run pytest -q`
Expected: PASS (all unit tests).

- [ ] **Step 5: Update the memory note**

Once streaming works, update the auto-memory: mark Hikvision support as working and record
the confirmed callback container format (from the spike) for future reference.

---

## Self-Review

**Spec coverage:**
- Section 1 (packaging) → Task 1. ✓
- Section 2 (abstraction) → Tasks 3–7. ✓
- Section 3 (Hikvision backend) → Task 8. ✓
- Section 4 (spike) → Task 2. ✓
- Section 5 (Docker/env/docs) → Task 9. ✓
- Section 6 (testing) → TDD steps throughout + Task 10. ✓

**Type consistency:** `Backend` fields (`client_factory`, `list_channels`, `probe`,
`stream_types`, `ffmpeg_input_args`, `default_port`) are used identically in `manager.py`,
`__main__.py`, and both backend modules. `ProbeResult` shape matches `streams_config.merge`'s
duck-typed contract (`channel/stream/ok/codec/width/height`). `StreamPipeline(rtsp_url,
input_args, ...)` and `FfmpegPublisher(rtsp_url, input_args)` signatures match their callers.
`get_backend` / `Config.vendor` casing handled consistently (lower-cased).

**Placeholder scan:** the one deliberate deferral is `hikvision.BACKEND.ffmpeg_input_args`,
which Task 2 confirms empirically before Task 8 sets it (default assumption `("-f","mpeg")`);
Task 2 Step 3 stops the plan if the spike disproves direct demux. No other placeholders.
