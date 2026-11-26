"""Public IPO discovery, live subscription fetching, and validated PDF downloading."""

from __future__ import annotations

import logging
import random
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ipo_daily_agent import config
from ipo_daily_agent.models import IPOListing, SubscriptionData

logger = logging.getLogger(__name__)


def _normalize_name(name: str) -> str:
    """Normalize company name for fuzzy cross-source matching."""
    cleaned = re.sub(r"\b(limited|ltd|private|pvt|india|holdings)\b", "", name.lower())
    return re.sub(r"[^a-z0-9]", "", cleaned)


def _parse_float(val: Any) -> Optional[float]:
    """Parse numeric strings such as '12.34', '1,234.5', or '-' safely."""
    if val is None:
        return None
    text = str(val).replace(",", "").strip()
    if not text or text in {"-", "NA", "N/A", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(val: Any) -> Optional[int]:
    num = _parse_float(val)
    return int(num) if num is not None else None


def _parse_nse_date(date_str: Optional[str]) -> datetime:
    """Parse NSE date formats like '26-Nov-2026' or '2026-11-26 00:00:00' for sorting."""
    if not date_str:
        return datetime.max
    for fmt in ("%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return datetime.max


def validate_pdf_file(path: Path, min_bytes: int = config.MIN_PDF_BYTES) -> None:
    """Verify file starts with %PDF magic bytes and exceeds minimum byte size."""
    if not path.exists():
        raise ValueError(f"PDF file missing: {path}")
    size = path.stat().st_size
    if size < min_bytes:
        path.unlink(missing_ok=True)
        raise ValueError(
            f"Downloaded file {path.name} too small ({size} bytes < {min_bytes}); removed."
        )
    with path.open("rb") as fh:
        header = fh.read(len(config.PDF_MAGIC))
    if header != config.PDF_MAGIC:
        path.unlink(missing_ok=True)
        raise ValueError(
            f"Downloaded file {path.name} does not start with %PDF (got {header!r}); removed."
        )


def _extract_pdf_from_zip(zip_path: Path, dest_pdf_path: Path) -> Path:
    """Extract the primary PDF file from a downloaded ZIP archive."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        pdf_members = [
            info
            for info in zf.infolist()
            if not info.is_dir() and info.filename.lower().endswith(".pdf")
        ]
        if not pdf_members:
            raise ValueError(f"ZIP archive {zip_path.name} contains no .pdf files")
        # Prefer largest PDF inside the archive (RHP zips occasionally include cover notes)
        chosen = max(pdf_members, key=lambda info: info.file_size)
        tmp_out = dest_pdf_path.with_suffix(".pdf.tmp")
        with zf.open(chosen, "r") as src, tmp_out.open("wb") as dst:
            while chunk := src.read(65536):
                dst.write(chunk)
        validate_pdf_file(tmp_out)
        tmp_out.replace(dest_pdf_path)
    return dest_pdf_path


class IPOFetcher:
    """Fetches IPO listings, subscription numbers, and RHP/Anchor PDFs from public sources."""

    def __init__(self) -> None:
        self.session = self._build_session()
        self._nse_warmed = False
        self._sebi_rhp_cache: Optional[list[tuple[str, str]]] = None
        self._chittorgarh_cache: Optional[list[tuple[str, str]]] = None

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=config.MAX_RETRIES,
            read=config.MAX_RETRIES,
            connect=config.MAX_RETRIES,
            backoff_factor=config.BACKOFF_FACTOR,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    @staticmethod
    def _polite_delay() -> None:
        time.sleep(random.uniform(config.RATE_LIMIT_MIN_SEC, config.RATE_LIMIT_MAX_SEC))

    def _warm_nse(self) -> None:
        if self._nse_warmed:
            return
        for url in (config.NSE_HOME_URL, config.NSE_IPO_PAGE_URL):
            try:
                self.session.get(
                    url, headers=config.HTML_HEADERS, timeout=config.REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                logger.debug("NSE warmup request failed for %s: %s", url, exc)
            self._polite_delay()
        self._nse_warmed = True

    def _get_json(self, url: str) -> Any:
        self._warm_nse()
        self._polite_delay()
        resp = self.session.get(
            url, headers=config.NSE_API_HEADERS, timeout=config.REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_upcoming_and_open_ipos(
        self, include_sme: bool = True
    ) -> list[IPOListing]:
        """Discover currently open and forthcoming IPOs, sorted nearest first."""
        listings_by_sym: dict[str, IPOListing] = {}

        for url, default_status in (
            (config.NSE_CURRENT_ISSUES_URL, "Active"),
            (config.NSE_UPCOMING_ISSUES_URL, "Forthcoming"),
        ):
            try:
                rows = self._get_json(url)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    symbol = str(row.get("symbol") or "").strip().upper()
                    if not symbol:
                        continue
                    series = str(row.get("series") or row.get("segment") or "Equity").strip()
                    if not include_sme and series.upper() == "SME":
                        continue
                    sub = SubscriptionData(total_x=_parse_float(row.get("noOfTime")))
                    listings_by_sym[symbol] = IPOListing(
                        symbol=symbol,
                        company_name=str(row.get("companyName") or symbol).strip(),
                        series=series,
                        status=str(row.get("status") or default_status).strip(),
                        issue_start_date=row.get("issueStartDate"),
                        issue_end_date=row.get("issueEndDate"),
                        price_range=row.get("issuePrice"),
                        issue_size_shares=_parse_int(row.get("issueSize")),
                        source="NSE",
                        subscription=sub,
                    )
            except Exception as exc:
                logger.warning("NSE endpoint %s failed: %s", url, exc)

        if not listings_by_sym:
            logger.info("Falling back to Chittorgarh IPO list discovery.")
            for item in self._discover_from_chittorgarh(include_sme=include_sme):
                listings_by_sym[item.symbol] = item

        ordered = sorted(
            listings_by_sym.values(),
            key=lambda x: (
                0 if x.status.lower() == "active" else 1,
                _parse_nse_date(x.issue_start_date),
                _parse_nse_date(x.issue_end_date),
                x.symbol,
            ),
        )
        return ordered

    def enrich_ipo_details(self, listing: IPOListing) -> IPOListing:
        """Populate live QIB/NII/Retail subscription, RHP URL, and Anchor URL."""
        try:
            detail = self._get_json(config.NSE_IPO_DETAIL_URL.format(symbol=listing.symbol))
            if isinstance(detail, dict):
                self._parse_nse_subscription(detail, listing)
                self._parse_nse_document_links(detail, listing)
        except Exception as exc:
            logger.warning("NSE ipo-detail failed for %s: %s", listing.symbol, exc)

        if not listing.rhp_url:
            listing.rhp_url = self._discover_rhp_url(listing)

        if not listing.anchor_url:
            listing.anchor_url = self._discover_anchor_url(listing)

        return listing

    @staticmethod
    def _parse_nse_subscription(detail: dict[str, Any], listing: IPOListing) -> None:
        sub = listing.subscription
        bid_rows = detail.get("bidDetails") or []
        active_rows = (detail.get("activeCat") or {}).get("dataList") or []
        update_time = (detail.get("activeCat") or {}).get("updateTime")

        for row in [*bid_rows, *active_rows]:
            cat = str(row.get("category") or "").lower()
            sr_no = str(row.get("srNo") or "").strip()
            times = _parse_float(row.get("noOfTime") or row.get("noOfTotalMeant"))
            if times is None:
                continue
            if sr_no == "1" or "qualified institutional" in cat:
                sub.qib_x = times
            elif sr_no == "2" or "non institutional" in cat and sr_no in {"2", ""}:
                if "more than" not in cat:
                    sub.nii_x = times
            elif sr_no == "3" or "retail individual" in cat:
                sub.retail_x = times
            elif cat == "total" and times > 0:
                sub.total_x = times

        if update_time and "null" not in str(update_time).lower():
            sub.updated_at = str(update_time)

    @staticmethod
    def _extract_href(raw_val: str) -> str:
        if "<a " in raw_val.lower():
            soup = BeautifulSoup(raw_val, "lxml")
            anchor = soup.find("a", href=True)
            if anchor:
                return str(anchor["href"]).strip()
        return raw_val.strip()

    def _parse_nse_document_links(
        self, detail: dict[str, Any], listing: IPOListing
    ) -> None:
        data_list = (detail.get("issueInfo") or {}).get("dataList") or []
        for entry in data_list:
            title = str(entry.get("title") or "").strip().lower()
            value = str(entry.get("value") or "").strip()
            if not value:
                continue
            url = self._extract_href(value)
            if not url.startswith("http"):
                continue

            if "red herring prospectus" in title or (
                "rhp" in title and "addendum" not in title
            ):
                listing.rhp_url = url
            elif "anchor" in title and "pre anchor" not in title and "preanchor" not in url.lower():
                listing.anchor_url = url

    def _probe_url_exists(self, url: str) -> bool:
        """Check with a short streamed GET whether a public archive URL is live (HTTP 200)."""
        self._polite_delay()
        try:
            with self.session.get(
                url,
                headers=config.DOWNLOAD_HEADERS,
                stream=True,
                timeout=config.REQUEST_TIMEOUT,
            ) as resp:
                return resp.status_code == 200
        except requests.RequestException:
            return False

    def _discover_rhp_url(self, listing: IPOListing) -> Optional[str]:
        """Fallback RHP discovery across NSE archives, SEBI Public Issues, and Chittorgarh."""
        nse_zip = config.NSE_ARCHIVE_RHP_URL.format(symbol=listing.symbol)
        if self._probe_url_exists(nse_zip):
            return nse_zip

        sebi_url = self._find_sebi_rhp_url(listing.company_name)
        if sebi_url:
            return sebi_url

        chitto_rhp, _ = self._find_chittorgarh_docs(listing.company_name)
        return chitto_rhp

    def _discover_anchor_url(self, listing: IPOListing) -> Optional[str]:
        """Fallback Anchor Allocation PDF discovery (returns None if not yet published)."""
        for template in (config.NSE_ARCHIVE_ANCHOR_ZIP_URL, config.NSE_ARCHIVE_ANCHOR_PDF_URL):
            candidate = template.format(symbol=listing.symbol)
            if self._probe_url_exists(candidate):
                return candidate

        try:
            circulars = self._get_json(config.NSE_CIRCULARS_URL)
            norm_company = _normalize_name(listing.company_name)
            for item in circulars.get("data", []):
                sub = str(item.get("sub") or "")
                sub_low = sub.lower()
                if "anchor" not in sub_low:
                    continue
                if (
                    listing.symbol.lower() in sub_low
                    or (norm_company and norm_company[:10] in _normalize_name(sub))
                ):
                    link = item.get("circFilelink")
                    if link:
                        return str(link)
        except Exception as exc:
            logger.debug("NSE circulars lookup failed: %s", exc)

        _, chitto_anchor = self._find_chittorgarh_docs(listing.company_name)
        return chitto_anchor

    def _find_sebi_rhp_url(self, company_name: str) -> Optional[str]:
        if self._sebi_rhp_cache is None:
            self._sebi_rhp_cache = []
            try:
                self._polite_delay()
                resp = self.session.get(
                    config.SEBI_RHP_LIST_URL,
                    headers=config.HTML_HEADERS,
                    timeout=config.REQUEST_TIMEOUT,
                )
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "lxml")
                    for a in soup.select("table a[href]"):
                        txt = a.get_text(" ", strip=True)
                        href = str(a["href"])
                        if "rhp" in txt.lower() and "abridged" not in txt.lower():
                            self._sebi_rhp_cache.append((txt, href))
            except Exception as exc:
                logger.debug("SEBI RHP list fetch failed: %s", exc)

        target = _normalize_name(company_name)
        for title, page_url in self._sebi_rhp_cache:
            if target and (target in _normalize_name(title) or _normalize_name(title)[:10] in target):
                return self._resolve_pdf_from_html_page(page_url)
        return None

    def _discover_from_chittorgarh(self, include_sme: bool = True) -> list[IPOListing]:
        items: list[IPOListing] = []
        for title, href in self._load_chittorgarh_list():
            is_sme = "sme" in title.lower()
            if not include_sme and is_sme:
                continue
            slug = href.rstrip("/").split("/")[-2] if "/" in href else title
            sym = re.sub(r"[^A-Z0-9]", "", slug.upper())[:12]
            items.append(
                IPOListing(
                    symbol=sym,
                    company_name=title.replace(" IPO", "").strip(),
                    series="SME" if is_sme else "Equity",
                    status="Forthcoming",
                    source="Chittorgarh",
                )
            )
        return items[:15]

    def _load_chittorgarh_list(self) -> list[tuple[str, str]]:
        if self._chittorgarh_cache is not None:
            return self._chittorgarh_cache
        self._chittorgarh_cache = []
        try:
            self._polite_delay()
            resp = self.session.get(
                config.CHITTORGARH_IPO_LIST_URL,
                headers=config.HTML_HEADERS,
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                for a in soup.select("a[href*='/ipo/']"):
                    txt = a.get_text(" ", strip=True)
                    href = urljoin(config.CHITTORGARH_IPO_LIST_URL, str(a["href"]))
                    if txt and "/ipo/" in href and href.count("/") >= 5:
                        self._chittorgarh_cache.append((txt, href))
        except Exception as exc:
            logger.debug("Chittorgarh list fetch failed: %s", exc)
        return self._chittorgarh_cache

    def _find_chittorgarh_docs(
        self, company_name: str
    ) -> tuple[Optional[str], Optional[str]]:
        target = _normalize_name(company_name)
        matched_url: Optional[str] = None
        for title, href in self._load_chittorgarh_list():
            norm_title = _normalize_name(title.replace("IPO", ""))
            if target and norm_title and (target[:10] in norm_title or norm_title[:10] in target):
                matched_url = href
                break
        if not matched_url:
            return None, None

        rhp_url: Optional[str] = None
        anchor_url: Optional[str] = None
        try:
            self._polite_delay()
            resp = self.session.get(
                matched_url, headers=config.HTML_HEADERS, timeout=config.REQUEST_TIMEOUT
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                for a in soup.select("a[href]"):
                    href = urljoin(matched_url, str(a["href"]))
                    txt = a.get_text(" ", strip=True).lower()
                    href_low = href.lower()
                    if (href_low.endswith(".pdf") or href_low.endswith(".zip")) and "anchor" in (
                        txt + " " + href_low
                    ):
                        anchor_url = anchor_url or href
                    elif (href_low.endswith(".pdf") or href_low.endswith(".zip")) and (
                        "rhp" in txt or "red_herring" in href_low or "rhp" in href_low
                    ):
                        rhp_url = rhp_url or href
        except Exception as exc:
            logger.debug("Chittorgarh IPO page parse failed for %s: %s", matched_url, exc)
        return rhp_url, anchor_url

    def _resolve_pdf_from_html_page(self, page_url: str) -> Optional[str]:
        """Extract embedded PDF URL from an HTML filing page (with optional Playwright fallback)."""
        try:
            self._polite_delay()
            resp = self.session.get(
                page_url, headers=config.HTML_HEADERS, timeout=config.REQUEST_TIMEOUT
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                # SEBI wraps full PDF inside iframe?file=<pdf_url>
                for iframe in soup.select("iframe[src]"):
                    src = str(iframe["src"])
                    if ".pdf" in src.lower():
                        qs = parse_qs(urlparse(src).query)
                        if "file" in qs and qs["file"]:
                            return qs["file"][0]
                        return urljoin(page_url, src)
                for a in soup.select("a[href]"):
                    href = urljoin(page_url, str(a["href"]))
                    if href.lower().endswith(".pdf") and "abridged" not in a.get_text(
                        " ", strip=True
                    ).lower():
                        return href
        except Exception as exc:
            logger.debug("Static HTML PDF discovery failed on %s: %s", page_url, exc)

        return self._find_pdf_via_playwright(page_url)

    @staticmethod
    def _find_pdf_via_playwright(page_url: str) -> Optional[str]:
        """Fallback to headless Playwright only when static HTML lacks the .pdf link."""
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            logger.debug("Playwright not installed; skipping browser PDF fallback.")
            return None

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(page_url, wait_until="networkidle", timeout=30000)
                links = page.eval_on_selector_all(
                    "a[href], iframe[src]",
                    "els => els.map(e => e.href || e.src || '')",
                )
                browser.close()
                for link in links:
                    if ".pdf" in str(link).lower():
                        return str(link)
        except Exception as exc:
            logger.warning("Playwright fallback failed for %s: %s", page_url, exc)
        return None

    def download_pdf(self, url: str, dest_path: Path) -> Path:
        """Download a PDF (or ZIP-wrapped PDF) with validation before caching."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        target_url = url
        if target_url.lower().endswith(".html") or "filings/public-issues" in target_url.lower():
            resolved = self._resolve_pdf_from_html_page(target_url)
            if not resolved:
                raise ValueError(f"Could not resolve a PDF link from page: {url}")
            target_url = resolved

        self._polite_delay()
        headers = dict(config.DOWNLOAD_HEADERS)
        if "sebi.gov.in" in target_url:
            headers["Referer"] = "https://www.sebi.gov.in/"

        tmp_download = dest_path.with_suffix(".download.tmp")
        with self.session.get(
            target_url, headers=headers, stream=True, timeout=config.DOWNLOAD_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            with tmp_download.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)

        try:
            with tmp_download.open("rb") as fh:
                magic = fh.read(4)

            if magic == config.ZIP_MAGIC or target_url.lower().endswith(".zip"):
                _extract_pdf_from_zip(tmp_download, dest_path)
                tmp_download.unlink(missing_ok=True)
            else:
                tmp_pdf = dest_path.with_suffix(".pdf.tmp")
                tmp_download.replace(tmp_pdf)
                validate_pdf_file(tmp_pdf)
                tmp_pdf.replace(dest_path)
        except Exception:
            tmp_download.unlink(missing_ok=True)
            raise

        return dest_path
