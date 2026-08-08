#!/usr/bin/env python3
"""MoneyMantra 9 robust universe refresh (v8).

Runs 24x7, including weekends and market holidays.

Design goals
------------
* No single data provider is a hard dependency.
* Official AMFI/SEBI sources are authoritative when available.
* Secondary APIs/listings are used for resilience and coverage detection.
* Public NFO listings/news are coverage sentinels; they never silently replace
  official scheme documents.
* Every source has a timestamp and health record.
* Existing good data is retained when a source fails.
* New scheme codes and name changes are logged.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TMP = ROOT / ".refresh_tmp"
CONFIG = ROOT / "config"
DATA.mkdir(exist_ok=True)
TMP.mkdir(exist_ok=True)

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()

SCHEME_CSV = "https://raw.githubusercontent.com/InertExpert2911/Mutual_Fund_Data/refs/heads/main/mutual_fund_data.csv"
TER_CSV = "https://raw.githubusercontent.com/captn3m0/india-mutual-fund-ter-tracker/refs/heads/main/data.csv"
AMFI_RSS_PAGE = "https://www.amfiindia.com/rss-feeds"
AMFI_NFO_RSS = "https://portal.amfiindia.com/rssNAV.aspx?nfo=y"
AMFI_NFO_PAGE = "https://www.amfiindia.com/new-fund-offer"
AMFI_SIF_NFO_PAGE = "https://www.amfiindia.com/sif/new-fund-offer"
AMFI_SIF_NAV_PAGE = "https://www.amfiindia.com/sif/latest-nav"
AMFI_NAV_ALL = "https://portal.amfiindia.com/spages/NAVAll.txt"
AMFI_FUNDWISE_AUM = "https://portal.amfiindia.com/rssNAV.aspx?fwise=y"
SEBI_MF_DRAFTS = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&smid=37&ssid=39"
SEBI_MF_REGISTRY = "https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doRecognisedFpi=yes&intmId=23"
MFDATA_SCHEMES = "https://mfdata.in/api/v1/schemes"
MFAPI_LATEST = "https://api.mfapi.in/mf/latest"

SECONDARY_NFO = [
    ("morningstar-nfo", "Morningstar India NFO list", "https://www.morningstar.in/nfo/nfolist.aspx"),
    ("groww-nfo", "Groww NFO page", "https://groww.in/nfo"),
    ("sharekhan-nfo", "Sharekhan NFO listing", "https://www.sharekhan.com/mutual-funds/upcoming-and-current-nfo"),
    ("hdfcsec-nfo", "HDFC Securities NFO listing", "https://www.hdfcsec.com/market/mutual-fund/new-fund-offer-nfo"),
    ("5paisa-nfo", "5paisa NFO listing", "https://www.5paisa.com/mutual-funds/nfo"),
    ("capitalmarket-nfo", "Capital Market NFO listing", "https://www.capitalmarket.com/markets/mutualfund/new-fund-offering.aspx?sectionname=new-fund-offering"),
    ("goodreturns-nfo", "Goodreturns NFO listing", "https://www.goodreturns.in/mutual-funds/nfo/"),
    ("sahifund-nfo", "SahiFund NFO listing", "https://sahifund.com/latest-nfo-opportunities/"),
    ("mutualfundsindia-nfo", "MutualFundsIndia NFO listing", "https://mfiframes.mutualfundsindia.com/MutualFundIndia/NFO.aspx"),
    ("navindia-nfo", "NAVIndia NFO listing", "https://www.navindia.com/"),
    ("anandrathi-nfo", "Anand Rathi NFO listing", "https://anandrathi.com/nfo"),
    ("icicidirect-nfo", "ICICI Direct NFO listing", "https://www.icicidirect.com/mutual-funds/nfo"),
    ("etmoney-nfo", "ET Money NFO listing", "https://www.etmoney.com/mutual-funds/nfo"),
    ("indmoney-nfo", "INDmoney NFO listing", "https://www.indmoney.com/mutual-funds/nfo"),
    ("angelone-mf-news", "Angel One mutual-fund launch/news sentinel", "https://www.angelone.in/news/mutual-funds"),
]
NEWS_QUERIES = [
    '"mutual fund" NFO India when:30d',
    '"new fund offer" mutual fund India when:30d',
    '"SIF" "new fund offer" India when:30d',
    '"long short fund" NFO India when:30d',
]
SCHEME_ALERT_QUERIES = [
    '"mutual fund" "SIP" (suspend OR suspension OR stop OR reopen) India when:30d',
    '"mutual fund" (merger OR merge OR consolidation) scheme India when:30d',
    '"mutual fund" "fundamental attributes" India when:30d',
    '"mutual fund" "fund manager" (change OR appointment OR resign) India when:30d',
    '"mutual fund" "exit load" change India when:30d',
    '"mutual fund" (subscription OR lumpsum) (suspend OR resume OR reopen) India when:30d',
    '"mutual fund" (renamed OR name change) scheme India when:30d',
]

SOURCE_REGISTRY = {}
try:
    SOURCE_REGISTRY = json.load(open(CONFIG / "source_registry.json", encoding="utf-8"))
except Exception:
    SOURCE_REGISTRY = {"sources": []}


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "MoneyMantra9-MF-Research/8.0 (+GitHub Actions; data aggregation)",
        "Accept": "*/*",
        "Accept-Language": "en-IN,en;q=0.9",
    })
    retry = Retry(
        total=3, connect=3, read=3, status=3, backoff_factor=1.0,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20))
    return s


S = make_session()
HEALTH = {}


def fetch(source_id: str, url: str, timeout: int = 35, *, accept_json=False):
    started = time.perf_counter()
    try:
        r = S.get(url, timeout=timeout)
        r.raise_for_status()
        ms = int((time.perf_counter() - started) * 1000)
        HEALTH[source_id] = {
            "ok": True, "url": url, "http": r.status_code, "bytes": len(r.content),
            "latencyMs": ms, "checkedAt": NOW.isoformat(),
        }
        if accept_json:
            return r.json()
        return r
    except Exception as e:
        HEALTH[source_id] = {
            "ok": False, "url": url, "error": str(e)[:500],
            "latencyMs": int((time.perf_counter() - started) * 1000), "checkedAt": NOW.isoformat(),
        }
        return None


def text(v):
    return "" if v is None else str(v).strip()


def norm(v):
    x = text(v).lower().replace("&", " and ")
    x = re.sub(r"\(formerly.*?\)", " ", x)
    x = re.sub(r"formerly known as.*$", " ", x)
    x = re.sub(r"\b(asset management company|asset management|amc|mutual fund|private|limited|ltd)\b", " ", x)
    x = re.sub(r"[^a-z0-9]+", " ", x)
    return " ".join(x.split())


def strip_variant(v):
    x = text(v)
    x = re.sub(r"\s*[-–:]?\s*(Direct|Regular|Institutional|Retail)\s*(Plan)?\b", " ", x, flags=re.I)
    x = re.sub(r"\s*[-–:]?\s*(Growth|IDCW|Dividend|Payout|Reinvestment|Bonus)(\s+Option)?\b", " ", x, flags=re.I)
    x = re.sub(r"\([^)]*(Growth|IDCW|Dividend|Direct|Regular)[^)]*\)", " ", x, flags=re.I)
    x = re.sub(r"\s+", " ", x).strip(" -–:")
    return x


def nfo_key(v):
    return norm(strip_variant(v))


def fnum(v):
    if v is None:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(v))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except Exception:
        return None


def date_iso(v):
    if not v:
        return None
    d = pd.to_datetime(v, dayfirst=True, errors="coerce")
    if pd.isna(d):
        return None
    return d.strftime("%Y-%m-%d")


def asset_class(category, name):
    c = text(category).lower(); n = text(name).lower()
    if "equity scheme" in c or re.search(r"\bequity\b", c): return "Equity"
    if "debt scheme" in c or re.search(r"\b(debt|income|liquid|duration|money market|gilt)\b", c): return "Debt"
    if "hybrid" in c or "multi asset" in c: return "Hybrid"
    if "solution oriented" in c: return "Solution Oriented"
    if "fund of funds" in c or "fof" in c or "fof" in n: return "Fund of Funds"
    if "etf" in c or "etf" in n: return "ETF"
    if "index" in c or "index" in n: return "Index / Passive"
    if "gold" in n or "silver" in n: return "Commodity"
    return "Other"


def plan_type(name, category=""):
    n = text(name).lower(); c = text(category).lower()
    if "direct" in n: return "Direct"
    if "regular" in n: return "Regular"
    if "institutional" in n: return "Institutional"
    if "retail" in n: return "Retail"
    if "etf" in n or "etf" in c: return "ETF / Exchange"
    return "Legacy / Other"


def option_type(name):
    n = text(name).lower()
    if "growth" in n: return "Growth"
    if re.search(r"\bidcw\b|dividend", n): return "IDCW / Dividend"
    if "bonus" in n: return "Bonus"
    if "segregated" in n: return "Segregated Portfolio"
    return "Other"


def clean_html_text(raw):
    soup = BeautifulSoup(raw or "", "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return "\n".join(x.strip() for x in soup.get_text("\n").splitlines() if x.strip())


def xml_items(content):
    soup = BeautifulSoup(content, "xml")
    out = []
    for item in soup.find_all("item"):
        d = {}
        for child in item.find_all(recursive=False):
            d[child.name] = child.get_text(" ", strip=True) if child.name != "description" else child.decode_contents()
        d["_text"] = clean_html_text(html_lib.unescape(d.get("description", "")))
        out.append(d)
    return out


def extract_label(raw, labels):
    lines = [x.strip() for x in str(raw or "").splitlines() if x.strip()]
    for i, line in enumerate(lines):
        for lab in labels:
            m = re.match(rf"^{re.escape(lab)}\s*[:\-]?\s*(.*)$", line, re.I)
            if m:
                tail = m.group(1).strip()
                return tail or (lines[i + 1] if i + 1 < len(lines) else "")
    for lab in labels:
        m = re.search(rf"{re.escape(lab)}\s*[:\-]\s*([^\n|]+)", str(raw or ""), re.I)
        if m:
            return m.group(1).strip()
    return ""


def load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def write_json(path, obj, indent=None):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":")), encoding="utf-8")


# -------------------------- scheme universe -------------------------------

def parse_amfi_navall(raw: str):
    rows = []
    current_amc = ""
    current_category = ""
    current_scheme_type = ""
    for line in raw.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if ";" in line:
            parts = line.split(";")
            if len(parts) < 6 or not parts[0].strip().isdigit():
                continue
            code = int(parts[0].strip())
            name = parts[3].strip()
            rows.append({
                "code": code, "name": name, "baseName": strip_variant(name),
                "amc": current_amc, "category": current_category,
                "schemeType": current_scheme_type, "plan": plan_type(name, current_category),
                "option": option_type(name), "asset": asset_class(current_category, name),
                "active": True, "nav": fnum(parts[4]), "navDate": date_iso(parts[5]),
                "isin": parts[1].strip() or None, "isinReinvest": parts[2].strip() or None,
                "sourceLive": "AMFI Complete NAV",
            })
            continue
        # AMFI sections and AMC headings are plain text lines between records.
        if re.search(r"\b(Open Ended|Close Ended|Interval)\b.*Schemes", line, re.I):
            current_category = line
            if re.search("open ended", line, re.I): current_scheme_type = "Open Ended"
            elif re.search("close ended", line, re.I): current_scheme_type = "Close Ended"
            elif re.search("interval", line, re.I): current_scheme_type = "Interval Fund"
        elif not re.search(r"Scheme|NAV|ISIN|Fund House", line, re.I) and len(line) < 120:
            current_amc = line
    return rows


def fetch_amfi_live():
    r = fetch("amfi-navall", AMFI_NAV_ALL, 60)
    if not r:
        return []
    rows = parse_amfi_navall(r.text)
    HEALTH["amfi-navall"]["records"] = len(rows)
    HEALTH["amfi-navall"]["latestDate"] = max([x["navDate"] for x in rows if x.get("navDate")], default=None)
    return rows


def fetch_mfdata_live():
    out = []
    limit = 1000
    for offset in range(0, 30000, limit):
        j = fetch("mfdata-schemes" if offset == 0 else f"mfdata-schemes-{offset//limit+1}", f"{MFDATA_SCHEMES}?limit={limit}&offset={offset}", 35, accept_json=True)
        if not j:
            break
        if isinstance(j, list):
            arr = j
        elif isinstance(j, dict):
            arr = j.get("data") or j.get("schemes") or j.get("results") or []
        else:
            arr = []
        if isinstance(arr, dict):
            arr = arr.get("data") or arr.get("schemes") or arr.get("results") or []
        if not isinstance(arr, list):
            break
        for z in arr:
            if not isinstance(z, dict):
                continue
            code = z.get("scheme_code") or z.get("amfi_code") or z.get("code")
            try: code = int(code)
            except Exception: continue
            name = z.get("scheme_name") or z.get("name") or ""
            out.append({
                "code": code, "name": name, "baseName": strip_variant(name),
                "amc": z.get("amc") or z.get("fund_house") or "",
                "category": z.get("category") or z.get("scheme_category") or "",
                "plan": (str(z.get("plan_type") or "").title() or plan_type(name, z.get("category"))),
                "option": option_type(name), "active": True,
                "nav": fnum(z.get("nav")), "navDate": date_iso(z.get("nav_date")),
                "latestAum": fnum(z.get("aum_cr") or z.get("aum")),
                "expenseRatio": fnum(z.get("expense_ratio")), "rating": z.get("rating") or z.get("morningstar"),
                "sourceLive": "mfdata.in",
            })
        if len(arr) < limit:
            break
        time.sleep(0.15)
    # collapse health shards into one user-facing line
    sub = [v for k, v in HEALTH.items() if k.startswith("mfdata-schemes")]
    HEALTH["mfdata-schemes"] = {"ok": bool(out), "records": len(out), "checkedAt": NOW.isoformat(), "url": MFDATA_SCHEMES,
                                 "pages": len(sub), "note": "Secondary coverage source"}
    return out


def fetch_mfapi_live():
    """Read MFapi's latest-schemes payload defensively.

    MFapi has returned both a top-level JSON list and wrapped dictionary
    payloads over time.  Treat either shape as valid and ignore malformed
    rows instead of aborting the complete universe refresh.
    """
    j = fetch("mfapi-latest", MFAPI_LATEST, 60, accept_json=True)
    if not j:
        return []

    if isinstance(j, list):
        arr = j
    elif isinstance(j, dict):
        arr = j.get("data") or j.get("schemes") or j.get("results") or []
    else:
        arr = []

    # Some API gateways wrap the actual list one level deeper.
    if isinstance(arr, dict):
        arr = arr.get("data") or arr.get("schemes") or arr.get("results") or []
    if not isinstance(arr, list):
        arr = []

    out = []
    for z in arr:
        if not isinstance(z, dict):
            continue
        code = z.get("schemeCode") or z.get("scheme_code") or z.get("code")
        try:
            code = int(code)
        except Exception:
            continue
        name = z.get("schemeName") or z.get("scheme_name") or z.get("name") or ""
        out.append({
            "code": code,
            "name": name,
            "baseName": strip_variant(name),
            "nav": fnum(z.get("nav")),
            "navDate": date_iso(z.get("date") or z.get("nav_date")),
            "active": True,
            "sourceLive": "MFapi.in",
        })

    HEALTH.setdefault("mfapi-latest", {})["records"] = len(out)
    return out


def refresh_scheme_seed():
    """Refresh rich mirror first; live central feeds then reconcile it."""
    path = TMP / "mutual_fund_data.csv"
    r = fetch("scheme-csv-mirror", SCHEME_CSV, 90)
    if r:
        path.write_bytes(r.content)
        try:
            subprocess.run([
                sys.executable, str(ROOT / "scripts" / "build_snapshot.py"), "--csv", str(path),
                "--previous-funds", str(DATA / "funds.json"),
                "--source-label", "Multi-source scheme snapshot; rich mirror + live AMFI/API reconciliation",
            ], check=True, capture_output=True, text=True)
            HEALTH["scheme-csv-mirror"]["records"] = len(pd.read_csv(path, usecols=["Scheme_Code"]))
        except Exception as e:
            HEALTH["scheme-csv-mirror"]["ok"] = False
            HEALTH["scheme-csv-mirror"]["error"] = f"snapshot build: {e}"
    return r is not None


def reconcile_variants(amfi_rows, mfdata_rows, mfapi_rows):
    before = load_json(DATA / "variants.json", [])
    by_code = {int(v["code"]): dict(v) for v in before if v.get("code") is not None}
    previous_codes = set(by_code)
    name_before = {c: v.get("name") for c, v in by_code.items()}

    # Source precedence for latest NAV: official AMFI > mfdata > mfapi.
    live_groups = [(mfapi_rows, 1), (mfdata_rows, 2), (amfi_rows, 3)]
    live_meta = {}
    for rows, rank in live_groups:
        for x in rows:
            code = x.get("code")
            if code is None:
                continue
            cur = live_meta.get(code)
            if cur is None or rank >= cur[0]:
                live_meta[code] = (rank, x)
            if code not in by_code:
                name = x.get("name") or f"AMFI Scheme {code}"
                by_code[code] = {
                    "code": code, "amc": x.get("amc") or "AMC awaiting enrichment",
                    "baseName": x.get("baseName") or strip_variant(name), "name": name,
                    "schemeType": x.get("schemeType") or "Awaiting enrichment",
                    "category": x.get("category") or "Awaiting enrichment",
                    "asset": x.get("asset") or asset_class(x.get("category"), name),
                    "plan": x.get("plan") or plan_type(name, x.get("category")), "option": x.get("option") or option_type(name),
                    "active": True, "nav": x.get("nav"), "navDate": x.get("navDate"),
                    "aaum": x.get("latestAum"), "aaumQuarter": None, "minInvestment": None,
                    "isin": x.get("isin"), "isinReinvest": x.get("isinReinvest"), "launchDate": None, "closureDate": None,
                    "discoveredAt": NOW.isoformat(), "sourceLive": x.get("sourceLive"),
                }
            else:
                v = by_code[code]
                # fill richer taxonomy only if live source has it
                for k in ("amc", "baseName", "category", "schemeType", "asset"):
                    if x.get(k) and (not v.get(k) or "awaiting" in str(v.get(k)).lower()): v[k] = x[k]
                if x.get("name") and (not v.get("name") or rank >= 3): v["name"] = x["name"]
                if x.get("latestAum") is not None: v["latestAum"] = x["latestAum"]
                if x.get("expenseRatio") is not None: v["expenseRatio"] = x["expenseRatio"]
                if x.get("rating") is not None: v["rating"] = x["rating"]

    for code, (rank, x) in live_meta.items():
        v = by_code[code]
        if x.get("nav") is not None:
            v["nav"] = x["nav"]
        if x.get("navDate"):
            v["navDate"] = x["navDate"]
        v["active"] = True
        v["lastSeenLive"] = NOW.isoformat()
        v["liveSource"] = x.get("sourceLive")

    variants = sorted(by_code.values(), key=lambda z: (norm(z.get("amc")), norm(z.get("baseName")), z.get("code") or 0))
    write_json(DATA / "variants.json", variants)

    changes = load_json(DATA / "changes.json", {"items": []}).get("items", [])
    new_codes = sorted(set(by_code) - previous_codes)
    for code in new_codes:
        v = by_code[code]
        changes.append({"detectedAt": NOW.isoformat(), "type": "new_scheme_code", "code": code, "name": v.get("name"), "amc": v.get("amc"), "source": v.get("liveSource") or v.get("sourceLive")})
    for code in previous_codes & set(by_code):
        old, new = text(name_before.get(code)), text(by_code[code].get("name"))
        if old and new and norm(old) != norm(new):
            changes.append({"detectedAt": NOW.isoformat(), "type": "scheme_name_change", "code": code, "oldName": old, "name": new, "amc": by_code[code].get("amc")})
    cutoff = NOW - timedelta(days=180)
    kept = []
    seen = set()
    for x in reversed(changes):
        try: dt = pd.to_datetime(x.get("detectedAt"), utc=True).to_pydatetime()
        except Exception: dt = NOW
        if dt < cutoff: continue
        sig = (x.get("type"), x.get("code"), x.get("name"), x.get("detectedAt", "")[:10])
        if sig in seen: continue
        seen.add(sig); kept.append(x)
    kept = list(reversed(kept[-1000:]))
    new_scheme_codes = [x for x in kept if x.get("type") == "new_scheme_code"]
    name_changes = [{**x, "newName": x.get("newName") or x.get("name")} for x in kept if x.get("type") == "scheme_name_change"]
    write_json(DATA / "changes.json", {"generatedAt": NOW.isoformat(), "items": kept, "newSchemeCodes": new_scheme_codes, "nameChanges": name_changes})
    HEALTH["universe-reconcile"] = {"ok": True, "records": len(variants), "newCodes": len(new_codes), "checkedAt": NOW.isoformat()}
    rebuild_funds_from_variants(variants)


def rebuild_funds_from_variants(variants):
    previous = load_json(DATA / "funds.json", [])
    prev = {(norm(f.get("amc")), norm(f.get("name"))): f for f in previous}
    groups = defaultdict(list)
    for v in variants:
        groups[(v.get("amc") or "AMC awaiting enrichment", v.get("baseName") or strip_variant(v.get("name")))].append(v)
    funds = []
    for (amc, name), recs in sorted(groups.items(), key=lambda kv: (norm(kv[0][0]), norm(kv[0][1]))):
        active = [x for x in recs if x.get("active")]
        def choose(plan, option="Growth"):
            return next((x for x in active if x.get("plan") == plan and x.get("option") == option), None)
        dg, rg = choose("Direct"), choose("Regular")
        og = next((x for x in active if x.get("option") == "Growth" and x.get("plan") not in ("Direct", "Regular")), None)
        rep = dg or rg or og or (active[0] if active else recs[0])
        old = prev.get((norm(amc), norm(name)), {})
        aaums = [fnum(x.get("aaum")) for x in recs if fnum(x.get("aaum")) is not None]
        latest_aums = [fnum(x.get("latestAum")) for x in recs if fnum(x.get("latestAum")) is not None]
        launches = [x.get("launchDate") for x in recs if x.get("launchDate")]
        closures = [x.get("closureDate") for x in recs if x.get("closureDate")]
        category = next((x.get("category") for x in recs if x.get("category") and "awaiting" not in x.get("category", "").lower()), recs[0].get("category") or "")
        scheme_type = next((x.get("schemeType") for x in recs if x.get("schemeType") and "awaiting" not in x.get("schemeType", "").lower()), recs[0].get("schemeType") or "")
        stable = {k: old.get(k) for k in [
            "benchmark", "benchmarkConfidence", "riskometer", "portfolioMonth", "pe", "pb", "turnover", "ytm",
            "modifiedDuration", "averageMaturity", "equityAllocation", "debtAllocation", "cashAllocation", "analyticsSeed",
            "exitLoad", "minSip", "fundManagers", "alpha", "beta", "informationRatio", "trackingError", "rating", "deepDataAt",
        ]}
        h = hashlib.sha1((norm(amc) + "|" + norm(name)).encode()).hexdigest()[:12]
        f = {
            "id": "MF-" + h, "amc": amc, "name": name, "schemeType": scheme_type, "category": category,
            "asset": asset_class(category, name), "active": bool(active), "variantCount": len(recs), "activeVariantCount": len(active),
            "directGrowthCode": dg.get("code") if dg else None, "regularGrowthCode": rg.get("code") if rg else None,
            "otherGrowthCode": og.get("code") if og else None, "repCode": rep.get("code"), "repPlan": rep.get("plan"),
            "nav": rep.get("nav"), "navDate": rep.get("navDate"),
            "latestAum": max(latest_aums) if latest_aums else old.get("latestAum"), "aumDate": old.get("aumDate"),
            "aaum": sum(aaums) if aaums else old.get("aaum"), "aaumQuarter": next((x.get("aaumQuarter") for x in recs if x.get("aaumQuarter")), old.get("aaumQuarter")),
            "launchDate": min(launches) if launches else old.get("launchDate"), "closureDate": max(closures) if closures else old.get("closureDate"),
            "minInvestment": min([fnum(x.get("minInvestment")) for x in recs if fnum(x.get("minInvestment")) is not None], default=old.get("minInvestment")),
            "sourceSnapshot": "Multi-source reconciled universe",
            "planCounts": dict(Counter(x.get("plan") for x in recs)), "optionCounts": dict(Counter(x.get("option") for x in recs)),
            **stable,
        }
        funds.append(f)
    active_variants = [v for v in variants if v.get("active")]
    counts = {
        "generatedAt": NOW.isoformat(), "sourceDate": max([v.get("navDate") for v in variants if v.get("navDate")], default=None),
        "schemeRecords": len(variants), "activeSchemeRecords": len(active_variants), "inactiveSchemeRecords": len(variants) - len(active_variants),
        "underlyingFunds": len(funds), "activeUnderlyingFunds": sum(1 for f in funds if f["active"]), "amcs": len({f["amc"] for f in funds if f.get("amc")}),
        "planAll": dict(Counter(v.get("plan") or "Unknown" for v in variants)), "planActive": dict(Counter(v.get("plan") or "Unknown" for v in active_variants)),
        "optionAll": dict(Counter(v.get("option") or "Unknown" for v in variants)), "optionActive": dict(Counter(v.get("option") or "Unknown" for v in active_variants)),
        "schemeTypeActive": dict(Counter(v.get("schemeType") or "Unknown" for v in active_variants)),
        "assetActive": dict(Counter(v.get("asset") or asset_class(v.get("category"), v.get("name")) for v in active_variants)),
        "growthByPlanActive": {
            "Direct": sum(1 for v in active_variants if v.get("plan") == "Direct" and v.get("option") == "Growth"),
            "Regular": sum(1 for v in active_variants if v.get("plan") == "Regular" and v.get("option") == "Growth"),
            "Other": sum(1 for v in active_variants if v.get("plan") not in ("Direct", "Regular") and v.get("option") == "Growth"),
        },
    }
    write_json(DATA / "funds.json", funds)
    write_json(DATA / "counts.json", counts, indent=2)
    build_funds_lite(funds)


def build_funds_lite(funds=None):
    """Small initial payload for near-instant search/cards.

    Full factsheet/deep fields live in funds.json and are loaded lazily only when
    details/export needs them. Keeping the first payload small is a major latency
    improvement on mobile connections.
    """
    funds = funds or load_json(DATA / "funds.json", [])
    keep = [
        "id", "amc", "name", "schemeType", "category", "asset", "active", "activeVariantCount",
        "directGrowthCode", "regularGrowthCode", "otherGrowthCode", "repCode", "repPlan",
        "nav", "navDate", "latestAum", "aumDate", "aaum", "aaumQuarter", "launchDate",
        "deepDataAt"
    ]
    lite = [{k: f.get(k) for k in keep if k in f} for f in funds]
    write_json(DATA / "funds-lite.json", lite)
    return lite


# -------------------------- TER and AUM -----------------------------------

def refresh_ter():
    r = fetch("ter-tracker", TER_CSV, 45)
    if not r:
        return
    try:
        p = TMP / "ter.csv"; p.write_bytes(r.content); df = pd.read_csv(p)
        out = {}
        for _, row in df.iterrows():
            name = text(row.get("Scheme Name")); key = nfo_key(name)
            if not key: continue
            out[key] = {
                "schemeName": name, "regularBase": fnum(row.get("Regular Plan - Base TER (%)")),
                "regularTotal": fnum(row.get("Regular Plan - Total TER (%)")), "directBase": fnum(row.get("Direct Plan - Base TER (%)")),
                "directTotal": fnum(row.get("Direct Plan - Total TER (%)")),
            }
        write_json(DATA / "ter.json", {"generatedAt": NOW.isoformat(), "source": TER_CSV, "count": len(out), "byName": out})
        HEALTH["ter-tracker"]["records"] = len(out)
    except Exception as e:
        HEALTH["ter-tracker"]["ok"] = False; HEALTH["ter-tracker"]["error"] = str(e)[:500]


def parse_aum_items(xml_content, source_url):
    rows = []
    for item in xml_items(xml_content):
        t = item.get("_text", "")
        name = extract_label(t, ["Scheme Name", "Scheme"]) or item.get("title", "")
        amc = extract_label(t, ["Mutual Fund", "AMC", "Fund House"])
        dt = date_iso(extract_label(t, ["AUM Date", "Date", "As on", "As On"]))
        aum = fnum(extract_label(t, ["AUM (Cr)", "AUM", "Assets Under Management", "Net Assets"]))
        if name and aum is not None: rows.append({"name": name, "amc": amc, "aum": aum, "date": dt, "source": source_url})
    return rows


def refresh_aum():
    scheme_rows, amc_rows, links = [], [], []
    r = fetch("amfi-aum-directory", AMFI_RSS_PAGE, 45)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(AMFI_RSS_PAGE, a["href"])
            if re.search(r"RssNAV\.aspx\?mf=\d+&swise=y", href, re.I): links.append(href)
        links = sorted(set(links))
        HEALTH["amfi-aum-directory"]["schemeFeeds"] = len(links)
    r = fetch("amfi-fundwise-aum", AMFI_FUNDWISE_AUM, 45)
    if r:
        for item in xml_items(r.content):
            t = item.get("_text", "")
            name = extract_label(t, ["Mutual Fund", "Fund House", "AMC"]) or item.get("title", "")
            aum = fnum(extract_label(t, ["AUM (Cr)", "AUM", "Assets Under Management", "Net Assets"]))
            dt = date_iso(extract_label(t, ["Date", "AUM Date", "As on"]))
            if name and aum is not None: amc_rows.append({"amc": name, "aum": aum, "date": dt})
        HEALTH["amfi-fundwise-aum"]["records"] = len(amc_rows)

    def one(url):
        try:
            rr = S.get(url, timeout=16); rr.raise_for_status(); return parse_aum_items(rr.content, url)
        except Exception:
            return []
    if links:
        with ThreadPoolExecutor(max_workers=min(12, len(links))) as ex:
            for rows in ex.map(one, links): scheme_rows += rows
    HEALTH["amfi-scheme-aum"] = {"ok": bool(scheme_rows), "records": len(scheme_rows), "feedsAttempted": len(links), "checkedAt": NOW.isoformat()}
    funds = load_json(DATA / "funds.json", [])
    exact, byname = {}, defaultdict(list)
    for x in scheme_rows:
        exact[(norm(x.get("amc")), nfo_key(x.get("name")))] = x; byname[nfo_key(x.get("name"))].append(x)
    matched = 0
    for f in funds:
        hit = exact.get((norm(f.get("amc")), nfo_key(f.get("name"))))
        if not hit:
            vals = byname.get(nfo_key(f.get("name")), []); hit = vals[0] if len(vals) == 1 else None
        if hit:
            f["latestAum"] = hit["aum"]; f["aumDate"] = hit["date"]; f["aumSource"] = "AMFI scheme-wise AUM RSS"; matched += 1
    HEALTH["amfi-scheme-aum"]["matchedFunds"] = matched
    write_json(DATA / "funds.json", funds)
    build_funds_lite(funds)
    if amc_rows:
        write_json(DATA / "amc_aum.json", {"generatedAt": NOW.isoformat(), "items": amc_rows})


# -------------------------- NFO aggregation -------------------------------

def nfo_from_text(raw, source_id, source_name, url, official=False):
    name = extract_label(raw, ["Scheme Name", "Scheme"]) or ""
    amc = extract_label(raw, ["Mutual Fund", "AMC", "Fund House"])
    category = extract_label(raw, ["Scheme Category", "Category"])
    scheme_type = extract_label(raw, ["Scheme Type", "Type"])
    od = date_iso(extract_label(raw, ["New Fund Launch Date", "NFO Open Date", "NFO Opens On", "Open Date", "Launch Date"]))
    cd = date_iso(extract_label(raw, ["New Fund Offer Closure Date", "NFO Close Date", "NFO Closes On", "Close Date", "End Date"]))
    minimum = extract_label(raw, ["Minimum Subscription Amount", "Minimum Application Amount", "Minimum Amount", "Min. Investment"])
    objective = extract_label(raw, ["Objective of Scheme", "Investment Objective"])
    if not name or not (od or cd): return None
    return {
        "name": strip_variant(name), "amc": amc or "AMC not supplied", "category": category or "Category not supplied",
        "schemeType": scheme_type or "Type not supplied", "openDate": od, "closeDate": cd,
        "minimum": minimum or None, "objective": objective or None, "offerPrice": None, "benchmark": None,
        "officialUrl": url, "productType": "Mutual Fund NFO", "sourceId": source_id, "source": source_name,
        "sourceDate": TODAY.isoformat(), "official": official,
    }


def parse_amfi_nfo_item(item):
    x = nfo_from_text(item.get("_text", ""), "amfi-nfo-rss", "AMFI NFO RSS", AMFI_NFO_PAGE, True)
    if not x and item.get("title"):
        t = item.get("_text", "") + "\nScheme Name: " + item.get("title", "")
        x = nfo_from_text(t, "amfi-nfo-rss", "AMFI NFO RSS", AMFI_NFO_PAGE, True)
    return x


def parse_amfi_nfo_page(raw):
    txt = clean_html_text(raw); lines = [x.strip() for x in txt.splitlines() if x.strip()]
    labels = ["Mutual Fund", "Scheme Name", "Objective of Scheme", "Scheme Type", "Scheme Category", "New Fund Launch Date", "New Fund Earliest Closure Date", "New Fund Offer Closure Date", "Minimum Subscription Amount", "For Further Details Please Visit Website"]
    def split_label(line):
        for lab in labels:
            m = re.match(rf"^{re.escape(lab)}(?:\s*[:\-]|\s{{2,}}|$)\s*(.*)$", line, re.I)
            if m: return lab, m.group(1).strip()
        return None, None
    recs, cur, amc = [], None, ""
    i = 0
    while i < len(lines):
        lab, tail = split_label(lines[i])
        if lab:
            val = tail
            if not val and i + 1 < len(lines) and not split_label(lines[i + 1])[0]: val = lines[i + 1]; i += 1
            if lab == "Mutual Fund": amc = val or amc
            elif lab == "Scheme Name":
                if cur and cur.get("Scheme Name"): recs.append(cur)
                cur = {"Mutual Fund": amc, "Scheme Name": val}
            elif cur is not None: cur[lab] = val
        i += 1
    if cur and cur.get("Scheme Name"): recs.append(cur)
    out = []
    for r in recs:
        pseudo = "\n".join(f"{k}: {v}" for k, v in r.items() if v)
        x = nfo_from_text(pseudo, "amfi-nfo-page", "AMFI official NFO page", AMFI_NFO_PAGE, True)
        if x:
            u = r.get("For Further Details Please Visit Website")
            if u and u.startswith("http"): x["officialUrl"] = u
            out.append(x)
    return out


DATE_RE = re.compile(r"\b(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?[\s\-/.,]+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|0?[1-9]|1[0-2])[\s\-/.,]+(?:20)?\d{2}\b", re.I)


def dates_in(v):
    return [date_iso(x) for x in DATE_RE.findall(text(v)) if date_iso(x)]


def candidate_name(cells):
    """Return a scheme-like name only; reject article headings and generic prose."""
    bad = re.compile(r"^(open|upcoming|closed|equity|debt|hybrid|others?|scheme|fund name|status|category|type)$", re.I)
    prose = re.compile(r"\b(why|what|how|after the nfo|working style|clear picture|actually perform|watchlist|blogs?|my tool|article|guide|explained)\b", re.I)
    choices=[]
    for c in cells:
        c=re.sub(r"\s+"," ",text(c)).strip(" >|:-")
        if len(c)<8 or len(c)>145 or bad.match(c) or DATE_RE.search(c) or prose.search(c) or ">>" in c: continue
        if re.search(r"\b(Fund|ETF|FoF|Fund of Funds|Omni)\b",c,re.I): choices.append(c)
    return min(choices,key=len) if choices else ""

def plausible_nfo_name(name):
    name=re.sub(r"\s+"," ",text(name)).strip()
    if not name or len(name)>145 or ">>" in name: return False
    if not re.search(r"\b(Fund|ETF|FoF|Fund of Funds|Omni)\b",name,re.I): return False
    if re.search(r"\b(why|what|how|watchlist|blogs?|my tool|working style|clear picture|actually perform|after the nfo|article|guide)\b",name,re.I): return False
    return True

def parse_secondary_nfo(raw, source_id, source_name, url):
    soup = BeautifulSoup(raw, "html.parser")
    rows = []
    # First choice: semantic table rows.
    for tr in soup.find_all("tr"):
        cells = [re.sub(r"\s+", " ", x.get_text(" ", strip=True)) for x in tr.find_all(["td", "th"])]
        joined = " | ".join(cells)
        ds = dates_in(joined)
        name = candidate_name(cells)
        if name and len(ds) >= 2:
            rows.append({
                "name": strip_variant(name), "amc": "AMC inferred from scheme name", "category": "Category not supplied", "schemeType": "Type not supplied",
                "openDate": ds[0], "closeDate": ds[1], "minimum": None, "offerPrice": "₹10 / verify source",
                "objective": None, "benchmark": None, "officialUrl": url, "productType": "Mutual Fund NFO",
                "sourceId": source_id, "source": source_name, "sourceDate": TODAY.isoformat(), "official": False,
            })
    # Card/list fallback. Look for a fund-like line followed by two dates within 14 lines.
    if not rows:
        lines = [re.sub(r"\s+", " ", x.strip()) for x in soup.get_text("\n").splitlines() if x.strip()]
        for i, line in enumerate(lines):
            if not re.search(r"\b(Fund|ETF|FOF)\b", line, re.I) or len(line) > 180 or len(line) < 6:
                continue
            block = " | ".join(lines[i:i + 14]); ds = dates_in(block)
            if len(ds) >= 2:
                rows.append({
                    "name": strip_variant(line), "amc": "AMC inferred from scheme name", "category": "Category not supplied", "schemeType": "Type not supplied",
                    "openDate": ds[0], "closeDate": ds[1], "minimum": None, "offerPrice": "₹10 / verify source",
                    "objective": None, "benchmark": None, "officialUrl": url, "productType": "Mutual Fund NFO",
                    "sourceId": source_id, "source": source_name, "sourceDate": TODAY.isoformat(), "official": False,
                })
    # de-dupe source-local
    out, seen = [], set()
    for x in rows:
        k = (nfo_key(x["name"]), x["openDate"], x["closeDate"])
        if not k[0] or k in seen or not plausible_nfo_name(x.get('name')): continue
        seen.add(k); out.append(x)
    return out[:200]


def merge_nfo_evidence(existing_items, observations):
    """Merge NFO observations without silently sacrificing coverage.

    Tracked items may be Official, Cross-verified, or Single-source observed.
    Date conflicts/invalid date pairs stay in Discovery Watch. A previously
    tracked snapshot is retained during an upstream outage, but its old
    sourceDate is preserved so the UI can show staleness honestly.
    """
    grouped = defaultdict(list)
    for x in existing_items:
        if x.get("name"):
            y = dict(x)
            y["sourceId"] = "retained-snapshot"
            y["_retainedTracked"] = True
            y["official"] = bool(y.get("official"))
            grouped[nfo_key(y["name"])].append(y)
    for x in observations:
        if x and x.get("name") and plausible_nfo_name(x.get('name')):
            grouped[nfo_key(x["name"])].append(x)

    items, watch = [], []
    cutoff = TODAY - timedelta(days=550)
    for key, ev in grouped.items():
        if not key: continue
        retained = [x for x in ev if x.get("sourceId") == "retained-snapshot"]
        live = [x for x in ev if x.get("sourceId") != "retained-snapshot"]
        official_live = [x for x in live if x.get("official")]
        canonical = max(official_live or live or retained, key=lambda x: len(text(x.get("name"))))

        # Count agreement by independent live source, never by retained snapshot.
        pair_sources = defaultdict(set)
        pair_rows = defaultdict(list)
        for x in live:
            pair=(x.get("openDate"),x.get("closeDate"))
            if pair[0] or pair[1]:
                pair_sources[pair].add(x.get("sourceId") or x.get("source"))
                pair_rows[pair].append(x)
        official_pair = next(((x.get("openDate"),x.get("closeDate")) for x in official_live if x.get("openDate") or x.get("closeDate")), None)
        ranked_pairs = sorted(pair_sources.items(), key=lambda kv:(len(kv[1]), kv[0][0] or ''), reverse=True)
        best_pair = official_pair or (ranked_pairs[0][0] if ranked_pairs else None)
        best_support = len(pair_sources.get(best_pair,set())) if best_pair else 0
        unique_live_sources = len({x.get("sourceId") or x.get("source") for x in live})
        conflicting = bool(unique_live_sources >= 2 and best_support < 2 and len(pair_sources) >= 2)

        def valid_pair(pair):
            if not pair or not pair[0] or not pair[1]: return False
            a=pd.to_datetime(pair[0],errors='coerce'); b=pd.to_datetime(pair[1],errors='coerce')
            return pd.notna(a) and pd.notna(b) and a <= b

        # No live source: fail-soft, retaining the last tracked snapshot without relabelling it current.
        if not live:
            if retained:
                best=dict(max(retained,key=lambda x:len(text(x.get('name')))))
                best.pop('_retainedTracked',None);best.pop('sourceId',None)
                best['confidence']='Retained tracked snapshot — live NFO sources unavailable'
                best.setdefault('evidenceCount',0)
                items.append(best)
            continue

        if conflicting or (best_pair and not valid_pair(best_pair)):
            # Keep prior tracked dates visible, but surface current disagreement separately.
            if retained:
                old=dict(max(retained,key=lambda x:len(text(x.get('name')))))
                old.pop('_retainedTracked',None);old.pop('sourceId',None)
                old['confidence']='Retained prior tracked dates — current sources conflict'
                items.append(old)
            w=dict(canonical)
            w['confidence']='Source date conflict — discovery watch'
            w['evidenceCount']=unique_live_sources
            w['evidence']=[{"source":x.get('source'),"sourceId":x.get('sourceId'),"url":x.get('officialUrl'),"openDate":x.get('openDate'),"closeDate":x.get('closeDate'),"official":bool(x.get('official'))} for x in live[-16:]]
            w['id']='nfo-watch-'+hashlib.sha1((key+'|conflict').encode()).hexdigest()[:14]
            w['sourceDate']=TODAY.isoformat()
            watch.append(w)
            continue

        if not best_pair or not valid_pair(best_pair):
            w=dict(canonical);w['confidence']='Incomplete dates — discovery watch';w['evidenceCount']=unique_live_sources;w['sourceDate']=TODAY.isoformat();w['id']='nfo-watch-'+hashlib.sha1((key+'|incomplete').encode()).hexdigest()[:14];watch.append(w);continue

        if official_pair:
            confidence='Official'
        elif best_support >= 2:
            confidence='Cross-verified'
        else:
            confidence='Single-source observed — verify official document'

        # Prefer a row supporting the chosen pair; then carry richer fields from retained snapshot if useful.
        supporters=pair_rows.get(best_pair,[]) or live
        best=dict(max(supporters,key=lambda x:len(text(x.get('name')))))
        if retained:
            old=max(retained,key=lambda x:len(text(x.get('name'))))
            for k,v in old.items():
                if k.startswith('_') or k in ('sourceId','official'): continue
                if best.get(k) in (None,'','Category not supplied','Type not supplied','AMC inferred from scheme name') and v not in (None,''):
                    best[k]=v
        best['openDate'],best['closeDate']=best_pair
        best['confidence']=confidence
        best['official']=bool(official_pair)
        best['evidenceCount']=unique_live_sources
        best['evidence']=[{"source":x.get('source'),"sourceId":x.get('sourceId'),"url":x.get('officialUrl'),"openDate":x.get('openDate'),"closeDate":x.get('closeDate'),"official":bool(x.get('official'))} for x in live[-16:]]
        best['id']='nfo-'+hashlib.sha1((key+'|'+text(best_pair[0])).encode()).hexdigest()[:14]
        best['sourceDate']=TODAY.isoformat()
        cd=pd.to_datetime(best.get('closeDate'),errors='coerce')
        if pd.notna(cd) and cd.date() < cutoff: continue
        items.append(best)

    # Final de-dupe because a retained item and a conflict path can share a name.
    ded_items={}
    for x in items:
        k=nfo_key(x.get('name'))
        if k: ded_items[k]=x
    items=list(ded_items.values())
    items.sort(key=lambda x:(x.get('closeDate') or '9999',x.get('name','')))
    watch.sort(key=lambda x:(x.get('openDate') or '9999',x.get('name','')))
    return items, watch

def fetch_news_watch():
    out = []
    for qi, q in enumerate(NEWS_QUERIES):
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-IN&gl=IN&ceid=IN:en"
        sid = f"news-discovery-{qi+1}"
        r = fetch(sid, url, 30)
        if not r: continue
        for item in xml_items(r.content):
            title = text(item.get("title")); link = text(item.get("link")); pub = text(item.get("pubDate"))
            if not re.search(r"\b(NFO|new fund offer|launch(?:es|ed)?|SIF|long.short)\b", title, re.I): continue
            try: pub_iso = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat()
            except Exception: pub_iso = None
            out.append({"title": title, "url": link, "publishedAt": pub_iso, "productType": "SIF / other product" if re.search(r"\bSIF\b|long.short", title, re.I) else "Mutual Fund NFO lead", "source": "Google News RSS discovery"})
    # de-dupe titles
    seen, ded = set(), []
    for x in sorted(out, key=lambda z: z.get("publishedAt") or "", reverse=True):
        k = norm(x["title"])
        if k in seen: continue
        seen.add(k); ded.append(x)
    HEALTH["news-discovery"] = {"ok": bool(ded), "records": len(ded), "checkedAt": NOW.isoformat(), "note": "Discovery leads only; not authoritative"}
    return ded[:120]



def refresh_scheme_alerts():
    """Discovery feed for scheme operational changes that are not captured by daily NAV master.
    These are leads, not authoritative instructions; the UI labels them as discovery until verified.
    """
    out = []
    trigger = re.compile(r"\b(SIP|STP|SWP|suspend|suspension|resume|reopen|merger|merge|fundamental attribute|fund manager|exit load|subscription|rename|name change|addendum)\b", re.I)
    for qi, q in enumerate(SCHEME_ALERT_QUERIES):
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-IN&gl=IN&ceid=IN:en"
        sid = f"scheme-alert-news-{qi+1}"
        r = fetch(sid, url, 30)
        if not r: continue
        for item in xml_items(r.content):
            title = text(item.get("title")); link = text(item.get("link")); pub = text(item.get("pubDate"))
            if not title or not trigger.search(title): continue
            try: pub_iso = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat()
            except Exception: pub_iso = None
            kind = "Scheme operational change"
            if re.search(r"\bSIP|STP|SWP|subscription|reopen|resume|suspend", title, re.I): kind = "Subscription / transaction change"
            elif re.search(r"\bfund manager\b", title, re.I): kind = "Fund-manager change"
            elif re.search(r"\bmerg|consolidat", title, re.I): kind = "Merger / consolidation"
            elif re.search(r"\bexit load\b", title, re.I): kind = "Exit-load change"
            elif re.search(r"\bfundamental attribute\b", title, re.I): kind = "Fundamental-attribute change"
            elif re.search(r"\brename|name change\b", title, re.I): kind = "Scheme-name change"
            out.append({"title": title, "url": link, "publishedAt": pub_iso, "kind": kind, "source": "Google News RSS discovery", "confidence": "Discovery — verify AMC/SEBI/AMFI notice"})
    seen, ded = set(), []
    for x in sorted(out, key=lambda z: z.get("publishedAt") or "", reverse=True):
        k = norm(x["title"])
        if not k or k in seen: continue
        seen.add(k); ded.append(x)
    previous = load_json(DATA / "alerts.json", {"items": []}).get("items", [])
    # retain up to 60 days of previous leads to make weekends/source outages fail-soft
    combined = ded + previous
    final=[]; seen=set(); cutoff=NOW-timedelta(days=60)
    for x in combined:
        k=norm(x.get("title"));
        if not k or k in seen: continue
        try:
            dt=pd.to_datetime(x.get("publishedAt"),utc=True).to_pydatetime() if x.get("publishedAt") else NOW
        except Exception: dt=NOW
        if dt < cutoff: continue
        seen.add(k); final.append(x)
    payload={"generatedAt":NOW.isoformat(),"count":len(final),"items":final[:150],"policy":"Discovery layer only. Verify actionable changes against AMC addendum/notice, AMFI or SEBI before relying on them."}
    write_json(DATA/"alerts.json",payload)
    HEALTH["scheme-change-discovery"]={"ok":bool(ded),"records":len(ded),"checkedAt":NOW.isoformat(),"note":"News/search discovery only; previous valid leads retained on outage"}
    return payload

def refresh_sebi_pipeline():
    out = []
    r = fetch("sebi-mf-drafts", SEBI_MF_DRAFTS, 35)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        for tr in soup.find_all("tr"):
            cells = [re.sub(r"\s+", " ", x.get_text(" ", strip=True)) for x in tr.find_all(["td", "th"])]
            if len(cells) < 2: continue
            dt = date_iso(cells[0]); title = cells[-1]
            if dt and len(title) > 4 and not re.match(r"title$", title, re.I):
                a = tr.find("a", href=True)
                out.append({"date": dt, "title": title, "url": urljoin(SEBI_MF_DRAFTS, a["href"]) if a else SEBI_MF_DRAFTS, "status": "SEBI draft filing — not an open NFO"})
        HEALTH["sebi-mf-drafts"]["records"] = len(out)
    # Retain prior if source temporarily fails.
    if not out:
        out = load_json(DATA / "sebi_pipeline.json", {"items": []}).get("items", [])
    write_json(DATA / "sebi_pipeline.json", {"generatedAt": NOW.isoformat(), "items": out[:150]})
    return out


def refresh_registry():
    r = fetch("sebi-mf-registry", SEBI_MF_REGISTRY, 35)
    reg = load_json(DATA / "registry.json", {})
    if r:
        txt = clean_html_text(r.text)
        m = re.search(r"\b\d+\s+to\s+\d+\s+of\s+(\d+)\s+records", txt, re.I)
        names = []
        soup = BeautifulSoup(r.text, "html.parser")
        # collect visible page names; total count is still useful even if paginated
        for s in soup.stripped_strings:
            z = text(s)
            if z.lower().endswith("mutual fund") or z.upper().endswith(" MF"):
                if len(z) < 100: names.append(z)
        reg = {"generatedAt": NOW.isoformat(), "registeredCount": int(m.group(1)) if m else reg.get("registeredCount"), "visibleNames": sorted(set(names)), "source": SEBI_MF_REGISTRY}
        HEALTH["sebi-mf-registry"]["registeredCount"] = reg.get("registeredCount")
    write_json(DATA / "registry.json", reg)
    return reg


def refresh_nfos():
    existing = load_json(DATA / "nfos.json", {"items": []})
    existing_items = existing.get("items", []) if isinstance(existing, dict) else existing
    obs = []
    r = fetch("amfi-nfo-rss", AMFI_NFO_RSS, 45)
    if r:
        xs = [parse_amfi_nfo_item(i) for i in xml_items(r.content)]; xs = [x for x in xs if x]; obs += xs; HEALTH["amfi-nfo-rss"]["records"] = len(xs)
    r = fetch("amfi-nfo-page", AMFI_NFO_PAGE, 45)
    if r:
        xs = parse_amfi_nfo_page(r.text); obs += xs; HEALTH["amfi-nfo-page"]["records"] = len(xs)

    def secondary_one(item):
        sid, name, url = item
        rr = fetch(sid, url, 30)
        if not rr: return []
        try:
            xs = parse_secondary_nfo(rr.text, sid, name, url); HEALTH[sid]["records"] = len(xs); return xs
        except Exception as e:
            HEALTH[sid]["ok"] = False; HEALTH[sid]["error"] = f"parse: {e}"; return []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for rows in ex.map(secondary_one, SECONDARY_NFO): obs += rows

    # Official AMC web-discovery observations are produced by the separate
    # bounded AMC-watch workflow. They are treated as official evidence, but
    # still retained with their exact source URL and timestamps for audit.
    amc_obs_payload = load_json(DATA / "amc_nfo_observations.json", {"items": []})
    amc_obs = amc_obs_payload.get("items", []) if isinstance(amc_obs_payload, dict) else []
    if amc_obs:
        obs += amc_obs
        HEALTH["official-amc-web-nfo"] = {"ok": True, "records": len(amc_obs), "checkedAt": amc_obs_payload.get("generatedAt") or NOW.isoformat(), "note": "Official AMC website observations from bounded sitemap/homepage discovery."}
    else:
        HEALTH["official-amc-web-nfo"] = {"ok": False, "records": 0, "checkedAt": NOW.isoformat(), "note": "Awaiting first Official AMC Watch workflow or no extractable NFO dates found."}

    items, watch = merge_nfo_evidence(existing_items, obs)
    news = fetch_news_watch()
    # News leads are shown separately. Never automatically call them verified NFOs.
    payload = {
        "generatedAt": NOW.isoformat(), "count": len(items), "watchCount": len(watch) + len(news),
        "items": items, "watch": watch, "newsWatch": news,
        "sourceStatus": [{"id": k, **v} for k, v in HEALTH.items() if "nfo" in k or k.startswith("news-discovery")],
        "policy": "Official AMFI/SEBI/AMC evidence has highest confidence. Two independent secondary listings can cross-verify a date pair. A credible single-source record with valid dates stays visible with a confidence warning; news-only leads remain Discovery Watch."
    }
    write_json(DATA / "nfos.json", payload)
    HEALTH["nfo-consensus"] = {"ok": bool(items) or bool(obs), "verifiedRecords": len(items), "watchRecords": len(watch), "newsLeads": len(news), "checkedAt": NOW.isoformat()}
    return payload


def refresh_product_watch():
    """Keep SIFs and other adjacent regulated products separate from MF NFOs."""
    previous = load_json(DATA / "product_watch.json", {"items": []})
    items = []
    r = fetch("amfi-sif-nfo", AMFI_SIF_NFO_PAGE, 45)
    if r:
        # AMFI's SIF NFO page follows substantially the same label pattern as MF NFO.
        for x in parse_amfi_nfo_page(r.text):
            x["productType"] = "Specialised Investment Fund (SIF)"
            x["sourceId"] = "amfi-sif-nfo"
            x["source"] = "AMFI official SIF NFO page"
            x["officialUrl"] = x.get("officialUrl") or AMFI_SIF_NFO_PAGE
            x["official"] = True
            x["confidence"] = "Official"
            items.append(x)
        HEALTH["amfi-sif-nfo"]["records"] = len(items)
    # If the NFO page is temporarily empty/unavailable, retain recent known SIF offers.
    if not items:
        items = previous.get("items", []) if isinstance(previous, dict) else []
    # Pull the official SIF NAV page only as a universe/health check; it is not an NFO source.
    rnav = fetch("amfi-sif-nav", AMFI_SIF_NAV_PAGE, 45)
    if rnav:
        txt = clean_html_text(rnav.text)
        codes = len(set(re.findall(r"\bSIF-\d+\b", txt, re.I)))
        HEALTH["amfi-sif-nav"]["codes"] = codes
    payload = {
        "generatedAt": NOW.isoformat(),
        "items": items,
        "note": "SIF and adjacent products are intentionally shown separately from ordinary mutual-fund NFOs.",
        "sourceStatus": [{"id":k, **v} for k,v in HEALTH.items() if k.startswith("amfi-sif")],
    }
    write_json(DATA / "product_watch.json", payload)
    return payload


# -------------------------- meta / orchestration --------------------------

def update_meta(registry):
    meta = load_json(DATA / "meta.json", {})
    meta.update({
        "schemaVersion": 8, "generatedAt": NOW.isoformat(), "fastRefreshAt": NOW.isoformat(),
        "sourceHealth": HEALTH, "sourceRegistry": "config/source_registry.json",
        "refreshPolicy": "Hourly 24x7 including weekends and market holidays; data dates remain the source's true reporting dates.",
        "registeredAMCCount": registry.get("registeredCount"),
        "methodology": {
            **meta.get("methodology", {}),
            "nfoConsensus": "Official source preferred; otherwise two independent secondary sources with matching dates; single-source/news leads remain unverified watch items.",
            "sourceFailover": "Existing valid snapshot is retained when an upstream source fails; UI exposes source health and staleness.",
            "weekendPolicy": "Refresh workflows run on calendar time, not exchange trading hours. NAV can remain at the last dealing day while NFO, filing, TER, scheme-master and operational-change sources may still update.",
            "changeDiscovery": "Scheme operational-change news/search is discovery only and must be verified against official AMC/AMFI/SEBI notices."
        }
    })
    write_json(DATA / "meta.json", meta, indent=2)
    write_json(DATA / "source_health.json", {"generatedAt": NOW.isoformat(), "sources": HEALTH, "registry": SOURCE_REGISTRY}, indent=2)


def main():
    # 1. Rich seed and three independent live scheme sources.
    refresh_scheme_seed()
    with ThreadPoolExecutor(max_workers=3) as ex:
        f1 = ex.submit(fetch_amfi_live); f2 = ex.submit(fetch_mfdata_live); f3 = ex.submit(fetch_mfapi_live)
        amfi_rows, mfdata_rows, mfapi_rows = f1.result(), f2.result(), f3.result()
    # At least one current scheme source must work OR existing snapshot must remain sufficiently large.
    reconcile_variants(amfi_rows, mfdata_rows, mfapi_rows)
    # 2. Independent data families are fail-soft.
    refresh_ter()
    refresh_aum()
    refresh_sebi_pipeline()
    registry = refresh_registry()
    refresh_nfos()
    refresh_product_watch()
    refresh_scheme_alerts()
    update_meta(registry)
    counts = load_json(DATA / "counts.json", {})
    print(json.dumps({"counts": counts, "health": HEALTH}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
