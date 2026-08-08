#!/usr/bin/env python3
"""Daily analytics refresh from historical NAV parquet.
Computes plan-level CAGR, standard deviation, Sharpe, Sortino, downside deviation,
max drawdown, Calmar and rolling-return summary using daily/monthly NAV history.
"""
from __future__ import annotations
import json, math, os, sys, tempfile, time
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests
import duckdb

ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/'data'; TMP=ROOT/'.refresh_tmp'; TMP.mkdir(exist_ok=True)
HISTORY_URLS=[
 'https://media.githubusercontent.com/media/InertExpert2911/Mutual_Fund_Data/refs/heads/main/mutual_fund_nav_history.parquet',
 'https://github.com/InertExpert2911/Mutual_Fund_Data/raw/refs/heads/main/mutual_fund_nav_history.parquet'
]
RF=0.065
NOW=datetime.now(timezone.utc)

def download_history(path):
    last=None
    for url in HISTORY_URLS:
        try:
            with requests.get(url,stream=True,timeout=(20,180),headers={'User-Agent':'MoneyMantra9-MF-Analytics/7.0'}) as r:
                r.raise_for_status(); size=0
                with open(path,'wb') as f:
                    for chunk in r.iter_content(1024*1024):
                        if chunk:f.write(chunk);size+=len(chunk)
            if size>5_000_000:return url,size
        except Exception as e:last=e
    raise RuntimeError(f'Historical NAV download failed: {last}')

def load_codes():
    funds=json.load(open(DATA/'funds.json',encoding='utf-8'))
    codes=[]
    for f in funds:
        for k in ('directGrowthCode','regularGrowthCode','otherGrowthCode'):
            c=f.get(k)
            if c:codes.append(int(c))
    return sorted(set(codes))

def risk_stats(g):
    g=g.sort_values('dt'); nav=g['nav'].astype(float).to_numpy(); dates=pd.to_datetime(g['dt'])
    if len(nav)<120:return {}
    rets=pd.Series(nav).pct_change().dropna().to_numpy()
    if len(rets)<100:return {}
    years=max((dates.iloc[-1]-dates.iloc[0]).days/365.25,1e-9)
    ann=(nav[-1]/nav[0])**(1/years)-1 if nav[0]>0 else np.nan
    sd=np.std(rets,ddof=1)*math.sqrt(252)
    rf_daily=(1+RF)**(1/252)-1
    downside=np.minimum(rets-rf_daily,0)
    ddev=math.sqrt(np.mean(downside**2))*math.sqrt(252)
    peak=np.maximum.accumulate(nav); dd=nav/peak-1; mdd=float(np.min(dd))
    return {
      'annualReturn3Y':None if not np.isfinite(ann) else ann*100,
      'stdDev3Y':None if not np.isfinite(sd) else sd*100,
      'downsideDev3Y':None if not np.isfinite(ddev) else ddev*100,
      'sharpe3Y':None if not np.isfinite(sd) or sd<=0 else (ann-RF)/sd,
      'sortino3Y':None if not np.isfinite(ddev) or ddev<=0 else (ann-RF)/ddev,
      'maxDrawdown3Y':mdd*100,
      'calmar3Y':None if mdd>=0 else ann/abs(mdd),
      'riskObs':int(len(rets)),
      'riskStart':dates.iloc[0].strftime('%Y-%m-%d'),'riskEnd':dates.iloc[-1].strftime('%Y-%m-%d')
    }

def nearest_start(dates,target,max_days=50):
    # dates ndarray datetime64[ns], return nearest prior/next monthly observation to target
    pos=np.searchsorted(dates,target)
    cand=[]
    if pos<len(dates):cand.append(pos)
    if pos>0:cand.append(pos-1)
    if not cand:return None
    best=min(cand,key=lambda i:abs((pd.Timestamp(dates[i])-pd.Timestamp(target)).days))
    return best if abs((pd.Timestamp(dates[best])-pd.Timestamp(target)).days)<=max_days else None

