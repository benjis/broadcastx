"""
Scanner module — Discover broadcast links from a Twitter/X user's timeline.

Uses Playwright to intercept GraphQL network responses and extract broadcast
URLs from the JSON data, which is far more stable than parsing DOM elements.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import (
    BROADCAST_PATTERNS,
    DEFAULT_BROADCASTS_FILE,
    DEFAULT_HEADLESS,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_SCROLLS,
    DEFAULT_SCROLL_DELAY,
    GRAPHQL_ENDPOINTS,
    normalize_broadcast_url,
)

console = Console()


@dataclass
class BroadcastInfo:
    """Information about a discovered broadcast."""
    broadcast_id: str
    url: str
    tweet_text: str | None = None
    tweet_url: str | None = None
    created_at: str | None = None
    user_name: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in {
            "broadcast_id": self.broadcast_id,
            "url": self.url,
            "tweet_text": self.tweet_text,
            "tweet_url": self.tweet_url,
            "created_at": self.created_at,
            "user_name": self.user_name,
        }.items() if v is not None}


@dataclass
class ScanResult:
    """Result of scanning a user's timeline."""
    username: str
    broadcasts: list[BroadcastInfo] = field(default_factory=list)
    tweets_scanned: int = 0
    scrolls_performed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "total_broadcasts": len(self.broadcasts),
            "tweets_scanned": self.tweets_scanned,
            "broadcasts": [b.to_dict() for b in self.broadcasts],
        }


