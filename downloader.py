"""
downloader.py — 小說章節下載、內容合併、TXT 產製與分割

特性：
  - 以 asyncio.Semaphore(5) 並行抓取章節，保留順序
  - 每 20 章觸發進度 callback
  - 單檔 > 45 MB 時自動切分多個 Part（對齊換行符）
"""

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Optional

from scraper import get_chapter_content, make_session

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 45 * 1024 * 1024  # 45 MB
CHAPTER_CONCURRENCY = 5

ProgressCallback = Optional[Callable[[int, int], Awaitable[None]]]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def download_novel(
    book_title: str,
    chapters: list[dict],
    progress_callback: ProgressCallback = None,
) -> list[str]:
    """並行下載所有章節、合併成 TXT 檔，超過 45 MB 自動切片。"""
    total = len(chapters)
    if total == 0:
        raise RuntimeError("章節清單為空")

    # 每 Semaphore 座位一個 cloudscraper session，避免 cookie / referer 互相打架
    sessions = [make_session() for _ in range(CHAPTER_CONCURRENCY)]
    session_pool: asyncio.Queue = asyncio.Queue()
    for s in sessions:
        session_pool.put_nowait(s)

    results: list[Optional[tuple[str, str]]] = [None] * total
    done_count = 0
    lock = asyncio.Lock()

    async def fetch_one(idx: int, chapter: dict):
        nonlocal done_count
        session = await session_pool.get()
        try:
            try:
                title, content = await asyncio.to_thread(
                    get_chapter_content, chapter["url"], session
                )
            except Exception as exc:
                logger.error(
                    "Chapter %d/%d failed (%s): %s",
                    idx + 1, total, chapter.get("url", ""), exc,
                )
                title = chapter.get("title", f"第{idx + 1}章")
                content = "[本章下載失敗]"
            if not title:
                title = chapter.get("title", f"第{idx + 1}章")
            results[idx] = (title, content)
        finally:
            session_pool.put_nowait(session)

        async with lock:
            done_count += 1
            if progress_callback and (done_count % 20 == 0 or done_count == total):
                try:
                    await progress_callback(done_count, total)
                except Exception as cb_exc:
                    logger.warning("Progress callback error: %s", cb_exc)

    sem = asyncio.Semaphore(CHAPTER_CONCURRENCY)

    async def bound(idx: int, ch: dict):
        async with sem:
            await fetch_one(idx, ch)

    await asyncio.gather(
        *(asyncio.create_task(bound(i, ch)) for i, ch in enumerate(chapters)),
        return_exceptions=False,
    )

    # 依序組成 (title, content) 並寫入檔案
    ordered = [r if r is not None else (f"第{i+1}章", "[本章下載失敗]")
               for i, r in enumerate(results)]
    return write_novel_txt(book_title, ordered)


# ---------------------------------------------------------------------------
# Output writer (also used by fanqie crawler)
# ---------------------------------------------------------------------------

def write_novel_txt(book_title: str, chapters: list[tuple[str, str]]) -> list[str]:
    """
    將 (chapter_title, content) 清單寫成 TXT；超過 45 MB 自動切片。
    回傳本機檔案路徑清單。
    """
    parts_text: list[str] = [f"{book_title}\n{'=' * 60}\n\n"]
    for title, content in chapters:
        block = f"\n\n{'─' * 40}\n{title}\n{'─' * 40}\n\n{content}\n"
        parts_text.append(block)

    full = "".join(parts_text)
    encoded = full.encode("utf-8")
    safe_title = _sanitize(book_title)

    if len(encoded) <= MAX_FILE_SIZE:
        path = f"/tmp/{safe_title}.txt"
        _write_bytes(path, encoded)
        logger.info("Novel saved: %s (%.1f MB)", path, len(encoded) / 1024 / 1024)
        return [path]

    file_paths: list[str] = []
    offset = 0
    part_num = 1
    while offset < len(encoded):
        chunk = encoded[offset: offset + MAX_FILE_SIZE]
        if len(chunk) == MAX_FILE_SIZE and offset + MAX_FILE_SIZE < len(encoded):
            last_nl = chunk.rfind(b"\n")
            if last_nl > MAX_FILE_SIZE // 2:
                chunk = chunk[: last_nl + 1]
        path = f"/tmp/{safe_title}_Part{part_num}.txt"
        _write_bytes(path, chunk)
        file_paths.append(path)
        logger.info("Part %d saved: %s (%.1f MB)", part_num, path, len(chunk) / 1024 / 1024)
        offset += len(chunk)
        part_num += 1
    return file_paths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _sanitize(name: str) -> str:
    """移除或替換檔名中的非法字元"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:80]
