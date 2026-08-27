"""
BroadcastX CLI — Discover and download X/Twitter broadcast videos.

Usage:
    broadcastx scan @username
    broadcastx download https://x.com/i/broadcasts/...
    broadcastx download --from broadcasts.json
"""

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import click
from rich.console import Console

from . import __version__
from .config import (
    AppConfig,
    ConfigError,
    DEFAULT_BROADCASTS_FILE,
    DEFAULT_BROWSER,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_SCROLLS,
    DEFAULT_SCROLL_DELAY,
    DEFAULT_VIDEOS_DIR,
    load_config,
    normalize_username,
)
from .downloader import check_ffmpeg, check_yt_dlp, download_all, download_broadcast
from .monitor import monitor_user, monitor_users
from .pause_detector import detect_pauses, pause_report, trim_intervals
from .scanner import scan_user
from .watermark import WatermarkOptions, add_watermark

console = Console()


def _load_cli_config(path: str | None) -> AppConfig:
    try:
        return load_config(path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
@click.version_option(version=__version__, prog_name="broadcastx")
def main():
    """BroadcastX — Discover and download X/Twitter broadcast videos."""
    pass


def _require_dependencies(*tools: str) -> None:
    """Check required tools are installed, exit with message if not.

    Accepts any combination of "yt-dlp" and "ffmpeg".
    """
    checks = {"yt-dlp": (check_yt_dlp, "yt-dlp"), "ffmpeg": (check_ffmpeg, "ffmpeg")}
    for tool in tools:
        check_fn, name = checks[tool]
        if not check_fn():
            console.print(f"[red]✗ {name} not found. Install with: brew install {name}[/red]")
            raise SystemExit(1)


@main.command()
@click.argument("username")
@click.option("--max-scrolls", "-n", default=DEFAULT_MAX_SCROLLS, show_default=True, help="Maximum scroll actions")
@click.option("--scroll-delay", "-d", default=DEFAULT_SCROLL_DELAY, show_default=True, help="Delay between scrolls in seconds")
@click.option("--idle-timeout", "-t", default=DEFAULT_IDLE_TIMEOUT, show_default=True, help="Stop after N seconds with no new target posts")
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
@click.argument("usernames", nargs=-1)
@click.option("--config", "config_file", type=click.Path(dir_okay=False), default=None, help="Config file (default: ./broadcastx.toml when present)")
@click.option("--check-interval", type=int, default=None, help="Seconds between profile checks")
@click.option("--live-interval", type=int, default=None, help="Seconds between live-status checks")
@click.option("--output", "-o", default=None, help="Monitor event JSON file path")
@click.option("--output-dir", default=None, help="Output root containing per-user directories")
@click.option("--browser", "-b", default=None, help="Browser for yt-dlp cookies")
@click.option("--headless/--no-headless", default=None, help="Run browser headless")
@click.option("--download/--no-download", default=None, help="Download when broadcast ends")
@click.option("--once", is_flag=True, help="Run one detection cycle, useful for testing")
def monitor(usernames, config_file, check_interval, live_interval, output, output_dir, browser, headless, download, once):
    """Monitor profiles for current live broadcasts and download ended replays.

    USERNAMES can be with or without @ (e.g., @SpaceX @NASA or SpaceX NASA).
    Multiple usernames share a single Chromium profile.
    """
    config = _load_cli_config(config_file)
    selected_users = [normalize_username(username) for username in usernames] if usernames else [user.username for user in config.users]
    if not selected_users:
        raise click.ClickException("Provide USERNAMES or add [[users]] entries to broadcastx.toml")
    if len(set(selected_users)) != len(selected_users):
        raise click.ClickException("The monitor user list contains duplicates")
    if output_dir:
        config = replace(config, output_root=Path(output_dir).expanduser().resolve())

    resolved_check_interval = check_interval if check_interval is not None else config.monitor.check_interval_seconds
    resolved_live_interval = live_interval if live_interval is not None else config.monitor.live_interval_seconds
    resolved_browser = browser if browser is not None else config.monitor.browser
    resolved_headless = headless if headless is not None else config.monitor.headless
    resolved_download = download if download is not None else config.monitor.download

    if resolved_download:
        _require_dependencies("yt-dlp", "ffmpeg")


    asyncio.run(monitor_users(
        usernames=selected_users,
        check_interval=resolved_check_interval,
        live_interval=resolved_live_interval,
        headless=resolved_headless,
        output_file=output,
        output_dir=None,
        browser=resolved_browser,
        download=resolved_download,
        once=once,
        app_config=config,
    ))


@main.command()
@click.argument("urls", nargs=-1)
@click.option("--config", "config_file", type=click.Path(dir_okay=False), default=None, help="Config file (default: ./broadcastx.toml when present)")
@click.option("--from", "from_file", default=None, type=click.Path(), help="Load URLs from a JSON file")
@click.option("--output-dir", "-o", default=None, help="Output root containing per-user directories")
@click.option("--browser", "-b", default=DEFAULT_BROWSER, help=f"Browser for cookies (default: {DEFAULT_BROWSER})")
@click.option("--verbose", "-v", is_flag=True, help="Show yt-dlp output")
@click.option("--parallel", "-p", default=1, help="Number of concurrent downloads (default: 1)")
def download(urls, config_file, from_file, output_dir, browser, verbose, parallel):
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
    _require_dependencies("yt-dlp", "ffmpeg")
    # Pre-flight checks


    if not urls and not from_file:
        console.print("[yellow]Provide URLs or use --from <file>.[/yellow]")
        raise SystemExit(1)

    config = _load_cli_config(config_file)
    if output_dir:
        config = replace(config, output_root=Path(output_dir).expanduser().resolve())

    results = download_all(
        urls=list(urls),
        from_file=from_file,
        output_dir=None,
        browser=browser,
        verbose=verbose,
        parallel=parallel,
        app_config=config,
    )

    # Exit with error code if any downloads failed
    if any(not r.success for r in results):
        raise SystemExit(1)


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

    if trim:
        _require_dependencies("ffmpeg")
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


@main.command()
@click.argument("video_path", type=click.Path(exists=True))
@click.option("--text", "-t", default=None, help="Watermark text (default: broadcastx)")
@click.option("--font", "-f", default="sans-serif", help="Font family or path (default: sans-serif)")
@click.option("--font-size", "-s", default=24, type=int, help="Font size in points (default: 24)")
@click.option("--opacity", "-o", default=0.7, type=float, help="Text opacity 0.0-1.0 (default: 0.7)")
@click.option("--color", "-c", default="white", help="Font color name or hex (default: white)")
@click.option("--position", "-p", default="bottom-right",
              type=click.Choice(["bottom-right", "bottom-left", "top-right", "top-left"]),
              help="Watermark position on screen (default: bottom-right)")
@click.option("--output", "-O", default=None, type=click.Path(), help="Output video path (default: overwrite input)")
@click.option("--dry-run", is_flag=True, help="Show what would be done without running ffmpeg")
def watermark(video_path, text, font, font_size, opacity, color, position, output, dry_run):
    """Add a text watermark to a video using ffmpeg.

    Draws customizable text (font, size, opacity, color) in a corner of
    the video.  Requires ffmpeg compiled with --enable-libfreetype (stock
    ffmpeg includes this).
    """

    _require_dependencies("ffmpeg")
    vid = Path(video_path)

    opts = WatermarkOptions(
        text=text or "broadcastx",
        font=font,
        font_size=font_size,
        opacity=opacity,
        color=color,
        position=position,
    )

    out = Path(output) if output else None

    console.print(f"[bold]Adding watermark...[/bold]")
    console.print(f"  Video:  {vid}")
    console.print(f"  Text:   \"{opts.text}\"")
    console.print(f"  Font:   {opts.font} {opts.font_size}pt")
    console.print(f"  Color:  {opts.color} @ {opts.opacity:.0%}")
    console.print(f"  Pos:    {opts.position}")
    console.print(f"  Output: {out or vid}")

    if dry_run:
        console.print("[yellow]Dry-run — nothing executed.[/yellow]")
        return

    try:
        result = add_watermark(vid, output_path=out, options=opts)
        console.print(f"  [green]Done → {result}[/green]")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        console.print(f"[red]ffmpeg failed (exit {e.returncode}):[/red]")
        console.print(stderr[:600])
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Failed: {e}[/red]")
        raise SystemExit(1)

if __name__ == "__main__":
    main()
