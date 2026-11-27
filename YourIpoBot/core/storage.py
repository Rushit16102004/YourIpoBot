"""
Database storage and tracking module for YourIpoBot.
Manages company profiles, files, subscription history, and predictions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from YourIpoBot import config

logger = logging.getLogger(__name__)


def get_connection(db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    """Open connection with foreign keys and WAL enabled for concurrency."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(db_path: Path = config.DB_PATH) -> None:
    """Create tracking tables for new unlisted IPOs, downloads, and GMP runs."""
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS active_ipo_queue (
                ipo_id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                symbol TEXT,
                series TEXT,
                is_sme INTEGER DEFAULT 0,
                is_mainboard INTEGER DEFAULT 1,
                issue_open_date TEXT,
                issue_close_date TEXT,
                issue_price_min REAL,
                issue_price_max REAL,
                issue_size_cr REAL,
                company_dir TEXT NOT NULL,
                rhp_status TEXT DEFAULT 'PENDING',       -- PENDING, DOWNLOADED, FAILED
                rhp_downloaded_at TEXT,
                anchor_status TEXT DEFAULT 'PENDING',    -- PENDING, DOWNLOADED, NOT_APPLICABLE, FAILED
                anchor_downloaded_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ipo_predictions_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ipo_id TEXT NOT NULL,
                stage TEXT NOT NULL,                     -- RHP_STAGE, DAY1, DAY2, DAY3_PRE_CUTOFF, FINAL
                predicted_gmp_pct REAL,
                matched_peers_count INTEGER,
                peer_ipo_ids_json TEXT,
                similarity_scores_json TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (ipo_id) REFERENCES active_ipo_queue(ipo_id)
            );

            CREATE TABLE IF NOT EXISTS download_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ipo_id TEXT NOT NULL,
                doc_type TEXT NOT NULL,                  -- RHP, ANCHOR
                source_url TEXT,
                file_path TEXT NOT NULL,
                file_size_kb REAL,
                downloaded_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    logger.info("Database queue and prediction tables initialized.")


def get_company_folder(canonical_name: str) -> Path:
    """Resolve and create isolated per-company directory: YourIpoBot/companies/<SLUG>/."""
    import re
    slug = re.sub(r"[^a-zA-Z0-9_]", "", canonical_name.upper().replace(" ", "_"))
    folder = config.COMPANIES_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def determine_is_sme(
    canonical_name: str = "",
    symbol: Optional[str] = None,
    issue_type: Optional[str] = None,
    segment: Optional[str] = None,
    exchange: Optional[str] = None,
    lot_size: Optional[int | float] = None,
    issue_size_cr: Optional[float] = None,
    explicit_is_sme: Optional[bool] = None,
) -> bool:
    """
    Accurately detects whether an IPO is SME or Mainboard using multiple institutional rules:
    1. Explicit boolean flag if provided.
    2. Exchange / Platform tags ('BSE SME', 'NSE Emerge', 'Emerge', 'SME').
    3. Issue Type strings ('SME', 'BSE SME', 'SME IPO', 'Book Building (SME)', 'Fixed Price (SME)').
    4. Company Name / Symbol tags ('SME', 'EMERGE', '-SM', '-ST').
    5. Lot Size heuristic: SEBI mandates SME minimum application of ₹1.0-1.2 Lakh,
       meaning lot sizes are almost universally >= 500 - 4000 shares (Mainboard is 15-100 shares).
    6. Issue size: SEBI post-issue capital limit for SME is ₹25 Cr. If issue_size > ₹250 Cr, it is Mainboard.
    """
    if explicit_is_sme is True:
        return True

    name_upper = (canonical_name or "").upper()
    sym_upper = (symbol or "").upper()
    type_upper = (issue_type or "").upper()
    seg_upper = (segment or "").upper()
    exch_upper = (exchange or "").upper()

    # Check text indicators in name, symbol, type, segment, or exchange
    text_blob = f"{name_upper} {sym_upper} {type_upper} {seg_upper} {exch_upper}"
    sme_keywords = ["SME", "EMERGE", "NSE EMERGE", "BSE SME", "SME IPO", "SME ISSUE", "BSESME", "NSEEMERGE"]
    for kw in sme_keywords:
        if kw in text_blob:
            return True

    if sym_upper.endswith("-SM") or sym_upper.endswith("-ST") or sym_upper.endswith(".SME"):
        return True

    # Lot size rule: SEBI minimum application value ₹1.0 - 1.2 Lakh means lot size >= 500
    if lot_size is not None:
        try:
            if float(lot_size) >= 500:
                return True
        except (ValueError, TypeError):
            pass

    # Issue size hard filter: definitely Mainboard if > 250 Cr
    if issue_size_cr is not None:
        try:
            if float(issue_size_cr) > 250.0:
                return False
        except (ValueError, TypeError):
            pass

    if explicit_is_sme is not None:
        return bool(explicit_is_sme)

    return False


