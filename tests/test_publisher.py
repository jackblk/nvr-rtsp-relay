import sys

import relay  # noqa: F401  (path bootstrap)
from relay.publisher import FfmpegPublisher


def test_ffmpeg_started_in_own_session(monkeypatch):
    """ffmpeg must run in its own session so a SIGINT/SIGTERM to the relay
    process group doesn't kill it (we stop it explicitly instead)."""
    captured = {}

    class FakeProc:
        stdin = None

        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def poll(self):
            return None

    monkeypatch.setattr("relay.publisher.subprocess.Popen", FakeProc)
    pub = FfmpegPublisher("rtsp://x/y", ("-f", "dhav"))
    pub.start()
    assert captured.get("start_new_session") is True


def test_input_args_are_placed_before_input():
    pub = FfmpegPublisher("rtsp://x/y", ("-f", "mpeg"))
    args = pub._args()
    assert args[:2] == ["ffmpeg", "-hide_banner"]
    assert "-f" in args and args[args.index("-f") + 1] == "mpeg"
    assert args.index("-f") < args.index("pipe:0")     # demuxer flag precedes -i input
    assert args[-1] == "rtsp://x/y"


def test_stalled_pipe_times_out_and_old_process_is_reaped_on_restart(monkeypatch):
    pub = FfmpegPublisher("rtsp://x/y", ("-f", "mpeg"))
    # A child that keeps its input open without reading simulates stalled ffmpeg.
    monkeypatch.setattr(pub, "_args", lambda: [sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setattr("relay.publisher._WRITE_TIMEOUT", 0.1)
    pub.start()
    try:
        stalled = pub._proc
        assert pub.write(b"x" * (4 * 1024 * 1024)) is False
        assert stalled.poll() is None
        pub.start()
        assert stalled.returncode is not None
        assert pub.is_alive()
    finally:
        pub.stop()
