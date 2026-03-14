"""
bot.py — Novel Finder Telegram Bot
架構：python-telegram-bot v20.7，全 async，ConversationHandler，webhook 模式
常駐運行於 Render.com，透過 aiohttp 同時提供 /telegram（webhook）與 /health 端點。
"""

import asyncio
import logging
import os

from aiohttp import web
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from downloader import download_novel
from scraper import CATEGORY_MAP, get_book_info, get_hot_list, search_novels

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
WEBHOOK_URL: str = os.environ["WEBHOOK_URL"]       # e.g. https://your-app.onrender.com
PORT: int = int(os.environ.get("PORT", "8443"))

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
IDLE = 0
WAIT_BOOK_CHOICE = 1

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
SUPPORTED_CATEGORIES = "、".join(CATEGORY_MAP.keys())


def _help_text() -> str:
    return (
        "📚 *小說搜尋下載器*\n\n"
        "*使用方式：*\n"
        "• 傳送書名關鍵字 → 搜尋小說\n"
        "• `hot` 或 `/hot` → 綜合熱門 Top 20\n"
        "• `/hot 玄幻` → 指定分類熱門榜\n"
        "• `/cancel` → 取消當前操作\n\n"
        f"*支援分類：*{SUPPORTED_CATEGORIES}"
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(_help_text(), parse_mode="Markdown")
    return IDLE


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(_help_text(), parse_mode="Markdown")
    return IDLE


async def cmd_hot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """處理 /hot [分類] 指令"""
    category = " ".join(context.args).strip() if context.args else None
    return await _do_hot(update, category)


async def msg_hot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """處理純文字 'hot' / 'hot 玄幻' 訊息"""
    text = update.message.text.strip()
    parts = text.split(None, 1)
    category = parts[1].strip() if len(parts) > 1 else None
    return await _do_hot(update, category)


async def _do_hot(update: Update, category: str | None) -> int:
    cat_display = category if category else "綜合"
    await update.message.reply_text(f"⏳ 正在獲取「{cat_display}」熱門榜，請稍候...")

    try:
        novels = await asyncio.wait_for(
            asyncio.to_thread(get_hot_list, category),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("❌ 所有站台均無回應（已等待 45 秒），請稍後再試。")
        return IDLE
    except Exception as exc:
        logger.exception("Hot list error")
        await update.message.reply_text(f"❌ 獲取失敗：{str(exc)[:300]}")
        return IDLE

    if not novels:
        await update.message.reply_text("❌ 暫時無法獲取榜單，請稍後再試。")
        return IDLE

    lines = [f"🔥 *{cat_display}熱門排行 Top {len(novels)}*\n"]
    for n in novels:
        lines.append(f"`{n['rank']:2}.` {n['title']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return IDLE


async def msg_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """處理搜尋關鍵字"""
    keyword = update.message.text.strip()
    if not keyword:
        await update.message.reply_text("請輸入書名關鍵字。")
        return IDLE

    await update.message.reply_text(f"🔍 正在搜尋「{keyword}」，請稍候...")

    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(search_novels, keyword),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("❌ 所有站台均無回應（已等待 45 秒），請稍後再試。")
        return IDLE
    except Exception as exc:
        logger.exception("Search error")
        await update.message.reply_text(f"❌ 搜尋失敗：{str(exc)[:300]}")
        return IDLE

    if not results:
        await update.message.reply_text("❌ 找不到相關小說，請換個關鍵字再試。")
        return IDLE

    context.user_data["search_results"] = results

    lines = [f"📖 *搜尋結果（共 {len(results)} 本）*\n傳送編號選擇書籍：\n"]
    for i, n in enumerate(results, 1):
        line = f"`{i}.` 《{n['title']}》"
        if n.get("author") and n["author"] != "未知":
            line += f" — {n['author']}"
        if n.get("latest") and n["latest"] != "未知":
            line += f"\n    最新：{n['latest']}"
        lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return WAIT_BOOK_CHOICE


async def msg_choose_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """使用者輸入編號選書"""
    text = update.message.text.strip()

    # If user typed something that looks like a new search keyword (not a number)
    if not text.isdigit():
        await update.message.reply_text(
            "請輸入搜尋結果的*編號*，或 /cancel 取消，或直接輸入新的書名重新搜尋。",
            parse_mode="Markdown",
        )
        return WAIT_BOOK_CHOICE

    choice = int(text)
    results: list[dict] = context.user_data.get("search_results", [])

    if not results:
        await update.message.reply_text("搜尋結果已過期，請重新輸入書名。")
        return IDLE

    if choice < 1 or choice > len(results):
        await update.message.reply_text(f"請輸入 1 到 {len(results)} 之間的編號。")
        return WAIT_BOOK_CHOICE

    selected = results[choice - 1]
    book_url = selected["url"]
    book_title = selected["title"]

    await update.message.reply_text(f"✅ 已選擇《{book_title}》\n⏳ 正在獲取章節列表，請稍候...")

    # Get chapter list
    try:
        book_info = await asyncio.wait_for(
            asyncio.to_thread(get_book_info, book_url),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("❌ 獲取章節列表超時（45 秒），請稍後再試。")
        return IDLE
    except Exception as exc:
        logger.exception("get_book_info error")
        await update.message.reply_text(f"❌ 獲取章節列表失敗：{str(exc)[:300]}")
        return IDLE

    chapters = book_info.get("chapters", [])
    if not chapters:
        await update.message.reply_text(
            "❌ 無法獲取章節列表，可能是網站結構問題。\n請稍後再試或換一本書。"
        )
        return IDLE

    await update.message.reply_text(
        f"📚 共找到 *{len(chapters)}* 章\n⏳ 開始下載，每 20 章回報一次進度……",
        parse_mode="Markdown",
    )

    # Progress callback
    async def progress_cb(current: int, total: int) -> None:
        pct = current * 100 // total
        try:
            await update.message.reply_text(f"⬇️ 下載進度：{current}/{total}（{pct}%）")
        except TelegramError:
            pass

    # Download
    try:
        file_paths = await download_novel(book_title, chapters, progress_cb)
    except Exception as exc:
        logger.exception("download_novel error")
        await update.message.reply_text(f"❌ 下載失敗：{str(exc)[:200]}")
        return IDLE

    # Send files
    total_parts = len(file_paths)
    for i, path in enumerate(file_paths, 1):
        part_label = f" Part {i}/{total_parts}" if total_parts > 1 else ""
        caption = f"《{book_title}》{part_label}｜共 {len(chapters)} 章"
        try:
            await update.message.reply_text(f"📤 正在傳送《{book_title}》{part_label}…")
            with open(path, "rb") as fh:
                await update.message.reply_document(
                    document=fh,
                    filename=os.path.basename(path),
                    caption=caption,
                )
        except TelegramError as exc:
            logger.error("Send file error: %s", exc)
            await update.message.reply_text(f"❌ 傳送檔案失敗：{exc}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    await update.message.reply_text(f"✅ 《{book_title}》下載完成！可再傳送書名繼續搜尋。")
    return IDLE


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("✅ 已取消。可重新傳送書名搜尋。")
    return IDLE


# ---------------------------------------------------------------------------
# Build Application
# ---------------------------------------------------------------------------

def build_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    hot_filter = filters.Regex(r"(?i)^hot(\s|$)")

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("help", cmd_help),
            CommandHandler("hot", cmd_hot),
            MessageHandler(hot_filter, msg_hot),
            MessageHandler(filters.TEXT & ~filters.COMMAND, msg_search),
        ],
        states={
            IDLE: [
                CommandHandler("hot", cmd_hot),
                MessageHandler(hot_filter, msg_hot),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_search),
            ],
            WAIT_BOOK_CHOICE: [
                MessageHandler(filters.Regex(r"^\d+$"), msg_choose_book),
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~hot_filter, msg_choose_book),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start),
            CommandHandler("help", cmd_help),
        ],
        allow_reentry=True,
        name="novel_conv",
        persistent=False,
    )

    app.add_handler(conv)
    return app


# ---------------------------------------------------------------------------
# Webhook + Health server（單一 aiohttp app，處理 /telegram 與 /health）
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
    web_app.router.add_get("/", health_handler)   # Render 自身的 health check 打 /
    return web_app


# ---------------------------------------------------------------------------
# Main — webhook 模式，常駐運行
# ---------------------------------------------------------------------------

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
        await asyncio.Event().wait()   # 永久阻塞，直到行程被終止
    finally:
        logger.info("Shutting down...")
        await runner.cleanup()
        await bot_app.stop()
        await bot_app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
