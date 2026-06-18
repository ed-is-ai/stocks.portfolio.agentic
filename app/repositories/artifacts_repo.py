"""Repository for on-disk artifact files (CSV/JSON).

Covers the pipeline/portfolio artifacts that live outside SQLite:
``pipeline_runs.csv``, ``portfolio_value.csv``, and the ``*_results.json``
files. Keeps file I/O out of agents and services.
"""

import csv
import json
from pathlib import Path
from typing import Any


class ArtifactsRepository:
    """Read/write helpers for CSV and JSON artifact files."""

    def read_json(self, path: str | Path, default: Any = None) -> Any:
        """Return parsed JSON from ``path``, or ``default`` if missing/unreadable."""
        p = Path(path)
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def write_json(self, path: str | Path, data: Any) -> None:
        """Write ``data`` as JSON to ``path``."""
        Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def read_csv_dicts(self, path: str | Path) -> list[dict[str, Any]]:
        """Return all rows of a CSV file as dicts (empty list if missing)."""
        p = Path(path)
        if not p.exists():
            return []
        with open(p, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def append_csv_row(
        self,
        path: str | Path,
        fieldnames: list[str],
        row: dict[str, Any],
    ) -> None:
        """Append a row to a CSV, writing the header if the file is new/empty."""
        p = Path(path)
        write_header = not p.exists() or p.stat().st_size == 0
        with open(p, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
