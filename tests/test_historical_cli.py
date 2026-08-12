from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = TOOLS_DIR / "src"
sys.path.insert(0, str(SOURCE_DIR))

from conversation_archive.chatgpt.historical_index import build_parser


class HistoricalCliTests(unittest.TestCase):
    def test_full_rebuild_is_the_default(self) -> None:
        self.assertTrue(build_parser().parse_args([]).reset)
        self.assertFalse(build_parser().parse_args(["--incremental"]).reset)

    def test_help_does_not_require_existing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment["CHATGPT_EXPORT_CONFIG"] = str(
                Path(temporary) / "missing-config.json"
            )
            environment["PYTHONPATH"] = str(SOURCE_DIR)
            result = subprocess.run(
                [sys.executable, "-m", "conversation_archive.chatgpt.historical_index", "--help"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--archive-root", result.stdout)


if __name__ == "__main__":
    unittest.main()
