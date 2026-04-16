"""
bot.py — Novel Finder Telegram Bot (v2)

v2 改動：
  - 移除 ConversationHandler 強制單工限制
  - 搜尋結果以 InlineKeyboardButton 每本一顆按鈕呈現
  - 選書後先顯示「確認卡片」（封面 + 簡介 + 作者 + 狀態）
  - 確認後下載走背景任務（asyncio.create_task），每使用者最多同時 3 本
  - 新增 /tasks（查看進行中 / 已完成）與 /cancel_task
  - 搜尋涵蓋所有 crawlers（zxcs、筆趣閣、番茄、SiteConfig 6 站）
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from crawlers import (
    BookInfo,
    SearchResult,
    detect_crawler,
    search_all,
)
from crawlers.base import BaseCrawler
from scraper import CATEGORY_MAP, get_hot_list

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_URL: str = os.environ["WEBHOOK_URL"]
PORT: int = int(os.environ.get("PORT", "8443"))

MAX_PER_USER_TASKS = 3
SEARCH_RESULT_TTL = 30 * 60  # 搜尋結果保留 30 分鐘
BOOK_INFO_TTL = 30 * 60

SUPPORTED_CATEGORIES = "、".join(CATEGORY_MAP.keys())


def _help_text() -> str:
    return (
        "📚 *小說搜尋下載器*\n\n"
        "*使用方式：*\n"
        "• 傳送書名關鍵字 → 多站同時搜尋\n"
        "• 點擊搜尋結果按鈕 → 查看介紹與封面\n"
        "• `/hot` 或 `hot` → 綜合熱門 Top 20\n"
        "• `/hot 玄幻` → 指定分類熱門榜\n"
        "• `/tasks` → 查看下載任務\n"
        "• `/cancel` → 清除本輪搜尋結果\n\n"
        f"*支援分類：*{SUPPORTED_CATEGORIES}\n"
        "*支援站台：*知軒藏書、筆趣閣系列、番茄小說、69書吧、飄天、UU看書、新笔趣阁、小說狂人、23小時"
    )


# ---------------------------------------------------------------------------
# 狀態（放 bot_data / user_data，避免跨進程持久化）
# ---------------------------------------------------------------------------

@dataclass
class TaskInfo:
    task_id: str
    user_id: int
    chat_id: int
    title: str
    source: str
    status: str = "queued"  # queued / running / done / failed / canceled
    progress: int = 0
    total: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    error: str = ""
    task: Optional[asyncio.Task] = None


def _user_sem(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Semaphore:
    sem = context.user_data.get("_task_sem")
    if sem is None:
        sem = asyncio.Semaphore(MAX_PER_USER_TASKS)
        context.user_data["_task_sem"] = sem
    return sem


def _get_tasks(context: ContextTypes.DEFAULT_TYPE) -> dict[str, TaskInfo]:
    if "tasks" not in context.user_data:
        context.user_data["tasks"] = {}
    return context.user_data["tasks"]


def _put_search_results(context: ContextTypes.DEFAULT_TYPE, results: list[SearchResult]) -> str:
    batch_id = secrets.token_urlsafe(6)
    context.user_data.setdefault("search_batches", {})[batch_id] = {
        "results": results,
        "ts": time.time(),
    }
    _gc_user_cache(context)
    return batch_id


def _get_search_result(context: ContextTypes.DEFAULT_TYPE, batch_id: str, idx: int) -> Optional[SearchResult]:
    batches = context.user_data.get("search_batches", {})
    batch = batches.get(batch_id)
    if not batch:
        return None
    if 0 <= idx < len(batch["results"]):
        return batch["results"][idx]
    return None


def _put_book_info(context: ContextTypes.DEFAULT_TYPE, info: BookInfo) -> str:
    book_id = secrets.token_urlsafe(8)
    context.user_data.setdefault("books", {})[book_id] = {
        "info": info,
        "ts": time.time(),
    }
    _gc_user_cache(context)
    return book_id


def _get_book_info(context: ContextTypes.DEFAULT_TYPE, book_id: str) -> Optional[BookInfo]:
    entry = context.user_data.get("books", {}).get(book_id)
    return entry["info"] if entry else None


def _gc_user_cache(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = time.time()
    for key, ttl in (("search_batches", SEARCH_RESULT_TTL), ("books", BOOK_INFO_TTL)):
        store = context.user_data.get(key, {})
        stale = [k for k, v in store.items() if now - v["ts"] > ttl]
        for k in stale:
            store.pop(k, None)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_help_text(), parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_help_text(), parse_mode=ParseMode.MARKDOWN)


async def cmd_hot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = " ".join(context.args).strip() if context.args else None
    await _do_hot(update, category)


async def msg_hot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parts = text.split(None, 1)
    category = parts[1].strip() if len(parts) > 1 else None
    await _do_hot(update, category)


async def _do_hot(update: Update, category: Optional[str]):
    cat_display = category if category else "綜合"
    await update.message.reply_text(f"⏳ 正在獲取「{cat_display}」熱門榜，請稍候...")
    try:
        novels = await asyncio.wait_for(
            asyncio.to_thread(get_hot_list, category),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("❌ 所有站台均無回應（已等待 45 秒），請稍後再試。")
        return
    except Exception as exc:
        logger.exception("Hot list error")
        await update.message.reply_text(f"❌ 獲取失敗：{str(exc)[:300]}")
        return
    if not novels:
        await update.message.reply_text("❌ 暫時無法獲取榜單，請稍後再試。")
        return

    lines = [f"🔥 *{cat_display}熱門排行 Top {len(novels)}*\n"]
    for n in novels:
        lines.append(f"`{n['rank']:2}.` {n['title']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("search_batches", None)
    context.user_data.pop("books", None)
    await update.message.reply_text("✅ 已清除本輪搜尋快取。下載中的任務不受影響，用 /tasks 查看。")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = _get_tasks(context)
    if not tasks:
        await update.message.reply_text("📭 尚無任何下載任務。")
        return

    lines = ["🗂 *下載任務*"]
    for tid, info in sorted(tasks.items(), key=lambda kv: kv[1].started_at, reverse=True)[:20]:
        icon = {
            "queued": "⏳", "running": "⬇️", "done": "✅",
            "failed": "❌", "canceled": "🚫",
        }.get(info.status, "•")
        prog = f"{info.progress}/{info.total}" if info.total else "-"
        line = f"{icon} `{tid}` 《{info.title}》 [{info.source}] `{info.status}` {prog}"
        if info.error:
            line += f"\n    錯誤：{info.error[:100]}"
        lines.append(line)
    lines.append("\n取消任務：`/cancel_task 任務ID`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_cancel_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/cancel_task 任務ID")
        return
    tid = context.args[0].strip()
    tasks = _get_tasks(context)
    info = tasks.get(tid)
    if not info:
        await update.message.reply_text(f"找不到任務 `{tid}`", parse_mode=ParseMode.MARKDOWN)
        return
    if info.task and not info.task.done():
        info.task.cancel()
        info.status = "canceled"
        info.finished_at = time.time()
        await update.message.reply_text(f"🚫 已取消任務 `{tid}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"任務 `{tid}` 已結束，無需取消", parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# 搜尋
# ---------------------------------------------------------------------------

async def msg_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    if not keyword:
        await update.message.reply_text("請輸入書名關鍵字。")
        return
    await update.message.reply_text(f"🔍 正在多站搜尋「{keyword}」，請稍候...")

    try:
        results = await asyncio.wait_for(search_all(keyword), timeout=60.0)
    except asyncio.TimeoutError:
        await update.message.reply_text("❌ 所有站台均無回應（已等待 60 秒）")
        return
    except Exception as exc:
        logger.exception("search_all error")
        await update.message.reply_text(f"❌ 搜尋失敗：{str(exc)[:300]}")
        return

    if not results:
        await update.message.reply_text("❌ 找不到相關小說，請換個關鍵字再試。")
        return

    batch_id = _put_search_results(context, results)

    keyboard = []
    for idx, r in enumerate(results[:20]):
        label = f"📖 《{r.title}》"
        if r.author and r.author != "未知":
            label += f" — {r.author}"
        label += f"  [{r.source}]"
        # Telegram 按鈕文字上限 64 bytes
        if len(label.encode("utf-8")) > 60:
            label = label[:28] + "…"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"b:{batch_id}:{idx}")])

    await update.message.reply_text(
        f"📖 *搜尋結果（共 {len(results)} 本）* — 點按查看介紹",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Callback：點選搜尋結果 / 確認下載 / 取消
# ---------------------------------------------------------------------------

async def cb_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, batch_id, idx_s = query.data.split(":", 2)
        idx = int(idx_s)
    except Exception:
        await query.edit_message_text("❌ 無效的選項")
        return

    result = _get_search_result(context, batch_id, idx)
    if not result:
        await query.edit_message_text("❌ 搜尋結果已過期，請重新搜尋。")
        return

    await query.message.reply_text(
        f"⏳ 正在取得《{result.title}》詳細資訊…[{result.source}]"
    )

    try:
        book_info = await asyncio.wait_for(
            asyncio.to_thread(_sync_book_info, result),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        await query.message.reply_text("❌ 獲取書籍資訊超時（45 秒），請稍後再試。")
        return
    except Exception as exc:
        logger.exception("get_book_info error")
        await query.message.reply_text(f"❌ 獲取失敗：{str(exc)[:300]}")
        return

    book_id = _put_book_info(context, book_info)
    await _send_book_card(query.message, book_info, book_id)


def _sync_book_info(result: SearchResult) -> BookInfo:
    crawler = detect_crawler(result.url)
    if crawler is None:
        raise RuntimeError(f"找不到對應 crawler: {result.url}")
    info = crawler.get_book_info(result.url)
    if not info.source:
        info.source = crawler.name
    return info


async def _send_book_card(message, info: BookInfo, book_id: str):
    # 卡片文字
    n_chapters = len(info.chapters)
    extra = ""
    if info.archive_urls:
        extra = f"📦 打包下載：{len(info.archive_urls)} 份"
    elif n_chapters:
        extra = f"📚 章節：{n_chapters} 章"

    desc = info.description or "（無簡介）"
    if len(desc) > 500:
        desc = desc[:500] + "…"

    caption = (
        f"📖 *{_md_escape(info.title)}*\n"
        f"✍️ {_md_escape(info.author)} ｜ {_md_escape(info.status) or '—'} ｜ {info.source}\n"
        f"{extra}\n\n"
        f"{_md_escape(desc)}"
    )

    can_download = bool(info.chapters or info.archive_urls)
    buttons = []
    if can_download:
        buttons.append([InlineKeyboardButton("✅ 開始下載", callback_data=f"d:{book_id}")])
    buttons.append([InlineKeyboardButton("❌ 取消", callback_data="x")])
    markup = InlineKeyboardMarkup(buttons)

    sent = False
    if info.cover_url:
        try:
            await message.reply_photo(
                photo=info.cover_url,
                caption=caption[:1024],  # Telegram photo caption 限制 1024
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=markup,
            )
            sent = True
        except TelegramError as exc:
            logger.warning("reply_photo failed (%s), fallback to text", exc)

    if not sent:
        await message.reply_text(
            caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=markup,
            disable_web_page_preview=True,
        )


def _md_escape(text: str) -> str:
    """轉義 MarkdownV2 特殊字元"""
    if not text:
        return ""
    specials = r"_*[]()~`>#+-=|{}.!\\"
    return "".join("\\" + c if c in specials else c for c in text)


async def cb_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, book_id = query.data.split(":", 1)
    info = _get_book_info(context, book_id)
    if info is None:
        await query.message.reply_text("❌ 書籍資訊已過期，請重新選擇。")
        return

    tasks = _get_tasks(context)
    running = [t for t in tasks.values() if t.status in ("queued", "running")]
    if len(running) >= MAX_PER_USER_TASKS:
        await query.message.reply_text(
            f"⚠️ 同時下載上限為 {MAX_PER_USER_TASKS} 本，請等當前任務完成或 /cancel_task。"
        )
        return

    tid = secrets.token_urlsafe(4)
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    task_info = TaskInfo(
        task_id=tid,
        user_id=user_id,
        chat_id=chat_id,
        title=info.title,
        source=info.source,
        total=len(info.chapters) or (1 if info.archive_urls else 0),
    )
    tasks[tid] = task_info

    await query.message.reply_text(
        f"📥 已加入下載佇列：《{info.title}》\n任務 ID：`{tid}`\n可用 /tasks 查看進度",
        parse_mode=ParseMode.MARKDOWN,
    )

    bot = context.bot
    sem = _user_sem(context)

    async def runner():
        async with sem:
            task_info.status = "running"
            try:
                crawler = detect_crawler(info.url)
                if crawler is None:
                    raise RuntimeError("找不到對應 crawler")

                async def progress_cb(current: int, total: int):
                    task_info.progress = current
                    task_info.total = total
                    if current == total or current % 40 == 0:
                        try:
                            await bot.send_message(
                                chat_id,
                                f"⬇️ `{tid}` 進度：{current}/{total}（{current * 100 // max(total, 1)}%）",
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        except TelegramError:
                            pass

                file_paths = await crawler.download(info, progress_cb)
                if not file_paths:
                    raise RuntimeError("下載完成但未產出檔案")

                total_parts = len(file_paths)
                for i, path in enumerate(file_paths, 1):
                    part_label = f" Part {i}/{total_parts}" if total_parts > 1 else ""
                    caption = f"《{info.title}》{part_label} ｜ {info.source}"
                    try:
                        with open(path, "rb") as fh:
                            await bot.send_document(
                                chat_id=chat_id,
                                document=fh,
                                filename=os.path.basename(path),
                                caption=caption,
                            )
                    finally:
                        try:
                            os.remove(path)
                        except OSError:
                            pass

                task_info.status = "done"
                task_info.finished_at = time.time()
                await bot.send_message(
                    chat_id,
                    f"✅ 《{info.title}》下載完成（任務 `{tid}`）",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except asyncio.CancelledError:
                task_info.status = "canceled"
                task_info.finished_at = time.time()
                raise
            except Exception as exc:
                logger.exception("Task %s failed", tid)
                task_info.status = "failed"
                task_info.error = str(exc)
                task_info.finished_at = time.time()
                try:
                    await bot.send_message(
                        chat_id,
                        f"❌ 《{info.title}》下載失敗：{str(exc)[:300]}",
                    )
                except TelegramError:
                    pass

    task_info.task = asyncio.create_task(runner())


async def cb_cancel_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("已取消")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass


# ---------------------------------------------------------------------------
# Build Application
# ---------------------------------------------------------------------------

def build_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    hot_filter = filters.Regex(r"(?i)^hot(\s|$)")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("hot", cmd_hot))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("cancel_task", cmd_cancel_task))
    app.add_handler(MessageHandler(hot_filter, msg_hot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_search))

    app.add_handler(CallbackQueryHandler(cb_book, pattern=r"^b:"))
    app.add_handler(CallbackQueryHandler(cb_download, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_card, pattern=r"^x$"))
    return app


# ---------------------------------------------------------------------------
# Webhook + Health server
# ---------------------------------------------------------------------------

async def _build_web_app(bot_app) -> web.Application:
    web_app = web.Application()

    async def telegram_handler(request: web.Request) -> web.Response:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return web.Response(text="OK")

    async def health_handler(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    web_app.router.add_post("/telegram", telegram_handler)
    web_app.router.add_get("/health", health_handler)
    web_app.router.add_get("/", health_handler)
    return web_app


async def run() -> None:
    bot_app = build_app()

    logger.info("Initializing bot (webhook mode)...")
    await bot_app.initialize()
    await bot_app.start()

    webhook_path = f"{WEBHOOK_URL}/telegram"
    await bot_app.bot.set_webhook(
        url=webhook_path,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    logger.info("Webhook registered: %s", webhook_path)

    web_app = await _build_web_app(bot_app)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Web server listening on 0.0.0.0:%d", PORT)

    try:
        await asyncio.Event().wait()
    finally:
        logger.info("Shutting down...")
        await runner.cleanup()
        await bot_app.stop()
        await bot_app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
