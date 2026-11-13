"""Multi-channel LLM configuration with an optional fallback provider."""
from __future__ import annotations
import os, threading, time, logging, requests
from app_config import SUPABASE_URL, SUPABASE_KEY

log = logging.getLogger(__name__)
_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.RLock()
TTL = 30

CHANNEL_COLUMNS = {
    "chat": "active",
    "background": "bg_active",
    "vision": "vision_active",
    "proactive": "free_activity_active",
    "qq": "qq_active",
    "wx": "wx_active",
}

ENV_PREFIX = {
    "chat": "CHAT", "background": "BG_CHAT", "vision": "VISION",
    "proactive": "PROACTIVE", "qq": "QQ_LLM", "wx": "WX_LLM",
}


def _pick_key(row: dict) -> str:
    keys = row.get("api_keys") or []
    if keys:
        idx = row.get("current_key_index", 0) or 0
        if not isinstance(idx, int) or idx < 0 or idx >= len(keys): idx = 0
        return keys[idx]
    return row.get("api_key", "")


def _specific_env_config(channel: str) -> dict | None:
    p = ENV_PREFIX[channel]
    base = os.getenv(f"{p}_BASE_URL", "").rstrip("/")
    key = os.getenv(f"{p}_API_KEY", "")
    model = os.getenv(f"{p}_MODEL", "")
    if base and key and model:
        return {"base_url": base, "api_key": key, "model": model, "extra_headers": {}, "channel": channel}
    return None


def _generic_env_chat_config() -> dict:
    return {
        "base_url": os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "api_key": os.getenv("LLM_API_KEY", ""),
        "model": os.getenv("LLM_MODEL", ""),
        "extra_headers": {},
        "channel": "chat",
    }


def _fallback_config() -> dict | None:
    """备用站点：主力挂了（429 / 5xx / 超时）就自动顶上，不用手动换。"""
    base = os.getenv("LLM_FALLBACK_BASE_URL", "").rstrip("/")
    key = os.getenv("LLM_FALLBACK_API_KEY", "")
    model = os.getenv("LLM_FALLBACK_MODEL", "")
    if base and key and model:
        return {"base_url": base, "api_key": key, "model": model, "extra_headers": {}, "is_fallback": True}
    return None


def _runtime_chat_config() -> dict | None:
    try:
        from runtime_config import get_active_provider, get_active_api_key
        row = get_active_provider()
        if not row:
            return None
        base = str(row.get("base_url") or "").rstrip("/")
        key = get_active_api_key(row)
        model = str(row.get("active_model") or row.get("model") or "")
        if not base or not key or not model:
            return None
        return {
            "base_url": base, "api_key": key, "model": model,
            "extra_headers": row.get("extra_headers") or {},
            "channel": "chat", "runtime_provider": True,
        }
    except Exception as exc:
        log.debug("runtime provider fallback unavailable: %s", exc)
        return None


def _supabase_config(channel: str) -> dict | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    col = CHANNEL_COLUMNS[channel]
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/llm_config?{col}=eq.true&limit=1",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=5,
        )
        rows = r.json() if r.ok else []
        if not rows:
            return None
        row = rows[0]
        return {
            "base_url": row["base_url"].rstrip("/"), "api_key": _pick_key(row),
            "model": row["model"], "extra_headers": row.get("extra_headers") or {},
            "config_id": row.get("id"), "channel": channel,
        }
    except Exception as exc:
        log.warning("LLM config %s lookup failed: %s", channel, exc)
        return None


def _apply_overrides(channel: str, cfg: dict) -> dict:
    if channel != "qq":
        return cfg
    try:
        from storage import get_setting
        override = (get_setting("qq_model_override") or "").strip()
        if override and override != cfg.get("model"):
            return {**cfg, "model": override, "model_override": True}
    except Exception:
        pass
    return cfg


def get_config(channel: str = "chat") -> dict:
    if channel not in CHANNEL_COLUMNS:
        raise ValueError(f"Unknown LLM channel: {channel}")
    now = time.time()
    cached = _CACHE.get(channel)
    if cached and now - cached[0] < TTL:
        return cached[1]
    with _LOCK:
        cached = _CACHE.get(channel)
        if cached and time.time() - cached[0] < TTL:
            return cached[1]

        cfg = _supabase_config(channel)
        if cfg is None:
            cfg = _specific_env_config(channel)

        if cfg is None and channel == "chat":
            cfg = _runtime_chat_config() or _generic_env_chat_config()
        elif cfg is None:
            cfg = get_config("chat")
            cfg = {**cfg, "channel": channel}

        cfg = _apply_overrides(channel, cfg)
        fb = _fallback_config()
        if fb and not (fb.get("base_url") == cfg.get("base_url") and fb.get("model") == cfg.get("model")):
            cfg = {**cfg, "fallback": fb}
        _CACHE[channel] = (time.time(), cfg)
        return cfg


def clear_cache(channel: str | None = None):
    if channel: _CACHE.pop(channel, None)
    else: _CACHE.clear()


def record_result(config_id, success: bool, channel: str = "chat", fail_threshold: int = 3):
    if not config_id or not SUPABASE_URL or not SUPABASE_KEY:
        return
    rpc = "llm_config_record_success" if success else "llm_config_record_failure"
    payload = {"p_config_id": config_id}
    if not success: payload["p_fail_threshold"] = fail_threshold
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{rpc}",
            headers={"apikey": SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}","Content-Type":"application/json"},
            json=payload, timeout=5,
        )
    except Exception as exc:
        log.debug("record_result ignored: %s", exc)
    clear_cache(channel)
