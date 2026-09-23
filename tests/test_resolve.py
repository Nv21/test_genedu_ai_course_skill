"""Unit tests for topic resolution, using a fake client (no network)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wiki_trends.resolve import resolve_topic


class FakeClient:
    """Duck-typed stand-in for WikimediaClient - returns canned responses
    instead of hitting the real Wikidata/Wikipedia APIs."""

    def __init__(self, wd_search=None, sitelinks=None, wiki_search=None):
        self._wd_search = wd_search or []
        self._sitelinks = sitelinks or {}
        self._wiki_search = wiki_search or {}

    def wikidata_search(self, query, language="en", limit=5):
        return self._wd_search

    def wikidata_sitelinks(self, qid, lang_codes):
        return {c: self._sitelinks.get(c) for c in lang_codes}

    def wiki_search(self, lang_code, query, limit=5):
        return self._wiki_search.get(lang_code, [])


class TestManualOverride(unittest.TestCase):
    def test_manual_override_is_high_confidence_and_skips_lookup(self):
        client = FakeClient()  # would return nothing for anything looked up
        result = resolve_topic(
            client, "intermittent fasting", ["pl"],
            manual_overrides={"pl": "Głodówka przerywana"},
        )
        self.assertEqual(result["pl"].title, "Głodówka przerywana")
        self.assertEqual(result["pl"].method, "manual")
        self.assertEqual(result["pl"].confidence, "high")


class TestWikidataPath(unittest.TestCase):
    def test_exact_label_match_uses_sitelinks(self):
        client = FakeClient(
            wd_search=[{"id": "Q1666254", "label": "intermittent fasting",
                        "match": {"type": "label"}}],
            sitelinks={"cs": "Přerušovaný půst", "uk": "Інтервальне голодування"},
        )
        result = resolve_topic(client, "intermittent fasting", ["cs", "uk"])
        self.assertEqual(result["cs"].title, "Přerušovaný půst")
        self.assertEqual(result["cs"].method, "wikidata")
        self.assertEqual(result["cs"].confidence, "high")
        self.assertEqual(result["uk"].title, "Інтервальне голодування")


class TestSearchFallback(unittest.TestCase):
    def test_relevant_fallback_hit_is_low_confidence_not_none(self):
        client = FakeClient(
            wd_search=[],  # no Wikidata anchor at all
            wiki_search={"pl": [{"title": "Post przerywany", "snippet": "..."}]},
        )
        result = resolve_topic(client, "post przerywany", ["pl"])
        self.assertEqual(result["pl"].title, "Post przerywany")
        self.assertEqual(result["pl"].method, "search_fallback")
        self.assertEqual(result["pl"].confidence, "low")

    def test_irrelevant_fallback_hit_is_reported_as_unresolved(self):
        """Regression test for a real failure found during manual testing:
        searching an English topic against a Polish wiki returned an
        unrelated article ('Stres oksydacyjny') that only cites the topic
        in passing. That must NOT be reported as a plausible guess."""
        client = FakeClient(
            wd_search=[],
            wiki_search={"pl": [{"title": "Stres oksydacyjny", "snippet": "..."}]},
        )
        result = resolve_topic(client, "intermittent fasting", ["pl"])
        self.assertIsNone(result["pl"].title)
        self.assertEqual(result["pl"].confidence, "none")
        self.assertTrue(len(result["pl"].candidates) >= 1)  # still surfaced for inspection

    def test_no_search_hits_at_all_is_unresolved(self):
        client = FakeClient(wd_search=[], wiki_search={})
        result = resolve_topic(client, "some obscure topic", ["xx"])
        self.assertIsNone(result["xx"].title)
        self.assertEqual(result["xx"].method, "unresolved")
        self.assertEqual(result["xx"].confidence, "none")


class TestWikidataAliasMatch(unittest.TestCase):
    def test_exact_alias_match_is_trusted_even_when_label_differs(self):
        """Regression test for a real failure found during manual testing:
        querying 'English language' hits Wikidata item Q1860 whose primary
        label is just 'English', matched via the alias 'English language'.
        Checking only `label` (and not `match.text`) left this unresolved."""
        client = FakeClient(
            wd_search=[{
                "id": "Q1860", "label": "English",
                "match": {"type": "alias", "text": "English language"},
            }],
            sitelinks={"uk": "Англійська мова"},
        )
        result = resolve_topic(client, "English language", ["uk"])
        self.assertEqual(result["uk"].title, "Англійська мова")
        self.assertEqual(result["uk"].method, "wikidata")
        self.assertEqual(result["uk"].confidence, "high")


class TestWikidataLowQualityMatchIsNotTrusted(unittest.TestCase):
    def test_unrelated_top_wikidata_hit_falls_back_to_search(self):
        # Top Wikidata hit's label doesn't match the query and isn't
        # flagged as a label match either -> should not anchor on it.
        client = FakeClient(
            wd_search=[{"id": "Q999", "label": "Something else entirely",
                        "match": {"type": "alias"}}],
            wiki_search={"pl": [{"title": "Something Polish", "snippet": "..."}]},
        )
        result = resolve_topic(client, "my topic", ["pl"])
        self.assertNotEqual(result["pl"].method, "wikidata")


if __name__ == "__main__":
    unittest.main()
