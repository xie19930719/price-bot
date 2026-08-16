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

# 初始化 SQLite 資料庫
def init_db():
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tracked_items (
            user_id TEXT,
            item_id TEXT,
            item_name TEXT,
            last_price REAL,
            PRIMARY KEY (user_id, item_id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# 抓取日本 Uniqlo 價格與名稱
def get_uniqlo_info(item_id):
    url = f"https://www.uniqlo.com/jp/api/commerce/v5/jp/products/{item_id}?priceGroup=official"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        if data.get('code') == '200':
            result = data['result']
            name = result['items'][0]['name']
            price = float(result['items'][0]['prices']['base']['value'])
            return name, price
    except Exception as e:
        print(f"Error fetching Uniqlo item {item_id}: {e}")
    return None, None

@app.route("/")
def home():
    return "H電商價格追蹤 Bot 運作中！", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK', 200

# 核心功能：自動檢查價格與發送降價推播
@app.route("/check_prices", methods=['GET', 'POST'])
def check_prices():
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, item_id, item_name, last_price FROM tracked_items")
    rows = cursor.fetchall()

    updated_count = 0
    discount_notifications = 0

    for user_id, item_id, item_name, last_price in rows:
        current_name, current_price = get_uniqlo_info(item_id)
        if current_price is not None:
            # 如果發現降價了！
            if current_price < last_price:
                drop_amount = last_price - current_price
                msg = (f"🎉【降價通知】🎉\n"
                       f"您追蹤的商品降價囉！\n\n"
                       f"📦 {current_name} ({item_id})\n"
                       f"原價: ¥ {int(last_price)}\n"
                       f"💥 特價: ¥ {int(current_price)} (省下 ¥ {int(drop_amount)})\n\n"
                       f"🔗 傳送門: https://www.uniqlo.com/jp/ja/products/{item_id}")
                try:
                    line_bot_api.push_message(user_id, TextSendMessage(text=msg))
                    discount_notifications += 1
                except Exception as e:
                    print(f"Failed to push message to {user_id}: {e}")

            # 更新資料庫中的最新價格與名稱
            cursor.execute(
                "UPDATE tracked_items SET last_price = ?, item_name = ? WHERE user_id = ? AND item_id = ?",
                (current_price, current_name, user_id, item_id)
            )
            updated_count += 1

    conn.commit()
    conn.close()
    return f"Checked {updated_count} items, sent {discount_notifications} notifications.", 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()

    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()

    if user_msg == "清單":
        cursor.execute("SELECT item_id, item_name, last_price FROM tracked_items WHERE user_id = ?", (user_id,))
        items = cursor.fetchall()
        if not items:
            reply = "📋 目前沒有追蹤任何商品。\n傳送 `+貨號`（例如 `+484808`）即可開始追蹤！"
        else:
            reply = "📋 您目前追蹤的商品清單：\n"
            for item_id, name, price in items:
                reply += f"\n• {name} ({item_id})\n  紀錄價格: ¥ {int(price)}"
            reply += "\n\n每天系統會自動為您檢查價格變動！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    # 指令修改：支援 +484808 或 + 484808
    elif user_msg.startswith("+"):
        item_id = user_msg[1:].strip()
        if item_id.isdigit():
            name, price = get_uniqlo_info(item_id)
            if price is not None:
                cursor.execute(
                    "INSERT OR REPLACE INTO tracked_items (user_id, item_id, item_name, last_price) VALUES (?, ?, ?, ?)",
                    (user_id, item_id, name, price)
                )
                conn.commit()
                reply = f"✅ 已加入追蹤！\n\n📦 商品：{name}\n💰 目前價格：¥ {int(price)}\n\n價格若有調降，我會在第一時間主動發訊息通知您！"
            else:
                reply = f"❌ 找不到貨號 `{item_id}` 的商品，請確認貨號是否正確。"
        else:
            reply = "💡 請使用正確格式：`+484808`"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    # 指令修改：支援 -484808 或 - 484808
    elif user_msg.startswith("-"):
        item_id = user_msg[1:].strip()
        if item_id.isdigit():
            cursor.execute("DELETE FROM tracked_items WHERE user_id = ? AND item_id = ?", (user_id, item_id))
            conn.commit()
            reply = f"🗑️ 已停止追蹤貨號 `{item_id}` 的商品。"
        else:
            reply = "💡 請使用正確格式：`-484808`"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    else:
        reply = "🤖 可用指令：\n• 輸入 `清單`：查看所有追蹤商品\n• 輸入 `+貨號`：加入追蹤 (例: `+484808`)\n• 輸入 `-貨號`：移除追蹤 (例: `-484808`)"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    conn.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
