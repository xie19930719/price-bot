import os
import re
import urllib.request
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, 
    QuickReply, QuickReplyButton, MessageAction
)
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "551273045ea1be5721345edf4196aec7")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "ftDqy1HYMrkLC/YX5uSh+9Pcq8Sk8bRcpn7vLbquj96GqzdJNhpxuybYD5DaCGtThb4fot7pctmHHgkAfpOzyqbN5vT/y5wSRcQpHtOZ6j5+k7bwhvZTXqVubSaiSFdJlVw3yZXQJlE/hU3N4p9gpQdB04t89/1O/w1cDnyilFU=")
DATABASE_URL = os.getenv("DATABASE_URL")

# 自動修正 postgres:// 為 postgresql://
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

TAIWAN_TZ = timezone(timedelta(hours=8))

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def get_db_connection():
    """取得 PostgreSQL 資料庫連線"""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 環境變數未設定！")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    """初始化雲端資料庫資料表"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracked_items (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            url TEXT,
            jp_url TEXT,
            name TEXT,
            tw_price TEXT,
            jp_price TEXT
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()

init_db()

def get_jpy_to_twd_rate():
    try:
        url = "https://rate.bot.com.tw/xrt/flcsv/0.5/day"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as res:
            lines = res.read().decode("utf-8").split("\n")
            for line in lines:
                if "JPY" in line:
                    parts = line.split(",")
                    return float(parts[12])
    except Exception:
        pass
    return 0.21

def fetch_product_details(url):
    """抓取 Goodjack 頁面資料"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html_content = response.read().decode("utf-8", errors="ignore")
            
        soup = BeautifulSoup(html_content, "html.parser")
        name_tag = soup.find("h1")
        name = " ".join(name_tag.text.split()) if name_tag else "未知商品"
        
        tw_price, jp_price = "無資料", "無資料"
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
    try:
        clean_val = float(jp_price_str.replace(",", ""))
        rate = get_jpy_to_twd_rate()
        twd_val = int(round(clean_val * rate))
        return f"￥ {jp_price_str} (約 NT$ {twd_val})"
    except Exception:
        return f"￥ {jp_price_str}"

def check_and_update_prices():
    print("⏳ 開始執行每日自動價格檢查...")
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id, user_id, url, name, tw_price, jp_price FROM tracked_items")
    rows = cursor.fetchall()
    
    for row in rows:
        item_id = row['id']
        user_id = row['user_id']
        url = row['url']
        old_name = row['name']
        old_tw = row['tw_price']
        old_jp = row['jp_price']

        new_name, new_tw, new_jp = fetch_product_details(url)

        if new_name and new_tw != "無資料":
            changes = []
            if new_tw != old_tw:
                changes.append(f"🇹🇼 台灣價格變動：NT$ {old_tw} ➔ NT$ {new_tw}")
            if new_jp != old_jp:
                changes.append(f"🇯🇵 日本價格變動：￥ {old_jp} ➔ ￥ {new_jp}")
            
            if changes:
                cursor.execute(
                    "UPDATE tracked_items SET name = %s, tw_price = %s, jp_price = %s WHERE id = %s",
                    (new_name, new_tw, new_jp, item_id)
                )
                conn.commit()
                
                change_text = "\n".join(changes)
                push_msg = f"🔔 【UQ 價格變動通知】\n📦 {new_name}\n\n{change_text}\n\n🔗 {url}"
                try:
                    line_bot_api.push_message(user_id, TextSendMessage(text=push_msg))
                except Exception as e:
                    print(f"Push error: {e}")
                    
    cursor.close()
    conn.close()
    print("✅ 每日價格檢查完畢。")

scheduler = BackgroundScheduler(timezone=TAIWAN_TZ)
scheduler.add_job(func=check_and_update_prices, trigger="cron", hour=12, minute=0)
scheduler.start()

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
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if msg == "清單":
        cursor.execute("SELECT id, name, tw_price, jp_price, url FROM tracked_items WHERE user_id = %s ORDER BY id ASC", (user_id,))
        rows = cursor.fetchall()
        
        if not rows:
            reply = "📋 目前沒有追蹤任何商品。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        else:
            reply = "📋 【你的追蹤清單】\n點擊下方按鈕可刪除商品：\n"
            quick_reply_buttons = []
            
            for idx, row in enumerate(rows, 1):
                name = row['name']
                tw_p = row['tw_price']
                jp_p = row['jp_price']
                url = row['url']

                jp_formatted = format_jp_price_with_twd(jp_p)
                reply += f"\n{idx}. {name}\n   🇹🇼 NT$ {tw_p} | 🇯🇵 {jp_formatted}\n   🔗 {url}\n"
                
                quick_reply_buttons.append(
                    QuickReplyButton(action=MessageAction(label=f"🗑️ 刪除 {idx}", text=f"-{idx}"))
                )
            
            quick_reply = QuickReply(items=quick_reply_buttons[:13])
            line_bot_api.reply_message(
                event.reply_token, 
                TextSendMessage(text=reply, quick_reply=quick_reply)
            )

    elif re.match(r"^-\d+$", msg):
        target_idx = int(msg.replace("-", ""))
        cursor.execute("SELECT id FROM tracked_items WHERE user_id = %s ORDER BY id ASC", (user_id,))
        rows = cursor.fetchall()
        
        if 1 <= target_idx <= len(rows):
            real_db_id = rows[target_idx - 1]['id']
            cursor.execute("DELETE FROM tracked_items WHERE id = %s", (real_db_id,))
            conn.commit()
            reply = f"🗑️ 已成功刪除第 {target_idx} 項商品。"
        else:
            reply = "⚠️ 找不到該編號的商品。"
            
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    elif "goodjack.tw" in msg:
        name, tw_p, jp_p = fetch_product_details(msg)
            
        if name and tw_p != "無資料":
            cursor.execute("SELECT id FROM tracked_items WHERE user_id = %s AND url = %s", (user_id, msg))
            existing = cursor.fetchone()
            
            if not existing:
                cursor.execute(
                    "INSERT INTO tracked_items (user_id, url, name, tw_price, jp_price) VALUES (%s, %s, %s, %s, %s)",
                    (user_id, msg, name, tw_p, jp_p)
                )
                status_msg = "✅ 已成功加入追蹤清單！"
            else:
                cursor.execute(
                    "UPDATE tracked_items SET name = %s, tw_price = %s, jp_price = %s WHERE user_id = %s AND url = %s",
                    (name, tw_p, jp_p, user_id, msg)
                )
                status_msg = "ℹ️ 已為您更新最新價格！"
            conn.commit()
            
            jp_formatted = format_jp_price_with_twd(jp_p) if jp_p != "無資料" else "無資料"
            reply = f"{status_msg}\n\n📦 商品名稱：{name}\n🇹🇼 台灣售價：NT$ {tw_p}\n🇯🇵 日本售價：{jp_formatted}"
        else:
            reply = "⚠️ 無法抓取該網址資料，請檢查網址。"
            
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="📋 查看清單", text="清單"))
        ])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply, quick_reply=quick_reply))
    else:
        reply = "💡 請傳送 UQ (Goodjack) 網址加入追蹤，或輸入「清單」查看全部。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        
    cursor.close()
    conn.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
