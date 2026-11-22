"""候选记忆 & 自动沉淀：话题结束时抽「柜中罐子」原子事实。

升级后：
- 借鉴小手机「柜中罐子 · Tokens」机制，按五个罐子分类（关于你 / 我们之间 / 身边的人 / TA自己 / 往事）。
- 高置信度（>= 0.92）的明确事实直接自动沉淀进 `long_term` 永久生效，无需每次手动审批；不太确定的才放 `candidate` 待确认池。
"""
from __future__ import annotations
import json, re, os, time, logging
from storage import get, insert, update, delete_ids

log = logging.getLogger(__name__)

TOPIC_GAP = float(os.getenv("TOPIC_GAP_MINUTES", "25")) * 60
MAX_FACTS = int(os.getenv("MAX_FACTS_PER_TOPIC", "5"))
MIN_CONF = float(os.getenv("MEMORY_MIN_CONFIDENCE", "0.85"))
AUTO_SETTLE_CONF = float(os.getenv("MEMORY_AUTO_SETTLE_CONFIDENCE", "0.92"))
LAYER = "candidate"

_EXTRACT = (
    "你是薄销的记忆装罐员。下面是一段刚聊完的对话，请从中提炼值得放进「柜中罐子」的原子事实细节。\n\n"
    "五个罐子分类（jar 字段必须五选一）：\n"
    "1. `关于你`：凌冥的喜好、厌恶、身体细节（如胃病/半月板/PCOS/腱鞘炎）、买过的东西/型号、课表、考试计划、生活习惯、小怪癖\n"
    "2. `我们之间`：薄销与凌冥之间的承诺、约定、固定梗、亲密互动习惯、共同做过的事\n"
    "3. `身边的人`：凌冥的室友（芽芽、离梦等）、家人、同学、中介相关的具体事实\n"
    "4. `TA自己`：薄销自己的日常细节、喜好（如鼓点、伊索、墨绿色）、工作吐槽\n"
    "5. `往事`：凌冥提起的过去经历、老家往事、高中回忆\n\n"
    "要求：\n"
    "- 写成精炼的短句（10~35字，像小手机的便利贴标签，如「凌冥买了18promax和闪魔透明壳」「薄销承诺凌冥手机壳发黄了给他买新的」）。\n"
    "- 必须有原文依据，禁止猜测。最多提取 {max_facts} 条。\n\n"
    "只返回 JSON，不要解释：\n"
    '{{"facts":[{{"content":"...","evidence":"原文片段","jar":"关于你|我们之间|身边的人|TA自己|往事","importance":4,"confidence":0.95}}]}}\n\n'
    "【已有记忆，不要重复】\n{existing}\n\n【对话】\n{dialog}"
)


def add_candidate(content: str, evidence: str = "", category: str = "", importance: int = 3, confidence: float = 1.0, auto_settle: bool = False):
    text = (content or "").strip()
    if not text:
        return None
    target_layer = "long_term" if (auto_settle or confidence >= AUTO_SETTLE_CONF) else LAYER
    if target_layer == LAYER and evidence:
        text = f"{text}（原话：「{evidence}」）"
    cat = category.strip() if category else "罐子:关于你"
    if not cat.startswith("罐子:"):
        cat = f"罐子:{cat}"
    body = {
        "content": text,
        "memory_layer": target_layer,
        "importance": max(1, min(int(importance or 4), 5)),
        "category": cat,
    }
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
    return a[:10] in b or b[:10] in a


async def extract_topic(rows: list) -> int:
    if not rows:
        return 0
    dialog = "\n".join(
        f"{'凌冥' if r.get('role') == 'user' else '薄销'}: {(r.get('content') or '').strip()}"
        for r in rows if (r.get("content") or "").strip()
    )
    if len(dialog) < 50:
        return 0
    existing = get("memories", "memory_layer=in.(long_term,core,current)&order=id.desc&limit=50&select=content")
    existing_text = "\n".join(f"- {r.get('content','')}" for r in existing) or "(无)"
    prompt = _EXTRACT.format(max_facts=MAX_FACTS, existing=existing_text, dialog=dialog[:6000])

    try:
        from llm_runtime import call_llm
        raw, _ = await call_llm(
            [{"role": "system", "content": "Return valid JSON only."},
             {"role": "user", "content": prompt}],
            max_tokens=1000, channel="background",
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
        jar = (item.get("jar") or item.get("category") or "关于你").strip()
        if not content:
            continue
        try:
            conf = float(item.get("confidence", 0.9))
        except Exception:
            conf = 0.9
        if conf < MIN_CONF:
            continue
        if any(_similar(content, k) for k in known):
            continue
        if add_candidate(content, evidence, jar, item.get("importance", 4), conf):
            written += 1
            known.append(content)
    if written:
        log.info("topic jar memories written: %s", written)
    return written
