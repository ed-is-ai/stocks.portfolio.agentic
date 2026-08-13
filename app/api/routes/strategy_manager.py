"""Strategy Manager HTML routes over the durable backtest ledger.

GET routes only render repository state.  All lifecycle changes stay behind
``StrategyJobService`` so browser navigation and polling cannot start work.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.dependencies import get_backtest_repository, get_strategy_job_service
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.services.backtest.snapshot_profile import SnapshotProfileV1
from app.services.backtest.strategy_job import (
    InitializationSubmissionV1,
    StrategyJobCancellationV1,
    StrategyJobConflict,
    StrategyJobDeletionV1,
    StrategyJobNotFound,
    StrategyJobRestartV1,
    StrategyJobStatus,
    StrategyJobType,
    StrategyJobV1,
)
from app.services.backtest.strategy_job_service import StrategyJobService
from app.services.backtest.trading_calendar import TradingCalendar

router = APIRouter()
BacktestDep = Annotated[BacktestRepository, Depends(get_backtest_repository)]
JobsDep = Annotated[StrategyJobService, Depends(get_strategy_job_service)]

_TERMINAL = {
    StrategyJobStatus.COMPLETE,
    StrategyJobStatus.FAILED,
    StrategyJobStatus.CANCELLED,
}


def _coverage_context(repo: BacktestRepository) -> dict[str, object]:
    """Return a visible setup error for missing/corrupt profile evidence."""
    try:
        coverage = repo.snapshot_coverage()
        return {"coverage": coverage, "coverage_error": None}
    except BacktestIntegrityError as exc:
        return {"coverage": None, "coverage_error": str(exc)}


def _profile_context(repo: BacktestRepository) -> dict[str, object]:
    try:
        active = repo.active_snapshot_profile()
        if active is None:
            return {
                "profile": None,
                "qualification_available": False,
                "qualification_reason": "No active scanner-data version is configured.",
            }
        profile = repo.snapshot_profile(active.profile_hash)
        qualified = repo.current_qualification_contract_digest() is not None
        return {
            "profile": profile,
            "qualification_available": qualified,
            "qualification_reason": None
            if qualified
            else "Historical data providers have not passed certification.",
        }
    except BacktestIntegrityError as exc:
        return {
            "profile": None,
            "qualification_available": False,
            "qualification_reason": str(exc),
        }


def _initialization_context(
    repo: BacktestRepository, **extra: object
) -> dict[str, object]:
    jobs = tuple(
        job
        for job in repo.list_strategy_jobs()
        if job.job_type is StrategyJobType.INITIALIZATION
    )
    return {
        **_coverage_context(repo),
        **_profile_context(repo),
        "jobs": jobs,
        "max_month": (date.today().replace(day=1) - timedelta(days=1)).strftime(
            "%Y-%m"
        ),
        "values": {"start_month": "", "end_month": ""},
        "errors": {},
        "message": None,
        **extra,
    }


def _validate_months(start_month: str, end_month: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    for field, value in (("start_month", start_month), ("end_month", end_month)):
        if (
            len(value) != 7
            or not value.isascii()
            or value[4:5] != "-"
            or not value[:4].isdigit()
            or not value[5:].isdigit()
        ):
            errors[field] = "Use a fully closed month in YYYY-MM format."
            continue
        try:
            TradingCalendar.months_inclusive(value, value)
        except ValueError:
            errors[field] = "Use a valid calendar month in YYYY-MM format."
    if not errors:
        current_month = date.today().strftime("%Y-%m")
        if start_month >= current_month:
            errors["start_month"] = "Choose a completed historical month."
        if end_month >= current_month:
            errors["end_month"] = "Choose a completed historical month."
        if start_month > end_month:
            errors["end_month"] = "End month must be on or after start month."
    return errors


@router.get("/partials/strategy-manager", response_class=HTMLResponse)
@router.get("/strategy-manager", response_class=HTMLResponse)
async def strategy_manager(request: Request, backtest: BacktestDep) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_strategy_manager.html", _initialization_context(backtest)
    )


@router.get("/strategy-manager/initialization", response_class=HTMLResponse)
async def historical_initialization(
    request: Request, backtest: BacktestDep
) -> Response:
    return templates.TemplateResponse(
        request, "_historical_initialization.html", _initialization_context(backtest)
    )


@router.post(
    "/strategy-manager/initialization",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def submit_initialization(
    request: Request,
    backtest: BacktestDep,
    jobs: JobsDep,
    start_month: Annotated[str, Form()] = "",
    end_month: Annotated[str, Form()] = "",
) -> Response:
    context = _initialization_context(
        backtest, values={"start_month": start_month, "end_month": end_month}
    )
    errors = _validate_months(start_month, end_month)
    profile = context["profile"]
    if profile is None:
        errors["form"] = str(context["qualification_reason"])
    elif not context["qualification_available"]:
        errors["form"] = str(context["qualification_reason"])
    if errors:
        return templates.TemplateResponse(
            request,
            "_historical_initialization.html",
            {**context, "errors": errors},
            status_code=422,
        )
    try:
        active_profile = cast(SnapshotProfileV1, profile)
        readiness = backtest.interval_readiness(
            active_profile.profile_hash, start_month, end_month
        )
        if readiness.no_op:
            return templates.TemplateResponse(
                request,
                "_historical_initialization.html",
                {
                    **context,
                    "message": "Coverage is already Ready for the requested period.",
                },
            )
        result = jobs.enqueue_initialization(
            InitializationSubmissionV1(
                profile_hash=active_profile.profile_hash,
                requested_start=start_month,
                requested_end=end_month,
                calendar_dataset_version=active_profile.calendar_dataset_version,
            )
        )
    except (BacktestIntegrityError, StrategyJobConflict, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "_historical_initialization.html",
            {**context, "errors": {"form": str(exc)}},
            status_code=422,
        )
    if result.no_op:
        return templates.TemplateResponse(
            request,
            "_historical_initialization.html",
            {
                **context,
                "message": "Coverage is already Ready for the requested period.",
            },
        )
    return RedirectResponse(
        f"/strategy-manager/activities/{result.job.id}", status_code=303
    )


def _activity_context(
    repo: BacktestRepository, service: StrategyJobService, job_id: str
) -> dict[str, object]:
    job = repo.strategy_job(job_id)
    if job.job_type is not StrategyJobType.INITIALIZATION:
        raise StrategyJobNotFound("initialization activity not found")
    return {
        "job": job,
        "run": repo.initialization_run(job_id),
        "actions": service.legal_actions(job_id).legal_actions,
        "terminal": job.status in _TERMINAL,
    }


@router.get("/strategy-manager/activities/{job_id}", response_class=HTMLResponse)
async def initialization_activity(
    request: Request, job_id: str, backtest: BacktestDep, jobs: JobsDep
) -> Response:
    try:
        return templates.TemplateResponse(
            request,
            "_initialization_activity.html",
            _activity_context(backtest, jobs, job_id),
        )
    except StrategyJobNotFound:
        return HTMLResponse("Activity no longer available.", status_code=404)


@router.get("/strategy-manager/activities/{job_id}/status", response_class=HTMLResponse)
async def initialization_activity_status(
    request: Request,
    job_id: str,
    backtest: BacktestDep,
    jobs: JobsDep,
    last_seen_version: int = 0,
) -> HTMLResponse:
    try:
        context = _activity_context(backtest, jobs, job_id)
    except StrategyJobNotFound:
        return HTMLResponse("Activity no longer available.", status_code=404)
    job = cast(StrategyJobV1, context["job"])
    if job.status_version <= last_seen_version:
        return HTMLResponse("", status_code=204)
    return templates.TemplateResponse(request, "_initialization_activity.html", context)


@router.post(
    "/strategy-manager/activities/{job_id}/cancel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def cancel_initialization(
    request: Request,
    job_id: str,
    backtest: BacktestDep,
    jobs: JobsDep,
    expected_version: Annotated[int, Form()],
) -> HTMLResponse:
    try:
        jobs.request_cancellation(
            StrategyJobCancellationV1(job_id=job_id, expected_version=expected_version)
        )
        return templates.TemplateResponse(
            request,
            "_initialization_activity.html",
            _activity_context(backtest, jobs, job_id),
        )
    except (StrategyJobConflict, StrategyJobNotFound, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=409)


@router.post(
    "/strategy-manager/activities/{job_id}/restart",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def restart_initialization(
    request: Request,
    job_id: str,
    backtest: BacktestDep,
    jobs: JobsDep,
    expected_version: Annotated[int, Form()],
) -> Response:
    try:
        result = jobs.restart_initialization(
            StrategyJobRestartV1(
                source_job_id=job_id,
                expected_version=expected_version,
                idempotency_key=str(uuid4()),
            )
        )
        return RedirectResponse(
            f"/strategy-manager/activities/{result.job.id}", status_code=303
        )
    except (StrategyJobConflict, StrategyJobNotFound, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=409)


@router.post(
    "/strategy-manager/activities/{job_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def delete_initialization(
    request: Request,
    job_id: str,
    jobs: JobsDep,
    expected_version: Annotated[int, Form()],
) -> HTMLResponse:
    try:
        jobs.delete_job(
            StrategyJobDeletionV1(job_id=job_id, expected_version=expected_version)
        )
    except (StrategyJobConflict, StrategyJobNotFound, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=409)
    return HTMLResponse(
        '<h2 id="initialization-history" tabindex="-1">Initialization history</h2><p>Activity deleted.</p>'
    )
