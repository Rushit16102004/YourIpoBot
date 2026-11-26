"""Post-listing yfinance market/financial enrichment and optional Gemini RHP debt extraction."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yfinance as yf
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from ipo_history_pipeline import config

logger = logging.getLogger(__name__)

YFINANCE_CACHE_PATH = config.STAGING_DIR / "yfinance_enrichment.json"
RHP_FINANCIALS_CACHE_PATH = config.STAGING_DIR / "rhp_financials.json"

RHP_DEBT_PROMPT = """
Extract ONLY factual restated financial, debt, borrowings, net worth, and cash flow numbers from this Red Herring Prospectus (RHP) PDF.
Do NOT invent, estimate, or calculate missing ratios.
Rules:
- Report monetary values in INR Crores (divide INR Millions by 10, INR Lakhs by 100).
- For each available fiscal year, extract revenue_cr, pat_cr, ebitda_cr, total_debt_cr, long_term_borrowings_cr, short_term_borrowings_cr, net_worth_cr, operating_cash_flow_cr, free_cash_flow_cr, and debt_to_equity (only if explicitly printed).
- Extract pre_ipo_total_debt_cr, pre_ipo_net_worth_cr, and debt_repayment_from_ipo_cr (from Objects of the Issue) if stated.
- Return null for any value not explicitly stated in the RHP.
""".strip()


class RestatedYearDebtMetrics(BaseModel):
    """Single fiscal year restated financials and debt metrics from an RHP."""

    fiscal_year: str
    revenue_cr: Optional[float] = None
    pat_cr: Optional[float] = None
    ebitda_cr: Optional[float] = None
    total_debt_cr: Optional[float] = None
    long_term_borrowings_cr: Optional[float] = None
    short_term_borrowings_cr: Optional[float] = None
    net_worth_cr: Optional[float] = None
    operating_cash_flow_cr: Optional[float] = None
    free_cash_flow_cr: Optional[float] = None
    debt_to_equity: Optional[float] = None


class RHPDebtFinancialsSchema(BaseModel):
    """Strict Pydantic schema for Gemini RHP debt & financial extraction."""

    company_name: str
    symbol: Optional[str] = None
    pre_ipo_total_debt_cr: Optional[float] = None
    pre_ipo_net_worth_cr: Optional[float] = None
    debt_repayment_from_ipo_cr: Optional[float] = None
    fiscal_years: list[RestatedYearDebtMetrics] = Field(default_factory=list)


@dataclass
class TickerEnrichmentTarget:
    """Ticker lookup metadata for a historical IPO."""

    ipo_id: str
    company_name: str
    symbol: Optional[str] = None
    nse_ticker: Optional[str] = None
    bse_ticker: Optional[str] = None
    issue_price: Optional[float] = None
    listing_date: Optional[str] = None


def _clean_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        fval = float(val)
        return None if math.isnan(fval) or math.isinf(fval) else fval
    except (TypeError, ValueError):
        return None


def _clean_int(val: Any) -> Optional[int]:
    fval = _clean_float(val)
    return int(fval) if fval is not None else None


def _build_candidate_tickers(target: TickerEnrichmentTarget) -> list[str]:
    """Build ordered Yahoo Finance candidate symbols (.NS primary, .BO fallback)."""
    candidates: list[str] = []

    def add(sym: Optional[str], suffix: Optional[str] = None) -> None:
        if not sym:
            return
        s = str(sym).strip().upper()
        if not s or s in {"NAN", "NONE", "NULL", "-"}:
            return
        if suffix and not (s.endswith(".NS") or s.endswith(".BO")):
            s = f"{s}{suffix}"
        if s not in candidates:
            candidates.append(s)

    if target.nse_ticker:
        add(target.nse_ticker, ".NS")
    if target.symbol:
        raw_sym = str(target.symbol).strip().upper()
        if raw_sym.endswith(".NS") or raw_sym.endswith(".BO"):
            add(raw_sym)
        elif raw_sym.isdigit():
            add(raw_sym, ".BO")
        else:
            add(raw_sym, ".NS")
            add(raw_sym, ".BO")
    if target.bse_ticker:
        add(target.bse_ticker, ".BO")

    return candidates


def fetch_single_ticker_yfinance(target: TickerEnrichmentTarget) -> dict[str, Any]:
    """Fetch OHLCV history and fundamentals from yfinance with .NS -> .BO fallback."""
    candidates = _build_candidate_tickers(target)
    if not candidates:
        return {
            "ipo_id": target.ipo_id,
            "resolved_ticker": None,
            "status": "no_ticker",
        }

    for ticker_sym in candidates:
        try:
            tk = yf.Ticker(ticker_sym)
            hist = None
            for period in ("max", "5y", "1y", "1mo", "5d"):
                try:
                    candidate_hist = tk.history(period=period, auto_adjust=False)
                    if candidate_hist is not None and not candidate_hist.empty:
                        hist = candidate_hist
                        break
                except Exception:
                    continue
            if hist is None or hist.empty:
                continue

            hist = hist.dropna(subset=["Close"])
            if hist.empty:
                continue

            first_row = hist.iloc[0]
            last_row = hist.iloc[-1]
            listing_trade_date = str(hist.index[0].date())
            latest_trade_date = str(hist.index[-1].date())

            listing_open = _clean_float(first_row.get("Open"))
            listing_high = _clean_float(first_row.get("High"))
            listing_low = _clean_float(first_row.get("Low"))
            listing_close = _clean_float(first_row.get("Close"))
            listing_volume = _clean_int(first_row.get("Volume"))
            latest_close = _clean_float(last_row.get("Close"))

            issue_px = _clean_float(target.issue_price)
            listing_return_pct: Optional[float] = None
            current_return_pct: Optional[float] = None
            if issue_px is not None and issue_px > 0:
                if listing_close is not None:
                    listing_return_pct = round(
                        ((listing_close - issue_px) / issue_px) * 100.0, 2
                    )
                if latest_close is not None:
                    current_return_pct = round(
                        ((latest_close - issue_px) / issue_px) * 100.0, 2
                    )

            info: dict[str, Any] = {}
            try:
                info = tk.info or {}
            except Exception as info_exc:
                logger.debug("yfinance .info unavailable for %s: %s", ticker_sym, info_exc)

            ohlcv_rows = []
            for ts, row in hist.iterrows():
                ohlcv_rows.append(
                    {
                        "trade_date": str(ts.date()),
                        "open": _clean_float(row.get("Open")),
                        "high": _clean_float(row.get("High")),
                        "low": _clean_float(row.get("Low")),
                        "close": _clean_float(row.get("Close")),
                        "volume": _clean_int(row.get("Volume")),
                    }
                )

            return {
                "ipo_id": target.ipo_id,
                "resolved_ticker": ticker_sym,
                "status": "ok",
                "listing_trade_date": listing_trade_date,
                "latest_trade_date": latest_trade_date,
                "listing_open": listing_open,
                "listing_high": listing_high,
                "listing_low": listing_low,
                "listing_close": listing_close,
                "listing_volume": listing_volume,
                "latest_close": latest_close,
                "listing_return_pct": listing_return_pct,
                "current_return_pct": current_return_pct,
                "ohlcv_days": len(ohlcv_rows),
                "ohlcv_sample_json": json.dumps(ohlcv_rows[:5] + ohlcv_rows[-5:])
                if len(ohlcv_rows) > 10
                else json.dumps(ohlcv_rows),
                "market_cap": _clean_float(info.get("marketCap")),
                "trailing_pe": _clean_float(info.get("trailingPE")),
                "forward_pe": _clean_float(info.get("forwardPE")),
                "price_to_book": _clean_float(info.get("priceToBook")),
                "total_debt": _clean_float(info.get("totalDebt")),
                "total_cash": _clean_float(info.get("totalCash")),
                "operating_cashflow": _clean_float(info.get("operatingCashflow")),
                "free_cashflow": _clean_float(info.get("freeCashflow")),
                "debt_to_equity": _clean_float(info.get("debtToEquity")),
                "revenue_growth": _clean_float(info.get("revenueGrowth")),
                "earnings_growth": _clean_float(info.get("earningsGrowth")),
                "return_on_equity": _clean_float(info.get("returnOnEquity")),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
            }
        except Exception as exc:
            logger.debug("yfinance fetch failed for %s: %s", ticker_sym, exc)

    return {
        "ipo_id": target.ipo_id,
        "resolved_ticker": None,
        "status": "not_found",
    }


def enrich_tickers_with_yfinance(
    targets: list[TickerEnrichmentTarget],
    max_tickers: Optional[int] = 50,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    """Enrich listed IPOs via yfinance with local JSON caching."""
    config.ensure_pipeline_dirs()
    cache: dict[str, dict[str, Any]] = {}
    if YFINANCE_CACHE_PATH.exists():
        try:
            cache = json.loads(YFINANCE_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    fetched = 0
    for target in targets:
        if not force and target.ipo_id in cache:
            continue
        if max_tickers is not None and fetched >= max_tickers:
            break
        if not _build_candidate_tickers(target):
            continue

        result = fetch_single_ticker_yfinance(target)
        cache[target.ipo_id] = result
        fetched += 1
        time.sleep(0.2)

    tmp_path = YFINANCE_CACHE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    tmp_path.replace(YFINANCE_CACHE_PATH)
    logger.info(
        "yfinance enrichment complete: %d newly queried, %d total cached.",
        fetched,
        len(cache),
    )
    return cache


def _discover_local_rhp_pdfs() -> list[tuple[str, Path]]:
    """Find local rhp.pdf files under data/<SYMBOL>/rhp.pdf or data/raw/**/*.pdf."""
    found: list[tuple[str, Path]] = []
    if not config.DATA_DIR.exists():
        return found

    for pdf_path in sorted(config.DATA_DIR.glob("*/rhp.pdf")):
        symbol = pdf_path.parent.name.upper()
        found.append((symbol, pdf_path))

    if config.RAW_DIR.exists():
        for pdf_path in sorted(config.RAW_DIR.rglob("*rhp*.pdf")):
            found.append((pdf_path.stem.upper(), pdf_path))

    return found


def extract_rhp_financials(
    pdf_paths: Optional[list[tuple[str, Path]]] = None,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    """Extract debt, borrowings, net worth, and cash flows from local RHP PDFs via Gemini."""
    config.ensure_pipeline_dirs()
    cache: dict[str, dict[str, Any]] = {}
    if RHP_FINANCIALS_CACHE_PATH.exists():
        try:
            cache = json.loads(RHP_FINANCIALS_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    targets = pdf_paths if pdf_paths is not None else _discover_local_rhp_pdfs()
    pending = [
        (key, path)
        for key, path in targets
        if path.exists() and (force or str(path) not in cache)
    ]
    if not pending:
        return cache

    if not config.GEMINI_API_KEY:
        logger.warning(
            "GEMINI_API_KEY not set; skipping Gemini RHP extraction for %d PDF(s).",
            len(pending),
        )
        return cache

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    for key, pdf_path in pending:
        uploaded = None
        try:
            logger.info("Extracting RHP debt/financials from %s via Gemini...", pdf_path)
            uploaded = client.files.upload(file=str(pdf_path))
            while (
                getattr(uploaded, "state", None)
                and getattr(uploaded.state, "name", "") == "PROCESSING"
            ):
                time.sleep(2.0)
                uploaded = client.files.get(name=uploaded.name)

            resp = None
            last_exc: Exception | None = None
            for model_name in (config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS):
                for attempt in range(2):
                    try:
                        resp = client.models.generate_content(
                            model=model_name,
                            contents=[uploaded, RHP_DEBT_PROMPT],
                            config=types.GenerateContentConfig(
                                temperature=0.0,
                                response_mime_type="application/json",
                                response_schema=RHPDebtFinancialsSchema,
                            ),
                        )
                        if model_name != config.GEMINI_MODEL:
                            logger.info(
                                "Extracted RHP financials for %s using fallback model %s.",
                                pdf_path.name,
                                model_name,
                            )
                        break
                    except Exception as mexc:
                        last_exc = mexc
                        msg = str(mexc)
                        if ("503" in msg or "UNAVAILABLE" in msg) and attempt == 0:
                            time.sleep(3.0)
                            continue
                        if any(
                            code in msg
                            for code in ("429", "RESOURCE_EXHAUSTED", "404", "503", "UNAVAILABLE")
                        ):
                            logger.warning(
                                "Model %s unavailable/quota-limited for %s; trying fallback.",
                                model_name,
                                pdf_path.name,
                            )
                            break
                        raise
                if resp is not None:
                    break

            if resp is None or not resp.text:
                raise RuntimeError(
                    f"Empty/failed Gemini response for {pdf_path}: {last_exc}"
                )

            parsed = RHPDebtFinancialsSchema.model_validate_json(resp.text)
            payload = parsed.model_dump(mode="json")
            payload["source_pdf"] = str(pdf_path)
            payload["lookup_key"] = key
            cache[str(pdf_path)] = payload
        except Exception as exc:
            logger.warning("Gemini RHP extraction failed for %s: %s", pdf_path, exc)
        finally:
            if uploaded is not None and getattr(uploaded, "name", None):
                try:
                    client.files.delete(name=uploaded.name)
                except Exception as del_exc:
                    logger.warning("Failed deleting Gemini remote file: %s", del_exc)

    tmp_path = RHP_FINANCIALS_CACHE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    tmp_path.replace(RHP_FINANCIALS_CACHE_PATH)
    return cache
