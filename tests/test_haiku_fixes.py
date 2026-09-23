"""Regression tests for the issues found by running the skill's example
queries through Claude Haiku 4.5 (see references/project-guide.md §9.1):
seasonal series misread as a steep decline, "least decline" ranked as the
best audience, and numbers in the recommendation not checked against data.

Run with:  python3 -m unittest discover -s tests   (from the skill root)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wiki_trends.check import check_recommendation
from wiki_trends.pipeline import _explore_next
from wiki_trends.stats import _confidence_label, analyze_series


def _monthly(values, start_year=2023, start_month=9):
    out, y, m = [], start_year, start_month
    for v in values:
        out.append({"date": f"{y:04d}-{m:02d}-01", "views": v})
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


# Shape of uk "Астрономія" 2023-09..2026-08: a big September peak every
# year on top of a decline that flattens out in the last year.
ASTRONOMY_LIKE = [9857, 3611, 3390, 3576, 3700, 3206, 2140, 2216, 2018, 959, 637, 754,
                  4687, 1776, 1746, 1634, 1558, 1296, 1068, 1019, 789, 361, 305, 375,
                  1642, 635, 557, 622, 449, 425, 410, 405, 600, 281, 322, 360]


class TestYearOverYear(unittest.TestCase):
    def test_yoy_compares_same_calendar_months(self):
        t = analyze_series(_monthly(ASTRONOMY_LIKE))
        # Jun-Aug 2026 (281, 322, 360) vs Jun-Aug 2025 (361, 305, 375)
        self.assertAlmostEqual(t.yoy_growth_pct, (321 / 347 - 1) * 100, delta=0.2)
        # ...which is far milder than the first-3 vs last-3 figure
        self.assertLess(t.growth_pct, -85)

    def test_yoy_is_none_for_short_series(self):
        self.assertIsNone(analyze_series(_monthly([100] * 6 + [120] * 6)).yoy_growth_pct)


class TestSeasonality(unittest.TestCase):
    def test_repeating_september_peak_is_detected(self):
        self.assertIn("09", analyze_series(_monthly(ASTRONOMY_LIKE)).seasonal_months)

    def test_flat_noisy_series_has_no_seasonality(self):
        values = [100, 104, 98, 101, 99, 103, 97, 102, 100, 98, 101, 99] * 2
        self.assertEqual(analyze_series(_monthly(values)).seasonal_months, [])


class TestMediumWording(unittest.TestCase):
    def test_significant_but_noisy_trend_is_not_called_insignificant(self):
        label, reason = _confidence_label(36, 0.008, 0.28, False, 100.0)
        self.assertEqual(label, "medium")
        self.assertNotIn("не строго значущий", reason)
        self.assertIn("значущий", reason)


class TestExploreNext(unittest.TestCase):
    @staticmethod
    def _trend(level, slope, yoy, conf):
        t = analyze_series(_monthly([level] * 24))
        t.avg_value, t.slope_per_period, t.yoy_growth_pct, t.confidence = level, slope, yoy, conf
        t.trend_change_12m_pct = yoy  # explore_next prefers the STL 12-month change
        t.growth_pct = -10.0 if slope < 0 else 10.0
        return t

    def test_all_declining_ranks_by_level_not_by_least_decline(self):
        order = _explore_next({
            "pl": self._trend(45, -1.0, 0.1, "high"),    # smallest decline, lowest level
            "uk": self._trend(127, -2.0, -8.1, "high"),
            "vi": self._trend(252, -3.0, -21.0, "low"),
        })
        self.assertEqual([r["lang"] for r in order], ["vi", "uk", "pl"])
        self.assertEqual(order[0]["tag"], "unclear")
        # flat YoY (+0.1%) on a confidently declining trend is not "growing"
        self.assertEqual(order[-1]["tag"], "reliably_declining")

    def test_decline_with_clear_yoy_recovery_is_turning(self):
        order = _explore_next({"pt": self._trend(54, -0.7, 7.9, "medium")})
        self.assertEqual(order[0]["tag"], "turning")


class TestRecommendationCheck(unittest.TestCase):
    def setUp(self):
        t = analyze_series(_monthly(ASTRONOMY_LIKE))
        self.trends = {"uk": t}
        self.growth = t.growth_pct

    def test_numbers_present_in_data_pass(self):
        text = f"Спад {abs(self.growth):.1f}% (довіра висока), рік до року {self.trends['uk'].yoy_growth_pct}%."
        self.assertTrue(check_recommendation(text, self.trends)["ok"])

    def test_invented_number_is_flagged(self):
        res = check_recommendation("Інтерес виріс на 42% за 2025 рік.", self.trends)
        self.assertFalse(res["ok"])
        self.assertEqual(res["unmatched"], ["42%"])

    def test_years_and_list_markers_are_ignored(self):
        self.assertTrue(check_recommendation("(1) У 2026 році - 3 місяці спаду.", self.trends)["ok"])


class TestManualTitleNotFound(unittest.TestCase):
    """Haiku run 3 passed an invented title via --articles; it came back as
    method=manual, confidence=high with zero data points."""

    def test_manual_title_without_pageviews_is_downgraded(self):
        import tempfile
        from unittest import mock
        from wiki_trends import pipeline

        class FakeClient:
            def __init__(self, *a, **k):
                self.stats = type("S", (), {"requests_made": 0, "cache_hits": 0})()
            def pageviews_per_article(self, project, title, *a, **k):
                return [] if project.startswith("pl") else _monthly([100] * 24)
            def pageviews_aggregate(self, *a, **k):
                return _monthly([1_000_000] * 24)

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(pipeline, "WikimediaClient", FakeClient):
            res = pipeline.run_study(
                topic=None, lang_codes=["pl", "cs"], start="2024-09", end="2026-08",
                out_base=f"{d}/out", cache_dir=f"{d}/cache", metric="views",
                manual_articles={"pl": "Poszczenie przerywane", "cs": "Přerušovaný půst"})
        self.assertEqual(res["resolved_articles"]["pl"]["confidence"], "none")
        self.assertEqual(res["resolved_articles"]["pl"]["method"], "manual_not_found")
        self.assertEqual(res["resolved_articles"]["cs"]["confidence"], "high")


if __name__ == "__main__":
    unittest.main()
