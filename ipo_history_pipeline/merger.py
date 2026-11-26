"""Name normalization, deterministic ipo_id FK linking, multi-source merging, and SQLite warehouse."""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import json5
import pandas as pd

from ipo_history_pipeline import config
from ipo_history_pipeline.enrichment import (
    RHP_FINANCIALS_CACHE_PATH,
    YFINANCE_CACHE_PATH,
    TickerEnrichmentTarget,
)

logger = logging.getLogger(__name__)

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(limited|ltd|private|pvt|india|corp|corporation|inc|incorporated|llc|plc|co|company|sme)\b",
    flags=re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_SPACE_RE = re.compile(r"\s+")
_YEAR_4_RE = re.compile(r"\b(199\d|20[0-3]\d)\b")
_DATE_DDMMYY_RE = re.compile(r"\b\d{1,2}/(\d{1,2})/(\d{2})\b")
_ISO_DATE_RE = re.compile(r"\b(?:199\d|20[0-3]\d)-(\d{1,2})-\d{1,2}\b")
_MONTH_MAP: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_WORD_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    flags=re.IGNORECASE,
)


def extract_month(value: Any) -> Optional[int]:
    """Extract numeric calendar month (1-12) from month or date strings without guessing."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "nat"}:
        return None

    if re.fullmatch(r"(\d{1,2})(?:\.0+)?", text):
        m = int(float(text))
        return m if 1 <= m <= 12 else None

    m_word = _MONTH_WORD_RE.search(text)
    if m_word:
        return _MONTH_MAP.get(m_word.group(1).lower())

    m_iso = _ISO_DATE_RE.search(text)
    if m_iso:
        m = int(m_iso.group(1))
        return m if 1 <= m <= 12 else None

    m_slash = _DATE_DDMMYY_RE.search(text)
    if m_slash:
        m = int(m_slash.group(1))
        return m if 1 <= m <= 12 else None

    return None

EXCLUDED_GLOBAL_COLUMNS_LOWER: frozenset[str] = frozenset(
    {
        "median_gmp",
        "min_gmp",
        "max_gmp",
        "gmp__hf_gmp_sme",
        "issue_price",
        "issue price",
        "issueprice",
        "issue_price_clean",
        "final_issue_price",
        "bse_final_issue_price",
        "nse_final_issue_price",
        "issue details offer price (₹)",
        "price(₹) offer price(₹)",
        "ipo price",
        "ipo issue price",
    }
)
EXCLUDED_SOURCE_COLUMNS: dict[str, frozenset[str]] = {
    "hf_gmp_sme": frozenset({"GMP"}),
}

# Exact warehouse/master column names requested for removal (including merged source date/year cols)
EXCLUDED_FINAL_COLUMNS: frozenset[str] = frozenset(
    {
        # Explicit named removals
        "IPO_Name",
        "Status",
        "Free_Float_Pct",
        "ipo_name_raw",
        "Promoter_Holding_Anomaly_Flag",
        "Is_Financial_Sector",
        "Issue Details IPO Name",
        "Price(₹) Current Gains(%)",
        "company_name_full",
        "ipo_name_for_lookup",
        "ipo_name_lookup_variants",
        "primary_ticker",
        "bse_ticker",
        "gemini_confidence",
        "gemini_canonical_name",
        "market_phase",
        "Listing_Gain_Pct",
        "Listing_Price_Clean",
        "Listing_Market_Cap",
        "ticker_source",
        "ticker_source_query",
        "name_recovery_method",
        "name_recovery_score",
        # Merged date/month/year source columns replaced by ipo_month & ipo_year
        "listing_year",
        "listing_date",
        "IPO_Month",
        "IPO_Year",
        # Range 124..132
        "NSE_Open",
        "NSE_Low",
        "NSE_High",
        "BSE_Last_Trade",
        "NSE_Last_Trade",
        "elasticity",
        "Open Date",
        "Close Date",
        "IPO Date",
        # Range 173..187 (VIX)
        "Price_vix_daily",
        "Open_vix_daily",
        "High_vix_daily",
        "Low_vix_daily",
        "Chg%_vix_daily",
        "Price_vix_weekly",
        "Open_vix_weekly",
        "High_vix_weekly",
        "Low_vix_weekly",
        "Chg%_vix_weekly",
        "Price_vix_monthly",
        "Open_vix_monthly",
        "High_vix_monthly",
        "Low_vix_monthly",
        "Chg%_vix_monthly",
        # Range 222..246
        *[f"answer_of_question_{i}" for i in range(1, 26)],
        # Range 254..271
        "most_relevant_link__hf_rev_mb",
        "Text_extracted_JSON__hf_rev_mb",
        "answer_of_question_0",
        *[f"answer_of_question_{i}__hf_rev_mb" for i in range(1, 16)],
        # Range 245..285
        "day_1_emp",
        "day_2_emp",
        "day_3_emp",
        "day_4_emp",
        "day_6_other",
        "day_7_date",
        "day_7_retail",
        "day_7_total",
        "day_7_other",
        "day_8_date",
        "day_8_retail",
        "day_8_total",
        "day_8_other",
        "day_9_date",
        "day_9_retail",
        "day_9_total",
        "day_9_other",
        "day_10_date",
        "day_10_retail",
        "day_10_total",
        "day_10_other",
        "day_11_date",
        "day_11_retail",
        "day_11_total",
        "day_11_other",
        "4_assets",
        "4_profit after tax",
        "5_assets",
        "5_profit after tax",
        "6_assets",
        "6_profit after tax",
        "4_net worth",
        "0_total income",
        "1_total income",
        "2_total income",
        "3_total income",
        "4_total income",
        "5_total income",
        "earningsAverage",
        "earningsLow",
        "earningsHigh",
        # Range 383..391
        "S-HNI (Min)_Lots",
        "S-HNI (Min)_Shares",
        "S-HNI (Min)_Amount",
        "S-HNI (Max)_Lots",
        "S-HNI (Max)_Shares",
        "S-HNI (Max)_Amount",
        "B-HNI (Min)_Lots",
        "B-HNI (Min)_Shares",
        "B-HNI (Min)_Amount",
        # Range 648..664
        "0_revenue",
        "1_revenue",
        "2_revenue",
        "3_revenue",
        "4_revenue",
        "5_revenue",
        "6_revenue",
        "0_total borrowing",
        "1_total borrowing",
        "2_total borrowing",
        "3_total borrowing",
        "4_total borrowing",
        "0_reserves and surplus",
        "1_reserves and surplus",
        "2_reserves and surplus",
        "3_reserves and surplus",
        "4_reserves and surplus",
        # Range 567..579 & 580..611 (fact_financials_debt trailing/yf/t1-t3/rhp cols)
        "yf_market_cap",
        "yf_trailing_pe",
        "yf_forward_pe",
        "yf_price_to_book",
        "yf_total_debt",
        "yf_total_cash",
        "yf_operating_cashflow",
        "yf_debt_to_equity",
        "yf_revenue_growth",
        "yf_earnings_growth",
        "yf_return_on_equity",
        "yf_sector",
        "yf_industry",
        "trailingPE",
        "forwardPE",
        "revenueGrowth",
        "debtToEquity",
        "forwardEps",
        "revenue_t1_cr",
        "pat_t1_cr",
        "pat_margin_t1_pct",
        "operatingCashflow",
        "revenue_t2_cr",
        "pat_t2_cr",
        "pat_margin_t2_pct",
        "revenue_growth_t1_pct",
        "revenue_t3_cr",
        "pat_t3_cr",
        "pat_margin_t3_pct",
        "yf_free_cashflow",
        "revenue_growth_t2_pct",
        "rhp_pre_ipo_total_debt_cr",
        "rhp_pre_ipo_net_worth_cr",
        "rhp_debt_repayment_from_ipo_cr",
        "rhp_latest_fy",
        "rhp_latest_revenue_cr",
        "rhp_latest_pat_cr",
        "rhp_latest_ebitda_cr",
        "rhp_latest_total_debt_cr",
        "rhp_latest_long_term_borrowings_cr",
        "rhp_latest_short_term_borrowings_cr",
        "rhp_latest_net_worth_cr",
        "rhp_latest_ocf_cr",
        "rhp_latest_fcf_cr",
        "rhp_latest_debt_to_equity",
        "rhp_fiscal_years_json",
        # Explicit fact_subscription_listing removals (258..267, 273..276, 342..347) & <15 non-null
        "Listing Open",
        "Listing Close",
        "Listing Gain Percent",
        "Subscription (No of times) Issue Size (₹.Cr)",
        "Price(₹) Listing Gains(%)",
        "Price(₹) CMP (BSE)",
        "Price(₹) CMP (NSE)",
        "day_3_bNII (bids above 10L)",
        # Merged QIB -> QIB_sum source columns
        "Subscription_QIB_Clean",
        "Subscription (No of times) QIB",
        "Subscription_QIB",
        "Subscription_QIB__kimk",
        "subscription_qib__ipodesk",
        *[f"day_{i}_qib" for i in range(1, 7)],
        # Merged Retail -> retail_sum source columns
        "Subscription_Retail_Clean",
        "Subscription (No of times) RII",
        "Subscription_RII",
        "Subscription_RII__kimk",
        "subscription_retail",
        *[f"day_{i}_retail" for i in range(1, 7)],
        # Merged NII/HNI -> nii_sum source columns
        "Subscription_NII_Clean",
        "Subscription (No of times) NII",
        "Subscription_HNI",
        "Subscription_HNI__kimk",
        "subscription_nii",
        *[f"day_{i}_nii" for i in range(1, 11)],
        *[f"day_{i}_nii*" for i in range(1, 7)],
        # Merged Total Subscription -> total_sub source columns
        "Subscription_Day3_Clean",
        "Subscription (No of times) Total",
        "Subscription_Total",
        "Total_Subscription",
        "Total_subscriptions",
        "Sub",
        "subscription_total__ipodesk",
        *[f"day_{i}_total" for i in range(1, 7)],
        # Range 150..168 (dim_ipos)
        "Retail (Min)_Lots",
        "Retail (Min)_Shares",
        "Retail (Min)_Amount",
        "Retail (Max)_Lots",
        "Retail (Max)_Shares",
        "Retail (Max)_Amount",
        "Share Holding Pre Issue",
        "0_assets",
        "0_profit after tax",
        "0_net worth",
        "1_assets",
        "1_profit after tax",
        "1_net worth",
        "2_assets",
        "2_profit after tax",
        "2_net worth",
        "3_assets",
        "3_profit after tax",
        "3_net worth",
        # Range 189..196 (dim_ipos day_1..day_4 date & other)
        "day_1_date",
        "day_1_other",
        "day_2_date",
        "day_2_other",
        "day_3_date",
        "day_3_other",
        "day_4_date",
        "day_4_other",
        # Range 214..234 (dim_ipos hf_rev_sme columns)
        "most_relevant_link__hf_rev_sme",
        "Text_extracted_JSON__hf_rev_sme",
        *[f"answer_of_question_{i}__hf_rev_sme" for i in range(0, 16)],
        "summary_of_all_answers__hf_rev_sme",
        "Recommendation__hf_rev_sme",
        "split__hf_rev_sme",
        # Explicit review, link, author, recommendation, split, scraped page, listing date removals
        "summary_of_all_answers",
        "ipo_link",
        "ipo_link__hf_rev_sme",
        "Author",
        "Author__hf_rev_sme",
        "review_link",
        "review_link__hf_rev_sme",
        "review_text",
        "review_text__hf_rev_sme",
        "Review Title",
        "Review Title__hf_rev_sme",
        "Recommendation",
        "split",
        "scraped_page_no",
        "scraped_page_no__aman_cm_sme",
        "Issue Details List Date",
        "Listing Date",
        "Issue Details Listing Date",
        "listingDate",
        # Name / Company columns (keep canonical_name only)
        "normalized_name",
        "shortName",
        "longName",
        "Issuer Company",
        "Company Name",
        "name",
        "Issue Details IPO Name__aman_cm_sme",
        "IPOName",
        # Merged year & month source columns (replaced by ipo_year & ipo_month)
        "year",
        "year__hf_rev_mb",
        "year__hf_rev_sme",
        "Close Year",
        "listing_month",
        # Merged IDs (keep ipo_id only)
        "~id",
        "id",
        # Merged Lot Size -> lot_size
        "Lot Size",
        "Issue Details Lot Size",
        "lotSize",
        # Merged Issue Type -> issue_type
        "Issue Type",
        # Merged Issue Size -> issue_size_cr
        "Issue_Size",
        "Issue Size",
        "Issue_Size_Norm",
        "Issue_Size_Original",
        "issueSizeCr",
        "Issue Details Issue Size (₹.Cr)",
        "IPO Size",
        "Total Issue Size",
        "Issue Size (Rs Cr.)",
        # Merged Key -> key
        "mapping_key",
        "key__hf_rev_sme",
        # Merged Listing Price -> listing_price
        "Listing Price",
        "Price(₹) List Price",
        "listingPrice",
        # Merged Symbol -> symbol
        "Symbol",
        "NSE_symbol",
        "nse_ticker",
        # Merged Date -> Date, removed all Price Current Gains(%), ranges 70..74 & 104..140, and gmp/gmpPercent
        "Date ",
        "Price Current Gains(%)",
        "1 day before Close Day",
        "1 week before Close Day week number",
        "1 week before Close Day year number",
        "1 month before Close Day month number",
        "1 month before Close Day year number",
        "Price_nifty50_daily",
        "Open_nifty50_daily",
        "High_nifty50_daily",
        "Low_nifty50_daily",
        "Volume_nifty50_daily",
        "Chg%_nifty50_daily",
        "Price_nifty50_weekly",
        "Open_nifty50_weekly",
        "High_nifty50_weekly",
        "Low_nifty50_weekly",
        "Volume_nifty50_weekly",
        "Chg%_nifty50_weekly",
        "Price_nifty50_monthly",
        "Open_nifty50_monthly",
        "High_nifty50_monthly",
        "Low_nifty50_monthly",
        "Volume_nifty50_monthly",
        "Chg%_nifty50_monthly",
        "previous_quarter_success_open",
        "previous_quarter_success_close",
        "previous_quarter_success_high",
        "dynamic_last_90Day_success_open",
        "dynamic_last_90Day_success_close",
        "dynamic_last_90Day_success_high",
        "Brokers_Neutral",
        "Brokers_Avoid",
        "Members_Neutral",
        "Members_Avoid",
        "Face Value per share",
        "Open",
        "Low",
        "High",
        "Last_Trade",
        "BSE_Open",
        "BSE_Low",
        "BSE_High",
        "gmp",
        "gmpPercent",
        # Removed upstream Yahoo Finance profile/analyst columns (31 cols)
        "exchange",
        "quoteType",
        "currency",
        "website",
        "country",
        "fullTimeEmployees",
        "firstTradeDate",
        "earningsGrowth",
        "numberOfAnalystOpinions",
        "targetHighPrice",
        "targetLowPrice",
        "targetMeanPrice",
        "targetMedianPrice",
        "heldPercentInsiders",
        "heldPercentInstitutions",
        "floatShares",
        "sharesOutstanding",
        "impliedSharesOutstanding",
        "institutionsCount",
        "insidersPercentHeld",
        "institutionsPercentHeld",
        "institutionsFloatPercentHeld",
        "lastFiscalYearEnd",
        "mostRecentQuarter",
        "nextFiscalYearEnd",
        "returnOnEquity",
        "returnOnAssets",
        "beta",
        "exDividendDate",
        "recommendationMean",
        "recommendationKey",
        # Drop listing_price after using it to backfill GMP_Day3_Clean (%)
        "listing_price",
        # Top 1 to 63 sparse/null columns removed as requested
        "day_6_date",
        "day_5_other",
        "day_5_date",
        "board",
        "listingGainPercent",
        "news_synopsis",
        "news_url",
        "news_headline",
        "news_content",
        "Listing_Gains_Percent",
        "Subscription Dates",
        "Offer for Sale",
        "fy_3_year",
        "Price CMP(BSE)",
        "fy_2_year",
        "Price Band",
        "Price CMP(NSE)",
        "HNI (Min)_Lots",
        "HNI (Min)_Shares",
        "HNI (Min)_Amount",
        "fy_1_year",
        "Date",
        "Fresh Issue",
        "Share Holding Post Issue",
        "Price Listing Close",
        "Price Listing Open",
        "Price Listing Gains(%)",
        "Basis of Allotment",
        "Initiation of Refunds",
        "Credit of Shares to Demat",
        "Cut-off time for UPI mandate confirmation",
        "GDP (current US$)",
        "Personal remittances, received (% of GDP)",
        "Inflation, consumer prices (annual %)",
        "GNI (current US$)",
        "Foreign direct investment, net inflows (BoP, current US$)",
        "Foreign direct investment, net (BoP, current US$)",
        "Stocks traded, total value (% of GDP)",
        "Success_Open",
        "Success_Close",
        "Success_High",
        "dhrp_rhp_links",
        "url",
        "Members_Subscribe",
        "Brokers_Subscribe",
        "subscription_link",
        "GNI per capita growth (annual %)",
        "key",
        "Unemployment, total (% of total labor force) (modeled ILO estimate)",
        "GDP per capita growth (annual %)",
        "GDP growth (annual %)",
        "previous_quarter",
        "Close Year Previous",
        "Text_extracted_JSON",
        "Net trade in goods and services (BoP, current US$)",
        "File_Rename_1st",
        "most_relevant_link",
        "dhrp_rhp_links_pdf",
        "full_text_content",
        "gemini_error",
        "manual_review_required",
        "lot_size",
        "Tier1_Anchor_Present",
        # Post-listing valuation & secondary performance metrics
        "marketCap",
        "enterpriseValue",
        "priceToBook",
        "priceToSalesTrailing12Months",
        "enterpriseToRevenue",
        "enterpriseToEbitda",
        "marketCapToSales",
        "PE_Ratio",
        "PB_Ratio",
        "Past_1_Month_Index_Return",
        "avg_listing_gain_last_30d",
        "avg_listing_gain_last_90d",
    }
)

# Per-table final exclusions for column names shared with other tables (keep dim_ipos intact)
EXCLUDED_TABLE_FINAL_COLUMNS: dict[str, frozenset[str]] = {
    "fact_subscription_listing": frozenset(
        {"scraped_page_no", "Issue Details List Date", "sector", "symbol"}
    ),
}

# Priority order for coalescing unified subscription columns (summary cols first, then latest cumulative day)
SUBSCRIPTION_MERGE_PRIORITY: dict[str, tuple[str, ...]] = {
    "QIB_sum": (
        "Subscription_QIB_Clean",
        "Subscription (No of times) QIB",
        "Subscription_QIB",
        "Subscription_QIB__kimk",
        "subscription_qib__ipodesk",
        "day_6_qib",
        "day_5_qib",
        "day_4_qib",
        "day_3_qib",
        "day_2_qib",
        "day_1_qib",
    ),
    "retail_sum": (
        "Subscription_Retail_Clean",
        "Subscription (No of times) RII",
        "Subscription_RII",
        "Subscription_RII__kimk",
        "subscription_retail",
        "day_6_retail",
        "day_5_retail",
        "day_4_retail",
        "day_3_retail",
        "day_2_retail",
        "day_1_retail",
    ),
    "nii_sum": (
        "Subscription_NII_Clean",
        "Subscription (No of times) NII",
        "Subscription_HNI",
        "Subscription_HNI__kimk",
        "subscription_nii",
        "day_10_nii",
        "day_9_nii",
        "day_8_nii",
        "day_7_nii",
        "day_6_nii",
        "day_6_nii*",
        "day_5_nii",
        "day_5_nii*",
        "day_4_nii",
        "day_4_nii*",
        "day_3_nii",
        "day_3_nii*",
        "day_2_nii",
        "day_2_nii*",
        "day_1_nii",
        "day_1_nii*",
    ),
    "total_sub": (
        "Subscription_Day3_Clean",
        "Subscription (No of times) Total",
        "Subscription_Total",
        "Total_Subscription",
        "Total_subscriptions",
        "Sub",
        "subscription_total__ipodesk",
        "day_6_total",
        "day_5_total",
        "day_4_total",
        "day_3_total",
        "day_2_total",
        "day_1_total",
    ),
}

ISSUE_BASE_PRICE_PRIORITY: tuple[tuple[str, Optional[str]], ...] = (
    ("Issue_Price_Clean", None),
    ("Final_Issue_Price", None),
    ("NSE_Final_Issue_Price", None),
    ("Issue Details Offer Price (₹)", None),
    ("Price(₹) Offer Price(₹)", None),
    ("Issue_Price", None),
    ("IPO Issue Price", "hf_gmp_mb"),
    ("issuePrice", None),
    ("IPO Price", "enriched"),
)


def _extract_issue_base_price(row: dict[str, Any], source_tag: str, domain_group: str) -> Optional[float]:
    """Extract numeric issue_base_price from the 9 approved issue-price columns."""
    for col_name, scope in ISSUE_BASE_PRICE_PRIORITY:
        if scope == "enriched" and domain_group != "enriched":
            continue
        if scope == "hf_gmp_mb" and source_tag != "hf_gmp_mb":
            continue
        val = _extract_numeric(row.get(col_name))
        if val is not None:
            return val
    return None


def _is_excluded_column(source_tag: str, col_name: str) -> bool:
    if col_name == "Unnamed: 0":
        return True
    if col_name.lower() in EXCLUDED_GLOBAL_COLUMNS_LOWER:
        return True
    return col_name in EXCLUDED_SOURCE_COLUMNS.get(source_tag, frozenset())


def normalize_company_name(name: Optional[str]) -> str:
    """Normalize Indian company name deterministically for cross-source matching."""
    if name is None:
        return ""
    text = str(name).strip().lower()
    if not text or text in {"nan", "none", "null"}:
        return ""
    text = text.replace("&", " and ")
    text = _PUNCT_RE.sub(" ", text)
    text = _LEGAL_SUFFIX_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text


def extract_year(value: Any) -> Optional[int]:
    """Extract a 4-digit listing/issue year from date or year fields without guessing."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "nat"}:
        return None

    # Direct integer/float year like 2024 or 2024.0
    if re.fullmatch(r"(199\d|20[0-3]\d)(?:\.0+)?", text):
        return int(float(text))

    m4 = _YEAR_4_RE.search(text)
    if m4:
        return int(m4.group(1))

    m2 = _DATE_DDMMYY_RE.search(text)
    if m2:
        yy = int(m2.group(2))
        return 1900 + yy if yy >= 90 else 2000 + yy

    return None


