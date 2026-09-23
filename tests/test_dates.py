import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wiki_trends.dates import parse_boundary, to_api_str


class TestParseBoundary(unittest.TestCase):
    def test_full_date_passthrough(self):
        self.assertEqual(parse_boundary("2024-03-15", is_end=False), date(2024, 3, 15))

    def test_year_month_as_start_is_first_of_month(self):
        self.assertEqual(parse_boundary("2024-03", is_end=False), date(2024, 3, 1))

    def test_year_month_as_end_is_last_of_month(self):
        self.assertEqual(parse_boundary("2024-02", is_end=True), date(2024, 2, 29))  # leap year

    def test_compact_yyyymmdd(self):
        self.assertEqual(parse_boundary("20240115", is_end=False), date(2024, 1, 15))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_boundary("not-a-date", is_end=False)


class TestToApiStr(unittest.TestCase):
    def test_format(self):
        self.assertEqual(to_api_str(date(2024, 3, 5)), "20240305")


if __name__ == "__main__":
    unittest.main()
