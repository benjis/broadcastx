"""HLS playlist resolution and HTTP helpers for broadcast metadata extraction."""

from __future__ import annotations

import subprocess
import urllib.parse
import urllib.request


def _resolve_hls_playlist_url(url: str, browser: str) -> str:
    cmd = [
        "yt-dlp",
        "--cookies-from-browser",
        browser,
        "--no-warnings",
        "--print",
        "urls",
        url,
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not urls:
        raise RuntimeError("yt-dlp did not return an HLS URL")
    return urls[-1]


def _parse_media_playlist(playlist_text: str, playlist_url: str) -> list[dict]:
    segments = []
    program_date_time: str | None = None
    for line in playlist_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            program_date_time = line.split(":", 1)[1]
        elif not line.startswith("#"):
            segments.append({
                "index": len(segments),
                "url": urllib.parse.urljoin(playlist_url, line),
                "program_date_time": program_date_time,
            })
            program_date_time = None
    return segments


def _ensure_media_playlist(playlist_text: str, playlist_url: str) -> tuple[str, str]:
    if "#EXT-X-STREAM-INF" not in playlist_text:
        return playlist_text, playlist_url

    expect_variant = False
    for line in playlist_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            expect_variant = True
        elif expect_variant and line and not line.startswith("#"):
            variant_url = urllib.parse.urljoin(playlist_url, line)
            return _http_text(variant_url), variant_url
    return playlist_text, playlist_url


def _http_text(url: str) -> str:
    return _http_bytes(url).decode("utf-8")


def _http_bytes(url: str, byte_range: int | None = None) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://x.com/",
    }
    if byte_range is not None:
        headers["Range"] = f"bytes=0-{byte_range}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()
