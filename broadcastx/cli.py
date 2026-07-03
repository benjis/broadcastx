"""
BroadcastX CLI — Discover and download X/Twitter broadcast videos.

Usage:
    broadcastx scan @username
    broadcastx download https://x.com/i/broadcasts/...
    broadcastx download --from broadcasts.json
"""

import asyncio
from pathlib import Path

import click
from rich.console import Console

from . import __version__
from .config import DEFAULT_BROADCASTS_FILE, DEFAULT_BROWSER, DEFAULT_VIDEOS_DIR
from .downloader import check_ffmpeg, check_yt_dlp, download_all, download_broadcast
from .monitor import monitor_user, monitor_users
from .pause_detector import detect_pauses, pause_report, trim_intervals
from .scanner import scan_user
from .scrape_broadcasts import scrape_broadcasts

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="broadcastx")
def main():
    """BroadcastX — Discover and download X/Twitter broadcast videos."""
    pass


@main.command()
@click.argument("username")
@click.option("--max-scrolls", "-n", default=100, help="Maximum scroll actions (default: 100)")
@click.option("--scroll-delay", "-d", default=2.0, help="Delay between scrolls in seconds (default: 2.0)")
@click.option("--idle-timeout", "-t", default=10.0, help="Stop after N seconds with no new data (default: 10)")
@click.option("--output", "-o", default=None, help="Output JSON file path")
@click.option("--headless/--no-headless", default=False, help="Run browser headless (default: visible)")
def scan(username, max_scrolls, scroll_delay, idle_timeout, output, headless):
    """Scan a user's timeline for broadcast links.

    USERNAME can be with or without @ (e.g., @elonmusk or elonmusk).
    """
    asyncio.run(scan_user(
        username=username,
        max_scrolls=max_scrolls,
        scroll_delay=scroll_delay,
        idle_timeout=idle_timeout,
        headless=headless,
        output_file=output,
    ))


@main.command()
@click.argument("username")
@click.option("--output", "-o", default=None, help="Output JSON file path")
@click.option("--delay", default=1.0, help="Delay between API calls in seconds (default: 1.0)")
@click.option("--headless/--no-headless", default=False, help="Run browser headless (default: visible)")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.option("--fresh", is_flag=True, help="Ignore saved state, start from beginning")
@click.option("--auth-token", default=None, help="Manual auth_token cookie (skips browser)")
@click.option("--csrf-token", default=None, help="Manual ct0/CSRF token (skips browser)")
@click.option("--user-id", default=None, help="Manual user ID (skips user ID lookup)")
def scrape(username, output, delay, headless, verbose, fresh, auth_token, csrf_token, user_id):
    """Scrape ALL past broadcasts from a user's timeline.

    Uses GraphQL API pagination. Saves cursor state so you can resume
    after rate limits. Run the same command again to continue.

    USERNAME can be with or without @ (e.g., @SpaceX or SpaceX).

    Examples:

        broadcastx scrape @SpaceX

        broadcastx scrape @SpaceX --fresh    # ignore saved state

        broadcastx scrape @SpaceX --delay 2.0 -v
    """
    if fresh:
        from .scrape_broadcasts import _state_file
        state_path = _state_file(username.lstrip("@"))
        if state_path.exists():
            state_path.unlink()
            console.print(f"[dim]Cleared saved state: {state_path}[/dim]")

    asyncio.run(scrape_broadcasts(
        username=username,
        headless=headless,
        output_file=output,
        delay=delay,
        verbose=verbose,
        auth_token=auth_token,
        csrf_token=csrf_token,
        user_id=user_id,
    ))


@main.command()
@click.argument("usernames", nargs=-1, required=True)
@click.option("--check-interval", default=30 * 60, help="Seconds between profile checks (default: 1800)")
@click.option("--live-interval", default=5 * 60, help="Seconds between live-status checks (default: 300)")
@click.option("--output", "-o", default=None, help="Monitor event JSON file path")
@click.option("--output-dir", default=None, help="Directory for downloaded videos")
@click.option("--browser", "-b", default=DEFAULT_BROWSER, help=f"Browser for yt-dlp cookies (default: {DEFAULT_BROWSER})")
@click.option("--headless/--no-headless", default=False, help="Run browser headless (default: visible)")
@click.option("--download/--no-download", default=True, help="Download when broadcast ends (default: download)")
@click.option("--once", is_flag=True, help="Run one detection cycle, useful for testing")
def monitor(usernames, check_interval, live_interval, output, output_dir, browser, headless, download, once):
    """Monitor profiles for current live broadcasts and download ended replays.

    USERNAMES can be with or without @ (e.g., @SpaceX @NASA or SpaceX NASA).
    Multiple usernames share a single Chromium profile.
    """
    if download and not check_yt_dlp():
        console.print("[red]✗ yt-dlp not found.[/red]")
        console.print("  Install with: [bold]brew install yt-dlp[/bold]")
        raise SystemExit(1)

    if download and not check_ffmpeg():
        console.print("[red]✗ ffmpeg not found.[/red]")
        console.print("  Install with: [bold]brew install ffmpeg[/bold]")
        raise SystemExit(1)

    asyncio.run(monitor_users(
        usernames=list(usernames),
        check_interval=check_interval,
        live_interval=live_interval,
        headless=headless,
        output_file=output,
        output_dir=output_dir,
        browser=browser,
        download=download,
        once=once,
    ))


