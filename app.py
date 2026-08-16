import os
import re
import sqlite3
import requests
from bs4 import BeautifulSoup
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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
}

# 動態抓取日幣對台幣匯率（備用為 0.22）
def get_jpy_rate():
    try:
        res = requests.get("https://rate.bot.com.tw/xrt/flats/003/day", timeout=3)
        if res.status_code == 200:
            lines = res.text.splitlines()
            for line in lines:
                if "JPY" in line:
                    parts = line.split(',')
                    # 抓取現金賣出或即期賣出價
                    rate = float(parts[12]) if len(parts) > 12 else float(parts[2])
                    if rate > 0:
                        return rate
    except Exception as e:
        print(f"Fetch exchange rate error: {e}")
    return 0.22

# 抓取 uq.goodjack.tw 上的台灣或日本商品頁面
def fetch_goodjack_info(item_id, region="tw"):
    if region == "tw":
        url = f"https://uq.goodjack.tw/item/{item_id}"
    else:
        url = f"https://uq.goodjack.tw/japan/item/{item_id}"

    try:
        res = requests.get(url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            
            title_tag = soup.find('h1') or soup.find('title')
            name = None
            if title_tag:
                name_text = title_tag.text.strip()
                name = re.sub(r'\|.*', '', name_text).strip()
            
            price_val = None
            text_content = soup.get_text()
            
            if region == "tw":
                match = re.search(r'NT\$?\s*([0-9,]+)', text_content)
            else:
                match = re.search(r'¥\s*([0-9,]+)', text_content)

            if match:
                price_val = float(match.group(1).replace(',', ''))

            if name and price_val:
                return name, price_val
    except Exception as e:
        print(f"Goodjack Fetch Error ({region}-{item_id}): {e}")

    return None, None

# 整合查詢：同時搜尋台日資料
def get_combined_info(item_id):
    name_tw, price_tw = fetch_goodjack_info(item_id, "tw")
    name_jp, price_jp = fetch_goodjack_info(item_id, "jp")

    brand = None
    if price_tw is not None or price_jp is not None:
        brand = "UNIQLO/GU"

    return brand, name_tw, price_tw, name_jp, price_jp

# 格式化日幣轉台幣文字
def format_jp_price(price_jp, rate):
    if price_jp is None:
        return "🇯🇵 日本：未發售 / 無資料"
    twd_approx = round(price_jp * rate)
    return f"🇯🇵 日本：¥ {int(price_jp)} (約 NT$ {twd_approx})"

@app.route("/")
def home():
    return "H電商台日價格追蹤 Bot (Goodjack版) 運作中！", 200

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
        c_brand, c_name_tw, c_p_tw, c_name_jp, c_p_jp = get_combined_info(item_id)
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

    # 指令：+貨號 (新增追蹤)
    elif user_msg.startswith("+"):
        item_id = user_msg[1:].strip()
        if item_id.isdigit():
            brand, n_tw, p_tw, n_jp, p_jp = get_combined_info(item_id)
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

    # 指令：-貨號 (移除追蹤)
    elif user_msg.startswith("-"):
        item_id = user_msg[1:].strip()
        if item_id.isdigit():
            cursor.execute("DELETE FROM tracked_items_v2 WHERE user_id = ? AND item_id = ?", (user_id, item_id))
            conn.commit()
            reply = f"🗑️ 已停止追蹤貨號 `{item_id}` 的商品。"
        else:
            reply = "💡 請使用正確格式：`-貨號`（例如 `-484808`）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    # 指令：純數字貨號 (純查價，不加入追蹤)
    elif user_msg.isdigit():
        item_id = user_msg
        brand, n_tw, p_tw, n_jp, p_jp = get_combined_info(item_id)
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
