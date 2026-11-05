"""Size gates for the scanner partial (#134, AD-7/AD-8).

Renders a 722-row fixture that mirrors the live mix and checks the average
``<tr>`` weight and the gzipped partial size.
"""

from __future__ import annotations

import gzip
import re

from app.api.templating import templates
from app.core.alerting import build_alert_ui_state
from app.core.recommendation import classify_recommendation
from app.schemas.record import StockRecord
from app.schemas.scan import CANSLIMScore, MomentumScore, StockAnalysis

ROW_COUNT = 722
MAX_AVG_ROW_BYTES = 1800  # relaxed from 1024, then 1640, by the user
MAX_GZIP_BYTES = 300_000
SECTORS = ("Technology", "Healthcare", "Financial Services", "Industrials")
SEPA_KEYS = (
    "above_150_200",
    "sma150_above_200",
    "sma200_rising",
    "sma50_above_150_200",
    "above_25pct_of_low",
    "within_25pct_of_high",
    "rs_leader",
    "above_sma50",
)


def _record(i: int) -> StockRecord:
    """Build one analysed record; ``i`` drives a deterministic live-like mix."""
    is_uk = i % 9 < 2  # ~22% .L
    price = 20.0 + (i * 7.31) % 480
    entry = round(price * 1.031, 2)
    stop = round(entry * 0.92, 2)
    alerting = i % 2 == 0  # Stage 2 approaching, score >= 5: alert sub-line
    analysis = StockAnalysis(
        score=5 + i % 6 if alerting else 1 + i % 10,
        stage="Stage 2" if alerting else f"Stage {1 + i % 4}",
        entry_zone="approaching" if alerting else "far",
        entry_price=entry,
        stop_loss=stop,
        r_multiples={
            "1.0R": entry + (entry - stop),
            "2.0R": entry + 2 * (entry - stop),
        },
        reward_risk_ratio=2.0,
        fresh_breakout=i % 40 == 0,  # a few VCP
        multiyear_breakout=i % 9 in (4, 5),  # ~22% MYB
        multiyear_pivot=round(price * 0.95, 2),
        sepa_template={key: (i >> n) % 3 != 0 for n, key in enumerate(SEPA_KEYS)},
        canslim=CANSLIMScore(C=2, A=1, N=2, S=1, L=2, I=1, M=1),
        momentum=MomentumScore(C=1, A=2, N=1, S=1, L=2, I=1, M=1),
        summary="fixture",
    )
    has_funds = i % 10 == 0  # ~10% funds data
    return StockRecord(
        ticker=f"T{i:03d}.L" if is_uk else f"TK{i:03d}",
        as_of="2026-11-04",
        price=price,
        currency="GBP" if is_uk else "USD",
        rsi14=round(15 + (i * 3.7) % 70, 2),
        volume=1000,
        rel_volume=1.1,
        high_52w=price * 1.3,
        low_52w=price * 0.6,
        pct_from_52w_high=-12.0,
        pct_change_week=1.5,
        sector=SECTORS[i % len(SECTORS)],
        congress_buys=i % 7,
        congress_sells=i % 5,
        senate_buys=i % 3,
        senate_sells=i % 2,
        funds_buying=12 if has_funds else None,
        funds_selling=4 if has_funds else None,
        funds_net=8 if has_funds else None,
        analysis=analysis,
    )


def _render(records: list[StockRecord]) -> str:
    """Render the real scanner partial with the real context helpers."""
    return templates.get_template("_stock_scanner.html").render(
        records=records,
        portfolio_tickers=set(),
        recommendations={r.ticker: classify_recommendation(r) for r in records},
        alert_states={
            r.ticker: build_alert_ui_state(
                r,
                has_watching=n % 4 == 0,  # some "already alerted" lines
                last_alerted_at=None,
            )
            for n, r in enumerate(records)
        },
        market_regime=None,
        market_narrative=None,
    )


def test_fixture_rows_fit_raw_and_gzip_budgets() -> None:
    html = _render([_record(i) for i in range(ROW_COUNT)])
    rows = re.findall(r"<tr data-sepa.*?</tr>", html, flags=re.S)
    assert len(rows) == ROW_COUNT
    average = sum(len(row.encode()) for row in rows) / ROW_COUNT
    assert average <= MAX_AVG_ROW_BYTES, f"average row {average:.0f} B"
    assert len(gzip.compress(html.encode(), 6)) < MAX_GZIP_BYTES
