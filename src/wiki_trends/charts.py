"""Chart rendering (matplotlib only - no extra plotting dependency)."""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")  # headless: never try to open a GUI window
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

# A small, colorblind-safer categorical palette (Okabe-Ito), used in a
# fixed order so the same language always gets the same color across a
# session's charts.
PALETTE = [
    "#0072B2", "#D55E00", "#009E73", "#CC79A7",
    "#E69F00", "#56B4E9", "#F0E442", "#000000",
]


def render_comparison_chart(series_by_label: dict, metric_label: str, title: str, subtitle: str = ""):
    """`series_by_label`: {label: TrendResult}. Returns a matplotlib Figure."""
    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=150)

    for i, (label, trend) in enumerate(series_by_label.items()):
        color = PALETTE[i % len(PALETTE)]
        if not trend.series:
            continue
        df = pd.DataFrame(trend.series)
        df["date"] = pd.to_datetime(df["date"])
        ax.plot(df["date"], df["value"], label=label, color=color, linewidth=2)
        if trend.trend_series:  # STL trend without seasonality
            tdf = pd.DataFrame(trend.trend_series)
            ax.plot(pd.to_datetime(tdf["date"]), tdf["value"], color=color,
                    linewidth=1.2, linestyle="--", alpha=0.8)
        if trend.anomalies:
            adates = pd.to_datetime([a["date"] for a in trend.anomalies])
            avals = [a["value"] for a in trend.anomalies]
            ax.scatter(adates, avals, color=color, marker="x", s=50, zorder=5)

    ax.set_title(title, fontsize=13, fontweight="bold", loc="left")
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color="#555555")
    ax.set_ylabel(metric_label, fontsize=10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    if len(series_by_label) > 1:
        ax.legend(frameon=False, fontsize=9)
    fig.text(
        0.01, 0.01,
        "Джерело: Wikimedia Pageviews API · × = виявлений сплеск (можливий разовий вплив) · пунктир = тренд без сезонності (STL)",
        fontsize=7, color="#888888",
    )
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    return fig
