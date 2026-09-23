"""Tests for STL decomposition and the multi-horizon view (no network).

Run with:  python3 -m unittest discover -s tests   (from the skill root)
"""
import random
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wiki_trends import horizons as hz
from wiki_trends.stats import analyze_series


def _monthly(values, start_year=2023, start_month=9):
    out, y, m = [], start_year, start_month
    for v in values:
        out.append({"date": f"{y:04d}-{m:02d}-01", "views": v})
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _seasonal_growth(n=36, monthly_growth=1.02, peak=2.5, noise=0.08, seed=3):
    """Known answer: trend doubles over 36 months (1.02**35 ~ 2.0), with a
    2.5x peak every September on top."""
    rnd = random.Random(seed)
    return [int(1000 * monthly_growth ** i * (peak if i % 12 == 0 else 1)
                * (1 + rnd.uniform(-noise, noise))) for i in range(n)]


class TestSTL(unittest.TestCase):
    def test_recovers_known_trend_through_strong_seasonality(self):
        t = analyze_series(_monthly(_seasonal_growth()))
        self.assertTrue(t.deseasonalized)
        self.assertEqual(t.seasonal_peak_month, "09")
        self.assertGreater(t.seasonal_strength, 0.4)
        self.assertAlmostEqual(t.trend_change_pct, 100.0, delta=20.0)
        self.assertEqual(t.confidence, "high")

    def test_deseasonalizing_raises_fit_quality(self):
        t = analyze_series(_monthly(_seasonal_growth()))
        # fitted on the adjusted series: the September peaks no longer
        # count as noise
        self.assertGreater(t.r_squared, 0.8)
        # the yearly September peaks are seasonality, not anomalies
        self.assertEqual([a for a in t.anomalies if a["date"][5:7] == "09"], [])

    def test_no_stl_for_short_or_non_monthly_series(self):
        self.assertIsNone(analyze_series(_monthly([100] * 20)).seasonal_strength)
        daily = [{"date": f"2026-01-{d:02d}", "views": 100 + d} for d in range(1, 29)]
        self.assertIsNone(analyze_series(daily).seasonal_strength)

    def test_tiny_regular_wobble_is_not_treated_as_seasonality(self):
        values = [100, 104, 98, 101, 99, 103, 97, 102, 100, 98, 101, 99] * 3
        t = analyze_series(_monthly(values))
        self.assertFalse(t.deseasonalized)
        self.assertEqual(t.seasonal_months, [])

    def test_one_off_burst_is_not_seasonality(self):
        # en "Oliver Tree" 2024-09..2026-08 (thousands): one June burst
        values = [24, 19, 29, 20, 18, 19, 60, 18, 17, 14, 12, 16,
                  13, 17, 15, 16, 19, 15, 22, 49, 36, 4891, 510, 258]
        t = analyze_series(_monthly([v * 1000 for v in values], 2024, 9))
        self.assertFalse(t.deseasonalized)
        self.assertTrue(t.spike_sensitive or t.anomalies)

    def test_two_unrelated_events_in_the_same_month_are_not_seasonality(self):
        # en "Gianni Infantino": July 2025 (x2.6) and July 2026 (x46)
        values = [15, 22, 19, 36, 32, 17, 50, 20, 51, 67, 79, 28,
                  25, 41, 51, 182, 55, 55, 46, 54, 65, 712, 2533, 535]
        t = analyze_series(_monthly([v * 1000 for v in values], 2024, 9))
        self.assertFalse(t.deseasonalized)

    def test_seasonal_factors_adjust_a_short_window(self):
        t3 = analyze_series(_monthly(_seasonal_growth()))
        last12 = _monthly(_seasonal_growth())[-12:]
        t1 = analyze_series(last12, seasonal_factors=t3.seasonal_factors)
        self.assertTrue(t1.deseasonalized)
        self.assertGreater(t1.adjusted_growth_pct, 0)


class TestWindows(unittest.TestCase):
    def test_windows_end_at_last_complete_week_and_month(self):
        w = hz.windows(date(2026, 9, 23))  # a Wednesday
        self.assertEqual(w["3m_weekly"][1], date(2026, 9, 20))   # last Sunday
        self.assertEqual(w["3m_weekly"][0], date(2026, 6, 22))   # Monday, 13 weeks earlier
        self.assertEqual(w["1y"], (date(2025, 9, 1), date(2026, 8, 31), "monthly"))
        self.assertEqual(w["3y"][0], date(2023, 9, 1))

    def test_to_weekly_drops_incomplete_weeks(self):
        days = [{"date": f"2026-06-{d:02d}", "views": 10} for d in range(22, 31)]  # Mon 22 .. Tue 30
        weeks = hz.to_weekly(days)
        self.assertEqual(weeks, [{"date": "2026-06-22", "views": 70}])

    def test_weekly_factor_uses_month_of_the_weeks_thursday(self):
        factors = {"2024-09-01": 2.0, "2025-09-01": 3.0, "2025-08-01": 0.5}
        out = hz.weekly_seasonal_factors([{"date": "2026-08-31"}, {"date": "2026-08-24"}], factors)
        self.assertEqual(out["2026-08-31"], 3.0)  # Thu Sep 3 -> September, latest year
        self.assertEqual(out["2026-08-24"], 0.5)  # Thu Aug 27 -> August


class TestSummarize(unittest.TestCase):
    @staticmethod
    def _r(growth, conf="high", trend=None, trend12=None):
        t = analyze_series(_monthly([100] * 6))
        t.growth_pct, t.confidence = growth, conf
        t.trend_change_pct, t.trend_change_12m_pct, t.adjusted_growth_pct = trend, trend12, None
        return t

    def test_long_decline_that_flattened(self):
        s = hz.summarize({"3y": self._r(-50, trend=-40, trend12=2),
                          "1y": self._r(1), "3m_weekly": self._r(0, "low")})
        self.assertEqual(s["pattern"], "decline_flattening")
        self.assertEqual(s["directions"]["3m_weekly"], "unclear")
        self.assertEqual(s["change_basis"]["1y"], "stl_trend_12m")

    def test_low_confidence_year_is_unclear_not_flat(self):
        s = hz.summarize({"3y": self._r(-30, trend=-30, trend12=-10),
                          "1y": self._r(-10, "low"), "3m_weekly": self._r(-7)})
        self.assertEqual(s["directions"]["1y"], "unclear")
        self.assertEqual(s["pattern"], "mixed")


if __name__ == "__main__":
    unittest.main()
