# Hikvision HCNetSDK — implementation notes

Hard-won facts from wiring the Hikvision backend (`relay/backends/hikvision.py`). Hikvision
ships **only C `.so` libraries — no Python bindings**, so we author the ctypes ourselves. If
the SDK is upgraded, the struct layouts may shift and must be **re-derived from the shipped
header**. This note is the map for doing that quickly.

Built against **`EN-HCNetSDKV6.1.9.48_build20230410_linux64`**. `hcnetsdk/setup.sh` copies the
header to `hcnetsdk/HCNetSDK.h` — that file is the ABI source of truth, not this note.

## Re-parsing the header (do this after any SDK upgrade)

`HCNetSDK.h` is ~2.5 MB, **CRLF line endings, ISO-8859/Latin-1** (Chinese comments). Practical grep:

```bash
grep -an 'tagNET_DVR_PREVIEWINFO' hcnetsdk/HCNetSDK.h        # -a: treat as text (else "binary, no match")
sed -n '13651,13681p' hcnetsdk/HCNetSDK.h | tr -d '\r'       # read a struct body cleanly
grep -an 'NET_DVR_API .*__stdcall NET_DVR_RealPlay_V40'      # function signatures live ~line 50500+
```

Struct bodies are `typedef struct tagXXX { ... } XXX, *LPXXX;`. Reproduce field order, integer
widths, and **reserved-byte counts** exactly.

**Layouts drift between builds** — e.g. this build's `NET_DVR_DEVICEINFO_V40` ends with
`byTLSCap; byRes2[237]`, but an older build (the original handoff) had `byRes2[238]` and no
`byTLSCap`. Always trust the header that shipped with the libs you're loading.

## Base types (Linux x86-64)

```
BYTE=c_ubyte  WORD=c_ushort  DWORD=c_uint32  LONG=c_int32  BOOL=c_int32  HWND=c_void_p (8 bytes)
```

- Do **not** set `_pack_` on the ctypes structs — natural alignment matches the compiler. In
  `NET_DVR_PREVIEWINFO`, `hPlayWnd` (a `void*`) forces 4 bytes of padding after the three
  leading 32-bit fields; ctypes inserts it automatically. Setting `_pack_=1` would break it.
- `CALLBACK`/`__stdcall` expand to nothing on Linux x64 (only one calling convention), so use
  plain `ctypes.CFUNCTYPE` for callbacks.

## Login (proven; do not change to V30)

`NET_DVR_Login_V40(LPNET_DVR_USER_LOGIN_INFO, LPNET_DVR_DEVICEINFO_V40) -> LONG` (uid; <0 = fail,
check `NET_DVR_GetLastError`). **`NET_DVR_Login_V30` fails with error 17** on this device — don't.
Login mode 0 (private protocol). Device tested: `DS-7216HGHI-F1/N`.

## SDK init & component loading (Linux gotcha)

Before `NET_DVR_Init`, point the SDK at its component dir or component load fails:

```python
NET_DVR_SetSDKInitCfg(2, &NET_DVR_LOCAL_SDK_PATH{sPath=<abs path to hcnetsdk/>})
# 2 == NET_SDK_INIT_CFG_SDK_PATH ; 3 == NET_SDK_INIT_CFG_LIBEAY_PATH (libcrypto/ssl) if needed
```

Also set `LD_LIBRARY_PATH=hcnetsdk:hcnetsdk/HCNetSDKCom` (Dockerfile does this). `HCNetSDKCom/`
holds the component `.so` (libHCPreview, libStreamTransClient, …); `libcrypto.so.1.1`/`libssl.so.1.1`
ship in the SDK too.

## Live preview — the two non-obvious traps

1. **Must be non-blocking on a WAN device.** `NET_DVR_PREVIEWINFO.bBlocked = 0` **plus**
   `NET_DVR_SetConnectTime(8000, 2)` and `NET_DVR_SetReconnect(10000, 1)` after `Init`. With
   `bBlocked = 1`, `RealPlay_V40` returns a valid handle but **the data callback never fires**
   over a slow/remote link. This cost hours — it looks like "login works, stream dead."
2. **An empty channel also returns a valid handle with zero callbacks** — that's "no camera on
   this input," not an error. Scan multiple channels to find live ones (on the test DVR only
   ch7 & ch8 of 16 analog + 18 IP had cameras).

Callback registration: passing the callback inline to `NET_DVR_RealPlay_V40` works; you can also
call `NET_DVR_SetRealDataCallBackEx(handle, cb, pUser)` after. **Keep the ctypes callback object
referenced for the handle's lifetime** or the SDK crashes when the GC collects it.

### REALDATACALLBACK signature (differs from Dahua's!)

```c
void (CALLBACK *REALDATACALLBACK)(LONG lPlayHandle, DWORD dwDataType, BYTE *pBuffer, DWORD dwBufSize, void *pUser)
```

5 args, not Dahua's 6. Data-type constants:

```
NET_DVR_SYSHEAD = 1   NET_DVR_STREAMDATA = 2   NET_DVR_AUDIOSTREAMDATA = 3   NET_DVR_PRIVATE_DATA = 112
```

Forward `SYSHEAD` (1) + `STREAMDATA` (2) bytes straight through to ffmpeg.

## Stream format (what ffmpeg gets)

The callback delivers Hikvision's **`IMKH` system header** (magic `49 4D 4B 48`, arrives as
`SYSHEAD`) followed by an **MPEG Program Stream** (`STREAMDATA`, pack headers `00 00 01 BA …`).
`ffmpeg -f mpeg -c copy` demuxes it — **h264, no transcode**. The PS has non-monotonic DTS over
a live pipe, so the input args are:

```
-fflags +genpts -probesize 5000000 -analyzeduration 5000000 -f mpeg
```

(ffprobe needs the same big probe window to detect the codec — the leading `IMKH` bytes throw off
short-probe format detection.) Tested resolutions varied per channel (960x1080, 1280x720; subs 352x288).

## Channels & stream types

- `dwStreamType`: **0 = main, 1 = sub** (2=third, 3=fourth, …). So `{main:0, sub:1}`.
- Channel numbers come **from the device**, not a 0-based range. From `NET_DVR_DEVICEINFO_V30`:
  analog `[byStartChan .. byStartChan+byChanNum)` + IP
  `[byStartDChan .. byStartDChan + byIPChanNum + (byHighDChanNum<<8))`. Test DVR: analog 1–16,
  IP start 33, 18 IP → 33–50. See `enumerate_channels()`.

## Probing for `parse`

No hand-written PS parser (unlike Dahua's `DhavParser`): capture a few seconds of callback bytes
per stream and run `ffprobe -f mpeg` to get codec + resolution. Batch concurrently off the one
login (the callback dispatches by handle). On a WAN device use a longer `--probe-seconds` (≥5) or
working channels can probe empty and get marked `enable: false`.
