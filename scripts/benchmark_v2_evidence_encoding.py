"""Measure v2 evidence encoding over a read-only SQLite sample."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import zlib

from app.services.backtest.canonical_manifest import canonical_json


def _chunks(
    rows: list[dict[str, object]], actions: list[dict[str, object]]
) -> tuple[bytes, ...]:
    grouped: dict[tuple[str, int], list[dict[str, object]]] = {}
    for kind, items in (("rows", rows), ("actions", actions)):
        for item in items:
            key = (kind, int(str(item["session"])[:4]))
            grouped.setdefault(key, []).append(item)
    return tuple(
        canonical_json({"kind": kind, "year": year, "items": items}).encode()
        for (kind, year), items in sorted(grouped.items())
    )


def measure(path: Path, limit: int) -> dict[str, object]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        selected = conn.execute(
            """SELECT data_revision, canonical_manifest_json
               FROM historical_price_revisions ORDER BY data_revision LIMIT ?""",
            (limit,),
        ).fetchall()
    compressed: dict[str, int] = {}
    source_bytes = metadata_bytes = 0
    chunk_references = 0
    for _revision, rendered in selected:
        source_bytes += len(str(rendered).encode())
        manifest = json.loads(str(rendered))
        rows = manifest.pop("rows")
        actions = manifest.pop("actions")
        metadata_bytes += len(canonical_json(manifest).encode())
        for chunk in _chunks(rows, actions):
            compressed.setdefault(sha256(chunk).hexdigest(), len(zlib.compress(chunk)))
            chunk_references += 1
    v2_bytes = metadata_bytes + sum(compressed.values())
    return {
        "database": str(path.resolve()),
        "sample_revisions": len(selected),
        "selection": "lexical data_revision order; read-only; no schema writes",
        "v1_canonical_manifest_bytes": source_bytes,
        "v2_metadata_plus_unique_compressed_chunk_bytes": v2_bytes,
        "chunk_references": chunk_references,
        "unique_chunks": len(compressed),
        "reduction_percent": round(100 * (1 - v2_bytes / source_bytes), 2)
        if source_bytes
        else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-db", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be positive")
    print(json.dumps(measure(args.historical_db, args.limit), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
