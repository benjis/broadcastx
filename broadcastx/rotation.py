"""Rotation metadata extraction and quantization for X broadcasts.

Orchestrates HLS resolution, ID3 parsing, and rotation quantisation.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .config import DEFAULT_BROWSER, extract_broadcast_id, normalize_broadcast_url
from .hls import (
    _ensure_media_playlist,
    _http_bytes,
    _http_text,
    _parse_media_playlist,
    _resolve_hls_playlist_url,
)
from .id3 import (
    ID3RotationSample,
    _extract_id3_from_ts,
    parse_id3_rotation_sample,
    quantize_rotation,
)
from .video_rotation import (
    load_sidecar,
    rotate_video as _rotate_video,
    rotation_timeline,
)

DEFAULT_HYSTERESIS_DEGREES = 10.0
SEGMENT_RANGE_BYTES = 8191

# Re-export for external consumers
__all__ = [
    "ID3RotationSample",
    "CARDINAL_ROTATIONS",
    "quantize_rotation",  # re-exported from id3
    "quantize_rotation_series",
    "parse_id3_rotation_sample",
    "extract_rotation_sidecar",
    "load_sidecar",
    "rotation_timeline",
    "rotate_video",
    # HLS helpers (used by pause_detector)
    "_ensure_media_playlist",
    "_http_text",
    "_parse_media_playlist",
    "_resolve_hls_playlist_url",
]

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

def rotate_video(
    video_path: str | Path,
    sidecar_path: str | Path,
    output_path: str | Path | None = None,
    *,
    dry_run: bool = False,
) -> Path:
    """Apply rotation correction to a video. Delegates to video_rotation module."""
    return _rotate_video(video_path, sidecar_path, output_path, dry_run=dry_run)

