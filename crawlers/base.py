"""
crawlers/base.py — BaseCrawler 抽象介面與共用資料結構
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Optional


ProgressCallback = Optional[Callable[[int, int], Awaitable[None]]]


@dataclass
class SearchResult:
    title: str
    url: str
    author: str = "未知"
    latest: str = "未知"
    source: str = ""  # crawler name, for UI

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "latest": self.latest,
            "_crawler": self.source,
        }


@dataclass
class BookInfo:
    title: str
    url: str
    author: str = "未知"
    description: str = ""
    cover_url: str = ""
    status: str = ""
    chapters: list[dict] = field(default_factory=list)
    # 若 crawler 是「直接下載 txt」類型（例如 zxcs），可放壓縮檔下載連結
    archive_urls: list[str] = field(default_factory=list)
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "description": self.description,
            "cover_url": self.cover_url,
            "status": self.status,
            "chapters": self.chapters,
            "archive_urls": self.archive_urls,
            "_crawler": self.source,
        }


class BaseCrawler(ABC):
    """
    每個站台實作三個操作：search / get_book_info / download。
    search 與 get_book_info 是同步（內部可視需要走 HTTP），
    download 為 async（整合進度回報與背景任務）。
    """

    name: str = ""
    domain_patterns: list[str] = []  # URL 網域片段，用於 URL routing

    def supports_url(self, url: str) -> bool:
        return any(p in url for p in self.domain_patterns)

    @abstractmethod
    def search(self, keyword: str) -> list[SearchResult]: ...

    @abstractmethod
    def get_book_info(self, url: str) -> BookInfo: ...

    @abstractmethod
    async def download(
        self,
        info: BookInfo,
        progress_cb: ProgressCallback = None,
    ) -> list[str]:
        """下載並回傳本機檔案路徑清單（呼叫方負責在 send 後刪除）。"""
