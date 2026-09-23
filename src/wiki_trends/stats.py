"""Deterministic trend analysis over a pageviews time series.

Everything here is a plain statistical computation (linear regression,
z-score outlier detection) - no LLM calls, so it is unit-testable and gives
the same answer every time for the same data. This is the part of the
skill that grounds any narrative claim ("interest is growing") in an
actual, checkable number, and it is deliberately conservative about
confidence: it is built to say "not enough signal" rather than manufacture
a trend out of noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from statsmodels.tsa.seasonal import STL


@dataclass
class TrendResult:
    n_points: int
    metric: str  # "views" | "share_per_million"
    avg_value: Optional[float]
    first_period_avg: Optional[float]
    last_period_avg: Optional[float]
    growth_pct: Optional[float]
    slope_per_period: Optional[float]
    r_squared: Optional[float]
    p_value: Optional[float]
    anomalies: list[dict]
    spike_sensitive: bool
    confidence: str  # "high" | "medium" | "low" | "none"
    confidence_reason: str
    series: list[dict] = field(default_factory=list)  # for the chart
    # Year-over-year growth: last 3 months vs the same 3 months a year
    # earlier (monthly series with >= 15 points only). Unlike first-3 vs
    # last-3 it is immune to a window that starts at a seasonal peak.
    yoy_growth_pct: Optional[float] = None
    # Calendar months ("09") with anomalies in 2+ different years - a
    # repeating seasonal spike, not a one-off event.
    seasonal_months: list[str] = field(default_factory=list)
    # STL decomposition (monthly series with >= 24 points): how much of the
    # variation is a yearly cycle (0..1), the change of the smoothed trend
    # over the whole window / the last 12 months, and whether the
    # regression, anomalies and confidence were computed on the
    # seasonally-adjusted series (done when seasonal_strength >= 0.4).
    seasonal_strength: Optional[float] = None
    trend_strength: Optional[float] = None
    trend_change_pct: Optional[float] = None
    trend_change_12m_pct: Optional[float] = None
    seasonal_peak_month: Optional[str] = None
    seasonal_amplitude: Optional[float] = None  # peak month / trough month - 1 (typical year)
    seasonal_repeats: Optional[bool] = None     # peak month stands out in >= 2 years
    deseasonalized: bool = False
    # first-3 vs last-3 on the seasonally-adjusted values (set when
    # deseasonalized) - the fair version of growth_pct for a short window
    # that crosses a seasonal peak, e.g. 13 weeks ending in September.
    adjusted_growth_pct: Optional[float] = None
    trend_series: list[dict] = field(default_factory=list)
    # {date: multiplicative seasonal factor} - lets a shorter window of the
    # same series be seasonally adjusted (see `seasonal_factors` below).
    # Internal: not written to the JSON.
    seasonal_factors: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d.pop("seasonal_factors", None)
        return d


def _linregress_safe(x: np.ndarray, y: np.ndarray):
    if len(x) < 2 or np.all(y == y[0]):
        return None
    result = scipy_stats.linregress(x, y)
    return result


def _edge_avg(values: np.ndarray, k: int) -> float:
    k = max(1, min(k, len(values)))
    return float(np.mean(values[:k])), float(np.mean(values[-k:]))


SEASONAL_STRENGTH_MIN = 0.4  # Hyndman's F_S; above this the yearly cycle dominates
# ...and it must also be big enough to matter: a perfectly regular +-4%
# wobble has high F_S but no product relevance.
SEASONAL_AMPLITUDE_MIN = 0.2  # typical peak month is >= 20% above the trough month


# ...and it must repeat: STL on only 2-3 cycles happily calls a one-off
# burst "seasonal" (en "Oliver Tree": F_S = 1.0 from a single June 2026
# spike, which then vanished from spike detection). So the peak month must
# stand out in >= 2 different years, by comparable factors.
SEASONAL_REPEAT_MIN_RATIO = 1.1   # peak month / that year's median
SEASONAL_REPEAT_MAX_SPREAD = 4.0  # strongest / weakest of those years


def _seasonality_matters(decomp: Optional[dict]) -> bool:
    return bool(decomp) and decomp["seasonal_strength"] >= SEASONAL_STRENGTH_MIN \
        and decomp["seasonal_amplitude"] >= SEASONAL_AMPLITUDE_MIN and decomp["repeats"]


def _peak_repeats(dates: list[str], values: np.ndarray, month: str) -> tuple[bool, list[float]]:
    """Per 12-month block (aligned to the series end): peak-month value /
    block median. True if >= 2 blocks exceed SEASONAL_REPEAT_MIN_RATIO and
    their ratios are within SEASONAL_REPEAT_MAX_SPREAD of each other."""
    ratios = []
    for start in range(len(values) - 12, -1, -12):
        block, bdates = values[start:start + 12], dates[start:start + 12]
        med = float(np.median(block))
        hits = [float(v) for v, d in zip(block, bdates) if d[5:7] == month]
        if hits and med > 0:
            ratios.append(round(hits[0] / med, 2))
    strong = [r for r in ratios if r >= SEASONAL_REPEAT_MIN_RATIO]
    return (len(strong) >= 2 and max(strong) / min(strong) <= SEASONAL_REPEAT_MAX_SPREAD), ratios


def analyze_series(
    points: list[dict],
    totals: Optional[list[dict]] = None,
    metric: str = "views",
    seasonal_factors: Optional[dict[str, float]] = None,
) -> TrendResult:
    """`points` / `totals`: lists of {"date": "YYYY-MM-DD", "views": int},
    as returned by `api.WikimediaClient`. `metric='share'` normalizes the
    topic's views by the language edition's total traffic for the same
    period (per million page views), which is what makes cross-language
    comparisons fair despite very different overall wiki sizes.

    `seasonal_factors` ({date: multiplicative seasonal factor}) lets a short
    window (e.g. the last 12 months) be analyzed on seasonally-adjusted
    values borrowed from a longer series' decomposition - 12 points alone
    can't separate a yearly cycle from a trend."""
    if not points:
        return TrendResult(
            n_points=0, metric=metric, avg_value=None, first_period_avg=None,
            last_period_avg=None, growth_pct=None, slope_per_period=None,
            r_squared=None, p_value=None, anomalies=[], spike_sensitive=False,
            confidence="none",
            confidence_reason="Немає даних (API не повернув перегляди для цього періоду/статті).",
            series=[],
        )

    df = pd.DataFrame(points).sort_values("date").reset_index(drop=True)

    if metric == "share" and totals:
        tot = pd.DataFrame(totals).rename(columns={"views": "total_views"})
        df = df.merge(tot, on="date", how="left")
        df["value"] = np.where(
            df["total_views"] > 0,
            df["views"] / df["total_views"] * 1_000_000,
            np.nan,
        )
        df = df.dropna(subset=["value"])
        used_metric = "share_per_million"
    else:
        df["value"] = df["views"].astype(float)
        used_metric = "views"

    n = len(df)
    values = df["value"].to_numpy()
    x = np.arange(n, dtype=float)

    if n == 0:
        return analyze_series([], None, metric)  # degrades to the empty-data branch above

    avg_value = float(values.mean())
    first_avg, last_avg = _edge_avg(values, k=3)
    growth_pct = None
    if first_avg > 0:
        growth_pct = (last_avg - first_avg) / first_avg * 100.0

    # Trend statistics are fitted on the seasonally-adjusted series when a
    # yearly cycle dominates: otherwise every September peak inflates the
    # residuals (R² capped, confidence stuck at "medium") and gets flagged
    # as an anomaly.
    decomp = _stl(df, values) if not seasonal_factors else None
    fit_values, deseasonalized = values, False
    if seasonal_factors:
        factors = np.array([seasonal_factors.get(d, 1.0) for d in df["date"]])
        fit_values, deseasonalized = values / factors, True
    elif _seasonality_matters(decomp):
        fit_values, deseasonalized = values / decomp["factor"], True

    adjusted_growth = None
    if deseasonalized:
        a_first, a_last = _edge_avg(fit_values, k=3)
        adjusted_growth = round((a_last / a_first - 1) * 100, 1) if a_first > 0 else None

    reg = _linregress_safe(x, fit_values)
    slope = float(reg.slope) if reg else None
    r_squared = float(reg.rvalue ** 2) if reg else None
    p_value = float(reg.pvalue) if reg else None

    # Outlier / spike detection: residuals from the linear trend, scored
    # with a robust (median/MAD-based) z-score. Using residuals from the
    # fitted trend - rather than a rolling local median - matters because a
    # rolling window centered on a point near either edge of the series has
    # nowhere to look but at the spike itself, which then pollutes its own
    # baseline and hides it. A global, trend-adjusted MAD has no such blind
    # spot, so a spike on the very first or very last data point (a
    # realistic case - "this topic just blew up") is still caught.
    anomalies: list[dict] = []
    spike_sensitive = False
    if n >= 5 and reg is not None:
        predicted = reg.intercept + reg.slope * x
        residual = fit_values - predicted
        med = float(np.median(residual))
        mad = float(np.median(np.abs(residual - med)))
        if mad > 0:
            z = 0.6745 * (residual - med) / mad  # modified z-score (Iglewicz & Hoaglin)
            for i, zi in enumerate(z):
                if abs(zi) > 3.5:
                    anomalies.append({
                        "date": df.loc[i, "date"],
                        "value": float(df.loc[i, "value"]),
                        "z_score": round(float(zi), 2),
                    })

        if anomalies and reg is not None:
            keep_mask = np.ones(n, dtype=bool)
            for a in anomalies:
                idx = df.index[df["date"] == a["date"]]
                keep_mask[idx] = False
            if keep_mask.sum() >= 2:
                y_excl = fit_values[keep_mask]
                # A perfectly flat remainder (no variation once the spike is
                # removed) is not "not enough data" here - it is the most
                # extreme case of the trend disappearing, i.e. slope -> 0.
                # `_linregress_safe` returns None for this exact input, so it
                # must be handled explicitly rather than skipped.
                if np.all(y_excl == y_excl[0]):
                    slope_excl = 0.0
                else:
                    reg_excl = _linregress_safe(x[keep_mask], y_excl)
                    slope_excl = float(reg_excl.slope) if reg_excl is not None else None
                if slope_excl is not None and slope is not None:
                    same_sign = (slope >= 0) == (slope_excl >= 0)
                    magnitude_kept = (
                        abs(slope) > 1e-9 and abs(slope_excl) < 0.5 * abs(slope)
                    )
                    spike_sensitive = (not same_sign) or magnitude_kept

    confidence, reason = _confidence_label(n, p_value, r_squared, spike_sensitive, avg_value)
    if deseasonalized:
        reason = "Після вилучення сезонності: " + reason[0].lower() + reason[1:]
    yoy = _yoy_growth(df)
    seasonal = _seasonal_months(df, anomalies)
    if _seasonality_matters(decomp) and decomp["peak_month"] not in seasonal:
        seasonal = sorted(seasonal + [decomp["peak_month"]])

    return TrendResult(
        n_points=n,
        metric=used_metric,
        avg_value=round(avg_value, 3),
        first_period_avg=round(first_avg, 3),
        last_period_avg=round(last_avg, 3),
        growth_pct=(round(growth_pct, 1) if growth_pct is not None else None),
        slope_per_period=(round(slope, 4) if slope is not None else None),
        r_squared=(round(r_squared, 3) if r_squared is not None else None),
        p_value=(round(p_value, 4) if p_value is not None else None),
        anomalies=anomalies,
        spike_sensitive=spike_sensitive,
        confidence=confidence,
        confidence_reason=reason,
        yoy_growth_pct=yoy,
        seasonal_months=seasonal,
        seasonal_strength=decomp["seasonal_strength"] if decomp else None,
        trend_strength=decomp["trend_strength"] if decomp else None,
        trend_change_pct=decomp["trend_change_pct"] if decomp else None,
        trend_change_12m_pct=decomp["trend_change_12m_pct"] if decomp else None,
        seasonal_peak_month=decomp["peak_month"] if decomp else None,
        seasonal_amplitude=decomp["seasonal_amplitude"] if decomp else None,
        seasonal_repeats=decomp["repeats"] if decomp else None,
        deseasonalized=deseasonalized,
        adjusted_growth_pct=adjusted_growth,
        trend_series=([{"date": d, "value": round(float(v), 3)}
                       for d, v in zip(df["date"], decomp["trend"])] if decomp else []),
        seasonal_factors=(dict(zip(df["date"], (float(f) for f in decomp["factor"])))
                          if _seasonality_matters(decomp) else {}),
        series=[
            {"date": row.date, "views": int(round(float(row.views))), "value": round(float(row.value), 3)}
            for row in df.itertuples()
        ],
    )


