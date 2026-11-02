const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('index.html','utf8');
const slice=(start,end)=>html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start)));
const nodes={};const node=id=>nodes[id]??=( {value:'',innerHTML:'',textContent:''});
const ctx=vm.createContext({console,Promise,AbortController,$:node,
 toEnglishTerms:t=>({...t,country:t.countryEN||t.country}),normLite:s=>String(s).toLowerCase(),
 currentResearchToken:0, money:x=>String(x),esc:String});
vm.runInContext(slice('function researchIdentityDecision(','async function resolveCoinViaBackend('),ctx);
const local={country:'Greece',countryEN:'Greece',denom:'5 drachma',year:'1901'};
const decision=(data,t=local)=>ctx.researchIdentityDecision(t,'original issue description',data);
assert.equal(decision(null).ok,true);
assert.equal(decision({status:'unresolved',best:null,candidates:[]}).ok,true);
assert.equal(decision({status:'review',ambiguous:true}).ok,false);
assert.equal(decision({status:'review',best:{country:'Serbia'}}).ok,false);
assert.equal(decision({status:'review',best:{country:'Greece',reasons:['year before currency period']}}).ok,false);
assert.equal(decision({status:'review',negative_flags:['BANKNOTE']}).ok,false);
assert.equal(decision(null,{country:'',denom:'2 euro',year:'2022'}).ok,false);
const resolved=decision({status:'resolved',best:{country:'Greece',year:1901,denomination_value:5,currency:'Greek drachma'}});
assert.equal(resolved.parsed.raw,'original issue description');
assert.equal(resolved.parsed.countryEN,'Greece');
assert.equal(ctx.marketAvailabilityMessage({provider_statuses:{'MA-Shops':'error'}}).title,'Market source unavailable.');
assert.equal(ctx.marketAvailabilityMessage({provider_statuses:{'MA-Shops':'ok'},offers:[]}),null);

(async()=>{
 // Execute the actual orchestration with a stalled price request. Catalogue
 // work must start and complete without waiting for the marketplace.
 let finishPrice, specDone=false;
 const pricePending=new Promise(resolve=>finishPrice=resolve);
 Object.assign(ctx,{lastPriceResearchSnapshot:{},currentResearchKey:'old',activePriceResearchController:null,
 activeCatalogLookupController:null,currentCatalogToken:0,localStorage:{getItem:()=>null},defaultApiEndpoint:()=>'/api/coin-search',
 baseTerms:()=>local,resolveCoinViaBackend:async()=>({ok:true,parsed:local,searchVariants:[],resolution:null}),
 searchVariants:()=>[],providerLinks:()=>[],renderOffers:()=>{},renderOwnedBox:()=>{},checkOwnCollection:()=>[],
 autoResearch:()=>pricePending,fetchCatalogLookup:async()=>{specDone=true}});
 node('rFreeText').value='Greece 5 drachma 1901';node('mWeight').value='999';
 vm.runInContext(slice('async function buildResearch(){',"$('researchBtn').onclick"),ctx);
 const run=ctx.buildResearch();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(specDone,true);assert.equal(node('mWeight').value,'');
 finishPrice();await run;

 // Delayed spot results must not write the previous coin's melt value.
 let finishSpot;
 Object.assign(ctx,{fetchMetalSpotFromBackend:()=>new Promise(resolve=>finishSpot=resolve),
 refreshFineMetalGrams:()=>3.5});
 node('mMetal').value='silver';node('mWeight').value='7';node('mFineness').value='500';
 const start=html.indexOf('async function calculateMainMetalValueAuto(){');
 const end=html.indexOf('\n}',start)+2;
 vm.runInContext(html.slice(start,end),ctx);
 const melt=ctx.calculateMainMetalValueAuto();
 ctx.currentResearchToken++;node('mValue').value='new coin pending';
 finishSpot({silver:30,eur:0.9});await melt;
 assert.equal(node('mValue').value,'new coin pending');
 console.log('PASS: shared identity policy, source errors, independent specs, stale metal protection');
})().catch(e=>{console.error(e);process.exitCode=1});
