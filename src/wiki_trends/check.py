"""Checks the agent-written recommendation against the study's numbers.

The recommendation is the only free text in a report. A cheap model can
round creatively, mix up two languages' values or invent a figure; this
module extracts every number from the text and verifies it exists in the
analysis, so a mismatch is caught before the PDF is shared rather than
by the reader.
"""
from __future__ import annotations

import re


_NUM_RE = re.compile(r"(?<![\w.])[-+−]?\d+(?:[.,]\d+)?(\s?%)?")
PCT_TOLERANCE = 1.0      # percentage points, for growth figures
REL_TOLERANCE = 0.02     # 2% relative, for levels / averages
ABS_TOLERANCE = 0.011    # for p-values, R² and similar small numbers


def _known_values(trends) -> tuple[list[float], list[float]]:
    pcts, others = [], []
    for t in (trends.values() if isinstance(trends, dict) else trends):
        pcts += [v for v in (t.growth_pct, t.yoy_growth_pct, t.trend_change_pct,
                             t.trend_change_12m_pct) if v is not None]
        if t.seasonal_amplitude:  # "peak ~x4.6" or "+357%"
            pcts.append(t.seasonal_amplitude * 100)
            others.append(1 + t.seasonal_amplitude)
        others += [v for v in (t.seasonal_strength, t.trend_strength) if v is not None]
        others += [v for v in (t.avg_value, t.first_period_avg, t.last_period_avg,
                               t.p_value, t.r_squared, t.n_points) if v is not None]
        others += [a["z_score"] for a in t.anomalies] + [a["value"] for a in t.anomalies]
    return [abs(v) for v in pcts], [abs(v) for v in others]


def check_recommendation(text: str, trends, extra_pcts: list[float] | None = None) -> dict:
    """{"ok": bool, "checked": int, "unmatched": ["-32%", ...]}.

    Ignored on purpose: years (1990-2100), and small bare integers (< 10)
    such as list markers "(1)" or "3 months". Signs are ignored ("a 30%
    decline" and "-30%" are the same claim)."""
    pcts, others = _known_values(trends)
    pcts += [abs(v) for v in (extra_pcts or [])]
    checked, unmatched = 0, []
    for m in _NUM_RE.finditer(text):
        raw = m.group(0).strip()
        is_pct = m.group(1) is not None
        num_txt = raw.rstrip("%").strip().replace("−", "-").replace(",", ".")
        value = abs(float(num_txt))
        is_int = re.fullmatch(r"[-+]?\d+", num_txt) is not None
        if not is_pct and is_int and (value < 10 or 1990 <= value <= 2100):
            continue
        checked += 1
        if is_pct:
            ok = any(abs(value - p) <= PCT_TOLERANCE for p in pcts)
        else:
            ok = any(abs(value - o) <= max(ABS_TOLERANCE, REL_TOLERANCE * o) for o in others) \
                or any(abs(value - p) <= PCT_TOLERANCE for p in pcts)
        if not ok:
            unmatched.append(raw)
    return {"ok": not unmatched, "checked": checked, "unmatched": unmatched}
