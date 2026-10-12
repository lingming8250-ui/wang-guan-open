"""Administrative endpoints for public MiniApp and diagnostics."""
import os, hmac, json, asyncio
from starlette.responses import JSONResponse
from memory_store import list_memories
from storage import get, delete_ids
from scheduled import run_nightly_summary, run_platform_batch_compress
from context import build_context

API_SECRET=os.getenv("API_SECRET","")

def auth_ok(request):
    if not API_SECRET:return False
    raw=request.headers.get("authorization",""); token=raw.split(" ",1)[-1].strip() if " " in raw else raw.strip()
    return hmac.compare_digest(token,API_SECRET)

async def context_preview(request):
    if not auth_ok(request):return JSONResponse({"error":"unauthorized"},401)
    text=await asyncio.to_thread(build_context)
    return JSONResponse({"ok":True,"content":text,"length":len(text)})

async def memories(request):
    if not auth_ok(request):return JSONResponse({"error":"unauthorized"},401)
    return JSONResponse({"ok":True,"items":list_memories(limit=100)})

async def threads(request):
    if not auth_ok(request):return JSONResponse({"error":"unauthorized"},401)
    return JSONResponse({"ok":True,"items":get("threads","order=created_at.desc&limit=100")})

async def reminders(request):
    if not auth_ok(request):return JSONResponse({"error":"unauthorized"},401)
    return JSONResponse({"ok":True,"items":get("reminders","order=trigger_at.asc&limit=100")})

async def trigger_summary(request):
    if not auth_ok(request):return JSONResponse({"error":"unauthorized"},401)
    await asyncio.to_thread(run_nightly_summary);return JSONResponse({"ok":True})

async def trigger_compress(request):
    if not auth_ok(request):return JSONResponse({"error":"unauthorized"},401)
    await asyncio.to_thread(run_platform_batch_compress);return JSONResponse({"ok":True})

async def trigger_qq_test(request):
    if not auth_ok(request):return JSONResponse({"error":"unauthorized"},401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    msg = (body.get("message") or "祖宗 收到没 老头在后台给你打了个卡").strip()
    from qq_bot import send_qq_msg, _conn
    from conversation_store import save_message
    from qq_workers import _split_into_bubbles, _segments
    target = os.getenv("QQ_OWNER_ID","")
    if not target:
        return JSONResponse({"error":"no QQ_OWNER_ID"}, 400)
    if _conn is None:
        return JSONResponse({"error":"QQ websocket not connected"}, 503)
    chunks = _split_into_bubbles(msg)
    sent_res = []
    for chunk in chunks:
        if chunk:
            save_message("assistant", chunk, scene="message", source="qq-test")
            res = await send_qq_msg("private", int(target), _segments(chunk))
            sent_res.append(res)
            await asyncio.sleep(1.2)
    return JSONResponse({"ok": True, "results": sent_res})
