from __future__ import annotations

import unittest
from datetime import date

from automation.ui import date_in_range


class DateInRangeTests(unittest.TestCase):
    def test_empty_range_keeps_everything(self) -> None:
        self.assertTrue(date_in_range(None, None, None))
        self.assertTrue(date_in_range("2026-10-10T12:00:00", None, None))

    def test_only_start_keeps_that_day_and_later(self) -> None:
        start = date(2026, 10, 10)
        self.assertFalse(date_in_range("2026-10-09T23:59:00", start, None))
        self.assertTrue(date_in_range("2026-10-10T00:00:00", start, None))
        self.assertTrue(date_in_range("2026-11-01T09:00:00", start, None))

    def test_only_end_keeps_that_day_and_earlier(self) -> None:
        end = date(2026, 10, 30)
        self.assertTrue(date_in_range("2026-10-30T23:59:00", None, end))
        self.assertFalse(date_in_range("2026-10-31T00:00:00", None, end))

    def test_same_start_and_end_is_one_day(self) -> None:
        day = date(2026, 10, 25)
        self.assertTrue(date_in_range("2026-10-25T15:00:00", day, day))
        self.assertFalse(date_in_range("2026-10-24T15:00:00", day, day))
        self.assertFalse(date_in_range("2026-10-26T15:00:00", day, day))

    def test_row_without_date_is_hidden_when_range_is_set(self) -> None:
        self.assertFalse(date_in_range(None, date(2026, 10, 1), None))
        self.assertFalse(date_in_range("", None, date(2026, 10, 1)))


if __name__ == "__main__":
    unittest.main()
