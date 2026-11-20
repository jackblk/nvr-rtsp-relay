"""Convert Hikvision's TCP-framed RTP preview to MPEG program stream."""

import struct
from dataclasses import dataclass

from relay.backends.hikvision_protocol import HikvisionProtocolError, read_exact

_START_CODE = b'\x00\x00\x00\x01'
_MAX_ACCESS_UNIT = 8 * 1024 * 1024
_VIDEO_TYPES = {'h264': 0x1B, 'hevc': 0x24}
_AUDIO_TYPES = {0: 0x91, 8: 0x90}  # G.711 µ-law and A-law
_PTS_MASK = (1 << 33) - 1


@dataclass(frozen=True)
class RTPPacket:
    payload_type: int
    sequence: int
    timestamp: int
    marker: bool
    payload: bytes


def read_rtp(conn) -> RTPPacket:
    prefix = read_exact(conn, 4)
    size = int.from_bytes(prefix[2:], 'little')
    if prefix[:2] != b'\x03\x00' or size < 16:
        raise HikvisionProtocolError('unexpected Hikvision preview frame header')
    data = read_exact(conn, size - len(prefix))
    flags, marker_type, sequence, timestamp = struct.unpack_from('>BBHI', data)
    start = 12 + 4 * (flags & 0x0F)  # CSRC list
    if flags & 0x10:
        if start + 4 > len(data):
            raise HikvisionProtocolError('truncated RTP extension header')
        start += 4 + 4 * int.from_bytes(data[start + 2:start + 4], 'big')
    padding = data[-1] if flags & 0x20 else 0
    end = len(data) - padding
    if flags >> 6 != 2 or start > end or flags & 0x20 and not padding:
        raise HikvisionProtocolError('invalid Hikvision RTP packet')
    return RTPPacket(marker_type & 0x7F, sequence, timestamp, bool(marker_type & 0x80), data[start:end])


def _detect_video(payload: bytes) -> str | None:
    # Learn the codec from parameter sets. Dynamic RTP payload numbers carry no codec declaration.
    if payload[0] & 0x1F == 24 and len(payload) >= 4:
        return 'h264' if payload[3] & 0x1F in (7, 8) else None
    if payload[0] == 0x60 and len(payload) >= 6 and payload[4] in (0x40, 0x42, 0x44):
        return 'hevc'
    if payload[0] & 0x1F in (7, 8):
        return 'h264'
    if len(payload) >= 2 and payload[0] in (0x40, 0x42, 0x44) and payload[1] & 7:
        return 'hevc'
    return None


class _VideoAccessUnit:
    def __init__(self, codec: str):
        self._codec = codec
        self._timestamp = None
        self._sequence = None
        self._data = bytearray()
        self._fragment = False
        self._damaged = False

    def _add(self, nal: bytes) -> None:
        self._data.extend(_START_CODE)
        self._data.extend(nal)

    def _aggregate(self, payload: bytes, offset: int) -> None:
        while offset + 2 <= len(payload):
            size = int.from_bytes(payload[offset:offset + 2], 'big')
            offset += 2
            if not size or offset + size > len(payload):
                self._damaged = True
                return
            self._add(payload[offset:offset + size])
            offset += size
        self._damaged |= offset != len(payload)

    def _fragmented(self, payload: bytes, header_size: int, nal_header: bytes) -> None:
        fragment_flags = payload[header_size]
        if fragment_flags & 0x80:
            self._damaged |= self._fragment
            self._add(nal_header)
            self._fragment = True
        if not self._fragment:
            self._damaged = True
        else:
            self._data.extend(payload[header_size + 1:])
        if fragment_flags & 0x40:
            self._fragment = False

    def feed(self, packet: RTPPacket) -> bytes | None:
        if packet.timestamp != self._timestamp:
            self._timestamp = packet.timestamp
            self._data.clear()
            self._fragment = self._damaged = False
        elif self._sequence is not None and packet.sequence != (self._sequence + 1) & 0xFFFF:
            self._damaged = True
        self._sequence = packet.sequence

        payload = packet.payload
        if not self._damaged and self._codec == 'h264':
            nal_type = payload[0] & 0x1F
            if nal_type == 24:  # STAP-A
                self._aggregate(payload, 1)
            elif nal_type == 28 and len(payload) >= 3:  # FU-A
                header = (payload[0] & 0xE0) | (payload[1] & 0x1F)
                self._fragmented(payload, 1, bytes([header]))
            elif 1 <= nal_type <= 23:
                self._add(payload)
        elif not self._damaged and len(payload) >= 2:
            nal_type = (payload[0] >> 1) & 0x3F
            if nal_type == 48:  # aggregation packet, without DONL
                self._aggregate(payload, 2)
            elif nal_type == 49 and len(payload) >= 4:  # fragmentation unit
                header = (payload[0] & 0x81) | ((payload[2] & 0x3F) << 1)
                self._fragmented(payload, 2, bytes([header, payload[1]]))
            elif nal_type < 48:
                self._add(payload)

        if len(self._data) > _MAX_ACCESS_UNIT:
            self._data.clear()
            self._damaged = True
        if not packet.marker:
            return None
        frame = bytes(self._data) if self._data and not (self._damaged or self._fragment) else None
        self._data.clear()
        self._fragment = self._damaged = False
        return frame