@main.command()
@click.argument("urls", nargs=-1)
@click.option("--from", "from_file", default=None, type=click.Path(), help="Load URLs from a JSON file")
@click.option("--output-dir", "-o", default=None, help="Output directory for videos")
@click.option("--browser", "-b", default=DEFAULT_BROWSER, help=f"Browser for cookies (default: {DEFAULT_BROWSER})")
@click.option("--verbose", "-v", is_flag=True, help="Show yt-dlp output")
@click.option("--parallel", "-p", default=1, help="Number of concurrent downloads (default: 1)")
def download(urls, from_file, output_dir, browser, verbose, parallel):
    """Download broadcast video(s).

    Pass one or more broadcast URLs directly, or use --from to load from a JSON file.

    Examples:

        broadcastx download https://x.com/i/broadcasts/1vAxRkBbDRzKl

        broadcastx download --from output/broadcasts.json

        broadcastx download --from output/broadcasts.json -o ./my_videos

    Rotation correction is applied automatically: if the broadcast carries
    phone-orientation metadata, the downloaded video is re-encoded so it
    displays upright. A `.rotation.jsonl` sidecar is also written alongside
    the video for inspection.
    """
    # Pre-flight checks
    if not check_yt_dlp():
        console.print("[red]✗ yt-dlp not found.[/red]")
        console.print("  Install with: [bold]brew install yt-dlp[/bold]")
        raise SystemExit(1)

    if not check_ffmpeg():
        console.print("[red]✗ ffmpeg not found.[/red]")
        console.print("  Install with: [bold]brew install ffmpeg[/bold]")
        raise SystemExit(1)

    if not urls and not from_file:
        console.print("[yellow]Provide URLs or use --from <file>.[/yellow]")
        raise SystemExit(1)

    out = Path(output_dir) if output_dir else DEFAULT_VIDEOS_DIR

    results = download_all(
        urls=list(urls),
        from_file=from_file,
        output_dir=out,
        browser=browser,
        verbose=verbose,
        parallel=parallel,
    )

    # Exit with error code if any downloads failed
    if any(not r.success for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
@main.command()
@click.argument("broadcast_url")
@click.option("--browser", "-b", default=DEFAULT_BROWSER, help=f"Browser for cookies (default: {DEFAULT_BROWSER})")
@click.option("--trim/--detect-only", default=False, help="Actually trim paused sections (default: detect only)")
@click.option("--output", "-o", default=None, help="Output video for --trim (default: <video>.trimmed.mp4)")
@click.option("--size-ratio", default=0.50, help="Size-drop threshold (default 0.50)")
@click.option("--gap-density", default=0.50, help="PDT-gap density threshold (default 0.50)")
@click.option("--min-pause", default=10.0, help="Minimum pause duration in seconds (default 10)")
def trim_pauses(broadcast_url, browser, trim, output, size_ratio, gap_density, min_pause):
    """Detect (and optionally trim) paused sections in a broadcast.

    Analyses HLS segments via HTTP HEAD requests (no full download) and
    playlist PDT timestamps to find sections where the video was paused
    while audio continued.  Default: detect-only.  Pass --trim to cut.
    """
    if trim and not check_ffmpeg():
        console.print("[red]ffmpeg not found - install with: brew install ffmpeg[/red]")
        raise SystemExit(1)

    console.print("[bold]Analysing HLS segments for pauses...[/bold]")

    pauses = detect_pauses(
        broadcast_url,
        browser=browser,
        size_ratio_threshold=size_ratio,
        gap_density_threshold=gap_density,
        min_pause_sec=min_pause,
    )

    console.print(pause_report(pauses))

    if trim and pauses:
        video_path = Path("output") / "videos" / f"{broadcast_url.split('/')[-1]}.mp4"
        if not video_path.exists():
            console.print(f"[red]Video not found: {video_path}")
            console.print("  Download first: broadcastx download <url>")
            raise SystemExit(1)

        out = Path(output) if output else Path(str(video_path).replace(".mp4", ".trimmed.mp4"))
        console.print(f"\n[bold]Trimming -> {out}...")
        try:
            trim_intervals(video_path, pauses, out)
            console.print(f"  [green]Done -> {out}")
        except Exception as e:
            console.print(f"  [red]Failed: {e}")
            raise SystemExit(1)
    elif trim and not pauses:
        console.print("[green]Nothing to trim.")
