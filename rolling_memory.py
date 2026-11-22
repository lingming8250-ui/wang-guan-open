"""滚动记忆 + 柜中罐子（原子事实） + 内心印记（双轨成长） + 远期大事记归档。

核心升级：
1. 滚动摘要容量扩充至 2800 字，保留具体数字、品牌、食物、情绪与生活细节。
2. 每次滚动压缩时，若旧摘要即将因超长被挤出，自动将溢出的早期条目脱水归档进 `long_term_chronicle`（远期大事记，永久不丢）。
3. 搭便车提取：每次滚动时顺手提炼「柜中罐子（5类原子事实）」与「内心印记（关系轨迹 & 自我轨迹）」。
"""
from __future__ import annotations
import os, re, json, time, threading, logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from storage import get, insert, get_setting, upsert_setting
from app_config import DEFAULT_TIMEZONE

log = logging.getLogger(__name__)

_TZ = ZoneInfo(DEFAULT_TIMEZONE)
_LOCK = threading.Lock()
_last = 0.0

INTERVAL = float(os.getenv("ROLLING_INTERVAL", "600"))
MIN_ROWS = int(os.getenv("ROLLING_MIN_ROWS", "25"))
MAX_ROWS = int(os.getenv("ROLLING_MAX_ROWS", "45"))
MAX_CHARS = int(os.getenv("ROLLING_MAX_CHARS", "2800"))
CHRONICLE_MAX_CHARS = int(os.getenv("CHRONICLE_MAX_CHARS", "2000"))

_PROMPT = (
    "【上一版摘要】\n{prev}\n\n【新增对话（每条前面是发生时间）】\n{dialog}\n\n"
    "你将把上面两部分整理并扩写成一份生动详实的近期对话记忆摘要，并顺带提取「柜中罐子事实」和「内心印记」。\n\n"
    "一、【近期详细记忆摘要（rolling_summary）】要求：\n"
    "1. 每一条都必须以日期开头，格式写作〔YYYY-MM-DD〕；同一天的事件合并或紧挨着写。\n"
    "2. 必须保留具体的生活细节：具体吃了什么/什么牌子、花了多少钱、体重/尺码数字、身体哪里不舒服、买了什么型号、当时的情绪起伏、随口立的小flag、没聊完的话头。\n"
    "3. 只丢掉纯表情和无意义寒暄，不要把鲜活的生活琐事当成废话删掉！\n"
    "4. 按时间先后排列，总字数控制在 {max_chars} 字以内。如果旧摘要太长塞不下，把最早那几天的条目原原本本挪进下面的 `overflow_old` 字段里交给远期年表归档，绝对不要凭空删丢！\n\n"
    "二、【柜中罐子 · 原子事实（jar_tokens）】要求：\n"
    "从【新增对话】里提取 0~4 条值得长期记住的「短小原子事实」（像便利贴一样，10~28字一条，例如“凌冥买了白色iPhone 18 Pro Max配闪魔透明壳”、“凌冥捏坏15元脆皮捏捏导致手部酸麻”）。\n"
    "jar 只能从这五个罐子里选一个：`关于你`、`我们之间`、`身边的人`、`TA自己`、`往事`。\n\n"
    "三、【内心印记 · 双轨心迹（inner_traces）】要求：\n"
    "从薄销的视角，提炼 0~1 条经过这段对话后产生的心态/偏爱变化：\n"
    "- `relation`（关系轨迹：薄销对凌冥的行为差异、心软与偏袒，例如“平时算账极讲规矩，但看凌冥为生活费发愁受委屈，只想暗中给他兜底顺毛”）\n"
    "- `self`（自我轨迹：薄销自己作为30岁上海社畜的状态与情绪，例如“下班后被报表折磨得疲惫不堪，只有在听凌冥絮叨日常时才卸下防备”）\n\n"
    "请严格只输出如下 JSON 格式（不要 markdown 代码块以外的多余解释）：\n"
    "{{\n"
    '  "rolling_summary": "〔2026-...〕...",\n'
    '  "overflow_old": "被挤出近期窗口的更早日期条目（没有则填空字符串）",\n'
    '  "jar_tokens": [{{"jar": "关于你|我们之间|身边的人|TA自己|往事", "content": "...", "importance": 4}}],\n'
    '  "inner_trace": {{"relation": "...", "self": "..."}}\n'
    "}}"
)

