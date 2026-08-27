# Changelog

All notable changes to BroadcastX are documented in this file.

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
