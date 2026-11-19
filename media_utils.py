"""Generic media helpers for vision, STT and TTS.

识图这条路的三条硬规矩：
1. 先压再送。原图几十兆直接塞过去，慢和超时都是这么来的。
2. 掐时间。超过 VISION_TIMEOUT 就当没看见，绝不能把整条回复拖死。
3. 认过的图记住。同一张表情包只花一次钱。
"""
from __future__ import annotations
import os,io,json,base64,tempfile,hashlib,threading,requests,asyncio,re
from llm_channels import get_config,record_result

VISION_MAX_TOKENS=int(os.getenv("VISION_MAX_TOKENS","350"))
STICKER_MAX_TOKENS=int(os.getenv("STICKER_MAX_TOKENS","70"))
# 识图只是配菜，绝不能把整条回复拖死：掐在 20 秒内，超了就当没看见
VISION_TIMEOUT=float(os.getenv("VISION_TIMEOUT","20"))
# 压缩：长边超过这个就缩，质量按下面给
VISION_MAX_EDGE=int(os.getenv("VISION_MAX_EDGE","1024"))
VISION_JPEG_QUALITY=int(os.getenv("VISION_JPEG_QUALITY","85"))
VISION_CACHE_PATH=os.getenv("VISION_CACHE_PATH","/app/data/vision_cache.json")
VISION_CACHE_MAX=int(os.getenv("VISION_CACHE_MAX","800"))

_cache_lock=threading.Lock()
_cache_loaded=False
_cache={}

def _load_cache():
    global _cache,_cache_loaded
    if _cache_loaded:
        return
    _cache_loaded=True
    try:
        with open(VISION_CACHE_PATH,"r",encoding="utf-8") as f:
            data=json.load(f)
        if isinstance(data,dict):
            _cache=data
    except Exception:
        _cache={}

def _save_cache():
    try:
        os.makedirs(os.path.dirname(VISION_CACHE_PATH) or ".",exist_ok=True)
        while len(_cache)>VISION_CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        tmp=VISION_CACHE_PATH+".tmp"
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump(_cache,f,ensure_ascii=False)
        os.replace(tmp,VISION_CACHE_PATH)
    except Exception:
        pass

def _cache_get(key):
    with _cache_lock:
        _load_cache()
        return _cache.get(key)

def _cache_put(key,value):
    with _cache_lock:
        _load_cache()
        _cache[key]=value
        _save_cache()

def _shrink(data: bytes) -> bytes:
    """长边缩到 VISION_MAX_EDGE，转 JPEG。没有 Pillow 就原样返回。"""
    try:
        from PIL import Image
    except Exception:
        return data
    try:
        im=Image.open(io.BytesIO(data))
        if im.mode not in ("RGB","L"):
            im=im.convert("RGB")
        w,h=im.size
        edge=max(w,h)
        if edge>VISION_MAX_EDGE:
            ratio=VISION_MAX_EDGE/float(edge)
            im=im.resize((max(1,int(w*ratio)),max(1,int(h*ratio))),Image.LANCZOS)
        buf=io.BytesIO()
        im.save(buf,format="JPEG",quality=VISION_JPEG_QUALITY,optimize=True)
        out=buf.getvalue()
        return out if out else data
    except Exception:
        return data

async def _vision(payload,label="vision",max_tokens=None):
    import httpx
    payload={**payload,"max_tokens":max_tokens or VISION_MAX_TOKENS}
    cfg=await asyncio.to_thread(get_config,"vision")
    extra={str(k):str(v) for k,v in (cfg.get("extra_headers") or {}).items() if str(k).lower() not in {"authorization","content-type"}}
    headers={**extra,"Authorization":f"Bearer {cfg['api_key']}","Content-Type":"application/json"}
    payload={**payload,"model":cfg["model"]}
    async with httpx.AsyncClient(timeout=VISION_TIMEOUT,http2=False) as c:
        r=await c.post(cfg["base_url"]+"/chat/completions",headers=headers,json=payload)
    if r.status_code>=400:
        record_result(cfg.get("config_id"),False,"vision");raise RuntimeError(f"vision HTTP {r.status_code}: {r.text[:300]}")
    record_result(cfg.get("config_id"),True,"vision")
    return r.json()["choices"][0]["message"]["content"].strip()

