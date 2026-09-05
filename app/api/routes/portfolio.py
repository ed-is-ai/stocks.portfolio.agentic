"""Portfolio routes — live price refresh and SIPP CSV import."""

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse

from app.agents.trader.trader_agent import OpeningLotDuplicateError, SippImportError
from app.api.dependencies import (
    get_notifications_repository,
    get_portfolio_service,
    get_realised_pnl_service,
    get_trader_service,
)
from app.api.params import optional_int
from app.api.templating import templates
from app.core.config import (
    IMPORTED_FILES_DIR,
    IMPORTED_FILES_RETENTION_DAYS,
    imported_file_max_bytes,
)
from app.core.security import require_local_or_token
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.schemas.trade import Trade
from app.services.portfolio_import.contract_registry import ContractRegistryError
from app.services.portfolio_import.registry_loader import get_contract_registry
from app.services.portfolio_service import PortfolioService
from app.services.realised_pnl_service import RealisedPnlService
from app.services.trader_service import TraderService

router = APIRouter()
logger = logging.getLogger(__name__)

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]
RealisedPnlDep = Annotated[RealisedPnlService, Depends(get_realised_pnl_service)]
NotificationsDep = Annotated[
    NotificationsRepository, Depends(get_notifications_repository)
]


def _run_snapshot_backfill(trader: TraderService, portfolio_id: int) -> None:
    """Run the pre-live-writer snapshot backfill, swallowing every failure (#502).

    Scheduled via ``BackgroundTasks`` after a SIPP import or a price refresh so
    the primary response is never blocked, and an exception here is logged
    only -- it must never change the HTTP status the user already received.
    """
    try:
        report = trader.backfill_snapshots(portfolio_id)
    except Exception:
        logger.exception("Snapshot backfill after portfolio update failed")
        return
    if report.fetch_failures or report.newly_unavailable:
        logger.warning(
            "Snapshot backfill left evidence gaps for portfolio %s: "
            "fetch_failures=%s newly_unavailable=%s",
            portfolio_id,
            report.fetch_failures,
            report.newly_unavailable,
        )


#: Cap on how many individual row/value issues to spell out in the import
#: message and notification body before falling back to "(+N more)".
_MAX_ISSUE_DETAILS = 5

#: Cap on how many failed-row entries the rejected-import page lists in
#: full (GH-308/AC4) -- a separate, larger bound than `_MAX_ISSUE_DETAILS`
#: above, which caps only the short prose summary sent to the notification
#: centre. Named (not inlined) so the two caps stay easy to tell apart.
_MAX_FAILED_ROWS_LISTED = 200


#: Anything other than alphanumerics/dot/dash/underscore in an uploaded
#: filename is collapsed to "_" before it's used as part of an archive path,
#: so a crafted filename (e.g. containing "../") can't escape
#: IMPORTED_FILES_DIR or clash with shell/filesystem-special characters.
_UNSAFE_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _prune_old_archives() -> None:
    """Delete archived files older than ``IMPORTED_FILES_RETENTION_DAYS``.

    Mirrors ``NotificationsRepository``'s prune-on-write precedent
    (``app/repositories/notifications_repo.py``) rather than adding a
    scheduler — there is no cron/lifespan/startup-task infrastructure
    anywhere in this app. This means a file only becomes *eligible* for
    removal at the retention cutoff; it is actually deleted the next time
    any successful import calls ``_archive_upload``, which may be later
    than exactly the cutoff if no further imports happen.
    """
    cutoff = time.time() - IMPORTED_FILES_RETENTION_DAYS * 86400
    for path in IMPORTED_FILES_DIR.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            pass  # already removed by a concurrent prune
        except OSError:
            logger.exception("Failed to prune archived file %s", path)


