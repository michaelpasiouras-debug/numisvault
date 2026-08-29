from __future__ import annotations
import json, re, unicodedata
from pathlib import Path
from difflib import SequenceMatcher
from multilingual_country_aliases import normalize_country_aliases_in_text

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
    return normalize_country_aliases_in_text(s)

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

# Some special-denomination aliases double as ordinary descriptions of a
# coin's design/imagery rather than a genuine denomination statement — "coin
# with an eagle on it" is not the same claim as "this is a $10 Eagle".
# Matching one of these bare words alone is not enough to auto-resolve a
# country+denomination; it requires independent corroborating context (an
# explicit country, an explicit currency, a catalog ID, or issuer/mint
# context — see the special_hits context gate in resolve()). Multi-word
# historical numismatic terms ("quarter eagle", "half eagle", "double eagle",
# "half crown") are NOT included here — a phrase that specific is very
# unlikely to appear as mere incidental visual description, unlike the bare
# single word.
AMBIGUOUS_VISUAL_DENOMINATION_WORDS={"eagle","crown","sovereign"}

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

    # Fuzzy typo-correction is only useful/safe for reasonably long, meaningful
    # words ("drahcma" -> "drachma"). Below this length, fuzzy SequenceMatcher
    # scores between short tokens and short codes/aliases (e.g. "no"~"NOK",
    # "us"~"USD", "be"~"BEF", "at"~"ATS") routinely cross typical similarity
    # thresholds purely by coincidence, since there are few possible characters
    # to differ on. Exact matching (which already excludes the dangerous short
    # stopword collisions via AMBIGUOUS_SHORT_ALIASES) remains fully available
    # for these short forms — only the FUZZY path is restricted here.
    MIN_FUZZY_ALIAS_LEN = 5

    def _best_fuzzy(self,text,kind=None,min_score=.80):
        toks=text.split()
        grams=set(toks)
        for n in (2,3):
            grams.update(" ".join(toks[i:i+n]) for i in range(max(0,len(toks)-n+1)))
        hits=[]
        for k,a,c,cur in self.alias_index:
            if kind and k!=kind:continue
            if len(a)<self.MIN_FUZZY_ALIAS_LEN:continue
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
        ids,_=self._parse_catalog_ids_with_spans(text)
        return ids

    def _catalog_id_patterns(self):
        return {
            "KM":r"(?<![A-Za-z0-9])KM\s*#?\s*([A-Za-z0-9.\-]+)",
            "Y":r"(?<![A-Za-z0-9])Y\s*#?\s*([A-Za-z0-9.\-]+)",
            # A bare "P" or "Pick" WITHOUT a "#" and followed only by a plain
            # number (e.g. "quarter P 1964") is far more likely to be a US
            # mintmark ("P" = Philadelphia) sitting next to a year than a Pick
            # catalog reference — Pick numbers are conventionally written with
            # a "#". Only "Pick", "Pick#", "Pick 123" (full word) or the
            # explicit "P#123" short form count as Pick; a bare isolated "P"
            # followed by a number never does.
            "Pick":r"(?<![A-Za-z0-9])(?:Pick\s*#?\s*|P#\s*)([A-Za-z0-9.\-]+)",
            "RIC":r"(?<![A-Za-z0-9])RIC\s*([A-Za-z0-9.\-]+)",
        }

    def _parse_catalog_ids_with_spans(self,text):
        """Returns (ids_dict, spans) where spans are (start,end) character
        ranges in `text` that matched a catalog-ID prefix (including its
        number). Used so the matched numbers can be masked out before year/
        denomination parsing — a catalog number like KM#123 must never be
        mistaken for the coin's denomination or year."""
        out={}
        spans=[]
        t=text or ""
        for k,rx in self._catalog_id_patterns().items():
            m=re.search(rx,t,re.I)
            if m:
                out[k]=m.group(1)
                spans.append(m.span())
        return out,spans

    def _mask_spans(self,text,spans):
        if not spans:return text
        chars=list(text)
        for start,end in spans:
            for i in range(start,min(end,len(chars))):
                chars[i]=" "
        return "".join(chars)

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

    def _denomination_aliases_for(self,country_code,currency_code,value):
        """Semantic denomination aliases for a given value (e.g. 0.25 USD ->
        'quarter', 'quarter dollar', '25 cents', ...), drawn from the same
        special_denominations ontology used elsewhere in the resolver. Lets
        listing_match_score recognize "quarter" as a match for 0.25 USD even
        though the listing title never spells out the digits "0.25"."""
        if value is None:return []
        c=next((x for x in self.countries if x["code"]==country_code),None)
        if not c:return []
        out=[]
        for currency in c.get("currencies",[]):
            if currency_code and currency["code"]!=currency_code:continue
            for sd in currency.get("special_denominations",[]):
                try:
                    if abs(float(sd.get("value"))-float(value))<1e-9:
                        out.extend(sd.get("aliases") or [])
                except (TypeError,ValueError):
                    continue
        return out

    def listing_match_score(self,target,listing_title):
        """0-100 identity score for a marketplace listing title against a
        resolved target identity. Hard fields (country/year) dominate; explicit
        product-type mismatches (banknote/replica/set/lot...) are penalized
        heavily. This score never overrides the existing hard filters
        (passes_hard_filter in the backend) — it is a supplementary, explainable
        ranking signal for the evidence panel.

        The score is normalized over only the fields actually PRESENT in the
        target identity (country/year/denomination/currency plus optional
        issuer/mint/variant when the resolver found them) — so an exact match
        on every available field reaches a genuinely high score, rather than
        being capped well below 100 merely because some optional metadata
        (issuer, mintmark, variant) wasn't part of this particular identity.
        Denomination matching also recognizes semantic aliases such as
        "quarter" for 0.25 USD, not only literal digits."""
        if not target:return {"score":0,"reasons":["no target identity"]}
        t=norm(listing_title)
        cc=target.get("country_code");cur=target.get("currency_code");year=target.get("year");val=target.get("denomination_value")
        c=next((x for x in self.countries if x["code"]==cc),None) if cc else None
        currency=None
        if c and cur:
            currency=next((x for x in c.get("currencies",[]) if x["code"]==cur),None)

        # (weight, present, matched, field_name) — only PRESENT fields count
        # toward the normalization denominator.
        fields=[]
        reasons=[];hard_fail=[]

        country_present=bool(c)
        country_matched=False
        if country_present:
            aliases=[c["name"],c["code"],*c.get("aliases",[])]
            country_matched=any(self._contains_alias(t,norm(a)) for a in aliases if a)
            if country_matched:reasons.append("country")
            else:hard_fail.append("country")
        fields.append((28,country_present,country_matched))

        year_present=bool(year)
        year_matched=False
        if year_present:
            year_matched=bool(re.search(r"(?<!\d)"+re.escape(str(year))+r"(?!\d)",t))
            if year_matched:reasons.append("year")
            else:hard_fail.append("year")
        fields.append((28,year_present,year_matched))

        denom_present=val is not None
        denom_matched=False
        if denom_present:
            v = str(int(val)) if float(val).is_integer() else str(val)
            denom_matched=bool(re.search(r"(?<!\d)"+re.escape(v)+r"(?!\d)",t))
            if not denom_matched:
                for alias in self._denomination_aliases_for(cc,cur,val):
                    if self._contains_alias(t,norm(alias)):
                        denom_matched=True;break
            if denom_matched:reasons.append("denomination")
            else:hard_fail.append("denomination")
        fields.append((22,denom_present,denom_matched))

        currency_present=bool(currency)
        currency_matched=False
        if currency_present:
            currency_matched=any(self._contains_alias(t,norm(a)) for a in [currency["name"],currency["code"],*currency.get("aliases",[])] if a)
            if currency_matched:reasons.append("currency")
        fields.append((16,currency_present,currency_matched))

        issuer=target.get("issuer")
        issuer_present=bool(issuer)
        issuer_matched=issuer_present and self._contains_alias(t,norm(issuer))
        if issuer_matched:reasons.append("issuer")
        fields.append((6,issuer_present,issuer_matched))

        mintmark=target.get("mintmark")
        mint_present=bool(mintmark)
        mint_matched=mint_present and self._contains_alias(t,norm(mintmark))
        if mint_matched:reasons.append("mintmark")
        fields.append((5,mint_present,mint_matched))

        variants_target=target.get("variants") or []
        variant_present=bool(variants_target)
        variant_matched=False
        if variant_present:
            matched_any=False
            for vname in variants_target:
                aliases=(self.issue_db.get("variant_aliases") or {}).get(vname,[vname])
                if any(self._contains_alias(t,norm(a)) for a in aliases):
                    matched_any=True;reasons.append("variant:"+vname)
            variant_matched=matched_any
        fields.append((4,variant_present,variant_matched))

        total_weight=sum(w for w,present,_ in fields if present)
        achieved_weight=sum(w for w,present,matched in fields if present and matched)
        score=(100.0*achieved_weight/total_weight) if total_weight>0 else 0.0

        # Wrong/missing country, year or denomination are strongly penalized
        # beyond simply not earning their points — these are the fields the
        # backend's separate hard filter also gates on, so a listing that
        # contradicts them should never look like a near-match here either.
        if hard_fail:
            score-=20*len(hard_fail);reasons.append("missing:"+",".join(hard_fail))

        neg=self._negative_flags(listing_title)
        if neg:
            # An explicit non-coin product signal (replica/banknote/set/lot...)
            # must never look like a plausible match regardless of how well
            # the other fields happened to line up textually — cap outright
            # rather than just subtracting, so this stays low at any base score.
            score=min(score-45,20)
            reasons.append("negative:"+",".join(neg))

        score=int(round(max(0,min(100,score))))
        return {"score":score,"reasons":reasons,"hard_fail":hard_fail,"negative_flags":neg}

    def resolve(self,raw:str):
        # Bound the input length before any processing. Fuzzy matching below
        # (SequenceMatcher against every alias in the DB) has no early exit and
        # its cost scales with input length; an unbounded string (a paste
        # mistake, or a deliberately long input) can otherwise stall a request
        # for many seconds — measured up to ~29s on a 50,000-character input in
        # testing, easily enough to trigger a backend worker timeout. No
        # legitimate coin description needs more than a couple hundred
        # characters, so truncating is safe and does not affect real usage.
        MAX_INPUT_LENGTH=500  # Increased to preserve year/theme/variant in long numismatic listings
        original=(raw or "")[:MAX_INPUT_LENGTH]
        # Catalog IDs (KM#123, Y#45, Pick#7, RIC 123...) must be identified and
        # masked out BEFORE year/denomination parsing — otherwise a catalog
        # number can be mistaken for the coin's face-value denomination (see
        # "KM#123 5 drachmai 1976": without masking, 123 could be picked up as
        # the denomination instead of 5).
        catalog_ids,catalog_spans=self._parse_catalog_ids_with_spans(original)
        numbers_source=self._mask_spans(original,catalog_spans)
        text=norm(original)
        latin=norm(transliterate_greek(original))
        texts={text,latin}
        year=self._parse_year(norm(numbers_source))
        denomination=self._parse_number(norm(numbers_source))

        country_scores={}
        country_match_len={}
        currency_scores={}
        # Two historically distinct monetary systems can share the same
        # display currency_code (e.g. Germany's "Deutsche Mark" 1948-2001 and
        # the earlier "German mark" 1871-1948 both use code "DEM"). Keying
        # candidates purely by (country_code, currency_code) would silently
        # collapse them into whichever one happens to appear first in the
        # database, regardless of which alias/year actually matched — so the
        # internal key uses the specific currency object's identity instead;
        # currency_registry recovers the (country, currency) pair for it.
        currency_registry={}
        special_hits=[]

        def _cur_key(c,cur):
            k=(c["code"],id(cur))
            currency_registry[k]=(c,cur)
            return k

        # Exact phrase matches: strongest signal
        for kind,a,c,cur in self.alias_index:
            matched=any(self._contains_alias(t,a) for t in texts)
            if not matched:continue
            if kind=="country":
                country_scores[c["code"]]=max(country_scores.get(c["code"],0),1.0)
                # Track the longest (most specific) alias that produced an
                # exact country match. A short adjectival word that happens to
                # double as a country alias (e.g. "swiss" — also the first
                # word of "Swiss franc") is a weaker, more ambiguous signal
                # than a full proper name like "Liechtenstein". Used below to
                # break ties when two countries are both matched at full
                # confidence: explicit country > inferred country from a
                # currency-adjective-style alias.
                if len(a)>country_match_len.get(c["code"],0):
                    country_match_len[c["code"]]=len(a)
            elif kind=="currency":
                key=_cur_key(c,cur)
                currency_scores[key]=max(currency_scores.get(key,0),1.0)
            elif kind=="special":
                currency,sd=cur
                special_hits.append((1.0,c,currency,sd,a))
            elif kind=="subunit":
                key=_cur_key(c,cur)
                currency_scores[key]=max(currency_scores.get(key,0),.92)

        # Fuzzy fallback only when exact matching was insufficient
        if not country_scores:
            for score,kind,a,c,cur in self._best_fuzzy(latin,"country",.84)[:5]:
                country_scores[c["code"]]=max(country_scores.get(c["code"],0),score*.86)
        if not currency_scores and not special_hits:
            for score,kind,a,c,cur in self._best_fuzzy(latin,"currency",.80)[:8]:
                key=_cur_key(c,cur)
                currency_scores[key]=max(currency_scores.get(key,0),score*.88)
            for score,kind,a,c,cur in self._best_fuzzy(latin,"special",.82)[:5]:
                currency,sd=cur
                special_hits.append((score*.90,c,currency,sd,a))

        # If the user explicitly named a country (exact/high-confidence match),
        # do not let the same generic currency/subunit in other countries create
        # artificial ambiguity (e.g. "2 euro Croatia 2025", "25 cents USA 1964").
        explicit_countries={cc for cc,score in country_scores.items() if score>=.95}
        if len(explicit_countries)>1:
            # Multiple countries tied at full confidence: prefer whichever was
            # matched via the more specific (longer) alias — e.g. an explicit
            # "Liechtenstein" beats an adjectival "swiss" that only matched
            # because it's also the first word of the currency name "Swiss
            # franc". If the tied countries were matched with equally specific
            # aliases, genuine ambiguity is preserved (both kept).
            best_len=max(country_match_len.get(cc,0) for cc in explicit_countries)
            explicit_countries={cc for cc in explicit_countries if country_match_len.get(cc,0)==best_len}
        if explicit_countries:
            currency_scores={k:v for k,v in currency_scores.items() if k[0] in explicit_countries}
            special_hits=[x for x in special_hits if x[1]["code"] in explicit_countries]

        candidates=[]

        # Special denomination nicknames (quarter, dime, sovereign...)
        for score,c,currency,sd,a in special_hits:
            sc=.62 + .20*score
            reasons=["special denomination alias: "+a]
            # Some special-denomination aliases (see
            # AMBIGUOUS_VISUAL_DENOMINATION_WORDS) are also ordinary words for
            # a coin's design/imagery — "coin with an eagle on it" is a visual
            # description, not necessarily a claim that this IS a $10 Eagle.
            # Require independent corroborating context before letting the
            # bare word alone drive an auto-resolved country+denomination;
            # without it, cap the confidence well below the auto-accept
            # threshold so the result comes back REVIEW/UNRESOLVED instead of
            # a confident (and potentially wrong) guess.
            if norm(a) in AMBIGUOUS_VISUAL_DENOMINATION_WORDS:
                has_context=(
                    c["code"] in explicit_countries
                    or _cur_key(c,currency) in currency_scores
                    or bool(catalog_ids)
                    or bool(self._resolve_issuer(original,None,None))
                    or bool(self._resolve_mint(original,c["code"]))
                )
                if not has_context:
                    sc=min(sc,.45)
                    reasons.append("ambiguous visual/design word without independent context")
            if c["code"] in country_scores:sc+=.16
            if year:
                vf=currency.get("valid_from"); vt=currency.get("valid_to")
                if vf and year<vf:sc-=.22
                if vt and year>vt:sc-=.22
                if (not vf or year>=vf) and (not vt or year<=vt):sc+=.06
            candidates.append(self._candidate(c,currency,sd.get("value"),year,sc,reasons))

        # Currency candidates
        for key,cs in currency_scores.items():
            cc,_=key
            c,currency=currency_registry[key]
            curcode=currency["code"]
            candidate_denom=denomination
            # Canonicalize decimal subunit inputs into the major currency unit.
            # 25 cents -> 0.25 USD/EUR; 50 pence -> 0.50 GBP.  Keep this
            # deliberately scoped to modern decimal currencies with an exact
            # lexical subunit signal so historical pre-decimal values are not
            # silently rescaled.
            _decimal_subunit=(
                curcode in ("USD","EUR") and re.search(r"(?<![a-z])cents?(?![a-z])",text,re.I)
            ) or (
                curcode=="GBP" and re.search(r"(?<![a-z])(?:pence|penn(?:y|ies))(?![a-z])",text,re.I)
            )
            if candidate_denom is not None and _decimal_subunit:
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

        # A candidate with a concrete denomination_value for the same
        # (country, currency, year) as another candidate with NO denomination
        # information is strictly more informative, not a competing
        # alternative identity — e.g. "USA quarter dollar 1964" matches both
        # the "quarter" special-denomination alias (denom=0.25) AND a plain
        # "dollar" currency-name match (denom=None) for the exact same
        # country/currency/year. Without this, the two tied at equal
        # confidence and the resolver reported false ambiguity/REVIEW for an
        # otherwise unambiguous input. Two candidates that BOTH have a
        # concrete (and different) denomination are left untouched — that is
        # genuine potential ambiguity, not this subsumption case.
        by_core={}
        extra=[]
        for x in merged.values():
            core=(x["country_code"],x["currency_code"],x.get("year"))
            prev=by_core.get(core)
            if prev is None:
                by_core[core]=x
                continue
            prev_has_denom=prev.get("denomination_value") is not None
            x_has_denom=x.get("denomination_value") is not None
            if x_has_denom and not prev_has_denom:
                by_core[core]=x
            elif prev_has_denom and not x_has_denom:
                pass
            else:
                extra.append(x)
        candidates=sorted(list(by_core.values())+extra,key=lambda x:x["confidence"],reverse=True)

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

        # Issuer/mint/variant/negative-product-term enrichment (Coin
        # Intelligence Core). catalog_ids was already computed above, before
        # year/denomination parsing. These never invent a country or
        # denomination on their own — they only annotate whatever country/year
        # was already resolved above.
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
