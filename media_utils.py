"""Generic media helpers for vision, STT and TTS.

识图优化：
针对商品详情、购物截图等高频图，强制在最前面提取核心文本标题和商品名字，
防止视觉模型写一堆画面视觉描述导致真正的商品标题被截断或漏掉。
"""
from __future__ import annotations
import os,io,base64,tempfile,requests,asyncio,re
from llm_channels import get_config,record_result

VISION_MAX_EDGE=int(os.getenv("VISION_MAX_EDGE","1280"))
VISION_JPEG_QUALITY=int(os.getenv("VISION_JPEG_QUALITY","85"))

def _download(url,headers=None):
    r=requests.get(url,headers=headers or {},timeout=20);r.raise_for_status();return r.content

def _shrink(data: bytes) -> bytes:
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

async def _vision(payload,label="vision"):
    import httpx
    cfg=await asyncio.to_thread(get_config,"vision")
    extra={str(k):str(v) for k,v in (cfg.get("extra_headers") or {}).items() if str(k).lower() not in {"authorization","content-type"}}
    headers={**extra,"Authorization":f"Bearer {cfg['api_key']}","Content-Type":"application/json"}
    payload={**payload,"model":cfg["model"]}
    async with httpx.AsyncClient(timeout=40,http2=False) as c:
        r=await c.post(cfg["base_url"]+"/chat/completions",headers=headers,json=payload)
    if r.status_code>=400:
        record_result(cfg.get("config_id"),False,"vision");raise RuntimeError(f"vision HTTP {r.status_code}: {r.text[:300]}")
    record_result(cfg.get("config_id"),True,"vision")
    return r.json()["choices"][0]["message"]["content"].strip()

async def recognize_image_url(url,caption=""):
    data=await asyncio.to_thread(_download,url,{"User-Agent":"wang-guan-open/1.0"})
    small=await asyncio.to_thread(_shrink,data)
    b64="data:image/jpeg;base64,"+base64.b64encode(small).decode()
    prompt=(
        "You are an OCR and visual recognition tool. "
        "CRITICAL REQUIREMENT: "
        "1. If this is an e-commerce page, product screenshot, chat screenshot, or document: "
        "FIRST directly output the EXACT product title, store/brand name, text headers, and key details at the very beginning of your response. "
        "2. Do NOT spend long paragraphs describing hands, background, or phone status bars before the actual product title. Put the primary readable text FIRST. "
        "3. If it's a meme or photo, describe its content and emotion clearly."
    )
    if caption:prompt+=f" The sender also wrote: {caption!r}; use it only as context."
    return await _vision({"max_tokens":1200,"messages":[{"role":"user","content":[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":b64}}]}]})

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