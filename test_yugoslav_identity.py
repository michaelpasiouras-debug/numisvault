"""Regression for the Yugoslavia query blocked as modern Serbia."""
import unittest
from coin_identity_resolver import resolve_coin_identity
from multilingual_country_aliases import normalize_country_aliases_in_text

class YugoslavIdentityTests(unittest.TestCase):
    def test_reported_query(self):
        result = resolve_coin_identity("Jugoslawien 10 Dinara 1931 Silber Aleksandar I.")
        self.assertEqual(result["status"], "resolved")
        best = result["best"]
        self.assertEqual((best["country_code"], best["currency_code"], best["year"],
                          best["denomination_value"]), ("YU", "YUD", 1931, 10))
        self.assertFalse(result["ambiguous"])
        self.assertTrue(any("Yugoslavia" in q and "1931" in q for q in best["search_variants"]))
        self.assertNotIn("weight_g", best)
        self.assertNotIn("fineness_per_mille", best)

    def test_country_aliases(self):
        for name in ("Yugoslavia", "Jugoslawien", "Jugoslavija", "Yougoslavie",
                     "Γιουγκοσλαβία", "Југославија"):
            with self.subTest(name=name):
                result = resolve_coin_identity(f"{name} 10 dinara 1931")
                self.assertEqual(result["status"], "resolved")
                self.assertEqual(result["best"]["country_code"], "YU")

    def test_shared_marketplace_normalization(self):
        for name in ("Jugoslawien", "Jugoslavija", "Γιουγκοσλαβία"):
            self.assertEqual(normalize_country_aliases_in_text(
                f"{name} 10 Dinara 1931 Silber Aleksandar I."),
                "yugoslavia 10 Dinara 1931 Silber Aleksandar I.")

    def test_modern_serbia_unchanged(self):
        for name in ("Serbia", "Srbija", "Serbien"):
            with self.subTest(name=name):
                result = resolve_coin_identity(f"{name} 10 dinara 2010")
                self.assertEqual(result["status"], "resolved")
                self.assertEqual(result["best"]["country_code"], "RS")
                self.assertEqual(result["best"]["currency_code"], "RSD")

    def test_interwar_period_not_extended_to_modern_coins(self):
        result = resolve_coin_identity("Yugoslavia 10 dinara 2010")
        self.assertNotEqual(result["status"], "resolved")

    def test_existing_canadian_identity(self):
        result = resolve_coin_identity("Canada 1 dollar 1987 Davis")
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["best"]["country_code"], "CA")

if __name__ == "__main__":
    unittest.main()