def monthly_stats(g):
    g=g.sort_values('dt'); dates=pd.to_datetime(g['dt']).to_numpy(); nav=g['nav'].astype(float).to_numpy()
    out={}
    if len(nav)<2:return out
    end_date=pd.Timestamp(dates[-1]); end_nav=nav[-1]
    for y in (1,3,5,10,15):
        target=end_date-pd.DateOffset(years=y); i=nearest_start(dates,target)
        val=None
        if i is not None and nav[i]>0:
            years=(end_date-pd.Timestamp(dates[i])).days/365.25
            if years>0.7*y: val=((end_nav/nav[i])**(1/years)-1)*100
        out[f'cagr{y}Y']=val
    for y in (1,3,5):
        vals=[]
        for j in range(len(nav)):
            ed=pd.Timestamp(dates[j]); target=ed-pd.DateOffset(years=y); i=nearest_start(dates[:j+1],target)
            if i is None or i>=j or nav[i]<=0:continue
            years=(ed-pd.Timestamp(dates[i])).days/365.25
            if years<0.85*y:continue
            vals.append(((nav[j]/nav[i])**(1/years)-1)*100)
        if vals:
            a=np.array(vals,dtype=float)
            out[f'rolling{y}Y']={'latest':float(a[-1]),'average':float(np.mean(a)),'median':float(np.median(a)),'minimum':float(np.min(a)),'maximum':float(np.max(a)),'positivePct':float(np.mean(a>0)*100),'observations':int(len(a))}
        else:out[f'rolling{y}Y']=None
    out['historyStart']=pd.Timestamp(dates[0]).strftime('%Y-%m-%d');out['historyEnd']=end_date.strftime('%Y-%m-%d');out['monthlyObs']=len(nav)
    return out

def main():
    codes=load_codes(); hist=TMP/'nav_history.parquet'; url,size=download_history(hist)
    con=duckdb.connect(); code_df=pd.DataFrame({'code':codes});con.register('codes_df',code_df)
    # discover columns and cast defensively
    base="""SELECT CAST(p.Scheme_Code AS BIGINT) code, CAST(p.Date AS DATE) dt, CAST(p.NAV AS DOUBLE) nav
             FROM read_parquet(?) p JOIN codes_df c ON CAST(p.Scheme_Code AS BIGINT)=c.code
             WHERE CAST(p.NAV AS DOUBLE)>0"""
    daily=con.execute(base+" AND CAST(p.Date AS DATE)>=CURRENT_DATE-INTERVAL '3 years' ORDER BY code,dt",[str(hist)]).df()
    monthly=con.execute("""WITH h AS ("""+base+""" AND CAST(p.Date AS DATE)>=CURRENT_DATE-INTERVAL '15 years'),
      m AS (SELECT code,date_trunc('month',dt) mon,arg_max(nav,dt) nav,max(dt) dt FROM h GROUP BY code,mon)
      SELECT code,dt,nav FROM m ORDER BY code,dt""",[str(hist)]).df()
    result={}
    for code,g in daily.groupby('code',sort=False): result[str(int(code))]=risk_stats(g)
    for code,g in monthly.groupby('code',sort=False): result.setdefault(str(int(code)),{}).update(monthly_stats(g))
    for v in result.values(): v.update({'source':'Daily NAV analytics from InertExpert2911/Mutual_Fund_Data historical parquet','asOf':NOW.date().isoformat()})
    payload={'generatedAt':NOW.isoformat(),'riskFreeRate':RF,'historySource':url,'historyBytes':size,'schemeCodes':len(result),'methodology':{'standardDeviation':'3Y daily simple-return standard deviation annualised using sqrt(252)','sharpe':'(3Y annualised CAGR - 6.5% risk-free rate) / annualised standard deviation','sortino':'(3Y annualised CAGR - 6.5% risk-free rate) / annualised downside deviation relative to daily risk-free rate','maxDrawdown':'3Y peak-to-trough decline from daily NAV','calmar':'3Y annualised CAGR / absolute max drawdown','rolling':'Month-end NAV observations with 1Y/3Y/5Y annualised rolling CAGR'},'byCode':result}
    tmp=DATA/'analytics.json.tmp';tmp.write_text(json.dumps(payload,separators=(',',':'),ensure_ascii=False),encoding='utf-8');tmp.replace(DATA/'analytics.json')
    meta=json.load(open(DATA/'meta.json',encoding='utf-8'));meta['analyticsRefreshAt']=NOW.isoformat();meta['analyticsCodes']=len(result);(DATA/'meta.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
    print(json.dumps({'codesRequested':len(codes),'codesComputed':len(result),'dailyRows':len(daily),'monthlyRows':len(monthly),'historyBytes':size},indent=2))

if __name__=='__main__':main()
