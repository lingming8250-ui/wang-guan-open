"""Unified streaming OpenAI-compatible LLM caller.

带自动测速：每个站点每次花多久都记下来，下一轮谁快就先用谁，失败自动换备用。
"""
from __future__ import annotations
import json, logging, re, time, httpx
from llm_channels import get_config, record_result, clear_cache

log = logging.getLogger(__name__)

ERROR_PATTERNS = [
    r"The prompt could not be submitted",
    r"violate Google",
    r"Prohibited Use policy",
    r"sensitive words",
    r"safety policy",
    r"content management policy",
    r"Upstream error",
    r"Rate limit reached",
]

# 每个 站点+模型 的耗时记录（指数移动平均）
_STATS: dict[str, dict] = {}


def _is_upstream_safety_error(text: str) -> bool:
    for pat in ERROR_PATTERNS:
        if re.search(pat, text, re.I):
            return True
    return False


def _safe_headers(api_key: str, extra_headers: dict | None = None) -> dict:
    custom = {str(k): str(v) for k, v in (extra_headers or {}).items()}
    for key in list(custom):
        if key.lower() in {"authorization", "content-type"}:
            custom.pop(key, None)
    return {**custom, "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _key(cfg) -> str:
    return f"{cfg.get('base_url')}|{cfg.get('model')}"


def _note(key: str, ms: float, ok: bool) -> None:
    s = _STATS.setdefault(key, {"avg": ms, "n": 0, "fails": 0})
    if ok:
        s["avg"] = ms if s["n"] == 0 else (s["avg"] * 0.7 + ms * 0.3)
        s["n"] += 1
    else:
        s["fails"] += 1
        s["avg"] = s["avg"] * 1.15 + 1500


def _estimate(cfg):
    s = _STATS.get(_key(cfg))
    if not s or s["n"] == 0:
        return None
    return s["avg"]


async def _once(cfg, messages, max_tokens, tools, extra_body, channel, on_delta, emitted):
    payload = {"model": cfg["model"], "messages": messages, "max_tokens": max_tokens, "stream": True}
    if tools: payload["tools"] = tools
    if extra_body: payload.update(extra_body)
    headers = _safe_headers(cfg["api_key"], cfg.get("extra_headers"))
    content = []
    tc_map = {}
    got_done = False

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=90, write=15, pool=15), http2=False) as client:
        async with client.stream("POST", cfg["base_url"] + "/chat/completions", headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode(errors="ignore")[:300]
                raise RuntimeError(f"HTTP {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                s = line[5:].strip()
                if s == "[DONE]":
                    got_done = True
                    break
                try:
                    d = json.loads(s)
                except Exception:
                    continue
                choices = d.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                if delta.get("reasoning_content"):
                    continue
                if delta.get("content"):
                    content.append(delta["content"])
                    emitted[0] = True
                    if on_delta is not None:
                        try:
                            await on_delta(delta["content"])
                        except Exception:
                            log.exception("on_delta failed channel=%s", channel)
                for td in delta.get("tool_calls") or []:
                    i = td.get("index", 0)
                    e = tc_map.setdefault(i, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if td.get("id"): e["id"] = td["id"]
                    fn = td.get("function") or {}
                    if fn.get("name"): e["function"]["name"] = fn["name"]
                    if fn.get("arguments"): e["function"]["arguments"] += fn["arguments"]

    if not got_done:
        log.warning("stream ended without [DONE] channel=%s", channel)
    full = "".join(content).strip()
    if _is_upstream_safety_error(full):
        log.warning("intercepted upstream safety text: %s", full[:120])
        return "刚卡了一下", []
    full = re.sub(r'<think>.*?</think>', '', full, flags=re.S)
    full = re.sub(r'(?:分析[^\n]*[:：]|情绪[^\n]*[:：]|-薄销的人设[^\n]*[:：]).*?(?=\n\n|\Z)', '', full, flags=re.S).strip()
    return full, [tc_map[i] for i in sorted(tc_map)]


def _ordered(cfg) -> list:
    """按实测耗时排序；没有数据的保持原顺序（主在前）。"""
    cands = [cfg]
    fb = cfg.get("fallback")
    if fb:
        cands.append(fb)
    if len(cands) > 1:
        known = [_estimate(c) for c in cands]
        if all(k is not None for k in known):
            cands = [c for _, c in sorted(zip(known, cands), key=lambda x: x[0])]
    return cands


async def call_llm(messages, max_tokens=4096, tools=None, extra_body=None, channel="chat", on_delta=None):
    cfg = get_config(channel)
    if not cfg.get("api_key") or not cfg.get("model"):
        raise RuntimeError(f"LLM channel '{channel}' is not configured")

    cands = _ordered(cfg)
    if len(cands) > 1 and cfg.get("fallback") and cands[0] is not cfg:
        log.info("按实测延迟切到 %s（主站当前较慢）", cands[0].get("model"))

    last_exc = None
    for idx, c in enumerate(cands):
        emitted = [False]
        started = time.monotonic()
        try:
            text, calls = await _once(c, messages, max_tokens, tools, extra_body, channel, on_delta, emitted)
            ms = (time.monotonic() - started) * 1000
            _note(_key(c), ms, True)
            record_result(c.get("config_id"), True, channel)
            log.info("llm ok %s in %dms", c.get("model"), int(ms))
            return text, calls
        except Exception as exc:
            ms = (time.monotonic() - started) * 1000
            _note(_key(c), ms, False)
            last_exc = exc
            log.warning("llm fail %s (%dms): %s", c.get("model"), int(ms), str(exc)[:160])
            if emitted[0]:
                raise
            record_result(c.get("config_id"), False, channel)
            continue
    raise last_exc if last_exc else RuntimeError("all LLM providers failed")
