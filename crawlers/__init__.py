"""
crawlers — 多站台爬蟲模組

- base: BaseCrawler 抽象介面、SearchResult / BookInfo dataclass
- configurable: 包裝 scraper.py 的 SiteConfig 6 個站台
- zxcs: 知軒藏書（預打包 txt 下載）
- biquge: 筆趣閣系列（多鏡像）
- fanqie: 番茄小說（官方 API）
- registry: 統一註冊表，提供 search_all / detect
"""
from .base import BaseCrawler, SearchResult, BookInfo
from .registry import CRAWLERS, search_all, detect_crawler, get_book_info

__all__ = [
    "BaseCrawler",
    "SearchResult",
    "BookInfo",
    "CRAWLERS",
    "search_all",
    "detect_crawler",
    "get_book_info",
]
