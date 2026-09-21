# BroadcastX

<a href="README_ZH.md">🇨🇳 中文版</a> | <a href="README.md">🇬🇧 English</a>

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/) [![PyPI](https://img.shields.io/pypi/v/broadcastx.svg)](https://pypi.org/project/broadcastx/)

Discover, monitor, and download X/Twitter broadcast videos from user timelines.

**BroadcastX** is a CLI tool that helps you:

- **Scan** — Find broadcast links in a user's timeline
- **Download** — Download broadcast videos with automatic phone-rotation correction
- **Watermark** — Add customizable text watermarks to videos via ffmpeg
- **Monitor** — Watch a profile for live broadcasts and auto-download replays

## Features

### Scan

Uses Playwright browser automation to scroll through a user's X profile and intercept GraphQL API responses to extract broadcast URLs. More reliable than DOM scraping.

### Download with Auto-Rotation

Downloads broadcast videos via `yt-dlp` and post-processes the video to correct phone orientation. Broadcasts streamed from a phone in portrait mode appear upright after processing. A `.rotation.jsonl` sidecar file is written alongside the video for inspection.

### Monitor

Continuously monitors a user's profile. When a live broadcast is detected, periodically checks its status. When the broadcast ends, automatically downloads the replay.

### Watermark

Adds customizable text watermarks to downloaded videos using ffmpeg's `drawtext` filter. The watermark text is rendered in the bottom-right corner by default, with options for font, size, opacity, color, and position (bottom-right, bottom-left, top-right, top-left).


## Prerequisites

- **Python 3.11+**
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — `brew install yt-dlp`
- **[ffmpeg](https://ffmpeg.org/)** — `brew install ffmpeg`
- **Google Chrome** (installed separately)

## Installation

```bash
# Install BroadcastX and dependencies
uv sync

# Install Playwright's browser driver
uv run playwright install chromium
```

## Quick Start

```bash
# Scan a user's timeline for broadcast links
broadcastx scan @username

# Include the user's own videos and photos (reposts are excluded)
broadcastx scan @username --all

# Download broadcasts from scan results
broadcastx download --from output/username/broadcasts.json

# Monitor every user in broadcastx.toml
broadcastx monitor

# Monitor the single "Live on X" area without visiting user profiles
broadcastx homemonitor
```

## Configuration

Copy [`broadcastx.example.toml`](broadcastx.example.toml) to `broadcastx.toml`
in the directory where you run BroadcastX. It is discovered automatically;
use `--config PATH` only when the config lives elsewhere.

```toml
output_root = "output"

[monitor]
check_interval_seconds = 1800
live_interval_seconds = 300
download = true

[download]
timezone = "UTC"
datetime_format = "%Y-%m-%d_%H-%M-%S"
filename_template = "{datetime}_{broadcast_id}"
video_filename_template = "{datetime}_{tweet_id}_{media_id}"

[[users]]
username = "Alice"
timezone = "Australia/Sydney"

[[users]]
username = "Bob"
timezone = "Asia/Ho_Chi_Minh"
```

Explicit CLI options override user settings, which override command-wide
settings, which override built-in defaults. A relative `output_root` is
resolved from the config file's directory.

## Usage

### Scan a timeline for broadcasts

```bash
broadcastx scan @username

# Options:
#   --max-scrolls 500      Safety cap for scroll actions
#   --scroll-delay 2.0     Seconds between scrolls
#   --idle-timeout 20.0    Stop after N seconds with no new target posts
#   --output FILE          Override path (default: output/<user>/broadcasts.json)
#   --headless             Run browser without visible window
#   --all                  Include original post videos and photos; exclude reposts
```

The scanner opens the user's X profile in Chrome, scrolls through the timeline, and intercepts API responses. Broadcast URLs are extracted from tweet cards. If you are not logged in, the browser shows the login page — log in manually, then press Enter in the terminal to continue. Your session is saved to `~/.broadcastx/chrome-profile/` for future runs.

With `--all`, the same `broadcasts.json` also contains a `media` array. Videos are represented by their post URL so the downloader fetches every video in that post. Photos use their direct X media URL. Replies are included; plain reposts are excluded. For quote posts, only media attached by the target user is included, not media from the quoted post.

### Download broadcasts and post videos

```bash
# Single broadcast
broadcastx download https://x.com/i/broadcasts/1vAxRkBbDRzKl

# Ordinary post: download every video or photo (also accepts twitter.com links)
broadcastx download https://x.com/username/status/1234567890123456789

# Mix broadcasts and ordinary posts
broadcastx download https://x.com/i/broadcasts/1vAxRkBbDRzKl https://x.com/username/status/1234567890123456789

# From scan results; Broadcasts, videos, and photos can be mixed
broadcastx download --from output/username/broadcasts.json

# Multiple concurrent downloads
broadcastx download --from output/username/broadcasts.json -p 3

# Custom output root
broadcastx download --from output/username/broadcasts.json -o ./archive

# Use Firefox cookies
broadcastx download --from output/username/broadcasts.json --browser firefox

# Verbose yt-dlp output
broadcastx download --from output/username/broadcasts.json -v
```

BroadcastX **automatically corrects phone rotation**: if the broadcast carries phone-orientation metadata in the HLS stream, the video is re-encoded so it displays upright in any player.

For a direct URL, BroadcastX reads yt-dlp's uploader ID and broadcast timestamp,
then selects the matching user config and output directory automatically. If
the user cannot be identified, it uses global settings and
`output/unknown/videos/`.

Ordinary video posts use `video_filename_template`, globally or in `[[users]]`.
It accepts `{datetime}`, `{tweet_id}`, and `{media_id}`; both IDs are required.
The existing timezone and datetime format settings apply. Missing timestamps
fall back to `<tweet_id>_<media_id>`; missing media IDs use stable media indices.
Videos are saved under `<output_root>/<user>/videos/`; scanned photos are saved
under `<output_root>/<user>/photos/`. Existing files are
skipped without overwriting. Post `/video/N` links download the entire post.

`--from` also accepts mixed URL arrays or arrays of `{"url": "..."}` objects.
Image-only post URLs are resolved automatically and every photo owned by the
post is downloaded. Mixed-media posts download videos first, then photos, and
return all files as one result. Invalid links, posts without native media, and permission errors are reported
as failures. Other downloads continue, and any failure yields a nonzero exit
code; successfully downloaded media is retained. External video site links
are not followed. Broadcast rotation correction remains unchanged; ordinary
post downloads do not create rotation sidecars. Monitor still discovers
Broadcast links only; Scan includes post media when `--all` is supplied.

For scanner JSON, `download --from` reads `broadcasts` first and `media`
second, deduplicating by first occurrence. The default `--parallel 1` processes
that order strictly. With `--parallel N`, completion output may appear in any
order, while returned results retain the source order.

### Monitor profiles for live broadcasts

```bash
# Monitor a single user
broadcastx monitor @username

# Monitor multiple users simultaneously (shares one Chromium profile)
broadcastx monitor @SpaceX @NASA @elonmusk

# Monitor every [[users]] entry in broadcastx.toml
broadcastx monitor

# Select one configured user while retaining that user's settings
broadcastx monitor Bob

# One-shot test cycle (no loop)
broadcastx monitor @username --once

# Use a custom output root
broadcastx monitor @username --output-dir ./archive

# Custom check intervals (seconds)
broadcastx monitor @username --check-interval 1800 --live-interval 300

# Detect only, skip download
broadcastx monitor @username --no-download
```

The monitor runs in a loop:

1. **Profile check** (every `check-interval`, default 30 min) — Opens each profile and looks for broadcast cards. The interval is measured from the start of each check round, so scan time is not added to it.
2. **Live detection** — When a candidate is found, checks whether it is currently live.
3. **Live check** (every `live-interval`, default 5 min) — Re-checks status until the broadcast ends.
4. **Download** — Downloads the replay automatically.

Multiple users share a single Chromium profile, so you only need to log in once.

Events are logged per user to `output/<username>/monitor_events.json`.

### Monitor Live on X from Home

```bash
# Monitor configured [[users]] from one Home page every two minutes
broadcastx homemonitor

# Monitor a selected subset or run one detection cycle
broadcastx homemonitor @SpaceX @NASA
broadcastx homemonitor --once

# Use the Home fleetline response when X does not render cards headlessly
broadcastx homemonitor --headless
```

`homemonitor` is independent from `monitor`. It loads `https://x.com/home`,
finds Broadcast entries in **Live on X**, ignores Spaces, and filters them to
the configured or explicitly supplied users. Visible mode opens the rendered
Broadcast cards; headless mode reads live-video records from the Home page's
existing `fleetline` response. Neither mode visits every user profile.

New Broadcast URLs are printed and logged to
`output/homemonitor_events.json`. Active links are stored atomically in
`output/homemonitor_state.json`, so they survive a process restart. When a
link disappears from Home, its Broadcast page must confirm that it ended
before the replay is downloaded. State is retained after an inconclusive
check or failed download and cleared only after a successful download (or
after end confirmation with `--no-download`). Detection-related HTTP 429
responses stop the process; X's unrelated `ViewerBadgeCounts` 429 is reported
and ignored. The temporary browser closes after every check round.

### Add watermarks to videos

```bash
# Add a default "broadcastx" watermark to the bottom-right of a video
broadcastx watermark video.mp4

# Custom text, font, and size
broadcastx watermark video.mp4 --text "My Channel" --font Arial --font-size 36

# Custom opacity, color, and position
broadcastx watermark video.mp4 --opacity 0.5 --color "#FF5733" --position top-left

# Write to a new file instead of overwriting
broadcastx watermark video.mp4 --output watermarked.mp4

# Dry-run to preview settings without running ffmpeg
broadcastx watermark video.mp4 --dry-run
```

The watermark command uses ffmpeg's `drawtext` filter. Stock ffmpeg builds include `libfreetype` and support this filter out of the box.

Options:
| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--text` | `-t` | `broadcastx` | Watermark text |
| `--font` | `-f` | `sans-serif` | Font family or path |
| `--font-size` | `-s` | `24` | Font size in points |
| `--opacity` | `-o` | `0.7` | Text opacity (0.0–1.0) |
| `--color` | `-c` | `white` | Font color name or hex |
| `--position` | `-p` | `bottom-right` | One of: `bottom-right`, `bottom-left`, `top-right`, `top-left` |
| `--output` | `-O` | *(in-place)* | Output video path |
| `--dry-run` | | *(off)* | Preview without executing |

## Output Structure

```
output/
├── alice/
│   ├── broadcasts.json
│   ├── monitor_events.json
│   └── videos/
│       ├── 2026-08-28_22-00-00_[id].mp4
│       └── 2026-08-28_22-00-00_[id].rotation.jsonl
└── bob/
    └── ...
```

## Pipeline Examples

```bash
# Scan + download all found broadcasts
broadcastx scan @username
broadcastx download --from output/username/broadcasts.json

# Monitor with auto-download
broadcastx monitor @username --output-dir ./archive

# Watermark a downloaded video
broadcastx watermark ./output/username/videos/*.mp4 --text "broadcastx" --output ./output/username/videos/watermarked/
```

## How It Works

### Scanner
Uses Playwright to intercept timeline GraphQL responses generated by X itself. It tracks target-authored post IDs for progress and extracts broadcasts only from posts attributable to the requested user, without depending on a fixed GraphQL operation name.

### Downloader
Wraps `yt-dlp` (which has a built-in `TwitterBroadcastIE` extractor) and adds:
- **Rotation sidecar extraction** — Parses timed-ID3 metadata from HLS segments
- **Auto-rotation** — Re-encodes the video with correct orientation via ffmpeg

### Rotation Sidecar
The JSONL sidecar shares its filename stem with the MP4 and contains one record per HLS segment:
- `raw_rotation` — Original sensor angle from Periscope
- `rotation` — Quantized to 0°, 90°, 180°, or 270° with hysteresis
- `ntp` — NTP timestamp for timeline reconstruction

## License

MIT
