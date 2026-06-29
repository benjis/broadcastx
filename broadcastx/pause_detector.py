"""Detect and optionally trim video-pause sections from X broadcast recordings.

Uses two signals from the HLS playlist and segment metadata:

1. **Segment Content-Length** – paused segments are much smaller (40-60% of
   normal) because the video content is nearly static (black screen + avatar).

2. **PDT gap density** – during a pause almost every segment pair has a PDT
   gap > 3s; during normal playback only ~20-30% do.

Either signal can trigger on its own; the two complement each other with
different "blind spots" so the combined result is reliable.
"""

from __future__ import annotations

import statistics
import subprocess
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .rotation import (
    _ensure_media_playlist,
    _http_text,
    _parse_media_playlist,
    _resolve_hls_playlist_url,
)


@dataclass
class PauseInterval:
    """A continuous region of paused video."""
    start_sec: float
    end_sec: float
    segment_count: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_pauses(
    broadcast_url: str,
    browser: str = "chrome",
    size_ratio_threshold: float = 0.50,
    gap_density_threshold: float = 0.50,
    min_pause_sec: float = 10.0,
    baseline_window: int = 200,
    gap_window: int = 20,
    fill_gap_segments: int = 10,
) -> list[PauseInterval]:
    """Detect pause sections by combining segment-size and PDT-gap signals.

    Parameters
    ----------
    broadcast_url:
        X.com broadcast URL.
    browser:
        Browser for yt-dlp cookie extraction.
    size_ratio_threshold:
        Segment flagged when its size < this × (median of first *N* segments).
    gap_density_threshold:
        Segment flagged when the PDT-gap density in its trailing window
        exceeds this fraction.
    min_pause_sec:
        Minimum pause duration to report.
    baseline_window:
        Number of initial segments used to compute the size baseline median.
    gap_window:
        Trailing window (segments) for PDT-gap density.
    fill_gap_segments:
        Merge nearby flagged runs if the gap between them is ≤ this many
        segments.
    """
    # 1. Resolve HLS media playlist
    playlist_url = _resolve_hls_playlist_url(broadcast_url, browser)
    playlist_text = _http_text(playlist_url)
    playlist_text, _ = _ensure_media_playlist(playlist_text, playlist_url)
    segments = _parse_media_playlist(playlist_text, playlist_url)

    if not segments:
        return []

    # 2. Collect Content-Length for every segment via HEAD
    sizes = [_head_content_length(seg["url"]) for seg in segments]

    # 3. Map segment indices to wall-clock times using PDT
    times = _segment_times(segments)

    # 4. PDT-gap binary array + rolling density
    pdt_gaps, gap_density = _compute_gap_density(segments, window=gap_window)

    # 5. Size-baseline (median of first *baseline_window* segments)
    baseline = statistics.median(sizes[:baseline_window]) if sizes else 1

    # 6. Combined signal: either size-drop OR gap-density-spike
    is_paused = []
    for i in range(len(sizes)):
        size_flag = sizes[i] < size_ratio_threshold * baseline
        gap_flag = gap_density[i] > gap_density_threshold if i < len(gap_density) else False
        is_paused.append(size_flag or gap_flag)

    # 7. Merge consecutive flagged segments, filling small gaps
    intervals = _merge_pause_intervals(is_paused, times, min_pause_sec, fill_gap_segments)

    return intervals


def pause_report(
    pauses: list[PauseInterval],
    total_duration_sec: float | None = None,
) -> str:
    """Format detected pauses as a human-readable report string."""
    if not pauses:
        return "No pause sections detected."

    lines = [f"Detected {len(pauses)} pause section(s):\n"]
    for i, p in enumerate(pauses, 1):
        dur = p.end_sec - p.start_sec
        lines.append(
            f"  #{i}:  {_fmt_time(p.start_sec)}  ->  {_fmt_time(p.end_sec)}"
            f"  ({dur:.1f}s, {p.segment_count} segments)"
        )

    total_paused = sum(p.end_sec - p.start_sec for p in pauses)
    if total_duration_sec:
        kept = total_duration_sec - total_paused
        lines.append(
            f"\nTotal paused: {_fmt_duration(total_paused)}"
            f"  |  Kept: {_fmt_duration(kept)}"
            f"  |  Original: {_fmt_duration(total_duration_sec)}"
        )

    lines.append('\nRun with --trim to remove these sections.')
    return "\n".join(lines)


def trim_intervals(
    video_path: Path,
    pauses: list[PauseInterval],
    output_path: Path,
) -> Path:
    """Remove all pause intervals from *video_path* using ffmpeg stream copy."""
    if not pauses:
        _copy_video(video_path, output_path)
        return output_path

    total_dur = _probe_duration(video_path)

    keep_regions: list[tuple[float, float]] = []
    cursor = 0.0
    for p in pauses:
        p_start = max(p.start_sec, cursor)
        p_end = min(p.end_sec, total_dur)
        if p_start > cursor:
            keep_regions.append((cursor, p_start))
        cursor = p_end
    if cursor < total_dur:
        keep_regions.append((cursor, total_dur))

    if not keep_regions:
        raise ValueError("No content would remain after trimming all pauses.")

    _trim_to_regions(video_path, keep_regions, output_path)
    return output_path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _head_content_length(url: str) -> int:
    """Return Content-Length of *url* via a HEAD request."""
    req = urllib.request.Request(url, method="HEAD")
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("Referer", "https://x.com/")
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.headers.get("Content-Length")
        return int(raw) if raw else 0


