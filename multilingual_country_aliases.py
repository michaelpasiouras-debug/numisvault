from __future__ import annotations
import re, unicodedata

# Shared country-name normalization for CoinBids input AND marketplace titles.
# Focus: names commonly encountered on European numismatic marketplaces.
# Canonical values match CoinBids' English country names.
ALIASES={
'Greece':['greece','greek','hellas','ellada','ellas','ελλαδα','ελλάδα','griechenland','griekenland','grece','grèce','grecia','grécia','grecja','recko','řecko','grecko','grécko','grcka','grčka','görögország','gorogorszag','yunanistan','grækenland','graekenland','grekland','kreikka'],
'Germany':['germany','deutschland','allemagne','alemania','germania','duitsland','germanie','alemanha','niemcy','nemecko','německo','nemacka','njemačka','nemecka','tyskland','saksa'],
'France':['france','frankreich','francia','frança','franca','frankrijk','frankrig','frankrike','ranska','francja','francie','francuska','francúzsko'],
'Italy':['italy','italia','italien','italie','italië','italie','italija','wlochy','włochy','italsko','italien','itália','italia'],
'Spain':['spain','espana','españa','spanien','espagne','spagna','spanje','hiszpania','spanelsko','španělsko','spanija','španija','espanha','spanien'],
'Portugal':['portugal','portugalia','portugalsko','portugalska','portogallo'],
'Netherlands':['netherlands','nederland','holland','niederlande','pays bas','pays-bas','paesi bassi','paises bajos','países bajos','holanda','niderlandy','nizozemsko','nizozemska','nizozemsko'],
'Belgium':['belgium','belgie','belgië','belgique','belgien','belgio','belgica','bélgica','belgia','belgie'],
'Austria':['austria','österreich','osterreich','autriche','austria','oostenrijk','austria','austrija','ausztria','avusturya'],
'Switzerland':['switzerland','schweiz','suisse','svizzera','suiza','zwitserland','szwajcaria','švýcarsko','svajcarska','švajčiarsko','svicarska','švicarska','isvicre'],
'United Kingdom':['united kingdom','great britain','britain','uk','england','grossbritannien','großbritannien','royaume uni','royaume-uni','regno unito','reino unido','verenigd koninkrijk','wielka brytania','velka britanie','velká británie','velka britania','velika britanija'],
'United States':['united states','united states of america','usa','u.s.a.','vereinigte staaten','etats unis','états-unis','stati uniti','estados unidos','verenigde staten','stany zjednoczone','spojene staty','spojené státy','sjedinjene drzave','sjedinjene države','abd'],
'Poland':['poland','polska','polen','pologne','polonia','polen','polsko'],
'Czech Republic':['czech republic','czechia','cesko','česko','tschechien','republique tcheque','république tchèque','repubblica ceca','republica checa','república checa','tsjechie','czechy'],
'Slovakia':['slovakia','slovensko','slowakei','slovaquie','slovacchia','eslovaquia','slowakije'],
'Hungary':['hungary','magyarorszag','magyarország','ungarn','hongrie','ungheria','hungria','hongarije','wegry','węgry','madarsko','maďarsko'],
'Romania':['romania','românia','rumanien','roumanie','romenia','rumania','roemenie','rumunia','rumunsko'],
'Bulgaria':['bulgaria','bulgarien','bulgarie','bulgaria','bulgarije','bulgaria','bułgaria','bulharsko'],
'Croatia':['croatia','hrvatska','kroatien','croatie','croazia','croacia','kroatie','chorwacja','chorvatsko'],
'Serbia':['serbia','srbija','serbien','serbie','serbia','servië','servie','serbia','serbia'],
'Slovenia':['slovenia','slovenija','slowenien','slovenie','slovénie','slovenia','slovenie','slowenia','slovinsko'],
'Bosnia and Herzegovina':['bosnia and herzegovina','bosna i hercegovina','bosnien und herzegowina','bosnie herzegovine','bosnie-herzégovine','bosnia ed erzegovina','bosnia y herzegovina'],
'Montenegro':['montenegro','crna gora'],
'North Macedonia':['north macedonia','macedonia del norte','nordmazedonien','macedoine du nord','macédoine du nord','macedonia del nord','noord macedonie','noord-macedonië','severna makedonija'],
'Albania':['albania','shqiperia','shqipëria','albanien','albanie','albania','albanië','albanië','albania'],
'Cyprus':['cyprus','kypros','κύπρος','zypern','chypre','cipro','chipre','cyprus','cypr'],
'Malta':['malta'],
'Ireland':['ireland','eire','éire','irland','irlande','irlanda','ierland','irlandia','irsko'],
'Denmark':['denmark','danmark','dänemark','danemark','danemark','danemarca','denemarken','dania','dansko'],
'Sweden':['sweden','sverige','schweden','suede','suède','svezia','suecia','zweden','szwecja','svedsko','švédsko'],
'Norway':['norway','norge','noreg','norwegen','norvege','norvège','norvegia','noruega','noorwegen','norwegia','norsko'],
'Finland':['finland','suomi','finnland','finlande','finlandia','finland','finlandia','finsko'],
'Iceland':['iceland','island','ísland','islande','islanda','ijsland','islandia'],
'Estonia':['estonia','eesti','estland','estonie','estonia','estland','estonia'],
'Latvia':['latvia','latvija','lettland','lettonie','lettonia','letonia','letland','lotwa','łotwa'],
'Lithuania':['lithuania','lietuva','litauen','lituanie','lituania','litouwen','litwa'],
'Luxembourg':['luxembourg','luxemburg','letzebuerg','lëtzebuerg','luxemburgo'],
'Turkey':['turkey','turkiye','türkiye','turkei','turquie','turchia','turquia','turkije','turcja','turecko'],
'Russia':['russia','rossiya','россия','russland','russie','russia','rusia','rusland','rosja','rusko'],
'Ukraine':['ukraine','ukraina','україна','ukraina','ukraine'],
'Georgia':['georgia','sakartvelo','საქართველო','georgien','georgie','géorgie','georgia','georgië','gruzja'],
'Armenia':['armenia','hayastan','հայաստան','armenien','armenie','arménie','armenia','armenië'],
'Azerbaijan':['azerbaijan','azerbaidjan','aserbaidschan','azerbaigian','azerbaiyan','azerbeidzjan'],
'Kosovo':['kosovo','kosova'],
'Liechtenstein':['liechtenstein'],
'Monaco':['monaco','monako'],
'San Marino':['san marino'],
'Vatican City':['vatican city','vatican','vatikanstadt','cite du vatican','cité du vatican','citta del vaticano','città del vaticano','ciudad del vaticano','vaticaanstad'],
}

