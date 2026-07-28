import requests
import feedparser
import time
import random
import re
import os
from datetime import datetime, timedelta, timezone

# 讀取環境變數（沿用舊Secret名 YAHOO_RSS）
YAHOO_RSS_BASE = os.environ["YAHOO_RSS"]

# 全局請求頭【升級仿真瀏覽器UA，改善被封問題】
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
    "Accept": "application/rss+xml,application/xml;q=0.9,text/xml,*/*;q=0.8",
    "Referer": "https://finance.yahoo.com/",
    "Cache-Control": "no-cache"
}

NEGATIVE_HINT_ZH = ["盈警", "虧損", "預虧", "業績倒退", "純利跌", "減持", "配股", "供股", "抽水", "攤薄", "批股", "處罰", "罰款", "召回", "制裁", "破產", "清盤", "除牌", "停牌", "調查", "起訴", "訴訟", "造假", "欺詐", "暴跌", "大跌", "下調", "降級"]
NEGATIVE_HINT_EN = ["profit warning", "loss", "net loss", "share placement", "dilution", "sanction", "bankruptcy", "delisting", "suspend", "fine", "penalty"]
NEGATIVE_EN_PATTERN = re.compile(r'\b(' + '|'.join(re.escape(w) for w in NEGATIVE_HINT_EN) + r')\b', re.IGNORECASE)

COMMON_PREFIX = ["中國", "中国", "香港", "國際", "国际", "環球", "环球", "亞洲", "亚洲", "遠東", "远东"]

STOCK_ALIAS = {
    "00700": ["騰訊", "腾讯", "Tencent", "騰訊控股"],
    "01810": ["小米", "小米集團", "Xiaomi", "小米集團-W"],
    "09988": ["阿里", "阿里巴巴", "Alibaba", "阿里巴巴-W"],
    "03690": ["美團", "美团", "Meituan", "美團-W"],
    "01024": ["快手", "快手科技", "Kuaishou", "快手-W"],
    "09618": ["京東", "京东", "JD.com", "JD", "京東集團-SW"],
    "00941": ["移動", "中國移動", "China Mobile"],
    "00005": ["匯豐", "滙豐", "HSBC", "匯豐控股"],
    "00883": ["中海油", "中國海洋石油", "CNOOC"],
    "02318": ["平保", "中國平安", "Ping An", "平安"],
    "01398": ["工行", "工商銀行", "ICBC"],
    "00388": ["港交所", "香港交易所", "HKEX"],
    "00001": ["長和", "CK Hutchison", "長江和記"],
    "00002": ["中電", "中電控股", "CLP Holdings"],
    "00016": ["新鴻基", "新鴻基地產", "SHK Properties"]
}

def _clean_html(raw_text):
    if not raw_text:
        return ""
    clean = re.sub(r'<[^>]+>', '', raw_text)
    clean = re.sub(r'\s+', ' ', clean).strip()
    clean = clean.replace("Read more", "").replace("閱讀更多", "").replace("繼續閱讀", "").replace("...", "").strip()
    return clean[:500]

def _sleep(min_t=1.2, max_t=2.2):
    time.sleep(random.uniform(min_t, max_t))

def _parse_publish_time(entry):
    try:
        if hasattr(entry, 'published_parsed') and entry.published_parsed:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    except:
        pass
    return datetime.now(timezone.utc)

def _generate_match_keywords(stock):
    code = stock["code"]
    name = stock["name"]
    keywords_zh = set()
    keywords_en = set()
    pure_code = code.replace("HK.", "").replace("hk.", "").strip()
    code_pattern = re.compile(r'(?<!\d)(0*' + re.escape(pure_code[-4:]) + r'|' + re.escape(pure_code) + r')(?!\d)(\.HK)?', re.IGNORECASE)
    keywords_zh.add(name)
    core_name = name
    for prefix in COMMON_PREFIX:
        if core_name.startswith(prefix):
            core_name = core_name[len(prefix):]
            break
    if len(core_name) >= 2:
        keywords_zh.add(core_name)
    if pure_code in STOCK_ALIAS:
        for alias in STOCK_ALIAS[pure_code]:
            if re.search(r'[a-zA-Z]', alias):
                keywords_en.add(alias.lower())
            else:
                keywords_zh.add(alias)
    return {"code_pattern": code_pattern, "zh": list(keywords_zh), "en": list(keywords_en)}

