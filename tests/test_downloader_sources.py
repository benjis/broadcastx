import json

from broadcastx.downloader import _load_download_source


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
