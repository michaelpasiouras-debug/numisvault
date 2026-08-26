from __future__ import annotations
import re

# Shared country-name normalization for CoinBids input AND marketplace titles.
# Focus: names commonly encountered on European numismatic marketplaces.
# Canonical values match CoinBids' English country names.
ALIASES={
'Greece':['greece','greek','hellas','ellada','ellas','ελλαδα','ελλάδα','griechenland','griekenland','grece','grèce','grecia','grécia','grecja','recko','řecko','grecko','grécko','grcka','grčka','görögország','gorogorszag','yunanistan','grækenland','graekenland','grekland','kreikka'],
'Germany':['germany','deutschland','allemagne','alemania','germania','duitsland','germanie','alemanha','niemcy','nemecko','německo','nemacka','njemačka','nemecka','tyskland','saksa'],
'France':['france','frankreich','francia','frança','franca','frankrijk','frankrig','frankrike','ranska','francja','francie','francuska','francúzsko'],
'Italy':['italy','italia','italien','italie','italië','italija','wlochy','włochy','italsko','itália'],
'Spain':['spain','espana','españa','spanien','espagne','spagna','spanje','hiszpania','spanelsko','španělsko','spanija','španija','espanha'],
'Portugal':['portugal','portugalia','portugalsko','portugalska','portogallo'],
'Netherlands':['netherlands','nederland','holland','niederlande','pays bas','pays-bas','paesi bassi','paises bajos','países bajos','holanda','niderlandy','nizozemsko','nizozemska'],
'Belgium':['belgium','belgie','belgië','belgique','belgien','belgio','belgica','bélgica','belgia'],
'Austria':['austria','österreich','osterreich','autriche','oostenrijk','austrija','ausztria','avusturya'],
'Switzerland':['switzerland','schweiz','suisse','svizzera','suiza','zwitserland','szwajcaria','švýcarsko','svajcarska','švajčiarsko','svicarska','švicarska','isvicre'],
'United Kingdom':['united kingdom','great britain','britain','uk','england','grossbritannien','großbritannien','royaume uni','royaume-uni','regno unito','reino unido','verenigd koninkrijk','wielka brytania','velka britanie','velká británie','velka britania','velika britanija'],
'United States':['united states','united states of america','usa','u.s.a.','vereinigte staaten','etats unis','états-unis','stati uniti','estados unidos','verenigde staten','stany zjednoczone','spojene staty','spojené státy','sjedinjene drzave','sjedinjene države','abd'],
'Poland':['poland','polska','polen','pologne','polonia','polsko'],
'Czech Republic':['czech republic','czechia','cesko','česko','tschechien','republique tcheque','république tchèque','repubblica ceca','republica checa','república checa','tsjechie','czechy'],
'Slovakia':['slovakia','slovensko','slowakei','slovaquie','slovacchia','eslovaquia','slowakije'],
'Hungary':['hungary','magyarorszag','magyarország','ungarn','hongrie','ungheria','hungria','hongarije','wegry','węgry','madarsko','maďarsko'],
'Romania':['romania','românia','rumanien','roumanie','romenia','rumania','roemenie','rumunia','rumunsko'],
'Bulgaria':['bulgaria','bulgarien','bulgarie','bulgarije','bułgaria','bulharsko'],
'Croatia':['croatia','hrvatska','kroatien','croatie','croazia','croacia','kroatie','chorwacja','chorvatsko'],
'Serbia':['serbia','srbija','serbien','serbie','servië','servie'],
'Slovenia':['slovenia','slovenija','slowenien','slovenie','slovénie','slowenia','slovinsko'],
'Bosnia and Herzegovina':['bosnia and herzegovina','bosna i hercegovina','bosnien und herzegowina','bosnie herzegovine','bosnie-herzégovine','bosnia ed erzegovina','bosnia y herzegovina'],
'Montenegro':['montenegro','crna gora'],
'North Macedonia':['north macedonia','macedonia del norte','nordmazedonien','macedoine du nord','macédoine du nord','macedonia del nord','noord macedonie','noord-macedonië','severna makedonija'],
'Albania':['albania','shqiperia','shqipëria','albanien','albanie','albanië'],
'Cyprus':['cyprus','kypros','κύπρος','zypern','chypre','cipro','chipre','cypr'],
'Malta':['malta'],
'Ireland':['ireland','eire','éire','irland','irlande','irlanda','ierland','irlandia','irsko'],
'Denmark':['denmark','danmark','dänemark','danemark','danemarca','denemarken','dania','dansko'],
'Sweden':['sweden','sverige','schweden','suede','suède','svezia','suecia','zweden','szwecja','svedsko','švédsko'],
'Norway':['norway','norge','noreg','norwegen','norvege','norvège','norvegia','noruega','noorwegen','norwegia','norsko'],
'Finland':['finland','suomi','finnland','finlande','finlandia','finsko'],
'Iceland':['iceland','island','ísland','islande','islanda','ijsland','islandia'],
'Estonia':['estonia','eesti','estland','estonie'],
'Latvia':['latvia','latvija','lettland','lettonie','lettonia','letonia','letland','lotwa','łotwa'],
'Lithuania':['lithuania','lietuva','litauen','lituanie','lituania','litouwen','litwa'],
'Luxembourg':['luxembourg','luxemburg','letzebuerg','lëtzebuerg','luxemburgo'],
'Turkey':['turkey','turkiye','türkiye','turkei','turquie','turchia','turquia','turkije','turcja','turecko'],
'Russia':['russia','rossiya','россия','russland','russie','rusia','rusland','rosja','rusko'],
'Ukraine':['ukraine','ukraina','україна'],
'Georgia':['georgia','sakartvelo','საქართველო','georgien','georgie','géorgie','georgië','gruzja'],
'Armenia':['armenia','hayastan','հայաստան','armenien','armenie','arménie','armenië'],
'Azerbaijan':['azerbaijan','azerbaidjan','aserbaidschan','azerbaigian','azerbaiyan','azerbeidzjan'],
'Kosovo':['kosovo','kosova'],
'Liechtenstein':['liechtenstein'],
'Monaco':['monaco','monako'],
'San Marino':['san marino'],
'Vatican City':['vatican city','vatican','vatikanstadt','cite du vatican','cité du vatican','citta del vaticano','città del vaticano','ciudad del vaticano','vaticaanstad'],
}

