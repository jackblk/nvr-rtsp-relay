"""Publish a raw camera stream to RTSP via ffmpeg (no transcode).

The demuxer input format is vendor-supplied (`input_args`, e.g. `-f dhav` for Dahua's
DHAV container, `-f mpeg` for Hikvision's MPEG-PS). We feed the bytes exactly as the SDK
callback delivers them and let ffmpeg parse frame headers, detect the codec, and assign
timestamps. `-c copy` means no re-encoding (low CPU / low latency)."""
import logging
import subprocess
import threading
from typing import Optional

log = logging.getLogger(__name__)


class FfmpegPublisher:
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

    def start(self):
        with self._lock:
            # start_new_session: run ffmpeg in its own process group so a
            # SIGINT/SIGTERM sent to the relay process (e.g. MediaMTX tearing down
            # an on-demand stream) is NOT propagated to ffmpeg. We stop it
            # explicitly via stop(); this prevents a shutdown-time restart race.
            self._proc = subprocess.Popen(
                self._args(), stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=None,
                start_new_session=True,
            )
        log.info("ffmpeg started (dhav demuxer) -> %s", self.rtsp_url)

    def write(self, data: bytes) -> bool:
        """Write to ffmpeg stdin. Returns False if the pipe is broken."""
        proc = self._proc
        if not proc or proc.stdin is None:
            return False
        try:
            proc.stdin.write(data)
            return True
        except (BrokenPipeError, ValueError):
            return False

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
        with self._lock:
            if self._proc:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.close()
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()
                self._proc = None
        log.info("ffmpeg stopped")
