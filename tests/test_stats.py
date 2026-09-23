"""Unit tests for the deterministic trend-analysis layer.

Run with:  python3 -m unittest discover -s tests   (from the skill root)
No network access and no extra dependencies beyond requirements.txt.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wiki_trends.stats import analyze_series


def _points(dates, views):
    return [{"date": d, "views": v} for d, v in zip(dates, views)]


def _month_dates(n, start_year=2023, start_month=1):
    dates = []
    y, m = start_year, start_month
    for _ in range(n):
        dates.append(f"{y:04d}-{m:02d}-01")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return dates


class TestEmptySeries(unittest.TestCase):
    def test_no_data_is_reported_as_none_confidence_not_a_crash(self):
        result = analyze_series([], None)
        self.assertEqual(result.n_points, 0)
        self.assertEqual(result.confidence, "none")
        self.assertIsNone(result.growth_pct)


class TestCleanTrend(unittest.TestCase):
    def test_steady_linear_growth_is_high_confidence_and_positive(self):
        dates = _month_dates(24)
        views = [100 + 20 * i for i in range(24)]  # perfectly linear, no noise
        result = analyze_series(_points(dates, views))
        self.assertEqual(result.n_points, 24)
        self.assertGreater(result.growth_pct, 0)
        self.assertEqual(result.confidence, "high")
        self.assertLess(result.p_value, 0.05)
        self.assertGreaterEqual(result.r_squared, 0.3)

    def test_steady_linear_decline_is_high_confidence_and_negative(self):
        dates = _month_dates(24)
        views = [1000 - 20 * i for i in range(24)]
        result = analyze_series(_points(dates, views))
        self.assertLess(result.growth_pct, 0)
        self.assertEqual(result.confidence, "high")


class TestNoisyFlatSeries(unittest.TestCase):
    def test_flat_series_with_alternating_noise_is_low_confidence(self):
        dates = _month_dates(24)
        # Oscillates around a constant mean with no real trend.
        views = [100 + (10 if i % 2 == 0 else -10) for i in range(24)]
        result = analyze_series(_points(dates, views))
        self.assertIn(result.confidence, ("low", "medium"))
        # A flat oscillation must not be reported as a strong trend either way.
        self.assertNotEqual(result.confidence, "high")


class TestTooFewPoints(unittest.TestCase):
    def test_fewer_than_six_points_is_always_low_confidence(self):
        dates = _month_dates(4)
        views = [10, 1000, 2000, 3000]  # looks dramatic, but n is tiny
        result = analyze_series(_points(dates, views))
        self.assertEqual(result.confidence, "low")
        self.assertIn("Замало точок", result.confidence_reason)


class TestSpikeSensitivity(unittest.TestCase):
    def test_single_spike_driving_growth_is_flagged_and_downgraded(self):
        dates = _month_dates(20)
        views = [50] * 20
        views[-1] = 5000  # one huge one-off spike right at the end
        result = analyze_series(_points(dates, views))
        self.assertTrue(len(result.anomalies) >= 1)
        self.assertTrue(result.spike_sensitive)
        self.assertEqual(result.confidence, "low")
        self.assertIn("аномальних сплесків", result.confidence_reason)

    def test_trend_that_survives_excluding_the_spike_is_not_downgraded(self):
        dates = _month_dates(20)
        # Real, steady growth, PLUS one extra one-off spike in the middle.
        views = [100 + 15 * i for i in range(20)]
        views[10] = 5000
        result = analyze_series(_points(dates, views))
        self.assertTrue(len(result.anomalies) >= 1)
        # Growth direction should still be positive even discounting the spike.
        self.assertGreater(result.growth_pct, 0)


class TestShareMetric(unittest.TestCase):
    def test_share_metric_normalizes_by_project_totals(self):
        dates = _month_dates(12)
        views = [1000] * 12
        totals = _points(dates, [1_000_000] * 12)  # constant 0.1% share
        result = analyze_series(_points(dates, views), totals, metric="share")
        self.assertEqual(result.metric, "share_per_million")
        # 1000 / 1_000_000 * 1_000_000 = 1000 per-million, constant -> ~0 growth
        self.assertAlmostEqual(result.avg_value, 1000.0, places=1)
        self.assertAlmostEqual(result.growth_pct, 0.0, places=1)

    def test_share_metric_handles_missing_total_for_a_date(self):
        dates = _month_dates(6)
        views = [10, 20, 30, 40, 50, 60]
        totals = _points(dates[:5], [1000] * 5)  # last date's total missing
        result = analyze_series(_points(dates, views), totals, metric="share")
        self.assertEqual(result.n_points, 5)  # dropped the row with no total


if __name__ == "__main__":
    unittest.main()
