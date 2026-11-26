"""Pydantic data models for listings, Gemini extraction schemas, state, and scoring."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class SubscriptionData(BaseModel):
    """Live IPO bidding subscription multiples."""

    qib_x: Optional[float] = None
    nii_x: Optional[float] = None
    retail_x: Optional[float] = None
    total_x: Optional[float] = None
    updated_at: Optional[str] = None


class IPOListing(BaseModel):
    """Discovered IPO metadata from NSE or fallback public sources."""

    symbol: str
    company_name: str
    series: str = "Equity"  # Equity (Mainboard) or SME
    status: str = "Forthcoming"  # Active / Forthcoming
    issue_start_date: Optional[str] = None
    issue_end_date: Optional[str] = None
    price_range: Optional[str] = None
    issue_size_shares: Optional[int] = None
    rhp_url: Optional[str] = None
    anchor_url: Optional[str] = None
    source: str = "NSE"
    subscription: SubscriptionData = Field(default_factory=SubscriptionData)


class FinancialYearMetrics(BaseModel):
    """Single fiscal year numbers extracted strictly as reported in the RHP."""

    fiscal_year: str = Field(description="Fiscal year label, e.g., 'FY24', 'FY25', 'FY26'")
    revenue_cr: Optional[float] = Field(
        default=None, description="Revenue from operations in INR Crores"
    )
    pat_cr: Optional[float] = Field(
        default=None, description="Profit After Tax (PAT) in INR Crores"
    )
    operating_cash_flow_cr: Optional[float] = Field(
        default=None, description="Net cash generated from operating activities in INR Crores"
    )
    eps_basic: Optional[float] = Field(
        default=None, description="Basic Earnings Per Share in INR"
    )
    eps_diluted: Optional[float] = Field(
        default=None, description="Diluted Earnings Per Share in INR"
    )


class PeerCompany(BaseModel):
    """Listed industry peer mentioned in RHP 'Basis for Issue Price'."""

    name: str
    nse_symbol: Optional[str] = Field(
        default=None, description="NSE ticker symbol without .NS suffix if known/listed"
    )
    reported_pe: Optional[float] = Field(
        default=None, description="P/E multiple reported in the RHP peer comparison table"
    )


class RHPExtraction(BaseModel):
    """Strict structured schema for Gemini extraction from Red Herring Prospectus PDF."""

    company_name: str
    symbol: Optional[str] = None
    face_value_inr: Optional[float] = None
    price_band_low_inr: Optional[float] = None
    price_band_high_inr: Optional[float] = None
    fresh_issue_cr: Optional[float] = Field(
        default=None, description="Fresh issue size at upper price band in INR Crores"
    )
    ofs_cr: Optional[float] = Field(
        default=None, description="Offer for Sale (OFS) size at upper price band in INR Crores"
    )
    total_issue_cr: Optional[float] = Field(
        default=None, description="Total issue size at upper price band in INR Crores"
    )
    lot_size: Optional[int] = None
    upper_band_eps_inr: Optional[float] = Field(
        default=None, description="Latest full-year basic/diluted EPS used in Basis for Issue Price"
    )
    financial_years: list[FinancialYearMetrics] = Field(default_factory=list)
    peer_companies: list[PeerCompany] = Field(default_factory=list)
    objects_of_issue: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)


class AnchorExtraction(BaseModel):
    """Strict structured schema for Gemini extraction from T-1 Anchor Allocation PDF."""

    company_name: str
    anchor_date: Optional[str] = None
    anchor_price_inr: Optional[float] = None
    total_anchor_shares: Optional[int] = Field(
        default=None, description="Total equity shares allotted to all anchor investors combined"
    )
    total_anchor_amount_cr: Optional[float] = Field(
        default=None, description="Total amount allotted to anchor investors in INR Crores"
    )
    domestic_mf_shares: Optional[int] = Field(
        default=None,
        description="Total shares allotted to Domestic Mutual Funds / Mutual Fund schemes",
    )
    domestic_insurance_shares: Optional[int] = Field(
        default=None,
        description="Total shares allotted to Domestic Insurance Companies",
    )
    fpi_shares: Optional[int] = Field(
        default=None, description="Total shares allotted to Foreign Portfolio Investors (FPIs)"
    )
    other_shares: Optional[int] = Field(
        default=None, description="Total shares allotted to other anchor investor categories"
    )
    anchor_investors_count: Optional[int] = None
    marquee_investors: list[str] = Field(default_factory=list)


AnchorStatus = Literal["PENDING", "DOWNLOADED", "EXTRACTED", "NOT_APPLICABLE"]
Decision = Literal["APPLY", "WATCH / CONFIRM DAY-3 QIB", "SKIP", "DATA_PENDING"]


class MetricBreakdown(BaseModel):
    """Deterministic Python-computed metrics for an IPO."""

    fresh_issue_pct: Optional[float] = None
    revenue_cagr_pct: Optional[float] = None
    pat_cagr_pct: Optional[float] = None
    latest_ocf_cr: Optional[float] = None
    ocf_positive_all_years: Optional[bool] = None
    ipo_upper_pe: Optional[float] = None
    peer_median_pe: Optional[float] = None
    peer_pe_source: Optional[str] = None
    pe_vs_peer_ratio: Optional[float] = None
    tier1_anchor_share_pct: Optional[float] = None
    qib_x: Optional[float] = None


class ScoringResult(BaseModel):
    """Deterministic score, hard-skip reasons, and final decision."""

    symbol: str
    company_name: str
    score: float
    decision: Decision
    anchor_status: AnchorStatus
    hard_skip_reasons: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metrics: MetricBreakdown = Field(default_factory=MetricBreakdown)


class IPOStateRecord(BaseModel):
    """Cached per-IPO processing state saved in data/seen_ipos.json."""

    symbol: str
    company_name: str
    series: str = "Equity"
    status: str = "Forthcoming"
    issue_start_date: Optional[str] = None
    issue_end_date: Optional[str] = None
    rhp_url: Optional[str] = None
    anchor_url: Optional[str] = None
    rhp_downloaded: bool = False
    rhp_extracted: bool = False
    anchor_status: AnchorStatus = "PENDING"
    last_qib_x: Optional[float] = None
    last_nii_x: Optional[float] = None
    last_retail_x: Optional[float] = None
    last_total_x: Optional[float] = None
    last_score: Optional[float] = None
    last_decision: Optional[str] = None
    last_checked_at: Optional[str] = None
    last_scored_date: Optional[str] = None


class DailyRunItem(BaseModel):
    """Single IPO outcome recorded in data/runs/YYYY-MM-DD.json."""

    symbol: str
    company_name: str
    series: str
    status: str
    issue_start_date: Optional[str] = None
    issue_end_date: Optional[str] = None
    rhp_path: Optional[str] = None
    anchor_path: Optional[str] = None
    anchor_status: AnchorStatus = "PENDING"
    scoring: Optional[ScoringResult] = None
    error: Optional[str] = None


class DailyRunReport(BaseModel):
    """Full daily execution log."""

    run_date: str
    ran_at: str
    ipos_processed: list[DailyRunItem] = Field(default_factory=list)
