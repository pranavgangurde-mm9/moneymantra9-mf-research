#!/usr/bin/env python3
"""Rotate through scheme families and refresh slow-changing deep fund fields.

Designed for GitHub Actions.  It is intentionally conservative with mfdata.in
rate limits and never erases previously known values when a source is missing.
"""
from __future__ import annotations

import json, time, re
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
NOW = datetime.now(timezone.utc)
MFDATA_DETAIL = "https://mfdata.in/api/v1/schemes/{code}"
BATCH_SIZE = 80
PAUSE_SECONDS = 2.10  # <= ~28.5 requests/minute across all standard API calls.


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def write(path, obj, indent=None):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":")), encoding="utf-8")


def session():
    s = requests.Session()
    s.headers.update({"User-Agent":"MoneyMantra9-MF-Research/8.0 (+GitHub Actions; deep enrichment)","Accept":"application/json"})
    retry = Retry(total=1, connect=1, read=1, status=1, backoff_factor=0.8,
                  status_forcelist=(408,429,500,502,503,504), allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4))
    return s

S = session()
_LAST_REQUEST = 0.0

def api_json(url, timeout=18):
    """Rate-limited GET respecting mfdata's documented 30 standard requests/minute."""
    global _LAST_REQUEST
    wait = PAUSE_SECONDS - (time.monotonic() - _LAST_REQUEST)
    if wait > 0: time.sleep(wait)
    r = S.get(url, timeout=timeout)
    _LAST_REQUEST = time.monotonic()
    if r.status_code == 429:
        reset = r.headers.get('X-RateLimit-Reset')
        delay = 8.0
        if reset:
            try: delay = max(delay, float(reset)-time.time()+1)
            except Exception: pass
        time.sleep(min(delay, 70))
        r = S.get(url, timeout=timeout); _LAST_REQUEST = time.monotonic()
    r.raise_for_status()
    return r.json()


def source_health_check():
    """Quickly determine whether mfdata is reachable before launching a slow batch.

    A provider outage must not cause 80 long retries or falsely mark stale deep data
    as freshly updated. Returns (ok, detail).
    """
    probes = [
        "https://mfdata.in/health",
        "https://mfdata.in/api/v1/stats",
    ]
    errors=[]
    for url in probes:
        try:
            r=S.get(url,timeout=10)
            if 200 <= r.status_code < 300:
                return True, f"{url} HTTP {r.status_code}"
            errors.append(f"{url} HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"{url}: {type(e).__name__}: {str(e)[:140]}")
    return False, " | ".join(errors)[:500]


def write_unavailable_status(funds, deep, reason):
    """Preserve prior deep values, record a failed source check, and exit cleanly."""
    now=NOW.isoformat()
    prev_bycode=deep.get("byCode",{}) if isinstance(deep,dict) else {}
    write(DATA/"deep_cursor.json",{
        "lastAttemptAt":now,
        "lastRun":load(DATA/"deep_cursor.json",{}).get("lastRun"),
        "batchSize":0,
        "success":0,
        "failure":0,
        "sourceAvailable":False,
        "preservedPrevious":True,
        "reason":reason,
        "policy":"External deep-data source unavailable; previous valid deep fields were retained and not relabelled as fresh."
    },indent=2)
    meta=load(DATA/"meta.json",{})
    meta["schemaVersion"]=8
    meta["deepLastAttemptAt"]=now
    sh=meta.setdefault("sourceHealth",{})
    sh["mfdata-deep"]={
        "ok":False,
        "checkedAt":now,
        "attempted":0,
        "success":0,
        "failure":0,
        "preservedPrevious":True,
        "detail":reason
    }
    # IMPORTANT: do not change deepRefreshAt here; that timestamp means a successful refresh.
    write(DATA/"meta.json",meta,indent=2)
    print(f"::warning::Deep source unavailable. Previous deep data retained. {reason}")
    print(json.dumps({"sourceAvailable":False,"preservedPrevious":True,"deepCodes":len(prev_bycode),"reason":reason},indent=2))


def num(v):
    if v is None or v is False: return None
    if isinstance(v, (int,float)): return float(v)
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(v))
    if not m: return None
    try: return float(m.group(0).replace(",",""))
    except Exception: return None


def scalar(v):
    if isinstance(v, (str,int,float,bool)) or v is None: return v
    if isinstance(v, list):
        vals=[scalar(x) for x in v[:12]]
        return ", ".join(str(x) for x in vals if x not in (None,"")) or None
    if isinstance(v, dict):
        for k in ("name","value","label","text","ratio","percentage"):
            if k in v and not isinstance(v[k], (dict,list)): return v[k]
    return None


