"""Shared data models for BroadcastX.

Used by scanner, scraper, and other modules that need to represent
discovered broadcast information.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import BROADCAST_PATTERNS, normalize_broadcast_url


@dataclass
class ScanResult:
    """Result from scanning a user's timeline for broadcasts."""

    username: str
    broadcasts: list[BroadcastInfo] = field(default_factory=list)
    scrolls_performed: int = 0
    errors: list[str] = field(default_factory=list)


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


def extract_broadcasts_from_response(data: dict, username: str) -> list[BroadcastInfo]:
    """Recursively search a Twitter GraphQL JSON response for broadcast URLs."""
    broadcasts: list[BroadcastInfo] = []
    seen_ids: set[str] = set()

    def _check_url(url_str: str, context: dict) -> None:
        for pattern in BROADCAST_PATTERNS:
            match = pattern.search(url_str)
            if match:
                bid = match.group(1)
                if bid not in seen_ids:
                    seen_ids.add(bid)
                    tweet_url = None
                    if context.get("tweet_id"):
                        tweet_url = (
                            f"https://x.com/{username}/status/{context['tweet_id']}"
                        )
                    broadcasts.append(
                        BroadcastInfo(
                            broadcast_id=bid,
                            url=normalize_broadcast_url(url_str) or url_str,
                            tweet_text=context.get("tweet_text"),
                            tweet_url=tweet_url,
                            tweet_id=context.get("tweet_id"),
                            created_at=context.get("created_at"),
                            user_name=username,
                        )
                    )

    def _walk(obj: object, context: dict | None = None) -> None:
        if context is None:
            context = {}
        if isinstance(obj, dict):
            legacy = obj.get("legacy", {})
            if isinstance(legacy, dict) and legacy.get("full_text"):
                context = {
                    **context,
                    "tweet_text": legacy["full_text"],
                    "tweet_id": legacy.get("id_str") or obj.get("rest_id"),
                    "created_at": legacy.get("created_at"),
                }
            entities = (
                legacy.get("entities", {}) if isinstance(legacy, dict) else {}
            )
            if isinstance(entities, dict):
                for url_entity in entities.get("urls", []):
                    if isinstance(url_entity, dict):
                        _check_url(url_entity.get("expanded_url", ""), context)
            card = obj.get("card", {})
            if isinstance(card, dict):
                bvs = card.get("legacy", {}).get("binding_values", [])
                if isinstance(bvs, list):
                    for bv in bvs:
                        if isinstance(bv, dict):
                            sval = bv.get("value", {}).get("string_value", "")
                            if sval:
                                _check_url(sval, context)
            for v in obj.values():
                _walk(v, context)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, context)

    _walk(data)
    return broadcasts
