#!/usr/bin/env python3
"""Official AMC website discovery layer for MoneyMantra 9 MF Research.

Purpose
-------
AMFI/SEBI remain the authoritative central sources, but product launches and
operational notices sometimes appear first on an AMC website. This job builds a
registry of official AMC websites from AMFI member pages, then checks a bounded
set of relevant pages discovered from the AMC home page and sitemap(s).

The crawler is intentionally conservative:
* it identifies itself with a normal User-Agent;
* it reads robots.txt for sitemap hints and keeps the crawl bounded;
* it limits pages per AMC and total concurrency;
* it never executes JavaScript or downloads arbitrary binaries;
* discovered items are a verification/discovery layer, not an automatic
  replacement for AMFI/SEBI data.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from refresh_fast import (
    DATA, NOW, TODAY, load_json, write_json, nfo_from_text, refresh_nfos
)

ROOT = Path(__file__).resolve().parents[1]
AMFI_MEMBERS = "https://www.amfiindia.com/aboutamfi?tab=members"
MEMBER_BASE = "https://www.amfiindia.com"
UA = "MoneyMantra9-MF-Research/8.0 (+public GitHub Pages research tool; respectful bounded refresh)"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
KEYWORDS = {
    "nfo": 10, "new-fund-offer": 10, "newfund": 9, "new_fund_offer": 9,
    "addendum": 9, "notice": 8, "notices": 8, "scheme-information": 8,
    "schemeinformation": 8, "sid": 7, "kim": 6, "fund-manager": 7,
    "fundmanager": 7, "factsheet": 6, "fact-sheet": 6, "expense-ratio": 6,
    "ter": 5, "statutory": 5, "disclosure": 5, "downloads": 4,
    "announcement": 6, "product": 3, "scheme": 3,
}
NOTICE_PATTERNS = [
    ("NFO / launch", r"\b(?:nfo|new fund offer|launch(?:ed|es|ing)?|subscription opens?)\b"),
    ("Scheme notice / addendum", r"\b(?:notice|addendum|corrigendum|fundamental attribute|change in scheme)\b"),
    ("SIP / STP / SWP / subscription", r"\b(?:sip|stp|swp|subscription|lumpsum|lump sum).{0,60}\b(?:suspend|resume|reopen|restrict|stop|discontinue|change)"),
    ("Fund manager change", r"\b(?:fund manager|portfolio manager).{0,50}\b(?:change|appoint|cease|resign|reassign)"),
    ("Expense / load change", r"\b(?:expense ratio|ter|exit load|entry load).{0,50}\b(?:change|revise|revision|effective)"),
    ("Merger / rename", r"\b(?:merge|merger|consolidat|rename|name change)\b"),
]


def get(url: str, timeout: int = 22, max_bytes: int = 2_500_000):
    t = time.perf_counter()
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        body = r.content[:max_bytes]
        return {"ok": True, "url": r.url, "status": r.status_code, "ctype": ctype,
                "text": body.decode(r.encoding or "utf-8", errors="replace"),
                "bytes": len(body), "latencyMs": round((time.perf_counter()-t)*1000)}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}",
                "latencyMs": round((time.perf_counter()-t)*1000)}


def host_root(url: str) -> str | None:
    try:
        p = urlparse(url if "://" in url else "https://" + url)
        if not p.netloc: return None
        return f"{p.scheme or 'https'}://{p.netloc}/"
    except Exception:
        return None


def clean_url(url: str, base: str | None = None) -> str | None:
    if not url: return None
    raw=url.strip()
    if not base and not re.match(r"^https?://",raw,re.I) and re.match(r"^(?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/.*)?$",raw):
        raw="https://"+raw
    u = urljoin(base or "", raw)
    if not u.startswith(("http://", "https://")): return None
    p = urlparse(u)
    if not p.netloc: return None
    return p._replace(fragment="").geturl()


def parse_member_ids(html: str):
    ids = set(re.findall(r"/member/(\d+)", html, re.I))
    return sorted(ids, key=int)


def member_from_page(member_id: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = "\n".join(s.strip() for s in soup.stripped_strings)
    # AMC name: first heading after member-detail marker is usually the fund name.
    name = None
    for tag in soup.find_all(["h1","h2","h3","h4","h5"]):
        tx = " ".join(tag.stripped_strings).strip()
        if tx and "AMFI Member" not in tx and "Member Details" not in tx:
            if "Mutual Fund" in tx.upper() or "FUND" in tx.upper():
                name = tx; break
    if not name:
        m = re.search(r"Name of the Mutual Fund\s*\n\s*([^\n]{3,120})", text, re.I)
        name = m.group(1).strip() if m else f"AMFI member {member_id}"
    website = None
    # Prefer links near Website labels.
    for a in soup.find_all("a", href=True):
        href = clean_url(a.get("href"), MEMBER_BASE)
        label = " ".join(a.stripped_strings).strip()
        if href and "amfiindia.com" not in urlparse(href).netloc and ("website" in label.lower() or label.lower().startswith("www.")):
            website = href; break
    if not website:
        m = re.search(r"Website\s*\n\s*((?:https?://)?(?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?)", text, re.I)
        if m: website = clean_url(m.group(1))
    reg = None
    m = re.search(r"SEBI\s*Reg(?:istration)?\s*ID\s*\n?\s*([A-Z]+/[0-9A-Z/.-]+)", text, re.I)
    if m: reg = m.group(1).strip()
    return {"memberId": int(member_id), "amc": name, "website": website, "sebiRegId": reg,
            "amfiMemberUrl": f"{MEMBER_BASE}/member/{member_id}"}


def discover_registry():
    previous = load_json(DATA / "amc_web_registry.json", {"items": []})
    r = get(AMFI_MEMBERS, 30)
    health = {"ok": r.get("ok", False), "checkedAt": NOW.isoformat(), "latencyMs": r.get("latencyMs"),
              "error": r.get("error")}
    ids = parse_member_ids(r.get("text", "")) if r.get("ok") else []
    if not ids:
        health["note"] = "AMFI member index unavailable/unparseable; retained prior AMC registry."
        return previous.get("items", []), health

    def one(mid):
        rr = get(f"{MEMBER_BASE}/member/{mid}", 20)
        if not rr.get("ok"): return None
        try: return member_from_page(mid, rr["text"])
        except Exception: return None

    items=[]
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for x in ex.map(one, ids):
            if x and x.get("website"): items.append(x)
    # Merge previous URLs for members whose detail page failed this time.
    by_id={str(x.get("memberId")):x for x in previous.get("items",[]) if x.get("memberId")}
    for x in items: by_id[str(x.get("memberId"))]=x
    final=sorted(by_id.values(), key=lambda x:(x.get("amc") or "").lower())
    health.update({"records": len(final), "indexMemberIds": len(ids), "note": "Official AMC websites discovered from AMFI member details."})
    payload={"generatedAt":NOW.isoformat(),"source":AMFI_MEMBERS,"count":len(final),"items":final}
    write_json(DATA / "amc_web_registry.json", payload, indent=2)
    return final, health


def parse_robots_for_sitemaps(root: str):
    r=get(urljoin(root,"robots.txt"),12,350_000)
    urls=[]
    if r.get("ok"):
        for line in r["text"].splitlines():
            m=re.match(r"\s*Sitemap\s*:\s*(\S+)",line,re.I)
            if m:
                u=clean_url(m.group(1),root)
                if u: urls.append(u)
    urls.extend([urljoin(root,"sitemap.xml"),urljoin(root,"sitemap_index.xml")])
    return list(dict.fromkeys(urls))[:4]


def sitemap_urls(url: str, allowed_host: str, depth=0):
    if depth>1: return []
    r=get(url,15,2_500_000)
    if not r.get("ok"): return []
    text=r.get("text","")
    out=[]
    try:
        root=ET.fromstring(text)
        # ignore namespaces using local-name extraction
        locs=[]
        for e in root.iter():
            if e.tag.split('}')[-1].lower()=="loc" and e.text: locs.append(e.text.strip())
        if root.tag.split('}')[-1].lower()=="sitemapindex":
            for child in locs[:8]:
                if urlparse(child).netloc==allowed_host:
                    out.extend(sitemap_urls(child,allowed_host,depth+1))
        else:
            out.extend([u for u in locs if urlparse(u).netloc==allowed_host])
    except Exception:
        # fallback regex for malformed XML
        out.extend(re.findall(r"<loc>\s*(https?://[^<]+)\s*</loc>",text,re.I))
    return out[:8000]


def link_score(url: str):
    u=url.lower()
    score=0
    for k,v in KEYWORDS.items():
        if k in u: score=max(score,v)
    if any(u.endswith(ext) for ext in ('.jpg','.jpeg','.png','.gif','.svg','.webp','.zip','.mp4','.mp3')): return -10
    if '.pdf' in u: score += 1  # keep official SID/KIM/addendum PDFs as link leads; don't download body here
    return score


def html_candidate_links(html: str, base: str, host: str):
    soup=BeautifulSoup(html,"html.parser")
    out=[]
    for a in soup.find_all("a",href=True):
        u=clean_url(a.get("href"),base)
        if not u or urlparse(u).netloc!=host: continue
        label=" ".join(a.stripped_strings).lower()
        s=link_score(u)
        if any(k.replace('-',' ') in label for k in KEYWORDS): s=max(s,6)
        if s>0: out.append((s,u))
    return out


def classify(text: str, url: str):
    hay=(url+" "+text[:60000]).lower()
    for kind,pat in NOTICE_PATTERNS:
        if re.search(pat,hay,re.I|re.S): return kind
    return "Official AMC disclosure"


def title_of(html: str, fallback: str):
    soup=BeautifulSoup(html,"html.parser")
    if soup.title and soup.title.string:
        return re.sub(r"\s+"," ",soup.title.string).strip()[:240]
    for h in soup.find_all(["h1","h2"]):
        tx=" ".join(h.stripped_strings).strip()
        if tx:return tx[:240]
    return fallback[:240]


def scan_amc(amc: dict):
    website=clean_url(amc.get("website"))
    root=host_root(website) if website else None
    if not root:return {"amc":amc.get("amc"),"ok":False,"error":"No official website"},[],[]
    host=urlparse(root).netloc
    home=get(root,18,1_500_000)
    candidates=[]
    if home.get("ok"):
        candidates.extend(html_candidate_links(home.get("text",""),home.get("url") or root,host))
    # Sitemap discovery expands coverage to statutory/addendum pages not linked on homepage.
    for sm in parse_robots_for_sitemaps(root):
        for u in sitemap_urls(sm,host):
            s=link_score(u)
            if s>0:candidates.append((s,u))
    # unique and bounded. Prefer NFO/addendum/notice pages.
    best={}
    for score,u in candidates:
        if u not in best or score>best[u]:best[u]=score
    ranked=sorted(best.items(),key=lambda kv:(kv[1],kv[0]),reverse=True)[:6]
    leads=[]; nfo_obs=[]
    for u,score in ranked:
        # Avoid pulling large PDF bodies here; link itself remains an official lead.
        if '.pdf' in u.lower():
            leads.append({"id":"amc-"+hashlib.sha1(u.encode()).hexdigest()[:14],"amc":amc.get("amc"),"kind":"Official AMC document","title":u.rsplit('/',1)[-1][:220],"url":u,"official":True,"source":"Official AMC website","detectedAt":NOW.isoformat(),"confidence":"Official AMC link — open document to verify details"})
            continue
        pr=get(u,18,1_500_000)
        if not pr.get("ok"):continue
        text=BeautifulSoup(pr.get("text",""),"html.parser").get_text("\n",strip=True)
        kind=classify(text,u)
        title=title_of(pr.get("text",""),u)
        leads.append({"id":"amc-"+hashlib.sha1(u.encode()).hexdigest()[:14],"amc":amc.get("amc"),"kind":kind,"title":title,"url":u,"official":True,"source":"Official AMC website","detectedAt":NOW.isoformat(),"confidence":"Official AMC web discovery"})
        if kind=="NFO / launch" or re.search(r"\bNFO\b|New Fund Offer",text,re.I):
            obs=nfo_from_text(text,"official-amc-web","Official AMC website",u,True)
            if obs:
                if not obs.get("amc") or obs.get("amc") in ("AMC not supplied","AMC inferred from scheme name"):
                    obs["amc"]=amc.get("amc")
                nfo_obs.append(obs)
    return {"amc":amc.get("amc"),"website":root,"ok":bool(home.get("ok")),"checkedAt":NOW.isoformat(),"candidateLinks":len(best),"pagesInspected":len(ranked),"error":home.get("error")},leads,nfo_obs


def main():
    registry, registry_health = discover_registry()
    prev=load_json(DATA / "amc_watch.json", {"items":[]})
    # Scan only real registered web endpoints; bounded concurrency avoids hammering AMCs.
    statuses=[]; leads=[]; nfo=[]
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        futures=[ex.submit(scan_amc,a) for a in registry if a.get("website")]
        for fut in cf.as_completed(futures):
            try:
                st,ls,ns=fut.result(); statuses.append(st);leads.extend(ls);nfo.extend(ns)
            except Exception as e:
                statuses.append({"ok":False,"error":f"{type(e).__name__}: {e}","checkedAt":NOW.isoformat()})

    # Retain previous official leads for 90 days so a transient sitemap/homepage change
    # does not make an already-detected AMC disclosure disappear.
    cutoff=NOW-timedelta(days=90)
    all_leads={}
    for x in prev.get("items",[]):
        try:d=datetime.fromisoformat(str(x.get("detectedAt","")).replace('Z','+00:00'))
        except Exception:d=cutoff-timedelta(days=1)
        if d>=cutoff and x.get("url"):all_leads[x["url"]]=x
    for x in leads:
        if x.get("url"):all_leads[x["url"]]=x
    final=sorted(all_leads.values(),key=lambda x:x.get("detectedAt") or "",reverse=True)[:1000]
    good=sum(1 for x in statuses if x.get("ok"))
    payload={
        "generatedAt":NOW.isoformat(),"registeredOfficialWebsites":len(registry),"respondingHomepages":good,
        "count":len(final),"items":final,"amcStatus":sorted(statuses,key=lambda x:(x.get('amc') or '')),
        "policy":"Official AMC website discovery layer. It supplements AMFI/SEBI and is never used to silently overwrite conflicting authoritative central data. Page scanning is bounded and fail-soft."
    }
    write_json(DATA / "amc_watch.json",payload,indent=2)
    write_json(DATA / "amc_nfo_observations.json",{"generatedAt":NOW.isoformat(),"count":len(nfo),"items":nfo},indent=2)
    write_json(DATA / "amc_source_health.json",{"generatedAt":NOW.isoformat(),"amfiMemberRegistry":registry_health,"respondingHomepages":good,"officialWebsites":len(registry)},indent=2)

    # Rebuild NFO consensus immediately so an official AMC observation can be reflected
    # without waiting for the next hourly universe job.
    nfo_payload=refresh_nfos()
    print({"officialAMCWebsites":len(registry),"responding":good,"officialLeads":len(final),"officialNfoObservations":len(nfo),"trackedNfos":len(nfo_payload.get('items',[]))})

if __name__=="__main__":
    main()
