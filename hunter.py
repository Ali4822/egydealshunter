"""Egypt Deals Hunter: fast, labelled alerts; best-effort storefront HTML only.

No anti-bot bypass. Prefer authorized retailer/affiliate data feeds for production.
Use Python 3.11+. The live retail selectors must be validated on GitHub Actions.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import logging
import os
import re
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("hunter")
CAIRO = dt.timezone(dt.timedelta(hours=3))  # for display only; UTC is stored in SQLite
DB_FILE = os.getenv("DB_FILE", "egypt_deals.db")
MIN_ADVERTISED_PCT = float(os.getenv("MIN_ADVERTISED_PCT", "50"))
MIN_OBSERVED_PCT = float(os.getenv("MIN_OBSERVED_PCT", "25"))
MIN_HISTORY_AGE_MIN = int(os.getenv("MIN_HISTORY_AGE_MIN", "25"))
MAX_WORKERS = min(4, max(1, int(os.getenv("MAX_WORKERS", "3"))))
AMAZON_PER_RUN = max(0, int(os.getenv("AMAZON_PER_RUN", "5")))
NOON_PER_RUN = max(0, int(os.getenv("NOON_PER_RUN", "0")))
# Noon HTML is disabled by default: use approved affiliate/catalog feeds if available.
ENABLE_NOON_HTML = os.getenv("ENABLE_NOON_HTML", "0") == "1"
ALLOW_ADVERTISED_ALERTS = os.getenv("ALLOW_ADVERTISED_ALERTS", "1") == "1"
PRODUCT_CONFIRMATION = os.getenv("PRODUCT_CONFIRMATION", "1") == "1"
USER_AGENT = "Mozilla/5.0 (compatible; EgyptDealsHunter/2.0; personal price watch)"

AMAZON_CATEGORIES = [
    "electronics", "computers", "kitchen", "hi", "automotive", "lawngarden",
    "grocery", "fashion", "hpc", "sporting",
]
NOON_CATEGORIES = [
    "electronics-and-mobiles/mobiles-and-accessories",
    "electronics-and-mobiles/computers-and-accessories",
    "home-and-kitchen/home-appliances",
    "tools-and-home-improvement",
    "automotive",
]


@dataclass(frozen=True)
class Offer:
    store: str
    product_id: str
    title: str
    price: float
    old_price: float | None
    url: str
    rating: float | None = None
    reviews: int | None = None

    @property
    def key(self) -> str:
        return f"{self.store}:{self.product_id}"


def number(s: str | None) -> float | None:
    """Parse Egyptian/Arabic numeric strings; don't silently join decimal digits."""
    if not s:
        return None
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    s = str(s).translate(trans).replace("٫", ".").replace("٬", ",")
    match = re.search(r"\d[\d.,\s]*", s)
    if not match:
        return None
    v = re.sub(r"\s", "", match.group(0)).strip(".,")
    if not v:
        return None
    if "," in v and "." in v:
        # The final separator is assumed to be decimal.
        if v.rfind(",") > v.rfind("."):
            v = v.replace(".", "").replace(",", ".")
        else:
            v = v.replace(",", "")
    elif "," in v:
        parts = v.split(",")
        v = "".join(parts) if len(parts[-1]) == 3 else "".join(parts[:-1]) + "." + parts[-1]
    elif v.count(".") > 1:
        parts = v.split(".")
        v = "".join(parts) if len(parts[-1]) == 3 else "".join(parts[:-1]) + "." + parts[-1]
    try:
        result = float(v)
        return result if 0 < result < 100_000_000 else None
    except ValueError:
        return None


def first_price(node, selectors: list[str]) -> float | None:
    for selector in selectors:
        found = node.select_one(selector)
        if found:
            amount = number(found.get_text(" ", strip=True))
            if amount:
                return amount
    return None


