"""
Property Scout Greece - v4.2
- Emoji formatting matching original style
- Deduplication with price tracking (seen_listings.json)
- All listings with score >= 4 sent (no cap)
- Two daily runs: 07:00 + 19:00 Israel time
- Spitogatos floor mapping: Basement/LG/G/UG/1st...
- Floor filter sent as URL params to Spitogatos
"""

import os
import json
import time
import hashlib
import logging
import re
import sys
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

PROFILES_FILE = "scraper/profiles.json"
RESULTS_FILE  = "scraper/results.json"
SEEN_FILE     = "scraper/seen_listings.json"

MIN_SCORE = 4

AUCTION_KEYWORDS = [
    "pleistairiasmos", "pleistiriasmou", "auction",
    "\u03c0\u03bb\u03b5\u03b9\u03c3\u03c4\u03b7\u03c1\u03b9\u03b1\u03c3\u03bc",
    "\u03ba\u03b1\u03c4\u03ac\u03c3\u03c7\u03b5\u03c3\u03b7",
    "\u03b5\u03ba\u03c0\u03bb\u03b5\u03b9\u03c3\u03c4\u03b7\u03c1\u03af\u03b1\u03c3\u03b7",
]


# ── Data class ───────────────────────────────────────────────────────────────
@dataclass
class Listing:
    id: str
    source: str
    title: str
    url: str
    price: Optional[int]
    sqm: Optional[float]
    floor: Optional[int]
    area: str
    price_per_sqm: Optional[float]
    is_auction: bool
    description: str
    scraped_at: str
    profile_id: str = ""
    deal_score: int = 0
    discount_vs_market: float = 0.0
    estimated_renovation: int = 0
    estimated_flip_roi: float = 0.0
    estimated_rent_yield: float = 0.0
    total_cost: int = 0
    arv: int = 0

    def compute_analysis(self, benchmarks, renovation_cost_per_sqm=800):
        if not self.price or not self.sqm:
            return
        bench = benchmarks.get(self.area, benchmarks.get("default", {"price_sqm": 2200, "rent_sqm": 10}))
        market_sqm = bench["price_sqm"]
        self.price_per_sqm = self.price / self.sqm
        self.discount_vs_market = (self.price_per_sqm - market_sqm) / market_sqm
        renov_cost = self.sqm * renovation_cost_per_sqm
        tx_cost = self.price * 0.09
        total_cost = self.price + tx_cost + renov_cost
        arv = self.sqm * market_sqm
        self.estimated_renovation = int(renov_cost)
        self.total_cost = int(total_cost)
        self.arv = int(arv)
        self.estimated_flip_roi = (arv - total_cost) / total_cost if total_cost > 0 else 0
        annual_rent = self.sqm * bench["rent_sqm"] * 12
        self.estimated_rent_yield = annual_rent / total_cost if total_cost > 0 else 0
        score = 0
        if self.discount_vs_market < -0.10:
            score += 3
        elif self.discount_vs_market < 0:
            score += 1
        if self.estimated_flip_roi > 0.15:
            score += 3
        elif self.estimated_flip_roi > 0.08:
            score += 1
        if self.estimated_rent_yield > 0.06:
            score += 2
        elif self.estimated_rent_yield > 0.04:
            score += 1
        self.deal_score = min(score, 7)


