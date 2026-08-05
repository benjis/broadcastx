"""Output-mode contract for download_broadcast.

progress="inplace" (default) keeps the single-line carriage-return
behavior; progress="stages" emits complete event lines only, never
carriage returns. Download, parallel metadata, rotation, and failure
semantics are identical in both modes — only the output shape differs.
Monitor background downloads use progress="stages".
"""

import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from broadcastx.downloader import _download_progress_text, download_broadcast

URL = "https://x.com/i/broadcasts/1vAxRkBbDRzKl"


def _fake_process(lines, returncode=0):
    """A fake yt-dlp subprocess instance yielding the given stdout lines."""

    class _FakeProcess:
        def __init__(self):
            self.stdout = iter(lines)
            self.returncode = returncode

        def wait(self):
            return None

    return _FakeProcess()


def _download(output_dir, progress=None, sidecar_sleep=0.0, write_video=False, mock_console=False, metadata_segments=None):
    """Run download_broadcast with yt-dlp, sidecar, and rotation faked.

    Mirrors tests/test_downloader_rotation.py: the yt-dlp subprocess is a
    fake emitting progress ticks plus a Destination line, metadata
    extraction and rotation are patched out. sidecar_sleep>0 keeps the
    metadata thread busy so the main thread deterministically reaches the
    stage-join before the sidecar finishes. progress=None exercises the
    default argument. mock_console=True also patches the module console
    (returning it) so the calls can be inspected directly, mirroring the
    existing in-place progress tests.
    """
    output_path = Path(output_dir) / "1vAxRkBbDRzKl.mp4"
    if write_video:
        output_path.write_text("fake video content")
    lines = [
        "[download]  10.0% of ~1.00GiB at  1.00MiB/s ETA 01:00\n",
        "[download]  50.0% of ~1.00GiB at  1.00MiB/s ETA 00:30\n",
        f"[download] Destination: {output_path}\n",
    ]

    def _sidecar(*args, **kwargs):
        if metadata_segments is not None:
            callback = kwargs["progress_callback"]
            callback(0, metadata_segments)
            callback(metadata_segments, metadata_segments)
        if sidecar_sleep:
            time.sleep(sidecar_sleep)
        return Path(output_dir) / "1vAxRkBbDRzKl.rotation.jsonl"

    kwargs = {} if progress is None else {"progress": progress}
    with ExitStack() as stack:
        stack.enter_context(patch("broadcastx.downloader.subprocess.Popen", return_value=_fake_process(lines)))
        stack.enter_context(patch("broadcastx.downloader.extract_rotation_sidecar", side_effect=_sidecar))
        stack.enter_context(patch("broadcastx.downloader._apply_rotation"))
        console = stack.enter_context(patch("broadcastx.downloader.console")) if mock_console else None
        result = download_broadcast(URL, output_dir=Path(output_dir), **kwargs)
        return result, console


def test_stages_mode_never_writes_carriage_return(capsys):
    """progress='stages' output contains no \r characters, even with progress ticks."""
    with tempfile.TemporaryDirectory() as tmp:
        result, console = _download(tmp, progress="stages", mock_console=True)
        assert result is not None
        assert result.success
    for call in console.print.call_args_list:
        assert "\r" not in call.args[0], f"stages mode must not emit \\r: {call.args[0]!r}"
    captured = capsys.readouterr()
    assert "\r" not in captured.out + captured.err


def test_stages_mode_emits_complete_stage_events(capsys):
    """progress='stages' prints one complete event line per pipeline stage."""
    with tempfile.TemporaryDirectory() as tmp:
        result, _ = _download(tmp, progress="stages", sidecar_sleep=0.3, write_video=True)
        assert result is not None
        assert result.success
        assert result.rotation_applied
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).splitlines()
    assert lines
    for line in lines:
        assert "\r" not in line, f"stages mode must not emit carriage returns: {line!r}"
    text = "\n".join(lines)
    assert "Downloading 1vAxRkBbDRzKl" in text        # download start
    assert "Downloading metadata" in text              # metadata stage event
    assert "Video downloaded" in text                  # video complete
    assert "Rotation sidecar" in text                  # metadata complete
    assert "Converting orientation" in text            # rotation stage event
    assert "Rotation corrected" in text                # rotation complete
    assert "Downloading video" not in text  # percent ticks are dropped


def test_inplace_mode_keeps_single_line_progress():
    """progress='inplace' (default) writes raw \r-updated progress."""
    with tempfile.TemporaryDirectory() as tmp:
        result, console = _download(tmp, mock_console=True)  # default progress="inplace"
        assert result is not None
        assert result.success
    progress_calls = [
        c for c in console.file.write.call_args_list if "Video " in c.args[0]
    ]
    assert progress_calls
    for call in progress_calls:
        assert call.args[0].startswith("\r"), "inplace progress must be carriage-return based"


def test_inplace_mode_shows_video_and_metadata_progress_together():
    """Parallel workers render their independent progress on the same line."""
    with tempfile.TemporaryDirectory() as tmp:
        result, console = _download(tmp, mock_console=True, metadata_segments=4)
        assert result is not None
        assert result.success
    progress = [c.args[0] for c in console.file.write.call_args_list if "Video 50.0%" in c.args[0]]
    assert any("Metadata 100.0% (4/4)" in line for line in progress)


def test_download_progress_recognizes_colorized_percentage_without_total_size():
    """Progress remains visible when yt-dlp has no total size to report."""
    line = "\x1b[?25l\x1b[0;94m[download]\x1b[0m   42.3% at 4.56MiB/s ETA 04:23"
    assert _download_progress_text(line) == "42.3% at 4.56MiB/s ETA 04:23"
