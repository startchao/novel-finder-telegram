"""
crawlers/registry.py — 所有 crawler 註冊與路由

負責：
  - 建立所有 crawler 實例
  - 依 URL 路由到正確 crawler
  - 並行 search_all 合併多站結果
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .base import BaseCrawler, BookInfo, SearchResult
from .biquge import BiqugeCrawler
from .configurable import ConfigurableCrawler
from .fanqie import FanqieCrawler
from .zxcs import ZxcsCrawler

logger = logging.getLogger(__name__)


def _build_configurable() -> list[ConfigurableCrawler]:
    """從 scraper.SITES 取既有 6 個站台包成 ConfigurableCrawler。"""
    from scraper import SITES

    return [ConfigurableCrawler(s) for s in SITES]


CRAWLERS: list[BaseCrawler] = [
    ZxcsCrawler(),
    BiqugeCrawler(),
    FanqieCrawler(),
    *_build_configurable(),
]


def detect_crawler(url: str) -> Optional[BaseCrawler]:
    for c in CRAWLERS:
        if c.supports_url(url):
            return c
    return None


def _search_sync(crawler: BaseCrawler, keyword: str) -> list[SearchResult]:
    try:
        return crawler.search(keyword)
    except Exception as exc:
        logger.warning("[%s] search error: %s", crawler.name, exc)
        return []


async def search_all(keyword: str, max_per_site: int = 5, total_limit: int = 25) -> list[SearchResult]:
    """並行多站搜尋並合併結果。"""
    tasks = [
        asyncio.to_thread(_search_sync, c, keyword) for c in CRAWLERS
    ]
    all_results: list[SearchResult] = []
    for batch in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(batch, Exception) or not batch:
            continue
        for r in batch[:max_per_site]:
            all_results.append(r)
    # 依書名去重（同書不同站取第一個）
    seen: set[str] = set()
    deduped: list[SearchResult] = []
    for r in all_results:
        key = r.title.strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
        if len(deduped) >= total_limit:
            break
    logger.info("search_all '%s' → %d (dedup from %d)", keyword, len(deduped), len(all_results))
    return deduped


def get_book_info(url: str) -> BookInfo:
    crawler = detect_crawler(url)
    if crawler is None:
        raise RuntimeError(f"找不到對應 crawler：{url}")
    return crawler.get_book_info(url)
