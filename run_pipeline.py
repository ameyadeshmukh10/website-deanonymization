"""Orchestrator — run the full pipeline (or any subset of steps).

Usage:
    python run_pipeline.py                          # full pipeline
    python run_pipeline.py --dry-run                # don't touch HubSpot
    python run_pipeline.py --limit 10               # cap PDL + HubSpot work
    python run_pipeline.py --only 1b                # just pull the worker
    python run_pipeline.py --skip 1b,2              # resume from PDL onward
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import config

STEPS = ("1b", "2", "3", "4", "5", "6", "7")


def _configure_logging() -> Path:
    """Console (INFO) + timestamped file (DEBUG). Return the log path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = config.LOGS_DIR / f"run_{ts}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Remove any pre-existing handlers (e.g. from notebook re-runs).
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.FileHandler(log_path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    return log_path


def _selected_steps(args: argparse.Namespace) -> list[str]:
    if args.only:
        only = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = only - set(STEPS)
        if unknown:
            raise SystemExit(f"Unknown step(s) in --only: {sorted(unknown)}")
        return [s for s in STEPS if s in only]
    if args.skip:
        skip = {s.strip() for s in args.skip.split(",") if s.strip()}
        unknown = skip - set(STEPS)
        if unknown:
            raise SystemExit(f"Unknown step(s) in --skip: {sorted(unknown)}")
        return [s for s in STEPS if s not in skip]
    return list(STEPS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip HubSpot writes (Step 4 will report what it WOULD do).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of records processed in Steps 3 and 4.",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Comma-separated list of steps to run, e.g. '1b,2'. "
        f"Valid: {','.join(STEPS)}",
    )
    parser.add_argument(
        "--skip",
        type=str,
        default=None,
        help="Comma-separated list of steps to skip.",
    )
    parser.add_argument(
        "--all-ips",
        action="store_true",
        help=(
            "Bypass Step 2's intent filter — feed Step 3 every IP in "
            "data/ip_visits.json. Useful for one-time sweeps that try to "
            "surface high-confidence person inferences from PDL by widening "
            "the candidate pool. Costs ~1 PDL credit per uncached IP."
        ),
    )
    args = parser.parse_args()
    if args.only and args.skip:
        raise SystemExit("--only and --skip are mutually exclusive.")

    log_path = _configure_logging()
    logger = logging.getLogger("pipeline")
    logger.info("logging to %s", log_path)

    steps = _selected_steps(args)
    logger.info("running steps: %s", steps)

    # Lazy imports so a `--only 4` run doesn't require WORKER_* env vars.
    if "1b" in steps:
        import worker_client
        logger.info("=== Step 1B: pulling /export from the Worker ===")
        worker_client.run()

    if "2" in steps:
        if args.all_ips:
            # Passthrough: write every IP from ip_visits.json into the
            # high_intent file so Step 3 enriches everything. We preserve
            # the original record shape so downstream steps don't break.
            import json
            visits = json.loads(config.IP_VISITS_FILE.read_text())
            config.HIGH_INTENT_FILE.write_text(
                json.dumps(visits, indent=2, sort_keys=True)
            )
            logger.info(
                "=== Step 2 SKIPPED (--all-ips): wrote %d IPs to %s ===",
                len(visits), config.HIGH_INTENT_FILE,
            )
        else:
            import filter_intent
            logger.info("=== Step 2: filtering to high-intent IPs ===")
            filter_intent.run()

    if "3" in steps:
        import pdl_client
        logger.info("=== Step 3: enriching with People Data Labs ===")
        pdl_client.run(limit=args.limit)

    if "4" in steps:
        import hubspot_client
        logger.info("=== Step 4: writing companies to HubSpot ===")
        hubspot_client.run(dry_run=args.dry_run, limit=args.limit)

    if "5" in steps:
        import role_filter
        logger.info("=== Step 5: qualifying visitors by inferred role ===")
        role_filter.run()

    if "6" in steps:
        import person_lookup
        logger.info("=== Step 6: PDL Person Search for peers at ICP companies ===")
        person_lookup.run(limit=args.limit)

    if "7" in steps:
        import contact_upsert
        logger.info("=== Step 7: writing contacts to HubSpot ===")
        contact_upsert.run(dry_run=args.dry_run, limit=args.limit)

    logger.info("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
