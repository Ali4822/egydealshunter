import sqlite3
import time
import random
import re
import os
import json
import cloudscraper
from bs4 import BeautifulSoup

# سحب بيانات تيليجرام من الخزنة السرية في GitHub
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ضع_توكن_البوت_هنا")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "ضع_الأيدي_الخاص_بك_هنا")

# ================= 1. جميع أقسام أمازون مصر (بخصم 40% فما فوق) =================
AMAZON_CATEGORIES = [
    "https://www.amazon.eg/s?i=electronics&rh=p_8%3A40-&s=featured-rank", # إلكترونيات وموبايلات
    "https://www.amazon.eg/s?i=kitchen&rh=p_8%3A40-&s=featured-rank",     # أجهزة منزلية ومطبخ
    "https://www.amazon.eg/s?i=hi&rh=p_8%3A40-&s=featured-rank",          # عدد يدوية وكهربائية ومعدات
    "https://www.amazon.eg/s?i=automotive&rh=p_8%3A40-&s=featured-rank",  # مستلزمات سيارات وموتوسيكلات
    "https://www.amazon.eg/s?i=lawngarden&rh=p_8%3A40-&s=featured-rank",  # زراعة وحدائق
    "https://www.amazon.eg/s?i=computers&rh=p_8%3A40-&s=featured-rank",   # كمبيوتر ولابتوبات
    "https://www.amazon.eg/s?i=grocery&rh=p_8%3A40-&s=featured-rank",     # سوبر ماركت وبقالة
    "https://www.amazon.eg/s?i=fashion&rh=p_8%3A40-&s=featured-rank",     # ملابس وأحذية وساعات
    "https://www.amazon.eg/s?i=hpc&rh=p_8%3A40-&s=featured-rank",         # عناية شخصية وصحة
    "https://www.amazon.eg/s?i=sporting&rh=p_8%3A40-&s=featured-rank"     # رياضة ودراجات
]

# ================= 2. أقسام نون مصر (بخصم 40% إلى 90%) =================
NOON_CATEGORIES = [
    "https://www.noon.com/egypt-ar/electronics-and-mobiles/mobiles-and-accessories/?f[discount][min]=40&f[discount][max]=90",
    "https://www.noon.com/egypt-ar/electronics-and-mobiles/computers-and-accessories/?f[discount][min]=40&f[discount][max]=90",
    "https://www.noon.com/egypt-ar/home-and-kitchen/home-appliances/?f[discount][min]=40&f[discount][max]=90",
    "https://www.noon.com/egypt-ar/tools-and-home-improvement/?f[discount][min]=40&f[discount][max]=90",
    "https://www.noon.com/egypt-ar/automotive/?f[discount][min]=40&f[discount][max]=90"
]

scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
)

