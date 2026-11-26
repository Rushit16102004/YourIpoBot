"""Configuration paths, GitHub source repositories, Gemini settings, and HTTP defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

os.environ.pop("GOOGLE_API_KEY", None)
load_dotenv(PROJECT_ROOT / ".env", override=True)

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
STAGING_DIR = DATA_DIR / "staging"
WAREHOUSE_DIR = DATA_DIR / "warehouse"
WAREHOUSE_DB_PATH = WAREHOUSE_DIR / "ipo_history.db"
DOWNLOAD_MANIFEST_PATH = RAW_DIR / "download_manifest.json"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
)

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_FACTOR = 1.5
GITHUB_API_DELAY_RANGE = (0.4, 0.8)
RAW_DOWNLOAD_DELAY_RANGE = (0.05, 0.15)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

GITHUB_API_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/vnd.github+json",
}

RAW_HTTP_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/plain, application/json, text/csv, application/octet-stream, */*",
}

ALLOWED_DATA_EXTENSIONS: tuple[str, ...] = (
    ".csv",
    ".json",
    ".xlsx",
    ".xls",
    ".parquet",
    ".tsv",
)

# Static TypeScript seed/backtest files that hold structured historical IPO arrays
# (parsed purely as text; never executed).
EXTRA_STRUCTURED_TS_FILES: dict[str, tuple[str, ...]] = {
    "deba608/IPO_Desk": ("src/features/backtest/data/historical-ipos.ts",),
}

# Skip unrelated tooling / non-data paths when mirroring repository data files
SKIP_PATH_SUBSTRINGS: tuple[str, ...] = (
    "node_modules/",
    ".git/",
    "captcha",
    "package-lock.json",
)


@dataclass(frozen=True)
class RepoSource:
    """Public GitHub repository source definition."""

    owner: str
    repo: str
    url: str

    @property
    def slug(self) -> str:
        return f"{self.owner}__{self.repo}"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


GITHUB_REPOS: tuple[RepoSource, ...] = (
    RepoSource(
        owner="Spectrumz00",
        repo="india-ipo-gmp-data",
        url="https://github.com/Spectrumz00/india-ipo-gmp-data",
    ),
    RepoSource(
        owner="saqibsafdar11",
        repo="Predicting-Listing-Gains-in-the-Indian-IPO-Market",
        url="https://github.com/saqibsafdar11/Predicting-Listing-Gains-in-the-Indian-IPO-Market",
    ),
    RepoSource(
        owner="kimkmathews",
        repo="IPO-PerformanceAnalysis",
        url="https://github.com/kimkmathews/IPO-PerformanceAnalysis",
    ),
    RepoSource(
        owner="amansingh2116",
        repo="ipo_analysis",
        url="https://github.com/amansingh2116/ipo_analysis",
    ),
    RepoSource(
        owner="deba608",
        repo="IPO_Desk",
        url="https://github.com/deba608/IPO_Desk",
    ),
    RepoSource(
        owner="DivyGupta4",
        repo="IPO-Quantitative-Analysis-and-Listing-Predictor",
        url="https://github.com/DivyGupta4/IPO-Quantitative-Analysis-and-Listing-Predictor",
    ),
)

# Public Hugging Face datasets with Indian IPO mainboard/SME/GMP/RHP files (no auth required)
HUGGINGFACE_DATASETS: dict[str, tuple[str, ...]] = {
    "sohomghosh/Indian_IPO_datasets": (
        "ipo_mainline_final_data_v18.xlsx",
        "SME_data_final_v11.xlsx",
        "GMP_data/GMP_Mainline_2019_2024YTD_Investorgrain_CLEAN.xlsx",
        "GMP_data/GMP_SME_2019_2024YTD_Investorgrain_CLEAN_v2.xlsx",
    ),
    "sohomghosh/indian_ipo_rating_prediction": (
        "mb_ipo-reviews-final_data.xlsx",
        "sme_ipo-reviews-final_data.xlsx",
    ),
}


def ensure_pipeline_dirs() -> None:
    """Create raw, staging, and warehouse directories if missing."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
