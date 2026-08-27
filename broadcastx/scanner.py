"""
Scanner module — Discover broadcast links from a Twitter/X user's timeline.

Uses Playwright to intercept GraphQL network responses and extract broadcast
URLs from the JSON data, which is far more stable than parsing DOM elements.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import (
    DEFAULT_BROADCASTS_FILE,
    DEFAULT_HEADLESS,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_SCROLLS,
    DEFAULT_SCROLL_DELAY,
)

console = Console()


from .models import (
    BroadcastInfo,
    extract_broadcasts_from_response,
    extract_post_ids_from_response,
)
from .x_auth import has_auth_cookies, looks_logged_out

@dataclass
class ScanResult:
    """Result of scanning a user's timeline."""
    username: str
    broadcasts: list[BroadcastInfo] = field(default_factory=list)
    tweets_scanned: int = 0
    scrolls_performed: int = 0
    responses_seen: int = 0
    stop_reason: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class TimelineProgress:
    """Progress derived from target-authored post IDs, not broadcast matches."""

    seen_post_ids: set[str] = field(default_factory=set)
    last_progress_at: float = field(default_factory=time.monotonic)
    responses_seen: int = 0
    responses_parsed: int = 0
    parse_failures: int = 0

    def observe(self, post_ids: set[str], now: float | None = None) -> int:
        new_ids = post_ids - self.seen_post_ids
        if new_ids:
            self.seen_post_ids.update(new_ids)
            self.last_progress_at = time.monotonic() if now is None else now
        return len(new_ids)

    def idle_seconds(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, current - self.last_progress_at)

    def reset_idle_timer(self, now: float | None = None) -> None:
        self.last_progress_at = time.monotonic() if now is None else now


def _is_graphql_response(url: str) -> bool:
    """Match the transport path without depending on an operation name."""
    if "/graphql/" not in url:
        return False
    path = url.split("?", 1)[0]
    tail = path.split("/graphql/", 1)[1].strip("/")
    parts = tail.split("/")
    return len(parts) >= 2 and not parts[-1].endswith(".json")


