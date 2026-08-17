import os
import re
import json
import sqlite3
import urllib.request
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
DB_PATH = "tracker.db"
TAIWAN_TZ = timezone(timedelta(hours=8))

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 商品清單資料表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracked_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            url TEXT,
            jp_url TEXT,
            name TEXT,
            tw_price TEXT,
            jp_price TEXT
        )
    """)
    # 使用者操作狀態資料表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_states (
            user_id TEXT PRIMARY KEY,
            action TEXT,
            target_item_id INTEGER
        )
    """)
    conn.commit()
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
    """抓取 Goodjack 頁面資料 (台灣與預設日幣)"""
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

def fetch_jp_official_price(jp_url):
    """透過日本 Uniqlo 官方 API 抓取精確日幣價格"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        # 強化的正則表達式：精確匹配網址中的 6 位數商品編號 (如 475344)
        productId_match = re.search(r"(\d{6})", jp_url)
        if not productId_match:
            return None
        
        product_id = productId_match.group(1)
        api_url = f"https://www.uniqlo.com/jp/api/commerce/v5/ja/products/{product_id}?priceGroup=PRICE_GROUP_REGULAR"
        
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            
            prices = data.get("result", {}).get("prices", {})
            promo_price = prices.get("promo", {}).get("base", {}).get("value")
            base_price = prices.get("base", {}).get("value")
            
            # 優先採用限定特價 (promo)，若無則採用原價 (base)
            final_price = promo_price if promo_price else base_price
            
            if final_price is not None:
                return f"{int(final_price):,}"
    except Exception as e:
        print(f"Error fetching JP official price via API: {e}")
    return None

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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, url, jp_url, name, tw_price, jp_price FROM tracked_items")
    rows = cursor.fetchall()
    
    for row in rows:
        item_id, user_id, url, jp_url, old_name, old_tw, old_jp = row
        new_name, new_tw, new_jp = fetch_product_details(url)
        
        # 若有綁定日本官網，優先從官方 API 更新精確日幣售價
        if jp_url:
            official_jp = fetch_jp_official_price(jp_url)
            if official_jp:
                new_jp = official_jp

        if new_name and new_tw != "無資料":
            changes = []
            if new_tw != old_tw:
                changes.append(f"🇹🇼 台灣價格變動：NT$ {old_tw} ➔ NT$ {new_tw}")
            if new_jp != old_jp:
                changes.append(f"🇯🇵 日本價格變動：￥ {old_jp} ➔ ￥ {new_jp}")
            
            if changes:
                cursor.execute(
                    "UPDATE tracked_items SET name = ?, tw_price = ?, jp_price = ? WHERE id = ?",
                    (new_name, new_tw, new_jp, item_id)
                )
                conn.commit()
                
                change_text = "\n".join(changes)
                push_msg = f"🔔 【UQ 價格變動通知】\n📦 {new_name}\n\n{change_text}\n\n🔗 {url}"
                try:
                    line_bot_api.push_message(user_id, TextSendMessage(text=push_msg))
                except Exception as e:
                    print(f"Push error: {e}")
                    
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
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 檢查使用者是否處於「等待輸入日本網址」的狀態
    cursor.execute("SELECT action, target_item_id FROM user_states WHERE user_id = ?", (user_id,))
    state = cursor.fetchone()
    
    if state and state[0] == "WAITING_JP_URL" and ("uniqlo.com" in msg or "goodjack.tw" in msg):
        target_db_id = state[1]
        
        # 判斷是日本官網網址還是 Goodjack 網址
        if "uniqlo.com" in msg:
            jp_price = fetch_jp_official_price(msg)
        else:
            jp_price = fetch_product_details(msg)[2]
        
        if jp_price:
            cursor.execute(
                "UPDATE tracked_items SET jp_url = ?, jp_price = ? WHERE id = ?",
                (msg, jp_price, target_db_id)
            )
            # 只有在「成功抓取並綁定」時才刪除等待狀態
            cursor.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
            conn.commit()
            
            jp_formatted = format_jp_price_with_twd(jp_price)
            reply = f"✅ 已成功綁定日本網址！\n最新日本價格為：{jp_formatted}\n\n請輸入「清單」查看最新狀態。"
        else:
            # 失敗時保留狀態，方便使用者補傳短網址
            reply = "⚠️ 無法抓取該日本網址的價格，請重新確認網址是否正確，或直接傳送含有 6 位數編號的網址。"
            
        conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 若傳送其他普通指令，清除等待狀態
    if state and not re.match(r"^綁定日幣-\d+$", msg):
        cursor.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
        conn.commit()

    if msg == "清單":
        cursor.execute("SELECT id, name, tw_price, jp_price, url, jp_url FROM tracked_items WHERE user_id = ?", (user_id,))
        rows = cursor.fetchall()
        
        if not rows:
            reply = "📋 目前沒有追蹤任何商品。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        else:
            reply = "📋 【你的追蹤清單】\n點擊下方按鈕可進行管理：\n"
            quick_reply_buttons = []
            
            for idx, row in enumerate(rows, 1):
                item_id, name, tw_p, jp_p, url, jp_url = row
                jp_formatted = format_jp_price_with_twd(jp_p)
                jp_note = " (已綁定日本官網)" if jp_url else ""
                reply += f"\n{idx}. {name}\n   🇹🇼 NT$ {tw_p} | 🇯🇵 {jp_formatted}{jp_note}\n   🔗 {url}\n"
                
                # 快捷按鈕：刪除與綁定
                quick_reply_buttons.append(
                    QuickReplyButton(action=MessageAction(label=f"🗑️ 刪除 {idx}", text=f"-{idx}"))
                )
                quick_reply_buttons.append(
                    QuickReplyButton(action=MessageAction(label=f"🇯🇵 綁定日幣 {idx}", text=f"綁定日幣-{idx}"))
                )
            
            quick_reply = QuickReply(items=quick_reply_buttons[:13]) # LINE QuickReply 限制最多 13 個按鈕
            line_bot_api.reply_message(
                event.reply_token, 
                TextSendMessage(text=reply, quick_reply=quick_reply)
            )

    elif re.match(r"^綁定日幣-\d+$", msg):
        target_idx = int(msg.replace("綁定日幣-", ""))
        cursor.execute("SELECT id, name FROM tracked_items WHERE user_id = ?", (user_id,))
        rows = cursor.fetchall()
        
        if 1 <= target_idx <= len(rows):
            target_db_id = rows[target_idx - 1][0]
            target_name = rows[target_idx - 1][1]
            
            cursor.execute(
                "INSERT OR REPLACE INTO user_states (user_id, action, target_item_id) VALUES (?, ?, ?)",
                (user_id, "WAITING_JP_URL", target_db_id)
            )
            conn.commit()
            
            reply = f"💡 請直接傳送「{target_name}」在日本官網（uniqlo.com/jp）的商品網址，將自動為您綁定價格！"
        else:
            reply = "⚠️ 找不到該編號的商品。"
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    elif re.match(r"^-\d+$", msg):
        target_idx = int(msg.replace("-", ""))
        cursor.execute("SELECT id FROM tracked_items WHERE user_id = ?", (user_id,))
        rows = cursor.fetchall()
        
        if 1 <= target_idx <= len(rows):
            real_db_id = rows[target_idx - 1][0]
            cursor.execute("DELETE FROM tracked_items WHERE id = ?", (real_db_id,))
            conn.commit()
            reply = f"🗑️ 已成功刪除第 {target_idx} 項商品。"
        else:
            reply = "⚠️ 找不到該編號的商品。"
            
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    elif "goodjack.tw" in msg or "uniqlo.com" in msg:
        if "uniqlo.com" in msg:
            jp_p = fetch_jp_official_price(msg)
            name, tw_p = "日本 Uniqlo 商品", "無資料"
        else:
            name, tw_p, jp_p = fetch_product_details(msg)
            
        if name and (tw_p != "無資料" or jp_p is not None):
            cursor.execute("SELECT id FROM tracked_items WHERE user_id = ? AND url = ?", (user_id, msg))
            existing = cursor.fetchone()
            
            if not existing:
                cursor.execute(
                    "INSERT INTO tracked_items (user_id, url, name, tw_price, jp_price) VALUES (?, ?, ?, ?, ?)",
                    (user_id, msg, name, tw_p, jp_p)
                )
                status_msg = "✅ 已成功加入追蹤清單！"
            else:
                cursor.execute(
                    "UPDATE tracked_items SET name = ?, tw_price = ?, jp_price = ? WHERE user_id = ? AND url = ?",
                    (name, tw_p, jp_p, user_id, msg)
                )
                status_msg = "ℹ️ 已為您更新最新價格！"
            conn.commit()
            
            jp_formatted = format_jp_price_with_twd(jp_p) if jp_p else "無資料"
            reply = f"{status_msg}\n\n📦 商品名稱：{name}\n🇹🇼 台灣售價：NT$ {tw_p}\n🇯🇵 日本售價：{jp_formatted}"
        else:
            reply = "⚠️ 無法抓取該網址資料，請檢查網址。"
            
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="📋 查看清單", text="清單"))
        ])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply, quick_reply=quick_reply))
    else:
        reply = "💡 請傳送 UQ 網址加入追蹤，或輸入「清單」查看全部。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        
    conn.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