def _download(url,headers=None):
    r=requests.get(url,headers=headers or {},timeout=15);r.raise_for_status();return r.content

STICKER_PROMPT=("这是一张表情包/贴图。只回一句：它表达什么情绪或什么意思，二十字以内。"
                "不要描述画面细节，不要分点，不要客套。")

async def recognize_image_url(url,caption="",sticker=False):
    try:
        data=await asyncio.wait_for(asyncio.to_thread(_download,url,{"User-Agent":"wang-guan-open/1.0"}),timeout=VISION_TIMEOUT)
    except Exception:
        return ""
    if not data:
        return ""

    key=hashlib.sha1(data).hexdigest()+("|s" if sticker else "|i")
    hit=_cache_get(key)
    if hit:
        return hit

    small=await asyncio.to_thread(_shrink,data)
    b64="data:image/jpeg;base64,"+base64.b64encode(small).decode()

    try:
        if sticker:
            text=await _vision({"messages":[{"role":"user","content":[{"type":"text","text":STICKER_PROMPT},{"type":"image_url","image_url":{"url":b64}}]}]},max_tokens=STICKER_MAX_TOKENS)
        else:
            # 只要它把关键信息说清楚，不用写长文，越长越慢
            prompt=("看图，用中文两三句话说明关键信息：是什么东西/什么场景，有文字就原样抄出来，"
                    "有价格、型号、数字、人名地名都别漏。不要客套，不要分点，不要脑补没看见的东西。")
            if caption:prompt+=f"\n发图的人同时说了：{caption[:120]!s}"
            text=await _vision({"messages":[{"role":"user","content":[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":b64}}]}]})
    except Exception:
        return ""

    text=(text or "").strip()
    if text:
        _cache_put(key,text)
    return text

async def recognize_telegram_image(file_id,caption=""):
    token=os.getenv("TG_BOT_TOKEN","")
    if not token:return ""
    info=await asyncio.to_thread(lambda:requests.get(f"https://api.telegram.org/bot{token}/getFile",params={"file_id":file_id},timeout=10).json())
    path=(info.get("result") or {}).get("file_path","")
    if not path:return ""
    return await recognize_image_url(f"https://api.telegram.org/file/bot{token}/{path}",caption)

async def transcribe_url(url,filename="audio.ogg"):
    key=os.getenv("STT_API_KEY",os.getenv("OPENAI_API_KEY","")).strip()
    base=os.getenv("STT_BASE_URL","https://api.openai.com/v1").rstrip("/")
    model=os.getenv("STT_MODEL","whisper-1")
    if not key:return ""
    data=await asyncio.to_thread(_download,url)
    def run():
        from openai import OpenAI
        suffix=os.path.splitext(filename)[1] or ".ogg"
        fd,path=tempfile.mkstemp(suffix=suffix);os.close(fd)
        try:
            with open(path,"wb") as f:f.write(data)
            client=OpenAI(api_key=key,base_url=base)
            with open(path,"rb") as f:r=client.audio.transcriptions.create(model=model,file=f)
            return re.sub(r'[\U00010000-\U0010ffff]','',r.text).strip()
        finally:
            try:os.remove(path)
            except OSError:pass
    return await asyncio.to_thread(run)

async def synthesize_mp3(text):
    key=os.getenv("TTS_API_KEY",os.getenv("OPENAI_API_KEY","")).strip()
    base=os.getenv("TTS_BASE_URL","https://api.openai.com/v1").rstrip("/")
    model=os.getenv("TTS_MODEL","tts-1");voice=os.getenv("TTS_VOICE","alloy")
    if not key:return b""
    def run():
        from openai import OpenAI
        r=OpenAI(api_key=key,base_url=base).audio.speech.create(model=model,voice=voice,input=text[:1200])
        return r.content
    return await asyncio.to_thread(run)
