"""Generic QQ message worker. Keeps owner restrictions, group PASS and real reply/@ tags."""
from __future__ import annotations
import os,asyncio,json,random,re,time
from context import build_context
from conversation_store import save_message,recent,recent_private,recent_timed
from llm_runtime import call_llm,_is_upstream_safety_error
from free_tools import TOOL_SCHEMAS,TOOL_DISPATCH

BOT_ID=os.getenv("QQ_BOT_ID","");BOT_NAME=os.getenv("QQ_BOT_NAME","薄销");OWNER_ID=os.getenv("QQ_OWNER_ID","");OWNER_ALIAS=os.getenv("OWNER_ALIAS","凌冥")
GROUP_IDS={x.strip() for x in os.getenv("QQ_GROUP_IDS","").split(",") if x.strip()}
_private_pending=None;_group_pending={}
_REPLY_RE=re.compile(r'\[(?:REPLY|引用|回复)[:：]?\s*(-?\d+)?\]',re.I)
_AT_RE=re.compile(r'\[(?:AT|艾特)[:：]\s*(\d+)\]',re.I)
_CQ_RE=re.compile(r'\[CQ:[^\]]+\]')
_STREAM_END=re.compile(r'[。！？!?…~\n]')
# 历史里贴的时间标记，模型会照着念，出口必须剥掉
_TIME_TAG_RE=re.compile(r'^\s*[（(]\s*(?:刚刚|今天|昨天|前天|\d+\s*分钟前|\d+\s*小时前|\d+\s*月\d+\s*日|\d{4}年)[^）)]*[）)]\s*')

MIN_BUBBLES = 1
MAX_BUBBLES = 5
QQ_MAX_TOKENS = int(os.getenv("QQ_MAX_TOKENS", "700"))
HISTORY_LIMIT = int(os.getenv("QQ_HISTORY_LIMIT", "20"))
CONTEXT_TTL = float(os.getenv("QQ_CONTEXT_TTL", "45"))

# 只在聊到这些事的时候才把工具清单塞进请求，省下的 prefill 就是省下的时间
_TOOL_HINTS = ("天气","温度","下雨","气温","提醒","闹钟","几点","日期","星期几","记一下","待办","日程","打卡","背单词","喝水","明天几")

_ctx_cache={"value":"","at":0.0}

def _cached_context():
    now=time.time()
    if _ctx_cache["value"] and now-_ctx_cache["at"]<CONTEXT_TTL:
        return _ctx_cache["value"]
    try:
        v=build_context()
    except Exception:
        v=_ctx_cache["value"]
    if v:
        _ctx_cache["value"]=v;_ctx_cache["at"]=now
    return v

def _clean_cq(s):return _CQ_RE.sub('',s or '').strip()

def _strip_time_tags(s: str) -> str:
    s = s or ""
    while True:
        n = _TIME_TAG_RE.sub('', s, count=1)
        if n == s:
            return s
        s = n

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
    """严格遵循：
    1. 换行即分气泡（无论单回车还是多回车）
    2. 如果只有一个长段落，按换行/句号/感叹号/问号等标点拆成自然短句
    3. 保留正常的逗号、句号、感叹号、问号等标点符号，严禁通篇用空格代替标点
    4. 每一句话一个独立气泡！
    """
    raw_lines = [l.strip() for l in re.split(r'[\r\n]+', text.strip()) if l.strip()]
    bubbles = []
    for line in raw_lines:
        if len(line) > 24:
            sub_sents = re.split(r'(?<=[。？！?!~…])\s*', line)
            sub_sents = [s.strip() for s in sub_sents if s.strip()]
            if sub_sents:
                bubbles.extend(sub_sents)
            else:
                bubbles.append(line)
        else:
            bubbles.append(line)

    if not bubbles:
        bubbles = [text.strip()]

    if len(bubbles) > max_b:
        kept = bubbles[:max_b - 1]
        tail = "，".join(bubbles[max_b - 1:])
        kept.append(tail)
        bubbles = kept

    return bubbles


