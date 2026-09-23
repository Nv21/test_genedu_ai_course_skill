import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wiki_trends.cache import DiskCache


class TestDiskCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = DiskCache(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        self.cache.set("k", {"a": 1})
        self.assertEqual(self.cache.get("k"), {"a": 1})

    def test_missing_key_is_none(self):
        self.assertIsNone(self.cache.get("missing"))

    def test_get_or_fetch_calls_fetch_only_once(self):
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return 42

        v1, hit1 = self.cache.get_or_fetch("x", fetch)
        v2, hit2 = self.cache.get_or_fetch("x", fetch)
        self.assertEqual(v1, 42)
        self.assertEqual(v2, 42)
        self.assertFalse(hit1)
        self.assertTrue(hit2)
        self.assertEqual(calls["n"], 1)  # second call was served from cache

    def test_max_age_expires_entry(self):
        self.cache.set("k", "old")
        time.sleep(0.05)
        self.assertIsNone(self.cache.get("k", max_age_seconds=0.001))
        self.assertEqual(self.cache.get("k", max_age_seconds=1000), "old")


if __name__ == "__main__":
    unittest.main()
