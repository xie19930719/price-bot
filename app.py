def fetch_jp_official_price(jp_url):
    """抓取日本 Uniqlo 官網價格 (優先從 HTML 內建 Next.js 數據解析，防 API 封鎖)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    
    # 提取 6 位數商品編號 (如 477345)
    productId_match = re.search(r"(\d{6})", jp_url)
    if not productId_match:
        return None
    product_id = productId_match.group(1)

    # 方案 A：直接爬取日本官網商品頁 HTML 並解析隱藏的 JSON 數據
    target_web_url = f"https://www.uniqlo.com/jp/ja/products/E{product_id}-000/00"
    try:
        req = urllib.request.Request(target_web_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore")
            
            # 從 Next.js 頁面數據或 ld+json 中找尋價格
            prices = re.findall(r'"promo":\{"base":\{"value":(\d+)|"base":\{"value":(\d+)', html)
            extracted_prices = []
            for p in prices:
                val = p[0] or p[1]
                if val:
                    extracted_prices.append(int(val))
            
            if extracted_prices:
                # 取得最可能的價格（通常為特價或當前售價）
                min_price = min(extracted_prices)
                return f"{min_price:,}"
    except Exception as e:
        print(f"HTML Parse failed, fallback to API: {e}")

    # 方案 B：備用 API 抓取
    try:
        api_url = f"https://www.uniqlo.com/jp/api/commerce/v5/ja/products/{product_id}?priceGroup=PRICE_GROUP_REGULAR"
        req_api = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req_api, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            prices = data.get("result", {}).get("prices", {})
            promo_price = prices.get("promo", {}).get("base", {}).get("value")
            base_price = prices.get("base", {}).get("value")
            final_price = promo_price if promo_price is not None else base_price
            if final_price is not None:
                return f"{int(final_price):,}"
    except Exception as e:
        print(f"API Parse failed: {e}")

    return None
