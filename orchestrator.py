"""
Orchestrator — wires the three agents together and schedules them.
Runs the MS Agent framework pipeline on market hours.
"""

import argparse
import json
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.analyst.analyst_agent import AnalystAgent
from agents.alert.alert_agent import AlertAgent
from agents.extraction.extraction_agent import ExtractionAgent
from ms_agent_framework import AgentApp
from agents.scanner.scanner_agent import ScannerAgent, load_watchlist


SCAN_OUTPUT = "agents/scanner/scan_results.json"
ANALYSIS_OUTPUT = "agents/analyst/analysis_results.json"
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MIN = 0


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

    watchlist: list[str]
    if extract:
        watchlist = ExtractionAgent().run()
        if not watchlist:
            print("Extraction returned no tickers — falling back to default watchlist")
            watchlist = load_watchlist()
    else:
        watchlist = load_watchlist()

    scanner = ScannerAgent(name="ScannerAgent")
    analyst = AnalystAgent()
    alerter = AlertAgent()
    app = AgentApp(name="MomentumStockAgent")
    app.add_agent(scanner)
    app.add_agent(analyst)
    app.add_agent(alerter)

    _, intermediates = app.execute_with_intermediates(watchlist)
    scan_results, analysis_results = intermediates

    with open(SCAN_OUTPUT, "w", encoding="utf-8") as stream:
        json.dump([item.model_dump() for item in scan_results], stream, indent=2)
    print(f"      Scanned {len(scan_results)} tickers")

    with open(ANALYSIS_OUTPUT, "w", encoding="utf-8") as stream:
        json.dump([item.model_dump() for item in analysis_results], stream, indent=2)

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
