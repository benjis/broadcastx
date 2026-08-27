# Changelog

All notable changes to BroadcastX are documented in this file.

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
