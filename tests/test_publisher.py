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
