"""Generic media helpers for vision, STT and TTS.

图不再先翻译成文字：压一把，直接给主模型看。
这里只负责三件事：下载、压、转 data URI。
"""
from __future__ import annotations
import os,io,base64,tempfile,requests,asyncio,re

VISION_MAX_EDGE=int(os.getenv("VISION_MAX_EDGE","1024"))
VISION_JPEG_QUALITY=int(os.getenv("VISION_JPEG_QUALITY","85"))
IMAGE_FETCH_TIMEOUT=float(os.getenv("IMAGE_FETCH_TIMEOUT","20"))

def _download(url,headers=None,timeout=20):
    r=requests.get(url,headers=headers or {},timeout=timeout);r.raise_for_status();return r.content

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
        return out or data
    except Exception:
        return data

async def fetch_image_data_uri(url) -> str:
    """下回来、压小、转成 data:image/jpeg;base64,... 直接塞进对话用。"""
    if not url or not str(url).startswith("http"):
        return ""
    try:
        data=await asyncio.wait_for(
            asyncio.to_thread(_download,url,{"User-Agent":"wang-guan-open/1.0"},IMAGE_FETCH_TIMEOUT),
            timeout=IMAGE_FETCH_TIMEOUT,
        )
    except Exception:
        return ""
    if not data:
        return ""
    small=await asyncio.to_thread(_shrink,data)
    return "data:image/jpeg;base64,"+base64.b64encode(small).decode()

async def recognize_telegram_image(file_id,caption=""):
    token=os.getenv("TG_BOT_TOKEN","")
    if not token:return ""
    info=await asyncio.to_thread(lambda:requests.get(f"https://api.telegram.org/bot{token}/getFile",params={"file_id":file_id},timeout=10).json())
    path=(info.get("result") or {}).get("file_path","")
    if not path:return ""
    return await fetch_image_data_uri(f"https://api.telegram.org/file/bot{token}/{path}")

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
