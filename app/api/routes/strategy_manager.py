"""Strategy Manager HTML routes over the durable backtest ledger.

GET routes only render repository state.  All lifecycle changes stay behind
``StrategyJobService`` so browser navigation and polling cannot start work.
"""

from __future__ import annotations

from datetime import date, timedelta
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import logging
import re
from time import perf_counter
from typing import Annotated, Literal, cast
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.datastructures import FormData
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from app.api.dependencies import (
    get_backtest_launch_service,
    get_backtest_repository,
    get_bootstrap_service,
    get_readiness_service,
    get_strategy_job_service,
)
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.repositories.backtest_repo import (
    BacktestIntegrityError,
    BacktestRepository,
    BacktestResultV1,
    ComparisonEligibilityV1,
)
from app.services.backtest.backtest_launch_service import (
    BacktestLaunchCommandV1,
    BacktestLaunchService,
    BacktestLaunchValidationError,
)
from app.services.backtest.result_presenter import (
    comparison_equity_payload,
    equity_curve_payload,
    metrics_view,
    note_view,
    provenance_view,
    trade_log_view,
)
from app.services.backtest.run_universe import (
    RunUniverseError,
    canonical_run_universe,
    run_universe_digest,
)
from app.services.backtest.skill_discovery import StrategyDescriptorV1
from app.services.backtest.snapshot_profile import SnapshotProfileV1
from app.services.backtest.strategy_bootstrap_service import (
    StrategyBootstrapAlreadySetUp,
    StrategyBootstrapService,
)
from app.services.backtest.strategy_job import (
    BootstrapSubmissionV1,
    InitializationSubmissionV1,
    StrategyJobCancellationV1,
    StrategyJobConflict,
    StrategyJobDeletionV1,
    StrategyJobNotFound,
    StrategyJobRestartV1,
    StrategyJobStatus,
    StrategyJobType,
    StrategyJobV1,
    RunUniverseSelectionV1,
)
from app.services.backtest.strategy_job_service import StrategyJobService
from app.services.backtest.strategy_protocol import JsonValue, StrategyParameterV1
from app.services.backtest.strategy_readiness_service import (
    StrategyReadinessService,
)
from app.services.backtest.trading_calendar import TradingCalendar

router = APIRouter()
logger = logging.getLogger(__name__)
BacktestDep = Annotated[BacktestRepository, Depends(get_backtest_repository)]
JobsDep = Annotated[StrategyJobService, Depends(get_strategy_job_service)]
LaunchDep = Annotated[BacktestLaunchService, Depends(get_backtest_launch_service)]
BootstrapDep = Annotated[StrategyBootstrapService, Depends(get_bootstrap_service)]
ReadinessDep = Annotated[StrategyReadinessService, Depends(get_readiness_service)]

_TERMINAL = {
    StrategyJobStatus.COMPLETE,
    StrategyJobStatus.FAILED,
    StrategyJobStatus.CANCELLED,
}
_ACTIVE_PROFILE_UNSET = object()


def _coverage_context(repo: BacktestRepository) -> dict[str, object]:
    """Return a visible setup error for missing/corrupt profile evidence."""
    try:
        coverage = repo.snapshot_coverage()
        return {"coverage": coverage, "coverage_error": None}
    except BacktestIntegrityError as exc:
        return {"coverage": None, "coverage_error": str(exc)}


def _profile_context(
    repo: BacktestRepository, *, active: object = _ACTIVE_PROFILE_UNSET
) -> dict[str, object]:
    try:
        if active is _ACTIVE_PROFILE_UNSET:
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


def _backtest_activities_context(repo: BacktestRepository) -> dict[str, object]:
    """Return the Backtest results list, or an explicit integrity alert
    (Story 2.8 AC 1, 7) -- a repository/integrity error is not an empty
    list, so the list template must be able to tell the two apart."""
    try:
        return {
            "backtest_activities": repo.list_backtest_activities(),
            "backtest_activities_error": None,
        }
    except BacktestIntegrityError as exc:
        return {"backtest_activities": (), "backtest_activities_error": str(exc)}


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


