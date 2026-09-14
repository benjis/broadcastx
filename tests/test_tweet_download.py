import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from broadcastx.cli import main
from broadcastx.config import AppConfig, ConfigError, DownloadConfig, format_video_stem, load_config, normalize_tweet_url
from broadcastx.downloader import DownloadResult, _load_download_source, download_all, download_video

TWEET = "https://x.com/Alice/status/123456"
BROADCAST = "https://x.com/i/broadcasts/1vAxRkBbDRzKl"


@pytest.mark.parametrize("host", ["x.com", "twitter.com", "www.x.com", "m.twitter.com", "mobile.x.com"])
def test_tweet_aliases(host):
    assert normalize_tweet_url(f"https://{host}/Alice/status/123456/video/2?s=20") == "https://x.com/i/web/status/123456"
    assert normalize_tweet_url(f"http://{host}/i/web/status/123456/") == "https://x.com/i/web/status/123456"


@pytest.mark.parametrize("url", ["https://x.com/Alice", "https://x.com.evil/Alice/status/123", "https://x.com@evil/Alice/status/123", "https://youtube.com/watch?v=123", "https://evil/?url=" + BROADCAST, "https://x.com/Alice/status/123bad", "https://x.com/Alice/status/0", None])
def test_invalid_links_are_failures(url):
    with patch("broadcastx.downloader.subprocess.run") as run:
        assert not download_video(url).success
        run.assert_not_called()


def media(id="456", **overrides):
    return {"id": id, "uploader_id": "Alice", "timestamp": 1787918400,
            "extractor_key": "Twitter", "formats": [{"url": "https://video.twimg.com/video.mp4", "ext": "mp4"}], **overrides}


def fake_download(commands, fail_ids=()):
    def start(cmd, **kwargs):
        commands.append(cmd)
        info = json.loads(Path(cmd[cmd.index("--load-info-json") + 1]).read_text())
        path = Path(cmd[cmd.index("--output") + 1].replace("%(ext)s", "mp4"))
        failed = info.get("id") in fail_ids
        if not failed:
            path.write_bytes(b"video")
        process = Mock(returncode=1 if failed else 0)
        # An iterable pipe with close(), like Popen's text stream.
        import io
        process.stdout = io.StringIO("ERROR: unavailable\n" if failed else "BX_FILE:" + json.dumps(str(path)) + "\n")
        return process
    return start


def invoke_download(tmp_path, payload, fail_ids=()):
    commands = []
    with (patch("broadcastx.downloader.subprocess.run", return_value=Mock(stdout=json.dumps(payload))),
          patch("broadcastx.downloader.subprocess.Popen", side_effect=fake_download(commands, fail_ids)),
          patch("broadcastx.downloader.extract_rotation_sidecar") as rotation):
        result = download_video(TWEET, app_config=AppConfig(output_root=tmp_path), browser="firefox")
        rotation.assert_not_called()
    return result, commands


def test_single_video_and_repeat_skip(tmp_path):
    result, commands = invoke_download(tmp_path, media())
    assert result.success
    assert result.output_files == [result.output_file]
    assert Path(result.output_file).name == "2026-08-28_12-00-00_123456_456.mp4"
    assert Path(result.output_file).parent == tmp_path / "alice" / "videos"
    assert commands[0][commands[0].index("--cookies-from-browser") + 1] == "firefox"
    assert not list((tmp_path / "alice" / "videos").glob(".tweet-*"))
    Path(result.output_file).write_bytes(b"keep")
    repeated, commands = invoke_download(tmp_path, media())
    assert repeated.success and not commands
    assert Path(repeated.output_file).read_bytes() == b"keep"


def test_multi_video_partial_failure_preserves_success_and_continues(tmp_path):
    payload = {"_type": "playlist", "entries": [media("1"), media("2"), media("3")]}
    result, commands = invoke_download(tmp_path, payload, fail_ids=("2",))
    assert not result.success
    assert "Media 2" in result.error
    assert len(result.output_files) == 2 and len(commands) == 3
    assert all(Path(p).is_file() for p in result.output_files)
    assert len({Path(cmd[cmd.index("--load-info-json") + 1]).parent for cmd in commands}) == 3


def test_multi_video_success(tmp_path):
    result, _ = invoke_download(tmp_path, {"_type": "playlist", "entries": [media("1"), media("2")]})
    assert result.success and len(result.output_files) == 2


