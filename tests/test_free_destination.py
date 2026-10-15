from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from automation.ui import free_destination


class FreeDestinationTests(unittest.TestCase):
    def test_keeps_name_when_folder_is_free(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(free_destination(Path(folder), "счёт.pdf"), Path(folder) / "счёт.pdf")

    def test_adds_number_instead_of_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "счёт.pdf").write_bytes(b"1")
            (root / "счёт (2).pdf").write_bytes(b"2")
            self.assertEqual(free_destination(root, "счёт.pdf"), root / "счёт (3).pdf")


if __name__ == "__main__":
    unittest.main()