def flatten(obj, prefix=""):
    out={}
    if isinstance(obj, dict):
        for k,v in obj.items():
            p=(prefix+"."+str(k) if prefix else str(k)).lower().replace("-","_").replace(" ","_")
            if isinstance(v,(dict,list)):
                out.update(flatten(v,p))
            else: out[p]=v
    elif isinstance(obj, list):
        for i,v in enumerate(obj[:30]): out.update(flatten(v,f"{prefix}.{i}" if prefix else str(i)))
    return out


def pick(flat, *patterns, numeric=False):
    pats=[p.lower() for p in patterns]
    # Exact suffix / key first, then substring.
    hits=[]
    for k,v in flat.items():
        lk=k.lower()
        score=0
        for p in pats:
            if lk==p or lk.endswith("."+p): score=max(score,100+len(p))
            elif p in lk: score=max(score,10+len(p))
        if score: hits.append((score,k,v))
    for _,_,v in sorted(hits, reverse=True):
        if numeric:
            n=num(v)
            if n is not None: return n
        else:
            s=scalar(v)
            if s not in (None,""): return s
    return None


def extract(j):
    root=j.get("data",j) if isinstance(j,dict) else j
    flat=flatten(root)
    out={
        "benchmark": pick(flat,"benchmark","benchmark_name","primary_benchmark"),
        "riskometer": pick(flat,"riskometer","risk_level","risk_rating"),
        "expenseRatio": pick(flat,"expense_ratio","ter","total_expense_ratio",numeric=True),
        "exitLoad": pick(flat,"exit_load"),
        "minInvestment": pick(flat,"minimum_investment","min_investment","minimum_lumpsum",numeric=True),
        "minSip": pick(flat,"minimum_sip","min_sip","sip_minimum",numeric=True),
        "fundManagers": pick(flat,"fund_managers","fund_manager","manager_name"),
        "pe": pick(flat,"portfolio_pe","p_e","pe_ratio",numeric=True),
        "pb": pick(flat,"portfolio_pb","p_b","pb_ratio",numeric=True),
        "turnover": pick(flat,"portfolio_turnover","turnover_ratio",numeric=True),
        "ytm": pick(flat,"yield_to_maturity","ytm",numeric=True),
        "modifiedDuration": pick(flat,"modified_duration",numeric=True),
        "averageMaturity": pick(flat,"average_maturity","avg_maturity",numeric=True),
        "equityAllocation": pick(flat,"equity_allocation","equity_percentage","equity_pct",numeric=True),
        "debtAllocation": pick(flat,"debt_allocation","debt_percentage","debt_pct",numeric=True),
        "cashAllocation": pick(flat,"cash_allocation","cash_percentage","cash_pct",numeric=True),
        "alpha": pick(flat,"alpha",numeric=True),
        "beta": pick(flat,"beta",numeric=True),
        "informationRatio": pick(flat,"information_ratio","info_ratio",numeric=True),
        "trackingError": pick(flat,"tracking_error",numeric=True),
        "standardDeviationSource": pick(flat,"standard_deviation","std_deviation","standard_deviation_3y",numeric=True),
        "sharpeSource": pick(flat,"sharpe_ratio","sharpe",numeric=True),
        "sortinoSource": pick(flat,"sortino_ratio","sortino",numeric=True),
        "rating": pick(flat,"rating","star_rating","morningstar_rating"),
        "familyId": pick(flat,"family_id","scheme_family_id","fund_family_id"),
        "portfolioDate": pick(flat,"portfolio_date","portfolio_as_of","holdings_date"),
        "fetchedAt": NOW.isoformat(),
        "source": "mfdata.in scheme detail",
    }
    return {k:v for k,v in out.items() if v not in (None,"")}


def unwrap(j):
    return j.get("data", j) if isinstance(j, dict) else j


def first_list(obj, *keys):
    if isinstance(obj, list): return obj
    if not isinstance(obj, dict): return []
    for k in keys:
        v=obj.get(k)
        if isinstance(v,list): return v
    return []


