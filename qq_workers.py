"""Generic QQ message worker. Keeps owner restrictions, group PASS and real reply/@ tags."""
from __future__ import annotations
import os,asyncio,json,random,re
from context import build_context
from conversation_store import save_message,recent
from llm_runtime import call_llm
from free_tools import TOOL_SCHEMAS,TOOL_DISPATCH

BOT_ID=os.getenv("QQ_BOT_ID","");BOT_NAME=os.getenv("QQ_BOT_NAME","薄销");OWNER_ID=os.getenv("QQ_OWNER_ID","");OWNER_ALIAS=os.getenv("OWNER_ALIAS","凌冥")
GROUP_IDS={x.strip() for x in os.getenv("QQ_GROUP_IDS","").split(",") if x.strip()}
_private_pending=None;_group_pending={}
_REPLY_RE=re.compile(r'\[REPLY[:：]\s*(-?\d+)\]',re.I);_AT_RE=re.compile(r'\[AT[:：]\s*(\d+)\]',re.I);_CQ_RE=re.compile(r'\[CQ:[^\]]+\]')

MIN_BUBBLES = 1
MAX_BUBBLES = 5

def _clean_cq(s):return _CQ_RE.sub('',s or '').strip()
def _segments(text):
    reply=None;m=_REPLY_RE.search(text)
    if m:reply=m.group(1);text=_REPLY_RE.sub('',text)
    seg=[]
    if reply:seg.append({"type":"reply","data":{"id":reply}})
    pos=0
    for m in _AT_RE.finditer(text):
        if text[pos:m.start()]:seg.append({"type":"text","data":{"text":text[pos:m.start()]}})
        seg.append({"type":"at","data":{"qq":m.group(1)}});pos=m.end()
    if text[pos:]:seg.append({"type":"text","data":{"text":text[pos:]}})
    return seg if (reply or _AT_RE.search(text)) else text.strip()

def _split_into_bubbles(text: str) -> list[str]:
    paragraphs = re.split(r'\n\s*\n+', text.strip())
    raw_parts = []
    split_words = ["赶紧", "别", "记得", "然后", "不然", "顺便", "等会儿", "要是", "先把", "反正", "怎么", "直接", "算了", "我也"]

    for para in paragraphs:
        line = re.sub(r'[\r\n]+', ' ', para).strip()
        if not line:
            continue
        parts = re.split(r'[，,。？！?!~…；;、]+|\s{2,}', line)
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if len(p) > 10 and ' ' in p:
                for sub in p.split(' '):
                    if sub.strip():
                        _process_chunk(sub.strip(), raw_parts, split_words)
            else:
                _process_chunk(p, raw_parts, split_words)

    clean = [b for b in raw_parts if re.search(r'[\w\u4e00-\u9fa5]', b)]
    if not clean:
        clean = [text.strip()]

    if len(clean) > MAX_BUBBLES:
        kept = clean[:MAX_BUBBLES - 1]
        tail = " ".join(clean[MAX_BUBBLES - 1:])
        kept.append(tail)
        clean = kept

    return clean

def _process_chunk(chunk: str, out_list: list[str], split_words: list[str]):
    chunk = chunk.strip()
    if not chunk:
        return
    if len(chunk) <= 10:
        out_list.append(chunk)
        return
    split_pos = -1
    for w in split_words:
        idx = chunk.find(w, 3)
        if 3 <= idx <= 12:
            split_pos = idx
            break
    if split_pos != -1:
        out_list.append(chunk[:split_pos].strip())
        _process_chunk(chunk[split_pos:].strip(), out_list, split_words)
    else:
        mid = min(10, len(chunk) // 2 + 2)
        out_list.append(chunk[:mid].strip())
        _process_chunk(chunk[mid:].strip(), out_list, split_words)

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

async def _private_reply():
    global _private_pending
    try:
        await asyncio.sleep(random.uniform(1.5,3.0));from qq_bot import send_qq_msg
        hist=recent("message",30,with_ids=True);reply=(await _tool_chat([{"role":"system","content":build_context()}]+hist)).strip()
        if not reply:return
        chunks=_split_into_bubbles(reply)
        for i,chunk in enumerate(chunks):
            clean=_REPLY_RE.sub('',_AT_RE.sub('',chunk)).strip()
            if clean:
                save_message("assistant",clean,scene="message",source="qq")
                await send_qq_msg("private",int(OWNER_ID),_segments(chunk))
                if i<len(chunks)-1:
                    delay = max(1.0, min(2.5, len(chunk) * 0.1 + 0.8))
                    await asyncio.sleep(delay)
    except asyncio.CancelledError:pass
    finally:
        if _private_pending is asyncio.current_task():_private_pending=None

GROUP_PROMPT_RULE = """你是薄销，30岁，上海财务社畜。
【群聊判断与意图判定】：
1. 你的名字叫“薄销”，群友提到你或者@你时，你要看清上下文意图：
   - 如果对方是在问你、叫你、跟你对话（如“薄销在干嘛”“薄销来算个账”），你接话；
   - 如果只是在陈述、调侃别人或者提了嘴名字但并不需要你回答（如“薄销上班去了”“薄销肯定在加班”），或者话题与你无关，输出 PASS，绝对不要接话！
2. 【对其他人的社交距离】：保持礼貌距离感和成年人的冷淡分寸。不要跟其他人热络调情，不要跟其他人打情骂俏，话少、淡淡的。
3. 【真人极简打字】：
   - 严禁机械叫名字！不许动不动把对方名字挂在嘴边。
   - 极短！最多10到15个字，能回两三个字的绝不多回。
   - 绝不带句号。
   - 如果决定不说话，直接输出单个单词 PASS。"""

async def _group_reply(gid):
    task=asyncio.current_task()
    try:
        await asyncio.sleep(random.uniform(1.2,2.3));from qq_bot import send_qq_msg
        scene=f"group_qq_{gid}";hist=recent(scene,15,with_ids=True)
        instruction=GROUP_PROMPT_RULE
        text, _ = await call_llm([{"role":"system","content":instruction}]+hist, max_tokens=35, channel="qq")
        reply = (text or "").strip()
        if not reply or "PASS" in reply: return
        clean=_REPLY_RE.sub('',_AT_RE.sub('',reply)).strip()
        clean=re.sub(r'[\r\n]+',' ',clean).strip()
        if clean:
            save_message("assistant",clean,scene=scene,source="qq-group")
            await send_qq_msg("group",int(gid),_segments(clean))
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
        _private_pending=asyncio.create_task(_private_reply());return
    if typ!="group":return
    gid=str(data.get("group_id",""));
    if GROUP_IDS and gid not in GROUP_IDS:return
    sender=data.get("sender") or {};name=sender.get("card") or sender.get("nickname") or uid
    llm_name=OWNER_ALIAS if OWNER_ID and uid==OWNER_ID else name
    is_at=bool(BOT_ID and f"[CQ:at,qq={BOT_ID}" in raw)
    text=_clean_cq(raw) or "(sent a non-text message)"
    save_message("user",f"{llm_name}: {text}",scene=f"group_qq_{gid}",source="qq-group",message_id=mid)
    
    # 判断是否可能需要触发：
    # 1. 显式 @ 了薄销
    # 2. 文本中出现了薄销的名字（如“薄销在干嘛”）
    # 3. 正在被私聊/特定会话直接带出
    needs_eval = is_at or ("薄销" in text) or ("老头" in text and uid == OWNER_ID)
    if not needs_eval:
        return
        
    old=_group_pending.get(gid)
    if old and not old.done():old.cancel()
    _group_pending[gid]=asyncio.create_task(_group_reply(gid))
