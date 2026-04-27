"""
Orchestrator — wires the three agents together and schedules them.
Runs the MS Agent framework pipeline on market hours.
"""

import argparse
import json
from datetime import datetime

import openpyxl
from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.analyst.analyst_agent import AnalystAgent, recommendation
from agents.alert.alert_agent import AlertAgent
from agents.extraction.extraction_agent import ExtractionAgent
from ms_agent_framework import AgentApp
from agents.scanner.scanner_agent import ScannerAgent, load_watchlist, load_source_map
from models import StockRecord


SCAN_OUTPUT = "agents/scanner/scan_results.json"
ANALYSIS_OUTPUT = "agents/analyst/analysis_results.json"
EXCEL_OUTPUT = "agents/analyst/analysis_results.xlsx"
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MIN = 0

_HEADERS = [
    "Ticker", "StockTwits", "Whale Wisdom", "Score", "CANSLIM", "Momentum", "Stage", "Near Entry",
    "Entry", "Stop", "Risk %", "Price", "P/E", "RSI", "Rel Vol",
    "% 52w High", "% Chg Week", "Rel Str vs SPY",
    "EPS Growth", "ROE", "Inst Ownership %", "Inst Count", "WW Buyers", "WW Sellers", "WW Net", "SPY Uptrend",
    "SMA Stack", "SMA50", "Near High", "RSI Zone", "Vol Zone",
    "Recommendation", "As Of", "Summary",
]

# Signal columns are AA-AE (1-indexed: 27-31)
_SIGNAL_COL_START = 27
_SIGNAL_COL_END = 31

_SCORE_FILLS = {
    "high":   PatternFill("solid", fgColor="C6EFCE"),  # green  ≥8
    "mid":    PatternFill("solid", fgColor="FFEB9C"),  # yellow 6–7
    "low":    PatternFill("solid", fgColor="FFC7CE"),  # red    ≤5
}
_SIGNAL_FILLS = {
    "+": PatternFill("solid", fgColor="C6EFCE"),  # green  — positive for buying
    "-": PatternFill("solid", fgColor="FFC7CE"),  # red    — negative / caution
    "~": PatternFill("solid", fgColor="FFEB9C"),  # yellow — neutral
}
_HEADER_FILL = PatternFill("solid", fgColor="2F5496")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_NEAR_ENTRY_FILL = PatternFill("solid", fgColor="E2EFDA")


def _yahoo_url(ticker: str) -> str:
    return f"https://finance.yahoo.com/quote/{ticker.replace('.', '-')}"


def _score_fill(score: int) -> PatternFill:
    if score >= 8:
        return _SCORE_FILLS["high"]
    if score >= 6:
        return _SCORE_FILLS["mid"]
    return _SCORE_FILLS["low"]


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else ""


def _signals(r: StockRecord) -> list[str]:
    """Return 5 buy-signal indicators (+/-/~) from scan data.

    These correspond to the conditions previously used in the heuristic score
    and are shown as coloured columns in the Excel output.
    """
    sma150 = r.sma150
    sma200 = r.sma200
    sma50 = r.sma50
    rsi = r.rsi14
    pct = r.pct_from_52w_high
    vol = r.rel_volume

    # SMA Stack: price > SMA150 > SMA200 → bullish macro structure
    if sma150 and sma200 and r.price > sma150 > sma200:
        sma_stack = "+"
    elif sma150 and sma200 and r.price < sma150 and r.price < sma200:
        sma_stack = "-"
    else:
        sma_stack = "~"

    # SMA50: short-term trend
    sma50_sig = "+" if sma50 and r.price > sma50 else "-"

    # Near High: -15% to -1% is the ideal consolidation zone; < -30% is too extended
    if pct is not None and -15 <= pct <= -1:
        near_high = "+"
    elif pct is not None and pct < -30:
        near_high = "-"
    else:
        near_high = "~"

    # RSI Zone: 50-80 healthy, >80 overbought, <40 weak, 40-50 neutral
    if rsi is not None and 50 <= rsi <= 80:
        rsi_zone = "+"
    elif rsi is not None and (rsi > 80 or rsi < 40):
        rsi_zone = "-"
    else:
        rsi_zone = "~"

    # Vol Zone: elevated volume signals institutional participation
    if vol >= 1.5:
        vol_zone = "+"
    elif vol < 0.7:
        vol_zone = "-"
    else:
        vol_zone = "~"

    return [sma_stack, sma50_sig, near_high, rsi_zone, vol_zone]


