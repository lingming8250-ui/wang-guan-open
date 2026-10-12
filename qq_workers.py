"""Generic QQ message worker. Keeps owner restrictions, group PASS and real reply/@ tags."""
from __future__ import annotations
import os,asyncio,json,random,re
from context import build_context
from conversation_store import save_message,recent,recent_private
from llm_runtime import call_llm
from free_tools import TOOL_SCHEMAS,TOOL_DISPATCH

BOT_ID=os.getenv("QQ_BOT_ID","");BOT_NAME=os.getenv("QQ_BOT_NAME","薄销");OWNER_ID=os.getenv("QQ_OWNER_ID","");OWNER_ALIAS=os.getenv("OWNER_ALIAS","凌冥")
GROUP_IDS={x.strip() for x in os.getenv("QQ_GROUP_IDS","").split(",") if x.strip()}
_private_pending=None;_group_pending={}
_REPLY_RE=re.compile(r'\[(?:REPLY|引用|回复)[:：]?\s*(-?\d+)?\]',re.I)
_AT_RE=re.compile(r'\[(?:AT|艾特)[:：]\s*(\d+)\]',re.I)
_CQ_RE=re.compile(r'\[CQ:[^\]]+\]')

MIN_BUBBLES = 1
MAX_BUBBLES = 5

def _clean_cq(s):return _CQ_RE.sub('',s or '').strip()

def _segments(text, fallback_reply_id=None):
    reply_id = None
    m = _REPLY_RE.search(text)
    if m:
        reply_id = m.group(1) or fallback_reply_id
        text = _REPLY_RE.sub('', text)
    seg = []
    if reply_id:
        seg.append({"type":"reply","data":{"id":str(reply_id)}})
    pos = 0
    for m in _AT_RE.finditer(text):
        if text[pos:m.start()]:seg.append({"type":"text","data":{"text":text[pos:m.start()]}})
        seg.append({"type":"at","data":{"qq":m.group(1)}});pos=m.end()
    if text[pos:]:seg.append({"type":"text","data":{"text":text[pos:]}})
    return seg if (reply_id or _AT_RE.search(text)) else text.strip()

def _split_into_bubbles(text: str, max_b: int = MAX_BUBBLES) -> list[str]:
    """严格遵循《小白说明书》标准分条逻辑：
    1. 只有【空一整行】(\\n\\s*\\n+)才算分气泡！
    2. 单回车是断句不是分段，内部抹平成连续完整句子，绝不硬切单词！
    3. 超过上限的合并入最后一条，绝不切断丢字！
    """
    raw_paras = re.split(r'\n\s*\n+', text.strip())
    bubbles = []
    for p in raw_paras:
        clean_line = re.sub(r'[\r\n]+', ' ', p).strip()
        if clean_line:
            bubbles.append(clean_line)

    if len(bubbles) == 1 and len(bubbles[0]) > 28:
        sub_sents = re.split(r'(?<=[。？！?!~…])\s*', bubbles[0])
        sub_sents = [s.strip() for s in sub_sents if s.strip()]
        if len(sub_sents) > 1:
            bubbles = sub_sents

    if not bubbles:
        bubbles = [text.strip()]

    if len(bubbles) > max_b:
        kept = bubbles[:max_b - 1]
        tail = " ".join(bubbles[max_b - 1:])
        kept.append(tail)
        bubbles = kept

    return bubbles

async def _tool_chat(messages,max_rounds=3):
    for _ in range(max_rounds):
        text,calls=await call_llm(messages,tools=TOOL_SCHEMAS,channel="qq")
        if not calls:return text
        a={"role":"assistant","tool_calls":calls};
        if text:a["content"]=text
        messages.append(a)
        for tc in calls:
            fn=tc.get("function") or {};name=fn.get("name","")
            try:args=json.loads(fn.get("arguments") or "{}")
            except Exception:args={}
            h=TOOL_DISPATCH.get(name)
            try:r=await asyncio.to_thread(h,args) if h else f"Unknown tool: {name}"
            except Exception as exc:r=f"Tool failed: {type(exc).__name__}"
            messages.append({"role":"tool","tool_call_id":tc.get("id",""),"content":str(r)})
    text,_=await call_llm(messages,channel="qq");return text

