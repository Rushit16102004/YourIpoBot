"""
Greedy ablation script:
1. Runs full baseline evaluation.
2. Runs Leave-One-Feature-Out (LOFO) ablation to test if dropping a feature REDUCES MAE (misleading) or INCREASES MAE (essential).
3. Produces rank-ordered impact table.
"""

from YourIpoBot.backtest.engine import FastBacktestEngine, CANDIDATE_FEATURES
import time

def run_ablation_study():
    print("=" * 60)
    print("STARTING FULL DATASET BACKTEST & GREEDY FEATURE ABLATION")
    print("=" * 60)
    engine = FastBacktestEngine()

    start_t = time.time()
    print("\n[1/3] Running Full Baseline Benchmark with all candidate features...")
    baseline = engine.run_loocv(active_features=CANDIDATE_FEATURES, threshold=83.0)
    print(f"BASELINE RESULTS:")
    print(f"  Total IPOs Evaluated : {baseline.total_evaluated}")
    print(f"  Baseline MAE        : {baseline.mae}%")
    print(f"  Baseline RMSE       : {baseline.rmse}%")
    print(f"  Directional Accuracy: {baseline.directional_accuracy_pct}%")
    print(f"  Avg Peer Similarity : {baseline.avg_similarity_of_peers}%")
    print(f"  Execution Time      : {round(time.time() - start_t, 2)}s")

    print("\n[2/3] Running Leave-One-Feature-Out (LOFO) Ablation across candidate columns...")
    results = []
    
    # Test each feature individually
    for feat in CANDIDATE_FEATURES:
        t0 = time.time()
        sub_features = [f for f in CANDIDATE_FEATURES if f != feat]
        res = engine.run_loocv(active_features=sub_features, threshold=83.0)
        
        # Delta MAE: if negative, removing this feature IMPROVES accuracy (feature is harmful/noisy)
        # If positive, removing this feature WORSENS accuracy (feature is valuable)
        delta_mae = round(res.mae - baseline.mae, 2)
        delta_dir = round(res.directional_accuracy_pct - baseline.directional_accuracy_pct, 2)
        
        status = "KEEP (Essential)" if delta_mae > 0.05 else ("REMOVE (Misleading/Noisy)" if delta_mae < -0.05 else "NEUTRAL / Low Impact")
        results.append({
            "feature": feat,
            "new_mae": res.mae,
            "delta_mae": delta_mae,
            "new_dir_acc": res.directional_accuracy_pct,
            "delta_dir_acc": delta_dir,
            "status": status,
            "time_sec": round(time.time() - t0, 1)
        })
        print(f"  - Tested without '{feat:28}': MAE={res.mae}% (Δ={delta_mae:+.2f}%) | DirAcc={res.directional_accuracy_pct}% | {status}")

    # Optimal feature set: retain all where delta_mae >= -0.05
    optimal_features = [r["feature"] for r in results if r["status"] != "REMOVE (Misleading/Noisy)"]
    print(f"\n[3/3] Evaluating Optimal Pruned Feature Set ({len(optimal_features)} features)...")
    optimal = engine.run_loocv(active_features=optimal_features, threshold=83.0)
    print(f"OPTIMAL RESULTS (AFTER PRUNING MISLEADING COLUMNS):")
    print(f"  New MAE             : {optimal.mae}% (Improvement: {round(baseline.mae - optimal.mae, 2)}% closer to actual GMP)")
    print(f"  New RMSE            : {optimal.rmse}%")
    print(f"  New Dir Accuracy    : {optimal.directional_accuracy_pct}%")

    # Generate Markdown Summary
    md = [
        "# Quantitative Backtest & Feature Pruning Report",
        f"- **Dataset Evaluated**: {baseline.total_evaluated} Historical IPOs (Outliers >200% & Top/Bottom 5 extremes excluded)",
        f"- **Matching Rule**: 3-Tier Hierarchy (Industry -> Sector -> Cross-Sector Top Match)",
        f"- **Similarity Threshold**: ≥83%",
        "",
        "## 1. Overall Error Distance (Before vs After Pruning)",
        "| Metric | Baseline (All 78 Candidates) | After Pruning Misleading Columns | Net Improvement |",
        "| :--- | :---: | :---: | :---: |",
        f"| **Mean Absolute Error (MAE)** | **{baseline.mae}%** | **{optimal.mae}%** | **-{round(baseline.mae - optimal.mae, 2)}% (Closer to actual GMP)** |",
        f"| **Root Mean Squared Error (RMSE)** | **{baseline.rmse}%** | **{optimal.rmse}%** | **-{round(baseline.rmse - optimal.rmse, 2)}%** |",
        f"| **Directional Accuracy (Gain vs Discount)** | **{baseline.directional_accuracy_pct}%** | **{optimal.directional_accuracy_pct}%** | **{round(optimal.directional_accuracy_pct - baseline.directional_accuracy_pct, 2):+}%** |",
        "",
        "## 2. Feature Importance & Misleading Columns Audit",
        "| Feature / Column | Impact on MAE (Δ) | Directional Acc (Δ) | Classification | Action Recommendation |",
        "| :--- | :---: | :---: | :---: | :--- |",
    ]

    # Sort results by delta_mae descending (most essential to most misleading)
    results_sorted = sorted(results, key=lambda x: x["delta_mae"], reverse=True)
    for r in results_sorted:
        act = "✅ KEEP & PRIORITIZE" if "KEEP" in r["status"] else ("❌ REMOVE (Noise/Misleading)" if "REMOVE" in r["status"] else "⚪ OPTIONAL / NEUTRAL")
        md.append(f"| `{r['feature']}` | `{r['delta_mae']:+.2f}%` | `{r['delta_dir_acc']:+.2f}%` | {r['status']} | {act} |")

    with open(r"YourIpoBot/backtest/backtest_report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print("\nReport written to YourIpoBot/backtest/backtest_report.md")

if __name__ == "__main__":
    run_ablation_study()
