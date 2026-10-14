import sqlite3
import time
import random
import re
import os
import cloudscraper
from bs4 import BeautifulSoup

# سحب بيانات تيليجرام من إعدادات السحابة الآمنة (أو ضعها هنا مباشرة إذا كان حسابك Private)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ضع_توكن_البوت_هنا")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "ضع_الأيدي_الخاص_بك_هنا")

TARGET_CATEGORIES = [
    # موبايلات وإلكترونيات في أمازون مصر بخصم أكثر من 40%
    "https://www.amazon.eg/s?i=electronics&rh=p_8%3A40-&s=featured-rank",
    # أجهزة منزلية ومطبخ في أمازون مصر بخصم أكثر من 40%
    "https://www.amazon.eg/s?i=kitchen&rh=p_8%3A40-&s=featured-rank"
]

scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
)

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

def verify_and_alert(product_id, title, current_price, old_price, rating, reviews_count, url):
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
        elif reviews_count >= 20:
            avg_price = old_price
            min_price = current_price
        else:
            conn.close()
            return

    claimed_discount = ((old_price - current_price) / old_price) * 100 if old_price > current_price else 0
    real_discount = ((avg_price - current_price) / avg_price) * 100 if avg_price > current_price else 0

    if real_discount >= 38 or (claimed_discount >= 45 and real_discount >= 25 and reviews_count >= 50):
        cursor.execute("INSERT OR REPLACE INTO alerted_deals (product_id, alerted_price) VALUES (?, ?)", (product_id, current_price))
        conn.commit()
        send_telegram(title, current_price, old_price, avg_price, real_discount, rating, reviews_count, url)
    else:
        print(f"[X] عرض وهمي تم تجاهله: {title[:25]}..")

    conn.close()

def send_telegram(title, current_price, old_price, avg_price, real_discount, rating, reviews_count, url):
    msg = (
        f"🔥 *لقطة حقيقية في السوق المصري!*\n\n"
        f"📦 *المنتج:* {title}\n"
        f"💰 *السعر الآن:* {current_price:,.0f} ج.م\n"
        f"❌ *السعر المشطوب:* {old_price:,.0f} ج.م\n"
        f"📊 *متوسط سعره الحقيقي:* {avg_price:,.0f} ج.م\n"
        f"✅ *الخصم الفعلي:* {real_discount:.1f}%\n"
        f"⭐ *التقييم:* {rating} من 5 ({reviews_count} تقييم)\n\n"
        f"🛒 [اضغط هنا لفتح صفحة المنتج فوراً]({url})"
    )
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        scraper.post(api_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
        print(f"[+] تم إرسال إشعار: {title[:30]}")
    except Exception as e:
        print(f"[-] خطأ في إرسال تيليجرام: {e}")

def scan_amazon_category(category_url):
    print(f"[*] جاري فحص القسم: {category_url[:45]}...")
    try:
        resp = scraper.get(category_url, timeout=15)
        if resp.status_code != 200:
            print(f"[-] تعذر فتح الصفحة (كود {resp.status_code})")
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
                verify_and_alert(asin, title, current_price, old_price, rating, reviews_count, product_url)
            except Exception:
                continue
    except Exception as e:
        print(f"[-] خطأ أثناء الفحص: {e}")

if __name__ == "__main__":
    init_db()
    print("🚀 بدء جولة الفحص السحابي...")
    for url in TARGET_CATEGORIES:
        scan_amazon_category(url)
        time.sleep(random.randint(5, 10))
    print("✅ انتهت الجولة بنجاح وتم تحديث قاعدة البيانات.")