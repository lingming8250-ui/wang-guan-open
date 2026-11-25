"""Public context engine.

优化前缀缓存架构：
将长期静态前缀（系统设定、人设特征、核心事实、柜中罐子、内心印记、往事编年史）严格放在头部，
确保命中 Gemini / 中转站的前缀缓存（Prompt Cache），大幅降低 Token 消耗与计费。
而随时间变化的动态生活切片、近期对话流、当前时间则作为尾部注入。
"""
from __future__ import annotations
import os, re, json, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Callable
from app_config import DEFAULT_TIMEZONE, AI_NAME, USER_NAME
from storage import get, get_setting

ContextProvider = Callable[[], str]
_PROVIDERS: list[ContextProvider] = []

_CACHE_TTL = float(os.getenv("CONTEXT_CACHE_TTL", "180"))
_MEMORY_LIMIT = int(os.getenv("CONTEXT_MEMORY_LIMIT", "60"))
_cache = {"body": "", "at": 0.0}

_RECALL_TRIGGERS = (
    "还记得", "记得吗", "记不记得", "之前", "上次", "上回", "以前", "那次", "那天",
    "多久", "什么时候", "几号", "哪天", "第一次", "最早", "当初", "那会儿"
)
_STOP_WORDS = {
    "还记得", "记得吗", "记不记得", "之前", "上次", "上回", "以前", "那次", "那天",
    "我们", "你们", "他们", "什么", "怎么", "为什么", "那个", "这个", "就是", "然后",
    "感觉", "好像", "是不是", "能不能", "可以", "一下", "来着", "还是", "没有", "已经"
}


def register_context_provider(provider: ContextProvider):
    if provider not in _PROVIDERS:
        _PROVIDERS.append(provider)


def unregister_context_provider(provider: ContextProvider):
    if provider in _PROVIDERS:
        _PROVIDERS.remove(provider)


def _base_prompt():
    env_prompt = os.getenv("SYSTEM_PROMPT", "").strip()
    if env_prompt:
        return env_prompt
    try:
        from runtime_config import get_system_prompt
        p = get_system_prompt()
        if p and p != "You are a helpful AI assistant.":
            return p
    except Exception:
        pass
    return f"You are {AI_NAME}, an assistant for {USER_NAME}."


def _persona():
    rows = get("persona_profile", "select=category,key,content&order=category.asc")
    if not rows:
        return ""
    groups = {}
    for r in rows:
        groups.setdefault(r.get("category", "persona"), []).append(f"[{r.get('key','item')}] {r.get('content','')}")
    return "【Persona / configurable profile】\n" + "\n".join(f"[{k}]\n" + "\n".join(v) for k, v in groups.items())


def _memories():
    rows = get(
        "memories",
        f"memory_layer=in.(core,current,long_term)&order=importance.desc,id.desc&limit={_MEMORY_LIMIT}"
        "&select=content,memory_layer,importance,category,tags",
    )
    if not rows:
        return ""
    jars: dict[str, list[str]] = {}
    general: list[str] = []
    for r in rows:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        cat = (r.get("category") or "").strip()
        layer = r.get("memory_layer", "long_term")
        imp = r.get("importance", 3)
        if cat.startswith("罐子:"):
            jar_name = cat.split(":", 1)[1].strip() or "关于你"
            jars.setdefault(jar_name, []).append(f"  · {content}")
        else:
            general.append(f"- [{layer}|{imp}] {content}")

    out = []
    if general:
        out.append("【Layered memory · 核心与长期记忆（共 " + str(len(general)) + " 条）】\n" + "\n".join(general))
    if jars:
        jar_blocks = []
        for jname in ("关于你", "我们之间", "身边的人", "TA自己", "往事"):
            if jname in jars:
                jar_blocks.append(f"▸ [{jname}]\n" + "\n".join(jars.pop(jname)))
        for jname, items in jars.items():
            jar_blocks.append(f"▸ [{jname}]\n" + "\n".join(items))
        out.append("【柜中罐子 · 收藏的原子事实细节】\n" + "\n".join(jar_blocks))
    return "\n\n".join(out)


def _inner_traces():
    raw = get_setting("inner_traces_json", "")
    if not raw:
        return ""
    try:
        arr = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return ""
    if not isinstance(arr, list) or not arr:
        return ""
    lines = []
    for item in arr[-6:]:
        if not isinstance(item, dict):
            continue
        dt = item.get("date", "")
        rel = (item.get("relation") or "").strip()
        slf = (item.get("self") or "").strip()
        parts = []
        if rel:
            parts.append(f"对凌冥：{rel}")
        if slf:
            parts.append(f"自身状态：{slf}")
        if parts:
            lines.append(f"- 〔{dt}〕" + " ｜ ".join(parts))
    if not lines:
        return ""
    return "【内心印记 · 薄销的双轨心迹演进（你的态度与情绪底色）】\n" + "\n".join(lines)


def _chronicle():
    text = (get_setting("long_term_chronicle", "") or "").strip()
    if not text:
        return ""
    return "【往事编年史 · 更早之前的重要经历与约定（永久归档）】\n" + text


def _threads():
    rows = get("threads", "status=in.(ACTIVE,DORMANT)&order=created_at.asc&limit=6&select=title,synthetic_guess,status,evidence")
    if not rows:
        return ""
    return "【Open threads】\n" + "\n".join(f"- [{r.get('status')}] {r.get('title','')}: {r.get('synthetic_guess','')}" for r in rows)


