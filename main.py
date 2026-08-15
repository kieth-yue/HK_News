import os
import json
import time
import random
import re
from datetime import datetime, timedelta
import google.generativeai as genai
import requests

# ========== 讀取 Secrets 環境變數 ==========
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK")
SYSTEM_PROMPT = os.getenv("HK_NEWS_PROMPT", "")

CACHE_FILE = "push_cache.json"
LOCK_FILE = "run.lock"
MODEL_NAME = "gemini-2.5-flash"

def get_hkt_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)

def is_weekend() -> bool:
    now = get_hkt_now()
    return now.weekday() >= 5

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {"pushed": []}
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_cache(cache_data):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)

def acquire_lock() -> bool:
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                ts = float(f.read())
            lock_dt = datetime.fromtimestamp(ts)
            now = get_hkt_now()
            if (now - lock_dt).total_seconds() > 12 * 60:
                os.unlink(LOCK_FILE)
            else:
                return False
        except Exception:
            os.unlink(LOCK_FILE)
    with open(LOCK_FILE, "w") as f:
        f.write(str(get_hkt_now().timestamp()))
    return True

def release_lock():
    if os.path.exists(LOCK_FILE):
        os.unlink(LOCK_FILE)

def get_run_mode():
    hkt = get_hkt_now()
    h, m = hkt.hour, hkt.minute
    if 8 <= h < 10:
        return "long_run"
    if (h == 11) or (h == 12) or (h ==13 and m <=30):
        return "long_run"
    if (h ==15 and m ==0) or (h ==15 and m ==50) or (h ==22 and m ==0):
        return "one_shot"
    return "none"

def is_long_run_time_over():
    hkt = get_hkt_now()
    h, m = hkt.hour, hkt.minute
    if h >= 10:
        return True
    if h >=14 or (h ==13 and m >30):
        return True
    return False

def gemini_call(prompt: str):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        MODEL_NAME,
        generation_config={"temperature": 0.1},
        tools=[{"google_search": {}}]
    )
    resp = model.generate_content(prompt)
    return resp.text

def send_feishu(raw_text: str):
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": raw_text}}]
        }
    }
    r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=30)
    return r.status_code

def parse_extract_keys(gemini_output: str):
    stock_pattern = re.compile(r"🏷️ 股票：.*?(\d+\.HK)", re.DOTALL)
    title_pattern = re.compile(r"📰 新聞標題：(.*?)\n", re.DOTALL)
    titles = title_pattern.findall(gemini_output)
    stock_codes = stock_pattern.findall(gemini_output)
    return titles, stock_codes

def scan_once():
    cache = load_cache()
    pushed_set = set(cache.get("pushed", []))

    llm_result = gemini_call(SYSTEM_PROMPT)
    print("=== Gemini output ===")
    print(llm_result)

    titles, stock_codes = parse_extract_keys(llm_result)
    lines = llm_result.splitlines()
    block_macro = []
    block_stock = []
    mode = None
    for line in lines:
        if "=== 【板塊宏觀消息】 ===" in line:
            mode = "macro"
            continue
        if "=== 【個股重大利好】 ===" in line:
            mode = "stock"
            continue
        if mode == "macro":
            block_macro.append(line)
        if mode == "stock":
            block_stock.append(line)

    macro_block_text = "\n".join(block_macro).strip()
    stock_block_text = "\n".join(block_stock).strip()

    filtered_stock_lines = []
    stock_lines_all = stock_block_text.split("\n")
    idx_title = 0
    skip_entry = False
    for line in stock_lines_all:
        if line.startswith("📰 新聞標題："):
            skip_entry = False
            current_title = line.replace("📰 新聞標題：", "").strip()
            if idx_title < len(stock_codes):
                code = stock_codes[idx_title]
                key = f"{code}||{current_title}"
                if key in pushed_set:
                    skip_entry = True
                else:
                    pushed_set.add(key)
                idx_title += 1
        if not skip_entry:
            filtered_stock_lines.append(line)

    final_stock_text = "\n".join(filtered_stock_lines).strip()

    final_out = [
        "=== 【板塊宏觀消息】 ===",
        macro_block_text,
        "=== 【個股重大利好】 ===",
        final_stock_text
    ]
    final_text = "\n".join(final_out)
    send_feishu(final_text)

    cache["pushed"] = list(pushed_set)
    save_cache(cache)

def main():
    hkt_now = get_hkt_now()
    print(f"HKT now:{hkt_now.strftime('%Y‑%m‑%d %H:%M:%S')}")

    if is_weekend():
        print("週末，退出")
        return

    run_mode = get_run_mode()
    print(f"運行模式: {run_mode}")
    if run_mode == "none":
        print("不在執行窗口，退出")
        return

    if not acquire_lock():
        print("已有另一個實例正在執行，跳過")
        return

    try:
        if run_mode == "one_shot":
            print("one‑shot模式，執行一次掃描完畢即結束")
            scan_once()

        elif run_mode == "long_run":
            print("長駐模式：執行prompt → 隨機sleep8‑10分鐘 循環，到點退出")
            while True:
                if is_long_run_time_over():
                    print("已到長駐結束時間，退出迴圈，job完結")
                    break
                try:
                    scan_once()
                except Exception as e:
                    print(f"本輪掃描發生異常，跳過本輪：{str(e)}")

                sleep_sec = random.randint(8*60, 10*60)
                print(f"本輪完成，休眠 {sleep_sec} 秒後下一輪掃描")
                time.sleep(sleep_sec)
    finally:
        release_lock()

if __name__ == "__main__":
    main()
