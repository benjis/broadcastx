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
import re
import shutil
import subprocess
import threading
import time
import tempfile
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console

from .config import (
    AppConfig,
    ConfigError,
    DEFAULT_BROWSER,
    DEFAULT_VIDEOS_DIR,
    DownloadConfig,
    YTDLP_OUTPUT_TEMPLATE,
    extract_broadcast_id,
    format_output_stem,
    is_broadcast_url,
    normalize_broadcast_url,
    normalize_username,
    normalize_tweet_url,
    format_video_stem,
    user_output_dir,
)
from .rotation import extract_rotation_sidecar
from .rotation import rotate_video as _apply_rotation

console = Console()


@dataclass
class DownloadResult:
    """Result of one input link, including any successfully downloaded media."""
    url: str
    success: bool
    output_file: str | None = None
    rotation_sidecar_file: str | None = None
    rotation_applied: bool = False
    error: str | None = None
    warning: str | None = None
    username: str | None = None
    output_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BroadcastMetadata:
    """Metadata needed to route and name one broadcast download."""

    uploader_id: str | None = None
    timestamp: int | float | None = None


def probe_broadcast_metadata(url: str, browser: str = DEFAULT_BROWSER) -> BroadcastMetadata:
    """Ask yt-dlp for routing/naming metadata without downloading media."""
    completed = subprocess.run(
        [
            "yt-dlp",
            "--cookies-from-browser", browser,
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        timestamp = None
    uploader_id = payload.get("uploader_id")
    if not isinstance(uploader_id, str) or not uploader_id.strip():
        uploader_id = None
    return BroadcastMetadata(uploader_id=uploader_id, timestamp=timestamp)


# In-place progress display. The pipeline (video download, metadata
# download, ffmpeg rotation) can run for tens of minutes, so every stage
# renders as a single status line overwritten in place (carriage-return
# based) instead of scrolling.
_PROGRESS_COLS = 76

# Matches yt-dlp progress lines, including streams where no total size is
# available. The display format has changed across yt-dlp releases, so only
# the percentage is required.
_DOWNLOAD_PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_progress_active = False
_progress_lock = threading.Lock()


def _write_progress(text: str) -> None:
    """Overwrite the current in-place status line."""
    global _progress_active
    # Rich treats carriage returns as layout input instead of forwarding them
    # to the terminal, which turns every refresh into another printed line.
    # Progress controls must therefore bypass Rich's renderer.
    with _progress_lock:
        console.file.write(f"\r{text.ljust(_PROGRESS_COLS)}")
        console.file.flush()
        _progress_active = True


def _clear_progress() -> None:
    """Erase the in-place status line, leaving the cursor at line start."""
    global _progress_active
    with _progress_lock:
        if not _progress_active:
            return
        console.file.write("\r" + " " * _PROGRESS_COLS + "\r")
        console.file.flush()
        _progress_active = False


def _download_progress_text(line: str) -> str | None:
    """Return a compact summary of a yt-dlp `[download]` progress line.

    Returns None for lines that are not progress updates (e.g.
    `[download] Destination: ...` or `[download] has already been
    downloaded`), so they can be printed normally instead.
    """
    # yt-dlp can prefix progress lines with terminal-color or cursor-control
    # sequences. Remove those before recognizing the stable `[download]` part.
    s = _ANSI_ESCAPE_RE.sub("", line).lstrip()
    if not s.startswith("[download]"):
        return None
    rest = s.split("]", 1)[1].lstrip()
    if not rest or not rest[0].isdigit():
        return None
    match = _DOWNLOAD_PROGRESS_RE.search(s)
    if match:
        parts = [f"{match.group(1)}%"]
        speed = re.search(r"\bat\s+(\S+)", s)
        eta = re.search(r"\bETA\s+([\d:]+)", s)
        if speed:
            parts.append(f"at {speed.group(1)}")
        if eta:
            parts.append(f"ETA {eta.group(1)}")
        return " ".join(parts)
    return rest


def _fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _metadata_progress_text(completed: int, total: int) -> str:
    """Format sidecar progress from its known HLS segment count."""
    if total <= 0:
        return "complete"
    return f"{completed / total * 100:.1f}% ({completed}/{total})"


@contextmanager
def _stage_indicator(label: str, interval: float = 0.5):
    """Show an in-place `{label} \u2026 mm:ss` ticker while the block runs.

    Used for stages with no intrinsic progress output (metadata download,
    ffmpeg rotation) so the user always knows what the pipeline is doing.
    """
    done = threading.Event()
    started = time.monotonic()

    def _tick() -> None:
        while not done.is_set():
            _write_progress(f"{label} \u2026 {_fmt_elapsed(time.monotonic() - started)}")
            done.wait(interval)

    ticker = threading.Thread(target=_tick, daemon=True)
    ticker.start()
    try:
        yield
    finally:
        done.set()
        ticker.join()
        _clear_progress()


def check_yt_dlp() -> bool:
    """Check if yt-dlp is installed and accessible."""
    return shutil.which("yt-dlp") is not None


def check_ffmpeg() -> bool:
    """Check if ffmpeg is installed and accessible."""
    return shutil.which("ffmpeg") is not None


def download_broadcast(
    url: str,
    output_dir: Path | None = None,
    browser: str = DEFAULT_BROWSER,
    verbose: bool = False,
    progress: str = "inplace",
    naming: DownloadConfig | None = None,
    metadata: BroadcastMetadata | None = None,
    app_config: AppConfig | None = None,
    username: str | None = None,
) -> DownloadResult:
    """
    Download a single broadcast video using yt-dlp.

    No timeout — broadcasts can be hours long. The video download and the
    rotation-metadata (sidecar) download run in parallel.

    `progress` selects the output shape; download, parallel metadata,
    rotation, and failure semantics are identical in both modes:
      - "inplace" (default): each pipeline stage renders as a single
        status line, overwritten in place via carriage returns (v1 UX).
      - "stages": complete event lines only, never carriage returns —
        safe while another loop prints to the same terminal (monitor
        mode). The in-place stage ticker does not run; long stages are
        reported as complete event lines instead.

    Errors and warnings always print as complete lines in both modes.

    Args:
        url: Broadcast URL (x.com/i/broadcasts/... or pscp.tv/w/...)
        output_dir: Directory to save the video
        browser: Browser to extract cookies from
        verbose: Show yt-dlp output
       
        progress: Output mode, "inplace" (single line) or "stages" (event lines)

    Returns:
        DownloadResult with success status and output file path
    """
    if progress not in ("inplace", "stages"):
        raise ValueError(f"progress must be 'inplace' or 'stages', got {progress!r}")
    stages = progress == "stages"

    normalized = normalize_broadcast_url(url)
    if not normalized:
        return DownloadResult(
            url=url,
            success=False,
            error=f"Not a valid broadcast URL: {url}",
        )

    metadata_warning = None
    if (naming is not None or app_config is not None) and metadata is None:
        try:
            metadata = probe_broadcast_metadata(normalized, browser)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            metadata = BroadcastMetadata()
            metadata_warning = f"Could not read broadcast metadata; using broadcast ID filename: {exc}"
            console.print(f"  [yellow]{metadata_warning}[/yellow]")
    if naming is not None and metadata is not None and metadata.timestamp is None and metadata_warning is None:
        metadata_warning = "Broadcast metadata has no timestamp; using broadcast ID filename"
        console.print(f"  [yellow]{metadata_warning}[/yellow]")

    resolved_username = None
    if username:
        resolved_username = normalize_username(username)
    elif app_config is not None and metadata and metadata.uploader_id:
        try:
            resolved_username = normalize_username(metadata.uploader_id)
        except ConfigError:
            resolved_username = None
    if app_config is not None:
        resolved_username = resolved_username or "unknown"
        if resolved_username == "unknown":
            routing_warning = "Broadcast metadata has no usable uploader ID; routing to output/unknown"
            metadata_warning = f"{metadata_warning}; {routing_warning}" if metadata_warning else routing_warning
            console.print(f"  [yellow]{routing_warning}[/yellow]")
        naming = app_config.download_for(resolved_username)
        output_dir = Path(output_dir) if output_dir is not None else user_output_dir(app_config.output_root, resolved_username) / "videos"
    else:
        output_dir = Path(output_dir) if output_dir is not None else DEFAULT_VIDEOS_DIR

    if naming is not None and metadata is not None and metadata.timestamp is None and metadata_warning is None:
        metadata_warning = "Broadcast metadata has no timestamp; using broadcast ID filename"
        console.print(f"  [yellow]{metadata_warning}[/yellow]")

    broadcast_id = extract_broadcast_id(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    if naming is not None:
        stem = format_output_stem(metadata.timestamp if metadata else None, broadcast_id, naming)
        final_video_path = output_dir / f"{stem}.mp4"
        final_sidecar_path = output_dir / f"{stem}.rotation.jsonl"
        output_template = str(output_dir / "%(id)s.%(ext)s")
        if final_video_path.exists():
            warning = "Already downloaded; existing file was not overwritten"
            console.print(f"  [yellow]{warning}[/yellow] → {final_video_path}")
            return DownloadResult(
                url=normalized,
                success=True,
                output_file=str(final_video_path),
                rotation_sidecar_file=str(final_sidecar_path) if final_sidecar_path.exists() else None,
                warning=warning,
                username=resolved_username,
            )
    else:
        final_video_path = None
        final_sidecar_path = None
        output_template = str(output_dir / YTDLP_OUTPUT_TEMPLATE)

    cmd = [
        "yt-dlp",
        "--cookies-from-browser", browser,
        "-f", "bestvideo+bestaudio/best",
        "--output", output_template,
        "--merge-output-format", "mp4",
        "--no-warnings",
        # Line-per-update: we stream lines through the pipe and collapse the
        # progress ones into a single in-place display line.
        "--newline",
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
        progress_state_lock = threading.Lock()
        video_progress = "starting"
        metadata_progress = "preparing"

        def _render_parallel_progress() -> None:
            if stages:
                return
            with progress_state_lock:
                _write_progress(f"Video {video_progress} | Metadata {metadata_progress}")

        def _metadata_progress(completed: int, total: int) -> None:
            nonlocal metadata_progress
            with progress_state_lock:
                metadata_progress = _metadata_progress_text(completed, total)
            _render_parallel_progress()

        def _run_sidecar():
            try:
                sc = extract_rotation_sidecar(
                    normalized,
                    output_dir,
                    browser=browser,
                    progress_callback=_metadata_progress,
                )
                rotation_result.append(("ok", str(sc)))
            except Exception as e:
                rotation_result.append(("error", e))

        rotation_thread = threading.Thread(target=_run_sidecar, daemon=True)
        rotation_thread.start()

        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)

            # Collapse yt-dlp progress updates into a single in-place line;
            # print other status lines normally.
            progress_text = _download_progress_text(line)
            if progress_text:
                if not stages:
                    with progress_state_lock:
                        video_progress = progress_text
                    _render_parallel_progress()
                continue
            if not stages:
                _clear_progress()

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
        if not stages:
            _clear_progress()

        if process.returncode == 0:
            console.print(f"  [green]Video downloaded[/green]" + (f" → {output_file}" if output_file else ""))
            sidecar_file = None
            warning = metadata_warning
            if rotation_result:
                rotation_thread.join(timeout=300)
            elif stages:
                console.print("  [dim]Downloading metadata...[/dim]")
                rotation_thread.join(timeout=300)
            else:
                with progress_state_lock:
                    video_progress = "complete"
                _render_parallel_progress()
                rotation_thread.join(timeout=300)
                _clear_progress()
            if rotation_result:
                r = rotation_result[0]
                kind, value = r
                if kind == "error":
                    sidecar_warning = f"Rotation sidecar failed: {value}"
                    warning = f"{warning}; {sidecar_warning}" if warning else sidecar_warning
                    console.print(f"  [yellow]{sidecar_warning}[/yellow]")
                else:
                    sidecar_file = value
                    console.print(f"  [green]Rotation sidecar[/green] → {sidecar_file}")
            rotation_applied = False
            if sidecar_file and output_file and Path(output_file).exists():
                try:
                    if stages:
                        console.print("  [dim]Converting orientation...[/dim]")
                        _apply_rotation(output_file, sidecar_file)
                    else:
                        def _rotation_progress(
                            encoded_seconds: float,
                            total_seconds: float,
                            frame: int | None,
                            speed: str | None,
                            phase: str,
                        ) -> None:
                            if total_seconds:
                                percent = min(encoded_seconds / total_seconds * 100, 100)
                                status = f"{phase} {percent:.1f}% ({_fmt_elapsed(encoded_seconds)}/{_fmt_elapsed(total_seconds)})"
                            else:
                                status = f"{phase} {_fmt_elapsed(encoded_seconds)}"
                            if frame is not None:
                                status += f" frame {frame}"
                            if speed:
                                status += f" at {speed}"
                            _write_progress(status)

                        _apply_rotation(
                            output_file,
                            sidecar_file,
                            progress_callback=_rotation_progress,
                        )
                        _clear_progress()
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
            if naming is not None:
                staging_video = Path(output_file) if output_file else output_dir / f"{broadcast_id}.mp4"
                if staging_video.exists() and staging_video != final_video_path:
                    staging_video.rename(final_video_path)
                if sidecar_file:
                    staging_sidecar = Path(sidecar_file)
                    if staging_sidecar.exists() and staging_sidecar != final_sidecar_path:
                        if final_sidecar_path.exists():
                            staging_sidecar.unlink()
                        else:
                            staging_sidecar.rename(final_sidecar_path)
                    sidecar_file = str(final_sidecar_path)
                output_file = str(final_video_path)
                console.print(f"  [green]Named artifacts[/green] → {final_video_path.name}")
            return DownloadResult(
                url=normalized,
                success=True,
                output_file=output_file,
                rotation_sidecar_file=sidecar_file,
                rotation_applied=rotation_applied,
                warning=warning,
                username=resolved_username,
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


def _normalize_download_url(url) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {"x.com", "twitter.com", "pscp.tv", "www.pscp.tv"}:
            if re.fullmatch(r"/(?:i/broadcasts|w)/[A-Za-z0-9]{8,}/?", parsed.path):
                if broadcast := normalize_broadcast_url(url):
                    return broadcast
    except ValueError:
        return None
    return normalize_tweet_url(url)


def download_video(
    url: str,
    output_dir: Path | None = None,
    browser: str = DEFAULT_BROWSER,
    verbose: bool = False,
    *,
    app_config: AppConfig | None = None,
    username: str | None = None,
) -> DownloadResult:
    """Download a broadcast or all native video media in one X post."""
    normalized = _normalize_download_url(url)
    if not normalized:
        return DownloadResult(url=url, success=False, error=f"Unsupported video URL: {url}")
    if normalize_broadcast_url(normalized):
        try:
            return download_broadcast(normalized, output_dir=output_dir, browser=browser,
                                      verbose=verbose, app_config=app_config, username=username)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return DownloadResult(url=normalized, success=False, error=str(exc))
    return _download_tweet(normalized, output_dir, browser, verbose, app_config)


def _download_tweet(
    url: str,
    output_dir: Path | None,
    browser: str,
    verbose: bool,
    app_config: AppConfig | None,
) -> DownloadResult:
    tweet_id = url.rsplit("/", 1)[-1]
    files, errors, warnings = [], [], []
    owner = None
    try:
        probe = subprocess.run(
            ["yt-dlp", "--cookies-from-browser", browser, "--dump-single-json",
             "--skip-download", "--no-warnings", "--yes-playlist",
             "--use-extractors", "Twitter", url],
            check=True, capture_output=True, text=True,
        )
        payload = json.loads(probe.stdout)
        if not isinstance(payload, dict):
            raise ValueError("Invalid tweet metadata")
        entries = payload.get("entries") if payload.get("_type") in {"playlist", "multi_video"} else [payload]
        if not isinstance(entries, list) or not entries:
            raise ValueError("No native video found in this tweet")
        config = app_config or AppConfig()
        for index, entry in enumerate(entries, 1):
            try:
                if not isinstance(entry, dict) or entry.get("_type", "video") != "video" or not entry.get("formats"):
                    raise ValueError("No native video formats; external links are not supported")
                extractor = entry.get("extractor_key") or entry.get("extractor") or payload.get("extractor_key") or payload.get("extractor")
                if extractor and extractor.lower() != "twitter":
                    raise ValueError("External video extractors are not supported")
                raw_owner = entry.get("uploader_id") or payload.get("uploader_id")
                try:
                    media_owner = normalize_username(raw_owner)
                except ConfigError:
                    media_owner = "unknown"
                    warnings.append("No usable uploader ID; routing to unknown")
                owner = owner or media_owner
                naming = config.download_for(media_owner)
                timestamp = entry.get("timestamp", payload.get("timestamp"))
                if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                    timestamp = None
                    warnings.append("No timestamp; using tweet and media IDs")
                media_id = str(entry.get("id") or index)
                if not re.fullmatch(r"[A-Za-z0-9_-]+", media_id):
                    raise ValueError("Invalid media ID")
                directory = Path(output_dir) if output_dir is not None else user_output_dir(config.output_root, media_owner) / "videos"
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / (format_video_stem(timestamp, tweet_id, media_id, naming) + ".mp4")
                if target.exists():
                    files.append(str(target))
                    warnings.append("Already downloaded; existing file was not overwritten")
                    continue
                with tempfile.TemporaryDirectory(prefix=".tweet-", dir=directory) as work:
                    info = Path(work) / "media.info.json"
                    info.write_text(json.dumps(entry), encoding="utf-8")
                    cmd = ["yt-dlp", "--cookies-from-browser", browser,
                           "--load-info-json", str(info), "-f", "bestvideo+bestaudio/best",
                           "--output", str(Path(work) / "media.%(ext)s"),
                           "--merge-output-format", "mp4", "--remux-video", "mp4",
                           "--no-warnings", "--newline", "--no-overwrites", "--no-simulate",
                           "--print", 'after_move:BX_FILE:%(filepath)j']
                    if verbose:
                        console.print(f"  [dim]$ {' '.join(cmd)}[/dim]")
                    final_path = None
                    tail = []
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    try:
                        for line in process.stdout:
                            line = line.rstrip()
                            tail = (tail + [line])[-3:]
                            if line.startswith("BX_FILE:"):
                                final_path = Path(json.loads(line[len("BX_FILE:"):]))
                            elif verbose or "[download]" in line or "[Merger]" in line:
                                console.print(f"    [dim]{line}[/dim]")
                        process.wait()
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            process.wait()
                        process.stdout.close()
                    if process.returncode:
                        raise ValueError("\n".join(tail) or f"yt-dlp exited with code {process.returncode}")
                    if final_path is None or not final_path.is_file() or final_path.resolve().parent != Path(work).resolve():
                        raise ValueError("yt-dlp did not report a valid final video file")
                    # Atomic creation prevents a concurrent invocation overwriting a target.
                    try:
                        target.hardlink_to(final_path)
                    except FileExistsError:
                        warnings.append("Already downloaded; existing file was not overwritten")
                    files.append(str(target))
                    console.print(f"  [green]Video downloaded[/green] → {target}")
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                errors.append(f"Media {index}: {exc}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        errors.append(detail)
    warning = "; ".join(dict.fromkeys(warnings)) or None
    if warning:
        console.print(f"  [yellow]{warning}[/yellow]")
    return DownloadResult(url=url, success=bool(files) and not errors,
                          output_file=files[0] if files else None, output_files=files,
                          error="; ".join(errors) or None, warning=warning, username=owner)


def download_all(
    urls: list[str] | None = None,
    from_file: Path | str | None = None,
    output_dir: Path | None = None,
    browser: str = DEFAULT_BROWSER,
    verbose: bool = False,
    parallel: int = 1,
    app_config: AppConfig | None = None,
) -> list[DownloadResult]:
    """
    Download Broadcast and ordinary X post videos, preserving link order.

    Args:
        urls: List of Broadcast or X post URLs
        from_file: Path to JSON file containing URLs or scanner data
        output_dir: Directory to save videos
        browser: Browser to extract cookies from
        verbose: Show yt-dlp output
        parallel: Number of concurrent downloads (default: 1 = sequential)
       
    Returns:
        List of DownloadResult objects
    """
    all_urls = list(urls or [])
    username_hints: dict[str, str] = {}
    source_error = None

    # Load URLs from file if provided
    if from_file:
        try:
            file_urls, source_username = _load_download_source(Path(from_file))
        except (OSError, ValueError) as exc:
            # Preserve direct downloads even when the additional file is unusable.
            source_error = DownloadResult(url=str(from_file), success=False, error=str(exc))
            file_urls, source_username = [], None
        else:
            source_error = None
        all_urls.extend(file_urls)
        if source_username:
            for file_url in file_urls:
                if isinstance(file_url, str) and (normalized := _normalize_download_url(file_url)) and normalize_broadcast_url(normalized):
                    username_hints[normalized] = source_username

    if not all_urls:
        if source_error:
            console.print(f"[red]✗ Failed: {source_error.error}[/red]")
        console.print("[yellow]No video URLs to download.[/yellow]")
        return [source_error] if source_error else []

    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for url in all_urls:
        normalized = _normalize_download_url(url) or str(url)
        if normalized not in seen:
            seen.add(normalized)
            unique_urls.append(normalized)

    total = len(unique_urls)
    console.print(f"\n[bold]Downloading {total} video link(s)" + (f" ({parallel} parallel)" if parallel > 1 else "") + f"...[/bold]\n")

    if parallel <= 1:
        # Sequential download
        results = []
        for i, url in enumerate(unique_urls, 1):
            console.print(f"[bold][{i}/{total}][/bold] {url}")
            result = download_video(
                url,
                output_dir=output_dir,
                browser=browser,
                verbose=verbose,
                app_config=app_config,
                username=username_hints.get(url),
            )
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
            return index, download_video(
                url,
                output_dir=output_dir,
                browser=browser,
                verbose=verbose,
                app_config=app_config,
                username=username_hints.get(url),
            )

        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(_download_one, i, url): (i, url)
                for i, url in enumerate(unique_urls)
            }

            for future in as_completed(futures):
                idx, result = future.result()
                results[idx] = result
                url = unique_urls[idx]
                bid = extract_broadcast_id(url) or url.rsplit("/", 1)[-1]

                with lock:
                    completed_count += 1
                    if result.success:
                        console.print(f"  [green]✓[/green] [{completed_count}/{total}] {bid}" + (f" → {result.output_file}" if result.output_file else ""))
                    else:
                        console.print(f"  [red]✗[/red] [{completed_count}/{total}] {bid}: {result.error}")

        console.print()

    # Summary
    if source_error:
        results.append(source_error)
        console.print(f"  [red]✗ Failed: {source_error.error}[/red]")
    succeeded = sum(1 for r in results if r and r.success)
    failed = sum(1 for r in results if r and not r.success)
    console.print(f"[bold]Done:[/bold] {succeeded} succeeded, {failed} failed")

    return results


def _load_download_source(path: Path) -> tuple[list[str], str | None]:
    """
    Load mixed video URLs from JSON, retaining scanner owner hints.

    Supports multiple formats:
        1. Plain list of URLs:    ["https://x.com/i/broadcasts/abc", ...]
        2. Scanner output:        {"broadcasts": [{"url": "..."}, ...]}
        3. List of objects:       [{"url": "..."}, ...]
    """
    if not path.exists():
        raise ValueError(f"File not found: {path}")

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {path}: {e}") from e

    urls = []
    username = None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict) and "url" in item:
                urls.append(item["url"])
    elif isinstance(data, dict) and "broadcasts" in data:
        if not isinstance(data["broadcasts"], list):
            raise ValueError(f"Invalid broadcasts array in {path}")
        raw_username = data.get("username")
        if isinstance(raw_username, str):
            try:
                username = normalize_username(raw_username)
            except ConfigError:
                username = None
        for item in data["broadcasts"]:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict) and "url" in item:
                urls.append(item["url"])

    else:
        raise ValueError(f"Unsupported download source format in {path}")

    valid = urls
    console.print(f"  [dim]Loaded {len(valid)} video URL(s) from {path}[/dim]")
    return valid, username


def _load_urls_from_file(path: Path) -> list[str]:
    """Compatibility wrapper returning only URLs from a download source."""
    return _load_download_source(path)[0]
