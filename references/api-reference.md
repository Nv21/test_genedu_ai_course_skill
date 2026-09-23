# Wikimedia / Wikidata API reference & gotchas

Background reading for maintaining `src/wiki_trends/api.py` and
`resolve.py`. Not needed to just *use* the skill - SKILL.md covers that.

## Endpoints used

All via `https://wikimedia.org/api/rest_v1/metrics/pageviews/...`
([full docs](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html)):

- **Per-article**: `/per-article/{project}/{access}/{agent}/{article}/{granularity}/{start}/{end}`
  e.g. `pl.wikipedia/all-access/user/Astronomia/monthly/20230101/20231231`.
  Returns one item per period: `{project, article, granularity, timestamp: "YYYYMMDDHH", access, agent, views}`.
- **Aggregate**: `/aggregate/{project}/{access}/{agent}/{granularity}/{start}/{end}` -
  total pageviews for the whole project (used to compute the `share`
  metric). Same item shape, no `article` field.
- `start`/`end` are always plain 8-digit `YYYYMMDD`, regardless of
  granularity (see `dates.py`).
- `access=all-access`, `agent=user` are the client's defaults: `agent=user`
  excludes classified bot/spider traffic, which is what you want for a
  human-interest signal. `access` could be split into `desktop`/`mobile-web`/
  `mobile-app` if device-level analysis is ever needed (not currently
  exposed).
- **404 means "no data for this exact request"**, not an error - e.g. an
  article that didn't exist yet in the requested period. The client
  (`_get_json`) treats it as an empty result, not an exception.

Topic resolution uses two additional, separate APIs:

- **Wikidata search**: `www.wikidata.org/w/api.php?action=wbsearchentities`
  - finds the Wikidata item (QID) for a topic by label/alias in one
    language.
- **Wikidata sitelinks**: `action=wbgetentities&props=sitelinks` - given a
  QID, lists the article title in every language edition that has one.
  This is the mechanism for "same concept, different language" and is
  strongly preferred over any form of machine translation.
- **Wikipedia full-text search** (`{lang}.wikipedia.org/w/api.php?action=query&list=search`)
  - fallback when Wikidata has no sitelink for a requested language.

## Data availability & methodology caveats

- The REST pageviews API's "new" per-article/aggregate data starts
  **2015-07-01** (`api.DATA_AVAILABLE_SINCE`). There is an older "legacy"
  pagecounts dataset (2007-07 to 2016-08) with a different, less reliable
  counting methodology (no bot filtering, different definition of a
  "view"); this skill deliberately does **not** use it, to avoid silently
  mixing two incompatible measurement methods in one trend line. A
  `--start` before 2015-07 is clamped, and this is surfaced as a caveat in
  the output rather than done silently.
- Wikimedia updates the current month's data incrementally; the client
  gives any period ending within the last ~35 days a short (6h) cache TTL
  and treats anything older as immutable (`_cached_ttl_for_range`).
- Wikimedia asks API consumers to identify themselves via `User-Agent`;
  set `WIKI_TRENDS_CONTACT` (email or URL) as an environment variable if
  you'll be running this at any real volume. No API key is required for
  the endpoints used here.
- No documented hard rate limit for reasonable interactive use, but avoid
  hammering it in a loop - the disk cache exists specifically so a normal
  agent workflow never needs to.

## Real edge cases found during manual testing (see also `tests/`)

These are documented here because they are easy to reintroduce if the
resolution logic is refactored without them in mind:

1. **A language may genuinely have no article on a topic.** Tested with
   "intermittent fasting": Wikidata's item (Q1666254) has sitelinks for
   `cs` and `uk` but not `pl` as of testing. Falling back to full-text
   search on `pl.wikipedia` with the *English* query string returned an
   unrelated article ("Stres oksydacyjny") that only cited the topic in a
   reference - a naive "take the top search hit" implementation would
   have silently reported a trend for the wrong article. `resolve.py`
   guards this with `_title_overlaps_topic` (a normalized-word-overlap
   check) and reports `confidence: "none"` instead of a false "low"
   guess when the top hit shares no words with the query.
2. **A Wikidata top search hit's `label` can differ from what actually
   matched.** Querying "English language" returns item Q1860 whose
   primary `label` is just "English" - the match happened on the *alias*
   "English language" (`match.type: "alias"`, `match.text: "English
   language"`). Checking only `label` for an exact match would leave a
   perfectly good match unresolved. Fix: also accept an exact match on
   `match.text`.
3. **Outlier detection must not be blind at the edges of the series.** A
   spike on the very first or last data point (a very plausible real
   case - "this topic just blew up") is invisible to a centered rolling
   window, because the window has nowhere to look but at the spike
   itself, which then pollutes its own local baseline. `stats.py` instead
   scores residuals from the fitted linear trend against a *global*
   median/MAD, which has no edge blind spot.
4. **A regression on a subset of data can legitimately have zero
   variance.** Excluding a single anomaly from an otherwise-constant
   series leaves a perfectly flat remainder; a naive linear-regression
   helper that refuses "no variation" input as invalid will silently skip
   the "does the trend survive without the spike?" check right when it
   matters most. `stats.py` treats a flat remainder explicitly as
   slope=0, not as missing data.
