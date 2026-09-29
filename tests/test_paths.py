from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.paths import AppPaths


class AppPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.environment = {"LOCALAPPDATA": str(self.base)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def discover(self) -> Path:
        with patch.dict("os.environ", self.environment, clear=True):
            return AppPaths.discover().root

    def test_new_installation_uses_new_folder(self) -> None:
        self.assertEqual(self.discover(), (self.base / "AutomationSystem").resolve())

    def test_existing_legacy_folder_keeps_its_data(self) -> None:
        (self.base / "RosaMailCollector").mkdir()
        self.assertEqual(self.discover(), (self.base / "RosaMailCollector").resolve())

    def test_new_folder_wins_over_legacy(self) -> None:
        (self.base / "RosaMailCollector").mkdir()
        (self.base / "AutomationSystem").mkdir()
        self.assertEqual(self.discover(), (self.base / "AutomationSystem").resolve())

    def test_legacy_environment_override_still_works(self) -> None:
        self.environment["ROSA_MAIL_DATA_DIR"] = str(self.base / "custom")
        self.assertEqual(self.discover(), (self.base / "custom").resolve())


if __name__ == "__main__":
    unittest.main()
