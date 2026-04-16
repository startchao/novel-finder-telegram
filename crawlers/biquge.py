"""
crawlers/biquge.py — 筆趣閣系列（多鏡像）

由於筆趣閣系統的鏡像眾多但 DOM 結構大同小異，本 crawler 維護一組鏡像清單，
啟動時做 health check 選出第一個可達的鏡像，其餘作為 fallback。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseCrawler, BookInfo, ProgressCallback, SearchResult

logger = logging.getLogger(__name__)


_MIRRORS = [
    "https://www.biqu5200.net",
    "https://www.biquge.info",
    "https://www.biqudu.net",
    "https://www.biqugebar.net",
    "https://www.xbiquge.la",
    "https://www.xbiquge.so",
]

_HEALTH_TTL = 600  # 健康檢查快取 10 分鐘


class BiqugeCrawler(BaseCrawler):
    name = "筆趣閣"
    domain_patterns = [
        "biqu5200", "biquge", "biqudu", "biqugebar", "xbiquge",
    ]

    def __init__(self):
        self._session = None
        self._mirror: str = ""
        self._mirror_checked_at: float = 0.0

    def _get_session(self):
        if self._session is None:
            from scraper import make_session

            self._session = make_session()
        return self._session

    def _pick_mirror(self) -> str:
        now = time.time()
        if self._mirror and (now - self._mirror_checked_at) < _HEALTH_TTL:
            return self._mirror
        from scraper import _fetch

        session = self._get_session()
        for base in _MIRRORS:
            try:
                resp = _fetch(session, base + "/", max_retries=1)
                if resp.status_code == 200:
                    self._mirror = base
                    self._mirror_checked_at = now
                    session.headers["Referer"] = base + "/"
                    logger.info("[biquge] using mirror: %s", base)
                    return base
            except Exception as exc:
                logger.warning("[biquge] mirror %s down: %s", base, exc)
        # 都不通時也回傳第一個作為 best-effort
        self._mirror = _MIRRORS[0]
        self._mirror_checked_at = now
        return self._mirror

    def search(self, keyword: str) -> list[SearchResult]:
        from scraper import _fetch, _decode

        session = self._get_session()
        base = self._pick_mirror()
        # 筆趣閣系列大多走 /search.php?searchkey= 或 /modules/article/search.php
        endpoints = [
            f"{base}/search.php?keyword={keyword}",
            f"{base}/search.php?searchkey={keyword}",
            f"{base}/modules/article/search.php?searchkey={keyword}",
        ]
        soup = None
        for ep in endpoints:
            try:
                resp = _fetch(session, ep, max_retries=1)
                soup = BeautifulSoup(_decode(resp), "lxml")
                if soup.select(".result-list, .novelslist2, .bookname, .grid"):
                    break
            except Exception as exc:
                logger.warning("[biquge] search endpoint %s failed: %s", ep, exc)
                continue
        if soup is None:
            return []

        results: list[SearchResult] = []
        # 多種候選 li
        candidates = (
            soup.select(".result-list .result-item")
            or soup.select(".novelslist2 li")
            or soup.select(".grid tr")
            or soup.select("ul.list li")
        )
        for item in candidates[:10]:
            link = item.select_one("h3 a, .bookname a, .result-game-item-title a, a")
            if not link:
                continue
            title = (link.get("title") or link.get_text(strip=True)).strip()
            href = link.get("href", "")
            if not title or not href:
                continue
            full = href if href.startswith("http") else urljoin(base, href)
            author_el = item.select_one(".author, .result-game-item-info-tag-title, .writer")
            author = author_el.get_text(strip=True) if author_el else "未知"
            author = author.replace("作者：", "").replace("作者:", "").strip() or "未知"
            results.append(
                SearchResult(title=title, url=full, author=author, source=self.name)
            )
        logger.info("[biquge] search '%s' → %d results", keyword, len(results))
        return results

    def get_book_info(self, url: str) -> BookInfo:
        from scraper import _fetch, _decode

        session = self._get_session()
        self._pick_mirror()
        resp = _fetch(session, url)
        soup = BeautifulSoup(_decode(resp), "lxml")

        title_el = soup.select_one("#info h1, .book h1, h1")
        title = title_el.get_text(strip=True) if title_el else "未知"

        author = ""
        info = soup.select_one("#info")
        if info:
            import re as _re

            m = _re.search(r"作\s*者[:：]?\s*([^\s]+)", info.get_text(" "))
            if m:
                author = m.group(1).strip()
        author = author or "未知"

        cover = ""
        cover_el = soup.select_one(
            "meta[property='og:image'], #fmimg img, .cover img, .book img"
        )
        if cover_el:
            cover = cover_el.get("content") or cover_el.get("src") or ""
            if cover and not cover.startswith("http"):
                cover = urljoin(url, cover)

        desc = ""
        desc_el = soup.select_one("#intro, .intro, #book-intro, meta[property='og:description']")
        if desc_el:
            if desc_el.name == "meta":
                desc = desc_el.get("content", "").strip()
            else:
                desc = desc_el.get_text(" ", strip=True)

        status = ""
        status_el = soup.select_one("meta[property='og:novel:status']")
        if status_el:
            status = status_el.get("content", "").strip()

        # 章節列表
        chapters: list[dict] = []
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        for sel in ("#list dl dd a", "#list a", ".listmain a", "#chapters a"):
            els = soup.select(sel)
            if not els:
                continue
            for a in els:
                href = a.get("href", "")
                t = a.get_text(strip=True)
                if not href or not t or len(t) < 2:
                    continue
                full = href if href.startswith("http") else urljoin(origin, href)
                chapters.append({"title": t, "url": full})
            break

        return BookInfo(
            title=title,
            url=url,
            author=author,
            description=desc,
            cover_url=cover,
            status=status,
            chapters=chapters,
            source=self.name,
        )

    async def download(
        self,
        info: BookInfo,
        progress_cb: ProgressCallback = None,
    ) -> list[str]:
        from downloader import download_novel

        return await download_novel(info.title, info.chapters, progress_cb)
