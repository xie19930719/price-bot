import os
import json
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "551273045ea1be5721345edf4196aec7")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "ftDqy1HYMrkLC/YX5uSh+9Pcq8Sk8bRcpn7vLbquj96GqzdJNhpxuybYD5DaCGtThb4fot7pctmHHgkAfpOzyqbN5vT/y5wSRcQpHtOZ6j5+k7bwhvZTXqVubSaiSFdJlVw3yZXQJlE/hU3N4p9gpQdB04t89/1O/w1cDnyilFU=")
CRON_SECRET = os.getenv("CRON_SECRET", "")
DB_PATH = os.getenv("DB_PATH", "tracker.db")
TAIWAN_TZ = timezone(timedelta(hours=8))

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
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
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            region TEXT NOT NULL,
            price REAL NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_jpy_rate():
    try:
        req = urllib.request.Request(
            "https://rate.bot.com.tw/xrt/flats/003/day",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            html = response.read().decode("utf-8", errors="ignore")
        for line in html.splitlines():
            if "JPY" not in line:
                continue
            parts = line.split(",")
            for index in (12, 2):
                if len(parts) > index:
                    try:
                        rate = float(parts[index])
                        if 0 < rate < 1:
                            return rate
                    except (ValueError, TypeError):
                        pass
    except Exception as e:
        print(f"[ERROR] Fetch exchange rate failed: {e}")
    return 0.22

def fetch_uniqlo_api(item_id, region="tw"):
    clean_id = item_id.strip()
    # 針對 UNIQLO 官方行動版 API 進行查詢（此 API 結構較穩定且直接回傳 JSON）
    url = f"https://www.uniqlo.com/{region}/api/commerce/v5/{region}/products?q={clean_id}&limit=5"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://www.uniqlo.com/{region}/"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            if response.status != 200:
                return None, None
            data = json.loads(response.read().decode("utf-8", errors="ignore"))
            
        items = data.get("result", {}).get("items", [])
        if not items:
            return None, None
            
        # 比對貨號是否符合
        target_item = None
        for item in items:
            p_code = str(item.get("productCode", ""))
            i_code = str(item.get("id", ""))
            if clean_id in p_code or clean_id in i_code or clean_id == p_code.lstrip('0'):
                target_item = item
                break
                
        if not target_item:
            target_item = items[0]  # 若沒完全吻合則取第一筆
            
        name = target_item.get("name") or target_item.get("productName")
        
        # 抓取價格
        price_val = None
        if target_item.get("minPrice") is not None:
            try:
                price_val = float(target_item["minPrice"])
            except:
                pass
                
        if price_val is None and isinstance(target_item.get("prices"), dict):
            for k in ("promo", "sale", "base", "original"):
                val = target_item["prices"].get(k)
                if isinstance(val, dict):
                    val = val.get("value")
                if val is not None:
                    try:
                        price_val = float(val)
                        break
                    except:
                        pass
                        
        return name, price_val
    except Exception as e:
        print(f"[API ERROR] {region}-{clean_id}: {e}")
        
    return None, None

def get_combined_info(item_id):
    name_tw, price_tw = fetch_uniqlo_api(item_id, "tw")
    name_jp, price_jp = fetch_uniqlo_api(item_id, "jp")
    
    display_tw = name_tw if name_tw else name_jp
    display_jp = name_jp if name_jp else name_tw
    
    brand = "UNIQLO" if any(x is not None for x in (price_tw, price_jp, display_tw, display_jp)) else None
    return brand, display_tw, price_tw, display_jp, price_jp

def format_jp_price(price_jp, rate):
    if price_jp is None:
        return "🇯🇵 日本：未發售 / 無資料"
    return f"🇯🇵 日本：¥ {int(price_jp):,} (約 NT$ {round(price_jp * rate):,})"

def now_tw():
    return datetime.now(TAIWAN_TZ).isoformat(timespec="seconds")

def get_previous_price(conn, item_id, region):
    row = conn.execute(
        "SELECT price FROM price_history WHERE item_id=? AND region=? ORDER BY id DESC LIMIT 1",
        (item_id, region),
    ).fetchone()
    return float(row["price"]) if row else None

def save_price_history(conn, item_id, region, price):
    if price is not None:
        conn.execute(
            "INSERT INTO price_history(item_id,region,price,checked_at) VALUES(?,?,?,?)",
            (item_id, region, float(price), now_tw()),
        )

@app.route("/")
def home():
    return "電商台日價格追蹤 Bot 運作中！", 200

@app.route("/test/<item_id>")
def test_item(item_id):
    brand, n_tw, p_tw, n_jp, p_jp = get_combined_info(item_id)
    return {"item_id": item_id, "brand": brand, "name_tw": n_tw, "price_tw": p_tw, "name_jp": n_jp, "price_jp": p_jp}, 200

@app.route("/callback", methods=["POST"])
def callback():
    if not handler:
        abort(500)
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        abort(500)
    return "OK", 200

if handler:
    @handler.add(MessageEvent, message=TextMessage)
    def handle_message(event):
        user_id = event.source.user_id
        user_msg = event.message.text.strip()
        conn = get_db()
        try:
            if user_msg == "清單":
                items = conn.execute("SELECT item_id,brand,name_tw,name_jp,price_tw,price_jp FROM tracked_items_v2 WHERE user_id=? ORDER BY item_id", (user_id,)).fetchall()
                if not items:
                    reply = "📋 目前沒有追蹤任何商品。\n輸入 `+475355` 即可開始追蹤！"
                else:
                    rate = get_jpy_rate()
                    reply = "📋 您目前追蹤的商品清單：\n"
                    for item in items:
                        reply += f"\n• [{item['brand']}] {item['item_id']}\n"
                        if item["name_tw"]: reply += f"  🇹🇼 中文：{item['name_tw']}\n"
                        if item["name_jp"]: reply += f"  🇯🇵 日文：{item['name_jp']}\n"
                        prices = []
                        if item["price_tw"] is not None: prices.append(f"NT$ {int(item['price_tw']):,}")
                        if item["price_jp"] is not None: prices.append(f"¥ {int(item['price_jp']):,} (約 NT$ {round(item['price_jp']*rate):,})")
                        if prices: reply += f"  💰 價格：{' / '.join(prices)}\n"
                    reply += "\n價格會由系統自動檢查，降價時會主動通知您！"

            elif user_msg.startswith("+"):
                item_id = user_msg[1:].strip()
                if not item_id.isdigit():
                    reply = "💡 請使用正確格式：`+貨號`（例如 `+475355`）"
                else:
                    brand, n_tw, p_tw, n_jp, p_jp = get_combined_info(item_id)
                    if not brand:
                        reply = f"❌ 找不到貨號 `{item_id}` 的商品，請確認貨號是否正確。"
                    else:
                        conn.execute("INSERT OR REPLACE INTO tracked_items_v2(user_id,item_id,brand,name_tw,name_jp,price_tw,price_jp) VALUES(?,?,?,?,?,?,?)", (user_id,item_id,brand,n_tw,n_jp,p_tw,p_jp))
                        if p_tw is not None and get_previous_price(conn,item_id,"tw") is None: save_price_history(conn,item_id,"tw",p_tw)
                        if p_jp is not None and get_previous_price(conn,item_id,"jp") is None: save_price_history(conn,item_id,"jp",p_jp)
                        conn.commit()
                        rate = get_jpy_rate()
                        reply = f"✅ 已成功加入追蹤！\n\n🏷️ 貨號：{item_id}\n"
                        if n_tw: reply += f"🇹🇼 中文：{n_tw}\n"
                        if n_jp: reply += f"🇯🇵 日文：{n_jp}\n"
                        reply += "\n💰 目前價格：\n"
                        reply += f"🇹🇼 台灣：NT$ {int(p_tw):,}\n" if p_tw is not None else "🇹🇼 台灣：未發售 / 無資料\n"
                        reply += format_jp_price(p_jp, rate) + "\n\n📌 已建立目前價格基準，之後降價會主動通知您！"

            elif user_msg.startswith("-"):
                item_id = user_msg[1:].strip()
                if not item_id.isdigit():
                    reply = "💡 請使用正確格式：`-貨號`（例如 `-475355`）"
                else:
                    result = conn.execute("DELETE FROM tracked_items_v2 WHERE user_id=? AND item_id=?", (user_id,item_id))
                    conn.commit()
                    reply = f"🗑️ 已取消追蹤貨號：{item_id}" if result.rowcount else f"⚠️ 你目前沒有追蹤貨號：{item_id}"

            elif user_msg.isdigit():
                item_id = user_msg
                brand, n_tw, p_tw, n_jp, p_jp = get_combined_info(item_id)
                if not brand:
                    reply = f"❌ 找不到貨號 `{item_id}` 的商品，請確認貨號是否正確。"
                else:
                    rate = get_jpy_rate()
                    reply = f"🔍 查價結果（貨號：{item_id}）\n\n"
                    if n_tw: reply += f"🇹🇼 中文：{n_tw}\n"
                    if n_jp: reply += f"🇯🇵 日文：{n_jp}\n"
                    reply += "\n💰 目前價格：\n"
                    reply += f"🇹🇼 台灣：NT$ {int(p_tw):,}\n" if p_tw is not None else "🇹🇼 台灣：未發售 / 無資料\n"
                    reply += format_jp_price(p_jp, rate) + f"\n\n💡 如需追蹤價格變動，請輸入 `+{item_id}`"

            else:
                reply = "🤖 可用指令：\n• 直接輸入 `貨號`（如 `475355`）：快速查價\n• 輸入 `+貨號`（如 `+475355`）：加入降價追蹤\n• 輸入 `-貨號`（如 `-475355`）：取消追蹤\n• 輸入 `清單`：查看所有追蹤商品"

            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        except Exception as e:
            print(f"[MESSAGE ERROR] user={user_id}: {e}")
            try:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 系統暫時發生錯誤，請稍後再試。"))
            except Exception as reply_error:
                print(f"[REPLY ERROR] {reply_error}")
        finally:
            conn.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
