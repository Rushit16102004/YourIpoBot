"""
Vectorized, high-speed Leave-One-Out Cross-Validation (LOOCV) Backtesting Engine.
Computes MAE, RMSE, Directional Accuracy, and tests feature subset ablations.
"""

from __future__ import annotations
import sqlite3
import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple, Optional

# Active feature columns tested for matching
CANDIDATE_FEATURES = [
    # Profitability (35%)
    "Net_Profit_Margin_Pct",
    "Operating_Margin_Pct",
    "Revenue_Growth_YoY_Pct",
    "Is_Profitable",
    "EPS",
    "ROCE_est",
    
    # Issue Size & Liquidity (25%)
    "issue_size_cr",
    "issue_base_price",
    "Fresh_Issue_Size_Norm",
    "OFS_Size_Norm",
    "Post_Issue_Shares_Clean",
    
    # Debt, Cash Flow & Balance Sheet (20%)
    "Debt_to_Equity_Ratio",
    "Total_Borrowings_Norm",
    "Total_Book_Value",
    "BVPS",
    "Cash_Norm",
    "Total_Assets_Norm",
    
    # Governance & Structure (20%)
    "is_sme",
    "sector",
    "industry",
    "Promoter_Holding_Post_Clean",
]

@dataclass
class BacktestMetrics:
    total_evaluated: int
    mae: float
    rmse: float
    directional_accuracy_pct: float
    avg_predicted_gmp: float
    avg_actual_gmp: float
    avg_similarity_of_peers: float

