import json
from unittest.mock import Mock, patch

from broadcastx.downloader import _load_download_source, probe_broadcast_metadata


def test_scanner_source_carries_username_into_download_context(tmp_path):
    source = tmp_path / "broadcasts.json"
    source.write_text(json.dumps({
        "username": "@Alice",
        "broadcasts": [
            {"url": "https://x.com/i/broadcasts/1vAxRkBbDRzKl"},
        ],
    }))

    urls, username = _load_download_source(source)

    assert urls == ["https://x.com/i/broadcasts/1vAxRkBbDRzKl"]
    assert username == "alice"


def test_scanner_source_includes_all_media_urls(tmp_path):
    source = tmp_path / "broadcasts.json"
    source.write_text(json.dumps({
        "username": "Alice",
        "broadcasts": [{"url": "https://x.com/i/broadcasts/1vAxRkBbDRzKl"}],
        "media": [
            {"media_type": "video", "url": "https://x.com/Alice/status/123"},
            {"media_type": "photo", "url": "https://pbs.twimg.com/media/photo.jpg"},
        ],
    }))

    urls, username = _load_download_source(source)

    assert urls == [
        "https://x.com/i/broadcasts/1vAxRkBbDRzKl",
        "https://x.com/Alice/status/123",
        "https://pbs.twimg.com/media/photo.jpg",
    ]
    assert username == "alice"


def test_metadata_probe_reads_owner_and_utc_timestamp():
    completed = Mock(stdout=json.dumps({"uploader_id": "Alice", "timestamp": 1787918400}))
    with patch("broadcastx.downloader.subprocess.run", return_value=completed) as run:
        metadata = probe_broadcast_metadata(
            "https://x.com/i/broadcasts/1vAxRkBbDRzKl",
            browser="chrome",
        )

    assert metadata.uploader_id == "Alice"
    assert metadata.timestamp == 1787918400
    assert "--skip-download" in run.call_args.args[0]
