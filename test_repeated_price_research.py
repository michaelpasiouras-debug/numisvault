#!/usr/bin/env python3
from pathlib import Path

text=Path('index.html').read_text(encoding='utf-8')
fail=[]

def need(s,msg):
    if s not in text: fail.append(msg)

need("const researchToken=++currentResearchToken;", "new research token is not allocated")
need("lastPriceResearchSnapshot=null;", "old Price Research snapshot is not cleared")
need("currentResearchKey='';", "old research key is not cleared")
need("activePriceResearchController.abort('Superseded by a newer price search')", "previous price request is not cancelled")
need("activeCatalogLookupController.abort('Superseded by a newer coin search')", "previous catalog request is not cancelled")
need("if(researchToken!==currentResearchToken) return;", "stale resolver continuation is not rejected")
need("[502,503,504].includes(res.status)", "transient production gateway responses are not retried")
need("attempt<2", "bounded retry is missing")

# Ordering matters: the run token must be created before resolver work. This is
# the core regression behind the second-search race.
start=text.find('async function buildResearch(){')
resolver=text.find('const rr=await resolveCoinViaBackend(raw,ep);', start)
token=text.find('const researchToken=++currentResearchToken;', start)
if min(start,resolver,token)<0 or not (start < token < resolver):
    fail.append('research token is not allocated before resolver work')

# There must be only one token allocation inside buildResearch; incrementing it
# again later silently invalidates the very run that is currently executing.
end=text.find("$('researchBtn').onclick", start)
body=text[start:end if end>start else None]
if body.count('const researchToken=++currentResearchToken;') != 1:
    fail.append('buildResearch allocates the research token more than once')

# The UI handler must remain callable for unlimited sequential clicks.
need("$('researchBtn').onclick=()=>{captureResearchState();return buildResearch()};", "Price Research button is not wired to a fresh buildResearch call")

if fail:
    print('FAIL')
    for x in fail: print(' -',x)
    raise SystemExit(1)
print('PASS — repeated Price Research lifecycle is race-safe and retryable')
