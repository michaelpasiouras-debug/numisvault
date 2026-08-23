from __future__ import annotations
import json, re, unicodedata
from pathlib import Path
from difflib import SequenceMatcher

DB_PATH = Path(__file__).with_name("coin_identity_database.json")

GREEK_TO_LATIN = str.maketrans({
    "α":"a","β":"v","γ":"g","δ":"d","ε":"e","ζ":"z","η":"i","θ":"th","ι":"i",
    "κ":"k","λ":"l","μ":"m","ν":"n","ξ":"x","ο":"o","π":"p","ρ":"r","σ":"s",
    "ς":"s","τ":"t","υ":"y","φ":"f","χ":"ch","ψ":"ps","ω":"o"
})

def strip_accents(s:str)->str:
    s=unicodedata.normalize("NFKD",s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def transliterate_greek(s:str)->str:
    return strip_accents((s or "").lower()).translate(GREEK_TO_LATIN)

def norm(s:str)->str:
    # Replace fraction symbols BEFORE strip_accents(): strip_accents() applies
    # Unicode NFKD normalization, which silently decomposes "½"/"¼" into
    # "1" + U+2044 FRACTION SLASH + "2" (a different character from the
    # regular "/" this function relies on). If that runs first, the literal
    # "½"/"¼" replacements below never match (the characters are already
    # gone), and the later character-class cleanup drops the fraction slash
    # entirely — turning e.g. "½ Rappen" into "1 2 rappen", which parses as
    # denomination 1 instead of the correct 0.5.
    s=(s or "").lower().replace("¼"," 1/4 ").replace("½"," 1/2 ")
    s=strip_accents(s)
    s=s.replace("€"," euro ").replace("£"," gbp ").replace("$"," usd ").replace("¢"," cent ")
    s=re.sub(r"[^a-z0-9α-ωа-яёіїєґ/.\-\s]+"," ",s,flags=re.I)
    s=re.sub(r"\s+"," ",s).strip()
    return s

def variants(s:str):
    n=norm(s)
    t=transliterate_greek(s)
    return {n, norm(t)}

def sim(a,b):
    return SequenceMatcher(None,norm(a),norm(b)).ratio()

class CoinIdentityResolver:
    def __init__(self, db_path=DB_PATH):
        self.db=json.loads(Path(db_path).read_text(encoding="utf-8"))
        self.countries=self.db["countries"]
        self.alias_index=[]
        for c in self.countries:
            for a in [c["name"],c["code"],*c.get("aliases",[])]:
                for v in variants(a):
                    if v:self.alias_index.append(("country",v,c,None))
            for cur in c.get("currencies",[]):
                for a in [cur["name"],cur["code"],*cur.get("aliases",[])]:
                    for v in variants(a):
                        if v:self.alias_index.append(("currency",v,c,cur))
                sub=cur.get("subunit") or {}
                for a in [sub.get("name",""),*sub.get("aliases",[])]:
                    for v in variants(a):
                        if v:self.alias_index.append(("subunit",v,c,cur))
                for sd in cur.get("special_denominations",[]):
                    for a in sd.get("aliases",[]):
                        for v in variants(a):
                            if v:self.alias_index.append(("special",v,c,(cur,sd)))

    def _contains_alias(self,text,alias):
        if not alias:return False
        # exact token/phrase boundary match
        return re.search(r"(?<![a-z0-9])"+re.escape(alias)+r"(?![a-z0-9])",text,re.I) is not None

    def _best_fuzzy(self,text,kind=None,min_score=.80):
        toks=text.split()
        grams=set(toks)
        for n in (2,3):
            grams.update(" ".join(toks[i:i+n]) for i in range(max(0,len(toks)-n+1)))
        hits=[]
        for k,a,c,cur in self.alias_index:
            if kind and k!=kind:continue
            best=max((sim(g,a) for g in grams),default=0)
            if best>=min_score:
                hits.append((best,k,a,c,cur))
        hits.sort(reverse=True,key=lambda x:x[0])
        return hits

    def _parse_year(self,text):
        years=[int(x) for x in re.findall(r"(?<!\d)(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})(?!\d)",text)]
        return years[-1] if years else None

    def _parse_number(self,text):
        # fractions first
        frac=re.search(r"(?<!\d)(1/2|1/4|2\s*1/2)(?!\d)",text)
        if frac:
            return {"1/2":.5,"1/4":.25,"2 1/2":2.5,"2 1/2":2.5}.get(re.sub(r"\s+"," ",frac.group(1)))
        # Avoid taking a four-digit year as denomination
        nums=[]
        for m in re.finditer(r"(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)",text):
            v=m.group(1).replace(",",".")
            try:f=float(v)
            except:continue
            if f>=1000 and f<=2199:continue
            nums.append(f)
        return nums[0] if nums else None

    def resolve(self,raw:str):
        original=raw or ""
        text=norm(original)
        latin=norm(transliterate_greek(original))
        texts={text,latin}
        year=self._parse_year(text)
        denomination=self._parse_number(text)

        # When the user explicitly typed a numeric face value together with a
        # currency (e.g. "1 dollar ... silver eagle"), that explicit value is
        # stronger than a conflicting historical nickname buried in a longer
        # phrase ("eagle" historically means $10).  The nickname is still used
        # when no explicit numeric denomination was supplied.
        explicit_numeric_denomination = denomination

        country_scores={}
        currency_scores={}
        special_hits=[]

        # Exact phrase matches: strongest signal
        for kind,a,c,cur in self.alias_index:
            matched=any(self._contains_alias(t,a) for t in texts)
            if not matched:continue
            if kind=="country":
                country_scores[c["code"]]=max(country_scores.get(c["code"],0),1.0)
            elif kind=="currency":
                key=(c["code"],cur["code"])
                currency_scores[key]=max(currency_scores.get(key,0),1.0)
            elif kind=="special":
                currency,sd=cur
                special_hits.append((1.0,c,currency,sd,a))
            elif kind=="subunit":
                key=(c["code"],cur["code"])
                currency_scores[key]=max(currency_scores.get(key,0),.92)

        # Fuzzy fallback only when exact matching was insufficient
        if not country_scores:
            for score,kind,a,c,cur in self._best_fuzzy(latin,"country",.84)[:5]:
                country_scores[c["code"]]=max(country_scores.get(c["code"],0),score*.86)
        if not currency_scores and not special_hits:
            for score,kind,a,c,cur in self._best_fuzzy(latin,"currency",.80)[:8]:
                currency_scores[(c["code"],cur["code"])]=max(currency_scores.get((c["code"],cur["code"]),0),score*.88)
            for score,kind,a,c,cur in self._best_fuzzy(latin,"special",.82)[:5]:
                currency,sd=cur
                special_hits.append((score*.90,c,currency,sd,a))

        # If the user explicitly named a country (exact/high-confidence match),
        # do not let the same generic currency/subunit in other countries create
        # artificial ambiguity (e.g. "2 euro Croatia 2025", "25 cents USA 1964").
        explicit_countries={cc for cc,score in country_scores.items() if score>=.95}
        if explicit_countries:
            currency_scores={k:v for k,v in currency_scores.items() if k[0] in explicit_countries}
            special_hits=[x for x in special_hits if x[1]["code"] in explicit_countries]

        # General conflict rule: an explicit numeric denomination wins over a
        # contradictory denomination nickname when both point to the same
        # currency.  This fixes "1 Dollar ... Silver Eagle" without hardcoding
        # Silver Eagle itself and also protects future phrases containing
        # quarter/half/double-eagle style words.
        if explicit_numeric_denomination is not None and currency_scores:
            explicit_currency_codes={curcode for (_cc,curcode) in currency_scores}
            kept=[]
            for hit in special_hits:
                _score,_c,_currency,_sd,_alias=hit
                if _currency.get("code") in explicit_currency_codes:
                    try:
                        if abs(float(_sd.get("value"))-float(explicit_numeric_denomination))>1e-9:
                            continue
                    except Exception:
                        pass
                kept.append(hit)
            special_hits=kept

        candidates=[]

        # Special denomination nicknames (quarter, dime, sovereign...)
        for score,c,currency,sd,a in special_hits:
            sc=.62 + .20*score
            if c["code"] in country_scores:sc+=.16
            if year:
                vf=currency.get("valid_from"); vt=currency.get("valid_to")
                if vf and year<vf:sc-=.22
                if vt and year>vt:sc-=.22
                if (not vf or year>=vf) and (not vt or year<=vt):sc+=.06
            candidates.append(self._candidate(c,currency,sd.get("value"),year,sc,
                ["special denomination alias: "+a]))

        # Currency candidates
        for (cc,curcode),cs in currency_scores.items():
            c=next(c for c in self.countries if c["code"]==cc)
            currency=next(x for x in c["currencies"] if x["code"]==curcode)
            candidate_denom=denomination
            # Canonicalize cent-denominated inputs for EUR/USD so "25 cents"
            # becomes 0.25 of the major currency unit rather than 25 dollars/euros.
            if candidate_denom is not None and curcode in ("USD","EUR") and re.search(r"(?<![a-z])cents?(?![a-z])",text,re.I):
                candidate_denom=candidate_denom/100.0
            sc=.52 + .28*cs
            reasons=["currency match"]
            if cc in country_scores:
                sc+=.16*country_scores[cc];reasons.append("country match")
            else:
                # Currency can infer country only if that currency code maps uniquely in DB.
                holders={x["code"] for x in self.countries for y in x["currencies"] if y["code"]==curcode}
                if len(holders)==1:
                    sc+=.08;reasons.append("country inferred from unique currency")
            if year:
                vf=currency.get("valid_from");vt=currency.get("valid_to")
                if vf and year<vf: sc-=.28;reasons.append("year before currency period")
                elif vt and year>vt: sc-=.28;reasons.append("year after currency period")
                else: sc+=.07;reasons.append("year compatible")
            if denomination is not None:sc+=.05
            candidates.append(self._candidate(c,currency,candidate_denom,year,sc,reasons))

        # Country-only + year: choose only currencies whose validity period fits,
        # but keep ambiguity when more than one could plausibly fit.
        if country_scores and not currency_scores and not special_hits:
            for cc,cscore in country_scores.items():
                c=next(c for c in self.countries if c["code"]==cc)
                plausible=[]
                for currency in c["currencies"]:
                    vf=currency.get("valid_from");vt=currency.get("valid_to")
                    if year and ((vf and year<vf) or (vt and year>vt)):continue
                    plausible.append(currency)
                for currency in plausible:
                    sc=.48+.22*cscore+(0.10 if year else 0)+(0.04 if denomination is not None else 0)
                    candidates.append(self._candidate(c,currency,denomination,year,sc,["country match","currency inferred by valid period"]))

        # Deduplicate candidates
        merged={}
        for x in candidates:
            k=(x["country_code"],x["currency_code"],x.get("denomination_value"),x.get("year"))
            if k not in merged or x["confidence"]>merged[k]["confidence"]:
                merged[k]=x
        candidates=sorted(merged.values(),key=lambda x:x["confidence"],reverse=True)

        # Detect ambiguous currencies like generic "franc" or "euro" without country.
        top=candidates[0] if candidates else None
        ambiguous=False
        if len(candidates)>1 and top:
            if top["confidence"]-candidates[1]["confidence"]<.08:
                ambiguous=True

        thresholds=self.db.get("confidence_thresholds",{})
        conf=top["confidence"] if top else 0
        if not top or conf < thresholds.get("reject_below",.55):
            status="unresolved"
        elif ambiguous or conf < thresholds.get("auto_accept",.88):
            status="review"
        else:
            status="resolved"

        return {
            "raw":original,
            "normalized":text,
            "transliterated":latin,
            "status":status,
            "ambiguous":ambiguous,
            "best":top,
            "candidates":candidates[:8]
        }

    def _candidate(self,c,currency,denomination,year,score,reasons):
        return {
            "country":c["name"],"country_code":c["code"],
            "currency":currency["name"],"currency_code":currency["code"],
            "denomination_value":denomination,
            "year":year,
            "confidence":round(max(0,min(score,1)),4),
            "reasons":reasons,
            "search_variants":self.search_variants(c,currency,denomination,year)
        }

    def search_variants(self,c,currency,denomination,year):
        val="" if denomination is None else (str(int(denomination)) if float(denomination).is_integer() else str(denomination))
        year_s=str(year or "")
        names=[c["name"]]
        names += [a for a in c.get("aliases",[]) if a.isascii() and len(a)>2][:3]
        cnames=[currency["name"],currency["code"]]
        cnames += [a for a in currency.get("aliases",[]) if a.isascii() and len(a)>2][:4]
        qs=[]
        for country in names[:3]:
            for curr in cnames[:4]:
                q=" ".join(x for x in [country,val,curr,year_s] if x)
                if q:qs.append(q)
        # Currency-only variants are useful for unique historical currency names.
        for curr in cnames[:3]:
            q=" ".join(x for x in [val,curr,year_s] if x)
            if q:qs.append(q)
        out=[];seen=set()
        for q in qs:
            k=norm(q)
            if k not in seen:
                seen.add(k);out.append(q)
        return out[:12]

_default=None
def get_resolver():
    global _default
    if _default is None:_default=CoinIdentityResolver()
    return _default

def resolve_coin_identity(text):
    return get_resolver().resolve(text)

if __name__=="__main__":
    import sys, json
    q=" ".join(sys.argv[1:]) or "5 drachmai 1976"
    print(json.dumps(resolve_coin_identity(q),ensure_ascii=False,indent=2))