# ── Formatting ────────────────────────────────────────────────────────────────
def format_message(l: Listing, profile_name: str, price_changed: bool = False) -> str:
    if l.is_auction:
        score_emoji = "\u2696\ufe0f"
    elif l.deal_score >= 6:
        score_emoji = "\U0001f525"
    elif l.deal_score >= 4:
        score_emoji = "\u2728"
    else:
        score_emoji = "\U0001f4cc"

    score_bar = "#" * l.deal_score + "-" * (7 - l.deal_score)
    disc_pct = l.discount_vs_market * 100
    roi_pct  = l.estimated_flip_roi * 100
    rent_pct = l.estimated_rent_yield * 100
    source_display = {"spitogatos": "Spitogatos", "xe": "XE.gr", "rightmove": "Rightmove"}.get(l.source, l.source)
    area_display = l.area.replace("-", " ").title()
    floor_str = f" \u00b7 floor {l.floor}" if l.floor is not None else ""
    header = "Property Scout \U0001f504 PRICE CHANGE:" if price_changed else "Property Scout:"

    lines = [
        header,
        f"{score_emoji} Score {l.deal_score}/7 [{score_bar}]",
        f"\U0001f4cd {area_display} \u00b7 {source_display}",
        l.title[:60],
        "",
        f"\U0001f4b0 EUR{l.price:,} \u00b7 {l.sqm:.0f}sqm \u00b7 EUR{l.price_per_sqm:.0f}/sqm{floor_str}",
        f"\U0001f4ca vs market: {disc_pct:+.1f}%",
        "",
        f"\U0001f527 Renovation: EUR{l.estimated_renovation:,}",
        f"\U0001f4c8 Flip ROI: {roi_pct:.1f}%  |  Rent: {rent_pct:.1f}%",
        "",
        l.url,
    ]
    return "\n".join(lines)


def format_summary(run_summary: list) -> str:
    now = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
    lines = ["\U0001f4cb Scout Summary", f"\U0001f550 {now} UTC", ""]
    for r in run_summary:
        if "error" in r:
            lines.append(f"\u274c {r['profile_name']}: {r['error'][:50]}")
        else:
            parts = f"{r['fetched']} fetched, {r['new_alerts']} alerts"
            if r.get("price_changes"):
                parts += f", {r['price_changes']} price changes"
            if r.get("auctions"):
                parts += f", {r['auctions']} auctions"
            lines.append(f"\u2705 {r['profile_name']}: {parts}")
    total_new   = sum(r.get("new_alerts", 0) for r in run_summary)
    total_price = sum(r.get("price_changes", 0) for r in run_summary)
    total_auct  = sum(r.get("auctions", 0) for r in run_summary)
    lines.append("")
    summary = f"Total: {total_new} alerts"
    if total_price:
        summary += f" \u00b7 {total_price} price changes"
    summary += f" \u00b7 {total_auct} auctions"
    lines.append(summary)
    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_number(text):
    text = re.sub(r"[\u20ac$\u00a3\s]", "", str(text))
    match = re.search(r"[\d.,]+", text)
    if not match:
        return None
    raw = match.group().replace(".", "").replace(",", "")
    try:
        v = int(raw)
        return v if v > 100 else None
    except ValueError:
        return None


def make_listing(source, href, title, price, sqm, floor, area, desc, profile_id, filters, benchmarks, renov_cost):
    listing_id = hashlib.md5((href + profile_id).encode()).hexdigest()[:12]
    full_text = (title + " " + desc).lower()
    is_auction = any(kw in full_text for kw in AUCTION_KEYWORDS)
    if price and sqm and (price / sqm) > filters.get("max_price_per_sqm", 999999):
        return None
    l = Listing(
        id=listing_id, source=source, title=title[:80], url=href,
        price=price, sqm=sqm, floor=floor, area=area,
        price_per_sqm=(price / sqm) if price and sqm else None,
        is_auction=is_auction, description=desc[:300],
        scraped_at=datetime.utcnow().isoformat(), profile_id=profile_id,
    )
    l.compute_analysis(benchmarks, renov_cost)
    return l


# Spitogatos floor name → numeric value mapping
# Basement=-2, LG=-1, G=0, UG=1, 1st=1, 2nd=2, ...
SPITOGATOS_FLOOR_MAP = {
    "basement": -2,
    "lower ground": -1,
    "lg": -1,
    "ground floor": 0,
    "ground": 0,
    "g": 0,
    "upper ground": 1,
    "ug": 1,
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
    "6th": 6, "7th": 7, "8th": 8, "9th": 9, "10th": 10,
}

# Spitogatos floor URL codes (from their filter system)
# basement=1, lower_ground=2, ground=3, upper_ground=4, 1st=5, 2nd=6 ...
SPITOGATOS_FLOOR_CODES = {
    -2: "1",   # Basement
    -1: "2",   # Lower Ground
     0: "3",   # Ground
     1: "4",   # Upper Ground / 1st
     2: "5",
     3: "6",
     4: "7",
     5: "8",
     6: "9",
     7: "10",
}

