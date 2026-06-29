import json
import json
import tempfile
from pathlib import Path
import unittest

from broadcastx.rotation import (
    ID3RotationSample,
    load_sidecar,
    parse_id3_rotation_sample,
    quantize_rotation_series,
    rotation_timeline,
)
from broadcastx.rotation import (
    ID3RotationSample,
    parse_id3_rotation_sample,
    quantize_rotation_series,
)


def _synchsafe(size: int) -> bytes:
    return bytes([
        (size >> 21) & 0x7F,
        (size >> 14) & 0x7F,
        (size >> 7) & 0x7F,
        size & 0x7F,
    ])


def _text_frame(frame_id: str, text: str) -> bytes:
    payload = b"\x03" + text.encode()
    return frame_id.encode() + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload


def _txxx_frame(description: str, value: str) -> bytes:
    payload = b"\x03" + description.encode() + b"\x00" + value.encode()
    return b"TXXX" + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload


def _id3_tag(*frames: bytes) -> bytes:
    body = b"".join(frames)
    return b"ID3\x04\x00\x00" + _synchsafe(len(body)) + body


class RotationSidecarTests(unittest.TestCase):
    def test_load_sidecar(self):
        lines = [
            '{"segment_index":0,"raw_rotation":89.7,"rotation":90,"program_date_time":"2026-06-11T20:07:35.534Z","ntp":3990197255.581486,"width":720,"height":1280}',
            '{"segment_index":1,"raw_rotation":91.2,"rotation":90,"ntp":3990197257.52085}',
            '{"segment_index":2,"raw_rotation":1.5,"rotation":0,"ntp":3990197259.10000}',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.rotation.jsonl"
            path.write_text("\n".join(lines) + "\n")
            samples = load_sidecar(path)
        self.assertEqual(len(samples), 3)
        self.assertEqual(samples[0].segment_index, 0)
        self.assertEqual(samples[0].rotation, 90)
        self.assertAlmostEqual(samples[0].raw_rotation, 89.7)
        self.assertAlmostEqual(samples[0].ntp, 3990197255.581486)
        self.assertEqual(samples[0].width, 720)
        self.assertEqual(samples[0].height, 1280)
        self.assertEqual(samples[0].program_date_time, "2026-06-11T20:07:35.534Z")
        self.assertIsNone(samples[1].width)
        self.assertIsNone(samples[1].height)

    def test_rotation_timeline_basic(self):
        samples = [
            ID3RotationSample(segment_index=0, raw_rotation=89.7, rotation=90, ntp=1000.0),
            ID3RotationSample(segment_index=1, raw_rotation=91.2, rotation=90, ntp=1002.0),
            ID3RotationSample(segment_index=2, raw_rotation=179.5, rotation=180, ntp=1005.0),
            ID3RotationSample(segment_index=3, raw_rotation=269.4, rotation=270, ntp=1008.0),
            ID3RotationSample(segment_index=4, raw_rotation=1.5, rotation=0, ntp=1012.0),
        ]
        timeline = rotation_timeline(samples, video_duration=16.0)
        self.assertEqual(len(timeline.intervals), 4)
        iv0 = timeline.intervals[0]
        self.assertEqual(iv0.rotation, 90)
        self.assertAlmostEqual(iv0.start_sec, 0.0)
        self.assertAlmostEqual(iv0.end_sec, 5.0)
        iv1 = timeline.intervals[1]
        self.assertEqual(iv1.rotation, 180)
        self.assertAlmostEqual(iv1.start_sec, 5.0)
        self.assertAlmostEqual(iv1.end_sec, 8.0)
        iv2 = timeline.intervals[2]
        self.assertEqual(iv2.rotation, 270)
        self.assertAlmostEqual(iv2.start_sec, 8.0)
        self.assertAlmostEqual(iv2.end_sec, 12.0)
        iv3 = timeline.intervals[3]
        self.assertEqual(iv3.rotation, 0)
        self.assertAlmostEqual(iv3.start_sec, 12.0)
        self.assertAlmostEqual(iv3.end_sec, 16.0)

    def test_rotation_timeline_all_zero_no_reencode(self):
        samples = [
            ID3RotationSample(segment_index=0, raw_rotation=1.2, rotation=0, ntp=1000.0),
            ID3RotationSample(segment_index=1, raw_rotation=358.5, rotation=0, ntp=1002.0),
        ]
        timeline = rotation_timeline(samples, video_duration=10.0)
        self.assertTrue(timeline.all_zero)
        self.assertEqual(timeline.uniform_rotation, 0)
        self.assertEqual(len(timeline.intervals), 1)
        self.assertEqual(timeline.intervals[0].rotation, 0)

    def test_empty_sidecar(self):
        self.assertEqual(len(rotation_timeline([]).intervals), 0)
        self.assertEqual(len(rotation_timeline([ID3RotationSample(segment_index=0, raw_rotation=90.0)]).intervals), 0)

    def test_load_empty_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.rotation.jsonl"
            path.write_text("")
            self.assertEqual(len(load_sidecar(path)), 0)

if __name__ == "__main__":
    unittest.main()
    def test_parse_id3_rotation_json_metadata(self):
        metadata = {
            "rotation": 89.721233,
            "ntp": 3990197255.581486,
            "width": 720,
            "height": 1280,
        }
        tag = _id3_tag(
            _text_frame("TKEY", "89.721"),
            _text_frame("TMED", "720.000"),
            _text_frame("TMOO", "1280.000"),
            _txxx_frame("JSONMetadata", json.dumps(metadata)),
        )

        sample = parse_id3_rotation_sample(tag, segment_index=12, program_date_time="2026-06-11T20:07:35.534Z")

        self.assertEqual(sample.segment_index, 12)
        self.assertEqual(sample.program_date_time, "2026-06-11T20:07:35.534Z")
        self.assertAlmostEqual(sample.raw_rotation, 89.721233)
        self.assertAlmostEqual(sample.ntp, 3990197255.581486)
        self.assertEqual(sample.width, 720)
        self.assertEqual(sample.height, 1280)
        self.assertEqual(sample.rotation, 90)

    def test_quantize_rotation_series_to_cardinal_angles(self):
        samples = [
            ID3RotationSample(segment_index=0, raw_rotation=89.7),
            ID3RotationSample(segment_index=1, raw_rotation=91.2),
            ID3RotationSample(segment_index=2, raw_rotation=179.1),
            ID3RotationSample(segment_index=3, raw_rotation=269.4),
            ID3RotationSample(segment_index=4, raw_rotation=359.2),
        ]

        quantized = quantize_rotation_series(samples)

        self.assertEqual([s.rotation for s in quantized], [90, 90, 180, 270, 0])

    def test_quantize_rotation_series_uses_hysteresis_near_boundaries(self):
        samples = [
            ID3RotationSample(segment_index=0, raw_rotation=134.0),
            ID3RotationSample(segment_index=1, raw_rotation=136.0),
            ID3RotationSample(segment_index=2, raw_rotation=134.5),
            ID3RotationSample(segment_index=3, raw_rotation=150.0),
        ]

        quantized = quantize_rotation_series(samples, hysteresis_degrees=10.0)

        self.assertEqual([s.rotation for s in quantized], [90, 90, 90, 180])


if __name__ == "__main__":
    unittest.main()
