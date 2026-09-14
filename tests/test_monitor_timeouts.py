import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import TimeoutError

from broadcastx.monitor import BroadcastCandidate, BroadcastStatus, _MonitorBrowser, monitor_users


class MonitorTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def run_monitor(self, profiles, statuses, once=True):
        context = Mock()
        context.pages = [Mock()]
        context.pages[0].close = AsyncMock()
        context.new_page = AsyncMock(return_value=context.pages[0])
        context.close = AsyncMock()
        playwright = Mock()
        playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
        manager = Mock()
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock(return_value=False)
        with tempfile.TemporaryDirectory() as directory, patch(
            'playwright.async_api.async_playwright', return_value=manager
        ), patch('broadcastx.monitor._ensure_logged_in', new=AsyncMock(return_value=True)), patch(
            'broadcastx.monitor._collect_profile_candidates', new=AsyncMock(side_effect=profiles)
        ) as collect, patch(
            'broadcastx.monitor._check_broadcast_status', new=AsyncMock(side_effect=statuses)
        ) as check, patch('broadcastx.monitor._append_event') as events, patch(
            'broadcastx.monitor.console'
        ) as console, patch(
            'broadcastx.monitor.asyncio.sleep', new=AsyncMock(side_effect=[None, asyncio.CancelledError()])
        ), patch('broadcastx.monitor.CHECK_RETRY_DELAYS', ()):
            try:
                await monitor_users(
                    ['alice', 'bob'], output_file=Path(directory) / 'events.json',
                    output_dir=directory, download=False, once=once,
                )
            except asyncio.CancelledError:
                if once:
                    raise
            return collect, check, events, console

    async def test_profile_timeout_does_not_skip_other_users(self):
        collect, _, events, console = await self.run_monitor([
            TimeoutError('navigation timed out'), []
        ], [])
        self.assertEqual([call.args[1] for call in collect.await_args_list], ['alice', 'bob'])
        failure = events.call_args_list[0].args[1]
        self.assertEqual(failure['type'], 'check_failed')
        self.assertEqual(failure['username'], 'alice')
        self.assertEqual(failure['operation'], 'profile_check')
        self.assertIn('failed_at', failure)
        self.assertIn('navigation timed out', failure['error'])
        self.assertTrue(any('alice' in str(call) for call in console.print.call_args_list))

    async def test_candidate_timeout_does_not_skip_other_candidates(self):
        first = BroadcastCandidate('1vAxRkBbDRzKl', 'https://x.com/i/broadcasts/1vAxRkBbDRzKl')
        second = BroadcastCandidate('1YqKDkWQnrAxV', 'https://x.com/i/broadcasts/1YqKDkWQnrAxV')
        _, check, events, _ = await self.run_monitor([[first, second], []], [
            TimeoutError('status timed out'), BroadcastStatus(second.url, 'ended')
        ])
        self.assertEqual(check.await_count, 2)
        failure = events.call_args_list[0].args[1]
        self.assertEqual(failure['operation'], 'broadcast_check')
        self.assertEqual(failure['url'], first.url)

    async def test_live_timeout_keeps_state_until_confirmed_ended(self):
        candidate = BroadcastCandidate('1vAxRkBbDRzKl', 'https://x.com/i/broadcasts/1vAxRkBbDRzKl')
        collect, check, events, _ = await self.run_monitor([[candidate], [], []], [
            BroadcastStatus(candidate.url, 'live'), TimeoutError('status timed out'),
            BroadcastStatus(candidate.url, 'ended'),
        ], once=False)
        self.assertEqual([call.args[1] for call in collect.await_args_list], ['alice', 'bob', 'bob'])
        self.assertEqual(check.await_count, 3)
        self.assertEqual([call.args[1]['type'] for call in events.call_args_list], [
            'live_found', 'check_failed', 'page_replaced', 'live_ended'
        ])

    async def test_browser_restart_preserves_known_live_candidate(self):
        candidate = BroadcastCandidate('1vAxRkBbDRzKl', 'https://x.com/i/broadcasts/1vAxRkBbDRzKl')
        collect, check, events, _ = await self.run_monitor([
            [candidate], TimeoutError('profile failed'), []
        ], [
            BroadcastStatus(candidate.url, 'live'), TimeoutError('live check failed'),
            BroadcastStatus(candidate.url, 'ended'),
        ], once=False)
        self.assertEqual([call.args[1] for call in collect.await_args_list], ['alice', 'bob', 'bob'])
        self.assertEqual(check.await_count, 3)
        self.assertEqual([call.args[1]['type'] for call in events.call_args_list], [
            'live_found', 'check_failed', 'page_replaced', 'check_failed',
            'browser_restarted', 'live_ended',
        ])


class MonitorBrowserRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = Mock()
        self.old_page = Mock(close=AsyncMock())
        self.new_page = Mock(close=AsyncMock())
        self.context.pages = [self.old_page]
        self.context.new_page = AsyncMock(return_value=self.new_page)
        self.context.close = AsyncMock()
        self.restarted_context = Mock()
        self.restarted_context.pages = [Mock(close=AsyncMock())]
        self.restarted_context.close = AsyncMock()
        self.playwright = Mock()
        self.launch = AsyncMock(side_effect=[self.context, self.restarted_context])
        self.playwright.chromium.launch_persistent_context = self.launch
        self.browser = _MonitorBrowser(self.playwright, Path('/tmp/broadcastx-test-profile'), True)
        self.patches = [
            patch('broadcastx.monitor._ensure_logged_in', new=AsyncMock(return_value=True)),
            patch('broadcastx.monitor._append_event'), patch('broadcastx.monitor.console'),
            patch('broadcastx.monitor.asyncio.sleep', new=AsyncMock()),
            patch('random.uniform', return_value=0.5),
        ]
        self.login, self.events, _, self.sleep, _ = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        await self.browser.open()

    async def check(self, callback):
        return await self.browser.check(
            callback, 'alice', Path('/tmp/events.json'), 'alice', 'profile_check', 'https://x.com/alice'
        )

    async def test_transient_failure_retries_without_recovery(self):
        callback = AsyncMock(side_effect=[TimeoutError('slow'), []])
        self.assertEqual(await self.check(callback), [])
        self.assertEqual(callback.await_count, 2)
        self.sleep.assert_awaited_once_with(5.5)
        self.context.new_page.assert_not_awaited()

    async def test_exhaustion_replaces_page_then_restarts_browser(self):
        callback = AsyncMock(side_effect=TimeoutError('slow'))
        self.assertIsNone(await self.check(callback))
        self.assertEqual(callback.await_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.await_args_list], [5.5, 15.5])
        self.old_page.close.assert_awaited_once()
        self.assertIs(self.browser.page, self.new_page)
        self.assertIsNone(await self.check(callback))
        self.context.close.assert_awaited_once()
        self.assertIs(self.browser.page, self.restarted_context.pages[0])
        self.assertEqual(self.launch.await_count, 2)
        self.assertEqual(self.launch.await_args_list[0].kwargs, self.launch.await_args_list[1].kwargs)
        self.assertEqual(self.login.await_count, 2)

    async def test_success_resets_recovery_escalation(self):
        await self.check(AsyncMock(side_effect=TimeoutError('slow')))
        await self.check(AsyncMock(return_value=[]))
        await self.check(AsyncMock(side_effect=TimeoutError('slow')))
        self.assertEqual(self.context.new_page.await_count, 2)
        self.context.close.assert_not_awaited()

    async def test_failed_recovery_is_retried_on_next_check(self):
        from playwright.async_api import Error
        self.context.new_page.side_effect = Error('browser disconnected')
        self.assertIsNone(await self.check(AsyncMock(side_effect=TimeoutError('slow'))))
        self.assertIsNone(self.browser.page)
        callback = AsyncMock(return_value=[])
        self.assertEqual(await self.check(callback), [])
        self.assertIs(callback.await_args.args[0], self.restarted_context.pages[0])

    async def test_cancellation_is_not_retried(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.check(AsyncMock(side_effect=asyncio.CancelledError()))
        self.sleep.assert_not_awaited()
        self.context.new_page.assert_not_awaited()