class FastBacktestEngine:
    def __init__(self, db_path: Optional[str] = None):
        p = db_path or r"YourIpoBot/data/warehouse/ipo_history.db"
        conn = sqlite3.connect(p)
        self.df = pd.read_sql("SELECT * FROM ipo_merged_master WHERE GMP_Day3_Clean IS NOT NULL", conn)
        conn.close()

        # Exclude Top 5 positive and Top 5 negative extreme outliers per user specification
        self.df = self.df.sort_values("GMP_Day3_Clean", ascending=True).reset_index(drop=True)
        self.df = self.df.iloc[5:-5].reset_index(drop=True)
        print(f"Loaded {len(self.df)} records for backtesting (after excluding top 5 + and top 5 - extremes).")

        # Pre-process numeric matrices for fast vectorized distances
        self._prepare_matrices()

    def _prepare_matrices(self):
        # Normalize currencies to Cr
        for col in ["issue_size_cr", "Fresh_Issue_Size_Norm", "OFS_Size_Norm", "Total_Borrowings_Norm", "Total_Book_Value", "Cash_Norm", "Total_Assets_Norm"]:
            if col in self.df.columns:
                vals = self.df[col].astype(float).fillna(0.0).values
                # Standardize values > 10M
                vals = np.where(np.abs(vals) > 1e7, vals / 1e7, vals)
                vals = np.where((np.abs(vals) > 1e5) & (np.abs(vals) <= 1e7), vals / 100.0, vals)
                self.df[col] = vals

        self.actual_gmps = self.df["GMP_Day3_Clean"].astype(float).values
        self.sectors = self.df["sector"].astype(str).str.lower().str.strip().values
        self.industries = self.df["industry"].astype(str).str.lower().str.strip().values
        self.is_prof = self.df["Is_Profitable"].fillna(1.0).astype(int).values

    def run_loocv(self, active_features: List[str], threshold: float = 83.0, sample_size: Optional[int] = None) -> BacktestMetrics:
        """Runs fast LOOCV across dataset."""
        df_sub = self.df
        indices = np.arange(len(df_sub))
        if sample_size and sample_size < len(df_sub):
            np.random.seed(42)
            indices = np.random.choice(indices, size=sample_size, replace=False)

        preds = []
        actuals = []
        peer_sims = []

        # Feature groupings
        prof_cols = [c for c in ["Net_Profit_Margin_Pct", "Operating_Margin_Pct", "Revenue_Growth_YoY_Pct", "Is_Profitable", "EPS", "ROCE_est"] if c in active_features and c in df_sub.columns]
        size_cols = [c for c in ["issue_size_cr", "issue_base_price", "Fresh_Issue_Size_Norm", "OFS_Size_Norm", "Post_Issue_Shares_Clean"] if c in active_features and c in df_sub.columns]
        debt_cols = [c for c in ["Debt_to_Equity_Ratio", "Total_Borrowings_Norm", "Total_Book_Value", "BVPS", "Cash_Norm", "Total_Assets_Norm"] if c in active_features and c in df_sub.columns]

        w_prof = 0.35 if prof_cols else 0.0
        w_size = 0.25 if size_cols else 0.0
        w_debt = 0.20 if debt_cols else 0.0
        w_other = 0.20
        total_w = w_prof + w_size + w_debt + w_other

        for idx in indices:
            target_actual_gmp = self.actual_gmps[idx]
            target_sec = self.sectors[idx]
            target_ind = self.industries[idx]
            target_prof = self.is_prof[idx]

            # Candidate pool: all except idx
            cand_mask = np.ones(len(df_sub), dtype=bool)
            cand_mask[idx] = False
            # Hard filter: profitable only with profitable
            cand_mask = cand_mask & (self.is_prof == target_prof)
            cand_indices = np.where(cand_mask)[0]

            if len(cand_indices) == 0:
                continue

            # Compute feature similarities
            def get_group_sim(cols, tol):
                if not cols:
                    return np.zeros(len(cand_indices))
                diffs = []
                for c in cols:
                    v_t = df_sub[c].iloc[idx]
                    if pd.isna(v_t):
                        continue
                    v_c = df_sub[c].iloc[cand_indices].values
                    valid_mask = pd.notna(v_c)
                    denom = np.maximum(np.maximum(np.abs(v_t), np.abs(v_c)), 1e-4)
                    rel_diff = np.abs(v_t - v_c) / denom
                    s = np.clip(1.0 - (rel_diff / (tol * 1.5)), 0.0, 1.0)
                    # For candidates with missing value, assign neutral baseline 0.5
                    s = np.where(valid_mask, s, 0.5)
                    diffs.append(s)
                return np.mean(diffs, axis=0) if diffs else np.full(len(cand_indices), 0.5)

            s_prof = get_group_sim(prof_cols, 1.5)
            s_size = get_group_sim(size_cols, 1.5)
            s_debt = get_group_sim(debt_cols, 1.8)

            # Categorical / other sim
            c_sec = self.sectors[cand_indices]
            c_ind = self.industries[cand_indices]
            same_ind = (target_ind != "") & (target_ind == c_ind)
            same_sec = (target_sec != "") & (target_sec == c_sec)

            s_other = np.where(same_ind, 1.0, np.where(same_sec, 0.75, 0.4))

            # Composite similarity
            comp_sim = ((s_prof * w_prof) + (s_size * w_size) + (s_debt * w_debt) + (s_other * w_other)) / total_w
            comp_sim = comp_sim * 100.0

            # Filter >= threshold
            valid_cand_idx = cand_indices
            valid_sims = comp_sim

            qual_mask = valid_sims >= threshold
            if np.any(qual_mask):
                valid_cand_idx = valid_cand_idx[qual_mask]
                valid_sims = valid_sims[qual_mask]

            # Sort descending
            sort_order = np.argsort(valid_sims)[::-1]
            valid_cand_idx = valid_cand_idx[sort_order]
            valid_sims = valid_sims[sort_order]

            # 3-Tier Hierarchy: same industry -> same sector -> other
            v_ind = self.industries[valid_cand_idx]
            v_sec = self.sectors[valid_cand_idx]

            m_ind = (target_ind != "") & (v_ind == target_ind)
            m_sec = (target_sec != "") & (v_sec == target_sec) & (~m_ind)
            m_other = (~m_ind) & (~m_sec)

            pick_idx = []
            pick_sims = []

            for m in [m_ind, m_sec, m_other]:
                subset_cand = valid_cand_idx[m]
                subset_sim = valid_sims[m]
                needed = 5 - len(pick_idx)
                if needed > 0 and len(subset_cand) > 0:
                    pick_idx.extend(subset_cand[:needed])
                    pick_sims.extend(subset_sim[:needed])

            if len(pick_idx) == 0:
                continue

            # Weighted average GMP of top peers
            peer_gmps = self.actual_gmps[pick_idx]
            pred_gmp = np.average(peer_gmps, weights=pick_sims)

            preds.append(pred_gmp)
            actuals.append(target_actual_gmp)
            peer_sims.append(np.mean(pick_sims))

        preds = np.array(preds)
        actuals = np.array(actuals)
        errors = np.abs(preds - actuals)
        mae = float(np.mean(errors))
        rmse = float(np.sqrt(np.mean((preds - actuals) ** 2)))
        dir_acc = float(np.mean((preds > 0) == (actuals > 0)) * 100.0)

        return BacktestMetrics(
            total_evaluated=len(preds),
            mae=round(mae, 2),
            rmse=round(rmse, 2),
            directional_accuracy_pct=round(dir_acc, 2),
            avg_predicted_gmp=round(float(np.mean(preds)), 2),
            avg_actual_gmp=round(float(np.mean(actuals)), 2),
            avg_similarity_of_peers=round(float(np.mean(peer_sims)), 2),
        )
