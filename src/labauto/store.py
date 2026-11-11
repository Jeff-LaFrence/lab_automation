"""The Store slot: where results and records live.

LocalStore writes CSV and JSON under campaigns/. A LIMS or ELN adapter would
implement the same three methods. None is built.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Protocol


class Store(Protocol):
    """Interface for keeping campaign results and records."""

    def append_results(self, campaign_id: str, rows: list[dict]) -> None: ...

    def load_results(self, campaign_id: str) -> list[dict]: ...

    def save_record(self, campaign_id: str, name: str, data: object) -> Path: ...


class LocalStore:
    """Results in campaigns/<id>/results_all.csv, records as JSON files beside it."""

    def __init__(self, base: str | Path = "campaigns"):
        self.base = Path(base)

    def folder(self, campaign_id: str) -> Path:
        path = self.base / campaign_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def append_results(self, campaign_id: str, rows: list[dict]) -> None:
        """Append rows to results_all.csv. The header comes from the first rows written."""
        if not rows:
            return
        path = self.folder(campaign_id) / "results_all.csv"
        exists = path.exists()
        fields = list(rows[0])
        if exists:
            with open(path, encoding="utf-8") as fh:
                fields = next(csv.reader(fh))
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerows(rows)

    def load_results(self, campaign_id: str) -> list[dict]:
        path = self.base / campaign_id / "results_all.csv"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def save_record(self, campaign_id: str, name: str, data: object) -> Path:
        if hasattr(data, "model_dump"):
            data = data.model_dump(mode="json")
        path = self.folder(campaign_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return path
