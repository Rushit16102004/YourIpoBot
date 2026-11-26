"""Configuration paths, HTTP settings, Gemini settings, and deterministic scoring thresholds."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

os.environ.pop("GOOGLE_API_KEY", None)
load_dotenv(PROJECT_ROOT / ".env", override=True)

DATA_DIR = PROJECT_ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
SEEN_IPOS_PATH = DATA_DIR / "seen_ipos.json"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
)

# HTTP / Rate limiting / Retry settings
REQUEST_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 90
RATE_LIMIT_MIN_SEC = 1.0
RATE_LIMIT_MAX_SEC = 2.0
MAX_RETRIES = 3
BACKOFF_FACTOR = 1.5

# PDF validation
PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"
MIN_PDF_BYTES = 10_000  # 10 KB minimum sanity floor

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

NSE_API_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
}

HTML_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DOWNLOAD_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,application/zip,application/octet-stream,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# Public endpoints
NSE_HOME_URL = "https://www.nseindia.com"
NSE_IPO_PAGE_URL = "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"
NSE_CURRENT_ISSUES_URL = "https://www.nseindia.com/api/ipo-current-issue"
NSE_UPCOMING_ISSUES_URL = "https://www.nseindia.com/api/all-upcoming-issues?category=ipo"
NSE_IPO_DETAIL_URL = "https://www.nseindia.com/api/ipo-detail?symbol={symbol}"
NSE_CIRCULARS_URL = "https://www.nseindia.com/api/circulars"
NSE_ARCHIVE_RHP_URL = "https://nsearchives.nseindia.com/content/ipo/RHP_{symbol}.zip"
NSE_ARCHIVE_ANCHOR_ZIP_URL = "https://nsearchives.nseindia.com/content/ipo/ANCHOR_{symbol}.zip"
NSE_ARCHIVE_ANCHOR_PDF_URL = "https://nsearchives.nseindia.com/content/ipo/ANCHOR_{symbol}.pdf"

SEBI_RHP_LIST_URL = (
    "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=11"
)
CHITTORGARH_IPO_LIST_URL = (
    "https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/"
)


@dataclass(frozen=True)
class ScoringThresholds:
    """Deterministic scoring weights and hard-skip thresholds."""

    # Weights (total = 100)
    weight_fresh_issue: int = 20
    weight_growth_ocf: int = 25
    weight_valuation: int = 20
    weight_anchor: int = 20
    weight_qib: int = 15

    # Fresh issue thresholds (%)
    fresh_issue_strong_pct: float = 60.0
    fresh_issue_ok_pct: float = 30.0

    # Growth thresholds (%)
    revenue_cagr_strong_pct: float = 20.0
    revenue_cagr_ok_pct: float = 10.0
    pat_cagr_strong_pct: float = 25.0
    pat_cagr_ok_pct: float = 12.0

    # Valuation vs peer median P/E ratio (ipo_pe / peer_median_pe)
    pe_discount_ratio: float = 0.85
    pe_fair_ratio: float = 1.05
    pe_expensive_ratio: float = 1.35
    hard_skip_pe_ratio: float = 1.80

    # Tier-1 Anchor share (%)
    anchor_strong_pct: float = 50.0
    anchor_ok_pct: float = 30.0

    # Live QIB subscription multiple (x)
    qib_strong_x: float = 15.0
    qib_confirm_x: float = 5.0
    qib_hard_skip_day3_x: float = 1.5

    # Decision score cutoffs
    apply_min_score: float = 70.0
    watch_min_score: float = 45.0


THRESHOLDS = ScoringThresholds()


def ensure_data_dirs() -> None:
    """Create base data directories if they do not exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def symbol_dir(symbol: str) -> Path:
    """Return the cache directory for a given IPO symbol."""
    clean = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in ("-", "_"))
    path = DATA_DIR / clean
    path.mkdir(parents=True, exist_ok=True)
    return path
