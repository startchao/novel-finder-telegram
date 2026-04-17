"""
crawlers/registry.py — 所有 crawler 註冊與路由

負責：
  - 建立所有 crawler 實例
  - 依 URL 路由到正確 crawler
  - search_all：支援單站模式（快）與多站模式（as_completed 早返回）
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .base import BaseCrawler, BookInfo, SearchResult
from .biquge import BiqugeCrawler
from .configurable import ConfigurableCrawler
from .fanqie import FanqieCrawler
from .zxcs import ZxcsCrawler

logger = logging.getLogger(__name__)


def _czbooks_only() -> list[ConfigurableCrawler]:
    """只包 czbooks，其餘站 selectors 已全面陳舊，暫時下架。"""
    from scraper import SITE_CZBOOKS

    return [ConfigurableCrawler(SITE_CZBOOKS)]


# 2026-04 搶救版：只註冊「實測可用」的兩個站，其他 crawler 類別暫不啟用。
# 下一次迭代會把 lncrawl adapter 和其餘 crawlers 重新啟用。
CRAWLERS: list[BaseCrawler] = [
    *_czbooks_only(),
    FanqieCrawler(),
]

# 保留 import 以避免 lint 警告（下一次迭代會用到）
_UNUSED = (ZxcsCrawler, BiqugeCrawler)


# 全站選項的標記值
ALL_SOURCES = "全部站台"


def list_sources() -> list[str]:
    """回傳所有 crawler 的名稱清單（可作為 /source 選單用）"""
    return [c.name for c in CRAWLERS]


def get_crawler_by_name(name: str) -> Optional[BaseCrawler]:
    for c in CRAWLERS:
        if c.name == name:
            return c
    return None


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


async def search_single(crawler: BaseCrawler, keyword: str, limit: int = 20) -> list[SearchResult]:
    """單站搜尋：直接回傳該站結果。"""
    results = await asyncio.to_thread(_search_sync, crawler, keyword)
    logger.info("[%s] search '%s' → %d results", crawler.name, keyword, len(results))
    return results[:limit]


async def search_all(
    keyword: str,
    source: Optional[str] = None,
    max_per_site: int = 5,
    total_limit: int = 25,
    early_return_after: float = 10.0,
    hard_timeout: float = 22.0,
) -> list[SearchResult]:
    """
    搜尋小說：
      - source 指定站名 → 單站模式（快）
      - source 為 None / "全部站台" → 多站並行，as_completed 收集，達條件早返回
    """
    # 單站快路徑
    if source and source != ALL_SOURCES:
        crawler = get_crawler_by_name(source)
        if crawler is None:
            logger.warning("Unknown source: %s", source)
            return []
        return await search_single(crawler, keyword, limit=total_limit)

    # 多站模式：並行 + 早返回
    tasks = [
        asyncio.create_task(asyncio.to_thread(_search_sync, c, keyword))
        for c in CRAWLERS
    ]
    start = time.monotonic()
    all_results: list[SearchResult] = []

    async def collect():
        for fut in asyncio.as_completed(tasks, timeout=hard_timeout):
            try:
                batch = await fut
            except Exception as exc:
                logger.warning("search task error: %s", exc)
                continue
            if batch:
                all_results.extend(batch[:max_per_site])
            elapsed = time.monotonic() - start
            # 早返回：已累積足夠結果 + 至少 4 秒過了
            if len(all_results) >= 10 and elapsed > 4.0:
                break
            # 或超過早返回時間，只要有任何結果就收工
            if all_results and elapsed > early_return_after:
                break

    try:
        await collect()
    except asyncio.TimeoutError:
        logger.info("search_all '%s' hit hard timeout", keyword)

    # 取消未完成的 task
    for t in tasks:
        if not t.done():
            t.cancel()

    # 依書名去重
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
    logger.info(
        "search_all '%s' → %d (from %d raw, %.1fs)",
        keyword, len(deduped), len(all_results), time.monotonic() - start,
    )
    return deduped


def get_book_info(url: str) -> BookInfo:
    crawler = detect_crawler(url)
    if crawler is None:
        raise RuntimeError(f"找不到對應 crawler：{url}")
    return crawler.get_book_info(url)
