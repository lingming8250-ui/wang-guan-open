"""滚动记忆：近的原文照留，出了窗口就压成一段摘要。

摘要里每条都带日期，方便以后分清「上周说的」和「昨天说的」。
水印记在 bot_settings.compress_watermark_seq，压过的就不再压，原文不删。
"""
from __future__ import annotations
import os, time, threading, logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from storage import get, insert, get_setting, upsert_setting
from app_config import DEFAULT_TIMEZONE

log = logging.getLogger(__name__)

_TZ = ZoneInfo(DEFAULT_TIMEZONE)
_LOCK = threading.Lock()
_last = 0.0

INTERVAL = float(os.getenv("ROLLING_INTERVAL", "600"))
MIN_ROWS = int(os.getenv("ROLLING_MIN_ROWS", "30"))
MAX_ROWS = int(os.getenv("ROLLING_MAX_ROWS", "40"))
MAX_CHARS = int(os.getenv("ROLLING_MAX_CHARS", "1200"))

_PROMPT = (
    "【上一版摘要】\n{prev}\n\n【新增对话（每条前面是发生时间）】\n{dialog}\n\n"
    "把上面两部分合并成一份新的对话记忆摘要，以后靠它回想。要求：\n"
    "1. 每一条都必须以日期开头，格式写作〔YYYY-MM-DD〕；同一天的写同一天，不确定就写〔约YYYY-MM-DD〕。\n"
    "2. 只留长期有用的：他的近况、计划、喜好、身体情况、情绪走向、约好的事、没结的线头。\n"
    "3. 丢掉寒暄、重复、一次性的琐碎。\n"
    "4. 按时间先后排列，最多 {max_chars} 字，短句罗列，不要标题不要客套不要评价。\n"
    "5. 旧摘要里已经有日期的那几条，保留原来的日期，不要重写。"
)


def _stamp(ts) -> str:
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
        return dt.astimezone(_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _llm(system: str, user: str, max_tokens: int = 900) -> str:
    import asyncio
    from llm_runtime import call_llm
    text, _ = asyncio.run(call_llm(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens, channel="background"))
    return (text or "").strip()


def compress_now() -> int:
    try:
        watermark = int(get_setting("compress_watermark_seq", "0") or 0)
    except Exception:
        watermark = 0

    rows = get("chat_context",
               f"seq=gt.{watermark}&order=seq.asc&limit={MAX_ROWS}"
               "&select=id,seq,role,content,created_at")
    rows = [r for r in rows if (r.get("content") or "").strip()]
    if len(rows) < MIN_ROWS:
        return 0

    lines = []
    for r in rows:
        who = "凌冥" if r.get("role") == "user" else "薄销"
        when = _stamp(r.get("created_at")) or "未知日期"
        lines.append(f"[{when}] {who}: {(r.get('content') or '')[:400]}")
    dialog = "\n".join(lines)

    old = get("platform_rolling_summary", "order=id.desc&limit=1&select=content")
    prev = (old[0].get("content") if old else "") or ""

    summary = _llm("你在整理长期记忆。只输出摘要正文，不要任何解释。",
                   _PROMPT.format(prev=prev or "（无）", dialog=dialog, max_chars=MAX_CHARS))
    if not summary:
        return 0
    summary = summary[:MAX_CHARS]

    insert("platform_rolling_summary", {
        "content": summary,
        "source_platforms": "qq",
        "period_start": rows[0].get("created_at") or "",
        "period_end": rows[-1].get("created_at") or "",
    })
    upsert_setting("compress_watermark_seq", str(rows[-1].get("seq") or watermark))
    log.info("rolling summary updated: %s rows", len(rows))
    return len(rows)


def maybe_schedule() -> None:
    global _last
    now = time.time()
    with _LOCK:
        if now - _last < INTERVAL:
            return
        _last = now
    threading.Thread(target=_run, daemon=True).start()


def _run():
    try:
        compress_now()
    except Exception:
        log.exception("rolling compress failed")
