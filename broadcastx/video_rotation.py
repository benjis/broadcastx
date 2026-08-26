"""Video rotation correction using ffmpeg."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .id3 import ID3RotationSample

RotationProgressCallback = Callable[[float, float, int | None, str | None, str], None]


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


def _compute_canvas(width: int, height: int) -> tuple[int, int]:
    """Return one landscape canvas for a mixed-orientation timeline."""
    return max(width, height), min(width, height)


def _rotation_frame_size(width: int, height: int, rotation: int) -> tuple[int, int]:
    if rotation in (90, 270):
        return height, width
    return width, height


def _run_ffmpeg_with_progress(
    cmd: list[str],
    *,
    progress_callback: RotationProgressCallback | None,
    offset_seconds: float = 0,
    total_seconds: float = 0,
    phase: str,
) -> None:
    """Run ffmpeg and relay its machine-readable progress records."""
    if progress_callback is None:
        subprocess.run(cmd, check=True, capture_output=True)
        return

    process = subprocess.Popen(
        [*cmd[:-1], "-progress", "pipe:1", "-nostats", cmd[-1]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    fields: dict[str, str] = {}
    assert process.stdout is not None
    for line in process.stdout:
        key, separator, value = line.rstrip().partition("=")
        if not separator:
            continue
        fields[key] = value
        if key != "progress":
            continue
        try:
            encoded_seconds = float(fields.get("out_time_us", fields.get("out_time_ms", "0"))) / 1_000_000
        except ValueError:
            encoded_seconds = 0
        try:
            frame = int(fields["frame"])
        except (KeyError, ValueError):
            frame = None
        progress_callback(
            min(offset_seconds + encoded_seconds, total_seconds) if total_seconds else offset_seconds + encoded_seconds,
            total_seconds,
            frame,
            fields.get("speed"),
            phase,
        )
        fields = {}
    stderr = process.stderr.read() if process.stderr else ""
    if process.wait() != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd, stderr=stderr)


def _extract_rotate_segment(
    video_path: Path,
    output_path: Path,
    interval: RotationInterval,
    canvas_w: int,
    canvas_h: int,
    original_w: int,
    original_h: int,
    progress_callback: RotationProgressCallback | None = None,
    offset_seconds: float = 0,
    total_seconds: float = 0,
    segment_number: int = 1,
    segment_count: int = 1,
) -> None:
    duration = interval.end_sec - interval.start_sec
    if duration <= 0:
        return
    fw, fh = _rotation_frame_size(original_w, original_h, interval.rotation)
    if interval.rotation == 0:
        orientation_filter = ""
    elif interval.rotation == 90:
        orientation_filter = f"rotate=90*PI/180:ow={fw}:oh={fh},"
    elif interval.rotation == 180:
        orientation_filter = "rotate=PI,"
    elif interval.rotation == 270:
        orientation_filter = f"rotate=270*PI/180:ow={fw}:oh={fh},"
    else:
        orientation_filter = ""
    vf = (
        f"{orientation_filter}"
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2,"
        f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )
    cmd = [
        "ffmpeg", "-ss", str(interval.start_sec), "-i", str(video_path),
        "-t", str(duration), "-vf", vf,
        "-c:a", "aac", "-avoid_negative_ts", "make_zero", "-y",
        str(output_path),
    ]
    _run_ffmpeg_with_progress(
        cmd,
        progress_callback=progress_callback,
        offset_seconds=offset_seconds,
        total_seconds=total_seconds,
        phase=f"Rotating segment {segment_number}/{segment_count}",
    )


def _concat_segments(
    segment_paths: list[Path],
    output_path: Path,
    progress_callback: RotationProgressCallback | None = None,
    total_seconds: float = 0,
) -> None:
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
    _run_ffmpeg_with_progress(
        cmd,
        progress_callback=progress_callback,
        total_seconds=total_seconds,
        phase="Joining segments",
    )


def rotate_video(
    video_path: str | Path,
    sidecar_path: str | Path,
    output_path: str | Path | None = None,
    *,
    dry_run: bool = False,
    progress_callback: RotationProgressCallback | None = None,
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
                _run_ffmpeg_with_progress(
                    cmd,
                    progress_callback=progress_callback,
                    total_seconds=dur,
                    phase="Rotating video",
                )
                if tmp.exists():
                    os.replace(str(tmp), str(output_path))
            except BaseException:
                if tmp.exists():
                    tmp.unlink()
                raise
        return output_path
    cw, ch = _compute_canvas(w, h)
    if not dry_run:
        with tempfile.TemporaryDirectory(prefix="broadcastx_rotate_") as tmpdir:
            segments: list[Path] = []
            processed_seconds = 0.0
            for idx, iv in enumerate(intervals):
                seg = Path(tmpdir) / f"seg_{idx:04d}.mp4"
                _extract_rotate_segment(
                    video_path,
                    seg,
                    iv,
                    cw,
                    ch,
                    w,
                    h,
                    progress_callback=progress_callback,
                    offset_seconds=processed_seconds,
                    total_seconds=dur,
                    segment_number=idx + 1,
                    segment_count=len(intervals),
                )
                segments.append(seg)
                processed_seconds += iv.end_sec - iv.start_sec
            _concat_segments(segments, output_path, progress_callback, dur)
    return output_path
