"""Background jobs for reminders, summaries, proactive messaging and rolling compression.

Private deployments can override prompts and delivery adapters without changing scheduling code.
Secret diary parsing is intentionally not part of the public build.
"""
from __future__ import annotations
import asyncio, random, logging, os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from storage import get, update, get_setting, upsert_setting
from conversation_store import recent
from llm_runtime import call_llm
from prompts import PROACTIVE_EXAMPLE
from scheduled import run_nightly_summary, run_platform_batch_compress, run_platform_summary_maintenance
from app_config import DEFAULT_TIMEZONE

log=logging.getLogger(__name__)


def _default_reminder_sender():
    """Build a delivery callback from configured public transports.

    Telegram is supported out of the box. Other platforms can pass their own callback
    when registering reminder_checker. Missing delivery configuration returns None;
    due reminders must stay pending rather than being silently consumed.
    """
    token=os.getenv("TG_BOT_TOKEN","").strip()
    owner=os.getenv("TG_OWNER_ID",os.getenv("TG_CHAT_ID","")).strip()
    if token and owner:
        async def _send(message:str) -> bool:
            from telegram_bot import send_message
            return bool(await asyncio.to_thread(send_message,message,owner))
        return _send
    return None


async def reminder_checker(send_callback=None):
    sender=send_callback or _default_reminder_sender()
    if sender is None:
        log.info("Reminder checker has no delivery transport; due reminders will remain pending")
    while True:
        await asyncio.sleep(60)
        rows=get("reminders",f"is_done=eq.false&trigger_at=lte.{datetime.now(timezone.utc).isoformat()}&order=trigger_at.asc")
        for row in rows:
            msg=(row.get("message") or "").strip()
            if not msg:
                update("reminders",f"id=eq.{row['id']}",{"is_done":True})
                continue
            if sender is None:
                continue
            try:
                ok=bool(await sender(msg))
            except Exception as exc:
                log.warning("reminder delivery failed id=%s: %s",row.get("id"),exc)
                ok=False
            if ok:
                repeat=(row.get("repeat_type") or "once").strip().lower()
                if repeat in {"daily","weekly"}:
                    from dateutil.parser import isoparse
                    from datetime import timedelta
                    try:
                        next_at=isoparse(str(row.get("trigger_at") or ""))
                        step=timedelta(days=1 if repeat=="daily" else 7)
                        now_utc=datetime.now(timezone.utc)
                        while next_at <= now_utc:
                            next_at += step
                        update("reminders",f"id=eq.{row['id']}",{"trigger_at":next_at.isoformat(),"is_done":False})
                    except Exception as exc:
                        log.warning("failed to reschedule recurring reminder id=%s: %s",row.get("id"),exc)
                else:
                    update("reminders",f"id=eq.{row['id']}",{"is_done":True})

# 每天固定的主动关怀时刻（北京时间，分钟数）
PROACTIVE_SLOTS = (
    (480, "早八催起床"),
    (510, "早八补一刀"),
    (700, "午饭"),
    (715, "临时测试"),
    (740, "午饭回访"),
    (765, "午休"),
    (1050, "晚饭"),
    (1110, "饭后"),
    (1425, "催睡觉"),
)
# 错过准点后还能补发的窗口（分钟）
PROACTIVE_GRACE = 10

# 额外时刻，用环境变量 PROACTIVE_EXTRA_SLOTS="11:55,20:10" 临时加，拨掉就没了
def _extra_slots():
    raw = os.getenv("PROACTIVE_EXTRA_SLOTS", "").strip()
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        try:
            h, m = part.split(":", 1)
            out.append((int(h) * 60 + int(m), f"临时 {part}"))
        except Exception:
            continue
    return tuple(out)


def _all_slots():
    return PROACTIVE_SLOTS + _extra_slots()


async def _qq_online() -> bool:
    """直接向 QQ 探活，而不是猜模块变量。"""
    try:
        import qq_bot
        if qq_bot._conn is None:
            return False
        probe = await qq_bot.send_action_wait("get_login_info", {}, timeout=4)
        return bool(probe and probe.get("status") == "ok")
    except Exception as exc:
        log.warning("qq probe failed: %s", exc)
        return False


