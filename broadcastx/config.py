"""Shared constants and TOML configuration for BroadcastX."""

import re
import string
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# Scanner defaults
DEFAULT_MAX_SCROLLS = 500       # Safety cap; idle detection normally stops first
DEFAULT_SCROLL_DELAY = 2.0      # Seconds between scrolls
DEFAULT_IDLE_TIMEOUT = 20.0     # Tolerate transient X timeline loading stalls
DEFAULT_HEADLESS = False        # Show browser by default (useful for login)

# yt-dlp output template — uses broadcast ID as filename
YTDLP_OUTPUT_TEMPLATE = "%(id)s [%(timestamp>%Y-%m-%d %H.%M.%S)s] %(title)s.%(ext)s"

DEFAULT_CONFIG_FILE = "broadcastx.toml"
DEFAULT_DATETIME_FORMAT = "%Y-%m-%d_%H-%M-%S"
DEFAULT_FILENAME_TEMPLATE = "{datetime}_{broadcast_id}"


class ConfigError(ValueError):
    """Raised when a BroadcastX configuration file is invalid."""


@dataclass(frozen=True)
class DownloadConfig:
    timezone: str = "UTC"
    datetime_format: str = DEFAULT_DATETIME_FORMAT
    filename_template: str = DEFAULT_FILENAME_TEMPLATE


@dataclass(frozen=True)
class MonitorConfig:
    check_interval_seconds: int = 30 * 60
    live_interval_seconds: int = 5 * 60
    browser: str = DEFAULT_BROWSER
    headless: bool = False
    download: bool = True


@dataclass(frozen=True)
class ScanConfig:
    max_scrolls: int = DEFAULT_MAX_SCROLLS
    scroll_delay_seconds: float = DEFAULT_SCROLL_DELAY
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT
    headless: bool = DEFAULT_HEADLESS


@dataclass(frozen=True)
class UserConfig:
    username: str
    timezone: str | None = None
    datetime_format: str | None = None
    filename_template: str | None = None


@dataclass(frozen=True)
class AppConfig:
    output_root: Path = DEFAULT_OUTPUT_DIR
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    users: tuple[UserConfig, ...] = ()
    source: Path | None = None

    def user(self, username: str) -> UserConfig | None:
        key = normalize_username(username)
        return next((user for user in self.users if user.username == key), None)

    def download_for(self, username: str | None) -> DownloadConfig:
        if not username or not (user := self.user(username)):
            return self.download
        resolved = replace(
            self.download,
            timezone=user.timezone or self.download.timezone,
            datetime_format=user.datetime_format or self.download.datetime_format,
            filename_template=user.filename_template or self.download.filename_template,
        )
        _validate_download_config(resolved, f"user @{user.username}")
        return resolved


def normalize_username(username: str) -> str:
    """Return the stable directory/config key for an X username."""
    if not isinstance(username, str):
        raise ConfigError(f"Invalid X username: {username!r}")
    normalized = username.strip().lstrip("@").casefold()
    if not normalized or not re.fullmatch(r"[a-z0-9_]{1,15}", normalized):
        raise ConfigError(f"Invalid X username: {username!r}")
    return normalized


def user_output_dir(output_root: Path, username: str) -> Path:
    return Path(output_root) / normalize_username(username)


def format_output_stem(timestamp: int | float | None, broadcast_id: str, config: DownloadConfig) -> str:
    """Format the shared video/sidecar stem from broadcast metadata."""
    if timestamp is None:
        return broadcast_id
    local = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(ZoneInfo(config.timezone))
    rendered = config.filename_template.format(
        datetime=local.strftime(config.datetime_format),
        broadcast_id=broadcast_id,
    )
    if not rendered or rendered in {".", ".."} or "/" in rendered or "\\" in rendered:
        raise ConfigError("Filename settings produced a path instead of a file name")
    return rendered


def discover_config_path(explicit: Path | str | None = None, cwd: Path | None = None) -> Path | None:
    """Resolve an explicit config, otherwise discover ./broadcastx.toml."""
    if explicit is not None:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ConfigError(f"Configuration file not found: {path}")
        return path
    candidate = (cwd or Path.cwd()) / DEFAULT_CONFIG_FILE
    return candidate.resolve() if candidate.is_file() else None


