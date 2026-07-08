"""Monitor a Twitter/X profile for live broadcasts, then download replays."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from .config import BROADCAST_ID_RE, DEFAULT_BROWSER, DEFAULT_OUTPUT_DIR, DEFAULT_VIDEOS_DIR, extract_broadcast_id, normalize_broadcast_url
from .downloader import download_broadcast

console = Console()

LIVE_STATES = {"live", "running", "started", "playing"}
ENDED_STATES = {"ended", "complete", "completed", "finished", "replay", "timed_out"}
BROADCAST_URL_RE = re.compile(
    rf"https?://(?:x|twitter)\.com/i/broadcasts/({BROADCAST_ID_RE})(?![A-Za-z0-9_])"
    rf"|/(?:i/)?broadcasts/({BROADCAST_ID_RE})(?![A-Za-z0-9_])"
)


@dataclass
class BroadcastCandidate:
    """A possible broadcast found in X web data."""

    broadcast_id: str
    url: str
    state: str | None = None
    title: str | None = None


@dataclass
class BroadcastStatus:
    """Current status of a broadcast page."""

    url: str
    state: str
    raw_state: str | None = None

    @property
    def is_live(self) -> bool:
        return self.state == "live"

    @property
    def is_ended(self) -> bool:
        return self.state == "ended"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _has_auth_cookies(cookies: list[dict]) -> bool:
    names = {cookie.get("name") for cookie in cookies}
    return "auth_token" in names and "ct0" in names


def _looks_logged_out(text: str) -> bool:
    lowered = text.lower()
    logged_out_markers = (
        "happening now",
        "email or username",
        "new to x?",
        "sign up now",
        "don't miss what's happening",
    )
    auth_actions = ("log in", "sign up", "create account", "continue with")
    return any(marker in lowered for marker in logged_out_markers) and any(action in lowered for action in auth_actions)


def _state_bucket(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    if lowered in LIVE_STATES or "live" in lowered or "running" in lowered:
        return "live"
    if lowered in ENDED_STATES or "ended" in lowered or "complete" in lowered or "replay" in lowered:
        return "ended"
    return None


def _is_live_candidate_status(status: str) -> bool:
    return status == "live"


def _string_from_binding_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    for key in ("string_value", "scribe_key", "url", "id_str"):
        if isinstance(value.get(key), str):
            return value[key]
    if isinstance(value.get("image_value"), dict):
        return value["image_value"].get("url")
    if isinstance(value.get("user_value"), dict):
        return value["user_value"].get("id_str")
    return None


def _extract_candidates(data: Any) -> list[BroadcastCandidate]:
    """Find broadcast URLs and card states in arbitrary X web JSON."""
    found: dict[str, BroadcastCandidate] = {}

    def add(url: str, state: str | None = None, title: str | None = None) -> None:
        normalized = normalize_broadcast_url(url)
        if not normalized:
            match = BROADCAST_URL_RE.search(url)
            if not match:
                return
            broadcast_id = match.group(1) or match.group(2)
            normalized = f"https://x.com/i/broadcasts/{broadcast_id}"
        broadcast_id = extract_broadcast_id(normalized)
        if not broadcast_id:
            return
        current = found.get(broadcast_id)
        if current is None:
            found[broadcast_id] = BroadcastCandidate(broadcast_id, normalized, state, title)
            return
        current.state = state or current.state
        current.title = title or current.title

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            card = obj.get("card")
            if isinstance(card, dict):
                walk(card)

            legacy = obj.get("legacy")
            if isinstance(legacy, dict):
                binding_values = legacy.get("binding_values")
                if isinstance(binding_values, list):
                    values: dict[str, str] = {}
                    for item in binding_values:
                        if not isinstance(item, dict) or "key" not in item:
                            continue
                        value = _string_from_binding_value(item.get("value"))
                        if value is not None:
                            values[item["key"]] = value
                    url = values.get("broadcast_url") or values.get("url")
                    broadcast_id = values.get("broadcast_id") or values.get("id")
                    if url or broadcast_id:
                        add(
                            url or f"https://x.com/i/broadcasts/{broadcast_id}",
                            values.get("broadcast_state"),
                            values.get("broadcast_title") or values.get("title"),
                        )

            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, str):
            for match in BROADCAST_URL_RE.finditer(obj):
                broadcast_id = match.group(1) or match.group(2)
                add(f"https://x.com/i/broadcasts/{broadcast_id}")

    walk(data)
    return list(found.values())


async def _collect_profile_candidates(page, username: str, wait_ms: int = 5000) -> list[BroadcastCandidate]:
    responses: list[Any] = []

    async def handle_response(response):
        if "/i/api/" not in response.url and "/graphql/" not in response.url:
            return
        try:
            responses.append(await response.json())
        except Exception:
            return

    page.on("response", handle_response)
    try:
        await page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(wait_ms)
        hrefs = await page.eval_on_selector_all(
            "a[href*='/i/broadcasts/'], a[href*='/broadcasts/']",
            "els => els.map(e => e.href)",
        )
    finally:
        page.remove_listener("response", handle_response)

    candidates: dict[str, BroadcastCandidate] = {}
    for href in hrefs:
        for candidate in _extract_candidates(href):
            candidates[candidate.broadcast_id] = candidate
    for payload in responses:
        for candidate in _extract_candidates(payload):
            candidates[candidate.broadcast_id] = candidate
    return list(candidates.values())


async def _check_broadcast_status(page, url: str, wait_ms: int = 5000) -> BroadcastStatus:
    states: list[str] = []

    async def handle_response(response):
        if "/i/api/" not in response.url and "/graphql/" not in response.url:
            return
        try:
            for candidate in _extract_candidates(await response.json()):
                if candidate.state:
                    states.append(candidate.state)
        except Exception:
            return

    page.on("response", handle_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(wait_ms)
        text = await page.locator("body").inner_text(timeout=5000)
    finally:
        page.remove_listener("response", handle_response)

    for raw_state in states:
        bucket = _state_bucket(raw_state)
        if bucket:
            return BroadcastStatus(url=url, state=bucket, raw_state=raw_state)

    lowered = text.lower()
    if "ended " in lowered or lowered.startswith("ended") or "broadcast has ended" in lowered:
        return BroadcastStatus(url=url, state="ended")
    if "watch live" in lowered or re.search(r"\blive\b", lowered):
        return BroadcastStatus(url=url, state="live")
    return BroadcastStatus(url=url, state="unknown")


def _append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, list):
                events = loaded
        except json.JSONDecodeError:
            events = []

    events.append(event)

    path.write_text(json.dumps(events, indent=2, ensure_ascii=False))

def _log_background_download_error(username: str, task):
    """Log any unhandled exception from a background download task."""
    try:
        task.result()
    except Exception:
        console.print("[red]Background download for @" + username + " failed with unhandled error:[/red]")
        console.print_exception()
async def _background_download(
    url: str,
    output_dir: Path,
    browser: str,
    output_path: Path,
    username: str,
    broadcast_id: str,
) -> None:
    """Download a broadcast in a background thread; log the result event."""
    try:
        result = await asyncio.to_thread(
            download_broadcast, url, output_dir=output_dir, browser=browser
        )
        _append_event(output_path, {
            "type": "download_finished",
            "username": username,
            "url": url,
            "broadcast_id": broadcast_id,
            "success": result.success,
            "output_file": result.output_file,
            "error": result.error,
            "finished_at": _now_iso(),
        })
    except Exception as e:
        _append_event(output_path, {
            "type": "download_failed",
            "username": username,
            "url": url,
            "broadcast_id": broadcast_id,
            "error": str(e),
            "finished_at": _now_iso(),
        })





async def _ensure_logged_in(context, page, headless: bool) -> bool:
    cookies = await context.cookies("https://x.com")
    if _has_auth_cookies(cookies):
        return True

    await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)
    try:
        text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        text = ""

    cookies = await context.cookies("https://x.com")
    if _has_auth_cookies(cookies) and not _looks_logged_out(text):
        return True

    if headless:
        console.print("[red]X login required, but monitor is running headless.[/red]")
        console.print("  Run once without [bold]--headless[/bold], log in to X in the Chrome window, then rerun headless.")
        return False

    console.print()
    console.print("[yellow]X login required.[/yellow]")
    console.print("[yellow]  Log in to X/Twitter in the Chrome window.[/yellow]")
    console.print("[yellow]  Your session will be saved for future monitor runs.[/yellow]")
    console.print("[yellow]  Press Enter here after you've logged in...[/yellow]")
    await asyncio.get_event_loop().run_in_executor(None, input)

    cookies = await context.cookies("https://x.com")
    if _has_auth_cookies(cookies):
        return True

    console.print("[red]Still not logged in; cannot monitor live broadcasts.[/red]")
    return False


async def monitor_user(
    username: str,
    check_interval: int = 30 * 60,
    live_interval: int = 5 * 60,
    headless: bool = False,
    output_file: Path | str | None = None,
    output_dir: Path | str | None = None,
    browser: str = DEFAULT_BROWSER,
    download: bool = True,
    once: bool = False,
) -> None:
    """Monitor a user profile for a current live broadcast."""
    await monitor_users(
        usernames=[username],
        check_interval=check_interval,
        live_interval=live_interval,
        headless=headless,
        output_file=output_file,
        output_dir=output_dir,
        browser=browser,
        download=download,
        once=once,
    )


async def monitor_users(
    usernames: list[str],
    check_interval: int = 30 * 60,
    live_interval: int = 5 * 60,
    headless: bool = False,
    output_file: Path | str | None = None,
    output_dir: Path | str | None = None,
    browser: str = DEFAULT_BROWSER,
    download: bool = True,
    once: bool = False,
) -> None:
    """Monitor multiple user profiles for live broadcasts using a single Chromium profile."""
    usernames = [u.lstrip("@") for u in usernames]
    output_path = Path(output_file) if output_file else DEFAULT_OUTPUT_DIR / "monitor_events.json"
    video_dir = Path(output_dir) if output_dir else DEFAULT_VIDEOS_DIR
    seen_completed: set[str] = set()
    live_candidates: dict[str, BroadcastCandidate] = {}

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        console.print("[red]Playwright not installed.[/red]")
        console.print("Run: [bold]uv sync && uv run playwright install chromium[/bold]")
        return

    profile_dir = Path.home() / ".broadcastx" / "chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Monitoring {len(usernames)} user(s) for live broadcasts[/bold]")
    console.print(f"  [dim]Users: {', '.join('@' + u for u in usernames)}[/dim]")
    console.print(f"  [dim]Check interval: {check_interval}s | live interval: {live_interval}s[/dim]")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            if not await _ensure_logged_in(context, page, headless):
                raise SystemExit(1)

            while True:
                console.print(f"[dim]{_now_iso()} checking {len(usernames)} user(s)...[/dim]")

                for username in usernames:
                    if username in live_candidates:
                        continue

                    console.print(f"  [dim]Checking @{username}...[/dim]")
                    candidates = await _collect_profile_candidates(page, username)

                    for candidate in candidates:
                        if candidate.broadcast_id in seen_completed:
                            continue
                        if _state_bucket(candidate.state) == "ended":
                            continue
                        status = await _check_broadcast_status(page, candidate.url)
                        if _is_live_candidate_status(status.state):
                            live_candidates[username] = candidate
                            console.print(f"  [green]Live found for @{username}:[/green] {candidate.url}")
                            _append_event(output_path, {
                                "type": "live_found",
                                "username": username,
                                "url": candidate.url,
                                "broadcast_id": candidate.broadcast_id,
                                "title": candidate.title,
                                "detected_at": _now_iso(),
                            })
                            break

                if not live_candidates:
                    console.print("  [dim]No active broadcasts found.[/dim]")
                    if once:
                        break
                    await asyncio.sleep(check_interval)
                    continue

                for username in list(live_candidates.keys()):
                    candidate = live_candidates[username]
                    status = await _check_broadcast_status(page, candidate.url)
                    console.print(f"  [dim]@{username} {candidate.broadcast_id}: {status.state}[/dim]")

                    if status.is_ended:
                        _append_event(output_path, {
                            "type": "live_ended",
                            "username": username,
                            "url": candidate.url,
                            "broadcast_id": candidate.broadcast_id,
                            "ended_at": _now_iso(),
                        })
                        if download:
                            task = asyncio.create_task(
                                _background_download(
                                    candidate.url,
                                    video_dir,
                                    browser,
                                    output_path,
                                    username,
                                    candidate.broadcast_id,
                                )
                            )
                            task.add_done_callback(lambda t, u=username: _log_background_download_error(u, t))
                        seen_completed.add(candidate.broadcast_id)
                        del live_candidates[username]

                if live_candidates:
                    await asyncio.sleep(live_interval)
                elif once:
                    break
                else:
                    await asyncio.sleep(check_interval)
        finally:
            await context.close()
