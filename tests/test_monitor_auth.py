import unittest
from datetime import datetime
import os
import time
from unittest.mock import patch

from broadcastx.monitor import _has_auth_cookies, _looks_logged_out, _now_iso


class MonitorAuthTests(unittest.TestCase):
    def test_has_auth_cookies_requires_auth_token_and_ct0(self):
        self.assertTrue(_has_auth_cookies([
            {"name": "auth_token"},
            {"name": "ct0"},
            {"name": "guest_id"},
        ]))
        self.assertFalse(_has_auth_cookies([{"name": "auth_token"}]))
        self.assertFalse(_has_auth_cookies([{"name": "ct0"}]))

    def test_looks_logged_out_for_x_sign_in_pages(self):
        self.assertTrue(_looks_logged_out(
            "Happening now.\nContinue with phone\nEmail or username\nContinue"
        ))
        self.assertTrue(_looks_logged_out(
            "New to X?\nSign up now\nDon't miss what's happening\nLog in\nSign up"
        ))
        self.assertFalse(_looks_logged_out(
            "Home\nFollowing\nFor you\nThis broadcast is live"
        ))

    @unittest.skipUnless(hasattr(time, "tzset"), "requires process timezone support")
    def test_now_iso_uses_local_timezone(self):
        old_tz = os.environ.get("TZ")
        with patch.dict(os.environ, {"TZ": "Australia/Sydney"}):
            time.tzset()
            try:
                timestamp = datetime.fromisoformat(_now_iso())
                local_offset = datetime.now().astimezone().utcoffset()
            finally:
                if old_tz is None:
                    os.environ.pop("TZ", None)
                else:
                    os.environ["TZ"] = old_tz
                time.tzset()

        self.assertEqual(timestamp.utcoffset(), local_offset)


if __name__ == "__main__":
    unittest.main()