def _archive_upload(filename: str, content: bytes) -> None:
    """Save a timestamped copy of a successfully-committed import file.

    Called exactly once per import, only after ``trader.import_sipp`` has
    returned a non-``"rejected"`` result — never for an upload that failed
    validation or whose plan was rejected under the whole-plan-failure rule
    (AC1/AC2). The archived file is guaranteed to correspond to committed
    database state (or, for ``status="error"``, a miscounted-but-committed
    one — see the call site). Also enforces a configured max size (AC4) and
    prunes files past the retention window (AC3). Best effort throughout —
    a failure here must not fail an otherwise-successful import (AC5).
    """
    try:
        max_bytes = imported_file_max_bytes()
        if len(content) > max_bytes:
            logger.warning(
                "Skipping archive of %s: %d bytes exceeds max %d bytes",
                filename,
                len(content),
                max_bytes,
            )
        else:
            IMPORTED_FILES_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = _UNSAFE_FILENAME_CHARS_RE.sub("_", filename) or "upload.csv"
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            # A random suffix, not just the microsecond timestamp, guards
            # against two same-named files in one multi-file queue import
            # landing in the same microsecond and clobbering each other's
            # archive copy.
            unique = uuid.uuid4().hex[:8]
            (IMPORTED_FILES_DIR / f"{stamp}_{unique}_{safe_name}").write_bytes(content)
        # Prune on every archive attempt, not just a successful write, so a
        # stretch of only-oversized uploads doesn't starve retention (AC3) --
        # a small, deliberate deviation from "prune after a successful write"
        # that keeps AC3's "eligible, not exact" removal timing intact.
        _prune_old_archives()
    except Exception:
        logger.exception("Failed to archive imported file %s", filename)


def _describe_issues(label: str, issues: list[str]) -> str:
    """Format a skipped-rows/parse-errors list with per-row detail.

    Each entry already reads like ``"row REF: unparseable quantity 'abc'"``
    (see ``TraderAgent.import_sipp``) — this just joins a bounded number of
    them under a count-labelled prefix so the reason (which row, which
    column, what value) is visible rather than only a bare count (#185).
    """
    shown = issues[:_MAX_ISSUE_DETAILS]
    detail = "; ".join(shown)
    if len(issues) > _MAX_ISSUE_DETAILS:
        detail += f"; (+{len(issues) - _MAX_ISSUE_DETAILS} more)"
    return f"{len(issues)} {label} — {detail}"


@router.post(
    "/api/portfolio/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def refresh_portfolio_prices(
    request: Request,
    trader: TraderDep,
    portfolio: PortfolioDep,
    background: BackgroundTasks,
    portfolio_id: str | None = None,
) -> HTMLResponse:
    """Fetch live prices from yfinance and return the updated portfolio partial."""
    pid = optional_int(portfolio_id)
    input_snapshot = None
    try:
        input_snapshot = portfolio.portfolio_input_snapshot(pid)
        positions = portfolio.positions_from_input_snapshot(input_snapshot)
        logger.info("Refreshing %d positions", len(positions))
        if not positions:
            context = portfolio.portfolio_partial_context(
                [],
                error_message="No positions to refresh",
                portfolio_id=pid,
                input_snapshot=input_snapshot,
            )
            return templates.TemplateResponse(
                request, "_portfolio.html", context=context, status_code=400
            )

        gbpusd = portfolio.gbpusd_rate()
        tickers = [p.ticker for p in positions]
        cached_prices, _cached_as_of, cached_display = trader.load_price_cache()
        gbp_prices, display_info, failed_tickers = (
            portfolio.fetch_all_prices_with_failures(
                tickers, portfolio.load_ticker_aliases(), gbpusd
            )
        )

        trader.save_price_cache(gbp_prices, display_info)
        _, prices_as_of, _ = trader.load_price_cache()
        # A partial provider response must not make an otherwise valid
        # holding disappear.  Fresh values win; only failed symbols inherit
        # their previous cached price/display metadata.
        effective_prices = {
            ticker: gbp_prices[ticker]
            if ticker in gbp_prices
            else cached_prices[ticker]
            for ticker in tickers
            if ticker in gbp_prices or ticker in cached_prices
        }
        effective_display = {
            ticker: display_info[ticker]
            if ticker in display_info
            else cached_display[ticker]
            for ticker in tickers
            if ticker in display_info or ticker in cached_display
        }
        cached_fallbacks = sorted(
            ticker for ticker in failed_tickers if ticker in cached_prices
        )
        unavailable = sorted(failed_tickers - set(cached_fallbacks))
        warning_parts: list[str] = []
        if cached_fallbacks:
            warning_parts.append(
                "using cached values for: " + ", ".join(cached_fallbacks)
            )
        if unavailable:
            warning_parts.append("prices unavailable for: " + ", ".join(unavailable))
        updated_positions = portfolio.positions_from_input_snapshot(
            input_snapshot, effective_prices, effective_display
        )
        logger.info("Refreshed %d positions successfully", len(updated_positions))
        cash_balance = input_snapshot.cash_balance
        trader.update_portfolio_snapshot(
            cash_balance, pid, positions=updated_positions, gbpusd=gbpusd
        )
        if pid is not None:
            background.add_task(_run_snapshot_backfill, trader, pid)
        input_snapshot = portfolio.with_current_chart_data(input_snapshot, pid)
        context = portfolio.portfolio_partial_context(
            updated_positions,
            prices_as_of=prices_as_of,
            gbpusd_rate=gbpusd,
            cash_balance=cash_balance,
            warning_message=(
                "Prices refreshed partially; " + "; ".join(warning_parts)
                if warning_parts
                else None
            ),
            portfolio_id=pid,
            input_snapshot=input_snapshot,
        )
        return templates.TemplateResponse(request, "_portfolio.html", context=context)
    except Exception as e:
        logger.exception("Failed to refresh portfolio prices: %s", e)
        cached_prices, prices_as_of, display_info = trader.load_price_cache()
        if input_snapshot is None:
            # If the initial snapshot itself could not be built, retain the
            # established fallback read. Once it exists, though, the error
            # response must render from that same request snapshot too.
            cached_positions = trader.get_portfolio(
                cached_prices or None, display_info or None, pid
            )
        else:
            cached_positions = portfolio.positions_from_input_snapshot(
                input_snapshot, cached_prices or None, display_info or None
            )
        gbpusd = cached_prices.get("__GBPUSD__")
        context = portfolio.portfolio_partial_context(
            cached_positions,
            prices_as_of=prices_as_of,
            gbpusd_rate=gbpusd,
            cash_balance=(input_snapshot.cash_balance if input_snapshot else None),
            error_message=f"Failed to fetch prices: {e}",
            portfolio_id=pid,
            input_snapshot=input_snapshot,
        )
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=500
        )


