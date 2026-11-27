# Quantitative Backtest & Feature Pruning Report
- **Dataset Evaluated**: 3554 Historical IPOs (Outliers >200% & Top/Bottom 5 extremes excluded)
- **Matching Rule**: 3-Tier Hierarchy (Industry -> Sector -> Cross-Sector Top Match)
- **Similarity Threshold**: ≥83%

## 1. Overall Error Distance (Before vs After Pruning)
| Metric | Baseline (All 78 Candidates) | After Pruning Misleading Columns | Net Improvement |
| :--- | :---: | :---: | :---: |
| **Mean Absolute Error (MAE)** | **26.95%** | **26.86%** | **-0.09% (Closer to actual GMP)** |
| **Root Mean Squared Error (RMSE)** | **38.09%** | **37.98%** | **-0.11%** |
| **Directional Accuracy (Gain vs Discount)** | **70.37%** | **70.46%** | **+0.09%** |

## 2. Feature Importance & Misleading Columns Audit
| Feature / Column | Impact on MAE (Δ) | Directional Acc (Δ) | Classification | Action Recommendation |
| :--- | :---: | :---: | :---: | :--- |
| `issue_base_price` | `+3.14%` | `-6.53%` | KEEP (Essential) | ✅ KEEP & PRIORITIZE |
| `Total_Borrowings_Norm` | `+0.15%` | `-0.34%` | KEEP (Essential) | ✅ KEEP & PRIORITIZE |
| `Is_Profitable` | `+0.14%` | `+0.09%` | KEEP (Essential) | ✅ KEEP & PRIORITIZE |
| `Cash_Norm` | `+0.13%` | `+0.09%` | KEEP (Essential) | ✅ KEEP & PRIORITIZE |
| `Revenue_Growth_YoY_Pct` | `+0.11%` | `+0.17%` | KEEP (Essential) | ✅ KEEP & PRIORITIZE |
| `Total_Assets_Norm` | `+0.08%` | `+0.34%` | KEEP (Essential) | ✅ KEEP & PRIORITIZE |
| `ROCE_est` | `+0.04%` | `-0.20%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `BVPS` | `+0.04%` | `-0.05%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `EPS` | `+0.03%` | `+0.09%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `issue_size_cr` | `+0.03%` | `+0.62%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `Debt_to_Equity_Ratio` | `+0.02%` | `+0.06%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `Total_Book_Value` | `+0.01%` | `-0.14%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `Operating_Margin_Pct` | `+0.00%` | `-0.05%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `is_sme` | `+0.00%` | `+0.00%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `sector` | `+0.00%` | `+0.00%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `industry` | `+0.00%` | `+0.00%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `Promoter_Holding_Post_Clean` | `+0.00%` | `+0.00%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `Post_Issue_Shares_Clean` | `-0.01%` | `-0.14%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `Net_Profit_Margin_Pct` | `-0.05%` | `-0.03%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `Fresh_Issue_Size_Norm` | `-0.05%` | `+0.45%` | NEUTRAL / Low Impact | ⚪ OPTIONAL / NEUTRAL |
| `OFS_Size_Norm` | `-0.09%` | `+0.09%` | REMOVE (Misleading/Noisy) | ❌ REMOVE (Noise/Misleading) |