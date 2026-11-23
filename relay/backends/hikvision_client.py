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

_RETRY_DELAY = 2
_LOGIN_RETRY_DELAY = 10
_MEDIA_TIMEOUT = 30


class _RecorderSession:
    """Share one login across previews and refresh it when a reopen fails."""

    def __init__(self, host: str, port: int, username: str, password: str):
        self.address = (host, port)
        self._credentials = (username, password)
        self.current = login(*self.address, *self._credentials)
        self._lock = threading.Lock()
        self._retry_at = 0.0

    def refresh(self, failed_session: DeviceSession, stop: threading.Event) -> None:
        with self._lock:
            if stop.is_set() or self.current is not failed_session or time.monotonic() < self._retry_at:
                return
            try:
                self.current = login(*self.address, *self._credentials)
            except (OSError, EOFError, HikvisionProtocolError) as error:
                log.warning('Hikvision recorder login refresh failed: %s', error)
            else:
                log.info('Refreshed Hikvision recorder login')
            finally:
                self._retry_at = time.monotonic() + _LOGIN_RETRY_DELAY


class _Preview:
    def __init__(self, recorder: _RecorderSession, channel: int,
                 stream: int, on_raw: Callable[[bytes], None]):
        self._recorder = recorder
        self._channel = channel
        self._stream = stream
        self._on_raw = on_raw
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connection = None
        self._preview_session = None
        self._received_media = False
        self._reported_failure = False
        self._started_at = time.monotonic()
        self._worker = threading.Thread(target=self._run, name=f'hikvision-ch{channel}-{stream}', daemon=True)

    def start(self) -> None:
        self._worker.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with connect(*self._recorder.address) as conn:
                    self._receive(conn)
            except (OSError, EOFError, HikvisionProtocolError) as error:
                if self._stop.is_set():
                    return
                if self._received_media and not self._reported_failure:
                    log.warning('Hikvision ch%s type %s preview ended; retrying: %s',
                                self._channel, self._stream, error)
                    self._reported_failure = True
                else:
                    log.debug('Hikvision ch%s type %s preview retry failed: %s',
                              self._channel, self._stream, error)
            except Exception:
                log.exception('Hikvision ch%s type %s preview worker failed', self._channel, self._stream)
                return
            finally:
                with self._lock:
                    self._connection = None
                    self._preview_session = None
            self._stop.wait(_RETRY_DELAY)

    def _receive(self, conn: socket.socket) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._connection = conn
        session = self._recorder.current
        try:
            header = session.start_preview(conn, self._channel, self._stream)
        except (OSError, EOFError, HikvisionProtocolError):
            if self._received_media:
                self._recorder.refresh(session, self._stop)
            raise
        if not header.startswith(b'IMKH'):
            raise HikvisionProtocolError('unsupported Hikvision preview system header')
        with self._lock:
            if self._stop.is_set():
                return
            self._preview_session = session
        # Reconnecting gives us a new RTP clock while ffmpeg keeps the same input pipe.
        mux = ProgramStream(start_pts=90_000 + int((time.monotonic() - self._started_at) * 90_000))
        self._on_raw(header)
        media_deadline = time.monotonic() + _MEDIA_TIMEOUT
        while not self._stop.is_set():
            packet = mux.feed(read_rtp(conn))
            if self._stop.is_set():
                return
            now = time.monotonic()
            if packet is None:
                if now >= media_deadline:
                    raise HikvisionProtocolError('preview stopped yielding media')
                continue
            self._on_raw(packet)
            media_deadline = now + _MEDIA_TIMEOUT
            self._received_media = True
            if self._reported_failure:
                log.info('Hikvision ch%s type %s media resumed', self._channel, self._stream)
                self._reported_failure = False

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            conn = self._connection
            if conn is not None:
                if self._preview_session is not None:
                    try:
                        conn.settimeout(0.25)
                        self._preview_session.stop_preview(conn)
                    except OSError:
                        pass
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self._worker.join(timeout=1)


class HikvisionPrivateClient:
    def __init__(self):
        self._recorder = None
        self._previews: dict[int, _Preview] = {}
        self._handles = itertools.count()

    def init(self) -> None:
        """The Python transport has no process-wide initialization."""

    def login(self, host: str, port: int, username: str, password: str) -> int:
        self.logout()
        self._recorder = _RecorderSession(host, port, username, password)
        log.info('Logged in over Hikvision private TCP (%d channels)', self.channel_count)
        return 0

    @property
    def channel_count(self) -> int:
        return len(self.channels())

    def channels(self) -> list[int]:
        return list(self._recorder.current.channels) if self._recorder else []

    def start_realplay(self, channel: int, play_type: int, on_raw: Callable[[bytes], None]) -> int:
        if self._recorder is None:
            raise RuntimeError('Hikvision client is logged out')
        if not 1 <= channel <= 0xFFFFFFFF or play_type not in (0, 1):
            raise ValueError('Hikvision preview requires a positive channel and a main or substream')
        handle = next(self._handles)
        preview = _Preview(self._recorder, channel, play_type, on_raw)
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
        self._recorder = None

    def cleanup(self) -> None:
        self.logout()