def _confidence_label(
    n: int, p_value: Optional[float], r_squared: Optional[float],
    spike_sensitive: bool, avg_value: float,
) -> tuple[str, str]:
    if n < 6:
        return "low", f"Замало точок даних ({n} < 6) для надійної оцінки тренду."
    if p_value is None:
        return "low", "Значення не змінюється в часі - недостатньо варіації для оцінки тренду."
    if spike_sensitive:
        return "low", (
            "Напрям або сила тренду суттєво змінюються після виключення "
            "аномальних сплесків - схоже на разовий вплив (новина, подія), "
            "а не стійку тенденцію."
        )
    if p_value < 0.05 and (r_squared or 0) >= 0.3:
        return "high", (
            f"Тренд статистично значущий (p={p_value:.3f}) і пояснює істотну "
            f"частку варіації (R²={r_squared:.2f})."
        )
    if p_value < 0.05:
        return "medium", (
            f"Напрям тренду значущий (p={p_value:.3f}), але R²={(r_squared or 0):.2f} - "
            f"багато шуму чи сезонності, тож величина зміни ненадійна."
        )
    if p_value < 0.1:
        return "medium", (
            f"Тренд імовірний, але не строго значущий (p={p_value:.3f}); "
            f"варто перевірити на довшому періоді."
        )
    return "low", (
        f"Тренд статистично незначущий (p={p_value:.3f}) - шум переважає "
        f"над сигналом у цих даних."
    )


