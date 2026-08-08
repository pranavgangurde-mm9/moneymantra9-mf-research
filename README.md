# MoneyMantra 9 — Mutual Fund Universe Research v8.1

A static-first, multi-source Progressive Web App for Indian mutual-fund research. The client browser reads prevalidated same-origin JSON from GitHub Pages for low latency. GitHub Actions performs the network-heavy discovery, reconciliation and analytics work in the background.

## Why v8.1 exists

Earlier builds relied too heavily on a phone/browser calling external APIs when the user pressed Sync. That can fail because of CORS rules, rate limits, source downtime, mobile browser caching and service-worker caching. v8.1 moves refresh work to scheduled GitHub Actions and treats every field as dated/source-aware.

## Calendar-time refresh model

The schedules are NOT tied to NSE/BSE trading hours.

- Every 30 minutes: NFO + scheme-change discovery, including weekends and market holidays.
- Every hour: full scheme universe, plan/option counts, latest available NAV, TER, AUM/AAUM, SEBI filing pipeline and NFO consensus.
- Every 4 hours: official AMC website watch discovered from the AMFI member directory.
- Every 4 hours: rotating deep scheme/portfolio refresh.
- Daily: historical NAV analytics (CAGR, standard deviation, downside deviation, Sharpe, Sortino, max drawdown, Calmar and rolling returns).

On a weekend the newest NAV may correctly remain Friday's NAV, while an NFO, addendum, filing, subscription restriction or fund-manager notice can still receive a newer Saturday/Sunday timestamp.

## Source hierarchy

1. Official: AMFI, SEBI and official AMC websites.
2. Structured enrichment/failover: mfdata.in, MFapi and maintained public data mirrors.
3. Independent NFO sentinels: Morningstar India, Groww, ET Money, INDmoney, Sharekhan, HDFC Securities, 5paisa, ICICI Direct, Anand Rathi and other configured sources.
4. Discovery: Google News RSS and mutual-fund news sentinels for launches, SIP/STP/SWP restrictions, mergers, renames, fund-manager changes, exit-load/TER changes and similar events.

No secondary/news source silently overrides a conflicting official date. Conflicting data is surfaced in Discovery Watch.

## Official AMC website watch

`refresh_amc_watch.py` obtains member IDs/websites from AMFI member pages and performs a bounded scan of AMC homepages/sitemaps for URLs related to NFOs, notices, addenda, scheme documents, factsheets, TER, managers and statutory disclosures. This is a discovery/verification layer; AMFI/SEBI remain central authoritative references.

## Low-latency browser design

- `funds-lite.json` loads first for search/filter cards.
- `funds.json` loads only when full factsheet/deep fields are required.
- `variants.json` loads only for plan/option details or exports.
- The service worker uses network-first for app/data files so old PWA caches do not silently hide a new build.
- External source crawling does not happen on the client's iPhone/Android device.

## Fund-count model

The dashboard deliberately separates underlying funds from scheme-plan-option records. The bundled seed currently contains:

- 3,734 underlying fund groups; 3,351 active.
- 16,358 total scheme/plan/option records; 14,252 active.
- Active Direct 6,604; Regular 4,111; Legacy/Other 2,985; ETF/Exchange 361; Retail 118; Institutional 73.
- Active Growth 5,229; IDCW/Dividend 7,073; Other 1,809; Bonus 139; Segregated Portfolio 2.

These counts will move when GitHub refresh jobs discover new, renamed, merged or inactive records.

## NFO Centre

NFO status is date-driven: Open, Upcoming, Recently Closed or Discovery Watch. Each record can carry multiple evidence sources and a confidence label. The 8-Aug-2026 build seed contains 33 tracked NFO records: 8 open, 10 upcoming and 15 recently closed, plus a separate conflict/watch item. The first GitHub refresh after deployment should be treated as the production-current snapshot.

## Deep scheme information

Where available, the rotating deep refresh retains:

- benchmark, riskometer, exit load, minimum investment/SIP and fund managers;
- P/E, P/B, portfolio turnover, YTM, modified duration and average maturity;
- equity/debt/cash/other allocation;
- top holdings, top sectors and debt credit-quality buckets;
- fund-manager start dates/tenure;
- alpha, beta, information ratio and tracking error only when an upstream structured source actually supplies them;
- portfolio month/date and deep-data refresh timestamp.

Blank fields are intentional. v8.1 does not fabricate unavailable ratios.

## GitHub repository

Target repository:
`https://github.com/pranavgangurde-mm9/moneymantra9-mf-research/`

See `GITHUB_UPDATE_STEPS.txt` for the beginner upload procedure.

## Important limitation

No public application can guarantee that it searches literally every page on the internet or that every third-party website will always remain parsable. v8.1 therefore uses a broad monitored source universe, source-health checks, failover, cached-good-data retention and visible conflict/staleness indicators. Official documents remain the final verification point for investment communication.
