import pytest
import relay  # noqa: F401
from relay.backends.base import get_backend, ProbeResult


def test_get_backend_unknown_vendor_raises():
    with pytest.raises(ValueError) as e:
        get_backend("acme")
    assert "acme" in str(e.value)


def test_get_backend_normalizes_case_and_whitespace():
    with pytest.raises(ValueError):
        get_backend("  DAHUAX ")   # still unknown, but must not crash on casing


def test_probe_result_shape():
    r = ProbeResult(3, "main", True, "h264", 960, 576)
    assert (r.channel, r.stream, r.ok, r.codec, r.width, r.height) == (3, "main", True, "h264", 960, 576)


def test_dahua_backend_metadata():
    b = get_backend("dahua")
    assert b.vendor == "dahua"
    assert b.stream_types == {"main": 0, "sub": 3, "sub2": 4}
    assert b.default_port == 37777
    assert b.ffmpeg_input_args == ("-f", "dhav")


class _FakeDevInfoClient:
    channel_count = 4


def test_dahua_list_channels_is_zero_based_range():
    b = get_backend("dahua")
    assert b.list_channels(_FakeDevInfoClient()) == [0, 1, 2, 3]
