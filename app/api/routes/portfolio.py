"""Portfolio routes — live price refresh and SIPP CSV import."""

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from app.agents.trader.trader_agent import SippImportError
from app.api.dependencies import (
    get_notifications_repository,
    get_portfolio_service,
    get_trader_service,
)
from app.api.params import optional_int
from app.api.templating import templates
from app.core.config import IMPORTED_FILES_DIR
from app.core.security import require_local_or_token
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

router = APIRouter()
logger = logging.getLogger(__name__)

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]
NotificationsDep = Annotated[
    NotificationsRepository, Depends(get_notifications_repository)
]

#: Cap on how many individual row/value issues to spell out in the import
#: message and notification body before falling back to "(+N more)".
_MAX_ISSUE_DETAILS = 5


#: Anything other than alphanumerics/dot/dash/underscore in an uploaded
#: filename is collapsed to "_" before it's used as part of an archive path,
#: so a crafted filename (e.g. containing "../") can't escape
#: IMPORTED_FILES_DIR or clash with shell/filesystem-special characters.
_UNSAFE_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _archive_upload(filename: str, content: bytes) -> None:
    """Save a timestamped copy of an uploaded import file for later reference.

    Called for every upload attempt, not just successful imports — a file
    rejected for a bad portfolio, wrong extension, or a failed parse still
    needs to be inspectable to diagnose why. Each upload is parsed directly
    from its own request-owned bytes (#210), so this archive copy is the
    only on-disk trace of an import attempt; every past attempt stays
    around. Best effort — a failure here must not fail an otherwise-
    successful import.
    """
    try:
        IMPORTED_FILES_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = _UNSAFE_FILENAME_CHARS_RE.sub("_", filename) or "upload.csv"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        # A random suffix, not just the microsecond timestamp, guards against
        # two same-named files in one multi-file queue import landing in the
        # same microsecond and clobbering each other's archive copy.
        unique = uuid.uuid4().hex[:8]
        (IMPORTED_FILES_DIR / f"{stamp}_{unique}_{safe_name}").write_bytes(content)
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
    portfolio_id: str | None = None,
) -> HTMLResponse:
    """Fetch live prices from yfinance and return the updated portfolio partial."""
    pid = optional_int(portfolio_id)
    try:
        positions = trader.get_portfolio(portfolio_id=pid)
        logger.info("Refreshing %d positions", len(positions))
        if not positions:
            context = portfolio.portfolio_partial_context(
                [], error_message="No positions to refresh", portfolio_id=pid
            )
            return templates.TemplateResponse(
                request, "_portfolio.html", context=context, status_code=400
            )

        gbpusd = portfolio.gbpusd_rate()
        tickers = [p.ticker for p in positions]
        gbp_prices, display_info = portfolio.fetch_all_prices(
            tickers, portfolio.load_ticker_aliases(), gbpusd
        )

        trader.save_price_cache(gbp_prices, display_info)
        _, prices_as_of, _ = trader.load_price_cache()
        updated_positions = trader.refresh_portfolio_prices(
            gbp_prices, display_info, pid
        )
        logger.info("Refreshed %d positions successfully", len(updated_positions))
        cash_balance = trader.get_cash_balance(pid)
        trader.update_portfolio_snapshot(cash_balance, pid)
        context = portfolio.portfolio_partial_context(
            updated_positions,
            prices_as_of=prices_as_of,
            gbpusd_rate=gbpusd,
            cash_balance=cash_balance,
            portfolio_id=pid,
        )
        return templates.TemplateResponse(request, "_portfolio.html", context=context)
    except Exception as e:
        logger.exception("Failed to refresh portfolio prices: %s", e)
        cached_prices, prices_as_of, display_info = trader.load_price_cache()
        cached_positions = trader.get_portfolio(
            cached_prices or None, display_info or None, pid
        )
        gbpusd = cached_prices.get("__GBPUSD__")
        context = portfolio.portfolio_partial_context(
            cached_positions,
            prices_as_of=prices_as_of,
            gbpusd_rate=gbpusd,
            error_message=f"Failed to fetch prices: {e}",
            portfolio_id=pid,
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
    file: Annotated[UploadFile, File()],
    portfolio_id: Annotated[str | None, Form()] = None,
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
    Every upload — success or failure — is archived as a timestamped copy
    under ``data/imported/`` (a permanent per-attempt record; a failed file
    still needs to be inspectable to diagnose why). On success, also
    records one Event per file in the notification centre (#184) — the
    multi-file import queue (index.html's ``handleSippImportSubmit``) calls
    this endpoint once per queued file, so this naturally yields one
    archived file and one event per file.
    """
    filename = file.filename or ""
    content = await file.read()
    _archive_upload(filename, content)

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
        result = trader.import_sipp(content, pid)
    except SippImportError as e:
        # Validation failure (e.g. missing columns) — show the reason verbatim.
        context = portfolio.default_portfolio_context(pid)
        context["error_message"] = str(e)
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=400
        )
    except Exception as e:
        logger.exception("SIPP import failed: %s", e)
        context = portfolio.default_portfolio_context(pid)
        context["error_message"] = f"Import failed: {e}"
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=400
        )

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

    positions = trader.get_portfolio(portfolio_id=pid)
    context = portfolio.portfolio_partial_context(
        positions, cash_balance=result.cash_balance, portfolio_id=pid
    )
    message = (
        f"Imported {result.buy_count} buy(s), {result.sell_count} sell(s), "
        f"{result.cash_flow_count} cash transaction(s); {len(positions)} open "
        f"position(s); cash balance £{result.cash_balance:,.2f}."
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