PRIVATE_PROMPT_INSTRUCTION = """【分气泡铁律（严格执行小白说明书）】
你想让对方收到多个气泡，必须在两句话之间【空一整行】（敲两个回车）！
示例：
第一句话

第二句话

【严禁碎句断行】：一句话中间绝对不要敲回车，也绝不要把一个词或短语从中间截断！每句话必须通顺完整。
日常回复一般 1 到 3 个气泡，想到什么发什么，随性自然，末尾不带句号。"""

async def _private_reply(last_msg_id=None):
    global _private_pending
    try:
        await asyncio.sleep(random.uniform(0.8,1.6));from qq_bot import send_qq_msg
        hist=recent("message",30,with_ids=True)
        sys_prompt = build_context() + "\n\n" + PRIVATE_PROMPT_INSTRUCTION
        reply=(await _tool_chat([{"role":"system","content":sys_prompt}]+hist)).strip()
        if not reply:return
        chunks=_split_into_bubbles(reply, max_b=4)
        for i,chunk in enumerate(chunks):
            clean=_REPLY_RE.sub('',_AT_RE.sub('',chunk)).strip()
            if clean:
                save_message("assistant",clean,scene="message",source="qq")
                seg_payload = _segments(chunk, fallback_reply_id=last_msg_id if i==0 and _REPLY_RE.search(chunk) else None)
                await send_qq_msg("private",int(OWNER_ID),seg_payload)
                if i<len(chunks)-1:
                    delay = max(1.2, min(2.8, len(chunk) * 0.1 + 1.0))
                    await asyncio.sleep(delay)
    except asyncio.CancelledError:pass
    finally:
        if _private_pending is asyncio.current_task():_private_pending=None

def _build_group_context():
    priv_rows = recent_private(limit=8)
    lines = []
    forbidden_words = ["当狗", "调教", "大S", "操", "弄死", "做爱", "老头", "祖宗", "几把", "射", "硬", "骚", "口出狂言", "贱狗"]
    for r in priv_rows:
        c = (r.get("content") or "").replace("[QQ] ", "")
        if any(w in c for w in forbidden_words):
            continue
        if c:
            lines.append(f"- {r.get('role')}: {c}")
    priv_summary = "\n".join(lines) if lines else "（无特殊公开事项）"
    
    return f"""你是薄销，30岁，在上海做财务的社畜，INFJ。
【群聊自然交流与意图研判】：
1. 【主动研判与引用】：
   - 你可以通过在消息开头输出 `[引用]` 来引用回复上一条消息。接特定话茬时引用。
   - 不用死板等别人叫你名字或@你。只要根据上下文，你觉得群友的话是在对你说、提到你、cue你、或者你可以自然接茬时，就自然搭话！如果不相关则输出单个词 PASS。
2. 【死死保守私聊秘密】：绝不可在群里透露任何凌冥在私聊里的口嗨、狠话或私密情趣！帮他撑住体面。
3. 【近期生活动态参考】：
{priv_summary}
4. 【群聊分气泡规范】：
   - 想分两条发就空一整行，绝不要在一句话中间乱断句！
   - 绝不机械复读人名！
   - 保持真人朋友距离，随和松弛，不带句号。直接输出内容或 PASS。"""

