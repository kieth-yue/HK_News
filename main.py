import os
import json
import time
import random
import re
import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from google import genai
from google.genai import types
import requests

# ========== 讀取 Secrets / Variables 環境變數 ==========
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK")
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")
SYSTEM_PROMPT = os.getenv("HK_NEWS_PROMPT", "")
FORCE_RUN = os.getenv("FORCE_RUN", "false").lower() == "true"

CACHE_FILE = "push_cache.json"
LOCK_FILE = "run.lock"
MODEL_NAME = "gemini-2.5-flash"

HKT = timezone(timedelta(hours=8))

# ========== Gemini 全局初始化（只做一次）==========
CLIENT = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_CONFIG = types.GenerateContentConfig(
    temperature=0.1,
    tools=[types.Tool(google_search=types.GoogleSearch())]
)

# ========== 時區輔助 ==========
def get_hkt_now() -> datetime:
    return datetime.now(HKT)

def is_weekend() -> bool:
    return get_hkt_now().weekday() >= 5

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
    date_str = hkt_now.strftime("%Y-%m-%d")
    time_str = hkt_now.strftime("%Y-%m-%d %H:%M HKT")

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 港股新聞監控快訊 | {date_str}"},
                "template": "wathet"
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

# ========== 文本後處理 ==========
def format_links(text: str) -> str:
    """將超長 URL 轉成 Markdown 短連結，多個連結只取第一個，一行顯示"""
    def replacer(m):
        urls = re.findall(r'https?://\S+', m.group(0))
        if urls:
            return f"🔗 連結：[點擊查看]({urls[0]})"
        return m.group(0)
    pattern = r'🔗 連結：[\s\S]*?(?=\n[💡🏷️📰⏰📌]|\n===|\Z)'
    return re.sub(pattern, replacer, text)

# ========== 鎖與快取（時區 BUG 已修復：統一用 unix timestamp）==========
def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {"pushed": []}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"pushed": []}

def save_cache(cache_data):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"寫入緩存失敗: {e}")

def acquire_lock() -> bool:
    now_ts = time.time()
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                ts = float(f.read().strip())
            # 直接用 unix timestamp 秒數差，唔再做時區轉換
            if (now_ts - ts) > 12 * 60:
                os.unlink(LOCK_FILE)
            else:
                return False
        except Exception:
            try:
                os.unlink(LOCK_FILE)
            except Exception:
                pass
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(now_ts))
        return True
    except Exception:
        return False

def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.unlink(LOCK_FILE)
    except Exception:
        pass

# ========== 時間窗口判斷 ==========
def get_run_mode():
    hkt = get_hkt_now()
    h, m = hkt.hour, hkt.minute
    # 長駐窗口
    if 8 <= h < 10:
        return "long_run"
    if (h == 11) or (h == 12) or (h == 13 and m <= 30):
        return "long_run"
    # 一次性定時點（加 ±5 分鐘容差，應對 cron 延遲）
    if (h == 15 and m <= 5) or (h == 15 and 45 <= m <= 55) or (h == 22 and m <= 5):
        return "one_shot"
    return "none"

def is_long_run_time_over():
    """判斷當前長駐 job 係咪到結束時間。
    08-10 時段：10:00-10:59 退出
    11-13:30 時段：13:31 後或 14:00 後退出
    """
    hkt = get_hkt_now()
    h, m = hkt.hour, hkt.minute
    # 08-10 時段結束（10:00-10:59）
    if 10 <= h < 11:
        return True
    # 11-13:30 時段結束
    if h >= 14 or (h == 13 and m > 30):
        return True
    return False

# ========== Gemini 調用（含 429 退避重試）==========
def is_rate_limit_error(e: Exception) -> bool:
    err_str = str(e).lower()
    return any(kw in err_str for kw in [
        "429", "rate limit", "rate_limit", "resource exhausted",
        "resource_exhausted", "quota", "too many requests"
    ])

