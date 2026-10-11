import os
import json
import logging
import asyncio
import httpx

log = logging.getLogger("vocab_service")

DEST_PATH = "miniapp/cet4.json"
# 备用源（含国内加速/镜像）
SOURCES = [
    "https://raw.githubusercontent.com/gbn48/wordlists/master/cet4/cet4.txt",
    "https://raw.githubusercontent.com/openetymology/OpenEtymology/master/CET4/CET4.txt",
    "https://raw.githubusercontent.com/qirong77/typo-word/master/src/books/3-CET4-%E9%A1%BA%E5%BA%8F.json"
]

async def auto_ensure_full_vocab():
    """后台静默拉取并合并四级 4000+ 大词库"""
    if os.path.exists(DEST_PATH):
        try:
            with open(DEST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) >= 2000:
                    log.info(f"CET4 vocab is already full: {len(data)} words.")
                    return
        except Exception:
            pass

    log.info("Fetching full CET4 vocab (target >3000 words)...")
    for url in SOURCES:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    text = resp.text
                    words = []
                    if url.endswith(".json"):
                        raw_json = resp.json()
                        for item in raw_json:
                            w = item if isinstance(item, str) else item.get("en", "")
                            if w:
                                words.append({
                                    "en": w,
                                    "ph": f"/{w}/",
                                    "cn": "四级考纲核心词",
                                    "tip": "真题高频考点",
                                    "tag": "四级大纲"
                                })
                    else:
                        for line in text.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            parts = line.split(None, 1)
                            w = parts[0]
                            cn = parts[1] if len(parts) > 1 else "四级考纲核心词"
                            words.append({
                                "en": w,
                                "ph": f"/{w}/",
                                "cn": cn,
                                "tip": "真题高频考点",
                                "tag": "四级大纲"
                            })
                    if len(words) > 1000:
                        os.makedirs(os.path.dirname(DEST_PATH), exist_ok=True)
                        with open(DEST_PATH, "w", encoding="utf-8") as f:
                            json.dump(words, f, ensure_ascii=False)
                        log.info(f"Successfully populated {len(words)} CET4 words into {DEST_PATH}")
                        return
        except Exception as e:
            log.warning(f"Failed to fetch from {url}: {e}")
