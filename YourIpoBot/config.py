"""
YourIpoBot System Configuration
Centralized settings for directory paths, thresholds, and schedule rules.
"""

from __future__ import annotations

import os
from pathlib import Path

# Base Paths
BASE_DIR: Path = Path(__file__).resolve().parent
COMPANIES_DIR: Path = BASE_DIR / "companies"
DATA_DIR: Path = BASE_DIR / "data"
WAREHOUSE_DIR: Path = DATA_DIR / "warehouse"
STAGING_DIR: Path = DATA_DIR / "staging"

# Core Database and Master CSV
DB_PATH: Path = WAREHOUSE_DIR / "ipo_history.db"
MASTER_CSV_PATH: Path = STAGING_DIR / "ipo_merged_master.csv"
STATE_FILE: Path = DATA_DIR / "pipeline_state.json"

# Network & Anti-Block Settings
DEFAULT_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

REQUEST_TIMEOUT_SECONDS: int = 30
POLITE_MIN_DELAY: float = 3.5
POLITE_MAX_DELAY: float = 8.0

# Download Constraints
MAX_RHP_PER_DAY: int = 1
MAX_ANCHOR_PER_DAY: int = 2
MIN_HOURS_BETWEEN_ANCHOR_DOWNLOADS: float = 1.0

# Similarity & Peer Selection Weights (Bidding subscription removed from similarity matching)
WEIGHT_SUBSCRIPTION: float = 0.00       # Ignored in peer similarity matching (used for Apply/Avoid filter)
WEIGHT_PROFITABILITY: float = 0.35      # Margins & Net Profit (35%)
WEIGHT_ISSUE_SIZE: float = 0.25         # Issue Size & Base Price (25%)
WEIGHT_DEBT_CASH: float = 0.20          # Debt-to-Equity, Cash, OFS vs Fresh (20%)
WEIGHT_OTHER_INFO: float = 0.20         # Sector, Governance, Promoter Stake (20%)

SIMILARITY_THRESHOLD: float = 83.0      # Minimum % match required (adjusted to 83% per user specification)
TOP_PEERS_COUNT: int = 5                # Number of historical peers to average
