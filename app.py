import os
import re
import sqlite3
import urllib.request
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "551273045ea1be5721345edf4196aec7")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "ftDqy1HYMrkLC/YX5uSh+9Pcq8Sk8bRcpn7vLbquj96GqzdJNhpxuybYD5DaCGtThb4fot7pctmHHgkAfpOzyqbN5vT/y5wSRcQpHtOZ6j5+k7bwhvZTXqVubSaiSFdJlVw3yZXQJlE/hU3N4p9gpQdB04t89/1O/w1cDnyilFU=")
DB_PATH = "tracker.db"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def init_db():
    """初始化資料庫"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracked_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            url TEXT,
            name TEXT,
            tw_price TEXT,
            jp_price TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def fetch_product_details(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html_content = response.read().decode("utf-8", errors="ignore")
            
        soup = BeautifulSoup(html_content, "html.parser")
        
        name_tag = soup.find("h1")
        name = " ".join(name_tag.text.split()) if name_tag else "未知商品"
        
        tw_price = "無資料"
        jp_price = "無資料"
        text = soup.get_text()
        
        tw_match = re.search(r"NT\$\s*([\d,]+)", text)
        if tw_match:
            tw_price = tw_match.group(1)
            
        current_price_box = soup.find(string=re.compile(r"當前價格"))
        if current_price_box:
            parent_text = current_price_box.find_parent().get_text()
            jp_match = re.search(r"[￥¥]\s*([\d,]+)", parent_text)
            if jp_match:
                jp_price = jp_match.group(1)
        
        if jp_price == "無資料":
            jp_match_all = re.search(r"[￥¥]\s*([\d,]+)", text)
            if jp_match_all:
                jp_price = jp_match_all.group(1)
            
        return name, tw_price, jp_price
    except Exception as e:
        print(f"Error fetching product: {e}")
        return None, None, None

@app.route("/")
def home():
    return "UQ 價格追蹤 Bot 運作中！", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    user_id = event.source.user_id if hasattr(event.source, "user_id") else "default_user"
    
    if msg == "清單":
        # 查詢資料庫中的追蹤清單
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name, tw_price, jp_price, url FROM tracked_items WHERE user_id = ?", (user_id,))
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            reply = "📋 目前沒有追蹤任何商品。"
        else:
            reply = "📋 【你的追蹤清單】\n"
            for idx, row in enumerate(rows, 1):
                name, tw_p, jp_p, url = row
                reply += f"\n{idx}. {name}\n   🇹🇼 NT$ {tw_p} | 🇯🇵 ¥ {jp_p}\n   🔗 {url}\n"
    elif "goodjack.tw" in msg:
        name, tw_p, jp_p = fetch_product_details(msg)
        if name:
            # 儲存到資料庫
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            # 檢查是否重複加入相同網址
            cursor.execute("SELECT id FROM tracked_items WHERE user_id = ? AND url = ?", (user_id, msg))
            existing = cursor.fetchone()
            
            if not existing:
                cursor.execute(
                    "INSERT INTO tracked_items (user_id, url, name, tw_price, jp_price) VALUES (?, ?, ?, ?, ?)",
                    (user_id, msg, name, tw_p, jp_p)
                )
                conn.commit()
                status_msg = "✅ 已成功加入追蹤清單！"
            else:
                # 更新最新價格
                cursor.execute(
                    "UPDATE tracked_items SET name = ?, tw_price = ?, jp_price = ? WHERE user_id = ? AND url = ?",
                    (name, tw_p, jp_p, user_id, msg)
                )
                conn.commit()
                status_msg = "ℹ️ 此商品已在清單中，已為您更新最新價格！"
            conn.close()
            
            reply = f"{status_msg}\n\n📦 商品名稱：{name}\n🇹🇼 台灣售價：NT$ {tw_p}\n🇯🇵 日本售價：￥ {jp_p}"
        else:
            reply = "⚠️ 無法抓取該網址資料，請檢查網址是否正確。"
    else:
        reply = "💡 請傳送 UQ 網址來查詢並加入追蹤，或輸入「清單」查看所有追蹤商品。"
        
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
