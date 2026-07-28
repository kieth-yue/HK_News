import json
import re
import time
import requests

SYSTEM_PROMPT = """
你是港股事件驅動對沖基金經理，專門從新聞中篩選未來48小時-2周可能引發股價大幅上漲的重大催化劑。
【核心判斷】
只有事件滿足以下至少2點才判定為重大利好：
1. 事件已正式落實，非傳聞/計劃/猜測
2. 可實質改變公司未來1-3年盈利能力
3. 可引發公司估值重估
4. 可吸引持續資金流入
5. 非一次性收益、非市場已廣泛預期
【重大催化劑分類】
超預期業績/盈喜、巨額合同、回購註銷、私有化、AI/科技合作、政策受益、新產品/技術突破、海外拓展、監管批准、超預期派息、納入指數(恆指/港股通/MSCI)、分拆上市、大額融資(利好型)
【評分規則】
95-100：極重大催化劑，單日漲幅10%+；85-94：明確重大利好；<85：忽略
【置信度規則】
95=官方公告；90=權威媒體；60=市場消息；30=傳聞
【緊急度規則】
Immediate：即日反應；1-3 Days：中线反應；Long Term：長期影響
【注意】
1. 信息不足一律返回false，不要猜測
2. 無股價數據時price_in="Unknown"，不要亂判
3. 嚴格返回JSON數組，順序同輸入新聞順序完全一致，不要額外解釋
【輸出格式】
返回JSON數組，每個元素字段：
{
  "is_major_bullish": true/false, "score": 0-100,
  "category": "利好分類", "confidence": 0-100,
  "reason": "80字以內中文點評", "risk": "低/中/高",
  "price_in": "Unknown", "urgency": "Immediate/1-3 Days/Long Term"
}
"""

RISK_BATCH_PROMPT = """
你是港股風控分析師，按編號順序分析每組新聞對應股票的潛在風險，每個風險提示40字以內。
【重大利空（必須標註🔴）】：配股/供股抽水、立案調查、財務造假、盈警、大股東減持>5%、退市風險、重大處罰
【輕微利空（標註🟡）】：小股東減持、行業政策小幅收緊、業績略遜預期、短期波動風險
【無明顯利空】：直接寫「近期未見明顯利空」
注意：不要過度解讀，有事實先講，冇就寫無，不要猜測。嚴格返回JSON數組，順序同輸入完全一致，格式：
[{"risk_warning": "風險提示內容"}, ...]
"""

def _extract_json(text):
    """增強版 JSON 提取，機能相容單一 Dict 與 Array 結構"""
    if not text:
        return None
        
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()
    
    # 策略 1：直接完整解析 JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
        
    # 策略 2：正則擷取 [...] 陣列
    arr_match = re.search(r'\[.*?\]', text, re.DOTALL)
    if arr_match:
        try:
            return json.loads(arr_match.group())
        except Exception:
            pass
            
    # 策略 3：正則擷取單一 {...} 物件並包裝成陣列
    dict_match = re.search(r'\{.*?\}', text, re.DOTALL)
    if dict_match:
        try:
            return [json.loads(dict_match.group())]
        except Exception:
            pass
            
    return None

def _call_siliconflow_with_backoff(api_key, news_batch, system_prompt):
    """SiliconFlow GLM-Z1 API調用，具備 Retry、Backoff 長度校驗（替換原本Gemini函數）"""
    prompt_parts = ["請按編號順序分析以下內容，嚴格按順序返回JSON數組：\n"]
    for idx, news in enumerate(news_batch, 1):
        stock_name = news.get("target_name", news.get("stock_name", "未知股票"))
        stock_code = news.get("target_code", news.get("stock_code", "00000"))
        pub_time = news['pub_time'].strftime('%Y-%m-%d %H:%M') if hasattr(news['pub_time'], 'strftime') else str(news['pub_time'])
        negative_tip = "\n⚠️ 注意：該新聞包含負面關鍵詞，請仔細判斷是否為「扭虧為盈」等邊界利好。" if news.get("negative_hint") else ""
        prompt_parts.append(f"""
=== 編號{idx} ===
股票：{stock_name} ({stock_code})
來源：{news['source']}
時間：{pub_time}
標題：{news['title']}
摘要：{news['summary'] if news['summary'] else '無摘要'}
{negative_tip}
""")
    prompt = "\n".join(prompt_parts)
    backoff_times = [60, 120]
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "THUDM/GLM-Z1-9B-0414",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"}
    }

    for wait_time in backoff_times:
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=90)
            if resp.status_code == 429:
                print(f"[限流] SiliconFlow額度限制，等待{wait_time}秒重試...")
                time.sleep(wait_time)
                continue
            resp.raise_for_status()
            data = resp.json()
            ai_text = data["choices"][0]["message"]["content"]
            result = _extract_json(ai_text)
            
            if result and isinstance(result, list):
                if len(result) == len(news_batch):
                    return result
                else:
                    raise ValueError(f"AI返回長度不符 (返回{len(result)}，期望{len(news_batch)})")
            else:
                raise ValueError("無法解析有效的 JSON Array")
                
        except Exception as e:
            err_msg = str(e)[:120]
            if "429" in err_msg or "quota" in err_msg.lower():
                print(f"[限流] 額度受限，等待{wait_time}秒後重試...")
                time.sleep(wait_time)
            else:
                print(f"[重試] 調用或解析失敗，等待2秒重試: {err_msg}")
                time.sleep(2)
                
    if "風控" in system_prompt:
        return [{"risk_warning": "⚠️ 風險分析失敗，請自行查證"} for _ in news_batch]
    return [{"is_major_bullish": False, "score": 0} for _ in news_batch]

