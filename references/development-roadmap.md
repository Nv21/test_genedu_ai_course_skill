# Development roadmap: from basic queries to deeper, larger-scale research

Part A describes what the skill already gives an agent to evaluate results,
verify conclusions and handle follow-ups cheaply - and the gaps in that.
Part B is the prioritized plan for growing it into harder studies and
larger data. Every item is scoped to be addable without rewriting what
exists.

**How to iterate.** Grow the skill from real failures, not imagined
features. Each iteration so far started from a concrete query that broke
something (429 rate limits -> retry/backoff; a clipped one-page PDF ->
pagination; "what is trending?" with no topic -> `rising`). Each new
capability should ship with (1) the code, (2) a golden test case that
reproduces the original failure, and (3) a line in SKILL.md telling the
agent when and how to use it - otherwise the agent won't reach for it.

---

## Part A. Evaluate, verify, repeat - current state

### Evaluating results
- Every growth number comes with `confidence` (p-value + R²) and a
  ready-made `confidence_reason`, so the agent never has to invent a
  justification.
- `spike_sensitive` re-fits the trend without anomalous months; if the
  conclusion flips, the trend is a one-off event, not a shift.
- Topic resolution reports how it matched (`wikidata` / `search_fallback`
  / `none`) and refuses to guess when the best hit shares no words with
  the topic (e.g. "intermittent fasting" on pl.wikipedia -> no article,
  not "Stres oksydacyjny").
- The `share` metric normalizes by the edition's total traffic, so
  languages of very different size are comparable.

### Verifying conclusions
- The recommendation is the only free text in the report, printed in a
  labeled box directly under the table it must be justified by.
- `caveats` are carried into both the JSON and the PDF footer.
- SKILL.md rules: never quote growth without confidence; never guess a
  title; flag "high growth, low confidence" as "promising but unverified".

### Repeated and related queries
- On-disk cache: past data cached forever, the last ~35 days for 6h;
  `api_calls` shows requests vs cache hits. Adding a language or switching
  `views`/`share` only fetches what is new; re-rendering a report costs 0
  requests.
- One `study` command runs the whole pipeline, so no intermediate step can
  be lost between tool calls.
- The client backs off and retries on HTTP 429.

### Gaps (fed into Part B)
- Direction claims in the recommendation are not checked yet (numbers are).
- The cache key is the exact date range, so extending a range refetches it.
- One article is a narrow proxy for a real question ("learning English" is
  not only the "English language" article).

---

## Part B. Roadmap (priority order)

| # | Area | Value | Effort |
|---|---|---|---|
| 1 | Trust in conclusions (checker, seasonality, CIs) | high | low |
| 2 | Noise: bots and news | high | medium |
| 3 | Topic clusters and redirects | high | medium |
| 4 | Segmentation and discovery | high | medium |
| 5 | External sources (triangulation, market) | very high | high |
| 6 | Scale and performance | medium (high at scale) | medium |
| 7 | Reports and monitoring | medium | low-medium |
| 8 | Verification / CI | high | low-medium |

### Done so far
- Paginated PDF: 1-3 pages recommended, 5 max; over the cap no PDF is
  written, exit code 3, JSON/PNG kept.
- 429 backoff/retry in the API client.
- `rising` command: year-over-year discovery of fast-growing articles per
  language edition or country, split into `rising` / `news_spikes` /
  `new_topics`, candidates verified on exact per-article history.
- Bot filter in `rising`: items with >90% desktop views go to
  `suspected_bots` (found when `.xyz`, `RDFa`, `Microdata (HTML)` topped
  en.wikipedia at 96-99.9% desktop).
- Cheap-model check on Claude Haiku 4.5, fixes, and a re-run: numbers
  match the data in all three scenarios (see `project-guide.md` §9.1-9.2).
- Baseline v1.0 evaluation on 10 queries (74/100) and a reusable eval set
  (`evals/baseline-queries.json`); v1.1 fixes F1 (seasonality must repeat).

### 0. v1.2 - fixes from the baseline evaluation (next)

Baseline v1.0 scored 74/100 on 10 queries run by Claude Haiku 4.5
(`references/baseline-evaluation.md`, set in `evals/baseline-queries.json`).
F1 (one-off burst classified as seasonality) is fixed in v1.1. Open:
- **F2** resolver: take the first *exact* label/alias match among all
  Wikidata hits, not only hit #1 ("mathematics" -> genealogy project).
- **F3** ambiguity: >= 2 exact matches -> `confidence: "ambiguous"` with
  labelled candidates; SKILL.md: ask the user ("Mercury" -> car brand).
- **F4** candidates carry pageview volume; mark a chosen title `low` if it
  has ~100x fewer views than the best candidate; guidance for yearly
  events (Eurovision 2025).
- **F5** `seasonal_peak_ratio` (= 1 + amplitude), the only figure models see.
- **F6** `explore_next`: `avg_level` + `current_level` instead of `level`.
- **F7** `recommendation_check`: the first market named must be first in
  `explore_next` (or the text must say why not).
- **F8** SKILL.md: pass the user's dates unchanged; the skill clamps and
  adds the caveat.
- Then re-run the eval set; a query dropping >= 2 points is a regression.

