"""Database connection factory and schema for ``trades.db``.

This module is the single owner of the ``trades.db`` schema (tables and
migrations). Repositories receive a zero-argument ``Connect`` callable so they
always open a connection against the current path, even if a caller reassigns
its database path after construction (as the trader tests do).
"""

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: A zero-argument factory returning a fresh SQLite connection.
Connect = Callable[[], sqlite3.Connection]


@contextmanager
def session(connect: "Connect") -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, and always close it.

    Unlike ``with sqlite3.connect(...)`` (which commits but leaves the
    connection open), this releases the file handle — important on Windows
    where an open handle keeps the database file locked.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolios (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    action      TEXT NOT NULL CHECK(action IN ('BUY', 'SELL')),
    shares      REAL NOT NULL CHECK(shares > 0),
    price       REAL NOT NULL CHECK(price > 0),
    date        TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    stop_loss   REAL,
    entry_price REAL,
    reference   TEXT,
    currency    TEXT NOT NULL DEFAULT 'GBP'
);
CREATE TABLE IF NOT EXISTS portfolio_trade_revisions (
    portfolio_id INTEGER PRIMARY KEY,
    revision     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS realised_pnl_input_revision (
    id       INTEGER PRIMARY KEY CHECK(id = 1),
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cash_flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    flow_type   TEXT NOT NULL CHECK(flow_type IN ('CONTRIBUTION', 'DIVIDEND', 'INTEREST', 'TAX_RELIEF', 'TRANSFER', 'WITHDRAWAL', 'OPENING', 'OTHER')),
    ticker      TEXT,
    amount      REAL NOT NULL CHECK(amount > 0),
    description TEXT,
    reference   TEXT,
    currency    TEXT NOT NULL DEFAULT 'GBP'
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id   INTEGER NOT NULL,
    timestamp      TEXT NOT NULL,
    -- Nullable since #466: NULL means "holdings could not be valued at this
    -- timestamp" -- an honest gap in the chart rather than a fabricated 0.00.
    total_value    REAL,
    total_cost     REAL,
    cash_balance   REAL
);
CREATE TABLE IF NOT EXISTS price_cache (
    ticker          TEXT PRIMARY KEY,
    price           REAL NOT NULL,
    fetched_at      TEXT NOT NULL,
    currency        TEXT DEFAULT 'GBP',
    original_price  REAL
);
CREATE TABLE IF NOT EXISTS account_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fx_rate_cache (
    pair TEXT NOT NULL,
    date TEXT NOT NULL,
    rate REAL,
    PRIMARY KEY (pair, date)
);
CREATE TABLE IF NOT EXISTS cash_balances (
    portfolio_id INTEGER,
    currency     TEXT NOT NULL,
    amount       TEXT NOT NULL,
    as_of        TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, currency)
);
CREATE TABLE IF NOT EXISTS cash_reconciliation_issues (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id     INTEGER,
    date             TEXT NOT NULL,
    prior_balance    REAL NOT NULL,
    expected_balance REAL NOT NULL,
    actual_balance   REAL NOT NULL,
    difference       REAL NOT NULL,
    row_ref          TEXT,
    currency         TEXT NOT NULL DEFAULT 'GBP',
    detected_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fx_quotes (
    quote_digest TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    pair         TEXT NOT NULL,
    as_of        TEXT NOT NULL,
    rate         TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fx_unavailable_attempts (
    provider       TEXT NOT NULL,
    pair           TEXT NOT NULL,
    requested_date TEXT NOT NULL,
    reason         TEXT NOT NULL,
    attempted_at   TEXT NOT NULL,
    PRIMARY KEY (provider, pair, requested_date)
);
CREATE TABLE IF NOT EXISTS ticker_currency_cache (
    ticker      TEXT PRIMARY KEY,
    currency    TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_import_receipts (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    import_batch_id          TEXT NOT NULL,
    portfolio_id             INTEGER,
    provider_id              TEXT NOT NULL,
    account_type_id          TEXT,
    contract_id              TEXT NOT NULL,
    contract_version         TEXT NOT NULL,
    contract_content_digest  TEXT NOT NULL,
    source_digest            TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    status                   TEXT NOT NULL,
    inserted_count           INTEGER NOT NULL,
    duplicate_count          INTEGER NOT NULL,
    skipped_count            INTEGER NOT NULL,
    failed_count             INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_import_rows (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id           INTEGER NOT NULL REFERENCES portfolio_import_receipts(id),
    physical_row_number  INTEGER NOT NULL,
    canonical_row_digest TEXT NOT NULL,
    outcome              TEXT NOT NULL,
    reason               TEXT,
    trade_id             INTEGER REFERENCES trades(id) ON DELETE SET NULL,
    cash_flow_id         INTEGER REFERENCES cash_flows(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS portfolio_strategies (
    portfolio_id    INTEGER PRIMARY KEY REFERENCES portfolios(id) ON DELETE CASCADE,
    strategy_id     TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    assigned_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_recommendation_dispatches (
    portfolio_id     INTEGER NOT NULL,
    analysis_run_id  TEXT NOT NULL,
    strategy_id      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'claimed',
    claimed_at       TEXT NOT NULL,
    completed_at     TEXT,
    PRIMARY KEY (portfolio_id, analysis_run_id)
);
"""

#: Name of the default portfolio existing single-portfolio data migrates into.
DEFAULT_PORTFOLIO_NAME = "SIPP"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection to ``db_path``."""
    conn = sqlite3.connect(db_path)
    # SQLite foreign-key checks are disabled by default and scoped to each
    # connection. Backtest evidence uses fresh connections per repository
    # operation, so schema-time PRAGMA statements alone cannot enforce its
    # immutable-reference contract.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def make_connect(db_path_getter: Callable[[], str | Path]) -> Connect:
    """Return a ``Connect`` factory that reads the path lazily on each call."""
    return lambda: connect(db_path_getter())


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if ``table`` has a column named ``column``."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _rebuild_cash_flows(conn: sqlite3.Connection) -> None:
    """Rebuild ``cash_flows`` to add ``portfolio_id`` and drop the global
    ``UNIQUE(reference)`` constraint.

    Pre-multi-portfolio databases keyed idempotency on a globally unique
    ``reference``, which would stop the same CSV importing into two
    portfolios. The rebuild also widens the ``flow_type`` CHECK to allow the
    ``OPENING`` balance rows recorded when a portfolio is created. New
    databases already get the correct shape from ``_SCHEMA`` and skip this.
    """
    conn.execute(
        """
        CREATE TABLE cash_flows_new (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT NOT NULL,
            flow_type    TEXT NOT NULL CHECK(flow_type IN ('CONTRIBUTION', 'DIVIDEND', 'INTEREST', 'TAX_RELIEF', 'TRANSFER', 'WITHDRAWAL', 'OPENING', 'OTHER')),
            ticker       TEXT,
            amount       REAL NOT NULL CHECK(amount > 0),
            description  TEXT,
            reference    TEXT,
            portfolio_id INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO cash_flows_new "
        "(id, date, flow_type, ticker, amount, description, reference) "
        "SELECT id, date, flow_type, ticker, amount, description, reference "
        "FROM cash_flows"
    )
    conn.execute("DROP TABLE cash_flows")
    conn.execute("ALTER TABLE cash_flows_new RENAME TO cash_flows")


def _migrate_default_portfolio(conn: sqlite3.Connection) -> None:
    """Backfill pre-multi-portfolio data into a single default portfolio.

    Existing trades, cash flows, and the stored ``cash_balance`` are adopted
    by a portfolio named :data:`DEFAULT_PORTFOLIO_NAME` so the app keeps
    working with zero user action. A brand-new empty database is left with no
    portfolios (the UI shows a "create your first portfolio" prompt), so new
    users are never handed a portfolio they didn't ask for.
    """
    if conn.execute("SELECT 1 FROM portfolios LIMIT 1").fetchone():
        return  # already has at least one portfolio; nothing to backfill
    has_trades = conn.execute("SELECT 1 FROM trades LIMIT 1").fetchone()
    has_flows = conn.execute("SELECT 1 FROM cash_flows LIMIT 1").fetchone()
    has_cash = conn.execute(
        "SELECT 1 FROM account_state WHERE key = 'cash_balance'"
    ).fetchone()
    if not (has_trades or has_flows or has_cash):
        return  # fresh install — start with the empty state

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cur = conn.execute(
        "INSERT INTO portfolios (name, created_at) VALUES (?, ?)",
        (DEFAULT_PORTFOLIO_NAME, created_at),
    )
    pid = int(cur.lastrowid)  # type: ignore[arg-type]
    conn.execute(
        "UPDATE trades SET portfolio_id = ? WHERE portfolio_id IS NULL", (pid,)
    )
    conn.execute(
        "UPDATE cash_flows SET portfolio_id = ? WHERE portfolio_id IS NULL", (pid,)
    )
    conn.execute(
        "UPDATE account_state SET key = ? WHERE key = 'cash_balance'",
        (f"cash_balance:{pid}",),
    )


def _migrate_portfolio_snapshots_nullable(conn: sqlite3.Connection) -> None:
    """Relax ``portfolio_snapshots.total_value``/``total_cost`` to nullable.

    Databases created before #466 declare both columns ``NOT NULL``, which
    leaves an unvaluable snapshot no way to record "unknown" other than a
    misleading ``0.00``. SQLite cannot drop a NOT NULL constraint in place, so
    the table is rebuilt once; every existing row keeps its ``id`` and values.
    Idempotent: the ``PRAGMA table_info`` notnull flags are inspected first, so
    an already-migrated (or brand-new) database does no work at all. The
    rebuild itself runs inside one transaction (mirroring
    ``_migrate_fx_rate_cache``) so a crash mid-migration cannot strand a
    half-renamed table or drop the original data.
    """
    info = conn.execute("PRAGMA table_info(portfolio_snapshots)").fetchall()
    notnull = {row[1]: row[3] for row in info}
    if not notnull.get("total_value") and not notnull.get("total_cost"):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS portfolio_snapshots_new")
        conn.execute(
            """
            CREATE TABLE portfolio_snapshots_new (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_id   INTEGER NOT NULL,
                timestamp      TEXT NOT NULL,
                total_value    REAL,
                total_cost     REAL,
                cash_balance   REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO portfolio_snapshots_new "
            "(id, portfolio_id, timestamp, total_value, total_cost, cash_balance) "
            "SELECT id, portfolio_id, timestamp, total_value, total_cost, cash_balance "
            "FROM portfolio_snapshots"
        )
        conn.execute("DROP TABLE portfolio_snapshots")
        conn.execute(
            "ALTER TABLE portfolio_snapshots_new RENAME TO portfolio_snapshots"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_fx_rate_cache(conn: sqlite3.Connection) -> None:
    """Atomically upgrade the legacy date-only cache to pair-aware rows."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        info = conn.execute("PRAGMA table_info(fx_rate_cache)").fetchall()
        columns = {row[1] for row in info}
        primary_key = [(row[1], row[5]) for row in info if row[5]]
        current_columns = {"pair", "date", "rate"}
        if current_columns.issubset(columns) and primary_key == [
            ("pair", 1),
            ("date", 2),
        ]:
            conn.commit()
            return

        if current_columns.issubset(columns):
            select_sql = "SELECT pair, date, rate FROM fx_rate_cache"
        elif {"date", "gbpusd_rate"}.issubset(columns):
            select_sql = (
                "SELECT 'GBPUSD=X' AS pair, date, gbpusd_rate AS rate "
                "FROM fx_rate_cache"
            )
        else:
            raise sqlite3.OperationalError(
                "unsupported fx_rate_cache schema; expected legacy or pair-aware"
            )

        conn.execute("DROP TABLE IF EXISTS fx_rate_cache_new")
        conn.execute(
            """
            CREATE TABLE fx_rate_cache_new (
                pair TEXT NOT NULL,
                date TEXT NOT NULL,
                rate REAL,
                PRIMARY KEY (pair, date)
            )
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO fx_rate_cache_new (pair, date, rate) " + select_sql
        )
        conn.execute("DROP TABLE fx_rate_cache")
        conn.execute("ALTER TABLE fx_rate_cache_new RENAME TO fx_rate_cache")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_trades_db(conn: sqlite3.Connection) -> None:
    """Create the ``trades.db`` schema and apply additive migrations."""
    conn.executescript(_SCHEMA)
    _migrate_fx_rate_cache(conn)
    _migrate_portfolio_snapshots_nullable(conn)
    for col_def in (
        "stop_loss REAL",
        "entry_price REAL",
        "reference TEXT",
        "realised_pnl_ack_at TEXT",
        "source_row_index INTEGER",
    ):
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col_def}")
        except sqlite3.OperationalError as exc:
            logger.debug("schema migration step skipped: %s", exc)
    for table in ("trades", "cash_flows"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN idempotency_key TEXT")
        except sqlite3.OperationalError as exc:
            logger.debug("schema migration step skipped: %s", exc)
    for table in ("trades", "cash_flows"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN currency TEXT DEFAULT 'GBP'")
        except sqlite3.OperationalError as exc:
            logger.debug("schema migration step skipped: %s", exc)
    for col_def in ("currency TEXT DEFAULT 'GBP'", "original_price REAL"):
        try:
            conn.execute(f"ALTER TABLE price_cache ADD COLUMN {col_def}")
        except sqlite3.OperationalError as exc:
            logger.debug("schema migration step skipped: %s", exc)

    # Multi-portfolio migration (#147): add portfolio_id everywhere, rebuild
    # cash_flows to drop the legacy global-unique reference, then backfill.
    if not _has_column(conn, "trades", "portfolio_id"):
        conn.execute("ALTER TABLE trades ADD COLUMN portfolio_id INTEGER")
    if not _has_column(conn, "cash_flows", "portfolio_id"):
        _rebuild_cash_flows(conn)

    # The legacy cash-flow rebuild above creates a fresh table, so apply this
    # additive migration after the rebuild as well.
    for table in ("trades", "cash_flows"):
        if not _has_column(conn, table, "idempotency_key"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN idempotency_key TEXT")
        if not _has_column(conn, table, "currency"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN currency TEXT DEFAULT 'GBP'")

    # Trade provenance (Story 2.4): which write path produced a row and,
    # for a SIPP import, which import call. Additive/nullable -- pre-
    # existing rows keep NULL/NULL ("unknown/pre-migration"), never
    # backfilled and never treated as an error.
    if not _has_column(conn, "trades", "source"):
        conn.execute("ALTER TABLE trades ADD COLUMN source TEXT")
    if not _has_column(conn, "trades", "import_batch_id"):
        conn.execute("ALTER TABLE trades ADD COLUMN import_batch_id TEXT")

    # Idempotency keys are per-portfolio: the same reference may recur across
    # portfolios, so drop the old global-unique index in favour of composite.
    # Idempotency is keyed on (portfolio_id, reference). ``ifnull`` collapses a
    # NULL portfolio to a single bucket so legacy imports with no portfolio_id
    # still dedupe (SQLite treats bare NULLs as distinct in a unique index).
    conn.execute("DROP INDEX IF EXISTS idx_trades_reference")
    conn.execute("DROP INDEX IF EXISTS idx_trades_portfolio_reference")
    conn.execute("DROP INDEX IF EXISTS idx_cash_flows_portfolio_reference")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_portfolio_idempotency "
        "ON trades(ifnull(portfolio_id, -1), idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_flows_portfolio_idempotency "
        "ON cash_flows(ifnull(portfolio_id, -1), idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    conn.execute(
        # The idempotency indexes above are partial and keyed on
        # ifnull(portfolio_id, -1), so they can't serve a plain
        # `portfolio_id = ?` predicate. Without this, `history()` and
        # `open_rows()` (trades_repo.py) full-scan the entire trades table
        # -- unboundedly large across years of quarterly SIPP imports.
        "CREATE INDEX IF NOT EXISTS idx_trades_portfolio_id ON trades(portfolio_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cash_flows_portfolio_id "
        "ON cash_flows(portfolio_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_portfolio "
        "ON portfolio_snapshots(portfolio_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reconciliation_portfolio "
        "ON cash_reconciliation_issues(portfolio_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fx_quotes_pair_asof ON fx_quotes(pair, as_of)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fx_unavailable_attempts_pair_date "
        "ON fx_unavailable_attempts(pair, requested_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_rows_receipt "
        "ON portfolio_import_rows(receipt_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_receipts_portfolio "
        "ON portfolio_import_receipts(portfolio_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_receipts_batch "
        "ON portfolio_import_receipts(import_batch_id)"
    )

    for table in ("trades", "cash_flows"):
        try:
            conn.execute(
                f"UPDATE {table} SET date = "
                "substr(date, 7, 4) || '-' || substr(date, 4, 2) || '-' "
                "|| substr(date, 1, 2) "
                "WHERE date LIKE '__/__/____'"
            )
        except sqlite3.OperationalError as exc:
            logger.debug("date migration step skipped: %s", exc)

    _migrate_default_portfolio(conn)
    # A revision is the single invalidation source for derived trade views.
    # The triggers execute in the same transaction as every trade mutation;
    # failed writes consequently leave both ledger and revision unchanged.
    # ``-1`` is the safe bucket for legacy rows with no portfolio ID.
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS trg_trades_revision_insert
        AFTER INSERT ON trades
        BEGIN
            INSERT INTO portfolio_trade_revisions (portfolio_id, revision)
            VALUES (COALESCE(NEW.portfolio_id, -1), 1)
            ON CONFLICT(portfolio_id) DO UPDATE SET revision = revision + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_trades_revision_delete
        AFTER DELETE ON trades
        BEGIN
            INSERT INTO portfolio_trade_revisions (portfolio_id, revision)
            VALUES (COALESCE(OLD.portfolio_id, -1), 1)
            ON CONFLICT(portfolio_id) DO UPDATE SET revision = revision + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_trades_revision_update_same_portfolio
        AFTER UPDATE ON trades
        WHEN COALESCE(OLD.portfolio_id, -1) = COALESCE(NEW.portfolio_id, -1)
        BEGIN
            INSERT INTO portfolio_trade_revisions (portfolio_id, revision)
            VALUES (COALESCE(NEW.portfolio_id, -1), 1)
            ON CONFLICT(portfolio_id) DO UPDATE SET revision = revision + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_trades_revision_update_moved_portfolio
        AFTER UPDATE ON trades
        WHEN COALESCE(OLD.portfolio_id, -1) != COALESCE(NEW.portfolio_id, -1)
        BEGIN
            INSERT INTO portfolio_trade_revisions (portfolio_id, revision)
            VALUES (COALESCE(OLD.portfolio_id, -1), 1)
            ON CONFLICT(portfolio_id) DO UPDATE SET revision = revision + 1;
            INSERT INTO portfolio_trade_revisions (portfolio_id, revision)
            VALUES (COALESCE(NEW.portfolio_id, -1), 1)
            ON CONFLICT(portfolio_id) DO UPDATE SET revision = revision + 1;
        END;
        -- Realised P&L depends on these durable valuation inputs in addition
        -- to its portfolio's trade ledger.  Keep one revision for the shared
        -- input set: FX and currency classifications can serve many
        -- portfolios, while their cache entries remain portfolio-specific.
        CREATE TRIGGER IF NOT EXISTS trg_pnl_input_fx_rate_insert
        AFTER INSERT ON fx_rate_cache
        BEGIN
            INSERT INTO realised_pnl_input_revision (id, revision) VALUES (1, 1)
            ON CONFLICT(id) DO UPDATE SET revision = revision + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_pnl_input_fx_rate_update
        AFTER UPDATE OF rate ON fx_rate_cache
        WHEN OLD.rate IS NOT NEW.rate
        BEGIN
            INSERT INTO realised_pnl_input_revision (id, revision) VALUES (1, 1)
            ON CONFLICT(id) DO UPDATE SET revision = revision + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_pnl_input_fx_rate_delete
        AFTER DELETE ON fx_rate_cache
        BEGIN
            INSERT INTO realised_pnl_input_revision (id, revision) VALUES (1, 1)
            ON CONFLICT(id) DO UPDATE SET revision = revision + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_pnl_input_ticker_currency_insert
        AFTER INSERT ON ticker_currency_cache
        BEGIN
            INSERT INTO realised_pnl_input_revision (id, revision) VALUES (1, 1)
            ON CONFLICT(id) DO UPDATE SET revision = revision + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_pnl_input_ticker_currency_update
        AFTER UPDATE OF currency ON ticker_currency_cache
        WHEN OLD.currency IS NOT NEW.currency
        BEGIN
            INSERT INTO realised_pnl_input_revision (id, revision) VALUES (1, 1)
            ON CONFLICT(id) DO UPDATE SET revision = revision + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_pnl_input_ticker_currency_delete
        AFTER DELETE ON ticker_currency_cache
        BEGIN
            INSERT INTO realised_pnl_input_revision (id, revision) VALUES (1, 1)
            ON CONFLICT(id) DO UPDATE SET revision = revision + 1;
        END;
        -- ``ticker_currencies`` prefers price-cache display metadata. Price
        -- refreshes themselves do not affect P&L: only a usable metadata
        -- entry appearing/disappearing, or its *trading-currency*
        -- classification changing, does.  In particular, GBp and GBP both
        -- classify as GBP for realised P&L.
        CREATE TRIGGER IF NOT EXISTS trg_pnl_input_price_currency_insert
        AFTER INSERT ON price_cache
        WHEN NEW.original_price IS NOT NULL
        BEGIN
            INSERT INTO realised_pnl_input_revision (id, revision) VALUES (1, 1)
            ON CONFLICT(id) DO UPDATE SET revision = revision + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_pnl_input_price_currency_update
        AFTER UPDATE OF currency, original_price ON price_cache
        WHEN (OLD.original_price IS NULL) IS NOT (NEW.original_price IS NULL)
          OR (
              OLD.original_price IS NOT NULL
              AND NEW.original_price IS NOT NULL
              AND CASE
                    WHEN lower(trim(COALESCE(OLD.currency, 'GBP'))) = 'gbp'
                    THEN 'GBP'
                    ELSE upper(trim(COALESCE(OLD.currency, 'GBP')))
                  END
                  IS NOT CASE
                    WHEN lower(trim(COALESCE(NEW.currency, 'GBP'))) = 'gbp'
                    THEN 'GBP'
                    ELSE upper(trim(COALESCE(NEW.currency, 'GBP')))
                  END
          )
        BEGIN
            INSERT INTO realised_pnl_input_revision (id, revision) VALUES (1, 1)
            ON CONFLICT(id) DO UPDATE SET revision = revision + 1;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_pnl_input_price_currency_delete
        AFTER DELETE ON price_cache
        WHEN OLD.original_price IS NOT NULL
        BEGIN
            INSERT INTO realised_pnl_input_revision (id, revision) VALUES (1, 1)
            ON CONFLICT(id) DO UPDATE SET revision = revision + 1;
        END;
        """
    )
    conn.commit()
