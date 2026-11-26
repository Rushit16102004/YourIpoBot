"""Daily CLI runner for ipo_daily_agent."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from ipo_daily_agent import config
from ipo_daily_agent.extractor import GeminiExtractor
from ipo_daily_agent.fetcher import IPOFetcher
from ipo_daily_agent.models import (
    AnchorExtraction,
    AnchorStatus,
    DailyRunItem,
    DailyRunReport,
    IPOListing,
    IPOStateRecord,
    RHPExtraction,
)
from ipo_daily_agent.scorer import score_ipo
from ipo_daily_agent.state import StateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ipo_daily_agent")


def _load_cached_json(path: Path, model_cls: type) -> Optional[object]:
    if not path.exists():
        return None
    return model_cls.model_validate_json(path.read_text(encoding="utf-8"))


def _print_summary(report: DailyRunReport) -> None:
    console = Console()
    table = Table(title=f"IPO Daily Agent Report — {report.run_date}")
    table.add_column("Symbol", style="cyan", no_wrap=True)
    table.add_column("Company")
    table.add_column("Series", no_wrap=True)
    table.add_column("Dates")
    table.add_column("RHP", no_wrap=True)
    table.add_column("Anchor", no_wrap=True)
    table.add_column("Score", justify="right")
    table.add_column("Decision", style="bold")

    for item in report.ipos_processed:
        dates = f"{item.issue_start_date or '?'} -> {item.issue_end_date or '?'}"
        rhp_ok = "YES" if item.rhp_path else "NO"
        score_str = f"{item.scoring.score:.1f}" if item.scoring else "-"
        decision = item.scoring.decision if item.scoring else (item.error or "ERROR")
        table.add_row(
            item.symbol,
            item.company_name[:34],
            item.series,
            dates,
            rhp_ok,
            item.anchor_status,
            score_str,
            decision,
        )

    console.print(table)


def run_daily(
    symbol_filter: Optional[str] = None,
    fetch_only: bool = False,
    force: bool = False,
    include_sme: bool = True,
    limit: Optional[int] = None,
) -> DailyRunReport:
    """Execute one daily pass: discover -> fetch PDFs -> extract -> score -> log."""
    config.ensure_data_dirs()
    now = datetime.now(timezone.utc)
    today_iso = now.date().isoformat()

    fetcher = IPOFetcher()
    state = StateStore()

    listings = fetcher.fetch_upcoming_and_open_ipos(include_sme=include_sme)
    if symbol_filter:
        target = symbol_filter.strip().upper()
        listings = [item for item in listings if item.symbol == target]

    if limit is not None and limit > 0:
        listings = listings[:limit]

    extractor: Optional[GeminiExtractor] = None
    if not fetch_only and config.GEMINI_API_KEY:
        extractor = GeminiExtractor()
    elif not fetch_only:
        logger.warning(
            "GEMINI_API_KEY not set; PDFs will be downloaded & cached, extraction skipped."
        )

    items: list[DailyRunItem] = []

    for listing in listings:
        logger.info("Processing %s (%s)...", listing.symbol, listing.company_name)
        sym_dir = config.symbol_dir(listing.symbol)
        rhp_pdf = sym_dir / "rhp.pdf"
        anchor_pdf = sym_dir / "anchor.pdf"
        rhp_json_path = sym_dir / "rhp.json"
        anchor_json_path = sym_dir / "anchor.json"

        existing = state.get(listing.symbol)
        need_rhp, need_anchor, need_rescore = state.needs_fetch_or_extract(
            listing, today_iso=today_iso, force=force
        )

        # Always refresh live detail when anchor is pending or IPO is active
        if need_rhp or need_anchor or listing.status.lower() == "active":
            listing = fetcher.enrich_ipo_details(listing)

        anchor_status: AnchorStatus = (
            existing.anchor_status if existing else "PENDING"
        )
        item_error: Optional[str] = None

        try:
            # 1. RHP download
            if (not rhp_pdf.exists() or force) and listing.rhp_url:
                logger.info("Downloading RHP for %s from %s", listing.symbol, listing.rhp_url)
                fetcher.download_pdf(listing.rhp_url, rhp_pdf)
            elif not rhp_pdf.exists():
                logger.warning("No RHP URL found yet for %s", listing.symbol)

            # 2. Anchor download (if published; else keep PENDING)
            if (not anchor_pdf.exists() or force) and listing.anchor_url:
                logger.info(
                    "Downloading Anchor PDF for %s from %s",
                    listing.symbol,
                    listing.anchor_url,
                )
                fetcher.download_pdf(listing.anchor_url, anchor_pdf)
                anchor_status = "DOWNLOADED"
            elif anchor_pdf.exists() and anchor_status == "PENDING":
                anchor_status = "DOWNLOADED"

            # 3. Gemini extraction (only when not already cached to JSON)
            rhp_data: Optional[RHPExtraction] = _load_cached_json(
                rhp_json_path, RHPExtraction
            )  # type: ignore[assignment]
            anchor_data: Optional[AnchorExtraction] = _load_cached_json(
                anchor_json_path, AnchorExtraction
            )  # type: ignore[assignment]

            if extractor is not None:
                if rhp_pdf.exists() and (not rhp_json_path.exists() or force):
                    rhp_data = extractor.extract_rhp(listing.symbol, rhp_pdf, force=force)
                if anchor_pdf.exists() and (not anchor_json_path.exists() or force):
                    anchor_data = extractor.extract_anchor(
                        listing.symbol, anchor_pdf, force=force
                    )
                    anchor_status = "EXTRACTED"
                elif anchor_json_path.exists():
                    anchor_status = "EXTRACTED"

            # 4. Deterministic Python scoring
            scoring = None
            if need_rescore or rhp_data is not None:
                scoring = score_ipo(
                    listing=listing,
                    rhp=rhp_data,
                    anchor=anchor_data,
                    anchor_status=anchor_status,
                )

            # 5. Persist state
            state.upsert(
                IPOStateRecord(
                    symbol=listing.symbol,
                    company_name=listing.company_name,
                    series=listing.series,
                    status=listing.status,
                    issue_start_date=listing.issue_start_date,
                    issue_end_date=listing.issue_end_date,
                    rhp_url=listing.rhp_url,
                    anchor_url=listing.anchor_url,
                    rhp_downloaded=rhp_pdf.exists(),
                    rhp_extracted=rhp_json_path.exists(),
                    anchor_status=anchor_status,
                    last_qib_x=listing.subscription.qib_x,
                    last_nii_x=listing.subscription.nii_x,
                    last_retail_x=listing.subscription.retail_x,
                    last_total_x=listing.subscription.total_x,
                    last_score=scoring.score if scoring else None,
                    last_decision=scoring.decision if scoring else None,
                    last_scored_date=today_iso if scoring else None,
                )
            )

            items.append(
                DailyRunItem(
                    symbol=listing.symbol,
                    company_name=listing.company_name,
                    series=listing.series,
                    status=listing.status,
                    issue_start_date=listing.issue_start_date,
                    issue_end_date=listing.issue_end_date,
                    rhp_path=str(rhp_pdf) if rhp_pdf.exists() else None,
                    anchor_path=str(anchor_pdf) if anchor_pdf.exists() else None,
                    anchor_status=anchor_status,
                    scoring=scoring,
                )
            )
        except Exception as exc:
            logger.exception("Error processing %s: %s", listing.symbol, exc)
            item_error = str(exc)
            items.append(
                DailyRunItem(
                    symbol=listing.symbol,
                    company_name=listing.company_name,
                    series=listing.series,
                    status=listing.status,
                    issue_start_date=listing.issue_start_date,
                    issue_end_date=listing.issue_end_date,
                    rhp_path=str(rhp_pdf) if rhp_pdf.exists() else None,
                    anchor_path=str(anchor_pdf) if anchor_pdf.exists() else None,
                    anchor_status=anchor_status,
                    error=item_error,
                )
            )

    state.save()

    report = DailyRunReport(
        run_date=today_iso,
        ran_at=now.isoformat(),
        ipos_processed=items,
    )
    run_log_path = config.RUNS_DIR / f"{today_iso}.json"
    run_log_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )

    _print_summary(report)
    logger.info("Saved daily run log to %s", run_log_path)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ipo_daily_agent.main",
        description="Free-tier daily Indian IPO RHP & Anchor analysis agent.",
    )
    parser.add_argument("--symbol", help="Process only a specific NSE IPO symbol.")
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Download PDFs and live subscription data without calling Gemini.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download PDFs and re-run extraction even if cached.",
    )
    parser.add_argument(
        "--mainboard-only",
        action="store_true",
        help="Skip SME issues and process Mainboard Equity IPOs only.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N nearest IPOs.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    run_daily(
        symbol_filter=args.symbol,
        fetch_only=args.fetch_only,
        force=args.force,
        include_sme=not args.mainboard_only,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
