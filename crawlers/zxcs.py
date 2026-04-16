"""
crawlers/zxcs.py — 知軒藏書（zxcs.me / zxcs.info / zxcs8.com）

zxcs 與其他爬章節站不同：
  - 每本書有預打包的 .rar / .zip / .txt 下載檔
  - 本 crawler 取得下載連結後直接下載並解壓，回傳 .txt 路徑
  - 不走 downloader.download_novel 的章節合併
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tempfile
import zipfile
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseCrawler, BookInfo, ProgressCallback, SearchResult

logger = logging.getLogger(__name__)

_MIRRORS = [
    "https://www.zxcs.me",
    "https://zxcs.me",
    "https://www.zxcs.info",
    "https://zxcs.info",
]


def _sanitize_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:80]


class ZxcsCrawler(BaseCrawler):
    name = "知軒藏書"
    domain_patterns = ["zxcs.me", "zxcs.info", "zxcs8.com", "zxcs.com"]

    def __init__(self):
        self._session = None
        self._base: str = ""

    def _get_session(self):
        if self._session is None:
            from scraper import make_session

            self._session = make_session()
        return self._session

    def _warm_up(self) -> str:
        """找出可用的鏡像。"""
        if self._base:
            return self._base
        session = self._get_session()
        from scraper import _fetch

        for base in _MIRRORS:
            try:
                resp = _fetch(session, base + "/", max_retries=1)
                if resp.status_code == 200:
                    session.headers["Referer"] = base + "/"
                    self._base = base
                    logger.info("[zxcs] using mirror: %s", base)
                    return base
            except Exception as exc:
                logger.warning("[zxcs] mirror %s failed: %s", base, exc)
        self._base = _MIRRORS[0]
        return self._base

    # ---- search ----------------------------------------------------------

    def search(self, keyword: str) -> list[SearchResult]:
        from scraper import _fetch, _decode

        session = self._get_session()
        base = self._warm_up()
        url = f"{base}/?s={keyword}"
        try:
            resp = _fetch(session, url)
        except Exception as exc:
            logger.warning("[zxcs] search failed: %s", exc)
            return []
        soup = BeautifulSoup(_decode(resp), "lxml")

        results: list[SearchResult] = []
        # zxcs 搜尋結果頁常見結構：<div id="plist"><dl><dt><a>《書名》 作者</a></dt>...</dl>
        for a in soup.select("#plist dt a, .post h2 a, article h2 a"):
            href = a.get("href", "")
            text = a.get_text(strip=True)
            if not href or not text:
                continue
            full = href if href.startswith("http") else urljoin(base, href)
            # text 可能是「《XXX》 作者名」，拆開
            m = re.match(r"[《【]?(.+?)[》】]?\s*[ 　]+(.+)", text)
            if m:
                title, author = m.group(1).strip(), m.group(2).strip()
            else:
                title, author = text.strip("《》【】 "), "未知"
            results.append(
                SearchResult(title=title, url=full, author=author, source=self.name)
            )
            if len(results) >= 10:
                break
        logger.info("[zxcs] search '%s' → %d results", keyword, len(results))
        return results

    # ---- book info -------------------------------------------------------

    def get_book_info(self, url: str) -> BookInfo:
        from scraper import _fetch, _decode

        session = self._get_session()
        self._warm_up()
        resp = _fetch(session, url)
        soup = BeautifulSoup(_decode(resp), "lxml")

        og_title = soup.select_one("meta[property='og:title']")
        title = (og_title.get("content") if og_title else "") or (
            soup.select_one("h1, .entry-title").get_text(strip=True)
            if soup.select_one("h1, .entry-title")
            else "未知"
        )

        # 作者
        author = ""
        info_block = soup.select_one("#content .info, .baoinfo, #info")
        if info_block:
            m = re.search(r"作\s*者[:：]\s*(.+)", info_block.get_text("\n"))
            if m:
                author = m.group(1).strip().split("\n")[0].strip()
        author = author or "未知"

        # 封面
        cover = ""
        cover_el = soup.select_one("meta[property='og:image'], #content img, .entry-content img")
        if cover_el:
            cover = cover_el.get("content") or cover_el.get("src") or ""
            if cover and not cover.startswith("http"):
                cover = urljoin(url, cover)

        # 簡介
        desc = ""
        content_div = soup.select_one(".entry-content, #content, article")
        if content_div:
            # 去掉下載按鈕 / 腳本
            for t in content_div.select("script, style, .download, a.down"):
                t.decompose()
            desc = content_div.get_text(" ", strip=True)[:1500]

        # 下載連結
        archives: list[str] = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            low = href.lower()
            if any(low.endswith(ext) for ext in (".txt", ".rar", ".zip", ".7z")) or (
                "download" in low and "zxcs" in low
            ):
                full = href if href.startswith("http") else urljoin(url, href)
                if full not in archives:
                    archives.append(full)

        # 有時下載頁藏在 /soft/ 或 /download.php?id=XXX，需要跳一層
        if not archives:
            download_link = soup.select_one("a[href*='download'], a[href*='/soft']")
            if download_link:
                next_url = urljoin(url, download_link["href"])
                try:
                    resp2 = _fetch(session, next_url)
                    soup2 = BeautifulSoup(_decode(resp2), "lxml")
                    for a in soup2.select("a[href]"):
                        href = a.get("href", "")
                        low = href.lower()
                        if any(low.endswith(ext) for ext in (".txt", ".rar", ".zip", ".7z")):
                            archives.append(urljoin(next_url, href))
                except Exception as exc:
                    logger.warning("[zxcs] follow download page failed: %s", exc)

        return BookInfo(
            title=title,
            url=url,
            author=author,
            description=desc,
            cover_url=cover,
            status="",
            chapters=[],
            archive_urls=archives,
            source=self.name,
        )

    # ---- download --------------------------------------------------------

    async def download(
        self,
        info: BookInfo,
        progress_cb: ProgressCallback = None,
    ) -> list[str]:
        if not info.archive_urls:
            raise RuntimeError("知軒藏書：未找到下載連結，該書可能已下架或需手動取得。")

        session = self._get_session()
        safe = _sanitize_name(info.title or "novel")

        # 優先選 .txt → .zip → .rar
        def _rank(u: str) -> int:
            u = u.lower()
            if u.endswith(".txt"):
                return 0
            if u.endswith(".zip"):
                return 1
            if u.endswith(".rar"):
                return 2
            return 3

        urls = sorted(info.archive_urls, key=_rank)

        last_err: Optional[Exception] = None
        for url in urls:
            try:
                path = await asyncio.to_thread(
                    self._download_and_extract, session, url, safe
                )
                if path:
                    if progress_cb:
                        try:
                            await progress_cb(1, 1)
                        except Exception:
                            pass
                    return [path]
            except Exception as exc:
                logger.warning("[zxcs] archive %s failed: %s", url, exc)
                last_err = exc

        raise RuntimeError(f"知軒藏書下載失敗：{last_err}")

    def _download_and_extract(self, session, url: str, safe_title: str) -> str:
        from scraper import _fetch

        logger.info("[zxcs] downloading archive: %s", url)
        resp = _fetch(session, url, max_retries=3)
        data = resp.content
        low = url.lower()

        if low.endswith(".txt"):
            path = f"/tmp/{safe_title}.txt"
            with open(path, "wb") as fh:
                fh.write(data)
            return path

        if low.endswith(".zip"):
            return self._extract_zip(data, safe_title)

        if low.endswith(".rar") or low.endswith(".7z"):
            return self._extract_rar(data, safe_title, url)

        # 嘗試從 Content-Disposition 推斷
        cd = resp.headers.get("Content-Disposition", "")
        if ".zip" in cd.lower():
            return self._extract_zip(data, safe_title)
        if ".rar" in cd.lower():
            return self._extract_rar(data, safe_title, url)
        # 當作文字
        path = f"/tmp/{safe_title}.txt"
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def _extract_zip(self, data: bytes, safe_title: str) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                txt_names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
                if not txt_names:
                    raise RuntimeError("ZIP 內找不到 .txt 檔")
                # 選最大的 txt（通常是主檔）
                txt_names.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
                raw = zf.read(txt_names[0])
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"ZIP 解析失敗：{exc}")
        return self._write_txt(raw, safe_title)

    def _extract_rar(self, data: bytes, safe_title: str, url: str) -> str:
        try:
            import rarfile  # type: ignore
        except ImportError:
            raise RuntimeError("需要 rarfile 套件與系統 unrar 才能解壓 .rar；請改下載其他格式")

        # rarfile 需要實體檔案
        with tempfile.NamedTemporaryFile(suffix=".rar", delete=False) as tf:
            tf.write(data)
            tmp_path = tf.name
        try:
            with rarfile.RarFile(tmp_path) as rf:
                txt_names = [n for n in rf.namelist() if n.lower().endswith(".txt")]
                if not txt_names:
                    raise RuntimeError("RAR 內找不到 .txt 檔")
                txt_names.sort(key=lambda n: rf.getinfo(n).file_size, reverse=True)
                raw = rf.read(txt_names[0])
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return self._write_txt(raw, safe_title)

    def _write_txt(self, raw: bytes, safe_title: str) -> str:
        # 嘗試以 GB18030 / UTF-8 解碼，若為 BOM 或 UTF-8 直接保留
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gb18030", errors="replace")
        path = f"/tmp/{safe_title}.txt"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path
