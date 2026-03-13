"""
scraper.py — 69書吧 (69shuba.cx) 爬蟲模組
包含反爬機制：隨機 UA、sleep 延遲、retry 指數退避
"""

import re
import time
import random
import logging

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.69shuba.cx"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0.6099.119 Mobile/15E148 Safari/604.1",
]

CATEGORY_MAP = {
    "玄幻": "xuanhuan",
    "仙侠": "xianxia",
    "仙俠": "xianxia",
    "都市": "dushi",
    "穿越": "chuanyue",
    "历史": "lishi",
    "歷史": "lishi",
    "悬疑": "xuanyi",
    "懸疑": "xuanyi",
}

# Patterns to skip when cleaning chapter content
_SKIP_RE = re.compile(
    r"69.*?shuba|www\.|http[s]?://|上一[章節頁]|下一[章節頁]|"
    r"返回書目|返回目錄|章節目錄|本章完|請記住.*?地址|"
    r"最新網址|手機版|加入書架|推薦閱讀|笔趣阁|筆趣閣|"
    r"第\s*\d+\s*頁\s*/\s*\d+|分享到",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    """建立帶有隨機 UA 的 requests.Session"""
    session = requests.Session()
    _rotate_ua(session)
    session.headers.update(
        {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    return session


def _rotate_ua(session: requests.Session) -> None:
    session.headers["User-Agent"] = random.choice(USER_AGENTS)


def _sleep() -> None:
    """每次請求後隨機 sleep 1~3 秒"""
    time.sleep(random.uniform(1.0, 3.0))


def _decode(resp: requests.Response) -> str:
    """自動偵測編碼（常見中文站使用 GB18030）"""
    enc = resp.encoding or ""
    if enc.lower() in ("gb2312", "gbk", "gb18030"):
        return resp.content.decode("gb18030", errors="replace")
    # Try apparent encoding
    apparent = resp.apparent_encoding or "utf-8"
    try:
        return resp.content.decode(apparent, errors="replace")
    except Exception:
        return resp.content.decode("utf-8", errors="replace")


def fetch(
    session: requests.Session,
    url: str,
    method: str = "GET",
    max_retries: int = 3,
    **kwargs,
) -> requests.Response:
    """帶 retry + 指數退避的 fetch，每次輪換 UA"""
    last_exc: Exception = RuntimeError(f"Failed: {url}")
    for attempt in range(max_retries):
        try:
            _rotate_ua(session)
            if method.upper() == "POST":
                resp = session.post(url, timeout=30, **kwargs)
            else:
                resp = session.get(url, timeout=30, **kwargs)
            resp.raise_for_status()
            _sleep()
            return resp
        except requests.HTTPError as exc:
            last_exc = exc
            if exc.response is not None and exc.response.status_code == 404:
                raise
            wait = 2 ** attempt
            logger.warning("HTTP %s on attempt %d for %s — waiting %ds", exc.response.status_code if exc.response else "?", attempt + 1, url, wait)
        except Exception as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning("Error on attempt %d for %s: %s — waiting %ds", attempt + 1, url, exc, wait)
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    raise last_exc


# ---------------------------------------------------------------------------
# Hot list
# ---------------------------------------------------------------------------

def get_hot_list(category: str | None = None) -> list[dict]:
    """返回 Top 20 熱門小說（含排名、書名、URL）"""
    session = make_session()
    if category and category in CATEGORY_MAP:
        cat_key = CATEGORY_MAP[category]
        url = f"{BASE_URL}/top/{cat_key}/"
    else:
        url = f"{BASE_URL}/top/allvisit/"

    logger.info("Fetching hot list: %s", url)
    resp = fetch(session, url)
    html = _decode(resp)
    soup = BeautifulSoup(html, "lxml")

    novels: list[dict] = []

    # Try progressively broader selectors
    for sel in (
        "ul.topbooks li",
        ".rank-books li",
        ".top-list li",
        "#topbooks li",
        "ol li",
        ".booklist li",
    ):
        items = soup.select(sel)
        if items:
            logger.info("Hot list selector '%s' → %d items", sel, len(items))
            for item in items:
                link = item.select_one("a")
                if not link:
                    continue
                title = link.get_text(strip=True)
                if len(title) < 2 or title in {"首頁", "排行", "書架", "登入", "注冊", "登录", "注册"}:
                    continue
                novels.append(
                    {
                        "rank": len(novels) + 1,
                        "title": title,
                        "url": link.get("href", ""),
                    }
                )
                if len(novels) >= 20:
                    break
            if novels:
                break

    if not novels:
        logger.warning("Fallback: scanning all <li> elements")
        for item in soup.find_all("li"):
            link = item.select_one("a")
            if not link:
                continue
            title = link.get_text(strip=True)
            if len(title) < 2 or title in {"首頁", "排行", "書架", "登入"}:
                continue
            # Heuristic: novel titles are usually 2-20 chars
            if 2 <= len(title) <= 20:
                novels.append(
                    {
                        "rank": len(novels) + 1,
                        "title": title,
                        "url": link.get("href", ""),
                    }
                )
                if len(novels) >= 20:
                    break

    logger.info("Hot list: found %d novels", len(novels))
    return novels


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_novels(keyword: str) -> list[dict]:
    """搜尋小說，返回最多 10 筆（書名、作者、最新章節、URL）"""
    session = make_session()
    search_url = f"{BASE_URL}/search.php"
    logger.info("Searching keyword: %s", keyword)

    # Try POST first, fallback to GET
    try:
        resp = session.post(
            search_url,
            data={"searchkey": keyword, "submit": ""},
            timeout=30,
            allow_redirects=True,
        )
        resp.raise_for_status()
        _sleep()
        html = _decode(resp)
    except Exception as exc:
        logger.warning("POST search failed (%s), trying GET", exc)
        try:
            resp = fetch(session, f"{BASE_URL}/search.php?searchkey={keyword}")
            html = _decode(resp)
        except Exception:
            resp = fetch(session, f"{BASE_URL}/search/{keyword}/")
            html = _decode(resp)

    soup = BeautifulSoup(html, "lxml")
    novels: list[dict] = []

    for sel in (
        ".search-list li",
        ".novelslist2 li",
        "#searchmain li",
        ".bookbox",
        ".result-item",
        "ul.books li",
        ".booklist li",
        "ul li",
    ):
        items = soup.select(sel)
        if items:
            logger.info("Search selector '%s' → %d items", sel, len(items))
            for item in items[:10]:
                title_el = item.select_one("h3 a, .bookname a, h4 a, a.name, dt a")
                if not title_el:
                    title_el = item.select_one("a")
                author_el = item.select_one(".author, .writer, span.author, dd.author")
                latest_el = item.select_one(".update a, .latest a, .newchapter a, dd.update a")
                if title_el:
                    title = title_el.get_text(strip=True)
                    if len(title) < 2:
                        continue
                    novels.append(
                        {
                            "title": title,
                            "url": title_el.get("href", ""),
                            "author": author_el.get_text(strip=True) if author_el else "未知",
                            "latest": latest_el.get_text(strip=True) if latest_el else "未知",
                        }
                    )
            if novels:
                break

    logger.info("Search: found %d results", len(novels))
    return novels


# ---------------------------------------------------------------------------
# Book info + chapter list
# ---------------------------------------------------------------------------

def get_book_info(book_url: str) -> dict:
    """返回 {'title': str, 'chapters': [{'title': str, 'url': str}]}"""
    session = make_session()
    if not book_url.startswith("http"):
        book_url = BASE_URL + book_url

    logger.info("Getting book info: %s", book_url)
    resp = fetch(session, book_url)
    html = _decode(resp)
    soup = BeautifulSoup(html, "lxml")

    # Book title
    title_el = soup.select_one("h1, .booktitle, .book-title, #bookinfo h1, .book-info h1")
    title = title_el.get_text(strip=True) if title_el else "未知"

    chapters = _extract_chapters(soup, book_url)

    # If no chapters on main page, try /catalog/ sub-page
    if not chapters:
        for catalog_suffix in ("/catalog/", "/list/", "/index.html"):
            catalog_url = book_url.rstrip("/") + catalog_suffix
            logger.info("Trying catalog page: %s", catalog_url)
            try:
                resp2 = fetch(session, catalog_url)
                soup2 = BeautifulSoup(_decode(resp2), "lxml")
                chapters = _extract_chapters(soup2, book_url)
                if chapters:
                    break
            except Exception as exc:
                logger.warning("Catalog page failed (%s): %s", catalog_url, exc)

    logger.info("Book '%s': %d chapters", title, len(chapters))
    return {"title": title, "chapters": chapters}


def _extract_chapters(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """從 BeautifulSoup 物件中提取章節清單"""
    chapters: list[dict] = []
    for sel in (
        "#chapters a",
        "#chapterlist a",
        ".chapterlist a",
        "#list a",
        ".listmain a",
        "#catalog a",
        ".catalog a",
        ".chapter-list a",
        "#booklist a",
        "#all-chapter a",
    ):
        els = soup.select(sel)
        if els:
            logger.info("Chapter selector '%s' → %d chapters", sel, len(els))
            for el in els:
                href = el.get("href", "")
                title = el.get_text(strip=True)
                if href and title and len(title) > 1:
                    full_url = href if href.startswith("http") else BASE_URL + href
                    chapters.append({"title": title, "url": full_url})
            break
    return chapters


# ---------------------------------------------------------------------------
# Chapter content
# ---------------------------------------------------------------------------

def get_chapter_content(chapter_url: str, session: requests.Session | None = None) -> tuple[str, str]:
    """
    下載並清洗單章內文。
    返回 (chapter_title, cleaned_text)
    """
    if session is None:
        session = make_session()

    resp = fetch(session, chapter_url)
    html = _decode(resp)
    soup = BeautifulSoup(html, "lxml")

    # Chapter title
    title_el = soup.select_one("h1, .chapter-title, #bookname, .readtitle")
    chapter_title = title_el.get_text(strip=True) if title_el else ""

    # Content container
    content_div = None
    for sel in (
        "#content",
        ".content",
        "#chaptercontent",
        ".chapter-content",
        "#nr",
        "#booktxt",
        ".booktxt",
        "#txtnav",
        ".txtnav",
        "div.read-content",
        "div#readcontent",
    ):
        content_div = soup.select_one(sel)
        if content_div:
            break

    if not content_div:
        logger.warning("No content div found for: %s", chapter_url)
        return chapter_title, ""

    # Strip unwanted tags
    for tag in content_div.select("script, style, .ad, .ads, .adsbygoogle, ins, iframe"):
        tag.decompose()
    for a in content_div.find_all("a"):
        a.decompose()

    raw_text = content_div.get_text("\n")

    # Clean lines
    cleaned: list[str] = []
    prev_line = None
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _SKIP_RE.search(line):
            continue
        # Deduplicate consecutive identical lines
        if line == prev_line:
            continue
        cleaned.append(line)
        prev_line = line

    return chapter_title, "\n".join(cleaned)