# ================= 3. إدارة قاعدة البيانات =================
def init_db():
    conn = sqlite3.connect("egypt_deals.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS price_history (
            product_id TEXT,
            title TEXT,
            price REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerted_deals (
            product_id TEXT PRIMARY KEY,
            alerted_price REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def fetch_kanbkam_history(product_id):
    try:
        search_url = f"https://www.kanbkam.com/eg/ar/search?q={product_id}"
        resp = scraper.get(search_url, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            prices = re.findall(r'(\d[\d,]*\.?\d*)\s*ج\.م', soup.text)
            clean_prices = [float(p.replace(",", "")) for p in prices if float(p.replace(",", "")) > 50]
            if clean_prices:
                return sum(clean_prices) / len(clean_prices), min(clean_prices)
    except Exception:
        pass
    return None, None

# ================= 4. فلتر كشف العروض الوهمية وإرسال الإشعار =================
def verify_and_alert(store_name, product_id, title, current_price, old_price, rating, reviews_count, url):
    if rating > 0 and (rating < 3.8 or reviews_count < 5):
        return

    conn = sqlite3.connect("egypt_deals.db")
    cursor = conn.cursor()

    cursor.execute("SELECT alerted_price FROM alerted_deals WHERE product_id = ?", (product_id,))
    already_sent = cursor.fetchone()
    if already_sent and already_sent[0] <= current_price:
        conn.close()
        return

    cursor.execute("SELECT AVG(price), MIN(price), COUNT(price) FROM price_history WHERE product_id = ?", (product_id,))
    avg_price, min_price, count = cursor.fetchone()

    cursor.execute("INSERT INTO price_history (product_id, title, price) VALUES (?, ?, ?)", (product_id, title, current_price))
    conn.commit()

    if count < 2 or avg_price is None:
        kb_avg, kb_min = fetch_kanbkam_history(product_id)
        if kb_avg:
            avg_price, min_price = kb_avg, kb_min
        elif reviews_count >= 15:
            avg_price = old_price
            min_price = current_price
        else:
            conn.close()
            return

    claimed_discount = ((old_price - current_price) / old_price) * 100 if old_price > current_price else 0
    real_discount = ((avg_price - current_price) / avg_price) * 100 if avg_price > current_price else 0

    if real_discount >= 38 or (claimed_discount >= 45 and real_discount >= 25 and reviews_count >= 35):
        cursor.execute("INSERT OR REPLACE INTO alerted_deals (product_id, alerted_price) VALUES (?, ?)", (product_id, current_price))
        conn.commit()
        send_telegram(store_name, title, current_price, old_price, avg_price, real_discount, rating, reviews_count, url)
    else:
        print(f"[X] عرض وهمي ({store_name}) تم تجاهله: {title[:25]}..")

    conn.close()

def send_telegram(store_name, title, current_price, old_price, avg_price, real_discount, rating, reviews_count, url):
    msg = (
        f"🔥 *لقطة حقيقية على {store_name}!*\n\n"
        f"📦 *المنتج:* {title}\n"
        f"💰 *السعر الآن:* {current_price:,.0f} ج.م\n"
        f"❌ *السعر المشطوب:* {old_price:,.0f} ج.م\n"
        f"📊 *متوسط سعره الحقيقي:* {avg_price:,.0f} ج.م\n"
        f"✅ *الخصم الفعلي:* {real_discount:.1f}%\n"
        f"⭐ *التقييم:* {rating} من 5 ({reviews_count} تقييم)\n\n"
        f"🛒 [اضغط هنا للشراء فوراً]({url})"
    )
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        scraper.post(api_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
        print(f"[+] تم إرسال إشعار ({store_name}): {title[:30]}")
    except Exception as e:
        print(f"[-] خطأ في إرسال تيليجرام: {e}")

# ================= 5. محرك فحص أمازون مصر =================
def scan_amazon_category(category_url):
    print(f"[*] فحص أمازون: {category_url[:50]}...")
    try:
        resp = scraper.get(category_url, timeout=15)
        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.find_all("div", {"data-component-type": "s-search-result"})

        for item in items:
            try:
                asin = item.get("data-asin")
                if not asin:
                    continue

                title_el = item.find("h2")
                title = title_el.text.strip() if title_el else "منتج بدون عنوان"

                price_whole = item.find("span", {"class": "a-price-whole"})
                if not price_whole:
                    continue
                current_price = float(re.sub(r'[^\d.]', '', price_whole.text))

                old_price_el = item.find("span", {"class": "a-text-price"})
                if old_price_el and old_price_el.find("span", {"class": "a-offscreen"}):
                    old_text = old_price_el.find("span", {"class": "a-offscreen"}).text
                    old_price = float(re.sub(r'[^\d.]', '', old_text))
                else:
                    continue

                if ((old_price - current_price) / old_price) * 100 < 40:
                    continue

                rating_el = item.find("span", {"class": "a-icon-alt"})
                rating = float(re.findall(r'\d+\.\d+|\d+', rating_el.text)[0]) if rating_el else 0.0

                reviews_el = item.find("span", {"class": "a-size-base", "dir": "auto"})
                reviews_count = int(re.sub(r'[^\d]', '', reviews_el.text)) if reviews_el and re.sub(r'[^\d]', '', reviews_el.text) else 0

                product_url = f"https://www.amazon.eg/dp/{asin}"
                verify_and_alert("أمازون مصر", asin, title, current_price, old_price, rating, reviews_count, product_url)
            except Exception:
                continue
    except Exception as e:
        print(f"[-] خطأ في فحص أمازون: {e}")

# ================= 6. محرك فحص نون مصر (Noon.com) =================
def scan_noon_category(noon_url):
    print(f"[*] فحص نون مصر: {noon_url[:50]}...")
    try:
        resp = scraper.get(noon_url, timeout=15)
        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        # سحب البيانات من هيكل نون الداخلي (Next.js JSON)
        next_data = soup.find("script", {"id": "__NEXT_DATA__"})
        if not next_data:
            return

        data = json.loads(next_data.string)
        hits = data.get("props", {}).get("pageProps", {}).get("catalog", {}).get("hits", [])

        for item in hits:
            try:
                sku = item.get("sku")
                title = item.get("name", "منتج نون")
                current_price = float(item.get("sale_price") or item.get("price") or 0)
                old_price = float(item.get("price") or 0)

                if current_price == 0 or old_price <= current_price:
                    continue

                if ((old_price - current_price) / old_price) * 100 < 40:
                    continue

                rating = float(item.get("product_rating", {}).get("value", 0.0))
                reviews_count = int(item.get("product_rating", {}).get("count", 0))
                uri = item.get("url", "")
                product_url = f"https://www.noon.com/egypt-ar/{uri}/{sku}/p/" if uri else f"https://www.noon.com/egypt-ar/{sku}/p/"

                verify_and_alert("نون مصر", sku, title, current_price, old_price, rating, reviews_count, product_url)
            except Exception:
                continue
    except Exception as e:
        print(f"[-] خطأ في فحص نون: {e}")

# ================= 7. التشغيل الشامل =================
if __name__ == "__main__":
    init_db()
    print("🚀 بدء جولة الفحص الشامل (أمازون + نون مصر)...")
    
    # 1. مسح أقسام أمازون (صفحتين من كل قسم لضمان سرعة الفحص وعدم الحظر)
    for base_url in AMAZON_CATEGORIES:
        for page in range(1, 3):
            scan_amazon_category(f"{base_url}&page={page}")
            time.sleep(random.randint(3, 6))
            
    # 2. مسح أقسام نون مصر
    for noon_url in NOON_CATEGORIES:
        scan_noon_category(noon_url)
        time.sleep(random.randint(3, 6))
        
    print("✅ انتهت الجولة الشاملة بنجاح وتم تحديث قاعدة البيانات.")
