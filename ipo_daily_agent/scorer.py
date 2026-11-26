"""Deterministic Python financial calculator, yfinance peer check, and scorer."""

from __future__ import annotations

import logging
import re
import statistics
from typing import Optional

import yfinance as yf

from ipo_daily_agent import config
from ipo_daily_agent.models import (
    AnchorExtraction,
    AnchorStatus,
    Decision,
    FinancialYearMetrics,
    IPOListing,
    MetricBreakdown,
    RHPExtraction,
    ScoringResult,
)

logger = logging.getLogger(__name__)


def _fiscal_year_sort_key(fy_label: str) -> int:
    match = re.search(r"(\d{2,4})", fy_label or "")
    if not match:
        return 0
    num = int(match.group(1))
    return 2000 + num if num < 100 else num


def _compute_cagr(values: list[Optional[float]]) -> Optional[float]:
    """Compute CAGR (%) from earliest to latest positive value across ordered years."""
    clean = [(idx, val) for idx, val in enumerate(values) if val is not None]
    if len(clean) < 2:
        return None
    start_idx, start_val = clean[0]
    end_idx, end_val = clean[-1]
    periods = end_idx - start_idx
    if periods <= 0 or start_val <= 0 or end_val <= 0:
        return None
    return ((end_val / start_val) ** (1.0 / periods) - 1.0) * 100.0


def _parse_upper_price_from_range(price_range: Optional[str]) -> Optional[float]:
    if not price_range:
        return None
    nums = [float(m.replace(",", "")) for m in re.findall(r"[\d,]+(?:\.\d+)?", price_range)]
    return max(nums) if nums else None


def fetch_peer_median_pe(rhp: RHPExtraction) -> tuple[Optional[float], Optional[str]]:
    """Fetch live NSE peer median P/E via yfinance (<SYMBOL>.NS), falling back to RHP peers."""
    live_pes: list[float] = []
    for peer in rhp.peer_companies:
        if not peer.nse_symbol:
            continue
        ticker_sym = f"{peer.nse_symbol.strip().upper()}.NS"
        try:
            info = yf.Ticker(ticker_sym).info or {}
            pe = info.get("trailingPE") or info.get("forwardPE")
            if isinstance(pe, (int, float)) and 0 < pe < 500:
                live_pes.append(float(pe))
        except Exception as exc:
            logger.debug("yfinance lookup failed for %s: %s", ticker_sym, exc)

    if live_pes:
        return round(statistics.median(live_pes), 2), f"yfinance ({len(live_pes)} NSE peers)"

    rhp_pes = [
        float(p.reported_pe)
        for p in rhp.peer_companies
        if p.reported_pe is not None and 0 < p.reported_pe < 500
    ]
    if rhp_pes:
        return round(statistics.median(rhp_pes), 2), f"RHP table ({len(rhp_pes)} peers)"

    return None, None