def _yoy_growth(df: pd.DataFrame) -> Optional[float]:
    """Mean of the last 3 monthly values vs the same calendar months one
    year earlier; None unless the series is monthly and covers them."""
    dates = list(df["date"])
    if len(dates) < 15 or not all(str(d).endswith("-01") for d in dates):
        return None
    by_date = dict(zip(dates, df["value"]))
    last = dates[-3:]
    prev = [f"{int(d[:4]) - 1}{d[4:]}" for d in last]
    if not all(d in by_date for d in prev):
        return None
    base = float(np.mean([by_date[d] for d in prev]))
    if base <= 0:
        return None
    return round((float(np.mean([by_date[d] for d in last])) / base - 1) * 100, 1)


def _seasonal_months(df: pd.DataFrame, anomalies: list[dict]) -> list[str]:
    """Calendar months that peak repeatedly: either flagged as anomalies in
    2+ different years, or the maximum of 2+ consecutive 12-month blocks
    while >= 1.5x that block's median (catches peaks too mild to be
    anomalies, e.g. a back-to-school September)."""
    years_by_month: dict[str, set] = {}
    for a in anomalies:
        d = str(a["date"])
        years_by_month.setdefault(d[5:7], set()).add(d[:4])
    found = {m for m, years in years_by_month.items() if len(years) >= 2}

    dates = [str(d) for d in df["date"]]
    if len(dates) >= 24 and all(d.endswith("-01") for d in dates):
        values = df["value"].to_numpy()
        peaks: dict[str, int] = {}
        for start in range(len(values) - 12, -1, -12):  # blocks aligned to the end
            block = values[start:start + 12]
            i = int(np.argmax(block))
            if np.median(block) > 0 and block[i] >= 1.5 * np.median(block):
                month = dates[start + i][5:7]
                peaks[month] = peaks.get(month, 0) + 1
        found |= {m for m, c in peaks.items() if c >= 2}
    return sorted(found)