def _record_to_row(r: StockRecord) -> list[object]:
    a = r.analysis
    if not a:
        return [r.ticker] + [""] * (len(_HEADERS) - 1)
    risk = (
        f"{(a.entry_price - a.stop_loss) / a.entry_price * 100:.1f}%"
        if a.entry_price and a.stop_loss
        else ""
    )
    return [
        r.ticker,
        "Y" if r.in_stocktwits else "",
        "Y" if r.in_whale_wisdom else "",
        f"{a.score}/10",
        f"{a.canslim.total}/14" if a.canslim else "",
        f"{a.momentum.total}/14" if a.momentum else "",
        a.stage,
        "Y" if a.near_entry else "",
        a.entry_price,
        a.stop_loss,
        risk,
        r.price,
        round(r.pe_ratio, 1) if r.pe_ratio else "",
        r.rsi14,
        r.rel_volume,
        r.pct_from_52w_high,
        r.pct_change_week,
        r.rel_strength_vs_spy,
        _pct(r.eps_growth),
        _pct(r.roe),
        _pct(r.inst_ownership_pct),
        r.inst_count if r.inst_count is not None else "",
        r.funds_buying if r.funds_buying is not None else "",
        r.funds_selling if r.funds_selling is not None else "",
        r.funds_net if r.funds_net is not None else "",
        "Y" if r.spy_uptrend else "N",
        *_signals(r),
        recommendation(a),
        r.as_of,
        a.summary,
    ]


def _apply_header(ws: openpyxl.worksheet.worksheet.Worksheet) -> None:
    ws.append(_HEADERS)
    for col, _ in enumerate(_HEADERS, 1):
        cell = ws.cell(1, col)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"


