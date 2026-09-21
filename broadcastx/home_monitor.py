"""Monitor the single "Live on X" surface on X Home."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from .config import AppConfig, DEFAULT_BROWSER, extract_broadcast_id, user_output_dir
from .downloader import download_broadcast
from .monitor import (
    _MonitorBrowser,
    _append_event,
    _check_broadcast_status,
    _log_background_download_error,
    _now_iso,
)

console = Console()
HOME_IGNORED_429_OPERATIONS = {"ViewerBadgeCounts"}
HOME_PROFILE_LOCK_RETRY_DELAYS = (1, 3, 5)


def _is_profile_lock_error(error: Exception) -> bool:
    message = str(error).casefold()
    return any(marker in message for marker in (
        "processsingleton",
        "singletonlock",
        "profile is already in use",
        "profile directory is already in use",
    ))


class HomeRateLimitError(RuntimeError):
    """A rate limit from an endpoint required by Home monitoring."""

    def __init__(self, url: str):
        super().__init__(f"X returned HTTP 429 for {url}")
        self.url = url


class _HomeMonitorBrowser(_MonitorBrowser):
    """Add Home-specific rate-limit observation to the original monitor browser."""

    def __init__(self, playwright, profile_dir: Path, headless: bool):
        super().__init__(playwright, profile_dir, headless)
        self.rate_limited = asyncio.Event()
        self.rate_limit_url = "https://x.com/home"
        self._prepared_pages: set[int] = set()

    async def _prepare_page(self, page) -> None:
        if id(page) in self._prepared_pages:
            return
        self._prepared_pages.add(id(page))

        def observe(response) -> None:
            if getattr(response, "status", None) != 429:
                return
            if not _is_fatal_home_rate_limit(response.url):
                return
            self.rate_limit_url = response.url
            self.rate_limited.set()

        page.on("response", observe)

    async def open(self) -> None:
        from playwright.async_api import Error

        for attempt in range(len(HOME_PROFILE_LOCK_RETRY_DELAYS) + 1):
            try:
                await super().open()
                break
            except Error as error:
                if not _is_profile_lock_error(error) or attempt == len(
                    HOME_PROFILE_LOCK_RETRY_DELAYS
                ):
                    raise
                delay = HOME_PROFILE_LOCK_RETRY_DELAYS[attempt]
                console.print(
                    f"  Chrome profile is still in use; retrying in {delay}s...",
                    style="yellow",
                )
                await asyncio.sleep(delay)
        await self._prepare_page(self.page)

    async def recover(self, path: Path, username: str) -> None:
        await super().recover(path, username)
        if self.page is not None:
            await self._prepare_page(self.page)

    async def check(self, *args, **kwargs):
        if self.rate_limited.is_set():
            raise HomeRateLimitError(self.rate_limit_url)
        result = await super().check(*args, **kwargs)
        if self.rate_limited.is_set():
            raise HomeRateLimitError(self.rate_limit_url)
        return result


@dataclass(frozen=True)
class HomeBroadcast:
    """A live broadcast associated with its host account."""

    username: str
    broadcast_id: str
    url: str


def _is_fatal_home_rate_limit(url: str) -> bool:
    return not any(operation in url for operation in HOME_IGNORED_429_OPERATIONS)


def _extract_fleetline_broadcasts(data: Any) -> list[HomeBroadcast]:
    """Extract running live videos from X's Home fleetline payload; ignore Spaces."""
    found: dict[str, HomeBroadcast] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            livevideo = value.get("livevideo")
            if isinstance(livevideo, dict):
                broadcast_id = livevideo.get("id")
                username = livevideo.get("twitter_username") or livevideo.get("username")
                state = livevideo.get("state")
                if (
                    isinstance(broadcast_id, str)
                    and extract_broadcast_id(f"https://x.com/i/broadcasts/{broadcast_id}")
                    and isinstance(username, str)
                    and isinstance(state, str)
                    and state.casefold() in {"live", "running", "started", "playing"}
                ):
                    found[broadcast_id] = HomeBroadcast(
                        username=username.casefold(),
                        broadcast_id=broadcast_id,
                        url=f"https://x.com/i/broadcasts/{broadcast_id}",
                    )
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return list(found.values())


