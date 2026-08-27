import tomllib
import unittest
from pathlib import Path

from broadcastx import __version__


class VersionTests(unittest.TestCase):
    def test_runtime_and_package_versions_match(self):
        project = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text()
        )

        self.assertEqual(__version__, project["project"]["version"])


if __name__ == "__main__":
    unittest.main()