def parse_spitogatos_floor(text: str) -> Optional[int]:
    text_lower = text.lower()
    for label, val in SPITOGATOS_FLOOR_MAP.items():
        if label in text_lower:
            return val
    # fallback: look for "Xth floor" or "floor X"
    m = re.search(r"(\d+)(?:st|nd|rd|th)?\s*floor", text_lower)
    if m:
        return int(m.group(1))
    return None


# ── Scrapers ───────────────────────────────────────────────────────────────────
def scrape_spitogatos(page, area, filters, profile_id, benchmarks, renov_cost):
    listings = []

    # Build URL with price + size filters
    # Floor filter: send all floor codes from min_floor to max_floor
    min_floor = filters.get("min_floor", -2)
    max_floor = filters.get("max_floor", 10)
    floor_params = ""
    for numeric, code in SPITOGATOS_FLOOR_CODES.items():
        if min_floor <= numeric <= max_floor:
            floor_params += f"&floor[]={code}"

    url = (
        "https://www.spitogatos.gr/en/for_sale-homes/" + area
        + "?price[]=" + str(filters["min_price"]) + "%2C" + str(filters["max_price"])
        + "&areas[]=" + str(filters["min_sqm"]) + "%2C" + str(filters["max_sqm"])
        + floor_params
    )
    log.info("Spitogatos: %s", url)
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('a[href*="/property/"], .no-results', timeout=10000)
        except PlaywrightTimeout:
            pass
        time.sleep(2)
        results = page.evaluate("""
        () => {
          const cards = document.querySelectorAll('a[href*="/property/"]');
          const seen = new Set(); const out = [];
          cards.forEach(card => {
            const href = card.href;
            if (seen.has(href)) return; seen.add(href);
            const container = card.closest('article, li, div');
            const text = container ? container.innerText : card.innerText;
            out.push({ href, text });
          });
          return out;
        }
        """)
        for item in results[:25]:
            try:
                href = item["href"]; text = item["text"]
                price_m = re.search(r"\u20ac\s*([\d.,]+)", text)
                price = parse_number(price_m.group(1)) if price_m else None
                sqm_m = re.search(r"(\d+)\s*m[\u00b22]", text)
                sqm = float(sqm_m.group(1)) if sqm_m else None
                floor_m = re.search(r"(\d+)(?:st|nd|rd|th)\s*floor", text, re.IGNORECASE)
                floor = int(floor_m.group(1)) if floor_m else parse_spitogatos_floor(text)
                title = text.split("\n")[0][:80] if text else "Apartment"
                l = make_listing("spitogatos", href, title, price, sqm, floor, area, text, profile_id, filters, benchmarks, renov_cost)
                if l: listings.append(l)
            except Exception as e:
                log.debug("Spitogatos card: %s", e)
    except Exception as e:
        log.warning("Spitogatos %s: %s", area, e)
    log.info("Spitogatos %s: %d", area, len(listings))
    return listings


