import sqlite3
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 🔒 請填入你的密鑰
LINE_CHANNEL_SECRET = '2011128364'
LINE_CHANNEL_ACCESS_TOKEN = '551273045ea1be5721345edf4196aec7'

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def init_db():
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS items (
            code TEXT PRIMARY KEY,
            name TEXT,
            price INTEGER
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_uq_info(item_code):
    url = f"https://www.uniqlo.com/jp/api/commerce/v5/ja/products/{item_code}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()['result']
            name = data['name']
            prices = data['prices']
            price = prices['promo']['value'] if 'promo' in prices and prices['promo'] else prices['base']['value']
            return name, int(price)
    except Exception as e:
        print(f"API Error: {e}")
    return None, None

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    reply_text = ""

    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()

    if user_msg == "清單":
        cursor.execute("SELECT code, name, price FROM items")
        rows = cursor.fetchall()
        if not rows:
            reply_text = "📌 目前沒有追蹤任何商品喔！"
        else:
            reply_text = "📋 目前追蹤清單：\n\n"
            for code, name, price in rows:
                reply_text += f"🔹 [{code}] {name}\n目前價格：¥{price}\n\n"

    elif user_msg.startswith("新增"):
        parts = user_msg.split()
        if len(parts) == 2 and parts[1].isdigit():
            code = parts[1]
            name, price = get_uq_info(code)
            if name and price:
                cursor.execute("INSERT OR REPLACE INTO items (code, name, price) VALUES (?, ?, ?)", (code, name, price))
                conn.commit()
                reply_text = f"✅ 成功新增追蹤！\n【{name}】\n當前價格：¥{price}"
            else:
                reply_text = f"❌ 找不到貨號 `{code}`，請確認輸入是否正確。"
        else:
            reply_text = "💡 請使用正確格式：`新增 465185`"

    elif user_msg.startswith("刪除"):
        parts = user_msg.split()
        if len(parts) == 2 and parts[1].isdigit():
            code = parts[1]
            cursor.execute("DELETE FROM items WHERE code = ?", (code,))
            conn.commit()
            reply_text = f"🗑️ 已刪除貨號 `{code}` 的追蹤。"
        else:
            reply_text = "💡 請使用正確格式：`刪除 465185`"

    else:
        reply_text = "🤖 可用指令：\n• 輸入 `清單`：查看所有追蹤商品\n• 輸入 `新增 <貨號>`：加入追蹤\n• 輸入 `刪除 <貨號>`：移除追蹤"

    conn.close()
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    app.run(port=5000)
