"""Shared configuration and constants for BroadcastX."""

import re
from pathlib import Path

# Default output directory (relative to cwd)
DEFAULT_OUTPUT_DIR = Path("output")
DEFAULT_VIDEOS_DIR = DEFAULT_OUTPUT_DIR / "videos"
DEFAULT_BROADCASTS_FILE = DEFAULT_OUTPUT_DIR / "broadcasts.json"

# Browser to extract cookies from (for yt-dlp)
DEFAULT_BROWSER = "chrome"

# X broadcast IDs observed in real broadcast URLs are opaque alphanumeric
# tokens, e.g. 1vAxRkBbDRzKl. Reject tiny fragments such as /broadcasts/1.
BROADCAST_ID_RE = r"[A-Za-z0-9]{8,}"

# Broadcast URL patterns to match
BROADCAST_PATTERNS = [
    re.compile(rf"https?://(?:x|twitter)\.com/i/broadcasts/({BROADCAST_ID_RE})(?![A-Za-z0-9_])"),
    re.compile(rf"https?://(?:www\.)?pscp\.tv/w/({BROADCAST_ID_RE})(?![A-Za-z0-9_])"),
]

# Twitter GraphQL endpoints to intercept
GRAPHQL_ENDPOINTS = [
    "UserTweets",
    "UserTweetsAndReplies",
    "TweetDetail",
    "SearchTimeline",
]

# Scanner defaults
DEFAULT_MAX_SCROLLS = 100       # Maximum number of scroll actions
DEFAULT_SCROLL_DELAY = 2.0      # Seconds between scrolls
DEFAULT_IDLE_TIMEOUT = 10.0     # Stop after N seconds with no new tweets
DEFAULT_HEADLESS = False        # Show browser by default (useful for login)

# yt-dlp output template — uses broadcast ID as filename
YTDLP_OUTPUT_TEMPLATE = "%(id)s [%(timestamp>%Y-%m-%d %H.%M.%S)s] %(title)s.%(ext)s"


def ensure_output_dirs():
    """Create output directories if they don't exist."""
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)


def extract_broadcast_id(url: str) -> str | None:
    """Extract broadcast ID from a broadcast URL."""
    for pattern in BROADCAST_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def is_broadcast_url(url: str) -> bool:
    """Check if a URL is a broadcast URL."""
    return extract_broadcast_id(url) is not None


def normalize_broadcast_url(url: str) -> str | None:
    """Normalize a broadcast URL to the canonical x.com format."""
    bid = extract_broadcast_id(url)
    if bid:
        return f"https://x.com/i/broadcasts/{bid}"
    return None
