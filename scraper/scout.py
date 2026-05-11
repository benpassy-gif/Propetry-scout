"""
Property Scout — Greece Real Estate
Scans Spitogatos, XE.gr, Rightmove Greece
Sends Telegram alerts for matching properties
"""

import os
import json
import time
import hashlib
import logging
import re
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── CONFIG (set via GitHub Secrets or .env) ─────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
SEEN_FILE          = "seen_listings.json"

# ─── SEARCH FILTERS (edit these) ─────────────────────────────────────────────
FILTERS = {
    "areas": [
        "nea-smyrni",   # Spitogatos area slugs
        "kallithea",
        "palaio-faliro",
        "glyfada",
        "ilioupoli",
    ],
    "min_sqm": 40,
    "max_sqm": 120,
    "min_price": 40_000,
    "max_price": 150_000,
    "min_floor": -1,   # -1 = ημιυπόγειο, 0 = ισόγειο
    "max_floor": 5,
    "max_price_per_sqm": 3_500,  # alert only if below this
    "property_type": "apartment",  # apartment | house | any
}

# Area benchmark prices per sqm (for deal scoring)
AREA_BENCHMARKS = {
    "nea-smyrni":     {"price_sqm": 2800, "rent_sqm": 12},
    "kallithea":      {"price_sqm": 2200, "rent_sqm": 10},
    "palaio-faliro":  {"price_sqm": 3200, "rent_sqm": 13},
    "glyfada":        {"price_sqm": 3500, "rent_sqm": 14},
    "ilioupoli":      {"price_sqm": 1800, "rent_sqm": 8.5},
    "default":        {"price_sqm": 2200, "rent_sqm": 10},
}

# Auction keywords (Greek)
AUCTION_KEYWORDS = [
    "πλειστηριασμ",  # covers πλειστηριασμός, πλειστηριασμού etc.
    "κατάσχεση",
    "δικαστήριο",
    "εκπλειστηρίαση",
    "κατασχεθέν",
    "αναγκαστική πώληση",
    "auction",
]

# ─── DATA MODEL ───────────────────────────────────────────────────────────────
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
    images: list
    scraped_at: str

    # Computed fields
    deal_score: int = 0
    discount_vs_market: float = 0.0
    estimated_renovation: int = 0
    estimated_flip_roi: float = 0.0
    estimated_rent_yield: float = 0.0

    def compute_analysis(self):
        if not self.price or not self.sqm:
            return
        bench = AREA_BENCHMARKS.get(self.area, AREA_BENCHMARKS["default"])

        self.price_per_sqm = self.price / self.sqm
        market_sqm = bench["price_sqm"]
        self.discount_vs_market = (self.price_per_sqm - market_sqm) / market_sqm

        # Renovation estimate (full renovation default)
        renov_per_sqm = 800
        renov_cost = self.sqm * renov_per_sqm

        # Transaction costs ~9%
        tx_cost = self.price * 0.09

        total_cost = self.price + tx_cost + renov_cost
        arv = self.sqm * market_sqm
        self.estimated_renovation = int(renov_cost)
        self.estimated_flip_roi = (arv - total_cost) / total_cost if total_cost > 0 else 0

        # Rent yield
        annual_rent = self.sqm * bench["rent_sqm"] * 12
        self.estimated_rent_yield = annual_rent / total_cost if total_cost > 0 else 0

        # Scoring
        score = 0
        if self.discount_vs_market < -0.10: score += 3
        elif self.discount_vs_market < 0:   score += 1
        if self.estimated_flip_roi > 0.15:  score += 3
        elif self.estimated_flip_roi > 0.08: score += 1
        if self.estimated_rent_yield > 0.06: score += 2
        elif self.estimated_rent_yield > 0.04: score += 1
        self.deal_score = min(score, 7)


# ─── SCRAPERS ─────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
}

def get_soup(url: str) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return None