class _BubbleStreamer:
    """边生成边发：凑够一句就发一个气泡，剩下的收尾时补。"""

    def __init__(self, send_fn, scene: str, scope: str, target: int, max_b: int = 4, gap: float = 0.45):
        self.send_fn = send_fn
        self.scene = scene
        self.scope = scope
        self.target = target
        self.max_b = max_b
        self.gap = gap
        self.buf = ""
        self.sent = 0
        self.first = True
        self.dead = False

    def _prepare(self, chunk):
        text = _clean_cq(_AT_RE.sub('', _REPLY_RE.sub('', chunk)))
        text = _strip_time_tags(text).strip()
        if not text:
            return None
        seg = _segments(_strip_time_tags(chunk))
        return text, seg

    async def _push(self, chunk):
        got = self._prepare(chunk)
        if not got:
            return False
        text, seg = got
        save_message("assistant", text, scene=self.scene, source="qq")
        await self.send_fn(self.scope, self.target, seg)
        self.sent += 1
        self.first = False
        return True

    async def feed(self, delta: str):
        if self.dead:
            return
        self.buf += delta
        # 开头可能是时间标记，先攒够字符再判断，别把半个括号发出去
        if self.sent == 0 and len(self.buf) < 8 and not _STREAM_END.search(self.buf):
            return
        # 思考标签没闭合前不发
        if "<think" in self.buf and "</think>" not in self.buf:
            return
        if _is_upstream_safety_error(self.buf):
            self.dead = True
            return
        while self.sent < self.max_b:
            m = _STREAM_END.search(self.buf)
            if not m:
                break
            chunk = self.buf[:m.end()]
            self.buf = self.buf[m.end():]
            if len(chunk.strip()) < 2:
                continue
            if await self._push(chunk) and self.sent < self.max_b:
                await asyncio.sleep(self.gap)

    async def flush(self, full_text: str):
        if self.dead or self.sent >= self.max_b:
            return
        rest = self.buf
        self.buf = ""
        if self.sent == 0:
            chunks = _split_into_bubbles(full_text or rest, max_b=self.max_b)
        elif rest.strip():
            chunks = _split_into_bubbles(rest, max_b=max(1, self.max_b - self.sent))
        else:
            chunks = []
        for i, chunk in enumerate(chunks):
            if await self._push(chunk) and i < len(chunks) - 1:
                await asyncio.sleep(max(0.35, min(0.9, len(chunk) * 0.04 + 0.3)))


def _needs_tools(text: str) -> bool:
    return any(h in (text or "") for h in _TOOL_HINTS)


async def _tool_chat(messages,max_rounds=3,on_delta=None,use_tools=True):
    if not use_tools:
        text,_=await call_llm(messages,max_tokens=QQ_MAX_TOKENS,channel="qq",on_delta=on_delta)
        return text
    for _ in range(max_rounds):
        text,calls=await call_llm(messages,max_tokens=QQ_MAX_TOKENS,tools=TOOL_SCHEMAS,channel="qq",on_delta=on_delta)
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
    text,_=await call_llm(messages,max_tokens=QQ_MAX_TOKENS,channel="qq",on_delta=on_delta);return text

PRIVATE_PROMPT_INSTRUCTION = """【时间标记是内部参考，不许说出来】
历史消息前面的「（刚刚）」「（今天 09:15）」「（昨天 22:40）」只是给你判断时间用的标注，不是你该说的话。
你的回复里绝对不能出现这类括号时间标记，一个字都不要抄，直接从正文开始说。
参考时间是用来算间隔的，不是用来念的。

【时间感铁律】
每条历史消息前面括号里是它真实发生的时间。配合系统给你的当前时间算清楚间隔。
别把几小时前、昨天、前好几天的事说成「刚才」「刚吃完」，也别把刚刚发生的事当成老早以前。
超过一周的事只记得个大概就直说记不太清，别硬编细节。

【标点与分气泡铁律（严格执行凌冥要求）】
1. 【必须使用正常标点符号】：严禁用空格代替逗号或句号！句子该用逗号用逗号，该用问号用问号，语气词该加逗号加逗号。
2. 【一句话一个气泡】：想发多句话，每句话必须独占一行（直接敲回车换行），不要把两句长话黏在同一个气泡里！
示例：
想吃就买点呗，大冷天的吃点热乎的暖胃。
不过别加辣汤，挑点清淡的萝卜豆腐垫垫肚。

3. 语气随性自然、接地气，符合三十岁疲惫社友的真实聊天状态，每次回复 1 到 2 句话，分 1 到 2 个气泡发送。"""

async def _private_reply(last_msg_id=None):
    global _private_pending
    try:
        await asyncio.sleep(random.uniform(0.15,0.4));from qq_bot import send_qq_msg
        hist=recent_timed("message",HISTORY_LIMIT)
        if not hist:
            hist=recent("message",HISTORY_LIMIT,with_ids=True)
        sys_prompt = _cached_context() + "\n\n" + PRIVATE_PROMPT_INSTRUCTION
        last_user = ""
        for row in reversed(hist or []):
            if row.get("role") == "user":
                last_user = str(row.get("content") or "")
                break
        streamer=_BubbleStreamer(send_qq_msg,"message","private",int(OWNER_ID or 0),max_b=4)
        reply=(await _tool_chat([{"role":"system","content":sys_prompt}]+hist,
                                on_delta=streamer.feed,
                                use_tools=_needs_tools(last_user))).strip()
        if not reply and streamer.sent==0:return
        if reply and streamer.sent==0:
            # 首气泡带引用回复
            first=_REPLY_RE.search(reply)
            if first and last_msg_id and not first.group(1):
                reply=_REPLY_RE.sub(f"[REPLY:{last_msg_id}]",reply,count=1)
        await streamer.flush(reply)
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
4. 【群聊规范】：
   - 必须使用正常标点符号，严禁通篇用空格代替逗号！
   - 一句话换一行，一句话一个气泡。
   - 保持真人朋友距离，随和松弛。直接输出内容或 PASS。"""

async def _group_reply(gid, last_msg_id=None):
    task=asyncio.current_task()
    try:
        await asyncio.sleep(random.uniform(0.6,1.2));from qq_bot import send_qq_msg
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
                    await asyncio.sleep(random.uniform(0.8, 1.4))
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