def parse_amazon_listing(page: str) -> list[Offer]:
    soup = BeautifulSoup(page, "html.parser")
    items: list[Offer] = []
    for node in soup.select('[data-component-type="s-search-result"][data-asin]'):
        asin = (node.get("data-asin") or "").strip()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        title_node = node.select_one("h2")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        current = first_price(node, [
            "span.a-price:not(.a-text-price) span.a-offscreen",
            "span.a-price:not(.a-text-price) .a-price-whole",
        ])
        old = first_price(node, ["span.a-text-price span.a-offscreen"])
        if not title or not current:
            continue
        rating = None
        rating_el = node.select_one(".a-icon-alt")
        if rating_el:
            rating = number(rating_el.get_text(" ", strip=True))
        reviews_el = node.select_one("span.a-size-base.s-underline-text")
        count = number(reviews_el.get_text(" ", strip=True)) if reviews_el else None
        items.append(Offer("amazon", asin, title, current, old,
                           f"https://www.amazon.eg/dp/{asin}", rating,
                           int(count) if count is not None else None))
    return items


def walk_noon_hits(node, depth=0):
    """Best-effort for the original __NEXT_DATA__ structure; never assume present."""
    if depth > 12:
        return []
    if isinstance(node, dict):
        if "hits" in node and isinstance(node["hits"], list):
            return node["hits"]
        for val in node.values():
            result = walk_noon_hits(val, depth + 1)
            if result:
                return result
    elif isinstance(node, list):
        for val in node[:30]:
            result = walk_noon_hits(val, depth + 1)
            if result:
                return result
    return []


def parse_noon_listing(page: str) -> list[Offer]:
    soup = BeautifulSoup(page, "html.parser")
    tag = soup.select_one('script#__NEXT_DATA__')
    if not tag or not tag.string:
        return []
    try:
        hits = walk_noon_hits(json.loads(tag.string))
    except (ValueError, TypeError):
        return []
    parsed = []
    for item in hits:
        if not isinstance(item, dict):
            continue
        sku = str(item.get("sku", "")).strip()
        title = item.get("name")
        current = number(str(item.get("sale_price") or item.get("price") or ""))
        old = number(str(item.get("price") or ""))
        # Missing url/identifiers must not be fabricated.
        uri = item.get("url", "")
        if not sku or not title or not current or not uri:
            continue
        product_url = urljoin("https://www.noon.com", str(uri))
        if urlparse(product_url).hostname not in ("noon.com", "www.noon.com"):
            continue
        parsed.append(Offer("noon", sku, str(title), current, old, product_url))
    return parsed


def jsonld_products(soup):
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            raw = json.loads(tag.string or tag.get_text())
        except (TypeError, ValueError):
            continue
        stack = raw if isinstance(raw, list) else [raw]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("@graph"), list):
                stack.extend(item["@graph"])
            typeval = item.get("@type", "")
            if "Product" in (typeval if isinstance(typeval, list) else [typeval]):
                yield item


def detail_price(page: str, store: str) -> tuple[float | None, bool]:
    """Price and availability from product detail; (None, False) is no evidence."""
    soup = BeautifulSoup(page, "html.parser")
    for product in jsonld_products(soup):
        offers = product.get("offers") or {}
        for offer in (offers if isinstance(offers, list) else [offers]):
            if not isinstance(offer, dict):
                continue
            if "OutOfStock" in str(offer.get("availability", "")):
                return None, False
            price = number(str(offer.get("price") or offer.get("lowPrice") or ""))
            currency = str(offer.get("priceCurrency") or "EGP").upper()
            if price and currency == "EGP":
                return price, True
    if store == "amazon":
        availability = soup.select_one("#availability")
        if availability and any(term in availability.get_text(" ", strip=True).lower()
                                for term in ("currently unavailable", "غير متوفر")):
            return None, False
        amount = first_price(soup, [
            '#corePrice_feature_div .priceToPay .a-offscreen',
            '#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen',
            '#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen',
            '#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen',
            '#priceblock_ourprice', '#priceblock_dealprice',
        ])
        return amount, bool(amount)
    return None, False


