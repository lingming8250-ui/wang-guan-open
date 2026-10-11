import json
import os
import re
import urllib.request

RAW_URL = "https://raw.githubusercontent.com/gbn48/wordlists/master/cet4/cet4.txt"
OUTPUT_FILE = "miniapp/cet4.json"

def fetch_and_convert():
    print(f"Fetching CET4 list from {RAW_URL}...")
    req = urllib.request.Request(
        RAW_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", errors="ignore")

    words = []
    lines = text.splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Format usually: word pos.meaning ...
        parts = line.split(None, 1)
        if len(parts) == 1:
            w = parts[0]
            cn = ""
        else:
            w, cn = parts[0], parts[1]
        
        words.append({
            "en": w,
            "ph": f"/{w}/",
            "cn": cn,
            "tip": "四级真题大纲核心词",
            "tag": "四级大纲"
        })

    print(f"Parsed {len(words)} CET4 words.")
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False)
    print(f"Successfully saved to {OUTPUT_FILE}.")

if __name__ == "__main__":
    fetch_and_convert()