@router.post(
    "/import-sipp",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def import_sipp(
    request: Request,
    trader: TraderDep,
    portfolio: PortfolioDep,
    notifications: NotificationsDep,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    portfolio_id: Annotated[str | None, Form()] = None,
    provider_id: Annotated[str | None, Form()] = None,
    account_type_id: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Import an uploaded SIPP portfolio CSV into the selected portfolio.

    Passes the upload's own bytes straight through to
    ``TraderService.import_sipp`` — no shared filesystem path is written or
    read, so concurrent uploads (different tabs, retries, overlapping
    requests) can never read or overwrite one another's *input CSV* on disk
    (#210). A missing/unknown portfolio, a non-CSV upload, or a failing import returns
    the portfolio partial with an error message rather than raising (#147).
    A successfully-parsed-but-``status="error"`` result (row-count mismatch,
    #187) is likewise reported with a non-2xx status rather than a 200 with
    an error field the caller might not check (#210) — the multi-file queue
    (see below) relies on this to know which uploads actually failed.
    A copy is archived under ``data/imported/`` only once the import has
    actually committed — never for a rejected plan or an upload that never
    reached ``import_sipp`` at all (FR-23). On success, also records one
    Event per file in the notification centre (#184) — the multi-file
    import queue (index.html's ``handleSippImportSubmit``) calls this
    endpoint once per queued file, so this naturally yields one archived
    file and one event per file.

    Story 3.3: ``provider_id``/``account_type_id`` are the user's explicit
    selection from the import form. ``provider_id`` is validated here
    against the registry's known providers before ``trader.import_sipp`` is
    even called (AC3, no write); ``account_type_id`` is forwarded as-is and
    validated inside ``TraderAgent.import_sipp`` against the CSV's
    auto-detected contract, since only that contract (not this route) knows
    which account types are valid for it.
    """
    filename = file.filename or ""
    content = await file.read()

    pid = optional_int(portfolio_id)
    if pid is None or not trader.portfolio_exists(pid):
        context = portfolio.default_portfolio_context(pid)
        context["error_message"] = (
            "Select a portfolio to import into (create one if you have none)."
        )
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=400
        )

    if not filename.lower().endswith(".csv"):
        context = portfolio.default_portfolio_context(pid)
        context["error_message"] = "Please upload a .csv file."
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=400
        )

    try:
        known_provider_ids = {
            opt.provider_id for opt in get_contract_registry().list_providers()
        }
    except ContractRegistryError:
        # A broken/misconfigured contracts directory must not surface as a
        # raw 500 -- an empty known-set naturally falls through to the
        # same "select a provider" 400 below as any other unknown
        # provider_id, without a second error-handling path.
        logger.exception("Failed to load known import providers")
        known_provider_ids = set()
    if (
        not provider_id
        or not provider_id.strip()
        or provider_id not in known_provider_ids
    ):
        context = portfolio.default_portfolio_context(pid)
        context["error_message"] = "Select a provider to import from."
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=400
        )

    try:
        result = trader.import_sipp(content, pid, provider_id, account_type_id)
    except SippImportError as e:
        # Validation failure (e.g. missing columns, provider/account-type
        # mismatch) — show the reason verbatim; already a user-safe message.
        context = portfolio.default_portfolio_context(pid)
        context["error_message"] = str(e)
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=400
        )
    except Exception as e:
        # Never leak str(exception), file paths, or contract source
        # internals for an unexpected failure -- log the real detail
        # server-side only, and show a fixed, generic message (AC4).
        logger.exception("SIPP import failed: %s", e)
        context = portfolio.default_portfolio_context(pid)
        context["error_message"] = "Import failed — see server logs."
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=400
        )

    # "ok" and "error" both mean the plan's writes were committed -- "error"
    # is the pre-existing row-accounting-mismatch safety net (#187), not a
    # rejected plan, and having the source file archived is exactly what
    # helps diagnose why the count didn't add up. Only "rejected" (nothing
    # persisted) skips archiving (AC1/AC2).
    if result.status != "rejected":
        _archive_upload(filename, content)
        # The plan committed ("ok" or the row-accounting-mismatch "error"):
        # backfill the pre-live-writer daily snapshots off the request path so
        # this import's new trade history extends the chart's long-range
        # presets. Best-effort -- a failure here never touches the response
        # (#502).
        background.add_task(_run_snapshot_backfill, trader, pid)

    if result.status == "rejected":
        # All-or-nothing commit (#210 follow-up): at least one row failed,
        # so nothing from this plan was persisted -- never claim buy/sell/
        # cash counts (there are none), never call get_portfolio (nothing
        # changed), and surface every failing row/reason instead.
        context = portfolio.default_portfolio_context(pid)
        # All four outcomes are still reported, but framed as what *would*
        # have happened so the would-have counts can't read as a partial
        # success.
        message = (
            f"Import rejected — nothing was saved. "
            f"{result.inserted_count} row(s) would have inserted, "
            f"{result.duplicate_count} would have been duplicates, "
            f"{result.skipped_count} skipped. "
        ) + _describe_issues("row(s) failed", result.failed_rows)
        context["error_message"] = message
        context["import_inserted_count"] = result.inserted_count
        context["import_duplicate_count"] = result.duplicate_count
        context["import_skipped_count"] = result.skipped_count
        context["import_failed_count"] = len(result.failed_rows)
        context["import_status"] = result.status
        # AC4: every discoverable row issue (bounded) plus a redacted
        # preview of parsed rows, so a rejected plan shows what would have
        # been imported without ever writing anything.
        context["import_failed_rows"] = result.failed_rows[:_MAX_FAILED_ROWS_LISTED]
        context["import_failed_rows_omitted"] = max(
            0, len(result.failed_rows) - _MAX_FAILED_ROWS_LISTED
        )
        context["import_preview_rows"] = result.preview_rows
        try:
            account = trader.get_portfolio_meta(pid)
            notifications.record(
                NotificationCategory.PORTFOLIO,
                "sipp_import",
                f"SIPP CSV import rejected — {filename}",
                severity=NotificationSeverity.ERROR,
                body=f"{account.name if account else 'account'}: {message}",
                portfolio_id=pid,
            )
        except Exception:
            # Never let notification bookkeeping fail an otherwise-handled
            # rejection (matches the success path's best-effort pattern).
            logger.exception("Failed to record SIPP import rejection notification")
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=400
        )

    cash_balance = result.cash_balance
    if cash_balance == 0.0:
        # 0.0 is ambiguous: SippImportResult.cash_balance can't represent
        # "no cash balance was ever recorded" separately from a genuine
        # zero (Story 1.9 AC2) -- it's a non-optional float. Re-read
        # storage only to break that tie: get_cash_balance() returns None
        # when nothing was ever recorded, which the "is not none" template
        # gate needs to keep hiding the CASH row for e.g. a stock-only
        # import into a portfolio with no cash history at all. Scoped to
        # the 0.0 case only, not every import, and falls back to the
        # already-committed result value if the re-read itself fails, so
        # this presentation refinement never turns an otherwise-successful
        # import into a 500.
        try:
            cash_balance = trader.get_cash_balance(pid)
        except Exception:
            logger.exception("Failed to re-read cash balance after import")
    input_snapshot = portfolio.portfolio_input_snapshot(pid, cash_balance=cash_balance)
    positions = portfolio.positions_from_input_snapshot(input_snapshot)
    context = portfolio.portfolio_partial_context(
        positions,
        cash_balance=cash_balance,
        portfolio_id=pid,
        input_snapshot=input_snapshot,
    )
    # Reuses the same cash_balance the page just rendered so the response
    # message can never disagree with what the page shows (AC1) -- e.g. a
    # stock-only import must not say "cash balance £0.00" while the page
    # correctly shows no CASH row at all.
    cash_balance_text = (
        f"£{cash_balance:,.2f}" if cash_balance is not None else "none on record"
    )
    message = (
        f"Imported {result.buy_count} buy(s), {result.sell_count} sell(s), "
        f"{result.cash_flow_count} cash transaction(s); {len(positions)} open "
        f"position(s); cash balance {cash_balance_text}."
    )
    # A duplicate is reported as a duplicate, never folded into the buy/sell/
    # cash counts above — re-importing an overlapping CSV should read as
    # "already imported", not as a fresh success.
    if result.duplicate_count:
        message += f" {result.duplicate_count} duplicate(s) already imported."
    if result.skipped_count:
        message += f" {result.skipped_count} row(s) skipped."
    warnings = []
    # As of Story 1.2, a successful (non-"rejected") result from the real
    # TraderAgent.import_sipp never populates skipped_rows -- every row-level
    # problem now either rejects the whole plan (failed_rows, handled above)
    # or is a genuine no-op. This branch is kept live for a future story that
    # may reintroduce a real "skip" outcome, and for route-level tests that
    # construct a SippImportResult directly.
    if result.skipped_rows:
        warnings.append(_describe_issues("row(s) skipped", result.skipped_rows))
    if result.parse_errors:
        warnings.append(_describe_issues("value(s) unparseable", result.parse_errors))
    if warnings:
        message += " Note: " + " | ".join(warnings) + "."
    if result.status == "error":
        # The four outcome counts didn't add up to total_rows — some row was
        # silently unaccounted for (#187). Distinct from an ordinary
        # data-quality warning: this points at a bug in the import, so it's
        # called out on its own rather than folded into `warnings` above.
        accounted_rows = (
            result.inserted_count
            + result.duplicate_count
            + result.skipped_count
            + len(result.skipped_rows)
        )
        message += (
            f" ERROR: only {accounted_rows} of {result.total_rows} row(s) were "
            "accounted for — some rows may be missing from this import."
        )
    context["import_message"] = message
    # Machine-readable counts for the multi-file import queue (index.html's
    # handleSippImportSubmit), which aggregates these across sequential
    # single-file POSTs rather than parsing the prose message above.
    context["import_buy_count"] = result.buy_count
    context["import_sell_count"] = result.sell_count
    context["import_cash_count"] = result.cash_flow_count
    context["import_inserted_count"] = result.inserted_count
    context["import_duplicate_count"] = result.duplicate_count
    context["import_failed_count"] = len(result.failed_rows)
    context["import_skipped_count"] = result.skipped_count
    context["import_status"] = result.status
    # AC5: auto-detected provider/account-type/contract-version, echoed
    # from the server-validated result -- never the raw form field.
    context["import_provider_name"] = result.provider_name
    context["import_account_type"] = result.account_type_id
    context["import_contract_version"] = result.contract_version
    logger.info(
        "SIPP import: %d buys, %d sells, %d cash flows, %d duplicates, "
        "%d skipped, %d parse errors, cash £%.2f, status=%s",
        result.buy_count,
        result.sell_count,
        result.cash_flow_count,
        result.duplicate_count,
        result.skipped_count,
        len(result.parse_errors),
        result.cash_balance,
        result.status,
    )
    try:
        account = trader.get_portfolio_meta(pid)
        severity = (
            NotificationSeverity.ERROR
            if result.status == "error"
            else NotificationSeverity.WARNING
            if warnings
            else NotificationSeverity.INFO
        )
        notifications.record(
            NotificationCategory.PORTFOLIO,
            "sipp_import",
            f"SIPP CSV imported — {filename}",
            severity=severity,
            body=f"{account.name if account else 'account'}: {message}",
            portfolio_id=pid,
        )
    except Exception:
        # Never let notification bookkeeping fail an otherwise-successful
        # import (matches AlertAgent's/orchestrator's best-effort pattern).
        logger.exception("Failed to record SIPP import notification")
    # AD-25: "a rejected or failed plan returns a non-2xx response." Mapped
    # explicitly per known status rather than `!= "ok"` so a status this
    # dict doesn't recognize is unambiguously a bug (SippImportResult.status
    # only ever takes these three values) and gets a 500, not a silent 400
    # that would masquerade as an ordinary client error (#210). "rejected"
    # is listed for documentation even though that status returns from a
    # separate branch above, before this point is ever reached.
    status_code = {"ok": 200, "rejected": 400, "error": 400}.get(result.status, 500)
    return templates.TemplateResponse(
        request, "_portfolio.html", context=context, status_code=status_code
    )


def _quick_add_error(
    request: Request,
    portfolio: PortfolioService,
    message: str,
    portfolio_id: int | None = None,
) -> HTMLResponse:
    """Render the portfolio partial with a quick-add error banner."""
    context = portfolio.default_portfolio_context(portfolio_id)
    context["error_message"] = message
    return templates.TemplateResponse(
        request, "_portfolio.html", context=context, status_code=400
    )


@router.post(
    "/portfolio/quick-add",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def quick_add_holding(
    request: Request,
    trader: TraderDep,
    portfolio: PortfolioDep,
    ticker: Annotated[str, Form()],
    value: Annotated[float, Form()],
    date: Annotated[str | None, Form()] = None,
    portfolio_id: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Add a holding from a ticker + GBP value, computing shares from live price.

    Fetches the current price, derives ``shares = value / gbp_price``, and
    records a BUY at the native price so cost/currency stay consistent with the
    rest of the portfolio. A missing/unknown portfolio, missing ticker,
    non-positive value, or price lookup failure returns the partial with an
    error and records nothing (#147).
    """
    pid = optional_int(portfolio_id)
    if pid is None or not trader.portfolio_exists(pid):
        return _quick_add_error(
            request,
            portfolio,
            "Select a portfolio first (create one if you have none).",
            pid,
        )

    ticker = ticker.strip().upper()
    if not ticker or value <= 0:
        return _quick_add_error(
            request, portfolio, "Enter a ticker and a positive value.", pid
        )

    gbpusd = portfolio.gbpusd_rate()
    gbp_prices, display_info = portfolio.fetch_all_prices(
        [ticker], portfolio.load_ticker_aliases(), gbpusd
    )
    gbp_price = gbp_prices.get(ticker)
    if not gbp_price:
        return _quick_add_error(
            request,
            portfolio,
            f"Couldn't fetch a current price for {ticker}.",
            pid,
        )

    original_price, _currency = display_info[ticker]
    shares = round(value / gbp_price, 4)
    if shares <= 0:
        return _quick_add_error(
            request,
            portfolio,
            f"Value is too small to buy any shares of {ticker}.",
            pid,
        )

    try:
        trader.record_buy(
            ticker,
            shares,
            original_price,
            date or None,
            notes=f"Quick-add: £{value:,.2f} @ £{gbp_price:g}/share",
            portfolio_id=pid,
            source="quick_add",
        )
    except Exception as e:
        logger.exception("Quick-add failed for %s: %s", ticker, e)
        return _quick_add_error(request, portfolio, f"Could not add {ticker}: {e}", pid)

    # Persist the fetched price so the new holding renders with live P&L.
    trader.save_price_cache(gbp_prices, display_info)
    context = portfolio.default_portfolio_context(pid)
    context["import_message"] = (
        f"Added {shares:g} share(s) of {ticker} (~£{value:,.2f})."
    )
    logger.info("Quick-add: %s x%.4f (~£%.2f)", ticker, shares, value)
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


@router.get("/portfolio/{portfolio_id}/reconciliation", response_class=HTMLResponse)
async def reconciliation_view(
    request: Request, portfolio_id: int, trader: TraderDep
) -> HTMLResponse:
    """The dedicated cash-reconciliation view (Story 1.5, AC5) — every
    detected statement-balance discrepancy for this portfolio, not just a
    passing warning banner. A thin ``TraderService`` passthrough to
    ``CashReconciliationRepository.list_issues``, matching every other
    read path (e.g. ``get_cash_flows``)."""
    raw_issues = trader.list_reconciliation_issues(portfolio_id)
    issues = [
        {
            "date": row[2],
            "prior_balance": row[3],
            "expected_balance": row[4],
            "actual_balance": row[5],
            "difference": row[6],
            "row_ref": row[7],
            "currency": row[8],
        }
        for row in raw_issues
    ]
    return templates.TemplateResponse(
        request,
        "_reconciliation.html",
        context={"issues": issues, "portfolio_id": portfolio_id},
    )


# --- Opening Lots (Story 2.4) -----------------------------------------------


def _opening_lot_error(
    request: Request,
    portfolio: PortfolioService,
    message: str,
    portfolio_id: int | None,
) -> HTMLResponse:
    """Render the portfolio partial with an Opening Lot error banner --
    mirrors ``_quick_add_error``'s shape for this feature's own validation
    failures (duplicate entry, consumed lot, unknown portfolio)."""
    context = portfolio.default_portfolio_context(portfolio_id)
    context["error_message"] = message
    return templates.TemplateResponse(
        request, "_portfolio.html", context=context, status_code=400
    )


def _same_day_ordering_warning(
    realised_pnl: RealisedPnlService, lot: Trade, portfolio_id: int
) -> str | None:
    """Detect a known replay-ordering gap (documented in
    ``deferred-work.md``): a same-date Opening Lot can't retroactively
    resolve a same-date unmatched sell, because the same-day tie-break
    (``_replay_sort_key``/``_REPLAY_ORDER``, shared with Stories 2.2/2.3)
    falls back to ascending trade id -- and the sell an Opening Lot is
    meant to fix is always written first, so it always has the lower id
    and replays before the lot on a tie.

    Returns a warning message only when ``lot`` is still entirely
    untouched *and* an unmatched sell remains for the exact same
    ticker/date -- the specific signature of this gap, not a lot that
    legitimately only partially covers a larger shortfall (which is
    expected behavior, not a bug). ``None`` when there's nothing to warn
    about, including when ``lot.id`` is unset (unreachable for a
    persisted row).
    """
    if lot.id is None:
        return None
    if realised_pnl.opening_lot_status(lot.id, portfolio_id) != "unconsumed":
        return None
    summary = realised_pnl.compute_summary(portfolio_id)
    same_day_unmatched = any(
        us.ticker == lot.ticker and us.date == lot.date
        for us in summary.unmatched_sells
    )
    if not same_day_unmatched:
        return None
    return (
        f"Added, but {lot.ticker} still has an unmatched sell dated "
        f"{lot.date}: a same-day Opening Lot can't resolve a same-day "
        "sell due to replay ordering. Try dating this Opening Lot one "
        "day earlier instead."
    )


@router.post(
    "/portfolio/opening-lot",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def create_opening_lot(
    request: Request,
    trader: TraderDep,
    portfolio: PortfolioDep,
    realised_pnl: RealisedPnlDep,
    ticker: Annotated[str, Form()],
    shares: Annotated[float, Form()],
    price: Annotated[float, Form()],
    date: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
    portfolio_id: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Record a manual Opening Lot (Story 2.4, AC3/AC4/AC5).

    A BUY trade tagged ``source="opening_lot"`` -- participates in FIFO
    and average-cost replay exactly like an imported Buy, including in the
    Match Trace (AC3). Rejects a duplicate entry (same canonicalized
    ticker/shares/date already recorded as an Opening Lot for this
    portfolio, AC4) with an error banner and no write. On success, the
    very next ``compute_summary()`` call (e.g. opening the Realised P&L
    tab) already reflects the fresh lot -- no separate re-match step
    (AC5), since that service recomputes from live trade data on every
    call, except for the known same-day tie-break gap
    ``_same_day_ordering_warning`` checks for.
    """
    pid = optional_int(portfolio_id)
    if pid is None or not trader.portfolio_exists(pid):
        return _opening_lot_error(
            request,
            portfolio,
            "Select a portfolio first (create one if you have none).",
            pid,
        )
    ticker = ticker.strip().upper()
    if not ticker or shares <= 0 or price <= 0:
        return _opening_lot_error(
            request, portfolio, "Enter a ticker and a positive shares/price.", pid
        )
    try:
        lot = trader.record_opening_lot(ticker, shares, price, date, notes, pid)
    except OpeningLotDuplicateError as exc:
        return _opening_lot_error(request, portfolio, str(exc), pid)

    context = portfolio.default_portfolio_context(pid)
    warning = _same_day_ordering_warning(realised_pnl, lot, pid)
    if warning:
        context["import_status"] = "warning"
        context["import_message"] = warning
    else:
        context["import_message"] = (
            f"Added Opening Lot: {shares:g} share(s) of {ticker} @ {price:g}."
        )
    logger.info("Opening Lot recorded: %s x%.4f @ %.4f", ticker, shares, price)
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


@router.post(
    "/portfolio/opening-lot/{trade_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def edit_opening_lot(
    request: Request,
    trade_id: int,
    trader: TraderDep,
    portfolio: PortfolioDep,
    realised_pnl: RealisedPnlDep,
    ticker: Annotated[str, Form()],
    shares: Annotated[float, Form()],
    price: Annotated[float, Form()],
    date: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
    portfolio_id: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Edit an existing Opening Lot's fields (Story 2.4, AC7/AC8).

    Gated on a fresh consumed/unconsumed check
    (``RealisedPnlService.opening_lot_status`` -- never a persisted
    column): an Opening Lot with any part already matched against a sell
    is rejected with a clear error and no mutation, not a silent no-op.
    """
    pid = optional_int(portfolio_id)
    if pid is None or not trader.portfolio_exists(pid):
        return _opening_lot_error(request, portfolio, "Select a portfolio first.", pid)
    ticker = ticker.strip().upper()
    if not ticker or shares <= 0 or price <= 0:
        return _opening_lot_error(
            request, portfolio, "Enter a ticker and a positive shares/price.", pid
        )
    status = realised_pnl.opening_lot_status(trade_id, pid)
    if status is None:
        return _opening_lot_error(
            request, portfolio, "This Opening Lot no longer exists.", pid
        )
    if status != "unconsumed":
        return _opening_lot_error(
            request,
            portfolio,
            "This Opening Lot has already been matched against a sell and "
            "can no longer be edited.",
            pid,
        )
    try:
        lot = trader.update_opening_lot(
            trade_id, ticker, shares, price, date, notes, pid
        )
    except (OpeningLotDuplicateError, ValueError) as exc:
        return _opening_lot_error(request, portfolio, str(exc), pid)

    context = portfolio.default_portfolio_context(pid)
    warning = _same_day_ordering_warning(realised_pnl, lot, pid)
    if warning:
        context["import_status"] = "warning"
        context["import_message"] = warning
    else:
        context["import_message"] = "Opening Lot updated."
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


@router.delete(
    "/portfolio/opening-lot/{trade_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def delete_opening_lot(
    request: Request,
    trade_id: int,
    trader: TraderDep,
    portfolio: PortfolioDep,
    realised_pnl: RealisedPnlDep,
    portfolio_id: str | None = None,
) -> HTMLResponse:
    """Delete an Opening Lot (Story 2.4, AC7/AC8).

    Gated on the same fresh consumed/unconsumed check as
    ``edit_opening_lot`` -- a consumed lot is rejected with a clear error,
    never silently ignored.
    """
    pid = optional_int(portfolio_id)
    if pid is None or not trader.portfolio_exists(pid):
        return _opening_lot_error(request, portfolio, "Select a portfolio first.", pid)
    status = realised_pnl.opening_lot_status(trade_id, pid)
    if status is None:
        return _opening_lot_error(
            request, portfolio, "This Opening Lot no longer exists.", pid
        )
    if status != "unconsumed":
        return _opening_lot_error(
            request,
            portfolio,
            "This Opening Lot has already been matched against a sell and "
            "can no longer be deleted.",
            pid,
        )
    trader.delete_opening_lot(trade_id, pid)
    context = portfolio.default_portfolio_context(pid)
    context["import_message"] = "Opening Lot deleted."
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


@router.get(
    "/portfolio/{portfolio_id}/match-trace/{trade_id}", response_class=HTMLResponse
)
async def match_trace_view(
    request: Request,
    portfolio_id: int,
    trade_id: int,
    realised_pnl: RealisedPnlDep,
) -> HTMLResponse:
    """The dedicated Match Trace detail view (Story 2.4) -- the full FIFO
    match explanation for one SELL (identity, candidate lots, ordering
    decision, skipped-date rows, source/import batch), alongside the
    existing coarse ``UnmatchedSell`` summary list. Mirrors the
    reconciliation view's shape (Story 1.5): a thin service passthrough,
    read-only (no ``require_local_or_token``, matching that view).
    """
    trace = realised_pnl.get_match_trace(trade_id, portfolio_id)
    return templates.TemplateResponse(
        request,
        "_match_trace.html",
        context={"trace": trace, "portfolio_id": portfolio_id, "trade_id": trade_id},
    )
