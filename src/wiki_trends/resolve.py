"""Resolve a free-text topic into a concrete Wikipedia article title per
language edition.

This step matters more than it looks: the same "topic" is a different
article title in every language, and not every language edition even has
an article on a given concept. Getting this step wrong silently (picking
the wrong article) would invalidate everything downstream, so every result
carries an explicit confidence level and, for low-confidence guesses, the
alternative candidates that were considered - so the calling agent can
either accept it or surface the ambiguity to the user instead of quietly
reporting on the wrong page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .api import WikimediaClient


@dataclass
class ResolvedArticle:
    lang: str
    title: Optional[str]
    method: str  # "manual" | "wikidata" | "search_fallback" | "unresolved"
    confidence: str  # "high" | "low" | "none"
    wikidata_qid: Optional[str] = None
    candidates: list[dict] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "lang": self.lang,
            "title": self.title,
            "method": self.method,
            "confidence": self.confidence,
            "wikidata_qid": self.wikidata_qid,
            "candidates": self.candidates,
            "note": self.note,
        }


def _normalize(s: str) -> str:
    return " ".join(s.lower().strip().split())


_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "for", "and", "or", "to", "za", "w",
    "na", "i", "з", "у", "та", "для", "o", "de", "la", "le",
}


def _title_overlaps_topic(title: str, topic: str) -> bool:
    """True if `title` and `topic` share at least one non-trivial word.
    A cheap but effective guard against a full-text search matching only a
    citation/reference inside an unrelated article."""
    def words(s: str) -> set[str]:
        return {
            w for w in _normalize(s).replace("(", " ").replace(")", " ").split()
            if len(w) > 2 and w not in _STOPWORDS
        }

    return bool(words(title) & words(topic))


def resolve_topic(
    client: WikimediaClient,
    topic: str,
    lang_codes: list[str],
    source_lang: str = "en",
    manual_overrides: Optional[dict[str, str]] = None,
) -> dict[str, ResolvedArticle]:
    manual_overrides = manual_overrides or {}
    results: dict[str, ResolvedArticle] = {}

    remaining = [c for c in lang_codes if c not in manual_overrides]
    for code, title in manual_overrides.items():
        results[code] = ResolvedArticle(
            lang=code, title=title, method="manual", confidence="high",
            note="Title supplied explicitly by the caller.",
        )

    if not remaining:
        return results

    # 1) Try to anchor the topic to a Wikidata item via a label search in
    #    `source_lang`, then read off per-language sitelinks. This is the
    #    right tool for "same concept, different language" - it does not
    #    rely on machine translation.
    qid = None
    wd_hits = client.wikidata_search(topic, language=source_lang, limit=5)
    if wd_hits:
        top = wd_hits[0]
        label = top.get("label", "")
        match_text = top.get("match", {}).get("text", "")
        # Only trust an automatic Wikidata anchor when the query exactly
        # matches either the item's displayed label OR the specific
        # label/alias string the search API says it matched on (`match.text`
        # - e.g. topic "English language" matches item Q1860 via the alias
        # "English language" even though its primary label is just
        # "English"; checking only `label` misses this and would wrongly
        # leave a correct match unresolved). Anything looser is left
        # unresolved rather than silently locking onto a plausible-looking
        # but wrong item.
        if _normalize(label) == _normalize(topic) or _normalize(match_text) == _normalize(topic):
            qid = top.get("id")

    sitelinks: dict[str, Optional[str]] = {}
    if qid:
        sitelinks = client.wikidata_sitelinks(qid, remaining)

    still_remaining = []
    for code in remaining:
        title = sitelinks.get(code)
        if title:
            results[code] = ResolvedArticle(
                lang=code, title=title, method="wikidata", confidence="high",
                wikidata_qid=qid,
                note="Matched via Wikidata sitelink - same concept across languages.",
            )
        else:
            still_remaining.append(code)

    # 2) Fallback: direct full-text search on the target-language Wikipedia.
    #    This is inherently less reliable (keyword match, not concept
    #    match), so it is always marked low confidence with the runner-up
    #    candidates attached for a human/agent to sanity-check.
    for code in still_remaining:
        hits = client.wiki_search(code, topic, limit=5)
        if not hits:
            results[code] = ResolvedArticle(
                lang=code, title=None, method="unresolved", confidence="none",
                note=(
                    f"No Wikidata sitelink and no search results for '{topic}' "
                    f"on {code}.wikipedia. The topic may not have (or may not "
                    f"yet have) an article in this language edition."
                ),
            )
            continue

        top_hit = hits[0]
        candidates = [
            {"title": h["title"], "snippet": _strip_html(h.get("snippet", ""))}
            for h in hits[:5]
        ]
        if _title_overlaps_topic(top_hit["title"], topic):
            results[code] = ResolvedArticle(
                lang=code,
                title=top_hit["title"],
                method="search_fallback",
                confidence="low",
                note=(
                    "No Wikidata cross-language link found; picked the top "
                    "full-text search hit, which shares words with the topic. "
                    "Verify this is the intended article before trusting the "
                    "trend for this language."
                ),
                candidates=candidates,
            )
        else:
            # The top hit shares no words with the topic at all - the query
            # was likely in the wrong language for this wiki's search index
            # (e.g. an English topic searched against a Polish wiki matches
            # only citations, not the actual concept). Reporting this as a
            # "low confidence" article would be actively misleading, so it
            # is left unresolved instead, with the weak candidates attached
            # for a human/agent to judge - or to supply the right title via
            # --articles.
            results[code] = ResolvedArticle(
                lang=code, title=None, method="search_fallback", confidence="none",
                note=(
                    f"No Wikidata sitelink, and the top full-text search hit "
                    f"('{top_hit['title']}') shares no words with the topic - "
                    f"likely a false match (query may need to be in {code}'s "
                    f"language, or this wiki may not cover the topic). Not "
                    f"reporting a guess; pass --articles {code}:<title> if you "
                    f"know the correct one."
                ),
                candidates=candidates,
            )

    return results


def _strip_html(s: str) -> str:
    out = []
    in_tag = False
    for ch in s:
        if ch == "<":
            in_tag = True
        elif ch == ">":
            in_tag = False
        elif not in_tag:
            out.append(ch)
    return "".join(out)