# IMPORTANT: this module must not re-normalize arbitrary coin text. The resolver
# and backend already have carefully tuned norm() functions for fractions,
# currencies, accents and numismatic words. We therefore use a lightweight,
# Unicode-preserving normalization ONLY to build/match country aliases and
# replace the matched country phrase in the original normalized text.
def _key(s:str)->str:
    s=str(s or '').casefold().replace('_',' ')
    s=re.sub(r'\s+',' ',s).strip()
    return s

_ALIAS_TO_CANON={}
for canon,aliases in ALIASES.items():
    for alias in [canon,*aliases]:
        k=_key(alias)
        if k and k not in _ALIAS_TO_CANON: _ALIAS_TO_CANON[k]=canon

# Add accentless equivalents as additional MATCH KEYS without changing the
# text passed through the function. This catches input already accent-stripped
# by coin_identity_resolver.norm() while preserving e.g. Jubiläum in backend
# title/theme matching.
def _accentless(s:str)->str:
    import unicodedata
    d=unicodedata.normalize('NFKD',s)
    return ''.join(c for c in d if not unicodedata.combining(c))
for alias,canon in list(_ALIAS_TO_CANON.items()):
    k=_key(_accentless(alias))
    _ALIAS_TO_CANON.setdefault(k,canon)

_long_aliases=sorted((a for a in _ALIAS_TO_CANON if len(a)>=4),key=len,reverse=True)
_ALIAS_RE=re.compile(r'(?<!\w)(?:'+ '|'.join(re.escape(a) for a in _long_aliases) +r')(?!\w)',re.I)

def canonical_country_name(value:str):
    n=_key(value)
    if n in _ALIAS_TO_CANON:return _ALIAS_TO_CANON[n]
    m=_ALIAS_RE.search(n)
    return _ALIAS_TO_CANON.get(_key(m.group(0))) if m else None

def normalize_country_aliases_in_text(text:str)->str:
    # Input is already normalized by the caller. Preserve every non-country
    # character exactly; only replace country-name spans with canonical English.
    s=str(text or '')
    def repl(m):
        return _ALIAS_TO_CANON.get(_key(m.group(0)),m.group(0)).casefold()
    return re.sub(r'\s+',' ',_ALIAS_RE.sub(repl,s)).strip()