def score_ipo(
    listing: IPOListing,
    rhp: Optional[RHPExtraction],
    anchor: Optional[AnchorExtraction],
    anchor_status: AnchorStatus,
    peer_median_pe_override: Optional[float] = None,
) -> ScoringResult:
    """Perform ALL financial math deterministically in Python and produce decision."""
    th = config.THRESHOLDS
    notes: list[str] = []
    hard_skips: list[str] = []
    metrics = MetricBreakdown()

    if rhp is None:
        return ScoringResult(
            symbol=listing.symbol,
            company_name=listing.company_name,
            score=0.0,
            decision="DATA_PENDING",
            anchor_status=anchor_status,
            notes=["RHP JSON not extracted yet (download-only or missing GEMINI_API_KEY)."],
            metrics=metrics,
        )

    score = 0.0

    # 1. Fresh Issue % = fresh_issue / (fresh_issue + ofs)
    fresh = rhp.fresh_issue_cr or 0.0
    ofs = rhp.ofs_cr or 0.0
    total_split = fresh + ofs
    if total_split > 0:
        fresh_pct = round((fresh / total_split) * 100.0, 2)
        metrics.fresh_issue_pct = fresh_pct
        if fresh_pct >= th.fresh_issue_strong_pct:
            score += th.weight_fresh_issue
        elif fresh_pct >= th.fresh_issue_ok_pct:
            score += th.weight_fresh_issue * 0.65
        else:
            score += th.weight_fresh_issue * 0.25
            notes.append(f"Heavy OFS: fresh issue is only {fresh_pct:.1f}%.")
    else:
        notes.append("Fresh issue vs OFS split unavailable in RHP.")

    # 2. Revenue & PAT growth + Operating Cash Flow check
    fys: list[FinancialYearMetrics] = sorted(
        rhp.financial_years, key=lambda fy: _fiscal_year_sort_key(fy.fiscal_year)
    )
    rev_cagr = _compute_cagr([fy.revenue_cr for fy in fys])
    pat_cagr = _compute_cagr([fy.pat_cr for fy in fys])
    metrics.revenue_cagr_pct = round(rev_cagr, 2) if rev_cagr is not None else None
    metrics.pat_cagr_pct = round(pat_cagr, 2) if pat_cagr is not None else None

    ocf_vals = [fy.operating_cash_flow_cr for fy in fys if fy.operating_cash_flow_cr is not None]
    if ocf_vals:
        metrics.latest_ocf_cr = round(ocf_vals[-1], 2)
        metrics.ocf_positive_all_years = all(v > 0 for v in ocf_vals)

    growth_score = 0.0
    if rev_cagr is not None:
        if rev_cagr >= th.revenue_cagr_strong_pct:
            growth_score += 8.0
        elif rev_cagr >= th.revenue_cagr_ok_pct:
            growth_score += 5.0
        elif rev_cagr > 0:
            growth_score += 2.0
    if pat_cagr is not None:
        if pat_cagr >= th.pat_cagr_strong_pct:
            growth_score += 9.0
        elif pat_cagr >= th.pat_cagr_ok_pct:
            growth_score += 5.5
        elif pat_cagr > 0:
            growth_score += 2.5

    if metrics.ocf_positive_all_years is True:
        growth_score += 8.0
    elif metrics.latest_ocf_cr is not None and metrics.latest_ocf_cr > 0:
        growth_score += 4.5
    elif metrics.latest_ocf_cr is not None and metrics.latest_ocf_cr <= 0:
        notes.append(f"Negative/zero latest Operating Cash Flow ({metrics.latest_ocf_cr} Cr).")

    score += min(growth_score, float(th.weight_growth_ocf))

    # Hard-skip check: negative latest OCF combined with declining revenue or negative latest PAT
    latest_pat = next((fy.pat_cr for fy in reversed(fys) if fy.pat_cr is not None), None)
    if latest_pat is not None and latest_pat <= 0:
        hard_skips.append(f"Loss-making latest FY (PAT = {latest_pat:.2f} Cr).")
    if (
        metrics.latest_ocf_cr is not None
        and metrics.latest_ocf_cr < 0
        and rev_cagr is not None
        and rev_cagr < 0
    ):
        hard_skips.append("Negative latest OCF combined with shrinking revenue.")

    # 3. Peer median P/E via yfinance (<SYMBOL>.NS) vs IPO upper-band P/E
    upper_price = rhp.price_band_high_inr or _parse_upper_price_from_range(listing.price_range)
    eps = rhp.upper_band_eps_inr
    if eps is None and fys:
        eps = next(
            (
                fy.eps_diluted or fy.eps_basic
                for fy in reversed(fys)
                if (fy.eps_diluted or fy.eps_basic) is not None
            ),
            None,
        )

    if upper_price is not None and eps is not None and eps > 0:
        metrics.ipo_upper_pe = round(upper_price / eps, 2)
    elif upper_price is not None and eps is not None and eps <= 0:
        hard_skips.append(f"Non-positive EPS ({eps}) at upper price band.")

    if peer_median_pe_override is not None:
        metrics.peer_median_pe = peer_median_pe_override
        metrics.peer_pe_source = "override"
    else:
        metrics.peer_median_pe, metrics.peer_pe_source = fetch_peer_median_pe(rhp)

    if metrics.ipo_upper_pe is not None and metrics.peer_median_pe is not None:
        ratio = round(metrics.ipo_upper_pe / metrics.peer_median_pe, 2)
        metrics.pe_vs_peer_ratio = ratio
        if ratio <= th.pe_discount_ratio:
            score += th.weight_valuation
        elif ratio <= th.pe_fair_ratio:
            score += th.weight_valuation * 0.75
        elif ratio <= th.pe_expensive_ratio:
            score += th.weight_valuation * 0.35
        else:
            notes.append(
                f"Expensive valuation: IPO P/E {metrics.ipo_upper_pe}x vs peer median {metrics.peer_median_pe}x."
            )
        if ratio >= th.hard_skip_pe_ratio:
            hard_skips.append(
                f"Extreme valuation premium ({ratio:.2f}x peer median P/E)."
            )
    elif metrics.ipo_upper_pe is not None:
        # Neutral partial score when no listed peers are available
        score += th.weight_valuation * 0.5
        notes.append(f"No listed peer P/E found; IPO upper P/E is {metrics.ipo_upper_pe}x.")

    # 4. Tier-1 Anchor share = (Domestic MF + Domestic Insurance shares) / total anchor shares
    if anchor is not None and anchor.total_anchor_shares and anchor.total_anchor_shares > 0:
        mf_shares = anchor.domestic_mf_shares or 0
        ins_shares = anchor.domestic_insurance_shares or 0
        tier1_pct = round(((mf_shares + ins_shares) / anchor.total_anchor_shares) * 100.0, 2)
        metrics.tier1_anchor_share_pct = tier1_pct
        if tier1_pct >= th.anchor_strong_pct:
            score += th.weight_anchor
        elif tier1_pct >= th.anchor_ok_pct:
            score += th.weight_anchor * 0.65
        else:
            score += th.weight_anchor * 0.25
            notes.append(f"Low Tier-1 Anchor participation ({tier1_pct:.1f}%).")
    else:
        notes.append(f"Anchor allocation status: {anchor_status}.")

    # 5. Live QIB subscription multiple (qib_x)
    qib_x = listing.subscription.qib_x
    metrics.qib_x = qib_x
    if qib_x is not None:
        if qib_x >= th.qib_strong_x:
            score += th.weight_qib
        elif qib_x >= th.qib_confirm_x:
            score += th.weight_qib * 0.7
        elif qib_x >= 1.0:
            score += th.weight_qib * 0.3
        else:
            notes.append(f"Current QIB subscription is {qib_x:.2f}x.")

    final_score = round(min(max(score, 0.0), 100.0), 1)

    # Final Decision Logic
    if hard_skips:
        decision: Decision = "SKIP"
    elif final_score < th.watch_min_score:
        decision = "SKIP"
    elif anchor_status == "PENDING" or qib_x is None or qib_x < th.qib_confirm_x:
        decision = "WATCH / CONFIRM DAY-3 QIB"
    elif final_score >= th.apply_min_score and qib_x >= th.qib_confirm_x:
        decision = "APPLY"
    else:
        decision = "WATCH / CONFIRM DAY-3 QIB"

    return ScoringResult(
        symbol=listing.symbol,
        company_name=listing.company_name,
        score=final_score,
        decision=decision,
        anchor_status=anchor_status,
        hard_skip_reasons=hard_skips,
        notes=notes,
        metrics=metrics,
    )
