# MoneyMantra 9 — Security-Hardened Future-Ready PWA

# Mutual Fund Research Analytics — Future-Ready Master Sync

This package contains the installable PWA version of the mutual fund research and comparison app.

## Future-ready sync

The **Sync & discover** function now:

- refreshes current NAV, AUM, AUM date and TER for existing schemes;
- scans the complete live scheme master;
- discovers newly launched mutual fund schemes;
- attaches newly created Direct or Regular Growth variants to an existing underlying fund;
- creates a new underlying-fund record when the family does not already exist;
- detects renamed scheme variants by AMFI scheme code;
- de-duplicates records using scheme code, fund family and normalized AMC/scheme names;
- keeps a scheme registry and sync history in IndexedDB;
- uses local storage as a fallback when IndexedDB is unavailable;
- marks records not seen in three complete scans as “Possibly inactive” instead of deleting them;
- includes newly discovered funds in search, comparison, rolling returns and Excel exports.

## Data sources and fallback order

1. mfdata.in scheme master — rich NAV, AUM, TER and family data.
2. AMFI NAVAll — official scheme-code and NAV cross-check when browser access is permitted.
3. MFapi.in — fallback scheme master and NAV history.

## Install on Android

1. Upload the contents of this folder to an HTTPS host such as GitHub Pages, Netlify or Cloudflare Pages.
2. Open `index.html` in Chrome on Android.
3. Choose **Install app** or **Add to Home screen**.

## Important

- New schemes naturally have limited return history. Long-period CAGR and rolling returns appear only after enough NAV history exists.
- Portfolio/factsheet fields are loaded on demand when a fund is opened or compared.
- Verify shortlisted schemes against the latest SID, KIM, AMC factsheet, portfolio disclosure and riskometer.


See `SECURITY.md` for security controls and deployment guidance.


## NFO Centre

The app now includes a separate NFO Centre with:
- Open NFOs
- Upcoming / announced NFOs
- Recently closed NFOs
- Opening date, closing date and countdown
- Offer price, minimum application, reopening date, benchmark and objective where supplied
- Official SID / AMFI source links
- NFO CSV download
- Offline dated snapshot plus live refresh from approved sources

The NFO Centre refreshes whenever **Sync & discover** is run and can also be refreshed separately.


## NFO completeness update
The embedded official-source snapshot dated 21 July 2026 includes NJ Value Fund, NJ Momentum Fund and the other current/recent NFOs identified through AMFI, AMC pages and cross-checks. Refresh NFOs performs an AMC coverage check and reports any gap.