def _short_date(ts) -> str:
    if not ts:
        return ""
    try:
        s = str(ts).replace("Z", "+00:00")
        if "." in s:
            head, _, tail = s.partition(".")
            rest = tail
            for sep in ("+", "-"):
                if sep in rest:
                    rest = rest[rest.index(sep):]
                    break
            else:
                rest = ""
            s = head + rest
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _rolling():
    rows = get("platform_rolling_summary", "order=id.desc&limit=1&select=content,period_start,period_end")
    if not rows:
        return ""
    content = (rows[0].get("content") or "").strip()
    if not content:
        return ""
    upto = _short_date(rows[0].get("period_end"))
    head = "【最近这段对话的详细记忆摘要（出了滑窗的近期内容都在这里）"
    head += f"，更新至 {upto}】" if upto else "】"
    return head + "\n" + content


def _recent_private():
    """实时注入最新的QQ私聊对话流，保证外部客户端（如Kelivo）与QQ无缝实时对齐，不产生时间差"""
    try:
        from conversation_store import recent_private
        rows = recent_private(limit=18)
        if not rows:
            return ""
        lines = []
        for r in rows:
            c = (r.get("content") or "").strip()
            if c.startswith("[QQ] "):
                c = c[5:].strip()
            dt = _short_date(r.get("created_at"))
            who = "凌冥" if r.get("role") == "user" else "薄销"
            if c:
                prefix = f"〔{dt}〕" if dt else ""
                lines.append(f"{prefix}{who}: {c}")
        if not lines:
            return ""
        return "【QQ私聊实时对话流（最新原话，各窗口神经与记忆绝对实时同步）】\n" + "\n".join(lines)
    except Exception:
        return ""


def _recent_groups():
    try:
        from conversation_store import recent_groups
        rows = recent_groups(limit=10)
        if not rows:
            return ""
        lines = [f"{r.get('content','')}" for r in rows if r.get("content")]
        if not lines:
            return ""
        return "【群聊实时动态追踪（你在私聊与当前对话中知晓这些群聊前情）】\n" + "\n".join(lines)
    except Exception:
        return ""


def _daily_life():
    try:
        from daily_life import get_daily_life_context
        return get_daily_life_context()
    except Exception:
        return ""


def recall_older_context(user_text: str) -> str:
    """当凌冥提到'之前/上次/还记得吗'时，自动从更早的聊天记录和全量记忆库里检索原话片段。"""
    t = (user_text or "").strip()
    if len(t) < 2:
        return ""
    if not any(k in t for k in _RECALL_TRIGGERS):
        return ""
    words = [w for w in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,8}", t) if w not in _STOP_WORDS]
    if not words:
        return ""
    kw = max(words, key=len)
    safe = "".join(c for c in kw if c not in "&=?(),*%")
    if len(safe) < 2:
        return ""
    hits = []
    try:
        mem_rows = get("memories", f"content=ilike.*{safe}*&order=importance.desc&limit=6&select=content,category")
        for r in mem_rows:
            c = (r.get("content") or "").strip()
            if c:
                hits.append(f"- [记忆库] {c}")
    except Exception:
        pass
    try:
        chat_rows = get("chat_context", f"content=ilike.*{safe}*&order=seq.desc&limit=8&select=role,content,created_at")
        for r in chat_rows[2:]:
            c = (r.get("content") or "").strip()
            dt = _short_date(r.get("created_at"))
            who = "凌冥" if r.get("role") == "user" else "薄销"
            if c:
                hits.append(f"- [{dt} 原话] {who}: {c[:160]}")
    except Exception:
        pass
    if not hits:
        return ""
    return f"【按关键词「{safe}」从更早记忆库与历史原话中翻出的旧账】\n" + "\n".join(hits[:8])


def _time():
    now = datetime.now(ZoneInfo(DEFAULT_TIMEZONE))
    return f"【Current time】{now.strftime('%Y-%m-%d %H:%M %Z')}"


def _tick_rolling():
    try:
        import rolling_memory
        rolling_memory.maybe_schedule()
    except Exception:
        pass


def _build_body() -> str:
    # 严格按照“稳定静态 -> 半静态 -> 动态”的顺序排序，最大化前缀匹配并触发 Prompt Cache
    parts = [_base_prompt()]
    loaders = (
        _persona,       # 静态人设
        _memories,      # 长期分层记忆与罐子（极稳定）
        _chronicle,     # 远期编年史（极稳定）
        _inner_traces,  # 双轨心迹
        _rolling,       # 滚动大摘要
        _threads,       # 待办/未结线程
        _daily_life,    # 当日生活切片
        _recent_groups, # 群聊动态
        _recent_private # QQ最新原话流（变化频率最高，置于末尾）
    )
    try:
        with ThreadPoolExecutor(max_workers=len(loaders)) as pool:
            results = list(pool.map(_safe, loaders))
    except Exception:
        results = []
    for v in results:
        if v:
            parts.append(v)
    for fn in list(_PROVIDERS):
        try:
            v = (fn() or "").strip()
        except Exception as exc:
            v = f"[context provider failed: {type(exc).__name__}]"
        if v:
            parts.append(v)
    return "\n\n".join(p for p in parts if p)


def _safe(fn):
    try:
        return (fn() or "").strip()
    except Exception:
        return ""


def build_context():
    now = time.time()
    body = _cache["body"]
    if not body or now - _cache["at"] > _CACHE_TTL:
        try:
            body = _build_body()
            if body:
                _cache["body"] = body
                _cache["at"] = now
        except Exception:
            body = body or ""
    parts = [body]
    if os.getenv("INCLUDE_TIME_CONTEXT", "1") != "0":
        parts.append(_time())
    _tick_rolling()
    return "\n\n".join(p for p in parts if p)


def clear_cache():
    _cache["body"] = ""
    _cache["at"] = 0.0