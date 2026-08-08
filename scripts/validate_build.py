#!/usr/bin/env python3
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/'data'

def load(n):
    return json.load(open(DATA/n,encoding='utf-8'))

required_files=['meta.json','counts.json','funds.json','funds-lite.json','variants.json','analytics.json','nfos.json','ter.json','amc_aum.json','source_health.json','changes.json','sebi_pipeline.json','registry.json','product_watch.json','alerts.json','deep_metrics.json','deep_cursor.json','amc_web_registry.json','amc_watch.json','amc_nfo_observations.json','amc_source_health.json']
errors=[]
for n in required_files:
    p=DATA/n
    if not p.exists() or p.stat().st_size < 2:
        errors.append(f'missing/empty {n}')
        continue
    try: json.load(open(p,encoding='utf-8'))
    except Exception as e: errors.append(f'invalid JSON {n}: {e}')
if errors:
    print(json.dumps({'errors':errors},indent=2)); sys.exit(1)

counts=load('counts.json'); funds=load('funds.json'); lite=load('funds-lite.json'); variants=load('variants.json'); analytics=load('analytics.json'); nfos=load('nfos.json'); ter=load('ter.json'); changes=load('changes.json'); alerts=load('alerts.json'); amc_watch=load('amc_watch.json'); amc_registry=load('amc_web_registry.json')
if len(funds)!=counts['underlyingFunds']: errors.append(f"fund count {len(funds)} != {counts['underlyingFunds']}")
if len(lite)!=len(funds): errors.append(f"lite fund count {len(lite)} != full fund count {len(funds)}")
if len(variants)!=counts['schemeRecords']: errors.append(f"variant count {len(variants)} != {counts['schemeRecords']}")
if sum(1 for v in variants if v.get('active'))!=counts['activeSchemeRecords']: errors.append('active variant count mismatch')
if sum(counts['planActive'].values())!=counts['activeSchemeRecords']: errors.append('plan bifurcation mismatch')
if sum(counts['optionActive'].values())!=counts['activeSchemeRecords']: errors.append('option bifurcation mismatch')
if counts['planActive'].get('Direct',0)<5000 or counts['planActive'].get('Regular',0)<3000: errors.append('plan counts unexpectedly low')
if len(analytics.get('byCode',{}))<3000: errors.append('analytics coverage unexpectedly low')
if len(nfos.get('items',[]))+len(nfos.get('watch',[]))<5: errors.append('NFO universe unexpectedly empty')
if not isinstance(alerts.get('items',[]),list): errors.append('alerts items is not a list')
if not isinstance(changes.get('items',[]),list): errors.append('changes items is not a list')
# Both compatibility arrays are required by the browser UI.
if not isinstance(changes.get('newSchemeCodes',[]),list) or not isinstance(changes.get('nameChanges',[]),list): errors.append('changes compatibility arrays missing')
required={'id','amc','name','category','schemeType','active','repCode'}
for f in funds[:200]:
    miss=required-set(f)
    if miss: errors.append(f"missing fund keys {sorted(miss)}"); break
lite_allowed={'id','amc','name','schemeType','category','asset','active','activeVariantCount','directGrowthCode','regularGrowthCode','otherGrowthCode','repCode','repPlan','nav','navDate','latestAum','aumDate','aaum','aaumQuarter','launchDate','deepDataAt'}
extra=set().union(*(set(x) for x in lite[:200]))-lite_allowed if lite else set()
if extra: errors.append(f'lite payload contains unexpected heavy keys: {sorted(extra)}')

report={
 'underlyingFunds':len(funds),'activeUnderlyingFunds':counts['activeUnderlyingFunds'],
 'schemeRecords':len(variants),'activeSchemeRecords':counts['activeSchemeRecords'],
 'planActive':counts['planActive'],'optionActive':counts['optionActive'],'amcs':counts['amcs'],
 'analyticsCodes':len(analytics.get('byCode',{})),
 'stdDevCodes':sum(1 for v in analytics.get('byCode',{}).values() if v.get('stdDev3Y') is not None),
 'sortinoCodes':sum(1 for v in analytics.get('byCode',{}).values() if v.get('sortino3Y') is not None),
 'nfoRecords':len(nfos.get('items',[])),'nfoWatch':len(nfos.get('watch',[]))+len(nfos.get('newsWatch',[])),
 'schemeAlerts':len(alerts.get('items',[])),
 'officialAMCWebsites':amc_registry.get('count',0),'officialAMCLeads':amc_watch.get('count',0),
 'terRecords':len(ter.get('byName',{})) if isinstance(ter,dict) and ter.get('byName') else len(ter.get('items',[])) if isinstance(ter,dict) else 0,
 'fundsLiteBytes':(DATA/'funds-lite.json').stat().st_size,
 'fundsFullBytes':(DATA/'funds.json').stat().st_size,
 'errors':errors
}
print(json.dumps(report,indent=2))
if errors: sys.exit(1)
