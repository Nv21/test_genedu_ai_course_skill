"""End-to-end orchestration for a single "study": resolve topic -> fetch
pageviews -> analyze trend -> chart -> one-page PDF report -> JSON summary.

This is the one function the CLI's `study` command calls, and the one an
agent should reach for by default: it makes the resolve/fetch/analyze/
chart/report pipeline atomic so a cheap model doesn't have to plan or
sequence multiple tool calls, or remember to pass intermediate results
between steps, for the common case.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Optional

from . import charts, horizons as hz, report
from .api import DATA_AVAILABLE_SINCE, WikimediaClient
from .check import check_recommendation
from .dates import parse_boundary
from .resolve import resolve_topic
from .stats import TrendResult, analyze_series


def parse_articles_arg(raw: Optional[str]) -> dict[str, str]:
    """Parses "pl:Głodówka przerywana,cs:Přerušovaný půst" -> {lang: title}."""
    if not raw:
        return {}
    out = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"--articles entry '{chunk}' must be 'lang:Title'")
        lang, title = chunk.split(":", 1)
        out[lang.strip()] = title.strip()
    return out


def run_study(
    *,
    topic: Optional[str],
    lang_codes: list[str],
    start: Optional[str],
    end: Optional[str],
    out_base: str,
    cache_dir: str,
    granularity: str = "monthly",
    metric: str = "views",
    source_lang: str = "en",
    manual_articles: Optional[dict[str, str]] = None,
    recommendation: str = "",
    title_override: Optional[str] = None,
) -> dict:
    if not topic and not manual_articles:
        raise ValueError("Provide --topic, or --articles for every language.")
    if metric not in ("views", "share"):
        raise ValueError("--metric must be 'views' or 'share'")
    if granularity not in ("daily", "monthly"):
        raise ValueError("--granularity must be 'daily' or 'monthly'")

    # No dates at all -> multi-horizon study (see horizons.py): the last 3
    # months by week, the last year and the last 3 years.
    if (start is None) != (end is None):
        raise ValueError("pass both --start and --end, or neither (multi-horizon mode)")
    horizon_mode = start is None
    windows = hz.windows() if horizon_mode else {}
    if horizon_mode:
        start_d, end_d, granularity = windows["3y"]
    else:
        start_d = parse_boundary(start, is_end=False)
        end_d = parse_boundary(end, is_end=True)
    if start_d >= end_d:
        raise ValueError("start date must be before end date")

    caveats: list[str] = []
    if start_d < DATA_AVAILABLE_SINCE:
        caveats.append(
            f"Дані Wikimedia Pageviews (у цьому форматі) доступні лише з "
            f"{DATA_AVAILABLE_SINCE.isoformat()}; початок діапазону скориговано в аналізі, "
            f"але порівняння з періодом до цієї дати неможливе."
        )
        start_d = max(start_d, DATA_AVAILABLE_SINCE)

    # A still-running period is a partial count: an unfinished last month
    # looks like a collapse in interest and skews the last-3 average.
    today = date.today()
    last_complete = (today.replace(day=1) - timedelta(days=1)) if granularity == "monthly" \
        else today - timedelta(days=1)
    if end_d > last_complete:
        caveats.append(
            f"Кінець періоду скориговано до {last_complete.isoformat()}: дані за "
            f"{'поточний місяць' if granularity == 'monthly' else 'сьогодні'} ще неповні."
        )
        end_d = last_complete
        if start_d >= end_d:
            raise ValueError("date range contains no complete period yet")

    os.makedirs(cache_dir, exist_ok=True)
    client = WikimediaClient(cache_dir=cache_dir)

    resolved = resolve_topic(
        client, topic or "", lang_codes, source_lang=source_lang,
        manual_overrides=manual_articles,
    )

    trends: dict[str, TrendResult] = {}
    horizon_results: dict[str, dict[str, TrendResult]] = {}
    resolved_out: dict[str, dict] = {}
    for lang, art in resolved.items():
        resolved_out[lang] = art.to_dict()
        if not art.title:
            trends[lang] = analyze_series([], None, metric)
            continue
        if art.confidence == "low":
            caveats.append(
                f"[{lang}] Стаття «{art.title}» знайдена евристичним пошуком (не через "
                f"Wikidata) - перевірте, що це саме та тема, перш ніж довіряти тренду."
            )
        project = f"{lang}.wikipedia"
        points = client.pageviews_per_article(
            project, art.title, start_d, end_d, granularity=granularity,
        )
        totals = None
        if metric == "share":
            totals = client.pageviews_aggregate(project, start_d, end_d, granularity=granularity)
        trends[lang] = analyze_series(points, totals, metric=metric)
        if horizon_mode:
            horizon_results[lang] = _analyze_horizons(
                client, project, art.title, metric, windows, points, totals, trends[lang])
        if trends[lang].n_points == 0:
            caveats.append(
                f"[{lang}] Немає даних переглядів для «{art.title}» у заданому періоді."
            )
            if art.method == "manual":
                # A caller-supplied title that has no pageviews at all almost
                # always does not exist (the API 404s). Reporting it as a
                # "high"-confidence match would let an agent's guessed title
                # pass as verified.
                resolved_out[lang].update(
                    method="manual_not_found", confidence="none",
                    note=(f"No pageviews for '{art.title}' on {project} - the title likely "
                          f"does not exist. Do not substitute another topic; report that "
                          f"this edition has no matching article, or ask the user for the "
                          f"exact title."),
                )

    # -- ranking, to answer "which audience should we look at next" ------
    ranking = _build_ranking(trends)

    for lang, t in trends.items():
        if not t.seasonal_months:
            continue
        if t.trend_change_pct is not None:
            amp = f" (типовий пік ≈ ×{1 + t.seasonal_amplitude:.1f} від мінімуму року)" \
                if t.seasonal_amplitude else ""
            caveats.append(
                f"[{lang}] Сезонність: пік щороку в місяці {', '.join(t.seasonal_months)}{amp}. "
                f"«Зростання*» (перші 3 vs останні 3) нею спотворене; тренд без сезонності (STL): "
                f"{t.trend_change_pct:+.1f}% за період, {t.trend_change_12m_pct:+.1f}% за останні 12 міс."
            )
        else:
            yoy = f"{t.yoy_growth_pct:+.1f}%" if t.yoy_growth_pct is not None else "н/д (замало даних)"
            caveats.append(
                f"[{lang}] Сезонність: піки повторюються щороку в місяці "
                f"{', '.join(t.seasonal_months)} - «зростання*» (перші 3 vs останні 3) може бути "
                f"спотворене; порівняння рік до року: {yoy}."
            )

    low_confidence_langs = [
        lang for lang, t in trends.items() if t.n_points > 0 and t.confidence in ("low", "none")
    ]
    if low_confidence_langs:
        caveats.append(
            f"Довіра до тренду низька для: {', '.join(low_confidence_langs)} - "
            f"див. колонку «Довіра» в таблиці; не варто робити на цьому категоричні висновки."
        )

    # -- chart + PDF -------------------------------------------------------
    metric_label = "Перегляди/період" if metric == "views" else "Перегляди на мільйон переглядів розділу"
    labels = {lang: _display_label(lang, resolved[lang]) for lang in trends}
    title = title_override or (f"Wikipedia: «{topic}»" if topic else "Порівняння тем у Wikipedia")
    date_range_label = f"{start_d.isoformat()} – {end_d.isoformat()} · {granularity}"

    os.makedirs(os.path.dirname(out_base) or ".", exist_ok=True)
    png_path = f"{out_base}.png"
    pdf_path = f"{out_base}.pdf"
    json_path = f"{out_base}.json"

    fig = charts.render_comparison_chart(
        {labels[lang]: t for lang, t in trends.items()},
        metric_label=metric_label, title=title, subtitle=date_range_label,
    )
    fig.savefig(png_path)

    stats_rows = [
        {
            "label": labels[lang],
            "growth_pct": t.growth_pct,
            "yoy_growth_pct": t.yoy_growth_pct,
            "trend_change_pct": t.trend_change_pct,
            "n_points": t.n_points,
            "confidence": t.confidence,
            "confidence_reason": t.confidence_reason,
        }
        for lang, t in trends.items()
    ]
    findings = _auto_findings(labels, trends)
    horizon_summaries = {lang: hz.summarize(h) for lang, h in horizon_results.items()}
    horizon_rows = [
        {"label": labels[lang],
         "cells": {h: _horizon_cell(horizon_summaries[lang]["changes"][h], res[h].confidence)
                   for h in hz.HORIZONS},
         "summary": horizon_summaries[lang]["text"]}
        for lang, res in horizon_results.items()
    ]
    if horizon_mode:
        date_range_label = (f"без заданих дат: 3 періоди - тижні {windows['3m_weekly'][0]}..{windows['3m_weekly'][1]}, "
                            f"рік {windows['1y'][0]:%Y-%m}..{windows['1y'][1]:%Y-%m}, "
                            f"3 роки {windows['3y'][0]:%Y-%m}..{windows['3y'][1]:%Y-%m}")

    import matplotlib.pyplot as plt
    plt.close(fig)

    recommendation_check = None
    if recommendation.strip():
        all_results = list(trends.values()) + [r for h in horizon_results.values() for r in h.values()]
        recommendation_check = check_recommendation(recommendation, all_results,
                                                    extra_pcts=_horizon_pcts(horizon_summaries))
        if not recommendation_check["ok"]:
            import sys
            unmatched = ", ".join(recommendation_check["unmatched"])
            print(f"WARNING: numbers in --recommendation not found in the data: {unmatched}",
                  file=sys.stderr)
            caveats.append(f"Числа у висновку, яких немає в даних аналізу: {unmatched}.")

    # A too-long report must not lose the analysis: keep the JSON/PNG and
    # record why there is no PDF, so the caller can split the study.
    try:
        report_pages = report.build_pdf(
            out_path=pdf_path, title=title, date_range_label=date_range_label,
            chart_png_path=png_path, stats_rows=stats_rows, findings=findings,
            caveats=caveats, recommendation=recommendation, horizon_rows=horizon_rows,
        )
    except report.ReportTooLongError as e:
        import sys
        print(f"ERROR: {e}", file=sys.stderr)
        caveats = caveats + [str(e)]
        pdf_path, report_pages = None, e.n_pages

    result = {
        "topic_query": topic,
        "date_range": {"start": start_d.isoformat(), "end": end_d.isoformat(), "granularity": granularity,
                       "mode": "multi_horizon" if horizon_mode else "explicit"},
        "metric": metric,
        "resolved_articles": resolved_out,
        "trends": {lang: t.to_dict() for lang, t in trends.items()},
        "horizons": {
            lang: {h: {**{k: v for k, v in r.to_dict().items() if k not in ("series", "trend_series")},
                       "window": [windows[h][0].isoformat(), windows[h][1].isoformat()]}
                   for h, r in res.items()}
            for lang, res in horizon_results.items()
        } or None,
        "horizon_summary": horizon_summaries or None,
        "ranking": ranking,
        "recommendation_check": recommendation_check,
        "caveats": caveats,
        "api_calls": {
            "requests_made": client.stats.requests_made,
            "cache_hits": client.stats.cache_hits,
        },
        "output_files": {"chart_png": png_path, "report_pdf": pdf_path,
                         "report_pages": report_pages, "analysis_json": json_path},
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def _display_label(lang: str, art) -> str:
    return f"{lang}: {art.title}" if art.title else f"{lang}: (не знайдено)"


def _build_ranking(trends: dict[str, TrendResult]) -> dict:
    valid = {lang: t for lang, t in trends.items() if t.n_points > 0}
    by_growth = sorted(valid, key=lambda l: (valid[l].growth_pct or -1e9), reverse=True)
    by_interest = sorted(valid, key=lambda l: (valid[l].avg_value or 0), reverse=True)
    by_confidence = sorted(
        valid, key=lambda l: {"high": 3, "medium": 2, "low": 1, "none": 0}[valid[l].confidence],
        reverse=True,
    )
    return {
        "by_growth": by_growth,
        "by_current_interest": by_interest,
        "by_confidence": by_confidence,
        "explore_next": _explore_next(valid),
    }


_CONF_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def _explore_next(trends: dict[str, TrendResult]) -> list[dict]:
    """Deterministic "which audience next" order, so the agent doesn't have
    to weigh level vs growth vs confidence itself (a cheap model ranked
    the lowest-interest markets first because "least decline" topped
    `by_growth`).

    The tag follows the regression slope, because that is what
    `confidence` measures; year-over-year growth can disagree (a long
    decline that has started to recover) and then the tag is `turning`.
    Order: reliably_growing, turning, unclear, reliably_declining; within a
    tier, by current interest level."""
    tiers = {"reliably_growing": 0, "turning": 1, "unclear": 2, "reliably_declining": 3}
    rows = []
    for lang, t in trends.items():
        slope = t.slope_per_period
        # recent change: STL trend over the last 12 months when available
        # (uses every month), else the 3-month year-over-year comparison
        yoy = t.trend_change_12m_pct if t.trend_change_12m_pct is not None else t.yoy_growth_pct
        if t.confidence in ("low", "none") or slope is None:
            tag = "unclear"
        elif yoy is not None and (slope > 0) != (yoy > 0) and abs(yoy) >= TURNING_YOY_PCT:
            tag = "turning"
        else:
            tag = "reliably_growing" if slope > 0 else "reliably_declining"
        rows.append({"lang": lang, "tag": tag, "level": t.avg_value,
                     "trend_growth_pct": (t.trend_change_pct if t.trend_change_pct is not None
                                          else t.growth_pct),
                     "recent_12m_pct": yoy,
                     "confidence": t.confidence})
    rows.sort(key=lambda r: (tiers[r["tag"]], -(r["level"] or 0)))
    return rows


# YoY change smaller than this is "flat", not a reversal of the trend.
TURNING_YOY_PCT = 5.0


def _auto_findings(labels: dict[str, str], trends: dict[str, TrendResult]) -> list[str]:
    out = []
    for lang, t in trends.items():
        label = labels[lang]
        if t.n_points == 0:
            out.append(f"{label}: даних немає.")
            continue
        direction = "зросли" if (t.growth_pct or 0) > 0 else "не зросли / знизились"
        growth_txt = "н/д" if t.growth_pct is None else f"{t.growth_pct:+.1f}%"
        if t.trend_change_pct is not None:
            yoy_txt = (f"; тренд без сезонності: {t.trend_change_pct:+.1f}%, "
                       f"за останні 12 міс.: {t.trend_change_12m_pct:+.1f}%")
        elif t.yoy_growth_pct is not None:
            yoy_txt = f"; рік до року: {t.yoy_growth_pct:+.1f}%"
        else:
            yoy_txt = ""
        out.append(
            f"{label}: перегляди {direction} на {growth_txt} за період "
            f"({t.n_points} точок даних{yoy_txt}). Довіра до тренду: {t.confidence} - "
            f"{t.confidence_reason}"
        )
        if t.anomalies:
            dates = ", ".join(a["date"] for a in t.anomalies[:3])
            out.append(f"{label}: виявлено сплеск(и) переглядів: {dates}.")
    return out


def _analyze_horizons(client, project, title, metric, windows, points_3y, totals_3y,
                      t3: TrendResult) -> dict[str, TrendResult]:
    """1y is a slice of the 3y monthly series (seasonally adjusted with the
    3y factors); 3m_weekly needs one daily fetch, summed into weeks."""
    y_start = windows["1y"][0].isoformat()
    p1 = [p for p in points_3y if p["date"] >= y_start]
    tot1 = [p for p in totals_3y if p["date"] >= y_start] if totals_3y else None
    t1 = analyze_series(p1, tot1, metric=metric, seasonal_factors=t3.seasonal_factors or None)

    w_start, w_end, _ = windows["3m_weekly"]
    daily = client.pageviews_per_article(project, title, w_start, w_end, granularity="daily")
    daily_tot = (client.pageviews_aggregate(project, w_start, w_end, granularity="daily")
                 if metric == "share" else None)
    weeks = hz.to_weekly(daily)
    # 13 weeks can't reveal a yearly cycle on their own; borrow the monthly
    # seasonal factors of the 3-year decomposition, or a back-to-school
    # September would read as "+400% in the last 3 months".
    week_factors = (hz.weekly_seasonal_factors(weeks, t3.seasonal_factors)
                    if t3.seasonal_factors else None)
    tw = analyze_series(weeks, hz.to_weekly(daily_tot) if daily_tot else None,
                        metric=metric, seasonal_factors=week_factors or None)
    return {"3m_weekly": tw, "1y": t1, "3y": t3}


_CONF_SHORT = {"high": "висока", "medium": "середня", "low": "низька", "none": "немає"}


def _horizon_cell(change, confidence: str) -> str:
    if change is None:
        return "н/д"
    return f"{change:+.1f}%\n{_CONF_SHORT.get(confidence, confidence)}"


def _horizon_pcts(summaries: dict) -> list[float]:
    return [c for s in summaries.values() for c in s["changes"].values() if c is not None]
