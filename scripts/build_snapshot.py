#!/usr/bin/env python3
import argparse, json, math, re, hashlib
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'
DATA.mkdir(exist_ok=True)

OLD_KEYS=[]; OLD_ROWS=[]
OLD_MAP={}
PREV_MAP={}

def s(v):
    if pd.isna(v): return ''
    return str(v).strip()

def num(v):
    if pd.isna(v) or v in ('','-'): return None
    try: return float(v)
    except: return None

def norm_text(v):
    x=s(v).lower().replace('&',' and ')
    x=re.sub(r'\b(asset management company|asset management|amc|mutual fund|private|limited|ltd)\b',' ',x)
    x=re.sub(r'[^a-z0-9]+',' ',x)
    return ' '.join(x.split())

def norm_name(v):
    x=s(v).lower().replace('&',' and ')
    x=re.sub(r'\(formerly.*?\)',' ',x)
    x=re.sub(r'formerly known as.*$',' ',x)
    x=re.sub(r'[^a-z0-9]+',' ',x)
    return ' '.join(x.split())

def asset_class(category,name):
    c=(category or '').lower(); n=(name or '').lower()
    if 'equity scheme' in c: return 'Equity'
    if 'debt scheme' in c or 'income'==c.strip().lower(): return 'Debt'
    if 'hybrid scheme' in c: return 'Hybrid'
    if 'solution oriented' in c: return 'Solution Oriented'
    if 'fund of funds' in c or 'fof' in c or 'fof' in n: return 'Fund of Funds'
    if 'etf' in c or 'etf' in n: return 'ETF'
    if 'index' in c or 'index' in n: return 'Index / Passive'
    if 'gold' in n or 'silver' in n: return 'Commodity'
    return 'Other'

def plan_type(nav_name,category=''):
    n=(nav_name or '').lower(); c=(category or '').lower()
    if 'direct' in n: return 'Direct'
    if 'regular' in n: return 'Regular'
    if 'institutional' in n: return 'Institutional'
    if 'retail' in n: return 'Retail'
    if 'etf' in n or 'other etf' in c or 'other  etf' in c: return 'ETF / Exchange'
    return 'Legacy / Other'

def option_type(nav_name):
    n=(nav_name or '').lower()
    if 'growth' in n: return 'Growth'
    if re.search(r'\bidcw\b|dividend',n): return 'IDCW / Dividend'
    if 'bonus' in n: return 'Bonus'
    if 'segregated' in n: return 'Segregated Portfolio'
    return 'Other'

def date_iso(v):
    if pd.isna(v) or not s(v): return None
    try:
        d=pd.to_datetime(v,errors='coerce')
        if pd.isna(d): return None
        return d.strftime('%Y-%m-%d')
    except: return None

def load_old_payload(path):
    global OLD_KEYS, OLD_ROWS, OLD_MAP
    if not path or not Path(path).exists(): return
    d=json.load(open(path,encoding='utf-8'))
    OLD_KEYS=d.get('keys',[]); OLD_ROWS=d.get('rows',[])
    ix={k:i for i,k in enumerate(OLD_KEYS)}
    for r in OLD_ROWS:
        name=r[ix['name']] if 'name' in ix else ''
        amc=r[ix['amc']] if 'amc' in ix else ''
        key=(norm_name(name),norm_text(amc))
        OLD_MAP[key]=(r,ix)
    # fallback by unique scheme name
    byname={}
    for r in OLD_ROWS:
        name=norm_name(r[ix['name']]); byname.setdefault(name,[]).append((r,ix))
    for name,vals in byname.items():
        if len(vals)==1: OLD_MAP[(name,'*')]=vals[0]


def load_previous_funds(path):
    global PREV_MAP
    if not path or not Path(path).exists(): return
    try:
        arr=json.load(open(path,encoding='utf-8'))
    except Exception:
        return
    for f in arr if isinstance(arr,list) else []:
        key=(norm_name(f.get('name')),norm_text(f.get('amc')))
        PREV_MAP[key]=f
        PREV_MAP.setdefault((norm_name(f.get('name')),'*'),f)