def _reconcile_active(
    active: dict[str, HomeBroadcast], current: list[HomeBroadcast],
) -> tuple[list[HomeBroadcast], list[HomeBroadcast]]:
    """Replace active state and return new and now-missing broadcasts."""
    current_by_id = {item.broadcast_id: item for item in current}
    new = [item for key, item in current_by_id.items() if key not in active]
    ended = [item for key, item in active.items() if key not in current_by_id]
    active.clear()
    active.update(current_by_id)
    return new, ended


async def _download_ended_broadcast(
    item: HomeBroadcast,
    output_root: Path,
    browser_name: str,
    event_path: Path,
    app_config: AppConfig | None,
) -> bool:
    try:
        result = await asyncio.to_thread(
            download_broadcast,
            item.url,
            output_dir=user_output_dir(output_root, item.username) / "videos",
            browser=browser_name,
            progress="stages",
            naming=app_config.download_for(item.username) if app_config else None,
        )
        _append_event(event_path, {
            "type": "download_finished",
            "username": item.username,
            "url": item.url,
            "broadcast_id": item.broadcast_id,
            "success": result.success,
            "output_file": result.output_file,
            "error": result.error,
            "finished_at": _now_iso(),
        })
        return result.success
    except Exception as error:
        _append_event(event_path, {
            "type": "download_failed",
            "username": item.username,
            "url": item.url,
            "broadcast_id": item.broadcast_id,
            "error": str(error),
            "finished_at": _now_iso(),
        })
        return False


def _load_active_state(path: Path) -> dict[str, HomeBroadcast]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("unsupported state format")
        active: dict[str, HomeBroadcast] = {}
        for value in data.get("active", []):
            item = HomeBroadcast(
                username=value["username"].casefold(),
                broadcast_id=value["broadcast_id"],
                url=value["url"],
            )
            active[item.broadcast_id] = item
        return active
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        console.print(
            f"Could not restore Home monitor state from {path}: {error}",
            style="yellow", markup=False,
        )
        return {}


def _save_active_state(path: Path, active: dict[str, HomeBroadcast]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "version": 1,
        "active": [
            {
                "username": item.username,
                "broadcast_id": item.broadcast_id,
                "url": item.url,
            }
            for item in sorted(active.values(), key=lambda item: item.broadcast_id)
        ],
    }
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


