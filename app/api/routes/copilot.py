"""Research Copilot route (GH-13) — one cited answer about one scanner row.

Read-only apart from the copilot's single appended audit line. The Claude
call runs in a worker thread so a slow answer never blocks the event loop.
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.agents.research.copilot import (
    DEFAULT_QUESTION,
    CopilotOutcome,
    ResearchCopilotClient,
    ask_copilot,
)
from app.api.dependencies import get_research_copilot_client, get_trader_service
from app.api.stock_scanner_context import load_source_health
from app.api.templating import templates
from app.core.config import ANALYSIS_JSON
from app.core.security import require_local_or_token
from app.schemas.analysis_artifact import read_analysis_snapshot
from app.schemas.record import StockRecord
from app.services.freshness_service import calculate_freshness
from app.services.trader_service import TraderService

router = APIRouter()

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
CopilotDep = Annotated[ResearchCopilotClient, Depends(get_research_copilot_client)]


@router.post(
    "/copilot/ask",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def copilot_ask(
    request: Request,
    trader: TraderDep,
    client: CopilotDep,
    ticker: str = Form(..., max_length=32),
    question: str = Form("", max_length=500),
) -> HTMLResponse:
    """Answer one question about one security and render the copilot panel."""
    if not ticker.strip():
        raise HTTPException(status_code=422, detail="ticker is required")
    outcome = await asyncio.to_thread(
        _ask, ticker, question.strip() or DEFAULT_QUESTION, trader, client
    )
    return templates.TemplateResponse(
        request, "_copilot_panel.html", {"outcome": outcome}
    )


def _ask(
    ticker: str,
    question: str,
    trader: TraderService,
    client: ResearchCopilotClient,
) -> CopilotOutcome:
    """Gather the record and run identity from one artifact read, then ask.

    One read means the record and run id always come from the same run,
    even if a pipeline run promotes a new artifact mid-question.
    """
    wanted = ticker.strip().upper()
    rows, meta = read_analysis_snapshot(ANALYSIS_JSON)
    record = next((r for r in _valid_records(rows) if r.ticker.upper() == wanted), None)
    return ask_copilot(
        record.ticker if record else wanted,
        question,
        record,
        client=client,
        is_held=record is not None and record.ticker in trader.held_tickers(),
        meta=meta,
        freshness=calculate_freshness(meta.generated_at if meta else None),
        source_health=load_source_health(),
    )


def _valid_records(rows: list[dict[str, Any]]) -> list[StockRecord]:
    """Validate rows, skipping malformed ones as ``load_analysis`` does."""
    records: list[StockRecord] = []
    for row in rows:
        try:
            records.append(StockRecord.model_validate(row))
        except Exception:
            continue
    return records
