"""Extract Periscope/X HLS timed-ID3 rotation metadata."""

from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable

from .config import DEFAULT_BROWSER, extract_broadcast_id, normalize_broadcast_url

CARDINAL_ROTATIONS = (0, 90, 180, 270)
DEFAULT_HYSTERESIS_DEGREES = 10.0
SEGMENT_RANGE_BYTES = 8191


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


def quantize_rotation(raw_rotation: float) -> int:
    """Map a continuous sensor angle to the nearest cardinal rotation."""
    normalized = raw_rotation % 360
    return min(CARDINAL_ROTATIONS, key=lambda angle: _angle_distance(normalized, angle))


def quantize_rotation_series(
    samples: Iterable[ID3RotationSample],
    hysteresis_degrees: float = DEFAULT_HYSTERESIS_DEGREES,
) -> list[ID3RotationSample]:
    """Quantize samples, switching only after a clear boundary crossing."""
    quantized: list[ID3RotationSample] = []
    current: int | None = None
    switch_distance = 45.0 - hysteresis_degrees

    for sample in samples:
        nearest = quantize_rotation(sample.raw_rotation)
        if current is None:
            current = nearest
        elif nearest != current and _angle_distance(sample.raw_rotation, nearest) <= switch_distance:
            current = nearest
        quantized.append(replace(sample, rotation=current))

    return quantized


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


def extract_rotation_sidecar(
    url: str,
    output_dir: Path,
    browser: str = DEFAULT_BROWSER,
    hysteresis_degrees: float = DEFAULT_HYSTERESIS_DEGREES,
) -> Path:
    """Resolve a broadcast HLS URL and write quantized rotation samples as JSONL."""
    normalized = normalize_broadcast_url(url)
    if not normalized:
        raise ValueError(f"Not a valid broadcast URL: {url}")

    broadcast_id = extract_broadcast_id(normalized)
    if not broadcast_id:
        raise ValueError(f"Could not extract broadcast ID: {url}")

    playlist_url = _resolve_hls_playlist_url(normalized, browser)
    playlist_text = _http_text(playlist_url)
    playlist_text, playlist_url = _ensure_media_playlist(playlist_text, playlist_url)
    playlist = _parse_media_playlist(playlist_text, playlist_url)
    samples = []
    for segment in playlist:
        data = _http_bytes(segment["url"], byte_range=SEGMENT_RANGE_BYTES)
        tag = _extract_id3_from_ts(data) or data
        sample = parse_id3_rotation_sample(
            tag,
            segment_index=segment["index"],
            program_date_time=segment.get("program_date_time"),
            segment_url=segment["url"],
        )
        if sample:
            samples.append(sample)

    quantized = quantize_rotation_series(samples, hysteresis_degrees=hysteresis_degrees)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = output_dir / f"{broadcast_id}.rotation.jsonl"
    with sidecar_path.open("w", encoding="utf-8") as fh:
        for sample in quantized:
            fh.write(json.dumps(sample.to_json_dict(), separators=(",", ":")) + "\n")
    return sidecar_path


def _resolve_hls_playlist_url(url: str, browser: str) -> str:
    cmd = [
        "yt-dlp",
        "--cookies-from-browser",
        browser,
        "--no-warnings",
        "--print",
        "urls",
        url,
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not urls:
        raise RuntimeError("yt-dlp did not return an HLS URL")
    return urls[-1]


def _parse_media_playlist(playlist_text: str, playlist_url: str) -> list[dict]:
    segments = []
    program_date_time: str | None = None
    for line in playlist_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            program_date_time = line.split(":", 1)[1]
        elif not line.startswith("#"):
            segments.append({
                "index": len(segments),
                "url": urllib.parse.urljoin(playlist_url, line),
                "program_date_time": program_date_time,
            })
            program_date_time = None
    return segments


def _ensure_media_playlist(playlist_text: str, playlist_url: str) -> tuple[str, str]:
    if "#EXT-X-STREAM-INF" not in playlist_text:
        return playlist_text, playlist_url

    expect_variant = False
    for line in playlist_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            expect_variant = True
        elif expect_variant and line and not line.startswith("#"):
            variant_url = urllib.parse.urljoin(playlist_url, line)
            return _http_text(variant_url), variant_url
    return playlist_text, playlist_url


def _http_text(url: str) -> str:
    return _http_bytes(url).decode("utf-8")


def _http_bytes(url: str, byte_range: int | None = None) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://x.com/",
    }
    if byte_range is not None:
        headers["Range"] = f"bytes=0-{byte_range}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


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