def _set_col_widths(ws: openpyxl.worksheet.worksheet.Worksheet) -> None:
    widths = {
        "A": 8,   # Ticker
        "B": 11,  # StockTwits
        "C": 12,  # Whale Wisdom
        "D": 8,   # Score
        "E": 10,  # CANSLIM
        "F": 10,  # Momentum
        "G": 10,  # Stage
        "H": 8,   # Near Entry
        "I": 10,  # Entry
        "J": 10,  # Stop
        "K": 8,   # Risk %
        "L": 10,  # Price
        "M": 7,   # P/E
        "N": 7,   # RSI
        "O": 8,   # Rel Vol
        "P": 11,  # % 52w High
        "Q": 11,  # % Chg Week
        "R": 14,  # Rel Str vs SPY
        "S": 11,  # EPS Growth
        "T": 8,   # ROE
        "U": 15,  # Inst Ownership
        "V": 10,  # Inst Count
        "W": 9,   # WW Buyers
        "X": 9,   # WW Sellers
        "Y": 8,   # WW Net
        "Z": 10,  # SPY Uptrend
        "AA": 9,  # SMA Stack
        "AB": 7,  # SMA50
        "AC": 9,  # Near High
        "AD": 8,  # RSI Zone
        "AE": 8,  # Vol Zone
        "AF": 12, # Recommendation
        "AG": 12, # As Of
        "AH": 50, # Summary
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width


def write_excel(records: list[StockRecord], path: str) -> None:
    """Write analysis results to a formatted Excel workbook."""
    wb = openpyxl.Workbook()

    # --- Sheet 1: All results ---
    ws_all = wb.active
    ws_all.title = "All Results"
    _apply_header(ws_all)
    _set_col_widths(ws_all)

    for r in records:
        row = _record_to_row(r)
        ws_all.append(row)
        data_row = ws_all.max_row
        score = r.analysis.score if r.analysis else 0
        fill = _score_fill(score)
        link_cell = ws_all.cell(data_row, 1)
        assert isinstance(link_cell, Cell)
        link_cell.hyperlink = Hyperlink(ref="", target=_yahoo_url(r.ticker))
        link_cell.font = Font(color="0563C1", underline="single")
        link_cell.fill = fill
        ws_all.cell(data_row, 4).fill = fill   # Score cell
        if r.analysis and r.analysis.near_entry:
            ws_all.cell(data_row, 8).fill = _NEAR_ENTRY_FILL
        for col in range(_SIGNAL_COL_START, _SIGNAL_COL_END + 1):
            cell = ws_all.cell(data_row, col)
            if cell.value in _SIGNAL_FILLS:
                cell.fill = _SIGNAL_FILLS[cell.value]
                cell.alignment = Alignment(horizontal="center", vertical="center")
        for col in range(1, len(_HEADERS) + 1):
            ws_all.cell(data_row, col).alignment = Alignment(vertical="center")

    # --- Sheet 2: Top 20 ---
    top20 = [r for r in records if r.analysis][:20]
    ws_top = wb.create_sheet("Top 20")
    _apply_header(ws_top)
    _set_col_widths(ws_top)

    for rank, r in enumerate(top20, 1):
        row = _record_to_row(r)
        row[0] = f"{rank}. {r.ticker}"
        ws_top.append(row)
        data_row = ws_top.max_row
        score = r.analysis.score if r.analysis else 0
        fill = _score_fill(score)
        top_link = ws_top.cell(data_row, 1)
        assert isinstance(top_link, Cell)
        top_link.hyperlink = Hyperlink(ref="", target=_yahoo_url(r.ticker))
        top_link.font = Font(color="0563C1", underline="single")
        top_link.fill = fill
        ws_top.cell(data_row, 4).fill = fill
        if r.analysis and r.analysis.near_entry:
            ws_top.cell(data_row, 8).fill = _NEAR_ENTRY_FILL
        for col in range(_SIGNAL_COL_START, _SIGNAL_COL_END + 1):
            cell = ws_top.cell(data_row, col)
            if cell.value in _SIGNAL_FILLS:
                cell.fill = _SIGNAL_FILLS[cell.value]
                cell.alignment = Alignment(horizontal="center", vertical="center")
        for col in range(1, len(_HEADERS) + 1):
            ws_top.cell(data_row, col).alignment = Alignment(vertical="center")

    wb.save(path)
    print(f"      Excel saved -> {path}")


def is_market_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    open_mins = MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN
    close_mins = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    current_mins = now.hour * 60 + now.minute
    return open_mins <= current_mins <= close_mins


def pipeline(force: bool = False, extract: bool = False) -> None:
    if not force and not is_market_hours():
        print(f"[{datetime.now().strftime('%H:%M')}] Outside market hours — skipping")
        return

    print(f"\n{'='*50}")
    print(f"Pipeline run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*50)

    if extract:
        ExtractionAgent(name="ExtractionAgent").run()  # refreshes extraction_results.json with latest WisdomWise data
    watchlist = load_watchlist()  # always scan the full combined watchlist
    source_map = load_source_map()

    scanner = ScannerAgent(name="ScannerAgent")
    analyst = AnalystAgent()
    alerter = AlertAgent(name="AlertAgent")
    app = AgentApp(name="MomentumStockAgent")
    app.add_agent(scanner)
    app.add_agent(analyst)
    app.add_agent(alerter)

    _, intermediates = app.execute_with_intermediates(watchlist)
    scan_results, analysis_results = intermediates

    for r in analysis_results:
        st, ww = source_map.get(r.ticker, (False, False))
        r.in_stocktwits = st
        r.in_whale_wisdom = ww

    with open(SCAN_OUTPUT, "w", encoding="utf-8") as stream:
        json.dump([item.model_dump() for item in scan_results], stream, indent=2)
    print(f"      Scanned {len(scan_results)} tickers")

    with open(ANALYSIS_OUTPUT, "w", encoding="utf-8") as stream:
        json.dump([item.model_dump() for item in analysis_results], stream, indent=2)

    write_excel(analysis_results, EXCEL_OUTPUT)

    print("\nPipeline complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Momentum stock scanner")
    parser.add_argument("--once", action="store_true", help="Run pipeline once and exit")
    parser.add_argument("--extract", action="store_true", help="Pull watchlist from WisdomWise instead of default")
    parser.add_argument(
        "--interval",
        type=int,
        default=15,
        help="Polling interval in minutes (default: 15)",
    )
    args = parser.parse_args()

    if args.once:
        pipeline(force=True, extract=args.extract)
        return

    print(f"Scheduler started — running every {args.interval} minutes during market hours")
    print("Press Ctrl+C to stop\n")

    scheduler = BlockingScheduler()
    scheduler.add_job(
        pipeline,
        CronTrigger(minute=f"*/{args.interval}"),
        id="momentum_scan",
        max_instances=1,
        coalesce=True,
    )

    pipeline(force=False)

    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\nScheduler stopped.")


if __name__ == "__main__":
    main()
