# Changelog

All notable changes to BroadcastX are documented in this file.

## [0.3.0] - 2026-10-16

### Highlights

- Recommend `broadcastx homemonitor` for routine monitoring. It periodically
  loads the logged-in account's Home **Live on X** list and filters selected
  users instead of scanning each user's timeline. Avoiding per-user timeline
  scans is intended to reduce network traffic; bandwidth savings have not been
  benchmarked. Discovery is limited to broadcasts X exposes on that Home list.
- Keep the original `broadcastx monitor` available for direct profile scanning
  and separate live-status checks. It has not been removed or deprecated.

### Added

- Added Home monitoring with a default 120-second interval, configured or explicit
  user selection, visible Broadcast-card discovery, and headless discovery from
  Home's existing `fleetline` response. Spaces are ignored.
- Persisted active Home broadcast links across restarts and logged Home events
  under the output root. When a previously seen link disappears from a successful
  Home check, start downloading immediately without a separate end-status check.
  Retain state after incomplete checks or failed downloads; `--once` waits for
  downloads started in that round.
- Added native X/Twitter Post video and photo downloads, including mixed-media
  posts and mixed broadcast/Post batches. Existing files are skipped; failures
  preserve completed downloads and produce a nonzero exit code.
- Added `scan --all` to discover target-authored Post videos and photos alongside
  broadcasts, excluding ordinary reposts and media owned by quoted posts.
- Added configurable Post video filenames using `{datetime}`, `{tweet_id}`, and
  `{media_id}`, with global and per-user timezone and naming settings.

### Fixed

- Kept profile monitoring running through transient browser failures and timeouts.
- Retried transient Home monitor browser-profile locks during startup.
- Stopped Home monitoring on detection-related HTTP 429 responses while reporting
  and ignoring unrelated `ViewerBadgeCounts` rate limits.
- Closed the temporary Home browser after each check round.

### Upgrade notes

- Existing `monitor` commands and configuration remain available. To use Home
  monitoring, replace `broadcastx monitor` with `broadcastx homemonitor`; existing
  `[[users]]`, `output_root`, and download naming settings are reused.
- Home monitoring shares `[monitor].browser` and `[monitor].download`, but uses
  its own 120-second interval and visible-browser default. Set `--check-interval`
  or `--headless` explicitly; the profile monitor's interval and headless config
  do not control Home monitoring. There is no `[homemonitor]` config section.
- Log in once in visible mode before using headless monitoring. Supplying a user
  does not guarantee their broadcasts appear on Home. Use `monitor` when direct
  profile discovery is needed; Home disappearance is a download trigger, not
  confirmation that a broadcast has ended.
- Home events and state default to `output/homemonitor_events.json` and
  `output/homemonitor_state.json`; downloaded videos retain the per-user layout.

## [0.2.1] - 2026-08-28

### Added

- Added automatic `broadcastx.toml` discovery with an explicit `--config` override.
- Added global monitor, scan, and download settings plus per-user timezone and
  filename overrides.
- Added config-driven monitoring with no positional usernames and CLI subset
  selection when usernames are supplied.

### Changed

- Organized scan results, monitor events, videos, and rotation sidecars under
  `output/<username>/` by default.
- Named videos from the broadcast datetime and ID using a configurable,
  timezone-aware stem shared by the MP4 and rotation sidecar.
- Automatically resolved a direct broadcast URL's user from yt-dlp metadata.
- Treated `--output-dir` on monitor and download as an output-root override.

### Compatibility

- Kept config-free scan, monitor, and download commands available with built-in
  defaults.
- Left existing output files untouched; BroadcastX does not migrate or delete
  the pre-0.2.1 layout.

## [0.2.0] - 2026-08-27

### Changed

- Consolidated historical timeline discovery on the `scan` command.
- Made timeline capture independent of X GraphQL operation names.
- Filtered broadcast discovery to posts attributable to the requested user,
  excluding nested quotes and reposts from other authors.
- Tracked target post IDs to determine scrolling progress and tolerate temporary
  X timeline loading stalls.
- Shared value-free X login-state checks between scanning and monitoring.
- Saved partial scan results atomically on timeouts and recoverable failures.
- Limited terminal summaries while preserving complete JSON output.

### Fixed

- Prevented login time from being counted as timeline idle time.
- Detected logged-out profile pages that do not redirect to a login URL.
- Recovered safely from malformed existing scan result files.

### Removed

- Removed the duplicate `scrape` command and its direct GraphQL replay
  implementation.
- Removed the unused `twscrape` dependency and its transitive dependencies.

### Packaging

- Excluded local agent and code-index state from source distributions.
