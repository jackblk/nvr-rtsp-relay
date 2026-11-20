"""Hikvision backends and MPEG-PS probing.

The portable private TCP client is the default. The optional Linux SDK client
uses ctypes bindings from hcnetsdk/HCNetSDK.h and loads the .so in init().
"""
import ctypes as C
import logging
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, List, Tuple

import relay  # noqa: F401
from relay.backends.base import DEFAULT_PORTS, Backend, ProbeResult
from relay.backends.hikvision_client import HikvisionPrivateClient
from relay.backends.hikvision_protocol import enumerate_channels

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


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def probe_streams(client, pairs: List[Tuple[int, str]], seconds: float,
                  concurrency: int = 4) -> List[ProbeResult]:
    """Probe (channel, stream) pairs in concurrent batches; preserve input order.

    Each batch opens up to `concurrency` RealPlay sessions off the shared login and
    captures into per-stream buffers simultaneously (the SDK callback dispatches by
    handle), sharing a single wait, then ffprobe-detects each. This keeps `parse`
    time ~ceil(len(pairs)/concurrency) * seconds instead of len(pairs) * seconds.
    """
    results = {}
    for batch in _chunks(pairs, max(1, concurrency)):
        active = []  # (channel, stream, buf, handle)
        for channel, stream in batch:
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
            active.append((channel, stream, buf, handle))

        time.sleep(seconds)

        for channel, stream, buf, handle in active:
            client.stop_realplay(handle)
            codec, w, h = _ffprobe_codec_resolution(bytes(buf)) if buf else (None, 0, 0)
            results[(channel, stream)] = ProbeResult(channel, stream, codec is not None, codec, w, h)
    return [results[pair] for pair in pairs]


BACKEND = Backend(
    vendor="hikvision",
    stream_types={"main": 0, "sub": 1},
    default_port=DEFAULT_PORTS["hikvision"],
    # Both transports emit an IMKH header followed by MPEG-PS. A longer probe and
    # generated PTS also handle imperfect timestamps from native SDK streams.
    ffmpeg_input_args=("-fflags", "+genpts", "-probesize", "5000000",
                       "-analyzeduration", "5000000", "-f", "mpeg"),
    client_factory=HikvisionPrivateClient,
    list_channels=lambda c: c.channels(),
    probe=probe_streams,
)

SDK_BACKEND = replace(BACKEND, client_factory=HikvisionClient)