def prev_val(name,amc,key):
    f=PREV_MAP.get((norm_name(name),norm_text(amc))) or PREV_MAP.get((norm_name(name),'*'))
    return f.get(key) if f else None

def keep(name,amc,key,oldkey=None):
    v=prev_val(name,amc,key)
    if v is not None and v!='': return v
    return old_val(name,amc,oldkey or key)

def old_val(name,amc,key):
    hit=OLD_MAP.get((norm_name(name),norm_text(amc))) or OLD_MAP.get((norm_name(name),'*'))
    if not hit: return None
    r,ix=hit
    return r[ix[key]] if key in ix and ix[key]<len(r) else None

def fid(amc,name):
    h=hashlib.sha1((norm_text(amc)+'|'+norm_name(name)).encode()).hexdigest()[:12]
    return 'MF-'+h

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--csv',required=True)
    ap.add_argument('--old-payload')
    ap.add_argument('--previous-funds')
    ap.add_argument('--source-label',default='Daily scheme snapshot')
    args=ap.parse_args()
    load_old_payload(args.old_payload)
    load_previous_funds(args.previous_funds)
    df=pd.read_csv(args.csv,low_memory=False)
    # Coerce critical columns if absent
    for col in ['Scheme_Code','Scheme_Name','AMC','Scheme_Type','Scheme_Category','Scheme_NAV_Name','Scheme_Min_Amt','NAV','Latest_NAV_Date','Average_AUM_Cr','AAUM_Quarter','ISIN_Div_Payout/Growth','ISIN_Div_Reinvestment','Launch_Date','Closure_Date']:
        if col not in df.columns: df[col]=None
    df['_plan']=[plan_type(s(n),s(c)) for n,c in zip(df['Scheme_NAV_Name'],df['Scheme_Category'])]
    df['_option']=[option_type(s(n)) for n in df['Scheme_NAV_Name']]
    df['_active']=df['NAV'].notna()
    df['_fundkey']=[norm_text(a)+'|'+norm_name(n) for a,n in zip(df['AMC'],df['Scheme_Name'])]

    variants=[]
    for _,r in df.iterrows():
        variants.append({
            'code':int(r['Scheme_Code']) if not pd.isna(r['Scheme_Code']) else None,
            'amc':s(r['AMC']),'baseName':s(r['Scheme_Name']),'name':s(r['Scheme_NAV_Name']),
            'schemeType':s(r['Scheme_Type']),'category':s(r['Scheme_Category']),
            'asset':asset_class(s(r['Scheme_Category']),s(r['Scheme_Name'])),
            'plan':r['_plan'],'option':r['_option'],'active':bool(r['_active']),
            'nav':num(r['NAV']),'navDate':date_iso(r['Latest_NAV_Date']),
            'aaum':num(r['Average_AUM_Cr']),'aaumQuarter':s(r['AAUM_Quarter']) or None,
            'minInvestment':num(r['Scheme_Min_Amt']),
            'isin':None if s(r['ISIN_Div_Payout/Growth']) in ('','-') else s(r['ISIN_Div_Payout/Growth']),
            'isinReinvest':None if s(r['ISIN_Div_Reinvestment']) in ('','-') else s(r['ISIN_Div_Reinvestment']),
            'launchDate':date_iso(r['Launch_Date']),'closureDate':date_iso(r['Closure_Date'])
        })

    funds=[]
    for (amc,name),g in df.groupby(['AMC','Scheme_Name'],dropna=False,sort=True):
        amc=s(amc); name=s(name)
        recs=[]
        for _,r in g.iterrows():
            recs.append({
                'code':int(r['Scheme_Code']) if not pd.isna(r['Scheme_Code']) else None,
                'name':s(r['Scheme_NAV_Name']),'plan':r['_plan'],'option':r['_option'],'active':bool(r['_active']),
                'nav':num(r['NAV']),'navDate':date_iso(r['Latest_NAV_Date']),'aaum':num(r['Average_AUM_Cr']),
                'aaumQuarter':s(r['AAUM_Quarter']) or None,'minInvestment':num(r['Scheme_Min_Amt']),
                'isin':None if s(r['ISIN_Div_Payout/Growth']) in ('','-') else s(r['ISIN_Div_Payout/Growth'])
            })
        active=[x for x in recs if x['active']]
        def choose(plan,option='Growth'):
            xs=[x for x in active if x['plan']==plan and x['option']==option]
            return xs[0] if xs else None
        dg=choose('Direct'); rg=choose('Regular')
        og=next((x for x in active if x['option']=='Growth' and x['plan'] not in ('Direct','Regular')),None)
        rep=dg or rg or og or (active[0] if active else recs[0])
        aaums=[x['aaum'] for x in recs if x['aaum'] is not None]
        qtrs=[x['aaumQuarter'] for x in recs if x['aaumQuarter']]
        launches=[date_iso(x) for x in g['Launch_Date'] if date_iso(x)]
        closures=[date_iso(x) for x in g['Closure_Date'] if date_iso(x)]
        category=s(g['Scheme_Category'].dropna().iloc[0]) if g['Scheme_Category'].notna().any() else ''
        scheme_type=s(g['Scheme_Type'].dropna().iloc[0]) if g['Scheme_Type'].notna().any() else ''
        f={
            'id':fid(amc,name),'amc':amc,'name':name,'schemeType':scheme_type,'category':category,
            'asset':asset_class(category,name),'active':bool(active),'variantCount':len(recs),'activeVariantCount':len(active),
            'directGrowthCode':dg['code'] if dg else None,'regularGrowthCode':rg['code'] if rg else None,
            'otherGrowthCode':og['code'] if og else None,'repCode':rep['code'],'repPlan':rep['plan'],
            'nav':rep['nav'],'navDate':rep['navDate'],'latestAum':None,'aumDate':None,
            'aaum':sum(aaums) if aaums else None,'aaumQuarter':qtrs[0] if qtrs else None,
            'launchDate':min(launches) if launches else None,'closureDate':max(closures) if closures else None,
            'minInvestment':min([x['minInvestment'] for x in recs if x['minInvestment'] is not None],default=None),
            'benchmark':keep(name,amc,'benchmark'),'benchmarkConfidence':keep(name,amc,'benchmarkConfidence','benchConfidence'),
            'riskometer':keep(name,amc,'riskometer'),'portfolioMonth':keep(name,amc,'portfolioMonth'),
            'pe':keep(name,amc,'pe'),'pb':keep(name,amc,'pb'),'turnover':keep(name,amc,'turnover'),
            'ytm':keep(name,amc,'ytm'),'modifiedDuration':keep(name,amc,'modifiedDuration','modDuration'),'averageMaturity':keep(name,amc,'averageMaturity','avgMaturity'),
            'equityAllocation':keep(name,amc,'equityAllocation','equityAlloc'),'debtAllocation':keep(name,amc,'debtAllocation','debtAlloc'),'cashAllocation':keep(name,amc,'cashAllocation','cashAlloc'),
            'sourceSnapshot':args.source_label,
            'planCounts':{str(k):int(v) for k,v in pd.Series([x['plan'] for x in recs]).value_counts().items()},
            'optionCounts':{str(k):int(v) for k,v in pd.Series([x['option'] for x in recs]).value_counts().items()}
        }
        # seed plan-specific analytics/TER from old app until scheduled analytics replaces them
        prev_seed=prev_val(name,amc,'analyticsSeed') or {}
        def sv(plan,key,oldkey):
            v=(prev_seed.get(plan) or {}).get(key)
            return v if v is not None else old_val(name,amc,oldkey)
        seed={
          'direct':{'cagr1Y':sv('direct','cagr1Y','d1'),'cagr3Y':sv('direct','cagr3Y','d3'),'cagr5Y':sv('direct','cagr5Y','d5'),'cagr10Y':sv('direct','cagr10Y','d10'),'cagr15Y':sv('direct','cagr15Y','d15'),'ter':sv('direct','ter','dter'),'stdDev3Y':sv('direct','stdDev3Y','dvol'),'sharpe3Y':sv('direct','sharpe3Y','dsharpe'),'sortino3Y':sv('direct','sortino3Y','dsortino')},
          'regular':{'cagr1Y':sv('regular','cagr1Y','r1'),'cagr3Y':sv('regular','cagr3Y','r3'),'cagr5Y':sv('regular','cagr5Y','r5'),'cagr10Y':sv('regular','cagr10Y','r10'),'cagr15Y':sv('regular','cagr15Y','r15'),'ter':sv('regular','ter','rter'),'stdDev3Y':sv('regular','stdDev3Y','rvol'),'sharpe3Y':sv('regular','sharpe3Y','rsharpe'),'sortino3Y':sv('regular','sortino3Y','rsortino')}
        }
        f['analyticsSeed']=seed
        funds.append(f)

    def count_dict(series): return {str(k):int(v) for k,v in series.value_counts(dropna=False).to_dict().items()}
    active_df=df[df['_active']]
    counts={
        'generatedAt':datetime.now(timezone.utc).isoformat(),
        'sourceDate':max([x for x in [date_iso(v) for v in df['Latest_NAV_Date']] if x],default=None),
        'schemeRecords':int(len(df)),'activeSchemeRecords':int(df['_active'].sum()),'inactiveSchemeRecords':int((~df['_active']).sum()),
        'underlyingFunds':int(len(funds)),'activeUnderlyingFunds':int(sum(1 for f in funds if f['active'])),'amcs':int(df['AMC'].nunique(dropna=True)),
        'planAll':count_dict(df['_plan']),'planActive':count_dict(active_df['_plan']),
        'optionAll':count_dict(df['_option']),'optionActive':count_dict(active_df['_option']),
        'schemeTypeActive':count_dict(active_df['Scheme_Type'].fillna('Unknown')),
        'assetActive':count_dict(pd.Series([asset_class(s(c),s(n)) for c,n in zip(active_df['Scheme_Category'],active_df['Scheme_Name'])])),
        'growthByPlanActive':{
            'Direct':int(((active_df['_plan']=='Direct')&(active_df['_option']=='Growth')).sum()),
            'Regular':int(((active_df['_plan']=='Regular')&(active_df['_option']=='Growth')).sum()),
            'Other':int((~active_df['_plan'].isin(['Direct','Regular'])&(active_df['_option']=='Growth')).sum())
        }
    }
    previous_meta={}
    try:
        previous_meta=json.load(open(DATA/'meta.json',encoding='utf-8'))
    except Exception:
        pass
    meta={
        **previous_meta,
        'schemaVersion':7,'generatedAt':counts['generatedAt'],'sourceDate':counts['sourceDate'],
        'source':args.source_label,'fundsFile':'funds.json','variantsFile':'variants.json','countsFile':'counts.json',
        'analyticsFile':'analytics.json','nfoFile':'nfos.json','terFile':'ter.json',
        'methodology':{'stdDev':'3Y daily returns annualised with sqrt(252)','sharpe':'(3Y CAGR - 6.5% risk-free rate) / annualised standard deviation','sortino':'(3Y CAGR - 6.5% risk-free rate) / annualised downside deviation','rolling':'Monthly observations; annualised CAGR for 1Y/3Y/5Y windows'}
    }
    (DATA/'funds.json').write_text(json.dumps(funds,separators=(',',':'),ensure_ascii=False),encoding='utf-8')
    (DATA/'variants.json').write_text(json.dumps(variants,separators=(',',':'),ensure_ascii=False),encoding='utf-8')
    (DATA/'counts.json').write_text(json.dumps(counts,indent=2,ensure_ascii=False),encoding='utf-8')
    (DATA/'meta.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf-8')
    if not (DATA/'analytics.json').exists(): (DATA/'analytics.json').write_text('{}',encoding='utf-8')
    if not (DATA/'nfos.json').exists(): (DATA/'nfos.json').write_text('[]',encoding='utf-8')
    if not (DATA/'ter.json').exists(): (DATA/'ter.json').write_text('{}',encoding='utf-8')
    print(json.dumps({'funds':len(funds),'variants':len(variants),'counts':counts},indent=2))

if __name__=='__main__': main()