def enrich_family(family_id):
    """Fetch portfolio/factsheet fields that are shared across Direct/Regular variants."""
    if family_id in (None, ""): return {}
    fid=str(family_id)
    out={}
    # Allocation
    try:
        a=unwrap(api_json(f"https://mfdata.in/api/v1/families/{fid}/allocation"))
        alloc=a.get("allocation",a) if isinstance(a,dict) else {}
        if isinstance(alloc,dict):
            out["equityAllocation"]=num(alloc.get("equity_pct") or alloc.get("equity"))
            out["debtAllocation"]=num(alloc.get("bond_pct") or alloc.get("debt_pct") or alloc.get("debt"))
            out["cashAllocation"]=num(alloc.get("cash_pct") or alloc.get("cash"))
            out["otherAllocation"]=num(alloc.get("other_pct") or alloc.get("other"))
    except Exception as e: out["allocationError"]=str(e)[:180]
    # Sectors
    try:
        sec=unwrap(api_json(f"https://mfdata.in/api/v1/families/{fid}/sectors"))
        rows=first_list(sec,"sectors","items")
        cleaned=[]
        for x in rows[:20]:
            if not isinstance(x,dict): continue
            name=x.get("sector") or x.get("name")
            w=num(x.get("weight_pct") or x.get("weight") or x.get("percentage"))
            if name: cleaned.append({"sector":name,"weightPct":w,"stockCount":x.get("stock_count")})
        if cleaned: out["topSectors"]=cleaned[:10]
    except Exception as e: out["sectorsError"]=str(e)[:180]
    # Credit quality
    try:
        cq=unwrap(api_json(f"https://mfdata.in/api/v1/families/{fid}/credit-quality"))
        q=cq.get("credit_quality",cq) if isinstance(cq,dict) else {}
        if isinstance(q,dict):
            cleaned={str(k):num(v) for k,v in q.items() if num(v) is not None}
            if cleaned: out["creditQuality"]=cleaned
    except Exception as e: out["creditQualityError"]=str(e)[:180]
    # Fund managers/tenure
    try:
        ppl=unwrap(api_json(f"https://mfdata.in/api/v1/families/{fid}/people"))
        mgr=first_list(ppl,"managers","fund_managers","people")
        cleaned=[]
        for x in mgr[:12]:
            if isinstance(x,dict):
                nm=x.get("name") or x.get("manager_name")
                if nm: cleaned.append({"name":nm,"startDate":x.get("start_date") or x.get("startDate"),"tenureYears":num(x.get("tenure_years") or x.get("tenure"))})
        if cleaned:
            out["managerDetails"]=cleaned
            out["fundManagers"]=', '.join(x['name'] for x in cleaned)
    except Exception as e: out["peopleError"]=str(e)[:180]
    # Ratios: dedicated family endpoint is richer and more stable than trying to
    # infer every ratio from the scheme-profile payload.
    try:
        rat=unwrap(api_json(f"https://mfdata.in/api/v1/families/{fid}/ratios"))
        if isinstance(rat,dict):
            val=rat.get("valuation",{}) if isinstance(rat.get("valuation"),dict) else {}
            risk=rat.get("risk",{}) if isinstance(rat.get("risk"),dict) else {}
            ret=rat.get("return",{}) if isinstance(rat.get("return"),dict) else {}
            eff=rat.get("efficiency",{}) if isinstance(rat.get("efficiency"),dict) else {}
            cat=rat.get("category_avg",{}) if isinstance(rat.get("category_avg"),dict) else {}
            mapping={
                "pe": val.get("pe"), "pb": val.get("pb"), "ps": val.get("ps"),
                "dividendYield": val.get("dividend_yield"),
                "standardDeviationSource": risk.get("std_deviation") or risk.get("standard_deviation"),
                "beta": risk.get("beta"), "sortinoSource": risk.get("sortino"),
                "rSquared": risk.get("r_squared"),
                "sharpeSource": ret.get("sharpe"), "alpha": ret.get("alpha"),
                "treynor": ret.get("treynor"), "informationRatio": ret.get("information_ratio"),
                "roe": eff.get("roe"), "roa": eff.get("roa"),
            }
            for k,v in mapping.items():
                n=num(v)
                if n is not None: out[k]=n
            if cat:
                out["categoryAverageRatios"]={k:num(v) for k,v in cat.items() if num(v) is not None}
    except Exception as e: out["ratiosError"]=str(e)[:180]

    # Risk-detail endpoint: capture ratios, drawdown and risk/return position.
    try:
        rd=unwrap(api_json(f"https://mfdata.in/api/v1/families/{fid}/risk-detail"))
        if isinstance(rd,dict):
            cap=rd.get("capture_ratios",{}) if isinstance(rd.get("capture_ratios"),dict) else {}
            dd=rd.get("drawdown",{}) if isinstance(rd.get("drawdown"),dict) else {}
            rr=rd.get("risk_return",{}) if isinstance(rd.get("risk_return"),dict) else {}
            vals={
                "upsideCapture":cap.get("upside_capture"),
                "downsideCapture":cap.get("downside_capture"),
                "maxDrawdownSource":dd.get("max_drawdown_pct"),
                "drawdownRecoveryDays":dd.get("recovery_days"),
                "annualizedReturnSource":rr.get("annualized_return"),
                "annualizedRiskSource":rr.get("annualized_risk"),
            }
            for k,v in vals.items():
                n=num(v)
                if n is not None: out[k]=n
            if dd.get("drawdown_date"): out["drawdownDate"]=dd.get("drawdown_date")
            if rd.get("analyst_rating"): out["analystRating"]=rd.get("analyst_rating")
    except Exception as e: out["riskDetailError"]=str(e)[:180]

    # Annual performance is useful context and is slow-changing; keep a compact history.
    try:
        perf=unwrap(api_json(f"https://mfdata.in/api/v1/families/{fid}/performance"))
        if isinstance(perf,dict):
            ar=perf.get("annual_returns") if isinstance(perf.get("annual_returns"),list) else []
            cleaned=[]
            for x in ar[:12]:
                if not isinstance(x,dict): continue
                cleaned.append({"year":x.get("year"),"returnPct":num(x.get("return_pct")),"percentileRank":num(x.get("percentile_rank"))})
            if cleaned: out["annualPerformance"]=cleaned
            g=perf.get("growth_of_10k") if isinstance(perf.get("growth_of_10k"),dict) else {}
            if g:
                out["growthOf10k"]={"currentValue":num(g.get("current_value")),"startDate":g.get("start_date")}
    except Exception as e: out["performanceError"]=str(e)[:180]

    # Holdings: retain only top positions so the published app stays compact.
    try:
        h=unwrap(api_json(f"https://mfdata.in/api/v1/families/{fid}/holdings"))
        if isinstance(h,dict):
            out["portfolioMonth"]=h.get("month") or h.get("portfolio_month")
            equity=first_list(h,"equity","equity_holdings")
            debt=first_list(h,"debt","debt_holdings")
            other=first_list(h,"other","other_holdings")
            def clean_h(rows,kind):
                rr=[]
                for x in rows[:20]:
                    if not isinstance(x,dict): continue
                    nm=x.get("name") or x.get("stock_name") or x.get("instrument_name") or x.get("security_name")
                    if not nm: continue
                    rr.append({"name":nm,"type":kind,"sector":x.get("sector"),"creditRating":x.get("credit_rating") or x.get("rating"),"maturity":x.get("maturity") or x.get("maturity_date"),"weightPct":num(x.get("weight_pct") or x.get("weight") or x.get("percentage")),"marketValueCr":num(x.get("market_value_cr") or x.get("market_value"))})
                return rr
            allh=clean_h(equity,"Equity")+clean_h(debt,"Debt")+clean_h(other,"Other")
            allh.sort(key=lambda x:(x.get("weightPct") is not None,x.get("weightPct") or -1),reverse=True)
            if allh: out["topHoldings"]=allh[:15]
            out["holdingCount"] = len(equity)+len(debt)+len(other)
    except Exception as e: out["holdingsError"]=str(e)[:180]
    return {k:v for k,v in out.items() if v not in (None,"",[],{})}


