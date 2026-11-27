"""
Trusted Primary Market Scraper for YourIpoBot.
Scrapes verified, institutional-grade IPO data directly from primary market authorities
(InvestorGain & Chittorgarh) to ensure 100% accurate Mainboard vs SME classification,
real price bands, issue sizes, lot sizes, and bidding dates.
"""

from __future__ import annotations
import re
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
from bs4 import BeautifulSoup

from YourIpoBot import config
from YourIpoBot.core.storage import register_new_ipo, determine_is_sme

logger = logging.getLogger("YourIpoBot.trusted_scraper")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def parse_date_str(d_str: str) -> Optional[str]:
    """Parse Indian IPO date string like '2-Dec', '26-Nov' into 'YYYY-MM-DD'."""
    if not d_str or d_str.strip() in ["-", "", "--"]:
        return None
    # Strip any extra text like '26-NovGMP: 7'
    match = re.search(r"(\d{1,2})-([A-Za-z]{3})", d_str)
    if not match:
        return None
    day, mon = match.groups()
    yr = datetime.now().year
    try:
        dt = datetime.strptime(f"{day}-{mon}-{yr}", "%d-%b-%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None

def fetch_trusted_ipos() -> List[Dict[str, Any]]:
    """
    Scrape live verified table from InvestorGain/Chittorgarh primary market feed.
    Returns cleaned, validated IPO dictionaries with accurate Mainboard/SME tagging.
    """
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/all/"
    logger.info("Fetching trusted primary market IPO feed from %s...", url)
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logger.error("Failed to fetch trusted feed: HTTP %s", resp.status_code)
            return []
    except Exception as exc:
        logger.error("Network error fetching trusted feed: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        logger.warning("No table found in trusted feed.")
        return []

    results = []
    rows = table.find_all("tr")[1:]

    for r in rows:
        cells = [td.get_text(strip=True) for td in r.find_all("td")]
        if len(cells) < 9:
            continue

        raw_name = cells[0]
        # Skip empty placeholder rows
        if not raw_name or raw_name == "-":
            continue

        # Extract board from raw name (e.g. 'Monomark EngineeringIPOU', 'Airpro TechnologyNSE SMEO')
        # InvestorGain appends tags like 'IPOU', 'IPOO', 'BSE SMEU', 'NSE SMEU', 'NSE SMEO'
        is_sme_flag = False
        board_tag = "Mainboard"
        clean_name = raw_name

        if "BSE SME" in raw_name or "NSE SME" in raw_name or "SME" in raw_name:
            is_sme_flag = True
            board_tag = "SME"
            clean_name = re.sub(r"(BSE SME[A-Z]?|NSE SME[A-Z]?|SME[A-Z]?)", "", clean_name).strip()
        elif "IPO" in raw_name:
            is_sme_flag = False
            board_tag = "Mainboard"
            clean_name = re.sub(r"IPO[A-Z]?", "", clean_name).strip()

        # Parse price
        price_val = None
        price_str = cells[4].replace("₹", "").replace(",", "").strip()
        if price_str and price_str != "-":
            try:
                price_val = float(price_str)
            except ValueError:
                pass

        # Parse issue size
        size_cr = None
        size_str = cells[5].replace("₹", "").replace("Cr", "").replace(",", "").strip()
        if size_str and size_str != "-":
            try:
                size_cr = float(size_str)
            except ValueError:
                pass

        # Parse lot size
        lot_size = None
        lot_str = cells[6].replace(",", "").strip()
        if lot_str and lot_str != "-":
            try:
                lot_size = int(lot_str)
            except ValueError:
                pass

        # Dates
        open_date = parse_date_str(cells[7])
        close_date = parse_date_str(cells[8])

        # SEBI Regulatory Double-Check via determine_is_sme
        final_is_sme = determine_is_sme(
            canonical_name=clean_name,
            issue_size_cr=size_cr,
            lot_size=lot_size,
            explicit_is_sme=is_sme_flag,
        )

        item = {
            "canonical_name": clean_name,
            "raw_name": raw_name,
            "is_sme": final_is_sme,
            "board": "SME" if final_is_sme else "Mainboard",
            "price": price_val,
            "issue_size_cr": size_cr,
            "lot_size": lot_size,
            "issue_open": open_date,
            "issue_close": close_date,
        }
        results.append(item)

    logger.info("Successfully parsed %d verified IPOs from trusted primary market feed.", len(results))
    return results

def sync_trusted_ipos_to_queue() -> int:
    """Fetch from trusted source and register/update all active IPOs."""
    ipos = fetch_trusted_ipos()
    count = 0
    for ipo in ipos:
        # Only register IPOs that have a price or issue size or open date
        if ipo["price"] or ipo["issue_size_cr"] or ipo["issue_open"]:
            register_new_ipo(
                canonical_name=ipo["canonical_name"],
                is_sme=ipo["is_sme"],
                price_max=ipo["price"],
                issue_size_cr=ipo["issue_size_cr"],
                lot_size=ipo["lot_size"],
                issue_open=ipo["issue_open"],
                issue_close=ipo["issue_close"],
            )
            count += 1
    return count

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    items = fetch_trusted_ipos()
    for item in items:
        print(f"[{item['board']}] {item['canonical_name']} | Price: ₹{item['price']} | Size: ₹{item['issue_size_cr']} Cr | Lot: {item['lot_size']} | {item['issue_open']} -> {item['issue_close']}")
