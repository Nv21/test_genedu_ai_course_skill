#!/usr/bin/env python3
"""CLI entry point for the wikipedia-trend-insights skill.

Commands:
    doctor    Check that dependencies are installed and the Wikimedia API
              is reachable. Run this first if anything else fails.
    resolve   Resolve a topic to a per-language article title (no fetch).
              Useful to sanity-check an ambiguous topic before spending a
              full `study` call on it.
    study     Full pipeline: resolve -> fetch -> analyze -> chart -> PDF.
              This is the command to reach for by default.
    rising    Discover the fastest-growing topics (year over year) in a
              language edition or country - for "what is trending" questions
              where the user has not named a topic.

Run `python3 wiki_trends.py <command> --help` for each command's options.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "src"))

try:
    from wiki_trends.pipeline import parse_articles_arg, run_study
    from wiki_trends.resolve import resolve_topic
    from wiki_trends.api import WikimediaClient
except ImportError as e:
    sys.stderr.write(
        "\n[wiki_trends] Missing dependency: {err}\n"
        "Install requirements once with:\n"
        "  pip install -r {req}\n\n".format(
            err=e, req=SKILL_ROOT / "requirements.txt",
        )
    )
    sys.exit(2)


def cmd_doctor(args):
    import requests
    out = {"skill_root": str(SKILL_ROOT), "checks": []}
    try:
        client = WikimediaClient(cache_dir=args.cache_dir)
        r = client.session.get(
            "https://wikimedia.org/api/rest_v1/metrics/pageviews/aggregate/"
            "en.wikipedia/all-access/user/monthly/20230101/20230201",
            timeout=10,
        )
        ok = r.status_code == 200
        out["checks"].append({"name": "wikimedia_api_reachable", "ok": ok, "status": r.status_code})
    except requests.RequestException as e:
        out["checks"].append({"name": "wikimedia_api_reachable", "ok": False, "error": str(e)})
    out["all_ok"] = all(c["ok"] for c in out["checks"])
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["all_ok"] else 1


def cmd_resolve(args):
    client = WikimediaClient(cache_dir=args.cache_dir)
    resolved = resolve_topic(
        client, args.topic, args.langs.split(","), source_lang=args.source_lang,
    )
    print(json.dumps({k: v.to_dict() for k, v in resolved.items()}, ensure_ascii=False, indent=2))
    return 0


def cmd_study(args):
    try:
        manual = parse_articles_arg(args.articles)
        result = run_study(
            topic=args.topic,
            lang_codes=args.langs.split(",") if args.langs else list(manual.keys()),
            start=args.start,
            end=args.end,
            out_base=args.out,
            cache_dir=args.cache_dir,
            granularity=args.granularity,
            metric=args.metric,
            source_lang=args.source_lang,
            manual_articles=manual,
            recommendation=args.recommendation or "",
            title_override=args.title,
        )
    except ValueError as e:
        sys.stderr.write(f"[wiki_trends] {e}\n")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # 3 = analysis done (JSON/PNG written) but the PDF exceeded the page cap.
    return 3 if result["output_files"]["report_pdf"] is None else 0


def build_parser():
    p = argparse.ArgumentParser(prog="wiki_trends.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    common_cache = dict(default=str(Path.cwd() / ".wiki_trends_cache"),
                         help="Directory for the on-disk API response cache (default: ./.wiki_trends_cache)")

    d = sub.add_parser("doctor", help="Check dependencies & API reachability")
    d.add_argument("--cache-dir", **common_cache)
    d.set_defaults(func=cmd_doctor)

    r = sub.add_parser("resolve", help="Resolve a topic to per-language article titles")
    r.add_argument("--topic", required=True)
    r.add_argument("--langs", required=True, help="Comma-separated ISO language codes, e.g. pl,cs,uk")
    r.add_argument("--source-lang", default="en", help="Language to search Wikidata in (default: en)")
    r.add_argument("--cache-dir", **common_cache)
    r.set_defaults(func=cmd_resolve)

    s = sub.add_parser("study", help="Full pipeline: resolve, fetch, analyze, chart, PDF report")
    s.add_argument("--topic", help="Topic to look up, e.g. 'intermittent fasting'")
    s.add_argument("--langs", help="Comma-separated ISO language codes, e.g. pl,cs,uk")
    s.add_argument("--articles", help="Skip resolution: 'pl:Tytuł,cs:Název' explicit article titles")
    s.add_argument("--start", help="YYYY-MM or YYYY-MM-DD. Omit both --start and --end for a multi-horizon study (3 months by week, 1 year, 3 years)")
    s.add_argument("--end", help="YYYY-MM or YYYY-MM-DD")
    s.add_argument("--granularity", default="monthly", choices=["daily", "monthly"])
    s.add_argument("--metric", default="views", choices=["views", "share"],
                    help="'share' normalizes by each wiki's total traffic - fairer across languages")
    s.add_argument("--source-lang", default="en")
    s.add_argument("--out", required=True, help="Output base path (no extension) for .png/.pdf/.json")
    s.add_argument("--recommendation", default="", help="Free-text conclusion to print in the PDF")
    s.add_argument("--recommendation-file", help="Read --recommendation text from a file instead")
    s.add_argument("--title", help="Override the report title")
    s.add_argument("--cache-dir", **common_cache)
    s.set_defaults(func=cmd_study)

    rs = sub.add_parser("rising", help="Discover the fastest-growing topics (YoY) per language edition or country")
    rs.add_argument("--targets", required=True,
                    help="Comma-separated: lowercase language codes (uk,pl,en) and/or uppercase country codes (US)")
    rs.add_argument("--end", help="Last month of the window, YYYY-MM (default: last full month)")
    rs.add_argument("--window", type=int, default=3, help="Months compared year-over-year (default 3)")
    rs.add_argument("--top", type=int, default=5)
    rs.add_argument("--verify", type=int, default=40, help="Candidates re-checked with exact per-article history")
    rs.add_argument("--out", required=True, help="Output path base; writes <out>.json")
    rs.add_argument("--cache-dir", **common_cache)
    rs.set_defaults(func=cmd_rising)

    return p


def cmd_rising(args):
    from wiki_trends.rising import discover_rising, last_full_month
    from wiki_trends.dates import parse_boundary
    client = WikimediaClient(cache_dir=args.cache_dir)
    end_month = parse_boundary(args.end, is_end=False).replace(day=1) if args.end else last_full_month()
    results = {t: discover_rising(client, t, end_month=end_month, window=args.window,
                                  top_n=args.top, verify_k=args.verify)
               for t in args.targets.split(",")}
    out = {"results": results,
           "api_calls": {"requests_made": client.stats.requests_made,
                         "cache_hits": client.stats.cache_hits}}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(f"{args.out}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "recommendation_file", None):
        args.recommendation = Path(args.recommendation_file).read_text(encoding="utf-8")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