### 1. Trust in conclusions
- Done (from the Haiku run): monthly `--end` clamped to the last full
  month; `yoy_growth_pct` + `seasonal_months` with an automatic caveat;
  deterministic `ranking.explore_next`; clearer medium wording; SKILL.md
  rules for which growth to quote, p-value phrasing, no invented causes,
  default 2-year period, always pass `--recommendation` when a report is
  requested.
- Done: **recommendation checker** (`check.py`) - every number in
  `--recommendation` is matched against the analysis; mismatches go to
  `recommendation_check`, stderr and a PDF caveat. Next: also check
  direction claims ("pl is growing" when pl's tag is declining).
- Done: **STL decomposition** (statsmodels, multiplicative via log):
  trend without seasonality is the primary change figure; regression,
  anomalies and confidence run on the adjusted series when the yearly
  cycle matters. Next: a changepoint test (e.g. `ruptures`) to date *when*
  a trend turned.
- Done: **multi-horizon mode** when no dates are given (3 months by
  week, 1 year, 3 years, with a code-computed `horizon_summary`).
- **Mann-Kendall** trend test as a second opinion to OLS; disagreement
  between them is itself a confidence signal.
- **Bootstrap confidence intervals** on growth: "+42% (90% CI +28..+61%)"
  instead of a bare point estimate.

### 2. Noise: bots and news
- **Automated traffic**: done for `rising` (desktop share > 90%); extend
  the same check to `study` series.
- **News attribution**: link anomaly dates to events (GDELT, Wikipedia
  "Current events" portal) so a report says "spike on 2026-07-30 coincides
  with X" instead of just "anomaly".
- Report spike-driven and sustained growth separately everywhere (`rising`
  already does this; `study` should too).

### 3. Topic clusters and redirects
- **Clusters instead of one article**: expand a topic via Wikidata
  ("part of", "subclass of", "related") into a set - e.g. "learning
  English" = English language + IELTS + TOEFL + English grammar + ESL -
  and aggregate. A cluster signal is more robust and closer to the real
  question.
- **Redirects and renames**: query MediaWiki for moves/redirects in the
  window and merge the series, or add a caveat ("renamed on X; earlier
  data is under another title").

### 4. Segmentation and discovery
- **Language is not a country**: use per-country pageview data to split a
  language edition geographically (Spanish = Spain + Latin America;
  Ukrainians also read en/ru). Needed for go-to-market questions.
- **Adjacent-topic discovery**: from a resolved topic, suggest related
  articles or other editions worth checking (related pages, Wikidata
  links, `rising` restricted to a topic cluster).
- Theme grouping of `rising` output (people / news / tech / entertainment)
  so product-relevant demand is not buried under news.

### 5. External sources
- **Triangulation (Google Trends and similar)**: the strongest upgrade. If
  two independent sources agree on a trend, confidence rises for a
  defensible reason; if they disagree, lower it and say why. Caveat:
  Google Trends has no stable official API (unofficial libraries get
  throttled; the official API is in alpha) - implement as a separate
  source module with its own caching and failure handling.
- **App popularity per country/language**: app store top charts (Apple
  RSS charts free; AppTweak / Sensor Tower paid) - the most direct signal
  for app decisions, e.g. which language-learning apps are climbing where.
- **Market status (supply side)**: number of startups/competitors on a
  topic (Crunchbase, Dealroom, Product Hunt). This is a different layer
  than demand and mostly paid data - better as a separate skill; this
  skill would then combine both into an **opportunity score** (growing
  demand x low competition), always shown with its inputs.

### 6. Scale and performance
- **Parallel fetches with a shared rate limiter** (a small thread pool
  plus a politeness delay) - wall-clock time drops roughly linearly with
  the number of languages, without triggering 429s.
- **Partial-range cache merging**: store monthly/daily granules and
  compose any range from them, so extending a range fetches only the new
  slice.
- **SQLite/DuckDB cache** instead of flat JSON files once it holds enough
  studies to be worth indexing.
- **Wikimedia pageview dumps** for bulk work (thousands of articles or a
  full-edition scan): the REST API is the wrong tool at that scale and
  hits rate limits.

### 7. Reports and monitoring
- **Summarize large studies**: top-N chart + "N more in the appendix /
  JSON" so a 20-language study still fits in 3 pages.
- **Scheduled re-runs** of a saved study with an alert when a trend's
  direction or confidence changes; a **cross-study report** ("this
  quarter vs last").
- Known rendering bugs: CJK/Hangul titles render as boxes (bundle a
  Noto CJK font or fall back to the language code); the chart's inner
  title overlaps its subtitle; long confidence reasons can overflow the
  table cell.

### 8. Verification / CI
- The existing `tests/` cover stats/resolve/cache with synthetic data.
  Add **golden query cases** taken from real sessions, e.g.
  "intermittent fasting, pl" -> resolution `none`; Turkish AI article
  Aug 2026 -> `spike_sensitive: true`; 6-language report -> 2 pages, no
  overflow; oversized recommendation -> exit 3, JSON kept.
- **Golden-PDF regression**: render from a fixed dataset and diff the text
  layer/pixels against a reference, so layout regressions are caught by
  CI rather than by eye.
- **Live-API smoke test** on a schedule (not every commit): one real
  `study` and one `rising` end-to-end.
- **Agent-level eval**: run the example queries through a cheap
  tool-using model to verify that SKILL.md's instructions - not just the
  code - are enough to drive the skill correctly. This is the most
  important step before treating the skill as validated end-to-end.
