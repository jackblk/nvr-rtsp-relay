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
