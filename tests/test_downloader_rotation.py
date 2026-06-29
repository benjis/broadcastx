import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from broadcastx.downloader import download_broadcast
from broadcastx.rotation import ID3RotationSample, load_sidecar, rotation_timeline, rotate_video


class _FakeProcess:
    def __init__(self, returncode=0):
        self._output_path = None
        self.stdout = iter([
            f"[download] Destination: {self._output_path}\n" if self._output_path else "output/videos/1vAxRkBbDRzKl.mp4\n",
        ])
        self.returncode = returncode

    def wait(self):
        return None


class DownloaderRotationTests(unittest.TestCase):
    def test_download_broadcast_always_rotates(self):
        import os
        url = "https://x.com/i/broadcasts/1vAxRkBbDRzKl"
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "1vAxRkBbDRzKl.rotation.jsonl"
            output_path = Path(tmp) / "1vAxRkBbDRzKl.mp4"
            output_path.write_text("fake video content")

            class _FakeProcessWithOutput:
                def __init__(self_, returncode=0):
                    self_.stdout = iter([
                        f"[download] Destination: {output_path}\n",
                    ])
                    self_.returncode = returncode
                def wait(self_):
                    return None

            with (
                patch("broadcastx.downloader.subprocess.Popen", return_value=_FakeProcessWithOutput()),
                patch("broadcastx.downloader.extract_rotation_sidecar", return_value=sidecar),
                patch("broadcastx.downloader._apply_rotation") as rotate,
            ):
                result = download_broadcast(url, output_dir=Path(tmp))

        self.assertTrue(result.success)
        self.assertEqual(result.rotation_sidecar_file, str(sidecar))
        self.assertTrue(result.rotation_applied)
        rotate.assert_called_once()

    def test_rotation_sidecar_failure_does_not_fail_video_download(self):
        import os
        url = "https://x.com/i/broadcasts/1vAxRkBbDRzKl"
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("broadcastx.downloader.subprocess.Popen", return_value=_FakeProcess()),
                patch("broadcastx.downloader.extract_rotation_sidecar", side_effect=RuntimeError("metadata unavailable")),
                patch("broadcastx.downloader._apply_rotation"),
            ):
                result = download_broadcast(url, output_dir=Path(tmp))

        self.assertTrue(result.success)
        self.assertIsNone(result.rotation_sidecar_file)
        self.assertFalse(result.rotation_applied)
        self.assertIn("metadata unavailable", result.warning)

    @patch("broadcastx.rotation._probe_video", return_value=(720, 1280, 30.0))
    @patch("broadcastx.rotation.subprocess.run")
    def test_rotate_video_all_zero_copies_inplace(self, mock_run, mock_probe):
        """All rotations = 0 → copy, not ffmpeg."""
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "in.mp4"
            video.write_text("fake mp4")
            output = Path(tmp) / "out.mp4"
            sidecar = Path(tmp) / "test.rotation.jsonl"
            samples = [
                '{"segment_index":0,"raw_rotation":0.5,"rotation":0,"ntp":1000.0}',
                '{"segment_index":1,"raw_rotation":1.2,"rotation":0,"ntp":1002.0}',
            ]
            sidecar.write_text("\n".join(samples) + "\n")
            out = rotate_video(video, sidecar, output_path=output, dry_run=False)
            self.assertEqual(out, output)
            self.assertTrue(output.exists())
            self.assertEqual(output.read_text(), "fake mp4")
        mock_run.assert_not_called()

    @patch("broadcastx.rotation._probe_video", return_value=(720, 1280, 30.0))
    @patch("broadcastx.rotation.subprocess.run")
    def test_rotate_video_uniform_90_calls_ffmpeg(self, mock_run, mock_probe):
        """Uniform 90° rotation → one ffmpeg call with rotate filter."""
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "in.mp4"
            video.write_text("fake mp4")
            output = Path(tmp) / "out.mp4"
            sidecar = Path(tmp) / "test.rotation.jsonl"
            samples = [
                '{"segment_index":0,"raw_rotation":89.7,"rotation":90,"ntp":1000.0}',
                '{"segment_index":1,"raw_rotation":91.2,"rotation":90,"ntp":1005.0}',
            ]
            sidecar.write_text("\n".join(samples) + "\n")
            out = rotate_video(video, sidecar, output_path=output, dry_run=False)
            self.assertEqual(out, output)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("-vf", cmd)
        vf_idx = cmd.index("-vf") + 1
        filter_str = cmd[vf_idx]
        self.assertIn("rotate=90*PI/180", filter_str)

    @patch("broadcastx.rotation._probe_video", return_value=(720, 1280, 30.0))
    def test_rotate_video_dry_run_no_execution(self, mock_probe):
        """dry_run=True should not execute any ffmpeg calls."""
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "in.mp4"
            video.write_text("fake mp4")
            output = Path(tmp) / "out.mp4"
            sidecar = Path(tmp) / "test.rotation.jsonl"
            samples = [
                '{"segment_index":0,"raw_rotation":89.7,"rotation":90,"ntp":1000.0}',
            ]
            sidecar.write_text("\n".join(samples) + "\n")
            out = rotate_video(video, sidecar, output_path=output, dry_run=True)
            self.assertEqual(out, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
