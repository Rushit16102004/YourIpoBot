"""Unit tests for deterministic scoring, state caching, and PDF/ZIP validation."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ipo_daily_agent.fetcher import _extract_pdf_from_zip, validate_pdf_file
from ipo_daily_agent.models import (
    AnchorExtraction,
    FinancialYearMetrics,
    IPOListing,
    PeerCompany,
    RHPExtraction,
    SubscriptionData,
)
from ipo_daily_agent.scorer import score_ipo
from ipo_daily_agent.state import StateStore


def test_validate_pdf_rejects_html_and_small_files(tmp_path: Path) -> None:
    bad_html = tmp_path / "fake.pdf"
    bad_html.write_bytes(b"<html>503 Service Unavailable</html>" * 500)
    with pytest.raises(ValueError, match="does not start with %PDF"):
        validate_pdf_file(bad_html)
    assert not bad_html.exists()

    tiny_pdf = tmp_path / "tiny.pdf"
    tiny_pdf.write_bytes(b"%PDF-1.7\nshort")
    with pytest.raises(ValueError, match="too small"):
        validate_pdf_file(tiny_pdf)
    assert not tiny_pdf.exists()


def test_extract_pdf_from_zip_picks_largest_valid_pdf(tmp_path: Path) -> None:
    zip_path = tmp_path / "RHP_TEST.zip"
    pdf_out = tmp_path / "rhp.pdf"
    valid_bytes = b"%PDF-1.7\n" + (b"0" * 15000)
    cover_bytes = b"%PDF-1.4\n" + (b"1" * 11000)

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("cover.pdf", cover_bytes)
        zf.writestr("main_rhp.pdf", valid_bytes)

    _extract_pdf_from_zip(zip_path, pdf_out)
    assert pdf_out.exists()
    assert pdf_out.read_bytes() == valid_bytes


def test_deterministic_scoring_apply_case() -> None:
    listing = IPOListing(
        symbol="GOODIPO",
        company_name="Good IPO Ltd",
        status="Active",
        subscription=SubscriptionData(qib_x=22.5, nii_x=15.0, retail_x=8.0, total_x=14.2),
    )
    rhp = RHPExtraction(
        company_name="Good IPO Ltd",
        price_band_high_inr=200.0,
        fresh_issue_cr=700.0,
        ofs_cr=300.0,
        upper_band_eps_inr=10.0,  # P/E = 20.0
        financial_years=[
            FinancialYearMetrics(
                fiscal_year="FY24", revenue_cr=500.0, pat_cr=40.0, operating_cash_flow_cr=45.0
            ),
            FinancialYearMetrics(
                fiscal_year="FY25", revenue_cr=650.0, pat_cr=58.0, operating_cash_flow_cr=60.0
            ),
            FinancialYearMetrics(
                fiscal_year="FY26", revenue_cr=850.0, pat_cr=80.0, operating_cash_flow_cr=85.0
            ),
        ],
        peer_companies=[PeerCompany(name="Peer A", reported_pe=28.0)],
    )
    anchor = AnchorExtraction(
        company_name="Good IPO Ltd",
        total_anchor_shares=1_000_000,
        domestic_mf_shares=450_000,
        domestic_insurance_shares=150_000,  # 60% Tier-1
    )

    res = score_ipo(
        listing=listing,
        rhp=rhp,
        anchor=anchor,
        anchor_status="EXTRACTED",
        peer_median_pe_override=28.0,
    )
    assert res.metrics.fresh_issue_pct == 70.0
    assert res.metrics.ipo_upper_pe == 20.0
    assert res.metrics.tier1_anchor_share_pct == 60.0
    assert res.score >= 85.0
    assert res.decision == "APPLY"
    assert not res.hard_skip_reasons


def test_deterministic_scoring_watch_when_anchor_pending() -> None:
    listing = IPOListing(
        symbol="WAITIPO",
        company_name="Wait IPO Ltd",
        status="Forthcoming",
        subscription=SubscriptionData(qib_x=None),
    )
    rhp = RHPExtraction(
        company_name="Wait IPO Ltd",
        price_band_high_inr=191.0,
        fresh_issue_cr=192.0,
        ofs_cr=168.75,
        upper_band_eps_inr=7.5,
        financial_years=[
            FinancialYearMetrics(
                fiscal_year="FY24", revenue_cr=300.0, pat_cr=20.0, operating_cash_flow_cr=25.0
            ),
            FinancialYearMetrics(
                fiscal_year="FY25", revenue_cr=420.0, pat_cr=32.0, operating_cash_flow_cr=35.0
            ),
        ],
    )

    res = score_ipo(
        listing=listing,
        rhp=rhp,
        anchor=None,
        anchor_status="PENDING",
        peer_median_pe_override=26.0,
    )
    assert res.decision == "WATCH / CONFIRM DAY-3 QIB"


def test_hard_skip_on_loss_making_latest_fy() -> None:
    listing = IPOListing(symbol="LOSSIPO", company_name="Loss IPO Ltd")
    rhp = RHPExtraction(
        company_name="Loss IPO Ltd",
        price_band_high_inr=100.0,
        fresh_issue_cr=100.0,
        ofs_cr=0.0,
        upper_band_eps_inr=-2.0,
        financial_years=[
            FinancialYearMetrics(
                fiscal_year="FY25", revenue_cr=120.0, pat_cr=-10.0, operating_cash_flow_cr=-5.0
            )
        ],
    )
    res = score_ipo(listing=listing, rhp=rhp, anchor=None, anchor_status="PENDING")
    assert res.decision == "SKIP"
    assert len(res.hard_skip_reasons) >= 1


def test_state_store_rechecks_pending_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ipo_daily_agent import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    sym_dir = tmp_path / "data" / "FOODLINK"
    sym_dir.mkdir(parents=True)
    (sym_dir / "rhp.pdf").write_bytes(b"%PDF-1.7\n" + b"0" * 12000)
    (sym_dir / "rhp.json").write_text("{}", encoding="utf-8")

    store = StateStore(path=tmp_path / "seen_ipos.json")
    listing = IPOListing(symbol="FOODLINK", company_name="Foodlink F&B Holdings", status="Forthcoming")
    from ipo_daily_agent.models import IPOStateRecord

    store.upsert(
        IPOStateRecord(
            symbol="FOODLINK",
            company_name="Foodlink F&B Holdings",
            rhp_downloaded=True,
            rhp_extracted=True,
            anchor_status="PENDING",
            last_decision="WATCH / CONFIRM DAY-3 QIB",
        )
    )
    store.save()

    reloaded = StateStore(path=tmp_path / "seen_ipos.json")
    need_rhp, need_anchor, _ = reloaded.needs_fetch_or_extract(listing, today_iso="2026-11-25")
    assert need_rhp is False
    assert need_anchor is True
