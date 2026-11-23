"""Forward raw camera data from a bounded callback queue to ffmpeg."""
import logging
import queue
import threading
import time
from urllib.parse import urlsplit

from relay.publisher import FfmpegPublisher

log = logging.getLogger(__name__)
_IDLE_TIMEOUT = 30
_RETRY_DELAY = 2


class StreamPipeline:
    def __init__(self, rtsp_url: str, input_args=("-f", "dhav"), max_queue: int = 256, publisher=None):
        self.rtsp_url = rtsp_url
        self._path = urlsplit(rtsp_url).path
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=max_queue)
        self._publisher = publisher or FfmpegPublisher(rtsp_url, input_args)
        self._worker = None
        self._stop = threading.Event()
        self._dropped = 0
        self._last_write_at = None
        self._retry_at = 0.0

    # called from the SDK callback thread
    def on_raw(self, data: bytes):
        try:
            self._q.put_nowait(data)
        except queue.Full:
            try:
                self._q.get_nowait()          # drop oldest
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(data)
            except queue.Full:
                pass
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning("%s: queue full; dropped %d chunks", self._path, self._dropped)

    def start(self):
        self._worker = threading.Thread(target=self._run, name="pipeline", daemon=True)
        self._worker.start()

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    data = self._q.get(timeout=0.5)
                except queue.Empty:
                    if self._last_write_at is not None and time.monotonic() - self._last_write_at >= _IDLE_TIMEOUT:
                        log.warning("%s: no camera data for %ss; resetting ffmpeg", self._path, _IDLE_TIMEOUT)
                        self._publisher.stop()
                        self._last_write_at = None
                    continue
                self._forward(data)
        finally:
            self._publisher.stop()

    def _forward(self, data):
        if self._stop.is_set():
            return
        if not self._publisher.is_alive():
            if self._last_write_at is not None:
                log.warning("%s: ffmpeg exited; retrying", self._path)
                self._last_write_at = None
            if not self._start_publisher():
                return
        if self._publisher.write(data):
            self._last_write_at = time.monotonic()
            return

        log.warning("%s: ffmpeg input pipe broke or stalled; retrying", self._path)
        self._publisher.stop()
        self._last_write_at = None
        if self._start_publisher():
            if self._publisher.write(data):
                self._last_write_at = time.monotonic()
            else:
                self._publisher.stop()

    def _start_publisher(self):
        now = time.monotonic()
        if self._stop.is_set() or now < self._retry_at:
            return False
        self._retry_at = now + _RETRY_DELAY
        try:
            self._publisher.start()
        except OSError as error:
            log.warning("%s: failed to start ffmpeg; retrying: %s", self._path, error)
            return False
        return True

    def stop(self):
        self._stop.set()
        if self._worker:
            self._worker.join()
