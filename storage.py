"""Small Supabase REST helper used by the public build.

The private project contains the same style of persistence, but this module deliberately
contains no personal table contents or IDs. Missing Supabase configuration simply disables
persistence instead of crashing the gateway.
"""
from __future__ import annotations
import os, logging
import requests
from app_config import SUPABASE_URL, SUPABASE_KEY

log = logging.getLogger(__name__)

_SEED_CHRONICLE_FILE = "docs/long_term_chronicle_seed.txt"


def enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _headers(prefer: str | None = None) -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def get(table: str, query: str = "") -> list:
    if not enabled():
        return []
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{query}", headers=_headers(), timeout=10)
        if not r.ok:
            log.warning("storage.get %s HTTP %s: %s", table, r.status_code, r.text[:200])
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("storage.get %s failed: %s", table, exc)
        return []


def insert(table: str, body: dict) -> list:
    if not enabled():
        return []
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}", headers=_headers("return=representation"),
        json=body, timeout=10,
    )
    if not r.ok:
        raise RuntimeError(f"Supabase insert {table}: HTTP {r.status_code} {r.text[:300]}")
    try:
        data = r.json()
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def update(table: str, query: str, body: dict) -> None:
    if not enabled():
        return
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}?{query}", headers=_headers(), json=body, timeout=10)
    if not r.ok:
        raise RuntimeError(f"Supabase update {table}: HTTP {r.status_code} {r.text[:300]}")


def delete_ids(table: str, ids: list, batch_size: int = 200) -> None:
    if not enabled() or not ids:
        return
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i+batch_size]
        q = ",".join(str(x) for x in batch)
        r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}?id=in.({q})", headers=_headers(), timeout=10)
        if not r.ok:
            raise RuntimeError(f"Supabase delete {table}: HTTP {r.status_code} {r.text[:300]}")


def upsert_setting(key: str, value: str) -> None:
    if not enabled():
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/bot_settings",
        headers=_headers("resolution=merge-duplicates"),
        json={"key": key, "value": value}, timeout=10,
    )
    if not r.ok:
        raise RuntimeError(f"Supabase setting {key}: HTTP {r.status_code} {r.text[:300]}")


def get_setting(key: str, default=None):
    rows = get("bot_settings", f"key=eq.{key}&select=value&limit=1")
    val = rows[0].get("value") if rows else None
    if val:
        return val
    # 如果数据库里还没种上往事编年史，自动从内置种子文件读取并回写进数据库
    if key == "long_term_chronicle":
        try:
            if os.path.exists(_SEED_CHRONICLE_FILE):
                with open(_SEED_CHRONICLE_FILE, "r", encoding="utf-8") as f:
                    seed = f.read().strip()
                if seed:
                    upsert_setting("long_term_chronicle", seed)
                    return seed
        except Exception as exc:
            log.warning("seed chronicle load failed: %s", exc)
    return default
