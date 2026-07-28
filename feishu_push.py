import requests
from datetime import datetime, timedelta, timezone

def send_feishu_card(news_list, webhook_url):
    unique_news = []
    seen = set()
    for news in news_list:
        key = f"{news['target_code']}_{news['title']}"
        if key not in seen:
            seen.add(key)
            unique_news.append(news)
    news_list = unique_news

    # 計算香港時間（UTC+8）
    hk_tz = timezone(timedelta(hours=8))
    push_time = datetime.now(hk_tz).strftime("%Y-%m-%d %H:%M")

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**📢 本輪掃描發現 {len(news_list)} 隻股票重大利好催化劑**"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"⏰ 推送時間：**{push_time}** 香港時間"}},
        {"tag": "hr"}
    ]

    for idx, news in enumerate(news_list, 1):
        price_in_text = {
            "Low": "🟢 低（未炒作）",
            "Medium": "🟡 中（部分反應）",
            "High": "🔴 高（已炒高）",
            "Unknown": "⚪ 未知（無股價數據）"
        }.get(news["price_in"], "⚪ 未知")
        risk_text = {"低": "🟢 低風險", "中": "🟡 中風險", "高": "🔴 高風險"}.get(news["risk"], "🟡 中風險")
        urgency_text = {"Immediate": "⚡ 即日反應", "1-3 Days": "📅 1-3日", "Long Term": "⏳ 長期"}.get(news["urgency"], "📅 1-3日")
        risk_warning = news.get("risk_warning", "近期未見明顯利空")
        content = f"""
**{idx}. {news['target_name']} ({news['target_code']})**
> ✅ 利好分類：{news['category']}
> ⭐ 利好評分：**{news['score']}/100**
> ⚡ 緊急度：{urgency_text}
> 🎯 置信度：{news['confidence']}%
> 📈 預期消化：{price_in_text}
> ⚠️ 風險等級：{risk_text}
> 🚨 風險提示：{risk_warning}
> 📝 點評：{news['reason']}
> 🔗 來源：{news['source']} | [查看新聞]({news['link']})
        """
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content.strip()}})
        elements.append({"tag": "hr"})

    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "⚠️ 以上僅為事件驅動提醒，不構成任何投資建議，交易請自行判斷風險"}]})

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🚀 港股重大利好提醒"},
                "template": "purple"
            },
            "elements": elements
        }
    }
    try:
        resp = requests.post(webhook_url, json=card, timeout=10)
        resp.raise_for_status()
        print("飛書卡片推送成功")
        return True
    except Exception as e:
        print(f"飛書推送失敗: {str(e)}")
        return False
