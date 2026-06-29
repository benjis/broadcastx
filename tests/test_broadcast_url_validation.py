import unittest

from broadcastx.config import extract_broadcast_id, is_broadcast_url, normalize_broadcast_url
from broadcastx.monitor import _extract_candidates, _is_live_candidate_status


class BroadcastUrlValidationTests(unittest.TestCase):
    def test_rejects_truncated_broadcast_ids(self):
        self.assertIsNone(extract_broadcast_id("https://x.com/i/broadcasts/1"))
        self.assertIsNone(normalize_broadcast_url("https://x.com/i/broadcasts/1"))
        self.assertFalse(is_broadcast_url("https://x.com/i/broadcasts/1"))

    def test_extracts_real_broadcast_ids(self):
        url = "https://x.com/i/broadcasts/1vAxRkBbDRzKl"
        self.assertEqual(extract_broadcast_id(url), "1vAxRkBbDRzKl")
        self.assertEqual(normalize_broadcast_url(url), url)

    def test_monitor_ignores_truncated_broadcast_links(self):
        candidates = _extract_candidates("https://x.com/i/broadcasts/1")
        self.assertEqual(candidates, [])

    def test_unknown_status_is_not_live(self):
        self.assertTrue(_is_live_candidate_status("live"))
        self.assertFalse(_is_live_candidate_status("ended"))
        self.assertFalse(_is_live_candidate_status("unknown"))


if __name__ == "__main__":
    unittest.main()
