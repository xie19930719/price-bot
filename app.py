import os
import sqlite3
import requests
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

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# 動態抓取台日即時匯率
def get_jpy_rate():
    try:
        res = requests.get("https://rate.bot.com.tw/xrt/flats/003/day", timeout=3)
        if res.status_code == 200:
            lines = res.text.splitlines()
            for line in lines:
                if "JPY" in line:
                    parts = line.split(',')
                    rate = float(parts[12]) if len(parts) > 12 else float(parts[2])
                    if rate > 0:
                        return rate
    except Exception as e:
        print(f"Fetch exchange rate error: {e}")
    return 0.22

# 直接對接 uq.goodjack.tw 的產品 API
def fetch_from_goodjack(item_id):
    formatted_id = item_id.zfill(6)
    
    # 台灣與日本 Goodjack API 路線
    url_tw = f"https://uq.goodjack.tw/api/v1/products/{formatted_id}"
    url_jp = f"https://uq.goodjack.tw/api/v1/japan/products/{formatted_id}"

    name_tw, price_tw = None, None
    name_jp, price_jp = None, None

    # 1. 抓台灣 Goodjack
    try:
        res_tw = requests.get(url_tw, headers=HEADERS, timeout=5)
        if res_tw.status_code == 200:
            data = res_tw.json()
            name_tw = data.get('name') or data.get('title')
            # 取得最新一筆價格紀錄
            prices = data.get('prices', [])
            if prices and len(prices) > 0:
                price_tw = float(prices[0].get('price'))
            elif 'price' in data:
                price_tw = float(data['price'])
    except Exception as e:
        print(f"Goodjack TW API Error: {e}")

    # 2. 抓日本 Goodjack
    try:
        res_jp = requests.get(url_jp, headers=HEADERS, timeout=5)
        if res_jp.status_code == 200:
            data = res_jp.json()
            name_jp = data.get('name') or data.get('title')
            prices = data.get('prices', [])
            if prices and len(prices) > 0:
                price_jp = float(prices[0].get('price'))
            elif 'price' in data:
                price_jp = float(data['price'])
    except Exception as e:
        print(f"Goodjack JP API Error: {e}")

    # 交叉補全品名
    display_tw = name_tw if name_tw else name_jp
    display_jp = name_jp if name_jp else name_tw

    brand = None
    if price_tw is not None or price_jp is not None or display_tw is not None:
        brand = "UNIQLO/GU"

    return brand, display_tw, price_tw, display_jp, price_jp

# 格式化日幣轉台幣顯示
def format_jp_price(price_jp, rate):
    if price_jp is None:
        return "🇯🇵 日本：未發售 / 無資料"
    twd_approx = round(price_jp * rate)
    return f"🇯🇵 日本：¥ {int(price_jp)} (約 NT$ {twd_approx})"

@app.route("/")
def home():
    return "H電商台日價格追蹤 Bot (Goodjack對接版) 運作中！", 200

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
        c_brand, c_name_tw, c_p_tw, c_name_jp, c_p_jp = fetch_from_goodjack(item_id)
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
                       f"🔗 台灣歷史價格: https://uq.goodjack.tw/item/{item_id}\n"
                       f"🔗 日本歷史價格: https://uq.goodjack.tw/japan/item/{item_id}")
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
            reply = "📋 目前沒有追蹤任何商品。\n輸入 `+貨號`（例如 `+484808`）即可開始追蹤！"
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
            brand, n_tw, p_tw, n_jp, p_jp = fetch_from_goodjack(item_id)
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
            reply = "💡 請使用正確格式：`+貨號`（例如 `+484808`）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    elif user_msg.startswith("-"):
        item_id = user_msg[1:].strip()
        if item_id.isdigit():
            cursor.execute("DELETE FROM tracked_items_v2 WHERE user_id = ? AND item_id = ?", (user_id, item_id))
            conn.commit()
            reply = f"🗑️ 已停止追蹤貨號 `{item_id}` 的商品。"
        else:
            reply = "💡 請使用正確格式：`-貨號`（例如 `-484808`）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    elif user_msg.isdigit():
        item_id = user_msg
        brand, n_tw, p_tw, n_jp, p_jp = fetch_from_goodjack(item_id)
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
        reply = "🤖 可用指令：\n• 直接輸入 `貨號`（如 `484808`）：快速查價\n• 輸入 `+貨號`（如 `+484808`）：加入降價追蹤\n• 輸入 `-貨號`（如 `-484808`）：取消追蹤\n• 輸入 `清單`：查看所有追蹤商品"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    conn.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
