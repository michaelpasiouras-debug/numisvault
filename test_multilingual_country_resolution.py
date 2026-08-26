#!/usr/bin/env python3
from coin_identity_resolver import resolve_coin_identity

CASES=[
('Greece 10 euro 2022 antikythera mechanism','Greece'),
('Griechenland 10 euro 2022 antikythera mechanism','Greece'),
('Griekenland 10 euro 2022 antikythera mechanism','Greece'),
('Grèce 10 euro 2022 antikythera mechanism','Greece'),
('Grecia 10 euro 2022 antikythera mechanism','Greece'),
('Grécia 10 euro 2022 antikythera mechanism','Greece'),
('Ελλάδα 10 euro 2022 antikythera mechanism','Greece'),
('Yunanistan 10 euro 2022 antikythera mechanism','Greece'),
('Grekland 10 euro 2022 antikythera mechanism','Greece'),
('Kreikka 10 euro 2022 antikythera mechanism','Greece'),
('Deutschland 5 mark 1975','Germany'),
('Duitsland 5 mark 1975','Germany'),
('Allemagne 5 mark 1975','Germany'),
('Frankreich 10 franc 1980','France'),
('Frankrijk 10 franc 1980','France'),
('Italia 500 lire 1985','Italy'),
('Niederlande 1 gulden 1967','Netherlands'),
('Paesi Bassi 1 gulden 1967','Netherlands'),
('Österreich 5 schilling 1968','Austria'),
('Suisse 5 franc 1969','Switzerland'),
('Schweiz 5 franc 1969','Switzerland'),
('Verenigd Koninkrijk 1 pound 2017','United Kingdom'),
('États-Unis quarter dollar 1964','United States'),
('Stati Uniti quarter dollar 1964','United States'),
]

def run():
    bad=[]
    for q,expected in CASES:
        out=resolve_coin_identity(q) or {}; best=out.get('best') or {}; got=best.get('country')
        if got!=expected: bad.append((q,expected,got,out.get('status')))
        else: print('OK ',q,'->',got)
    if bad:
        print('\nFAILURES')
        for row in bad: print(row)
        return 1
    print(f'\n{len(CASES)}/{len(CASES)} multilingual country cases passed')
    return 0

if __name__=='__main__': raise SystemExit(run())
