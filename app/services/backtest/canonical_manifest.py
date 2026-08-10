"""Canonical JSON and SHA-256 helpers for immutable Backtest manifests."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping

import pandas as pd

CANONICAL_MANIFEST_VERSION = "CanonicalManifestJsonV1"


def jsonable(value: Any) -> Any:
    """Return a deterministic JSON-safe representation of supported values."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite manifest value")
        return value.hex()
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return jsonable(model_dump(mode="python"))
    raise ValueError(f"unsupported manifest type: {type(value).__name__}")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_json(value: object) -> str:
    return canonical_bytes(value).decode("utf-8")


def manifest_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
