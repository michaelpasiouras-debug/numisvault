import unittest

from price_research_providers import (
    CallableProvider,
    ProviderRegistry,
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


if __name__ == "__main__":
    unittest.main()
