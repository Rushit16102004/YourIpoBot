"""
Comprehensive Day 3 (03:00 PM - Closing) Backtest Suite.
Evaluates the full combined quantitative decision pipeline:
Fundamental k-NN GMP Prediction + Day 3 3pm Institutional Bidding Confirmation (QIB & NII).
"""

from __future__ import annotations
import sqlite3
import numpy as np
import pandas as pd
from typing import Dict, Any, List

from YourIpoBot.backtest.engine import FastBacktestEngine, CANDIDATE_FEATURES

def run_day3_3pm_backtest():
    print("=" * 70)
    print("RUNNING DAY 3 (03:00 PM) CLOSING SUBSCRIPTION BACKTEST")
    print("=" * 70)

    engine = FastBacktestEngine()
    df = engine.df

    # Extract Day 3 3pm signals and actual listing gains
    qib = pd.to_numeric(df['QIB_sum'], errors='coerce').fillna(0.0).values
    nii = pd.to_numeric(df['nii_sum'], errors='coerce').fillna(0.0).values
    total_sub = pd.to_numeric(df['total_sub'], errors='coerce').fillna(0.0).values
    actual_gmps = engine.actual_gmps

    # Actual listing gain column if available, else actual_gmps
    listing_gain_raw = pd.to_numeric(df.get('~str_listing_gain_in_per'), errors='coerce')
    # Where listing gain is present use it, otherwise use actual GMP
    actual_returns = np.where(listing_gain_raw.notna(), listing_gain_raw.values, actual_gmps)

    print(f"Total Cohort: {len(df)} IPOs")

    # 1. Run LOOCV to obtain fundamental predicted GMP for every stock
    print("Generating Fundamental k-NN Predictions using pruned core features...")
    prof_cols = ['Net_Profit_Margin_Pct', 'Revenue_Growth_YoY_Pct', 'Is_Profitable', 'EPS', 'ROCE_est']
    size_cols = ['issue_base_price', 'issue_size_cr']
    debt_cols = ['Debt_to_Equity_Ratio', 'Total_Borrowings_Norm']

    predicted_gmps = []
    peer_similarities = []

    for idx in range(len(df)):
        target_prof = engine.is_prof[idx]
        cand_mask = np.ones(len(df), dtype=bool)
        cand_mask[idx] = False
        cand_mask = cand_mask & (engine.is_prof == target_prof)
        cand_indices = np.where(cand_mask)[0]

        if len(cand_indices) == 0:
            predicted_gmps.append(0.0)
            peer_similarities.append(50.0)
            continue

        def get_group_sim(cols, tol):
            diffs = []
            for c in cols:
                v_t = df[c].iloc[idx]
                if pd.isna(v_t): continue
                v_c = df[c].iloc[cand_indices].values
                valid_mask = pd.notna(v_c)
                denom = np.maximum(np.maximum(np.abs(v_t), np.abs(v_c)), 1e-4)
                rel_diff = np.abs(v_t - v_c) / denom
                s = np.clip(1.0 - (rel_diff / (tol * 1.5)), 0.0, 1.0)
                s = np.where(valid_mask, s, 0.5)
                diffs.append(s)
            return np.mean(diffs, axis=0) if diffs else np.full(len(cand_indices), 0.5)

        s_prof = get_group_sim(prof_cols, 1.5)
        s_size = get_group_sim(size_cols, 1.5)
        s_debt = get_group_sim(debt_cols, 1.8)

        c_sec = engine.sectors[cand_indices]
        c_ind = engine.industries[cand_indices]
        same_ind = (engine.industries[idx] != '') & (engine.industries[idx] == c_ind)
        same_sec = (engine.sectors[idx] != '') & (engine.sectors[idx] == c_sec)
        s_other = np.where(same_ind, 1.0, np.where(same_sec, 0.75, 0.4))

        comp_sim = ((s_prof * 0.35) + (s_size * 0.25) + (s_debt * 0.20) + (s_other * 0.20)) * 100.0
        qual = comp_sim >= 83.0
        valid_cand = cand_indices[qual] if np.any(qual) else cand_indices
        valid_sims = comp_sim[qual] if np.any(qual) else comp_sim

        order = np.argsort(valid_sims)[::-1][:5]
        valid_cand = valid_cand[order]
        valid_sims = valid_sims[order]

        pred = np.average(engine.actual_gmps[valid_cand], weights=valid_sims)
        predicted_gmps.append(pred)
        peer_similarities.append(np.mean(valid_sims))

    predicted_gmps = np.array(predicted_gmps)
    peer_similarities = np.array(peer_similarities)

    # 2. Apply Day 3 3pm Decision Matrix
    # Categories:
    # 1. STRONG_APPLY: QIB >= 10x & NII >= 6x & Pred GMP >= 15%
    # 2. APPLY_FOR_GAIN: QIB >= 8x & NII >= 4x & Pred GMP >= 5% (not strong apply)
    # 3. RISK_CAUTION: QIB < 5x or Total < 2x (where subscription is known)
    # 4. AVOID: Pred GMP < 0%
    # 5. NEUTRAL: Others

    strong_apply_mask = (predicted_gmps >= 15.0) & (qib >= 10.0) & (nii >= 6.0)
    apply_gain_mask = (~strong_apply_mask) & (predicted_gmps >= 5.0) & (qib >= 8.0) & (nii >= 4.0)
    
    # Subdued demand filter (Day 3 3pm)
    caution_mask = (~strong_apply_mask) & (~apply_gain_mask) & ((qib > 0) & (qib < 5.0) | ((total_sub > 0) & (total_sub < 2.0)))
    avoid_mask = (predicted_gmps < 0.0)

    # Performance per bucket
    def analyze_bucket(name: str, mask: np.ndarray):
        count = int(np.sum(mask))
        if count == 0:
            print(f"--- {name}: 0 IPOs ---")
            return
        returns = actual_returns[mask]
        pos_count = int(np.sum(returns > 0))
        flat_count = int(np.sum(returns == 0))
        neg_count = int(np.sum(returns < 0))
        win_rate = (pos_count / count) * 100.0
        loss_rate = (neg_count / count) * 100.0
        avg_ret = float(np.mean(returns))
        median_ret = float(np.median(returns))
        max_gain = float(np.max(returns))
        max_loss = float(np.min(returns))

        print(f"\n[{name.upper()}] (Total: {count} IPOs)")
        print(f"  Win Rate (Gain > 0%)       : {win_rate:.2f}% ({pos_count}/{count} IPOs)")
        print(f"  Breakeven (Gain == 0%)     : {(flat_count / count) * 100:.2f}% ({flat_count}/{count} IPOs)")
        print(f"  Loss Rate (Loss < 0%)      : {loss_rate:.2f}% ({neg_count}/{count} IPOs) -> {'TARGET <10% ACHIEVED' if loss_rate < 10 else 'ATTENTION'}")
        print(f"  Average Return on Listing  : +{avg_ret:.2f}%")
        print(f"  Median Return on Listing   : +{median_ret:.2f}%")
        print(f"  Best Performer             : +{max_gain:.2f}%")
        print(f"  Worst Drawdown             : {max_loss:.2f}%")

    print("\n" + "=" * 70)
    print("RESULTS AFTER 3RD DAY 03:00 PM INSTITUTIONAL CONFIRMATION")
    print("=" * 70)

    analyze_bucket("Tier 1: STRONG APPLY (QIB >= 10x & NII >= 6x & Pred GMP >= 15%)", strong_apply_mask)
    analyze_bucket("Tier 2: APPLY FOR GAIN (QIB >= 8x & NII >= 4x & Pred GMP >= 5%)", apply_gain_mask)
    
    # Combined recommended actionable universe (All recommended applies)
    all_recommended_mask = strong_apply_mask | apply_gain_mask
    analyze_bucket("ALL RECOMMENDED APPLIES COMBINED (Tier 1 + Tier 2)", all_recommended_mask)

    analyze_bucket("Tier 3: RISK CAUTION / WEAK DEMAND (QIB < 5x or Total < 2x)", caution_mask)
    analyze_bucket("Tier 4: AVOID (Negative Predicted GMP < 0%)", avoid_mask)

    # General baseline (all stocks without filter)
    print("\n" + "=" * 70)
    print("BASELINE UNFILTERED BENCHMARK (All 3,554 IPOs)")
    print("=" * 70)
    baseline_losses = int(np.sum(actual_returns < 0))
    print(f"  Unfiltered Loss Rate       : {(baseline_losses / len(df)) * 100:.2f}% ({baseline_losses}/{len(df)} had losses)")
    print(f"  Unfiltered Win Rate        : {(np.sum(actual_returns > 0) / len(df)) * 100:.2f}%")
    print(f"  Unfiltered Average Return  : +{np.mean(actual_returns):.2f}%")

if __name__ == "__main__":
    run_day3_3pm_backtest()
