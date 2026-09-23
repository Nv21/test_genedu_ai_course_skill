"""Multi-horizon view, used when the user names no period.

One window hides the story: "astronomy fell 90% over 3 years" and "it fell
~60% in the last year" and "the last 3 months are flat" are all true, and a
product decision needs all three. So without explicit dates a study looks at:

- 3m_weekly: the last 13 complete Monday-Sunday weeks. Fetched daily and
  summed per week - daily data is noisy and has weekday effects.
- 1y: the last 12 complete months, seasonally adjusted with the factors of
  the 3-year decomposition (12 points alone can't separate a yearly cycle
  from a trend).
- 3y: the last 36 complete months; STL runs here.

1y is a slice of the 3y series, so it costs no extra API requests; only the
weekly view needs one extra daily fetch per language.

`summarize` turns the three results into a direction per horizon and one
pattern sentence, computed in code so a cheap model doesn't have to
reconcile three tables itself.
"""
from __future__ import annotations

from datetime import date, timedelta

from .stats import TrendResult

HORIZONS = ("3m_weekly", "1y", "3y")
HORIZON_LABELS = {"3m_weekly": "3 міс. (тижні)", "1y": "1 рік", "3y": "3 роки"}
FLAT_PCT = 5.0  # |change| below this is "flat"


def windows(today: date | None = None) -> dict[str, tuple[date, date, str]]:
    """{horizon: (start, end, granularity)} ending at the last complete
    week / month before `today`."""
    today = today or date.today()
    last_month_end = today.replace(day=1) - timedelta(days=1)

    def months_back(n: int) -> date:
        y, m = last_month_end.year, last_month_end.month - (n - 1)
        while m <= 0:
            m, y = m + 12, y - 1
        return date(y, m, 1)

    yesterday = today - timedelta(days=1)
    last_sunday = yesterday - timedelta(days=(yesterday.weekday() + 1) % 7)
    return {
        "3m_weekly": (last_sunday - timedelta(weeks=13) + timedelta(days=1), last_sunday, "daily"),
        "1y": (months_back(12), last_month_end, "monthly"),
        "3y": (months_back(36), last_month_end, "monthly"),
    }


def to_weekly(points: list[dict]) -> list[dict]:
    """Daily {date, views} -> weekly sums keyed by the week's Monday.
    Incomplete weeks (fewer than 7 days of data) are dropped."""
    weeks: dict[str, list[int]] = {}
    for p in points:
        d = date.fromisoformat(p["date"])
        monday = (d - timedelta(days=d.weekday())).isoformat()
        weeks.setdefault(monday, []).append(p["views"])
    return [{"date": w, "views": sum(v)} for w, v in sorted(weeks.items()) if len(v) == 7]


def horizon_change(horizon: str, results: dict[str, TrendResult]) -> tuple[float | None, str]:
    """The change figure shown for a horizon and what it is based on.
    Prefers the seasonally-adjusted STL trend when it exists."""
    t3 = results.get("3y")
    if horizon == "3y" and t3 is not None:
        if t3.trend_change_pct is not None:
            return t3.trend_change_pct, "stl_trend"
        return t3.growth_pct, "first3_vs_last3"
    if horizon == "1y":
        if t3 is not None and t3.trend_change_12m_pct is not None:
            return t3.trend_change_12m_pct, "stl_trend_12m"
        t1 = results.get("1y")
        return (t1.growth_pct if t1 else None), "first3_vs_last3"
    t = results.get(horizon)
    if t is not None and t.adjusted_growth_pct is not None:
        return t.adjusted_growth_pct, "first3_vs_last3_weeks_seasonally_adjusted"
    return (t.growth_pct if t else None), "first3_vs_last3_weeks"


def weekly_seasonal_factors(weeks: list[dict], monthly_factors: dict[str, float]) -> dict[str, float]:
    """Gives each week the seasonal factor of its calendar month (the most
    recent year that has one), judged by the week's Thursday so a week
    belongs to the month holding most of its days."""
    by_month: dict[str, float] = {}
    for d in sorted(monthly_factors):  # later years overwrite earlier ones
        by_month[d[5:7]] = monthly_factors[d]
    out = {}
    for w in weeks:
        month = (date.fromisoformat(w["date"]) + timedelta(days=3)).strftime("%m")
        if month in by_month:
            out[w["date"]] = by_month[month]
    return out


def direction(change: float | None, confidence: str) -> str:
    if change is None or confidence in ("low", "none"):
        return "unclear"
    if change > FLAT_PCT:
        return "up"
    if change < -FLAT_PCT:
        return "down"
    return "flat"


_DIR_UA = {"up": "зростання", "down": "спад", "flat": "стабільно", "unclear": "неясно"}


def summarize(results: dict[str, TrendResult]) -> dict:
    """{"directions": {horizon: up|down|flat|unclear}, "changes": {...},
    "pattern": code, "text": Ukrainian sentence}."""
    dirs, changes, bases = {}, {}, {}
    for h in HORIZONS:
        t = results.get(h)
        change, basis = horizon_change(h, results)
        dirs[h] = direction(change, t.confidence if t else "none")
        changes[h], bases[h] = change, basis

    long, year, short = dirs["3y"], dirs["1y"], dirs["3m_weekly"]
    if long == "unclear" and year == "unclear":
        pattern, text = "no_clear_trend", "чіткого тренду немає на жодному з довгих періодів"
    elif long == year and long in ("up", "down", "flat"):
        pattern = {"up": "steady_growth", "down": "steady_decline", "flat": "stable"}[long]
        text = {"up": "стабільно росте і за 3 роки, і за останній рік",
                "down": "стабільно падає і за 3 роки, і за останній рік",
                "flat": "стабільний рівень"}[long]
    elif long == "down" and year == "flat":
        pattern, text = "decline_flattening", "довгий спад, але за останній рік рівень стабілізувався"
    elif long == "down" and year == "up":
        pattern, text = "reversal_up", "після довгого спаду - розворот угору за останній рік"
    elif long == "up" and year in ("flat", "down"):
        pattern = "growth_stalling" if year == "flat" else "reversal_down"
        text = ("ріст за 3 роки, але за останній рік зупинився" if year == "flat"
                else "ріст за 3 роки змінився спадом за останній рік")
    else:
        pattern, text = "mixed", (f"змішана картина: 3 роки - {_DIR_UA[long]}, "
                                  f"останній рік - {_DIR_UA[year]}")
    if short != "unclear":
        text += f"; останні 3 місяці - {_DIR_UA[short]}"
    else:
        text += "; останні 3 місяці - без надійного сигналу"
    return {"directions": dirs, "changes": changes, "change_basis": bases,
            "pattern": pattern, "text": text[0].upper() + text[1:] + "."}