def _segment_times(segments: list[dict]) -> list[float]:
    """Map each segment to its start offset (seconds) via PDT."""
    times: list[float] = []
    first_pdt: datetime | None = None
    for seg in segments:
        pdt_str = seg.get("program_date_time")
        if pdt_str:
            t = datetime.strptime(pdt_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            if first_pdt is None:
                first_pdt = t
                times.append(0.0)
            else:
                times.append((t - first_pdt).total_seconds())
        else:
            approx = times[-1] + 2.0 if times else 0.0
            times.append(approx)
    return times


def _compute_gap_density(
    segments: list[dict],
    window: int = 20,
) -> tuple[list[bool], list[float]]:
    """PDT-gap flag per segment pair + rolling gap density (len = segments)."""
    # Binary: has a PDT gap > 3s between segment i-1 and i
    binary: list[bool] = [False]  # seg 0 has no predecessor
    for i in range(1, len(segments)):
        pdt1 = segments[i-1].get("program_date_time")
        pdt2 = segments[i].get("program_date_time")
        if pdt1 and pdt2:
            try:
                t1 = datetime.strptime(pdt1, "%Y-%m-%dT%H:%M:%S.%fZ")
                t2 = datetime.strptime(pdt2, "%Y-%m-%dT%H:%M:%S.%fZ")
                binary.append((t2 - t1).total_seconds() > 3.0)
            except ValueError:
                binary.append(False)
        else:
            binary.append(False)

    # Rolling density
    density: list[float] = []
    for i in range(len(binary)):
        lo = max(0, i - window)
        chunk = binary[lo:i+1]
        density.append(sum(chunk) / len(chunk) if chunk else 0.0)
    return binary, density


def _probe_duration(video_path: Path) -> float:
    """Return duration (seconds) via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def _merge_pause_intervals(
    is_paused: list[bool],
    times: list[float],
    min_pause_sec: float,
    fill_gap_segments: int = 10,
) -> list[PauseInterval]:
    """Merge consecutive flagged segments into intervals, bridging small gaps."""
    # Collect raw runs
    raw_intervals: list[tuple[int, int]] = []  # (start_idx, end_idx_exclusive)
    start_idx: int | None = None

    for i, flagged in enumerate(is_paused):
        if flagged and start_idx is None:
            start_idx = i
        elif not flagged and start_idx is not None:
            raw_intervals.append((start_idx, i))
            start_idx = None

    if start_idx is not None:
        raw_intervals.append((start_idx, len(is_paused)))

    if not raw_intervals:
        return []

    # Bridge gaps between runs
    merged: list[PauseInterval] = []
    merge_start, merge_end = raw_intervals[0]

    for start, end in raw_intervals[1:]:
        gap_segs = start - merge_end
        if gap_segs <= fill_gap_segments:
            merge_end = end
        else:
            _append_interval(merged, merge_start, merge_end, times, min_pause_sec)
            merge_start, merge_end = start, end

    _append_interval(merged, merge_start, merge_end, times, min_pause_sec)

    return merged


def _append_interval(
    intervals: list[PauseInterval],
    start_idx: int,
    end_idx: int,
    times: list[float],
    min_pause_sec: float,
) -> None:
    """Append a PauseInterval if it meets the minimum duration."""
    start_t = times[start_idx] if start_idx < len(times) else 0.0
    end_t = times[min(end_idx, len(times) - 1)] if end_idx > 0 else 0.0
    dur = end_t - start_t
    if dur >= min_pause_sec:
        intervals.append(PauseInterval(
            start_sec=round(start_t, 1),
            end_sec=round(end_t, 1),
            segment_count=end_idx - start_idx,
        ))


def _trim_to_regions(
    video_path: Path,
    regions: list[tuple[float, float]],
    output_path: Path,
) -> None:
    """Use ffmpeg to keep only *regions* of the video (concat demuxer)."""
    import os
    import tempfile

    if len(regions) == 1 and regions[0][0] == 0.0:
        _copy_video(video_path, output_path)
        return

    with tempfile.TemporaryDirectory(prefix="broadcastx_trim_") as tmpdir:
        tmp = Path(tmpdir)
        seg_files: list[Path] = []

        for i, (start, end) in enumerate(regions):
            seg = tmp / f"seg_{i:04d}.mp4"
            dur = end - start
            cmd = [
                "ffmpeg",
                "-ss", str(start),
                "-i", str(video_path),
                "-t", str(dur),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-y",
                str(seg),
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            seg_files.append(seg)

        concat_txt = tmp / "concat.txt"
        concat_txt.write_text(
            "\n".join(f"file {f.name}" for f in seg_files),
            encoding="utf-8",
        )

        cmd_concat = [
            "ffmpeg", "-f", "concat", "-safe", "0",
            "-i", str(concat_txt),
            "-c", "copy",
            "-y", str(output_path),
        ]
        subprocess.run(cmd_concat, check=True, capture_output=True)


def _copy_video(src: Path, dst: Path) -> None:
    """Copy src to dst."""
    if src == dst:
        return
    import shutil
    shutil.copy2(src, dst)


def _fmt_time(total_seconds: float) -> str:
    """Format seconds as MM:SS.t or H:MM:SS.t."""
    if total_seconds < 0:
        return "-:--"
    m, s = divmod(int(total_seconds), 60)
    dec = int(round((total_seconds - int(total_seconds)) * 10))
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}.{dec}"
    return f"{m}:{s:02d}.{dec}"


def _fmt_duration(total_seconds: float) -> str:
    """Format as Xs, Xmin Ys, or Xh Ymin Zs."""
    if total_seconds < 60:
        return f"{total_seconds:.0f}s"
    m, s = divmod(int(total_seconds), 60)
    if m < 60:
        return f"{m}min {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}min {s}s"
