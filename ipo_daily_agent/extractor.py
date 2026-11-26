"""Gemini File API structured PDF extractor using strict Pydantic response schemas."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Type, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from ipo_daily_agent import config
from ipo_daily_agent.models import AnchorExtraction, RHPExtraction

logger = logging.getLogger(__name__)
ModelT = TypeVar("ModelT", bound=BaseModel)

RHP_PROMPT = """
You are a precision financial document parser. Search the ENTIRE Red Herring Prospectus (RHP) PDF — including Summary of Offer Document, Summary of Restated Financial Information, Objects of the Offer, Basis for Issue Price, and Restated Financial Statements — and extract ONLY factual values stated in this RHP.
Do NOT perform arithmetic, projections, or scoring.
Rules:
- Convert monetary amounts to INR Crores if reported in INR Millions (divide by 10) or Lakhs (divide by 100), keeping exact reported figures where already in Crores.
- Extract all available historical fiscal years (typically last 3 full fiscal years) from the Summary of Restated Financial Information / Restated Financial Statements: revenue_cr (Revenue from operations), pat_cr (Profit After Tax), operating_cash_flow_cr (Net cash generated from operating activities), eps_basic, eps_diluted.
- If fresh_issue_cr or ofs_cr is not printed in Crores (because price band is not printed inside the RHP), leave those as null and extract what is stated; extract objects_of_issue and top key_risks.
- Extract listed industry peer companies from the 'Basis for Issue Price' section including their NSE ticker symbol (if listed) and P/E multiple as printed in the table.
- If a field is not disclosed, return null.
""".strip()

ANCHOR_PROMPT = """
You are a precision financial document parser. Extract ONLY factual share allocation numbers from this T-1 Anchor Allocation document.
Do NOT compute percentages or scores.
Rules:
- Extract total_anchor_shares and total_anchor_amount_cr.
- Sum shares allotted to Domestic Mutual Funds (domestic_mf_shares) and Domestic Insurance Companies (domestic_insurance_shares) strictly from the table entries.
- Extract FPI shares (fpi_shares) and remaining shares (other_shares), plus anchor_price_inr, anchor_date, anchor_investors_count, and up to 10 marquee investor names.
- If a category is absent or 0, return 0 (or null if the document does not break down categories).
""".strip()


class GeminiExtractor:
    """Uploads PDFs to Gemini File API, extracts strict Pydantic JSON, and cleans up."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key or config.GEMINI_API_KEY).strip()
        if not self.api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Set it in your environment or .env file before running extraction."
            )
        self.client = genai.Client(api_key=self.api_key)

    def _wait_until_active(self, remote_file: Any, timeout_sec: int = 120) -> Any:
        deadline = time.monotonic() + timeout_sec
        current = remote_file
        while getattr(current, "state", None) and getattr(current.state, "name", "") == "PROCESSING":
            if time.monotonic() > deadline:
                raise TimeoutError(f"Gemini file processing timed out: {current.name}")
            time.sleep(2.0)
            current = self.client.files.get(name=current.name)
        state_name = getattr(getattr(current, "state", None), "name", "ACTIVE")
        if state_name == "FAILED":
            raise RuntimeError(f"Gemini file processing failed: {current.name}")
        return current

    def _extract_pdf(
        self,
        pdf_path: Path,
        json_path: Path,
        schema: Type[ModelT],
        prompt: str,
        force: bool = False,
    ) -> ModelT:
        if json_path.exists() and not force:
            return schema.model_validate_json(json_path.read_text(encoding="utf-8"))

        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found for extraction: {pdf_path}")

        uploaded_file = None
        try:
            logger.info("Uploading %s to Gemini File API...", pdf_path.name)
            uploaded_file = self.client.files.upload(file=str(pdf_path))
            uploaded_file = self._wait_until_active(uploaded_file)

            models_to_try = [config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS]
            response = None
            last_exc: Exception | None = None
            for model_name in models_to_try:
                for attempt in range(2):
                    try:
                        response = self.client.models.generate_content(
                            model=model_name,
                            contents=[uploaded_file, prompt],
                            config=types.GenerateContentConfig(
                                temperature=0.0,
                                response_mime_type="application/json",
                                response_schema=schema,
                            ),
                        )
                        if model_name != config.GEMINI_MODEL:
                            logger.info(
                                "Extracted %s using fallback model %s.",
                                pdf_path.name,
                                model_name,
                            )
                        break
                    except Exception as exc:
                        last_exc = exc
                        msg = str(exc)
                        if ("503" in msg or "UNAVAILABLE" in msg) and attempt == 0:
                            logger.warning(
                                "Model %s returned 503 UNAVAILABLE for %s; retrying once...",
                                model_name,
                                pdf_path.name,
                            )
                            time.sleep(3.0)
                            continue
                        if any(
                            code in msg
                            for code in ("429", "RESOURCE_EXHAUSTED", "404", "503", "UNAVAILABLE")
                        ):
                            logger.warning(
                                "Model %s unavailable/quota-limited for %s; trying next fallback.",
                                model_name,
                                pdf_path.name,
                            )
                            break
                        raise
                if response is not None:
                    break

            if response is None:
                raise RuntimeError(
                    f"All Gemini models failed for {pdf_path.name}: {last_exc}"
                )
            raw_text = response.text
            if not raw_text:
                raise RuntimeError(f"Empty response from Gemini for {pdf_path.name}")

            parsed = schema.model_validate_json(raw_text)
            json_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_json = json_path.with_suffix(".json.tmp")
            tmp_json.write_text(
                json.dumps(parsed.model_dump(mode="json"), indent=2),
                encoding="utf-8",
            )
            tmp_json.replace(json_path)
            return parsed
        finally:
            if uploaded_file is not None and getattr(uploaded_file, "name", None):
                try:
                    self.client.files.delete(name=uploaded_file.name)
                except Exception as exc:
                    logger.warning(
                        "Failed to delete remote Gemini file %s: %s",
                        uploaded_file.name,
                        exc,
                    )

    def extract_rhp(
        self, symbol: str, pdf_path: Path, force: bool = False
    ) -> RHPExtraction:
        """Extract structured RHP fields into data/<SYMBOL>/rhp.json."""
        json_path = config.symbol_dir(symbol) / "rhp.json"
        return self._extract_pdf(
            pdf_path=pdf_path,
            json_path=json_path,
            schema=RHPExtraction,
            prompt=RHP_PROMPT,
            force=force,
        )

    def extract_anchor(
        self, symbol: str, pdf_path: Path, force: bool = False
    ) -> AnchorExtraction:
        """Extract structured Anchor Allocation fields into data/<SYMBOL>/anchor.json."""
        json_path = config.symbol_dir(symbol) / "anchor.json"
        return self._extract_pdf(
            pdf_path=pdf_path,
            json_path=json_path,
            schema=AnchorExtraction,
            prompt=ANCHOR_PROMPT,
            force=force,
        )
