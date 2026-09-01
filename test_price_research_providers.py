import unittest

from price_research_providers import (
    CallableProvider,
    ProviderResult,
    ProviderRegistry,
    consolidate_provider_statuses,
    flatten_provider_results,
)


class PriceResearchProviderTests(unittest.TestCase):
    def test_successful_provider_preserves_candidates(self):
        def fetch(query, payload):
            return ([{"title": "Greece 10 Euro 2022 Antikythera", "price": 149.75,
                      "currency": "EUR", "url": "https://example.test/item/1"}],
                    "https://example.test/search", None)

        result = CallableProvider("AuthorizedFeed", fetch).search("greece 10 euro 2022", {})
        self.assertEqual(result.availability, "ok")
        offers, used, errors, status = flatten_provider_results([result])
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["dealer_source"], "AuthorizedFeed")
        self.assertEqual(status["AuthorizedFeed"], "ok")
        self.assertFalse(errors)
        self.assertTrue(used)

    def test_waf_checkpoint_is_source_unavailable_not_no_match(self):
        def fetch(query, payload):
            return [], None, "MA-Shops human-verification checkpoint blocked automated search"

        result = CallableProvider("MA-Shops", fetch).search("greece 10 euro 2022", {})
        self.assertEqual(result.availability, "unavailable")
        self.assertEqual(result.offers, [])
        _, _, errors, status = flatten_provider_results([result])
        self.assertEqual(status["MA-Shops"], "unavailable")
        self.assertIn("human-verification", errors[0]["error"])

    def test_one_blocked_provider_does_not_hide_working_provider(self):
        blocked = CallableProvider("MA-Shops", lambda q, p: ([], None, "checkpoint blocked automated search"))
        working = CallableProvider("PartnerFeed", lambda q, p: ([{
            "title": "Greece 10 Euro 2022 Mechanism", "price": 150.0,
            "currency": "EUR", "url": "https://partner.test/1"
        }], "https://partner.test/search", None))
        registry = ProviderRegistry([blocked, working])
        results = registry.search_all("Greece 10 euro 2022 mechanism", {})
        offers, _, _, status = flatten_provider_results(results)
        self.assertEqual(status["MA-Shops"], "unavailable")
        self.assertEqual(status["PartnerFeed"], "ok")
        self.assertEqual(len(offers), 1)

    def test_duplicate_provider_name_is_rejected(self):
        registry = ProviderRegistry([CallableProvider("A", lambda q, p: ([], None, None))])
        with self.assertRaises(ValueError):
            registry.register(CallableProvider("A", lambda q, p: ([], None, None)))

    def test_multi_query_status_is_ok_when_any_query_succeeds(self):
        results = [
            ProviderResult("MA-Shops", "q1", availability="unavailable"),
            ProviderResult("MA-Shops", "q2", availability="ok"),
        ]
        self.assertEqual(consolidate_provider_statuses(results), {"MA-Shops": "ok"})

    def test_multi_query_status_is_unavailable_only_when_all_are_unavailable(self):
        all_blocked = [
            ProviderResult("MA-Shops", "q1", availability="unavailable"),
            ProviderResult("MA-Shops", "q2", availability="unavailable"),
        ]
        mixed_failure = [
            ProviderResult("MA-Shops", "q1", availability="unavailable"),
            ProviderResult("MA-Shops", "q2", availability="error"),
        ]
        self.assertEqual(consolidate_provider_statuses(all_blocked), {"MA-Shops": "unavailable"})
        self.assertEqual(consolidate_provider_statuses(mixed_failure), {"MA-Shops": "error"})

    def test_expected_but_unexecuted_provider_is_not_configured(self):
        self.assertEqual(
            consolidate_provider_statuses([], ["AuthorizedFeed"]),
            {"AuthorizedFeed": "not_configured"},
        )

    def test_flatten_does_not_mutate_provider_owned_query_provenance(self):
        source_offer = {"title": "Coin", "_source_queries": ["original"]}
        result = ProviderResult("Feed", "new", offers=[source_offer])
        offers, _, _, _ = flatten_provider_results([result])
        self.assertEqual(offers[0]["_source_queries"], ["original", "new"])
        self.assertEqual(source_offer["_source_queries"], ["original"])


if __name__ == "__main__":
    unittest.main()
