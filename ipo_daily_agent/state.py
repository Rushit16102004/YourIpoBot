"""Persistent state management for data/seen_ipos.json."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ipo_daily_agent import config
from ipo_daily_agent.models import IPOListing, IPOStateRecord


class StateStore:
    """Loads and atomically persists seen IPO processing state."""

    def __init__(self, path: Path = config.SEEN_IPOS_PATH) -> None:
        self.path = path
        self._records: dict[str, IPOStateRecord] = {}
        self.load()

    def load(self) -> None:
        """Load state from disk if present."""
        if not self.path.exists():
            self._records = {}
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._records = {
            sym: IPOStateRecord.model_validate(item) for sym, item in raw.items()
        }

    def save(self) -> None:
        """Write state atomically to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            sym: rec.model_dump(mode="json")
            for sym, rec in sorted(self._records.items())
        }
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def get(self, symbol: str) -> Optional[IPOStateRecord]:
        return self._records.get(symbol.upper())

    def upsert(self, record: IPOStateRecord) -> None:
        record.last_checked_at = datetime.now(timezone.utc).isoformat()
        self._records[record.symbol.upper()] = record

    def all_records(self) -> dict[str, IPOStateRecord]:
        return dict(self._records)

    def needs_fetch_or_extract(
        self, listing: IPOListing, today_iso: str, force: bool = False
    ) -> tuple[bool, bool, bool]:
        """Return (need_rhp, need_anchor, need_rescore) for a live listing.

        Avoids re-downloading or re-parsing unchanged PDFs, while re-checking when:
        - anchor_status is PENDING (e.g., Anchor PDF published T-1 evening), or
        - IPO is Active and live QIB/subscription numbers changed or haven't been
          re-scored today.
        """
        if force:
            return True, True, True

        rec = self.get(listing.symbol)
        if rec is None:
            return True, True, True

        sym_dir = config.symbol_dir(listing.symbol)
        rhp_pdf_exists = (sym_dir / "rhp.pdf").exists()
        rhp_json_exists = (sym_dir / "rhp.json").exists()
        anchor_pdf_exists = (sym_dir / "anchor.pdf").exists()
        anchor_json_exists = (sym_dir / "anchor.json").exists()

        need_rhp = not (rec.rhp_downloaded and rhp_pdf_exists)
        need_rhp_extract = not (rec.rhp_extracted and rhp_json_exists)

        need_anchor = False
        if rec.anchor_status == "PENDING":
            need_anchor = True
        elif rec.anchor_status == "DOWNLOADED" and not anchor_pdf_exists:
            need_anchor = True
        elif rec.anchor_status == "EXTRACTED" and not (
            anchor_pdf_exists and anchor_json_exists
        ):
            need_anchor = True

        qib_changed = listing.subscription.qib_x != rec.last_qib_x
        is_active = listing.status.lower() == "active"
        stale_score_today = is_active and rec.last_scored_date != today_iso

        need_rescore = (
            need_rhp
            or need_rhp_extract
            or (rec.anchor_status == "PENDING" and listing.anchor_url is not None)
            or qib_changed
            or stale_score_today
            or rec.last_decision is None
        )
        return (need_rhp or need_rhp_extract), need_anchor, need_rescore
