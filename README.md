# wikipedia-trend-insights

An [Agent Skill](https://agentskills.io/specification) that lets an agent
turn a question like *"is interest in astronomy growing in Ukrainian
Wikipedia, and can we trust that?"* into a resolved article, a trend with
an honest confidence label, a comparison chart, and a shareable 1-3 page
PDF - grounded in the [Wikimedia Pageviews API](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html).
It can also discover which topics are growing fastest in a language
edition or country when the user has not named a topic.

**Agent-facing usage instructions live in [`SKILL.md`](SKILL.md).** This
README is for a human maintaining or reviewing the code.

**Task requirements -> where they are met**

| Requirement | How |
|---|---|
| `SKILL.md` + own code doing the data work | `SKILL.md`; `scripts/wiki_trends.py` + `src/wiki_trends/` do fetching, caching, statistics (OLS, STL), charts and the PDF - the agent only runs one command |
| No compiled executables; reproducible environment | pure Python; `requirements.txt` (ranges) + `requirements.lock.txt` (exact); `.venv` / `.pyc` git-ignored; `doctor` checks the setup |
| All materials inside the skill directory | the repository root is the skill directory: code, tests, docs, eval set, example reports |
| Usable by a cheap tool-using model | checked with Claude Haiku 4.5 on 10 queries (`references/baseline-evaluation.md`) |
| AI tools used, output verified | see "How this was verified" below and `references/project-guide.md` §10 |

| Document | For whom | What it covers |
|---|---|---|
| [`SKILL.md`](SKILL.md) | the agent | when/how to call each command, how to read the JSON, rules for honest answers |
| `README.md` (this file) | maintainer / reviewer | layout, setup, execution logic, verification, roadmap summary |
| [`references/project-guide.md`](references/project-guide.md) | whoever presents/defends the project (Ukrainian) | detailed call graph per function, statistics, design decisions, limitations, likely review questions |
| [`references/baseline-evaluation.md`](references/baseline-evaluation.md) | reviewer / maintainer (Ukrainian) | baseline v1.0: 10 real queries run by Claude Haiku 4.5, rubric, scores (74/100), findings and their status |
| [`evals/baseline-queries.json`](evals/baseline-queries.json) | maintainer | the same 10 queries + prompt template + rubric, to re-run after every change |
| [`references/development-roadmap.md`](references/development-roadmap.md) | maintainer | full prioritized roadmap |
| [`references/api-reference.md`](references/api-reference.md) | maintainer | endpoints and real edge cases found in testing |

## Layout

```
SKILL.md                        agent instructions (start here if you're an agent)
requirements.txt                range-pinned dependencies (no compiled binaries)
scripts/wiki_trends.py          CLI entry point: doctor / resolve / study / rising
src/wiki_trends/
  api.py                        cached Wikimedia + Wikidata/Wikipedia HTTP client, 429 backoff
  cache.py                      disk cache (keeps repeat/related queries cheap)
  dates.py                      date-string parsing for the API's YYYYMMDD format
  resolve.py                    topic -> per-language article, with confidence levels
  stats.py                      trend/significance/anomaly analysis (deterministic, tested)
  charts.py                     matplotlib comparison chart
  report.py                     paginated PDF report (1-3 pages recommended, 5 max)
  pipeline.py                   orchestrates the above into the `study` command
  horizons.py                   multi-horizon mode: 3 months by week / 1 year / 3 years + summary
  rising.py                     year-over-year topic discovery (`rising` command)
  check.py                      checks numbers in the agent's recommendation against the data
tests/                          49 unit tests, no network required
evals/baseline-queries.json     agent-level evaluation set (10 queries + rubric)
references/                     guide, roadmap, API notes (see table above)
reports/                        example outputs (PNG / PDF / JSON), incl. the Haiku evaluation runs
requirements.lock.txt           exact tested dependency versions
```

## Install as a skill

The repository root **is** the skill directory (everything the skill needs
lives in it). Clone it where your agent looks for skills, e.g. for Claude
Code:

```bash
git clone <repo-url> ~/.claude/skills/wikipedia-trend-insights          # all projects
# or, for one project:  <project>/.claude/skills/wikipedia-trend-insights
```

## Setup

Pure Python, no compiled binaries in the repository; the environment is
recreated from the requirements files (the `.venv`, `__pycache__` and the
API cache are git-ignored).

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.lock.txt   # exact tested versions (Python 3.14)
# or: ./.venv/bin/pip install -r requirements.txt  # version ranges, Python 3.10+
./.venv/bin/python3 scripts/wiki_trends.py doctor   # deps + API reachability
```

Verified reproducible: a clean copy of the repository (no `.venv`, no
cache) installed from `requirements.lock.txt`, passed all tests and ran a
live `study` end-to-end.

Always call the venv's own interpreter by path (`./.venv/bin/python3`):
an agent's shell usually starts a fresh process per call, so an activated
venv does not persist.

## Running the tests

```bash
./.venv/bin/python3 -m unittest discover -s tests -v
```

49 tests, all offline (the resolve/API layer is tested against a fake
client, not the network). Several are regression tests for real bugs found
against the live API - see `references/api-reference.md`.

## Commands

| Command | Purpose | Exit codes |
|---|---|---|
| `doctor` | check dependencies and API reachability | 0 ok, 1 API unreachable |
| `resolve --topic --langs` | topic -> article per language only (no pageviews) | 0 |
| `study ...` | full pipeline for a named topic: resolve -> fetch -> analyze -> chart -> PDF -> JSON | 0 ok, 2 bad arguments, 3 analysis done but PDF over 5 pages (JSON/PNG kept) |
| `rising --targets uk,pl,en,US` | discover the fastest-growing articles, year over year | 0 |

```bash
./.venv/bin/python3 scripts/wiki_trends.py study \
  --topic "intermittent fasting" --langs pl,cs \
  --start 2024-09 --end 2026-08 --metric share \
  --out reports/fasting-pl-cs

# no dates -> three horizons at once (3 months by week, 1 year, 3 years)
./.venv/bin/python3 scripts/wiki_trends.py study \
  --topic astronomy --langs uk --metric views --out reports/astronomy-uk

./.venv/bin/python3 scripts/wiki_trends.py rising \
  --targets uk,pl,cs,en,US --top 5 --out reports/rising
```

## Execution logic

### `study` (a named topic)

1. **Validate and normalize input** (`pipeline.run_study`, `dates.parse_boundary`).
   Without `--start`/`--end` the study switches to **multi-horizon mode**
   (`horizons.windows`): last 13 complete weeks, last 12 complete months,
   last 36 complete months.
   Dates before 2015-07-01 (when the pageviews data starts) are clamped,
   and an end date inside a still-running month is cut back to the last
   full month (a partial month looks like a collapse) - both with a caveat.
2. **Resolve the topic to one article per language** (`resolve.resolve_topic`).
   - Manual titles (`--articles`) are trusted as given.
   - Otherwise the topic is anchored to a Wikidata item **only on an exact
     label or alias match**, and per-language titles come from that item's
     sitelinks (same concept across languages, no machine translation).
   - Languages without a sitelink fall back to full-text search on that
     wiki: a hit that shares words with the topic is marked `low`
     confidence; a hit that shares none is reported as `none` with its
     candidates - never silently guessed.
3. **Fetch pageviews** (`api.WikimediaClient.pageviews_per_article`, plus
   `pageviews_aggregate` for `--metric share`). Every response goes
   through the disk cache.
4. **Analyze each series** (`stats.analyze_series`):
   - `share` = article views per million views of that edition, so wikis
     of different size are comparable;
   - growth % = mean of the last 3 points vs mean of the first 3;
   - OLS slope with p-value and R²;
   - anomalies = robust (median/MAD) z-score of residuals from the trend
     line, |z| > 3.5;
   - `spike_sensitive` = re-fit without anomalies; if the slope flips sign
     or more than halves, the trend is carried by a spike;
   - **STL decomposition** (`statsmodels`, robust, period 12, on the log
     of the series - pageview seasonality is multiplicative) for monthly
     series with >= 24 points: `trend_change_pct` / `trend_change_12m_pct`
     (smoothed trend without seasonality), `seasonal_strength` (Hyndman's
     F_S), `seasonal_amplitude`, `seasonal_peak_month`. When the yearly
     cycle matters (F_S >= 0.4, the peak month >= 20% above the trough, and
     that peak repeats: >= 1.1x the year's median in >= 2 different years
     with comparable strength - otherwise a single burst on 24 months fools
     STL),
     regression, anomalies and confidence are computed on the seasonally
     adjusted series (`deseasonalized: true`) - so every September peak is
     no longer "noise" or an "anomaly";
   - `yoy_growth_pct` = last 3 months vs the same months a year earlier
     (fallback when there is no STL);
   - `seasonal_months` = calendar months that peak in 2+ years (repeated
     anomalies, or the same month topping each 12-month block at >= 1.5x
     its median) -> automatic seasonality caveat;
   - confidence: `high` if p < 0.05 and R² >= 0.3; `medium` if p < 0.1;
     `low` for fewer than 6 points, no variation, spike-driven or noisy
     series. Each label comes with a ready-made `confidence_reason`.
5. **Rank** languages three ways (`by_growth`, `by_current_interest`,
   `by_confidence`) plus a deterministic `explore_next` order: tag by the
   regression direction that the confidence describes
   (`reliably_growing` / `turning` when YoY disagrees by >= 5% /
   `unclear` / `reliably_declining`), then by interest level within a tag -
   so the agent never has to weigh these itself. Collect `caveats`
   (clamped dates, fallback matches, missing data, seasonality,
   low-confidence languages).
6. **Multi-horizon mode only** (`pipeline._analyze_horizons`,
   `horizons.summarize`): 1y = the last 12 months of the 3y series,
   adjusted with the 3y seasonal factors; 3m = one daily fetch summed into
   complete weeks, adjusted with each week's calendar-month factor. Each
   horizon gets a change (STL trend where available), a confidence and a
   direction (up / down / flat / unclear), and `horizon_summary` turns them
   into one pattern sentence (e.g. "long decline, flattened in the last
   year") so the agent doesn't reconcile three tables itself.
7. **Check the recommendation** (`check.check_recommendation`): every
   number in `--recommendation` must exist in the analysis; unmatched ones
   go to `recommendation_check`, stderr and a caveat in the PDF.
8. **Render** the chart (`charts.render_comparison_chart`) and the PDF
   (`report.build_pdf`): header -> chart (dashed line = STL trend) -> table
   -> "Різні періоди" table (multi-horizon mode) -> data-derived findings
   -> the agent's recommendation in a separate box directly under the data
   -> caveats. Pages break automatically; more than 5 pages raises
   `ReportTooLongError`, and the study still writes its JSON/PNG and exits 3.
9. **Write the JSON** with every number the agent needs for its answer.

### `rising` (no topic named)

1. Take the last `--window` (3) full months and the same months a year
   earlier - comparing year over year cancels seasonality.
2. Candidates = articles in the monthly top-1000 of **every** recent month
   (a one-month news spike cannot qualify). Country targets (`US`) use the
   per-country top list, which only exists per day, so each month is
   sampled on days 1/8/15/22.
3. Filter out non-articles (main page, localized namespaces from
   `site_info`), URL fragments, and titles containing a year (yearly
   events/lists have no year-ago counterpart).
4. Pre-rank by growth using the year-ago top lists (an article absent from
   them gets the list's floor, so its growth is a lower bound), then
   re-check the best 40 on their exact 24-month history with the same
   `analyze_series` used by `study`.
5. Split the results into `rising` (sustained), `news_spikes` (growth
   carried by one burst), `new_topics` (almost no views a year ago) and
   `suspected_bots` (more than 90% desktop views; human traffic is typically
   15-40%). The bot check is lazy: only items that would be shown cost a
   request.

### Caching and API etiquette

- Each response is cached as one JSON file keyed by sha256 of the request.
  Ranges that ended more than 35 days ago are cached forever (history does
  not change); recent ranges for 6 hours.
- `api_calls` in every JSON output reports `requests_made` vs `cache_hits`.
  Re-rendering a report or adding a language only pays for what is new.
- HTTP 429 is retried with `Retry-After` or exponential backoff; 404 means
  "no data", not an error. Set `WIKI_TRENDS_CONTACT` to identify yourself
  in the User-Agent, as Wikimedia asks.

### How conclusions stay grounded in data

- The agent's recommendation is the only free text in the report, and it
  sits directly under the table it must be justified by.
- Every growth number carries its confidence and reason; `caveats` appear
  in both the JSON and the PDF.
- `SKILL.md` forbids quoting growth without confidence and guessing
  article titles, and says how to phrase "promising but unverified".

## How this was verified

- **API behavior first.** Every endpoint was checked against the live
  Wikimedia/Wikidata API before code was written against it.
- **End-to-end runs with visual checks.** The full pipeline was run on real
  data for all three query shapes from the task, and the generated PDFs
  were rendered and inspected - which caught layout bugs (table
  overlapping the next section, clipped text, single-page overflow)
  that a code-only review missed.
- **Numbers cross-checked by hand.** Surprising results were checked on the
  raw monthly/daily series: e.g. a Turkish "+132%" turned out to be a step
  change from 2026-07-30, not a one-day bot burst; `.xyz`/`RDFa`/
  `Microdata (HTML)` topping en.wikipedia's growth list were 96-99.9%
  desktop traffic, i.e. bots - which led to the `suspected_bots` filter.
- **Edge cases on purpose**: 6- and 12-language reports (2 pages), an
  oversized recommendation (4 pages -> warning; 7-9 pages -> exit 3 with
  JSON kept), a topic missing from one language edition.
- **Cheap-model run (Claude Haiku 4.5, 2026-09-22).** Three independent
  Haiku agents got only the skill path and a user request (the task's
  three example queries). All three read `SKILL.md`, ran `doctor` and a
  single correct `study` call (`share` for cross-language, `views` for
  one language) and wrote no data code of their own - the mechanics pass.
  The **conclusions** did not fully pass: on astronomy it reported "-91%,
  very trustworthy" without noticing the September seasonality and the
  partial last month; on English it ranked the lowest-interest markets
  first because "least decline" topped `by_growth`, and it invented
  causes.
- **Fix and re-run.** The fixes moved those judgment calls from the model
  into code: the end date is clamped to the last full month;
  `yoy_growth_pct` and `seasonal_months` add an automatic seasonality
  caveat; a deterministic `ranking.explore_next`; a `recommendation_check`
  that matches every number in the agent's text against the data.
  9 regression tests were added. On the re-run, Haiku's numbers matched
  the JSON in all three scenarios, seasonality was named, and audiences
  were ordered by `explore_next`. The remaining wording slips (period
  label, a missing `--recommendation`, "average" called "current") were
  addressed with SKILL.md rules. Details and per-scenario tables:
  `references/project-guide.md` §9.1-9.2.
- **STL corrected our own earlier metric.** For uk "Астрономія" the
  year-over-year figure said -7.5% (June-August only, where interest is
  always at its floor), while the STL trend over all 12 months fell ~59%
  (e.g. September 4,687 -> 1,642). For pt "Língua inglesa" a "+7.9% YoY
  recovery" became -15% on the STL trend. That is why STL is now the
  primary change figure and YoY only a fallback. The decomposition was
  checked on synthetic data with a known answer (trend doubling under a
  2.5x September peak -> recovered +108%).
- **Haiku runs 3-4.** With STL and multi-horizon mode, astronomy and the
  English ranking passed (numbers match the JSON; recommendation check
  13/13). The fasting scenario regressed: Haiku invented a Polish title via
  `--articles` and switched to the broader topic "Post/Půst". Root cause
  partly in code - a caller-supplied title with zero pageviews was still
  reported as `confidence: high`. Fixed (`manual_not_found`, test) plus
  SKILL.md rules; run 4 passed. Lesson recorded in the guide §9.3: rules
  that only *ask* the model eventually get broken; checks in code hold.
- **Baseline v1.0 evaluation (10 queries, Haiku 4.5): 74/100.** Each
  query probes one capability or edge case (multi-horizon, explicit
  period, `rising` for a language and a country, daily peak, 8 languages,
  pre-2015 start, ambiguous topic, niche topic, seasonality). Choosing the
  right command scored 18/20; the weak spots were judgement calls the
  skill still left to the model: an ambiguous "Mercury" resolved to the
  car brand with `high` confidence (4/10), a near-empty "Євробачення" page
  instead of the 2025 contest article (5/10), market order ignoring
  `explore_next` (6/10). One regression found during the run (one-off
  bursts classified as seasonality after STL) was fixed in v1.1; the rest
  are the v1.2 plan. Full table, per-query notes and findings F1-F9:
  `references/baseline-evaluation.md`.

## Roadmap (summary)

Full version with priorities: [`references/development-roadmap.md`](references/development-roadmap.md).

**Done:**
- paginated PDF with a 3/5 page budget;
- 429 backoff;
- `rising` topic discovery with a bot filter;
- Haiku-driven fixes: full months, YoY + seasonality, `explore_next`, recommendation check;
- STL trend/seasonality decomposition and multi-horizon mode (3 months by week, 1 year, 3 years).

**Next, by priority:**
0. **v1.2 - fixes from the baseline evaluation** (F2-F8): scan all
   Wikidata hits for an exact match; flag ambiguous topics (>= 2 exact
   matches) instead of picking the first; pageview volume on resolution
   candidates + yearly-event guidance; `seasonal_peak_ratio`;
   `avg_level` / `current_level` in `explore_next`; check that the
   recommendation's first-named language matches `explore_next`; pass the
   user's dates through unchanged. Then re-run `evals/baseline-queries.json`.
1. **Trust in conclusions.** Mann-Kendall test, bootstrap CIs on growth,
   direction claims in the recommendation checker. (Done: STL
   decomposition, multi-horizon mode, full months only, seasonality
   caveat, `explore_next`, number check of the recommendation.)
2. **Noise.** Bot filtering in `study` too; attribute spikes to news
   events (GDELT / Current events).
3. **Topic clusters and redirects.** "Learning English" = English language
   + IELTS + TOEFL + grammar + ESL, aggregated; merge renamed articles.
4. **Segmentation and discovery.** Split language editions by country
   (language is not a country); suggest adjacent topics.
5. **External sources.** Google Trends for triangulation, app-store charts
   per country, market/competitor data (a separate skill) combined into an
   opportunity score.
6. **Scale.** Parallel fetches with a shared rate limiter, range-merging
   cache, SQLite/DuckDB, Wikimedia dumps for bulk scans.
7. **Reports and monitoring.** Top-N summaries for huge studies, scheduled
   re-runs with alerts, CJK font fix.
8. **Verification / CI.** Golden cases from real sessions, golden-PDF
   regression, scheduled live smoke test, re-run the Haiku scenarios after
   every change to `SKILL.md`.