def _stl(df: pd.DataFrame, values: np.ndarray) -> Optional[dict]:
    """Robust STL decomposition (period 12) of a monthly series into
    trend x yearly seasonal factor x remainder. Needs two full years;
    returns None otherwise (or for non-monthly / non-positive data).

    Decomposed in log space, i.e. multiplicatively: pageview seasonality
    scales with the level ("September is 3x a normal month" whether the
    level is 10k or 1k). An additive STL on a series that fell 90% would
    see the shrinking September peaks as noise, not as a stable cycle.

    Strengths follow Hyndman & Athanasopoulos (FPP3 §4.3), in log space:
    F = max(0, 1 - Var(remainder) / Var(component + remainder))."""
    dates = [str(d) for d in df["date"]]
    if len(values) < 24 or not all(d.endswith("-01") for d in dates) or np.any(values <= 0):
        return None
    try:
        res = STL(pd.Series(np.log(values), dtype=float), period=12, robust=True).fit()
    except (ValueError, np.linalg.LinAlgError):
        return None
    seasonal, trend, resid = (np.asarray(c, dtype=float) for c in (res.seasonal, res.trend, res.resid))

    def strength(component):
        denom = np.var(component + resid)
        return round(float(max(0.0, 1 - np.var(resid) / denom)), 3) if denom > 0 else 0.0

    by_month: dict[str, list[float]] = {}
    for d, sv in zip(dates, seasonal):
        by_month.setdefault(d[5:7], []).append(sv)
    month_means = {m: float(np.mean(v)) for m, v in by_month.items()}
    peak_month = max(month_means, key=month_means.get)
    trend_level = np.exp(trend)

    def change(a, b):
        return round(float((b / a - 1) * 100), 1) if a > 0 else None

    return {
        "factor": np.exp(seasonal),  # value / factor = seasonally adjusted value
        "trend": trend_level,
        "seasonal_strength": strength(seasonal), "trend_strength": strength(trend),
        "trend_change_pct": change(trend_level[0], trend_level[-1]),
        "trend_change_12m_pct": change(trend_level[-13], trend_level[-1]),
        "peak_month": peak_month,
        "repeats": _peak_repeats(dates, values, peak_month)[0],
        # peak month vs trough month of the typical year, e.g. 2.0 = 3x
        "seasonal_amplitude": round(float(np.exp(max(month_means.values()) - min(month_means.values())) - 1), 3),
    }
