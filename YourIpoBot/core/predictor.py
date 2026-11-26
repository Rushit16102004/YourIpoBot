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
    is_same_sector: bool
    issue_size_cr: Optional[float]
    net_profit_margin_pct: Optional[float]


@dataclass
class GMPPredictionResponse:
    target_ipo_id: str
    canonical_name: str
    predicted_gmp_pct: Optional[float]
    confidence_level: str               # HIGH, MODERATE, LOW, INSUFFICIENT_DATA
    peers_used_count: int
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
        """Calculate weighted composite similarity score between target and candidate."""
        sub_scores = []
        for col in ["total_sub", "QIB_sum", "nii_sum", "retail_sum"]:
            s = self._feature_similarity(target.get(col), candidate.get(col), tolerance_ratio=1.2)
            if s is not None:
                sub_scores.append(s)

        size_scores = []
        for col in ["issue_size_cr", "issue_base_price", "Fresh_Issue_Size_Norm", "OFS_Size_Norm"]:
            v1 = self._normalize_currency_units(target.get(col)) if "size" in col.lower() else target.get(col)
            v2 = self._normalize_currency_units(candidate.get(col)) if "size" in col.lower() else candidate.get(col)
            s = self._feature_similarity(v1, v2, tolerance_ratio=1.5)
            if s is not None:
                size_scores.append(s)

        prof_scores = []
        for col in ["Net_Profit_Margin_Pct", "Operating_Margin_Pct", "Revenue_Current_Norm", "PAT_Norm", "EBITDA_Norm", "EPS"]:
            v1 = self._normalize_currency_units(target.get(col)) if any(k in col for k in ["Revenue", "PAT", "EBITDA"]) else target.get(col)
            v2 = self._normalize_currency_units(candidate.get(col)) if any(k in col for k in ["Revenue", "PAT", "EBITDA"]) else candidate.get(col)
            s = self._feature_similarity(v1, v2, tolerance_ratio=1.8)
            if s is not None:
                prof_scores.append(s)

        debt_scores = []
        for col in ["Debt_to_Equity_Ratio", "Total_Borrowings_Norm", "Total_Book_Value", "BVPS"]:
            v1 = self._normalize_currency_units(target.get(col)) if any(k in col for k in ["Borrowings", "Book_Value"]) else target.get(col)
            v2 = self._normalize_currency_units(candidate.get(col)) if any(k in col for k in ["Borrowings", "Book_Value"]) else candidate.get(col)
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
            "sub": (np.mean(sub_scores) if sub_scores else None, config.WEIGHT_SUBSCRIPTION),
            "size": (np.mean(size_scores) if size_scores else None, config.WEIGHT_ISSUE_SIZE),
            "prof": (np.mean(prof_scores) if prof_scores else None, config.WEIGHT_PROFITABILITY),
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
        Execute Sector-Priority Top-5 selection rule:
        1. Select peers matching >= min_threshold (default 80%) similarity in the same sector.
        2. Fill remaining spots up to 5 from the highest non-sector matches >= min_threshold.
        3. If no peers meet 80%, adaptively fall back to the highest available cohort.
        4. Compute weighted average GMP.
        """
        cname = target_ipo.get("canonical_name", "Unknown IPO")
        target_id = target_ipo.get("ipo_id", "unlisted_ipo")
        target_sec = str(target_ipo.get("sector") or "").lower()

        # Phase 1: Try strict user threshold (>= 80%)
        candidate_matches: list[tuple[float, pd.Series, bool]] = []
        for _, row in self.history_df.iterrows():
            sim = self.compute_similarity(target_ipo, row)
            r_sec = str(row.get("sector") or "").lower()
            is_same = bool((target_sec != "" and target_sec in r_sec) or (r_sec != "" and r_sec in target_sec))
            candidate_matches.append((sim, row, is_same))

        candidate_matches.sort(key=lambda x: x[0], reverse=True)

        # Check if any matches meet the threshold
        qualifying = [item for item in candidate_matches if item[0] >= min_threshold]

        # If zero meet the threshold, fallback gracefully to top 5 available (flagging confidence)
        effective_threshold = min_threshold
        if not qualifying:
            qualifying = candidate_matches[:config.TOP_PEERS_COUNT]
            effective_threshold = qualifying[-1][0] if qualifying else 0.0

        pool_same_sector: list[PeerMatchResult] = []
        pool_other_sector: list[PeerMatchResult] = []

        for sim, row, is_same in qualifying:
            peer = PeerMatchResult(
                ipo_id=str(row.get("ipo_id")),
                canonical_name=str(row.get("canonical_name")),
                ipo_year=int(row.get("ipo_year")) if pd.notna(row.get("ipo_year")) else None,
                sector=str(row.get("sector")) if pd.notna(row.get("sector")) else None,
                industry=str(row.get("industry")) if pd.notna(row.get("industry")) else None,
                similarity_score=sim,
                historical_gmp_pct=float(row.get("GMP_Day3_Clean")),
                is_same_sector=is_same,
                issue_size_cr=float(row.get("issue_size_cr")) if pd.notna(row.get("issue_size_cr")) else None,
                net_profit_margin_pct=float(row.get("Net_Profit_Margin_Pct")) if pd.notna(row.get("Net_Profit_Margin_Pct")) else None,
            )
            if is_same:
                pool_same_sector.append(peer)
            else:
                pool_other_sector.append(peer)

        # Sector Priority Selection
        selected_peers: list[PeerMatchResult] = []
        selected_peers.extend(pool_same_sector[:config.TOP_PEERS_COUNT])
        needed = config.TOP_PEERS_COUNT - len(selected_peers)
        if needed > 0:
            selected_peers.extend(pool_other_sector[:needed])

        if not selected_peers:
            return GMPPredictionResponse(
                target_ipo_id=target_id,
                canonical_name=cname,
                predicted_gmp_pct=None,
                confidence_level="INSUFFICIENT_DATA",
                peers_used_count=0,
                sector_peers_count=0,
                other_peers_count=0,
                top_peers=[],
                summary_message="No historical peer records available.",
            )

        weights = [p.similarity_score for p in selected_peers]
        gmps = [p.historical_gmp_pct for p in selected_peers]
        pred_gmp = round(float(np.average(gmps, weights=weights)), 2)

        sector_count = sum(1 for p in selected_peers if p.is_same_sector)
        other_count = len(selected_peers) - sector_count

        if min(weights) >= 80.0:
            confidence = "HIGH" if sector_count >= 2 else "MODERATE"
        else:
            confidence = "MODERATE" if min(weights) >= 70.0 else "LOW"

        msg = (
            f"Predicted GMP: {pred_gmp}% based on {len(selected_peers)} peers "
            f"({sector_count} same-sector, {other_count} cross-sector) with similarity {selected_peers[-1].similarity_score}% - {selected_peers[0].similarity_score}%."
        )

        return GMPPredictionResponse(
            target_ipo_id=target_id,
            canonical_name=cname,
            predicted_gmp_pct=pred_gmp,
            confidence_level=confidence,
            peers_used_count=len(selected_peers),
            sector_peers_count=sector_count,
            other_peers_count=other_count,
            top_peers=selected_peers,
            summary_message=msg,
        )
