import relay  # noqa: F401
from relay.backends.hikvision import enumerate_channels, BACKEND


def test_enumerate_analog_plus_ip():
    # handoff device: 16 analog from 1, 18 IP from 33
    assert enumerate_channels(16, 1, 18, 33) == list(range(1, 17)) + list(range(33, 51))


def test_enumerate_analog_only():
    assert enumerate_channels(4, 1, 0, 0) == [1, 2, 3, 4]


def test_enumerate_ip_only():
    assert enumerate_channels(0, 1, 4, 33) == [33, 34, 35, 36]


def test_enumerate_high_ip_count():
    # high byte extends the IP channel count beyond 255
    assert enumerate_channels(0, 1, 0, 1, high_dchan_num=1) == list(range(1, 257))


def test_hikvision_backend_metadata():
    assert BACKEND.vendor == "hikvision"
    assert BACKEND.stream_types == {"main": 0, "sub": 1}
    assert BACKEND.default_port == 8000
    assert BACKEND.ffmpeg_input_args[0] == "-fflags"
    assert "mpeg" in BACKEND.ffmpeg_input_args