def _table(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _only_keys(table: dict, allowed: set[str], label: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"Unknown {label} option(s): {', '.join(unknown)}")


def _validate_download_config(config: DownloadConfig, label: str = "[download]") -> None:
    for field_name in ("timezone", "datetime_format", "filename_template"):
        if not isinstance(getattr(config, field_name), str):
            raise ConfigError(f"{label} {field_name} must be a string")
    try:
        ZoneInfo(config.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Invalid timezone for {label}: {config.timezone!r}") from exc

    fields = []
    try:
        for _, field_name, format_spec, conversion in string.Formatter().parse(config.filename_template):
            if field_name:
                fields.append(field_name)
            if format_spec or conversion:
                raise ConfigError(f"Format specs and conversions are not allowed in {label} filename_template")
    except ValueError as exc:
        raise ConfigError(f"Invalid filename_template for {label}: {exc}") from exc
    unknown = sorted(set(fields) - {"datetime", "broadcast_id"})
    if unknown:
        raise ConfigError(f"Unknown filename field(s) for {label}: {', '.join(unknown)}")
    if "broadcast_id" not in fields:
        raise ConfigError(f"{label} filename_template must include {{broadcast_id}}")

    try:
        sample = config.filename_template.format(
            datetime=datetime(2026, 8, 28).strftime(config.datetime_format),
            broadcast_id="1vAxRkBbDRzKl",
        )
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"Invalid datetime_format for {label}: {exc}") from exc
    if not sample or sample in {".", ".."} or "/" in sample or "\\" in sample:
        raise ConfigError(f"{label} filename settings must produce a file name, not a path")


def load_config(explicit: Path | str | None = None, cwd: Path | None = None) -> AppConfig:
    """Load and validate BroadcastX configuration, or return built-in defaults."""
    path = discover_config_path(explicit, cwd)
    if path is None:
        return AppConfig(output_root=(cwd or Path.cwd()) / DEFAULT_OUTPUT_DIR)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Could not read configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Configuration root must be a TOML table")
    _only_keys(data, {"output_root", "monitor", "download", "scan", "users"}, "top-level")

    monitor_data = _table(data, "monitor")
    download_data = _table(data, "download")
    scan_data = _table(data, "scan")
    _only_keys(monitor_data, {"check_interval_seconds", "live_interval_seconds", "browser", "headless", "download"}, "[monitor]")
    _only_keys(download_data, {"timezone", "datetime_format", "filename_template"}, "[download]")
    _only_keys(scan_data, {"max_scrolls", "scroll_delay_seconds", "idle_timeout_seconds", "headless"}, "[scan]")

    monitor = MonitorConfig(**monitor_data)
    download = DownloadConfig(**download_data)
    scan = ScanConfig(**scan_data)
    _validate_download_config(download)
    if (
        not isinstance(monitor.check_interval_seconds, int)
        or isinstance(monitor.check_interval_seconds, bool)
        or not isinstance(monitor.live_interval_seconds, int)
        or isinstance(monitor.live_interval_seconds, bool)
        or not isinstance(monitor.browser, str)
        or not isinstance(monitor.headless, bool)
        or not isinstance(monitor.download, bool)
    ):
        raise ConfigError("[monitor] has an invalid value type")
    if monitor.check_interval_seconds <= 0 or monitor.live_interval_seconds <= 0:
        raise ConfigError("Monitor intervals must be greater than zero")
    if (
        not isinstance(scan.max_scrolls, int)
        or isinstance(scan.max_scrolls, bool)
        or not isinstance(scan.scroll_delay_seconds, (int, float))
        or isinstance(scan.scroll_delay_seconds, bool)
        or not isinstance(scan.idle_timeout_seconds, (int, float))
        or isinstance(scan.idle_timeout_seconds, bool)
        or not isinstance(scan.headless, bool)
    ):
        raise ConfigError("[scan] has an invalid value type")
    if scan.max_scrolls <= 0 or scan.scroll_delay_seconds < 0 or scan.idle_timeout_seconds <= 0:
        raise ConfigError("Scan limits must be positive (scroll delay may be zero)")

    raw_users = data.get("users", [])
    if not isinstance(raw_users, list):
        raise ConfigError("[[users]] must be an array of TOML tables")
    users = []
    seen = set()
    for index, raw in enumerate(raw_users, 1):
        if not isinstance(raw, dict):
            raise ConfigError(f"[[users]] entry {index} must be a table")
        _only_keys(raw, {"username", "timezone", "datetime_format", "filename_template"}, f"[[users]] entry {index}")
        if "username" not in raw:
            raise ConfigError(f"[[users]] entry {index} is missing username")
        user = UserConfig(username=normalize_username(raw["username"]), **{k: v for k, v in raw.items() if k != "username"})
        if user.username in seen:
            raise ConfigError(f"Duplicate user in configuration: @{user.username}")
        seen.add(user.username)
        users.append(user)

    raw_root = Path(data.get("output_root", DEFAULT_OUTPUT_DIR)).expanduser()
    output_root = raw_root if raw_root.is_absolute() else path.parent / raw_root
    config = AppConfig(
        output_root=output_root.resolve(),
        monitor=monitor,
        download=download,
        scan=scan,
        users=tuple(users),
        source=path,
    )
    for user in users:
        config.download_for(user.username)
    return config


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
