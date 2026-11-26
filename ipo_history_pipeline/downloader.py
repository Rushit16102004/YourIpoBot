"""GitHub repository data file discovery and cached downloader."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ipo_history_pipeline import config
from ipo_history_pipeline.config import RepoSource

logger = logging.getLogger(__name__)


@dataclass
class DownloadedFileRecord:
    """Metadata for a single discovered repository file."""

    repo: str
    remote_path: str
    local_path: str
    status: str  # downloaded | cached | failed
    size_bytes: int = 0
    error: Optional[str] = None


@dataclass
class DownloadSummary:
    """Summary of all repository download attempts."""

    ran_at: str
    total_discovered: int = 0
    downloaded: int = 0
    cached: int = 0
    failed: int = 0
    files: list[DownloadedFileRecord] = field(default_factory=list)
    repo_errors: dict[str, str] = field(default_factory=dict)


class GitHubDataDownloader:
    """Discovers and downloads data files from public GitHub repositories."""

    def __init__(self) -> None:
        self.session = self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=config.MAX_RETRIES,
            read=config.MAX_RETRIES,
            connect=config.MAX_RETRIES,
            backoff_factor=config.BACKOFF_FACTOR,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    @staticmethod
    def _sleep(delay_range: tuple[float, float]) -> None:
        time.sleep(random.uniform(*delay_range))

    @staticmethod
    def _is_relevant_data_file(repo: RepoSource, path: str) -> bool:
        low = path.lower()
        if any(skip in low for skip in config.SKIP_PATH_SUBSTRINGS):
            return False
        extra_ts = config.EXTRA_STRUCTURED_TS_FILES.get(repo.full_name, ())
        if path in extra_ts:
            return True
        return low.endswith(config.ALLOWED_DATA_EXTENSIONS)

    def _discover_via_api(self, repo: RepoSource) -> tuple[str, list[str]]:
        """Use GitHub public REST API to resolve default branch and recursive tree."""
        self._sleep(config.GITHUB_API_DELAY_RANGE)
        repo_resp = self.session.get(
            f"https://api.github.com/repos/{repo.full_name}",
            headers=config.GITHUB_API_HEADERS,
            timeout=config.REQUEST_TIMEOUT,
        )
        repo_resp.raise_for_status()
        default_branch = repo_resp.json().get("default_branch", "main")

        self._sleep(config.GITHUB_API_DELAY_RANGE)
        tree_resp = self.session.get(
            f"https://api.github.com/repos/{repo.full_name}/git/trees/{default_branch}?recursive=1",
            headers=config.GITHUB_API_HEADERS,
            timeout=config.REQUEST_TIMEOUT,
        )
        tree_resp.raise_for_status()
        tree = tree_resp.json().get("tree", [])
        paths = [
            item["path"]
            for item in tree
            if item.get("type") == "blob"
            and self._is_relevant_data_file(repo, item.get("path", ""))
        ]
        return default_branch, paths

    def _discover_via_html(self, repo: RepoSource) -> tuple[str, list[str]]:
        """Fallback HTML discovery of top-level/linked data files if GitHub API limits."""
        for branch in ("main", "master"):
            self._sleep(config.GITHUB_API_DELAY_RANGE)
            resp = self.session.get(
                f"{repo.url}/tree/{branch}",
                headers=config.RAW_HTTP_HEADERS,
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            found: set[str] = set()
            prefix = f"/{repo.full_name}/blob/{branch}/"
            for a in soup.select("a[href]"):
                href = str(a["href"])
                if href.startswith(prefix):
                    rel = href[len(prefix) :]
                    if self._is_relevant_data_file(repo, rel):
                        found.add(rel)
            for extra_path in config.EXTRA_STRUCTURED_TS_FILES.get(repo.full_name, ()):
                found.add(extra_path)
            if found:
                return branch, sorted(found)
        raise RuntimeError(f"Could not discover branch or data files for {repo.full_name}")

    def discover_repo_files(self, repo: RepoSource) -> tuple[str, list[str]]:
        """Discover all data files in a repository via API, falling back to HTML."""
        try:
            return self._discover_via_api(repo)
        except Exception as exc:
            logger.warning(
                "GitHub API discovery failed for %s (%s); trying HTML fallback.",
                repo.full_name,
                exc,
            )
            return self._discover_via_html(repo)

    def _download_raw_file(
        self, repo: RepoSource, branch: str, remote_path: str, dest_path: Path, force: bool
    ) -> DownloadedFileRecord:
        if dest_path.exists() and dest_path.stat().st_size > 0 and not force:
            return DownloadedFileRecord(
                repo=repo.full_name,
                remote_path=remote_path,
                local_path=str(dest_path),
                status="cached",
                size_bytes=dest_path.stat().st_size,
            )

        encoded_path = "/".join(quote(part) for part in remote_path.split("/"))
        raw_url = f"https://raw.githubusercontent.com/{repo.full_name}/{branch}/{encoded_path}"
        self._sleep(config.RAW_DOWNLOAD_DELAY_RANGE)

        try:
            resp = self.session.get(
                raw_url,
                headers=config.RAW_HTTP_HEADERS,
                stream=True,
                timeout=config.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
            with tmp_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(dest_path)
            return DownloadedFileRecord(
                repo=repo.full_name,
                remote_path=remote_path,
                local_path=str(dest_path),
                status="downloaded",
                size_bytes=dest_path.stat().st_size,
            )
        except Exception as exc:
            logger.warning("Failed downloading %s: %s", raw_url, exc)
            return DownloadedFileRecord(
                repo=repo.full_name,
                remote_path=remote_path,
                local_path=str(dest_path),
                status="failed",
                error=str(exc),
            )

    def download_all(
        self,
        repos: tuple[RepoSource, ...] = config.GITHUB_REPOS,
        force: bool = False,
        skip_page_shards: bool = False,
    ) -> DownloadSummary:
        """Download all discovered data files across configured GitHub repositories."""
        config.ensure_pipeline_dirs()
        summary = DownloadSummary(ran_at=datetime.now(timezone.utc).isoformat())

        for repo in repos:
            logger.info("Discovering data files in %s...", repo.full_name)
            try:
                branch, paths = self.discover_repo_files(repo)
            except Exception as exc:
                logger.error("Repository discovery failed for %s: %s", repo.full_name, exc)
                summary.repo_errors[repo.full_name] = str(exc)
                continue

            if skip_page_shards:
                paths = [p for p in paths if not re.search(r"/pages/page_\d+\.csv$", p)]

            logger.info("Found %d data file(s) in %s (%s).", len(paths), repo.full_name, branch)
            for rel_path in paths:
                summary.total_discovered += 1
                dest = config.RAW_DIR / repo.slug / Path(rel_path)
                rec = self._download_raw_file(
                    repo=repo,
                    branch=branch,
                    remote_path=rel_path,
                    dest_path=dest,
                    force=force,
                )
                summary.files.append(rec)
                if rec.status == "downloaded":
                    summary.downloaded += 1
                elif rec.status == "cached":
                    summary.cached += 1
                else:
                    summary.failed += 1

        # Also download configured public Hugging Face datasets
        for ds_id, files in config.HUGGINGFACE_DATASETS.items():
            ds_slug = "hf__" + ds_id.replace("/", "__")
            for rel_path in files:
                summary.total_discovered += 1
                dest = config.RAW_DIR / ds_slug / Path(rel_path)
                rec = self._download_hf_file(
                    dataset_id=ds_id,
                    remote_path=rel_path,
                    dest_path=dest,
                    force=force,
                )
                summary.files.append(rec)
                if rec.status == "downloaded":
                    summary.downloaded += 1
                elif rec.status == "cached":
                    summary.cached += 1
                else:
                    summary.failed += 1

        manifest_tmp = config.DOWNLOAD_MANIFEST_PATH.with_suffix(".json.tmp")
        manifest_tmp.write_text(
            json.dumps(asdict(summary), indent=2), encoding="utf-8"
        )
        manifest_tmp.replace(config.DOWNLOAD_MANIFEST_PATH)
        return summary

    def _download_hf_file(
        self, dataset_id: str, remote_path: str, dest_path: Path, force: bool
    ) -> DownloadedFileRecord:
        if dest_path.exists() and dest_path.stat().st_size > 0 and not force:
            return DownloadedFileRecord(
                repo=f"hf:{dataset_id}",
                remote_path=remote_path,
                local_path=str(dest_path),
                status="cached",
                size_bytes=dest_path.stat().st_size,
            )

        encoded_path = "/".join(quote(part) for part in remote_path.split("/"))
        raw_url = f"https://huggingface.co/datasets/{dataset_id}/resolve/main/{encoded_path}"
        self._sleep(config.RAW_DOWNLOAD_DELAY_RANGE)

        try:
            resp = self.session.get(
                raw_url,
                headers=config.RAW_HTTP_HEADERS,
                stream=True,
                timeout=config.REQUEST_TIMEOUT * 2,
            )
            resp.raise_for_status()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
            with tmp_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(dest_path)
            return DownloadedFileRecord(
                repo=f"hf:{dataset_id}",
                remote_path=remote_path,
                local_path=str(dest_path),
                status="downloaded",
                size_bytes=dest_path.stat().st_size,
            )
        except Exception as exc:
            logger.warning("Failed downloading HF dataset file %s: %s", raw_url, exc)
            return DownloadedFileRecord(
                repo=f"hf:{dataset_id}",
                remote_path=remote_path,
                local_path=str(dest_path),
                status="failed",
                error=str(exc),
            )