def test_concurrent_downloads_do_not_overwrite_or_share_temporary_files(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    commands = []
    start = fake_download(commands)
    def concurrent_start(cmd, **kwargs):
        barrier.wait(timeout=5)
        return start(cmd, **kwargs)
    with (patch("broadcastx.downloader.subprocess.run", return_value=Mock(stdout=json.dumps(media()))),
          patch("broadcastx.downloader.subprocess.Popen", side_effect=concurrent_start),
          ThreadPoolExecutor(max_workers=2) as executor):
        futures = [executor.submit(download_video, TWEET, output_dir=tmp_path) for _ in range(2)]
        results = [future.result() for future in futures]
    assert all(result.success for result in results)
    assert results[0].output_files == results[1].output_files
    assert len({cmd[cmd.index("--load-info-json") + 1] for cmd in commands}) == 2
    assert not list(tmp_path.glob(".tweet-*"))


def test_exit_zero_without_final_file_is_failure(tmp_path):
    import io
    process = Mock(returncode=0, stdout=io.StringIO("[download] finished\n"))
    with (patch("broadcastx.downloader.subprocess.run", return_value=Mock(stdout=json.dumps(media()))),
          patch("broadcastx.downloader.subprocess.Popen", return_value=process)):
        result = download_video(TWEET, output_dir=tmp_path)
    assert not result.success and "final video file" in result.error


def test_missing_metadata_uses_unknown_and_stable_indices(tmp_path):
    result, _ = invoke_download(tmp_path, {"_type": "playlist", "entries": [media(None, uploader_id=None, timestamp=None), media(None, uploader_id=None, timestamp=None)]})
    assert result.success and result.username == "unknown"
    assert [Path(p).name for p in result.output_files] == ["123456_1.mp4", "123456_2.mp4"]
    assert "No timestamp" in result.warning


@pytest.mark.parametrize("payload", [{"_type": "playlist", "entries": []}, {"_type": "url", "url": "https://youtube.com/watch?v=1"}, media(extractor_key="Youtube"), media(formats=[]), None])
def test_no_video_or_external_metadata_fails(tmp_path, payload):
    result, commands = invoke_download(tmp_path, payload)
    assert not result.success and not commands


def test_probe_error_is_reported():
    with patch("broadcastx.downloader.subprocess.run", side_effect=subprocess.CalledProcessError(1, "yt-dlp", stderr="Login required")):
        assert "Login required" in download_video(TWEET).error


def test_broadcast_dispatch_keeps_existing_pipeline():
    with patch("broadcastx.downloader.download_broadcast", return_value=DownloadResult(BROADCAST, True)) as download:
        assert download_video(BROADCAST).success
        download.assert_called_once()


@pytest.mark.parametrize("parallel", [1, 3])
def test_mixed_sources_dedup_order_and_failure(tmp_path, parallel):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"username": "Bob", "broadcasts": [BROADCAST, TWEET]}))
    calls = []
    def download(url, **kwargs):
        calls.append((url, kwargs.get("username")))
        return DownloadResult(url, not url.startswith("https://evil"))
    with patch("broadcastx.downloader.download_video", side_effect=download):
        results = download_all([TWEET + "/video/2?s=20", "https://evil/"], from_file=source, parallel=parallel)
    assert [r.url for r in results] == [normalize_tweet_url(TWEET), "https://evil/", BROADCAST]
    assert dict(calls)[BROADCAST] == "bob"
    assert dict(calls)[normalize_tweet_url(TWEET)] is None
    assert not results[1].success


def test_plain_json_sources_accept_both_link_types(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps([TWEET, {"url": BROADCAST}]))
    assert _load_download_source(source)[0] == [TWEET, BROADCAST]


def test_cli_exit_and_help():
    runner = CliRunner()
    assert "All videos" in " ".join(runner.invoke(main, ["download", "--help"]).output.split())
    with patch("broadcastx.cli._require_dependencies"), patch("broadcastx.cli.download_all", return_value=[DownloadResult(TWEET, False)]):
        assert runner.invoke(main, ["download", TWEET]).exit_code == 1


def test_cli_output_root_and_success(tmp_path):
    with patch("broadcastx.cli._require_dependencies"), patch("broadcastx.cli.download_all", return_value=[DownloadResult(TWEET, True)]) as download:
        result = CliRunner().invoke(main, ["download", TWEET, "-o", str(tmp_path)])
    assert result.exit_code == 0
    assert download.call_args.kwargs["app_config"].output_root == tmp_path


def test_invalid_source_does_not_cancel_direct_links(tmp_path):
    with patch("broadcastx.downloader.download_video", return_value=DownloadResult(TWEET, True)):
        results = download_all([TWEET], from_file=tmp_path / "missing.json")
    assert results[0].success and not results[1].success


def test_user_video_template_and_old_template_compatibility(tmp_path):
    path = tmp_path / "broadcastx.toml"
    path.write_text('[download]\nvideo_filename_template = "{tweet_id}_{media_id}"\n[[users]]\nusername = "Alice"\ntimezone = "Australia/Sydney"\nvideo_filename_template = "{tweet_id}_{media_id}_{datetime}"')
    config = load_config(path)
    assert config.download.filename_template == "{datetime}_{broadcast_id}"
    assert format_video_stem(1787918400, "123", "456", config.download_for("alice")) == "123_456_2026-08-28_22-00-00"
    assert format_video_stem(None, "123", "456", DownloadConfig()) == "123_456"


@pytest.mark.parametrize("template", ["{tweet_id}", "{media_id}", "../{tweet_id}_{media_id}", "{title}_{tweet_id}_{media_id}", "{tweet_id!r}_{media_id}", "{tweet_id:>10}_{media_id}", "", "{tweet_id}_{media_id}_{"])
def test_invalid_video_templates(tmp_path, template):
    path = tmp_path / "broadcastx.toml"
    path.write_text('[download]\nvideo_filename_template = ' + json.dumps(template))
    with pytest.raises(ConfigError):
        load_config(path)