def representative_codes(funds):
    rows=[]
    for f in funds:
        if not f.get("active"): continue
        code=f.get("directGrowthCode") or f.get("regularGrowthCode") or f.get("otherGrowthCode") or f.get("repCode")
        if code is None: continue
        rows.append((str(code),f.get("id"),f.get("deepDataAt") or "1970-01-01T00:00:00Z"))
    rows.sort(key=lambda x:(x[2],x[0]))  # oldest enrichment first
    return rows


def main():
    funds=load(DATA/"funds.json",[])
    deep=load(DATA/"deep_metrics.json",{"generatedAt":None,"byCode":{}})
    bycode=deep.get("byCode",{}) if isinstance(deep,dict) else {}
    cursor=load(DATA/"deep_cursor.json",{"lastRun":None,"success":0,"failure":0})
    healthy, health_detail = source_health_check()
    if not healthy:
        write_unavailable_status(funds, deep, health_detail)
        return
    candidates=representative_codes(funds)
    selected=candidates[:BATCH_SIZE]
    ok=0; fail=0; details={}; error_samples=[]
    for i,(code,_,_) in enumerate(selected):
        try:
            d=extract(api_json(MFDATA_DETAIL.format(code=code)))
            fid=d.get("familyId") if d else None
            if fid not in (None,""):
                family=enrich_family(fid)
                # Family endpoints add holdings/sectors/credit/people; they never erase detail fields.
                for k,v in family.items():
                    if v not in (None,"",[],{}): d[k]=v
            if d:
                old=bycode.get(code,{})
                old.update(d); bycode[code]=old; details[code]=old; ok+=1
            else: fail+=1
        except Exception as e:
            fail+=1
            msg=f"{type(e).__name__}: {str(e)[:260]}"
            bycode.setdefault(code,{})["lastError"] = msg
            bycode[code]["lastAttemptAt"] = NOW.isoformat()
            if len(error_samples) < 8: error_samples.append({"code":code,"error":msg})

    # Merge non-null deep fields into the fund master. Do not erase good old values.
    allowed=["benchmark","riskometer","exitLoad","minInvestment","minSip","fundManagers","pe","pb","turnover","ytm",
             "modifiedDuration","averageMaturity","equityAllocation","debtAllocation","cashAllocation","alpha","beta",
             "informationRatio","trackingError","rating","familyId","portfolioDate","otherAllocation","topSectors","creditQuality","managerDetails","topHoldings","holdingCount","portfolioMonth",
             "dividendYield","ps","rSquared","treynor","roe","roa","categoryAverageRatios","upsideCapture","downsideCapture",
             "maxDrawdownSource","drawdownRecoveryDays","drawdownDate","annualizedReturnSource","annualizedRiskSource","analystRating",
             "annualPerformance","growthOf10k","standardDeviationSource","sharpeSource","sortinoSource"]
    fund_by_code={}
    for f in funds:
        for c in (f.get("directGrowthCode"),f.get("regularGrowthCode"),f.get("otherGrowthCode"),f.get("repCode")):
            if c is not None: fund_by_code.setdefault(str(c),[]).append(f)
    touched=0
    for code,d in details.items():
        for f in fund_by_code.get(code,[]):
            changed=False
            for k in allowed:
                if d.get(k) not in (None,""):
                    f[k]=d[k]; changed=True
            if d.get("expenseRatio") is not None:
                f["deepExpenseRatio"]=d["expenseRatio"]; changed=True
            if changed:
                f["deepDataAt"]=d.get("fetchedAt"); f["deepSource"]="mfdata.in"; touched+=1

    write(DATA/"funds.json",funds)
    # Small initial payload; keep only fields needed for cards/search/filter.
    lite_keys=["id","amc","name","schemeType","category","asset","active","activeVariantCount",
               "directGrowthCode","regularGrowthCode","otherGrowthCode","repCode","repPlan","nav","navDate","latestAum","aumDate",
               "aaum","aaumQuarter","launchDate","deepDataAt"]
    lite=[{k:f.get(k) for k in lite_keys if k in f} for f in funds]
    write(DATA/"funds-lite.json",lite)
    write(DATA/"deep_metrics.json",{"generatedAt":NOW.isoformat(),"count":len(bycode),"byCode":bycode})
    write(DATA/"deep_cursor.json",{"lastRun":NOW.isoformat(),"batchSize":len(selected),"success":ok,"failure":fail,
                                  "touchedFunds":touched,"remainingOldestFirst":max(0,len(candidates)-len(selected)),
                                  "policy":"Rotates through the oldest deepDataAt records; factsheet/ratio fields are slow-changing."},indent=2)
    meta=load(DATA/"meta.json",{})
    meta["schemaVersion"]=8
    meta["deepLastAttemptAt"]=NOW.isoformat()
    if ok>0:
        meta["deepRefreshAt"]=NOW.isoformat()
    meta["deepMetricsCodes"]=len(bycode)
    meta.setdefault("methodology",{})["deepEnrichment"]="Rotating scheme-detail + family portfolio enrichment (allocation, sectors, credit quality, managers, top holdings); non-null values only; old valid fields retained on source failure."
    sh=meta.setdefault("sourceHealth",{})
    sh["mfdata-deep"]={"ok":ok>0,"checkedAt":NOW.isoformat(),"attempted":len(selected),"success":ok,"failure":fail,"touchedFunds":touched}
    write(DATA/"meta.json",meta,indent=2)
    result={"attempted":len(selected),"success":ok,"failure":fail,"touchedFunds":touched,"deepCodes":len(bycode),"errorSamples":error_samples}
    print(json.dumps(result,indent=2))
    # Provider outages are fail-soft: preserve previous values and surface an explicit
    # warning/source-health state instead of making the whole scheduled pipeline red.
    if selected and ok==0:
        print("::warning::Deep enrichment returned zero successful records. Previous deep data was retained; deepRefreshAt was NOT advanced.")

if __name__=="__main__":
    main()
