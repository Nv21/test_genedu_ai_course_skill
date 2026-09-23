"""Thin, cached client for the Wikimedia pageviews REST API and the
Wikidata / Wikipedia search APIs used to resolve a topic to per-language
article titles.

Reference notes (endpoints, data availability, caveats) live in
``references/api-reference.md`` next to SKILL.md - keep this module and
that file in sync when the upstream API changes.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import quote

import requests

from .cache import DiskCache
from .dates import to_api_str

PAGEVIEWS_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews"
DATA_AVAILABLE_SINCE = date(2015, 7, 1)  # per-article/new-format data starts here

# Wikimedia asks API consumers to identify themselves. Contact is optional
# but recommended; set WIKI_TRENDS_CONTACT to your email/URL to be a good
# API citizen if you run this a lot.
_contact = os.environ.get("WIKI_TRENDS_CONTACT", "no-contact-set")
USER_AGENT = f"wikipedia-trend-insights-skill/0.1 ({_contact})"

# Cache full historical ranges "forever" (past data doesn't change), but
# only trust cached data that includes the last ~35 days for a short time,
# since the current month is still accumulating views.
FULL_HISTORY_TTL = None
RECENT_DATA_TTL_SECONDS = 6 * 3600


class WikimediaAPIError(RuntimeError):
    """Raised for unexpected (non-404) API failures."""


@dataclass
class ApiCallStats:
    requests_made: int = 0
    cache_hits: int = 0


class WikimediaClient:
    def __init__(self, cache_dir: str, timeout: float = 15.0):
        self.cache = DiskCache(cache_dir)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.stats = ApiCallStats()

    # -- low level ---------------------------------------------------
    def _get_json(self, url: str, params: Optional[dict] = None):
        # Wikimedia rate-limits bursts with 429; back off and retry rather
        # than failing a whole multi-language study on one request.
        for attempt in range(5):
            resp = self.session.get(url, params=params, timeout=self.timeout)
            if resp.status_code != 429:
                break
            retry_after = resp.headers.get("Retry-After", "")
            time.sleep(float(retry_after) if retry_after.isdigit() else 5 * 2 ** attempt)
        if resp.status_code == 404:
            return None  # "no data" - not an error for this API
        if not resp.ok:
            raise WikimediaAPIError(
                f"GET {resp.url} -> {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    def _cached_ttl_for_range(self, end: date) -> Optional[float]:
        """Recent ranges get a short TTL (data still settling); anything
        that ends more than ~35 days ago is treated as final/immutable."""
        if (date.today() - end).days > 35:
            return FULL_HISTORY_TTL
        return RECENT_DATA_TTL_SECONDS

    def _cached_call(self, cache_key: str, ttl: Optional[float], fetch_fn):
        data, hit = self.cache.get_or_fetch(cache_key, fetch_fn, max_age_seconds=ttl)
        self.stats.requests_made += 0 if hit else 1
        self.stats.cache_hits += 1 if hit else 0
        return data

    # -- pageviews -----------------------------------------------------
    def pageviews_per_article(
        self,
        project: str,
        article_title: str,
        start: date,
        end: date,
        granularity: str = "monthly",
        access: str = "all-access",
        agent: str = "user",
    ) -> list[dict]:
        """Returns a list of {date: 'YYYY-MM-DD', views: int}, ascending."""
        article_path = quote(article_title.replace(" ", "_"), safe="")
        url = (
            f"{PAGEVIEWS_BASE}/per-article/{project}/{access}/{agent}/"
            f"{article_path}/{granularity}/{to_api_str(start)}/{to_api_str(end)}"
        )
        cache_key = f"per-article:{url}"
        ttl = self._cached_ttl_for_range(end)

        def fetch():
            payload = self._get_json(url)
            return payload["items"] if payload else []

        items = self._cached_call(cache_key, ttl, fetch)
        return _normalize_items(items)

    def pageviews_aggregate(
        self,
        project: str,
        start: date,
        end: date,
        granularity: str = "monthly",
        access: str = "all-access",
        agent: str = "user",
    ) -> list[dict]:
        """Total project pageviews per period - used to normalize a topic's
        raw views into a "share of attention", which is what makes it fair
        to compare interest across languages with very different traffic."""
        url = (
            f"{PAGEVIEWS_BASE}/aggregate/{project}/{access}/{agent}/"
            f"{granularity}/{to_api_str(start)}/{to_api_str(end)}"
        )
        cache_key = f"aggregate:{url}"
        ttl = self._cached_ttl_for_range(end)

        def fetch():
            payload = self._get_json(url)
            return payload["items"] if payload else []

        items = self._cached_call(cache_key, ttl, fetch)
        return _normalize_items(items)

    # -- top lists (topic discovery) -------------------------------------
    def top_articles_month(self, project: str, year: int, month: int,
                           access: str = "all-access") -> list[dict]:
        """Top ~1000 articles of a project for one month:
        [{"article", "views", "rank"}]. Includes non-article pages (main
        page, Special:Search) - filter with `site_info`."""
        url = f"{PAGEVIEWS_BASE}/top/{project}/{access}/{year}/{month:02d}/all-days"
        ttl = self._cached_ttl_for_range(date(year, month, 28))

        def fetch():
            payload = self._get_json(url)
            return payload["items"][0]["articles"] if payload else []

        return self._cached_call(f"top:{url}", ttl, fetch)

    def top_articles_country_day(self, country: str, day: date,
                                 access: str = "all-access") -> list[dict]:
        """Top ~1000 articles viewed from one country on one day, across all
        projects: [{"article", "project", "views_ceil", "rank"}]. This
        endpoint has no monthly granularity, and views are rounded up
        (privacy), so it is a coarse signal."""
        url = (f"{PAGEVIEWS_BASE}/top-per-country/{country}/{access}/"
               f"{day.year}/{day.month:02d}/{day.day:02d}")
        ttl = self._cached_ttl_for_range(day)

        def fetch():
            payload = self._get_json(url)
            return payload["items"][0]["articles"] if payload else []

        return self._cached_call(f"top-country:{url}", ttl, fetch)

    def site_info(self, project: str) -> dict:
        """{"mainpage": str, "namespaces": [local names/aliases of every
        non-article namespace]} - used to drop non-article pages from top
        lists (these are localized, e.g. "Спеціальна:", "Wikipedie:")."""
        url = f"https://{project}.org/w/api.php"
        params = {"action": "query", "meta": "siteinfo",
                  "siprop": "general|namespaces|namespacealiases", "format": "json"}

        def fetch():
            q = (self._get_json(url, params=params) or {}).get("query", {})
            names = set()
            for ns in q.get("namespaces", {}).values():
                if ns.get("id", 0) != 0:
                    names.update(filter(None, [ns.get("*"), ns.get("canonical")]))
            for alias in q.get("namespacealiases", []):
                if alias.get("id", 0) != 0:
                    names.add(alias.get("*"))
            return {"mainpage": q.get("general", {}).get("mainpage", ""),
                    "namespaces": sorted(n for n in names if n)}

        return self._cached_call(f"siteinfo:{project}", FULL_HISTORY_TTL, fetch)

    # -- topic resolution -----------------------------------------------
    def wikidata_search(self, query: str, language: str = "en", limit: int = 5) -> list[dict]:
        url = "https://www.wikidata.org/w/api.php"
        params = {
            "action": "wbsearchentities",
            "search": query,
            "language": language,
            "format": "json",
            "limit": limit,
        }
        cache_key = f"wd-search:{language}:{limit}:{query}"

        def fetch():
            payload = self._get_json(url, params=params)
            return (payload or {}).get("search", [])

        return self._cached_call(cache_key, FULL_HISTORY_TTL, fetch)

    def wikidata_sitelinks(self, qid: str, lang_codes: list[str]) -> dict[str, Optional[str]]:
        url = "https://www.wikidata.org/w/api.php"
        params = {
            "action": "wbgetentities",
            "ids": qid,
            "props": "sitelinks",
            "format": "json",
        }
        cache_key = f"wd-sitelinks:{qid}"

        def fetch():
            payload = self._get_json(url, params=params) or {}
            entity = payload.get("entities", {}).get(qid, {})
            return entity.get("sitelinks", {})

        sitelinks = self._cached_call(cache_key, FULL_HISTORY_TTL, fetch)
        result: dict[str, Optional[str]] = {}
        for code in lang_codes:
            site_key = f"{code}wiki"
            result[code] = sitelinks.get(site_key, {}).get("title")
        return result

    def wiki_search(self, lang_code: str, query: str, limit: int = 5) -> list[dict]:
        """Direct full-text search on a specific language Wikipedia. Used as
        a fallback when Wikidata has no sitelink for that language."""
        url = f"https://{lang_code}.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": limit,
        }
        cache_key = f"wiki-search:{lang_code}:{limit}:{query}"

        def fetch():
            payload = self._get_json(url, params=params) or {}
            return payload.get("query", {}).get("search", [])

        return self._cached_call(cache_key, FULL_HISTORY_TTL, fetch)


def _normalize_items(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        ts = it["timestamp"]  # YYYYMMDDHH
        d = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
        out.append({"date": d, "views": it["views"]})
    out.sort(key=lambda r: r["date"])
    return out
