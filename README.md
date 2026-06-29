# BroadcastX

<a href="README_ZH.md">🇨🇳 中文版</a> | <a href="README.md">🇬🇧 English</a>

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Discover, monitor, and download X/Twitter broadcast videos from user timelines.

**BroadcastX** is a CLI tool that helps you:

- **Scan** — Find broadcast links in a user's timeline
- **Download** — Download broadcast videos with automatic phone-rotation correction
- **Monitor** — Watch a profile for live broadcasts and auto-download replays

## Features

### Scan

Uses Playwright browser automation to scroll through a user's X profile and intercept GraphQL API responses to extract broadcast URLs. More reliable than DOM scraping.

### Download with Auto-Rotation

Downloads broadcast videos via `yt-dlp` and post-processes the video to correct phone orientation. Broadcasts streamed from a phone in portrait mode appear upright after processing. A `.rotation.jsonl` sidecar file is written alongside the video for inspection.

### Monitor

Continuously monitors a user's profile. When a live broadcast is detected, periodically checks its status. When the broadcast ends, automatically downloads the replay.


## Prerequisites

- **Python 3.11+**
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — `brew install yt-dlp`
- **[ffmpeg](https://ffmpeg.org/)** — `brew install ffmpeg`
- **Google Chrome** (installed separately)

## Installation

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install BroadcastX and dependencies
pip install -e .

# Install Playwright's browser driver
playwright install chromium
```

## Quick Start

```bash
# Scan a user's timeline for broadcast links
broadcastx scan @username

# Download broadcasts from scan results
broadcastx download --from output/broadcasts.json

# Monitor a user for live broadcasts
broadcastx monitor @username
```

## Usage

### Scan a timeline for broadcasts

```bash
broadcastx scan @username

# Options:
#   --max-scrolls 100      Maximum scroll actions
#   --scroll-delay 2.0     Seconds between scrolls
#   --idle-timeout 10.0    Stop after N seconds with no new data
#   --output FILE          Output path (default: output/broadcasts.json)
#   --headless             Run browser without visible window
```

The scanner opens the user's X profile in Chrome, scrolls through the timeline, and intercepts API responses. Broadcast URLs are extracted from tweet cards. If you are not logged in, the browser shows the login page — log in manually, then press Enter in the terminal to continue. Your session is saved to `~/.broadcastx/chrome-profile/` for future runs.

### Download broadcasts

```bash
# Single broadcast
broadcastx download https://x.com/i/broadcasts/1vAxRkBbDRzKl

# From scan results
broadcastx download --from output/broadcasts.json

# Multiple concurrent downloads
broadcastx download --from output/broadcasts.json -p 3

# Custom output directory
broadcastx download --from output/broadcasts.json -o ./videos

# Use Firefox cookies
broadcastx download --from output/broadcasts.json --browser firefox

# Verbose yt-dlp output
broadcastx download --from output/broadcasts.json -v
```

BroadcastX **automatically corrects phone rotation**: if the broadcast carries phone-orientation metadata in the HLS stream, the video is re-encoded so it displays upright in any player.

### Monitor a profile for live broadcasts

```bash
broadcastx monitor @username

# One-shot test cycle (no loop)
broadcastx monitor @username --once

# Download to a custom directory
broadcastx monitor @username -o ./my_videos

# Custom check intervals (seconds)
broadcastx monitor @username --check-interval 1800 --live-interval 300

# Detect only, skip download
broadcastx monitor @username --no-download
```

The monitor runs in a loop:

1. **Profile check** (every `check-interval`, default 30 min) — Opens the profile and looks for broadcast cards.
2. **Live detection** — When a candidate is found, checks whether it is currently live.
3. **Live check** (every `live-interval`, default 5 min) — Re-checks status until the broadcast ends.
4. **Download** — Downloads the replay automatically.

Events are logged to `output/monitor_events.json`.
### Scrape all past broadcasts

```bash
broadcastx scrape @username

# Ignore saved state and start from the beginning
broadcastx scrape @username --fresh

# Add delay and verbose output
broadcastx scrape @username --delay 2.0 -v

# Supply credentials directly (skips browser login)
broadcastx scrape @username \
  --auth-token "your_auth_token" \
  --csrf-token "your_ct0" \
  --user-id "1234567890"
```

Uses GraphQL API pagination with cursor-based resumption for full history traversal. State is saved locally, so you can pause and resume after rate limits.

## Output Structure

```
output/
├── broadcasts.json          # Scan results
├── monitor_events.json      # Monitor event log
└── videos/
    ├── [title] [id].mp4     # Downloaded broadcast
    ├── [id].rotation.jsonl  # Rotation timeline sidecar
    └── ...
```

## Pipeline Examples

```bash
# Scan + download all found broadcasts
broadcastx scan @username
broadcastx download --from output/broadcasts.json

# Monitor with auto-download
broadcastx monitor @username -o ./videos

# Bulk scrape + download
broadcastx scrape @username
broadcastx download --from output/username_broadcasts.json
```

## How It Works

### Scanner
Uses Playwright to intercept Twitter's GraphQL API responses (`UserTweets` / `TweetDetail`). This is more stable than DOM scraping because JSON response structures change less frequently than HTML.

### Downloader
Wraps `yt-dlp` (which has a built-in `TwitterBroadcastIE` extractor) and adds:
- **Rotation sidecar extraction** — Parses timed-ID3 metadata from HLS segments
- **Auto-rotation** — Re-encodes the video with correct orientation via ffmpeg

### Rotation Sidecar
The JSONL sidecar (`[id].rotation.jsonl`) contains one record per HLS segment:
- `raw_rotation` — Original sensor angle from Periscope
- `rotation` — Quantized to 0°, 90°, 180°, or 270° with hysteresis
- `ntp` — NTP timestamp for timeline reconstruction

## License

MIT
