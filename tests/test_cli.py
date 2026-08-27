import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from broadcastx.cli import main


class CliTests(unittest.TestCase):
    def test_timeline_discovery_has_only_scan_command(self):
        result = CliRunner().invoke(main, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("scan", main.commands)
        self.assertNotIn("scrape", main.commands)
        self.assertIn("scan", result.output)
        self.assertNotIn("scrape", result.output)

    def test_scan_help_describes_progress_safety_limits(self):
        result = CliRunner().invoke(main, ["scan", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("[default: 500]", result.output)
        self.assertIn("no new target posts", result.output)

    def test_monitor_without_usernames_uses_automatically_discovered_config(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("broadcastx.toml").write_text(
                """
[monitor]
check_interval_seconds = 60
live_interval_seconds = 10
download = false

[[users]]
username = "Alice"
timezone = "Australia/Sydney"
""".strip()
            )
            monitor_users = AsyncMock()
            with patch("broadcastx.cli.monitor_users", monitor_users):
                result = runner.invoke(main, ["monitor"])

        self.assertEqual(result.exit_code, 0, result.output)
        kwargs = monitor_users.await_args.kwargs
        self.assertEqual(kwargs["usernames"], ["alice"])
        self.assertEqual(kwargs["check_interval"], 60)
        self.assertEqual(kwargs["live_interval"], 10)
        self.assertFalse(kwargs["download"])
        self.assertEqual(kwargs["app_config"].download_for("alice").timezone, "Australia/Sydney")

    def test_monitor_cli_usernames_select_configured_subset(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("broadcastx.toml").write_text(
                """
[monitor]
download = false

[[users]]
username = "Alice"
timezone = "Australia/Sydney"

[[users]]
username = "Bob"
timezone = "Asia/Ho_Chi_Minh"
""".strip()
            )
            monitor_users = AsyncMock()
            with patch("broadcastx.cli.monitor_users", monitor_users):
                result = runner.invoke(main, ["monitor", "BOB"])

        self.assertEqual(result.exit_code, 0, result.output)
        kwargs = monitor_users.await_args.kwargs
        self.assertEqual(kwargs["usernames"], ["bob"])
        self.assertEqual(kwargs["app_config"].download_for("bob").timezone, "Asia/Ho_Chi_Minh")

    def test_monitor_without_users_or_config_explains_requirement(self):
        with CliRunner().isolated_filesystem():
            result = CliRunner().invoke(main, ["monitor", "--no-download"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Provide USERNAMES", result.output)

    def test_download_automatically_loads_user_configuration(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("broadcastx.toml").write_text(
                """
[[users]]
username = "Alice"
timezone = "Australia/Sydney"
""".strip()
            )
            with (
                patch("broadcastx.cli._require_dependencies"),
                patch("broadcastx.cli.download_all", return_value=[]) as download_all,
            ):
                result = runner.invoke(
                    main,
                    ["download", "https://x.com/i/broadcasts/1vAxRkBbDRzKl"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        config = download_all.call_args.kwargs["app_config"]
        self.assertEqual(config.download_for("alice").timezone, "Australia/Sydney")


if __name__ == "__main__":
    unittest.main()
