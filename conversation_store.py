"""Conversation persistence shared by API, Telegram, QQ and WeChat adapters."""
from __future__ import annotations
import re
from collections import deque
from datetime import datetime, timezone
from storage import get, insert

try:
    from zoneinfo import ZoneInfo
    from app_config import DEFAULT_TIMEZONE
    _TZ = ZoneInfo(DEFAULT_TIMEZONE)
except Exception:
    _TZ = timezone.utc

_TIME_TAG_RE = re.compile(r'^\s*[（(]\s*(?:刚刚|今天|昨天|前天|\d+\s*分钟前|\d+\s*小时前|\d+\s*月\d+\s*日|\d{4}年)[^）)]*[）)]\s*')

_SEEN_IDS: deque = deque(maxlen=4000)


def _strip_time_tags(s: str) -> str:
    s = s or ""
    while True:
        n = _TIME_TAG_RE.sub('', s, count=1)
        if n == s:
            return s
        s = n


def _ago(ts) -> str:
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
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(_TZ)
        now = datetime.now(_TZ)
        delta = (now - local).total_seconds()
    except Exception:
        return ""
    if delta < 0:
        delta = 0
    if delta < 60:
        return "（刚刚）"
    if delta < 3600:
        return f"（{int(delta // 60)}分钟前 {local:%H:%M}）"
    days = (now.date() - local.date()).days
    if days == 0:
        return f"（今天 {local:%H:%M}）"
    if days == 1:
        return f"（昨天 {local:%H:%M}）"
    if days <= 6:
        return f"（{local.month}月{local.day}日 星期{'一二三四五六日'[local.weekday()]} {local:%H:%M}）"
    if local.year == now.year:
        return f"（{local.month}月{local.day}日 {local:%H:%M}）"
    return f"（{local.year}年{local.month}月{local.day}日 {local:%H:%M}）"


def save_message(role: str, content: str, *, scene: str = "message", source: str = "", message_id=None):
    if not content: return
    if message_id is not None:
        key = str(message_id)
        if key in _SEEN_IDS:
            return
        _SEEN_IDS.append(key)
    body = {"type": scene, "role": role, "content": content}
    if source: body["source"] = source
    if message_id is not None: body["message_id"] = str(message_id)
    try: insert("chat_context", body)
    except Exception: pass


def recent(scene: str = "message", limit: int = 30, with_ids: bool = False) -> list:
    rows = get("chat_context", f"type=eq.{scene}&order=seq.desc&limit={max(1,min(limit,100))}")
    rows.reverse()
    out=[]
    for r in rows:
        content=r.get("content","")
        if with_ids and r.get("message_id") is not None:
            content=f"[id:{r['message_id']}] {content}"
        out.append({"role":r.get("role","user"),"content":content})
    return out


def recent_timed(scene: str = "message", limit: int = 20) -> list:
    """带时间标记的最近对话。

    助手一次回好几条气泡，每条都单独存一行，会把窗口挤得只剩几轮。
    这里多取几行，再把连着的气泡并成一条，同样的窗口能装下更多轮对话。
    """
    want = max(1, min(int(limit), 100))
    fetch = max(want, min(want * 3, 120))
    base = f"type=eq.{scene}&order=seq.desc&limit={fetch}"
    rows = None
    try:
        rows = get("chat_context", base + "&select=role,content,message_id,created_at")
    except Exception:
        rows = None
    if not rows:
        try:
            rows = get("chat_context", base)
        except Exception:
            return []
    rows.reverse()

    merged = []
    for r in rows:
        role = r.get("role", "user")
        content = r.get("content", "") or ""
        mid = r.get("message_id")
        if mid is not None:
            content = f"[id:{mid}] {content}"

        if role == "user":
            stamp = _ago(r.get("created_at"))
            if stamp:
                content = f"{stamp} {content}"
        else:
            content = _strip_time_tags(content)
            if merged and merged[-1]["role"] == "assistant" and not content.startswith("[id:"):
                merged[-1]["content"] = merged[-1]["content"] + "\n" + content
                continue

        merged.append({"role": role, "content": content})

    return merged[-want:]


def recent_groups(limit: int = 50) -> list:
    rows = get("chat_context", f"type=like.group_*&order=seq.desc&limit={max(1,min(limit,200))}")
    rows.reverse(); return rows


def recent_private(limit: int = 15) -> list:
    """获取私聊最近对话，供群聊了解前情但绝不泄密"""
    rows = get("chat_context", f"type=eq.message&order=seq.desc&limit={max(1,min(limit,50))}")
    rows.reverse()
    return rows