def _graphql_operation(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "unknown"


def _record_error(result: ScanResult, message: str) -> None:
    if message not in result.errors and len(result.errors) < 20:
        result.errors.append(message)


async def _requires_login(context, page) -> bool:
    cookies = await context.cookies()
    try:
        page_text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        page_text = ""
    return not has_auth_cookies(cookies) or looks_logged_out(page_text)


async def scan_user(
    username: str,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    scroll_delay: float = DEFAULT_SCROLL_DELAY,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    headless: bool = DEFAULT_HEADLESS,
    output_file: Path | str | None = None,
) -> ScanResult:
    """
    Scan a Twitter/X user's timeline for broadcast links.

    Opens the user's profile in Playwright, scrolls through their timeline,
    and intercepts GraphQL responses to find broadcast URLs.

    Uses the real installed Chrome (not Chromium-for-Testing) with a
    persistent profile directory so login cookies are preserved between runs.
    """
    username = username.lstrip("@")
    if output_file is None:
        output_file = DEFAULT_BROADCASTS_FILE
    output_file = Path(output_file)

    result = ScanResult(username=username)
    all_broadcasts: dict[str, BroadcastInfo] = {}
    progress = TimelineProgress()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        console.print("[red]Playwright not installed.[/red]")
        console.print("Run: [bold]uv sync && uv run playwright install chromium[/bold]")
        result.errors.append("Playwright not installed")
        return result

    # Persistent profile directory — cookies survive across runs
    profile_dir = Path.home() / ".broadcastx" / "chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"\n[bold]Scanning @{username} for broadcasts...[/bold]")
    console.print(f"  [dim]Using Chrome profile: {profile_dir}[/dim]\n")

    context = None
    try:
        async with async_playwright() as p:
            try:
                # Use the real installed Chrome with a persistent profile so
                # X login cookies survive between runs.
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    channel="chrome",
                    headless=headless,
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = context.pages[0] if context.pages else await context.new_page()

                async def handle_response(response):
                    if not _is_graphql_response(response.url):
                        return
                    progress.responses_seen += 1
                    operation = _graphql_operation(response.url)
                    if response.status >= 400:
                        _record_error(
                            result,
                            f"GraphQL HTTP {response.status} from {operation}",
                        )
                    try:
                        body = await response.json()
                    except Exception as exc:
                        progress.parse_failures += 1
                        _record_error(
                            result,
                            f"Could not parse GraphQL response from {operation}: "
                            f"{type(exc).__name__}",
                        )
                        return

                    progress.responses_parsed += 1
                    progress.observe(extract_post_ids_from_response(body, username))
                    result.tweets_scanned = len(progress.seen_post_ids)

                    for broadcast in extract_broadcasts_from_response(body, username):
                        if broadcast.broadcast_id in all_broadcasts:
                            continue
                        all_broadcasts[broadcast.broadcast_id] = broadcast
                        console.print(f"  [green]🔴 Found:[/green] {broadcast.url}")

                page.on("response", handle_response)

                profile_url = f"https://x.com/{username}"
                console.print(f"  [dim]Opening {profile_url}[/dim]")
                try:
                    await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:
                    console.print(f"[yellow]  Load timeout (continuing): {exc}[/yellow]")

                if await _requires_login(context, page):
                    if headless:
                        result.stop_reason = "login_required"
                        _record_error(result, "X login required; rerun without --headless")
                        return result
                    console.print()
                    console.print("[yellow]⚠ Login required.[/yellow]")
                    console.print("[yellow]  Log in to X in the Chrome window.[/yellow]")
                    console.print("[yellow]  Your session will be saved for future runs.[/yellow]")
                    console.print("[yellow]  Press Enter here after you've logged in...[/yellow]")
                    await asyncio.get_running_loop().run_in_executor(None, input)
                    try:
                        await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                    except Exception as exc:
                        console.print(f"[yellow]  Load timeout (continuing): {exc}[/yellow]")
                    if await _requires_login(context, page):
                        result.stop_reason = "login_required"
                        _record_error(result, "X login was not completed")
                        return result

                await page.wait_for_timeout(3000)
                progress.reset_idle_timer()
                console.print(f"  [dim]Scrolling (max {max_scrolls})...[/dim]")

                for scroll_number in range(1, max_scrolls + 1):
                    result.scrolls_performed = scroll_number
                    await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                    await page.wait_for_timeout(int(scroll_delay * 1000))

                    if progress.idle_seconds() >= idle_timeout:
                        result.stop_reason = "timeline_idle"
                        console.print(
                            f"\n  [dim]No new target posts for {idle_timeout}s, "
                            "stopping.[/dim]"
                        )
                        break
                    if scroll_number % 10 == 0:
                        console.print(
                            f"  [dim]  Scroll {scroll_number}/{max_scrolls} | "
                            f"{len(progress.seen_post_ids)} posts | "
                            f"{len(all_broadcasts)} broadcasts[/dim]"
                        )
                else:
                    result.stop_reason = "max_scrolls"
            finally:
                if context is not None:
                    try:
                        await context.close()
                    except Exception as exc:
                        _record_error(
                            result,
                            f"Could not close browser context: {type(exc).__name__}",
                        )
    except Exception as exc:
        result.stop_reason = result.stop_reason or "error"
        _record_error(result, f"Scan failed: {type(exc).__name__}: {exc}")
    finally:
        result.broadcasts = list(all_broadcasts.values())
        result.tweets_scanned = len(progress.seen_post_ids)
        result.responses_seen = progress.responses_seen
        if result.stop_reason != "login_required":
            if progress.responses_seen == 0:
                _record_error(result, "No GraphQL responses were captured")
            elif progress.responses_parsed == 0:
                _record_error(result, "No GraphQL responses could be parsed")
            elif not progress.seen_post_ids:
                _record_error(result, "No target-authored posts were found in GraphQL responses")
        _save_results(result, output_file)
        _print_summary(result)
    return result


def _save_results(result: ScanResult, output_file: Path):
    """Save scan results to JSON, merging with any existing data."""
    output_file.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {"broadcasts": []}
    if output_file.exists():
        try:
            existing = json.loads(output_file.read_text())
            if not isinstance(existing, dict):
                existing = {"broadcasts": []}
        except (json.JSONDecodeError, OSError):
            existing = {"broadcasts": []}

    existing_broadcasts = existing.get("broadcasts", [])
    if not isinstance(existing_broadcasts, list):
        existing_broadcasts = []
    existing_ids = {
        broadcast.get("broadcast_id")
        for broadcast in existing_broadcasts
        if isinstance(broadcast, dict) and broadcast.get("broadcast_id")
    }
    new_entries = [b.to_dict() for b in result.broadcasts if b.broadcast_id not in existing_ids]

    broadcasts_list = existing_broadcasts + new_entries
    data = {
        "username": result.username,
        "total_broadcasts": len(broadcasts_list),
        "broadcasts": broadcasts_list,
    }

    temporary = output_file.with_name(f".{output_file.name}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    temporary.replace(output_file)
    console.print(f"\n  [dim]Saved to {output_file}[/dim]")


def _print_summary(result: ScanResult):
    """Print a summary table of scan results."""
    table = Table(title=f"Broadcasts from @{result.username}")
    table.add_column("ID", style="cyan")
    table.add_column("URL", style="blue")
    table.add_column("Date", style="green")

    displayed = result.broadcasts[:20]
    for b in displayed:
        table.add_row(b.broadcast_id, b.url, b.created_at or "—")

    console.print()
    console.print(table)
    if len(result.broadcasts) > len(displayed):
        console.print(
            f"[dim]  Showing {len(displayed)} of {len(result.broadcasts)} broadcasts; "
            "all results are in the JSON output.[/dim]"
        )
    console.print(
        f"\n[bold]{len(result.broadcasts)}[/bold] broadcast(s) found | "
        f"{result.tweets_scanned} posts | {result.scrolls_performed} scrolls | "
        f"{result.responses_seen} GraphQL responses | "
        f"stop: {result.stop_reason or 'unknown'}"
    )
    for error in result.errors:
        console.print(f"[yellow]  ⚠ {error}[/yellow]")
