#!/usr/bin/env python3
import os, json, time, random, re, requests, threading
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

BOT_TOKEN = "8054493496:AAFF_eF-uyYLXluv0pktnI63Elws6IUOwCw"
TONY_ID = "8685464868"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# 分類對應
CATEGORIES = {
    '玄幻': 'xuanhuan', '奇幻': 'xuanhuan',
    '武俠': 'xianxia', '仙俠': 'xianxia',
    '歷史': 'lishi', '軍事': 'lishi',
    '科幻': 'wangyou', '未來': 'wangyou',
    '靈異': 'lingyi',
    '都市': 'dushi',
}

# 排除的分類關鍵字（標題或標籤含這些就跳過）
EXCLUDE_KEYWORDS = [
    '耽美', 'BL', '言情', '愛情', '包養', '重生戀愛',
    '攻X受', '攻×受', '金主', '腐', '1v1限', 'H',
    '骨科', '父女', '父子', '雙性',
]

user_state = {}

def send(chat_id, text, parse_mode='HTML'):
    requests.post(f"{API}/sendMessage",
        data={'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode})

def send_file(chat_id, path, caption):
    with open(path, 'rb') as f:
        requests.post(f"{API}/sendDocument",
            data={'chat_id': chat_id, 'caption': caption},
            files={'document': f})

def is_excluded(title, tags_text=''):
    combined = title + tags_text
    return any(kw in combined for kw in EXCLUDE_KEYWORDS)

def get_browser():
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    browser = p.chromium.launch(headless=True,
        args=['--no-sandbox', '--disable-blink-features=AutomationControlled'])
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        locale='zh-TW')
    return p, browser, ctx

def get_html(url, wait=8):
    p, browser, ctx = get_browser()
    try:
        page = ctx.new_page()
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        time.sleep(wait)
        html = page.content()
        return html
    finally:
        browser.close()
        p.stop()

def parse_book_info(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    # 書名
    title_match = re.search(r'《(.+?)》', soup.title.text if soup.title else '')
    title = title_match.group(1) if title_match else '未知書名'
    # 作者
    author_el = soup.find('a', href=re.compile(r'/a/'))
    author = author_el.text.strip() if author_el else '未知作者'
    # 簡介（找含「簡介」文字的段落）
    intro = '（無簡介）'
    for el in soup.find_all(['p', 'div']):
        text = el.get_text().strip()
        if len(text) > 30 and len(text) < 500 and not el.find('a'):
            parent_text = str(el.parent)
            if 'intro' in parent_text or 'desc' in parent_text or '簡介' in parent_text:
                intro = text[:250]
                break
    # 標籤
    tags = [a.text.strip() for a in soup.find_all('a', href=re.compile(r'/hashtag/'))][:6]
    # 狀態
    status = '✅ 已完結' if '已完結' in html else '🔄 連載中'
    # 收藏數
    collect = '?'
    for el in soup.find_all(text=re.compile(r'^\d{3,}$')):
        collect = el.strip()
        break
    # 章節數
    book_id = url.rstrip('/').split('/')[-1]
    chapter_links = soup.find_all('a', href=re.compile(rf'/n/{book_id}/'))
    chapters = len(chapter_links)
    # 前2則評論
    comments = []
    for c in soup.find_all(['p', 'div'], class_=re.compile(r'comment|review')):
        t = c.get_text().strip()
        if 20 < len(t) < 120:
            comments.append(t)
        if len(comments) >= 2:
            break

    return {
        'title': title, 'author': author, 'intro': intro,
        'tags': tags, 'status': status, 'collect': collect,
        'chapters': chapters, 'comments': comments, 'url': url
    }

def format_card(info):
    tags_str = ' / '.join(info['tags']) if info['tags'] else '無標籤'
    card = (
        f"📖 <b>《{info['title']}》</b>\n"
        f"👤 作者：{info['author']}\n"
        f"🏷️ 標籤：{tags_str}\n"
        f"📊 {info['status']} ｜ 章節：{info['chapters']}"
    )
    return card

def get_hot_list(category=None):
    if category and category in CATEGORIES:
        url = f"https://czbooks.net/c/{CATEGORIES[category]}"
    else:
        url = "https://czbooks.net/"

    html = get_html(url, wait=7)
    soup = BeautifulSoup(html, 'html.parser')

    results = []
    seen = set()
    for a in soup.find_all('a', href=re.compile(r'//czbooks\.net/n/[^/]+$')):
        href = a.get('href', '')
        title = a.text.strip()
        if not title or len(title) < 2 or href in seen:
            continue
        if title in ['已完結', '連載中']:
            continue
        if is_excluded(title):
            continue
        seen.add(href)
        parent_text = a.parent.get_text() if a.parent else ''
        done = '已完結' in parent_text or '完結' in title
        full_url = 'https:' + href
        results.append({'title': title, 'url': full_url, 'done': done})
        if len(results) >= 10:
            break

    return results

def search_novels(keyword, complete_only=False):
    url = f"https://czbooks.net/s/{requests.utils.quote(keyword)}"
    html = get_html(url, wait=7)
    soup = BeautifulSoup(html, 'html.parser')

    results = []
    seen = set()
    for a in soup.find_all('a', href=re.compile(r'//czbooks\.net/n/[^/]+$')):
        href = a.get('href', '')
        title = a.text.strip()
        if not title or len(title) < 2 or href in seen:
            continue
        if title in ['已完結', '連載中']:
            continue
        if is_excluded(title):
            continue
        seen.add(href)
        parent_text = a.parent.get_text() if a.parent else ''
        done = '已完結' in parent_text or '完結' in title
        if complete_only and not done:
            continue
        full_url = 'https:' + href
        results.append({'title': title, 'url': full_url, 'done': done})
        if len(results) >= 10:
            break

    return results

def download_novel(chat_id, url, title):
    send(chat_id, f"⏳ 開始下載《{title}》...\n每500章回報進度，完成後傳 TXT")
    try:
        import novel_finder as nf
        p, browser, ctx = get_browser()
        page = ctx.new_page()
        page.goto("https://czbooks.net", timeout=60000, wait_until="domcontentloaded")
        time.sleep(6)
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        time.sleep(8)

        book_id = url.rstrip('/').split('/')[-1]
        links = page.query_selector_all(f'a[href*="/n/{book_id}/"]')
        chapters = []
        for link in links:
            href = link.get_attribute('href')
            text = link.inner_text().strip()
            if href and text and '付費' not in text:
                ch_url = 'https:' + href if href.startswith('//') else 'https://czbooks.net' + href
                chapters.append({'title': text, 'url': ch_url})

        total = len(chapters)
        os.makedirs(os.path.expanduser("~/novels"), exist_ok=True)
        safe = title.replace('/', '_').replace(' ', '_')
        out = os.path.expanduser(f"~/novels/{safe}.txt")

        with open(out, 'w', encoding='utf-8') as f:
            f.write(f"《{title}》\n{'='*40}\n\n")

        failed = 0
        for i, ch in enumerate(chapters):
            try:
                page.goto(ch['url'], timeout=45000, wait_until="domcontentloaded")
                time.sleep(2)
                html = page.content()
                if 'Just a moment' in html:
                    browser.close()
                    p2, browser, ctx = get_browser()
                    page = ctx.new_page()
                    page.goto("https://czbooks.net", timeout=60000, wait_until="domcontentloaded")
                    time.sleep(8)
                    continue
                content = nf.parse_content(html)
            except:
                content = None
                failed += 1

            with open(out, 'a', encoding='utf-8') as f:
                f.write(f"\n\n{'='*20}\n{ch['title']}\n{'='*20}\n\n" +
                    (content if content else '[抓取失敗]'))

            if (i+1) % 500 == 0:
                send(chat_id, f"⏳ 《{title}》進度：{i+1}/{total} 章")
            time.sleep(random.uniform(1.5, 2.5))

        browser.close()
        p.stop()

        size_kb = os.path.getsize(out) // 1024
        caption = f"✅ 《{title}》完成\n共 {total} 章｜{size_kb} KB"
        if failed:
            caption += f"\n⚠️ {failed} 章失敗"
        send_file(chat_id, out, caption)

    except Exception as e:
        send(chat_id, f"❌ 下載失敗：{e}")

def handle_message(msg):
    chat_id = str(msg['chat']['id'])
    text = msg.get('text', '').strip()
    if not text:
        return
    if chat_id != TONY_ID:
        send(chat_id, "⛔ 私人機器人")
        return

    state = user_state.get(chat_id, {})

    if text in ['/start', '/help']:
        send(chat_id,
            "📚 <b>小說下載機器人</b>\n\n"
            "• 書名關鍵字 → 搜尋\n"
            "• <code>hot</code> → 熱門 Top 10\n"
            "• <code>hot 玄幻</code> → 分類熱門\n"
            "• <code>完本 武俠</code> → 完結篩選\n"
            "• <code>/cancel</code> → 取消\n\n"
            "可用分類：玄幻、武俠、歷史、科幻、靈異、都市\n"
            "（已自動過濾言情、耽美類）")
        return

    if text == '/cancel':
        user_state.pop(chat_id, None)
        send(chat_id, "✅ 已取消")
        return

    # 選擇編號
    if state.get('action') == 'select' and text.isdigit():
        idx = int(text) - 1
        results = state.get('results', [])
        if 0 <= idx < len(results):
            book = results[idx]
            user_state[chat_id] = {'action': 'confirm', 'book': book}
            send(chat_id, f"⏳ 讀取《{book['title']}》詳情...")
            try:
                html = get_html(book['url'])
                info = parse_book_info(html, book['url'])
                card = format_card(info)
                send(chat_id, card + "\n\n輸入 <code>下載</code> 開始，或 <code>/cancel</code> 取消")
                user_state[chat_id] = {'action': 'confirm', 'book': book}
            except Exception as e:
                send(chat_id, f"❌ 讀取失敗：{e}")
        else:
            send(chat_id, "❌ 請輸入正確的編號")
        return

    # 確認下載
    if state.get('action') == 'confirm' and text in ['下載', '確認', 'yes', 'y']:
        book = state.pop(chat_id, {}).get('book') or state.get('book', {})
        user_state.pop(chat_id, None)
        t = threading.Thread(target=download_novel,
            args=(chat_id, book['url'], book['title']), daemon=True)
        t.start()
        return

    # hot 指令
    if text.lower().startswith('hot'):
        parts = text.split()
        cat = parts[1] if len(parts) > 1 else None
        send(chat_id, f"⏳ 取得{'「'+cat+'」' if cat else '綜合'}熱門榜...")
        def do_hot():
            try:
                results = get_hot_list(cat)
                if not results:
                    send(chat_id, "❌ 取得失敗，請稍後再試")
                    return
                msg_text = f"🔥 熱門榜 Top {len(results)}\n（已過濾言情/耽美）\n\n"
                for i, r in enumerate(results, 1):
                    tag = '✅' if r['done'] else '🔄'
                    msg_text += f"{i}. {tag} {r['title']}\n"
                msg_text += "\n輸入編號查看詳情"
                send(chat_id, msg_text)
                user_state[chat_id] = {'action': 'select', 'results': results}
            except Exception as e:
                send(chat_id, f"❌ 錯誤：{e}")
        threading.Thread(target=do_hot, daemon=True).start()
        return

    # 完本篩選
    if text.startswith('完本') or text.startswith('完結'):
        parts = text.split()
        kw = parts[1] if len(parts) > 1 else '完結'
        send(chat_id, f"⏳ 搜尋完本{'「'+kw+'」' if kw != '完結' else ''}小說...")
        def do_complete():
            try:
                results = search_novels(kw, complete_only=True)
                if not results:
                    send(chat_id, "❌ 找不到符合的完本小說")
                    return
                msg_text = f"📚 完本結果（{len(results)} 筆）\n\n"
                for i, r in enumerate(results, 1):
                    msg_text += f"{i}. ✅ {r['title']}\n"
                msg_text += "\n輸入編號查看詳情"
                send(chat_id, msg_text)
                user_state[chat_id] = {'action': 'select', 'results': results}
            except Exception as e:
                send(chat_id, f"❌ 錯誤：{e}")
        threading.Thread(target=do_complete, daemon=True).start()
        return

    # 關鍵字搜尋
    send(chat_id, f"🔍 搜尋「{text}」中...")
    def do_search():
        try:
            results = search_novels(text)
            if not results:
                send(chat_id, f"❌ 找不到「{text}」\n\n試試：\n• 不同關鍵字\n• 輸入 hot 看熱門榜")
                return
            msg_text = f"📚 搜尋結果（{len(results)} 筆）\n\n"
            for i, r in enumerate(results, 1):
                tag = '✅完結' if r['done'] else '🔄連載'
                msg_text += f"{i}. [{tag}] {r['title']}\n"
            msg_text += "\n輸入編號查看詳情"
            send(chat_id, msg_text)
            user_state[chat_id] = {'action': 'select', 'results': results}
        except Exception as e:
            send(chat_id, f"❌ 錯誤：{e}")
    threading.Thread(target=do_search, daemon=True).start()

def run():
    print("🤖 小說機器人啟動")
    offset = 0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                params={'offset': offset, 'timeout': 30}, timeout=35)
            for u in r.json().get('result', []):
                offset = u['update_id'] + 1
                if 'message' in u:
                    threading.Thread(target=handle_message,
                        args=(u['message'],), daemon=True).start()
        except Exception as e:
            print(f"錯誤：{e}")
            time.sleep(5)

if __name__ == '__main__':
    run()
