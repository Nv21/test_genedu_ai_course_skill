"""Topic discovery: which articles in a language edition (or country) are
gaining attention fastest, year over year.

Method (deliberately cheap in API calls, then verified):
1. Candidates come from the monthly top-1000 lists of the last `window`
   full months. Only articles present in *every* recent month's list are
   kept - a one-month news spike cannot qualify.
2. Year-over-year comparison against the same months a year earlier
   cancels seasonality (exam season, holidays) - comparing Jun-Aug to
   Mar-May would just rediscover the calendar.
3. Pre-ranking uses the year-ago top lists; an article absent from them
   gets the list's floor as an upper bound for its past views, which
   makes its growth a lower bound.
4. The best `verify_k` candidates are re-checked with their exact monthly
   per-article history (24 months) through the same `analyze_series`
   used by `study`, so each result carries an honest confidence label
   and spike flag, and the final ranking uses exact numbers.

Country targets (e.g. "US") use the top-per-country endpoint, which only
exists per day and with rounded-up views, so months are sampled on fixed
days; verification then uses the article's global per-project history
(the API has no per-country per-article series) - flagged in caveats.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from .api import WikimediaClient
from .stats import analyze_series

SAMPLE_DAYS = (1, 8, 15, 22)
MIN_BASE_MONTHLY_VIEWS = 500  # below this a YoY ratio is mostly noise
# Human traffic to an article is typically ~15-40% desktop; crawlers that
# slip past Wikimedia's agent=user filter are almost all desktop (verified:
# en ".xyz", "RDFa", "Microdata (HTML)" at 96-99.9% in 2026-08).
BOT_DESKTOP_SHARE = 0.9
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_JUNK_RE = re.compile(r"\.(php|phtml|html?|asp)$", re.I)  # URL fragments logged as titles


def last_full_month(today: Optional[date] = None) -> date:
    today = today or date.today()
    return (today.replace(day=1) - timedelta(days=1)).replace(day=1)


def _month_seq(end_month: date, n: int) -> list[date]:
    out, m = [], end_month
    for _ in range(n):
        out.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    return list(reversed(out))


def _is_country(target: str) -> bool:
    return len(target) == 2 and target.isupper()


def _month_top(client: WikimediaClient, target: str, month: date) -> tuple[dict, float]:
    """{(project, article): views} for one month and the list's floor
    (smallest views on it - the ceiling for anything absent)."""
    if not _is_country(target):
        project = f"{target}.wikipedia"
        rows = client.top_articles_month(project, month.year, month.month)
        views = {(project, r["article"]): r["views"] for r in rows}
        return views, float(min(views.values(), default=0))
    views: dict = {}
    floors = []
    for d in SAMPLE_DAYS:
        rows = client.top_articles_country_day(target, month.replace(day=d))
        floors.append(min((r["views_ceil"] for r in rows), default=0))
        for r in rows:
            key = (r["project"], r["article"])
            views.setdefault(key, []).append(r["views_ceil"])
    # Scale sampled days to a month-equivalent; a key missing on a sampled
    # day counts as that day's floor (an upper bound).
    scale = 30 / len(SAMPLE_DAYS)
    floor_sum = sum(floors)
    out = {k: (sum(v) + floor_sum * (len(SAMPLE_DAYS) - len(v)) / len(SAMPLE_DAYS)) * scale
           for k, v in views.items() if len(v) >= len(SAMPLE_DAYS) // 2}
    return out, floor_sum * scale


def _is_content(client: WikimediaClient, project: str, article: str, cache: dict) -> bool:
    if project not in cache:
        cache[project] = client.site_info(project)
    info = cache[project]
    title = article.replace("_", " ")
    if title == info["mainpage"].replace("_", " ") or article in ("-", "Main_Page"):
        return False
    prefix = title.split(":", 1)[0] if ":" in title else None
    return not (prefix and prefix in info["namespaces"])


def discover_rising(client: WikimediaClient, target: str, *, end_month: date,
                    window: int = 3, top_n: int = 5, verify_k: int = 40) -> dict:
    recent = _month_seq(end_month, window)
    base = [m.replace(year=m.year - 1) for m in recent]
    site_cache: dict = {}

    recent_lists = [_month_top(client, target, m) for m in recent]
    base_lists = [_month_top(client, target, m) for m in base]

    keys = set.intersection(*(set(v) for v, _ in recent_lists))
    excluded_dated = 0
    candidates = []
    for key in keys:
        project, article = key
        if _JUNK_RE.search(article) or not _is_content(client, project, article, site_cache):
            continue
        # Yearly articles ("2026 in film", "Eurovision 2026") have no
        # year-ago counterpart - their "growth" is a renamed recurring topic.
        if _YEAR_RE.search(article):
            excluded_dated += 1
            continue
        recent_avg = sum(v[key] for v, _ in recent_lists) / window
        base_upper = sum(v.get(key, floor) for v, floor in base_lists) / window
        candidates.append((recent_avg / max(base_upper, 1.0), key, recent_avg))
    candidates.sort(reverse=True)

    verified, new_topics = [], []
    start = _month_seq(recent[-1], 24)[0]  # 24 months, same span as a study
    series_end = (recent[-1].replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    for _, (project, article), _recent_avg in candidates[:verify_k]:
        points = client.pageviews_per_article(project, article, start, series_end)
        by_month = {p["date"][:7]: p["views"] for p in points}
        r_views = [by_month.get(m.isoformat()[:7], 0) for m in recent]
        b_views = [by_month.get(m.isoformat()[:7], 0) for m in base]
        r_avg, b_avg = sum(r_views) / window, sum(b_views) / window
        trend = analyze_series(points, metric="views")
        item = {
            "project": project,
            "article": article.replace("_", " "),
            "recent_monthly_avg": round(r_avg),
            "year_ago_monthly_avg": round(b_avg),
            "yoy_growth_pct": round((r_avg / b_avg - 1) * 100, 1) if b_avg else None,
            "recent_months": dict(zip((m.isoformat()[:7] for m in recent), r_views)),
            "trend_confidence": trend.confidence,
            "trend_confidence_reason": trend.confidence_reason,
            "spike_sensitive": trend.spike_sensitive,
            "anomalies": [a["date"][:7] for a in trend.anomalies],
        }
        if b_avg < MIN_BASE_MONTHLY_VIEWS:
            item["note"] = ("Рік тому статті майже не читали (або її не існувало) - "
                            "відсоток зростання неінформативний.")
            new_topics.append(item)
        else:
            verified.append(item)
    verified.sort(key=lambda i: i["yoy_growth_pct"], reverse=True)
    new_topics.sort(key=lambda i: i["recent_monthly_avg"], reverse=True)
    # A YoY jump carried by one burst month (a news story) is not the same
    # decision signal as attention that stayed high - report them apart.
    # Bot check is lazy: only items that would be shown cost an API call.
    last = recent[-1]
    last_end = series_end
    sustained, spiky, new_clean, suspected_bots = [], [], [], []
    for pool, sink in ((verified, None), (new_topics, new_clean)):
        for item in pool:
            target_list = sink if sink is not None else (spiky if item["spike_sensitive"] else sustained)
            if len(target_list) >= top_n:
                continue
            desk = client.pageviews_per_article(item["project"], item["article"], last, last_end,
                                                access="desktop")
            desk_views = sum(p["views"] for p in desk)
            total = item["recent_months"].get(last.isoformat()[:7], 0)
            item["desktop_share"] = round(desk_views / total, 3) if total else None
            if item["desktop_share"] is not None and item["desktop_share"] > BOT_DESKTOP_SHARE:
                suspected_bots.append(item)
            else:
                target_list.append(item)

    caveats = [
        f"Порівняння рік до року: {recent[0]:%Y-%m}..{recent[-1]:%Y-%m} проти "
        f"{base[0]:%Y-%m}..{base[-1]:%Y-%m}; кандидати - статті з топ-1000 у кожному з {window} останніх місяців.",
        f"Відкинуто {excluded_dated} статей з роком у назві (щорічні події/списки).",
    ]
    if suspected_bots:
        caveats.append(
            f"Відкинуто як ймовірний автоматичний трафік (>{BOT_DESKTOP_SHARE:.0%} переглядів з "
            f"десктопа): {', '.join(i['article'] for i in suspected_bots)}.")
    if _is_country(target):
        caveats.append(
            f"{target}: топ-списки країни доступні лише подобово - місяці оцінено за "
            f"{len(SAMPLE_DAYS)} днями ({', '.join(map(str, SAMPLE_DAYS))}); точні цифри й довіра "
            f"нижче - глобальні перегляди статті в її проєкті, не лише з {target}.")
    return {
        "target": target,
        "window": {"recent": [m.isoformat()[:7] for m in recent],
                   "year_ago": [m.isoformat()[:7] for m in base]},
        "rising": sustained,
        "news_spikes": spiky,
        "new_topics": new_clean,
        "suspected_bots": suspected_bots,
        "n_candidates": len(candidates),
        "n_verified": min(verify_k, len(candidates)),
        "caveats": caveats,
    }
