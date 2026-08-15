import os
import json
import time
import random
import re
import base64
import hashlib
import hmac
from datetime import datetime, timedelta
from google import genai
from google.genai import types
import requests

# ========== 讀取 Secrets / Variables 環境變數 ==========
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK")
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")
SYSTEM_PROMPT = os.getenv("HK_NEWS_PROMPT", "")

CACHE_FILE = "push_cache.json"
LOCK_FILE = "run.lock"
MODEL_NAME = "gemini-2.5-flash"

# ========== Gemini 全局初始化（新 SDK，只做一次）==========
CLIENT = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_CONFIG = types.GenerateContentConfig(
    temperature=0.1,
    tools=[types.Tool(google_search=types.GoogleSearch())]
)

# ========== 飛書推送 ==========
def gen_feishu_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")

def send_feishu(raw_text: str) -> int:
    hkt_now = get_hkt_now()
    time_str = hkt_now.strftime("%Y-%m-%d %H:%M HKT")

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📊 港股新聞監控快訊"},
                "template": "red"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": raw_text}
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"⏰ 推送時間：{time_str}"}
                    ]
                }
            ]
        }
    }

    if FEISHU_SECRET:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = gen_feishu_sign(ts, FEISHU_SECRET)

    for attempt in range(3):
        try:
            r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=30)
            resp = r.json()
            if r.status_code == 200 and resp.get("code", 0) == 0:
                print(f"飛書推送成功 (attempt {attempt+1})")
                return 200
            else:
                print(f"飛書推送失敗: status={r.status_code}, resp={resp}")
        except Exception as e:
            print(f"飛書推送異常 (attempt {attempt+1}): {e}")
        if attempt < 2:
            time.sleep(3)
    return -1

# ========== 時間 / 鎖 / 緩存 ==========
def get_hkt_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)

def is_weekend() -> bool:
    return get_hkt_now().weekday() >= 5

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
    if (h == 11) or (h == 12) or (h == 13 and m <= 30):
        return "long_run"
    if (h == 15 and m == 0) or (h == 15 and m == 50) or (h == 22 and m == 0):
        return "one_shot"
    return "none"

def is_long_run_time_over():
    hkt = get_hkt_now()
    h, m = hkt.hour, hkt.minute
    if h >= 10:
        return True
    if h >= 14 or (h == 13 and m > 30):
        return True
    return False

# ========== Gemini 調用（新 SDK，唯一扣 token 嘅地方）==========
def gemini_call(prompt: str):
    response = CLIENT.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=GEMINI_CONFIG
    )
    return response.text

def parse_extract_keys(gemini_output: str):
    stock_pattern = re.compile(r"🏷️ 股票：.*?(\d+\.HK)", re.DOTALL)
    title_pattern = re.compile(r"📰 新聞標題：(.*?)\n", re.DOTALL)
    titles = title_pattern.findall(gemini_output)
    stock_codes = stock_pattern.findall(gemini_output)
    return titles, stock_codes

def scan_once(include_macro: bool = True):
    cache = load_cache()
    pushed_set = set(cache.get("pushed", []))

    if include_macro:
        prompt = SYSTEM_PROMPT
    else:
        prompt = (
            SYSTEM_PROMPT
            + "\n\n【重要補充指令】今次掃描**只需要輸出「=== 【個股重大利好】 ===」部分**，"
              "**唔好輸出「=== 【板塊宏觀消息】 ===」區塊**，板塊宏觀消息已經喺本次啟動第一輪掃描過，唔使重複。"
        )

    llm_result = gemini_call(prompt)
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

    final_out = []
    if include_macro:
        final_out.append("=== 【板塊宏觀消息】 ===")
        final_out.append(macro_block_text)
    final_out.append("=== 【個股重大利好】 ===")
    final_out.append(final_stock_text)

    final_text = "\n".join(final_out)
    send_feishu(final_text)

    cache["pushed"] = list(pushed_set)
    save_cache(cache)

# ========== 主流程 ==========
def main():
    hkt_now = get_hkt_now()
    print(f"HKT now:{hkt_now.strftime('%Y-%m-%d %H:%M:%S')}")

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
            print("one-shot模式，執行一次完整掃描（板塊+個股）")
            scan_once(include_macro=True)

        elif run_mode == "long_run":
            print("長駐模式：第一輪跑板塊+個股，後續輪次只跑個股")
            first_round = True
            while True:
                if is_long_run_time_over():
                    print("已到長駐結束時間，退出迴圈，job完結")
                    break
                try:
                    scan_once(include_macro=first_round)
                    first_round = False
                except Exception as e:
                    print(f"本輪掃描發生異常，跳過本輪：{str(e)}")
                    first_round = False

                sleep_sec = random.randint(8 * 60, 10 * 60)
                print(f"本輪完成，休眠 {sleep_sec} 秒後下一輪掃描")
                time.sleep(sleep_sec)
    finally:
        release_lock()

if __name__ == "__main__":
    main()
