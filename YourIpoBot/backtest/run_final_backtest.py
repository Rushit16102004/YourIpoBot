"""
Final Backtest Verification Script:
Evaluates all 3,554 IPOs with the newly enriched dataset (27,292 cells filled),
OFS pruned, pure percentage profit matching, and issue size bracketing.
"""

from YourIpoBot.backtest.engine import FastBacktestEngine
import numpy as np
import time

def run_final_backtest():
    print("=" * 65)
    print("RUNNING FINAL FULL LOOCV BACKTEST ACROSS ALL HISTORICAL IPOS")
    print("=" * 65)

    engine = FastBacktestEngine()
    total_records = len(engine.df)
    print(f"Total Cohort Evaluated: {total_records} IPOs (excluding top 5 + and top 5 - extremes)")

    # Active pruned feature list
    active_features = [
        "Net_Profit_Margin_Pct",
        "Operating_Margin_Pct",
        "Revenue_Growth_YoY_Pct",
        "Is_Profitable",
        "EPS",
        "ROCE_est",
        "issue_size_cr",
        "issue_base_price",
        "Fresh_Issue_Size_Norm",
        "Debt_to_Equity_Ratio",
        "Total_Borrowings_Norm",
        "Total_Book_Value",
        "BVPS",
        "Cash_Norm",
        "Total_Assets_Norm",
        "is_sme",
        "sector",
        "industry",
    ]

    t0 = time.time()
    results = engine.run_loocv(active_features=active_features, threshold=83.0)
    elapsed = round(time.time() - t0, 2)

    print("\n" + "=" * 65)
    print("FINAL BACKTEST QUANTITATIVE PERFORMANCE METRICS")
    print("=" * 65)
    print(f"  Total Evaluated Stocks     : {results.total_evaluated} / {total_records}")
    print(f"  Mean Absolute Error (MAE)  : {results.mae}%")
    print(f"  Root Mean Squared Error    : {results.rmse}%")
    print(f"  Directional Accuracy       : {results.directional_accuracy_pct}%")
    print(f"  Average Predicted GMP      : {results.avg_predicted_gmp}%")
    print(f"  Average Actual GMP         : {results.avg_actual_gmp}%")
    print(f"  Average Peer Similarity    : {results.avg_similarity_of_peers}%")
    print(f"  Total Computation Time     : {elapsed} seconds")
    print("=" * 65)

if __name__ == "__main__":
    run_final_backtest()
