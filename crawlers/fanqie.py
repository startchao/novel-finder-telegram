"""
crawlers/fanqie.py — 番茄小說（fanqienovel）

核心 API 與字型反混淆表參考自 github.com/ying-ck/fanqienovel-downloader
（Apache-2.0，作者 Yck & qxqycb & lingo34），感謝他們的維護工作。

策略：
  - 搜尋：api5-normal-lf.fqnovel.com 搜尋 API（JSON）
  - 書籍頁：fanqienovel.com/page/<id> HTML 解析 h1/author/chapter list
  - 章節內文：fanqienovel.com/api/reader/full?itemId=<cid> JSON → 再用
    crawlers/fanqie_charset.json 把 private-use 字元還原回真正的中文
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Optional

from bs4 import BeautifulSoup

from .base import BaseCrawler, BookInfo, ProgressCallback, SearchResult

logger = logging.getLogger(__name__)


_WEB_BASE = "https://fanqienovel.com"
_API_BASE = "https://api5-normal-lf.fqnovel.com"
_SEARCH_PARAMS = {
    "aid": "1967",
    "channel": "0",
    "os_version": "0",
    "device_type": "0",
    "device_platform": "0",
    "iid": "466614321180296",
    "passback": "0",
    "version_code": "999",
}

# 反混淆私有區間（見 ying-ck 原始碼）
_CODE_RANGES = [(58344, 58715), (58345, 58716)]

_CHARSET_PATH = os.path.join(os.path.dirname(__file__), "fanqie_charset.json")
_charset_cache: Optional[list[list[str]]] = None


def _load_charset() -> list[list[str]]:
    global _charset_cache
    if _charset_cache is None:
        try:
            with open(_CHARSET_PATH, "r", encoding="utf-8") as f:
                _charset_cache = json.load(f)
        except Exception as exc:
            logger.warning("fanqie charset load failed: %s", exc)
            _charset_cache = [[], []]
    return _charset_cache


def _decode_fanqie(content: str) -> str:
    """把 private-use 字元還原回中文。兩個 mode 各試一次，取能解較多字的版本。"""
    if not content:
        return ""
    charset = _load_charset()
    best = content
    best_hits = 0
    for mode in (0, 1):
        if mode >= len(charset) or not charset[mode]:
            continue
        lo, hi = _CODE_RANGES[mode]
        out: list[str] = []
        hits = 0
        for ch in content:
            uni = ord(ch)
            if lo <= uni <= hi:
                bias = uni - lo
                if 0 <= bias < len(charset[mode]) and charset[mode][bias] != "?":
                    out.append(charset[mode][bias])
                    hits += 1
                    continue
            out.append(ch)
        if hits > best_hits:
            best_hits = hits
            best = "".join(out)
    return best


class FanqieCrawler(BaseCrawler):
    name = "番茄小說"
    domain_patterns = ["fanqienovel.com", "fqnovel.com", "fanqie"]

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

    # ---- search ----------------------------------------------------------

    def search(self, keyword: str) -> list[SearchResult]:
        from scraper import _fetch
        import urllib.parse

        session = self._get_session()
        params = dict(_SEARCH_PARAMS)
        params["query"] = keyword
        qs = urllib.parse.urlencode(params)
        url = f"{_API_BASE}/reading/bookapi/search/page/v/?{qs}"

        try:
            resp = _fetch(session, url, max_retries=2)
        except Exception as exc:
            logger.warning("[fanqie] search request failed: %s", exc)
            return []

        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("[fanqie] search json parse failed: %s", exc)
            return []

        items = data.get("data") or []
        if not isinstance(items, list):
            items = []

        results: list[SearchResult] = []
        for it in items[:20]:
            book_id = str(it.get("book_id") or it.get("id") or "")
            title = (it.get("book_name") or "").strip()
            author = (it.get("author") or "").strip() or "未知"
            if not book_id or not title:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=f"{_WEB_BASE}/page/{book_id}",
                    author=author,
                    source=self.name,
                )
            )
        logger.info("[fanqie] search '%s' → %d results", keyword, len(results))
        return results

    # ---- book info -------------------------------------------------------

    def _book_id(self, url: str) -> str:
        m = re.search(r"/(?:page|book|reader)/(\d+)", url)
        if not m:
            raise RuntimeError(f"無法從 URL 解析 book_id: {url}")
        return m.group(1)

    def get_book_info(self, url: str) -> BookInfo:
        from scraper import _fetch, _decode

        book_id = self._book_id(url)
        session = self._get_session()

        page_url = f"{_WEB_BASE}/page/{book_id}"
        resp = _fetch(session, page_url, max_retries=2)
        html = _decode(resp)
        soup = BeautifulSoup(html, "lxml")

        # 標題 / 作者 / 狀態 / 封面 / 簡介
        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else "未知"

        author = "未知"
        # 作者多半在 ld+json
        ld = soup.select_one('script[type="application/ld+json"]')
        if ld and ld.string:
            try:
                ldd = json.loads(ld.string)
                if isinstance(ldd, dict):
                    authors = ldd.get("author") or []
                    if isinstance(authors, list) and authors:
                        author = (authors[0].get("name") or "未知").strip()
                    elif isinstance(authors, dict):
                        author = (authors.get("name") or "未知").strip()
            except Exception:
                pass

        status_el = soup.select_one("span.info-label-yellow, .info-label-yellow")
        status = status_el.get_text(strip=True) if status_el else ""

        cover = ""
        og_image = soup.select_one("meta[property='og:image']")
        if og_image:
            cover = (og_image.get("content") or "").strip()

        desc = ""
        og_desc = soup.select_one("meta[property='og:description']") or soup.select_one(
            "meta[name='description']"
        )
        if og_desc:
            desc = (og_desc.get("content") or "").strip()

        # 章節
        chapters: list[dict] = []
        # 番茄頁面章節在 div.chapter 內 a 節點
        for a in soup.select("div.chapter a[href*='/reader/']"):
            href = a.get("href", "")
            m = re.search(r"/reader/(\d+)", href)
            if not m:
                continue
            cid = m.group(1)
            ctitle = a.get_text(strip=True) or f"第 {len(chapters)+1} 章"
            chapters.append({"title": ctitle, "url": f"fanqie://{book_id}/{cid}"})

        # Fallback：若上面選不到，嘗試 a[href^="/reader/"]
        if not chapters:
            for a in soup.select("a[href^='/reader/']"):
                href = a.get("href", "")
                m = re.search(r"/reader/(\d+)", href)
                if not m:
                    continue
                cid = m.group(1)
                ctitle = a.get_text(strip=True) or f"第 {len(chapters)+1} 章"
                chapters.append({"title": ctitle, "url": f"fanqie://{book_id}/{cid}"})

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
        from scraper import _fetch

        session = self._get_session()
        url = f"{_WEB_BASE}/api/reader/full?itemId={item_id}"
        try:
            resp = _fetch(session, url, max_retries=2)
            data = resp.json()
        except Exception as exc:
            logger.warning("[fanqie] chapter %s fetch failed: %s", item_id, exc)
            return f"章節 {item_id}", "[本章下載失敗]"

        cd = ((data.get("data") or {}).get("chapterData") or {})
        title = cd.get("title") or f"章節 {item_id}"
        raw = cd.get("content") or ""
        if not raw:
            return title, "[本章內容為空]"

        # content 有時是 HTML（<p>…</p>），有時是純文字
        if "<" in raw and ">" in raw:
            soup = BeautifulSoup(raw, "lxml")
            for t in soup.select("script, style"):
                t.decompose()
            text = soup.get_text("\n")
        else:
            text = raw

        text = _decode_fanqie(text)
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
                if progress_cb and (done == total or done % 20 == 0):
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
