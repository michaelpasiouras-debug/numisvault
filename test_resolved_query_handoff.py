"""Exercise the real raw-only endpoint up to the external-search boundary."""
import unittest
from unittest.mock import patch
import numisvault_backend as backend


class QueryHandoffTests(unittest.TestCase):
    def test_greek_raw_endpoint_keeps_canonical_queries(self):
        original = backend.make_queries
        observed = {}

        class SearchBoundary(Exception):
            pass

        def capture(payload, **kwargs):
            observed['queries'] = original(payload, **kwargs)
            observed['payload'] = payload
            raise SearchBoundary()

        with patch.dict(backend.app.config, TESTING=True), patch.object(backend, 'make_queries', side_effect=capture):
            with backend._SEARCH_CACHE_LOCK:
                backend._SEARCH_CACHE.clear()
            with self.assertRaises(SearchBoundary):
                backend.app.test_client().post('/api/coin-search', json={
                    'raw_query':'Ελλάδα 5 δραχμές 1901',
                    'coin':{'raw':'Ελλάδα 5 δραχμές 1901'}, 'currency':'EUR'})
        self.assertIn('Greece 5 drachma 1901', observed['queries'])
        self.assertLessEqual(len(observed['queries']), 5)
        self.assertTrue(backend.passes_hard_filter('Griechenland 5 Drachmen 1901', observed['payload']))
        self.assertFalse(backend.passes_hard_filter('Griechenland 2 Drachmen 1901', observed['payload']))
        self.assertFalse(backend.passes_hard_filter('Griechenland 5 Drachmen 1876', observed['payload']))


if __name__ == '__main__':
    unittest.main()
