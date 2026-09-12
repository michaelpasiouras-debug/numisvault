import unittest
from unittest.mock import patch
import numisvault_backend as b

class CanadaIdentityTests(unittest.TestCase):
    def coin(self, theme='Davis', year='1987'):
        return dict(country='Canada',denom='1 dollar',year=year,theme=theme)

    def test_country_not_inferred_as_us(self):
        for country in ('Canada','cansda','Kanada'):
            result=b.resolve_coin_identity(f'{country} 1 dollar 1987 Davis')
            self.assertEqual(result['best']['country_code'],'CA')
            self.assertEqual(result['status'],'resolved')

    def test_requested_issue_is_required(self):
        p={'coin':self.coin(),'raw_query':'Canada 1 dollar 1987 Davis'}
        self.assertTrue(b.passes_hard_filter('Canada 1 Dollar 1987 Davis Strait Silver Proof',p))
        self.assertFalse(b.passes_hard_filter('Canada 1 Dollar 1987 Loonie',p))
        p['coin']['theme']='Griffon'
        self.assertFalse(b.passes_hard_filter('Canada 1 Dollar 1987 Davis Strait Silver Proof',p))
        self.assertFalse(b.passes_hard_filter('Canada 1 Dollar 1979 Griffon Silver Proof',p))

    def test_typo_country_removed_from_theme(self):
        p={'coin':dict(country='Canada',denom='1 dollar',year='1987'),'raw_query':'cansda 1 dollar 1987 Griffon'}
        b.make_queries(p)
        self.assertEqual(p['coin']['theme'],'griffon')

    def test_spec_cache_is_separate_per_issue(self):
        self.assertNotEqual(b._coin_identity_key(self.coin()),b._coin_identity_key(self.coin('Griffon')))

    def test_lookup_keeps_raw_theme_before_database_and_fallback(self):
        coin=dict(countryEN='Canada',denom='1 dollar',year='1987')
        with patch.object(b,'pg_coin_spec_match',return_value=None) as pg, patch.object(b,'local_coin_spec_match',return_value=None), patch.object(b,'mashops_spec_fallback',return_value=None) as ma, patch.object(b,'numista_search',return_value=(None,'offline')) as numista:
            response=b.app.test_client().post('/api/coin-lookup',json={'coin':coin,'raw_query':'Canada 1 dollar 1987 Davis'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(pg.call_args.args[0]['theme'],'davis')
        self.assertEqual(ma.call_args.args[0]['theme'],'davis')
        self.assertIn('davis',numista.call_args.args[0].lower())

    def test_generic_local_specs_cannot_satisfy_named_issue(self):
        row=dict(countries=['Canada'],denomination=1,year_from=1987,year_to=1987,composition='Nickel',weight_g=7)
        with patch.object(b,'_COIN_SPECS',[row]):
            self.assertIsNone(b.local_coin_spec_match(self.coin()))

if __name__=='__main__': unittest.main()
