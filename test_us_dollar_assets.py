import unittest
from unittest.mock import patch
import numisvault_backend as b

class DollarTests(unittest.TestCase):
    def test_explicit_value_overrides_visual_nickname(self):
        for text in ('USA 1 dollar 1987 Silver Eagle','USA 1 dollar 2020 eagle'):
            result=b.resolve_coin_identity(text)
            self.assertEqual(result['status'],'resolved',result)
            self.assertEqual(result['best']['denomination_value'],1)
            self.assertFalse(any(c['denomination_value']==10 for c in result['candidates']))
        result=b.resolve_coin_identity('USA 10 dollar 1907 eagle')
        self.assertEqual(result['best']['denomination_value'],10)

    def test_review_identity_never_populates_market_target(self):
        review={'status':'review','best':{'country':'Serbia','year':1931,'denomination_value':10,'currency':'dinar'}}
        with patch.object(b,'resolve_coin_identity',return_value=review), \
             patch.object(b,'make_queries',return_value=[]) as queries:
            b.app.test_client().post('/api/coin-search',json={'raw_query':'unresolved test identity'})
        self.assertFalse(queries.call_args.args[0].get('coin'))

    def test_paper_and_unknown_are_not_dollar_coins(self):
        for year,title in [(1923,'1 Dollar 1923 United States of America, 1 Dollar, 1923 IV'),
                           (1923,'4,20 Goldmark : 1 Dollar USA 1923 Notgeldschein Handelskammer AU-55'),
                           (1987,'Disney Dollars Voucher 1 Dollar 1987 Sleeping Beauty Castle Mickey Mouse'),
                           (1923,'USA 1 Dollar 1923 Silver Certificate UNC'),
                           (1987,'1 dollar 1983-1987 Belize 1983-1987')]:
            p={'raw_query':f'Usa 1 dollar {year}','coin':{'country':'United States','denom':'1 dollar','year':year}}
            self.assertFalse(b.passes_hard_filter(title,p),title)
        p={'raw_query':'Usa 1 dollar 1923','coin':{'country':'United States','denom':'1 dollar','year':1923}}
        self.assertTrue(b.passes_hard_filter('USA Peace 1 Dollar 1923 Silver Coin',p))

    def test_ambiguous_modern_dollar_never_uses_generic_silver_row(self):
        c={'country':'United States','denom':'1 dollar','year':1987}
        with patch.object(b,'_get_pg_connection') as db, patch.object(b,'mashops_spec_fallback') as ma:
            self.assertIsNone(b.pg_coin_spec_match(c))
            r=b.app.test_client().post('/api/coin-lookup',json={'coin':c,'raw_query':'Usa 1 dollar 1987'})
            self.assertIsNone(r.json['match'])
            self.assertTrue(r.json['ambiguous'])
            db.assert_not_called();ma.assert_not_called()

if __name__=='__main__':unittest.main()
