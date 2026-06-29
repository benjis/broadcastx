"""
Downloader module — Download broadcast videos using yt-dlp.

Usage:
    from broadcastx.downloader import download_broadcast, download_all

    # Single download
    download_broadcast("https://x.com/i/broadcasts/1vAxRkBbDRzKl")

    # Batch download from JSON file
    download_all("broadcasts.json", output_dir="./videos")
"""

import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from .config import (
    DEFAULT_BROWSER,
    DEFAULT_VIDEOS_DIR,
    YTDLP_OUTPUT_TEMPLATE,
    extract_broadcast_id,
    is_broadcast_url,
    normalize_broadcast_url,
)
from .rotation import extract_rotation_sidecar
from .rotation import rotate_video as _apply_rotation

console = Console()


@dataclass
class DownloadResult:
    """Result of a single broadcast download attempt."""
    url: str
    success: bool
    output_file: str | None = None
    rotation_sidecar_file: str | None = None
    rotation_applied: bool = False
    error: str | None = None
    warning: str | None = None


def check_yt_dlp() -> bool:
    """Check if yt-dlp is installed and accessible."""
    return shutil.which("yt-dlp") is not None


def check_ffmpeg() -> bool:
    """Check if ffmpeg is installed and accessible."""
    return shutil.which("ffmpeg") is not None


