import os
import json
import sqlite3
import re
import urllib.request
import urllib.error
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ================= 填入你的 Token 與 Secret =================
LINE_CHANNEL_SECRET = '551273045ea1be5721345edf4196aec7'
LINE_CHANNEL_ACCESS_TOKEN = 'ftDqy1HYMrkLC/YX5uSh+9Pcq8Sk8bRcpn7vLbquj96GqzdJNhpxuybYD5DaCGtThb4fot7pctmHHgkAfpOzyqbN5vT/y5wSRcQpHtOZ6j5+k7bwhvZTXqVubSaiSFdJlVw3yZXQJlE/hU3N4p9gpQdB04t89/1O/w1cDnyilFU='
# ===========================================================

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def init_db():
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tracked_items_v2 (
            user_id TEXT,
            item_id TEXT,
            brand TEXT,
            name_tw TEXT,
            name_jp TEXT,
            price_tw REAL,
            price_jp REAL,
            PRIMARY KEY (user_id, item_id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# 動態抓取台日即時匯率
def get_jpy_rate():
    try:
        req = urllib.request.Request(
            "https://rate.bot.com.tw/xrt/flats/003/day",
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            html = response.read().decode('utf-8')
            lines = html.splitlines()
            for line in lines:
                if "JPY" in line:
                    parts = line.split(',')
                    rate = float(parts[12]) if len(parts) > 12 else float(parts[2])
                    if rate > 0:
                        return rate
    except Exception as e:
        print(f"Fetch exchange rate error: {e}")
    return 0.22

# 解析 API JSON
def parse_uniqlo_json(res_data):
    result = res_data.get('result', {})
    if not result:
        return None, None
        
    if 'items' in result and isinstance(result['items'], list) and len(result['items']) > 0:
        result = result['items'][0]

    name = result.get('name') or result.get('productName')
    price_val = None

    if 'minPrice' in result and result['minPrice'] is not None:
        price_val = float(result['minPrice'])
    elif 'prices' in result and isinstance(result['prices'], dict):
        prices = result['prices']
        for k in ['promo', 'base', 'original']:
            if k in prices and isinstance(prices[k], dict) and prices[k].get('value') is not None:
                price_val = float(prices[k]['value'])
                break

    return name, price_val

# 三重強效抓取 (API 直連 -> API 搜尋 -> 網頁備援)
def fetch_uniqlo_official(item_id, region="tw"):
    clean_id = item_id.strip()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7' if region == 'tw' else 'ja-JP,ja;q=0.9,en-US;q=0.8'
    }

    # 1. 嘗試 API 抓取
    api_urls = [
        f"https://www.uniqlo.com/{region}/api/commerce/v5/{region}/products/{clean_id}?priceCode=L2",
        f"https://www.uniqlo.com/{region}/api/commerce/v5/{region}/products?query={clean_id}&limit=1"
    ]
    for url in api_urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=4) as response:
                if response.status == 200:
                    res_data = json.loads(response.read().decode('utf-8'))
                    name, price = parse_uniqlo_json(res_data)
                    if name and price:
                        return name, price
        except Exception:
            pass

    # 2. 終極備援：直接解析網頁 HTML
    try:
        web_url = f"https://www.uniqlo.com/tw/zh_TW/product-detail.html?productCode={clean_id}" if region == "tw" else f"https://www.uniqlo.com/jp/ja/products/{clean_id}"
        req = urllib.request.Request(web_url, headers=headers)
        with urllib.request.urlopen(req, timeout=4) as response:
            html = response.read().decode('utf-8')
            
            # 從 Open Graph meta 標籤抓取商品名稱
            name_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
            # 從內容抓取價格數字
            price_match = re.search(r'NT\$\s*([\d,]+)', html) if region == "tw" else re.search(r'¥\s*([\d,]+)', html)

            name = name_match.group(1).split('|')[0].strip() if name_match else None
            price = float(price_match.group(1).replace(',', '')) if price_match else None

            if name or price:
                return name or f"Uniqlo 商品 ({clean_id})", price
    except Exception as e:
        print(f"Web scraper fallback failed ({region}-{item_id}): {e}")

    return None, None

# 整合台日兩地資料
def get_combined_info(item_id):
    name_tw, price_tw = fetch_uniqlo_official(item_id, "tw")
    name_jp, price_jp = fetch_uniqlo_official(item_id, "jp")

    display_tw = name_tw if name_tw else name_jp
    display_jp = name_jp if name_jp else name_tw

    brand = None
    if price_tw is not None or price_jp is not None or display_tw is not None:
        brand = "UNIQLO"

    return brand, display_tw, price_tw, display_jp, price_jp

# 格式化日幣轉台幣顯示
def format_jp_price(price_jp, rate):
    if price_jp is None:
        return "🇯🇵 日本：未發售 / 無資料"
    twd_approx = round(price_jp * rate)
    return f"🇯🇵 日本：¥ {int(price_jp)} (約 NT$ {twd_approx})"

@app.route("/")
def home():
    return "電商台日價格追蹤 Bot 運作中！", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK', 200

@app.route("/check_prices", methods=['GET', 'POST'])
def check_prices():
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, item_id, brand, name_tw, name_jp, price_tw, price_jp FROM tracked_items_v2")
    rows = cursor.fetchall()

    updated_count, notifications = 0, 0
    rate = get_jpy_rate()

    for user_id, item_id, brand, old_name_tw, old_name_jp, old_p_tw, old_p_jp in rows:
        c_brand, c_name_tw, c_p_tw, c_name_jp, c_p_jp = get_combined_info(item_id)
        if c_p_tw is not None or c_p_jp is not None:
            drops = []
            if old_p_tw and c_p_tw and c_p_tw < old_p_tw:
                drops.append(f"🇹🇼 台灣特價: NT$ {int(c_p_tw)} (原價 NT$ {int(old_p_tw)})")
            if old_p_jp and c_p_jp and c_p_jp < old_p_jp:
                twd_approx = round(c_p_jp * rate)
                drops.append(f"🇯🇵 日本特價: ¥ {int(c_p_jp)} (約 NT$ {twd_approx}, 原價 ¥ {int(old_p_jp)})")

            if drops:
                display_name = c_name_tw if c_name_tw else c_name_jp
                msg = (f"🎉【降價通知】🎉\n"
                       f"您追蹤的商品降價囉！\n\n"
                       f"📦 [{brand}] {display_name} ({item_id})\n" +
                       "\n".join(drops) + "\n\n"
                       f"🔗 台灣官網: https://www.uniqlo.com/tw/zh_TW/product-detail.html?productCode={item_id}\n"
                       f"🔗 日本官網: https://www.uniqlo.com/jp/ja/products/{item_id}")
                try:
                    line_bot_api.push_message(user_id, TextSendMessage(text=msg))
                    notifications += 1
                except Exception as e:
                    print(f"Push failed: {e}")

            cursor.execute('''
                UPDATE tracked_items_v2 
                SET name_tw = ?, name_jp = ?, price_tw = ?, price_jp = ? 
                WHERE user_id = ? AND item_id = ?
            ''', (c_name_tw, c_name_jp, c_p_tw, c_p_jp, user_id, item_id))
            updated_count += 1

    conn.commit()
    conn.close()
    return f"Checked {updated_count} items, sent {notifications} notifications.", 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()

    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    rate = get_jpy_rate()

    if user_msg == "清單":
        cursor.execute("SELECT item_id, brand, name_tw, name_jp, price_tw, price_jp FROM tracked_items_v2 WHERE user_id = ?", (user_id,))
        items = cursor.fetchall()
        if not items:
            reply = "📋 目前沒有追蹤任何商品。\n輸入 `+貨號`（例如 `+465185`）即可開始追蹤！"
        else:
            reply = "📋 您目前追蹤的商品清單：\n"
            for item_id, brand, n_tw, n_jp, p_tw, p_jp in items:
                reply += f"\n• [{brand}] {item_id}\n"
                if n_tw: reply += f"  🇹🇼 中文: {n_tw}\n"
                if n_jp: reply += f"  🇯🇵 日文: {n_jp}\n"
                
                prices_str = []
                if p_tw: prices_str.append(f"NT$ {int(p_tw)}")
                if p_jp: 
                    twd = round(p_jp * rate)
                    prices_str.append(f"¥ {int(p_jp)} (約 NT$ {twd})")
                reply += f"  💰 價格: {' / '.join(prices_str)}\n"
            reply += "\n每天系統會自動檢查台日兩地的價格變動！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    elif user_msg.startswith("+"):
        item_id = user_msg[1:].strip()
        if item_id.isdigit():
            brand, n_tw, p_tw, n_jp, p_jp = get_combined_info(item_id)
            if brand:
                cursor.execute('''
                    INSERT OR REPLACE INTO tracked_items_v2 
                    (user_id, item_id, brand, name_tw, name_jp, price_tw, price_jp) 
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (user_id, item_id, brand, n_tw, n_jp, p_tw, p_jp))
                conn.commit()

                reply = f"✅ 已成功加入追蹤！\n\n🏷️ 貨號：{item_id}\n"
                if n_tw: reply += f"🇹🇼 中文：{n_tw}\n"
                if n_jp: reply += f"🇯🇵 日文：{n_jp}\n"
                reply += "\n💰 目前價格：\n"
                reply += f"🇹🇼 台灣：NT$ {int(p_tw)}\n" if p_tw else "🇹🇼 台灣：未發售 / 無資料\n"
                reply += format_jp_price(p_jp, rate) + "\n"
                reply += "\n任一地區價格調降時，我都會主動通知您！"
            else:
                reply = f"❌ 找不到貨號 `{item_id}` 的商品，請確認貨號是否正確。"
        else:
            reply = "💡 請使用正確格式：`+貨號`（例如 `+465185`）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    elif user_msg.startswith("-"):
        item_id = user_msg[1:].strip()
        if item_id.isdigit():
            cursor.execute("DELETE FROM tracked_items_v2 WHERE user_id = ? AND item_id = ?", (user_id, item_id))
            conn.commit()
            reply = f"🗑️ 已停止追蹤貨號 `{item_id}` 的商品。"
        else:
            reply = "💡 請使用正確格式：`-貨號`（例如 `-465185`）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    elif user_msg.isdigit():
        item_id = user_msg
        brand, n_tw, p_tw, n_jp, p_jp = get_combined_info(item_id)
        if brand:
            reply = f"🔍 查價結果 (貨號：{item_id})\n\n"
            if n_tw: reply += f"🇹🇼 中文：{n_tw}\n"
            if n_jp: reply += f"🇯🇵 日文：{n_jp}\n"
            reply += "\n💰 目前價格：\n"
            reply += f"🇹🇼 台灣：NT$ {int(p_tw)}\n" if p_tw else "🇹🇼 台灣：未發售 / 無資料\n"
            reply += format_jp_price(p_jp, rate) + "\n"
            reply += f"\n💡 如需追蹤價格變動，請輸入 `+{item_id}`"
        else:
            reply = f"❌ 找不到貨號 `{item_id}` 的商品，請確認貨號是否正確。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    else:
        reply = "🤖 可用指令：\n• 直接輸入 `貨號`（如 `465185`）：快速查價\n• 輸入 `+貨號`（如 `+465185`）：加入降價追蹤\n• 輸入 `-貨號`（如 `-465185`）：取消追蹤\n• 輸入 `清單`：查看所有追蹤商品"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    conn.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
