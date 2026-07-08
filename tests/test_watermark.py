"""Tests for the watermark module."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, call

from broadcastx.watermark import (
    POSITION_BOTTOM_LEFT,
    POSITION_BOTTOM_RIGHT,
    POSITION_TOP_LEFT,
    POSITION_TOP_RIGHT,
    WatermarkOptions,
    _build_drawtext_filter,
    add_watermark,
)


class WatermarkOptionsTests(unittest.TestCase):
    """WatermarkOptions validation."""

    def test_defaults(self):
        opts = WatermarkOptions()
        self.assertEqual(opts.text, "broadcastx")
        self.assertEqual(opts.font, "sans-serif")
        self.assertEqual(opts.font_size, 24)
        self.assertEqual(opts.opacity, 0.7)
        self.assertEqual(opts.color, "white")
        self.assertEqual(opts.position, POSITION_BOTTOM_RIGHT)
        self.assertEqual(opts.padding_x, 10)
        self.assertEqual(opts.padding_y, 10)

    def test_custom_values(self):
        opts = WatermarkOptions(
            text="custom",
            font="Arial",
            font_size=36,
            opacity=0.5,
            color="yellow",
            position=POSITION_TOP_LEFT,
            padding_x=20,
            padding_y=30,
        )
        self.assertEqual(opts.text, "custom")
        self.assertEqual(opts.font, "Arial")
        self.assertEqual(opts.font_size, 36)
        self.assertEqual(opts.opacity, 0.5)
        self.assertEqual(opts.color, "yellow")
        self.assertEqual(opts.position, POSITION_TOP_LEFT)
        self.assertEqual(opts.padding_x, 20)
        self.assertEqual(opts.padding_y, 30)

    def test_rejects_invalid_position(self):
        with self.assertRaises(ValueError) as ctx:
            WatermarkOptions(position="center")
        self.assertIn("Invalid position", str(ctx.exception))

    def test_rejects_opacity_below_zero(self):
        with self.assertRaises(ValueError) as ctx:
            WatermarkOptions(opacity=-0.1)
        self.assertIn("Opacity", str(ctx.exception))

    def test_rejects_opacity_above_one(self):
        with self.assertRaises(ValueError) as ctx:
            WatermarkOptions(opacity=1.5)
        self.assertIn("Opacity", str(ctx.exception))

    def test_accepts_boundary_opacities(self):
        opts = WatermarkOptions(opacity=0.0)
        self.assertEqual(opts.opacity, 0.0)
        opts = WatermarkOptions(opacity=1.0)
        self.assertEqual(opts.opacity, 1.0)


class DrawtextFilterTests(unittest.TestCase):
    """_build_drawtext filter string generation."""

    def test_bottom_right_default(self):
        opts = WatermarkOptions(position=POSITION_BOTTOM_RIGHT)
        result = _build_drawtext_filter(opts)
        self.assertIn("x=w-tw-10", result)
        self.assertIn("y=h-th-10", result)
        self.assertIn("fontsize=24", result)
        self.assertIn("fontcolor=white@0.7", result)
        self.assertIn("font='sans-serif'", result)

    def test_bottom_left(self):
        opts = WatermarkOptions(position=POSITION_BOTTOM_LEFT, padding_x=15)
        result = _build_drawtext_filter(opts)
        self.assertIn("x=15", result)
        self.assertIn("y=h-th-10", result)
        self.assertIn("fontcolor=white@0.7", result)

    def test_top_right(self):
        opts = WatermarkOptions(position=POSITION_TOP_RIGHT, padding_y=20)
        result = _build_drawtext_filter(opts)
        self.assertIn("x=w-tw-10", result)
        self.assertIn("y=20", result)

    def test_top_left(self):
        opts = WatermarkOptions(position=POSITION_TOP_LEFT)
        result = _build_drawtext_filter(opts)
        self.assertIn("x=10", result)
        self.assertIn("y=10", result)

    def test_custom_color_and_opacity(self):
        opts = WatermarkOptions(color="red", opacity=0.3)
        result = _build_drawtext_filter(opts)
        self.assertIn("fontcolor=red@0.3", result)

    def test_custom_font_size(self):
        opts = WatermarkOptions(font_size=48)
        result = _build_drawtext_filter(opts)
        self.assertIn("fontsize=48", result)


class AddWatermarkTests(unittest.TestCase):
    """add_watermark() function behavior."""

    def test_missing_video_raises(self):
        with self.assertRaises(FileNotFoundError):
            add_watermark("/nonexistent/video.mp4")

    def test_dry_run_returns_path_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "test.mp4"
            video.write_text("fake video content")
            out = add_watermark(video, dry_run=True)
            self.assertEqual(out, video)
            # File content should be untouched
            self.assertEqual(video.read_text(), "fake video content")

    @patch("broadcastx.watermark.subprocess.run")
    def test_default_options_pass_correct_filter(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "input.mp4"
            video.write_text("fake video content")
            output = Path(tmp) / "watermarked.mp4"

            result = add_watermark(video, output_path=output)

            self.assertEqual(result, output)
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]
            self.assertEqual(cmd[0], "ffmpeg")
            self.assertEqual(cmd[1], "-i")
            self.assertEqual(cmd[2], str(video))
            # Check -vf flag
            vf_idx = cmd.index("-vf")
            filter_str = cmd[vf_idx + 1]
            self.assertIn("drawtext=", filter_str)
            self.assertIn("fontsize=24", filter_str)
            self.assertIn("fontcolor=white@0.7", filter_str)
            self.assertIn("x=w-tw-10", filter_str)
            self.assertIn("y=h-th-10", filter_str)
            # Should use textfile (not text=) to avoid escaping issues
            self.assertIn("textfile=", filter_str)

    @patch("broadcastx.watermark.subprocess.run")
    def test_custom_options_passed_to_ffmpeg(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "input.mp4"
            video.write_text("fake video content")
            output = Path(tmp) / "out.mp4"

            opts = WatermarkOptions(
                text="My Custom Watermark",
                font="Arial",
                font_size=48,
                opacity=0.5,
                color="red",
                position=POSITION_TOP_LEFT,
                padding_x=20,
                padding_y=30,
            )

            add_watermark(video, output_path=output, options=opts)

            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]
            vf_idx = cmd.index("-vf")
            filter_str = cmd[vf_idx + 1]
            self.assertIn("fontsize=48", filter_str)
            self.assertIn("fontcolor=red@0.5", filter_str)
            self.assertIn("x=20", filter_str)
            self.assertIn("y=30", filter_str)
            self.assertIn("font='Arial'", filter_str)

    @patch("broadcastx.watermark.subprocess.run")
    def test_output_path_defaults_to_input(self, mock_run):
        """When no output_path is given, overwrite the input video."""
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "input.mp4"
            video.write_text("fake")

            result = add_watermark(video)

            self.assertEqual(result, video)

    @patch("broadcastx.watermark.subprocess.run")
    def test_output_path_overwritten_by_temp_then_replaced(self, mock_run):
        """Verify the temp-file rename pattern is used."""
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "input.mp4"
            video.write_text("fake")

            # Simulate that ffmpeg creates the temp file
            def _fake_run(cmd, **_kwargs):
                # Find the temp output path in the command
                tmp_output = Path(cmd[-1])
                tmp_output.write_text("watermarked video")
                return unittest.mock.MagicMock()

            mock_run.side_effect = _fake_run

            result = add_watermark(video)

            self.assertEqual(result, video)
            # After the replace, the original path should have the new content
            self.assertEqual(video.read_text(), "watermarked video")

    @patch("broadcastx.watermark.subprocess.run")
    def test_temp_file_cleaned_on_ffmpeg_failure(self, mock_run):
        """If ffmpeg fails, the temp .watermarking.mp4 file is cleaned up."""
        import subprocess
        mock_run.side_effect = subprocess.CalledProcessError(1, ["ffmpeg"], stderr="error")

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "input.mp4"
            video.write_text("fake")

            with self.assertRaises(subprocess.CalledProcessError):
                add_watermark(video)

            # No .watermarking.mp4 files should remain
            leftovers = list(Path(tmp).glob("*.watermarking.mp4"))
            self.assertEqual(leftovers, [])

    @patch("broadcastx.watermark.subprocess.run")
    def test_writes_text_to_temp_file(self, mock_run):
        """The watermark text is written to a tempfile and referenced via textfile=."""
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "input.mp4"
            video.write_text("fake")
            output = Path(tmp) / "out.mp4"

            add_watermark(video, output_path=output)

            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]
            vf_idx = cmd.index("-vf")
            filter_str = cmd[vf_idx + 1]

            # Should reference a temp file via textfile=
            textfile_match = filter_str.split("textfile=")[1]
            textfile_path = textfile_match.split(":")[0] if ":" in textfile_match else textfile_match

            # The temp file should have been cleaned up after run
            self.assertFalse(Path(textfile_path).exists())

    @patch("broadcastx.watermark.subprocess.run")
    def test_video_path_as_string(self, mock_run):
        """Accept str in addition to Path."""
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "input.mp4"
            video.write_text("fake")

            result = add_watermark(str(video))

            self.assertIsInstance(result, Path)
            self.assertEqual(result, video)


if __name__ == "__main__":
    unittest.main()