class State:
    """One process, several scanner threads, serialized SQLite operations."""
    def __init__(self, filename=DB_FILE):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(filename, check_same_thread=False, timeout=15)
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS observed_v2 (
                  product_key TEXT NOT NULL, at_unix INTEGER NOT NULL,
                  price REAL NOT NULL, PRIMARY KEY(product_key, at_unix)
                );
                CREATE INDEX IF NOT EXISTS idx_obs_product ON observed_v2(product_key,at_unix);
                CREATE TABLE IF NOT EXISTS alert_v2 (
                  product_key TEXT PRIMARY KEY, price REAL NOT NULL,
                  confidence INTEGER NOT NULL, at_unix INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta_v2 (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            """)
            self.db.commit()

    def next_category_offset(self, name: str, count: int, total: int) -> int:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta_v2 WHERE name=?", (name,)).fetchone()
            start = row[0] if row else 0
            self.db.execute("INSERT OR REPLACE INTO meta_v2(name,value) VALUES(?,?)",
                            (name, (start + count) % total))
            self.db.commit()
        return start

    def baseline_and_record(self, key: str, price: float, now: int) -> tuple[float | None, int]:
        with self.lock:
            # Only historic actual observations; don't mix with crossed-out prices.
            previous = self.db.execute(
                "SELECT price, at_unix FROM observed_v2 "
                "WHERE product_key=? AND at_unix>? ORDER BY at_unix DESC LIMIT 100",
                (key, now - 30 * 86400),
            ).fetchall()
            # Distinct observations must span a meaningful period; same-run duplicates not evidence.
            aged = [row for row in previous if row[1] <= now - MIN_HISTORY_AGE_MIN * 60]
            baseline = statistics.median([p for p, _ in aged[:30]]) if len(aged) >= 2 else None
            latest = previous[0] if previous else None
            # Save changes at once; unchanged products at most once each 15 min.
            if not latest or latest[0] != price or now - latest[1] >= 900:
                self.db.execute("INSERT OR IGNORE INTO observed_v2 VALUES(?,?,?)",
                                (key, now, price))
                self.db.commit()
            return baseline, len(aged)

    def should_alert(self, key: str, price: float, confidence: int) -> bool:
        with self.lock:
            row = self.db.execute(
                "SELECT price, confidence FROM alert_v2 WHERE product_key=?", (key,)
            ).fetchone()
            if not row:
                return True
            previous_price, level = row
            # Send again for a >=5% further drop or confidence upgrade.
            return price <= previous_price * 0.95 or (confidence > level and price <= previous_price * 1.01)

    def alerted(self, key: str, price: float, confidence: int):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO alert_v2 VALUES(?,?,?,?)",
                            (key, price, confidence, int(time.time())))
            self.db.commit()

    def reserve_health_warning(self, reason: str, now: int) -> bool:
        """Only one source-health warning every three hours per reason."""
        key = "health_warning_" + reason
        with self.lock:
            row = self.db.execute("SELECT value FROM meta_v2 WHERE name=?", (key,)).fetchone()
            if row and now - row[0] < 3 * 3600:
                return False
            self.db.execute("INSERT OR REPLACE INTO meta_v2 VALUES(?, ?)", (key, now))
            self.db.commit()
            return True

    def housekeeping(self):
        with self.lock:
            self.db.execute("DELETE FROM observed_v2 WHERE at_unix < ?",
                            (int(time.time()) - 60 * 86400,))
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()


class Hunter:
    def __init__(self, state: State, session_factory=requests.Session):
        self.state = state
        self.session_factory = session_factory
        self._thread_sessions = threading.local()
        self.seen = set()
        self.seen_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.stats = {"pages_ok": 0, "pages_error": 0, "items": 0, "candidates": 0,
                      "confirmed": 0, "sent": 0, "send_errors": 0}

    def inc(self, name: str, delta=1):
        with self.stats_lock:
            self.stats[name] += delta

    def session(self):
        if not hasattr(self._thread_sessions, "session"):
            sess = self.session_factory()
            sess.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ar-EG,ar;q=0.8,en;q=0.5"})
            self._thread_sessions.session = sess
        return self._thread_sessions.session

    def fetch(self, url: str) -> str | None:
        # Never bypass CAPTCHAs. Treat blocked pages and throttling as unhealthy sources.
        try:
            resp = self.session().get(url, timeout=(5, 12))
            if resp.status_code in (403, 429, 503):
                LOG.warning("Blocked or throttled: HTTP %s %s", resp.status_code, url[:120])
                self.inc("pages_error")
                return None
            resp.raise_for_status()
            self.inc("pages_ok")
            if any(s in resp.text[:6000].lower() for s in ("validatecaptcha", "captcha-form", "robot check")):
                LOG.warning("Page challenge for %s", url[:120])
                self.inc("pages_error")
                return None
            return resp.text
        except requests.RequestException as exc:
            LOG.warning("Network request failed for %s: %s", url[:120], exc.__class__.__name__)
            self.inc("pages_error")
            return None

    def telegram_status(self, message: str) -> bool:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return False
        try:
            response = self.session().post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message}, timeout=(5, 10))
            response.raise_for_status()
            return bool(response.json().get("ok"))
        except (requests.RequestException, ValueError):
            LOG.warning("Status Telegram message not delivered")
            return False

    def telegram(self, offer: Offer, actual_price: float, confidence: int,
                 pct: float, baseline: float | None) -> bool:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            LOG.error("Telegram secrets absent")
            self.inc("send_errors")
            return False
        label = "✅ نزول سعر مثبت بمتابعة سابقة" if confidence == 2 else "⚡ سعر مؤكد الآن، الخصم المعلن غير مثبت تاريخيًا"
        message = (
            f"<b>{label}</b>\n"
            f"🏪 {html.escape(offer.store.upper())}\n"
            f"📦 {html.escape(offer.title[:250])}\n"
            f"💰 <b>{actual_price:,.2f} ج.م</b>\n"
            f"📉 {('انخفاض رصدناه' if confidence == 2 else 'خصم معلن')}: {pct:.1f}%\n"
            + (f"📊 السعر السابق المرصود: {baseline:,.2f} ج.م\n" if baseline and confidence == 2 else "")
            + (f"🏷 السعر المشطوب: {offer.old_price:,.2f} ج.م\n" if offer.old_price else "")
            + f"🕒 الفحص: {dt.datetime.now(CAIRO):%Y-%m-%d %H:%M} القاهرة\n"
            + f'<a href="{html.escape(offer.url, quote=True)}">🛒 افتح المنتج واطلب</a>\n'
            + "⚠️ تحقق من المخزون والشحن والسعر النهائي عند الدفع."
        )
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for attempt in range(2):
            try:
                resp = self.session().post(url, json={"chat_id": chat_id, "text": message,
                                                      "parse_mode": "HTML", "disable_web_page_preview": True},
                                           timeout=(5, 10))
                if resp.status_code == 429 and attempt == 0:
                    delay = min(10, max(1, int(resp.json().get("parameters", {}).get("retry_after", 3))))
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                if not resp.json().get("ok"):
                    raise ValueError("Telegram returned ok=false")
                self.inc("sent")
                LOG.info("ALERT SENT: %s %.2f (%s)", offer.key, actual_price, label)
                return True
            except (requests.RequestException, ValueError) as exc:
                LOG.warning("Telegram send failed: %s", exc.__class__.__name__)
                break
        self.inc("send_errors")
        return False

    def handle(self, offer: Offer, trusted_feed: bool = False):
        if not (offer.price and offer.price > 0):
            return
        with self.seen_lock:
            if offer.key in self.seen:
                return
            self.seen.add(offer.key)
        self.inc("items")
        now = int(time.time())
        baseline, n = self.state.baseline_and_record(offer.key, offer.price, now)
        actual_pct = (baseline - offer.price) / baseline * 100 if baseline and baseline > offer.price else 0
        advertised_pct = ((offer.old_price - offer.price) / offer.old_price * 100
                          if offer.old_price and offer.old_price > offer.price else 0)
        has_observed_drop = n >= 2 and actual_pct >= MIN_OBSERVED_PCT
        has_advertised_drop = (ALLOW_ADVERTISED_ALERTS and advertised_pct >= MIN_ADVERTISED_PCT)
        if not (has_observed_drop or has_advertised_drop):
            return
        self.inc("candidates")
        confidence = 2 if has_observed_drop else 1
        # Product-detail re-check prevents alerts based on stale search listing cards.
        confirmed_price = offer.price
        if PRODUCT_CONFIRMATION and not trusted_feed:
            detail = self.fetch(offer.url)
            if not detail:
                LOG.info("SKIP: unable to recheck product detail %s", offer.key)
                return
            confirmed_price, available = detail_price(detail, offer.store)
            if not available or confirmed_price is None:
                LOG.info("SKIP: unavailable/unparsable product detail %s", offer.key)
                return
            if confirmed_price > offer.price * 1.03:
                LOG.info("SKIP: search price stale vs detail %s", offer.key)
                return
        self.inc("confirmed")
        confirmed_actual_pct = ((baseline - confirmed_price) / baseline * 100
                                if baseline and baseline > confirmed_price else 0)
        confirmed_ad_pct = ((offer.old_price - confirmed_price) / offer.old_price * 100
                            if offer.old_price and offer.old_price > confirmed_price else 0)
        if n >= 2 and confirmed_actual_pct >= MIN_OBSERVED_PCT:
            confidence, pct = 2, confirmed_actual_pct
        elif ALLOW_ADVERTISED_ALERTS and confirmed_ad_pct >= MIN_ADVERTISED_PCT:
            confidence, pct = 1, confirmed_ad_pct
        else:
            return
        if not self.state.should_alert(offer.key, confirmed_price, confidence):
            return
        if self.telegram(offer, confirmed_price, confidence, pct, baseline):
            self.state.alerted(offer.key, confirmed_price, confidence)

    def scan(self, kind: str, url: str):
        page = self.fetch(url)
        if not page:
            return
        offers = parse_amazon_listing(page) if kind == "amazon" else parse_noon_listing(page)
        LOG.info("%s: parsed %s items from %s", kind, len(offers), url[:110])
        if not offers:
            LOG.warning("ZERO PRODUCTS: parsing changed, no products, or blocked content: %s", url[:110])
        for offer in offers:
            try:
                self.handle(offer)
            except Exception:
                LOG.exception("Failed while handling %s", offer.key)
                self.inc("pages_error")

    def collect_watchlist_item(self, item: dict):
        try:
            store, product_id = item["store"].lower(), str(item["id"]).strip()
            if store == "amazon" and re.fullmatch("[A-Z0-9]{10}", product_id):
                url = f"https://www.amazon.eg/dp/{product_id}"
            elif store == "noon" and item.get("url"):
                url = item["url"]
            else:
                return
            host = urlparse(url).hostname
            if host not in ("www.amazon.eg", "amazon.eg", "www.noon.com", "noon.com"):
                return
            page = self.fetch(url)
            if not page:
                return
            price, available = detail_price(page, store)
            if not available or not price:
                return
            title = BeautifulSoup(page, "html.parser").title
            name = title.get_text(" ", strip=True) if title else product_id
            self.handle_watchlisted(Offer(store, product_id, name, price, None, url))
        except (KeyError, TypeError, AttributeError):
            LOG.warning("Skipping invalid watchlist item")

    def watchlist(self, path: Path):
        if not path.is_file():
            return
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                raise ValueError("watchlist must be a JSON array")
        except (OSError, ValueError) as exc:
            LOG.error("Bad watchlist: %s", exc)
            self.inc("pages_error")
            return
        # Priority items are checked concurrently, not in a 100-item serial queue.
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(self.collect_watchlist_item, item) for item in items[:30]
                       if isinstance(item, dict)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception:
                    LOG.exception("Watchlist check failed")
                    self.inc("pages_error")

    def approved_feed(self, path: Path):
        """Optional normalized JSON export supplied by a *licensed* feed provider.

        Array fields: store, id, title, price, old_price(optional), url, updated_at.
        This is our interface; it is NOT claimed to be noon/Amazon's native schema.
        """
        if not path.is_file():
            return
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                raise ValueError("feed must be an array")
            for item in items[:1000]:
                try:
                    store = str(item["store"]).lower()
                    product_id = str(item["id"]).strip()
                    url = str(item["url"])
                    host = urlparse(url).hostname
                    if store not in ("amazon", "noon") or not product_id:
                        continue
                    valid_hosts = {"amazon": ("www.amazon.eg", "amazon.eg"),
                                   "noon": ("www.noon.com", "noon.com")}
                    if host not in valid_hosts[store] or not url.startswith("https://"):
                        continue
                    price = number(str(item["price"]))
                    old_price = number(str(item.get("old_price") or ""))
                    # Avoid relaying stale vendor prices from an old feed export.
                    updated_at = dt.datetime.fromisoformat(
                        str(item["updated_at"]).replace("Z", "+00:00"))
                    if updated_at.tzinfo is None:
                        raise ValueError("approved feed timestamp must include UTC offset")
                    age = (dt.datetime.now(dt.timezone.utc) - updated_at).total_seconds()
                    if not (-90 <= age <= int(os.getenv("FEED_MAX_AGE_SECONDS", "600"))):
                        LOG.info("Feed item stale or future dated: %s", product_id)
                        continue
                    if price:
                        # Licensed feed's recently observed price is the live verification.
                        self.handle(Offer(store, product_id, str(item["title"]),
                                          price, old_price, url), trusted_feed=True)
                except (KeyError, TypeError, ValueError):
                    LOG.warning("Invalid approved feed item")
        except (OSError, ValueError) as exc:
            LOG.error("Feed read error: %s", exc)
            self.inc("pages_error")

    def handle_watchlisted(self, offer: Offer):
        with self.seen_lock:
            if offer.key in self.seen:
                return
            self.seen.add(offer.key)
        self.inc("items")
        baseline, n = self.state.baseline_and_record(offer.key, offer.price, int(time.time()))
        pct = ((baseline - offer.price) / baseline * 100
               if baseline and baseline > offer.price else 0)
        if n >= 2 and pct >= MIN_OBSERVED_PCT and self.state.should_alert(offer.key, offer.price, 2):
            self.inc("candidates")
            self.inc("confirmed")  # Price came from the product-detail page.
            if self.telegram(offer, offer.price, 2, pct, baseline):
                self.state.alerted(offer.key, offer.price, 2)

    def run_once(self):
        start = time.monotonic()
        # Watchlist first: user-specified products are a smaller/faster critical path.
        self.watchlist(Path(os.getenv("WATCHLIST_FILE", "watchlist.json")))
        self.approved_feed(Path(os.getenv("APPROVED_FEED_FILE", "approved_feed.json")))
        tasks = []
        if AMAZON_PER_RUN:
            start_index = self.state.next_category_offset(
                "amazon_offset", AMAZON_PER_RUN, len(AMAZON_CATEGORIES))
            for index in range(AMAZON_PER_RUN):
                category = AMAZON_CATEGORIES[(start_index + index) % len(AMAZON_CATEGORIES)]
                tasks.append(("amazon", f"https://www.amazon.eg/s?i={category}&rh=p_8%3A40-&s=featured-rank"))
        if ENABLE_NOON_HTML and NOON_PER_RUN:
            start_index = self.state.next_category_offset("noon_offset", NOON_PER_RUN,
                                                           len(NOON_CATEGORIES))
            for index in range(NOON_PER_RUN):
                category = NOON_CATEGORIES[(start_index + index) % len(NOON_CATEGORIES)]
                tasks.append(("noon", f"https://www.noon.com/egypt-ar/{category}/"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(self.scan, *task) for task in tasks]
            for future in concurrent.futures.as_completed(futures):
                future.result()
        self.state.housekeeping()
        LOG.info("RUN SUMMARY duration=%.1fs stats=%s", time.monotonic() - start,
                 json.dumps(self.stats, ensure_ascii=False))
        if self.stats["pages_ok"] == 0 and self.stats["items"] == 0:
            raise RuntimeError("No successful page reads or feed products; check blocked sources")
        if self.stats["send_errors"]:
            raise RuntimeError(f"{self.stats['send_errors']} Telegram alerts failed")
        if self.stats["items"] == 0:
            raise RuntimeError("No products parsed; selectors/site access need investigation")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one round and exit (for GitHub Actions)")
    parser.add_argument("--test-telegram", action="store_true", help="Test Telegram only, no retailer scans")
    parser.add_argument("--interval", type=int, default=60, help="VPS mode polling seconds, >=60")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first")
    if not args.once and args.interval < 60:
        raise SystemExit("Minimum interval is 60 seconds; respect source rate limits")
    if args.test_telegram:
        if not Hunter(State(":memory:")).telegram_status("✅ Egypt Deals Hunter: اتصال تيليجرام ناجح"):
            raise SystemExit("Telegram connection failed: verify bot token and chat ID")
        LOG.info("Telegram smoke test succeeded")
        return
    state = State(DB_FILE)
    try:
        while True:
            start = time.monotonic()
            hunter = Hunter(state)
            try:
                hunter.run_once()
            except Exception as exc:
                LOG.exception("Scan round failed")
                if state.reserve_health_warning("scan", int(time.time())):
                    hunter.telegram_status(
                        "⚠️ Egypt Deals Hunter: فشل الفحص أو لم يتم العثور على منتجات. "
                        "الرجاء مراجعة GitHub Actions. آخر خطأ: " + str(exc)[:180])
                if args.once:
                    raise
            if args.once:
                return
            time.sleep(max(0, args.interval - (time.monotonic() - start)))
    finally:
        state.close()


if __name__ == "__main__":
    main()
