"""
downloader.py — 小說章節下載、內容合併、TXT 產製與分割
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

ProgressCallback = Optional[Callable[[int, int], Awaitable[None]]]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def download_novel(
    book_title: str,
    chapters: list[dict],
    progress_callback: ProgressCallback = None,
) -> list[str]:
    """
    下載所有章節，合併成 TXT 檔。
    若超過 45 MB 則自動切分多個 Part。
    返回本機暫存檔案路徑清單（呼叫方需自行刪除）。
    """
    session = make_session()
    total = len(chapters)
    parts_text: list[str] = []

    # Header
    header = f"{book_title}\n{'=' * 60}\n\n"
    parts_text.append(header)

    for idx, chapter in enumerate(chapters, 1):
        # Progress notification every 20 chapters
        if progress_callback and idx % 20 == 0:
            try:
                await progress_callback(idx, total)
            except Exception as cb_exc:
                logger.warning("Progress callback error: %s", cb_exc)

        try:
            chapter_title, content = await asyncio.to_thread(
                get_chapter_content, chapter["url"], session
            )
        except Exception as exc:
            logger.error("Chapter %d/%d failed (%s): %s", idx, total, chapter.get("url", ""), exc)
            chapter_title = chapter.get("title", f"第{idx}章")
            content = "[本章下載失敗]"

        if not chapter_title:
            chapter_title = chapter.get("title", f"第{idx}章")

        block = f"\n\n{'─' * 40}\n{chapter_title}\n{'─' * 40}\n\n{content}\n"
        parts_text.append(block)
        logger.debug("Chapter %d/%d done: %s", idx, total, chapter_title)

    full_text = "".join(parts_text)
    encoded = full_text.encode("utf-8")

    safe_title = _sanitize(book_title)

    if len(encoded) <= MAX_FILE_SIZE:
        path = f"/tmp/{safe_title}.txt"
        _write_bytes(path, encoded)
        logger.info("Novel saved: %s (%.1f MB)", path, len(encoded) / 1024 / 1024)
        return [path]

    # Split into parts
    file_paths: list[str] = []
    offset = 0
    part_num = 1

    while offset < len(encoded):
        chunk = encoded[offset : offset + MAX_FILE_SIZE]

        # Align to last newline to avoid splitting mid-line
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