# 轉換股票代碼：HK.00700 → 00700.HK
def _build_yahoo_symbol(stock):
    code = stock["code"]
    pure_code = code.replace("HK.", "").replace("hk.", "").strip()
    yahoo_symbol = f"{pure_code}.HK"
    return yahoo_symbol

def _fetch_single_rss(url, source_name):
    news_list = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = _clean_html(entry.get("summary", entry.get("description", "")))
            pub_time = _parse_publish_time(entry)
            if not title or not link:
                continue
            news_list.append({
                "title": title, "link": link, "summary": summary,
                "source": source_name, "pub_time": pub_time, "negative_hint": False
            })
        _sleep()
    except Exception as e:
        print(f"[警告] 抓取 {source_name} 失敗: {str(e)[:80]}")
    return news_list

def fetch_yahoo_rss(stock):
    symbol = _build_yahoo_symbol(stock)
    # 拼接完整URL，對應 feeds.finance.yahoo.com 個股RSS
    url = f"{YAHOO_RSS_BASE}{symbol}&region=HK&lang=zh-Hant-HK"
    return _fetch_single_rss(url, "Yahoo RSS")

def fetch_all_stock_rss(stock_list, config=None):
    if config is None:
        config = {
            "enable_yahoo_rss": True,
            "crawl_batch_size": 10,
            "batch_sleep_min": 2,
            "batch_sleep_max": 5
        }
    all_news = []
    seen_links = set()
    time_threshold = datetime.now(timezone.utc) - timedelta(hours=48)
    total = len(stock_list)

    shuffled_stocks = stock_list.copy()
    random.shuffle(shuffled_stocks)
    batch_size = config.get("crawl_batch_size", 10)
    batch_sleep_min = config.get("batch_sleep_min", 2)
    batch_sleep_max = config.get("batch_sleep_max", 5)

    for idx, stock in enumerate(shuffled_stocks):
        code = stock["code"]
        name = stock["name"]
        print(f"[{idx+1}/{total}] 正在抓取 {name}({code}) Yahoo RSS新聞...")

        yahoo_news = fetch_yahoo_rss(stock) if config.get("enable_yahoo_rss", True) else []

        for news in yahoo_news:
            if news["pub_time"] < time_threshold:
                continue
            if news["link"] in seen_links:
                continue
            seen_links.add(news["link"])
            news["stock_code"] = code
            news["stock_name"] = name
            all_news.append(news)

        # 每隻股票爬完短暫休息
        _sleep(0.3, 0.8)

        if (idx + 1) % batch_size == 0 and (idx + 1) != total:
            batch_wait = random.randint(batch_sleep_min, batch_sleep_max)
            print(f"⏸️  完成一批{batch_size}隻股票，休息{batch_wait}秒再繼續...")
            time.sleep(batch_wait)

    print(f"✅ Yahoo RSS抓取完成，原始有效新聞: {len(all_news)} 條")
    return all_news

def match_news_to_stocks(all_news, stock_list):
    matched = []
    seen_match = set()
    negative_hint_count = 0
    stock_rules = {}
    for stock in stock_list:
        stock_rules[stock["code"]] = {"stock": stock, "rules": _generate_match_keywords(stock)}

    for news in all_news:
        title = news["title"]
        summary = news["summary"]
        content_zh = f"{title} {summary}"
        content_en = content_zh.lower()
        news_link = news["link"]

        is_negative = False
        for bad in NEGATIVE_HINT_ZH:
            if bad in content_zh:
                is_negative = True
                break
        if not is_negative and NEGATIVE_EN_PATTERN.search(content_en):
            is_negative = True
        if is_negative:
            negative_hint_count +=1
        news["negative_hint"] = is_negative

        for code, info in stock_rules.items():
            match_key = f"{code}_{news_link}"
            if match_key in seen_match:
                continue
            rules = info["rules"]
            matched_flag = False

            if rules["code_pattern"].search(content_en):
                matched_flag = True
            else:
                for kw in rules["zh"]:
                    if kw in content_zh:
                        matched_flag = True
                        break
                if not matched_flag:
                    for kw in rules["en"]:
                        if re.search(r'\b' + re.escape(kw) + r'\b', content_en):
                            matched_flag = True
                            break

            if matched_flag:
                news_copy = news.copy()
                news_copy["target_code"] = code
                news_copy["target_name"] = info["stock"]["name"]
                matched.append(news_copy)
                seen_match.add(match_key)

    print(f"✅ 關鍵詞匹配完成，送入AI分析新聞: {len(matched)} 條")
    print(f"⚠️  包含負面提示詞需AI重點判斷: {negative_hint_count} 條")
    return matched
