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
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*'
}

# 1. 透過搜尋 API 將 6 位數貨號轉為內部 Product Code
def search_product_code(query, region="tw", brand="uniqlo"):
    domain = "uniqlo.com" if brand == "uniqlo" else "gu-global.com"
    url = f"https://www.{domain}/{region}/api/commerce/v5/{region}/products?query={query}&limit=1"
    try:
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code == 200:
            data = res.json()
            items = data.get('result', {}).get('items', [])
            if items:
                return items[0].get('productId') or items[0].get('productCode')
    except Exception as e:
        print(f"Search error ({brand}-{region}): {e}")
    return None

# 2. 抓取具體商品的名稱與價格
def fetch_product_details(product_code, region="tw", brand="uniqlo"):
    if not product_code:
        return None, None
    domain = "uniqlo.com" if brand == "uniqlo" else "gu-global.com"
    url = f"https://www.{domain}/{region}/api/commerce/v5/{region}/products/{product_code}?priceGroup=official&expand=prices"
    try:
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code == 200:
            data = res.json()
            result = data.get('result', {})
            items = result.get('items', [result.get('product', {})]) if 'items' in result or 'product' in result else []
            for item in items:
                name = item.get('name')
                prices = item.get('prices', {})
                price_val = None
                for key in ['base', 'promo', 'original']:
                    if key in prices and prices[key].get('value') is not None:
                        price_val = prices[key]['value']
                        break
                if name and price_val is not None:
                    return name, float(price_val)
    except Exception as e:
        print(f"Fetch details error ({brand}-{region}): {e}")
    return None, None

# 跨國與跨品牌整合查詢
def get_combined_info(item_id):
    brand = None
    name_tw, price_tw = None, None
    name_jp, price_jp = None, None

    # 搜尋 Uniqlo
    code_tw = search_product_code(item_id, "tw", "uniqlo") or item_id
    code_jp = search_product_code(item_id, "jp", "uniqlo") or item_id

    name_tw, price_tw = fetch_product_details(code_tw, "tw", "uniqlo")
    name_jp, price_jp = fetch_product_details(code_jp, "jp", "uniqlo")

    if price_tw is not None or price_jp is not None:
        brand = "UNIQLO"
    else:
        # 搜尋 GU
        code_tw_g = search_product_code(item_id, "tw", "gu") or item_id
        code_jp_g = search_product_code(item_id, "jp", "gu") or item_id

        name_tw, price_tw = fetch_product_details(code_tw_g, "tw", "gu")
        name_jp, price_jp = fetch_product_details(code_jp_g, "jp", "gu")

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

@app.route("/check_prices", methods=['GET', 'POST'])
def check_prices():
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, item_id, brand, name_tw, name_jp, price_tw, price_jp FROM tracked_items_v2")
    rows = cursor.fetchall()

    updated_count, notifications = 0, 0

    for user_id, item_id, brand, old_name_tw, old_name_jp, old_p_tw, old_p_jp in rows:
        c_brand, c_name_tw, c_p_tw, c_name_jp, c_p_jp = get_combined_info(item_id)
        if c_p_tw is not None or c_p_jp is not None:
            drops = []
            if old_p_tw and c_p_tw and c_p_tw < old_p_tw:
                drops.append(f"🇹🇼 台灣特價: NT$ {int(c_p_tw)} (原價 NT$ {int(old_p_tw)})")
            if old_p_jp and c_p_jp and c_p_jp < old_p_jp:
                drops.append(f"🇯🇵 日本特價: ¥ {int(c_p_jp)} (原價 ¥ {int(old_p_jp)})")

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
