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

# ========== Gemini 全局初始化（180秒超時）==========
CLIENT = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=180000)
)
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
    """將超長 URL 轉成短連結；冇 URL 嘅空連結行直接移除"""
    def replacer(m):
        urls = re.findall(r'https?://\S+', m.group(0))
        if urls:
            return f"🔗 連結：[點擊查看]({urls[0]})"
        return ""
    pattern = r'🔗 連結：[\s\S]*?(?=\n[💡🏷️📰⏰📌]|\n===|\Z)'
    text = re.sub(pattern, replacer, text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def append_grounding_source(text: str, grounding_urls: list) -> str:
    """如果文本冇任何連結，附加一個 Gemini 聯網搜尋來源 URL（確保有來源可追溯，防幻覺）"""
    if not grounding_urls:
        return text
    if "點擊查看" in text or "http://" in text or "https://" in text:
        return text
    title, uri = grounding_urls[0]
    clean_title = (title or "來源")[:40]
    text += f"\n\n---\n📎 參考來源：[{clean_title}]({uri})"
    return text

def normalize_stock_code(raw_code: str) -> str:
    """將 HK.09988 或 09988.HK 統一標準化為 09988.HK"""
    digits_match = re.search(r"\d{4,5}", raw_code)
    if digits_match:
        return f"{digits_match.group(0)}.HK"
    return "UNKNOWN"

# ========== 鎖與快取 ==========
def load_cache():
    if not os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"pushed": []}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"創建緩存文件失敗: {e}")
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
    if 8 <= h < 10:
        return "long_run"
    if (h == 11) or (h == 12) or (h == 13 and m <= 30):
        return "long_run"
    if (h == 15 and m <= 5) or (h == 15 and 45 <= m <= 55) or (h == 22 and m <= 5):
        return "one_shot"
    return "none"

def is_long_run_time_over():
    hkt = get_hkt_now()
    h, m = hkt.hour, hkt.minute
    if 10 <= h < 11:
        return True
    if h >= 14 or (h == 13 and m > 30):
        return True
    return False

# ========== Gemini 調用（含超時 + 429重試 + grounding URL 提取）==========
def is_retryable_error(e: Exception) -> bool:
    err_str = str(e).lower()
    return any(kw in err_str for kw in [
        "429", "rate limit", "rate_limit", "resource exhausted",
        "resource_exhausted", "quota", "too many requests",
        "timeout", "timed out", "deadline exceeded", "503", "502", "500"
    ])

def extract_grounding_urls(response) -> list:
    """從 Gemini response 嘅 grounding metadata 提取聯網搜尋來源 URL"""
    urls = []
    try:
        if not response.candidates:
            return urls
        candidate = response.candidates[0]
        metadata = getattr(candidate, 'grounding_metadata', None)
        if not metadata:
            return urls
        chunks = getattr(metadata, 'grounding_chunks', None)
        if not chunks:
            return urls
        for chunk in chunks:
            web = getattr(chunk, 'web', None)
            if web:
                uri = getattr(web, 'uri', '')
                title = getattr(web, 'title', '來源')
                if uri:
                    urls.append((title, uri))
    except Exception as e:
        print(f"提取 grounding URL 失敗: {e}")
    return urls

def gemini_call(prompt: str, max_retries: int = 3):
    """返回 (text, grounding_urls)"""
    for attempt in range(max_retries):
        try:
            chat = CLIENT.chats.create(
                model=MODEL_NAME,
                config=GEMINI_CONFIG
            )
            response = chat.send_message(prompt)
            grounding_urls = extract_grounding_urls(response)
            return response.text, grounding_urls
        except Exception as e:
            if is_retryable_error(e) and attempt < max_retries - 1:
                wait_sec = 60
                print(f"⚠️ Gemini 調用失敗（429/超時/暫時性錯誤），等待 {wait_sec} 秒後重試 "
                      f"({attempt+1}/{max_retries}): {str(e)[:100]}")
                time.sleep(wait_sec)
            else:
                raise

