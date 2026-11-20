"""Portable live-preview client for the relay's Hikvision backend."""

import itertools
import logging
import socket
import threading
import time
from typing import Callable

from relay.backends.hikvision_media import ProgramStream, read_rtp
from relay.backends.hikvision_protocol import DeviceSession, HikvisionProtocolError, connect, login

log = logging.getLogger(__name__)


class _Preview:
    def __init__(self, address: tuple[str, int], session: DeviceSession, channel: int,
                 stream: int, on_raw: Callable[[bytes], None]):
        self._address = address
        self._session = session
        self._channel = channel
        self._stream = stream
        self._on_raw = on_raw
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connection = None
        self._preview_open = False
        self._started_at = time.monotonic()
        self._worker = threading.Thread(target=self._run, name=f'hikvision-ch{channel}-{stream}', daemon=True)

    def start(self) -> None:
        self._worker.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with connect(*self._address) as conn:
                    self._receive(conn)
            except (OSError, EOFError, HikvisionProtocolError) as error:
                if not self._stop.is_set():
                    log.debug('Hikvision ch%s type %s preview ended: %s', self._channel, self._stream, error)
            except Exception:
                log.exception('Hikvision ch%s type %s preview worker failed', self._channel, self._stream)
                return
            finally:
                with self._lock:
                    self._connection = None
                    self._preview_open = False
            self._stop.wait(2)

    def _receive(self, conn: socket.socket) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._connection = conn
        header = self._session.start_preview(conn, self._channel, self._stream)
        if not header.startswith(b'IMKH'):
            raise HikvisionProtocolError('unsupported Hikvision preview system header')
        with self._lock:
            if self._stop.is_set():
                return
            self._preview_open = True
        # Reconnecting gives us a new RTP clock while ffmpeg keeps the same input pipe.
        mux = ProgramStream(start_pts=90_000 + int((time.monotonic() - self._started_at) * 90_000))
        self._on_raw(header)
        while not self._stop.is_set():
            packet = mux.feed(read_rtp(conn))
            if packet is not None and not self._stop.is_set():
                self._on_raw(packet)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            conn = self._connection
            if conn is not None:
                if self._preview_open:
                    try:
                        conn.settimeout(0.25)
                        self._session.stop_preview(conn)
                    except OSError:
                        pass
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self._worker.join(timeout=1)


class HikvisionPrivateClient:
    def __init__(self):
        self._address = None
        self._session = None
        self._previews: dict[int, _Preview] = {}
        self._handles = itertools.count()

    def init(self) -> None:
        """The Python transport has no process-wide initialization."""

    def login(self, host: str, port: int, username: str, password: str) -> int:
        self.logout()
        self._session = login(host, port, username, password)
        self._address = (host, port)
        log.info('Logged in over Hikvision private TCP (%d channels)', self.channel_count)
        return 0

    @property
    def channel_count(self) -> int:
        return len(self._session.channels) if self._session else 0

    def channels(self) -> list[int]:
        return list(self._session.channels) if self._session else []

    def start_realplay(self, channel: int, play_type: int, on_raw: Callable[[bytes], None]) -> int:
        if self._session is None:
            raise RuntimeError('Hikvision client is logged out')
        if not 1 <= channel <= 0xFFFFFFFF or play_type not in (0, 1):
            raise ValueError('Hikvision preview requires a positive channel and a main or substream')
        handle = next(self._handles)
        preview = _Preview(self._address, self._session, channel, play_type, on_raw)
        preview.start()
        self._previews[handle] = preview
        return handle

    def stop_realplay(self, handle: int) -> None:
        preview = self._previews.pop(handle, None)
        if preview is not None:
            preview.stop()

    def logout(self) -> None:
        for handle in list(self._previews):
            self.stop_realplay(handle)
        self._session = None
        self._address = None

    def cleanup(self) -> None:
        self.logout()
