"""Add text watermarks to videos using ffmpeg's drawtext filter."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Watermark position constants
POSITION_BOTTOM_RIGHT = "bottom-right"
POSITION_BOTTOM_LEFT = "bottom-left"
POSITION_TOP_RIGHT = "top-right"
POSITION_TOP_LEFT = "top-left"

VALID_POSITIONS = frozenset({
    POSITION_BOTTOM_RIGHT,
    POSITION_BOTTOM_LEFT,
    POSITION_TOP_RIGHT,
    POSITION_TOP_LEFT,
})

# Default watermark text
DEFAULT_WATERMARK_TEXT = "broadcastx"


@dataclass
class WatermarkOptions:
    """Options for text watermark appearance and placement.

    Attributes:
        text: Watermark text to display on the video.
        font: Font family or path to a font file (default: sans-serif).
        font_size: Font size in points (default: 24).
        opacity: Text opacity from 0.0 (transparent) to 1.0 (opaque) (default: 0.7).
        color: Font color (default: "white").
        position: Watermark position on screen. One of "bottom-right",
                  "bottom-left", "top-right", "top-left" (default: bottom-right).
        padding_x: Horizontal padding from the edge in pixels (default: 10).
        padding_y: Vertical padding from the edge in pixels (default: 10).
    """

    text: str = DEFAULT_WATERMARK_TEXT
    font: str = "sans-serif"
    font_size: int = 24
    opacity: float = 0.7
    color: str = "white"
    position: str = POSITION_BOTTOM_RIGHT
    padding_x: int = 10
    padding_y: int = 10

    def __post_init__(self) -> None:
        if self.position not in VALID_POSITIONS:
            raise ValueError(
                f"Invalid position '{self.position}'. "
                f"Must be one of: {', '.join(sorted(VALID_POSITIONS))}"
            )
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError(
                f"Opacity must be between 0.0 and 1.0, got {self.opacity}"
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_drawtext_filter(opts: WatermarkOptions) -> str:
    """Build the ffmpeg drawtext filter string from watermark options.

    Uses ``textfile`` (a temp file with the watermark text) to avoid
    shell-escaping problems with special characters in the text string.
    """
    # Position coordinates — w/tw = frame/text width, h/th = frame/text height
    px, py = opts.padding_x, opts.padding_y
    pos_map = {
        POSITION_BOTTOM_RIGHT: (f"w-tw-{px}", f"h-th-{py}"),
        POSITION_BOTTOM_LEFT: (str(px), f"h-th-{py}"),
        POSITION_TOP_RIGHT: (f"w-tw-{px}", str(py)),
        POSITION_TOP_LEFT: (str(px), str(py)),
    }
    x, y = pos_map[opts.position]

    fontcolor = f"{opts.color}@{opts.opacity}"

    # We build the filter without the textfile parameter here; the caller
    # adds it dynamically so it can point to a temporary file.
    return (
        f"drawtext="
        f"fontsize={opts.font_size}:"
        f"fontcolor={fontcolor}:"
        f"font='{opts.font}':"
        f"x={x}:y={y}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_watermark(
    video_path: str | Path,
    output_path: str | Path | None = None,
    options: WatermarkOptions | None = None,
    *,
    dry_run: bool = False,
) -> Path:
    """Add a text watermark to a video using ffmpeg's drawtext filter.

    Args:
        video_path: Path to the input video file.
        output_path: Path for the output video. If None, overwrites the input
                     video in-place.
        options: :class:`WatermarkOptions` controlling appearance and
                 placement. If None, defaults to ``"broadcastx"`` white text
                 at 70% opacity, 24pt, bottom-right corner.
        dry_run: If True, return the expected output path without running
                 ffmpeg.

    Returns:
        Path to the watermarked video (same as *output_path*).

    Raises:
        FileNotFoundError: If *video_path* does not exist.
        subprocess.CalledProcessError: If ffmpeg fails.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    if options is None:
        options = WatermarkOptions()

    if output_path is None:
        output_path = video_path
    output_path = Path(output_path)

    if dry_run:
        return output_path

    # Write the watermark text to a temp file so we can use textfile=
    # in the drawtext filter. This avoids all escaping problems with
    # colons, quotes, percent signs, etc.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="broadcastx_wm_", delete=False
    ) as tf:
        tf.write(options.text)
        textfile_path = tf.name

    vf = _build_drawtext_filter(options)
    vf += f":textfile={textfile_path}"

    # Write to a temp file first, then replace, to avoid reading and
    # writing the same file simultaneously (ffmpeg exit code 234).
    tmp = output_path.with_name(output_path.stem + ".watermarking.mp4")
    try:
        cmd = [
            "ffmpeg",
            "-i", str(video_path),
            "-vf", vf,
            "-c:a", "aac",
            "-y",
            str(tmp),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        if tmp.exists():
            tmp.replace(str(output_path))
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    finally:
        # Clean up the temp text file
        Path(textfile_path).unlink(missing_ok=True)

    return output_path