# ========== 核心掃描 ==========
def scan_once(include_macro: bool = True, macro_pushed_set=None):
    cache = load_cache()
    pushed_set = set(cache.get("pushed", []))

    if include_macro:
        prompt = SYSTEM_PROMPT + "\n\n【當前掃描模式：模式A — 首輪/定時掃描，請完整輸出板塊+個股】"
    else:
        prompt = SYSTEM_PROMPT + "\n\n【當前掃描模式：模式B — 盤中輪詢掃描，重點輸出個股；僅突發黑天鵝先出板塊】"

    llm_result, grounding_urls = gemini_call(prompt)
    print("=== Gemini output ===")
    print(llm_result)
    if grounding_urls:
        print(f"=== Grounding 來源: {len(grounding_urls)} 個 URL ===")

    # 檢查有冇實質內容
    has_news_emoji = "📰" in llm_result
    has_macro_section = "【板塊宏觀消息】" in llm_result
    has_stock_section = "【個股重大利好" in llm_result
    has_section = has_macro_section or has_stock_section
    explicit_no_news = any(kw in llm_result for kw in [
        "無符合條件", "冇符合條件", "无符合条件",
        "無具催化力", "无具催化力",
        "沒有符合條件", "没有符合条件"
    ])

    if not has_news_emoji and not has_section:
        print("無任何新聞內容，唔推送飛書")
        return

    if explicit_no_news and not has_news_emoji:
        print("當前時段無符合條件之重大消息，唔推送飛書")
        return

    # 兜底：有區塊標題但冇 📰（Gemini 冇跟格式），提取內容原樣推送
    if has_section and not has_news_emoji:
        print("⚠️ Gemini 冇跟從輸出格式，提取內容原樣推送")
        start_candidates = []
        if has_macro_section:
            start_candidates.append(llm_result.find("【板塊宏觀消息】"))
        if has_stock_section:
            start_candidates.append(llm_result.find("【個股重大利好"))
        start_idx = min(i for i in start_candidates if i >= 0)
        raw_content = llm_result[start_idx:]
        raw_content = re.sub(r'\n*當前時段[^。\n]*(?:之重大利好|之板塊消息)\s*$', '', raw_content).strip()
        if raw_content:
            raw_content = format_links(raw_content)
            raw_content = append_grounding_source(raw_content, grounding_urls)
            send_feishu(raw_content)
        else:
            print("兜底提取後內容為空，唔推送")
        return

    # 正常格式解析
    macro_part = ""
    stock_part = ""
    if "=== 【個股重大利好" in llm_result:
        split_marker = "=== 【個股重大利好/異動】 ===" if "=== 【個股重大利好/異動】 ===" in llm_result else "=== 【個股重大利好】 ==="
        parts = llm_result.split(split_marker)
        stock_part = parts[1] if len(parts) > 1 else ""
        if "=== 【板塊宏觀消息】 ===" in parts[0]:
            macro_parts = parts[0].split("=== 【板塊宏觀消息】 ===")
            macro_part = macro_parts[1] if len(macro_parts) > 1 else ""
    elif "=== 【板塊宏觀消息】 ===" in llm_result:
        macro_parts = llm_result.split("=== 【板塊宏觀消息】 ===")
        macro_part = macro_parts[1] if len(macro_parts) > 1 else ""

    # 板塊消息逐條解析 + 同一 job 內去重
    macro_entries = []
    if macro_part.strip() and "📰" in macro_part:
        macro_raw_entries = re.split(r'(?=📰)', macro_part.strip())
        for entry in macro_raw_entries:
            entry = entry.strip()
            if not entry or "📰" not in entry:
                continue
            title_match = re.search(r"📰 新聞標題：([^\n]+)", entry)
            title = title_match.group(1).strip() if title_match else entry[:50]
            if macro_pushed_set is not None:
                if title in macro_pushed_set:
                    print(f"已過濾重複板塊消息: {title}")
                    continue
                macro_pushed_set.add(title)
            macro_entries.append(entry)

    final_macro_text = "\n\n".join(macro_entries).strip()
    macro_has_news = len(macro_entries) > 0

    # 個股逐條解析 + 跨 job 持久化去重
    filtered_stock_entries = []
    stock_entries = re.split(r'(?=📰)', stock_part.strip())

    for entry in stock_entries:
        entry = entry.strip()
        if not entry or "📰" not in entry:
            continue

        title_match = re.search(r"📰 新聞標題：([^\n]+)", entry)
        stock_match = re.search(r"🏷️ 股票：.*?(HK\.\d{4,5}|\d{4,5}\.HK)", entry, re.DOTALL)

        title = title_match.group(1).strip() if title_match else ""
        code = normalize_stock_code(stock_match.group(1)) if stock_match else "UNKNOWN"

        key = f"{code}||{title}"
        if key not in pushed_set:
            pushed_set.add(key)
            filtered_stock_entries.append(entry)
        else:
            print(f"已過濾重複新聞: {key}")

    final_stock_text = "\n\n".join(filtered_stock_entries).strip()
    stock_has_news = len(filtered_stock_entries) > 0

    if not macro_has_news and not stock_has_news:
        print("篩選後無新發布之實質新聞，唔推送飛書")
        return

    # 組裝推送內容
    final_out = []
    if macro_has_news:
        final_out.append("=== 【板塊宏觀消息】 ===")
        final_out.append(final_macro_text)
    if stock_has_news:
        final_out.append("=== 【個股重大利好】 ===")
        final_out.append(final_stock_text)

    final_text = "\n\n".join(final_out)
    final_text = format_links(final_text)
    final_text = append_grounding_source(final_text, grounding_urls)
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
            print("長駐模式：第一輪跑板塊+個股，後續輪次重點跑個股（突發黑天鵝仍會出板塊警報）")
            first_round = True
            macro_pushed = set()
            while True:
                if is_long_run_time_over():
                    print("已到長駐結束時間，退出迴圈，job完結")
                    break
                try:
                    scan_once(include_macro=first_round, macro_pushed_set=macro_pushed)
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