def _norm(s:str)->str:
    s=unicodedata.normalize('NFKD',str(s or '').casefold())
    s=''.join(c for c in s if not unicodedata.combining(c))
    s=re.sub(r'[^a-z0-9α-ωа-яёіїєґ\s.-]+',' ',s,flags=re.I)
    return re.sub(r'\s+',' ',s).strip()

_ALIAS_TO_CANON={}
for canon,aliases in ALIASES.items():
    for alias in [canon,*aliases]:
        k=_norm(alias)
        if k and k not in _ALIAS_TO_CANON: _ALIAS_TO_CANON[k]=canon
_sorted_aliases=sorted(_ALIAS_TO_CANON,key=len,reverse=True)

def canonical_country_name(value:str):
    n=_norm(value)
    if n in _ALIAS_TO_CANON:return _ALIAS_TO_CANON[n]
    # Whole phrase inside a listing/title; longest aliases first.
    for a in _sorted_aliases:
        if len(a)<4: continue
        if re.search(r'(?<![a-z0-9])'+re.escape(a)+r'(?![a-z0-9])',n):
            return _ALIAS_TO_CANON[a]
    return None

def normalize_country_aliases_in_text(text:str)->str:
    n=_norm(text)
    # Replace only full alias phrases. This intentionally runs after normal
    # text cleanup in resolver/backend norm() and is idempotent.
    for a in _sorted_aliases:
        if len(a)<4: continue
        canon=_norm(_ALIAS_TO_CANON[a])
        n=re.sub(r'(?<![a-z0-9])'+re.escape(a)+r'(?![a-z0-9])',canon,n)
    return re.sub(r'\s+',' ',n).strip()
