"""Serve the interface.

    python -m services.api                    # http://localhost:8080
    python -m services.api --reload           # develop against it
    python -m services.api --production demo  # check a different production

Configuration, all optional, all environment variables (see .env.example):

    PORT                    port to bind, default 8080
    PRODUCTION_ID           override the call sheet's production_id
    GOLDENROD_CALL_SHEET    path to the call sheet, default the fixture
    GOLDENROD_PAGES         path to the revision's Fountain file, for citations
"""

from __future__ import annotations

import argparse
import os


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.api", description="Serve the Goldenrod interface."
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--reload", action="store_true", help="reload on edit")
    parser.add_argument(
        "--production",
        help="override the call sheet's production_id (sets PRODUCTION_ID)",
    )
    parser.add_argument("--call-sheet", help="path to the call sheet JSON")
    parser.add_argument("--pages", help="path to the revision's Fountain file")
    args = parser.parse_args(argv)

    # Passed through the environment because uvicorn imports the app in a fresh
    # process when --reload is on, so a flag parsed here would not survive.
    if args.production:
        os.environ["PRODUCTION_ID"] = args.production
    if args.call_sheet:
        os.environ["GOLDENROD_CALL_SHEET"] = args.call_sheet
    if args.pages:
        os.environ["GOLDENROD_PAGES"] = args.pages

    try:
        import uvicorn
    except ImportError:
        print(
            "the API needs its optional dependencies:\n    pip install -e \".[api]\""
        )
        return 2

    uvicorn.run(
        "services.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
