"""Cross-cutting regressions: catalogue gaps, unavailable sources, DB-only lookup."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import numisvault_backend as b
from coin_identity_resolver import CoinIdentityResolver, DB_PATH
from price_research_providers import CallableProvider

class ResearchContracts(unittest.TestCase):
    def test_country_language_does_not_change_market_query_budget(self):
        for country,aliases,description in [
            ('Greece',['Greece','Griechenland','Griekenland','Ελλάδα'],'2 euro 2022 Erasmus'),
            ('Germany',['Germany','Deutschland','Allemagne'],'5 mark 1935 Hindenburg'),
            ('Yugoslavia',['Yugoslavia','Jugoslawien','Yougoslavie'],'10 dinara 1931 Aleksandar'),
        ]:
            queries=[]
            for alias in aliases:
                raw=f'{alias} {description}'
                queries.append(b.make_queries({'raw_query':raw,'coin':{'raw':raw}}))
            with self.subTest(country=country):
                for result in queries:
                    self.assertEqual(result,queries[0])
                    self.assertEqual(result[0].lower(),f'{country} {description}'.lower().replace('dinara','dinar'))

    def test_issue_translations_keep_same_queries_and_qualifiers(self):
        from test_live_market_qa import CASES
        for case in CASES:
            queries=[b.make_queries({'raw_query':q,'coin':{'raw':q}}) for q in [case['query']]+case['aliases']]
            self.assertTrue(all(q==queries[0] for q in queries),queries)
        groups=[
            ['Greece 2 euro 2022 Erasmus','Ελλάδα 2 ευρώ 2022 Erasmus','Griekenland 2 EUR 2022 Erasmus'],
            ['Greece 10 euro 2022 antikythera mechanism','Grecia 10 euro 2022 meccanismo di anticitera',"Grèce 10 euro 2022 mécanisme d’anticythère".replace('’',"'")],
        ]
        for group in groups:
            results=[b.make_queries({'raw_query':q,'coin':{'raw':q}}) for q in group]
            for result in results:
                self.assertEqual(result,results[0])
                self.assertLessEqual(len(result),5)
        q='Greece 10 euro 2022 antikythera mechanism KM#123 Proof A'
        result=b.make_queries({'raw_query':q,'coin':{'raw':q}})
        self.assertIn('km#123 proof a',result[0].lower())
        q='Greece 2 euro 2022 Erasmus'
        result=b.make_queries({'raw_query':q,'coin':{'raw':q}})
        self.assertIn('griekenland 2 euro 2022 erasmus',result)

    def test_missing_country_never_becomes_another_currency_holder(self):
        for name, query in [('Greece','Greece 2 euro 2022'),
                            ('Belgium','Belgium 5 francs 1930'),
                            ('Yugoslavia','Jugoslawien 10 dinara 1931')]:
            with self.subTest(country=name), tempfile.TemporaryDirectory() as d:
                db=json.loads(DB_PATH.read_text())
                db['countries']=[c for c in db['countries'] if c['name']!=name]
                p=Path(d)/'db.json';p.write_text(json.dumps(db))
                result=CoinIdentityResolver(p).resolve(query)
                self.assertTrue(result['catalogue_coverage_gap'])
                self.assertEqual(result['input_country'],name)
                self.assertIsNone(result['best'])
                self.assertEqual(result['status'],'unresolved')

    def test_confirmation_page_is_not_empty_market(self):
        response=Mock(status_code=200,url='https://www.ma-shops.com/shops/search.php',
                      text='<html><title>Filtered list - MA-Shops</title><p>You opened a filtered list directly. Please confirm that you would like to see the results.</p><a>Show results</a></html>')
        with patch.object(b.SESSION,'get',return_value=response), patch.object(b,'extract_cards') as parser:
            result=CallableProvider('MA-Shops',b.fetch_search).search('arbitrary coin',{})
        self.assertEqual(result.availability,'unavailable')
        self.assertEqual(result.offers,[])
        parser.assert_not_called()

    def test_public_search_avoids_confirmation_round_trip(self):
        response=Mock(status_code=200,url=b.ma_urls('Canada Davis')[0],text='<html>results</html>')
        offer={'title':'coin','price':10,'currency':'EUR','url':'https://www.ma-shops.com/dealer/item.php?id=1'}
        with patch.object(b.SESSION,'get',return_value=response) as get, patch.object(b,'extract_cards',return_value=[offer]), patch.object(b,'extract_from_jsonld',return_value=[]):
            offers,used,error=b.fetch_search('Canada Davis',{})
        self.assertEqual(offers,[dict(offer,dealer_source='MA-Shops')])
        self.assertIsNone(error)
        get.assert_called_once()
        self.assertNotIn('sortby',used)

    def test_sorted_confirmation_uses_public_search_once(self):
        confirmation=Mock(status_code=200,url='https://www.ma-shops.com/shops/search.php?sortby=preis_eur',
                          text='You opened a filtered list directly.')
        results=Mock(status_code=200,url='https://www.ma-shops.com/shops/search.php?searchstr=test',text='<html>listings</html>')
        offer={'title':'test coin','price':10,'currency':'EUR','url':'https://www.ma-shops.com/dealer/item.php?id=1'}
        with patch.object(b,'ma_urls',return_value=[confirmation.url]), patch.object(b.SESSION,'get',side_effect=[confirmation,results]) as get, patch.object(b,'extract_cards',return_value=[offer]), patch.object(b,'extract_from_jsonld',return_value=[]):
            offers,used,error=b.fetch_search('test',{})
        self.assertEqual(len(offers),1)
        self.assertIsNone(error)
        self.assertNotIn('sortby',used)
        self.assertEqual(get.call_count,2)
        self.assertNotIn('sortby',get.call_args.args[0])

    def test_transliterated_names_keep_other_identity_constraints(self):
        self.assertTrue(b.theme_word_matches_title('Aleksandar', 'Alexander I'))
        self.assertFalse(b.theme_word_matches_title('Griffon', 'Davis Strait'))
        p={'coin':{'country':'Yugoslavia','denom':'10 dinara','year':1931,'theme':'Aleksandar'},
           'raw_query':'Yugoslavia 10 dinara 1931 Aleksandar'}
        self.assertTrue(b.passes_hard_filter('Yugoslavia 10 Dinara 1931 Alexander I',p))
        self.assertFalse(b.passes_hard_filter('Yugoslavia 20 Dinara 1931 Alexander I',p))
        self.assertFalse(b.passes_hard_filter('Yugoslavia 10 Dinara 1938 Peter II',p))

    def test_fast_lookup_does_not_trigger_external_requests_or_writes(self):
        with patch.object(b,'pg_coin_spec_match',return_value=None), patch.object(b,'local_coin_spec_match',return_value=None), patch.object(b,'mashops_spec_fallback') as ma, patch.object(b,'numista_search') as num, patch.object(b,'persist_mashops_spec') as write:
            response=b.app.test_client().post('/api/coin-lookup',json={
                'coin':{'country':'Greece','denom':'5 drachma','year':1901},'fast':True})
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.json['match'])
        ma.assert_not_called();num.assert_not_called();write.assert_not_called()

if __name__=='__main__':unittest.main()
