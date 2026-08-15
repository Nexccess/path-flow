from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

# Load local secrets/settings from repository-root .env before importing modules
# that read environment variables. Existing shell variables take precedence.
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

from campaign_runner import run as run_campaign
from daily_report import report as build_report
from ollama_client import OllamaClient
from response_watcher import run as run_response_watcher


def main() -> None:
    p = argparse.ArgumentParser(description="Path-Flow Sales Agent orchestrator")
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--live", action="store_true", help="Enable real outbound email sends. Default is dry-run.")
    p.add_argument("--max-sends", type=int)
    p.add_argument("--skip-inbox", action="store_true", help="Skip Office365 inbox processing for local tests.")
    p.add_argument("--inbox-only", action="store_true", help="Process inbox and print report without running outbound campaign actions.")
    p.add_argument("--skip-ollama-health", action="store_true")
    args = p.parse_args()

    print("=== Path-Flow Sales Agent ===")

    if not args.skip_ollama_health:
        health = OllamaClient().health()
        print("ollama=" + json.dumps({"ok": health.get("ok"), "model": health.get("model")}, ensure_ascii=False))

    if not args.skip_inbox:
        matched, unmatched, classified = run_response_watcher(args.db, lookback_hours=72, use_ollama=True)
        print(f"inbox matched={matched} unmatched={unmatched} classified={classified}")
    else:
        print("inbox skipped")

    if args.inbox_only:
        print("campaign skipped (inbox-only mode)")
    else:
        result = run_campaign(args.db, live=args.live, max_sends=args.max_sends)
        print("campaign=" + json.dumps(result, ensure_ascii=False))

    conn = sqlite3.connect(args.db)
    try:
        print("\n" + build_report(conn))
    finally:
        conn.close()

    print("=== Agent cycle complete ===")


if __name__ == "__main__":
    main()
