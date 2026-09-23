---
name: wikipedia-trend-insights
description: Analyzes Wikipedia pageview trends (via the Wikimedia Pageviews API) to help B2C product teams decide which topics, languages or markets to invest in next. Generates a comparison chart and a short shareable PDF report (1-3 pages, max 5) with growth %, statistical confidence, and detected anomalies. Use when the user asks to compare interest in a topic across Wikipedia language editions, check whether interest in a topic is growing (and how much to trust that), or wants a short report on Wikipedia pageview trends to justify a product/content decision.
compatibility: Requires Python 3.10+ and outbound internet access to wikimedia.org and wikidata.org / wikipedia.org.
---

# Wikipedia Trend Insights

Turns a plain-language question about topic/language interest into: a
resolved article per language, a trend with an honest confidence label, a
comparison chart, and a short PDF report (1-3 pages, max 5) - grounded in real pageview
data, not a guess.

**Do the real work with the scripts in this skill, not by writing your own
data-fetching/plotting code.** The scripts already handle topic
resolution, caching, statistics and PDF layout correctly (including edge
cases found during testing - see `references/api-reference.md`); a
one-off script written from scratch for a single request will not.

## Setup (once per environment)

Your shell tool likely does not keep an activated virtualenv between
separate calls (each call is a fresh process) - so set up a venv once,
then always invoke the *venv's own* `python3` by path. Don't rely on
`source .venv/bin/activate` persisting; it won't.

```bash
cd <this skill's directory>
python3 -m venv .venv          # once
./.venv/bin/pip install -q -r requirements.txt   # once
./.venv/bin/python3 scripts/wiki_trends.py doctor   # confirms deps installed + API reachable
```

If `.venv` already exists (check with `ls .venv` before recreating it),
skip straight to `doctor`. From here on, every command in this file uses
`./.venv/bin/python3` - use that same path, not a bare `python3`.

## The one command to reach for: `study`

For almost every user request, resolve → fetch → analyze → chart → PDF in
one call:

```bash
./.venv/bin/python3 scripts/wiki_trends.py study \
  --topic "intermittent fasting" --langs pl,cs \
  --start 2023-09 --end 2025-09 --granularity monthly --metric share \
  --out reports/fasting-pl-cs
```

This prints a JSON summary to stdout and writes `reports/fasting-pl-cs.png`
(chart), `.pdf` (report, 1-3 pages recommended, 5 max) and `.json` (the same summary, saved). Read the
JSON to write your reply to the user; do not re-derive numbers from the
chart image.

Key flags:

| Flag | Meaning |
|---|---|
| `--topic "..."` | Free-text topic; resolved to a per-language article automatically (see below). Omit if using `--articles`. |
| `--langs pl,cs,uk` | ISO language codes (maps to `{code}.wikipedia`). |
| `--articles "pl:Tytuł,cs:Název"` | Skip resolution - use exact known titles. Combine with `--langs` for the rest. |
| `--start` / `--end` | `YYYY-MM` or `YYYY-MM-DD`. Data only exists from 2015-07 onward. **If the user names no period, omit both** - the study then runs three horizons at once (last 3 months by week, last year, last 3 years; see "Multi-horizon mode"). If they name one ("last two years"), pass it. In your answer, describe periods from `date_range` / `horizons.*.window` in the JSON - never call a 3-year window "2-year". |
| `--granularity` | `monthly` (default; use for multi-year comparisons - less noisy, smaller payload) or `daily` (short/recent windows, or to pinpoint an exact spike date). |
| `--metric` | `views` (raw) or `share` (per-million share of that language edition's total traffic). **Use `share` whenever comparing across languages** - raw views make a huge wiki (e.g. English) look more "interested" than it is; `share` is the fair comparison. Use `views` for a single-language trend. |
| `--recommendation "..."` | Your own conclusion text, printed in a clearly-labeled box in the PDF, directly under the data it must be justified by. See "Writing the recommendation" below. |
| `--out path/base` | Output base path (no extension). |

Run `./.venv/bin/python3 scripts/wiki_trends.py study --help` for the full list.

### Mapping the task's example queries to calls

- *"Compare growth of interest in intermittent fasting between Polish and
  Czech Wikipedia over the last two years"* →
  `study --topic "intermittent fasting" --langs pl,cs --start <today-2y> --end <today> --metric share`
- *"Is interest in astronomy growing in Ukrainian Wikipedia, and how much
  can we trust that?"* →
  `study --topic astronomy --langs uk --metric views` (no period named ->
  no dates -> multi-horizon), then lead with `horizon_summary.uk.text` and
  give each horizon's change **with** its confidence - never quote growth
  without the confidence that goes with it. With explicit dates,
  `--end <today>` is fine: an unfinished current month is dropped
  automatically (caveat added).
- *"Compare interest in learning English across our chosen language
  editions and report which audiences to explore next"* →
  `study --topic "English language" --langs <their list> --metric share`
  (no dates unless they gave a period), then follow `ranking.explore_next` **in its given order** - do not
  re-rank by `by_growth` yourself (when every language declines, the top of
  `by_growth` is merely the smallest decline, often the smallest audience).
  Use each item's `tag`: `reliably_growing` -> explore first;
  `turning` (the long trend declined but the last 12 months are up >= 5%) ->
  "possible recovery, confirm next quarter"; `unclear` (low confidence) ->
  "promising but unverified", check with a longer range;
  `reliably_declining` -> large-but-shrinking or low priority depending on
  `level`. Always mention `level` - a big audience in slow decline can
  still matter more than a tiny growing one.

## Reading the JSON output

```jsonc
{
  "resolved_articles": {
    "pl": {"title": "...", "method": "wikidata|search_fallback|manual|unresolved",
           "confidence": "high|low|none", "candidates": [...], "note": "..."}
  },
  "trends": {
    "pl": {
      "n_points": 25, "growth_pct": -13.2, "p_value": 0.278, "r_squared": 0.02,
      "confidence": "high|medium|low|none", "confidence_reason": "...",
      "anomalies": [{"date": "2024-08-01", "value": 5000.0, "z_score": 8.0}],
      "spike_sensitive": false,
      "yoy_growth_pct": -7.5,          // last 3 months vs same months a year earlier (monthly, >=15 points)
      "seasonal_months": ["09"],       // calendar months that peak every year
      // STL decomposition (monthly, >= 24 points; null otherwise):
      "trend_change_pct": -90.4,       // smoothed trend without seasonality, whole window
      "trend_change_12m_pct": -58.9,   // ...over the last 12 months
      "seasonal_strength": 0.75, "seasonal_amplitude": 3.57,  // peak month ~4.6x the trough
      "deseasonalized": true           // p/R²/anomalies/confidence computed without the yearly cycle
    }
  },
  // multi-horizon mode only (no --start/--end), else null:
  "horizon_summary": {"uk": {"text": "Стабільно падає і за 3 роки, і за останній рік; ...",
                             "pattern": "steady_decline|decline_flattening|reversal_up|growth_stalling|reversal_down|stable|steady_growth|mixed|no_clear_trend",
                             "directions": {"3m_weekly": "unclear", "1y": "down", "3y": "down"},
                             "changes": {"3m_weekly": 94.1, "1y": -58.9, "3y": -90.4}}},
  "horizons": {"uk": {"3m_weekly": {"confidence": "low", "window": ["2026-06-22", "2026-09-20"], ...},
                      "1y": {...}, "3y": {...}}},
  "ranking": {"by_growth": [...], "by_current_interest": [...], "by_confidence": [...],
              "explore_next": [{"lang": "vi", "tag": "reliably_growing|turning|unclear|reliably_declining",
                                "level": 252.6, "trend_growth_pct": -31.8, "yoy_growth_pct": -21.0,
                                "confidence": "low"}]},
  "recommendation_check": {"ok": true, "checked": 6, "unmatched": []},
  "caveats": ["..."],
  "output_files": {"chart_png": "...", "report_pdf": "...", "analysis_json": "..."}
}
```

**Always carry `confidence` and `caveats` into your reply to the user** -
a growth number without its confidence is a half-answer and can mislead a
product decision. `confidence_reason` is already a ready-to-use sentence
explaining why (statistical significance, sample size, or a spike-driven
trend) - reuse it rather than inventing your own justification.

### Which growth number to quote

- Levels: `avg_value` is the average over the **whole period**;
  `last_period_avg` is the **current** level (last 3 points);
  `first_period_avg` is where it started. Don't label the average "current".

- Order of preference for "how much did it change":
  1. `trend_change_pct` / `trend_change_12m_pct` (STL trend without
     seasonality - uses every month);
  2. `yoy_growth_pct` (only the last 3 months vs the same months a year
     earlier - can mislead: for astronomy it compared only summer months,
     where interest is always at its floor, and showed -7.5% while the
     year's trend fell ~59%);
  3. `growth_pct` (first 3 vs last 3 points - distorted by seasonality).
  Mention lower-ranked figures only as secondary context, and name which
  one you quote ("trend without seasonality: -58.9% over 12 months").
- Two true numbers for different periods are not a contradiction:
  "-90% over 3 years" and "-59% in the last year" both hold. Don't call
  one of them "the real" change.
- If `seasonal_months` is non-empty, say so explicitly ("interest peaks
  every September") - and never present `growth_pct` alone as the trend:
  a window that starts at a peak and ends in a trough exaggerates change.
- Phrase a p-value correctly: "p=0.004: a slope this strong would appear by
  chance in under 1% of cases if there were no real trend" - **not** "99.6%
  probability the trend is real".
- **Do not state causes that are not in the data** (social media, school
  reforms, competitors...). You may offer one as an explicit hypothesis to
  check, labeled as such.

### Multi-horizon mode (no `--start` / `--end`)

The same study answers for three windows: `3m_weekly` (13 complete weeks,
daily data summed per week), `1y` (12 complete months) and `3y` (36
months, where STL runs). Short windows are seasonally adjusted with the
3-year factors, so a back-to-school September does not read as growth.

- Lead with `horizon_summary.<lang>.text` - it is computed in code from
  the directions and confidences; don't re-derive it.
- Then give each horizon: change + its confidence (the PDF table
  "Різні періоди" shows the same). A horizon with low confidence is
  "no reliable signal", not "flat".
- `trends` / `ranking` / `explore_next` in this mode describe the 3-year
  window.
- Cost: one extra daily request per language for the weekly view; 1y is a
  slice of the 3y series.

### When resolution confidence is "low" or "none"

- `"none"`: no article was found or the closest match shares no words
  with the topic. **Do not silently proceed and do not guess a title.**
  Tell the user this language edition may not have the topic, show the
  `candidates` if any were returned, and offer `--articles <lang>:<title>`
  if they can supply the right one. "This edition has no article on it"
  **is a complete, correct answer** for that language - answer the rest
  of the question with the languages that do have data.
- `--articles` only takes titles **the user gave you** or titles from
  `candidates`. Never pass a title you made up or translated yourself: a
  non-existent title comes back as `method: "manual_not_found"`,
  `confidence: "none"`.
- **Never swap in a different topic** (broader, narrower, related) to fill
  the gap - e.g. "Post"/"Půst" (fasting in general, peaking at Lent) is not
  "intermittent fasting". If a related article might help, *ask the user*
  first; don't run it and lead your answer with it.
- `"low"` (`search_fallback`): a plausible article was found via
  full-text search, not a verified Wikidata link. Mention this to the
  user as a soft caveat before treating the trend as solid, especially if
  they'll act on it.
- `"high"` (`wikidata` or `manual`): safe to treat as correct.

### When a trend's confidence is "low"

This means the data does not clearly support a trend claim - usually too
few points, too much noise (high p-value), or a trend driven by a single
spike (`spike_sensitive: true`). Report the number but explicitly say it
should not be treated as a reliable signal, and suggest a longer date
range or a look at the `anomalies` dates (they may correspond to a real
event worth explaining, e.g. a news story or a school-year cycle - not a
sustained shift in interest).

## Discovering what is trending: `rising`

When the user has *not* named a topic ("top 5 topics with fast-growing
demand in uk/pl/en", "what's trending in the US"), `study` cannot help -
use `rising`:

```bash
./.venv/bin/python3 scripts/wiki_trends.py rising \
  --targets uk,pl,cs,en,US --top 5 --out reports/rising
```

- Targets: lowercase = language edition (`uk` -> uk.wikipedia); uppercase
  2-letter = country (`US`), which covers everything viewed from that
  country (mostly en.wikipedia). There is no "American Wikipedia" - map
  "American" to `US`, "English" to `en`.
- Method: articles in the top-1000 of *each* of the last `--window` (3)
  full months, compared year-over-year to the same months a year earlier
  (cancels seasonality). The best `--verify` (40) candidates are re-checked
  on exact per-article history with the same trend statistics as `study`.
  Titles with a year in them (yearly events/lists), namespace pages and
  URL fragments are filtered out.
- Output per target: `rising` (YoY growth not driven by one burst month -
  the answer to "demand is growing"), `news_spikes` (growth carried by a
  burst - usually a news story), `new_topics` (almost no views a year ago,
  so a % is meaningless), `suspected_bots` (>90% desktop views - automated
  traffic; list them as excluded, never as trends). Each item carries
  `trend_confidence` and `desktop_share` - report the confidence.
- What it measures is **attention**, and top lists are dominated by news,
  people, war and entertainment. Group results by theme in your reply and
  say plainly which are news-driven; don't present a politician's
  appointment as "market demand".
- Country targets are sampled on 4 days per month (the API has no monthly
  country list), and their exact numbers come from the article's global
  history - say so.
- Cost: ~50 requests per language target and ~70 for a country target on
  a cold cache; Wikimedia may throttle (the client backs off and retries
  on 429), so a 5-target run can take several minutes. Run it in the
  background.

## Writing the recommendation

The `--recommendation` text you pass is the *only* free-text content in
the report - everything else is generated straight from the numbers. Keep
it to claims the printed table/findings actually support: reference the
specific growth/confidence values, and say plainly when the data is too
thin or noisy to justify a strong recommendation. If you don't have a
recommendation yet (e.g. the user hasn't said what they're deciding
between), omit `--recommendation` rather than filling it with filler text.

**If the user asks for a report, recommendations or "which X next"**, the
PDF must carry your conclusion: run `study` once, read the JSON, then run
the same command again with `--recommendation "..."` (the second run is
served from cache). A PDF saying "(не надано)" does not answer that request.

Every number in it is checked against the analysis: see
`recommendation_check` in the JSON (a warning is also printed). If
`ok` is false, the `unmatched` numbers are not in the data - **fix the text
and re-run the same command** (served from cache, no extra API calls);
otherwise the PDF lists them as a caveat. Copy numbers from the JSON as
printed (e.g. `-29.9%`, `127`), don't round creatively.

## Efficient follow-ups (same session or later)

The skill caches every Wikimedia/Wikidata API response on disk under
`--cache-dir` (default `./.wiki_trends_cache`, reused automatically across
calls in the same working directory). This means:

- Re-running the same `study` costs no extra API calls.
- A follow-up that changes one thing (add a language, extend the date
  range, switch `views`↔`share`) only fetches what's new - already-fetched
  languages/periods are served from cache. Just call `study` again with
  the updated flags; you do not need to plan a special "incremental" path.
- If the user asks to *reconfirm* freshness of very recent data, pass
  `--cache-dir` pointing at a fresh empty directory, or wait - the last
  ~35 days of data have a short (6h) cache TTL and refresh automatically.

Check `api_calls` in the JSON output (`requests_made` vs `cache_hits`) if
you want to confirm caching is working as expected.

## Known limitations (be upfront about these with the user)

- Pageview data only exists from **2015-07-01** onward; a `--start` before
  that is silently clamped (with a caveat noted in the output).
- Confidence combines a significance test (p-value) and goodness-of-fit
  (R²); a trend that is statistically real but strongly seasonal (e.g. a
  back-to-school spike every September) can score only "medium" even with
  a tiny p-value, because R² is diluted by the seasonality. Read
  `confidence_reason`, not just the label, and mention seasonality
  yourself if you see repeating same-month anomalies across years.
- Topic resolution across languages depends on Wikidata coverage; niche
  or very new topics may simply not have an article in some language
  editions yet (this is itself a finding worth reporting, not a bug).
- The PDF paginates automatically across A4 pages (page numbers, table
  header repeated on continuation). Page budget: **up to 3 pages is good
  practice** (builds silently), **4-5 pages builds with a warning**, and
  **more than 5 pages writes no PDF**: the command exits with code 3,
  still writes the `.json`/`.png`, sets `output_files.report_pdf` to
  `null` and adds the reason to `caveats`. `output_files.report_pages`
  always holds the page count. If you hit the 4-5 page warning or the
  cap, split the study into fewer languages per report or shorten
  `--recommendation` - aim for <=3 pages. ~12 languages with a short
  recommendation fit in 2 pages. Newlines in `--recommendation` are kept
  as paragraph breaks.

See `references/development-roadmap.md` for how this skill is meant to
grow beyond these limits, and `references/api-reference.md` for the
underlying API details and edge cases this skill's code already accounts
for.