def download_broadcast(
    url: str,
    output_dir: Path = DEFAULT_VIDEOS_DIR,
    browser: str = DEFAULT_BROWSER,
    verbose: bool = False,
) -> DownloadResult:
    """
    Download a single broadcast video using yt-dlp.

    No timeout — broadcasts can be hours long. yt-dlp runs until completion
    with live output streamed to the terminal.

    Args:
        url: Broadcast URL (x.com/i/broadcasts/... or pscp.tv/w/...)
        output_dir: Directory to save the video
        browser: Browser to extract cookies from
        verbose: Show yt-dlp output
       
    Returns:
        DownloadResult with success status and output file path
    """
    normalized = normalize_broadcast_url(url)
    if not normalized:
        return DownloadResult(
            url=url,
            success=False,
            error=f"Not a valid broadcast URL: {url}",
        )

    broadcast_id = extract_broadcast_id(url)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(output_dir / YTDLP_OUTPUT_TEMPLATE)

    cmd = [
        "yt-dlp",
        "--cookies-from-browser", browser,
        "-f", "bestvideo+bestaudio/best",
        "--output", output_template,
        "--merge-output-format", "mp4",
        "--no-warnings",
        "--newline",            # Progress on new lines
        "--no-overwrites",      # Skip already downloaded
        normalized,
    ]

    console.print(f"  [dim]Downloading {broadcast_id}...[/dim]")
    if verbose:
        console.print(f"  [dim]$ {' '.join(cmd)}[/dim]")

    try:
        # Stream output live — no timeout, broadcasts can be very long
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        output_lines = []
        output_file = None

        # Start rotation extraction in parallel with yt-dlp download
        # Both read from CDN independently; no need to wait.
        rotation_result: list = []
        def _run_sidecar():
            try:
                sc = extract_rotation_sidecar(normalized, output_dir, browser=browser)
                rotation_result.append(("ok", str(sc)))
            except Exception as e:
                rotation_result.append(("error", e))

        rotation_thread = threading.Thread(target=_run_sidecar, daemon=True)
        rotation_thread.start()

        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)

            # Always show progress/status lines
            if verbose or "[download]" in line or "[Merger]" in line or "already" in line.lower():
                console.print(f"    [dim]{line}[/dim]")

            # Try to capture the output filename
            if "Merging formats into" in line and '"' in line:
                start = line.index('"') + 1
                end = line.rindex('"')
                output_file = line[start:end]
            elif "Destination:" in line:
                output_file = line.split("Destination:")[-1].strip()

        process.wait()

        if process.returncode == 0:
            sidecar_file = None
            warning = None
            rotation_thread.join(timeout=300)
            if rotation_result:
                r = rotation_result[0]
                kind, value = r
                if kind == "error":
                    warning = f"Rotation sidecar failed: {value}"
                    console.print(f"  [yellow]{warning}[/yellow]")
                else:
                    sidecar_file = value
                    console.print(f"  [green]Rotation sidecar[/green] → {sidecar_file}")
            rotation_applied = False
            if sidecar_file and output_file and Path(output_file).exists():
                try:
                    _apply_rotation(output_file, sidecar_file)
                    # rotate_video returns the same path when it replaces in-place
                    rotation_applied = True
                    console.print(f"  [green]Rotation corrected[/green] → {output_file}")
                except subprocess.CalledProcessError as e:
                    err = (e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
                    w = f"Rotation correction failed (exit {e.returncode}): {err[:400]}"
                    warning = f"{warning}; {w}" if warning else w
                    console.print(f"  [yellow]{w}[/yellow]")
                except Exception as e:
                    w = f"Rotation correction failed: {e!r}"
                    warning = f"{warning}; {w}" if warning else w
                    console.print(f"  [yellow]{w}[/yellow]")
            return DownloadResult(
                url=normalized,
                success=True,
                output_file=output_file,
                rotation_sidecar_file=sidecar_file,
                rotation_applied=rotation_applied,
                warning=warning,
            )
        else:
            # Extract error from output
            error_msg = "\n".join(output_lines[-3:]) or f"yt-dlp exited with code {process.returncode}"
            return DownloadResult(
                url=normalized,
                success=False,
                error=error_msg,
            )
    except FileNotFoundError:
        return DownloadResult(
            url=normalized,
            success=False,
            error="yt-dlp not found. Install it with: brew install yt-dlp",
        )


def download_all(
    urls: list[str] | None = None,
    from_file: Path | str | None = None,
    output_dir: Path = DEFAULT_VIDEOS_DIR,
    browser: str = DEFAULT_BROWSER,
    verbose: bool = False,
    parallel: int = 1,
) -> list[DownloadResult]:
    """
    Download multiple broadcast videos.

    Args:
        urls: List of broadcast URLs
        from_file: Path to JSON file containing broadcast data
        output_dir: Directory to save videos
        browser: Browser to extract cookies from
        verbose: Show yt-dlp output
        parallel: Number of concurrent downloads (default: 1 = sequential)
       
    Returns:
        List of DownloadResult objects
    """
    all_urls = list(urls or [])

    # Load URLs from file if provided
    if from_file:
        file_urls = _load_urls_from_file(Path(from_file))
        all_urls.extend(file_urls)

    if not all_urls:
        console.print("[yellow]No broadcast URLs to download.[/yellow]")
        return []

    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for url in all_urls:
        normalized = normalize_broadcast_url(url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_urls.append(normalized)

    total = len(unique_urls)
    console.print(f"\n[bold]Downloading {total} broadcast(s)" + (f" ({parallel} parallel)" if parallel > 1 else "") + f"...[/bold]\n")

    if parallel <= 1:
        # Sequential download
        results = []
        for i, url in enumerate(unique_urls, 1):
            console.print(f"[bold][{i}/{total}][/bold] {url}")
            result = download_broadcast(url, output_dir=output_dir, browser=browser, verbose=verbose)
            results.append(result)
            if result.success:
                console.print(f"  [green]✓ Done[/green]" + (f" → {result.output_file}" if result.output_file else ""))
            else:
                console.print(f"  [red]✗ Failed: {result.error}[/red]")
            console.print()
    else:
        # Parallel download
        results = [None] * total
        lock = threading.Lock()
        completed_count = 0

        def _download_one(index: int, url: str) -> tuple[int, DownloadResult]:
            return index, download_broadcast(url, output_dir=output_dir, browser=browser, verbose=verbose)

        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(_download_one, i, url): (i, url)
                for i, url in enumerate(unique_urls)
            }

            for future in as_completed(futures):
                idx, result = future.result()
                results[idx] = result
                url = unique_urls[idx]
                bid = extract_broadcast_id(url)

                with lock:
                    completed_count += 1
                    if result.success:
                        console.print(f"  [green]✓[/green] [{completed_count}/{total}] {bid}" + (f" → {result.output_file}" if result.output_file else ""))
                    else:
                        console.print(f"  [red]✗[/red] [{completed_count}/{total}] {bid}: {result.error}")

        console.print()

    # Summary
    succeeded = sum(1 for r in results if r and r.success)
    failed = sum(1 for r in results if r and not r.success)
    console.print(f"[bold]Done:[/bold] {succeeded} succeeded, {failed} failed")

    return results


def _load_urls_from_file(path: Path) -> list[str]:
    """
    Load broadcast URLs from a JSON file.

    Supports multiple formats:
        1. Plain list of URLs:    ["https://x.com/i/broadcasts/abc", ...]
        2. Scanner output:        {"broadcasts": [{"url": "..."}, ...]}
        3. List of objects:       [{"url": "..."}, ...]
    """
    if not path.exists():
        console.print(f"[red]File not found: {path}[/red]")
        return []

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON in {path}: {e}[/red]")
        return []

    urls = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict) and "url" in item:
                urls.append(item["url"])
    elif isinstance(data, dict) and "broadcasts" in data:
        for item in data["broadcasts"]:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict) and "url" in item:
                urls.append(item["url"])

    valid = [u for u in urls if is_broadcast_url(u)]
    console.print(f"  [dim]Loaded {len(valid)} broadcast URL(s) from {path}[/dim]")
    return valid
