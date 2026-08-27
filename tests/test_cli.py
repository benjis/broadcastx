import unittest

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


if __name__ == "__main__":
    unittest.main()