async def _collect_home_broadcasts(
    page, usernames: set[str], wait_ms: int = 8000, use_dom: bool = True,
) -> list[HomeBroadcast]:
    """Open each Home Broadcast card and read its URL and broadcaster metadata."""
    found: dict[str, HomeBroadcast] = {}
    rate_limited = asyncio.Event()
    rate_limit_url = "https://x.com/home"
    ignored_rate_limits: set[str] = set()
    fleetline_ready = asyncio.Event()

    async def handle_response(response):
        nonlocal rate_limit_url
        is_fleetline = response.url.split("?", 1)[0].endswith("/fleetline")
        if "/i/api/" not in response.url and "/graphql/" not in response.url and not is_fleetline:
            return
        if getattr(response, "status", None) == 429:
            if not _is_fatal_home_rate_limit(response.url):
                ignored_rate_limits.add(response.url)
                return
            rate_limit_url = response.url
            rate_limited.set()
            return
        if is_fleetline:
            try:
                for broadcast in _extract_fleetline_broadcasts(await response.json()):
                    if not usernames or broadcast.username in usernames:
                        found[broadcast.broadcast_id] = broadcast
                fleetline_ready.set()
            except Exception:
                return
    page.on("response", handle_response)
    try:
        navigation = await page.goto(
            "https://x.com/home", wait_until="domcontentloaded", timeout=30000,
        )
        if navigation is not None and navigation.status == 429:
            raise HomeRateLimitError(navigation.url)

        if use_dom:
            await page.wait_for_selector('h2:has-text("Live on X")', timeout=wait_ms)
            broadcast_selector = "[aria-label^='Broadcast,']"
            broadcast_count = await page.locator(broadcast_selector).count()
            for index in range(broadcast_count):
                await page.locator(broadcast_selector).nth(index).click()
                open_button = page.locator("button[aria-label='Open Live Screen']")
                await open_button.wait_for(state="visible", timeout=wait_ms)
                await open_button.click()
                await page.wait_for_url("**/i/broadcasts/*", timeout=wait_ms)

                url = page.url
                username = await page.locator(
                    'meta[name="twitter:text:broadcaster_username"]'
                ).get_attribute("content")
                broadcast_id = extract_broadcast_id(url)
                if username and broadcast_id:
                    normalized_username = username.casefold()
                    if not usernames or normalized_username in usernames:
                        found[broadcast_id] = HomeBroadcast(
                            username=normalized_username,
                            broadcast_id=broadcast_id,
                            url=url,
                        )

                if index + 1 < broadcast_count:
                    await page.go_back(wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_selector('h2:has-text("Live on X")', timeout=wait_ms)
                if rate_limited.is_set():
                    raise HomeRateLimitError(rate_limit_url)
        else:
            fleetline_wait = asyncio.create_task(fleetline_ready.wait())
            rate_limit_wait = asyncio.create_task(rate_limited.wait())
            _, pending = await asyncio.wait(
                {fleetline_wait, rate_limit_wait},
                timeout=wait_ms / 1000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        if rate_limited.is_set():
            raise HomeRateLimitError(rate_limit_url)
    finally:
        page.remove_listener("response", handle_response)

    for url in sorted(ignored_rate_limits):
        console.print(
            f"  ignored unrelated HTTP 429 from {url}", style="yellow", markup=False,
        )
    return list(found.values())


async def home_monitor(
    usernames: list[str],
    check_interval: int = 120,
    headless: bool = False,
    output_file: Path | str | None = None,
    browser: str = DEFAULT_BROWSER,
    download: bool = True,
    once: bool = False,
    app_config: AppConfig | None = None,
) -> None:
    """Monitor live Broadcast entries on Home without visiting user profiles."""
    if check_interval <= 0:
        raise ValueError("check_interval must be positive")

    from playwright.async_api import Error, async_playwright

    selected = {username.casefold() for username in usernames}
    output_root = app_config.output_root if app_config else Path("output")
    event_path = Path(output_file) if output_file else output_root / "homemonitor_events.json"
    state_path = output_root / "homemonitor_state.json"
    profile_dir = Path.home() / ".broadcastx" / "chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    restored = _load_active_state(state_path)
    unmanaged = {
        key: item for key, item in restored.items() if item.username not in selected
    }
    active = {
        key: item for key, item in restored.items() if item.username in selected
    }
    download_tasks: set[asyncio.Task] = set()
    downloading: set[str] = set()
    state_lock = asyncio.Lock()

    async def persist_active() -> None:
        async with state_lock:
            _save_active_state(state_path, {**unmanaged, **active})

    async def download_then_clear(item: HomeBroadcast) -> None:
        succeeded = await _download_ended_broadcast(
            item, output_root, browser, event_path, app_config,
        )
        downloading.discard(item.broadcast_id)
        if succeeded:
            active.pop(item.broadcast_id, None)
            await persist_active()

    console.print(f"[bold]Home monitoring {len(selected)} user(s) for live broadcasts[/bold]")
    console.print(f"  [dim]Home check interval: {check_interval}s[/dim]")
    console.print(f"  [dim]Browser mode: {'headless' if headless else 'visible'}[/dim]")
    if active:
        console.print(f"  [dim]Restored {len(active)} active broadcast(s) from state.[/dim]")

    async with async_playwright() as playwright:
        monitor_browser = _HomeMonitorBrowser(playwright, profile_dir, headless)
        try:
            await monitor_browser.open()
            next_check = asyncio.get_running_loop().time()
            while True:
                now = asyncio.get_running_loop().time()
                if now < next_check:
                    await asyncio.sleep(next_check - now)
                scheduled = next_check
                next_check = scheduled + check_interval
                started_at = _now_iso()
                started = asyncio.get_running_loop().time()
                console.print(f"{started_at} checking Live on X...")
                if monitor_browser.context is None:
                    await monitor_browser.open()
                check_succeeded = False
                try:
                    broadcasts = await _collect_home_broadcasts(
                        monitor_browser.page, selected, use_dom=not headless,
                    )
                    if monitor_browser.rate_limited.is_set():
                        raise HomeRateLimitError(monitor_browser.rate_limit_url)
                    check_succeeded = True
                except HomeRateLimitError as error:
                    console.print(
                        f"{_now_iso()} X returned HTTP 429; stopping homemonitor immediately.",
                        style="red", markup=False,
                    )
                    console.print(f"  {error.url}", style="red", markup=False)
                    raise SystemExit(1) from error
                except Error as error:
                    console.print(
                        f"{_now_iso()} Home check failed: {error}", style="yellow", markup=False,
                    )
                    broadcasts = []

                if check_succeeded:
                    new_broadcasts, missing_broadcasts = _reconcile_active(active, broadcasts)
                    # Keep missing broadcasts durable until their ended state and download finish.
                    for item in missing_broadcasts:
                        active[item.broadcast_id] = item
                    await persist_active()
                    for item in new_broadcasts:
                        console.print(f"  [green]Live found for @{item.username}:[/green] {item.url}")
                        _append_event(event_path, {
                            "type": "live_found",
                            "username": item.username,
                            "url": item.url,
                            "broadcast_id": item.broadcast_id,
                            "detected_at": _now_iso(),
                            "source": "home_live_on_x",
                        })
                    ended_broadcasts = []
                    try:
                        for item in missing_broadcasts:
                            if item.broadcast_id in downloading:
                                continue
                            status = await monitor_browser.check(
                                _check_broadcast_status,
                                item.url,
                                event_path,
                                item.username,
                                "home_live_end_check",
                                item.url,
                            )
                            if status is None or not status.is_ended:
                                continue
                            ended_broadcasts.append(item)
                    except HomeRateLimitError as error:
                        console.print(
                            f"{_now_iso()} X returned HTTP 429; "
                            "stopping homemonitor immediately.",
                            style="red", markup=False,
                        )
                        console.print(f"  {error.url}", style="red", markup=False)
                        raise SystemExit(1) from error

                    for item in ended_broadcasts:
                        console.print(f"  [cyan]Live ended for @{item.username}:[/cyan] {item.url}")
                        _append_event(event_path, {
                            "type": "live_ended",
                            "username": item.username,
                            "url": item.url,
                            "broadcast_id": item.broadcast_id,
                            "ended_at": _now_iso(),
                            "source": "home_live_on_x",
                        })
                        if download:
                            if item.broadcast_id not in downloading:
                                downloading.add(item.broadcast_id)
                                task = asyncio.create_task(download_then_clear(item))
                                download_tasks.add(task)
                                task.add_done_callback(download_tasks.discard)
                                task.add_done_callback(
                                    lambda finished, user=item.username:
                                    _log_background_download_error(user, finished)
                                )
                        else:
                            active.pop(item.broadcast_id, None)
                    await persist_active()
                    if not broadcasts and active:
                        console.print(
                            f"  [yellow]{len(active)} broadcast(s) absent from Home but not "
                            "confirmed ended; still tracking.[/yellow]"
                        )
                    elif not broadcasts:
                        console.print("  [dim]No monitored broadcasts found in Live on X.[/dim]")
                    elif not new_broadcasts and not missing_broadcasts:
                        console.print(
                            f"  [dim]{len(broadcasts)} active broadcast(s); no changes.[/dim]"
                        )
                else:
                    console.print("  [yellow]Active state preserved; this check was incomplete.[/yellow]")

                await monitor_browser.close()
                elapsed = asyncio.get_running_loop().time() - started
                console.print(f"{_now_iso()} finished checking Live on X in {elapsed:.1f}s.")

                if once:
                    return
                now = asyncio.get_running_loop().time()
                while next_check <= now:
                    next_check += check_interval
        finally:
            await monitor_browser.close()
