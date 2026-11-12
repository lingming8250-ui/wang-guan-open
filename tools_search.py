"""Free zero-config web search provider using DuckDuckGo HTML / Instant Answers.
No external dependencies required, built purely on httpx + regex.
"""
import re, logging, html, urllib.parse, httpx

log = logging.getLogger(__name__)

def web_search(query: str, max_results: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return "Search query cannot be empty."

    # 1. 尝试 DuckDuckGo Lite / HTML API
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    results = []

    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            # 优先尝试 duckduckgo html
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            r = client.get(url, headers=headers)
            if r.status_code == 200:
                raw_html = r.text
                # 提取搜索结果块: result__snippet 和 result__title
                snippets = re.findall(r'<a[^>]+class="result__snippet[^>]*>(.*?)</a>', raw_html, re.S)
                titles = re.findall(r'<a[^>]+class="result__url[^>]*>(.*?)</a>', raw_html, re.S)
                for i in range(min(len(snippets), max_results)):
                    clean_s = re.sub(r'<[^>]+>', '', snippets[i])
                    clean_s = html.unescape(clean_s).strip()
                    if clean_s:
                        results.append(f"{i+1}. {clean_s}")
    except Exception as exc:
        log.warning("DuckDuckGo HTML search failed: %s", exc)

    # 2. 如果被风控或无结果，fallback 到百科/即时摘要
    if not results:
        try:
            with httpx.Client(timeout=8.0) as client:
                api_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
                r = client.get(api_url, headers=headers)
                if r.status_code == 200:
                    d = r.json()
                    abstract = d.get("AbstractText") or ""
                    if abstract:
                        results.append(f"1. {abstract}")
                    for topic in (d.get("RelatedTopics") or [])[:max_results]:
                        txt = topic.get("Text")
                        if txt:
                            results.append(f"- {txt}")
        except Exception as exc:
            log.warning("DuckDuckGo Instant Answer failed: %s", exc)

    if not results:
        return f"未检索到关于「{query}」的有效即时公开结果，可能是搜索源暂时受到频控。"

    return "\n".join(results[:max_results])

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网实时搜索最新资讯、新闻、天气、百科、近期事件等。遇到自己不确定的近况或需要查证的事实时必须调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或短语"
                    }
                },
                "required": ["query"]
            }
        }
    }
]

DISPATCH = {
    "web_search": lambda a: web_search(a.get("query", ""))
}
