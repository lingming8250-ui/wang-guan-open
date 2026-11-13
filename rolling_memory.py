"""滚动记忆：近的原文照留，出了窗口就压成一段摘要，而不是等攒够一大坨再一次性压。

思路照小手机那套：滑窗 + 递进摘要。
水印记在 bot_settings.compress_watermark_seq，压过的就不再压，原文不删。
"""
from __future__ import annotations
import os, time, threading, logging
from storage import get, insert, get_setting, upsert_setting

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_last = 0.0

INTERVAL = float(os.getenv("ROLLING_INTERVAL", "600"))     # 最快每 10 分钟跑一次
MIN_ROWS = int(os.getenv("ROLLING_MIN_ROWS", "30"))        # 攒够这么多条才值得压
MAX_ROWS = int(os.getenv("ROLLING_MAX_ROWS", "40"))
MAX_CHARS = int(os.getenv("ROLLING_MAX_CHARS", "1200"))

_PROMPT = (
    "【上一版摘要】\n{prev}\n\n【新增对话】\n{dialog}\n\n"
    "把上面两部分合并成一份新的对话记忆摘要，以后靠它回想。要求：\n"
    "1. 只留长期有用的：他的近况、计划、喜好、身体情况、情绪走向、约好的事、没结的线头。\n"
    "2. 丢掉寒暄、重复、一次性的琐碎。\n"
    "3. 不超过 {max_chars} 字，短句罗列，不要标题，不要客套，不要评价。\n"
    "4. 提到时间就写具体日期，不要写「今天」「刚才」。"
)


def _llm(system: str, user: str, max_tokens: int = 800) -> str:
    import asyncio
    from llm_runtime import call_llm
    text, _ = asyncio.run(call_llm(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens, channel="background"))
    return (text or "").strip()


def compress_now() -> int:
    """把水印之后的老消息并进摘要。返回这次吞掉的条数。"""
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

    dialog = "\n".join(
        f"{'凌冥' if r.get('role') == 'user' else '薄销'}: {(r.get('content') or '')[:400]}"
        for r in rows
    )

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
    """请求路径上调一下就走，不阻塞回复。"""
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