def scrape_xe(page, area, filters, profile_id, benchmarks, renov_cost):
    listings = []
    # XE.gr new URL format (2025): /en/property/r/apartment-for-sale/<place-id>_<slug>
    # Place IDs taken from XE.gr search results
    xe_place_map = {
        "nea-smyrni":    "ChIJM3OFEly9oRQRVBiTBzlCMDc_nea-smyrni",
        "kallithea":     "ChIJw7AEqla9oRQRgZbMHahZSFs_kallithea",
        "palaio-faliro": "ChIJiaiuGqC9oRQRdmMfD8cMLSY_palaio-faliro",
        "glyfada":       "ChIJpxbHN_e9oRQRCkX_yFWHKwQ_glyfada",
        "ilioupoli":     "ChIJpT7gJbK9oRQRvZfAEOOlJww_ilioupoli",
        "byronas":       "ChIJ8RzNYL29oRQRtf_EEMaYNkI_vyronas",
        "athens-center": "ChIJ8UNwBh-9oRQR3Y1mdkU1Nic_athens",
        "neos-kosmos":   "ChIJcybCQLO9oRQR3F_YGRa3SJk_neos-kosmos",
    }
    place_id = xe_place_map.get(area)
    if not place_id:
        log.warning("XE: no place ID for area %s", area)
        return listings

    # New URL format with price/size filters as query params
    url = (
        f"https://www.xe.gr/en/property/r/apartment-for-sale/{place_id}"
        f"?minimum_price={filters['min_price']}"
        f"&maximum_price={filters['max_price']}"
        f"&minimum_level_size={filters['min_sqm']}"
        f"&maximum_level_size={filters['max_sqm']}"
    )
    log.info("XE: %s", url)
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            # XE uses React — wait for listing cards to appear
            page.wait_for_selector(
                'a[href*="/en/property/d/"], [data-testid="property-ad-item"], .property-ad',
                timeout=12000
            )
        except PlaywrightTimeout:
            log.warning("XE %s: listings selector timeout", area)

        time.sleep(3)

        results = page.evaluate("""
        () => {
          // XE new format: listing links contain /en/property/d/
          const cards = document.querySelectorAll('a[href*="/en/property/d/"]');
          const seen = new Set(); const out = [];
          cards.forEach(card => {
            const href = card.href;
            if (seen.has(href)) return; seen.add(href);
            // Walk up to find the listing card container
            let container = card.closest('[data-testid="property-ad-item"]')
                         || card.closest('article')
                         || card.closest('li')
                         || card.parentElement;
            const text = container ? container.innerText : card.innerText;
            if (text && text.length > 20) out.push({ href, text });
          });
          return out;
        }
        """)

        log.info("XE %s: raw results: %d", area, len(results))
        for item in results[:25]:
            try:
                href = item["href"]; text = item["text"]
                # Price: look for € followed by number
                price_m = re.search(r"\u20ac\s*([\d.,]+)", text)
                price = parse_number(price_m.group(1)) if price_m else None
                # Size: number followed by m² or τ.μ (Greek sq meter abbreviation)
                sqm_m = re.search(r"(\d+)\s*(?:m[\u00b22]|\u03c4\.\u03bc\.?)", text)
                sqm = float(sqm_m.group(1)) if sqm_m else None
                # Floor
                floor_m = re.search(r"(\d+)(?:st|nd|rd|th)?\s*(?:floor|\u03cc\u03c1\u03bf\u03c6\u03bf\u03c2)", text, re.IGNORECASE)
                floor = int(floor_m.group(1)) if floor_m else None
                title = text.split("\n")[0][:80] if text else "Apartment"
                l = make_listing("xe", href, title, price, sqm, floor, area, text,
                                 profile_id, filters, benchmarks, renov_cost)
                if l: listings.append(l)
            except Exception as e:
                log.debug("XE card: %s", e)
    except Exception as e:
        log.warning("XE %s: %s", area, e)
    log.info("XE %s: %d listings", area, len(listings))
    return listings


def scrape_rightmove(page, area, filters, profile_id, benchmarks, renov_cost):
    listings = []
    rm_map = {
        "nea-smyrni": "REGION%5E87528", "kallithea": "REGION%5E87509",
        "glyfada": "REGION%5E87537", "palaio-faliro": "REGION%5E87535",
        "ilioupoli": "REGION%5E87523",
    }
    loc = rm_map.get(area)
    if not loc:
        return listings
    url = (
        f"https://www.rightmove.co.uk/overseas-property/in-Greece.html"
        f"?locationIdentifier={loc}"
        f"&minPrice={filters['min_price']}"
        f"&maxPrice={filters['max_price']}"
        f"&propertyTypes=flat&mustHave=&dontShow=&furnishTypes=&keywords="
    )
    log.info("Rightmove: %s", url)
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(
                'a[href*="/properties/"], [data-test="property-details"], .propertyCard',
                timeout=12000
            )
        except PlaywrightTimeout:
            log.warning("Rightmove %s: selector timeout", area)
        time.sleep(3)
        results = page.evaluate("""
        () => {
          const cards = document.querySelectorAll('a[href*="/properties/"]');
          const seen = new Set(); const out = [];
          cards.forEach(card => {
            const href = card.href;
            if (!href.includes('/properties/')) return;
            if (seen.has(href)) return; seen.add(href);
            const container = card.closest('[data-test="property-details"]')
                           || card.closest('.propertyCard')
                           || card.closest('article')
                           || card.closest('li')
                           || card.parentElement;
            const text = container ? container.innerText : card.innerText;
            if (text && text.length > 20) out.push({ href, text });
          });
          return out;
        }
        """)
        log.info("Rightmove %s: raw results: %d", area, len(results))
        for item in results[:15]:
            try:
                href = item["href"]; text = item["text"]
                price_m = re.search(r"[\u20ac\u00a3]\s*([\d.,]+)", text)
                price = parse_number(price_m.group(1)) if price_m else None
                sqm_m = re.search(r"(\d+)\s*(?:sq\.?\s*m|m[\u00b22])", text, re.IGNORECASE)
                sqm = float(sqm_m.group(1)) if sqm_m else None
                title = text.split("\n")[0][:80] if text else "Property"
                l = make_listing("rightmove", href, title, price, sqm, None, area, text,
                                 profile_id, filters, benchmarks, renov_cost)
                if l: listings.append(l)
            except Exception as e:
                log.debug("Rightmove card: %s", e)
    except Exception as e:
        log.warning("Rightmove %s: %s", area, e)
    log.info("Rightmove %s: %d listings", area, len(listings))
    return listings


