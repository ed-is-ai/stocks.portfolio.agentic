"""Read-only view routes — the main page and htmx partials."""

import csv
import json
import logging
from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import (
    get_alerts_repository,
    get_portfolio_recommendation_service,
    get_portfolio_service,
    get_position_thesis_service,
    get_realised_pnl_service,
    get_strategy_assignment_service,
    get_trader_service,
)
from app.api.params import chart_range, optional_int
from app.api.templating import templates
from app.api.stock_scanner_context import (
    build_freshness_context,
    build_stock_scanner_context,
)
from app.services.evidence_funnel import parse_run_log_source_health
from app.services.evidence_quality import (
    CALENDAR_MIC,
    FAULT_SEVERITY_RANK,
    load_data_quality_view,
)
from app.core.config import PIPELINE_RUNS_CSV
from app.core.security import require_local_or_token
from app.repositories.alerts_repo import AlertsRepository
from app.services.portfolio_recommendation_service import (
    PortfolioRecommendationService,
)
from app.services.pipeline_service import PipelineService
from app.services.portfolio_service import PortfolioService
from app.services.position_thesis_service import PositionThesisService
from app.services.realised_pnl_service import RealisedPnlService
from app.services.strategy_assignment_service import StrategyAssignmentService
from app.services.trader_service import TraderService

router = APIRouter()
logger = logging.getLogger(__name__)

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]
AlertsDep = Annotated[AlertsRepository, Depends(get_alerts_repository)]
RealisedPnlDep = Annotated[RealisedPnlService, Depends(get_realised_pnl_service)]


