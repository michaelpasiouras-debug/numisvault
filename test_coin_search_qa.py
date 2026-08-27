#!/usr/bin/env python3
"""CoinBids Coin Search QA benchmark.

Deterministic/offline identity tests. No Numista quota is consumed.
Every confirmed production bug should become a permanent case here.
"""
import sys
from coin_identity_resolver import resolve_coin_identity

# query, expected country, denomination, year
CASES = [
    # Greece + multilingual / transliteration / typo coverage
    ("Greece 10 euro 2022 mechanism", "Greece", 10.0, 2022),
    ("Ελλάδα 10 euro 2022 mechanism", "Greece", 10.0, 2022),
    ("Griechenland 10 euro 2022 mechanism", "Greece", 10.0, 2022),
    ("Griekenland 10 euro 2022 mechanism", "Greece", 10.0, 2022),
    ("Grecia 10 euro 2022 mechanism", "Greece", 10.0, 2022),
    ("Grece 10 euro 2022 mechanism", "Greece", 10.0, 2022),
    ("Greece 5 drachma 1901", "Greece", 5.0, 1901),
    ("Ελλάδα 5 δραχμές 1901", "Greece", 5.0, 1901),
    ("Greek 5 drachmai 1976", "Greece", 5.0, 1976),
    ("GR 20 drachma 1973", "Greece", 20.0, 1973),

    # Europe
    ("Austria 5 schilling 1968", "Austria", 5.0, 1968),
    ("Österreich 5 schilling 1968", "Austria", 5.0, 1968),
    ("Netherlands 1 gulden 1967", "Netherlands", 1.0, 1967),
    ("Nederland 1 gulden 1967", "Netherlands", 1.0, 1967),
    ("Latvia 2 lats 1999", "Latvia", 2.0, 1999),
    ("United Kingdom 50 pence 1997", "United Kingdom", 0.5, 1997),
    ("UK 1 pound 2017", "United Kingdom", 1.0, 2017),
    ("Great Britain 1 pound 2017", "United Kingdom", 1.0, 2017),
    ("Switzerland 5 francs 1969", "Switzerland", 5.0, 1969),
    ("Schweiz 5 francs 1969", "Switzerland", 5.0, 1969),
    ("Suisse 5 francs 1969", "Switzerland", 5.0, 1969),
    ("Serbia 1 dinar 2009", "Serbia", 1.0, 2009),
    ("Srbija 2 dinara 2010", "Serbia", 2.0, 2010),
    ("Србија 5 dinara 2012", "Serbia", 5.0, 2012),
    ("Albania 1 lek 2008", "Albania", 1.0, 2008),
    ("Shqiperi 1 lek 1996", "Albania", 1.0, 1996),
    ("Italy 500 lire 1982", "Italy", 500.0, 1982),
    ("Italia 500 lire 1982", "Italy", 500.0, 1982),
    ("France 10 francs 1988", "France", 10.0, 1988),
    ("España 100 pesetas 1992", "Spain", 100.0, 1992),
    ("Spain 100 pesetas 1992", "Spain", 100.0, 1992),
    ("Portugal 100 escudos 1990", "Portugal", 100.0, 1990),
    ("Germany 5 mark 1975", "Germany", 5.0, 1975),
    ("Deutschland 5 mark 1975", "Germany", 5.0, 1975),
    ("Belgium 5 francs 1986", "Belgium", 5.0, 1986),
    ("Belgique 5 francs 1986", "Belgium", 5.0, 1986),
    ("Ireland 1 pound 1990", "Ireland", 1.0, 1990),
    ("Finland 10 markkaa 1995", "Finland", 10.0, 1995),

    # Fractions / symbols / common world cases
    ("½ Rappen 1850 Switzerland", "Switzerland", 0.5, 1850),
    ("USA 1 dollar 1987 silver eagle", "United States", 1.0, 1987),
    ("United States 1 dollar 1987", "United States", 1.0, 1987),
    ("¼ Dollar 1990 USA", "United States", 0.25, 1990),
    ("US quarter 1990", "United States", 0.25, 1990),
]

# Equivalent queries must resolve to the same core identity.
EQUIVALENCE_GROUPS = [
    ["Greece 10 euro 2022 mechanism", "Ελλάδα 10 euro 2022 mechanism", "Griechenland 10 euro 2022 mechanism", "Griekenland 10 euro 2022 mechanism", "Grecia 10 euro 2022 mechanism"],
    ["United Kingdom 1 pound 2017", "UK 1 pound 2017", "Great Britain 1 pound 2017"],
    ["Austria 5 schilling 1968", "Österreich 5 schilling 1968"],
    ["Germany 5 mark 1975", "Deutschland 5 mark 1975"],
]

def core(result):
    b = (result or {}).get("best") or {}
    return b.get("country"), b.get("denomination_value"), b.get("year")

def main():
    failures=[]
    passed=0
    print("COINBIDS COIN SEARCH QA")
    print("="*72)
    for q,c,d,y in CASES:
        try:
            r=resolve_coin_identity(q)
            got=core(r)
            ok=(got[0]==c and got[1] is not None and abs(float(got[1])-d)<1e-9 and got[2]==y)
        except Exception as e:
            ok=False; got=f"EXCEPTION {type(e).__name__}: {e}"
        if ok: passed+=1
        else: failures.append((q,(c,d,y),got))
        print(f"{'PASS' if ok else 'FAIL':4} | {q} | got={got}")

    eq_pass=0
    for group in EQUIVALENCE_GROUPS:
        vals=[]
        try: vals=[core(resolve_coin_identity(q)) for q in group]
        except Exception as e: vals=[f"EXCEPTION {e}"]
        ok=len(vals)==len(group) and len(set(map(str,vals)))==1
        if ok: eq_pass+=1
        else: failures.append(("EQUIVALENCE: "+" <> ".join(group),"same identity",vals))
        print(f"{'PASS' if ok else 'FAIL':4} | multilingual equivalence | {vals}")

    total=len(CASES)+len(EQUIVALENCE_GROUPS)
    print("="*72)
    print(f"RESULT: {passed+eq_pass}/{total} PASS; {len(failures)} FAIL")
    if failures:
        print("\nFAILURES")
        for q,exp,got in failures:
            print(f"- {q}\n  expected={exp}\n  got={got}")
        return 1
    return 0

if __name__=='__main__':
    sys.exit(main())