def _angle_distance(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)
@dataclass(frozen=True)
class RotationInterval:
    start_sec: float
    end_sec: float
    rotation: int


@dataclass(frozen=True)
class RotationIntervals:
    intervals: list[RotationInterval] = field(default_factory=list)
    canvas_width: int = 0
    canvas_height: int = 0

    @property
    def all_zero(self) -> bool:
        return all(iv.rotation == 0 for iv in self.intervals)

    @property
    def uniform_rotation(self) -> int | None:
        unique = {iv.rotation for iv in self.intervals}
        return unique.pop() if len(unique) == 1 else None
def load_sidecar(path: str | Path) -> list[ID3RotationSample]:
    samples: list[ID3RotationSample] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            samples.append(ID3RotationSample(
                segment_index=data.get("segment_index", 0),
                raw_rotation=float(data.get("raw_rotation", 0)),
                rotation=data.get("rotation"),
                program_date_time=data.get("program_date_time"),
                ntp=data.get("ntp"),
                width=data.get("width"),
                height=data.get("height"),
                segment_url=data.get("segment_url"),
            ))
    return samples
def rotation_timeline(
    samples: list[ID3RotationSample],
    video_duration: float | None = None,
) -> RotationIntervals:
    if not samples:
        return RotationIntervals()
    valid = [s for s in samples if s.rotation is not None and s.ntp is not None]
    if not valid:
        return RotationIntervals()
    first_ntp = valid[0].ntp
    raw: list[RotationInterval] = []
    for i, sample in enumerate(valid):
        start = sample.ntp - first_ntp
        if i + 1 < len(valid):
            end = valid[i + 1].ntp - first_ntp
        else:
            end = video_duration if video_duration else start + 6.0
        if end <= start:
            continue
        if video_duration is not None and start >= video_duration:
            continue
        if video_duration is not None and end > video_duration:
            end = video_duration
        raw.append(RotationInterval(
            start_sec=round(start, 3),
            end_sec=round(end, 3),
            rotation=sample.rotation,
        ))
    merged: list[RotationInterval] = []
    for iv in raw:
        if merged and merged[-1].rotation == iv.rotation:
            merged[-1] = RotationInterval(
                start_sec=merged[-1].start_sec,
                end_sec=iv.end_sec,
                rotation=iv.rotation,
            )
        else:
            merged.append(iv)
    return RotationIntervals(intervals=merged)
def _probe_video(path: str | Path) -> tuple[int, int, float]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-of", "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    return stream["width"], stream["height"], float(stream.get("duration") or 0)
def _compute_canvas(width: int, height: int, intervals: RotationIntervals) -> tuple[int, int]:
    all_w: set[int] = {width}
    all_h: set[int] = {height}
    for iv in intervals.intervals:
        if iv.rotation in (90, 270):
            all_w.add(height)
            all_h.add(width)
    return max(all_w), max(all_h)
def _rotation_frame_size(width: int, height: int, rotation: int) -> tuple[int, int]:
    if rotation in (90, 270):
        return height, width
    return width, height
