"""Publish a raw camera stream to RTSP via ffmpeg (no transcode).

The demuxer input format is vendor-supplied (`input_args`, e.g. `-f dhav` for Dahua's
DHAV container, `-f mpeg` for Hikvision's MPEG-PS). We feed the bytes exactly as the SDK
callback delivers them and let ffmpeg parse frame headers, detect the codec, and assign
timestamps. `-c copy` means no re-encoding (low CPU / low latency)."""
import logging
import os
import select
import subprocess
import time
from typing import Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
_WRITE_TIMEOUT = 5


class FfmpegPublisher:
    def __init__(self, rtsp_url: str, input_args):
        self.rtsp_url = rtsp_url
        self._input_args = list(input_args)
        self._proc: Optional[subprocess.Popen] = None

    def _args(self):
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            *self._input_args, "-i", "pipe:0",
            "-c", "copy",
            "-f", "rtsp", "-rtsp_transport", "tcp",
            self.rtsp_url,
        ]

    def start(self):
        self.stop()
        # The pipeline owns ffmpeg's lifetime, including on-demand shutdown.
        self._proc = subprocess.Popen(
            self._args(), stdin=subprocess.PIPE, bufsize=0,
            stdout=subprocess.DEVNULL, stderr=None, start_new_session=True,
        )
        if self._proc.stdin is not None:
            os.set_blocking(self._proc.stdin.fileno(), False)
        log.info("ffmpeg started for %s", urlsplit(self.rtsp_url).path)

    def write(self, data: bytes) -> bool:
        """Return False if ffmpeg closes its pipe or stops reading it."""
        proc = self._proc
        if not proc or proc.stdin is None:
            return False
        fd = proc.stdin.fileno()
        pending = memoryview(data)
        deadline = time.monotonic() + _WRITE_TIMEOUT
        poller = None
        while pending:
            try:
                pending = pending[os.write(fd, pending):]
            except BrokenPipeError:
                return False
            except BlockingIOError:
                if poller is None:
                    poller = select.poll()
                    poller.register(fd, select.POLLOUT)
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0 or not poller.poll(remaining_ms):
                    return False
        return True

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
