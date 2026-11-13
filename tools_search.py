"""Free zero-config web search: Bing first, DuckDuckGo as fallback.

只依赖 httpx + re，不需要任何 API Key。
服务器 IP 调 DuckDuckGo 经常被风控（返回 200 但是空壳页），所以主路走 Bing。
"""
import re, logging, html, urllib.parse, httpx

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).replace("\xa0", " ").strip()


def _bing(query: str, limit: int) -> list:
    out = []
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query) + "&count=10&setlang=zh-CN"
    with httpx.Client(timeout=12.0, follow_redirects=True, headers=_HEADERS) as c:
        r = c.get(url)
        if r.status_code != 200:
            log.warning("bing status %s", r.status_code)
            return out
        page = r.text
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S)[:limit]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        link = html.unescape(m.group(1))
        title = _clean(m.group(2))
        p = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        snippet = _clean(p.group(1)) if p else ""
        if title and (snippet or link):
            out.append(f"{len(out)+1}. {title}\n   {snippet}\n   {link}")
    return out


def _ddg(query: str, limit: int) -> list:
    out = []
    with httpx.Client(timeout=10.0, follow_redirects=True, headers=_HEADERS) as c:
        r = c.post("https://html.duckduckgo.com/html/", data={"q": query})
        if r.status_code != 200:
            return out
        page = r.text
    blocks = re.findall(r'<div class="result__body">(.*?)</div>\s*</div>', page, re.S)
    if not blocks:
        blocks = re.findall(r'<div class="result results_links.*?</div>\s*</div>', page, re.S)
    for block in blocks[:limit]:
        m = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        link = html.unescape(m.group(1))
        title = _clean(m.group(2))
        s = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        snippet = _clean(s.group(1)) if s else ""
        if title:
            out.append(f"{len(out)+1}. {title}\n   {snippet}\n   {link}")
    return out


def _ddg_api(query: str, limit: int) -> list:
    out = []
    with httpx.Client(timeout=8.0, headers=_HEADERS) as c:
        r = c.get("https://api.duckduckgo.com/",
                  params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
        if r.status_code != 200:
            return out
        d = r.json()
    if d.get("AbstractText"):
        out.append(f"1. {d['AbstractText']}")
    for t in (d.get("RelatedTopics") or [])[:limit]:
        txt = t.get("Text") if isinstance(t, dict) else None
        if txt:
            out.append(f"- {txt}")
    return out


def web_search(query: str, max_results: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return "搜索词是空的。"

    for name, fn in (("bing", _bing), ("ddg", _ddg), ("ddg_api", _ddg_api)):
        try:
            hits = fn(query, max_results)
        except Exception as exc:
            log.warning("search %s failed: %s", name, exc)
            continue
        if hits:
            log.info("search %s hit %s results for %r", name, len(hits), query)
            return "\n".join(hits[:max_results])

    return f"没搜到「{query}」的结果（搜索源可能被限流）。"


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网实时搜索新闻、天气、赛事、价格、近期事件等任何你不确定的最新信息。问到这类事必须先调用它。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    }
]

DISPATCH = {"web_search": lambda a: web_search(a.get("query", ""))}
