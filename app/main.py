"""Application entry point.

Usage:
    python -m app.main serve [--host H] [--port P] [--reload]
    python -m app.main run-pipeline [--extract]
"""

import argparse


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
