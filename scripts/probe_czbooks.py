#!/usr/bin/env python3
"""
probe_czbooks.py — 診斷 czbooks.net 實際可用的搜尋 / 書頁 URL

用法：
  python scripts/probe_czbooks.py 劍來

可選用環境變數：
  PROXY_BASE=https://xxx.workers.dev  → 透過 CF Worker 代理
  PROXY_BASE=""（未設）→ 直連

輸出：對幾個候選 URL 印出 status / size / 是否包含關鍵字 / 可命中的 CSS
       selector 數量，協助快速驗證 scraper.py 的 SITE_CZBOOKS 配置是否正確。
"""
from __future__ import annotations

import sys
import time
import urllib.parse

import cloudscraper
from bs4 import BeautifulSoup


_CANDIDATES = [
    "/s/{q}/1",
    "/s/{q}",
    "/search/{q}/1",
    "/search?q={q}",
    "/?s={q}",
]

_ITEM_SELECTORS = [
    "li.novel-item-wrapper",
    "div.novel-item-wrapper",
    ".novel-list li",
    ".search-result li",
]

_TITLE_SELECTORS = [
    ".novel-item-title",
    "a[href*='/n/']",
    "h3 a",
]


def _session() -> cloudscraper.CloudScraper:
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True},
    )


def _probe(session, base: str, path: str, keyword: str) -> None:
    url = base + path
    try:
        resp = session.get(url, timeout=15)
    except Exception as exc:
        print(f"  [FAIL] {url}  →  {exc}")
        return
    body = resp.text or ""
    has_kw = keyword in body
    print(f"  [{resp.status_code}] size={len(body):>6}  keyword={has_kw}  url={url}")
    if resp.status_code != 200 or not has_kw:
        return
    soup = BeautifulSoup(body, "lxml")
    for sel in _ITEM_SELECTORS:
        items = soup.select(sel)
        if items:
            print(f"    item sel '{sel}' → {len(items)} items")
            first = items[0]
            for tsel in _TITLE_SELECTORS:
                el = first.select_one(tsel)
                if el:
                    txt = el.get_text(strip=True)[:30]
                    href = el.get("href", "")
                    print(f"      title sel '{tsel}' → text={txt!r}  href={href!r}")
                    break
            break


def main() -> None:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "劍來"
    base = "https://czbooks.net"
    session = _session()
    q = urllib.parse.quote(keyword, safe="")

    print(f"Probing czbooks.net for keyword: {keyword}")
    print("-" * 70)
    for tpl in _CANDIDATES:
        path = tpl.replace("{q}", q)
        _probe(session, base, path, keyword)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