_CHRONICLE_PROMPT = (
    "【已有远期往事大事记】\n{old_chronicle}\n\n"
    "【新退下来的早期日记条目】\n{overflow}\n\n"
    "将新退下来的早期条目浓缩并入【远期往事大事记】中。要求：\n"
    "1. 按月份或日期节点写作，如〔2026-10〕或〔2026-11上旬〕。\n"
    "2. 保留关键事件、重要约定、情感转折、买过的大件、重要的生活节点，字数控制在 {max_chars} 字以内。\n"
    "3. 只输出合并后的正文，不要任何解释。"
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


def _llm(system: str, user: str, max_tokens: int = 2800) -> str:
    import asyncio
    from llm_runtime import call_llm
    text, _ = asyncio.run(call_llm(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens, channel="background"))
    return (text or "").strip()


def _similar(a: str, b: str) -> bool:
    a = re.sub(r"\s+", "", a or "")
    b = re.sub(r"\s+", "", b or "")
    if not a or not b:
        return False
    return a[:10] in b or b[:10] in a


def _save_jar_tokens(tokens: list) -> int:
    if not isinstance(tokens, list) or not tokens:
        return 0
    existing = get("memories", "memory_layer=in.(core,long_term,current)&order=id.desc&limit=80&select=content")
    known = [r.get("content", "") for r in existing]
    saved = 0
    for item in tokens[:5]:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        jar = (item.get("jar") or "关于你").strip()
        if not content or len(content) < 4:
            continue
        if any(_similar(content, k) for k in known):
            continue
        try:
            imp = max(2, min(int(item.get("importance", 4)), 5))
        except Exception:
            imp = 4
        try:
            insert("memories", {
                "content": content,
                "memory_layer": "long_term",
                "category": f"罐子:{jar}",
                "importance": imp,
            })
            known.append(content)
            saved += 1
        except Exception:
            log.exception("save jar token failed")
    return saved


def _save_inner_trace(trace: dict) -> None:
    if not isinstance(trace, dict):
        return
    rel = (trace.get("relation") or "").strip()
    slf = (trace.get("self") or "").strip()
    if not rel and not slf:
        return
    today = datetime.now(_TZ).strftime("%Y-%m-%d")
    raw = get_setting("inner_traces_json", "[]")
    try:
        arr = json.loads(raw) if isinstance(raw, str) else (raw or [])
        if not isinstance(arr, list):
            arr = []
    except Exception:
        arr = []
    entry = {"date": today}
    if rel:
        entry["relation"] = rel
    if slf:
        entry["self"] = slf
    # 同一天已有就更新最新感悟，否则追加，保留最近 12 条心迹演进
    if arr and isinstance(arr[-1], dict) and arr[-1].get("date") == today:
        arr[-1].update(entry)
    else:
        arr.append(entry)
    arr = arr[-12:]
    try:
        upsert_setting("inner_traces_json", json.dumps(arr, ensure_ascii=False))
    except Exception:
        log.exception("save inner trace failed")


def _archive_overflow(overflow_text: str) -> None:
    overflow_text = (overflow_text or "").strip()
    if len(overflow_text) < 20:
        return
    old_chronicle = (get_setting("long_term_chronicle", "") or "").strip()
    merged = _llm(
        "你是往事编年史整理员。只输出合并后的编年史正文。",
        _CHRONICLE_PROMPT.format(
            old_chronicle=old_chronicle or "（尚无早期记录）",
            overflow=overflow_text,
            max_chars=CHRONICLE_MAX_CHARS,
        ),
        max_tokens=1800,
    )
    if merged:
        try:
            upsert_setting("long_term_chronicle", merged[:CHRONICLE_MAX_CHARS])
            log.info("chronicle updated, len=%d", len(merged))
        except Exception:
            log.exception("save chronicle failed")


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
        lines.append(f"[{when}] {who}: {(r.get('content') or '')[:500]}")
    dialog = "\n".join(lines)

    old = get("platform_rolling_summary", "order=id.desc&limit=1&select=content")
    prev = (old[0].get("content") if old else "") or ""

    # 若旧摘要已经很长，手工切出最早的 1/4 作为保底归档候选
    auto_overflow = ""
    if len(prev) > int(MAX_CHARS * 0.9):
        prev_lines = [l for l in prev.splitlines() if l.strip()]
        if len(prev_lines) > 6:
            cut = max(2, len(prev_lines) // 4)
            auto_overflow = "\n".join(prev_lines[:cut])
            prev = "\n".join(prev_lines[cut:])

    raw = _llm(
        "你在整理多维长期记忆。请严格输出 JSON。",
        _PROMPT.format(prev=prev or "（无）", dialog=dialog, max_chars=MAX_CHARS),
        max_tokens=3200,
    )
    if not raw:
        return 0

    summary = ""
    overflow = auto_overflow
    jar_tokens = []
    inner_trace = {}

    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            summary = (data.get("rolling_summary") or "").strip()
            ov = (data.get("overflow_old") or "").strip()
            if ov:
                overflow = (overflow + "\n" + ov).strip() if overflow else ov
            jar_tokens = data.get("jar_tokens") or []
            inner_trace = data.get("inner_trace") or {}
        except Exception:
            summary = ""

    # 如果模型没按 JSON 回，兜底直接当纯文本摘要收下
    if not summary:
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        if cleaned and not cleaned.startswith("{"):
            summary = cleaned

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

    try:
        _save_jar_tokens(jar_tokens)
    except Exception:
        log.exception("jar tokens hook failed")

    try:
        _save_inner_trace(inner_trace)
    except Exception:
        log.exception("inner trace hook failed")

    if overflow:
        try:
            _archive_overflow(overflow)
        except Exception:
            log.exception("archive overflow hook failed")

    log.info("rolling summary updated: %s rows, jars=%s", len(rows), len(jar_tokens))
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
