import json
import tempfile
import unittest
from pathlib import Path

from broadcastx.models import BroadcastInfo
from broadcastx.scanner import (
    ScanResult,
    TimelineProgress,
    _is_graphql_response,
    _requires_login,
    _save_results,
)


class TimelineProgressTests(unittest.TestCase):
    def test_progress_changes_only_for_new_target_post_ids(self):
        progress = TimelineProgress(last_progress_at=10.0)

        self.assertEqual(progress.observe({"post-1", "post-2"}, now=12.0), 2)
        self.assertEqual(progress.last_progress_at, 12.0)
        self.assertEqual(progress.observe({"post-1", "post-2"}, now=20.0), 0)
        self.assertEqual(progress.last_progress_at, 12.0)
        self.assertEqual(progress.idle_seconds(now=22.5), 10.5)

        progress.reset_idle_timer(now=22.5)
        self.assertEqual(progress.idle_seconds(now=23.0), 0.5)

    def test_graphql_matching_does_not_depend_on_operation_name(self):
        self.assertTrue(
            _is_graphql_response(
                "https://x.com/i/api/graphql/hash-completely-new/NewTimelineSurface"
            )
        )
        self.assertFalse(_is_graphql_response("https://x.com/example/UserTweets"))
        self.assertFalse(
            _is_graphql_response(
                "https://x.com/i/api/graphql/abc/viewer_context.json"
            )
        )


class LoginDetectionTests(unittest.IsolatedAsyncioTestCase):
    class Context:
        def __init__(self, cookies):
            self._cookies = cookies

        async def cookies(self):
            return self._cookies

    class Locator:
        def __init__(self, text):
            self._text = text

        async def inner_text(self, timeout):
            return self._text

    class Page:
        def __init__(self, text):
            self._text = text

        def locator(self, selector):
            return LoginDetectionTests.Locator(self._text)

    async def test_requires_login_when_auth_cookies_are_missing(self):
        required = await _requires_login(
            self.Context([]),
            self.Page("Public profile content"),
        )

        self.assertTrue(required)

    async def test_accepts_authenticated_profile_page(self):
        required = await _requires_login(
            self.Context([{"name": "auth_token"}, {"name": "ct0"}]),
            self.Page("Posts Replies Reposts Media"),
        )

        self.assertFalse(required)

    async def test_rejects_expired_session_page(self):
        required = await _requires_login(
            self.Context([{"name": "auth_token"}, {"name": "ct0"}]),
            self.Page("Happening now. Continue with phone. Log in"),
        )

        self.assertTrue(required)


class ScanResultPersistenceTests(unittest.TestCase):
    def test_save_results_is_atomic_and_deduplicates_existing_broadcasts(self):
        broadcast = BroadcastInfo(
            broadcast_id="1SyntheticSaveABC",
            url="https://x.com/i/broadcasts/1SyntheticSaveABC",
            tweet_id="1000000000000000100",
            user_name="target_creator",
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "broadcasts.json"
            result = ScanResult(username="target_creator", broadcasts=[broadcast])

            _save_results(result, output)
            _save_results(result, output)

            saved = json.loads(output.read_text())
            self.assertEqual(saved["total_broadcasts"], 1)
            self.assertEqual(
                saved["broadcasts"][0]["broadcast_id"],
                "1SyntheticSaveABC",
            )
            self.assertFalse((Path(directory) / ".broadcasts.json.tmp").exists())

    def test_save_results_recovers_from_invalid_existing_json(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "broadcasts.json"
            output.write_text("not json")
            result = ScanResult(username="target_creator")

            _save_results(result, output)

            saved = json.loads(output.read_text())
            self.assertEqual(saved["broadcasts"], [])


if __name__ == "__main__":
    unittest.main()
