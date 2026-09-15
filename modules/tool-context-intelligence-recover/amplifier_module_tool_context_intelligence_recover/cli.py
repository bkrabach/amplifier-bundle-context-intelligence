"""Command-line entry point for durable native capture recovery."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .recovery import RecoveryRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="context-intelligence-recover",
        description="Recover completed native Context Intelligence captures to configured destinations.",
    )
    parser.add_argument(
        "--path", action="append", type=Path, default=[], help="Additional scan root."
    )
    parser.add_argument("--state-dir", type=Path, help="Owner-only checkpoint directory.")
    parser.add_argument("--job-id", help="Resume this durable recovery job.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true", help="Run once (default) and report unfinished work."
    )
    mode.add_argument(
        "--watch", action="store_true", help="Retry unfinished work, honoring Retry-After."
    )
    parser.add_argument(
        "--dry-run",
        "--plan",
        action="store_true",
        help="Inventory and route without network calls.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    job_id = args.job_id or "default"
    state_dir = args.state_dir or (
        Path.home() / ".local" / "state" / "context-intelligence" / "recovery" / job_id
    )
    runner = RecoveryRunner(state_dir=state_dir, job_id=job_id)
    try:
        while True:
            summary = runner.run(args.path, dry_run=args.dry_run)
            print(json.dumps(summary.to_dict(), sort_keys=True))
            if not args.watch or not summary.pending or args.dry_run:
                raise SystemExit(1 if summary.pending else 0)
            time.sleep(max(0.1, summary.retry_after_s or 1.0))
    finally:
        runner.close()


if __name__ == "__main__":
    main()
