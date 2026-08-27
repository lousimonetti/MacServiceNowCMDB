"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .config import Config
from .errors import ConfigError, SyncError
from .graph import GraphClient
from .logging_setup import configure_logging
from .models import RunReport
from .servicenow.client import ServiceNowClient
from .sync import SyncRunner

log = logging.getLogger("intune_cmdb_sync")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_FAILED = 3
EXIT_PARTIAL = 4  # ran, but did not do its whole job


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intune-cmdb-sync",
        description=(
            "Sync corporate-owned Microsoft Intune devices into the ServiceNow CMDB "
            "through the Identification and Reconciliation Engine."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Read from Intune and run IRE identification against ServiceNow without "
            "committing anything. Overrides DRY_RUN=false."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and connectivity to both systems, then exit.",
    )
    parser.add_argument("--log-level", help="Override LOG_LEVEL (DEBUG, INFO, WARNING, ERROR).")
    parser.add_argument("--log-format", choices=("json", "text"), help="Override LOG_FORMAT.")
    parser.add_argument(
        "--report",
        help="Write the JSON run report to this path. Overrides RUN_REPORT_PATH.",
    )
    parser.add_argument(
        "--report-devices",
        action="store_true",
        help="Include the per-device outcome list in the JSON report.",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit non-zero if any individual device failed to write.",
    )
    return parser


def _apply_overrides(args: argparse.Namespace) -> None:
    """CLI flags win over environment variables."""
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    if args.log_level:
        os.environ["LOG_LEVEL"] = args.log_level
    if args.log_format:
        os.environ["LOG_FORMAT"] = args.log_format
    if args.report:
        os.environ["RUN_REPORT_PATH"] = args.report


def _write_report(report: RunReport, path: Path | None, include_devices: bool) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.as_dict(include_outcomes=include_devices), indent=2),
            encoding="utf-8",
        )
        log.info("wrote run report", extra={"path": str(path)})
    except OSError as exc:
        log.error("could not write run report", extra={"path": str(path), "error": str(exc)})


def _check(cfg: Config) -> int:
    with ServiceNowClient(cfg.servicenow) as snow:
        identity = snow.verify_connectivity()
        log.info("ServiceNow reachable", extra={"instance": identity.get("instance")})

    with GraphClient(cfg.graph) as graph:
        devices = graph.iter_managed_devices()
        first = next(devices, None)
        log.info(
            "Microsoft Graph reachable",
            extra={
                "sample_device": (first or {}).get("deviceName"),
                "found_any": first is not None,
            },
        )
    log.info("configuration and connectivity check passed")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _apply_overrides(args)

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        configure_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FORMAT", "text"))
        log.error("%s", exc)
        return EXIT_CONFIG

    configure_logging(cfg.runtime.log_level, cfg.runtime.log_format)
    log.info(
        "starting intune-cmdb-sync",
        extra={
            "version": __version__,
            "dry_run": cfg.runtime.dry_run,
            "write_mode": cfg.servicenow.write_mode,
            "ownership": cfg.graph.ownership,
            "graph_auth": cfg.graph.auth_mode,
            "snow_auth": cfg.servicenow.auth_mode,
        },
    )

    try:
        if args.check:
            return _check(cfg)

        with GraphClient(cfg.graph) as graph, ServiceNowClient(cfg.servicenow) as snow:
            runner = SyncRunner(cfg, graph=graph, snow=snow)
            try:
                report = runner.run()
            except SyncError as exc:
                # Write the partial report before giving up. A failed run is
                # exactly when the per-device detail is worth having, and
                # returning here without it used to throw it away.
                log.error("sync failed", extra={"error": str(exc)})
                partial = runner.report
                partial.degrade(f"run aborted: {exc}")
                partial.finished_at = datetime.now(UTC)
                _write_report(partial, cfg.runtime.run_report_path, args.report_devices)
                return EXIT_FAILED
    except SyncError as exc:
        # Raised while building the clients, before there is a runner or report.
        log.error("sync failed", extra={"error": str(exc)})
        return EXIT_FAILED
    except KeyboardInterrupt:  # pragma: no cover
        log.warning("interrupted")
        return EXIT_FAILED

    _write_report(report, cfg.runtime.run_report_path, args.report_devices)

    summary = report.summary()
    log.info("run complete", extra=summary)
    for warning in report.warnings:
        log.warning("%s", warning)

    if report.degraded:
        # A tripped guard or a lost state file means the next run cannot be
        # trusted to behave correctly. Exiting 0 hides that from the scheduler.
        log.error(
            "run completed in a degraded state",
            extra={"conditions": len(report.degraded)},
        )
        return EXIT_PARTIAL
    if report.errors and args.fail_on_error:
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
