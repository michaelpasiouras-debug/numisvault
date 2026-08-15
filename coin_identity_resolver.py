from __future__ import annotations
import json, re, unicodedata
from pathlib import Path
from difflib import SequenceMatcher

DB_PATH = Path(__file__).with_name("coin_identity_database.json")
ISSUE_DB_PATH = Path(__file__).with_name("coin_issue_database.json")

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
    s=strip_accents((s or "").lower())
    s=s.replace("€"," euro ").replace("£"," gbp ").replace("$"," usd ").replace("¢"," cent ")
    s=s.replace("¼"," 1/4 ").replace("½"," 1/2 ")
    s=re.sub(r"[^a-z0-9α-ωа-яёіїєґ/.\-\s]+"," ",s,flags=re.I)
    s=re.sub(r"\s+"," ",s).strip()
    return s

def variants(s:str):
    n=norm(s)
    t=transliterate_greek(s)
    return {n, norm(t)}

def sim(a,b):
    return SequenceMatcher(None,norm(a),norm(b)).ratio()

# Short ISO country/currency codes that collide with common English words in
# ordinary sentences ("...with NO coin info", "give US the price", "ALL of
# them"). Matched as whole-word exact hits, these previously scored a full
# 1.0 confidence off completely unrelated text — the opposite of the
# "conservative fuzzy matching" the resolver is supposed to do. Every one of
# these countries/currencies has other, unambiguous aliases (full name,
# native name, etc.), so dropping just the colliding short form costs no
# real recall for genuine input, only removes false positives.
AMBIGUOUS_SHORT_ALIASES={"no","is","it","be","at","us","by","me","all","or","an","on",
                          "so","to","of","in","as","if","do","my","he","we","hi","up"}

