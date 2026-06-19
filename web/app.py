"""Backward-compatibility shim — the FastAPI app moved to ``app.api.app``.

Import or run from the new location in new code:
    python -m uvicorn app.api.app:app --reload

This shim re-exports ``app`` so the legacy ``uvicorn web.app:app`` command and
``from web.app import app`` keep working during the layered-architecture
migration. Phase 5 removes it.
"""

from app.api.app import app

__all__ = ["app"]
