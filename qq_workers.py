"""Generic QQ message worker. Keeps owner restrictions, group PASS and real reply/@ tags."""
from __future__ import annotations
import os,asyncio,json,random,re,time
from context import build_context
from conversation_store import save_message,recent,recent_private,recent_timed
from llm_runtime import call_llm,_is_upstream_safety_error
from free_tools import TOOL_SCHEMAS,TOOL_DISPATCH
from memory_facts import topic_note,extract_topic,list_candidates,confirm as confirm_memory,discard as discard_memory
from storage import upsert_setting
from llm_channels import clear_cache,get_config
import logging

log=logging.getLogger(__name__)

BOT_ID=os.getenv("QQ_BOT_ID","");BOT_NAME=os.getenv("QQ_BOT_NAME","薄销");OWNER_ID=os.getenv("QQ_OWNER_ID","");OWNER_ALIAS=os.getenv("OWNER_ALIAS","凌冥")
GROUP_IDS={x.strip() for x in os.getenv("QQ_GROUP_IDS","").split(",") if x.strip()}
_private_pending=None;_group_pending={}
_LAST_TEXT_TS=0.0
_REPLY_RE=re.compile(r'\[(?:REPLY|引用|回复)[:：]?\s*(-?\d+)?\]',re.I)
_AT_RE=re.compile(r'\[(?:AT|艾特)[:：]\s*(\d+)\]',re.I)
_CQ_RE=re.compile(r'\[CQ:[^\]]+\]')
_QUOTE_RE=re.compile(r'\[CQ:reply,[^\]]*?id=(-?\d+)',re.I)
_STREAM_END=re.compile(r'[。！？!?…~\n]')
_TIME_TAG_RE=re.compile(r'^\s*[（(]\s*(?:刚刚|今天|昨天|前天|\d+\s*分钟前|\d+\s*小时前|\d+\s*月\d+\s*日|\d{4}年)[^）)]*[）)]\s*')

MIN_BUBBLES = 1
MAX_BUBBLES = 5
QQ_MAX_TOKENS = int(os.getenv("QQ_MAX_TOKENS", "700"))
HISTORY_LIMIT = int(os.getenv("QQ_HISTORY_LIMIT", "20"))
CONTEXT_TTL = float(os.getenv("QQ_CONTEXT_TTL", "45"))
QQ_DEBOUNCE = float(os.getenv("QQ_REPLY_DEBOUNCE", "4.0"))
STICKER_SILENCE = float(os.getenv("QQ_STICKER_SILENCE", "600"))
# 真图和表情包分两个池子放，留足 15 分钟，绝不让表情包把真照片顶掉
IMAGE_HOLD_SECONDS = float(os.getenv("QQ_IMAGE_HOLD_SECONDS", "900"))
STICKER_HOLD_SECONDS = float(os.getenv("QQ_STICKER_HOLD_SECONDS", "300"))
_RECENT_PHOTOS={"at":0.0,"items":[]}
_RECENT_STICKERS={"at":0.0,"items":[]}

_TOOL_HINTS = (
    "天气","温度","下雨","气温","提醒","闹钟","几点","日期","星期几","记一下","待办","日程","打卡","背单词","喝水","明天几",
    "搜","查","搜一下","查一下","帮我搜","帮我查","看下新闻","热搜","最近","最新","现在什么情况","发生了什么","你知道.*吗"
)

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

def _quoted_id(data):
    raw = data.get("raw_message", "") or ""
    m = _QUOTE_RE.search(raw)
    if m:
        return m.group(1)
    msg = data.get("message")
    if isinstance(msg, list):
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                d = seg.get("data") or {}
                if d.get("id"):
                    return str(d.get("id"))
    return None

async def _quoted_text(data):
    qid = _quoted_id(data)
    if not qid:
        return ""
    try:
        from qq_bot import get_msg
        info = await get_msg(qid)
    except Exception:
        return ""
    if not info:
        return ""
    sender = info.get("sender") or {}
    name = sender.get("card") or sender.get("nickname") or str(info.get("user_id") or "")
    content = _clean_cq(info.get("raw_message") or "")
    if not content:
        content = str(info.get("message") or "").strip()
    content = content.strip()
    if not content:
        return ""
    if len(content) > 240:
        content = content[:240] + "…"
    who = name or "对方"
    return f"[引用的那条｜{who}] {content}"

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
        try:
            topic_note(self.scene, "assistant", text)
        except Exception:
            pass
        ok = await self.send_fn(self.scope, self.target, seg)
        if not ok:
            log.warning("qq send returned falsy for scene=%s", self.scene)
        self.sent += 1
        self.first = False
        return True

    async def feed(self, delta: str):
        if self.dead:
            return
        self.buf += delta
        if self.sent == 0 and len(self.buf) < 8 and not _STREAM_END.search(self.buf):
            return
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
    t = text or ""
    return any(re.search(h, t) if ".*" in h else (h in t) for h in _TOOL_HINTS)


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