def make_ipo_id(norm_name: str, year: Optional[int]) -> str:
    """Construct deterministic ipo_id key from normalized name + year."""
    slug = norm_name.replace(" ", "_") if norm_name else "unknown_company"
    suffix = str(year) if year is not None else "unknown"
    return f"{slug}_{suffix}"


def _clean_cell(val: Any) -> Any:
    """Preserve missing values strictly as None (SQL NULL) and serialize nested containers."""
    if val is None:
        return None
    if isinstance(val, (list, dict, tuple, set)):
        if not val:
            return None
        return json.dumps(list(val) if isinstance(val, (tuple, set)) else val, ensure_ascii=False)
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass
    if isinstance(val, (pd.Timestamp, datetime, date)):
        return val.isoformat()
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    if isinstance(val, str):
        stripped = val.strip()
        if not stripped or stripped.lower() in {"nan", "none", "null", "nat", "-"}:
            return None
        return stripped
    return val


def _extract_numeric(val: Any) -> Optional[float]:
    cleaned = _clean_cell(val)
    if cleaned is None:
        return None
    if isinstance(cleaned, (int, float)):
        return float(cleaned)
    m = re.search(r"-?\d+(?:,\d+)*(?:\.\d+)?", str(cleaned))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


@dataclass
class SourceFrame:
    """Loaded source dataset ready for normalization and merging."""

    source_tag: str
    df: pd.DataFrame
    name_cols: tuple[str, ...]
    date_cols: tuple[str, ...]
    domain_group: str  # dim | subscription | gmp | financial | market


