"""Egypt Deals Hunter SMART v5: evidence-based, automatic pricing decisions.

Price checks are best effort; requires permitted data sources for reliable scale.
No anti-bot bypass. Existing SQLite observations are retained.
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
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("hunter")
CAIRO = ZoneInfo("Africa/Cairo")  # daylight-saving-aware; UTC stored in SQLite
DB_FILE = os.getenv("DB_FILE", "egypt_deals.db")
MAX_WORKERS = min(4, max(1, int(os.getenv("MAX_WORKERS", "3"))))
AMAZON_PER_RUN = max(0, int(os.getenv("AMAZON_PER_RUN", "5")))
NOON_PER_RUN = max(0, int(os.getenv("NOON_PER_RUN", "0")))
# Noon HTML is disabled by default: use approved affiliate/catalog feeds if available.
ENABLE_NOON_HTML = os.getenv("ENABLE_NOON_HTML", "0") == "1"
# Do not use merchant crossed-out prices as an alert signal.
AMAZON_PAGES_PER_CATEGORY = min(3, max(1, int(os.getenv("AMAZON_PAGES_PER_CATEGORY", "2"))))
PRODUCT_CONFIRMATION = os.getenv("PRODUCT_CONFIRMATION", "1") == "1"
AUTO_WATCH_PER_RUN = min(25, max(0, int(os.getenv("AUTO_WATCH_PER_RUN", "10"))))
USER_AGENT = "EgyptDealsHunter/5.0 (price monitoring; respects blocks)"
SOURCE_MODE = os.getenv("SOURCE_MODE", "auto").lower()  # auto / feed / html

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
    gtin: str | None = None
    total_verified: bool = False  # feed price includes mandatory shipping

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


@dataclass(frozen=True)
class Decision:
    confidence: int   # 2 = historical evidence, 3 = independent current market quotes
    baseline: float
    pct: float
    evidence: str
    score: int         # adaptive statistical opportunity score, NOT a trained AI probability


def validate_gtin(value: object) -> str | None:
    """Accept GTIN-8/12/13/14 with valid GS1 check digit. Exact variant match only."""
    digits = re.sub(r"[ -]", "", str(value or ""))
    if len(digits) not in (8, 12, 13, 14) or not digits.isascii() or not digits.isdigit():
        return None
    weighted = sum(int(d) * (3 if i % 2 == 0 else 1)
                   for i, d in enumerate(reversed(digits[:-1])))
    expected = (-weighted) % 10
    return digits if expected == int(digits[-1]) else None


def percentile(sorted_values: list[float], q: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    i = q * (len(sorted_values) - 1)
    lo = int(i)
    return sorted_values[lo] * (1 - (i - lo)) + sorted_values[min(len(sorted_values) - 1, lo + 1)] * (i - lo)


def automatic_history_decision(previous: list[tuple[float, int]], current: float,
                               now: int) -> Decision | None:
    """Learn a conservative normal price using independent 30-min bins.

    Never uses MSRP/strike-through price. Need 3 prior calendar days, or
    4 separated bins spanning 2h; confidence adjusts for coverage.
    """
    bins: dict[int, list[float]] = {}
    recent_days = set()
    for price, stamp in previous:
        if not (price > 0 and now - 90 * 86400 <= stamp < now - 5 * 60):
            continue
        key = int(stamp) // 1800
        bins.setdefault(key, []).append(float(price))
        recent_days.add(dt.datetime.fromtimestamp(stamp, dt.timezone.utc).date())
    if not bins:
        return None
    n = len(bins)
    span = (max(bins) - min(bins)) * 1800
    if not (len(recent_days) >= 3 or (n >= 4 and span >= 2 * 3600)):
        return None
    samples = sorted(statistics.median(x) for x in bins.values())
    # Lower-quartile of normal prices resists artificial high-price spikes.
    floor = percentile(samples, 0.25)
    if current >= floor:
        return None
    median = statistics.median(samples)
    mad = statistics.median(abs(v - median) for v in samples)
    # Scale adapts to volatility, with small allowance for normal fluctuations.
    variability = max(1.4826 * mad, floor * .02, 0.1)
    surprise = (floor - current) / variability
    # Scores are evidence rankings, never guarantees of value.
    if surprise < 4.0:
        return None
    change_pct = (floor - current) / floor * 100
    coverage = min(1.0, n / 10)
    multi_day = len(recent_days) >= 3
    score = round(min(96, 48 + 4.0 * min(8, surprise - 4) +
                      10 * coverage + (12 if multi_day else 0)))
    if score < 58:
        return None
    confidence = 2
    evidence = (f"مقارنة بتاريخ مرصود ({n} فترات مستقلة، "
                f"{len(recent_days)} أيام)، مرجع محافظ لا يعتمد على السعر المشطوب")
    return Decision(confidence, floor, change_pct, evidence, score)


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
                CREATE TABLE IF NOT EXISTS market_quotes_v4 (
                  gtin TEXT NOT NULL, merchant TEXT NOT NULL,
                  product_key TEXT NOT NULL, total REAL NOT NULL,
                  at_unix INTEGER NOT NULL,
                  PRIMARY KEY(gtin,merchant,product_key)
                );
                CREATE INDEX IF NOT EXISTS idx_market_gtin ON market_quotes_v4(gtin,at_unix);
            """)
            self.db.commit()
            self._migrate_legacy_if_present()

    def _migrate_legacy_if_present(self):
        """One-off migration: only actual historical observed prices from legacy Amazon ASINs.

        Never imports crossed-out prices or old alert decisions.
        SQLite CURRENT_TIMESTAMP in the old schema was UTC.
        """
        if self.db.execute("SELECT 1 FROM meta_v2 WHERE name='legacy_migrated'").fetchone():
            return
        exists = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_history'"
        ).fetchone()
        migrated = 0
        if exists:
            now = int(time.time())
            try:
                rows = self.db.execute(
                    "SELECT product_id, price, timestamp FROM price_history LIMIT 50000"
                ).fetchall()
                for product_id, price, timestamp in rows:
                    asin = str(product_id or '').strip().upper()
                    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
                        continue
                    price = float(price)
                    if not 0 < price < 100_000_000:
                        continue
                    when = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=dt.timezone.utc)
                    stamp = int(when.timestamp())
                    if not now - 90 * 86400 <= stamp <= now:
                        continue
                    cur = self.db.execute(
                        "INSERT OR IGNORE INTO observed_v2(product_key,at_unix,price) VALUES(?,?,?)",
                        (f"amazon:{asin}", stamp, price))
                    migrated += cur.rowcount
            except (sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
                LOG.warning("Legacy history migration incomplete: %s", exc)
                raise
        self.db.execute("INSERT OR REPLACE INTO meta_v2(name,value) VALUES('legacy_migrated',?)", (int(time.time()),))
        self.db.commit()
        LOG.info("Legacy observed price history migrated: %s rows", migrated)

    def next_category_offset(self, name: str, count: int, total: int) -> int:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta_v2 WHERE name=?", (name,)).fetchone()
            start = row[0] if row else 0
            self.db.execute("INSERT OR REPLACE INTO meta_v2(name,value) VALUES(?,?)",
                            (name, (start + count) % total))
            self.db.commit()
        return start

    def historical_prices_and_record(self, key: str, price: float,
                                     now: int) -> list[tuple[float, int]]:
        with self.lock:
            previous = self.db.execute(
                "SELECT price,at_unix FROM observed_v2 WHERE product_key=? "
                "AND at_unix>=? ORDER BY at_unix DESC LIMIT 3000",
                (key, now - 90 * 86400)).fetchall()
            if not previous or previous[0][0] != price or now - previous[0][1] >= 300:
                self.db.execute("INSERT OR IGNORE INTO observed_v2 VALUES(?,?,?)", (key, now, price))
                self.db.commit()
            return [(float(v), int(t)) for v, t in previous]

    def record_market_quote(self, gtin: str, merchant: str, key: str,
                            all_in_price: float, at_unix: int):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO market_quotes_v4 VALUES(?,?,?,?,?)",
                            (gtin,merchant,key,all_in_price,at_unix))
            self.db.commit()

    def market_decision(self, gtin: str | None, store: str, key: str,
                        current: float, now: int) -> Decision | None:
        if not gtin:
            return None
        with self.lock:
            rows = self.db.execute(
                "SELECT merchant,product_key,total FROM market_quotes_v4 "
                "WHERE gtin=? AND at_unix>=? AND product_key<>?",
                (gtin,now-3600,key)).fetchall()
        # Require two INDEPENDENT competing merchants, not duplicated listings.
        by_merchant = {}
        for merchant, other_key, total in rows:
            if merchant == store or other_key == key:
                continue
            by_merchant[merchant] = min(by_merchant.get(merchant, float('inf')), float(total))
        if len(by_merchant) < 2:
            return None
        conservative_competitor = min(by_merchant.values())
        if current >= conservative_competitor:
            return None
        gap = (conservative_competitor - current) / conservative_competitor * 100
        # Bound trivial rounding/shipping noise; calibrated against dispersion of peer prices.
        peer_spread = statistics.pstdev(by_merchant.values()) if len(by_merchant) > 1 else 0
        noise = max(0.03 * conservative_competitor, min(peer_spread, 0.12 * conservative_competitor))
        if conservative_competitor - current <= noise:
            return None
        score = round(min(97, 68 + 40 * (conservative_competitor - current) /
                          max(conservative_competitor, 1)))
        return Decision(3, conservative_competitor, gap,
                        f"مقارنة آلية لسعر شامل الشحن مع {len(by_merchant)} متاجر مختلفة "
                        f"بنفس GTIN، بيانات أقل من ساعة",score)

    def automatic_priority(self, limit: int) -> list[dict]:
        """Rotate recently discovered Amazon products automatically; no hand-made watchlist."""
        if limit <= 0:
            return []
        now = int(time.time())
        with self.lock:
            rows = self.db.execute(
                "SELECT product_key,MAX(at_unix) as seen FROM observed_v2 "
                "WHERE at_unix>=? AND product_key LIKE 'amazon:%' "
                "GROUP BY product_key ORDER BY seen DESC LIMIT 180", (now-2*86400,)).fetchall()
        if not rows:
            return []
        # Round-robin prevents always rechecking only newest products.
        offset = self.next_category_offset('automatic_priority_offset',limit,len(rows))
        selected = [rows[(offset+i)%len(rows)][0] for i in range(min(limit,len(rows)))]
        return [{'store':'amazon','id':key.split(':',1)[1]} for key in selected]

    def should_alert(self, key: str, price: float, confidence: int) -> bool:
        with self.lock:
            row = self.db.execute(
                "SELECT price, confidence FROM alert_v2 WHERE product_key=?", (key,)
            ).fetchone()
            if not row:
                return True
            previous_price, _level = row
            # One message per price level, then again after a further meaningful drop.
            return price <= previous_price * 0.95

    def alerted(self, key: str, price: float, confidence: int):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO alert_v2 VALUES(?,?,?,?)",
                            (key, price, confidence, int(time.time())))
            self.db.commit()

    def reserve_health_warning(self, reason: str, now: int, interval: int = 3 * 3600) -> bool:
        """Rate-limit health and heartbeat messages across scheduled runs."""
        key = "health_warning_" + reason
        with self.lock:
            row = self.db.execute("SELECT value FROM meta_v2 WHERE name=?", (key,)).fetchone()
            if row and now - row[0] < interval:
                return False
            self.db.execute("INSERT OR REPLACE INTO meta_v2 VALUES(?, ?)", (key, now))
            self.db.commit()
            return True

    def housekeeping(self):
        with self.lock:
            self.db.execute("DELETE FROM observed_v2 WHERE at_unix < ?",
                            (int(time.time()) - 90 * 86400,))
            self.db.execute("DELETE FROM market_quotes_v4 WHERE at_unix < ?",
                            (int(time.time()) - 2 * 86400,))
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
        self.blocked_count = 0
        self.block_lock = threading.Lock()
        self.circuit_open = threading.Event()
        self.stats = {"pages_ok": 0, "pages_error": 0, "items": 0, "candidates": 0,
                      "confirmed": 0, "sent": 0, "send_errors": 0,
                      "listing_attempts": 0, "listing_healthy": 0, "listing_bad": 0,
                      "feed_items": 0}

    def inc(self, name: str, delta=1):
        with self.stats_lock:
            self.stats[name] += delta

    def session(self):
        if not hasattr(self._thread_sessions, "session"):
            sess = self.session_factory()
            sess.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ar-EG,ar;q=0.8,en;q=0.5"})
            self._thread_sessions.session = sess
        return self._thread_sessions.session

    def mark_blocked(self, url: str):
        if urlparse(url).hostname not in ("www.amazon.eg", "amazon.eg"):
            return
        with self.block_lock:
            self.blocked_count += 1
            if self.blocked_count >= 2:
                self.circuit_open.set()
                LOG.error("AMAZON CIRCUIT OPEN: repeated CAPTCHA/503, stopping requests for this run")

    def fetch(self, url: str) -> str | None:
        # Never bypass CAPTCHAs. Stop hammering an unhealthy Amazon source.
        if self.circuit_open.is_set() and urlparse(url).hostname in ("amazon.eg", "www.amazon.eg"):
            return None
        try:
            resp = self.session().get(url, timeout=(5, 12))
            if resp.status_code in (403, 429, 503):
                LOG.warning("Blocked or throttled: HTTP %s %s", resp.status_code, url[:120])
                self.inc("pages_error")
                self.mark_blocked(url)
                return None
            resp.raise_for_status()
            if any(s in resp.text[:6000].lower() for s in
                   ("validatecaptcha", "captcha-form", "robot check", "enter the characters you see below")):
                LOG.warning("Page challenge for %s", url[:120])
                self.inc("pages_error")
                self.mark_blocked(url)
                return None
            self.inc("pages_ok")
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

    def telegram(self, offer: Offer, actual_price: float, decision: Decision) -> bool:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            LOG.error("Telegram secrets absent")
            self.inc("send_errors")
            return False
        label = ("مقارنة سوق آلية" if decision.confidence == 3 else
                 "انخفاض قوي أمام تاريخ البوت")
        msg = (f"🧠 <b>فرصة سعرية: {label}</b>\n"
               f"📦 {html.escape(offer.title[:240])}\n"
               f"🏪 {html.escape(offer.store.upper())}\n"
               f"💰 <b>{actual_price:,.2f} جنيه</b>\n"
               f"📊 المرجع: {decision.baseline:,.2f} جنيه\n"
               f"📉 فرق مرصود: {decision.pct:.1f}%\n"
               f"📌 {html.escape(decision.evidence)}\n"
               f"🧪 قوة الإشارة: {decision.score}/100 (ليست ضمانًا)\n"
               f"🕒 {dt.datetime.now(CAIRO):%Y-%m-%d %H:%M} القاهرة\n"
               f'<a href="{html.escape(offer.url, quote=True)}">🛒 افتح المنتج</a>\n'
               "⚠️ راجع البائع والكمية والشحن والتوفر والسعر النهائي قبل الدفع.")
        api = f"https://api.telegram.org/bot{token}/sendMessage"
        for attempt in range(2):
            try:
                resp = self.session().post(api, json={"chat_id": chat_id, "text": msg,
                    "parse_mode": "HTML", "disable_web_page_preview": True},timeout=(5,10))
                if resp.status_code == 429 and attempt == 0:
                    time.sleep(min(10,max(1,int(resp.json().get("parameters",{}).get("retry_after",3)))))
                    continue
                resp.raise_for_status()
                if not resp.json().get("ok"):
                    raise ValueError("Telegram returned ok=false")
                self.inc("sent")
                LOG.info("SMART ALERT SENT %s %.2f score=%s",offer.key,actual_price,decision.score)
                return True
            except (requests.RequestException, ValueError) as exc:
                LOG.warning("Telegram send failed: %s",exc.__class__.__name__)
                break
        self.inc("send_errors")
        return False

    def decide(self, offer: Offer, current: float,
               history: list[tuple[float,int]]) -> Decision | None:
        now = int(time.time())
        candidates = []
        if offer.total_verified and offer.gtin:
            market = self.state.market_decision(offer.gtin,offer.store,offer.key,current,now)
            if market:
                candidates.append(market)
        observed = automatic_history_decision(history,current,now)
        if observed:
            candidates.append(observed)
        return max(candidates,key=lambda d:(d.confidence,d.score)) if candidates else None

    def handle(self, offer: Offer, trusted_feed: bool = False):
        if not (offer.price and offer.price > 0):
            return
        with self.seen_lock:
            if offer.key in self.seen:
                return
            self.seen.add(offer.key)
        self.inc("items")
        history = self.state.historical_prices_and_record(offer.key,offer.price,int(time.time()))
        decision = self.decide(offer,offer.price,history)
        if not decision:
            return
        self.inc("candidates")
        confirmed = offer.price
        if PRODUCT_CONFIRMATION and not trusted_feed:
            page = self.fetch(offer.url)
            if not page:
                LOG.info("SKIP: cannot recheck %s",offer.key)
                return
            confirmed, available = detail_price(page,offer.store)
            if not available or not confirmed:
                LOG.info("SKIP: unavailable/unparsable %s",offer.key)
                return
            if confirmed > offer.price * 1.03:
                LOG.info("SKIP: listing vs detail price diverged %s",offer.key)
                return
        # Never use old listed prices for confirming a detected deal.
        decision = self.decide(offer,confirmed,history)
        if not decision:
            return
        self.inc("confirmed")
        if not self.state.should_alert(offer.key,confirmed,decision.confidence):
            return
        if self.telegram(offer,confirmed,decision):
            self.state.alerted(offer.key,confirmed,decision.confidence)

    def scan(self, kind: str, url: str):
        self.inc("listing_attempts")
        page = self.fetch(url)
        if not page:
            self.inc("listing_bad")
            return
        offers = parse_amazon_listing(page) if kind == "amazon" else parse_noon_listing(page)
        LOG.info("%s: parsed %s items from %s", kind, len(offers), url[:110])
        if not offers:
            self.inc("listing_bad")
            LOG.warning("ZERO PRODUCTS: parsing changed, no products, or blocked content: %s", url[:110])
        else:
            self.inc("listing_healthy")
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
            self.handle(Offer(store, product_id, name, price, None, url), trusted_feed=True)
        except (KeyError, TypeError, AttributeError):
            LOG.warning("Skipping invalid watchlist item")

    def approved_feed(self, path: Path):
        """Optional normalized, LICENSED multi-store feed, automatically matched by GTIN.

        Each item: store, id, title, url, gtin, total_price (including shipping),
        updated_at (ISO with tz), in_stock:true, condition:'new', currency:'EGP'.
        Only same validated GTIN is treated as an exact product/variant match.
        No user-set target or manually entered product prices.
        """
        url = os.getenv("APPROVED_FEED_URL", "").strip()
        if not path.is_file() and not url:
            return 0
        try:
            if url:
                if urlparse(url).scheme != "https" or not urlparse(url).hostname:
                    raise ValueError("APPROVED_FEED_URL must be HTTPS")
                auth = os.getenv("APPROVED_FEED_BEARER", "")
                headers = {"Authorization": f"Bearer {auth}"} if auth else {}
                # Requests is explicitly configured by repo owner: HTTPS only; no redirect to different host.
                response = requests.get(url, headers=headers, timeout=(5, 15), allow_redirects=False)
                response.raise_for_status()
                if len(response.content) > 2_000_000:
                    raise ValueError("Price feed exceeds 2 MB")
                raw = response.json()
            else:
                raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("approved_feed must be a JSON array")
        except (OSError, ValueError, requests.RequestException) as exc:
            LOG.warning("Approved feed read failed: %s", type(exc).__name__)
            self.inc("pages_error")
            return 0
        prepared=[]
        allow = {"amazon": ("amazon.eg","www.amazon.eg"),
                 "noon": ("noon.com","www.noon.com"),
                 "jumia": ("jumia.com.eg","www.jumia.com.eg")}
        now = int(time.time())
        for item in raw[:5000]:
            try:
                store=str(item["store"]).lower().strip()
                pid=str(item["id"]).strip()
                url=str(item["url"]).strip()
                gtin=validate_gtin(item.get("gtin"))
                updated=dt.datetime.fromisoformat(str(item["updated_at"]).replace('Z','+00:00'))
                if updated.tzinfo is None:
                    continue
                age=now-int(updated.timestamp())
                if not (0 <= age <= 600 and pid and store in allow and
                        urlparse(url).scheme=='https' and urlparse(url).hostname in allow[store] and
                        item.get('currency')=='EGP' and item.get('condition')=='new' and
                        item.get('in_stock') is True):
                    continue
                total=number(str(item['total_price']))
                if not total:
                    continue
                # Count marketplace platforms, not arbitrary seller names supplied in a feed.
                # Several listings from a single shop must NOT count as independent proof.
                merchant=store
                offer=Offer(store,pid,str(item['title']),total,None,url,
                            gtin=gtin,total_verified=True)
                prepared.append((offer,merchant))
            except (KeyError,TypeError,ValueError,AttributeError):
                continue
        # Ingest all quotes FIRST, so first sighting is comparable to other stores.
        for offer,merchant in prepared:
            if offer.gtin:
                self.state.record_market_quote(offer.gtin,merchant,offer.key,
                                               offer.price,now)
        for offer,_merchant in prepared:
            self.handle(offer,trusted_feed=True)
        LOG.info("Approved feed fresh verified items: %s",len(prepared))
        self.inc("feed_items", len(prepared))
        return len(prepared)

    def run_once(self):
        start = time.monotonic()
        # Feed first, so current independent comparisons win over automatic rechecks.
        if SOURCE_MODE not in ("auto", "html", "feed"):
            raise ValueError("SOURCE_MODE must be auto, html, or feed")
        feed_count = self.approved_feed(Path(os.getenv("APPROVED_FEED_FILE", "approved_feed.json"))) \
            if SOURCE_MODE in ("auto", "feed") else 0
        use_html = SOURCE_MODE == "html" or (SOURCE_MODE == "auto" and feed_count == 0)
        if SOURCE_MODE == "feed" and feed_count == 0:
            raise RuntimeError("No fresh, valid items in approved feed: check source and updated_at")
        # Only use legacy HTML if no usable approved feed is configured.
        # Challenges and throttling are NEVER bypassed.
        priority = self.state.automatic_priority(AUTO_WATCH_PER_RUN) if use_html else []
        if priority:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = [pool.submit(self.collect_watchlist_item, item) for item in priority]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
        tasks = []
        if use_html and AMAZON_PER_RUN:
            start_index = self.state.next_category_offset(
                "amazon_offset", AMAZON_PER_RUN, len(AMAZON_CATEGORIES))
            for index in range(AMAZON_PER_RUN):
                category = AMAZON_CATEGORIES[(start_index + index) % len(AMAZON_CATEGORIES)]
                # No discount filter. Cheap items without any discount badge are now
                # eligible when we have prior observations or a checked reference.
                for page in range(1, AMAZON_PAGES_PER_CATEGORY + 1):
                    tasks.append(("amazon", f"https://www.amazon.eg/s?i={category}&s=featured-rank&page={page}"))
        if use_html and ENABLE_NOON_HTML and NOON_PER_RUN:
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
        if self.stats["send_errors"]:
            raise RuntimeError(f"{self.stats['send_errors']} Telegram alerts failed")
        if use_html:
            attempts = self.stats["listing_attempts"]
            good = self.stats["listing_healthy"]
            if attempts == 0:
                raise RuntimeError("No HTML listing scans configured")
            if good == 0 or good / attempts < 0.5:
                raise RuntimeError(
                    f"SOURCE UNHEALTHY: only {good}/{attempts} listing pages parsed; "
                    "Amazon CAPTCHA/503 or changed page markup. Need a permitted live price feed."
                )
        if self.stats["items"] == 0:
            raise RuntimeError("No products parsed; selectors/source access need investigation")
        # A genuine healthy run with no deal still emits a periodic proof-of-life.
        if os.getenv("HEARTBEAT", "1") == "1" and self.state.reserve_health_warning(
                "heartbeat", int(time.time()), 12 * 3600):
            summary = self.stats
            if not self.telegram_status(
                    "🟢 Egypt Deals Hunter v5: الفحص شغال. "
                    f"منتجات: {summary['items']} | مرشحة: {summary['candidates']} | "
                    f"تنبيهات: {summary['sent']} | صفحات سليمة: "
                    f"{summary['listing_healthy']}/{summary['listing_attempts']} | "
                    f"تغذية معتمدة: {summary['feed_items']}"):
                raise RuntimeError("Periodic Telegram status delivery failed")


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
        smoke_state = State(":memory:")
        try:
            delivered = Hunter(smoke_state).telegram_status("✅ Egypt Deals Hunter v5: اتصال تيليجرام ناجح")
        finally:
            smoke_state.close()
        if not delivered:
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
