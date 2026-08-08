#!/usr/bin/env python3
"""Lightweight 24x7 discovery refresh.

Runs more frequently than the full scheme universe job so NFO openings/closures,
SIF offers, SEBI filing leads and scheme operational-change alerts can update on
weekends and market holidays without rebuilding 14k+ scheme records.
"""
from refresh_fast import (
    DATA, NOW, HEALTH, SOURCE_REGISTRY, load_json, write_json,
    refresh_nfos, refresh_product_watch, refresh_sebi_pipeline, refresh_scheme_alerts
)


def main():
    refresh_sebi_pipeline()
    nfos = refresh_nfos()
    products = refresh_product_watch()
    alerts = refresh_scheme_alerts()

    meta = load_json(DATA / 'meta.json', {})
    meta['schemaVersion'] = 8
    meta['nfoRefreshAt'] = NOW.isoformat()
    meta['alertRefreshAt'] = NOW.isoformat()
    meta['discoveryRefreshAt'] = NOW.isoformat()
    meta['discoveryRefreshPolicy'] = 'Every 30 minutes, 24x7 including weekends and market holidays.'
    sh = meta.setdefault('sourceHealth', {})
    sh.update(HEALTH)
    write_json(DATA / 'meta.json', meta, indent=2)

    existing = load_json(DATA / 'source_health.json', {'sources': {}})
    merged = existing.get('sources', {}) if isinstance(existing, dict) else {}
    merged.update(HEALTH)
    write_json(DATA / 'source_health.json', {
        'generatedAt': NOW.isoformat(),
        'sources': merged,
        'registry': SOURCE_REGISTRY
    }, indent=2)

    print({
        'verifiedNfo': len(nfos.get('items', [])),
        'nfoWatch': len(nfos.get('watch', [])) + len(nfos.get('newsWatch', [])),
        'productWatch': len(products.get('items', [])),
        'schemeAlerts': len(alerts.get('items', [])),
    })


if __name__ == '__main__':
    main()
