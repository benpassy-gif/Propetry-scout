"""
Property Scout Greece - v4.0
Based on v3.1 (Playwright Edition) with the following changes:
- Deduplication: tracks price per listing, re-alerts if price changed
- No alert cap: sends ALL listings with score >= 4 (was capped at 4)
- Two daily runs: 07:00 + 19:00 Israel time
- seen_listings.json stores {id: {price, url, last_seen}} instead of a flat set
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

MIN_SCORE = 4  # Send ALL listings with score >= 4 (no cap on number)

AUCTION_KEYWORDS = [
    "pleistairiasmos",
    "pleistiriasmou",
    "auction",
    "\u03c0\u03bb\u03b5\u03b9\u03c3\u03c4\u03b7\u03c1\u03b9\u03b1\u03c3\u03bc",
    "\u03ba\u03b1\u03c4\u03ac\u03c3\u03c7\u03b5\u03c3\u03b7",
    "\u03b5\u03ba\u03c0\u03bb\u03b5\u03b9\u03c3\u03c4\u03b7\u03c1\u03af\u03b1\u03c3\u03b7",
]


# ── Data class ──────────────────────────────────────────────────────────────
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


# ── Helpers ──────────────────────────────────────────────────────────────────
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


# ── Scrapers ──────────────────────────────────────────────────────────────────
def scrape_spitogatos_playwright(page, area, filters, profile_id, benchmarks, renov_cost):
    listings = []
    base = "https://www.spitogatos.gr"
    url = (
        base + "/en/for_sale-homes/" + area
        + "?price[]=" + str(filters["min_price"]) + "%2C" + str(filters["max_price"])
        + "&areas[]=" + str(filters["min_sqm"]) + "%2C" + str(filters["max_sqm"])
    )
    log.info("Spitogatos: %s", url)

    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('a[href*="/property/"], .no-results', timeout=10000)
        except PlaywrightTimeout:
            log.warning("Spitogatos %s: no listings selector found", area)

        time.sleep(2)

        results = page.evaluate("""
        () => {
          const cards = document.querySelectorAll('a[href*="/property/"]');
          const seen = new Set();
          const out = [];
          cards.forEach(card => {
            const href = card.href;
            if (seen.has(href)) return;
            seen.add(href);
            const container = card.closest('article, li, div');
            const text = container ? container.innerText : card.innerText;
            out.push({ href: href, text: text });
          });
          return out;
        }
        """)

        for item in results[:25]:
            try:
                href = item["href"]
                text = item["text"]
                price_match = re.search(r"\u20ac\s*([\d.,]+)", text)
                price = parse_number(price_match.group(1)) if price_match else None
                sqm_match = re.search(r"(\d+)\s*m[\u00b22]", text)
                sqm = float(sqm_match.group(1)) if sqm_match else None
                title = text.split("\n")[0][:80] if text else "Apartment"
                floor_match = re.search(r"(\d+)(?:st|nd|rd|th)\s*floor", text, re.IGNORECASE)
                floor = int(floor_match.group(1)) if floor_match else None
                l = make_listing("spitogatos", href, title, price, sqm, floor, area, text,
                                 profile_id, filters, benchmarks, renov_cost)
                if l:
                    listings.append(l)
            except Exception as e:
                log.debug("Spitogatos card error: %s", e)

    except Exception as e:
        log.warning("Spitogatos %s failed: %s", area, e)

    log.info("Spitogatos %s: %d listings", area, len(listings))
    return listings


def scrape_xe_playwright(page, area, filters, profile_id, benchmarks, renov_cost):
    listings = []
    xe_area_map = {
        "nea-smyrni": "nea-smyrni-attiki",
        "kallithea": "kallithea-attiki",
        "palaio-faliro": "palaio-faliro-attiki",
        "glyfada": "glyfada-attiki",
        "ilioupoli": "ilioupoli-attiki",
    }
    area_slug = xe_area_map.get(area, area)
    url = (
        "https://www.xe.gr/property/results/"
        + "?Transaction.type_channel=117518"
        + "&Item.category=117541"
        + "&geo_place_ids=" + area_slug
        + "&Item.price.from=" + str(filters["min_price"])
        + "&Item.price.to=" + str(filters["max_price"])
        + "&Item.area.from=" + str(filters["min_sqm"])
        + "&Item.area.to=" + str(filters["max_sqm"])
    )
    log.info("XE.gr: %s", url)

    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('a[href*="/property/"], [class*="result"]', timeout=10000)
        except PlaywrightTimeout:
            log.warning("XE %s: no listings found", area)

        time.sleep(2)

        results = page.evaluate("""
        () => {
          const cards = document.querySelectorAll('a[href*="/property/"]');
          const seen = new Set();
          const out = [];
          cards.forEach(card => {
            const href = card.href;
            if (seen.has(href)) return;
            seen.add(href);
            const container = card.closest('article, li, div');
            const text = container ? container.innerText : card.innerText;
            out.push({ href: href, text: text });
          });
          return out;
        }
        """)

        for item in results[:25]:
            try:
                href = item["href"]
                text = item["text"]
                price_match = re.search(r"\u20ac\s*([\d.,]+)", text)
                price = parse_number(price_match.group(1)) if price_match else None
                sqm_match = re.search(r"(\d+)\s*(?:m[\u00b22]|\u03c4\.\u03bc)", text)
                sqm = float(sqm_match.group(1)) if sqm_match else None
                title = text.split("\n")[0][:80] if text else "Apartment"
                l = make_listing("xe", href, title, price, sqm, None, area, text,
                                 profile_id, filters, benchmarks, renov_cost)
                if l:
                    listings.append(l)
            except Exception as e:
                log.debug("XE card error: %s", e)

    except Exception as e:
        log.warning("XE %s failed: %s", area, e)

    log.info("XE %s: %d listings", area, len(listings))
    return listings


def scrape_rightmove_playwright(page, area, filters, profile_id, benchmarks, renov_cost):
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

    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('a[href*="/properties/"], .propertyCard', timeout=10000)
        except PlaywrightTimeout:
            log.warning("Rightmove %s: no listings", area)

        time.sleep(2)

        results = page.evaluate("""
        () => {
          const cards = document.querySelectorAll('a[href*="/properties/"]');
          const seen = new Set();
          const out = [];
          cards.forEach(card => {
            const href = card.href;
            if (seen.has(href)) return;
            seen.add(href);
            const container = card.closest('.propertyCard, article, div');
            const text = container ? container.innerText : card.innerText;
            out.push({ href: href, text: text });
          });
          return out;
        }
        """)

        for item in results[:15]:
            try:
                href = item["href"]
                text = item["text"]
                price_match = re.search(r"[\u20ac\u00a3]\s*([\d.,]+)", text)
                price = parse_number(price_match.group(1)) if price_match else None
                sqm_match = re.search(r"(\d+)\s*(?:sq\.?\s*m|m[\u00b22])", text, re.IGNORECASE)
                sqm = float(sqm_match.group(1)) if sqm_match else None
                title = text.split("\n")[0][:80] if text else "Property"
                l = make_listing("rightmove", href, title, price, sqm, None, area, text,
                                 profile_id, filters, benchmarks, renov_cost)
                if l:
                    listings.append(l)
            except Exception as e:
                log.debug("Rightmove card error: %s", e)

    except Exception as e:
        log.warning("Rightmove %s failed: %s", area, e)

    log.info("Rightmove %s: %d listings", area, len(listings))
    return listings


# ── Seen listings (deduplication with price tracking) ────────────────────────
def load_seen() -> dict:
    """Returns {listing_id: {price, url, last_seen}}"""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            data = json.load(f)
        # Migrate old format (flat list) to new dict format
        if isinstance(data, list):
            log.info("Migrating seen_listings to new format")
            return {lid: {"price": 0, "url": "", "last_seen": ""} for lid in data}
        return data
    return {}


def save_seen(seen: dict):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def is_new_or_price_changed(listing: Listing, seen: dict) -> bool:
    entry = seen.get(listing.id)
    if entry is None:
        return True  # Never seen before
    prev_price = entry.get("price", 0)
    if listing.price and listing.price != prev_price:
        return True  # Price has changed
    return False


def mark_seen(listing: Listing, seen: dict):
    seen[listing.id] = {
        "price": listing.price,
        "url": listing.url,
        "last_seen": datetime.utcnow().isoformat(),
    }


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(message: str):
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


# ── Formatting ────────────────────────────────────────────────────────────────
def format_message(l: Listing, profile_name: str, price_changed: bool = False) -> str:
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
        header = "AUCTION ALERT"
    elif price_changed:
        header = "PRICE CHANGE - Score: {}/7 [{}]".format(l.deal_score, score_bar)
    else:
        header = "Deal Score: {}/7 [{}]".format(l.deal_score, score_bar)

    lines = [
        header,
        "",
        "Profile: {}".format(profile_name),
        "{} | {}".format(area_clean, source_map.get(l.source, l.source)),
        l.title[:60],
        "",
        "{} | {} | {}".format(price_str, sqm_str, sqmp_str),
        "vs market: {:+.1f}%".format(disc),
        "",
        "Renovation est.: {}".format(renov_str),
        "Total cost: EUR{:,}".format(l.total_cost) if l.total_cost else "",
        "ARV: EUR{:,}".format(l.arv) if l.arv else "",
        "Flip ROI: {:.1f}%".format(roi),
        "Rent yield: {:.1f}%".format(yld),
        "",
        l.url,
    ]
    return "\n".join(line for line in lines if line != "")


# ── Profile runner ────────────────────────────────────────────────────────────
def run_profile(profile, benchmarks, seen, results, page):
    name = profile["name"]
    pid = profile["id"]
    filters = profile["filters"]
    renov = profile.get("renovation_cost_per_sqm", 800)

    log.info("=" * 60)
    log.info("Running profile: %s (%s)", name, pid)
    log.info("=" * 60)

    all_listings = []
    for area in filters["areas"]:
        all_listings.extend(scrape_spitogatos_playwright(page, area, filters, pid, benchmarks, renov))
        time.sleep(1)
        all_listings.extend(scrape_xe_playwright(page, area, filters, pid, benchmarks, renov))
        time.sleep(1)
        all_listings.extend(scrape_rightmove_playwright(page, area, filters, pid, benchmarks, renov))
        time.sleep(1)

    log.info("Profile %s: %d total listings fetched", pid, len(all_listings))

    new_count = 0
    price_change_count = 0
    auction_count = 0

    for l in all_listings:
        if not l.price or not l.sqm:
            continue
        if l.floor is not None:
            if l.floor < filters.get("min_floor", -10) or l.floor > filters.get("max_floor", 100):
                continue

        already_seen = l.id in seen
        send_it = is_new_or_price_changed(l, seen)
        price_changed = already_seen and send_it

        mark_seen(l, seen)
        results["listings"].append(asdict(l))

        if not send_it:
            continue

        msg = format_message(l, name, price_changed=price_changed)

        if l.is_auction:
            send_telegram("AUCTION ALERT\n\n" + msg)
            auction_count += 1
        elif l.deal_score >= MIN_SCORE:
            send_telegram(msg)
            if price_changed:
                price_change_count += 1
            else:
                new_count += 1

        time.sleep(0.5)

    return {
        "profile_id": pid,
        "profile_name": name,
        "fetched": len(all_listings),
        "new_alerts": new_count,
        "price_changes": price_change_count,
        "auctions": auction_count,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ── File helpers ──────────────────────────────────────────────────────────────
def load_profiles():
    if not os.path.exists(PROFILES_FILE):
        log.error("profiles.json not found at %s", PROFILES_FILE)
        sys.exit(1)
    with open(PROFILES_FILE) as f:
        return json.load(f)


def save_profiles(data):
    with open(PROFILES_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_results():
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            return json.load(f)
    return {"listings": [], "runs": []}


def save_results(data):
    data["listings"] = data["listings"][-500:]
    data["runs"] = data["runs"][-50:]
    with open(RESULTS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== Property Scout v4.0 starting ===")
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
        send_telegram("Property Scout: No active profiles to scan.")
        return

    seen = load_seen()
    results = load_results()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="el-GR",
            viewport={"width": 1366, "height": 768},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
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
                log.error("Profile %s failed: %s", profile["id"], e)
                run_summary.append({
                    "profile_id": profile["id"],
                    "profile_name": profile["name"],
                    "error": str(e),
                    "timestamp": datetime.utcnow().isoformat(),
                })

        browser.close()

    save_seen(seen)
    results["runs"].extend(run_summary)
    save_results(results)
    save_profiles(data)

    total_new = sum(r.get("new_alerts", 0) for r in run_summary)
    total_price = sum(r.get("price_changes", 0) for r in run_summary)
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
            parts = "{} fetched, {} new".format(r["fetched"], r["new_alerts"])
            if r.get("price_changes"):
                parts += ", {} price changes".format(r["price_changes"])
            if r.get("auctions"):
                parts += ", {} auctions".format(r["auctions"])
            summary_lines.append("[OK] {}: {}".format(r["profile_name"], parts))

    summary_lines.append("")
    summary_lines.append("Total: {} new, {} price changes, {} auctions".format(
        total_new, total_price, total_auctions
    ))
    send_telegram("\n".join(summary_lines))
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
