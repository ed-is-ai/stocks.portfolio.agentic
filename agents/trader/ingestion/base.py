"""Abstract base class for portfolio transaction ingestors."""

import csv
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent.parent  # repo root
_STAGING_ROOT = _ROOT / "data" / "staging"
_PROCESSED_ROOT = _ROOT / "data" / "processed"
_PORTFOLIO_VALUE_CSV = _ROOT / "portfolio_value.csv"
_PV_FIELDNAMES = ["timestamp", "portfolio", "total_value", "total_cost"]


@dataclass
class IngestResult:
    file: str
    portfolio: str
    trades_imported: int
    value_rows_imported: int
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class IngestorBase(ABC):
    """Base class for staging-folder CSV ingestors."""

    portfolio: str

    @property
    def staging_dir(self) -> Path:
        return _STAGING_ROOT / self.portfolio

    @property
    def processed_dir(self) -> Path:
        return _PROCESSED_ROOT / self.portfolio

    def pending_files(self) -> list[Path]:
        """Return CSV files in the staging dir (skip .gitkeep and config.json)."""
        if not self.staging_dir.exists():
            return []
        return sorted(
            p for p in self.staging_dir.glob("*.csv")
            if p.name not in (".gitkeep",)
        )

    def mark_processed(self, path: Path) -> None:
        """Move a file to the processed directory."""
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        dest = self.processed_dir / path.name
        # Avoid overwriting: append suffix if collision
        if dest.exists():
            dest = dest.with_stem(f"{dest.stem}_{path.stat().st_mtime_ns}")
        shutil.move(str(path), str(dest))

    @abstractmethod
    def ingest_file(self, path: Path, trader: "TraderAgent") -> IngestResult:  # type: ignore[name-defined]
        ...

    def run(self, trader: "TraderAgent") -> list[IngestResult]:  # type: ignore[name-defined]
        """Process all pending files in the staging directory."""
        results = []
        for f in self.pending_files():
            result = self.ingest_file(f, trader)
            if result.ok:
                self.mark_processed(f)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Shared helper: append rows to portfolio_value.csv
    # ------------------------------------------------------------------

    @staticmethod
    def _load_existing_value_keys() -> set[tuple[str, str]]:
        """Return set of (date_prefix, portfolio) already in portfolio_value.csv."""
        if not _PORTFOLIO_VALUE_CSV.exists():
            return set()
        with open(_PORTFOLIO_VALUE_CSV, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            return {
                (r["timestamp"][:10], r.get("portfolio", "SIPP"))
                for r in reader
            }

    @staticmethod
    def _append_value_rows(rows: list[dict[str, str]]) -> int:
        """Append rows to portfolio_value.csv, skipping duplicates. Returns count added."""
        existing = IngestorBase._load_existing_value_keys()
        write_header = (
            not _PORTFOLIO_VALUE_CSV.exists()
            or _PORTFOLIO_VALUE_CSV.stat().st_size == 0
        )
        added = 0
        with open(_PORTFOLIO_VALUE_CSV, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_PV_FIELDNAMES)
            if write_header:
                writer.writeheader()
            for row in rows:
                key = (row["timestamp"][:10], row["portfolio"])
                if key not in existing:
                    writer.writerow(row)
                    existing.add(key)
                    added += 1
        return added