# ── Seen listings ──────────────────────────────────────────────────────────────
def load_seen() -> dict:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return {lid: {"price": 0, "url": "", "last_seen": ""} for lid in data}
        return data
    return {}

def save_seen(seen: dict):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)

def is_new_or_price_changed(listing: Listing, seen: dict) -> bool:
    entry = seen.get(listing.id)
    if entry is None:
        return True
    if listing.price and listing.price != entry.get("price", 0):
        return True
    return False

def mark_seen(listing: Listing, seen: dict):
    seen[listing.id] = {"price": listing.price, "url": listing.url, "last_seen": datetime.utcnow().isoformat()}


# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_BOT_TOKEN)
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": False}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.error("Telegram: %s", e)


# ── Building scraper (whole buildings) ────────────────────────────────────────
def scrape_spitogatos_buildings(page, area, filters, profile_id, benchmarks, renov_cost):
    """Search for whole buildings (polykatoikia) on Spitogatos."""
    listings = []
    # Spitogatos building category URL
    url = (
        "https://www.spitogatos.gr/en/for_sale-buildings/" + area
        + "?price[]=" + str(filters["min_price"]) + "%2C" + str(filters["max_price"])
        + "&areas[]=" + str(filters["min_sqm"]) + "%2C" + str(filters["max_sqm"])
    )
    log.info("Spitogatos Buildings: %s", url)
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('a[href*="/property/"], .no-results', timeout=10000)
        except PlaywrightTimeout:
            pass
        time.sleep(2)
        results = page.evaluate("""
        () => {
          const cards = document.querySelectorAll('a[href*="/property/"]');
          const seen = new Set(); const out = [];
          cards.forEach(card => {
            const href = card.href;
            if (seen.has(href)) return; seen.add(href);
            const container = card.closest('article, li, div');
            const text = container ? container.innerText : card.innerText;
            if (text && text.length > 20) out.push({ href, text });
          });
          return out;
        }
        """)
        for item in results[:20]:
            try:
                href = item["href"]; text = item["text"]
                price_m = re.search(r"\u20ac\s*([\d.,]+)", text)
                price = parse_number(price_m.group(1)) if price_m else None
                sqm_m = re.search(r"(\d+)\s*m[\u00b22]", text)
                sqm = float(sqm_m.group(1)) if sqm_m else None
                title = text.split("\n")[0][:80] if text else "Building"
                l = make_listing("spitogatos", href, title, price, sqm, None, area, text,
                                 profile_id, filters, benchmarks, renov_cost)
                if l: listings.append(l)
            except Exception as e:
                log.debug("Building card: %s", e)
    except Exception as e:
        log.warning("Buildings %s: %s", area, e)
    log.info("Buildings %s: %d", area, len(listings))
    return listings