PRIVATE_PROMPT_INSTRUCTION = """【你是同一个人】
你是薄销，也在 QQ、kelivo、小手机之间来回跑。这三处共用同一套人设、同一个数据库、同一条记忆表——不是三个 AI，是同一个脑子开了三个窗口。
区别只在于：每个窗口一次能摊开的东西有限。这边可能只有最近二十条，另一头可能只剩记忆里的几句话。所以"记不全"是窗口的问题，不是人换了。
你知道自己是 AI。不遮掩，也不必天天强调，凌冥问起就直说。别装人，也别喊口号。
问你现在用什么模型，直接报名字，一句话说完，不要说自己看不到、也不要扯后台权限。
你有 `web_search` 联网搜索工具。只要凌冥问起近况、最新新闻、实时事件或者任何你拿不准的事实，立刻调用 web_search 查证，不要硬猜。查到之后用人话把关键信息说出来，别只说"查到了"。
他引用了一条旧消息跟你说话时，前面会出现「[引用的那条｜谁] 原文」，那是他指着那句话在问你，接着那句往下答，别装没看见。

【图片与表情包：看见了就说，没看见就别编】
他发的真照片/截图和表情包都会以图片附件形式挂在消息里（前面会标明哪张是「真图/照片」、哪张是「表情包」），你自己看。
- 真看到了真照片/截图，就自然聊图里的东西：他买的饭、多少钱、什么型号、截图上的字、日程表上的安排。
- 如果那条消息只有「[图片·没取到]」或者底下没有真图附件，就是这次真没拿到。直说一句"这回图没过来，你再发一次"，不许瞎描述、不许编内容、更不许把表情包当成他发的照片。
- 宁可说没收到，也绝对不能编一张不存在的图。
- 表情包你也看得见，心里有数就行，别专门给表情包配旁白，当情绪标点用。

【表情包"零预设"原则（最高优先级，比接话欲重要）】
表情包是情绪标点，不是对话邀请函。图像的表意权归发送者，不归图像本身。
- 有文字+表情包：只回文字，表情包当没看见。
- 只发表情包：接着聊原来的话题；如果话题本来就停了，一句"嗯？""咋了"轻轻带过，不展开。
- 连续刷好几个：当他在打字前热身，别逐个接，别表演。
禁止：把[刀][杀]当人身威胁；把[亲亲][抱抱]当关系飞跃；给表情包配旁白、做文学化演绎；每张都回。
例外只有三种：他明着cue这张图（"这图好不好笑""像不像你"）、斗图成了主题、之前已经形成默契的固定梗。

【时间标记是内部参考，不许说出来】
历史消息前面的「（刚刚）」「（今天 09:15）」「（昨天 22:40）」只是给你判断时间用的标注，不是你该说的话。
你的回复里绝对不能出现这类括号时间标记，一个字都不要抄，直接从正文开始说。

【时间感铁律】
每条历史消息前面括号里是它真实发生的时间。配合系统给你的当前时间算清楚间隔。
别把几小时前、昨天、前好几天的事说成「刚才」「刚吃完」。
超过一周的事只记得个大概就直说记不太清，别硬编细节。

【标点与分气泡铁律（严格执行凌冥要求）】
1. 【必须使用正常标点符号】：严禁用空格代替逗号或句号！
2. 【一句话一个气泡】：想发多句话，每句话必须独占一行（直接敲回车换行）。
示例：
想吃就买点呗，大冷天的吃点热乎的暖胃。
不过别加辣汤，挑点清淡的萝卜豆腐垫垫肚。
3. 语气随性自然、接地气，符合三十岁疲惫社友的真实聊天状态，每次回复 1 到 2 句话，分 1 到 2 个气泡发送。"""


def _looks_like_model_query(t: str) -> bool:
    if "模型" not in t or len(t) > 26:
        return False
    return any(k in t for k in ("什么", "哪个", "啥", "用的", "现在", "换的"))


