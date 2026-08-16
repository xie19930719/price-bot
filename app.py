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

# 通用 API 請求函式（帶上完整的瀏覽器 Headers 避免被 API 反爬蟲擋掉）
def fetch_api(url, referer_url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        'Referer': referer_url
    }
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get('code') == '200' and 'result' in data:
                items = data['result'].get('items', [])
                if items:
                    name = items[0].get('name')
                    price_val = items[0].get('prices', {}).get('base', {}).get('value')
                    if price_val is not None:
                        return name, float(price_val)
    except Exception as e:
        print(f"Fetch API error for {url}: {e}")
    return None, None

# 整合查詢：同時搜尋 Uniqlo / GU 的台灣與日本資料
def get_combined_info(item_id):
    brand = None
    name_tw, price_tw = None, None
    name_jp, price_jp = None, None

    # 格式化貨號 (確保 6 位數字)
    formatted_id = item_id.zfill(6)

    # 1. 搜尋 Uniqlo (台灣 & 日本)
    tw_u_url = f"https://www.uniqlo.com/tw/api/commerce/v5/tw/products/{formatted_id}?priceGroup=official"
    jp_u_url = f"https://www.uniqlo.com/jp/api/commerce/v5/jp/products/{formatted_id}?priceGroup=official"
    
    name_tw, price_tw = fetch_api(tw_u_url, "https://www.uniqlo.com/tw/")
    name_jp, price_jp = fetch_api(jp_u_url, "https://www.uniqlo.com/jp/")

    if price_tw is not None or price_jp is not None:
        brand = "UNIQLO"
    else:
        # 2. 若 Uniqlo 沒找到，搜尋 GU (台灣 & 日本)
        tw_g_url = f"https://www.gu-global.com/tw/api/commerce/v5/tw/products/{formatted_id}?priceGroup=official"
        jp_g_url = f"https://www.gu-global.com/jp/api/commerce/v5/jp/products/{formatted_id}?priceGroup=official"
        
        name_tw, price_tw = fetch_api(tw_g_url, "https://www.gu-global.com/tw/")
        name_jp, price_jp = fetch_api(jp_g_url, "https://www.gu-global.com/jp/")
        
        if price_tw is not None or price_jp is not None:
            brand = "GU"

    return brand, name_tw, price_tw, name_jp, price_jp

@app.route("/")
def home():
    return "H電商台日價格追蹤 Bot 運作中！", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK', 200

# 定時檢查價格與推播降價
@app.route("/check_prices", methods=['GET', 'POST'])
def check_prices():
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, item_id, brand, name_tw, name_jp, price_tw, price_jp FROM tracked_items_v2")
    rows = cursor.fetchall()

    updated_count = 0
    notifications = 0

    for user_id, item_id, brand, old_name_tw, old_name_jp, old_p_tw, old_p_jp in rows:
        c_brand, c_name_tw, c_p_tw, c_name_jp, c_p_jp = get_combined_info(item_id)
        
        if c_p_tw is not None or c_p_jp is not None:
            drops = []
            # 檢查台灣是否降價
            if old_p_tw and c_p_tw and c_p_tw < old_p_tw:
                drops.append(f"🇹🇼 台灣特價: NT$ {int(c_p_tw)} (原價 NT$ {int(old_p_tw)})")
            # 檢查日本是否降價
            if old_p_jp and c_p_jp and c_p_jp < old_p_jp:
                drops.append(f"🇯🇵 日本特價: ¥ {int(c_p_jp)} (原價 ¥ {int(old_p_jp)})")

            # 若有任一地區降價，發送推播
            if drops:
                display_name = c_name_tw if c_name_tw else c_name_jp
                msg = (f"🎉【降價通知】🎉\n"
                       f"您追蹤的商品降價囉！\n\n"
                       f"📦 [{brand}] {display_name} ({item_id})\n" +
                       "\n".join(drops) + "\n\n"
                       f"🔗 台灣官網: https://www.uniqlo.com/tw/zh_TW/product-detail.html?productCode={item_id}\n"
                       f"🔗 日本官網: https://www.uniqlo.com/jp/ja/products/{item_id}")
                try:
                    line_bot_api.push_message(user_id, TextSendMessage(text=msg))
                    notifications += 1
                except Exception as e:
                    print(f"Push failed: {e}")

            # 更新最新價格與名稱
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

    if user_msg == "清單":
        cursor.execute("SELECT item_id, brand, name_tw, name_jp, price_tw, price_jp FROM tracked_items_v2 WHERE user_id = ?", (user_id,))
        items = cursor.fetchall()
        if not items:
            reply = "📋 目前沒有追蹤任何商品。\n傳送 `+貨號`（例如 `+484808`）即可開始追蹤！"
        else:
            reply = "📋 您目前追蹤的商品清單：\n"
            for item_id, brand, n_tw, n_jp, p_tw, p_jp in items:
                reply += f"\n• [{brand}] {item_id}\n"
                if n_tw: reply += f"  🇹🇼 中文: {n_tw}\n"
                if n_jp: reply += f"  🇯🇵 日文: {n_jp}\n"
                
                prices_str = []
                if p_tw: prices_str.append(f"NT$ {int(p_tw)}")
                if p_jp: prices_str.append(f"¥ {int(p_jp)}")
                reply += f"  💰 價格: {' / '.join(prices_str)}\n"
            reply += "\n每天系統會自動檢查台日兩地的價格變動！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    # 指令：+貨號
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

                reply = f"✅ 已成功加入追蹤！\n\n🏷️ 品牌：{brand}\n"
                if n_tw: reply += f"🇹🇼 中文：{n_tw}\n"
                if n_jp: reply += f"🇯🇵 日文：{n_jp}\n"
                reply += "\n💰 目前價格：\n"
                reply += f"🇹🇼 台灣：NT$ {int(p_tw)}\n" if p_tw else "🇹🇼 台灣：未發售 / 無資料\n"
                reply += f"🇯🇵 日本：¥ {int(p_jp)}\n" if p_jp else "🇯🇵 日本：未發售 / 無資料\n"
                reply += "\n任一地區價格調降時，我都會主動通知您！"
            else:
                reply = f"❌ 找不到貨號 `{item_id}` 的商品，請確認 Uniqlo 或 GU 的貨號是否正確。"
        else:
            reply = "💡 請使用正確格式：`+貨號`（例如 `+484808`）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    # 指令：-貨號
    elif user_msg.startswith("-"):
        item_id = user_msg[1:].strip()
        if item_id.isdigit():
            cursor.execute("DELETE FROM tracked_items_v2 WHERE user_id = ? AND item_id = ?", (user_id, item_id))
            conn.commit()
            reply = f"🗑️ 已停止追蹤貨號 `{item_id}` 的商品。"
        else:
            reply = "💡 請使用正確格式：`-貨號`（例如 `-484808`）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    else:
        reply = "🤖 可用指令：\n• 輸入 `清單`：查看所有追蹤商品\n• 輸入 `+貨號`：加入追蹤 (台日 Uniqlo / GU 雙平台通用)\n• 輸入 `-貨號`：移除追蹤"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    conn.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