def scrape_spitogatos(area: str, filters: dict) -> list[Listing]:
    listings = []
    base = "https://www.spitogatos.gr"
    min_p, max_p = filters["min_price"], filters["max_price"]
    min_s, max_s = filters["min_sqm"],  filters["max_sqm"]

    url = (
        f"{base}/sale-flats/{area}"
        f"?minPrice={min_p}&maxPrice={max_p}"
        f"&minArea={min_s}&maxArea={max_s}"
        f"&sort=date_desc"
    )
    log.info(f"Spitogatos: {url}")
    soup = get_soup(url)
    if not soup:
        return listings

    cards = soup.select("article.listing-item, div.property-listing-item, li.result-item")
    if not cards:
        # Try alternate selectors
        cards = soup.select("[data-id], .property-card")

    for card in cards[:20]:
        try:
            # Extract URL
            link = card.select_one("a[href]")
            if not link:
                continue
            href = link.get("href", "")
            if not href.startswith("http"):
                href = base + href

            listing_id = hashlib.md5(href.encode()).hexdigest()[:12]

            # Price
            price_el = card.select_one(".price, [class*='price']")
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = parse_number(price_text)

            # Sqm
            sqm_el = card.select_one(".area, [class*='area'], [class*='sqm']")
            sqm_text = sqm_el.get_text(strip=True) if sqm_el else ""
            sqm = parse_number(sqm_text)

            # Floor
            floor_el = card.select_one("[class*='floor'], [class*='level']")
            floor_text = floor_el.get_text(strip=True) if floor_el else ""
            floor = parse_floor(floor_text)

            # Title
            title_el = card.select_one("h2, h3, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else "Διαμέρισμα"

            # Description
            desc_el = card.select_one("p, [class*='desc']")
            desc = desc_el.get_text(strip=True) if desc_el else ""

            # Auction check
            full_text = (title + " " + desc).lower()
            is_auction = any(kw in full_text for kw in AUCTION_KEYWORDS)

            if price and sqm:
                price_sqm = price / sqm
                if price_sqm > filters["max_price_per_sqm"]:
                    continue

            listing = Listing(
                id=listing_id,
                source="spitogatos",
                title=title,
                url=href,
                price=price,
                sqm=sqm,
                floor=floor,
                area=area,
                price_per_sqm=(price / sqm) if price and sqm else None,
                is_auction=is_auction,
                description=desc[:300],
                images=[],
                scraped_at=datetime.utcnow().isoformat(),
            )
            listing.compute_analysis()
            listings.append(listing)

        except Exception as e:
            log.debug(f"Error parsing card: {e}")

    log.info(f"Spitogatos {area}: {len(listings)} listings")
    return listings


def scrape_xe(area: str, filters: dict) -> list[Listing]:
    """XE.gr scraper"""
    listings = []
    base = "https://www.xe.gr"
    # XE uses different area names
    xe_area_map = {
        "nea-smyrni": "Νέα Σμύρνη",
        "kallithea": "Καλλιθέα",
        "palaio-faliro": "Παλαιό Φάληρο",
        "glyfada": "Γλυφάδα",
        "ilioupoli": "Ηλιούπολη",
    }
    area_name = xe_area_map.get(area, area)

    url = (
        f"{base}/property/for-sale/apartments"
        f"?area={requests.utils.quote(area_name)}"
        f"&price_from={filters['min_price']}&price_to={filters['max_price']}"
        f"&size_from={filters['min_sqm']}&size_to={filters['max_sqm']}"
        f"&sort=date"
    )
    log.info(f"XE.gr: {url}")
    soup = get_soup(url)
    if not soup:
        return listings

    cards = soup.select(".property-list-item, article[data-id], .listing-result")

    for card in cards[:20]:
        try:
            link = card.select_one("a[href]")
            if not link:
                continue
            href = link.get("href", "")
            if not href.startswith("http"):
                href = base + href

            listing_id = hashlib.md5(href.encode()).hexdigest()[:12]
            price = parse_number(card.get_text())
            sqm_match = re.search(r"(\d+)\s*τ\.μ", card.get_text())
            sqm = float(sqm_match.group(1)) if sqm_match else None
            title_el = card.select_one("h2, h3, .title")
            title = title_el.get_text(strip=True) if title_el else "Διαμέρισμα"
            full_text = card.get_text().lower()
            is_auction = any(kw in full_text for kw in AUCTION_KEYWORDS)

            listing = Listing(
                id=listing_id,
                source="xe",
                title=title,
                url=href,
                price=price,
                sqm=sqm,
                floor=None,
                area=area,
                price_per_sqm=(price / sqm) if price and sqm else None,
                is_auction=is_auction,
                description=card.get_text(strip=True)[:300],
                images=[],
                scraped_at=datetime.utcnow().isoformat(),
            )
            listing.compute_analysis()
            listings.append(listing)
        except Exception as e:
            log.debug(f"XE parse error: {e}")

    log.info(f"XE {area}: {len(listings)} listings")
    return listings


def scrape_rightmove(area: str, filters: dict) -> list[Listing]:
    """Rightmove Greece scraper"""
    listings = []
    # Rightmove Greece uses location IDs
    rightmove_areas = {
        "nea-smyrni": "REGION%5E87528",
        "kallithea": "REGION%5E87509",
        "glyfada": "REGION%5E87537",
        "palaio-faliro": "REGION%5E87535",
        "ilioupoli": "REGION%5E87523",
    }
    loc = rightmove_areas.get(area)
    if not loc:
        return listings

    url = (
        f"https://www.rightmove.co.uk/overseas-property/in-Greece.html"
        f"?locationIdentifier={loc}"
        f"&minPrice={filters['min_price']}&maxPrice={filters['max_price']}"
        f"&propertyTypes=flat"
    )
    log.info(f"Rightmove: {url}")
    soup = get_soup(url)
    if not soup:
        return listings

    cards = soup.select("article.l-searchResult, .propertyCard")
    for card in cards[:10]:
        try:
            link = card.select_one("a.propertyCard-link, a[href*='/properties/']")
            if not link:
                continue
            href = "https://www.rightmove.co.uk" + link.get("href", "")
            listing_id = hashlib.md5(href.encode()).hexdigest()[:12]

            price_el = card.select_one(".propertyCard-priceValue, .price")
            price = parse_number(price_el.get_text() if price_el else "")

            title_el = card.select_one("h2, .propertyCard-title")
            title = title_el.get_text(strip=True) if title_el else "Property"

            desc_el = card.select_one(".propertyCard-description")
            desc = desc_el.get_text(strip=True) if desc_el else ""
            sqm_match = re.search(r"(\d+)\s*(sq\.?\s*m|m²|τ\.μ)", desc, re.IGNORECASE)
            sqm = float(sqm_match.group(1)) if sqm_match else None

            full_text = (title + " " + desc).lower()
            is_auction = any(kw in full_text for kw in AUCTION_KEYWORDS)

            listing = Listing(
                id=listing_id,
                source="rightmove",
                title=title,
                url=href,
                price=price,
                sqm=sqm,
                floor=None,
                area=area,
                price_per_sqm=(price / sqm) if price and sqm else None,
                is_auction=is_auction,
                description=desc[:300],
                images=[],
                scraped_at=datetime.utcnow().isoformat(),
            )
            listing.compute_analysis()
            listings.append(listing)
        except Exception as e:
            log.debug(f"Rightmove parse error: {e}")

    log.info(f"Rightmove {area}: {len(listings)} listings")
    return listings


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def parse_number(text: str) -> Optional[int]:
    """Extract first number from text, handles 74.000 and 74,000"""
    text = re.sub(r"[€$£\s]", "", text)
    match = re.search(r"[\d.,]+", text)
    if not match:
        return None
    raw = match.group().replace(".", "").replace(",", "")
    try:
        return int(raw)
    except ValueError:
        return None


def parse_floor(text: str) -> Optional[int]:
    text = text.lower()
    if "ημι" in text or "ημιυπό" in text: return -1
    if "ισόγ" in text or "ground" in text: return 0
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


# ─── SEEN LISTINGS ────────────────────────────────────────────────────────────
def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
SCORE_EMOJI = {7: "🔥", 6: "🔥", 5: "👍", 4: "👍", 3: "⚠️", 2: "⚠️", 1: "❌", 0: "❌"}

def format_message(l: Listing) -> str:
    pct = lambda x: f"{x*100:+.1f}%"
    score_bar = "█" * l.deal_score + "░" * (7 - l.deal_score)

    if l.is_auction:
        header = "⚖️ *ΠΛΕΙΣΤΗΡΙΑΣΜΟΣ / מכרז*\n"
    else:
        header = f"{SCORE_EMOJI.get(l.deal_score, '📍')} *Deal Score: {l.deal_score}/7* `{score_bar}`\n"

    price_str   = f"€{l.price:,}"       if l.price else "?"
    sqm_str     = f"{l.sqm:.0f} מ"ר"    if l.sqm   else "?"
    sqm_p_str   = f"€{l.price_per_sqm:.0f}/מ"ר" if l.price_per_sqm else "?"
    floor_str   = {-1: "ημιυπόγειο", 0: "ισόγειο"}.get(l.floor, f"קומה {l.floor}") if l.floor is not None else "?"
    renov_str   = f"€{l.estimated_renovation:,}" if l.estimated_renovation else "?"
    disc_str    = pct(l.discount_vs_market) if l.discount_vs_market else "?"
    roi_str     = f"{l.estimated_flip_roi*100:.1f}%" if l.estimated_flip_roi else "?"
    yield_str   = f"{l.estimated_rent_yield*100:.1f}%" if l.estimated_rent_yield else "?"

    source_map = {"spitogatos": "Spitogatos", "xe": "XE.gr", "rightmove": "Rightmove"}

    msg = (
        f"{header}"
        f"📍 `{l.area.replace('-', ' ').title()}`  |  {source_map.get(l.source, l.source)}\n"
        f"🏠 {l.title[:60]}\n\n"
        f"💰 *{price_str}*  |  {sqm_str}  |  {sqm_p_str}\n"
        f"🏢 {floor_str}\n"
        f"📊 vs שוק: `{disc_str}`\n\n"
        f"🔨 שיפוץ מוערך: {renov_str}\n"
        f"📈 ROI פליפ: `{roi_str}`\n"
        f"🏠 תשואה שכ"ד: `{yield_str}`\n\n"
        f"🔗 [לפתיחת המודעה]({l.url})\n"
        f"🕐 _{l.scraped_at[:16].replace('T', ' ')}_"
    )
    return msg


def send_telegram(message: str, auction: bool = False):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Auctions go to a separate topic/thread if you want, or same chat
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram sent ✓")
    except Exception as e:
        log.error(f"Telegram error: {e}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    log.info("=== Property Scout starting ===")
    seen = load_seen()
    all_listings: list[Listing] = []

    for area in FILTERS["areas"]:
        # Spitogatos
        all_listings.extend(scrape_spitogatos(area, FILTERS))
        time.sleep(2)
        # XE
        all_listings.extend(scrape_xe(area, FILTERS))
        time.sleep(2)
        # Rightmove
        all_listings.extend(scrape_rightmove(area, FILTERS))
        time.sleep(2)

    log.info(f"Total fetched: {len(all_listings)}")

    new_count = 0
    auction_count = 0

    for listing in all_listings:
        if listing.id in seen:
            continue
        seen.add(listing.id)

        # Skip if missing core data
        if not listing.price or not listing.sqm:
            continue

        # Apply floor filter
        if listing.floor is not None:
            if listing.floor < FILTERS["min_floor"] or listing.floor > FILTERS["max_floor"]:
                continue

        msg = format_message(listing)

        if listing.is_auction:
            # Send auction listings separately
            send_telegram(f"⚖️ *AUCTION ALERT*\n\n{msg}", auction=True)
            auction_count += 1
        else:
            # Only send if deal score >= 3 (avoid noise)
            if listing.deal_score >= 3:
                send_telegram(msg)
                new_count += 1

        time.sleep(0.5)

    save_seen(seen)
    log.info(f"=== Done: {new_count} new alerts, {auction_count} auctions ===")

    # Summary message if anything was sent
    if new_count + auction_count > 0:
        summary = (
            f"📊 *סיכום סריקה*\n"
            f"✅ {new_count} נכסים חדשים\n"
            f"⚖️ {auction_count} מכרזים\n"
            f"🕐 {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC"
        )
        send_telegram(summary)


if __name__ == "__main__":
    main()