def _pack_bits(fields: tuple[tuple[int, int], ...], size: int) -> bytes:
    value = 0
    for width, field in fields:
        value = (value << width) | field
    return value.to_bytes(size, 'big')


def _pack_header(pts: int) -> bytes:
    scr = pts & _PTS_MASK
    return b'\x00\x00\x01\xba' + _pack_bits((
        (2, 1), (3, scr >> 30), (1, 1), (15, (scr >> 15) & 0x7FFF),
        (1, 1), (15, scr & 0x7FFF), (1, 1), (9, 0), (1, 1),
        (22, 25_200), (2, 3), (5, 31), (3, 0),
    ), 10)


def _program_map(video_type: int, audio_type: int | None) -> bytes:
    streams = bytes([video_type, 0xE0, 0, 0])
    if audio_type is not None:
        streams += bytes([audio_type, 0xC0, 0, 0])
    body = b'\xe0\xff\x00\x00' + struct.pack('>H', len(streams)) + streams
    packet = b'\x00\x00\x01\xbc' + struct.pack('>H', len(body) + 4) + body
    crc = 0xFFFFFFFF
    for byte in packet:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return packet + struct.pack('>I', crc)


def _pes(stream_id: int, pts: int, data: bytes) -> bytes:
    pts &= _PTS_MASK
    timestamp = _pack_bits((
        (4, 2), (3, pts >> 30), (1, 1), (15, (pts >> 15) & 0x7FFF),
        (1, 1), (15, pts & 0x7FFF), (1, 1),
    ), 5)
    prefix = b'\x00\x00\x01' + bytes([stream_id])
    output = bytearray()
    for offset in range(0, len(data), 65_527):
        chunk = data[offset:offset + 65_527]
        header = b'\x80\x00\x00' if offset else b'\x80\x80\x05' + timestamp
        output.extend(prefix + struct.pack('>H', len(header) + len(chunk)) + header)
        output.extend(chunk)
    return bytes(output)


class _RTPClock:
    def __init__(self, scale: int, start_pts: int):
        self._scale = scale
        self._start_pts = start_pts
        self._previous = None
        self._ticks = 0

    def pts(self, timestamp: int) -> int:
        if self._previous is not None:
            delta = ((timestamp - self._previous + (1 << 31)) & 0xFFFFFFFF) - (1 << 31)
            self._ticks += delta
        self._previous = timestamp
        return self._start_pts + self._ticks * 90_000 // self._scale


class ProgramStream:
    """Depacketize one preview and mux its encoded frames without transcoding."""
    def __init__(self, start_pts: int = 90_000):
        self._video_type = None
        self._video_payload_type = None
        self._video = None
        self._audio_type = None
        self._map = b''
        self._video_clock = _RTPClock(90_000, start_pts)
        self._audio_clock = _RTPClock(8_000, start_pts)

    def feed(self, packet: RTPPacket) -> bytes | None:
        if not packet.payload or packet.payload_type == 112:  # recorder metadata
            return None
        if packet.payload_type in _AUDIO_TYPES:
            if self._video_type is None:
                return None
            audio_type = _AUDIO_TYPES[packet.payload_type]
            if self._audio_type != audio_type:
                self._audio_type = audio_type
                self._map = _program_map(self._video_type, audio_type)
            pts = self._audio_clock.pts(packet.timestamp)
            return _pack_header(pts) + self._map + _pes(0xC0, pts, packet.payload)
        if self._video is None:
            codec = _detect_video(packet.payload)
            if codec is None:
                return None
            self._video = _VideoAccessUnit(codec)
            self._video_type = _VIDEO_TYPES[codec]
            self._video_payload_type = packet.payload_type
            self._map = _program_map(self._video_type, self._audio_type)
        if packet.payload_type != self._video_payload_type:
            return None
        frame = self._video.feed(packet)
        if frame is None:
            return None
        pts = self._video_clock.pts(packet.timestamp)
        return _pack_header(pts) + self._map + _pes(0xE0, pts, frame)
