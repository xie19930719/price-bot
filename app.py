import os
import sqlite3
import urllib.request
import urllib.parse
import re
import json
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# --- 設定 ---
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "551273045ea1be5721345edf4196aec7")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "ftDqy1HYMrkLC/YX5uSh+9Pcq8Sk8bRcpn7vLbquj96GqzdJNhpxuybYD5DaCGtThb4fot7pctmHHgkAfpOzyqbN5vT/y5wSRcQpHtOZ6j5+k7bwhvZTXqVubSaiSFdJlVw3yZXQJlE/hU3N4p9gpQdB04t89/1O/w1cDnyilFU=")
DB_PATH = "tracker.db"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def fetch_from_goodjack(item_id):
    """
    從 goodjack.tw 搜尋結果頁抓取資料
    自動挑選該貨號下的最低價
    """
    url = f"https://uq.goodjack.tw/search?query={urllib.parse.quote(item_id)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html_content = response.read().decode("utf-8")
        
        soup = BeautifulSoup(html_content, "html.parser")
        
        # 尋找名稱：取搜尋結果中出現的第一個標題
        name_tag = soup.find(["h2", "h3", "div"], class_=re.compile(r"title|name"))
        name = name_tag.text.strip() if name_tag else f"商品 {item_id}"
        
        # 尋找價格：找出所有符合價格格式的數字，並取最小值
        price_tags = soup.find_all(string=re.compile(r"\$\s*\d+"))
        prices = []
        for tag in price_tags:
            # 擷取數字部分
            match = re.search(r"\$\s*(\d+)", tag)
            if match:
                price = int(match.group(1))
                # 篩選合理衣服價格區間 (避免抓到其他無關數字)
                if 50 < price < 10000:
                    prices.append(price)
        
        min_price = min(prices) if prices else None
        return name, min_price
        
    except Exception as e:
        print(f"Error fetching from goodjack: {e}")
        return None, None

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    
    # 假設輸入即為貨號
    name, price = fetch_from_goodjack(user_text)
    
    if name and price:
        reply = f"找到商品：{name}\n目前最低價格：NT${price}"
    else:
        reply = "找不到該貨號，或網站資料異常。"
        
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
