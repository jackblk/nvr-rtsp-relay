"""Dahua NetSDK backend: DahuaClient + DHAV probe. Moved from relay/sdk_client.py.

No Dahua hardware is available to re-test this; keep the client logic unchanged. The
NetSDK import is lazy (in __init__) so importing this module stays pure.
"""
import ctypes
import logging
import time
from typing import Callable, List, Tuple

import relay  # noqa: F401  (sys.path bootstrap so NetSDK imports resolve)
from relay.backends.base import DEFAULT_PORTS, Backend, ProbeResult
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
        # Hold strong refs so ctypes callbacks are not garbage-collected.
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
        """Start a headless preview session; return its handle. May be called many times."""
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

    # --- ctypes callbacks ---
    def _raw_trampoline(self, lRealHandle, dwDataType, pBuffer, dwBufSize, param, dwUser):
        # dwDataType == 0 => raw (DHAV) stream bytes (video+audio multiplexed).
        if dwDataType != 0 or dwBufSize <= 0:
            return
        handler = self._handlers.get(lRealHandle)
        if handler is None:
            return
        data = ctypes.string_at(pBuffer, dwBufSize)
        try:
            handler(data)
        except Exception:                       # never let an exception cross into the SDK
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
    """Probe (channel, stream) pairs in concurrent batches; preserve input order.

    All probes in a batch run simultaneously (one RealPlay session each, off the
    shared login) and share a single wait, so total time is roughly
    ceil(len(pairs)/concurrency) * seconds instead of len(pairs) * seconds.
    """
    results = {}
    for batch in _chunks(pairs, max(1, concurrency)):
        active = []  # (channel, stream, parser, handle)
        for channel, stream in batch:
            parser = DhavParser()

            def on_raw(data, _p=parser):  # bind parser per iteration
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
    default_port=DEFAULT_PORTS["dahua"],
    ffmpeg_input_args=("-f", "dhav"),
    client_factory=DahuaClient,
    list_channels=lambda c: list(range(c.channel_count)),
    probe=probe_streams,
)