async def qq_proactive_loop():
    """QQ 主动查岗与定时关怀后台任务"""
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    owner_id = os.getenv("QQ_OWNER_ID", "").strip()
    if not owner_id:
        log.info("QQ_OWNER_ID not set; proactive loop idle")
        while True:
            await asyncio.sleep(3600)

    sent_keys = set()

    while True:
        await asyncio.sleep(20)
        now = datetime.now(tz)
        now_min = now.hour * 60 + now.minute
        day_str = now.strftime("%Y-%m-%d")

        slot = None
        for minutes, label in _all_slots():
            if 0 <= now_min - minutes <= PROACTIVE_GRACE:
                slot = (minutes, label)
                break
        if slot is None:
            continue

        key = f"{day_str}_{slot[0]}"
        if key in sent_keys:
            continue

        try:
            if not await _qq_online():
                log.warning("proactive skipped at %s: qq offline", now.strftime("%H:%M"))
                continue

            from context import build_context
            from conversation_store import save_message
            from qq_bot import send_qq_msg

            hist = recent("message", 20)
            prompt = (
                build_context()
                + f"\n\n【当前现实时间】北京时间 {now.strftime('%m月%d日 %H:%M')}\n"
                + f"【任务指示】{slot[1]}：你主动在 QQ 上给凌冥发消息。"
                + "结合你了解的生活细节，像真人随手敲手机一样发 1 到 2 句短促口语。"
                + "严禁做作深情，严禁说教。"
            )
            messages = [{"role": "system", "content": prompt}]
            if hist:
                messages.extend(hist[-6:])
            messages.append({"role": "user", "content": "(系统定时触发：主动给他发一条消息)"})

            text, _ = await call_llm(messages, max_tokens=150, channel="qq")
            msg = (text or "").strip()
            if not msg:
                log.warning("proactive empty reply at %s", now.strftime("%H:%M"))
                continue

            from qq_workers import _split_into_bubbles, _segments
            chunks = _split_into_bubbles(msg)
            ok_count = 0
            for i, chunk in enumerate(chunks):
                if not chunk:
                    continue
                save_message("assistant", chunk, scene="message", source="qq-proactive")
                if await send_qq_msg("private", int(owner_id), _segments(chunk)):
                    ok_count += 1
                if i < len(chunks) - 1:
                    await asyncio.sleep(random.uniform(1.0, 2.0))

            if ok_count:
                sent_keys.add(key)
                if len(sent_keys) > 80:
                    sent_keys = set(sorted(sent_keys)[-40:])
                log.info("proactive sent (%s): %s", slot[1], msg[:60])
            else:
                log.warning("proactive send failed at %s", now.strftime("%H:%M"))
        except Exception as exc:
            log.warning("QQ proactive push failed: %s", exc)

async def summary_loop():
    tz=ZoneInfo(DEFAULT_TIMEZONE)
    hour=max(0,min(int(os.getenv("NIGHTLY_SUMMARY_HOUR","4")),23))
    while True:
        await asyncio.sleep(60)
        now=datetime.now(tz)
        day_key=now.strftime("%Y-%m-%d")
        if now.hour!=hour:
            continue
        if get_setting("last_nightly_summary_day","")==day_key:
            continue
        try:
            await asyncio.to_thread(run_nightly_summary)
            upsert_setting("last_nightly_summary_day",day_key)
        except Exception as exc:
            log.warning("summary loop failed: %s",exc)

async def platform_compress_loop():
    while True:
        await asyncio.sleep(90)
        try:
            rows=get("chat_context","select=id&limit=101")
            if len(rows)>=100: await asyncio.to_thread(run_platform_batch_compress)
        except Exception as exc:log.warning("platform compress failed: %s",exc)

async def platform_maintenance_loop():
    while True:
        await asyncio.sleep(6*3600)
        try:
            await asyncio.to_thread(run_platform_summary_maintenance)
        except Exception as exc:log.warning("platform maintenance failed: %s",exc)

REGISTERED_TASKS=[reminder_checker,qq_proactive_loop,summary_loop,platform_compress_loop,platform_maintenance_loop]
