import os
import json
import logging
import asyncio

log = logging.getLogger("vocab_sync")

RAW_URL = "https://raw.githubusercontent.com/gbn48/wordlists/master/cet4/cet4.txt"
DEST_PATH = "miniapp/cet4.json"

async def auto_ensure_full_vocab():
    """
    检查 miniapp/cet4.json 是否存在或词数是否充足。
    如果只有打底几十词，则自动在后台异步拉取全量 4000+ 四级大纲词汇并写入。
    """
    needs_fetch = False
    if not os.path.exists(DEST_PATH):
        needs_fetch = True
    else:
        try:
            with open(DEST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, list) or len(data) < 500:
                    needs_fetch = True
        except Exception:
            needs_fetch = True

    if not needs_fetch:
        log.info("CET4 full vocab already exists with full count.")
        return

    log.info("Starting background download of complete CET4 vocab database...")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(RAW_URL)
            if resp.status_code == 200:
                lines = resp.text.splitlines()
                words = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(None, 1)
                    w = parts[0]
                    cn = parts[1] if len(parts) > 1 else ""
                    words.append({
                        "en": w,
                        "ph": f"/{w}/",
                        "cn": cn,
                        "tip": "四级真题大纲核心词",
                        "tag": "四级大纲"
                    })
                os.makedirs(os.path.dirname(DEST_PATH), exist_ok=True)
                with open(DEST_PATH, "w", encoding="utf-8") as f:
                    json.dump(words, f, ensure_ascii=False)
                log.info(f"Successfully downloaded and saved {len(words)} CET4 words to {DEST_PATH}!")
            else:
                log.warning(f"Failed to fetch vocab: status {resp.status_code}")
    except Exception as exc:
        log.warning(f"Failed to fetch full CET4 vocab: {exc}")