def _extract_rotate_segment(
    video_path: Path,
    output_path: Path,
    interval: RotationInterval,
    canvas_w: int,
    canvas_h: int,
    original_w: int,
    original_h: int,
) -> None:
    duration = interval.end_sec - interval.start_sec
    if duration <= 0:
        return
    fw, fh = _rotation_frame_size(original_w, original_h, interval.rotation)
    pad_x = (canvas_w - fw) // 2
    pad_y = (canvas_h - fh) // 2
    if interval.rotation == 0:
        vf = f"pad={canvas_w}:{canvas_h}:{pad_x}:{pad_y}:black"
    elif interval.rotation == 90:
        vf = f"rotate=90*PI/180:ow={fw}:oh={fh},pad={canvas_w}:{canvas_h}:{pad_x}:{pad_y}:black"
    elif interval.rotation == 180:
        vf = f"rotate=PI,pad={canvas_w}:{canvas_h}:{pad_x}:{pad_y}:black"
    elif interval.rotation == 270:
        vf = f"rotate=270*PI/180:ow={fw}:oh={fh},pad={canvas_w}:{canvas_h}:{pad_x}:{pad_y}:black"
    else:
        vf = f"pad={canvas_w}:{canvas_h}:{pad_x}:{pad_y}:black"
    cmd = [
        "ffmpeg", "-ss", str(interval.start_sec), "-i", str(video_path),
        "-t", str(duration), "-vf", vf,
        "-c:a", "aac", "-avoid_negative_ts", "make_zero", "-y",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
def _concat_segments(segment_paths: list[Path], output_path: Path) -> None:
    if len(segment_paths) == 1:
        os.replace(str(segment_paths[0]), str(output_path))
        return
    input_args: list[str] = []
    for p in segment_paths:
        input_args.extend(["-i", str(p)])
    n = len(segment_paths)
    labels = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    filter_str = f"{labels}concat=n={n}:v=1:a=1[outv][outa]"
    cmd = [
        "ffmpeg", *input_args,
        "-filter_complex", filter_str,
        "-map", "[outv]", "-map", "[outa]",
        "-y", str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
def rotate_video(
    video_path: str | Path,
    sidecar_path: str | Path,
    output_path: str | Path | None = None,
    *,
    dry_run: bool = False,
) -> Path:
    video_path = Path(video_path)
    sidecar_path = Path(sidecar_path)
    if output_path is None:
        output_path = video_path
    output_path = Path(output_path)
    samples = load_sidecar(sidecar_path)
    if not samples:
        raise ValueError(f"No rotation samples in sidecar: {sidecar_path}")
    w, h, dur = _probe_video(video_path)
    timeline = rotation_timeline(samples, video_duration=dur)
    intervals = timeline.intervals
    if not intervals:
        raise ValueError("Cannot build rotation timeline from sidecar data")
    if timeline.all_zero and not dry_run:
        if Path(output_path) != Path(video_path):
            import shutil
            shutil.copy2(video_path, output_path)
        return output_path
    uniform = timeline.uniform_rotation
    if uniform is not None and uniform != 0:
        fw, fh = _rotation_frame_size(w, h, uniform)
        if uniform == 90:
            vf = f"rotate=90*PI/180:ow={fw}:oh={fh}"
        elif uniform == 180:
            vf = "rotate=PI"
        elif uniform == 270:
            vf = f"rotate=270*PI/180:ow={fw}:oh={fh}"
        else:
            vf = "null"
        if not dry_run:
            # Write to a temp file first, then replace, to avoid reading
            # and writing the same file simultaneously (exit 234).
            tmp = output_path.with_name(output_path.stem + ".rotating.mp4")
            try:
                cmd = ["ffmpeg", "-i", str(video_path), "-vf", vf, "-c:a", "aac", "-y", str(tmp)]
                result = subprocess.run(cmd, check=True, capture_output=True)
                if tmp.exists():
                    os.replace(str(tmp), str(output_path))
            except BaseException:
                if tmp.exists():
                    tmp.unlink()
                raise
                os.replace(str(tmp), str(output_path))
            except BaseException:
                if tmp.exists():
                    tmp.unlink()
                raise
        return output_path
    cw, ch = _compute_canvas(w, h, timeline)
    if not dry_run:
        with tempfile.TemporaryDirectory(prefix="broadcastx_rotate_") as tmpdir:
            segments: list[Path] = []
            for idx, iv in enumerate(intervals):
                seg = Path(tmpdir) / f"seg_{idx:04d}.mp4"
                _extract_rotate_segment(video_path, seg, iv, cw, ch, w, h)
                segments.append(seg)
            _concat_segments(segments, output_path)
    return output_path