def _strategy_manager_context(repo: BacktestRepository) -> dict[str, object]:
    """Context for the main Strategy Manager view only -- unlike
    ``_initialization_context``, this never queries ``list_strategy_jobs()``
    or ``list_backtest_activities()``'s verified-complete Metrics rebuild
    for the unrelated Historical Initialization sub-view/submit handler,
    which render neither."""
    try:
        active = repo.active_snapshot_profile()
    except BacktestIntegrityError as exc:
        active = None
        profile_context = {
            "profile": None,
            "qualification_available": False,
            "qualification_reason": str(exc),
        }
    else:
        profile_context = _profile_context(repo, active=active)
    return {
        **_coverage_context(repo),
        **profile_context,
        **_backtest_activities_context(repo),
        "setup_required": active is None,
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


def _form_error_status(request: Request) -> int:
    """Let HTMX swap validation fragments while preserving HTTP semantics."""
    return 200 if request.headers.get("HX-Request", "").lower() == "true" else 422


@router.get("/partials/strategy-manager", response_class=HTMLResponse)
@router.get("/strategy-manager", response_class=HTMLResponse)
async def strategy_manager(request: Request, backtest: BacktestDep) -> HTMLResponse:
    started = perf_counter()
    response = templates.TemplateResponse(
        request, "_strategy_manager.html", _strategy_manager_context(backtest)
    )
    logger.info(
        "Strategy Manager tab rendered in %.1fms",
        (perf_counter() - started) * 1_000,
    )
    return response


# ---------------------------------------------------------------------------
# Story 4.3: Bootstrap setup
# ---------------------------------------------------------------------------


@router.get("/strategy-manager/setup", response_class=HTMLResponse)
async def strategy_setup(
    request: Request,
    backtest: BacktestDep,
    bootstrap: BootstrapDep,
) -> HTMLResponse:
    """Show setup confirmation form or 'already set up' message."""
    already, activated_at = bootstrap.is_already_set_up()
    setup_required = bootstrap.is_setup_required()
    return templates.TemplateResponse(
        request,
        "_strategy_setup.html",
        {
            "already_set_up": already,
            "setup_required": setup_required,
            "activated_at": activated_at,
            "is_fixture": bootstrap.is_fixture,
            "idempotency_key": str(uuid4()),
        },
    )


@router.post(
    "/strategy-manager/setup",
    dependencies=[Depends(require_local_or_token)],
)
async def submit_strategy_setup(
    request: Request,
    backtest: BacktestDep,
    bootstrap: BootstrapDep,
    idempotency_key: Annotated[str | None, Form()] = None,
) -> Response:
    """Enqueue one bootstrap job, redirect to activity."""
    if idempotency_key is None:
        return templates.TemplateResponse(
            request,
            "_strategy_setup.html",
            {
                "setup_required": True,
                "already_set_up": False,
                "is_fixture": bootstrap.is_fixture,
                "idempotency_key": str(uuid4()),
                "error": "Enter a valid setup submission.",
            },
            status_code=422,
        )
    try:
        job = bootstrap.start_setup(
            BootstrapSubmissionV1(idempotency_key=idempotency_key)
        )
    except ValidationError:
        return templates.TemplateResponse(
            request,
            "_strategy_setup.html",
            {
                "setup_required": True,
                "already_set_up": False,
                "is_fixture": bootstrap.is_fixture,
                "idempotency_key": str(uuid4()),
                "error": "Enter a valid setup submission.",
            },
            status_code=422,
        )
    except StrategyBootstrapAlreadySetUp:
        return RedirectResponse("/strategy-manager/setup", status_code=303)
    except StrategyJobConflict:
        return templates.TemplateResponse(
            request,
            "_strategy_setup.html",
            {
                "setup_required": True,
                "already_set_up": False,
                "is_fixture": bootstrap.is_fixture,
                "idempotency_key": idempotency_key,
                "error": "Unable to submit setup. Please try again.",
            },
            status_code=422,
        )
    return RedirectResponse(f"/strategy-manager/activities/{job.id}", status_code=303)


# ---------------------------------------------------------------------------
# Story 4.4: Readiness & diagnostics
# ---------------------------------------------------------------------------


@router.get("/strategy-manager/readiness", response_class=HTMLResponse)
async def strategy_readiness(
    request: Request,
    backtest: BacktestDep,
    readiness: ReadinessDep,
) -> HTMLResponse:
    """Show readiness prerequisite rows (read-only)."""
    result = readiness.evaluate()
    return templates.TemplateResponse(
        request,
        "_strategy_readiness.html",
        {
            "readiness": result,
            "diagnostics_view": False,
        },
    )


@router.get("/strategy-manager/diagnostics", response_class=HTMLResponse)
async def strategy_diagnostics(
    request: Request,
    backtest: BacktestDep,
    readiness: ReadinessDep,
) -> HTMLResponse:
    """Show bounded diagnostics (read-only)."""
    result = readiness.evaluate()
    diagnostics = readiness.diagnostics(readiness=result)
    return templates.TemplateResponse(
        request,
        "_strategy_readiness.html",
        {
            "readiness": result,
            "diagnostics": diagnostics,
            "diagnostics_view": True,
        },
    )


# ---------------------------------------------------------------------------
# Story 4.5: Universe selection
# ---------------------------------------------------------------------------


@router.get(
    "/strategy-manager/configuration/universe",
    response_class=HTMLResponse,
)
async def universe_selector(
    request: Request,
    backtest: BacktestDep,
    q: str = "",
    security_ids: list[str] | None = None,
    whole_universe: str | None = None,
) -> HTMLResponse:
    """Return universe selector partial with roster securities.

    ``whole_universe`` reflects the caller's *current* toggle state so
    search-as-you-type re-renders preserve it rather than resetting to
    the fresh-load default.
    """
    active = backtest.active_snapshot_profile()
    securities: list[tuple[str, str, str, str]] = []
    if active is not None:
        securities = backtest.roster_member_identities(active.profile_hash)
    return templates.TemplateResponse(
        request,
        "_universe_selector.html",
        {
            "securities": securities,
            "search_query": q,
            "selected_security_ids": frozenset(security_ids or ()),
            "whole_universe": _is_truthy_flag(whole_universe),
            "profile_hash": (active.profile_hash if active is not None else ""),
            "activation_seq": (active.activation_seq if active is not None else 0),
        },
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
            status_code=_form_error_status(request),
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
            status_code=_form_error_status(request),
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


# ---------------------------------------------------------------------------
# Story 2.7: Strategy configuration + launch
# ---------------------------------------------------------------------------

_PARAM_FIELD_PREFIX = "param__"
_INTEGER_RE = re.compile(r"-?[0-9]+")
_NUMBER_RE = re.compile(r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")
_INDEX_RE = re.compile(r"[0-9]+")
_FALSY_FLAG_VALUES = frozenset({"false", "0", "off", "no"})


def _is_truthy_flag(raw: str | None) -> bool:
    """Parse a submitted boolean-flag string, not just its presence.

    A checked HTML checkbox only ever sends its ``value`` (never a
    literal ``"false"``), but ``bool(raw)`` is still wrong for any raw
    string input -- ``bool("false")`` is ``True`` in Python. Treat a
    present-but-falsy-looking value as off so hand-built query strings
    or a future hidden-fallback-input pattern behave correctly.
    """
    return raw is not None and raw.strip().lower() not in _FALSY_FLAG_VALUES | {""}


_FIXED_FORM_FIELDS = frozenset(
    {
        "strategy_id",
        "profile_hash",
        "activation_seq",
        "start_month",
        "end_month",
        "base_currency",
        "starting_capital",
        "idempotency_key",
        "security_ids",
        "whole_universe",
        # Search-only UI state from the universe selector. It is submitted
        # because the selector lives inside the launch form, but it is not
        # part of the durable backtest command.
        "q",
    }
)


def _configuration_context(
    launch: BacktestLaunchService,
    backtest_repo: BacktestRepository,
    **extra: object,
) -> dict[str, object]:
    view = launch.configuration()
    selected_id = cast(str | None, extra.get("selected_strategy_id"))
    if selected_id is None and view.strategies:
        selected_id = view.strategies[0].strategy_id
    selected = next(
        (item for item in view.strategies if item.strategy_id == selected_id), None
    )
    extra.setdefault("idempotency_key", str(uuid4()))
    # Default the "whole universe" toggle to on for a genuinely fresh
    # render; the POST-error re-render path passes the submitted state
    # explicitly via ``extra`` so it round-trips instead of resetting.
    extra.setdefault("whole_universe", True)
    enum_options: dict[str, tuple[tuple[str, str], ...]] = {}
    enum_default_tokens: dict[str, str] = {}
    if selected is not None:
        for parameter in selected.parameters:
            if parameter.type != "enum":
                continue
            enum_options[parameter.name] = tuple(
                (str(index), str(candidate))
                for index, candidate in enumerate(parameter.enum_values or ())
            )
            token = _enum_token(parameter, parameter.default)
            if token is not None:
                enum_default_tokens[parameter.name] = token
    period_options: tuple[tuple[object, tuple[str, ...]], ...] = ()
    if view.coverage is not None:
        period_options = tuple(
            (
                interval,
                TradingCalendar.months_inclusive(
                    interval.start_month, interval.end_month
                ),
            )
            for interval in view.coverage.intervals
        )
    # Story 4.5: universe selector data
    active = backtest_repo.active_snapshot_profile()
    securities: list[tuple[str, str, str, str]] = []
    if active is not None:
        securities = backtest_repo.roster_member_identities(active.profile_hash)
    return {
        "strategies": view.strategies,
        "warnings": view.warnings,
        "coverage": view.coverage,
        "coverage_error": view.coverage_error,
        "profile": view.profile,
        "selected": selected,
        "period_options": period_options,
        "enum_options": enum_options,
        "enum_default_tokens": enum_default_tokens,
        "securities": securities,
        "active_profile": active,
        "values": {
            "start_month": "",
            "end_month": "",
            "base_currency": "GBP",
            "starting_capital": "",
        },
        "parameter_values": {},
        "errors": {},
        **extra,
    }


def _enum_token(parameter: StrategyParameterV1, value: object) -> str | None:
    """Return the opaque option-index token matching ``value``, if any --
    the inverse of :func:`_decode_parameter`'s enum branch, used only to
    re-render an already-decoded value (e.g. a declared default)."""
    for index, candidate in enumerate(parameter.enum_values or ()):
        if type(value) is type(candidate) and value == candidate:
            return str(index)
    return None


def _decode_parameter(
    parameter: StrategyParameterV1, raw: str
) -> tuple[JsonValue | None, str | None]:
    """Decode one raw form string into ``parameter``'s exact declared JSON
    scalar type (Story 2.7 AC 6) -- never reinterprets constraints, that
    stays exclusively in ``validate_strategy_parameters``. Returns
    ``(value, None)`` or ``(None, error_message)``.
    """
    if parameter.type == "boolean":
        if raw == "true":
            return True, None
        if raw == "false":
            return False, None
        return None, "Choose true or false."
    if parameter.type == "integer":
        if not _INTEGER_RE.fullmatch(raw):
            return None, "Enter a whole number."
        return int(raw), None
    if parameter.type == "number":
        if not _NUMBER_RE.fullmatch(raw):
            return None, "Enter a decimal number."
        return float(raw), None
    if parameter.type == "string":
        return raw, None
    # "enum": raw is an opaque index token into parameter.enum_values --
    # never the raw text, so "true"/"1"/"1.0"/'"1"' can never collide.
    if not _INDEX_RE.fullmatch(raw):
        return None, "Choose one of the listed options."
    values = parameter.enum_values or ()
    index = int(raw)
    if index >= len(values):
        return None, "Choose one of the listed options."
    return values[index], None


def _decode_launch_form(
    form: FormData, strategy: StrategyDescriptorV1 | None
) -> tuple[
    BacktestLaunchCommandV1 | None, dict[str, str], dict[str, str], dict[str, str]
]:
    """Decode a raw submitted form into a typed launch command.

    Returns ``(command_or_None, raw_values, parameter_raw_values,
    field_errors)``. Duplicate or unknown fields (relative to the fixed
    fields plus the currently-selected Strategy's own declared
    parameters) are rejected rather than silently dropped or overwritten.
    """
    expected = set(_FIXED_FORM_FIELDS)
    if strategy is not None:
        expected |= {f"{_PARAM_FIELD_PREFIX}{p.name}" for p in strategy.parameters}

    errors: dict[str, str] = {}
    counts: dict[str, int] = {}
    for key, _value in form.multi_items():
        counts[key] = counts.get(key, 0) + 1
    form_problems: list[str] = []
    for key, count in counts.items():
        if count > 1 and key != "security_ids":
            form_problems.append(f"{key!r} was submitted more than once.")
        if key not in expected:
            form_problems.append(f"Unknown field submitted: {key!r}.")
    if form_problems:
        errors["form"] = " ".join(form_problems)

    def _get(name: str) -> str:
        value = form.get(name)
        return value if isinstance(value, str) else ""

    strategy_id = _get("strategy_id")
    profile_hash = _get("profile_hash") or None
    start_month = _get("start_month")
    end_month = _get("end_month")
    base_currency_raw = _get("base_currency") or "GBP"
    starting_capital_raw = _get("starting_capital")
    idempotency_key = _get("idempotency_key") or None

    raw_values = {
        "start_month": start_month,
        "end_month": end_month,
        "base_currency": base_currency_raw,
        "starting_capital": starting_capital_raw,
    }

    base_currency: Literal["GBP", "USD"] = "GBP"
    if base_currency_raw not in ("GBP", "USD"):
        errors["base_currency"] = "Choose GBP or USD."
    else:
        base_currency = cast(Literal["GBP", "USD"], base_currency_raw)

    capital: Decimal | None = None
    if not _NUMBER_RE.fullmatch(starting_capital_raw or ""):
        errors["starting_capital"] = "Enter a positive amount, e.g. 10000.00."
    else:
        try:
            capital = Decimal(starting_capital_raw)
        except InvalidOperation:
            errors["starting_capital"] = "Enter a positive amount, e.g. 10000.00."

    parameters: dict[str, JsonValue] = {}
    parameter_raw: dict[str, str] = {}
    if strategy is not None:
        for parameter in strategy.parameters:
            field = f"{_PARAM_FIELD_PREFIX}{parameter.name}"
            if field not in form:
                continue
            submitted = _get(field)
            parameter_raw[parameter.name] = submitted
            value, error = _decode_parameter(parameter, submitted)
            if error is not None:
                errors[field] = error
            else:
                parameters[parameter.name] = cast(JsonValue, value)

    if not strategy_id:
        errors.setdefault("strategy_id", "Choose a Strategy.")
    elif strategy is None:
        # The rendered Strategy is no longer discoverable (e.g. its Skill
        # was removed between form render and submit) -- say so directly
        # rather than letting its now-orphaned param__* fields surface as
        # a confusing pile of "Unknown field submitted" errors instead.
        errors.setdefault(
            "strategy_id",
            "This Strategy is no longer available. Choose a Strategy again.",
        )

    if errors or strategy is None or capital is None:
        return None, raw_values, parameter_raw, errors

    command = BacktestLaunchCommandV1(
        strategy_id=strategy_id,
        rendered_profile_hash=profile_hash,
        start_month=start_month,
        end_month=end_month,
        base_currency=base_currency,
        starting_capital=capital,
        parameters=parameters,
        idempotency_key=idempotency_key,
    )
    return command, raw_values, parameter_raw, errors


@router.get("/strategy-manager/configuration", response_class=HTMLResponse)
async def strategy_configuration(
    request: Request,
    launch: LaunchDep,
    backtest: BacktestDep,
    strategy_id: str | None = None,
) -> HTMLResponse:
    """Render the launch form: one fresh discovery + coverage projection
    (Story 2.7 AC 1)."""
    context = _configuration_context(launch, backtest, selected_strategy_id=strategy_id)
    return templates.TemplateResponse(request, "_strategy_configuration.html", context)


@router.get("/strategy-manager/configuration/fields", response_class=HTMLResponse)
async def strategy_configuration_fields(
    request: Request,
    launch: LaunchDep,
    backtest: BacktestDep,
    strategy_id: str | None = None,
    start_month: str = "",
    end_month: str = "",
    base_currency: str = "GBP",
    starting_capital: str = "",
) -> HTMLResponse:
    """Re-render only the parameter/period section for the selected
    Strategy (htmx partial swap on radio change) -- metadata-driven, no
    Strategy-ID branches (Story 2.7 AC 2).

    The Period/Capital/Currency fields are unrelated to which Strategy is
    selected, so the triggering radio's ``hx-include`` carries their
    currently-entered values through this swap rather than discarding
    them back to blank defaults.
    """
    context = _configuration_context(
        launch,
        backtest,
        selected_strategy_id=strategy_id,
        values={
            "start_month": start_month,
            "end_month": end_month,
            "base_currency": base_currency or "GBP",
            "starting_capital": starting_capital,
        },
    )
    return templates.TemplateResponse(
        request, "_strategy_configuration_fields.html", context
    )


@router.post(
    "/strategy-manager/configuration",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def submit_strategy_configuration(
    request: Request, launch: LaunchDep, backtest: BacktestDep
) -> Response:
    """Validate and launch a Backtest (Story 2.7 AC 6-8).

    422 with a focused, linked error summary on any correctable failure
    (preserving submitted safe values and the selected Strategy); 303 to
    the durable Activity URL only after an accepted enqueue. Never
    persists anything before every validation step has passed.
    """
    form = await request.form()
    submitted_strategy_id = form.get("strategy_id")
    strategy = None
    if isinstance(submitted_strategy_id, str) and submitted_strategy_id:
        strategy = next(
            (
                item
                for item in launch.discover().strategies
                if item.strategy_id == submitted_strategy_id
            ),
            None,
        )
    command, raw_values, parameter_raw, errors = _decode_launch_form(form, strategy)
    # "whole universe" toggle -- rendered back on every response
    # (success or error) so it round-trips through htmx partial swaps.
    submitted_whole_universe = form.get("whole_universe")
    whole_universe = _is_truthy_flag(
        submitted_whole_universe if isinstance(submitted_whole_universe, str) else None
    )

    def submitted_context() -> dict[str, object]:
        """Build the expensive full coverage view only for a re-render.

        A successful submit does not need the configuration page again. In
        particular, it must not verify every historical snapshot once here
        and then verify the requested interval again in ``launch``.
        """
        return _configuration_context(
            launch,
            backtest,
            selected_strategy_id=submitted_strategy_id
            if isinstance(submitted_strategy_id, str)
            else None,
            values=raw_values,
            parameter_values=parameter_raw,
            idempotency_key=form.get("idempotency_key") or str(uuid4()),
            whole_universe=whole_universe,
        )

    # Story 4.5: validate universe selection
    raw_security_ids: list[str] = [
        v if isinstance(v, str) else v.filename or ""
        for v in form.getlist("security_ids")
    ]
    submitted_profile_hash_raw = form.get("profile_hash")
    submitted_profile_hash = (
        submitted_profile_hash_raw
        if isinstance(submitted_profile_hash_raw, str)
        else ""
    )
    submitted_activation_seq_raw = form.get("activation_seq")
    try:
        submitted_activation_seq = int(
            submitted_activation_seq_raw
            if isinstance(submitted_activation_seq_raw, str)
            else "0"
        )
    except ValueError:
        submitted_activation_seq = 0
    active = backtest.active_snapshot_profile()
    if active is None:
        errors.setdefault("security_ids", "No active profile is configured.")
    else:
        # Stale profile detection
        if (
            active.profile_hash != submitted_profile_hash
            or active.activation_seq != submitted_activation_seq
        ):
            errors.setdefault(
                "security_ids",
                "The active profile has changed. Please reselect securities.",
            )
        elif whole_universe:
            # Never trust a client-submitted list of security IDs for
            # whole-universe mode -- resolve fresh from the roster
            # from the current active roster. The resolved set is definitionally
            # in-roster, so the "unknown securities" check below is
            # skipped for this branch.
            raw_security_ids = [
                sid
                for sid, _ps, _mic, _cur in backtest.roster_member_identities(
                    active.profile_hash
                )
            ]
            if not raw_security_ids:
                errors.setdefault(
                    "security_ids",
                    "The active roster has no securities to select.",
                )
        elif raw_security_ids:
            # Validate all submitted IDs are in the roster
            roster_ids = {
                sid
                for sid, _ps, _mic, _cur in (
                    backtest.roster_member_identities(active.profile_hash)
                )
            }
            unknown = [sid for sid in raw_security_ids if sid not in roster_ids]
            if unknown:
                errors.setdefault(
                    "security_ids",
                    f"Unknown securities: {', '.join(unknown[:5])}",
                )
    if not raw_security_ids and "security_ids" not in errors:
        errors.setdefault("security_ids", "Select at least one security.")
    if errors or command is None:
        return templates.TemplateResponse(
            request,
            "_strategy_configuration.html",
            {**submitted_context(), "errors": errors},
            status_code=422,
        )
    # Canonicalize the universe
    try:
        canonical = canonical_run_universe(raw_security_ids)
    except RunUniverseError as exc:
        return templates.TemplateResponse(
            request,
            "_strategy_configuration.html",
            {**submitted_context(), "errors": {"security_ids": str(exc)}},
            status_code=422,
        )
    # Bind the universe into the strategy's host-bound parameter
    if strategy is not None and command is not None:
        universe_binding = strategy.bind_universe(canonical)
        merged_params = dict(command.parameters)
        merged_params.update(universe_binding)
        selection = RunUniverseSelectionV1(
            profile_hash=submitted_profile_hash,
            activation_seq=submitted_activation_seq,
            universe_schema=strategy.universe.schema_version,
            universe_mode=strategy.universe.mode,
            universe_parameter=strategy.universe.parameter,
            canonical_security_ids=canonical,
            run_universe_digest=run_universe_digest(
                canonical,
                universe_schema=strategy.universe.schema_version,
                mode=strategy.universe.mode,
                parameter=strategy.universe.parameter,
                profile_hash=submitted_profile_hash,
            ),
        )
        command = replace(
            command, parameters=merged_params, universe_selection=selection
        )
    try:
        result = launch.launch(command)
    except BacktestLaunchValidationError as exc:
        field_errors = {error.field: error.message for error in exc.errors}
        return templates.TemplateResponse(
            request,
            "_strategy_configuration.html",
            {**submitted_context(), "errors": field_errors},
            status_code=422,
        )
    return RedirectResponse(
        f"/strategy-manager/activities/{result.job.id}", status_code=303
    )


@router.get("/strategy-manager/backtests", response_class=HTMLResponse)
async def strategy_backtests(request: Request, backtest: BacktestDep) -> HTMLResponse:
    """Render the standalone Backtest results list (Story 2.8 AC 2, 7)."""
    return templates.TemplateResponse(
        request, "_backtest_results_list.html", _backtest_activities_context(backtest)
    )


#: Story 2.6 AC 9: the Activity route renders a minimal, correct status
#: shell for either job type (no initialization-specific fields leak into
#: a backtest view, and vice versa) -- Stories 2.8/2.9 own list/polling/
#: Result presentation.
_ACTIVITY_TEMPLATES: dict[StrategyJobType, str] = {
    StrategyJobType.BOOTSTRAP: "_bootstrap_activity.html",
    StrategyJobType.PREPARATION: "_preparation_activity.html",
    StrategyJobType.INITIALIZATION: "_initialization_activity.html",
    StrategyJobType.BACKTEST: "_backtest_activity.html",
}


def _activity_context(
    repo: BacktestRepository, service: StrategyJobService, job_id: str
) -> dict[str, object]:
    job = repo.strategy_job(job_id)
    if job.job_type is StrategyJobType.INITIALIZATION:
        run: object = repo.initialization_run(job_id)
    elif job.job_type is StrategyJobType.BOOTSTRAP:
        run = repo.bootstrap_run(job_id)
    elif job.job_type is StrategyJobType.BACKTEST:
        run = repo.strategy_run(job_id)
    elif job.job_type is StrategyJobType.PREPARATION:
        run = repo.preparation_run(job_id)
    else:
        raise StrategyJobNotFound("activity not found")
    #: Story 2.8 AC 3 / Story 2.9: a completed Backtest exposes a named
    #: review destination -- the real standalone Result URL this story
    #: adds, never the Activity shell (which Story 2.9 now redirects away
    #: from for a complete job).
    review_url = (
        f"/strategy-manager/results/{job_id}"
        if job.job_type is StrategyJobType.BACKTEST
        and job.status is StrategyJobStatus.COMPLETE
        else None
    )
    child_id = (
        repo.preparation_child_backtest_id(job_id)
        if job.job_type is StrategyJobType.PREPARATION
        else None
    )
    return {
        "job": job,
        "run": run,
        "actions": service.legal_actions(job_id).legal_actions,
        "terminal": job.status in _TERMINAL,
        "retry_setup_idempotency_key": (
            str(uuid4())
            if job.job_type is StrategyJobType.BOOTSTRAP
            and job.status is StrategyJobStatus.FAILED
            else None
        ),
        "review_url": review_url,
        "child_url": f"/strategy-manager/activities/{child_id}" if child_id else None,
    }


def _activity_template(job_type: StrategyJobType) -> str:
    return _ACTIVITY_TEMPLATES[job_type]


@router.get("/strategy-manager/activities/{job_id}", response_class=HTMLResponse)
async def strategy_activity(
    request: Request, job_id: str, backtest: BacktestDep, jobs: JobsDep
) -> Response:
    try:
        context = _activity_context(backtest, jobs, job_id)
        job = cast(StrategyJobV1, context["job"])
        return templates.TemplateResponse(
            request, _activity_template(job.job_type), context
        )
    except StrategyJobNotFound:
        return HTMLResponse("Activity no longer available.", status_code=404)


@router.get("/strategy-manager/activities/{job_id}/status", response_class=HTMLResponse)
async def strategy_activity_status(
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
    return templates.TemplateResponse(
        request, _activity_template(job.job_type), context
    )


@router.post(
    "/strategy-manager/activities/{job_id}/cancel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def cancel_strategy_job(
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
        context = _activity_context(backtest, jobs, job_id)
        job = cast(StrategyJobV1, context["job"])
        return templates.TemplateResponse(
            request, _activity_template(job.job_type), context
        )
    except (StrategyJobConflict, StrategyJobNotFound, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=409)


@router.post(
    "/strategy-manager/activities/{job_id}/restart",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def restart_strategy_job(
    request: Request,
    job_id: str,
    backtest: BacktestDep,
    jobs: JobsDep,
    expected_version: Annotated[int, Form()],
) -> Response:
    try:
        source = backtest.strategy_job(job_id)
        restart_request = StrategyJobRestartV1(
            source_job_id=job_id,
            expected_version=expected_version,
            idempotency_key=str(uuid4()),
        )
        result = (
            jobs.restart_backtest(restart_request)
            if source.job_type is StrategyJobType.BACKTEST
            else jobs.restart_initialization(restart_request)
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
    backtest: BacktestDep,
    jobs: JobsDep,
    expected_version: Annotated[int, Form()],
) -> HTMLResponse:
    try:
        job_type = backtest.strategy_job(job_id).job_type
        jobs.delete_job(
            StrategyJobDeletionV1(job_id=job_id, expected_version=expected_version)
        )
    except (StrategyJobConflict, StrategyJobNotFound, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=409)
    if job_type is StrategyJobType.BOOTSTRAP:
        return HTMLResponse(
            '<h2 id="setup-history" tabindex="-1">Setup history</h2><p>Activity deleted.</p><a href="/strategy-manager/setup" hx-get="/strategy-manager/setup" hx-target="#tab-content" hx-swap="innerHTML">Set up Strategy Manager</a>'
        )
    return HTMLResponse(
        '<h2 id="initialization-history" tabindex="-1">Initialization history</h2><p>Activity deleted.</p>'
    )


# ---------------------------------------------------------------------------
# Story 2.9: standalone completed-Backtest Result review + note CAS
# ---------------------------------------------------------------------------


def _result_context(repo: BacktestRepository, run_id: str) -> dict[str, object]:
    """Build the Result page's full context from Story 2.5's aggregate
    alone -- every Metrics/Equity-Curve/Trade-Log/provenance value is
    formatted, never recomputed, by ``result_presenter.py``."""
    try:
        result = repo.backtest_result(run_id)
    except StrategyJobNotFound as exc:
        # The caller already confirmed job.status is COMPLETE, so a
        # missing Result row here is a genuine integrity defect (Story
        # 2.8's own "complete job with missing Result is an integrity
        # error" boundary), never a 404/partial render.
        raise BacktestIntegrityError(
            f"backtest result evidence is missing for a complete job: {exc}"
        ) from exc
    coverage = repo.snapshot_coverage(profile_hash=result.profile_hash)
    return {
        "run_id": run_id,
        "integrity_error": None,
        "result": result,
        "metrics": metrics_view(result),
        "equity_curve_payload": equity_curve_payload(result),
        "trade_log": trade_log_view(result),
        "provenance": provenance_view(result, coverage),
        "note": note_view(result),
        "submitted_note_text": None,
        "note_error": None,
        "note_conflict": False,
        "note_saved": False,
    }


def _note_context(
    repo: BacktestRepository,
    run_id: str,
    *,
    result: BacktestResultV1 | None = None,
    submitted_text: str | None = None,
    error: str | None = None,
    conflict: bool = False,
    saved: bool = False,
) -> dict[str, object]:
    """Build ``_backtest_note.html``'s context for the guarded POST route.

    ``submitted_text`` -- this request's own just-submitted (unescaped,
    not-yet-persisted) text -- is what the textarea always renders on a
    422/409 retry. The read-state line and hidden expected-version field,
    by contrast, always reflect a fresh read of the current persisted
    Result (never the failed submission), so a Retry naturally carries the
    correct ``expected_note_version`` forward.

    ``result`` lets a caller that already holds a freshly-verified
    ``BacktestResultV1`` (e.g. the successful CAS write's own return
    value) pass it straight through instead of triggering a redundant
    ``backtest_result()`` re-read/re-verification.
    """
    try:
        current = note_view(
            result if result is not None else repo.backtest_result(run_id)
        )
    except (BacktestIntegrityError, StrategyJobNotFound):
        current = None
    return {
        "run_id": run_id,
        "note": current,
        "submitted_note_text": submitted_text,
        "note_error": error,
        "note_conflict": conflict,
        "note_saved": saved,
    }


@router.get("/strategy-manager/results/{run_id}", response_class=HTMLResponse)
async def backtest_result_view(
    request: Request, run_id: str, backtest: BacktestDep
) -> Response:
    """Render a completed Backtest's standalone Result page (AC 1).

    Non-complete/non-Backtest jobs redirect to the existing Activity
    shell (never a partial Result render); an unknown ``run_id`` 404s;
    missing/corrupt Result evidence renders an explicit integrity-error
    state with no partial data.
    """
    try:
        job = backtest.strategy_job(run_id)
    except StrategyJobNotFound:
        return HTMLResponse("Backtest result not found.", status_code=404)
    if (
        job.job_type is not StrategyJobType.BACKTEST
        or job.status is not StrategyJobStatus.COMPLETE
    ):
        return RedirectResponse(
            f"/strategy-manager/activities/{run_id}", status_code=303
        )
    try:
        context = _result_context(backtest, run_id)
    except BacktestIntegrityError as exc:
        return templates.TemplateResponse(
            request,
            "_backtest_result.html",
            {"run_id": run_id, "integrity_error": str(exc)},
        )
    return templates.TemplateResponse(request, "_backtest_result.html", context)


@router.post(
    "/strategy-manager/results/{run_id}/note",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def submit_backtest_result_note(
    request: Request,
    run_id: str,
    backtest: BacktestDep,
    note: Annotated[str, Form()] = "",
    expected_note_version: Annotated[int, Form()] = 0,
) -> HTMLResponse:
    """Guarded note compare-and-swap (AC 7, 8) -- calls Story 2.5's
    ``update_backtest_result_note`` exactly, retaining the submitted text
    and an explicit error/conflict + Retry on 422/409, never announcing a
    false Saved.
    """
    try:
        result = backtest.update_backtest_result_note(
            run_id, expected_note_version=expected_note_version, note=note
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "_backtest_note.html",
            _note_context(backtest, run_id, submitted_text=note, error=str(exc)),
            status_code=422,
        )
    except StrategyJobConflict:
        return templates.TemplateResponse(
            request,
            "_backtest_note.html",
            _note_context(
                backtest,
                run_id,
                submitted_text=note,
                error=(
                    "This note changed since you loaded it. Retry to save "
                    "your text against the current version."
                ),
                conflict=True,
            ),
            status_code=409,
        )
    except StrategyJobNotFound:
        return templates.TemplateResponse(
            request,
            "_backtest_note.html",
            _note_context(
                backtest,
                run_id,
                submitted_text=note,
                error="Backtest result not found.",
            ),
            status_code=404,
        )
    except BacktestIntegrityError as exc:
        # The CAS write itself succeeded, but the post-write re-read
        # (``update_backtest_result_note``'s own ``backtest_result()``
        # call) detected pre-existing corrupt evidence unrelated to the
        # note. Report it explicitly rather than letting it propagate as
        # an unhandled 500 -- the story's own "explicit error, never a
        # false Saved" contract applies here too.
        return templates.TemplateResponse(
            request,
            "_backtest_note.html",
            _note_context(
                backtest,
                run_id,
                submitted_text=note,
                error=f"Result evidence needs attention: {exc}",
            ),
            status_code=500,
        )
    return templates.TemplateResponse(
        request,
        "_backtest_note.html",
        _note_context(backtest, run_id, result=result, saved=True),
    )


# ---------------------------------------------------------------------------
# Story 3.2: Compare picker -- choose an eligible Result to compare against
# ---------------------------------------------------------------------------


def _reraise_vanished_evidence(exc: StrategyJobNotFound) -> BacktestIntegrityError:
    """Wrap a complete job's vanished Result exactly like ``_result_context``
    does -- a missing Result row for a job already confirmed complete is a
    genuine integrity defect, never a 404/partial render."""
    return BacktestIntegrityError(
        f"backtest result evidence is missing for a complete job: {exc}"
    )


def _compare_integrity_response(
    request: Request, run_id: str, exc: BacktestIntegrityError
) -> HTMLResponse:
    """Render the Compare picker's explicit integrity-error branch --
    the single call site every corrupt-evidence path in this section
    routes through, so none of them can silently diverge."""
    return templates.TemplateResponse(
        request,
        "_compare_picker.html",
        {"run_id": run_id, "integrity_error": str(exc)},
    )


def _compare_context(repo: BacktestRepository, run_id: str) -> dict[str, object]:
    """Build the Compare picker's context: the anchor Result's own
    display fields plus Story 3.1's ``comparison_candidates(run_id)`` --
    a pure read, never a mutation, and the sole candidate-listing call
    (AC 1, 2, 3). No candidate is ever preselected/defaulted here."""
    try:
        anchor = repo.backtest_result(run_id)
    except StrategyJobNotFound as exc:
        raise _reraise_vanished_evidence(exc) from exc
    return {
        "run_id": run_id,
        "integrity_error": None,
        "anchor": anchor,
        "candidates": repo.comparison_candidates(run_id),
        "picker_error": None,
    }


def _revalidate_eligibility(
    repo: BacktestRepository, run_id: str, candidate_run_id: str
) -> ComparisonEligibilityV1:
    """Revalidate eligibility at submit time (AC 4, 5) -- the sole call
    site for ``is_comparable``; a rendered candidate must never be
    trusted as still-eligible. Mirrors ``_compare_context``'s wrapping
    of a vanished/corrupt Result into an explicit integrity error."""
    try:
        return repo.is_comparable(run_id, candidate_run_id)
    except StrategyJobNotFound as exc:
        raise _reraise_vanished_evidence(exc) from exc


@router.get("/strategy-manager/compare", response_class=HTMLResponse)
async def compare_picker(
    request: Request,
    backtest: BacktestDep,
    run_id: str,
    reason: str | None = None,
) -> Response:
    """Render the Compare picker for a completed Backtest Result (Story
    3.2 AC 1-3).

    A non-existent/non-complete/non-Backtest anchor redirects to the
    existing Activity shell, matching the read-only redirect pattern
    Story 2.9's ``backtest_result_view`` already established; corrupt
    anchor evidence (a malformed job row, or a vanished/corrupt Result)
    renders the explicit integrity-error branch instead of raising.

    ``reason`` (Story 3.3) is the optional query param the Comparison
    route's ``303`` redirects an ineligible/stale pair back through --
    when present it populates ``picker_error`` exactly like a failed
    submit's re-render does, so the existing ``role="alert"
    tabindex="-1"`` template branch needs no change.
    """
    try:
        job = backtest.strategy_job(run_id)
        anchor_ready = (
            job.job_type is StrategyJobType.BACKTEST
            and job.status is StrategyJobStatus.COMPLETE
        )
    except StrategyJobNotFound:
        anchor_ready = False
    except BacktestIntegrityError as exc:
        return _compare_integrity_response(request, run_id, exc)
    if not anchor_ready:
        return RedirectResponse(
            f"/strategy-manager/activities/{run_id}", status_code=303
        )
    try:
        context = _compare_context(backtest, run_id)
    except BacktestIntegrityError as exc:
        return _compare_integrity_response(request, run_id, exc)
    if reason:
        context = {**context, "picker_error": reason}
    return templates.TemplateResponse(request, "_compare_picker.html", context)


@router.post(
    "/strategy-manager/compare",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def submit_compare(
    request: Request,
    backtest: BacktestDep,
    run_id: Annotated[str, Form()],
    candidate_run_id: Annotated[str, Form()] = "",
) -> Response:
    """Guarded Compare submit (AC 4, 5) -- revalidates eligibility via
    ``is_comparable`` on every submit (never trusting a rendered
    candidate), redirecting to Story 3.3's Comparison URL only when
    ``eligible=True``. A missing/malformed/stale/ineligible
    ``candidate_run_id`` re-renders the picker with a linked error and a
    freshly re-fetched candidate list at 422; a broken anchor surfaces
    the same explicit integrity-error branch the GET route uses. Neither
    the Result, its note, nor its evidence manifest is ever mutated
    here.
    """
    try:
        eligibility = _revalidate_eligibility(backtest, run_id, candidate_run_id)
    except BacktestIntegrityError as exc:
        return _compare_integrity_response(request, run_id, exc)
    if eligibility.eligible:
        return RedirectResponse(
            f"/strategy-manager/comparisons/{run_id}/{candidate_run_id}",
            status_code=303,
        )
    try:
        context = _compare_context(backtest, run_id)
    except BacktestIntegrityError as exc:
        return _compare_integrity_response(request, run_id, exc)
    return templates.TemplateResponse(
        request,
        "_compare_picker.html",
        {**context, "picker_error": eligibility.detail},
        status_code=422,
    )


# ---------------------------------------------------------------------------
# Story 3.3: Comparison -- review two eligible Results side by side
# ---------------------------------------------------------------------------


def _comparison_integrity_response(
    request: Request, run_id_a: str, run_id_b: str, exc: BacktestIntegrityError
) -> HTMLResponse:
    """Render the Comparison page's explicit integrity-error branch --
    the single call site every corrupt-evidence path in this section
    routes through, mirroring ``_compare_integrity_response``'s shape."""
    return templates.TemplateResponse(
        request,
        "_comparison.html",
        {
            "run_id_a": run_id_a,
            "run_id_b": run_id_b,
            "integrity_error": str(exc),
        },
    )


def _comparison_side_context(
    repo: BacktestRepository, run_id: str
) -> dict[str, object]:
    """Build one side of the Comparison page's context: the same Story
    2.9 Metrics/Trade-Log/Provenance formatting ``_result_context`` uses,
    called once per side -- reusing ``result_presenter.py``, never a
    second formatter. Notes stay a standalone-Result (Story 2.9) concern,
    so ``note_view`` is never called here."""
    try:
        result = repo.backtest_result(run_id)
    except StrategyJobNotFound as exc:
        raise _reraise_vanished_evidence(exc) from exc
    coverage = repo.snapshot_coverage(profile_hash=result.profile_hash)
    return {
        "run_id": run_id,
        "result": result,
        "metrics": metrics_view(result),
        "trade_log": trade_log_view(result),
        "provenance": provenance_view(result, coverage),
    }


def _comparison_context(
    repo: BacktestRepository, run_id_a: str, run_id_b: str
) -> dict[str, object]:
    """Build the Comparison page's full context: both sides' independent
    presenter context (AC 1-4) plus the shared-timeline
    ``comparison_equity_payload`` (AC 3) -- a pure read, never a
    mutation of either Result, its note, or its manifest."""
    side_a = _comparison_side_context(repo, run_id_a)
    side_b = _comparison_side_context(repo, run_id_b)
    equity = comparison_equity_payload(
        cast(BacktestResultV1, side_a["result"]),
        cast(BacktestResultV1, side_b["result"]),
    )
    return {
        "run_id_a": run_id_a,
        "run_id_b": run_id_b,
        "integrity_error": None,
        "a": side_a,
        "b": side_b,
        "comparison_equity_payload": equity,
    }


@router.get(
    "/strategy-manager/comparisons/{run_id_a}/{run_id_b}",
    response_class=HTMLResponse,
)
async def comparison_view(
    request: Request, run_id_a: str, run_id_b: str, backtest: BacktestDep
) -> Response:
    """Render two eligible Backtest Results side by side (Story 3.3).

    Eligibility is revalidated fresh via ``is_comparable`` on every load
    -- this route is reachable directly/bookmarked after either Result's
    state has changed, so a stale referring picker/session is never
    trusted. An ineligible pair (including a self-comparison, which
    ``is_comparable`` already rejects) redirects back to the Compare
    picker with the reason. Corrupt/vanished evidence on either side, or
    a divergent shared-timeline equity-curve date sequence, renders this
    page's own explicit integrity-error branch with no partial data --
    read-only end to end, so neither original Result is ever touched and
    both remain independently accessible.
    """
    try:
        eligibility = _revalidate_eligibility(backtest, run_id_a, run_id_b)
    except BacktestIntegrityError as exc:
        return _comparison_integrity_response(request, run_id_a, run_id_b, exc)
    if not eligibility.eligible:
        return RedirectResponse(
            f"/strategy-manager/compare?run_id={quote(run_id_a)}"
            f"&reason={quote(eligibility.detail)}",
            status_code=303,
        )
    try:
        context = _comparison_context(backtest, run_id_a, run_id_b)
    except BacktestIntegrityError as exc:
        return _comparison_integrity_response(request, run_id_a, run_id_b, exc)
    return templates.TemplateResponse(request, "_comparison.html", context)
