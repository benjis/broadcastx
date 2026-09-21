"""Shared data models for BroadcastX.

Used by the scanner and other modules that need to represent discovered
broadcast information.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .config import BROADCAST_PATTERNS, normalize_broadcast_url


@dataclass
class BroadcastInfo:
    """Information about a discovered broadcast."""

    broadcast_id: str
    url: str
    tweet_text: str | None = None
    tweet_url: str | None = None
    tweet_id: str | None = None
    created_at: str | None = None
    user_name: str | None = None

    def to_dict(self) -> dict:
        return {
            k: v
            for k, v in {
                "broadcast_id": self.broadcast_id,
                "url": self.url,
                "tweet_text": self.tweet_text,
                "tweet_url": self.tweet_url,
                "tweet_id": self.tweet_id,
                "created_at": self.created_at,
                "user_name": self.user_name,
            }.items()
            if v is not None
        }


@dataclass(frozen=True)
class MediaInfo:
    """Downloadable media owned by one target-authored post."""

    media_id: str
    media_type: str
    url: str
    tweet_id: str
    tweet_url: str
    created_at: str | None = None
    user_name: str | None = None

    def to_dict(self) -> dict:
        return {
            key: value
            for key, value in {
                "media_id": self.media_id,
                "media_type": self.media_type,
                "url": self.url,
                "tweet_id": self.tweet_id,
                "tweet_url": self.tweet_url,
                "created_at": self.created_at,
                "user_name": self.user_name,
            }.items()
            if value is not None
        }


def _looks_like_tweet(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    legacy = obj.get("legacy")
    if not isinstance(legacy, dict):
        return False
    tweet_id = legacy.get("id_str") or obj.get("rest_id")
    return bool(
        tweet_id
        and (
            "full_text" in legacy
            or "entities" in legacy
            or "card" in obj
            or "note_tweet" in obj
        )
    )


def _iter_tweets(obj: object) -> Iterator[dict]:
    """Yield tweet-shaped objects from arbitrary GraphQL response wrappers."""
    if isinstance(obj, dict):
        if _looks_like_tweet(obj):
            yield obj
        for value in obj.values():
            yield from _iter_tweets(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_tweets(item)


def _iter_timeline_tweets(obj: object) -> Iterator[dict]:
    """Yield timeline tweet wrappers without descending into quote/repost payloads."""
    if isinstance(obj, dict):
        if _looks_like_tweet(obj):
            yield obj
            return
        for value in obj.values():
            yield from _iter_timeline_tweets(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_timeline_tweets(item)


def _iter_dicts(obj: object) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_dicts(item)


def _direct_screen_name(obj: dict) -> str | None:
    for container_name in ("legacy", "core"):
        container = obj.get(container_name)
        if isinstance(container, dict) and isinstance(container.get("screen_name"), str):
            return container["screen_name"]
    screen_name = obj.get("screen_name")
    return screen_name if isinstance(screen_name, str) else None


def _screen_name(tweet: dict) -> str | None:
    """Read the author handle across known User result shapes."""
    core = tweet.get("core")
    if not isinstance(core, dict):
        return None
    user_results = core.get("user_results")
    if not isinstance(user_results, dict):
        return None
    user = user_results.get("result")
    while isinstance(user, dict) and isinstance(user.get("result"), dict):
        user = user["result"]
    if not isinstance(user, dict):
        return None
    return _direct_screen_name(user)


def _target_user_ids(data: object, target: str) -> set[str]:
    user_ids: set[str] = set()
    for obj in _iter_dicts(data):
        screen_name = _direct_screen_name(obj)
        if screen_name is None or screen_name.casefold() != target:
            continue
        user_id = obj.get("rest_id") or obj.get("id_str")
        if user_id is not None:
            user_ids.add(str(user_id))
    return user_ids


def _is_target_tweet(tweet: dict, target: str, target_user_ids: set[str]) -> bool:
    author = _screen_name(tweet)
    if author is not None:
        return author.casefold() == target
    legacy = tweet.get("legacy")
    if not isinstance(legacy, dict):
        return False
    user_id = legacy.get("user_id_str")
    return user_id is not None and str(user_id) in target_user_ids


def _tweet_id(tweet: dict) -> str | None:
    legacy = tweet.get("legacy")
    if not isinstance(legacy, dict):
        return None
    value = legacy.get("id_str") or tweet.get("rest_id")
    return str(value) if value is not None else None


def extract_post_ids_from_response(data: dict, username: str) -> set[str]:
    """Return IDs for posts that can be attributed to ``username``."""
    target = username.lstrip("@").casefold()
    target_user_ids = _target_user_ids(data, target)
    return {
        tweet_id
        for tweet in _iter_timeline_tweets(data)
        if _is_target_tweet(tweet, target, target_user_ids)
        if (tweet_id := _tweet_id(tweet)) is not None
    }


def _iter_strings(obj: object) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_strings(item)


def _tweet_payload_strings(tweet: dict) -> Iterator[str]:
    """Yield strings owned by one tweet without entering quoted/reposted tweets."""
    legacy = tweet.get("legacy")
    if isinstance(legacy, dict):
        legacy = {
            key: value
            for key, value in legacy.items()
            if key not in {"quoted_status_result", "retweeted_status_result"}
        }
        yield from _iter_strings(legacy)

    for key in ("card", "note_tweet", "entities"):
        payload = tweet.get(key)
        if isinstance(payload, (dict, list)):
            yield from _iter_strings(payload)


def _is_repost(tweet: dict) -> bool:
    legacy = tweet.get("legacy")
    return bool(
        isinstance(legacy, dict)
        and isinstance(legacy.get("retweeted_status_result"), dict)
    ) or isinstance(tweet.get("retweeted_status_result"), dict)


def _owned_media(tweet: dict) -> list[dict]:
    """Return only media attached directly to this tweet wrapper."""
    legacy = tweet.get("legacy")
    if not isinstance(legacy, dict):
        return []
    extended = legacy.get("extended_entities")
    if not isinstance(extended, dict):
        extended = tweet.get("extended_entities")
    media = extended.get("media") if isinstance(extended, dict) else None
    return [item for item in media if isinstance(item, dict)] if isinstance(media, list) else []


def extract_media_from_response(data: dict, username: str) -> list[MediaInfo]:
    """Extract target-owned photos and video-post URLs, excluding reposts."""
    target = username.lstrip("@").casefold()
    target_user_ids = _target_user_ids(data, target)
    media_items: list[MediaInfo] = []
    seen: set[tuple[str, str, str]] = set()

    for tweet in _iter_timeline_tweets(data):
        if not _is_target_tweet(tweet, target, target_user_ids) or _is_repost(tweet):
            continue
        tweet_id = _tweet_id(tweet)
        legacy = tweet.get("legacy")
        if tweet_id is None or not isinstance(legacy, dict):
            continue
        author = _screen_name(tweet) or username.lstrip("@")
        tweet_url = f"https://x.com/{author}/status/{tweet_id}"
        video_added = False
        for index, media in enumerate(_owned_media(tweet), 1):
            media_type = media.get("type")
            media_id = str(media.get("id_str") or media.get("media_key") or index)
            if media_type in {"video", "animated_gif"}:
                if video_added:
                    continue
                url = tweet_url
                video_added = True
                normalized_type = "video"
            elif media_type == "photo":
                url = media.get("media_url_https") or media.get("media_url")
                if not isinstance(url, str):
                    continue
                normalized_type = "photo"
            else:
                continue
            key = (tweet_id, normalized_type, url)
            if key in seen:
                continue
            seen.add(key)
            media_items.append(MediaInfo(
                media_id=media_id,
                media_type=normalized_type,
                url=url,
                tweet_id=tweet_id,
                tweet_url=tweet_url,
                created_at=legacy.get("created_at"),
                user_name=author,
            ))
    return media_items


def extract_broadcasts_from_response(data: dict, username: str) -> list[BroadcastInfo]:
    """Extract broadcasts from posts authored by ``username`` in a GraphQL response."""
    target = username.lstrip("@").casefold()
    target_user_ids = _target_user_ids(data, target)
    broadcasts: list[BroadcastInfo] = []
    seen_ids: set[str] = set()

    for tweet in _iter_timeline_tweets(data):
        author = _screen_name(tweet)
        if not _is_target_tweet(tweet, target, target_user_ids):
            continue

        legacy = tweet.get("legacy")
        if not isinstance(legacy, dict):
            continue
        tweet_id = _tweet_id(tweet)
        author = author or username.lstrip("@")
        tweet_url = f"https://x.com/{author}/status/{tweet_id}" if tweet_id else None

        for value in _tweet_payload_strings(tweet):
            for pattern in BROADCAST_PATTERNS:
                match = pattern.search(value)
                if not match:
                    continue
                broadcast_id = match.group(1)
                if broadcast_id in seen_ids:
                    continue
                seen_ids.add(broadcast_id)
                broadcasts.append(
                    BroadcastInfo(
                        broadcast_id=broadcast_id,
                        url=normalize_broadcast_url(value) or value,
                        tweet_text=legacy.get("full_text"),
                        tweet_url=tweet_url,
                        tweet_id=tweet_id,
                        created_at=legacy.get("created_at"),
                        user_name=author,
                    )
                )

    return broadcasts
