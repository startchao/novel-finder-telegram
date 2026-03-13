"""
bot.py — Novel Finder Telegram Bot
架構：python-telegram-bot v20.7，全 async，ConversationHandler，long-polling
執行時間由 BOT_LIFETIME_SECONDS 環境變數控制（預設 1200 秒）
"""

import asyncio
import logging
import os

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
BOT_LIFETIME: int = int(os.environ.get("BOT_LIFETIME_SECONDS", "1200"))

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
        novels = await asyncio.to_thread(get_hot_list, category)
    except Exception as exc:
        logger.exception("Hot list error")
        await update.message.reply_text(f"❌ 獲取失敗：{str(exc)[:200]}")
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
        results = await asyncio.to_thread(search_novels, keyword)
    except Exception as exc:
        logger.exception("Search error")
        await update.message.reply_text(f"❌ 搜尋失敗：{str(exc)[:200]}")
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
        book_info = await asyncio.to_thread(get_book_info, book_url)
    except Exception as exc:
        logger.exception("get_book_info error")
        await update.message.reply_text(f"❌ 獲取章節列表失敗：{str(exc)[:200]}")
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
# Main — manual async run with lifetime control
# ---------------------------------------------------------------------------

async def run() -> None:
    app = build_app()

    logger.info("Initializing bot (lifetime=%ds)...", BOT_LIFETIME)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
    )

    logger.info("Bot is running. Will stop after %d seconds.", BOT_LIFETIME)
    await asyncio.sleep(BOT_LIFETIME)

    logger.info("Lifetime reached — shutting down gracefully.")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(run())