@router.get("/startup-status")
async def startup_status(request: Request) -> dict[str, str]:
    """Report boot preparation without reading the evidence database."""
    return {"status": request.app.state.strategy_preparation}


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the shell, with freshness ready for the header affordance (#418).

    The refresh control is now the primary place freshness is discovered, so
    it is server-rendered on first paint rather than waiting on (or failing
    with) the load-triggered ``/pipeline-status`` fetch.
    """
    return templates.TemplateResponse(
        request,
        "index.html",
        context={
            **build_freshness_context(),
            #: Reduced-coverage warnings live in the Refresh Data menu, beside
            #: the buttons that start a run, rather than in a confirmation
            #: dialog after the click. They come from the process environment,
            #: so first paint is the only time they can change.
            "pipeline_warnings": PipelineService.missing_configuration(),
        },
    )


@router.get("/partials/stock-scanner", response_class=HTMLResponse)
async def partial_stock_scanner(
    request: Request, trader: TraderDep, portfolio: PortfolioDep, alerts: AlertsDep
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_stock_scanner.html",
        context=build_stock_scanner_context(trader, portfolio, alerts),
    )


@router.get("/partials/portfolio", response_class=HTMLResponse)
async def partial_portfolio(
    request: Request,
    portfolio: PortfolioDep,
    portfolio_id: str | None = None,
    range_key: str | None = Query(None, alias="range"),
) -> HTMLResponse:
    # Accept a raw string: the client sends an empty ``portfolio_id=`` when no
    # account is selected, which an ``int | None`` param rejects with 422 and
    # breaks the tab (#147 regression). ``range`` rides the same request via
    # ``hx-vals`` so the first paint already honours the stored range (#421).
    context = portfolio.default_portfolio_context(
        optional_int(portfolio_id), range_key=chart_range(range_key)
    )
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


@router.get("/partials/portfolio/chart", response_class=HTMLResponse)
async def partial_portfolio_chart(
    request: Request,
    portfolio: PortfolioDep,
    portfolio_id: str | None = None,
    range_key: str | None = Query(None, alias="range"),
) -> HTMLResponse:
    """Re-render only the portfolio value-chart card on a range change (#421).

    Lean by design: it does not rebuild positions, prices, or cash — a range
    switch swaps ``#portfolio-chart-card`` alone.
    """
    context = portfolio.chart_fragment_context(
        optional_int(portfolio_id), chart_range(range_key)
    )
    return templates.TemplateResponse(request, "_portfolio_chart.html", context=context)


@router.get("/partials/portfolio/risk", response_class=HTMLResponse)
def partial_portfolio_risk(
    request: Request,
    portfolio: PortfolioDep,
    portfolio_id: str | None = None,
) -> HTMLResponse:
    """Render the read-only Portfolio Risk Coach panel (GH-16).

    A plain ``def`` for the same reason as ``partial_strategy_assign``: the
    report reads the ledger, price cache and scan artifact, so FastAPI runs
    it in its threadpool. Never mutates trades, cash flows, portfolios or
    Strategy assignments; a holding in a currency other than GBP/GBp/USD may
    fetch and cache an FX quote exactly as the Portfolio tab render does.
    """
    report = portfolio.risk_report(optional_int(portfolio_id))
    return templates.TemplateResponse(
        request, "_portfolio_risk.html", context={"report": report}
    )


@router.get("/partials/portfolio/agents", response_class=HTMLResponse)
def partial_portfolio_agents(
    request: Request,
    portfolio: PortfolioDep,
    recommendations: Annotated[
        PortfolioRecommendationService, Depends(get_portfolio_recommendation_service)
    ],
    theses: Annotated[PositionThesisService, Depends(get_position_thesis_service)],
    portfolio_id: str | None = None,
) -> HTMLResponse:
    """Out-of-band swaps for the Portfolio tab's agent layer (GH-19, GH-14).

    A plain ``def`` like ``partial_portfolio_risk``: the recommendation and
    risk evaluation read the ledger, scan artifact and price cache, so they
    run in the threadpool, lazily, after the tab has painted. Never mutates
    trades, cash flows, portfolios, Strategy assignments or theses; the risk
    valuation may fetch and cache an FX quote exactly as the Portfolio tab
    render does.
    """
    view = portfolio.agent_view(
        optional_int(portfolio_id), recommendations.recommend, theses.statuses
    )
    return templates.TemplateResponse(
        request, "_portfolio_agents.html", context={"view": view}
    )


@router.get("/partials/strategy-assign", response_class=HTMLResponse)
def partial_strategy_assign(
    request: Request,
    assignment: Annotated[
        StrategyAssignmentService, Depends(get_strategy_assignment_service)
    ],
    recommendations: Annotated[
        PortfolioRecommendationService, Depends(get_portfolio_recommendation_service)
    ],
    portfolio_id: str | None = None,
) -> HTMLResponse:
    """Render the assign-Strategy modal partial (#440).

    Read-only: lists discovery choices/warnings, the portfolio's current
    assignment, and each choice's current recommendation support (#471).
    Accepts a raw string portfolio_id like /partials/portfolio (an empty
    value means no account selected). Support lookup is fail-soft — the
    modal still renders when it cannot be determined.

    Deliberately a plain ``def``: ``strategy_support()`` reads the scan
    artifact and imports every Strategy runtime, so FastAPI must run this
    in its threadpool rather than blocking the event loop.
    """
    pid = optional_int(portfolio_id)
    strategy_support: Mapping[str, str] = {}
    strategy_support_diagnostics: Mapping[str, tuple[object, ...]] = {}
    bundle = getattr(recommendations, "strategy_support_with_diagnostics", None)
    bundle_ok = False
    if callable(bundle):
        try:
            bundled = bundle()
            if not isinstance(bundled, tuple) or len(bundled) != 2:
                raise TypeError("strategy support bundle must contain two mappings")
            strategy_support, strategy_support_diagnostics = bundled
            if not isinstance(strategy_support, Mapping):
                strategy_support = {}
            if not isinstance(strategy_support_diagnostics, Mapping):
                strategy_support_diagnostics = {}
            bundle_ok = True
        except Exception:
            logger.exception("Strategy support bundle lookup failed")
    if not bundle_ok:
        # Keep lightweight/test doubles and legacy callers compatible while
        # isolating diagnostics failures from the existing support labels.
        try:
            strategy_support = recommendations.strategy_support()
        except Exception:
            logger.exception("Strategy support lookup failed")
        try:
            strategy_support_diagnostics = (
                recommendations.strategy_support_diagnostics()
            )
            if not isinstance(strategy_support_diagnostics, Mapping):
                strategy_support_diagnostics = {}
        except Exception:
            logger.exception("Strategy support diagnostics lookup failed")
    context = {
        "portfolio_id": pid,
        "strategy_choices": assignment.list_choices(),
        "strategy_warnings": assignment.list_warnings(),
        "strategy_assignment": (
            assignment.assignment_view(pid) if pid is not None else None
        ),
        "strategy_freshness": assignment.freshness(),
        "strategy_support": strategy_support,
        "strategy_support_diagnostics": strategy_support_diagnostics,
    }
    return templates.TemplateResponse(request, "_strategy_assign.html", context=context)


@router.get("/partials/realised-pnl", response_class=HTMLResponse)
def partial_realised_pnl(
    request: Request,
    trader: TraderDep,
    realised_pnl: RealisedPnlDep,
    portfolio_id: str | None = None,
) -> HTMLResponse:
    # Accept a raw string: the client sends an empty ``portfolio_id=`` when no
    # account is selected, which an ``int | None`` param rejects with 422 and
    # breaks the tab (#147 regression).
    pid = optional_int(portfolio_id)
    portfolios = trader.list_portfolios()
    if not portfolios:
        return templates.TemplateResponse(
            request, "_realised_pnl.html", context={"no_portfolios": True}
        )
    # Resolve the active portfolio: an unknown/None id falls back to the
    # first portfolio, matching PortfolioService.default_portfolio_context.
    active_id = pid
    if active_id is None or not any(p.id == active_id for p in portfolios):
        active_id = portfolios[0].id
    active_portfolio = next(p for p in portfolios if p.id == active_id)
    summary = realised_pnl.compute_summary(active_id)
    # Projected from the summary already in hand -- never a second
    # compute_summary, which would re-run FIFO for the same render (#563).
    timeline = RealisedPnlService.timeline_points(summary)
    return templates.TemplateResponse(
        request,
        "_realised_pnl.html",
        context={
            "portfolios": portfolios,
            "active_portfolio": active_portfolio,
            "summary": summary,
            "unmatched_sells": summary.unmatched_sells,
            "pnl_timeline": json.dumps(timeline),
            "pnl_timeline_count": len(timeline),
        },
    )


@router.post(
    "/trades/{trade_id}/ack",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def ack_unmatched_sell(
    request: Request,
    trade_id: int,
    trader: TraderDep,
    realised_pnl: RealisedPnlDep,
    portfolio_id: str | None = None,
) -> HTMLResponse:
    """Toggle one unmatched sell's acknowledgment; re-render its fragment only.

    Bodyless per AD-8 -- ``portfolio_id`` travels as a query-string param on
    the ``hx-post`` URL (not a form field/body), only so the response can be
    re-scoped to the same Account; the ack value itself is never supplied by
    the client, only toggled server-side.
    """
    pid = optional_int(portfolio_id)
    portfolios = trader.list_portfolios()
    if not portfolios:
        return templates.TemplateResponse(
            request, "_unmatched_sells.html", context={"unmatched_sells": []}
        )
    active_id = pid
    if active_id is None or not any(p.id == active_id for p in portfolios):
        active_id = portfolios[0].id
    active_portfolio = next(p for p in portfolios if p.id == active_id)
    summary = realised_pnl.toggle_unmatched_sell_ack(trade_id, active_id)
    return templates.TemplateResponse(
        request,
        "_unmatched_sells.html",
        context={
            "unmatched_sells": summary.unmatched_sells,
            "active_portfolio": active_portfolio,
        },
    )


@router.get("/partials/history", response_class=HTMLResponse)
async def partial_history(
    request: Request, trader: TraderDep, realised_pnl: RealisedPnlDep
) -> HTMLResponse:
    # Trade History spans every portfolio; a name map feeds the Portfolio
    # column, disambiguating duplicate names with #id (#147).
    portfolios = trader.list_portfolios()
    seen: dict[str, int] = {}
    for pf in portfolios:
        seen[pf.name] = seen.get(pf.name, 0) + 1
    names = {
        pf.id: (f"{pf.name} #{pf.id}" if seen[pf.name] > 1 else pf.name)
        for pf in portfolios
    }
    # FIFO display work is revision-cached in the service. Edit/delete
    # routes retain their fresh single-lot guards; this snapshot is never a
    # mutation authority.
    trades, opening_lot_status = realised_pnl.get_history_presentation(
        [pf.id for pf in portfolios]
    )
    return templates.TemplateResponse(
        request,
        "_history.html",
        context={
            "trades": trades,
            "portfolio_names": names,
            "opening_lot_status": opening_lot_status,
        },
    )


def _run_date(start: str | None) -> date | None:
    """Return the date of a run log ``start`` timestamp, None if unusable."""
    try:
        return datetime.fromisoformat(start or "").date()
    except ValueError:
        return None


@router.get("/partials/runlog", response_class=HTMLResponse)
async def partial_runlog(request: Request) -> HTMLResponse:
    runs: list[dict] = []
    if PIPELINE_RUNS_CSV.exists():
        with open(PIPELINE_RUNS_CSV, newline="", encoding="utf-8-sig") as fh:
            runs = list(csv.DictReader(fh))
    for run in runs:
        # Legacy CSV rows (written before a header field existed) simply
        # lack that key rather than having it as "" — fill in safe defaults
        # so the template can render them without a KeyError/Undefined.
        for field in (
            "duration_seconds",
            "scanned",
            "analysed",
            "buy_alerts",
            "sell_alerts",
            "actionable",
        ):
            run.setdefault(field, "0")
        run.setdefault("errors", "")
        run.setdefault("sources", "")
        run["source_health"] = parse_run_log_source_health(run)
        # Cached-source age is measured against the run's own date (GH-3).
        run["run_date"] = _run_date(run.get("start"))
    runs.reverse()  # most recent first
    return templates.TemplateResponse(request, "_runlog.html", context={"runs": runs})


@router.get("/partials/data-quality", response_class=HTMLResponse)
def partial_data_quality(
    request: Request, portfolio_id: str | None = None
) -> HTMLResponse:
    """Render the read-only Data Quality tab (#639).

    Both sub-tabs are rendered in one response and toggled client-side, so
    nothing on the screen issues a request or writes anything. Declared
    ``def`` so its blocking reads (artifact, run log, Strategy runtimes) run
    in the threadpool rather than on the event loop.

    ``portfolio_id`` arrives as a raw string for the same reason the
    portfolio partial takes one: the client sends an empty ``portfolio_id=``
    when no account is selected, which an ``int | None`` param 422s (#147).
    It scopes the holdings the census accounts for.
    """
    return templates.TemplateResponse(
        request,
        "_evidence_census.html",
        context={
            "view": load_data_quality_view(optional_int(portfolio_id)),
            "fault_rank": FAULT_SEVERITY_RANK,
            "calendar_mic": CALENDAR_MIC,
        },
    )
