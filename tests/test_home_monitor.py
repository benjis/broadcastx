import asyncio
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import Error

from broadcastx.home_monitor import (
    HomeBroadcast,
    HomeRateLimitError,
    _HomeMonitorBrowser,
    _collect_home_broadcasts,
    _download_ended_broadcast,
    _extract_fleetline_broadcasts,
    _load_active_state,
    _reconcile_active,
    _save_active_state,
)
from broadcastx.monitor import _MonitorBrowser


def test_reconcile_active_reports_new_and_missing_transitions():
    alice = HomeBroadcast("alice", "1vAxRkBbDRzKl", "https://x.com/i/broadcasts/1vAxRkBbDRzKl")
    bob = HomeBroadcast("bob", "1YqKDkWQnrAxV", "https://x.com/i/broadcasts/1YqKDkWQnrAxV")
    active = {alice.broadcast_id: alice}

    new, missing = _reconcile_active(active, [bob])

    assert new == [bob]
    assert missing == [alice]
    assert active == {bob.broadcast_id: bob}


def test_extract_fleetline_broadcasts_reads_headless_livevideo():
    payload = {
        "threads": [{
            "live_content": {
                "livevideo": {
                    "id": "1dKrPrZkvrYJX",
                    "state": "RUNNING",
                    "username": "Andres_OF__",
                }
            }
        }]
    }

    assert _extract_fleetline_broadcasts(payload) == [HomeBroadcast(
        "andres_of__",
        "1dKrPrZkvrYJX",
        "https://x.com/i/broadcasts/1dKrPrZkvrYJX",
    )]


def test_extract_fleetline_broadcasts_ignores_spaces_and_ended_video():
    payload = {
        "threads": [
            {"live_content": {"audiospace": {"id": "1YqKDkWQnrAxV", "state": "RUNNING"}}},
            {"live_content": {"livevideo": {
                "id": "1vAxRkBbDRzKl", "state": "ENDED", "username": "Alice",
            }}},
        ]
    }

    assert _extract_fleetline_broadcasts(payload) == []


def test_active_state_round_trip_supports_process_restart(tmp_path):
    path = tmp_path / "homemonitor_state.json"
    item = HomeBroadcast("alice", "1vAxRkBbDRzKl", "https://x.com/i/broadcasts/1vAxRkBbDRzKl")

    _save_active_state(path, {item.broadcast_id: item})

    assert _load_active_state(path) == {item.broadcast_id: item}
    assert not path.with_suffix(".json.tmp").exists()