class CoinIdentityResolver:
    def __init__(self, db_path=DB_PATH, issue_db_path=ISSUE_DB_PATH):
        self.db=json.loads(Path(db_path).read_text(encoding="utf-8"))
        # Issue-level seed database (issuers, mints, catalog-ID prefixes, variant
        # aliases, negative product terms, and a small seed of known issues).
        # This is deliberately a SEED, not an exhaustive catalog — see
        # _issue_validation() below, which never treats "absent from this file"
        # as "does not exist".
        self.issue_db=json.loads(Path(issue_db_path).read_text(encoding="utf-8")) if Path(issue_db_path).exists() else {}
        self.countries=self.db["countries"]
        self.alias_index=[]
        def safe_variants(a):
            return {v for v in variants(a) if v not in AMBIGUOUS_SHORT_ALIASES}
        for c in self.countries:
            for a in [c["name"],c["code"],*c.get("aliases",[])]:
                for v in safe_variants(a):
                    if v:self.alias_index.append(("country",v,c,None))
            for cur in c.get("currencies",[]):
                for a in [cur["name"],cur["code"],*cur.get("aliases",[])]:
                    for v in safe_variants(a):
                        if v:self.alias_index.append(("currency",v,c,cur))
                sub=cur.get("subunit") or {}
                for a in [sub.get("name",""),*sub.get("aliases",[])]:
                    for v in safe_variants(a):
                        if v:self.alias_index.append(("subunit",v,c,cur))
                for sd in cur.get("special_denominations",[]):
                    for a in sd.get("aliases",[]):
                        for v in safe_variants(a):
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

    def _parse_catalog_ids(self,text):
        out={}
        t=text or ""
        patterns={
            "KM":r"(?<![A-Za-z0-9])KM\s*#?\s*([A-Za-z0-9.\-]+)",
            "Y":r"(?<![A-Za-z0-9])Y\s*#?\s*([A-Za-z0-9.\-]+)",
            "Pick":r"(?<![A-Za-z0-9])(?:Pick|P)\s*#?\s*([A-Za-z0-9.\-]+)",
            "RIC":r"(?<![A-Za-z0-9])RIC\s*([A-Za-z0-9.\-]+)",
        }
        for k,rx in patterns.items():
            m=re.search(rx,t,re.I)
            if m:out[k]=m.group(1)
        return out

    def _resolve_issuer(self,text,country_code=None,year=None):
        nt=norm(text)
        hits=[]
        for issuer in self.issue_db.get("issuers",[]):
            if country_code and issuer.get("country_code") and issuer["country_code"]!=country_code:
                continue
            aliases=[issuer.get("canonical",""),*issuer.get("aliases",[])]
            score=0
            matched=None
            for a in aliases:
                na=norm(a)
                if na and self._contains_alias(nt,na):
                    score=1;matched=a;break
                s=sim(nt,na)
                if s>score and s>=.72:score=s;matched=a
            if score:
                if year:
                    vf=issuer.get("valid_from");vt=issuer.get("valid_to")
                    # A clear validity violation (coin year outside the issuer's
                    # reign) means this is not the issuer — reject outright
                    # rather than merely discounting, so a long-dead ruler is
                    # never credited for a coin struck decades later.
                    if (vf and year<vf) or (vt and year>vt):
                        continue
                hits.append((score,issuer,matched))
        hits.sort(reverse=True,key=lambda x:x[0])
        return hits[0] if hits else None

    def _resolve_mint(self,text,country_code=None):
        nt=norm(text)
        hits=[]
        for mint in self.issue_db.get("mints",[]):
            # Mintmarks are context-bound to a country: a bare "D" means Denver
            # for a US coin but Munich for a German coin. Never resolved globally.
            if country_code and mint.get("country_code")!=country_code:continue
            if not country_code:continue
            for a in [mint.get("mintmark",""),mint.get("canonical",""),*mint.get("aliases",[])]:
                na=norm(a)
                if not na:continue
                if self._contains_alias(nt,na):
                    # one-letter mintmarks are accepted only when explicitly isolated
                    score=1.0 if len(na)>1 else .92
                    hits.append((score,mint,a))
                    break
        hits.sort(reverse=True,key=lambda x:x[0])
        return hits[0] if hits else None

    def _resolve_variants(self,text):
        nt=norm(text)
        found=[]
        for canon,aliases in (self.issue_db.get("variant_aliases") or {}).items():
            for a in aliases:
                if self._contains_alias(nt,norm(a)):
                    found.append(canon);break
        return sorted(set(found))

    def _negative_flags(self,text):
        nt=norm(text)
        hits=[]
        for term in self.issue_db.get("negative_terms",[]):
            if self._contains_alias(nt,norm(term)):hits.append(term)
        return sorted(set(hits))

    def _issue_validation(self,candidate):
        issues=self.issue_db.get("issues",[])
        if not issues:
            return {"status":"unknown","matches":[],"note":"No issue database loaded."}
        hits=[]
        for issue in issues:
            if issue.get("country_code")!=candidate.get("country_code"):continue
            if issue.get("currency_code")!=candidate.get("currency_code"):continue
            if issue.get("year")!=candidate.get("year"):continue
            iv=issue.get("denomination_value")
            cv=candidate.get("denomination_value")
            if iv is not None and cv is not None and abs(float(iv)-float(cv))>1e-9:continue
            hits.append(issue)
        if hits:
            return {"status":"confirmed_seed","matches":hits[:5],"note":"Identity exists in the loaded issue seed/database."}
        # Absence from a partial database is NOT evidence that the issue does
        # not exist — this is a seed, not an exhaustive world coin catalog.
        return {"status":"not_in_local_issue_db","matches":[],"note":"Not found in the current partial issue database; verify via catalog/API before rejecting."}

    def listing_match_score(self,target,listing_title):
        """0-100 identity score for a marketplace listing title against a
        resolved target identity. Hard fields (country/year) dominate; explicit
        product-type mismatches (banknote/replica/set/lot...) are penalized
        heavily. This score never overrides the existing hard filters
        (passes_hard_filter in the backend) — it is a supplementary, explainable
        ranking signal for the evidence panel."""
        if not target:return {"score":0,"reasons":["no target identity"]}
        t=norm(listing_title)
        score=0;reasons=[];hard_fail=[]
        cc=target.get("country_code");cur=target.get("currency_code");year=target.get("year");val=target.get("denomination_value")
        c=next((x for x in self.countries if x["code"]==cc),None)
        if c:
            aliases=[c["name"],c["code"],*c.get("aliases",[])]
            if any(self._contains_alias(t,norm(a)) for a in aliases if a):
                score+=22;reasons.append("country")
            else:
                hard_fail.append("country")
        if year:
            if re.search(r"(?<!\d)"+re.escape(str(year))+r"(?!\d)",t):
                score+=22;reasons.append("year")
            else:hard_fail.append("year")
        if val is not None:
            # denomination representation is complex; use exact numeric plus currency/subunit aliases
            v = str(int(val)) if float(val).is_integer() else str(val)
            if re.search(r"(?<!\d)"+re.escape(v)+r"(?!\d)",t):
                score+=18;reasons.append("denomination number")
        if cur:
            currency=None
            if c:
                currency=next((x for x in c.get("currencies",[]) if x["code"]==cur),None)
            if currency and any(self._contains_alias(t,norm(a)) for a in [currency["name"],currency["code"],*currency.get("aliases",[])] if a):
                score+=14;reasons.append("currency")
        issuer=target.get("issuer")
        if issuer and self._contains_alias(t,norm(issuer)):
            score+=8;reasons.append("issuer")
        mintmark=target.get("mintmark")
        if mintmark and self._contains_alias(t,norm(mintmark)):
            score+=7;reasons.append("mintmark")
        for vname in (target.get("variants") or []):
            aliases=(self.issue_db.get("variant_aliases") or {}).get(vname,[vname])
            if any(self._contains_alias(t,norm(a)) for a in aliases):
                score+=4;reasons.append("variant:"+vname)
        neg=self._negative_flags(listing_title)
        if neg:
            score-=45;reasons.append("negative:"+",".join(neg))
        if hard_fail:
            score-=18*len(hard_fail);reasons.append("missing:"+",".join(hard_fail))
        score=max(0,min(100,score))
        return {"score":score,"reasons":reasons,"hard_fail":hard_fail,"negative_flags":neg}

    def resolve(self,raw:str):
        original=raw or ""
        text=norm(original)
        latin=norm(transliterate_greek(original))
        texts={text,latin}
        year=self._parse_year(text)
        denomination=self._parse_number(text)

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

        # Issuer/mint/catalog-ID/variant enrichment and negative-product-term
        # detection (Coin Intelligence Core). These never invent a country or
        # denomination on their own — they only annotate whatever country/year
        # was already resolved above.
        catalog_ids=self._parse_catalog_ids(original)
        issuer_hit=self._resolve_issuer(original,top.get("country_code") if top else None,year) if top else None
        mint_hit=self._resolve_mint(original,top.get("country_code") if top else None) if top else None
        variants_found=self._resolve_variants(original)
        negative_flags=self._negative_flags(original)

        if top:
            top=dict(top)
            top["catalog_ids"]=catalog_ids
            top["issuer"]=issuer_hit[1]["canonical"] if issuer_hit else None
            top["mint"]=mint_hit[1]["canonical"] if mint_hit else None
            top["mintmark"]=mint_hit[1]["mintmark"] if mint_hit else None
            top["variants"]=variants_found
            top["negative_flags"]=negative_flags
            top["issue_validation"]=self._issue_validation(top)
            # A catalog ID that matches a validated issue is a strong signal;
            # an explicit non-coin product term (banknote/replica/set/lot...)
            # must never be silently auto-resolved, however high the confidence.
            if negative_flags:
                status="review"

        return {
            "raw":original,
            "normalized":text,
            "transliterated":latin,
            "status":status,
            "ambiguous":ambiguous,
            "best":top,
            "candidates":candidates[:8],
            "catalog_ids":catalog_ids,
            "issuer_match":issuer_hit[1] if issuer_hit else None,
            "mint_match":mint_hit[1] if mint_hit else None,
            "variants":variants_found,
            "negative_flags":negative_flags
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
