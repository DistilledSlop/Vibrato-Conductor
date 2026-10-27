#!/usr/bin/env python3
"""Browser-check the real dashboard; optional Playwright is a development-only dependency."""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:8080')
parser.add_argument('--token-file', type=Path, required=True)
parser.add_argument('--output', type=Path, default=Path('.state/dashboard-browser'))
parser.add_argument('--with-workers', action='store_true')
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1080}, device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(args.url, wait_until='networkidle')
    page.get_by_label('Access token').fill(args.token_file.read_text().strip())
    page.get_by_role('button', name='Open console').click()
    page.get_by_role('heading', name='Fleet overview').wait_for()
    assert page.locator('#console').is_visible()
    page.get_by_role('button', name='Inspect gaps').click()
    page.get_by_label('Filter targets').fill('oak_log')
    page.wait_for_function("document.querySelectorAll('#gapList .gap-row').length > 0")
    page.get_by_role('button', name='Close coverage gaps').click()
    if args.with_workers:
        page.wait_for_function("document.querySelectorAll('.worker-card').length > 0", timeout=30000)
        page.wait_for_function("[...document.querySelectorAll('.thumbnail img')].some(i => i.naturalWidth > 0)", timeout=45000)
        card = page.locator('.worker-card').filter(has=page.locator('img:not([hidden])')).first
        card.locator('button.thumbnail').click()
        page.wait_for_function("document.querySelector('#detailFrame')?.naturalWidth > 0", timeout=10000)
        previous = page.locator('#detailFrame').get_attribute('src')
        page.wait_for_function("previous => document.querySelector('#detailFrame').src.endsWith(previous) === false", arg=previous, timeout=20000)
        page.screenshot(path=str(args.output / 'inspection.png'), full_page=True)
        page.get_by_role('button', name='Close worker details').click()
        page.get_by_role('button', name='Table view').click()
        assert page.locator('#workers tr').count() > 0
        page.get_by_role('button', name='Gallery view').click()
    page.screenshot(path=str(args.output / 'desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.screenshot(path=str(args.output / 'mobile.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile horizontal overflow'
    assert not errors, errors
    print('Dashboard browser checks passed (login, coverage search, responsive layout' +
          (', real gallery frames, refreshed inspection, table' if args.with_workers else '') + ')')
    browser.close()
