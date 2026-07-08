"""TS transport-stream and ID3 tag parsing for rotation metadata extraction."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace

CARDINAL_ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True)
class ID3RotationSample:
    """One timed-ID3 rotation sample from an HLS segment."""

    segment_index: int
    raw_rotation: float
    rotation: int | None = None
    program_date_time: str | None = None
    ntp: float | None = None
    width: int | None = None
    height: int | None = None
    segment_url: str | None = None

    def to_json_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value is not None}


def _synchsafe_to_int(value: bytes) -> int:
    return (
        ((value[0] & 0x7F) << 21)
        | ((value[1] & 0x7F) << 14)
        | ((value[2] & 0x7F) << 7)
        | (value[3] & 0x7F)
    )


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _strip_pes_header(payload: bytes) -> bytes:
    if len(payload) >= 9 and payload[:3] == b"\x00\x00\x01":
        pes_header_length = payload[8]
        start = 9 + pes_header_length
        return payload[start:]
    return payload


def _extract_id3_tag(data: bytes) -> bytes | None:
    start = data.find(b"ID3\x04\x00")
    if start < 0 or start + 10 > len(data):
        return None
    size = _synchsafe_to_int(data[start + 6:start + 10])
    end = start + 10 + size
    if end > len(data):
        return None
    return data[start:end]


def _parse_text_frames(tag: bytes) -> dict[str, str]:
    size = _synchsafe_to_int(tag[6:10])
    body = tag[10:10 + size]
    frames = {}
    pos = 0
    while pos + 10 <= len(body):
        frame_id = body[pos:pos + 4].decode("latin1")
        frame_size = int.from_bytes(body[pos + 4:pos + 8], "big")
        if not frame_id.strip("\x00") or frame_size <= 0:
            break
        payload = body[pos + 10:pos + 10 + frame_size]
        if frame_id.startswith("T") and payload[:1] in (b"\x00", b"\x03"):
            text = payload[1:].rstrip(b"\x00")
            if frame_id == "TXXX":
                description, _, value = text.partition(b"\x00")
                key = f"TXXX:{description.decode('utf-8', 'replace')}"
                frames[key] = value.decode("utf-8", "replace")
            else:
                frames[frame_id] = text.decode("utf-8", "replace")
        pos += 10 + frame_size
    return frames


def _parse_json_metadata(value: str | None) -> dict:
    if not value:
        return {}
    json_text = _first_json_object(value)
    if not json_text:
        return {}
    return json.loads(json_text)


def _first_json_object(value: str) -> str | None:
    start = value.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(value[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return value[start:index + 1]
    return None


def _extract_id3_from_ts(data: bytes) -> bytes | None:
    if len(data) < 188 or data[0] != 0x47:
        return None

    buffers: dict[int, bytearray] = {}
    for offset in range(0, len(data) - 187, 188):
        packet = data[offset:offset + 188]
        if packet[0] != 0x47:
            continue

        payload_start = bool(packet[1] & 0x40)
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        adaptation_control = (packet[3] >> 4) & 0x03
        if adaptation_control not in (1, 3):
            continue

        payload_offset = 4
        if adaptation_control == 3:
            payload_offset += 1 + packet[4]
        if payload_offset >= len(packet):
            continue

        payload = packet[payload_offset:]
        if payload_start:
            buffers[pid] = bytearray(_strip_pes_header(payload))
        else:
            buffers.setdefault(pid, bytearray()).extend(payload)

        tag = _extract_id3_tag(bytes(buffers[pid]))
        if tag:
            return tag

    for payload in buffers.values():
        tag = _extract_id3_tag(bytes(payload))
        if tag:
            return tag
    return None




def _angle_distance(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def quantize_rotation(raw_rotation: float) -> int:
    """Map a continuous sensor angle to the nearest cardinal rotation."""
    normalized = raw_rotation % 360
    return min(CARDINAL_ROTATIONS, key=lambda angle: _angle_distance(normalized, angle))


def parse_id3_rotation_sample(
    data: bytes,
    segment_index: int,
    program_date_time: str | None = None,
    segment_url: str | None = None,
) -> ID3RotationSample | None:
    """Parse a raw ID3 tag and return its rotation sample."""
    tag = _extract_id3_tag(data)
    if not tag:
        return None

    frames = _parse_text_frames(tag)
    metadata = _parse_json_metadata(frames.get("TXXX:JSONMetadata"))
    raw_rotation = _float_or_none(metadata.get("rotation") if metadata else None)
    if raw_rotation is None:
        raw_rotation = _float_or_none(frames.get("TKEY"))
    if raw_rotation is None:
        return None

    width = _int_or_none((metadata or {}).get("width")) or _int_or_none(frames.get("TMED"))
    height = _int_or_none((metadata or {}).get("height")) or _int_or_none(frames.get("TMOO"))
    sample = ID3RotationSample(
        segment_index=segment_index,
        raw_rotation=raw_rotation,
        program_date_time=program_date_time,
        ntp=_float_or_none((metadata or {}).get("ntp")),
        width=width,
        height=height,
        segment_url=segment_url,
    )
    return replace(sample, rotation=quantize_rotation(raw_rotation))
