"""
crawlers/fanqie.py — 番茄小說（fanqienovel）

番茄小說網頁版使用混淆字型，直接爬網頁文字會拿到「亂碼（反混淆表才能還原）」。
本 crawler 走公開 reader API（社群已公布），回傳 HTML 或純文字 content。

若 API 行為變動（header / 簽章 / 鎖國），此 crawler 會失敗並記 log；
使用者可透過別的站台補救。
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseCrawler, BookInfo, ProgressCallback, SearchResult

logger = logging.getLogger(__name__)


_WEB_BASE = "https://fanqienovel.com"
_API_BASE = "https://api5-normal-sinfonlineb.fqnovel.com"
_COMMON_QS = {
    "aid": "1967",
    "app_name": "novelapp",
    "version_code": "999",
    "channel": "0",
    "iid": "466614321180296",
    "device_id": "0",
    "device_type": "Web",
    "device_platform": "web",
    "os_version": "10",
    "version_name": "6.0.0",
}


def _default_params(extra: dict) -> dict:
    p = dict(_COMMON_QS)
    p.update(extra)
    return p


class FanqieCrawler(BaseCrawler):
    name = "番茄小說"
    domain_patterns = ["fanqienovel.com", "fanqie"]

    def __init__(self):
        self._session = None

    def _get_session(self):
        if self._session is None:
            from scraper import make_session

            self._session = make_session()
            self._session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/plain, */*",
                    "Referer": _WEB_BASE + "/",
                }
            )
        return self._session

    def _api_get(self, path: str, params: dict) -> dict:
        from scraper import _fetch
        import urllib.parse

        session = self._get_session()
        qs = urllib.parse.urlencode(_default_params(params))
        url = f"{_API_BASE}{path}?{qs}"
        resp = _fetch(session, url, max_retries=2)
        return resp.json()

    # ---- search ----------------------------------------------------------

    def search(self, keyword: str) -> list[SearchResult]:
        """用網頁搜尋頁擷取 book_id，比 API 穩。"""
        from scraper import _fetch, _decode
        import urllib.parse

        session = self._get_session()
        try:
            qs = urllib.parse.urlencode({"query": keyword})
            resp = _fetch(session, f"{_WEB_BASE}/search?{qs}", max_retries=1)
        except Exception as exc:
            logger.warning("[fanqie] search request failed: %s", exc)
            return []

        soup = BeautifulSoup(_decode(resp), "lxml")
        results: list[SearchResult] = []
        # 番茄搜尋結果卡片 a[href^="/page/"] 或 a[href^="/book/"]
        seen: set[str] = set()
        for a in soup.select("a[href*='/page/'], a[href*='/book/']"):
            href = a.get("href", "")
            m = re.search(r"/(?:page|book)/(\d+)", href)
            if not m:
                continue
            book_id = m.group(1)
            if book_id in seen:
                continue
            seen.add(book_id)
            title = (a.get("title") or a.get_text(strip=True) or "").strip()
            if not title or len(title) > 40:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=f"{_WEB_BASE}/page/{book_id}",
                    source=self.name,
                )
            )
            if len(results) >= 10:
                break
        logger.info("[fanqie] search '%s' → %d results", keyword, len(results))
        return results

    # ---- book info -------------------------------------------------------

    def _book_id(self, url: str) -> str:
        m = re.search(r"/(?:page|book|reader)/(\d+)", url)
        if not m:
            raise RuntimeError(f"無法從 URL 解析 book_id: {url}")
        return m.group(1)

    def get_book_info(self, url: str) -> BookInfo:
        book_id = self._book_id(url)
        # 目錄 API
        try:
            dir_data = self._api_get(
                "/reading/bookapi/directory/all_items/v/",
                {"book_id": book_id},
            )
        except Exception as exc:
            logger.warning("[fanqie] directory api failed: %s", exc)
            dir_data = {}

        # 書籍資訊 API
        try:
            info_data = self._api_get(
                "/reading/bookapi/publish/detail/v/",
                {"book_id": book_id},
            )
        except Exception as exc:
            logger.warning("[fanqie] detail api failed: %s", exc)
            info_data = {}

        book = (info_data.get("data") or {}).get("book_info") or {}
        title = book.get("book_name") or "未知"
        author = book.get("author") or "未知"
        desc = (book.get("abstract") or book.get("description") or "").strip()
        cover = book.get("thumb_url") or book.get("audio_thumb_uri_hd") or ""
        status_code = book.get("creation_status")
        status = "完結" if str(status_code) == "0" else ("連載中" if status_code is not None else "")

        chapters: list[dict] = []
        items = (
            (dir_data.get("data") or {}).get("item_data_list")
            or (dir_data.get("data") or {}).get("all_item_ids")
            or []
        )
        # 兩種 response 格式：list[dict] 或 list[str]
        for idx, it in enumerate(items):
            if isinstance(it, dict):
                cid = str(it.get("item_id") or it.get("id") or "")
                ctitle = it.get("title") or f"第 {idx+1} 章"
            else:
                cid = str(it)
                ctitle = f"第 {idx+1} 章"
            if cid:
                chapters.append(
                    {"title": ctitle, "url": f"fanqie://{book_id}/{cid}"}
                )

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

    # ---- download --------------------------------------------------------

    def _fetch_chapter(self, book_id: str, item_id: str) -> tuple[str, str]:
        try:
            data = self._api_get(
                "/reading/reader/full/v/",
                {"item_ids": item_id, "book_id": book_id},
            )
        except Exception as exc:
            logger.error("[fanqie] chapter %s failed: %s", item_id, exc)
            return f"章節 {item_id}", "[本章下載失敗]"

        item = (data.get("data") or {}).get("item_content_list") or []
        if not item:
            # 另一種格式
            item = (data.get("data") or {}).get("items") or []
        if not item:
            return f"章節 {item_id}", "[本章內容為空]"

        entry = item[0]
        title = entry.get("title") or f"章節 {item_id}"
        raw = entry.get("content") or ""
        # content 可能是 HTML
        if "<" in raw:
            soup = BeautifulSoup(raw, "lxml")
            for t in soup.select("script, style"):
                t.decompose()
            text = soup.get_text("\n")
        else:
            text = raw
        # 清理多餘空白
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return title, "\n".join(lines)

    async def download(
        self,
        info: BookInfo,
        progress_cb: ProgressCallback = None,
    ) -> list[str]:
        if not info.chapters:
            raise RuntimeError("番茄小說：章節清單為空")
        book_id = self._book_id(info.url)
        total = len(info.chapters)
        sem = asyncio.Semaphore(5)
        results: list[tuple[int, str, str]] = []
        done = 0
        lock = asyncio.Lock()

        async def worker(idx: int, item_id: str):
            nonlocal done
            async with sem:
                title, text = await asyncio.to_thread(self._fetch_chapter, book_id, item_id)
            async with lock:
                done += 1
                if progress_cb and done % 20 == 0:
                    try:
                        await progress_cb(done, total)
                    except Exception:
                        pass
            results.append((idx, title, text))

        tasks = []
        for idx, ch in enumerate(info.chapters, 1):
            item_id = ch["url"].split("/")[-1]
            tasks.append(asyncio.create_task(worker(idx, item_id)))
        await asyncio.gather(*tasks, return_exceptions=True)

        results.sort(key=lambda x: x[0])

        from downloader import write_novel_txt

        return write_novel_txt(info.title, [(t, c) for _, t, c in results])
