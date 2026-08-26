#!/usr/bin/env python3
"""End-to-end multilingual hard-filter regressions for Price Research/Auction market listings."""
import importlib

b=importlib.import_module('numisvault_backend')

CASES=[
 # user/raw query language, resolved canonical target, marketplace title language, expected
 ('Griekenland 10 euro 2022 antikythera mechanism', {'country':'Greece','denom':'10 euro','year':'2022','theme':'mechanism'}, "10 euro 2022 Griekenland 'Ancient Greek Technology' zilver", True),
 ('Griechenland 10 Euro 2022 Antikythera Mechanismus', {'country':'Greece','denom':'10 euro','year':'2022','theme':'mechanism'}, 'Griechenland 10 Euro 2022 Antikythera-Mechanismus Silber PP', True),
 ('Grecia 10 euro 2022 meccanismo anticitera', {'country':'Greece','denom':'10 euro','year':'2022','theme':'mechanism'}, 'Grecia 10 Euro 2022 Meccanismo di Anticitera Argento Proof', True),
 ('Grèce 10 euro 2022 mecanisme anticythere', {'country':'Greece','denom':'10 euro','year':'2022','theme':'mechanism'}, "Grèce 10 Euro 2022 Mécanisme d'Anticythère Argent", True),
 ('Ελλάδα 10 Ευρώ 2022 μηχανισμός αντικυθήρων', {'country':'Greece','denom':'10 euro','year':'2022','theme':'mechanism'}, 'Ελλάδα 10 Ευρώ 2022 Μηχανισμός των Αντικυθήρων Ασήμι', True),
 # wrong denomination and wrong issue must remain rejected even with multilingual aliases
 ('Griekenland 10 euro 2022 antikythera mechanism', {'country':'Greece','denom':'10 euro','year':'2022','theme':'mechanism'}, 'Griekenland 2 Euro 2022 Erasmus UNC', False),
 ('Grecia 10 euro 2022 antikythera mechanism', {'country':'Greece','denom':'10 euro','year':'2022','theme':'mechanism'}, "Grecia 10 Euro 2022 Lord Byron argento Proof", False),
 # Greek-script drachma denomination must work on listings too.
 ('Ελλάδα 5 δραχμές 1901', {'country':'Greece','denom':'5 drachma','year':'1901','theme':''}, 'Ελλάδα 5 Δραχμές 1901 Ασημένιο νόμισμα', True),
]

def run():
    bad=[]
    for raw,coin,title,expected in CASES:
        got=b.passes_hard_filter(title,{'coin':coin,'raw_query':raw})
        if got!=expected: bad.append((raw,title,expected,got))
        print(('OK  ' if got==expected else 'FAIL'), repr(raw), '::', repr(title), '->',got)
    if bad:
        print('\nFAILURES')
        for x in bad: print(x)
        return 1
    print(f'\n{len(CASES)}/{len(CASES)} multilingual market matching cases passed')
    return 0

if __name__=='__main__': raise SystemExit(run())
