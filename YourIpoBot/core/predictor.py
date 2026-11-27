"""
5-Tier Weighted k-NN Similarity & GMP Predictor.
Implements user-specified weights:
- Day 2-3 Subscription: 30%
- Issue Size & Liquidity: 20%
- Profitability & Margin Strength: 25%
- Debt, Cash Flow & Dilution: 15%
- Other Info & Governance: 10%

With Sector-Priority Top-5 Peer Selection and adaptive scaling for unit differences.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd

from YourIpoBot import config

logger = logging.getLogger(__name__)


@dataclass
class PeerMatchResult:
    ipo_id: str
    canonical_name: str
    ipo_year: Optional[int]
    sector: Optional[str]
    industry: Optional[str]
    similarity_score: float
    historical_gmp_pct: float
    is_same_industry: bool
    is_same_sector: bool
    issue_size_cr: Optional[float]
    net_profit_margin_pct: Optional[float]


@dataclass
class GMPPredictionResponse:
    target_ipo_id: str
    canonical_name: str
    predicted_gmp_pct: Optional[float]
    confidence_level: str               # HIGH, MODERATE, LOW, INSUFFICIENT_DATA
    decision_badge: str                 # STRONG_APPLY, APPLY_FOR_GAIN, RISK_CAUTION, AVOID, NEUTRAL_PENDING
    decision_reason: str                # Explanatory trigger reason
    peers_used_count: int
    industry_peers_count: int
    sector_peers_count: int
    other_peers_count: int
    top_peers: list[PeerMatchResult]
    summary_message: str


class GMPSimilarityEngine:
    """Quantitative k-NN peer matcher using 5 weighted feature sub-groups."""

    def __init__(self, master_csv_path: Optional[str] = None) -> None:
        csv_file = master_csv_path or str(config.MASTER_CSV_PATH)
        self.history_df = pd.read_csv(csv_file)
        self._prepare_history()

    def _prepare_history(self) -> None:
        """Filter to valid historical records that have known GMP."""
        self.history_df = self.history_df[self.history_df["GMP_Day3_Clean"].notna()].copy()
        logger.info("Loaded %d historical benchmark IPOs for peer matching.", len(self.history_df))

    def _normalize_currency_units(self, val: float) -> float:
        """Convert large absolute currency numbers (e.g., 5,000,000,000) into Crores (500)."""
        if val is None or pd.isna(val):
            return 0.0
        val = float(val)
        if abs(val) > 10000000.0:
            return val / 10000000.0  # Convert Rupees to Crores
        elif abs(val) > 100000.0:
            return val / 100.0       # Convert Lakhs to Crores
        return val

    def _feature_similarity(self, val_a: Any, val_b: Any, tolerance_ratio: float = 1.0) -> Optional[float]:
        """Compute relative percentage closeness between two metrics [0.0 to 1.0]."""
        if pd.isna(val_a) or pd.isna(val_b) or val_a is None or val_b is None:
            return None
        v1 = float(val_a)
        v2 = float(val_b)
        denom = max(abs(v1), abs(v2), 1e-4)
        rel_diff = abs(v1 - v2) / denom
        return max(0.0, 1.0 - (rel_diff / (tolerance_ratio * 1.5)))

    def compute_similarity(self, target: dict[str, Any], candidate: pd.Series) -> float:
        """Calculate weighted composite similarity score across fundamentals (excluding live subscription)."""
        size_scores = []
        # Pruned noisy columns (OFS_Size_Norm, Fresh_Issue_Size_Norm) - keeping core pricing & size signal
        for col in ["issue_size_cr", "issue_base_price"]:
            v1 = self._normalize_currency_units(target.get(col)) if "size" in col.lower() else target.get(col)
            v2 = self._normalize_currency_units(candidate.get(col)) if "size" in col.lower() else candidate.get(col)
            s = self._feature_similarity(v1, v2, tolerance_ratio=1.5)
            if s is not None:
                size_scores.append(s)

        prof_scores = []
        # 100% Percentage-based Profit Comparison:
        # Net_Profit_Margin_Pct: (PAT / Revenue) * 100
        # Revenue_Growth_YoY_Pct: Year-over-Year revenue expansion %
        # Is_Profitable: Binary 1.0 vs 0.0 match
        for col in ["Net_Profit_Margin_Pct", "Revenue_Growth_YoY_Pct"]:
            val_t = target.get(col)
            val_c = candidate.get(col)
            # If Net_Profit_Margin_Pct is missing in target, compute directly from PAT and Revenue if present
            if val_t is None and col == "Net_Profit_Margin_Pct":
                pat = target.get("PAT_Norm") or target.get("PAT")
                rev = target.get("Revenue_Current_Norm") or target.get("revenue")
                if pat is not None and rev and float(rev) > 0:
                    val_t = (float(pat) / float(rev)) * 100.0

            s = self._feature_similarity(val_t, val_c, tolerance_ratio=1.5)
            if s is not None:
                prof_scores.append(s)

        # Is Profitable match
        t_prof = target.get("Is_Profitable")
        c_prof = candidate.get("Is_Profitable")
        if pd.notna(t_prof) and pd.notna(c_prof):
            prof_scores.append(1.0 if int(float(t_prof)) == int(float(c_prof)) else 0.0)

        debt_scores = []
        # Pruned noisy Total_Book_Value and BVPS - keeping core solvency & leverage
        for col in ["Debt_to_Equity_Ratio", "Total_Borrowings_Norm"]:
            v1 = self._normalize_currency_units(target.get(col)) if "Borrowings" in col else target.get(col)
            v2 = self._normalize_currency_units(candidate.get(col)) if "Borrowings" in col else candidate.get(col)
            s = self._feature_similarity(v1, v2, tolerance_ratio=1.8)
            if s is not None:
                debt_scores.append(s)

        other_scores = []
        if "is_sme" in target and pd.notna(target["is_sme"]):
            other_scores.append(1.0 if target["is_sme"] == candidate.get("is_sme") else 0.0)
        t_sec = str(target.get("sector") or "").lower()
        c_sec = str(candidate.get("sector") or "").lower()
        if t_sec and c_sec and (t_sec in c_sec or c_sec in t_sec):
            other_scores.append(1.0)
        elif t_sec or c_sec:
            other_scores.append(0.5)

        group_weights = {
            "prof": (np.mean(prof_scores) if prof_scores else None, config.WEIGHT_PROFITABILITY),
            "size": (np.mean(size_scores) if size_scores else None, config.WEIGHT_ISSUE_SIZE),
            "debt": (np.mean(debt_scores) if debt_scores else None, config.WEIGHT_DEBT_CASH),
            "other": (np.mean(other_scores) if other_scores else None, config.WEIGHT_OTHER_INFO),
        }

        active_weights = sum(w for score, w in group_weights.values() if score is not None)
        if active_weights == 0:
            return 0.0

        composite_score = sum(score * w for score, w in group_weights.values() if score is not None) / active_weights
        return round(composite_score * 100.0, 2)

    def predict_gmp(self, target_ipo: dict[str, Any], min_threshold: float = config.SIMILARITY_THRESHOLD) -> GMPPredictionResponse:
        """
        Execute 3-Tier Hierarchy Selection Rule:
        1. Select peers matching >= min_threshold (default 85%) in the SAME INDUSTRY.
        2. If fewer than 5, select peers matching >= min_threshold in the SAME SECTOR.
        3. If still fewer than 5, fill remaining spots up to 5 from the HIGHEST remaining matches >= min_threshold.
        4. If fewer than 5 meet 85%, adaptively average whatever meets the threshold (e.g. 4, 3, etc.).
        5. Compute similarity-weighted average GMP.
        """
        cname = target_ipo.get("canonical_name", "Unknown IPO")
        target_id = target_ipo.get("ipo_id", "unlisted_ipo")
        target_ind = str(target_ipo.get("industry") or "").lower().strip()
        target_sec = str(target_ipo.get("sector") or "").lower().strip()

        candidate_matches: list[tuple[float, pd.Series, bool, bool]] = []
        target_is_profitable = target_ipo.get("Is_Profitable")
        if target_is_profitable is None and (target_ipo.get("Net_Profit_Margin_Pct") is not None):
            target_is_profitable = 1 if float(target_ipo["Net_Profit_Margin_Pct"]) > 0 else 0

        target_size = self._normalize_currency_units(target_ipo.get("issue_size_cr"))

        for _, row in self.history_df.iterrows():
            # Hard Safety Filter: Profitable companies must only match profitable peers;
            # Loss-making companies must only match loss-making peers.
            cand_is_profitable = row.get("Is_Profitable")
            if target_is_profitable is not None and pd.notna(cand_is_profitable):
                if int(float(target_is_profitable)) != int(float(cand_is_profitable)):
                    continue

            # Issue Size Liquidity Bracketing:
            # Prevents a ₹200 Cr IPO from matching against a ₹20,000 Cr Mega-IPO
            if target_size and target_size > 0:
                cand_size = self._normalize_currency_units(row.get("issue_size_cr"))
                if cand_size and cand_size > 0:
                    ratio = cand_size / target_size
                    if ratio < 0.15 or ratio > 6.0:  # Beyond reasonable liquidity bracket
                        continue

            sim = self.compute_similarity(target_ipo, row)
            r_ind = str(row.get("industry") or "").lower().strip()
            r_sec = str(row.get("sector") or "").lower().strip()

            is_same_ind = bool(target_ind and r_ind and (target_ind in r_ind or r_ind in target_ind))
            is_same_sec = bool(target_sec and r_sec and (target_sec in r_sec or r_sec in target_sec))
            candidate_matches.append((sim, row, is_same_ind, is_same_sec))

        candidate_matches.sort(key=lambda x: x[0], reverse=True)

        # Filter strictly >= min_threshold (85%)
        qualifying = [item for item in candidate_matches if item[0] >= min_threshold]

        # Graceful fallback if zero peers qualify at 85%
        if not qualifying:
            qualifying = candidate_matches[:config.TOP_PEERS_COUNT]

        pool_same_industry: list[PeerMatchResult] = []
        pool_same_sector: list[PeerMatchResult] = []
        pool_other: list[PeerMatchResult] = []

        for sim, row, is_same_ind, is_same_sec in qualifying:
            peer = PeerMatchResult(
                ipo_id=str(row.get("ipo_id")),
                canonical_name=str(row.get("canonical_name")),
                ipo_year=int(row.get("ipo_year")) if pd.notna(row.get("ipo_year")) else None,
                sector=str(row.get("sector")) if pd.notna(row.get("sector")) else None,
                industry=str(row.get("industry")) if pd.notna(row.get("industry")) else None,
                similarity_score=sim,
                historical_gmp_pct=float(row.get("GMP_Day3_Clean")),
                is_same_industry=is_same_ind,
                is_same_sector=is_same_sec,
                issue_size_cr=float(row.get("issue_size_cr")) if pd.notna(row.get("issue_size_cr")) else None,
                net_profit_margin_pct=float(row.get("Net_Profit_Margin_Pct")) if pd.notna(row.get("Net_Profit_Margin_Pct")) else None,
            )
            if is_same_ind:
                pool_same_industry.append(peer)
            elif is_same_sec:
                pool_same_sector.append(peer)
            else:
                pool_other.append(peer)

        # 3-Tier Priority Selection: Industry -> Sector -> Cross-Sector Top Match
        selected_peers: list[PeerMatchResult] = []
        selected_peers.extend(pool_same_industry[:config.TOP_PEERS_COUNT])

        needed = config.TOP_PEERS_COUNT - len(selected_peers)
        if needed > 0:
            selected_peers.extend(pool_same_sector[:needed])

        needed = config.TOP_PEERS_COUNT - len(selected_peers)
        if needed > 0:
            selected_peers.extend(pool_other[:needed])

        if not selected_peers:
            return GMPPredictionResponse(
                target_ipo_id=target_id,
                canonical_name=cname,
                predicted_gmp_pct=None,
                confidence_level="INSUFFICIENT_DATA",
                peers_used_count=0,
                industry_peers_count=0,
                sector_peers_count=0,
                other_peers_count=0,
                top_peers=[],
                summary_message="No historical peer records available.",
            )

        weights = [p.similarity_score for p in selected_peers]
        gmps = [p.historical_gmp_pct for p in selected_peers]
        pred_gmp = round(float(np.average(gmps, weights=weights)), 2)

        industry_count = sum(1 for p in selected_peers if p.is_same_industry)
        sector_count = sum(1 for p in selected_peers if p.is_same_sector and not p.is_same_industry)
        other_count = len(selected_peers) - industry_count - sector_count

        if min(weights) >= 85.0:
            confidence = "HIGH" if (industry_count >= 1 or sector_count >= 2) else "MODERATE"
        else:
            confidence = "MODERATE" if min(weights) >= 75.0 else "LOW"

        msg = (
            f"Predicted GMP: {pred_gmp}% based on {len(selected_peers)} peers "
            f"({industry_count} same-industry, {sector_count} same-sector, {other_count} top-match) "
            f"with similarity {selected_peers[-1].similarity_score}% - {selected_peers[0].similarity_score}%."
        )

        # Real-Time Decision Logic based on live QIB / NII / Total subscription & Predicted GMP
        # Rules:
        # 1. QIB >= 10x & NII >= 6x AND GMP >= 15% -> STRONG_APPLY
        # 2. QIB >= 8x & NII >= 4x AND GMP >= 5% to 15% -> APPLY_FOR_GAIN
        # 3. QIB < 5x or Total < 2x (Any GMP) -> RISK_CAUTION
        # 4. GMP < 0% (Any subscription) -> AVOID
        qib_val = float(target_ipo.get("QIB_sum") or target_ipo.get("qib_sub") or 0.0)
        nii_val = float(target_ipo.get("nii_sum") or target_ipo.get("nii_sub") or 0.0)
        total_sub_val = float(target_ipo.get("total_sub") or 0.0)

        decision_badge = "NEUTRAL_PENDING"
        decision_reason = "Awaiting final bidding demand confirmation."

        if pred_gmp < 0.0:
            decision_badge = "AVOID"
            decision_reason = f"Predicted GMP is negative ({pred_gmp}%). High risk of listing discount."
        elif (qib_val > 0 and qib_val < 5.0) or (total_sub_val > 0 and total_sub_val < 2.0):
            decision_badge = "RISK_CAUTION"
            decision_reason = f"Subdued institutional interest (QIB: {qib_val}x, Total: {total_sub_val}x). Caution advised."
        elif qib_val >= 10.0 and nii_val >= 6.0 and pred_gmp >= 15.0:
            decision_badge = "STRONG_APPLY"
            decision_reason = f"Robust institutional demand (QIB: {qib_val}x, NII: {nii_val}x) with strong GMP (+{pred_gmp}%)."
        elif qib_val >= 8.0 and nii_val >= 4.0 and pred_gmp >= 5.0:
            decision_badge = "APPLY_FOR_GAIN"
            decision_reason = f"Solid institutional demand (QIB: {qib_val}x, NII: {nii_val}x) with positive GMP (+{pred_gmp}%)."
        elif pred_gmp >= 15.0:
            decision_badge = "STRONG_APPLY"
            decision_reason = f"High predicted GMP (+{pred_gmp}%) based on {len(selected_peers)} fundamental peers."
        elif pred_gmp >= 5.0:
            decision_badge = "APPLY_FOR_GAIN"
            decision_reason = f"Favorable predicted GMP (+{pred_gmp}%) from fundamental peer benchmarks."

        return GMPPredictionResponse(
            target_ipo_id=target_id,
            canonical_name=cname,
            predicted_gmp_pct=pred_gmp,
            confidence_level=confidence,
            decision_badge=decision_badge,
            decision_reason=decision_reason,
            peers_used_count=len(selected_peers),
            industry_peers_count=industry_count,
            sector_peers_count=sector_count,
            other_peers_count=other_count,
            top_peers=selected_peers,
            summary_message=msg,
        )