def gemini_call(prompt: str, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            chat = CLIENT.chats.create(
                model=MODEL_NAME,
                config=GEMINI_CONFIG
            )
            response = chat.send_message(prompt)
            return response.text
        except Exception as e:
            if is_rate_limit_error(e) and attempt < max_retries - 1:
                wait_sec = 60
                print(f"⚠️ 遇到 Gemini 限流 (429/quota)，等待 {wait_sec} 秒後重試 "
                      f"({attempt+1}/{max_retries})")
                time.sleep(wait_sec)
            else:
                raise

# ========== 核心掃描（去重 index 錯位 BUG 已修復）==========
def scan_once(include_macro: bool = True):
    cache = load_cache()
    pushed_set = set(cache.get("pushed", []))

    if include_macro:
        prompt = (
            SYSTEM_PROMPT
            + "\n\n【額外要求】板塊宏觀消息必須係真正重量級、足以引發整個板塊集體異動或大市急升急跌嘅消息先好輸出；"
              "如果只係普通政策、常規公開市場操作、輕微影響或市場已消化嘅消息，"
              "請直接喺板塊區塊寫「當前時段無符合條件之重大利好」，唔好勉強列出。"
        )
    else:
        prompt = (
            SYSTEM_PROMPT
            + "\n\n【重要補充指令】今次掃描**只需要輸出「=== 【個股重大利好】 ===」部分**，"
              "**唔好輸出「=== 【板塊宏觀消息】 ===」區塊**，板塊宏觀消息已經喺本次啟動第一輪掃描過，唔使重複。"
        )

    llm_result = gemini_call(prompt)
    print("=== Gemini output ===")
    print(llm_result)

    if "📰" not in llm_result:
        print("當前時段無符合條件之重大消息，唔推送飛書")
        return

    # 切分板塊同個股區塊
    macro_part = ""
    stock_part = ""
    if "=== 【個股重大利好】 ===" in llm_result:
        parts = llm_result.split("=== 【個股重大利好】 ===")
        stock_part = parts[1] if len(parts) > 1 else ""
        if "=== 【板塊宏觀消息】 ===" in parts[0]:
            macro_parts = parts[0].split("=== 【板塊宏觀消息】 ===")
            macro_part = macro_parts[1] if len(macro_parts) > 1 else ""
    elif "=== 【板塊宏觀消息】 ===" in llm_result:
        macro_parts = llm_result.split("=== 【板塊宏觀消息】 ===")
        macro_part = macro_parts[1] if len(macro_parts) > 1 else ""

    macro_block_text = macro_part.strip()

    # 個股逐條解析（以 📰 為邊界切分，每條獨立提取標題+代碼，徹底消除 index 錯位）
    filtered_stock_entries = []
    stock_entries = re.split(r'(?=📰)', stock_part.strip())

    for entry in stock_entries:
        entry = entry.strip()
        if not entry or "📰" not in entry:
            continue

        title_match = re.search(r"📰 新聞標題：([^\n]+)", entry)
        stock_match = re.search(r"🏷️ 股票：.*?(\d{4,5}\.HK)", entry, re.DOTALL)

        title = title_match.group(1).strip() if title_match else ""
        code = stock_match.group(1).strip() if stock_match else "UNKNOWN"

        key = f"{code}||{title}"
        if key not in pushed_set:
            pushed_set.add(key)
            filtered_stock_entries.append(entry)
        else:
            print(f"已過濾重複新聞: {key}")

    final_stock_text = "\n\n".join(filtered_stock_entries).strip()

    macro_has_news = "📰" in macro_block_text
    stock_has_news = len(filtered_stock_entries) > 0

    if not macro_has_news and not stock_has_news:
        print("篩選後無新發布之實質新聞，唔推送飛書")
        return

    # 組裝推送內容
    final_out = []
    if include_macro and macro_has_news:
        final_out.append("=== 【板塊宏觀消息】 ===")
        final_out.append(macro_block_text)
    if stock_has_news:
        final_out.append("=== 【個股重大利好】 ===")
        final_out.append(final_stock_text)

    final_text = "\n\n".join(final_out)
    final_text = format_links(final_text)
    send_feishu(final_text)

    cache["pushed"] = list(pushed_set)
    save_cache(cache)

# ========== 主流程 ==========
def main():
    hkt_now = get_hkt_now()
    print(f"HKT now: {hkt_now.strftime('%Y-%m-%d %H:%M:%S')}")

    if FORCE_RUN:
        print("⚠️ FORCE_RUN 模式：忽略週末同時間窗口，即時跑一次")
        run_mode = "one_shot"
    else:
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