def _extract_broadcasts_from_response(data: dict, username: str) -> list[BroadcastInfo]:
    """Recursively search a Twitter GraphQL JSON response for broadcast URLs."""
    broadcasts = []
    seen_ids: set[str] = set()

    def _check_url(url_str: str, context: dict):
        for pattern in BROADCAST_PATTERNS:
            match = pattern.search(url_str)
            if match:
                bid = match.group(1)
                if bid not in seen_ids:
                    seen_ids.add(bid)
                    tweet_url = None
                    if context.get("tweet_id"):
                        tweet_url = f"https://x.com/{username}/status/{context['tweet_id']}"
                    broadcasts.append(BroadcastInfo(
                        broadcast_id=bid,
                        url=normalize_broadcast_url(url_str) or url_str,
                        tweet_text=context.get("tweet_text"),
                        tweet_url=tweet_url,
                        created_at=context.get("created_at"),
                        user_name=username,
                    ))

    def _walk(obj, context=None):
        if context is None:
            context = {}
        if isinstance(obj, dict):
            legacy = obj.get("legacy", {})
            if isinstance(legacy, dict) and legacy.get("full_text"):
                context = {
                    **context,
                    "tweet_text": legacy["full_text"],
                    "tweet_id": legacy.get("id_str") or obj.get("rest_id"),
                    "created_at": legacy.get("created_at"),
                }
            # Check URL entities
            entities = (legacy if isinstance(legacy, dict) else {}).get("entities", {})
            for url_entity in (entities.get("urls", []) if isinstance(entities, dict) else []):
                if isinstance(url_entity, dict):
                    _check_url(url_entity.get("expanded_url", ""), context)
            # Check card binding values
            card = obj.get("card", {})
            if isinstance(card, dict):
                bvs = card.get("legacy", {}).get("binding_values", [])
                for bv in (bvs if isinstance(bvs, list) else []):
                    if isinstance(bv, dict):
                        sval = bv.get("value", {}).get("string_value", "")
                        if sval:
                            _check_url(sval, context)
            for v in obj.values():
                _walk(v, context)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, context)

    _walk(data)
    return broadcasts


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
    response_count = 0
    last_new_data_time = time.time()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        console.print("[red]Playwright not installed.[/red]")
        console.print("Run: [bold]pip install playwright && playwright install chromium[/bold]")
        result.errors.append("Playwright not installed")
        return result

    # Persistent profile directory — cookies survive across runs
    profile_dir = Path.home() / ".broadcastx" / "chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"\n[bold]Scanning @{username} for broadcasts...[/bold]")
    console.print(f"  [dim]Using Chrome profile: {profile_dir}[/dim]\n")

    async with async_playwright() as p:
        # Use the real installed Chrome (channel="chrome") with a persistent
        # profile. This avoids the "browser not secure" error from Google
        # because it's the actual Chrome binary, not Chromium-for-Testing.
        # Cookies are saved to profile_dir so you only log in once.
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        async def handle_response(response):
            nonlocal response_count, last_new_data_time
            if not any(ep in response.url for ep in GRAPHQL_ENDPOINTS):
                return
            try:
                body = await response.json()
                response_count += 1
                new = _extract_broadcasts_from_response(body, username)
                for b in new:
                    if b.broadcast_id not in all_broadcasts:
                        all_broadcasts[b.broadcast_id] = b
                        last_new_data_time = time.time()
                        preview = b.tweet_text[:60] + "..." if b.tweet_text and len(b.tweet_text) > 60 else b.tweet_text or ""
                        console.print(f"  [green]🔴 Found:[/green] {b.url}  [dim]{preview}[/dim]")
            except Exception:
                pass

        page.on("response", handle_response)

        profile_url = f"https://x.com/{username}"
        console.print(f"  [dim]Opening {profile_url}[/dim]")
        try:
            await page.goto(profile_url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            console.print(f"[yellow]  Load timeout (continuing): {e}[/yellow]")

        # Handle login redirect — user logs in manually, cookies are persisted
        if "login" in page.url.lower():
            console.print()
            console.print("[yellow]⚠ Login required.[/yellow]")
            console.print("[yellow]  Log in to X/Twitter in the Chrome window.[/yellow]")
            console.print("[yellow]  Your session will be saved for future runs.[/yellow]")
            console.print("[yellow]  Press Enter here after you've logged in...[/yellow]")
            await asyncio.get_event_loop().run_in_executor(None, input)
            try:
                await page.goto(profile_url, wait_until="networkidle", timeout=30000)
            except Exception:
                pass

        await page.wait_for_timeout(3000)
        console.print(f"  [dim]Scrolling (max {max_scrolls})...[/dim]")

        for i in range(1, max_scrolls + 1):
            result.scrolls_performed = i
            if time.time() - last_new_data_time > idle_timeout and response_count > 5:
                console.print(f"\n  [dim]Idle for {idle_timeout}s, stopping.[/dim]")
                break
            await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            await page.wait_for_timeout(int(scroll_delay * 1000))
            if i % 10 == 0:
                console.print(f"  [dim]  Scroll {i}/{max_scrolls} | {len(all_broadcasts)} found[/dim]")

        result.broadcasts = list(all_broadcasts.values())
        await context.close()

    # Save results
    _save_results(result, output_file)
    _print_summary(result)
    return result


def _save_results(result: ScanResult, output_file: Path):
    """Save scan results to JSON, merging with any existing data."""
    output_file.parent.mkdir(parents=True, exist_ok=True)

    existing = {"broadcasts": []}
    if output_file.exists():
        try:
            existing = json.loads(output_file.read_text())
        except (json.JSONDecodeError, KeyError):
            pass

    existing_ids = {b["broadcast_id"] for b in existing.get("broadcasts", []) if isinstance(b, dict)}
    new_entries = [b.to_dict() for b in result.broadcasts if b.broadcast_id not in existing_ids]

    broadcasts_list = existing.get("broadcasts", []) + new_entries
    data = {
        "username": result.username,
        "total_broadcasts": len(broadcasts_list),
        "broadcasts": broadcasts_list,
    }

    output_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    console.print(f"\n  [dim]Saved to {output_file}[/dim]")


def _print_summary(result: ScanResult):
    """Print a summary table of scan results."""
    table = Table(title=f"Broadcasts from @{result.username}")
    table.add_column("ID", style="cyan")
    table.add_column("URL", style="blue")
    table.add_column("Tweet Preview", style="dim", max_width=50)
    table.add_column("Date", style="green")

    for b in result.broadcasts:
        preview = (b.tweet_text[:50] + "...") if b.tweet_text and len(b.tweet_text) > 50 else (b.tweet_text or "—")
        table.add_row(b.broadcast_id, b.url, preview, b.created_at or "—")

    console.print()
    console.print(table)
    console.print(f"\n[bold]{len(result.broadcasts)}[/bold] broadcast(s) found | {result.scrolls_performed} scrolls")