def analyze_news_batch(news_list, all_news, api_key, threshold=85):
    """批量新聞分析主流程【已切換SiliconFlow】"""
    valid_bullish = []
    total = len(news_list)
    batch_size = 35
    
    for batch_idx in range(0, total, batch_size):
        batch = news_list[batch_idx:batch_idx+batch_size]
        batch_start = batch_idx + 1
        batch_end = min(batch_idx + batch_size, total)
        print(f"AI利好分析進度: [{batch_start}-{batch_end}/{total}] ...")
        
        # 替換調用函數
        batch_results = _call_siliconflow_with_backoff(api_key, batch, SYSTEM_PROMPT)
        for news, result in zip(batch, batch_results):
            result.setdefault("is_major_bullish", False)
            result.setdefault("score", 0)
            result.setdefault("category", "其他")
            result.setdefault("confidence", 0)
            result.setdefault("reason", "")
            result.setdefault("risk", "高")
            result.setdefault("price_in", "Unknown")
            result.setdefault("urgency", "Long Term")
            result["title"] = news["title"]
            result["link"] = news["link"]
            result["source"] = news["source"]
            result["target_code"] = news["target_code"]
            result["target_name"] = news["target_name"]
            result["pub_time"] = news["pub_time"].strftime("%Y-%m-%d %H:%M") if hasattr(news["pub_time"], 'strftime') else str(news["pub_time"])
            
            if result["is_major_bullish"] and result["score"] >= threshold and result["confidence"] >= 70:
                valid_bullish.append(result)
                print(f"✅ 發現重大利好: {result['target_name']} {result['score']}分 {result['category']} | 緊急度:{result['urgency']}")
        time.sleep(6)

    # 按股票代碼去重，同隻股票保留最高分新聞
    bullish_dict = {}
    for item in valid_bullish:
        code = item["target_code"]
        if code not in bullish_dict or item["score"] > bullish_dict[code]["score"]:
            bullish_dict[code] = item
    valid_bullish = list(bullish_dict.values())
    print(f"✅ 去重後有效利好: {len(valid_bullish)} 隻股票")

    # 進行風險預警批量分析
    if valid_bullish:
        print("="*50)
        print(f"開始對{len(valid_bullish)}隻利好股票做批量風險分析...")
        print("="*50)
        risk_input = []
        for bull in valid_bullish:
            code = bull["target_code"]
            related_news = [n for n in all_news if n.get("stock_code") == code]
            if related_news:
                related_news.sort(key=lambda x: x["pub_time"], reverse=True)
                risk_input.append(related_news[0])
            else:
                risk_input.append({
                    "stock_name": bull["target_name"],
                    "stock_code": code,
                    "source": "系統",
                    "pub_time": bull["pub_time"],
                    "title": "近期無相關新聞",
                    "summary": ""
                })
        
        risk_results = _call_siliconflow_with_backoff(api_key, risk_input, RISK_BATCH_PROMPT)
        for i, bull in enumerate(valid_bullish):
            if i < len(risk_results):
                bull["risk_warning"] = risk_results[i].get("risk_warning", "⚠️ 風險分析失敗，請自行查證")
            else:
                bull["risk_warning"] = "⚠️ 風險分析失敗，請自行查證"
            print(f"⚠️  {bull['target_name']} 風險: {bull['risk_warning']}")
        time.sleep(6)

    urgency_order = {"Immediate": 0, "1-3 Days": 1, "Long Term": 2}
    valid_bullish.sort(key=lambda x: (urgency_order.get(x["urgency"], 3), -x["score"]))
    print(f"AI分析完成，符合條件重大利好: {len(valid_bullish)} 條")
    return valid_bullish
