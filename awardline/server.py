"""Run the pilot API locally. Never starts a server on import."""

from __future__ import annotations

import argparse
import os

from .api import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Awardline API on localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--research-demo", action="store_true", help="Serve buyer history without charging, for local research only")
    args = parser.parse_args()
    dsn = os.environ.get("AWARDLINE_DATABASE_URL")
    if not dsn:
        parser.error("AWARDLINE_DATABASE_URL is required")
    import uvicorn

    uvicorn.run(create_app(dsn, redis_url=os.environ.get("AWARDLINE_REDIS_URL"), research_demo=args.research_demo), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
