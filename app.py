import os
import json
import sqlite3
import re
import urllib.request
import urllib.error
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

def get_jpy_rate():
    try:
        req = urllib.request.Request(
            "https://rate.bot.com.tw/xrt/flats/003/day",
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            html = response.read().decode('utf-8')
            lines = html.splitlines()
            for line in lines:
                if "JPY" in line:
                    parts = line.split(',')
                    rate = float(parts[12]) if len(parts) > 12 else float(parts[2])
                    if rate > 0:
                        return rate
    except Exception as e:
        print(f"[ERROR] Fetch exchange rate failed: {e}")
    return 0.22

def parse_html_for_data(html_str):
    name = None
    price = None

    # 1. 嘗試從 __NEXT_DATA__ 抓取完整商品資訊
    next_data_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html_str, re.DOTALL)
    if next_data_match:
        try:
            json_data = json.loads(next_data_match.group(1))
            # 遞迴或多路徑尋找 product 物件
            page_props = json_data.get('props', {}).get('pageProps', {})
            product = page_props.get('product') or page_props.get('initialState', {}).get('product', {})
            if not product and 'dehydratedState' in page_props:
                # 尋找 react-query 快取中的商品資料
                queries = page_props['dehydratedState'].get('queries', [])
                for q in queries:
                    q_data = q.get('state', {}).get('data', {})
                    if isinstance(q_data, dict):
                        p_info = q_data.get('result', {} ) or q_data
                        if 'name' in p_info or 'productName' in p_info:
                            product = p_info
                            break

            if product:
                name = product.get('name') or product.get('productName')
                min_p = product.get('minPrice') or product.get('price')
                if min_p is not None:
                    price = float(min_p)
        except Exception as e:
            print(f"[JSON PARSE ERROR] {e}")

    # 2. 備用方案：從網頁內嵌的 JSON 結構以正規表達式抓取 productName 與 price
    if not name:
        name_match = re.search(r'"productName"\s*:\s*"([^"]+)"', html_str) or re.search(r'"name"\s*:\s*"([^"]+)"', html_str)
        if name_match:
            name = name_match.group(1)

    if not price:
        price_match = re.search(r'"minPrice"\s*:\s*([0-9.]+)', html_str) or re.search(r'"price"\s*:\s*([0-9.]+)', html_str)
        if price_match:
            try:
                price = float(price_match.group(1))
            except ValueError:
                pass

    return name, price

def fetch_uniqlo_official(item_id, region="tw"):
    clean_id = item_id.strip().zfill(6)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-TW,zh;q=0.9' if region == 'tw' else 'ja-JP,ja;q=0.9',
    }

    url = f"https://www.uniqlo.com/{region}/zh_TW/product-detail.html?productCode=u00000000{clean_id}" if region == "tw" else f"https://www.uniqlo.com/jp/ja/products/E{clean_id}-000"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as response:
            if response.status == 200:
                content = response.read().decode('utf-8')
                name, price = parse_html_for_data(content)
                if name or price:
                    return name, price
    except Exception as e:
        print(f"[FETCH ERROR] {region}-{clean_id}: {e}")

    return None, None

def get_combined_info(item_id):
    name_tw, price_tw = fetch_uniqlo_official(item_id, "tw")
    name_jp, price_jp = fetch_uniqlo_official(item_id, "jp")

    display_tw = name_tw if name_tw else name_jp
    display_jp = name_jp if name_jp else name_tw

    brand = None
    if price_tw is not None or price_jp is not None or display_tw is not None:
        brand = "UNIQLO"

    return brand, display_tw, price_tw, display_jp, price_jp

def format_jp_price(price_jp, rate):
    if price_jp is None:
        return "🇯🇵 日本：未發售 / 無資料"
    twd_approx = round(price_jp * rate)
    return f"🇯🇵 日本：¥ {int(price_jp)} (約 NT$ {twd_approx})"

@app.route("/")
def home():
    return "電商台日價格追蹤 Bot 運作中！", 200

@app.route("/test/<item_id>")
def test_item(item_id):
    brand, n_tw, p_tw, n_jp, p_jp = get_combined_info(item_id)
    return {
        "item_id": item_id,
        "brand": brand,
        "name_tw": n_tw,
        "price_tw": p_tw,
        "name_jp": n_jp,
        "price_jp": p_jp
    }, 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK', 200

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
            reply = "📋 目前沒有追蹤任何商品。\n輸入 `+貨號`（例如 `+475355`）即可開始追蹤！"
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
            reply = "💡 請使用正確格式：`+貨號`（例如 `+475355`）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

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
        reply = "🤖 可用指令：\n• 直接輸入 `貨號`（如 `475355`）：快速查價\n• 輸入 `+貨號`（如 `+475355`）：加入降價追蹤\n• 輸入 `-貨號`：取消追蹤\n• 輸入 `清單`：查看所有追蹤商品"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    conn.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
