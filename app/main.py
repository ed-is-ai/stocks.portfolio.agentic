"""Application entry point.

Usage:
    python -m app.main serve [--host H] [--port P] [--reload]
    python -m app.main run-pipeline [--extract]
"""

import argparse
import logging
import os


def _configure_logging() -> None:
    """Route application ``INFO`` logs to the console (#508).

    Nothing configured the root logger, so it sat at the default ``WARNING``
    and every ``logger.info`` in the app -- including the snapshot backfill's
    end-of-run report -- was silently dropped. That made a background job
    that was working indistinguishable from one that never ran. Uvicorn
    configures its own handlers, so this only adds the application's.

    ``LOG_LEVEL`` overrides the default for a quieter or noisier run.
    """
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # yfinance logs an ERROR line per delisted/missing symbol; the backfill
    # already reports those as unavailable, so keep them out of the console.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def main() -> None:
    """Parse the sub-command and dispatch to the web app or the pipeline."""
    parser = argparse.ArgumentParser(description="Agents.stocks application")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Run the FastAPI web app via uvicorn")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)
    serve_p.add_argument("--reload", action="store_true")

    run_p = sub.add_parser("run-pipeline", help="Run the momentum pipeline once")
    run_p.add_argument(
        "--extract", action="store_true", help="Refresh the watchlist first"
    )

    args = parser.parse_args()
    _configure_logging()

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "app.api.app:app", host=args.host, port=args.port, reload=args.reload
        )
    elif args.command == "run-pipeline":
        from app.orchestration.orchestrator import pipeline

        pipeline(force=True, extract=args.extract)


if __name__ == "__main__":
    main()
