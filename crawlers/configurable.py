"""
crawlers/configurable.py — 包裝 scraper.py 中的 SiteConfig 站台。

重用 scraper.py 已有的 HTTP / 解析邏輯，所有 6 個 SiteConfig-based 站台
透過此包裝類別統一以 BaseCrawler 介面供 registry 使用。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .base import BaseCrawler, BookInfo, ProgressCallback, SearchResult

if TYPE_CHECKING:
    from scraper import SiteConfig

logger = logging.getLogger(__name__)


class ConfigurableCrawler(BaseCrawler):
    """將既有 SiteConfig 包成 BaseCrawler。"""

    def __init__(self, site: "SiteConfig"):
        self.site = site
        self.name = site.name
        host = urlparse(site.base_url).netloc.replace("www.", "")
        # 只比對主要網域片段（去掉常見 TLD 後綴混用）
        core = host.split(".")[0]
        self.domain_patterns = [host, core]

    def search(self, keyword: str) -> list[SearchResult]:
        from scraper import _search_from_site  # 延遲匯入避免循環

        try:
            raw = _search_from_site(self.site, keyword)
        except Exception as exc:
            logger.warning("[%s] search failed: %s", self.name, exc)
            return []
        results: list[SearchResult] = []
        for n in raw:
            results.append(
                SearchResult(
                    title=n["title"],
                    url=n["url"],
                    author=n.get("author", "未知") or "未知",
                    latest=n.get("latest", "未知") or "未知",
                    source=self.name,
                )
            )
        return results

    def get_book_info(self, url: str) -> BookInfo:
        from scraper import get_book_info as _raw_get_book_info

        data = _raw_get_book_info(url)
        return BookInfo(
            title=data.get("title", "未知"),
            url=url,
            author=data.get("author", "未知"),
            description=data.get("description", ""),
            cover_url=data.get("cover_url", ""),
            status=data.get("status", ""),
            chapters=data.get("chapters", []),
            source=self.name,
        )

    async def download(
        self,
        info: BookInfo,
        progress_cb: ProgressCallback = None,
    ) -> list[str]:
        # 交給 downloader.download_novel 做通用章節合併
        from downloader import download_novel

        return await download_novel(info.title, info.chapters, progress_cb)
