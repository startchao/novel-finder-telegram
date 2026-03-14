"""
scraper.py — 多站點輪流爬取模組
使用 cloudscraper 繞過 Cloudflare / WAF，支援 6 個站台自動備援。

站台優先順序：
  1. 69書吧      (www.69shuba.cx / .com)
  2. 飄天文學    (www.ptwxz.com)
  3. UU看書      (www.uukanshu.com)
  4. 新笔趣阁    (www.xbiquge.la / .so)
  5. 小說狂人    (czbooks.net)
  6. 23小時      (www.23us.so)
"""

import re
import time
import random
import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, urljoin

import os
import urllib.parse

import cloudscraper
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# CF Workers proxy base URL. When set, all novel-site requests are routed
# through the proxy instead of directly. Falls back to direct if not set.
PROXY_BASE: str = os.environ.get("PROXY_BASE", "").rstrip("/")

# ---------------------------------------------------------------------------
# 共用分類對照表（用於 bot.py 顯示支援清單）
# ---------------------------------------------------------------------------
CATEGORY_MAP: dict[str, str] = {
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

# ---------------------------------------------------------------------------
# 站台設定
# ---------------------------------------------------------------------------

@dataclass
class SiteConfig:
    name: str
    base_url: str
    hot_paths: list[str]           # 熱門榜 URL 路徑（依序嘗試）
    hot_cat_tpl: str               # 分類榜模板，{cat} 替換為英文分類
    category_map: dict[str, str]   # 此站台的分類對照
    hot_item_sels: list[str]       # 熱門榜 li 選擇器（依序嘗試）
    search_path: str               # 搜尋路徑
    search_method: str             # GET 或 POST
    search_param: str              # 搜尋關鍵字的 param 名
    search_item_sels: list[str]    # 搜尋結果 li 選擇器
    search_title_sels: list[str]   # 書名連結選擇器
    search_author_sels: list[str]  # 作者選擇器
    search_latest_sels: list[str]  # 最新章節選擇器
    chapter_list_sels: list[str]   # 章節列表 a 選擇器
    content_sels: list[str]        # 章節內文容器選擇器
    encoding: str = "utf-8"


SITE_69SHUBA = SiteConfig(
    name="69書吧",
    base_url="https://www.69shuba.cx",
    hot_paths=["/top/allvisit/", "/top/allvisit.htm", "/top/", "/"],
    hot_cat_tpl="/top/{cat}/",
    category_map=CATEGORY_MAP,
    hot_item_sels=["ul.topbooks li", ".rank-books li", "#topbooks li", "ol li"],
    search_path="/search.php",
    search_method="POST",
    search_param="searchkey",
    search_item_sels=[".novelslist2 li", ".search-list li", "#searchmain li", "ul.list li"],
    search_title_sels=["h3 a", ".bookname a", "h4 a", "dt a"],
    search_author_sels=[".author", ".writer", "span.author"],
    search_latest_sels=[".update a", ".latest a", ".newchapter a"],
    chapter_list_sels=["#chapters a", "#chapterlist a", ".chapterlist a", "#list a", ".listmain a"],
    content_sels=["#content", ".content", "#chaptercontent", "#booktxt", "#txtnav"],
    encoding="gb18030",
)

SITE_PTWXZ = SiteConfig(
    name="飄天文學",
    base_url="https://www.ptwxz.com",
    hot_paths=["/top/allvisit/", "/top/", "/"],
    hot_cat_tpl="/top/{cat}/",
    category_map=CATEGORY_MAP,
    hot_item_sels=["ul.topbooks li", ".rank-books li", "ol li", "ul.list li"],
    search_path="/search.php",
    search_method="GET",
    search_param="searchkey",
    search_item_sels=[".novelslist2 li", ".search-list li", "ul.list li"],
    search_title_sels=["h3 a", ".bookname a", "dt a", "a"],
    search_author_sels=[".author", "span.author"],
    search_latest_sels=[".update a", ".newchapter a"],
    chapter_list_sels=["#list a", "#chapters a", ".chapterlist a", ".listmain a"],
    content_sels=["#content", ".content", "#chaptercontent"],
    encoding="gb18030",
)

SITE_UUKANSHU = SiteConfig(
    name="UU看書",
    base_url="https://www.uukanshu.com",
    hot_paths=["/top/", "/top/allvisit/", "/"],
    hot_cat_tpl="/top/{cat}/",
    category_map=CATEGORY_MAP,
    hot_item_sels=["ul.ranking li", ".ranking li", "ul.topbooks li", ".rank-list li", "ol li"],
    search_path="/search/",
    search_method="GET",
    search_param="q",
    search_item_sels=[".resultbox li", ".result-list li", ".search-list li", "ul.list li"],
    search_title_sels=[".bookname a", "h3 a", "dt a", "a"],
    search_author_sels=[".author", "span.author", ".writer"],
    search_latest_sels=[".update a", ".lastchapter a", ".newchapter a"],
    chapter_list_sels=["ul.chapterlist li a", "dl dd a", "#chapters a", "#list a", ".chapterlist a"],
    content_sels=["#contentbox", ".contentbox", "#content", ".content"],
    encoding="utf-8",
)

SITE_XBIQUGE = SiteConfig(
    name="新笔趣阁",
    base_url="https://www.xbiquge.la",   # .la 為目前較穩定的鏡像；.so 已備註為備援
    hot_paths=["/top/allvisit/", "/top/", "/"],
    hot_cat_tpl="/top/{cat}/",
    category_map=CATEGORY_MAP,
    hot_item_sels=["ul.topbooks li", ".rank-books li", "ol li", "ul.list li"],
    search_path="/search.php",
    search_method="GET",
    search_param="searchkey",
    search_item_sels=[".novelslist2 li", ".search-list li", "ul.list li"],
    search_title_sels=["h3 a", ".bookname a", "dt a", "a"],
    search_author_sels=[".author", "span.author"],
    search_latest_sels=[".update a", ".newchapter a"],
    chapter_list_sels=["#list a", "#chapters a", ".chapterlist a"],
    content_sels=["#content", ".content", "#chaptercontent"],
    encoding="utf-8",
)

SITE_CZBOOKS = SiteConfig(
    name="小說狂人",
    base_url="https://czbooks.net",
    hot_paths=["/rank/", "/ranking/", "/top/", "/"],
    hot_cat_tpl="/rank/{cat}/",
    category_map={"玄幻": "xuanhuan", "仙俠": "xianxia", "都市": "dushi", "穿越": "chuanyue"},
    hot_item_sels=[".rank-list li", ".novel-list li", "ul.list li", "ol li"],
    search_path="/search",
    search_method="GET",
    search_param="q",
    search_item_sels=[".novel-list li", ".search-result li", "ul.list li"],
    search_title_sels=[".title a", "h3 a", "h2 a", ".bookname a", "a"],
    search_author_sels=[".author", ".writer", "span.author"],
    search_latest_sels=[".last-chapter a", ".update a", ".newchapter a"],
    chapter_list_sels=["#chapters-list a", "#chapter-list a", ".chapter-list a", "#list a", "ul.list a"],
    content_sels=["#novel-content", ".chapter-content", "#content", ".content"],
    encoding="utf-8",
)

SITE_23US = SiteConfig(
    name="23小時",
    base_url="https://www.23us.so",
    hot_paths=["/top/allvisit/", "/top/", "/"],
    hot_cat_tpl="/top/{cat}/",
    category_map=CATEGORY_MAP,
    hot_item_sels=["ul.topbooks li", ".rank-books li", "ol li"],
    search_path="/search.php",
    search_method="GET",
    search_param="searchkey",
    search_item_sels=[".novelslist2 li", ".search-list li", "ul.list li"],
    search_title_sels=["h3 a", ".bookname a", "dt a", "a"],
    search_author_sels=[".author", "span.author"],
    search_latest_sels=[".update a", ".newchapter a"],
    chapter_list_sels=["#list a", "#chapters a", ".chapterlist a"],
    content_sels=["#content", ".content", "#chaptercontent"],
    encoding="gb18030",
)

# 站台優先順序
SITES: list[SiteConfig] = [
    SITE_69SHUBA,   # GB18030，biquge 風格，最熱門
    SITE_PTWXZ,     # GB18030，飄天文學
    SITE_UUKANSHU,  # UTF-8，UU看書，大陸最大之一
    SITE_XBIQUGE,   # UTF-8，新笔趣阁
    SITE_CZBOOKS,   # UTF-8，台灣小說狂人
    SITE_23US,      # GB18030，23小時
]

# ---------------------------------------------------------------------------
# 內文清洗 pattern
# ---------------------------------------------------------------------------
_SKIP_RE = re.compile(
    r"69.*?shuba|ptwxz|xbiquge|twkan|www\.|http[s]?://|"
    r"上一[章節頁]|下一[章節頁]|返回書目|返回目錄|章節目錄|本章完|"
    r"請記住.*?地址|最新網址|手機版|加入書架|推薦閱讀|笔趣阁|筆趣閣|"
    r"第\s*\d+\s*頁\s*/\s*\d+|分享到",
    re.IGNORECASE,
)

# NAV items to skip when scanning <li> for titles
_NAV_TITLES = {
    "首頁", "首页", "排行", "書架", "书架", "登入", "登录",
    "注冊", "注册", "搜索", "搜尋", "分類", "分类",
}

# ---------------------------------------------------------------------------
# Session / HTTP helpers
# ---------------------------------------------------------------------------

def make_session() -> cloudscraper.CloudScraper:
    """
    建立 CloudScraper session（cloudscraper 是 requests.Session 子類別）。
    使用 nodejs 解析器（GitHub Actions 內建 Node.js）。
    """
    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True},
            interpreter="nodejs",
        )
    except Exception:
        # nodejs 不可用時 fallback 到預設解析器
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True},
        )
    scraper.headers.update(
        {
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    return scraper


def _sleep() -> None:
    time.sleep(random.uniform(1.0, 3.0))


def _decode(resp) -> str:
    """自動偵測 GB18030 / UTF-8 編碼"""
    enc = (resp.encoding or "").lower()
    if enc in ("gb2312", "gbk", "gb18030"):
        return resp.content.decode("gb18030", errors="replace")
    apparent = resp.apparent_encoding or "utf-8"
    try:
        return resp.content.decode(apparent, errors="replace")
    except Exception:
        return resp.content.decode("utf-8", errors="replace")


def _fetch_via_proxy(scraper, url: str, method: str = "GET", **kwargs):
    """透過 CF Workers 代理發出請求"""
    params: dict = {"url": url, "method": method.upper()}

    referer = scraper.headers.get("Referer", "")
    if referer:
        params["referer"] = referer

    parsed_url = urllib.parse.urlparse(url)
    cookie_dict = (
        scraper.cookies.get_dict(domain=parsed_url.netloc)
        or scraper.cookies.get_dict()
    )
    if cookie_dict:
        params["cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())

    if method.upper() == "POST" and "data" in kwargs:
        data = kwargs["data"]
        params["body"] = (
            urllib.parse.urlencode(data) if isinstance(data, dict) else data
        )

    proxy_url = PROXY_BASE + "/?" + urllib.parse.urlencode(params)
    logger.debug("Proxy fetch: %s", proxy_url)
    return scraper.get(proxy_url, timeout=20)


def _fetch(scraper, url: str, method: str = "GET", max_retries: int = 2, **kwargs):
    """帶 retry + 指數退避的請求（timeout=8s，最多重試 2 次）"""
    last_exc: Exception = RuntimeError(f"Failed: {url}")
    for attempt in range(max_retries):
        try:
            if PROXY_BASE:
                resp = _fetch_via_proxy(scraper, url, method, **kwargs)
            elif method.upper() == "POST":
                resp = scraper.post(url, timeout=8, **kwargs)
            else:
                resp = scraper.get(url, timeout=8, **kwargs)
            resp.raise_for_status()
            _sleep()
            return resp
        except Exception as exc:
            last_exc = exc
            if hasattr(exc, "response") and exc.response is not None:
                code = exc.response.status_code
                if code in (403, 404):
                    raise   # 明確拒絕/不存在，不需要 retry
            wait = 2 ** attempt
            logger.warning("Attempt %d/%d failed for %s: %s (wait %ds)",
                           attempt + 1, max_retries, url, exc, wait)
            if attempt < max_retries - 1:
                time.sleep(wait)
    raise last_exc


def _warm_up(scraper, site: SiteConfig) -> str:
    """
    先訪問首頁取得 Cookie，返回可用的 base URL。
    僅當重定向後的域名「核心詞」與原始相同時才更新 base URL，
    避免跨站重定向導致後續路徑拼錯（如 ptwxz.com → piaotian.xxx）。
    """
    candidates = [site.base_url]
    if ".cx" in site.base_url:
        candidates.append(site.base_url.replace(".cx", ".com"))
    elif "69shuba.com" in site.base_url:
        candidates.append(site.base_url.replace(".com", ".cx"))
    if "xbiquge.la" in site.base_url:
        candidates.append(site.base_url.replace(".la", ".so"))
        candidates.append(site.base_url.replace(".la", ".bid"))

    orig_core = urlparse(site.base_url).netloc.replace("www.", "").split(".")[0]

    for url in candidates:
        try:
            if PROXY_BASE:
                resp = _fetch_via_proxy(scraper, url + "/", "GET")
            else:
                resp = scraper.get(url + "/", timeout=8, allow_redirects=True)
            if resp.status_code == 200:
                parsed = urlparse(resp.url)
                redirected_netloc = parsed.netloc
                redirected_base = f"{parsed.scheme}://{redirected_netloc}"
                redir_core = redirected_netloc.replace("www.", "").split(".")[0]

                # 只在同一站台域名變體時採用重定向 URL（例如 .cx→.com）
                # 跨站重定向（核心詞不同）則繼續用原始 base URL
                if orig_core == redir_core:
                    base = redirected_base
                else:
                    base = url.rstrip("/")
                    logger.warning(
                        "[%s] warm-up redirected to different domain (%s → %s), "
                        "keeping original for URL construction",
                        site.name, url, redirected_base,
                    )

                scraper.headers["Referer"] = redirected_base + "/"
                logger.info("[%s] warm-up OK → using base: %s", site.name, base)
                _sleep()
                return base
        except Exception as exc:
            logger.warning("[%s] warm-up failed (%s): %s", site.name, url, exc)
    return site.base_url


# ---------------------------------------------------------------------------
# Hot list
# ---------------------------------------------------------------------------

def _parse_hot(soup: BeautifulSoup, site: SiteConfig, base: str) -> list[dict]:
    """從已解析的 soup 中提取熱門小說清單"""
    novels: list[dict] = []

    for sel in site.hot_item_sels:
        items = soup.select(sel)
        if not items:
            continue
        logger.info("[%s] hot selector '%s' → %d items", site.name, sel, len(items))
        for item in items:
            link = item.select_one("a")
            if not link:
                continue
            title = link.get_text(strip=True)
            if len(title) < 2 or title in _NAV_TITLES:
                continue
            href = link.get("href", "")
            full_url = href if href.startswith("http") else urljoin(base, href)
            novels.append({"rank": len(novels) + 1, "title": title, "url": full_url})
            if len(novels) >= 20:
                break
        if novels:
            break

    return novels


def _hot_from_site(site: SiteConfig, category: Optional[str]) -> list[dict]:
    scraper = make_session()
    base = _warm_up(scraper, site)
    scraper.headers["Referer"] = base + "/"

    if category and category in site.category_map:
        cat_key = site.category_map[category]
        paths = [site.hot_cat_tpl.format(cat=cat_key)] + site.hot_paths
    else:
        paths = site.hot_paths

    for path in paths:
        url = base + path
        try:
            logger.info("[%s] trying hot URL: %s", site.name, url)
            resp = _fetch(scraper, url)
            html = _decode(resp)
            soup = BeautifulSoup(html, "lxml")
            novels = _parse_hot(soup, site, base)
            if novels:
                logger.info("[%s] hot list: %d novels from %s", site.name, len(novels), url)
                return novels
        except Exception as exc:
            logger.warning("[%s] hot URL %s failed: %s", site.name, url, exc)

    return []


def get_hot_list(category: Optional[str] = None) -> list[dict]:
    """輪流嘗試各站台，返回 Top 20 熱門小說"""
    errors: list[str] = []
    for site in SITES:
        try:
            novels = _hot_from_site(site, category)
            if novels:
                return novels
        except Exception as exc:
            errors.append(f"{site.name}: {exc}")
            logger.warning("Site %s hot list failed: %s", site.name, exc)
    raise RuntimeError("所有站台均無法取得熱門榜，請稍後再試。\n" + "\n".join(errors))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _parse_search(soup: BeautifulSoup, site: SiteConfig, base: str) -> list[dict]:
    novels: list[dict] = []
    for sel in site.search_item_sels:
        items = soup.select(sel)
        if not items:
            continue
        logger.info("[%s] search selector '%s' → %d items", site.name, sel, len(items))
        for item in items[:10]:
            title_el = None
            for ts in site.search_title_sels:
                title_el = item.select_one(ts)
                if title_el:
                    break
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if len(title) < 2:
                continue
            href = title_el.get("href", "")
            full_url = href if href.startswith("http") else urljoin(base, href)

            author_el = None
            for s in site.search_author_sels:
                author_el = item.select_one(s)
                if author_el:
                    break

            latest_el = None
            for s in site.search_latest_sels:
                latest_el = item.select_one(s)
                if latest_el:
                    break

            novels.append(
                {
                    "title": title,
                    "url": full_url,
                    "author": author_el.get_text(strip=True) if author_el else "未知",
                    "latest": latest_el.get_text(strip=True) if latest_el else "未知",
                    "_site": site,
                }
            )
        if novels:
            break
    return novels


def _search_from_site(site: SiteConfig, keyword: str) -> list[dict]:
    scraper = make_session()
    base = _warm_up(scraper, site)
    scraper.headers["Referer"] = base + "/"
    search_url = base + site.search_path

    try:
        if site.search_method.upper() == "POST":
            resp = _fetch(
                scraper,
                search_url,
                method="POST",
                data={site.search_param: keyword, "submit": ""},
            )
        else:
            resp = _fetch(scraper, f"{search_url}?{site.search_param}={keyword}")
    except Exception as exc:
        # Fallback: try GET if POST failed
        logger.warning("[%s] search primary failed (%s), trying GET fallback", site.name, exc)
        resp = _fetch(scraper, f"{search_url}?{site.search_param}={keyword}")

    html = _decode(resp)
    soup = BeautifulSoup(html, "lxml")
    return _parse_search(soup, site, base)


def search_novels(keyword: str) -> list[dict]:
    """輪流嘗試各站台搜尋，返回最多 10 筆結果"""
    errors: list[str] = []
    for site in SITES:
        try:
            results = _search_from_site(site, keyword)
            if results:
                logger.info("Search '%s' → %d results from %s", keyword, len(results), site.name)
                return results
        except Exception as exc:
            errors.append(f"{site.name}: {exc}")
            logger.warning("Site %s search failed: %s", site.name, exc)
    raise RuntimeError("所有站台均無法搜尋，請稍後再試。\n" + "\n".join(errors))


# ---------------------------------------------------------------------------
# Book info + chapter list
# ---------------------------------------------------------------------------

def _detect_site(book_url: str) -> SiteConfig:
    """依 URL 判斷屬於哪個站台，找不到則回傳第一個"""
    for site in SITES:
        domain = urlparse(site.base_url).netloc.replace("www.", "")
        if domain in book_url:
            return site
    return SITES[0]


def get_book_info(book_url: str) -> dict:
    """返回 {'title': str, 'chapters': [{'title': str, 'url': str}]}"""
    site = _detect_site(book_url)
    scraper = make_session()
    base = _warm_up(scraper, site)

    if not book_url.startswith("http"):
        book_url = base + book_url

    scraper.headers["Referer"] = base + "/"
    logger.info("[%s] getting book info: %s", site.name, book_url)

    resp = _fetch(scraper, book_url)
    html = _decode(resp)
    soup = BeautifulSoup(html, "lxml")

    title_el = soup.select_one("h1, .booktitle, .book-title, #bookinfo h1, .book-info h1")
    title = title_el.get_text(strip=True) if title_el else "未知"

    chapters = _extract_chapters(soup, site, book_url)

    if not chapters:
        for suffix in ("/catalog/", "/list/", "/index.html"):
            catalog_url = book_url.rstrip("/") + suffix
            logger.info("[%s] trying catalog: %s", site.name, catalog_url)
            try:
                resp2 = _fetch(scraper, catalog_url)
                soup2 = BeautifulSoup(_decode(resp2), "lxml")
                chapters = _extract_chapters(soup2, site, book_url)
                if chapters:
                    break
            except Exception as exc:
                logger.warning("[%s] catalog page failed: %s", site.name, exc)

    logger.info("[%s] book '%s': %d chapters", site.name, title, len(chapters))
    return {"title": title, "chapters": chapters}


def _extract_chapters(soup: BeautifulSoup, site: SiteConfig, base_url: str) -> list[dict]:
    chapters: list[dict] = []
    parsed_base = urlparse(base_url)
    origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

    for sel in site.chapter_list_sels:
        els = soup.select(sel)
        if not els:
            continue
        logger.info("[%s] chapter selector '%s' → %d", site.name, sel, len(els))
        for el in els:
            href = el.get("href", "")
            ch_title = el.get_text(strip=True)
            if href and ch_title and len(ch_title) > 1:
                full_url = href if href.startswith("http") else urljoin(origin, href)
                chapters.append({"title": ch_title, "url": full_url})
        break
    return chapters


# ---------------------------------------------------------------------------
# Chapter content
# ---------------------------------------------------------------------------

def get_chapter_content(
    chapter_url: str,
    session: Optional[cloudscraper.CloudScraper] = None,
) -> tuple[str, str]:
    """
    下載並清洗單章內文。
    返回 (chapter_title, cleaned_text)
    session 參數接受 CloudScraper 實例（与 downloader.py 共用）。
    """
    if session is None:
        session = make_session()
        site = _detect_site(chapter_url)
        _warm_up(session, site)

    # Referer 設為書籍目錄頁（chapter_url 的上層路徑）
    parsed = urlparse(chapter_url)
    parent_path = parsed.path.rsplit("/", 1)[0] + "/"
    session.headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}{parent_path}"

    resp = _fetch(session, chapter_url)
    html = _decode(resp)
    soup = BeautifulSoup(html, "lxml")

    # Chapter title
    title_el = soup.select_one("h1, .chapter-title, #bookname, .readtitle")
    chapter_title = title_el.get_text(strip=True) if title_el else ""

    # Detect site for content selectors
    site = _detect_site(chapter_url)

    content_div = None
    for sel in site.content_sels:
        content_div = soup.select_one(sel)
        if content_div:
            break

    if not content_div:
        logger.warning("No content div found for: %s", chapter_url)
        return chapter_title, ""

    # Remove noise tags
    for tag in content_div.select("script, style, .ad, .ads, .adsbygoogle, ins, iframe, a"):
        tag.decompose()

    raw = content_div.get_text("\n")

    cleaned: list[str] = []
    prev = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or _SKIP_RE.search(line):
            continue
        if line == prev:
            continue
        cleaned.append(line)
        prev = line

    return chapter_title, "\n".join(cleaned)