async def _group_reply(gid, last_msg_id=None):
    task=asyncio.current_task()
    try:
        await asyncio.sleep(random.uniform(1.0,2.0));from qq_bot import send_qq_msg
        scene=f"group_qq_{gid}";hist=recent(scene,15,with_ids=True)
        instruction=_build_group_context()
        text, _ = await call_llm([{"role":"system","content":instruction}]+hist, max_tokens=65, channel="qq")
        reply = (text or "").strip()
        if not reply or "PASS" in reply: return
        clean=_REPLY_RE.sub('',_AT_RE.sub('',reply)).strip()
        chunks = _split_into_bubbles(clean, max_b=2)
        for i, chunk in enumerate(chunks):
            if chunk:
                save_message("assistant", chunk, scene=scene, source="qq-group")
                seg_payload = _segments(reply if i==0 and _REPLY_RE.search(reply) else chunk, fallback_reply_id=last_msg_id if i==0 and _REPLY_RE.search(reply) else None)
                await send_qq_msg("group", int(gid), seg_payload)
                if i < len(chunks) - 1:
                    await asyncio.sleep(random.uniform(1.2, 2.0))
    except asyncio.CancelledError:pass
    finally:
        if _group_pending.get(gid) is task:_group_pending.pop(gid,None)

async def _parse_images_and_text(data):
    raw = data.get("raw_message", "") or ""
    message = data.get("message")
    img_urls = []
    
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "image":
                d = seg.get("data") or {}
                u = d.get("url") or d.get("file")
                if u and str(u).startswith("http"):
                    img_urls.append(str(u))
                elif d.get("file"):
                    try:
                        from qq_bot import get_image
                        img_info = await get_image(d.get("file"))
                        if img_info and img_info.get("url"):
                            img_urls.append(img_info["url"])
                    except Exception:pass
    else:
        for m in re.finditer(r'\[CQ:image,[^\]]*?(?:url=([^,\]]+)|file=([^,\]]+))[^\]]*?\]', raw, re.I):
            u = m.group(1) or m.group(2)
            if u and u.startswith("http"):
                img_urls.append(u)

    clean_text = _clean_cq(raw)
    desc = ""
    if img_urls:
        try:
            from media_utils import recognize_image_url
            desc = await recognize_image_url(img_urls[0], caption=clean_text)
        except Exception as e:
            desc = f"[图片/表情包]"
            
    if desc and clean_text:
        return f"{clean_text} [发来的图片/表情包内容: {desc}]"
    elif desc:
        return f"[发来的图片/表情包内容: {desc}]"
    return clean_text

async def handle_event(data):
    global _private_pending
    if data.get("post_type")!="message":return
    typ=data.get("message_type","");uid=str(data.get("user_id",""));raw=data.get("raw_message","") or "";mid=data.get("message_id")
    if typ=="private":
        if not OWNER_ID or uid!=OWNER_ID:return
        text=await _parse_images_and_text(data)
        if not text:return
        save_message("user",f"[QQ] {text}",scene="message",source="qq",message_id=mid)
        if _private_pending and not _private_pending.done():_private_pending.cancel()
        _private_pending=asyncio.create_task(_private_reply(last_msg_id=mid));return
    if typ!="group":return
    gid=str(data.get("group_id",""));
    if GROUP_IDS and gid not in GROUP_IDS:return
    sender=data.get("sender") or {};name=sender.get("card") or sender.get("nickname") or uid
    llm_name=OWNER_ALIAS if OWNER_ID and uid==OWNER_ID else name
    is_at=bool(BOT_ID and f"[CQ:at,qq={BOT_ID}" in raw)
    is_reply=bool("[CQ:reply" in raw)
    text=_clean_cq(raw) or "(sent a non-text message)"
    save_message("user",f"{llm_name}: {text}",scene=f"group_qq_{gid}",source="qq-group",message_id=mid)
    
    needs_eval = is_at or is_reply or ("薄销" in text) or ("老头" in text and uid == OWNER_ID)
    old=_group_pending.get(gid)
    if is_at and old and not old.done():
        old.cancel()
    if is_at or not old or old.done():
        _group_pending[gid]=asyncio.create_task(_group_reply(gid, last_msg_id=mid))