def register_new_ipo(
    canonical_name: str,
    symbol: Optional[str] = None,
    is_sme: bool = False,
    issue_open: Optional[str] = None,
    issue_close: Optional[str] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    issue_size_cr: Optional[float] = None,
    issue_type: Optional[str] = None,
    segment: Optional[str] = None,
    exchange: Optional[str] = None,
    lot_size: Optional[int | float] = None,
) -> str:
    """Add a newly approved IPO to active_ipo_queue with dedicated company folder."""
    import re
    norm_name = re.sub(r"[^a-z0-9]", "", canonical_name.lower())
    yr = datetime.now().year
    ipo_id = f"{norm_name}_{yr}"
    comp_dir = get_company_folder(canonical_name)

    # Automatically and accurately resolve SME vs Mainboard
    resolved_is_sme = determine_is_sme(
        canonical_name=canonical_name,
        symbol=symbol,
        issue_type=issue_type,
        segment=segment,
        exchange=exchange,
        lot_size=lot_size,
        issue_size_cr=issue_size_cr,
        explicit_is_sme=is_sme,
    )

    anchor_init = "NOT_APPLICABLE" if resolved_is_sme else "PENDING"
    now_iso = datetime.now().isoformat()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO active_ipo_queue (
                ipo_id, canonical_name, symbol, is_sme, is_mainboard,
                issue_open_date, issue_close_date, issue_price_min, issue_price_max,
                issue_size_cr, company_dir, anchor_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ipo_id) DO UPDATE SET
                symbol = COALESCE(excluded.symbol, active_ipo_queue.symbol),
                is_sme = excluded.is_sme,
                is_mainboard = excluded.is_mainboard,
                issue_open_date = COALESCE(excluded.issue_open_date, active_ipo_queue.issue_open_date),
                issue_close_date = COALESCE(excluded.issue_close_date, active_ipo_queue.issue_close_date),
                issue_price_max = COALESCE(excluded.issue_price_max, active_ipo_queue.issue_price_max),
                issue_size_cr = COALESCE(excluded.issue_size_cr, active_ipo_queue.issue_size_cr),
                updated_at = excluded.updated_at;
            """,
            (
                ipo_id,
                canonical_name,
                symbol,
                1 if resolved_is_sme else 0,
                0 if resolved_is_sme else 1,
                issue_open,
                issue_close,
                price_min,
                price_max,
                issue_size_cr,
                str(comp_dir),
                anchor_init,
                now_iso,
                now_iso,
            ),
        )
        conn.commit()

    # Create profile.json inside company folder
    profile_path = comp_dir / "profile.json"
    if not profile_path.exists():
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ipo_id": ipo_id,
                    "canonical_name": canonical_name,
                    "symbol": symbol,
                    "is_sme": resolved_is_sme,
                    "issue_open": issue_open,
                    "issue_close": issue_close,
                    "price_band": [price_min, price_max],
                    "issue_size_cr": issue_size_cr,
                    "registered_at": now_iso,
                },
                f,
                indent=2,
            )

    logger.info("Registered active IPO: %s (Directory: %s)", ipo_id, comp_dir)
    return ipo_id
