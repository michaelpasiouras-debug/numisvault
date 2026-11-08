"""User selection of the correct catalogue issue when several remain valid.

Contract:
- An ambiguous lookup returns every validated candidate with its OWN specs
  (weight, diameter, composition, images, years) — never merged.
- A confirmed selection loads specs only for that Numista type, skips the
  search/local/dealer paths, and is still validated against the identity.
- A selection that contradicts the identity (other issuer) is rejected.
"""
import unittest
from unittest.mock import patch
import numisvault_backend as b

TYPES = {
    101: {"id": 101, "title": "2 Euros (Erasmus Programme)", "issuer": {"name": "Greece"},
          "value": {"text": "2 Euros"}, "min_year": 2022, "max_year": 2022,
          "composition": {"text": "Bimetallic: nickel brass clad nickel centre in copper-nickel ring"},
          "weight": 8.5, "size": 25.75,
          "obverse": {"picture": "https://img/101o.jpg"}, "reverse": {"picture": "https://img/101r.jpg"}},
    102: {"id": 102, "title": "2 Euros (Standard circulation)", "issuer": {"name": "Greece"},
          "value": {"text": "2 Euros"}, "min_year": 2007, "max_year": 2026,
          "composition": {"text": "Bimetallic"}, "weight": 8.5, "size": 25.75},
    201: {"id": 201, "title": "2 Euros (Erasmus Programme)", "issuer": {"name": "Cyprus"},
          "value": {"text": "2 Euros"}, "min_year": 2022, "max_year": 2022,
          "composition": {"text": "Bimetallic"}, "weight": 8.5, "size": 25.75},
}
ISSUES = {101: [{"year": 2022}], 102: [{"year": 2022}], 201: [{"year": 2022}]}
COIN = {"country": "Greece", "countryEN": "Greece", "denom": "2 euro", "year": 2022}


def _get_type(tid):
    return TYPES.get(int(tid)), None


def _get_issues(tid):
    return ISSUES.get(int(tid), []), None


class SpecIssueSelection(unittest.TestCase):
    def _patches(self):
        return [
            patch.object(b, "pg_coin_spec_match", return_value=None),
            patch.object(b, "local_coin_spec_match", return_value=None),
            patch.object(b, "mashops_spec_fallback", return_value=None),
            patch.object(b, "persist_mashops_spec", return_value=False),
            patch.object(b, "numista_get_type", side_effect=_get_type),
            patch.object(b, "numista_get_issues", side_effect=_get_issues),
            patch.object(b, "_theme_issue_gate", return_value=True),
        ]

    def _post(self, payload, search_results=None):
        ps = self._patches()
        search = patch.object(b, "numista_search",
                              return_value=(search_results or [], None))
        for p in ps:
            p.start()
        num = search.start()
        try:
            r = b.app.test_client().post("/api/coin-lookup", json=payload)
        finally:
            search.stop()
            for p in ps:
                p.stop()
        return r, num

    def test_ambiguous_candidates_carry_their_own_specs(self):
        results = [{"id": 101, "title": "2 Euros (Erasmus Programme)", "issuer": {"name": "Greece"}},
                   {"id": 102, "title": "2 Euros (Standard circulation)", "issuer": {"name": "Greece"}}]
        r, _ = self._post({"coin": COIN, "raw_query": "Greece 2 euro 2022"}, results)
        self.assertEqual(r.status_code, 200)
        data = r.json
        self.assertTrue(data["ambiguous"])
        self.assertTrue(data["selection_required"])
        self.assertIsNone(data["match"])
        by_id = {c["id"]: c for c in data["candidates"]}
        self.assertEqual(set(by_id), {101, 102})
        self.assertEqual(by_id[101]["weight_g"], 8.5)
        self.assertEqual(by_id[101]["diameter_mm"], 25.75)
        self.assertIn("nickel", by_id[101]["composition"].lower())
        self.assertEqual(by_id[101]["obverse_image"], "https://img/101o.jpg")
        self.assertEqual((by_id[101]["min_year"], by_id[101]["max_year"]), (2022, 2022))
        self.assertEqual(by_id[101]["missing_fields"], [])
        # Specs are not borrowed across candidates.
        self.assertIsNone(by_id[102]["obverse_image"])

    def test_confirmed_selection_loads_only_that_issue(self):
        r, num = self._post({"coin": COIN, "raw_query": "Greece 2 euro 2022",
                             "selected_numista_id": 101})
        self.assertEqual(r.status_code, 200)
        m = r.json["match"]
        self.assertEqual(m["id"], 101)
        self.assertTrue(m["user_selected"])
        self.assertEqual(m["match_class"], "USER_SELECTED")
        self.assertEqual(m["weight_g"], 8.5)
        num.assert_not_called()

    def test_selection_conflicting_with_identity_is_rejected(self):
        r, _ = self._post({"coin": COIN, "selected_numista_id": "201"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json["match"])
        self.assertTrue(r.json["selection_rejected"])
        self.assertIn("WRONG_ISSUER", r.json["rejected"][0]["reasons"])

    def test_invalid_selection_id(self):
        r, _ = self._post({"coin": COIN, "selected_numista_id": "abc"})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(r.json["match"])

    def test_ui_offers_confirmed_selection_in_both_tools(self):
        with open("index.html", encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("function renderSpecCandidatePicker", html)
        self.assertIn("selected_numista_id", html)
        self.assertIn('id="auctionSpecPicker"', html)
        # Price Research and Auction Intelligence both use the shared picker.
        self.assertGreaterEqual(html.count("renderSpecCandidatePicker("), 3)


if __name__ == "__main__":
    unittest.main()
