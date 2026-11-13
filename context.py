"""Public context engine.

The private deployment injects many personal providers. The public build keeps the same
composition pattern but uses generic data sources: configurable persona, layered memories,
rolling cross-platform summary, unresolved threads and time. Optional providers can be
registered by extensions without editing this file.
"""
from __future__ import annotations
import os, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Callable
from app_config import DEFAULT_TIMEZONE, AI_NAME, USER_NAME
from storage import get

ContextProvider=Callable[[],str]
_PROVIDERS:list[ContextProvider]=[]

# 上下文里除了时间以外的东西，一分钟内不用重查；每条消息都去 Supabase 跑五个请求太慢
_CACHE_TTL=float(os.getenv("CONTEXT_CACHE_TTL","60"))
_cache={"body":"","at":0.0}


def register_context_provider(provider:ContextProvider):
    if provider not in _PROVIDERS:_PROVIDERS.append(provider)

def unregister_context_provider(provider:ContextProvider):
    if provider in _PROVIDERS:_PROVIDERS.remove(provider)


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
    rows=get("persona_profile","select=category,key,content&order=category.asc")
    if not rows:return ""
    groups={}
    for r in rows:groups.setdefault(r.get("category","persona"),[]).append(f"[{r.get('key','item')}] {r.get('content','')}")
    return "【Persona / configurable profile】\n"+"\n".join(f"[{k}]\n"+"\n".join(v) for k,v in groups.items())


def _memories():
    rows=get("memories","memory_layer=in.(core,current,long_term)&order=importance.desc&limit=32&select=content,memory_layer,importance,tags")
    if not rows:return ""
    return "【Layered memory】\n"+"\n".join(f"- [{r.get('memory_layer')}|{r.get('importance',3)}] {r.get('content','')}" for r in rows)


def _threads():
    rows=get("threads","status=in.(ACTIVE,DORMANT)&order=created_at.asc&limit=6&select=title,synthetic_guess,status,evidence")
    if not rows:return ""
    return "【Open threads】\n"+"\n".join(f"- [{r.get('status')}] {r.get('title','')}: {r.get('synthetic_guess','')}" for r in rows)


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
    rows=get("platform_rolling_summary","order=id.desc&limit=1&select=content,period_start,period_end")
    if not rows:return ""
    content=(rows[0].get("content") or "").strip()
    if not content:return ""
    upto=_short_date(rows[0].get("period_end"))
    head="【最近这段对话的记忆摘要（出了滑窗的老内容都在这里）"
    head += f"，更新至 {upto}】" if upto else "】"
    return head+"\n"+content


def _recent_groups():
    try:
        from conversation_store import recent_groups
        rows = recent_groups(limit=10)
        if not rows: return ""
        lines = [f"{r.get('content','')}" for r in rows if r.get('content')]
        if not lines: return ""
        return "【群聊实时动态追踪（你在私聊与当前对话中知晓这些群聊前情）】\n" + "\n".join(lines)
    except Exception:
        return ""


def _time():
    now=datetime.now(ZoneInfo(DEFAULT_TIMEZONE))
    return f"【Current time】{now.strftime('%Y-%m-%d %H:%M %Z')}"


def _tick_rolling():
    try:
        import rolling_memory
        rolling_memory.maybe_schedule()
    except Exception:
        pass


def _build_body() -> str:
    parts=[_base_prompt()]
    loaders=(_persona,_memories,_threads,_rolling,_recent_groups)
    # 五个查询并行跑，别一条一条等
    try:
        with ThreadPoolExecutor(max_workers=len(loaders)) as pool:
            results=list(pool.map(_safe, loaders))
    except Exception:
        results=[]
    for v in results:
        if v:
            parts.append(v)
    for fn in list(_PROVIDERS):
        try:v=(fn() or "").strip()
        except Exception as exc:v=f"[context provider failed: {type(exc).__name__}]"
        if v:parts.append(v)
    return "\n\n".join(p for p in parts if p)


def _safe(fn):
    try:
        return (fn() or "").strip()
    except Exception:
        return ""


def build_context():
    now=time.time()
    body=_cache["body"]
    if not body or now-_cache["at"]>_CACHE_TTL:
        try:
            body=_build_body()
            if body:
                _cache["body"]=body
                _cache["at"]=now
        except Exception:
            body=body or ""
    parts=[body]
    if os.getenv("INCLUDE_TIME_CONTEXT","1")!="0":
        parts.append(_time())
    _tick_rolling()
    return "\n\n".join(p for p in parts if p)


def clear_cache():
    _cache["body"]=""
    _cache["at"]=0.0
