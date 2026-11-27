"""
Flask Web Application & REST API for YourIpoBot.
Production-ready dashboard for Oracle Cloud (OCI) deployment.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_from_directory

from YourIpoBot import config
from YourIpoBot.core.pipeline import YourIpoBotPipeline
from YourIpoBot.core.storage import get_company_folder, get_connection, register_new_ipo

app = Flask(
    __name__,
    template_folder=str(config.BASE_DIR / "web" / "templates"),
    static_folder=str(config.BASE_DIR / "web" / "static"),
)
pipeline = YourIpoBotPipeline()


@app.route("/")
def index():
    """Main dashboard page."""
    with get_connection() as conn:
        active_ipos = conn.execute(
            """
            SELECT q.ipo_id, q.canonical_name, q.symbol, q.is_sme, q.issue_price_max,
                   q.issue_size_cr, q.issue_open_date, q.issue_close_date,
                   q.rhp_status, q.anchor_status, q.company_dir
            FROM active_ipo_queue q
            ORDER BY q.issue_open_date DESC NULLS LAST;
            """
        ).fetchall()

    # Read latest prediction for each IPO from their company folders
    ipos_list = []
    for r in active_ipos:
        comp_dir_str = r[10]
        comp_folder_name = Path(comp_dir_str).name
        comp_dir = config.COMPANIES_DIR / comp_folder_name
        if not comp_dir.exists():
            comp_dir = Path(comp_dir_str)
        pred_file = comp_dir / "prediction.json"
        pred_data = {}
        if pred_file.exists():
            try:
                with open(pred_file, "r", encoding="utf-8") as f:
                    pred_data = json.load(f)
            except Exception:
                pass

        ipos_list.append(
            {
                "ipo_id": r[0],
                "canonical_name": r[1],
                "symbol": r[2] or "N/A",
                "is_sme": bool(r[3]),
                "board": "SME" if r[3] else "Mainboard",
                "issue_price": r[4],
                "issue_size_cr": r[5],
                "issue_open": r[6] or "TBD",
                "issue_close": r[7] or "TBD",
                "rhp_status": r[8],
                "anchor_status": r[9],
                "predicted_gmp": pred_data.get("predicted_gmp_pct"),
                "confidence": pred_data.get("confidence_level", "TBD"),
                "decision_badge": pred_data.get("decision_badge", "NEUTRAL_PENDING"),
                "decision_reason": pred_data.get("decision_reason", ""),
                "peers_count": pred_data.get("peers_used_count", 0),
                "summary": pred_data.get("summary", "Pending analysis"),
                "top_peers": pred_data.get("top_peers", []),
            }
        )

    # Warehouse metrics
    with get_connection() as conn:
        total_hist = conn.execute("SELECT COUNT(*) FROM ipo_merged_master;").fetchone()[0]

    return render_template("dashboard.html", ipos=ipos_list, total_hist_count=total_hist)


@app.route("/api/ipos", methods=["GET"])
def api_get_ipos():
    """JSON API to fetch all active IPO predictions."""
    with get_connection() as conn:
        rows = conn.execute("SELECT ipo_id, canonical_name, company_dir FROM active_ipo_queue").fetchall()

    out = []
    for ipo_id, cname, comp_dir_str in rows:
        comp_folder_name = Path(comp_dir_str).name
        comp_dir = config.COMPANIES_DIR / comp_folder_name
        if not comp_dir.exists():
            comp_dir = Path(comp_dir_str)
        p_file = comp_dir / "prediction.json"
        if p_file.exists():
            with open(p_file, "r", encoding="utf-8") as f:
                out.append(json.load(f))
    return jsonify(out)


@app.route("/api/run_pipeline", methods=["POST"])
def api_trigger_pipeline():
    """Manual trigger endpoint for immediate GMP refresh."""
    try:
        pipeline.job_day_end_reconciliation()
        return jsonify({"status": "success", "message": "Pipeline completed successfully."})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/files/<path:subpath>")
def download_company_file(subpath):
    """Serve company PDFs safely from isolated directories."""
    file_path = config.COMPANIES_DIR / subpath
    if not file_path.exists():
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(file_path.parent, file_path.name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