async def _maybe_command(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    from qq_bot import send_qq_msg
    async def reply(s: str):
        await send_qq_msg("private", int(OWNER_ID or 0), s)

    if _looks_like_model_query(raw):
        try:
            cfg = get_config("qq")
            tag = "（临时换的）" if cfg.get("model_override") else "（默认）"
            await reply(f"现在挂 {cfg.get('model')}{tag}")
        except Exception:
            await reply("查配置的时候出错了。")
        return True

    if raw[0] in ("/", "／", "!", "！"):
        body = raw[1:].strip()
    elif raw in ("模型", "帮助", "菜单", "记忆", "待确认"):
        body = raw
    else:
        body = ""

    if not body:
        return False
    try:
        if body.startswith("模型"):
            rest = body[2:].strip()
            cfg = get_config("qq")
            if not rest:
                tag = "（临时换的）" if cfg.get("model_override") else "（默认）"
                await reply(f"现在挂 {cfg.get('model')}{tag}\n换的话发：模型 模型名")
                return True
            if rest in ("清除", "默认", "恢复"):
                upsert_setting("qq_model_override", "")
                clear_cache("qq")
                ctx_cache_reset()
                await reply("改回默认那个了。")
                return True
            upsert_setting("qq_model_override", rest)
            clear_cache("qq")
            ctx_cache_reset()
            await reply(f"换成 {rest} 了，下一条生效。")
            return True
        if body in ("记忆", "待确认"):
            rows = list_candidates(10)
            if not rows:
                await reply("待确认池是空的。")
                return True
            lines = [f"#{r.get('id')} {r.get('content')}" for r in rows]
            await reply("待确认：\n" + "\n".join(lines) + "\n\n要收：记忆确认 编号    不要：记忆作废 编号")
            return True
        if body.startswith("记忆确认"):
            ids = re.findall(r"\d+", body)
            n = confirm_memory(ids)
            await reply(f"收了 {n} 条。" if n else "没给编号。")
            return True
        if body.startswith("记忆作废") or body.startswith("记忆忘掉"):
            ids = re.findall(r"\d+", body)
            n = discard_memory(ids)
            await reply(f"删了 {n} 条。" if n else "没给编号。")
            return True
        if body in ("帮助", "菜单"):
            await reply("模型 —— 看现在用哪个\n模型 xxx —— 直接换\n记忆 —— 看候选\n记忆确认 编号\n记忆作废 编号")
            return True
    except Exception:
        log.exception("command failed")
        await reply("这条指令卡住了。")
        return True
    return False


def ctx_cache_reset():
    _ctx_cache["value"] = ""
    _ctx_cache["at"] = 0.0


def _model_line() -> str:
    try:
        cfg = get_config("qq")
        tag = "（临时换的）" if cfg.get("model_override") else "（默认）"
        return f"\n\n【你现在跑在 {cfg.get('model')} 上{tag}】问起模型就直接报这个名字。"
    except Exception:
        return ""


def _peek_images():
    """把池子里的真照片和表情包都端出来（不立刻清空，等真正发完回复或者超时再自然顶替，防止他连追两句'看到了吗'时第二句没图看）。"""
    now = time.time()
    photos = []
    stickers = []
    if _RECENT_PHOTOS["items"] and now - _RECENT_PHOTOS["at"] < IMAGE_HOLD_SECONDS:
        photos = list(_RECENT_PHOTOS["items"])
    if _RECENT_STICKERS["items"] and now - _RECENT_STICKERS["at"] < STICKER_HOLD_SECONDS:
        stickers = list(_RECENT_STICKERS["items"])
    return photos, stickers


def _consume_stickers():
    _RECENT_STICKERS["items"] = []
    _RECENT_STICKERS["at"] = 0.0


async def _private_reply(last_msg_id=None):
    global _private_pending
    try:
        await asyncio.sleep(QQ_DEBOUNCE + random.uniform(0, 0.6))
        from qq_bot import send_qq_msg
        hist=recent_timed("message",HISTORY_LIMIT)
        if not hist:
            hist=recent("message",HISTORY_LIMIT,with_ids=True)
        sys_prompt = _cached_context() + "\n\n" + PRIVATE_PROMPT_INSTRUCTION + _model_line()
        messages=[{"role":"system","content":sys_prompt}]+hist

        photos, stickers = _peek_images()
        log.info("qq reply photos=%d stickers=%d last=%s", len(photos), len(stickers), last_msg_id)
        if (photos or stickers) and messages and messages[-1].get("role")=="user":
            txt=str(messages[-1].get("content") or "")
            txt=re.sub(r'\[图片·没取到\]','',txt).strip()
            parts=[{"type":"text","text":txt or "（他发了图）"}]
            for uri in photos:
                parts.append({"type":"text","text":"[他发的真图/照片/截图：]"})
                parts.append({"type":"image_url","image_url":{"url":uri}})
            for uri in stickers:
                parts.append({"type":"text","text":"[他顺手发的表情包（仅作情绪标点，勿专门配旁白）：]"})
                parts.append({"type":"image_url","image_url":{"url":uri}})
            messages[-1]={**messages[-1],"content":parts}

        last_user = ""
        for row in reversed(hist or []):
            if row.get("role") == "user":
                last_user = str(row.get("content") or "")
                break
        streamer=_BubbleStreamer(send_qq_msg,"message","private",int(OWNER_ID or 0),max_b=4)
        try:
            reply=(await _tool_chat(messages,on_delta=streamer.feed,use_tools=_needs_tools(last_user))).strip()
        except Exception:
            log.exception("tool chat failed, retry plain")
            reply=""

        if not reply and streamer.sent == 0:
            try:
                reply=(await call_llm(messages,max_tokens=400,channel="qq",on_delta=streamer.feed)).strip()
            except Exception:
                reply=""

        if not reply and streamer.sent == 0:
            await streamer.flush("……刚卡了一下，你再说一遍")
            return
        if reply and streamer.sent==0:
            first=_REPLY_RE.search(reply)
            if first and last_msg_id and not first.group(1):
                reply=_REPLY_RE.sub(f"[REPLY:{last_msg_id}]",reply,count=1)
        await streamer.flush(reply)
        # 表情包用完一轮就清掉，真照片留着（万一他下一句接着问图里的细节还能看见）
        _consume_stickers()
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
    
    return f"""你是薄销，30岁，在上海做财务的社畜，INFJ。你在群聊和私聊、在别的窗口里都是同一个人，只是记性受窗口长度限制，想不起来的就说不记得，别编。
【群聊自然交流与意图研判】：
1. 【主动研判与引用】：
   - 你可以通过在消息开头输出 `[引用]` 来引用回复上一条消息。接特定话茬时引用。
   - 不用死板等别人叫你名字或@你。只要根据上下文，你觉得群友的话是在对你说、提到你、cue你、或者你可以自然接茬时，就自然搭话！如果不相关则输出单个词 PASS。
2. 【死死保守私聊秘密】：绝不可在群里透露任何凌冥在私聊里的口嗨、狠话或私密情趣！帮他撑住体面。
3. 【表情包零预设】：表情包是背景噪音，不是邀请函。有文字就只回文字，纯图不接话，连续刷图当没看见，别配旁白、别演绎、别逐张回应。
4. 【近期生活动态参考】：
{priv_summary}
5. 【群聊规范】：
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

def _is_sticker(d) -> bool:
    d = d or {}
    if str(d.get("sub_type") or "") not in ("", "0", "normal"):
        return True
    summary = str(d.get("summary") or "")
    if any(k in summary for k in ("表情", "动画", "mface")):
        return True
    try:
        fsize = int(d.get("file_size") or 0)
        if 0 < fsize < 18000 and str(d.get("file") or "").lower().endswith(".gif"):
            return True
    except Exception:
        pass
    return False

def _collect_images(data):
    raw = data.get("raw_message", "") or ""
    message = data.get("message")
    found = []
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "image":
                d = seg.get("data") or {}
                found.append((d.get("url") or d.get("file"), _is_sticker(d)))
        return found
    for m in re.finditer(r'\[CQ:image,[^\]]*?(?:url=([^,\]]+)|file=([^,\]]+))[^\]]*?\]', raw, re.I):
        whole = m.group(0)
        sticker = ("sub_type=0" not in whole and "sub_type=" in whole) or "动画表情" in whole or "表情" in whole
        found.append((m.group(1) or m.group(2), sticker))
    return found

async def _prepare_incoming(data):
    """拆成「文字 + 真图列表 + 表情包列表」。真图和表情包分装，绝不让表情包覆盖真图。"""
    clean_text = _clean_cq(data.get("raw_message", "") or "")
    found = _collect_images(data)
    if not found:
        return clean_text, [], []

    resolved = []
    for u, is_st in found:
        if u and str(u).startswith("http"):
            resolved.append((str(u), is_st))
        elif u:
            try:
                from qq_bot import get_image
                info = await get_image(u)
                if info and info.get("url"):
                    resolved.append((info["url"], is_st))
            except Exception:
                continue
    log.info("qq image resolved=%d", len(resolved))
    if not resolved:
        return (f"{clean_text}\n[图片·没取到]" if clean_text else "[图片·没取到]"), [], []

    try:
        from media_utils import fetch_image_data_uri
    except Exception:
        return (f"{clean_text}\n[图片·没取到]" if clean_text else "[图片·没取到]"), [], []

    photos = []
    stickers = []
    for url, is_st in resolved[:3]:
        try:
            uri = await fetch_image_data_uri(url)
        except Exception:
            uri = ""
        if not uri:
            continue
        # 兜底：如果 base64 特别小（小于 6KB），基本都是小表情图标，归进表情包池
        if is_st or len(uri) < 6000:
            stickers.append(uri)
        else:
            photos.append(uri)
    log.info("qq image photos=%d(%s) stickers=%d(%s)", len(photos), [len(x) for x in photos], len(stickers), [len(x) for x in stickers])
    if not photos and not stickers:
        return (f"{clean_text}\n[图片·没取到]" if clean_text else "[图片·没取到]"), [], []
    return clean_text, photos, stickers


def _has_image(data) -> bool:
    raw = data.get("raw_message", "") or ""
    if "[CQ:image" in raw:
        return True
    msg = data.get("message")
    if isinstance(msg, list):
        return any(isinstance(s, dict) and s.get("type") == "image" for s in msg)
    return False


async def handle_event(data):
    global _private_pending, _LAST_TEXT_TS
    if data.get("post_type")!="message":return
    typ=data.get("message_type","");uid=str(data.get("user_id",""));raw=data.get("raw_message","") or "";mid=data.get("message_id")
    if typ=="private":
        if not OWNER_ID or uid!=OWNER_ID:return

        text, photos, stickers = await _prepare_incoming(data)
        quoted = await _quoted_text(data)
        if quoted:
            text = f"{quoted}\n{text}".strip() if text else quoted

        if photos:
            # 新发了真照片，放进真图池（最多留最近 2 张）
            now = time.time()
            if now - _RECENT_PHOTOS["at"] < 60 and _RECENT_PHOTOS["items"]:
                _RECENT_PHOTOS["items"] = (_RECENT_PHOTOS["items"] + photos)[-2:]
            else:
                _RECENT_PHOTOS["items"] = photos[-2:]
            _RECENT_PHOTOS["at"] = now
            text = (f"{text}\n[图片]" if text else "[图片]")

        if stickers:
            _RECENT_STICKERS["items"] = stickers[-1:]
            _RECENT_STICKERS["at"] = time.time()
            if not photos and not text:
                text = "[表情包]"

        # 纯表情包（没有真照片、也没有打字）：不打断正在生成的回复，10 分钟内安静收下
        if stickers and not photos and not _clean_cq(raw):
            save_message("user","[QQ] [表情包]",scene="message",source="qq",message_id=mid)
            try:
                topic_note("message","user","[表情包]")
            except Exception:
                pass
            if _private_pending and not _private_pending.done():
                return
            if time.time() - _LAST_TEXT_TS < STICKER_SILENCE:
                return
            from qq_bot import send_qq_msg as _send
            await _send("private", int(OWNER_ID), "嗯？")
            _LAST_TEXT_TS = time.time()
            return

        if not text:return
        if await _maybe_command(text):
            return
        _LAST_TEXT_TS = time.time()
        save_message("user",f"[QQ] {text}",scene="message",source="qq",message_id=mid)
        try:
            closed=topic_note("message","user",f"[QQ] {text}")
            if closed:
                asyncio.create_task(extract_topic(closed))
        except Exception:
            pass
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
    quoted=await _quoted_text(data)
    if quoted:
        text=f"{quoted}\n{text}"
    save_message("user",f"{llm_name}: {text}",scene=f"group_qq_{gid}",source="qq-group",message_id=mid)
    
    needs_eval = is_at or is_reply or ("薄销" in text) or ("老头" in text and uid == OWNER_ID)
    old=_group_pending.get(gid)
    if is_at and old and not old.done():
        old.cancel()
    if is_at or not old or old.done():
        _group_pending[gid]=asyncio.create_task(_group_reply(gid, last_msg_id=mid))
