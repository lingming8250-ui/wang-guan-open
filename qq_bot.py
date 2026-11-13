"""Generic OneBot v11 forward-WebSocket transport for QQ/NapCat-compatible bridges."""
from __future__ import annotations
import os,asyncio,json,uuid,logging
from starlette.websockets import WebSocket,WebSocketDisconnect

log=logging.getLogger(__name__)
WS_TOKEN=os.getenv("QQ_WS_TOKEN",os.getenv("NAPCAT_WS_TOKEN",""))
_conn:WebSocket|None=None
_loop=None
_lock=asyncio.Lock()
_pending={}
_proactive_started=False

async def send_action(action,params):
    if _conn is None:
        log.warning("send_action %s skipped: no qq connection",action)
        return False
    payload={"action":action,"params":params,"echo":str(uuid.uuid4())}
    async with _lock:
        try:
            await _conn.send_text(json.dumps(payload,ensure_ascii=False))
        except Exception as exc:
            log.warning("send_action %s failed: %s",action,exc)
            return False
    return True

async def send_action_wait(action,params,timeout=5):
    if _conn is None:return None
    echo=str(uuid.uuid4()); fut=asyncio.get_running_loop().create_future();_pending[echo]=fut
    async with _lock:await _conn.send_text(json.dumps({"action":action,"params":params,"echo":echo},ensure_ascii=False))
    try:return await asyncio.wait_for(asyncio.shield(fut),timeout)
    except asyncio.TimeoutError:_pending.pop(echo,None);return None

async def send_qq_msg(target_type,target_id,message):
    action="send_private_msg" if target_type=="private" else "send_group_msg"
    key="user_id" if target_type=="private" else "group_id"
    return await send_action(action,{key:int(target_id),"message":message})

async def get_msg(message_id):
    r=await send_action_wait("get_msg",{"message_id":int(message_id)});return r.get("data") if r and r.get("status")=="ok" else None

async def get_image(file_id):
    r=await send_action_wait("get_image",{"file":str(file_id)});return r.get("data") if r and r.get("status")=="ok" else None


def _ensure_proactive():
    """主动消息必须跑在有连接的这个进程里，后台进程看不到 _conn。"""
    global _proactive_started
    if _proactive_started:
        return
    _proactive_started=True
    try:
        from background_tasks import qq_proactive_loop
        asyncio.create_task(qq_proactive_loop())
        log.info("proactive loop started in web process")
    except Exception as exc:
        log.warning("failed to start proactive loop: %s",exc)


async def websocket_endpoint(ws:WebSocket):
    global _conn,_loop
    auth=(ws.headers.get("authorization") or "").replace("Bearer ","").replace("bearer ","").strip()
    if WS_TOKEN and auth!=WS_TOKEN:
        log.warning("qq ws rejected: bad token")
        await ws.close(code=1008);return
    await ws.accept();this=ws;_conn=this;_loop=asyncio.get_running_loop()
    log.info("qq ws connected from %s",ws.client)
    _ensure_proactive()
    from qq_workers import handle_event
    try:
        while True:
            raw=await ws.receive_text()
            try:data=json.loads(raw)
            except Exception:
                log.warning("qq ws non-json payload: %.120s",raw)
                continue
            echo=data.get("echo") if isinstance(data,dict) else None
            if echo and echo in _pending:
                fut=_pending.pop(echo)
                if not fut.done():fut.set_result(data)
                continue
            if not isinstance(data,dict):
                log.warning("qq ws non-dict event: %s",type(data).__name__)
                continue
            pt=data.get("post_type")
            if pt and pt!="meta_event":
                log.info("qq event: post=%s type=%s uid=%s gid=%s len=%s",pt,data.get("message_type"),data.get("user_id"),data.get("group_id"),len(raw))
                asyncio.create_task(handle_event(data))
    except WebSocketDisconnect:
        log.warning("qq ws disconnected")
    except Exception as exc:
        log.warning("qq ws error: %s",exc)
    finally:
        if _conn is this:
            _conn=None
            log.warning("qq ws closed; pending=%s",len(_pending))
            for fut in list(_pending.values()):
                if not fut.done():fut.cancel()
            _pending.clear()