class CollectHomeBroadcastsTests(IsolatedAsyncioTestCase):
    async def test_home_browser_retries_transient_profile_lock(self):
        browser = _HomeMonitorBrowser(None, Path("/tmp/broadcastx-home-test"), True)
        page = Mock()
        page.on = Mock()
        attempts = 0

        async def open_browser(_browser):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise Error(
                    "Failed to create a ProcessSingleton for your profile directory; "
                    "SingletonLock: File exists"
                )
            browser.page = page

        with patch.object(_MonitorBrowser, "open", open_browser), patch(
            "broadcastx.home_monitor.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            await browser.open()

        self.assertEqual(attempts, 2)
        sleep.assert_awaited_once_with(1)
        page.on.assert_called_once()

    async def test_home_browser_does_not_retry_unrelated_launch_error(self):
        browser = _HomeMonitorBrowser(None, Path("/tmp/broadcastx-home-test"), True)

        with patch.object(
            _MonitorBrowser, "open", new=AsyncMock(side_effect=Error("Chrome executable missing")),
        ), patch("broadcastx.home_monitor.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(Error, "Chrome executable missing"):
                await browser.open()

        sleep.assert_not_awaited()

    async def test_headless_collects_selected_user_from_fleetline(self):
        class Response:
            url = "https://x.com/i/api/2/fleetline"
            status = 200

            async def json(self):
                return {"threads": [{"live_content": {"livevideo": {
                    "id": "1dKrPrZkvrYJX",
                    "state": "RUNNING",
                    "twitter_username": "Andres_OF__",
                }}}]}

        class Page:
            def __init__(self):
                self.listener = None

            def on(self, _event, listener):
                self.listener = listener

            def remove_listener(self, _event, _listener):
                pass

            async def goto(self, *_args, **_kwargs):
                await self.listener(Response())
                return None

        found = await _collect_home_broadcasts(
            Page(), {"andres_of__"}, wait_ms=100, use_dom=False,
        )

        assert found == [HomeBroadcast(
            "andres_of__", "1dKrPrZkvrYJX", "https://x.com/i/broadcasts/1dKrPrZkvrYJX",
        )]

    async def test_home_browser_global_observer_ignores_viewer_badge_counts_429(self):
        page = Mock(route=AsyncMock())
        browser = _HomeMonitorBrowser(None, Path("/tmp/broadcastx-home-test"), True)

        await browser._prepare_page(page)
        observer = page.on.call_args.args[1]
        observer(type("Response", (), {
            "status": 429,
            "url": "https://x.com/i/api/graphql/example/ViewerBadgeCounts",
        })())

        self.assertFalse(browser.rate_limited.is_set())

        observer(type("Response", (), {
            "status": 429,
            "url": "https://x.com/i/api/graphql/example/HomeTimeline",
        })())

        self.assertTrue(browser.rate_limited.is_set())

    async def test_download_ended_broadcast_passes_url_browser_and_user_directory(self):
        item = HomeBroadcast(
            "alice", "1vAxRkBbDRzKl", "https://x.com/i/broadcasts/1vAxRkBbDRzKl"
        )
        download = Mock(return_value=Mock(
            success=True, output_file="archive/alice/videos/video.mp4", error=None,
        ))

        with patch("broadcastx.home_monitor.download_broadcast", download), patch(
            "broadcastx.home_monitor._append_event"
        ):
            succeeded = await _download_ended_broadcast(
                item, Path("archive"), "chrome", Path("events.json"), None,
            )

        self.assertTrue(succeeded)
        download.assert_called_once_with(
            item.url,
            output_dir=Path("archive/alice/videos"),
            browser="chrome",
            progress="stages",
            naming=None,
        )

    async def test_collects_broadcast_from_home_dom_and_broadcast_meta(self):
        class Response:
            url = "https://x.com/home"
            status = 200

        class Locator:
            def __init__(self, page, selector, index=0):
                self.page = page
                self.selector = selector
                self.index = index

            async def count(self):
                return 1 if self.selector.startswith("[aria-label^='Broadcast") else 0

            def nth(self, index):
                return Locator(self.page, self.selector, index)

            async def click(self):
                if self.selector == "button[aria-label='Open Live Screen']":
                    self.page.url = "https://x.com/i/broadcasts/1vAxRkBbDRzKl"

            async def wait_for(self, **_kwargs):
                pass

            async def get_attribute(self, name):
                if self.selector.startswith('meta[name=') and name == "content":
                    return "Alice"
                return None

        class Page:
            def __init__(self):
                self.listener = None
                self.url = "https://x.com/home"

            def on(self, _event, listener):
                self.listener = listener

            def remove_listener(self, _event, _listener):
                pass

            async def goto(self, *_args, **_kwargs):
                return Response()

            async def wait_for_selector(self, *_args, **_kwargs):
                pass

            def locator(self, selector):
                return Locator(self, selector)

            async def wait_for_url(self, *_args, **_kwargs):
                pass

            async def go_back(self, **_kwargs):
                self.url = "https://x.com/home"

        found = await _collect_home_broadcasts(Page(), {"alice"}, wait_ms=100)

        assert found == [HomeBroadcast(
            "alice", "1vAxRkBbDRzKl", "https://x.com/i/broadcasts/1vAxRkBbDRzKl"
        )]

    async def test_raises_on_graphql_429(self):
        class Response:
            url = "https://x.com/i/api/graphql/example/HomeTimeline"
            status = 429

            async def json(self):
                return {}

        class Page:
            def __init__(self):
                self.listener = None

            def on(self, _event, listener):
                self.listener = listener

            def remove_listener(self, _event, _listener):
                pass

            async def goto(self, *_args, **_kwargs):
                await self.listener(Response())
                return Response()

            async def wait_for_selector(self, *_args, **_kwargs):
                await asyncio.sleep(0)

        with self.assertRaises(HomeRateLimitError):
            await _collect_home_broadcasts(Page(), {"alice"}, wait_ms=100)

    async def test_ignores_unrelated_viewer_badge_counts_429(self):
        class Response:
            url = "https://x.com/i/api/graphql/example/ViewerBadgeCounts?variables=%7B%7D"
            status = 429

            async def json(self):
                return {}

        class Page:
            def __init__(self):
                self.listener = None

            def on(self, _event, listener):
                self.listener = listener

            def remove_listener(self, _event, _listener):
                pass

            async def goto(self, *_args, **_kwargs):
                await self.listener(Response())
                return None

            async def wait_for_selector(self, *_args, **_kwargs):
                pass

            def locator(self, _selector):
                return Mock(count=AsyncMock(return_value=0))

        with patch("broadcastx.home_monitor.console") as console:
            found = await _collect_home_broadcasts(Page(), {"alice"}, wait_ms=1)

        self.assertEqual(found, [])
        output = "\n".join(str(call) for call in console.print.call_args_list)
        self.assertIn("ignored unrelated HTTP 429", output)