# ── Profile runner ─────────────────────────────────────────────────────────────
def run_profile(profile, benchmarks, seen, results, page):
    name = profile["name"]; pid = profile["id"]
    filters = profile["filters"]
    renov = profile.get("renovation_cost_per_sqm", 800)
    is_building = profile.get("property_type") == "building"
    log.info("=" * 50)
    log.info("Profile: %s (building=%s)", name, is_building)

    all_listings = []
    for area in filters["areas"]:
        # For building profiles, use building-specific scrapers
        if is_building:
            all_listings.extend(scrape_spitogatos_buildings(page, area, filters, pid, benchmarks, renov))
        else:
            all_listings.extend(scrape_spitogatos(page, area, filters, pid, benchmarks, renov))
        time.sleep(1)
        all_listings.extend(scrape_xe(page, area, filters, pid, benchmarks, renov))
        time.sleep(1)
        if not is_building:
            all_listings.extend(scrape_rightmove(page, area, filters, pid, benchmarks, renov))
            time.sleep(1)

    log.info("Total fetched: %d", len(all_listings))

    # Filter by floor
    filtered = []
    for l in all_listings:
        if not l.price or not l.sqm:
            continue
        if l.floor is not None:
            if l.floor < filters.get("min_floor", -10) or l.floor > filters.get("max_floor", 100):
                continue
        filtered.append(l)

    # ── SORT BY SCORE (highest first) before sending ──
    filtered.sort(key=lambda x: x.deal_score, reverse=True)
    log.info("After filtering: %d | Sorted by score", len(filtered))

    new_count = price_change_count = auction_count = 0

    for l in filtered:
        already_seen = l.id in seen
        send_it = is_new_or_price_changed(l, seen)
        price_changed = already_seen and send_it
        mark_seen(l, seen)
        results["listings"].append(asdict(l))
        if not send_it:
            continue
        if l.is_auction:
            send_telegram(format_message(l, name, price_changed))
            auction_count += 1
        elif l.deal_score >= MIN_SCORE:
            send_telegram(format_message(l, name, price_changed))
            if price_changed: price_change_count += 1
            else: new_count += 1
        time.sleep(0.5)

    return {"profile_id": pid, "profile_name": name, "fetched": len(all_listings),
            "new_alerts": new_count, "price_changes": price_change_count, "auctions": auction_count,
            "timestamp": datetime.utcnow().isoformat()}


# ── File helpers ───────────────────────────────────────────────────────────────
def load_profiles():
    if not os.path.exists(PROFILES_FILE):
        log.error("profiles.json not found"); sys.exit(1)
    with open(PROFILES_FILE) as f: return json.load(f)

def save_profiles(data):
    with open(PROFILES_FILE, "w") as f: json.dump(data, f, indent=2, ensure_ascii=False)

def load_results():
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f: return json.load(f)
    return {"listings": [], "runs": []}

def save_results(data):
    data["listings"] = data["listings"][-500:]
    data["runs"] = data["runs"][-50:]
    with open(RESULTS_FILE, "w") as f: json.dump(data, f, indent=2, ensure_ascii=False)


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log.info("=== Property Scout v4.1 ===")
    profile_filter = os.environ.get("PROFILE_ID", "").strip()
    data = load_profiles()
    benchmarks = data.get("area_benchmarks", {})
    profiles = data.get("profiles", [])
    if profile_filter:
        profiles = [p for p in profiles if p["id"] == profile_filter]
    else:
        profiles = [p for p in profiles if p.get("active", True)]
    if not profiles:
        send_telegram("\U0001f4cb Property Scout: No active profiles.")
        return

    seen = load_seen()
    results = load_results()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="el-GR", viewport={"width": 1366, "height": 768},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        run_summary = []
        for profile in profiles:
            try:
                r = run_profile(profile, benchmarks, seen, results, page)
                run_summary.append(r)
                for p_obj in data["profiles"]:
                    if p_obj["id"] == profile["id"]:
                        p_obj["last_run"] = datetime.utcnow().isoformat()
            except Exception as e:
                log.error("Profile %s: %s", profile["id"], e)
                run_summary.append({"profile_id": profile["id"], "profile_name": profile["name"],
                                    "error": str(e), "timestamp": datetime.utcnow().isoformat()})
        browser.close()

    save_seen(seen)
    results["runs"].extend(run_summary)
    save_results(results)
    save_profiles(data)
    send_telegram(format_summary(run_summary))
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
