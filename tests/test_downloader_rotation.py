import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from broadcastx import downloader
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

    @patch("broadcastx.video_rotation._probe_video", return_value=(720, 1280, 30.0))
    @patch("broadcastx.video_rotation.subprocess.run")
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

    @patch("broadcastx.video_rotation._probe_video", return_value=(720, 1280, 30.0))
    @patch("broadcastx.video_rotation.subprocess.run")
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

    @patch("broadcastx.video_rotation._probe_video", return_value=(720, 1280, 30.0))
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


class _ProgressProcess:
    """Fake subprocess that emits yt-dlp-style progress lines."""

    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return None


class DownloaderProgressTests(unittest.TestCase):
    def test_download_progress_updates_single_line(self):
        """Multiple yt-dlp progress updates collapse into one in-place line."""
        url = "https://x.com/i/broadcasts/1vAxRkBbDRzKl"
        output_path = Path("/tmp") / "1vAxRkBbDRzKl.mp4"
        lines = [
            "[download]  10.0% of ~1.00GiB at  1.00MiB/s ETA 01:00\n",
            "[download]  50.0% of ~1.00GiB at  1.00MiB/s ETA 00:30\n",
            f"[download] Destination: {output_path}\n",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("broadcastx.downloader.subprocess.Popen", return_value=_ProgressProcess(lines)),
                patch("broadcastx.downloader.extract_rotation_sidecar", return_value=Path(tmp) / "1vAxRkBbDRzKl.rotation.jsonl"),
                patch("broadcastx.downloader._apply_rotation"),
                patch("broadcastx.downloader.console") as console,
            ):
                download_broadcast(url, output_dir=Path(tmp))

        calls = console.file.write.call_args_list
        progress = [c for c in calls if "Video " in c.args[0]]
        self.assertEqual(len(progress), 2)
        for call in progress:
            self.assertTrue(call.args[0].startswith("\r"), "progress must be carriage-return based")
        self.assertIn("10.0%", progress[0].args[0])
        self.assertIn("50.0%", progress[1].args[0])
        # the final Destination line is a real newline-terminated line
        dest = [c for c in console.print.call_args_list if "Destination" in c.args[0]]
        self.assertEqual(len(dest), 1)
        self.assertNotEqual(dest[0].kwargs.get("end"), "")

    def test_metadata_download_runs_parallel_with_video(self):
        """The sidecar (rotation metadata) download starts while the video is still downloading."""
        url = "https://x.com/i/broadcasts/1vAxRkBbDRzKl"
        reading_started = threading.Event()
        release_reading = threading.Event()
        sidecar_started = threading.Event()
        results = []

        def _fake_sidecar(*args, **kwargs):
            sidecar_started.set()
            return Path("/tmp") / "fake.rotation.jsonl"

        class _BlockingStdout:
            def __init__(self_):
                self_.first = True

            def __iter__(self_):
                return self_

            def __next__(self_):
                if self_.first:
                    self_.first = False
                    reading_started.set()
                    if not release_reading.wait(timeout=5):
                        raise StopIteration
                    return "[download] Destination: /tmp/fake.mp4\n"
                raise StopIteration

        class _BlockingProcess:
            returncode = 0

            def __init__(self_, cmd, **kwargs):
                self_.stdout = _BlockingStdout()

            def wait(self_):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("broadcastx.downloader.subprocess.Popen", _BlockingProcess),
                patch("broadcastx.downloader.extract_rotation_sidecar", side_effect=_fake_sidecar),
                patch("broadcastx.downloader.console"),
            ):
                thread = threading.Thread(
                    target=lambda: results.append(download_broadcast(url, output_dir=Path(tmp)))
                )
                thread.start()
                self.assertTrue(reading_started.wait(timeout=5), "video download should have started")
                self.assertTrue(
                    sidecar_started.wait(timeout=5),
                    "metadata download should run in parallel with the video download",
                )
                release_reading.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0].success)

    def test_rotation_starts_only_after_video_and_metadata_ready(self):
        """ffmpeg rotation must wait for both the video and the sidecar."""
        url = "https://x.com/i/broadcasts/1vAxRkBbDRzKl"
        video_done = threading.Event()
        sidecar_done = threading.Event()
        order = []

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "1vAxRkBbDRzKl.mp4"
            out.write_text("fake video content")

            class _FakeProcess:
                def __init__(self_, cmd, **kwargs):
                    self_.stdout = iter([f"[download] Destination: {out}\n"])
                    self_.returncode = 0

                def wait(self_):
                    video_done.set()
                    order.append("video")

            def _fake_sidecar(*args, **kwargs):
                order.append("sidecar")
                sidecar_done.set()
                return Path(tmp) / "1vAxRkBbDRzKl.rotation.jsonl"

            def _fake_rotate(video, sidecar, **kwargs):
                order.append("rotate")
                self.assertTrue(video_done.is_set(), "rotation must wait for the video download")
                self.assertTrue(sidecar_done.is_set(), "rotation must wait for the metadata download")

            with (
                patch("broadcastx.downloader.subprocess.Popen", _FakeProcess),
                patch("broadcastx.downloader.extract_rotation_sidecar", side_effect=_fake_sidecar),
                patch("broadcastx.downloader._apply_rotation", side_effect=_fake_rotate),
                patch("broadcastx.downloader.console"),
            ):
                result = download_broadcast(url, output_dir=Path(tmp))

        self.assertTrue(result.success)
        self.assertTrue(result.rotation_applied)
        self.assertEqual(order[-1], "rotate")

    def test_stage_indicator_updates_in_place_while_running(self):
        """Stages without intrinsic progress show an in-place elapsed ticker."""
        with patch("broadcastx.downloader.console") as console:
            with downloader._stage_indicator("Converting orientation"):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if any("Converting orientation" in c.args[0] for c in console.file.write.call_args_list):
                        break
                    time.sleep(0.01)
            ticks = [c for c in console.file.write.call_args_list if "Converting orientation" in c.args[0]]
            self.assertTrue(ticks, "stage indicator should update while the stage runs")
            for call in ticks:
                self.assertTrue(call.args[0].startswith("\r"))
            # the in-place line is erased again when the stage finishes
            erased = [
                c for c in console.file.write.call_args_list
                if "Converting orientation" not in c.args[0] and c.args[0].startswith("\r")
            ]
            self.assertTrue(erased, "indicator line should be erased on completion")


if __name__ == "__main__":
    unittest.main()
