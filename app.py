import os
import re
import urllib.request
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "551273045ea1be5721345edf4196aec7")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "ftDqy1HYMrkLC/YX5uSh+9Pcq8Sk8bRcpn7vLbquj96GqzdJNhpxuybYD5DaCGtThb4fot7pctmHHgkAfpOzyqbN5vT/y5wSRcQpHtOZ6j5+k7bwhvZTXqVubSaiSFdJlVw3yZXQJlE/hU3N4p9gpQdB04t89/1O/w1cDnyilFU=")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def fetch_product_details(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html_content = response.read().decode("utf-8", errors="ignore")
            
        soup = BeautifulSoup(html_content, "html.parser")
        
        # 抓取商品名稱
        name_tag = soup.find("h1")
        name = name_tag.text.strip() if name_tag else "未知商品"
        
        # 取得網頁內文並用正規表達式撈取台幣與日幣價格
        text = soup.get_text()
        tw_price = "無資料"
        jp_price = "無資料"
        
        tw_match = re.search(r"NT\$\s*([\d,]+)", text)
        jp_match = re.search(r"￥\s*([\d,]+)", text)
        
        if tw_match:
            tw_price = tw_match.group(1)
        if jp_match:
            jp_price = jp_match.group(1)
            
        return name, tw_price, jp_price
    except Exception as e:
        print(f"Error fetching product: {e}")
        return None, None, None

@app.route("/")
def home():
    return "UQ 價格查詢 Bot 運作中！", 200

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
    
    if "goodjack.tw" in msg:
        name, tw_p, jp_p = fetch_product_details(msg)
        if name:
            reply = f"📦 商品名稱：{name}\n🇹🇼 台灣售價：NT$ {tw_p}\n🇯🇵 日本售價：￥ {jp_p}"
        else:
            reply = "⚠️ 無法抓取該網址資料，請檢查網址是否正確。"
    else:
        reply = "💡 請傳送完整的 UQ 搜尋商品網址給我（例如：https://uq.goodjack.tw/...）。"
        
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
