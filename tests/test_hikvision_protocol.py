import io
import struct

from relay.backends.base import get_backend
from relay.backends.hikvision import HikvisionClient
from relay.backends.hikvision_client import HikvisionPrivateClient
from relay.backends.hikvision_media import ProgramStream, RTPPacket, read_rtp
from relay.backends.hikvision_protocol import _command_checksum, _login_proof
from relay.config import Config


def test_hikvision_defaults_to_python_and_can_select_native_sdk():
    env = {'HOST': 'recorder', 'USERNAME': 'viewer', 'PASSWORD': 'example', 'VENDOR': 'hik'}
    default = Config.from_env(env)
    legacy = Config.from_env(env | {'HIKVISION_TRANSPORT': 'sdk'})
    default_backend = get_backend(default.vendor, hikvision_transport=default.hikvision_transport)
    legacy_backend = get_backend(legacy.vendor, hikvision_transport=legacy.hikvision_transport)
    assert default_backend.client_factory is HikvisionPrivateClient
    assert legacy_backend.client_factory is HikvisionClient


def test_login_proof():
    proof = _login_proof(b'viewer', b'example', b'0' * 32, b'f' * 64)
    assert proof.hex() == (
        '668a1f24f894e50f97100a3fd3099cf1'
        '00000000000000000000000000000000'
        '4fc9041f3943c8d6cb8b2537bc79e251'
    )


def test_preview_checksum():
    nonce = 0x12345678
    client_id = bytes.fromhex('020304050607')
    key = b'0123456789abcdef'
    assert _command_checksum(0x10203040, nonce, 0x30000, client_id, key, 123456) == 0x6B643975


def test_read_rtp_handles_split_tcp_reads_extension_and_padding():
    class Socket(io.BytesIO):
        def recv_into(self, dest):
            return self.readinto(dest[:3])

    payload = b'\x67\x64'
    rtp = (struct.pack('>BBHII', 0xB0, 0xE0, 13, 90_000, 123)
           + b'\xbf\xde\x00\x01\x12\x34\x00\x00' + payload + b'\x00\x02')
    conn = Socket(b'\x03\x00' + struct.pack('<H', len(rtp) + 4) + rtp)
    assert read_rtp(conn) == RTPPacket(96, 13, 90_000, True, payload)


def test_reassembles_h264_fragmentation_units_into_program_stream():
    mux = ProgramStream()
    assert mux.feed(RTPPacket(96, 65_534, 1_000, False, b'\x67\x64')) is None
    assert mux.feed(RTPPacket(96, 65_535, 1_000, False, b'\x7c\x85\x12\x34')) is None
    packet = mux.feed(RTPPacket(96, 0, 1_000, True, b'\x7c\x45\x56\x78'))
    assert packet.startswith(b'\x00\x00\x01\xba')
    assert b'\x1b\xe0\x00\x00' in packet  # H.264 stream map entry
    assert packet.endswith(b'\x00\x00\x00\x01\x67\x64\x00\x00\x00\x01\x65\x12\x34\x56\x78')


def test_drops_incomplete_video_frame_and_detects_hevc():
    mux = ProgramStream()
    assert mux.feed(RTPPacket(98, 1, 1_000, False, b'\x40\x01\x01')) is None
    assert mux.feed(RTPPacket(98, 3, 1_000, True, b'\x02\x01\x06')) is None
    packet = mux.feed(RTPPacket(98, 4, 4_000, True, b'\x42\x01\x01'))
    assert b'\x24\xe0\x00\x00' in packet  # HEVC stream map entry
    assert packet.endswith(b'\x00\x00\x00\x01\x42\x01\x01')
