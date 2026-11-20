"""Authenticated Hikvision private TCP login and preview framing."""

import hashlib
import hmac
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field

from tlslite.utils.keyfactory import generateRSAKey
from tlslite.utils.rijndael import Rijndael

_TIMEOUT = 12
_UINT32_MASK = (1 << 32) - 1
_LOGIN = 0x00010000
_LOGIN_VERSION = 0x05013D4B
_PREVIEW = 0x00030000
_STOP_PREVIEW = 0x00030006


class HikvisionProtocolError(RuntimeError):
    """The recorder rejected a request or returned an unsupported response."""


def enumerate_channels(chan_num, start_chan, ip_chan_num, start_dchan, high_dchan_num=0):
    analog = list(range(start_chan, start_chan + chan_num)) if chan_num else []
    ip_count = ip_chan_num + (high_dchan_num << 8)
    ip = list(range(start_dchan, start_dchan + ip_count)) if ip_count and start_dchan else []
    return analog + ip


def read_exact(conn: socket.socket, size: int) -> bytes:
    data = bytearray(size)
    received = 0
    while received < size:
        count = conn.recv_into(memoryview(data)[received:])
        if not count:
            raise EOFError("Hikvision connection closed")
        received += count
    return bytes(data)


def _read_reply(conn: socket.socket, *, max_size=4096) -> bytes:
    header = read_exact(conn, 4)
    size, = struct.unpack('>I', header)
    if not 12 <= size <= max_size:
        raise HikvisionProtocolError(f"invalid Hikvision reply length {size}")
    return header + read_exact(conn, size - len(header))


def _public_key_der(key) -> bytes:
    # The recorder expects PKCS#1 RSAPublicKey, i.e. a DER SEQUENCE of n and e.
    # For a 1024-bit modulus, the sign octet takes the integer above 127 bytes.
    modulus = b'\x02\x81\x81\x00' + key.n.to_bytes(128, 'big')
    exponent = key.e.to_bytes((key.e.bit_length() + 7) // 8, 'big')
    body = modulus + b'\x02' + bytes([len(exponent)]) + exponent
    return b'\x30\x81' + bytes([len(body)]) + body


def _login_request(ip: bytes, client_id: bytes, body: bytes) -> bytes:
    return (struct.pack('>6I', 36 + len(body), 0x5A000000, 0, _LOGIN, _LOGIN_VERSION, 0x00000101)
            + ip + client_id + b'\xef\x00' + body)


def _login_proof(username: bytes, password: bytes, challenge: bytes, salt: bytes) -> bytes:
    password_digest = hashlib.sha256(username + salt + password).hexdigest().encode('ascii')
    return (hmac.digest(challenge, username, 'md5') + bytes(16)
            + hmac.digest(challenge, password_digest, 'md5'))


def _command_checksum(user_id: int, nonce: int, command: int, client_id: bytes,
                      key: bytes, clock: int) -> int:
    client_bits = sum(byte & (user_id >> (5 * index)) for index, byte in enumerate(client_id))
    value = (client_bits + 2 * command + nonce) & _UINT32_MASK
    # The private protocol uses four Rijndael rounds for this command checksum.
    cipher = Rijndael(key)
    cipher.Ke = cipher.Ke[:5]
    a, b, c, d = struct.unpack('<4I', cipher.encrypt(struct.pack('<I', value) + bytes(12)))
    return ((a ^ b ^ c ^ d) + clock) & _UINT32_MASK


@dataclass(frozen=True)
class DeviceSession:
    channels: tuple[int, ...]
    _user_id: int = field(repr=False)
    _nonce: int = field(repr=False)
    _key: bytes = field(repr=False)
    _client_id: bytes = field(repr=False)
    _ip: bytes = field(repr=False)
    _clock: int = field(repr=False)
    _logged_in_at: float = field(repr=False)

    def _command(self, command: int, body: bytes = b'') -> bytes:
        clock = self._clock + int(time.monotonic() - self._logged_in_at)
        checksum = _command_checksum(self._user_id, self._nonce, command, self._client_id,
                                     self._key, clock)
        return (struct.pack('>4I', 32 + len(body), 0x63000000, checksum, command)
                + self._ip + struct.pack('>I', self._user_id) + self._client_id + bytes(2) + body)

    def start_preview(self, conn: socket.socket, channel: int, stream: int) -> bytes:
        # Mode 0x0401 requests the recorder's private TCP-framed preview packets.
        conn.sendall(self._command(_PREVIEW, struct.pack('>3I', channel, stream, 0x0401)))
        reply = _read_reply(conn)
        if len(reply) < 20 or reply[8:12] != b'\x00\x00\x00\x01':
            status = int.from_bytes(reply[8:12], 'big')
            raise HikvisionProtocolError(f"preview rejected with status {status}")
        header_size, = struct.unpack_from('>I', reply, 16)
        if len(reply) < 20 + header_size:
            raise HikvisionProtocolError("truncated Hikvision preview header")
        return reply[20:20 + header_size]

    def stop_preview(self, conn: socket.socket) -> None:
        conn.sendall(self._command(_STOP_PREVIEW))


def connect(host: str, port: int) -> socket.socket:
    return socket.create_connection((host, port), timeout=_TIMEOUT)


def login(host: str, port: int, username: str, password: str) -> DeviceSession:
    username_bytes = username.encode('utf-8')
    if not 1 <= len(username_bytes) <= 32 or '\x00' in username or '\x00' in password:
        raise ValueError("Hikvision authentication requires a username of 1–32 UTF-8 bytes and NUL-free credentials")
    key = generateRSAKey(1024, implementations=('python',))
    client_id = bytes([0x02]) + secrets.token_bytes(5)
    with connect(host, port) as conn:
        try:
            ip = socket.inet_pton(socket.AF_INET, conn.getsockname()[0])[::-1]
        except OSError as error:
            raise HikvisionProtocolError("legacy Hikvision private login requires an IPv4 connection") from error
        conn.sendall(_login_request(ip, client_id, username_bytes.ljust(48, b'\0') + _public_key_der(key)))
        reply = _read_reply(conn)
        if len(reply) != 208 or reply[8:12] != b'\x01\x00\x00\x63':
            status = int.from_bytes(reply[8:12], 'big')
            raise HikvisionProtocolError(f"unsupported Hikvision login challenge (status {status})")
        clock, = struct.unpack_from('>I', reply, 4)
        clock_time = time.monotonic()
        challenge = key.decrypt(reply[16:144])
        if challenge is None or len(challenge.rstrip(b'\0')) != 32:
            raise HikvisionProtocolError("invalid Hikvision RSA login challenge")
        challenge = bytes(challenge).rstrip(b'\0')
        proof = _login_proof(username_bytes, password.encode('utf-8'), challenge, reply[144:208])
        conn.sendall(_login_request(ip, client_id, proof))
        reply = _read_reply(conn)
        if len(reply) < 87 or reply[8:12] != b'\x00\x00\x00\x01':
            status = int.from_bytes(reply[8:12], 'big')
            raise HikvisionProtocolError(f"Hikvision login rejected with status {status}")
        nonce, = struct.unpack_from('>I', reply, 4)
        user_id, = struct.unpack_from('>I', reply, 16)
        channels = enumerate_channels(reply[69], reply[70], reply[76], reply[86])
        return DeviceSession(tuple(channels), user_id, nonce, challenge[:16], client_id, ip, clock, clock_time)
