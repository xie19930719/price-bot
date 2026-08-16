import os
import json
import sqlite3
import urllib.request
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# Token / Secret 請設定在環境變數，不要寫死在程式碼
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")
DB_PATH = os.getenv("DB_PATH", "tracker.db")
TAIWAN_TZ = timezone(timedelta(hours=8))

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    print("[WARNING] LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN 尚未設定。")

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
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_price_history_item_region_time
        ON price_history(item_id, region, id)
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


def fetch_uniqlo_official(item_id, region="tw"):
    clean_id = item_id.strip()
    url = f"https://www.uniqlo.com/{region}/api/commerce/v5/{region}/products?q={clean_id}&limit=10"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            if response.status != 200:
                return None, None
            data = json.loads(response.read().decode("utf-8", errors="ignore"))
        items = data.get("result", {}).get("items", [])
        if not isinstance(items, list) or not items:
            return None, None

        # 優先找真正等於貨號的結果，找不到才退回第一筆
        item = None
        for candidate in items:
            ids = [candidate.get(k) for k in ("id", "productCode", "itemCode", "code", "sku")]
            if clean_id in [str(x).strip() for x in ids if x is not None]:
                item = candidate
                break
        if item is None:
            item = items[0]

        name = item.get("name") or item.get("productName")
        price_val = None
        if item.get("minPrice") is not None:
            try:
                price_val = float(item["minPrice"])
            except (ValueError, TypeError):
                pass
        if price_val is None and isinstance(item.get("prices"), dict):
            for key in ("promo", "sale", "base", "original"):
                value = item["prices"].get(key)
                if isinstance(value, dict):
                    value = value.get("value")
                if value is not None:
                    try:
                        price_val = float(value)
                        break
                    except (ValueError, TypeError):
                        pass
        return name, price_val
    except Exception as e:
        print(f"[SEARCH API ERROR] {region}-{clean_id}: {e}")
    return None, None


def get_combined_info(item_id):
    name_tw, price_tw = fetch_uniqlo_official(item_id, "tw")
    name_jp, price_jp = fetch_uniqlo_official(item_id, "jp")
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


def build_price_change_message(item_id, name_tw, name_jp, old_tw, new_tw, old_jp, new_jp, rate):
    lines = ["🔔 UNIQLO 降價通知", f"🏷️ 貨號：{item_id}"]
    name = name_tw or name_jp
    if name:
        lines.append(f"商品：{name}")
    lines.append("")
    if old_tw is not None and new_tw is not None and new_tw < old_tw:
        lines.append(f"🇹🇼 台灣：NT$ {int(old_tw):,} → NT$ {int(new_tw):,}（降 NT$ {int(old_tw-new_tw):,}）")
    if old_jp is not None and new_jp is not None and new_jp < old_jp:
        lines.append(f"🇯🇵 日本：¥ {int(old_jp):,} → ¥ {int(new_jp):,}（降 ¥ {int(old_jp-new_jp):,}，約 NT$ {round(new_jp*rate):,}）")
    lines.append("")
    lines.append("💡 任一地區價格下降，我就會通知你。")
    return "\n".join(lines)


def check_all_prices():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT item_id FROM tracked_items_v2 ORDER BY item_id").fetchall()
    if not rows:
        conn.close()
        return {"checked_items": 0, "notifications": 0, "message": "目前沒有追蹤商品。"}

    rate = get_jpy_rate()
    checked_items = 0
    notifications = 0

    for row in rows:
        item_id = row["item_id"]
        try:
            brand, name_tw, price_tw, name_jp, price_jp = get_combined_info(item_id)
            if not brand:
                print(f"[CHECK] {item_id}: 找不到商品")
                continue

            old_tw = get_previous_price(conn, item_id, "tw")
            old_jp = get_previous_price(conn, item_id, "jp")
            dropped_tw = old_tw is not None and price_tw is not None and price_tw < old_tw
            dropped_jp = old_jp is not None and price_jp is not None and price_jp < old_jp

            save_price_history(conn, item_id, "tw", price_tw)
            save_price_history(conn, item_id, "jp", price_jp)
            conn.execute(
                "UPDATE tracked_items_v2 SET price_tw=COALESCE(?,price_tw), price_jp=COALESCE(?,price_jp), name_tw=COALESCE(?,name_tw), name_jp=COALESCE(?,name_jp) WHERE item_id=?",
                (price_tw, price_jp, name_tw, name_jp, item_id),
            )

            if dropped_tw or dropped_jp:
                users = conn.execute("SELECT DISTINCT user_id FROM tracked_items_v2 WHERE item_id=?", (item_id,)).fetchall()
                message = build_price_change_message(item_id, name_tw, name_jp, old_tw, price_tw, old_jp, price_jp, rate)
                if line_bot_api:
                    for user in users:
                        try:
                            line_bot_api.push_message(user["user_id"], TextSendMessage(text=message))
                            notifications += 1
                        except Exception as e:
                            print(f"[PUSH ERROR] item={item_id}, user={user['user_id']}: {e}")

            conn.commit()
            checked_items += 1
            print(f"[CHECK] {item_id}: TW={price_tw}, JP={price_jp}, oldTW={old_tw}, oldJP={old_jp}")
        except Exception as e:
            print(f"[CHECK ERROR] {item_id}: {e}")

    conn.close()
    return {"checked_items": checked_items, "notifications": notifications, "message": "價格檢查完成。"}


@app.route("/")
def home():
    return "電商台日價格追蹤 Bot 運作中！", 200


@app.route("/test/<item_id>")
def test_item(item_id):
    brand, n_tw, p_tw, n_jp, p_jp = get_combined_info(item_id)
    return {"item_id": item_id, "brand": brand, "name_tw": n_tw, "price_tw": p_tw, "name_jp": n_jp, "price_jp": p_jp}, 200


@app.route("/cron/check", methods=["GET", "POST"])
def cron_check():
    # 外部排程可呼叫此網址；設定 CRON_SECRET 後需帶 X-Cron-Secret header
    if CRON_SECRET and request.headers.get("X-Cron-Secret", "") != CRON_SECRET:
        abort(403)
    return check_all_prices(), 200


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
                    reply = "📋 目前沒有追蹤任何商品。\n輸入 `+貨號`（例如 `+475355`）即可開始追蹤！"
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
                    reply += "\n價格會由排程自動檢查，降價時會主動通知您！"

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
