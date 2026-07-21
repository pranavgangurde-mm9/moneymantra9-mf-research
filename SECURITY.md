# Security notes — MoneyMantra 9 Mutual Fund Research App

This package is a security-hardened build.

## Controls
- No third-party JavaScript or CDN dependencies.
- Strict Content Security Policy.
- API connections allowed only to `mfdata.in`, `api.mfapi.in`, and the AMFI NAV endpoint.
- No `eval`, `new Function`, iframes, camera, microphone, geolocation, payment, USB or Bluetooth permissions.
- Unknown third-party API fallback removed.
- Generated Excel files remain on the device.
- Service worker caches only the fixed application shell and never caches external API responses.

## Recommended deployment
Host this folder on an HTTPS provider and retain the supplied `_headers` file. Do not edit the JavaScript files with unknown online tools or inject advertising/analytics scripts.

## Integrity
Standalone hardened HTML SHA-256: `3294a6cb68fb19767a6020a111724d778c0059b3bc6190df537406176ea89a58`

No software can guarantee immunity from every future threat. Keep the browser, Android device, antivirus and hosting account updated and protected.


The NFO module is restricted to the same approved data providers plus the official AMFI `www.amfiindia.com` NFO page. It does not execute remote scripts.
