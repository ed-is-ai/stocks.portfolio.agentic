"""Repository for ``results.db`` — the AnalystAgent's analysis history.

Single owner of the ``analysis_history`` table schema.
"""

from app.repositories.db import Connect, session

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_history (
    ticker         TEXT NOT NULL,
    as_of          TEXT NOT NULL,
    score          INTEGER NOT NULL,
    canslim_total  INTEGER,
    momentum_total INTEGER,
    stage          TEXT,
    price          REAL,
    entry_price    REAL,
    stop_loss      REAL,
    entry_zone     TEXT DEFAULT 'far',
    PRIMARY KEY (ticker, as_of)
)
"""

# Row of analysis fields persisted per (ticker, as_of).
ResultRow = tuple[
    str,
    str,
    int,
    int | None,
    int | None,
    str | None,
    float | None,
    float | None,
    float | None,
    str,
]


class ResultsRepository:
    """Typed access to the ``analysis_history`` table in ``results.db``."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def ensure_schema(self) -> None:
        """Create the analysis_history table and apply additive migrations."""
        with session(self._connect) as conn:
            conn.execute(_SCHEMA)
            try:
                conn.execute(
                    "ALTER TABLE analysis_history"
                    " ADD COLUMN entry_zone TEXT DEFAULT 'far'"
                )
            except Exception:
                pass
            conn.commit()

    def latest_scores(self) -> dict[str, int]:
        """Return {ticker: score} from each ticker's most recent ``as_of``."""
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT ticker, score FROM analysis_history"
                " WHERE as_of = ("
                "   SELECT MAX(as_of) FROM analysis_history h2"
                "   WHERE h2.ticker = analysis_history.ticker"
                " )"
            ).fetchall()
        return dict(rows)

    def save_results(self, rows: list[ResultRow]) -> None:
        """Upsert a batch of analysis rows."""
        with session(self._connect) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO analysis_history"
                " (ticker, as_of, score, canslim_total, momentum_total, stage,"
                "  price, entry_price, stop_loss, entry_zone)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
