"""
Property Scout v2 - Multi-Profile Edition
Reads profiles.json and runs scraping for all active profiles
Saves results to results.json for the dashboard
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
from typing import Optional, List

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

PROFILES_FILE = "scraper/profiles.json"
RESULTS_FILE  = "scraper/results.json"
SEEN_FILE     = "seen_listings.json"

AUCTION_KEYWORDS = [
    "pleistairiasmos",
    "pleistiriasmou",
    "auction",
    "\u03c0\u03bb\u03b5\u03b9\u03c3\u03c4\u03b7\u03c1\u03b9\u03b1\u03c3\u03bc",
    "\u03ba\u03b1\u03c4\u03ac\u03c3\u03c7\u03b5\u03c3\u03b7",
    "\u03b5\u03ba\u03c0\u03bb\u03b5\u03b9\u03c3\u03c4\u03b7\u03c1\u03af\u03b1\u03c3\u03b7",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
}


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
        self.price_per_sqm = self.price / self.sqm
        market_sqm = bench["price_sqm"]
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


def get_soup(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return None


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
        id=listing_id,
        source=source,
        title=title[:80],
        url=href,
        price=price,
        sqm=sqm,
        floor=floor,
        area=area,
        price_per_sqm=(price / sqm) if price and sqm else None,
        is_auction=is_auction,
        description=desc[:300],
        scraped_at=datetime.utcnow().isoformat(),
        profile_id=profile_id,
    )
    l.compute_analysis(benchmarks, renov_cost)
    return l


def scrape_spitogatos(area, filters, profile_id, benchmarks, renov_cost):
    listings = []
    base = "https://www.spitogatos.gr"
    url = (
        base + "/sale-flats/" + area
        + "?minPrice=" + str(filters["min_price"])
        + "&maxPrice=" + str(filters["max_price"])
        + "&minArea=" + str(filters["min_sqm"])
        + "&maxArea=" + str(filters["max_sqm"])
        + "&sort=date_desc"
    )
    log.info("Spitogatos: %s", url)
    soup = get_soup(url)
    if not soup:
        return listings
    cards = soup.select("article.listing-item, div.property-listing-item, li.result-item, [data-id], .property-card")
    for card in cards[:20]:
        try:
            link = card.select_one("a[href]")
            if not link:
                continue
            href = link.get("href", "")
            if not href.startswith("http"):
                href = base + href
            price_el = card.select_one(".price, [class*='price']")
            price = parse_number(price_el.get_text() if price_el else "")
            sqm_el = card.select_one(".area, [class*='area'], [class*='sqm']")
            sqm_text = sqm_el.get_text() if sqm_el else ""
            sqm_match = re.search(r"(\d+)", sqm_text)
            sqm = float(sqm_match.group(1)) if sqm_match else None
            title_el = card.select_one("h2, h3, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else "Apartment"
            desc_el = card.select_one("p, [class*='desc']")
            desc = desc_el.get_text(strip=True) if desc_el else ""
            l = make_listing("spitogatos", href, title, price, sqm, None, area, desc, profile_id, filters, benchmarks, renov_cost)
            if l:
                listings.append(l)
        except Exception as e:
            log.debug("Spitogatos error: %s", e)
    log.info("Spitogatos %s: %d listings", area, len(listings))
    return listings


def scrape_xe(area, filters, profile_id, benchmarks, renov_cost):
    listings = []
    base = "https://www.xe.gr"
    xe_area_map = {
        "nea-smyrni": "Nea+Smyrni",
        "kallithea": "Kallithea",
        "palaio-faliro": "Palaio+Faliro",
        "glyfada": "Glyfada",
        "ilioupoli": "Ilioupoli",
    }
    area_name = xe_area_map.get(area, area)
    url = (
        base + "/property/for-sale/apartments"
        + "?area=" + area_name
        + "&price_from=" + str(filters["min_price"])
        + "&price_to=" + str(filters["max_price"])
        + "&size_from=" + str(filters["min_sqm"])
        + "&size_to=" + str(filters["max_sqm"])
        + "&sort=date"
    )
    log.info("XE.gr: %s", url)
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
            price = parse_number(card.get_text())
            sqm_match = re.search(r"(\d+)\s*t\.m", card.get_text())
            sqm = float(sqm_match.group(1)) if sqm_match else None
            title_el = card.select_one("h2, h3, .title")
            title = title_el.get_text(strip=True) if title_el else "Apartment"
            l = make_listing("xe", href, title, price, sqm, None, area, card.get_text()[:300], profile_id, filters, benchmarks, renov_cost)
            if l:
                listings.append(l)
        except Exception as e:
            log.debug("XE error: %s", e)
    log.info("XE %s: %d listings", area, len(listings))
    return listings


def scrape_rightmove(area, filters, profile_id, benchmarks, renov_cost):
    listings = []
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
        "https://www.rightmove.co.uk/overseas-property/in-Greece.html"
        + "?locationIdentifier=" + loc
        + "&minPrice=" + str(filters["min_price"])
        + "&maxPrice=" + str(filters["max_price"])
        + "&propertyTypes=flat"
    )
    log.info("Rightmove: %s", url)
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
            price_el = card.select_one(".propertyCard-priceValue, .price")
            price = parse_number(price_el.get_text() if price_el else "")
            title_el = card.select_one("h2, .propertyCard-title")
            title = title_el.get_text(strip=True) if title_el else "Property"
            desc_el = card.select_one(".propertyCard-description")
            desc = desc_el.get_text(strip=True) if desc_el else ""
            sqm_match = re.search(r"(\d+)\s*(sq\.?\s*m|m2)", desc, re.IGNORECASE)
            sqm = float(sqm_match.group(1)) if sqm_match else None
            l = make_listing("rightmove", href, title, price, sqm, None, area, desc, profile_id, filters, benchmarks, renov_cost)
            if l:
                listings.append(l)
        except Exception as e:
            log.debug("Rightmove error: %s", e)
    log.info("Rightmove %s: %d listings", area, len(listings))
    return listings


def load_profiles():
    if not os.path.exists(PROFILES_FILE):
        log.error("profiles.json not found at %s", PROFILES_FILE)
        sys.exit(1)
    with open(PROFILES_FILE) as f:
        return json.load(f)


def save_profiles(data):
    with open(PROFILES_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def load_results():
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            return json.load(f)
    return {"listings": [], "runs": []}


def save_results(data):
    # Keep only last 500 listings to prevent file bloat
    data["listings"] = data["listings"][-500:]
    data["runs"] = data["runs"][-50:]
    with open(RESULTS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def format_message(l, profile_name):
    score_bar = "#" * l.deal_score + "-" * (7 - l.deal_score)
    disc = (l.discount_vs_market * 100) if l.discount_vs_market else 0
    roi = (l.estimated_flip_roi * 100) if l.estimated_flip_roi else 0
    yld = (l.estimated_rent_yield * 100) if l.estimated_rent_yield else 0
    price_str = "EUR{:,}".format(l.price) if l.price else "?"
    sqm_str = "{:.0f}sqm".format(l.sqm) if l.sqm else "?"
    sqmp_str = "EUR{:.0f}/sqm".format(l.price_per_sqm) if l.price_per_sqm else "?"
    renov_str = "EUR{:,}".format(l.estimated_renovation) if l.estimated_renovation else "?"
    area_clean = l.area.replace("-", " ").title()
    source_map = {"spitogatos": "Spitogatos", "xe": "XE.gr", "rightmove": "Rightmove"}

    if l.is_auction:
        header = "AUCTION ALERT\n"
    else:
        header = "Deal Score: {}/7 [{}]\n".format(l.deal_score, score_bar)

    lines = [
        header,
        "Profile: {}".format(profile_name),
        "{} | {}".format(area_clean, source_map.get(l.source, l.source)),
        l.title[:60],
        "",
        "{} | {} | {}".format(price_str, sqm_str, sqmp_str),
        "vs market: {:+.1f}%".format(disc),
        "",
        "Renovation est.: {}".format(renov_str),
        "Flip ROI: {:.1f}%".format(roi),
        "Rent yield: {:.1f}%".format(yld),
        "",
        l.url,
    ]
    return "\n".join(lines)


def send_telegram(message):
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram sent OK")
    except Exception as e:
        log.error("Telegram error: %s", e)


def run_profile(profile, benchmarks, seen, results):
    """Run scraping for a single profile."""
    name = profile["name"]
    pid = profile["id"]
    filters = profile["filters"]
    renov = profile.get("renovation_cost_per_sqm", 800)

    log.info("=" * 60)
    log.info("Running profile: %s (%s)", name, pid)
    log.info("=" * 60)

    all_listings = []
    for area in filters["areas"]:
        all_listings.extend(scrape_spitogatos(area, filters, pid, benchmarks, renov))
        time.sleep(2)
        all_listings.extend(scrape_xe(area, filters, pid, benchmarks, renov))
        time.sleep(2)
        all_listings.extend(scrape_rightmove(area, filters, pid, benchmarks, renov))
        time.sleep(2)

    log.info("Profile %s: %d total listings fetched", pid, len(all_listings))

    new_count = 0
    auction_count = 0
    min_score = filters.get("min_deal_score", 3)

    for l in all_listings:
        if l.id in seen:
            continue
        seen.add(l.id)
        if not l.price or not l.sqm:
            continue
        if l.floor is not None:
            if l.floor < filters.get("min_floor", -10) or l.floor > filters.get("max_floor", 100):
                continue

        # Save to results regardless of score
        results["listings"].append(asdict(l))

        msg = format_message(l, name)
        if l.is_auction:
            send_telegram("AUCTION ALERT\n\n" + msg)
            auction_count += 1
        elif l.deal_score >= min_score:
            send_telegram(msg)
            new_count += 1

        time.sleep(0.5)

    return {
        "profile_id": pid,
        "profile_name": name,
        "fetched": len(all_listings),
        "new_alerts": new_count,
        "auctions": auction_count,
        "timestamp": datetime.utcnow().isoformat(),
    }


def main():
    log.info("=== Property Scout v2 starting ===")

    # Check for profile filter argument (used by manual runs)
    profile_filter = os.environ.get("PROFILE_ID", "").strip()

    data = load_profiles()
    profiles = data.get("profiles", [])
    benchmarks = data.get("area_benchmarks", {})

    if profile_filter:
        profiles = [p for p in profiles if p["id"] == profile_filter]
        log.info("Filter: running only profile '%s'", profile_filter)
    else:
        profiles = [p for p in profiles if p.get("active", True)]
        log.info("Running %d active profiles", len(profiles))

    if not profiles:
        log.warning("No profiles to run")
        send_telegram("No active profiles to scan.")
        return

    seen = load_seen()
    results = load_results()

    run_summary = []
    for profile in profiles:
        try:
            r = run_profile(profile, benchmarks, seen, results)
            run_summary.append(r)
            # Update last_run timestamp
            for p in data["profiles"]:
                if p["id"] == profile["id"]:
                    p["last_run"] = datetime.utcnow().isoformat()
        except Exception as e:
            log.error("Profile %s failed: %s", profile["id"], e)
            run_summary.append({
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            })

    # Save everything
    save_seen(seen)
    results["runs"].extend(run_summary)
    save_results(results)
    save_profiles(data)

    # Send summary
    total_new = sum(r.get("new_alerts", 0) for r in run_summary)
    total_auctions = sum(r.get("auctions", 0) for r in run_summary)
    summary_lines = [
        "Scout Summary",
        "Time: {} UTC".format(datetime.utcnow().strftime("%d/%m/%Y %H:%M")),
        "",
    ]
    for r in run_summary:
        if "error" in r:
            summary_lines.append("[X] {}: {}".format(r["profile_name"], r["error"][:50]))
        else:
            summary_lines.append("[OK] {}: {} fetched, {} alerts, {} auctions".format(
                r["profile_name"], r["fetched"], r["new_alerts"], r["auctions"]
            ))
    summary_lines.extend(["", "Total: {} alerts, {} auctions".format(total_new, total_auctions)])
    send_telegram("\n".join(summary_lines))

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
