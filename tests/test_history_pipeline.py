"""Unit tests for ipo_history_pipeline normalization, FK merging, and SQLite NULL preservation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from ipo_history_pipeline.merger import (
    build_warehouse,
    extract_year,
    make_ipo_id,
    normalize_company_name,
)


def test_normalize_company_name_and_year_extraction() -> None:
    assert normalize_company_name("Chalet Hotels Ltd.") == "chalet hotels"
    assert (
        normalize_company_name("Foodlink F&B Holdings (India) Limited")
        == "foodlink f and b holdings"
    )
    assert normalize_company_name("Aegis Logistics Pvt. Ltd.") == "aegis logistics"

    assert extract_year("03/02/10") == 2010
    assert extract_year("24 Aug 2026") == 2026
    assert extract_year("2019-02-07") == 2019
    assert extract_year(None) is None

    assert make_ipo_id("chalet hotels", 2019) == "chalet_hotels_2019"
    assert make_ipo_id("chalet hotels", None) == "chalet_hotels_unknown"


def test_build_warehouse_preserves_columns_and_nulls(tmp_path: Path, monkeypatch) -> None:
    from ipo_history_pipeline import config

    raw_dir = tmp_path / "raw"
    staging_dir = tmp_path / "staging"
    db_path = tmp_path / "warehouse" / "ipo_history.db"
    monkeypatch.setattr(config, "RAW_DIR", raw_dir)
    monkeypatch.setattr(config, "STAGING_DIR", staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Create synthetic samples mimicking two distinct GitHub repos
    saqib_dir = raw_dir / "saqibsafdar11__Predicting-Listing-Gains-in-the-Indian-IPO-Market"
    saqib_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "Date ": "07/02/19",
                "IPOName": "Chalet Hotels Ltd",
                "Issue_Size": 1641.2,
                "Subscription_QIB": 4.7,
                "Subscription_HNI": 1.1,
                "Subscription_RII": 0.03,
                "Subscription_Total": 1.6,
                "Issue_Price": 280.0,
                "Listing_Gains_Percent": 3.7,
            }
        ]
    ).to_csv(saqib_dir / "Indian_IPO_Market_Data.csv", index=False)

    spec_dir = raw_dir / "Spectrumz00__india-ipo-gmp-data" / "data"
    spec_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "name": "Chalet Hotels Limited",
                "slug": "chalet-hotels",
                "type": "Mainboard",
                "issue_price": 280.0,
                "final_gmp": 12.0,
                "listing_price": 290.4,
                "predicted_pct": 4.29,
                "actual_pct": 3.71,
                "error_pct": 0.58,
                "direction_ok": True,
                "listed_date": "07 Feb 2019",
            },
            {
                "name": "Only GMP Company Pvt Ltd",
                "slug": "only-gmp",
                "type": "SME",
                "issue_price": None,
                "final_gmp": 5.0,
                "listing_price": None,
                "predicted_pct": None,
                "actual_pct": None,
                "error_pct": None,
                "direction_ok": None,
                "listed_date": "10 Oct 2025",
            },
        ]
    ).to_csv(spec_dir / "accuracy.csv", index=False)

    yf_cache = staging_dir / "yfinance_enrichment.json"
    yf_cache.write_text(
        json.dumps(
            {
                "chalet_hotels_2019": {
                    "status": "ok",
                    "resolved_ticker": "CHALET.NS",
                    "listing_close": 290.4,
                    "latest_close": 850.0,
                    "total_debt": 2500.0,
                }
            }
        ),
        encoding="utf-8",
    )

    summary = build_warehouse(
        raw_dir=raw_dir,
        db_path=db_path,
        yfinance_cache_path=yf_cache,
        rhp_cache_path=staging_dir / "missing_rhp.json",
    )

    assert summary.table_row_counts["dim_ipos"] == 1
    assert summary.matched_multi_source_ipos == 1  # chalet_hotels_2019 matched saqib + spec + yfinance

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT ipo_id, ipo_year, ipo_month, issue_base_price, issue_size_cr, QIB_sum, retail_sum, nii_sum, total_sub, GMP_Day3_Clean, sources_present FROM ipo_merged_master WHERE ipo_id = 'chalet_hotels_2019'"
        ).fetchone()
        assert row[:10] == ("chalet_hotels_2019", 2019, 2, 280.0, 1641.2, 4.7, 0.03, 1.1, 1.6, 3.71)
        assert "yfinance" in row[10]

        # Verify rows without valid GMP are excluded from master
        null_row = conn.execute(
            "SELECT ipo_id FROM ipo_merged_master WHERE ipo_id = 'only_gmp_2025'"
        ).fetchone()
        assert null_row is None

        # Verify excluded GMP, old issue-price, merged date/year, fact_market_prices, removed financials, and merged source columns are never emitted
        master_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(ipo_merged_master)").fetchall()
        }
        assert not (
            master_cols
            & {
                "median_gmp",
                "min_gmp",
                "max_gmp",
                "GMP__hf_gmp_sme",
                "final_gmp",
                "slug",
                "predicted_pct",
                "Issue_Price",
                "issue_price__fact_gmp_history",
                "listing_year",
                "listing_date",
                "IPO_Month",
                "IPO_Year",
                "IPO_Name",
                "Status",
                "Price_vix_daily",
                "yf_resolved_ticker",
                "yf_listing_close",
                "yf_latest_close",
                "yf_market_cap",
                "yf_total_debt",
                "trailingPE",
                "rhp_latest_fy",
                "Subscription_QIB",
                "Subscription_HNI",
                "Subscription_RII",
                "Subscription_Total",
                "day_1_qib",
                "day_10_nii",
                "normalized_name",
                "name__fact_gmp_history",
                "listingDate",
                "scraped_page_no",
                "Review Title",
                "summary_of_all_answers",
                "Issue_Size",
                "~id",
                "id",
                "Listing Price",
                "listingPrice",
                "Symbol",
                "NSE_symbol",
                "nse_ticker",
                "Date ",
                "Price Current Gains(%)",
                "Price Current Gains(%)__fact_subscription_listing",
                "gmp",
                "gmpPercent",
                "Price_nifty50_daily",
                "BSE_Open",
                "exchange",
                "beta",
                "recommendationMean",
                "listing_price",
            }
        )
        assert {"canonical_name", "issue_size_cr", "GMP_Day3_Clean"} <= master_cols
