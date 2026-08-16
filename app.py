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

def get_jpy_to_twd_rate():
    """簡單取得日幣換台幣匯率，若失敗則預設 0.21"""
    try:
        url = "https://rate.bot.com.tw/xrt/flcsv/0.5/day" # 臺灣銀行牌告匯率
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as res:
            lines = res.read().decode("utf-8").split("\n")
            for line in lines:
                if "JPY" in line:
                    parts = line.split(",")
                    # 賣出匯率通常在特定欄位，取現鈔賣出或即期賣出
                    rate = float(parts[12]) # 視臺銀 CSV 格式而定
                    return rate
    except Exception:
        pass
    return 0.21 # 預設參考匯率

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

def format_jp_price_with_twd(jp_price_str):
    """計算日幣換算台幣金額"""
    try:
        clean_val = float(jp_price_str.replace(",", ""))
        rate = get_jpy_to_twd_rate()
        twd_val = int(round(clean_val * rate))
        return f"￥ {jp_price_str} (約 NT$ {twd_val})"
    except Exception:
        return f"￥ {jp_price_str}"

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
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, tw_price, jp_price, url FROM tracked_items WHERE user_id = ?", (user_id,))
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            reply = "📋 目前沒有追蹤任何商品。"
        else:
            reply = "📋 【你的追蹤清單】\n(輸入「-數字」可刪除該項，例如 -2)\n"
            for idx, row in enumerate(rows, 1):
                item_id, name, tw_p, jp_p, url = row
                jp_formatted = format_jp_price_with_twd(jp_p)
                reply += f"\n{idx}. {name}\n   🇹🇼 NT$ {tw_p} | 🇯🇵 {jp_formatted}\n   🔗 {url}\n"
                
    elif re.match(r"^-\d+$", msg):
        # 處理刪除指令，例如 "-2"
        target_idx = int(msg.replace("-", ""))
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tracked_items WHERE user_id = ?", (user_id,))
        rows = cursor.fetchall()
        
        if 1 <= target_idx <= len(rows):
            real_db_id = rows[target_idx - 1][0]
            cursor.execute("DELETE FROM tracked_items WHERE id = ?", (real_db_id,))
            conn.commit()
            reply = f"🗑️ 已成功刪除清單中的第 {target_idx} 項商品。"
        else:
            reply = "⚠️ 找不到該編號的商品，請先輸入「清單」確認正確編號。"
        conn.close()
        
    elif "goodjack.tw" in msg:
        name, tw_p, jp_p = fetch_product_details(msg)
        if name:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
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
                cursor.execute(
                    "UPDATE tracked_items SET name = ?, tw_price = ?, jp_price = ? WHERE user_id = ? AND url = ?",
                    (name, tw_p, jp_p, user_id, msg)
                )
                conn.commit()
                status_msg = "ℹ️ 此商品已在清單中，已為您更新最新價格！"
            conn.close()
            
            jp_formatted = format_jp_price_with_twd(jp_p)
            reply = f"{status_msg}\n\n📦 商品名稱：{name}\n🇹🇼 台灣售價：NT$ {tw_p}\n🇯🇵 日本售價：{jp_formatted}"
        else:
            reply = "⚠️ 無法抓取該網址資料，請檢查網址是否正確。"
    else:
        reply = "💡 請傳送 UQ 網址加入追蹤，輸入「清單」查看全部，或輸入「-數字」刪除指定項目。"
        
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
