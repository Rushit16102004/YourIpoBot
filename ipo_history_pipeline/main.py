"""CLI entry point for the historical Indian IPO data warehouse pipeline."""

from __future__ import annotations

import argparse
import logging
from typing import Optional

from rich.console import Console
from rich.table import Table

from ipo_history_pipeline import config
from ipo_history_pipeline.downloader import DownloadSummary, GitHubDataDownloader
from ipo_history_pipeline.enrichment import (
    enrich_tickers_with_yfinance,
    extract_rhp_financials,
)
from ipo_history_pipeline.merger import (
    MergeSummary,
    build_warehouse,
    extract_yfinance_targets_from_raw,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ipo_history_pipeline")


def _print_download_summary(console: Console, dl: DownloadSummary) -> None:
    table = Table(title="GitHub Repository Download Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="bold")
    table.add_row("Discovered data files", str(dl.total_discovered))
    table.add_row("Newly downloaded", str(dl.downloaded))
    table.add_row("Loaded from cache", str(dl.cached))
    table.add_row("Failed", str(dl.failed))
    table.add_row("Repository errors", str(len(dl.repo_errors)))
    console.print(table)


def _print_merge_summary(console: Console, summary: MergeSummary) -> None:
    counts_table = Table(title=f"SQLite Warehouse Summary ({config.WAREHOUSE_DB_PATH.name})")
    counts_table.add_column("Table", style="cyan")
    counts_table.add_column("Rows", justify="right")
    counts_table.add_column("Non-Null %", justify="right")
    counts_table.add_column("Null %", justify="right")

    for tname, count in summary.table_row_counts.items():
        cov = summary.null_coverage_by_group.get(tname)
        non_null = f"{cov['non_null_pct']:.2f}%" if cov else "-"
        null_pct = f"{cov['null_pct']:.2f}%" if cov else "-"
        counts_table.add_row(tname, str(count), non_null, null_pct)

    console.print(counts_table)
    console.print(
        f"[bold]Master columns preserved:[/bold] {summary.total_master_columns} | "
        f"[bold]Multi-source matched ipo_ids:[/bold] {summary.matched_multi_source_ipos}"
    )


def run_pipeline(
    download_only: bool = False,
    enrich_only: bool = False,
    merge_only: bool = False,
    force: bool = False,
    enrich_limit: Optional[int] = 30,
    skip_page_shards: bool = False,
) -> None:
    """Run selected stages or the full download -> enrich -> merge pipeline."""
    console = Console()
    config.ensure_pipeline_dirs()

    run_all = not (download_only or enrich_only or merge_only)
    do_download = run_all or download_only
    do_enrich = run_all or enrich_only
    do_merge = run_all or merge_only

    if do_download:
        downloader = GitHubDataDownloader()
        dl_summary = downloader.download_all(
            force=force, skip_page_shards=skip_page_shards
        )
        _print_download_summary(console, dl_summary)

    if do_enrich:
        targets = extract_yfinance_targets_from_raw()
        logger.info("Found %d unique ipo_id targets for enrichment.", len(targets))
        limit = None if (enrich_limit is not None and enrich_limit < 0) else enrich_limit
        yf_cache = enrich_tickers_with_yfinance(
            targets=targets, max_tickers=limit, force=force
        )
        rhp_cache = extract_rhp_financials(force=force)
        console.print(
            f"[bold]Enrichment cache:[/bold] yfinance={len(yf_cache)} ipo_ids | "
            f"Gemini RHP PDFs={len(rhp_cache)}"
        )

    if do_merge:
        merge_summary = build_warehouse()
        _print_merge_summary(console, merge_summary)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ipo_history_pipeline.main",
        description="Historical Indian IPO ingestion, enrichment, and SQLite warehouse pipeline.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--download-only",
        action="store_true",
        help="Only discover and download repository data files to data/raw/.",
    )
    mode.add_argument(
        "--enrich-only",
        action="store_true",
        help="Only run yfinance and optional Gemini RHP enrichment into data/staging/.",
    )
    mode.add_argument(
        "--merge-only",
        action="store_true",
        help="Only normalize, merge, and build SQLite warehouse from cached files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download or re-enrichment instead of using local cache.",
    )
    parser.add_argument(
        "--enrich-limit",
        type=int,
        default=30,
        help="Max new tickers to query via yfinance per run (-1 for all). Default: 30.",
    )
    parser.add_argument(
        "--skip-page-shards",
        action="store_true",
        help="Skip downloading redundant per-page CSV shards when full scrape CSV exists.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    run_pipeline(
        download_only=args.download_only,
        enrich_only=args.enrich_only,
        merge_only=args.merge_only,
        force=args.force,
        enrich_limit=args.enrich_limit,
        skip_page_shards=args.skip_page_shards,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
