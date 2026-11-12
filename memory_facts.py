"""候选记忆：话题结束时抽事实先存 candidate，人确认后才进上下文。

借鉴 anteros 的做法：
- 按消息间隔切话题（默认 30 分钟）
- 话题结束后才抽，每话题最多 3 条
- 每条必须带原文证据，置信度不足不写
- 先落 candidate 层，不进上下文；用 /记忆确认 才升级成 long_term
"""
from __future__ import annotations
import json, re, os, time, logging
from storage import get, insert, update, delete_ids

log = logging.getLogger(__name__)

TOPIC_GAP = float(os.getenv("TOPIC_GAP_MINUTES", "30")) * 60
MAX_FACTS = int(os.getenv("MAX_FACTS_PER_TOPIC", "3"))
MIN_CONF = float(os.getenv("MEMORY_MIN_CONFIDENCE", "0.9"))
LAYER = "candidate"

_EXTRACT=("你是记忆整理员。下面是一段对话，只提取「本人明确说出的、长期有效」的事实。\n\n"
"只收五类：\n"
"1. 长期偏好（喜欢、讨厌、习惯）\n"
"2. 稳定背景（身份、身体、家人、学业、工作）\n"
"3. 正在进行的项目或计划\n"
"4. 反复出现的约定\n"
"5. 明确的长期目标\n\n"
"不收：天气、短期行程、临时状态、玩笑、角色扮演台词、助手自己的话、任何猜测。\n"
"每条必须能从原文逐字找到证据，置信度低于 {min_conf} 的不要。最多 {max_facts} 条。\n"
"content 用第三人称短句（如「凌冥……」），不要照抄整句，不要评论，不要建议。\n\n"
"只返回 JSON，不要解释：\n"
'{{"facts":[{{"content":"...","evidence":"原文逐字片段","category":"preference|background|project|agreement|goal","importance":1,"confidence":0.0}}]}}\n\n'
"【已有记忆，不要重复】\n{existing}\n\n【对话】\n{dialog}")


def add_candidate(content: str, evidence: str = "", category: str = "", importance: int = 3, confidence: float = 1.0):
    text = (content or "").strip()
    if not text:
        return None
    if evidence:
        text = f"{text}（原话：「{evidence}」）"
    body = {
        "content": text,
        "memory_layer": LAYER,
        "importance": max(1, min(int(importance or 3), 5)),
    }
    if category:
        body["category"] = category
    try:
        return insert("memories", body)
    except Exception:
        log.exception("add_candidate failed")
        return None


def list_candidates(limit: int = 10) -> list:
    return get("memories", f"memory_layer=eq.{LAYER}&order=id.desc&limit={max(1,min(limit,30))}&select=id,content,importance,category")


def confirm(ids: list) -> int:
    ids = [str(i) for i in ids if str(i).isdigit()]
    if not ids:
        return 0
    update("memories", f"id=in.({','.join(ids)})", {"memory_layer": "long_term"})
    return len(ids)


def discard(ids: list) -> int:
    ids = [str(i) for i in ids if str(i).isdigit()]
    if not ids:
        return 0
    delete_ids("memories", ids)
    return len(ids)


_topic: dict = {}


def topic_note(scene: str, role: str, content: str, ts: float | None = None) -> list:
    """记一条消息；如果距上一条超过阈值，把上一段话题整体交回去。"""
    now = ts or time.time()
    st = _topic.setdefault(scene, {"ts": now, "rows": []})
    closed = []
    if now - st["ts"] > TOPIC_GAP and len(st["rows"]) >= 2:
        closed = st["rows"]
        st["rows"] = []
    st["ts"] = now
    if content:
        st["rows"].append({"role": role, "content": content})
    if len(st["rows"]) > 60:
        st["rows"] = st["rows"][-60:]
    return closed


def _similar(a: str, b: str) -> bool:
    a = re.sub(r"\s+", "", a or "")
    b = re.sub(r"\s+", "", b or "")
    if not a or not b:
        return False
    return a[:12] in b or b[:12] in a


async def extract_topic(rows: list) -> int:
    if not rows:
        return 0
    dialog = "\n".join(
        f"{'凌冥' if r.get('role') == 'user' else '薄销'}: {(r.get('content') or '').strip()}"
        for r in rows if (r.get("content") or "").strip()
    )
    if len(dialog) < 60:
        return 0
    existing = get("memories", "memory_layer=in.(long_term,core,current)&order=id.desc&limit=30&select=content")
    existing_text = "\n".join(f"- {r.get('content','')}" for r in existing) or "(无)"
    prompt = _EXTRACT.format(min_conf=MIN_CONF, max_facts=MAX_FACTS, existing=existing_text, dialog=dialog[:6000])

    try:
        from llm_runtime import call_llm
        raw, _ = await call_llm(
            [{"role": "system", "content": "Return valid JSON only."},
             {"role": "user", "content": prompt}],
            max_tokens=900, channel="background",
        )
    except Exception:
        log.exception("topic memory extract failed")
        return 0

    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return 0
    try:
        data = json.loads(m.group(0))
    except Exception:
        return 0

    written = 0
    known = [r.get("content", "") for r in existing]
    for item in (data.get("facts") or [])[:MAX_FACTS]:
        content = (item.get("content") or "").strip()
        evidence = (item.get("evidence") or "").strip()
        if not content or not evidence:
            continue
        try:
            conf = float(item.get("confidence", 0))
        except Exception:
            conf = 0.0
        if conf < MIN_CONF:
            continue
        if any(_similar(content, k) for k in known):
            continue
        if add_candidate(content, evidence, (item.get("category") or "").strip(), item.get("importance", 3), conf):
            written += 1
            known.append(content)
    if written:
        log.info("topic memories written: %s", written)
    return written
