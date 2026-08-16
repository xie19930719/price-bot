import os
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from bs4 import BeautifulSoup

app = Flask(__name__)

# 請務必確認環境變數設定
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "551273045ea1be5721345edf4196aec7")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "ftDqy1HYMrkLC/YX5uSh+9Pcq8Sk8bRcpn7vLbquj96GqzdJNhpxuybYD5DaCGtThb4fot7pctmHHgkAfpOzyqbN5vT/y5wSRcQpHtOZ6j5+k7bwhvZTXqVubSaiSFdJlVw3yZXQJlE/hU3N4p9gpQdB04t89/1O/w1cDnyilFU=")
DB_PATH = os.getenv("DB_PATH", "tracker.db")
TAIWAN_TZ = timezone(timedelta(hours=8))

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

# (init_db, get_jpy_rate 等基礎函式保持不變，略過以縮減篇幅)

def fetch_from_goodjack(item_id):
    """從 UQ 搜尋網站抓取資料"""
    url = f"https://uq.goodjack.tw/search?q={item_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            soup = BeautifulSoup(response.read(), "html.parser")
            
        # 注意：此處解析邏輯需對應 goodjack.tw 的實際 HTML 結構
        # 建議您手動確認網頁原始碼中的名稱與價格標籤
        name_tag = soup.select_one(".product-name") 
        price_tag = soup.select_one(".price")
        
        name = name_tag.text.strip() if name_tag else None
        price = float(price_tag.text.replace("NT$", "").replace(",", "")) if price_tag else None
        
        return name, price
    except Exception as e:
        print(f"Error fetching from goodjack: {e}")
        return None, None

def get_combined_info(item_id):
    # 改用第三方搜尋
    name, price = fetch_from_goodjack(item_id)
    return ("UNIQLO" if name else None), name, price, None, None

# ... 其餘處理 LINE 訊息的邏輯 (handle_message) 保持與之前一致 ...

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception:
        abort(400)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
