"""Conversation persistence shared by API, Telegram, QQ and WeChat adapters."""
from __future__ import annotations
from datetime import datetime, timezone
from storage import get, insert

try:
    from zoneinfo import ZoneInfo
    from app_config import DEFAULT_TIMEZONE
    _TZ = ZoneInfo(DEFAULT_TIMEZONE)
except Exception:
    _TZ = timezone.utc


def _ago(ts) -> str:
    """把 UTC 时间戳转成「多久之前 + 具体几点几号」，让模型分得清刚才和上个月。"""
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


def recent_timed(scene: str = "message", limit: int = 14) -> list:
    """带相对时间+绝对日期标记的历史，模型才知道哪句是刚才说的、哪句是哪天的。"""
    base = f"type=eq.{scene}&order=seq.desc&limit={max(1,min(limit,100))}"
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
    out=[]
    for r in rows:
        content=r.get("content","")
        mid=r.get("message_id")
        if mid is not None:
            content=f"[id:{mid}] {content}"
        stamp=_ago(r.get("created_at"))
        if stamp:
            content=f"{stamp} {content}"
        out.append({"role":r.get("role","user"),"content":content})
    return out


def recent_groups(limit: int = 50) -> list:
    rows = get("chat_context", f"type=like.group_*&order=seq.desc&limit={max(1,min(limit,200))}")
    rows.reverse(); return rows


def recent_private(limit: int = 15) -> list:
    """获取私聊最近对话，供群聊了解前情但绝不泄密"""
    rows = get("chat_context", f"type=eq.message&order=seq.desc&limit={max(1,min(limit,50))}")
    rows.reverse()
    return rows
