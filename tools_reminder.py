from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from storage import insert, get, update, delete_ids
from app_config import DEFAULT_TIMEZONE

_TZ = ZoneInfo(DEFAULT_TIMEZONE)

def timer_add(minutes: int, message: str = "") -> str:
    """添加单次倒计时提醒，到点通过 QQ 私聊推送，且仅执行一次后自动标记完成。"""
    try:
        mins = int(minutes)
        if mins <= 0:
            return "计时时间必须大于0分钟。"
    except Exception:
        return "时间格式不正确。"

    now = datetime.now(_TZ)
    target = now + timedelta(minutes=mins)
    # 统一存 UTC ISO 时间供后台轮询
    target_utc = target.astimezone(timezone.utc).isoformat()
    msg_text = message.strip() or f"{mins}分钟倒计时到啦"

    insert("reminders", {
        "message": msg_text,
        "trigger_at": target_utc,
        "repeat_type": "once",
        "is_done": False
    })
    return f"已开启{mins}分钟倒计时（预计 {target.strftime('%H:%M')} 提醒：{msg_text}），仅提醒一次。"

def timer_cancel(keyword: str = "") -> str:
    """取消未触发的倒计时/提醒。"""
    q = "is_done=eq.false&order=id.desc&limit=10"
    rows = get("reminders", q)
    if not rows:
        return "目前没有正在进行中的倒计时。"

    matched_ids = []
    kw = (keyword or "").strip().lower()
    for r in rows:
        m = (r.get("message") or "").lower()
        if not kw or kw in m or kw in ("全部", "所有", "计时", "倒计时"):
            matched_ids.append(str(r["id"]))

    if not matched_ids:
        return f"没找到跟「{keyword}」相关的进行中倒计时。"

    delete_ids("reminders", matched_ids)
    return f"已成功取消 {len(matched_ids)} 个倒计时任务。"

def timer_list() -> str:
    """查看当前所有进行中的倒计时。"""
    now_utc = datetime.now(timezone.utc).isoformat()
    rows = get("reminders", f"is_done=eq.false&trigger_at=gt.{now_utc}&order=trigger_at.asc&limit=10")
    if not rows:
        return "目前没有进行中的倒计时。"
    lines = []
    for r in rows:
        try:
            from dateutil.parser import isoparse
            t = isoparse(r["trigger_at"]).astimezone(_TZ)
            lines.append(f"- 预计 {t.strftime('%H:%M')} 提醒：{r.get('message')}")
        except Exception:
            lines.append(f"- {r.get('trigger_at')} 提醒：{r.get('message')}")
    return "当前进行中的倒计时：\n" + "\n".join(lines)

def reminder_add(message, trigger_at, repeat_type="once"):
    insert("reminders", {"message": message, "trigger_at": trigger_at, "repeat_type": repeat_type, "is_done": False})
    return "Reminder created."

def reminder_list(include_done=False):
    q = "order=trigger_at.asc&limit=50" + ("" if include_done else "&is_done=eq.false")
    rows = get("reminders", q)
    return "\n".join(f"- {r.get('trigger_at')} | {r.get('message')} | {r.get('repeat_type','once')}" for r in rows) or "No reminders."

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "timer_add",
            "description": "设置单次倒计时提醒（例如5分钟后叫我起床、10分钟后面膜到时间）。到点自动在QQ发消息提醒，并且仅提醒一次，绝不会跨天重复。",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "description": "多少分钟后提醒，例如 5 或 10"},
                    "message": {"type": "string", "description": "到点要提醒的内容，例如'面膜敷好了快去洗脸'或'五分钟到了该起床啦'"}
                },
                "required": ["minutes"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "timer_cancel",
            "description": "取消正在进行中的倒计时或提醒任务。当凌冥说'不用提醒了'、'取消计时'、'算了不用叫我了'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要取消的关键词，如'面膜'、'起床'，不填或填'全部'则取消所有"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "timer_list",
            "description": "列出当前所有尚未到期的倒计时提醒任务。",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]

DISPATCH = {
    "timer_add": lambda a: timer_add(a.get("minutes", 0), a.get("message", "")),
    "timer_cancel": lambda a: timer_cancel(a.get("keyword", "")),
    "timer_list": lambda a: timer_list(),
    "reminder_add": lambda a: reminder_add(a.get("message", ""), a.get("trigger_at", ""), a.get("repeat_type", "once")),
    "reminder_list": lambda a: reminder_list(a.get("include_done", False))
}