@dataclass
class MergeSummary:
    """Warehouse build report metrics."""

    table_row_counts: dict[str, int] = field(default_factory=dict)
    total_master_rows: int = 0
    total_master_columns: int = 0
    matched_multi_source_ipos: int = 0
    null_coverage_by_group: dict[str, dict[str, float]] = field(default_factory=dict)


def _safe_read_excel(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        return pd.read_excel(path)
    except Exception as exc:
        logger.warning("Failed reading Excel %s: %s", path, exc)
        return None


def _safe_read_csv(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    for enc in ("utf-8", "utf-8-sig", "latin1"):
        try:
            df = pd.read_csv(path, encoding=enc, low_memory=False)
            # Drop unnamed pandas index artifacts like 'Unnamed: 0' only if purely sequential index
            return df
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            logger.warning("Failed reading CSV %s: %s", path, exc)
            return None
    return None


def _load_spectrumz_json(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ipos = payload.get("ipos") if isinstance(payload, dict) else payload
        if isinstance(ipos, list) and ipos:
            return pd.json_normalize(ipos, sep="_")
    except Exception as exc:
        logger.warning("Failed parsing Spectrumz JSON %s: %s", path, exc)
    return None


def _load_ipodesk_ts(path: Path) -> Optional[pd.DataFrame]:
    """Parse static HISTORICAL_IPOS array from TypeScript file without executing JS."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        match = re.search(
            r"export\s+const\s+HISTORICAL_IPOS[^=]*=\s*(\[[\s\S]*\])\s*;",
            text,
        )
        if not match:
            return None
        arr_text = match.group(1)
        records = json5.loads(arr_text)
        if isinstance(records, list) and records:
            return pd.json_normalize(records, sep="_")
    except Exception as exc:
        logger.warning("Failed parsing IPO_Desk TS data %s: %s", path, exc)
    return None


def load_raw_sources(raw_dir: Path = config.RAW_DIR) -> list[SourceFrame]:
    """Load all downloaded repository datasets cleanly, preserving all original columns."""
    frames: list[SourceFrame] = []

    def add_csv(
        repo_slug: str,
        rel_path: str,
        tag: str,
        name_cols: tuple[str, ...],
        date_cols: tuple[str, ...],
        group: str,
    ) -> None:
        df = _safe_read_csv(raw_dir / repo_slug / rel_path)
        if df is not None and not df.empty:
            frames.append(SourceFrame(tag, df, name_cols, date_cols, group))

    # 1. Spectrumz00/india-ipo-gmp-data
    add_csv(
        "Spectrumz00__india-ipo-gmp-data",
        "data/accuracy.csv",
        "spec_acc",
        ("name", "slug"),
        ("listed_date",),
        "gmp",
    )
    df_spec_json = _load_spectrumz_json(
        raw_dir / "Spectrumz00__india-ipo-gmp-data" / "data/gmp-latest.json"
    )
    if df_spec_json is not None and not df_spec_json.empty:
        frames.append(
            SourceFrame(
                "spec_gmp",
                df_spec_json,
                ("name", "company_name", "ipo_name", "slug"),
                ("listed_date", "listing_date", "close_date", "open_date"),
                "gmp",
            )
        )

    # 2. saqibsafdar11/Predicting-Listing-Gains-in-the-Indian-IPO-Market
    add_csv(
        "saqibsafdar11__Predicting-Listing-Gains-in-the-Indian-IPO-Market",
        "Indian_IPO_Market_Data.csv",
        "saqib",
        ("IPOName",),
        ("Date ", "Date"),
        "subscription",
    )

    # 3. kimkmathews/IPO-PerformanceAnalysis
    add_csv(
        "kimkmathews__IPO-PerformanceAnalysis",
        "IPO_2024.csv",
        "kimk",
        ("IPO_Name",),
        ("Date",),
        "subscription",
    )

    # 4. amansingh2116/ipo_analysis
    aman_slug = "amansingh2116__ipo_analysis"
    add_csv(
        aman_slug,
        "capital_market_ipo_scrape/ipo_historical_data_all_pages.csv",
        "aman_cm_main",
        ("Issue Details IPO Name",),
        ("Issue Details Listing Date", "Issue Details List Date"),
        "subscription",
    )
    add_csv(
        aman_slug,
        "capital_market_sme_ipo_scrape/sme_ipo_historical_data_all_pages.csv",
        "aman_cm_sme",
        ("Issue Details IPO Name",),
        ("Issue Details Listing Date", "Issue Details List Date"),
        "subscription",
    )
    add_csv(
        aman_slug,
        "ipo_gmp_2019_2026_clean.csv",
        "aman_gmp",
        ("IPO",),
        ("Listing Date", "year"),
        "gmp",
    )
    for rel, tag in (
        ("ipo_enriched_outputs/ipo_enriched_analysis_ready.csv", "aman_enr_main"),
        ("ipo_gmp_enriched_outputs/gmp_ipo_enriched_analysis_ready.csv", "aman_enr_gmp"),
        ("sme_ipo_enriched_outputs/ipo_enriched_analysis_ready.csv", "aman_enr_sme"),
        ("ipo_enriched_outputs/needs_manual_review.csv", "aman_rev_main"),
        ("ipo_gmp_enriched_outputs/gmp_needs_manual_review.csv", "aman_rev_gmp"),
        ("sme_ipo_enriched_outputs/needs_manual_review.csv", "aman_rev_sme"),
        ("ipo_enriched_outputs/rejected_closed.csv", "aman_rej_main"),
        ("ipo_gmp_enriched_outputs/gmp_rejected_closed.csv", "aman_rej_gmp"),
        ("sme_ipo_enriched_outputs/rejected_closed.csv", "aman_rej_sme"),
        ("ipo_enriched_outputs/ticker_resolution_log.csv", "aman_tkr_main"),
        ("ipo_gmp_enriched_outputs/gmp_ticker_resolution_log.csv", "aman_tkr_gmp"),
        ("sme_ipo_enriched_outputs/ticker_resolution_log.csv", "aman_tkr_sme"),
    ):
        add_csv(
            aman_slug,
            rel,
            tag,
            (
                "company_name_full",
                "ipo_name_recovered",
                "gemini_canonical_name",
                "ipo_name_for_lookup",
                "Issue Details IPO Name",
                "IPO",
                "normalized_name",
            ),
            (
                "Issue Details Listing Date",
                "Issue Details List Date",
                "Listing Date",
                "firstTradeDate",
                "listing_year",
                "year",
            ),
            "enriched",
        )

    # 5. deba608/IPO_Desk
    df_ipodesk = _load_ipodesk_ts(
        raw_dir
        / "deba608__IPO_Desk"
        / "src/features/backtest/data/historical-ipos.ts"
    )
    if df_ipodesk is not None and not df_ipodesk.empty:
        frames.append(
            SourceFrame(
                "ipodesk",
                df_ipodesk,
                ("name",),
                ("listingDate",),
                "subscription",
            )
        )

    # 6. DivyGupta4/IPO-Quantitative-Analysis-and-Listing-Predictor (RHP financials + debt)
    add_csv(
        "DivyGupta4__IPO-Quantitative-Analysis-and-Listing-Predictor",
        "Cleaned_v6_with_rhp.csv",
        "divy_rhp",
        ("IPO_Name", "IPO_Name_Raw"),
        ("IPO_Year",),
        "enriched",
    )

    # 7. Hugging Face: sohomghosh/Indian_IPO_datasets & indian_ipo_rating_prediction
    def add_excel(
        repo_slug: str,
        rel_path: str,
        tag: str,
        name_cols: tuple[str, ...],
        date_cols: tuple[str, ...],
        group: str,
    ) -> None:
        df = _safe_read_excel(raw_dir / repo_slug / rel_path)
        if df is not None and not df.empty:
            frames.append(SourceFrame(tag, df, name_cols, date_cols, group))

    hf_ds1 = "hf__sohomghosh__Indian_IPO_datasets"
    add_excel(
        hf_ds1,
        "ipo_mainline_final_data_v18.xlsx",
        "hf_mb",
        ("Issuer Company", "Company Name"),
        ("Listing Date", "Close Date", "Open Date", "IPO Date", "Close Year"),
        "enriched",
    )
    add_excel(
        hf_ds1,
        "SME_data_final_v11.xlsx",
        "hf_sme",
        ("Issuer Company", "Company Name"),
        ("Listing Date", "Close Date", "IPO Date", "Close Year"),
        "enriched",
    )
    add_excel(
        hf_ds1,
        "GMP_data/GMP_Mainline_2019_2024YTD_Investorgrain_CLEAN.xlsx",
        "hf_gmp_mb",
        ("IPO",),
        ("Listing Date", "Year"),
        "gmp",
    )
    add_excel(
        hf_ds1,
        "GMP_data/GMP_SME_2019_2024YTD_Investorgrain_CLEAN_v2.xlsx",
        "hf_gmp_sme",
        ("IPO",),
        ("Listing Date", "Year"),
        "gmp",
    )

    hf_ds2 = "hf__sohomghosh__indian_ipo_rating_prediction"
    for rel, tag in (
        ("mb_ipo-reviews-final_data.xlsx", "hf_rev_mb"),
        ("sme_ipo-reviews-final_data.xlsx", "hf_rev_sme"),
    ):
        df_rev = _safe_read_excel(raw_dir / hf_ds2 / rel)
        if df_rev is not None and not df_rev.empty:
            frames.append(
                SourceFrame(
                    tag,
                    df_rev,
                    ("Review Title",),
                    ("year",),
                    "dim",
                )
            )

    return frames


def _pick_row_name_and_year(
    row: dict[str, Any], name_cols: tuple[str, ...], date_cols: tuple[str, ...]
) -> tuple[Optional[str], str, Optional[int], Optional[str]]:
    raw_name: Optional[str] = None
    best_norm = ""

    # Prefer non-truncated company names when available
    for col in name_cols:
        val = _clean_cell(row.get(col))
        if val is None:
            continue
        sval = str(val)
        if raw_name is None:
            raw_name = sval
        if "..." not in sval:
            norm = normalize_company_name(sval)
            if norm:
                raw_name = sval
                best_norm = norm
                break

    if not best_norm and raw_name:
        best_norm = normalize_company_name(raw_name)

    year: Optional[int] = None
    raw_date: Optional[str] = None
    for dcol in date_cols:
        dval = _clean_cell(row.get(dcol))
        if dval is None:
            continue
        if raw_date is None:
            raw_date = str(dval)
        yr = extract_year(dval)
        if yr is not None:
            year = yr
            break

    return raw_name, best_norm, year, raw_date


def _sql_safe_col_registry() -> dict[str, set[str]]:
    return {}


def _assign_sqlite_columns(
    table_name: str,
    candidate_cols: list[str],
    source_tag: str,
    used_lower: dict[str, set[str]],
    lineage: list[tuple[str, str, str, str]],
) -> dict[str, str]:
    """Map original column names to unique case-insensitive SQLite column names."""
    seen = used_lower.setdefault(table_name, set())
    mapping: dict[str, str] = {}
    for col in candidate_cols:
        col_clean = str(col)
        low = col_clean.lower()
        if low not in seen:
            seen.add(low)
            mapping[col_clean] = col_clean
        else:
            tagged = f"{col_clean}__{source_tag}"
            t_low = tagged.lower()
            idx = 2
            while t_low in seen:
                tagged = f"{col_clean}__{source_tag}_{idx}"
                t_low = tagged.lower()
                idx += 1
            seen.add(t_low)
            mapping[col_clean] = tagged
        lineage.append((source_tag, col_clean, table_name, mapping[col_clean]))
    return mapping


def _coalesce_row(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Fill missing (None) columns without overwriting existing non-null values."""
    for key, val in incoming.items():
        cleaned = _clean_cell(val)
        if cleaned is not None and target.get(key) is None:
            target[key] = cleaned


def _classify_enriched_column(col: str) -> str:
    """Route columns from multi-domain enriched CSVs to normalized fact/dim tables."""
    low = col.lower()
    if any(
        tok in low
        for tok in (
            "debt",
            "borrowing",
            "cash",
            "ebitda",
            "revenue",
            "pat",
            "pbt",
            "margin",
            "roe",
            "roce",
            "roa",
            "book",
            "bvps",
            "eps",
            "pe_ratio",
            "pb_ratio",
            "enterprise",
            "trailingpe",
            "forwardpe",
            "pricetobook",
            "pricetosales",
            "marketcap",
            "capex",
            "netincome",
            "capitalemployed",
            "total_assets",
            "total_liabilities",
            "share_capital",
            "reserves",
            "finance_cost",
            "depreciation",
            "is_profitable",
        )
    ):
        return "financial"
    if any(
        tok in low
        for tok in (
            "close_",
            "return_",
            "cmp",
            "current_close",
            "current_return",
            "max_drawdown",
            "highest_",
            "nifty_",
            "regularmarket",
            "fifty",
            "twohundred",
            "listing_ref_price",
            "listing open",
            "listing close",
            "listing day cls",
            "closing price",
        )
    ):
        return "market"
    if any(tok in low for tok in ("gmp", "estimated price", "predicted_pct", "actual_pct", "error_pct", "direction_ok")):
        return "gmp"
    if any(
        tok in low
        for tok in (
            "subscription",
            "sub",
            "qib",
            "nii",
            "hni",
            "rii",
            "issue size",
            "issue_size",
            "fresh_issue",
            "ofs_size",
            "ipo size",
            "offer price",
            "issue price",
            "listing gain",
            "list price",
            "anchor",
        )
    ):
        return "subscription"
    return "dim"


def extract_yfinance_targets_from_raw(raw_dir: Path = config.RAW_DIR) -> list[TickerEnrichmentTarget]:
    """Collect distinct IPO ticker targets from downloaded datasets for yfinance enrichment."""
    frames = load_raw_sources(raw_dir)
    # First pass: determine norm_name -> unique year map
    name_years: dict[str, set[int]] = {}
    parsed_rows: list[tuple[SourceFrame, dict[str, Any], Optional[str], str, Optional[int]]] = []

    for sf in frames:
        for row in sf.df.to_dict(orient="records"):
            raw_name, norm, yr, _ = _pick_row_name_and_year(row, sf.name_cols, sf.date_cols)
            if not norm:
                continue
            if yr is not None:
                name_years.setdefault(norm, set()).add(yr)
            parsed_rows.append((sf, row, raw_name, norm, yr))

    targets: dict[str, TickerEnrichmentTarget] = {}
    for sf, row, raw_name, norm, yr in parsed_rows:
        if yr is None and len(name_years.get(norm, set())) == 1:
            yr = next(iter(name_years[norm]))
        ipo_id = make_ipo_id(norm, yr)

        sym = _clean_cell(row.get("Symbol") or row.get("symbol") or row.get("primary_ticker"))
        nse = _clean_cell(row.get("nse_ticker"))
        bse = _clean_cell(row.get("bse_ticker"))
        issue_px = _extract_numeric(
            row.get("Issue Price")
            or row.get("Issue_Price")
            or row.get("issue_price")
            or row.get("IPO Price")
            or row.get("Price(₹) Offer Price(₹)")
            or row.get("issuePrice")
        )
        list_dt = _clean_cell(
            row.get("Listing Date")
            or row.get("listed_date")
            or row.get("Issue Details List Date")
            or row.get("Date")
            or row.get("listingDate")
        )

        if ipo_id not in targets:
            targets[ipo_id] = TickerEnrichmentTarget(
                ipo_id=ipo_id,
                company_name=raw_name or norm,
                symbol=str(sym) if sym else None,
                nse_ticker=str(nse) if nse else None,
                bse_ticker=str(bse) if bse else None,
                issue_price=issue_px,
                listing_date=str(list_dt) if list_dt else None,
            )
        else:
            existing = targets[ipo_id]
            existing.symbol = existing.symbol or (str(sym) if sym else None)
            existing.nse_ticker = existing.nse_ticker or (str(nse) if nse else None)
            existing.bse_ticker = existing.bse_ticker or (str(bse) if bse else None)
            existing.issue_price = existing.issue_price or issue_px
            existing.listing_date = existing.listing_date or (str(list_dt) if list_dt else None)

    # Prioritize rows that actually have tickers
    return sorted(
        targets.values(),
        key=lambda t: (0 if (t.nse_ticker or t.symbol or t.bse_ticker) else 1, t.ipo_id),
    )


def build_warehouse(
    raw_dir: Path = config.RAW_DIR,
    db_path: Path = config.WAREHOUSE_DB_PATH,
    yfinance_cache_path: Path = YFINANCE_CACHE_PATH,
    rhp_cache_path: Path = RHP_FINANCIALS_CACHE_PATH,
) -> MergeSummary:
    """Merge all raw datasets + enrichments by ipo_id into normalized SQLite warehouse."""
    config.ensure_pipeline_dirs()
    frames = load_raw_sources(raw_dir)

    # 1. Resolve normalized names & unique-year fallback across all sources
    name_years: dict[str, set[int]] = {}
    staged_records: list[tuple[SourceFrame, dict[str, Any], Optional[str], str, Optional[int], Optional[str]]] = []

    for sf in frames:
        for row in sf.df.to_dict(orient="records"):
            raw_name, norm, yr, raw_date = _pick_row_name_and_year(
                row, sf.name_cols, sf.date_cols
            )
            if not norm:
                continue
            if yr is not None:
                name_years.setdefault(norm, set()).add(yr)
            staged_records.append((sf, row, raw_name, norm, yr, raw_date))

    # 2. Build row dictionaries per normalized table, preserving EVERY column cleanly
    dim_rows: dict[str, dict[str, Any]] = {}
    sub_rows: dict[str, dict[str, Any]] = {}
    gmp_rows: dict[str, dict[str, Any]] = {}
    fin_rows: dict[str, dict[str, Any]] = {}
    mkt_rows: dict[str, dict[str, Any]] = {}
    sources_per_ipo: dict[str, set[str]] = {}

    used_lower: dict[str, set[str]] = {
        "dim_ipos": {"ipo_id", "normalized_name", "canonical_name", "ipo_year", "ipo_month", "sources_present"},
        "fact_subscription_listing": {
            "ipo_id",
            "issue_base_price",
            "qib_sum",
            "retail_sum",
            "nii_sum",
            "total_sub",
        },
        "fact_gmp_history": {"ipo_id"},
        "fact_financials_debt": {"ipo_id"},
        "fact_market_prices": {"ipo_id"},
    }
    lineage: list[tuple[str, str, str, str]] = []
    source_col_maps: dict[tuple[str, str], str] = {}

    # Pre-register exact column mappings per SourceFrame so columns are uniform across rows
    for sf in frames:
        cols = [
            str(c)
            for c in sf.df.columns
            if not _is_excluded_column(sf.source_tag, str(c))
        ]
        if sf.domain_group == "enriched":
            # Route columns cleanly across dim / fact tables by semantic role
            grouped: dict[str, list[str]] = {
                "dim_ipos": [],
                "fact_subscription_listing": [],
                "fact_gmp_history": [],
                "fact_financials_debt": [],
                "fact_market_prices": [],
            }
            group_map = {
                "dim": "dim_ipos",
                "subscription": "fact_subscription_listing",
                "gmp": "fact_gmp_history",
                "financial": "fact_financials_debt",
                "market": "fact_market_prices",
            }
            for col in cols:
                tbl = group_map[_classify_enriched_column(col)]
                grouped[tbl].append(col)
            for tbl, tcols in grouped.items():
                # Avoid re-creating duplicates across multiple amansingh enriched files;
                # only register unseen columns to the shared table schema while mapping existing ones!
                for col in tcols:
                    low = col.lower()
                    if low in used_lower[tbl]:
                        source_col_maps[(sf.source_tag, col)] = next(
                            (c for c in used_lower[tbl] if c == low), col
                        )
                        # Keep exact original casing if first seen; store canonical col name
                    else:
                        used_lower[tbl].add(low)
                        source_col_maps[(sf.source_tag, col)] = col
                        lineage.append((sf.source_tag, col, tbl, col))
        else:
            tbl = {
                "subscription": "fact_subscription_listing",
                "gmp": "fact_gmp_history",
                "financial": "fact_financials_debt",
                "market": "fact_market_prices",
            }.get(sf.domain_group, "dim_ipos")
            cmap = _assign_sqlite_columns(tbl, cols, sf.source_tag, used_lower, lineage)
            for orig, mapped in cmap.items():
                source_col_maps[(sf.source_tag, orig)] = mapped

    # Actually let's ensure exact case preservation for shared enriched columns
    exact_name_lookup: dict[tuple[str, str], str] = {}
    for _, _, tbl, wcol in lineage:
        exact_name_lookup[(tbl, wcol.lower())] = wcol

    table_dicts = {
        "dim_ipos": dim_rows,
        "fact_subscription_listing": sub_rows,
        "fact_gmp_history": gmp_rows,
        "fact_financials_debt": fin_rows,
        "fact_market_prices": mkt_rows,
    }

    for sf, row, raw_name, norm, yr, raw_date in staged_records:
        if yr is None and len(name_years.get(norm, set())) == 1:
            yr = next(iter(name_years[norm]))
        ipo_id = make_ipo_id(norm, yr)
        sources_per_ipo.setdefault(ipo_id, set()).add(sf.source_tag)

        # Track year & month from IPO_Year, IPO_Month, listing_year, listing_date
        row_ipo_year = extract_year(row.get("IPO_Year")) or yr
        row_ipo_month = extract_month(row.get("IPO_Month")) or extract_month(raw_date)

        if ipo_id not in dim_rows:
            dim_rows[ipo_id] = {
                "ipo_id": ipo_id,
                "normalized_name": norm,
                "canonical_name": raw_name,
                "ipo_year": row_ipo_year,
                "ipo_month": row_ipo_month,
                "sources_present": None,
            }
        else:
            if raw_name and (
                not dim_rows[ipo_id].get("canonical_name")
                or (
                    "..." in str(dim_rows[ipo_id]["canonical_name"])
                    and "..." not in raw_name
                )
            ):
                dim_rows[ipo_id]["canonical_name"] = raw_name
            if row_ipo_year is not None and dim_rows[ipo_id].get("ipo_year") is None:
                dim_rows[ipo_id]["ipo_year"] = row_ipo_year
            if row_ipo_month is not None and dim_rows[ipo_id].get("ipo_month") is None:
                dim_rows[ipo_id]["ipo_month"] = row_ipo_month

        # Populate merged numeric issue_base_price from the 9 approved columns
        base_px = _extract_issue_base_price(row, sf.source_tag, sf.domain_group)
        if base_px is not None:
            sub_rows.setdefault(ipo_id, {"ipo_id": ipo_id})
            if sub_rows[ipo_id].get("issue_base_price") is None:
                sub_rows[ipo_id]["issue_base_price"] = base_px

        # Populate target tables
        if sf.domain_group == "enriched":
            group_map = {
                "dim": "dim_ipos",
                "subscription": "fact_subscription_listing",
                "gmp": "fact_gmp_history",
                "financial": "fact_financials_debt",
                "market": "fact_market_prices",
            }
            bucket: dict[str, dict[str, Any]] = {}
            for orig_col, val in row.items():
                col_str = str(orig_col)
                if _is_excluded_column(sf.source_tag, col_str):
                    continue
                tbl = group_map[_classify_enriched_column(col_str)]
                wcol = exact_name_lookup.get((tbl, col_str.lower()), col_str)
                bucket.setdefault(tbl, {"ipo_id": ipo_id})[wcol] = _clean_cell(val)
            for tbl, payload in bucket.items():
                target_tbl = table_dicts[tbl]
                if ipo_id not in target_tbl:
                    target_tbl[ipo_id] = {"ipo_id": ipo_id}
                _coalesce_row(target_tbl[ipo_id], payload)
        else:
            tbl = {
                "subscription": "fact_subscription_listing",
                "gmp": "fact_gmp_history",
                "financial": "fact_financials_debt",
                "market": "fact_market_prices",
            }.get(sf.domain_group, "dim_ipos")
            target_tbl = table_dicts[tbl]
            if ipo_id not in target_tbl:
                target_tbl[ipo_id] = {"ipo_id": ipo_id}
            payload = {"ipo_id": ipo_id}
            for orig_col, val in row.items():
                col_str = str(orig_col)
                if _is_excluded_column(sf.source_tag, col_str):
                    continue
                wcol = source_col_maps.get((sf.source_tag, col_str), col_str)
                payload[wcol] = _clean_cell(val)
            _coalesce_row(target_tbl[ipo_id], payload)

    # 3. Merge yfinance enrichment cache into fact_market_prices & fact_financials_debt
    if yfinance_cache_path.exists():
        try:
            yf_cache: dict[str, dict[str, Any]] = json.loads(
                yfinance_cache_path.read_text(encoding="utf-8")
            )
            for ipo_id, ydata in yf_cache.items():
                if ipo_id not in dim_rows or ydata.get("status") != "ok":
                    continue
                sources_per_ipo.setdefault(ipo_id, set()).add("yfinance")
                mkt_payload = {
                    "ipo_id": ipo_id,
                    "yf_resolved_ticker": ydata.get("resolved_ticker"),
                    "yf_listing_trade_date": ydata.get("listing_trade_date"),
                    "yf_latest_trade_date": ydata.get("latest_trade_date"),
                    "yf_listing_open": ydata.get("listing_open"),
                    "yf_listing_high": ydata.get("listing_high"),
                    "yf_listing_low": ydata.get("listing_low"),
                    "yf_listing_close": ydata.get("listing_close"),
                    "yf_listing_volume": ydata.get("listing_volume"),
                    "yf_latest_close": ydata.get("latest_close"),
                    "yf_listing_return_pct": ydata.get("listing_return_pct"),
                    "yf_current_return_pct": ydata.get("current_return_pct"),
                    "yf_ohlcv_days": ydata.get("ohlcv_days"),
                    "yf_ohlcv_sample_json": ydata.get("ohlcv_sample_json"),
                }
                mkt_rows.setdefault(ipo_id, {"ipo_id": ipo_id})
                _coalesce_row(mkt_rows[ipo_id], mkt_payload)

                fin_payload = {
                    "ipo_id": ipo_id,
                    "yf_market_cap": ydata.get("market_cap"),
                    "yf_trailing_pe": ydata.get("trailing_pe"),
                    "yf_forward_pe": ydata.get("forward_pe"),
                    "yf_price_to_book": ydata.get("price_to_book"),
                    "yf_total_debt": ydata.get("total_debt"),
                    "yf_total_cash": ydata.get("total_cash"),
                    "yf_operating_cashflow": ydata.get("operating_cashflow"),
                    "yf_free_cashflow": ydata.get("free_cashflow"),
                    "yf_debt_to_equity": ydata.get("debt_to_equity"),
                    "yf_revenue_growth": ydata.get("revenue_growth"),
                    "yf_earnings_growth": ydata.get("earnings_growth"),
                    "yf_return_on_equity": ydata.get("return_on_equity"),
                    "yf_sector": ydata.get("sector"),
                    "yf_industry": ydata.get("industry"),
                }
                fin_rows.setdefault(ipo_id, {"ipo_id": ipo_id})
                _coalesce_row(fin_rows[ipo_id], fin_payload)
        except Exception as exc:
            logger.warning("Failed merging yfinance cache: %s", exc)

    # 4. Merge Gemini RHP debt/financials cache into fact_financials_debt
    if rhp_cache_path.exists():
        try:
            rhp_cache: dict[str, dict[str, Any]] = json.loads(
                rhp_cache_path.read_text(encoding="utf-8")
            )
            for _, rdata in rhp_cache.items():
                norm = normalize_company_name(rdata.get("company_name"))
                if not norm:
                    continue
                yrs = name_years.get(norm, set())
                yr = next(iter(yrs)) if len(yrs) == 1 else None
                ipo_id = make_ipo_id(norm, yr)
                if ipo_id not in dim_rows:
                    dim_rows[ipo_id] = {
                        "ipo_id": ipo_id,
                        "normalized_name": norm,
                        "canonical_name": rdata.get("company_name"),
                        "ipo_year": yr,
                        "ipo_month": None,
                        "sources_present": "rhp_gemini",
                    }
                sources_per_ipo.setdefault(ipo_id, set()).add("rhp_gemini")
                fys = rdata.get("fiscal_years") or []
                latest_fy = fys[-1] if fys else {}
                fin_payload = {
                    "ipo_id": ipo_id,
                    "rhp_pre_ipo_total_debt_cr": rdata.get("pre_ipo_total_debt_cr"),
                    "rhp_pre_ipo_net_worth_cr": rdata.get("pre_ipo_net_worth_cr"),
                    "rhp_debt_repayment_from_ipo_cr": rdata.get("debt_repayment_from_ipo_cr"),
                    "rhp_latest_fy": latest_fy.get("fiscal_year"),
                    "rhp_latest_revenue_cr": latest_fy.get("revenue_cr"),
                    "rhp_latest_pat_cr": latest_fy.get("pat_cr"),
                    "rhp_latest_ebitda_cr": latest_fy.get("ebitda_cr"),
                    "rhp_latest_total_debt_cr": latest_fy.get("total_debt_cr"),
                    "rhp_latest_long_term_borrowings_cr": latest_fy.get(
                        "long_term_borrowings_cr"
                    ),
                    "rhp_latest_short_term_borrowings_cr": latest_fy.get(
                        "short_term_borrowings_cr"
                    ),
                    "rhp_latest_net_worth_cr": latest_fy.get("net_worth_cr"),
                    "rhp_latest_ocf_cr": latest_fy.get("operating_cash_flow_cr"),
                    "rhp_latest_fcf_cr": latest_fy.get("free_cash_flow_cr"),
                    "rhp_latest_debt_to_equity": latest_fy.get("debt_to_equity"),
                    "rhp_fiscal_years_json": json.dumps(fys) if fys else None,
                }
                fin_rows.setdefault(ipo_id, {"ipo_id": ipo_id})
                _coalesce_row(fin_rows[ipo_id], fin_payload)
        except Exception as exc:
            logger.warning("Failed merging RHP cache: %s", exc)

    # Finalize sources_present
    for ipo_id, drow in dim_rows.items():
        drow["sources_present"] = ",".join(sorted(sources_per_ipo.get(ipo_id, set())))

    # Also update rhp_gemini dim fallback row schema if present
    # (already handled below via column filtering)

    # Merge all remaining IPO year & month sources into ipo_year and ipo_month before dropping source cols
    year_source_cols = ("year", "year__hf_rev_mb", "year__hf_rev_sme", "Close Year")
    date_source_cols = (
        "Listing Date",
        "Issue Details Listing Date",
        "Issue Details List Date",
        "listingDate",
        "Date",
        "Date ",
        "firstTradeDate",
    )
    for ipo_id, drow in dim_rows.items():
        srow = sub_rows.get(ipo_id, {})
        if drow.get("ipo_year") is None:
            yr_val: Optional[int] = None
            for ycol in year_source_cols:
                yr_val = extract_year(drow.get(ycol) if drow.get(ycol) is not None else srow.get(ycol))
                if yr_val is not None:
                    break
            if yr_val is None:
                for dcol in date_source_cols:
                    yr_val = extract_year(drow.get(dcol) if drow.get(dcol) is not None else srow.get(dcol))
                    if yr_val is not None:
                        break
            if yr_val is not None:
                drow["ipo_year"] = yr_val

        if drow.get("ipo_month") is None:
            mo_val = extract_month(drow.get("listing_month"))
            if mo_val is None:
                for dcol in date_source_cols:
                    mo_val = extract_month(drow.get(dcol) if drow.get(dcol) is not None else srow.get(dcol))
                    if mo_val is not None:
                        break
            if mo_val is not None:
                drow["ipo_month"] = mo_val

    # Populate unified QIB_sum, retail_sum, nii_sum, total_sub across dim_rows & sub_rows before column removal
    for ipo_id in dim_rows:
        drow = dim_rows.get(ipo_id, {})
        srow = sub_rows.get(ipo_id, {})
        grow = gmp_rows.get(ipo_id, {})
        merged_vals: dict[str, Optional[float]] = {}
        for target_col, src_cols in SUBSCRIPTION_MERGE_PRIORITY.items():
            picked: Optional[float] = None
            for scol in src_cols:
                raw_val = srow.get(scol)
                if raw_val is None:
                    raw_val = drow.get(scol)
                picked = _extract_numeric(raw_val)
                if picked is not None:
                    break
            if picked is not None:
                merged_vals[target_col] = picked
        if merged_vals:
            sub_rows.setdefault(ipo_id, {"ipo_id": ipo_id})
            for k, v in merged_vals.items():
                sub_rows[ipo_id][k] = v

        # Merge unique Symbol -> symbol (dim_ipos)
        clean_sym: Optional[str] = None
        for raw_sym, strip_suffix in (
            (drow.get("Symbol"), False),
            (drow.get("NSE_symbol"), False),
            (srow.get("symbol"), False),
            (drow.get("nse_ticker"), True),
        ):
            s_val = _clean_cell(raw_sym)
            if s_val is None:
                continue
            sym_txt = str(s_val).strip().upper()
            if strip_suffix:
                sym_txt = re.sub(r"(?:-SM)?\.(?:NS|BO)$", "", sym_txt, flags=re.IGNORECASE)
            if sym_txt:
                clean_sym = sym_txt
                break
        if clean_sym is not None:
            drow["symbol"] = clean_sym

        # Merge Sector -> sector (dim_ipos)
        if _clean_cell(drow.get("sector")) is None:
            sec_val = _clean_cell(srow.get("sector"))
            if sec_val is not None:
                drow["sector"] = str(sec_val)

        # Merge Issue Type -> issue_type (dim_ipos)
        it_val = _clean_cell(drow.get("Issue Type")) or _clean_cell(drow.get("issue_type"))
        if it_val is not None:
            drow["issue_type"] = str(it_val)

        # Merge Key -> key (dim_ipos)
        key_val: Optional[float] = None
        for kcol in ("key", "key__hf_rev_sme", "mapping_key"):
            key_val = _extract_numeric(drow.get(kcol))
            if key_val is not None:
                break
        if key_val is not None:
            drow["key"] = key_val

        # Merge Lot Size -> lot_size (fact_subscription_listing)
        lot_val: Optional[float] = None
        for l_raw in (
            srow.get("Issue Details Lot Size"),
            drow.get("Issue Details Lot Size"),
            drow.get("Lot Size"),
            srow.get("lotSize"),
        ):
            lot_val = _extract_numeric(l_raw)
            if lot_val is not None and lot_val > 0:
                break
            lot_val = None
        if lot_val is not None:
            sub_rows.setdefault(ipo_id, {"ipo_id": ipo_id})["lot_size"] = lot_val

        # Merge Issue Size -> issue_size_cr in Rs Crores (fact_subscription_listing)
        size_cr: Optional[float] = None
        for scol, positive_only in (
            ("Issue Details Issue Size (₹.Cr)", True),
            ("Issue Size (Rs Cr.)", True),
            ("Issue Size", True),
            ("Issue_Size", True),
            ("issueSizeCr", True),
            ("IPO Size", True),
        ):
            v = _extract_numeric(srow.get(scol) if srow.get(scol) is not None else drow.get(scol))
            if v is not None and (not positive_only or v > 0):
                size_cr = v
                break
        if size_cr is None:
            tot_raw = _clean_cell(srow.get("Total Issue Size"))
            if tot_raw is not None:
                m_cr = re.search(r"([\d,.]+)\s*Cr", str(tot_raw), flags=re.IGNORECASE)
                if m_cr:
                    try:
                        v = float(m_cr.group(1).replace(",", ""))
                        if v > 0:
                            size_cr = v
                    except ValueError:
                        pass
        if size_cr is None:
            for rcol in ("Issue_Size_Original", "Issue_Size_Norm"):
                rv = _extract_numeric(srow.get(rcol))
                if rv is None or rv <= 0:
                    continue
                if 1e6 <= rv <= 3e11:
                    size_cr = round(rv / 1e7, 4)
                    break
                if rv <= 30000:
                    size_cr = rv
                    break
        if size_cr is not None:
            sub_rows.setdefault(ipo_id, {"ipo_id": ipo_id})["issue_size_cr"] = size_cr

        # Merge Listing Price -> listing_price (fact_subscription_listing)
        lp_val: Optional[float] = None
        for lp_raw in (
            srow.get("Price(₹) List Price"),
            drow.get("Listing Price"),
            grow.get("listing_price"),
            srow.get("listingPrice"),
        ):
            lp_val = _extract_numeric(lp_raw)
            if lp_val is not None and lp_val > 0:
                break
            lp_val = None
        if lp_val is not None:
            sub_rows.setdefault(ipo_id, {"ipo_id": ipo_id})["listing_price"] = lp_val

        # Merge Date_ (DD/MM/YY) and Date (YYYY-MM-DD) -> normalized Date in fact_subscription_listing
        raw_dt = _clean_cell(srow.get("Date")) or _clean_cell(srow.get("Date "))
        if raw_dt is not None:
            dt_str = str(raw_dt).strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt_str):
                iso_dt = dt_str
            else:
                try:
                    parsed_dt = pd.to_datetime(dt_str, dayfirst=True, errors="coerce")
                    iso_dt = parsed_dt.strftime("%Y-%m-%d") if pd.notna(parsed_dt) else dt_str
                except Exception:
                    iso_dt = dt_str
            sub_rows.setdefault(ipo_id, {"ipo_id": ipo_id})["Date"] = iso_dt

        # Compute unified GMP_Day3_Clean in %:
        # 1) Convert Rs GMP (GMP_Day3_Clean or gmp) via gmp_rs / issue_base_price * 100
        # 2) Fallback to gmpPercent if present
        # 3) Fallback to actual listing gain % = (listing_price - issue_base_price) / issue_base_price * 100
        gmp_rs = _extract_numeric(grow.get("GMP_Day3_Clean"))
        if gmp_rs is None:
            gmp_rs = _extract_numeric(srow.get("gmp"))
        gmp_pct_direct = _extract_numeric(srow.get("gmpPercent"))
        base_p = _extract_numeric(srow.get("issue_base_price"))
        lp_val = _extract_numeric(srow.get("listing_price"))

        gmp_pct_final: Optional[float] = None
        if gmp_rs is not None:
            if base_p is not None and base_p > 0:
                gmp_pct_final = round((gmp_rs / base_p) * 100.0, 2)
            elif gmp_pct_direct is not None:
                gmp_pct_final = round(gmp_pct_direct, 2)
        elif gmp_pct_direct is not None:
            gmp_pct_final = round(gmp_pct_direct, 2)
        elif base_p is not None and base_p > 0 and lp_val is not None:
            gmp_pct_final = round(((lp_val - base_p) / base_p) * 100.0, 2)

        if gmp_pct_final is not None:
            gmp_rows.setdefault(ipo_id, {"ipo_id": ipo_id})["GMP_Day3_Clean"] = gmp_pct_final
        elif ipo_id in gmp_rows:
            gmp_rows.pop(ipo_id, None)

    # Only keep explicitly retained GMP metric in fact_gmp_history (listing_price moved to fact_subscription_listing)
    fact_gmp_keep_cols: frozenset[str] = frozenset({"ipo_id", "GMP_Day3_Clean"})

    # 5. Convert each table dict to DataFrame ensuring missing values stay None (NULL)
    def to_df(
        rows_map: dict[str, dict[str, Any]],
        table_name: str = "",
        allowed_cols: Optional[frozenset[str]] = None,
    ) -> pd.DataFrame:
        if not rows_map:
            return pd.DataFrame(columns=["ipo_id"])
        df = pd.DataFrame(list(rows_map.values()))
        tbl_excl = EXCLUDED_TABLE_FINAL_COLUMNS.get(table_name, frozenset())
        # Drop any columns marked in EXCLUDED_FINAL_COLUMNS / EXCLUDED_TABLE_FINAL_COLUMNS (or outside table allow-list)
        keep_cols = [
            c
            for c in df.columns
            if c not in EXCLUDED_FINAL_COLUMNS
            and c not in tbl_excl
            and (allowed_cols is None or c in allowed_cols)
        ]
        # Ensure ipo_id (plus canonical merged cols when present) come first cleanly
        priority_first = [
            "ipo_id",
            "canonical_name",
            "ipo_year",
            "ipo_month",
            "sources_present",
            "symbol",
            "sector",
            "issue_type",
            "key",
            "issue_base_price",
            "listing_price",
            "issue_size_cr",
            "lot_size",
            "QIB_sum",
            "retail_sum",
            "nii_sum",
            "total_sub",
        ]
        head = [c for c in priority_first if c in keep_cols]
        tail = [c for c in keep_cols if c not in head]
        df = df[head + tail]
        return df.where(pd.notnull(df), None)

    lineage = [
        item
        for item in lineage
        if item[3] not in EXCLUDED_FINAL_COLUMNS
        and item[3] not in EXCLUDED_TABLE_FINAL_COLUMNS.get(item[2], frozenset())
        and item[2] != "fact_market_prices"
        and (item[2] != "fact_gmp_history" or item[3] in fact_gmp_keep_cols)
    ]

    df_dim = to_df(dim_rows, table_name="dim_ipos")
    df_sub = to_df(sub_rows, table_name="fact_subscription_listing")
    df_gmp = to_df(gmp_rows, table_name="fact_gmp_history", allowed_cols=fact_gmp_keep_cols)
    df_fin = to_df(fin_rows, table_name="fact_financials_debt")
    # Remove all columns under fact_market_prices (keep empty PK/FK stub only)
    df_mkt = pd.DataFrame(columns=["ipo_id"])

    # Filter to only keep IPOs that have a valid GMP value
    valid_gmp_ipo_ids: set[str] = set()
    if not df_gmp.empty and "GMP_Day3_Clean" in df_gmp.columns:
        valid_gmp_ipo_ids = set(df_gmp.loc[df_gmp["GMP_Day3_Clean"].notna(), "ipo_id"])

    df_dim = df_dim[df_dim["ipo_id"].isin(valid_gmp_ipo_ids)].reset_index(drop=True)
    df_sub = df_sub[df_sub["ipo_id"].isin(valid_gmp_ipo_ids)].reset_index(drop=True)
    df_gmp = df_gmp[df_gmp["ipo_id"].isin(valid_gmp_ipo_ids)].reset_index(drop=True)
    df_fin = df_fin[df_fin["ipo_id"].isin(valid_gmp_ipo_ids)].reset_index(drop=True)
    df_mkt = df_mkt[df_mkt["ipo_id"].isin(valid_gmp_ipo_ids)].reset_index(drop=True)

    # 6. Construct wide ipo_merged_master table preserving all non-key columns
    master = df_dim.copy()
    master_seen_lower = {c.lower() for c in master.columns}
    for tbl_name, df_part in (
        ("fact_subscription_listing", df_sub),
        ("fact_gmp_history", df_gmp),
        ("fact_financials_debt", df_fin),
        ("fact_market_prices", df_mkt),
    ):
        if df_part.empty or len(df_part.columns) <= 1:
            continue
        rename_map: dict[str, str] = {}
        for col in df_part.columns:
            if col == "ipo_id":
                continue
            low = col.lower()
            if low in master_seen_lower:
                new_col = f"{col}__{tbl_name}"
                while new_col.lower() in master_seen_lower:
                    new_col = f"{new_col}_1"
                rename_map[col] = new_col
                master_seen_lower.add(new_col.lower())
            else:
                master_seen_lower.add(low)
        part_clean = df_part.rename(columns=rename_map)
        master = master.merge(part_clean, on="ipo_id", how="left")

    master = master[master["ipo_id"].isin(valid_gmp_ipo_ids)].reset_index(drop=True)

    # 6b. Deterministic trusted cross-fill for pre-listing financial and market features
    # Backfill normalized financials from verified reported accounting equivalents
    if "Total_Borrowings_Norm" in master.columns and "totalDebt" in master.columns:
        m_b = master["Total_Borrowings_Norm"].isna() & master["totalDebt"].notna()
        master.loc[m_b, "Total_Borrowings_Norm"] = master.loc[m_b, "totalDebt"]
    if "Total_Book_Value" in master.columns and "bookValue" in master.columns:
        m_bv = master["Total_Book_Value"].isna() & master["bookValue"].notna()
        master.loc[m_bv, "Total_Book_Value"] = master.loc[m_bv, "bookValue"]
    if "Revenue_Current_Norm" in master.columns and "revenue" in master.columns:
        m_rev = master["Revenue_Current_Norm"].isna() & master["revenue"].notna()
        master.loc[m_rev, "Revenue_Current_Norm"] = master.loc[m_rev, "revenue"]
    if "PAT_Norm" in master.columns and "PAT" in master.columns:
        m_pat = master["PAT_Norm"].isna() & master["PAT"].notna()
        master.loc[m_pat, "PAT_Norm"] = master.loc[m_pat, "PAT"]
    if "EBITDA_Norm" in master.columns and "EBITDA" in master.columns:
        m_eb = master["EBITDA_Norm"].isna() & master["EBITDA"].notna()
        master.loc[m_eb, "EBITDA_Norm"] = master.loc[m_eb, "EBITDA"]
    if "EPS" in master.columns and "trailingEps" in master.columns:
        m_eps = master["EPS"].isna() & master["trailingEps"].notna()
        master.loc[m_eps, "EPS"] = master.loc[m_eps, "trailingEps"]
    if "Is_Profitable" in master.columns and "PAT_Norm" in master.columns:
        m_prof = master["Is_Profitable"].isna() & master["PAT_Norm"].notna()
        master.loc[m_prof, "Is_Profitable"] = (master.loc[m_prof, "PAT_Norm"] > 0).astype(int)
    if "Net_Profit_Margin_Pct" in master.columns and "PAT_Norm" in master.columns and "Revenue_Current_Norm" in master.columns:
        m_npm = master["Net_Profit_Margin_Pct"].isna() & master["PAT_Norm"].notna() & master["Revenue_Current_Norm"].notna() & (master["Revenue_Current_Norm"] > 0)
        master.loc[m_npm, "Net_Profit_Margin_Pct"] = (master.loc[m_npm, "PAT_Norm"] / master.loc[m_npm, "Revenue_Current_Norm"] * 100.0).round(2)
    if "Debt_to_Equity_Ratio" in master.columns and "Total_Borrowings_Norm" in master.columns and "Total_Book_Value" in master.columns:
        m_de = master["Debt_to_Equity_Ratio"].isna() & master["Total_Borrowings_Norm"].notna() & master["Total_Book_Value"].notna() & (master["Total_Book_Value"] > 0)
        master.loc[m_de, "Debt_to_Equity_Ratio"] = (master.loc[m_de, "Total_Borrowings_Norm"] / master.loc[m_de, "Total_Book_Value"]).round(2)
    if "Derived_Fresh_Shares" in master.columns and "Fresh_Issue_Size_Norm" in master.columns and "issue_base_price" in master.columns:
        m_fs = master["Derived_Fresh_Shares"].isna() & master["Fresh_Issue_Size_Norm"].notna() & (master["issue_base_price"] > 0)
        master.loc[m_fs, "Derived_Fresh_Shares"] = (master.loc[m_fs, "Fresh_Issue_Size_Norm"] * 1e7 / master.loc[m_fs, "issue_base_price"]).round(0)
    if "sector" in master.columns and "symbol" in master.columns:
        sym_sec_map = master[master["symbol"].notna() & master["sector"].notna()].groupby("symbol")["sector"].first().to_dict()
        m_sec = master["sector"].isna() & master["symbol"].isin(sym_sec_map)
        master.loc[m_sec, "sector"] = master.loc[m_sec, "symbol"].map(sym_sec_map)
    if "industry" in master.columns and "symbol" in master.columns:
        sym_ind_map = master[master["symbol"].notna() & master["industry"].notna()].groupby("symbol")["industry"].first().to_dict()
        m_ind = master["industry"].isna() & master["symbol"].isin(sym_ind_map)
        master.loc[m_ind, "industry"] = master.loc[m_ind, "symbol"].map(sym_ind_map)
    if "is_mainboard" in master.columns and "issue_type" in master.columns and "is_sme" in master.columns:
        m_mb = master["is_mainboard"].isna() & master["issue_type"].isin(["IPO", "Fixed Price Issue IPO", "Book Built Issue IPO"])
        master.loc[m_mb, "is_mainboard"] = 1.0
        master.loc[m_mb, "is_sme"] = 0.0

    master = master.where(pd.notnull(master), None)

    # 7. Write relational tables + foreign keys + master table into SQLite
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = db_path.with_suffix(".db.tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        _write_sqlite_table(conn, "dim_ipos", df_dim, pk="ipo_id")
        _write_sqlite_table(
            conn, "fact_subscription_listing", df_sub, pk="ipo_id", fk_parent="dim_ipos"
        )
        _write_sqlite_table(
            conn, "fact_gmp_history", df_gmp, pk="ipo_id", fk_parent="dim_ipos"
        )
        _write_sqlite_table(
            conn, "fact_financials_debt", df_fin, pk="ipo_id", fk_parent="dim_ipos"
        )
        _write_sqlite_table(
            conn, "fact_market_prices", df_mkt, pk="ipo_id", fk_parent="dim_ipos"
        )
        _write_sqlite_table(conn, "ipo_merged_master", master, pk="ipo_id")

        lineage_df = pd.DataFrame(
            lineage,
            columns=[
                "source_dataset",
                "original_column",
                "warehouse_table",
                "warehouse_column",
            ],
        )
        lineage_df.to_sql("meta_column_lineage", conn, if_exists="replace", index=False)
        conn.commit()
    finally:
        conn.close()

    tmp_db.replace(db_path)

    # Also export master snapshot to staging CSV for inspection
    master_csv = config.STAGING_DIR / "ipo_merged_master.csv"
    tmp_csv = master_csv.with_suffix(".csv.tmp")
    try:
        master.to_csv(tmp_csv, index=False)
        tmp_csv.replace(master_csv)
    except PermissionError as exc:
        logger.warning(
            "Could not overwrite %s (file may be open/locked): %s", master_csv, exc
        )
        tmp_csv.unlink(missing_ok=True)

    # 8. Build summary metrics
    matched_multi = sum(
        1 for iid, tags in sources_per_ipo.items() if iid in valid_gmp_ipo_ids and len(tags) > 1
    )
    null_cov: dict[str, dict[str, float]] = {}
    for tname, df_tbl in (
        ("dim_ipos", df_dim),
        ("fact_subscription_listing", df_sub),
        ("fact_gmp_history", df_gmp),
        ("fact_financials_debt", df_fin),
        ("fact_market_prices", df_mkt),
    ):
        if df_tbl.empty:
            null_cov[tname] = {"non_null_pct": 0.0, "null_pct": 100.0}
        else:
            total_cells = df_tbl.shape[0] * df_tbl.shape[1]
            non_null_cells = int(df_tbl.notna().sum().sum())
            non_null_pct = round((non_null_cells / total_cells) * 100.0, 2) if total_cells else 0.0
            null_cov[tname] = {
                "non_null_pct": non_null_pct,
                "null_pct": round(100.0 - non_null_pct, 2),
            }

    return MergeSummary(
        table_row_counts={
            "dim_ipos": len(df_dim),
            "fact_subscription_listing": len(df_sub),
            "fact_gmp_history": len(df_gmp),
            "fact_financials_debt": len(df_fin),
            "fact_market_prices": len(df_mkt),
            "ipo_merged_master": len(master),
        },
        total_master_rows=len(master),
        total_master_columns=len(master.columns),
        matched_multi_source_ipos=matched_multi,
        null_coverage_by_group=null_cov,
    )


def _sqlite_type(dtype: Any) -> str:
    s = str(dtype).lower()
    if "int" in s:
        return "INTEGER"
    if "float" in s:
        return "REAL"
    return "TEXT"


def _to_sqlite_scalar(val: Any) -> Any:
    """Convert DataFrame cell to SQLite-safe scalar while preserving None as NULL."""
    if val is None:
        return None
    if isinstance(val, (list, dict, tuple, set)):
        return json.dumps(list(val) if isinstance(val, (tuple, set)) else val, ensure_ascii=False) if val else None
    try:
        if pd.isna(val):
            return None
    except Exception:
        return str(val)
    if isinstance(val, (pd.Timestamp, datetime, date)):
        return val.isoformat()
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    if isinstance(val, (int, float, str, bytes, bool)):
        return val
    if hasattr(val, "item"):
        return val.item()
    return str(val)


def _write_sqlite_table(
    conn: sqlite3.Connection,
    table_name: str,
    df: pd.DataFrame,
    pk: str,
    fk_parent: Optional[str] = None,
) -> None:
    """Create SQLite table with explicit PRIMARY KEY / FOREIGN KEY and insert rows cleanly."""
    cols = list(df.columns)
    col_defs = []
    for col in cols:
        qcol = '"' + str(col).replace('"', '""') + '"'
        ctype = _sqlite_type(df[col].dtype)
        if col == pk:
            col_defs.append(f"{qcol} TEXT PRIMARY KEY")
        else:
            col_defs.append(f"{qcol} {ctype}")

    if fk_parent:
        qpk = '"' + pk.replace('"', '""') + '"'
        col_defs.append(f"FOREIGN KEY ({qpk}) REFERENCES {fk_parent}({qpk})")

    qtable = '"' + table_name.replace('"', '""') + '"'
    conn.execute(f"DROP TABLE IF EXISTS {qtable};")
    conn.execute(f"CREATE TABLE {qtable} ({', '.join(col_defs)});")

    if df.empty:
        return

    placeholders = ", ".join(["?"] * len(cols))
    qcols = ", ".join('"' + str(c).replace('"', '""') + '"' for c in cols)
    sql = f"INSERT INTO {qtable} ({qcols}) VALUES ({placeholders});"

    rows_to_insert = []
    for rec in df.itertuples(index=False, name=None):
        clean_rec = tuple(_to_sqlite_scalar(v) for v in rec)
        rows_to_insert.append(clean_rec)

    conn.executemany(sql, rows_to_insert)
