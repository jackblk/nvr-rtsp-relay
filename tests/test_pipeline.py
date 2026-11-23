import threading

import relay  # noqa: F401  (path bootstrap)
from relay.pipeline import StreamPipeline


class FakePublisher:
    def __init__(self, alive=True, write_ok=True):
        self._alive = alive
        self._write_ok = write_ok
        self.start_calls = 0
        self.writes = []

    def start(self):
        self.start_calls += 1
        self._alive = True

    def is_alive(self):
        return self._alive

    def write(self, data):
        self.writes.append(data)
        return self._write_ok

    def stop(self):
        self._alive = False


def _pipeline(pub):
    return StreamPipeline("rtsp://x/y", publisher=pub)


def test_forward_restarts_dead_publisher_when_running():
    pub = FakePublisher(alive=False)
    p = _pipeline(pub)
    p._forward(b"data")
    assert pub.start_calls == 1            # dead -> restarted
    assert pub.writes == [b"data"]


def test_forward_does_not_restart_when_stopping():
    pub = FakePublisher(alive=False)
    p = _pipeline(pub)
    p._stop.set()                          # shutting down
    p._forward(b"data")
    assert pub.start_calls == 0            # must stay dead, no restart
    assert pub.writes == []


def test_forward_restarts_on_broken_pipe():
    pub = FakePublisher(alive=True, write_ok=False)
    p = _pipeline(pub)
    p._forward(b"data")
    assert pub.start_calls == 1            # broken pipe -> restart + retry write


def test_pipeline_resets_publisher_after_camera_goes_quiet(monkeypatch):
    idle = threading.Event()
    resumed = threading.Event()

    class Publisher(FakePublisher):
        def write(self, data):
            if data == b"resumed":
                resumed.set()
            return super().write(data)

        def stop(self):
            super().stop()
            idle.set()

    monkeypatch.setattr("relay.pipeline._IDLE_TIMEOUT", 0)
    monkeypatch.setattr("relay.pipeline._RETRY_DELAY", 0)
    pub = Publisher(alive=False)
    pipeline = _pipeline(pub)
    pipeline.start()
    try:
        pipeline.on_raw(b"initial")
        assert idle.wait(2)
        pipeline.on_raw(b"resumed")
        assert resumed.wait(2)
        assert pub.start_calls == 2
    finally:
        pipeline.stop()
